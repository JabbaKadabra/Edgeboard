# Xeneon Edge Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `xdash`, a localhost web dashboard for a 2560×720 Corsair Xeneon Edge showing Claude usage, Claude Code sessions, Spotify, and system metrics.

**Architecture:** One Python process (FastAPI + uvicorn) runs asyncio collector loops that fill an in-memory `State`; the browser receives the snapshot over Server-Sent Events once a second and renders it with vanilla JS. Pure parsing functions are separated from I/O so they are unit-testable without disk, D-Bus, or network.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, httpx, psutil, pytest; vanilla HTML/CSS/JS; playerctl; Chromium kiosk; systemd user units.

**Spec:** `docs/superpowers/specs/2026-09-03-xeneon-dashboard-design.md`

## Global Constraints

- Linux / Arch only. No Windows or macOS code paths.
- Server binds `127.0.0.1:8765` by default.
- No frontend build step, no web fonts, no CDN assets (kiosk may be offline).
- Every collector isolates its own failures; the page must never go blank because one source is missing.
- Pure functions take strings/dicts and return dataclasses; I/O wrappers stay thin.
- Commit after each task.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `xdash/__init__.py`, `xdash/__main__.py`, `xdash/config.py`, `tests/test_config.py`, `.gitignore`

**Interfaces:**
- Produces: `Settings` dataclass with `claude_dir: Path`, `host: str`, `port: int`, `spotify_player: str`, `system_interval: float`, `sessions_interval: float`, `usage_interval: float`, `timeline_interval: float`, `done_sessions_limit: int`; `Settings.from_env(env: Mapping[str,str]) -> Settings`.

- [ ] Test: `from_env({})` gives defaults (`~/.claude`, 127.0.0.1, 8765, "spotify"); `from_env({"XDASH_PORT":"9000","XDASH_CLAUDE_DIR":"/x"})` overrides.
- [ ] Implement `config.py`; `__main__` calls `xdash.server.main()`.
- [ ] `pytest -q` green. Commit `feat: scaffold xdash package`.

### Task 2: Transcript parsing

**Files:**
- Create: `xdash/collectors/__init__.py`, `xdash/collectors/claude_transcripts.py`, `tests/test_transcripts.py`, `tests/fixtures.py` (helpers building JSONL lines).

**Interfaces:**
- Produces: `iter_entries(text: str) -> Iterator[dict]`; `UsageEvent(ts: datetime, model: str, input: int, output: int, cache_read: int, cache_write: int)`; `usage_events(entries: Iterable[dict]) -> list[UsageEvent]`; `SessionFacts(title, cwd, branch, model, last_kind, last_stop_reason, context_tokens, first_ts, last_ts, assistant_messages)`; `session_facts(entries) -> SessionFacts`; `read_tail(path: Path, max_bytes: int = 256_000) -> str`; `short_model(name: str) -> str`; `clean_prompt(text: str) -> str`.

- [ ] Tests: malformed line skipped; duplicate `message.id` counted once with last values; title from `summary` beats first prompt; `<system-reminder>` stripped and truncated to 60 chars; `short_model("claude-fable-5-1") == "fable-5-1"`, `short_model("claude-haiku-4-5-20251001") == "haiku-4-5"`; context tokens = input+cache_read+cache_write of last assistant; `last_kind` is `"user_prompt"`, `"tool_result"`, or `"assistant"`.
- [ ] Implement; tests green. Commit `feat: parse Claude Code transcripts`.

### Task 3: Session discovery and status

**Files:**
- Create: `xdash/collectors/claude_sessions.py`, `tests/test_sessions.py`

**Interfaces:**
- Consumes: Task 2 functions.
- Produces: `classify(facts: SessionFacts, alive: bool) -> tuple[str, str]` (status, detail) per the spec table; `Session` dataclass (`id, name, project, cwd, branch, model, status, detail, context_tokens, started_at, last_activity, messages`); `find_transcript(claude_dir, session_id) -> Path | None`; `collect_sessions(settings, now, pid_alive=os_pid_alive) -> tuple[list[Session], dict]` where the dict is `{"today": n, "done": n, "working": n}`.

- [ ] Tests for all five rows of the status table; `collect_sessions` with a temp `claude_dir` containing one live session json (pid alive faked True) and one stale transcript modified today (done).
- [ ] Implement; commit `feat: discover Claude Code sessions`.

### Task 4: Usage windows and timeline

**Files:**
- Create: `xdash/collectors/claude_usage.py`, `tests/test_usage.py`

