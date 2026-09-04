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
5. Git — today's commits across the repositories the sessions work in, with
   the added / deleted line totals, and per session the commits it made.

Visual direction: a terminal-multiplexer look (tmux-style panes with the
title cut into the border, pipe-style progress bars) on a near-black Dracula palette with amber as the Claude highlight,
JetBrains Mono, pixel-art mascot, big clock, card grid of sessions.

## Non-goals

- Other operating systems. Linux only, tested against Arch conventions.
- Spotify Web API for playback control. MPRIS does that; the Web API is used
  only, and optionally, for the play queue (see Spotify below).
- Multi-user or remote access. The server binds to localhost only.
- Persisting history across restarts. Everything is recomputed from the
  Claude Code transcript files on disk.

## Architecture

```
┌──────────────┐  poll   ┌──────────────────────────────┐   SSE   ┌────────────────┐
│ ~/.claude/*  │◄────────│                              │────────►│                │
│ playerctl    │◄────────│  edgeboard (FastAPI + uvicorn)   │  JSON   │ Chromium kiosk │
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
edgeboard/
  __init__.py
  __main__.py            python -m edgeboard
  config.py              Settings from env vars (paths, port, intervals)
  state.py               State dataclass + to_dict()
  server.py              FastAPI app, SSE, static files, spotify POSTs
  collectors/
    claude_transcripts.py  pure parsing of Claude Code JSONL files
    claude_sessions.py     session discovery + status classification
    claude_usage.py        OAuth usage endpoint + local fallback + timeline
    spotify.py             playerctl wrapper
    system.py              psutil + hwmon + GPU
    git.py                 today's commits per repository (git log)
  static/
    index.html, app.js, style.css
scripts/kiosk.sh
systemd/edgeboard.service, systemd/edgeboard-kiosk.service
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
  last usage, last message kind / stop reason, the last assistant message's
  last `tool_use` (name plus a hint from its input, see `tool_hint`: Bash
  command cut to 40 chars, file basename for Read/Edit/Write, quoted pattern
  for Grep/Glob, description for Agent), the most recent user prompt (tags
  stripped, 300 chars), first and last timestamps, assistant message count.
- `read_tail(path, max_bytes)` — reads only the end of large files for
  status classification; full read only for usage aggregation.

### Sessions (`claude_sessions.py`)

Discovery:

1. `~/.claude/sessions/<pid>.json` lists live interactive sessions
   (`sessionId`, `cwd`, `startedAt`, `name`, `messagingSocketPath`). A
   session is *alive* when the pid exists in `/proc`; it `can_send` when it
   is alive and the socket path exists.
2. Transcript files modified today whose session id is not alive are
   shown as *done* (limited to the most recent N, default 12).
3. Subagent transcripts are the `*.jsonl` files under
   `<project>/<session-id>/subagents/` (workflow agents one level deeper;
   `.meta.json` files are not agents). `agents` is their count,
   `active_agents` how many were written in the last 60 s. A headless
   session with an active subagent counts as alive.

Status classification from the transcript tail:

| last entry                            | alive | status  | detail                 |
|---------------------------------------|-------|---------|------------------------|
| user prompt (text, no tool_result)    | yes   | working | working on your prompt |
| user tool_result                      | yes   | working | thinking               |
| assistant, tool_use of AskUserQuestion | yes  | attention | answer in the terminal (the question itself is on the card; see below) |
| assistant, stop_reason = tool_use     | yes   | working | running `<cmd>` / reading `<file>` / editing `<file>` / writing `<file>` / searching `"<pattern>"` / agent: `<description>`; `running <Tool>` without a hint; `running tool` without a tool |
| assistant, end_turn, active subagent  | yes   | working | agents running         |
| assistant, stop_reason = end_turn     | yes   | idle    | waiting for you        |
| any                                   | no    | done    | finished               |

Hook overrides (`POST /api/hook`, see the README): the latest Claude Code
hook event per session is kept in `State.hooks` with a receipt time and
applied on top of the table above while the process is alive, the event is
younger than 10 min (`HOOK_TTL`) and the transcript has not been written
since it arrived. A permission prompt, an elicitation dialog or a
`PreToolUse` of `AskUserQuestion` gives the fourth status **attention**
(sorted before working; pink); other `PreToolUse` events give the tool
detail above; `PostToolUse` → thinking; `UserPromptSubmit` → working on
your prompt; `Stop` / idle prompt → waiting for you; `SessionStart` (except
`compact`) → session started. Malformed bodies get a 400. `hook_applies()`
is the one freshness rule; the same fresh hook also feeds `question` (a
pending `AskUserQuestion`, flattened by `question_from_hook()` to
`{tool_use_id, title, questions: [{question, header, options: [label…], multi}]}`,
null once its `question_state` is `answered` or `abandoned`), `last_reply`
(a `Stop` hook's `last_assistant_message`, else the transcript's last
assistant text block) and `waiting_since` (the hook's receipt time, else
the last activity, only while idle or attention). The transcript also
yields `permission_mode` (latest user prompt's `permissionMode`).

The transcript itself also carries a pending question: the assistant
`tool_use` block of `AskUserQuestion` (its `id` and `input`, flattened by
the same `flatten_question()`), cleared by the `tool_result` that answers it.
`question` therefore exists with or without hooks and carries `answerable`:
true only while a fresh `PreToolUse` hook is waiting on `/api/answer`, false
for the transcript copy (or once the hook gave up), in which case the card
shows the question and its options read-only with "answer in the terminal".

Title: the latest `summary` line if any, else the first user prompt's first
line with `<system-reminder>` and similar tags stripped, truncated to 60
characters. Project = last path component of `cwd`. Model string is
shortened (`claude-fable-5-1` → `fable-5-1`; a `[1m]` marker stays).
Context tokens = last assistant `input + cache_read + cache_creation`;
`context_window` is 1,000,000 for `[1m]` models, else
`EDGEBOARD_CONTEXT_WINDOW` (200k), and `context_pct` their ratio. The
`system` / `compact_boundary` lines Claude Code writes when it compacts give
`compactions`, `last_compact_at` and `last_compact_trigger` (`auto`,
`manual`). Live sessions also get `tasks = {total, done, current}` from
`~/.claude/tasks/<session-id>/<n>.json` (`summarize_tasks()`: `done` counts
`completed`, `current` is the `activeForm` or `subject` of the first
`in_progress` task, else of the first unfinished one), null without tasks.

Summary counters: sessions today, done today, working now, idle, attention.
The page gets only the first `sessions_shown` (4) sessions after sorting
(attention, working, idle, done); the counters cover all.

Live sessions also carry `commits`: how many commits the git collector
(below) found in the session's repository since it started, filled in by
the server on every sessions round.

Cards are compact records at a 18 / 15 / 14 / 13 px scale: a head with the
status and the age of the last activity on the left and `project@branch` on
the right; the title on up to two lines; a body with the task progress
(`3/7 tasks · <current>` with a mini bar), your last prompt on one line
(`you ❯ …`) and as many whole lines of the last reply as the body has room
for (the page measures the room and sets the line clamp; the body takes the
height the other rows leave); the "now" line (the `detail`, or the pending
question on up to two lines); the action row; and a 2×2 grid of figures
behind a dashed rule, whose cells sit at the same spot on every card so the
row of cards reads as one table: model and permission mode (`plan`,
`auto-edits`, `bypass`, `dont-ask`; nothing for `default`) | `up <duration
since started_at>` and the message count; the context gauge (`ctx 197k` + a
mini bar coloured by `settings.context_warn`: amber from 10 points below, red
from the threshold, + `98%` + `⟲2` when compacted) | the `N agents` badge
(`a/N` in amber while `a` are active) and `N commits` when there are any.
Tapping a card opens a
full-height overlay (markup in `index.html`, refilled from every snapshot)
with the full title, cwd, branch, model, start time and duration, last
activity, message count, context tokens / window / % with the compactions,
tasks, agents, permission mode, waiting time, the last prompt and reply; it
closes on a backdrop tap, after 20 s (restarted by any tap inside), or when
the session leaves the snapshot.

Answering and sending: an *attention* card with a `question` shows the
question text instead of the detail line and an action row: the options of
a single-choice question (plus `terminal`, which hands the question back to
the terminal dialog), or one `answer…` button opening the overlay, where
every question has its options as toggles (multi-choice comma-joined) and a
`send answers` button; typed text fills questions without a selection. An
*idle* card that `can_send` shows the first three presets; the overlay lists
them all with a free-text line. Buttons stop propagation (they never open
the overlay), stay busy until the server answers, show `sent` for 3 s and
report failures on the red error line. Both flows are mechanisms Claude Code
documents: answers go back through the `PreToolUse` hook's `updatedInput`
(`answers` keyed by question text), prompts through the session's inbox
socket. Demo mode exercises both against its canned sessions.

### Answers and inbox (`answers.py`, `claude_inbox.py`, `scripts/edgeboard-hook.py`)

`scripts/edgeboard-hook.py` is the hook command for every event: it POSTs
the stdin JSON to `/api/hook` and, for a `PreToolUse` of `AskUserQuestion`,
long-polls `GET /api/answer/{tool_use_id}` (25 s per request) until the
panel answers or `--wait` / `EDGEBOARD_ANSWER_WAIT` (90 s) runs out, then
prints `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "allow", "updatedInput": {…tool_input, "answers"}}}`
or nothing (the terminal dialog appears). `Answers` keeps one pending entry
per `tool_use_id` (opened by the hook route, resolved by the answer route,
expired after `HOOK_TTL`); no poll for 35 s means the script gave up, the
entry is *abandoned* and the session's hook dict gets
`question_state = "abandoned"` (the card says "answer in the terminal").
`claude_inbox.find_inbox()` reads the newest pid file of a session for
`messagingSocketPath` and the `peerToken` from `<pid>.<sha256>.key`;
`send_message()` writes `{"type":"auth","token"}` and
`{"type":"user","message":{"role":"user","content"}}` as JSON lines and
closes. Slash commands do not execute through that channel, so presets
(`EDGEBOARD_PRESETS`, `label=text|…`, `DEFAULT_PRESETS` otherwise) are
phrased as instructions.

### Usage (`claude_usage.py`)

Primary source: `GET https://api.anthropic.com/api/oauth/usage` with the
OAuth bearer token from `~/.claude/.credentials.json`
(`claudeAiOauth.accessToken`) and header `anthropic-beta: oauth-2025-04-20`.
The response is a map of window name → `{utilization, resets_at}`
(`five_hour`, `seven_day`, `seven_day_opus`, …). Parsing is generic: every
top-level key whose value has `utilization` is a window, but only
`five_hour` ("5-hour") and `seven_day` ("Weekly") are shown; per-model and
extra-usage windows are dropped as noise. Polled every 60 s; failures
keep the last good value and mark the source stale. The endpoint is
shared with running Claude Code sessions and rate limits bursts, so a
429 is not an error: the poll interval doubles per consecutive 429 (up
to 10 min, or Retry-After if longer) and the red error line only
appears once the panel has been stale for 15 min.

