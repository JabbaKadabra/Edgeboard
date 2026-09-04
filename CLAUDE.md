# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`edgeboard`: a single-process FastAPI dashboard for a Corsair Xeneon Edge (2560×720 touch panel) on Arch Linux. It shows Claude usage limits, live Claude Code sessions, Spotify (via MPRIS/playerctl), and system metrics. Vanilla JS frontend, no build step. The approved design spec is `docs/superpowers/specs/2026-09-03-xeneon-dashboard-design.md`; keep behaviour changes consistent with it (or update it).

## Commands

```sh
# setup (uv may not be installed; plain venv works)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
# or: uv venv && uv pip install -e ".[dev]"

.venv/bin/pytest                                  # full suite (~1 s; +3 s when Playwright is installed)
.venv/bin/pytest -m browser                       # tests/test_page.py only: demo page in Chromium at 2560×720
.venv/bin/pytest tests/test_sessions.py           # one file
.venv/bin/pytest tests/test_sessions.py -k classify   # one test

.venv/bin/python -m edgeboard                         # http://127.0.0.1:8765
EDGEBOARD_DEMO=1 .venv/bin/python -m edgeboard            # canned data, no Claude/Spotify/sensors needed
```

The cursor is hidden only with `?kiosk=1` in the URL (added by `scripts/kiosk.sh`); append `?debug` to get it back in the kiosk. All config is `EDGEBOARD_*` env vars (see `edgeboard/config.py` and the README table), optionally from a `.env` in the working directory (`EDGEBOARD_ENV_FILE` to relocate; real env vars win; `.env.example` lists every key, `.env` is git-ignored). Both systemd units load the same file via `EnvironmentFile`. No secrets are needed: the OAuth token comes from `~/.claude/.credentials.json`. Lint with `.venv/bin/ruff check .` (pyflakes, pycodestyle errors, isort, bugbear; `pyproject.toml`); CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push. No formatter is configured.

## Architecture

