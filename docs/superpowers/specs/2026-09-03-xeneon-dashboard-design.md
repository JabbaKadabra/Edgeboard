# Xeneon Edge dashboard — design

Date: 2026-09-03
Status: approved-by-assumption (autonomous session; user could not answer questions mid-task).
Assumptions are listed at the end. Revisit any of them if they turn out wrong.

## Goal

A single-page dashboard, rendered full-screen in Chromium kiosk mode on a
Corsair Xeneon Edge (2560×720 touch panel) attached to an EndeavourOS
(Arch Linux) machine. It shows, live:

1. Claude usage — 5-hour and weekly windows with % used and time until reset,
   today's token totals, and a 24-hour usage timeline.
2. Active Claude Code sessions — one card per session with title, project,
   branch, model, context size, and working / idle / done state.
3. Spotify — current track with album cover, progress, and touch controls for
   previous / play-pause / next.
4. System performance — CPU load + temperature, GPU load + temperature,
   memory, disk, network throughput, with short history sparklines.

Visual direction follows the reference photo: dark background, orange
accent, monospace type, pixel-art mascot, big clock, card grid of sessions.

## Non-goals

- Other operating systems. Linux only, tested against Arch conventions.
- Spotify Web API integration. Local MPRIS (the desktop client) is enough.
- Multi-user or remote access. The server binds to localhost only.
- Persisting history across restarts. Everything is recomputed from the
  Claude Code transcript files on disk.

## Architecture

```
┌──────────────┐  poll   ┌──────────────────────────────┐   SSE   ┌────────────────┐
│ ~/.claude/*  │◄────────│                              │────────►│                │
│ playerctl    │◄────────│  xdash (FastAPI + uvicorn)   │  JSON   │ Chromium kiosk │
│ psutil/sysfs │◄────────│  background collectors       │◄────────│ static HTML/JS │
│ usage API    │◄────────│  in-memory state snapshot    │  POST   │ (touch UI)     │
└──────────────┘         └──────────────────────────────┘         └────────────────┘
```

One Python process. Each collector runs as an asyncio task on its own
interval and writes into a shared `State` object. The web layer serves the
static page, a `/api/state` snapshot, a `/api/events` Server-Sent-Events
stream that pushes the snapshot once per second, and a few POST endpoints
for Spotify control. The browser is a thin renderer: no build step, no
framework, vanilla JS updating the DOM in place.

Package layout:

```
xdash/
  __init__.py
  __main__.py            python -m xdash
  config.py              Settings from env vars (paths, port, intervals)
  state.py               State dataclass + to_dict()
  server.py              FastAPI app, SSE, static files, spotify POSTs
  collectors/
    claude_transcripts.py  pure parsing of Claude Code JSONL files
    claude_sessions.py     session discovery + status classification
    claude_usage.py        OAuth usage endpoint + local fallback + timeline
    spotify.py             playerctl wrapper
    system.py              psutil + hwmon + GPU
  static/
    index.html, app.js, style.css
scripts/kiosk.sh
systemd/xdash.service, systemd/xdash-kiosk.service
tests/
```

Pure parsing lives in functions that take strings / dicts and return
dataclasses, so tests need no filesystem, no D-Bus, and no network. Thin
I/O wrappers around them are kept small.

## Components

### Claude transcripts (`claude_transcripts.py`)

Claude Code writes one JSONL file per session at
`~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`. Relevant line types:

- `user` — `message.content` is a string or a list of blocks (`text`,
  `image`, `tool_result`). Carries `cwd`, `gitBranch`, `timestamp`.
- `assistant` — `message.id`, `message.model`, `message.stop_reason`
  (`end_turn` | `tool_use`), `message.usage` with `input_tokens`,
  `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`.
  The same message id is written several times during streaming, so
  usage must be de-duplicated by `message.id` (keep the last occurrence).
- `summary` — `summary` text used as a session title when present.
- Anything else (`attachment`, `queue-operation`, hooks, …) is skipped.

Functions:

- `iter_entries(text) -> Iterator[dict]` — tolerant line parser.
- `usage_events(entries) -> list[UsageEvent]` — de-duplicated
  `(timestamp, model, input, output, cache_read, cache_write)`.
- `session_facts(entries) -> SessionFacts` — title, cwd, branch, model,
  last usage, last message kind / stop reason, first and last timestamps,
  assistant message count.
- `read_tail(path, max_bytes)` — reads only the end of large files for
  status classification; full read only for usage aggregation.

### Sessions (`claude_sessions.py`)

Discovery:

1. `~/.claude/sessions/<pid>.json` lists live interactive sessions
   (`sessionId`, `cwd`, `startedAt`, `name`). A session is *alive* when the
   pid exists in `/proc`.
2. Transcript files modified today whose session id is not alive are
   shown as *done* (limited to the most recent N, default 12).

Status classification from the transcript tail:

| last entry                            | alive | status  | detail                 |
|---------------------------------------|-------|---------|------------------------|
| user prompt (text, no tool_result)    | yes   | working | working on your prompt |
| user tool_result                      | yes   | working | thinking               |
| assistant, stop_reason = tool_use     | yes   | working | running tool           |
| assistant, stop_reason = end_turn     | yes   | idle    | waiting for you        |
| any                                   | no    | done    | finished               |

Title: the latest `summary` line if any, else the first user prompt's first
line with `<system-reminder>` and similar tags stripped, truncated to 60
characters. Project = last path component of `cwd`. Model string is
shortened (`claude-fable-5-1` → `fable-5-1`). Context tokens = last
assistant `input + cache_read + cache_creation`.

Summary counters: sessions today, done today, working now.