Fallback when no token or the endpoint fails: compute from local
transcripts — tokens in the rolling 5-hour and 7-day windows, reset time =
first event in window + window length. No percentage is shown (the plan's
limits are not known locally); the UI labels this "estimated".

Pace projection: the server keeps the last 30 `(timestamp, utilization)`
samples per window (about half an hour at the 60 s poll) and drops those
from before the last reset (a drop in utilization). `project_window()` fits
a least-squares slope through three or more samples (the oldest/newest
delta with two) and yields `rate_per_hour` and `projected_full_at`, both
`null` below 0.1 %/h. The Limits panel shows "at this pace 100% at HH:MM"
in amber when that is before `resets_at`, "safe until reset" otherwise,
and no line at all while flat.

Always from local transcripts:

- Today totals: output, input, cache read, cache write, assistant messages.
- Timeline: 24 hourly buckets of `input + output + cache_write` ("burn"),
  plus the peak bucket for scaling.

### Spotify (`spotify.py`)

Wraps `playerctl -p <player>` (default player `spotify`). One call per
poll: `playerctl metadata --format` with a unit-separator-delimited template
yielding status, title, artist, album, artUrl, length, position, shuffle,
volume (the parser also accepts the older eight-field output, volume 1.0).
Missing binary or no player → `{running: false}`. Controls run
`playerctl play-pause | next | previous`, seeking runs `playerctl position
<seconds>` (the server converts the tapped fraction with the current track
length), volume runs `playerctl volume <0..1>`, and skipping to a queue entry
runs `playerctl next` index+1 times because MPRIS cannot jump into the queue.
Album art URL (https://i.scdn.co) is loaded by the browser directly.

Play queue (`spotify_queue.py`, optional): MPRIS has no queue, so the next
tracks come from the Web API `GET /v1/me/player/queue`. A one-time
`scripts/spotify_auth.py` login (Authorization Code + PKCE, no client secret)
writes `{client_id, refresh_token}` to `EDGEBOARD_SPOTIFY_TOKEN_FILE`; the
server refreshes access tokens from it and polls every 10 s while a player is
running. Without the file the snapshot says `configured: false` and the page
shows a hint instead of a list. Snapshot key `spotify_queue`.

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
- History: ring buffers (120 samples) of CPU % and GPU % for the trace; net
  rates are current values only (nothing draws their history).

### Git (`git.py`)

The Git pane lists today's commits across the repositories the sessions
work in. Every `EDGEBOARD_GIT_INTERVAL` (30 s) the collector takes the
`cwd` of each session on the panel plus `EDGEBOARD_GIT_REPOS`
(`:`-separated paths), maps each to its repository root with
`git rev-parse --show-toplevel` (paths outside a repository are skipped),
and runs `git log --since=<local midnight> --no-merges --shortstat` once
per root with a record-separated format (`parse_log` is pure). Commits are
sorted newest first across repositories; the snapshot's `git` block is
`{commits: [{hash, repo, path, message, ts, author, added, deleted}] (first
20), count, added, deleted}` with the totals over every commit. The same
list gives each session its `commits` (`commits_since`: commits whose
repository contains the session's `cwd` and whose committer date is not
before `started_at`). Until the sessions loop has run there is nothing to
scan, so the loop retries after 5 s. Errors land in `errors.git`; git is
run through the same injectable runner shape as playerctl.

### Server (`server.py`)

- `GET /` static index; `/static/*` assets.
- `GET /api/state` → full snapshot JSON.
- `GET /api/events` → SSE, one `state` event per second, keep-alive comment
  every 15 s.
- `POST /api/spotify/{play_pause|next|previous}` → runs the command, returns
  the fresh spotify block.
- `POST /api/spotify/seek {fraction}`, `POST /api/spotify/volume {volume}`,
  `POST /api/spotify/skip {index}` → JSON bodies validated to 0..1 (index
  0..19), 422 otherwise; same reply shape. Demo mode only mutates its canned
  state.
- `POST /api/hook` → stores the latest hook event per session (400 without
  `session_id` / `hook_event_name`); a `PreToolUse` of `AskUserQuestion`
  with a `tool_use_id` also opens a pending answer.
- `GET /api/answer/{tool_use_id}?wait=` → long-poll for the hook script:
  404 unknown, `{status: pending}` after `wait` (capped at 30 s),
  `{status: answered, answers}` or `{status: pass}`.
- `POST /api/sessions/{id}/answer {tool_use_id, answers | pass}` → resolves
  it: 404 unknown or another session's, 409 abandoned or already answered,
  422 malformed.
- `POST /api/sessions/{id}/send {text}` → writes into the session's inbox
  socket: 404 without an inbox, 502 on a socket error; on success the
  session's hook state becomes `UserPromptSubmit` so the card reads
  "working on your prompt" until the transcript catches up. Demo mode only
  mutates its canned sessions and never opens a socket.
- Snapshot `settings` = `{alert_sound, presets: [{label, text}], context_warn,
  system_interval}`.
- Binds `127.0.0.1:8765` by default (`EDGEBOARD_HOST`, `EDGEBOARD_PORT`).

### Frontend

Fixed 2560×720 layout via CSS grid, but fluid enough to preview in a normal
browser window. Three columns (the "Edgeboard Improved" Claude Design):

1. The rail (260 px): the clock set like a digital watch (hour:minute
   at 76 px, the seconds ticking on its baseline beside the minutes with
   the AM/PM of 12 h locales stacked above them, and under it the date on
   its own line: the weekday bright, "Sep 4" muted), the pomodoro box, the red error line, the pixel mascot, and a
   two-column grid of the current system figures (CPU % and °, GPU % and °,
   MEM, DISK, ↓ and ↑ rates) behind a dashed rule.
2. The centre (flexible), three rows: a one-row Limits pane (per window the
   big %, "resets HH:MM · ▲ full HH:MM" and a 16 px bar; today's counters
   out / in / cache rd / cache wr / msgs behind a dashed rule); the Sessions
   pane with the four cards in one row; then a 168 px row of three panes:
   Activity (the 24 h burn as a smooth amber curve with hour labels, a tap
   reads the hour under the finger), System (the CPU+GPU history trace with
   the current percentages in its legend and the load averages) and Git
   today (the commit rows `hash repo subject age`, with `N commits · +added
   −deleted` in the head).
3. Spotify pane filling the column (400 px): a 200 px square of album art
   over the centred title and "artist · album", a 28 px seekable progress
   bar that shows the target time while pressed, three 44 px touch buttons
   in one low row, a slim volume slider, and the scrollable "up next" list
   in what is left: the next track is always fully on screen (two or three
   with a one-line title), and a tap on a row skips to that track.

Colours (Dracula on a near-black ground): background and panes `#15161d`,
raised surfaces `#1e1f28`, borders `#363848`, text `#f8f8f2`, muted
`#6272a4`, Claude/accent amber `#ff9f1c` (also "working"), burn bars peach
`#ffb86c`, ok/active pane green `#50fa7b`, done purple `#bd93f9`, alerts red
`#ff5555`, sparklines cyan `#8be9fd` and pink `#ff79c6`. JetBrains Mono is
vendored as woff2 under `static/fonts/` (OFL) so the kiosk needs no network;
the stack falls back to any installed monospace. Collector errors are shown
as a red line under the clock; there is no status bar.

The page reconnects the SSE stream automatically and shows a "disconnected"
badge if no event arrives for 5 s. The mascot blinks periodically and is a
tap target for a client-side pomodoro: one tap starts a 25 min "focus"
countdown shown above the clock in amber; at zero (or on another tap) the
mascot flashes and becomes a coffee cup for a 5 min purple "break"; at zero
(or a tap) it flashes back to Claude and the countdown hides. Nothing is
persisted; the server knows nothing about it.

Attention alerts: the page compares each session's status with the previous
snapshot. `working → idle` (Claude finished) and anything `→ attention`
(permission prompt, question) make the card flash a few times and keep a
double border until its status changes again, and the mascot raises its arms
(pink) while any card is in that state. The server runs the same detection
(`attention_transitions`) for an optional desktop notification through
`notify-send` (`EDGEBOARD_ALERT_NOTIFY`); an optional chime on the panel
(`EDGEBOARD_ALERT_SOUND`, exposed as `settings.alert_sound` in the snapshot)
reuses the pomodoro's synthesized WebAudio notes. Both are off by default; a
session seen for the first time never alerts.

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
  `/api/spotify/next` calls the injected runner; the hook, answer and send
  routes (`find_inbox` / `send_message` monkeypatched) and their demo
  variants.
- `tests/test_inbox.py` posts into a real `AF_UNIX` listener;
  `tests/test_answers.py` drives the registry; `tests/test_hook_script.py`
  runs `scripts/edgeboard-hook.py` as a subprocess against a stub server.
- Browser layout test (`tests/test_page.py`, marker `browser`, skipped
  without Playwright): the demo page in headless Chromium at 2560×720 must
  not scroll or overflow, show four columns and four cards, the Spotify
  title and no console errors; a screenshot goes to `tests/artifacts/`. A
  second test answers the demo question through the overlay and taps a
  preset.
- Manual: run `python -m edgeboard` on the target machine and open the page.

## Deployment (Arch)

```
sudo pacman -S --needed python uv playerctl chromium
uv venv && uv pip install -e .   # creates .venv from pyproject
systemctl --user enable --now edgeboard.service edgeboard-kiosk.service
```

Both units use `Restart=always` (the server with `RestartSec=3` and
`StartLimitIntervalSec=0`) so a clean exit or a crash loop never leaves the
kiosk on "disconnected"; `systemctl --user stop edgeboard` is the way to stop
it on purpose.

`scripts/kiosk.sh` launches Chromium in kiosk mode with
`--window-position` set from `EDGEBOARD_DISPLAY_OFFSET` (the X offset of the
Xeneon Edge in the desktop layout) and `--window-size=2560,720`. It opens the
dashboard with `?kiosk=1`, which is the only case where the page hides the
mouse cursor (`?debug` restores it); a normal browser window keeps the cursor.

## Assumptions made without the user

1. Backend in Python (FastAPI); frontend without a build step. Chosen for
   easiest maintenance on a single Arch box.
2. Spotify via the desktop client's MPRIS interface (playerctl), not the
   Web API. If the user listens through a browser tab, the player name is
   configurable.
3. Claude limits come from the same OAuth usage endpoint Claude Code's
   `/usage` command uses; credentials are read from
   `~/.claude/.credentials.json`. If that file lives elsewhere, `EDGEBOARD_CLAUDE_DIR`
   overrides the base directory.
4. The panel runs at 2560×720 and is driven by Chromium in kiosk mode.
5. GPU may be NVIDIA or AMD; both paths are implemented, Intel shows nothing.