Data flow: collectors poll sources on their own asyncio loops → write dicts into one shared `State` object (`edgeboard/state.py`) → the web layer serves `State.snapshot()` via `GET /api/state` and pushes it once per second over SSE at `GET /api/events`. The browser (`edgeboard/static/app.js`) only renders snapshots; the write paths are `POST /api/spotify/{action}`, `POST /api/sessions/{id}/answer` (answers a pending `AskUserQuestion`) and `POST /api/sessions/{id}/send` (a prompt into the session's inbox socket). Every `/api/*` request passes `request_allowed()` (server.py): the `Host` header and, when a browser sends one, the `Origin` must name loopback or the configured bind address, otherwise 403; this keeps other web pages on the machine (cross-site "simple requests", DNS rebinding) off the API. `/api/hook` additionally requires `application/json`. Tests use `TestClient(app, base_url="http://127.0.0.1:8765")` for that reason. The snapshot's `version` is `build_id()` (`<version>+<hash of index.html/app.js/style.css>`); the page reloads when it changes and the asset links carry it as `?v=` (the `__BUILD__` placeholder in `index.html`), so a deploy under a weeks-old kiosk page takes effect on its own.

**`edgeboard/server.py`** holds both the `Collectors` class (one `_loop` per source, intervals from `Settings`) and `create_app()`. Every collector exception is caught in `_loop`, logged once per distinct message, and surfaced as `state.errors[<panel>]`. The `usage` and `timeline` loops both feed the usage panel, so their errors merge into `errors["usage"]`. Blocking work runs via `run_in_executor`; collectors themselves are sync functions.

**Pure parsing vs I/O.** `collectors/claude_transcripts.py` is pure (strings/dicts in, dataclasses out) and is what most tests target. `claude_sessions.py` and `claude_usage.py` wrap it with filesystem/network access. Keep new logic in the pure layer so tests stay fixture-only; `tests/fixtures.py` builds synthetic transcript lines (`user_line`, `assistant_line`, `summary_line`).

**Claude Code transcript facts to preserve:**
- Transcripts are `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`; subagent transcripts live under `<project>/<session>/subagents/*.jsonl` and count toward usage.
- The same assistant `message.id` is written several times while streaming; usage must be de-duplicated keeping the last occurrence.
- "Burn" (tokens counted against limits) = input + output + cache_write, excluding cache reads. Context size = last assistant input + cache_read + cache_write.
- Session title = latest `summary` line, else first user prompt with paired tags like `<system-reminder>` stripped, truncated to 60 chars. Entries with `isSidechain` or user `isMeta` are ignored. `last_prompt` is the most recent user prompt (300 chars); `last_tool`/`last_tool_hint` come from the last `tool_use` block of the last assistant message (`tool_hint()` is shared with the hook path).

**Session discovery and status** (`claude_sessions.py`): live sessions come from `~/.claude/sessions/<pid>.json`, verified against `/proc/<pid>/cmdline` containing "claude" (pids get reused). Transcripts modified today with no process are "done", capped at `done_sessions_limit` with the overflow still counted in the summary. After sorting (working, idle, done) only `sessions_shown` (default 4) sessions are returned; the summary still counts all of them. Headless transcripts (`claude -p`) count as working only if written in the last 60 s and the tail says Claude is mid-turn. Status comes from the `classify()` table (last entry kind + stop_reason + alive + active subagents); `tool_detail()` phrases the running tool (`running ls -la`, `editing server.py`). Subagents are counted by `subagent_activity()` (`<project>/<session>/subagents/**/*.jsonl`, active = written in the last 60 s). Claude Code hooks may `POST /api/hook`; `State.hooks` keeps the latest event per session and `apply_hook()` lets it override the transcript status while alive, younger than `HOOK_TTL` and newer than the transcript's last line. That is where the fourth status `attention` (permission prompt, question) comes from; it sorts first. `hook_applies()` is the shared freshness rule; the same fresh hook also feeds `question` (`question_from_hook()`, null once `question_state` is `answered`/`abandoned`), `last_reply` (`Stop`'s `last_assistant_message`, else the transcript's last assistant text) and `waiting_since`. The pid file also gives `session_name` and `messagingSocketPath` (`can_send`).

**Answering and sending** (`answers.py`, `collectors/claude_inbox.py`, `scripts/edgeboard-hook.py`): the hook script replaces the old bare-curl hook; for a `PreToolUse` of `AskUserQuestion` it long-polls `GET /api/answer/{tool_use_id}` for `EDGEBOARD_ANSWER_WAIT` (90 s) and returns the panel's answers as `updatedInput.answers` (keyed by question text), which makes Claude Code skip the terminal dialog; no output means the terminal asks. `Answers` (memory only) holds the pending entries and flags `question_state` in `State.hooks` (it needs the same dict object, so the sessions loop prunes in place). `find_inbox()`/`send_message()` post `{"type":"auth"}` + `{"type":"user",...}` JSON lines into the session's Unix socket; slash commands do not run that way, so presets (`EDGEBOARD_PRESETS`, `label=text|…`) are instructions. Demo mode never opens a socket. Both this module and `claude_usage.py` keep module-level caches keyed by `(mtime_ns, size)` and parse transcripts incrementally from the last byte offset (`read_new_lines()` in the pure module feeds `SessionParser` / `UsageParser` only the complete lines appended since; a file that shrank is re-parsed), so append-only assumptions matter.

**Attention alerts**: `attention_transitions(previous_statuses, sessions)` (pure, in `claude_sessions.py`) flags sessions that went `working → idle` or `→ attention`; the sessions loop feeds it to an injectable `notifier` (`notify-send`) when `alert_notify` is set. The browser does the same detection per session id (`needsYou` in `app.js`) for the card flash/highlight, the mascot's arms-up pose and the opt-in chime; `snapshot()["settings"]` carries the `alert_sound` flag.

**Usage limits** (`claude_usage.py`): primary source is Claude's OAuth usage endpoint using the token from `~/.claude/.credentials.json` with the `anthropic-beta: oauth-2025-04-20` header. Only the `five_hour` and `seven_day` windows are kept (`SHOWN_WINDOWS`); per-model and extra-usage windows are deliberately dropped. Without a token the panel falls back to local transcript sums (`source: "local"`, no percentages). A 401 forces a token re-read since Claude Code may have refreshed it. A 429 (the endpoint is shared with running Claude Code sessions and rate limits bursts) keeps the last value marked stale and backs off (interval doubles per consecutive 429, capped at `USAGE_BACKOFF_MAX`); it only becomes a visible error after `USAGE_STALE_AFTER`. A collector may return a float from its poll to override the next sleep in `_loop`. Each window also carries `rate_per_hour` / `projected_full_at` from `project_window()` (pure: least-squares over the samples since the last utilization drop, `None` when flat); `Collectors` keeps the last 30 samples per window key.

**Spotify** (`spotify.py`): one `playerctl metadata --format` call with a `\x1f`-separated template. The `Runner` callable is injectable, which is how `test_server.py` and `test_spotify.py` avoid D-Bus. Demo mode must never invoke a real player (the server special-cases it).

**Spotify queue** (`spotify_queue.py`): MPRIS has no queue, so "up next" comes from the Web API's `/me/player/queue`, optional and off until `scripts/spotify_auth.py` (PKCE, no secret) writes the token file. `QueueClient` refreshes the access token, persists a rotated refresh token, and takes an injectable `httpx.Client`; `parse_queue` is the pure part. The `queue` loop only fetches while MPRIS says a player is running, and its errors merge into `errors["spotify"]`. Snapshot key: `spotify_queue = {configured, tracks}`.

**System** (`system.py`): psutil + hwmon; CPU temp chosen by the `CPU_SENSOR_PREFERENCE` order; GPU via `nvidia-smi` or AMD sysfs, else `null`. 120-sample ring buffers of CPU and GPU % feed the trace (`history` carries only what the page draws; net rates are current values).

**Frontend**: fixed four-column CSS grid sized for 2560×720 but fluid enough to preview in a normal window: clock | limits + today + system stats with a cpu/gpu trace | a 2×2 grid of session cards | Spotify with the play queue. Session cards and the Limits rows keep their DOM nodes between snapshots (update in place, don't re-create). Tapping a card opens the detail overlay in `index.html`, refilled from each snapshot and closed on a backdrop tap or after 20 s. JetBrains Mono is vendored as woff2 in `edgeboard/static/fonts/` (no network fonts; the kiosk may be offline). Theme: tmux-style panes on a near-black Dracula palette with amber for Claude. There is no status bar; collector errors show in red under the clock. The mascot is a tap target for a client-only pomodoro (25 min focus, 5 min coffee-cup break, countdown above the clock); it is not part of the snapshot.

## Deployment

`systemd/edgeboard.service` runs the server from `%h/Dashboard/.venv`; `systemd/edgeboard-kiosk.service` runs `scripts/kiosk.sh`, which waits for `/api/state` then launches Chromium with `--kiosk --class=edgeboard`. Paths assume the repo is checked out at `~/Dashboard`. The server has no auth; keep `EDGEBOARD_HOST` on loopback (the origin guard admits loopback and the configured host only). After a deploy, restart `edgeboard.service`; the kiosk page reloads itself when the build id changes.