**Interfaces:**
- Produces: `parse_usage_response(data: dict, now: datetime) -> list[UsageWindow]` with `UsageWindow(key, label, utilization: float|None, resets_at: datetime|None, seconds_to_reset: int|None)`; `local_windows(events, now) -> list[UsageWindow]` (five_hour, seven_day, tokens in `tokens` field, utilization None); `today_totals(events, now, tz) -> TodayTotals(output, input, cache_read, cache_write, messages)`; `timeline(events, now, hours=24) -> list[TimelineBucket(hour_start, tokens)]`; `load_token(claude_dir) -> str | None`; `async fetch_usage(client, token) -> dict`; `load_all_events(claude_dir, since: datetime) -> list[UsageEvent]`.

- [ ] Tests: API dict with `five_hour`, `seven_day`, `seven_day_fable` → 3 windows with labels "5-hour", "Weekly", "Fable weekly"; nulls skipped; local windows reset = first event + 5h; timeline has 24 buckets, event at now lands in last bucket; today totals exclude yesterday.
- [ ] Implement; commit `feat: Claude usage windows, totals, timeline`.

### Task 5: Spotify via playerctl

**Files:**
- Create: `xdash/collectors/spotify.py`, `tests/test_spotify.py`

**Interfaces:**
- Produces: `parse_metadata(output: str) -> SpotifyState(running=True, status, title, artist, album, art_url, length_s, position_s, shuffle)`; `FORMAT` template string; `read_spotify(runner, player) -> SpotifyState` where `runner(args: list[str]) -> tuple[int, str]`; `control(runner, player, action) -> bool` for actions `play_pause|next|previous`.

- [ ] Tests: sample output parses; nonzero exit → `running=False`; invalid action raises `ValueError`.
- [ ] Implement with `subprocess.run` default runner, 2 s timeout, `FileNotFoundError` → running False, `available=False`.
- [ ] Commit `feat: Spotify state and controls via playerctl`.

### Task 6: System metrics

**Files:**
- Create: `xdash/collectors/system.py`, `tests/test_system.py`

**Interfaces:**
- Produces: `pick_cpu_temp(temps: dict[str, list]) -> float | None` (prefers k10temp Tctl, coretemp Package id 0, zenpower, else first); `parse_nvidia_smi(line: str) -> GpuState`; `read_amd_gpu(sysfs_root: Path) -> GpuState | None`; `SystemSampler` class with `sample() -> dict` holding ring buffers (`deque(maxlen=120)`) for cpu, gpu, rx, tx.

- [ ] Tests: temp preference order with fake psutil-shaped structures; nvidia csv line parse; AMD sysfs from tmp dir; sampler produces `net.rx_bps` >= 0 on second call.
- [ ] Commit `feat: system metrics collector`.

### Task 7: State and server

**Files:**
- Create: `xdash/state.py`, `xdash/server.py`, `tests/test_server.py`

**Interfaces:**
- Produces: `State` with fields `usage, sessions, sessions_summary, spotify, system, errors` and `snapshot() -> dict` (JSON-safe, adds `now`); `create_app(settings, state=None, spotify_runner=None, start_collectors=True) -> FastAPI`; routes from spec; `main()` runs uvicorn.

- [ ] Tests with `TestClient(create_app(settings, start_collectors=False))`: `/api/state` has keys; `/api/spotify/next` invokes fake runner with `["playerctl","-p","spotify","next"]`; unknown action 404; `/` returns HTML.
- [ ] Implement collectors loop (`asyncio.create_task` per collector in lifespan, `run_in_executor` for blocking work), SSE generator.
- [ ] Commit `feat: FastAPI server with SSE snapshot stream`.

### Task 8: Frontend

**Files:**
- Create: `xdash/static/index.html`, `xdash/static/style.css`, `xdash/static/app.js`

- [ ] Layout per spec (4 columns), components: brand+mascot+clock, limits bars, today counters, 24 h histogram, session cards, Spotify card with controls, system meters + sparkline, disconnected badge.
- [ ] Manual check with a fake-state mode: `XDASH_DEMO=1` makes the server serve a canned snapshot so the page can be previewed anywhere (implemented in `xdash/demo.py`).
- [ ] Screenshot at 2560×720 with Playwright to verify layout; commit `feat: dashboard frontend`.

### Task 9: Deployment files and README

**Files:**
- Create: `scripts/kiosk.sh`, `systemd/xdash.service`, `systemd/xdash-kiosk.service`, `README.md` (replace stub)

- [ ] Kiosk script with `XDASH_DISPLAY_OFFSET`, `XDASH_URL`; systemd units using `%h/Dashboard/.venv/bin/python -m xdash`; README install steps for Arch.
- [ ] Commit `docs: deployment and README`.
