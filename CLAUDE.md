# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`edgeboard`: a single-process FastAPI dashboard for a Corsair Xeneon Edge (2560×720 touch panel) on Arch Linux. It shows Claude usage limits, live Claude Code sessions, Spotify (via MPRIS/playerctl), and system metrics. Vanilla JS frontend, no build step. The approved design spec is `docs/superpowers/specs/2026-09-03-xeneon-dashboard-design.md`; keep behaviour changes consistent with it (or update it).

## Commands

```sh
# setup (uv may not be installed; plain venv works)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
# or: uv venv && uv pip install -e ".[dev]"

.venv/bin/pytest                                  # full suite (~1 s)
.venv/bin/pytest tests/test_sessions.py           # one file
.venv/bin/pytest tests/test_sessions.py -k classify   # one test

.venv/bin/python -m edgeboard                         # http://127.0.0.1:8765
EDGEBOARD_DEMO=1 .venv/bin/python -m edgeboard            # canned data, no Claude/Spotify/sensors needed
```

The cursor is hidden only with `?kiosk=1` in the URL (added by `scripts/kiosk.sh`); append `?debug` to get it back in the kiosk. All config is `EDGEBOARD_*` env vars (see `edgeboard/config.py` and the README table), optionally from a `.env` in the working directory (`EDGEBOARD_ENV_FILE` to relocate; real env vars win; `.env.example` lists every key, `.env` is git-ignored). Both systemd units load the same file via `EnvironmentFile`. No secrets are needed: the OAuth token comes from `~/.claude/.credentials.json`. No linter or formatter is configured.

## Architecture

Data flow: collectors poll sources on their own asyncio loops → write dicts into one shared `State` object (`edgeboard/state.py`) → the web layer serves `State.snapshot()` via `GET /api/state` and pushes it once per second over SSE at `GET /api/events`. The browser (`edgeboard/static/app.js`) only renders snapshots; `POST /api/spotify/{action}` is the sole write path.

**`edgeboard/server.py`** holds both the `Collectors` class (one `_loop` per source, intervals from `Settings`) and `create_app()`. Every collector exception is caught in `_loop`, logged once per distinct message, and surfaced as `state.errors[<panel>]`. The `usage` and `timeline` loops both feed the usage panel, so their errors merge into `errors["usage"]`. Blocking work runs via `run_in_executor`; collectors themselves are sync functions.

**Pure parsing vs I/O.** `collectors/claude_transcripts.py` is pure (strings/dicts in, dataclasses out) and is what most tests target. `claude_sessions.py` and `claude_usage.py` wrap it with filesystem/network access. Keep new logic in the pure layer so tests stay fixture-only; `tests/fixtures.py` builds synthetic transcript lines (`user_line`, `assistant_line`, `summary_line`).

**Claude Code transcript facts to preserve:**
- Transcripts are `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`; subagent transcripts live under `<project>/<session>/subagents/*.jsonl` and count toward usage.
- The same assistant `message.id` is written several times while streaming; usage must be de-duplicated keeping the last occurrence.
- "Burn" (tokens counted against limits) = input + output + cache_write, excluding cache reads. Context size = last assistant input + cache_read + cache_write.
- Session title = latest `summary` line, else first user prompt with paired tags like `<system-reminder>` stripped, truncated to 60 chars. Entries with `isSidechain` or user `isMeta` are ignored.

**Session discovery and status** (`claude_sessions.py`): live sessions come from `~/.claude/sessions/<pid>.json`, verified against `/proc/<pid>/cmdline` containing "claude" (pids get reused). Transcripts modified today with no process are "done", capped at `done_sessions_limit` with the overflow still counted in the summary. After sorting (working, idle, done) only `sessions_shown` (default 4) sessions are returned; the summary still counts all of them. Headless transcripts (`claude -p`) count as working only if written in the last 60 s and the tail says Claude is mid-turn. Status comes from the `classify()` table (last entry kind + stop_reason + alive). Both this module and `claude_usage.py` keep module-level caches keyed by `(mtime_ns, size)`; sessions parse transcripts incrementally from the last byte offset, so append-only assumptions matter.

**Usage limits** (`claude_usage.py`): primary source is Claude's OAuth usage endpoint using the token from `~/.claude/.credentials.json` with the `anthropic-beta: oauth-2025-04-20` header. Only the `five_hour` and `seven_day` windows are kept (`SHOWN_WINDOWS`); per-model and extra-usage windows are deliberately dropped. Without a token the panel falls back to local transcript sums (`source: "local"`, no percentages). A 401 forces a token re-read since Claude Code may have refreshed it.

**Spotify** (`spotify.py`): one `playerctl metadata --format` call with a `\x1f`-separated template. The `Runner` callable is injectable, which is how `test_server.py` and `test_spotify.py` avoid D-Bus. Demo mode must never invoke a real player (the server special-cases it).

**Spotify queue** (`spotify_queue.py`): MPRIS has no queue, so "up next" comes from the Web API's `/me/player/queue`, optional and off until `scripts/spotify_auth.py` (PKCE, no secret) writes the token file. `QueueClient` refreshes the access token, persists a rotated refresh token, and takes an injectable `httpx.Client`; `parse_queue` is the pure part. The `queue` loop only fetches while MPRIS says a player is running, and its errors merge into `errors["spotify"]`. Snapshot key: `spotify_queue = {configured, tracks}`.

**System** (`system.py`): psutil + hwmon; CPU temp chosen by the `CPU_SENSOR_PREFERENCE` order; GPU via `nvidia-smi` or AMD sysfs, else `null`. 120-sample ring buffers feed the sparklines.

**Frontend**: fixed four-column CSS grid sized for 2560×720 but fluid enough to preview in a normal window: clock | limits + today + system stats with a cpu/gpu trace | a 2×2 grid of session cards | Spotify with the play queue. Session cards keep their DOM position between snapshots (update in place, don't re-create). JetBrains Mono is vendored as woff2 in `edgeboard/static/fonts/` (no network fonts; the kiosk may be offline). Theme: tmux-style panes on a near-black Dracula palette with amber for Claude. There is no status bar; collector errors show in red under the clock. The mascot is a tap target for a client-only pomodoro (25 min focus, 5 min coffee-cup break, countdown above the clock); it is not part of the snapshot.

## Deployment

`systemd/edgeboard.service` runs the server from `%h/Dashboard/.venv`; `systemd/edgeboard-kiosk.service` runs `scripts/kiosk.sh`, which waits for `/api/state` then launches Chromium with `--kiosk --class=edgeboard`. Paths assume the repo is checked out at `~/Dashboard`. The server has no auth; keep `EDGEBOARD_HOST` on loopback.