### Usage (`claude_usage.py`)

Primary source: `GET https://api.anthropic.com/api/oauth/usage` with the
OAuth bearer token from `~/.claude/.credentials.json`
(`claudeAiOauth.accessToken`) and header `anthropic-beta: oauth-2025-04-20`.
The response is a map of window name → `{utilization, resets_at}`
(`five_hour`, `seven_day`, `seven_day_opus`, …). Parsing is generic: every
top-level key whose value has `utilization` becomes a bar; known keys get
friendly labels, unknown ones are capitalized (`seven_day_fable` → "Fable weekly"). Polled every 60 s; failures
keep the last good value and mark the source stale.

Fallback when no token or the endpoint fails: compute from local
transcripts — tokens in the rolling 5-hour and 7-day windows, reset time =
first event in window + window length. No percentage is shown (the plan's
limits are not known locally); the UI labels this "estimated".

Always from local transcripts:

- Today totals: output, input, cache read, cache write, assistant messages.
- Timeline: 24 hourly buckets of `input + output + cache_write` ("burn"),
  plus the peak bucket for scaling.

### Spotify (`spotify.py`)

Wraps `playerctl -p <player>` (default player `spotify`). One call per
poll: `playerctl metadata --format` with a unit-separator-delimited template
yielding status, title, artist, album, artUrl, length, position, shuffle.
Missing binary or no player → `{running: false}`. Controls run
`playerctl play-pause | next | previous`. Album art URL (https://i.scdn.co)
is loaded by the browser directly.

### System (`system.py`)

- CPU: `psutil.cpu_percent(percpu=True)`, `cpu_freq`, temperature from
  `psutil.sensors_temperatures()` preferring `k10temp Tctl`, `coretemp
  Package id 0`, `zenpower`, then the first sensor found.
- Memory: `virtual_memory`.
- Disk: `disk_usage` for `/` and `/home` if separately mounted.
- Network: byte deltas between polls → bytes/s.
- GPU: `nvidia-smi` query if available; else AMD sysfs
  (`/sys/class/drm/card*/device/gpu_busy_percent`, hwmon `temp1_input`,
  `mem_info_vram_used/total`); else `null`.
- History: ring buffers (120 samples) of CPU %, GPU %, and net rates for
  sparklines.

### Server (`server.py`)

- `GET /` static index; `/static/*` assets.
- `GET /api/state` → full snapshot JSON.
- `GET /api/events` → SSE, one `state` event per second, keep-alive comment
  every 15 s.
- `POST /api/spotify/{play_pause|next|previous}` → runs the command, returns
  the fresh spotify block.
- Binds `127.0.0.1:8765` by default (`XDASH_HOST`, `XDASH_PORT`).

### Frontend

Fixed 2560×720 layout via CSS grid, but fluid enough to preview in a normal
browser window. Four columns:

1. Brand + pixel mascot + big clock + date (left, 300 px).
2. Limits (progress bars with % and "resets in"), Today counters, 24 h burn
   histogram, one-line system summary (620 px).
3. Session cards grid (flexible).
4. Spotify card (album art, track, progress, three 64 px touch buttons) over
   system meters (CPU/GPU/MEM/DISK bars, net rates, CPU sparkline) (380 px).

Colours: background `#0b0d12`, panel `#141821`, accent orange `#ff9f1c`,
working yellow `#ffd23f`, idle grey `#8b93a7`, done purple `#a78bfa`, text
`#e8eaf0`. Monospace font stack, no web fonts (kiosk may be offline).

The page reconnects the SSE stream automatically and shows a "disconnected"
badge if no event arrives for 5 s. Mascot blinks periodically and bounces
while any session is working.

## Error handling

- Every collector catches its own exceptions, logs once per distinct
  error, and leaves the previous value in place with an `error` field so
  the UI can grey out that panel instead of the whole page failing.
- Missing optional tools (`playerctl`, `nvidia-smi`, credentials file) are
  detected once and reported in the snapshot as `available: false`.
- Malformed JSONL lines are skipped.

## Testing

- `pytest` unit tests for all pure functions with synthetic fixtures:
  transcript parsing and de-duplication, status classification table,
  usage response parsing (known and unknown windows), local window
  fallback, timeline bucketing, playerctl output parsing, temperature
  sensor selection, model-name shortening.
- Server smoke test with `TestClient`: `/api/state` returns the schema,
  `/api/spotify/next` calls the injected runner.
- Manual: run `python -m xdash` on the target machine and open the page.

## Deployment (Arch)

```
sudo pacman -S --needed python uv playerctl chromium
uv venv && uv pip install -e .   # creates .venv from pyproject
systemctl --user enable --now xdash.service xdash-kiosk.service
```

`scripts/kiosk.sh` launches Chromium in kiosk mode with
`--window-position` set from `XDASH_DISPLAY_OFFSET` (the X offset of the
Xeneon Edge in the desktop layout) and `--window-size=2560,720`.

## Assumptions made without the user

1. Backend in Python (FastAPI); frontend without a build step. Chosen for
   easiest maintenance on a single Arch box.
2. Spotify via the desktop client's MPRIS interface (playerctl), not the
   Web API. If the user listens through a browser tab, the player name is
   configurable.
3. Claude limits come from the same OAuth usage endpoint Claude Code's
   `/usage` command uses; credentials are read from
   `~/.claude/.credentials.json`. If that file lives elsewhere, `XDASH_CLAUDE_DIR`
   overrides the base directory.
4. The panel runs at 2560×720 and is driven by Chromium in kiosk mode.
5. GPU may be NVIDIA or AMD; both paths are implemented, Intel shows nothing.
