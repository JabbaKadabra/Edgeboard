# edgeboard

A full-screen dashboard for a Corsair Xeneon Edge (2560×720 touch panel) on
EndeavourOS / Arch Linux. One Python process, one browser tab, no build step.

It shows, live:

- **Claude usage** – 5-hour and weekly limits with % used and time until
  reset, today's token totals, and a 24-hour usage histogram.
- **Claude Code sessions** – a card per session with title, project, branch,
  model, context size, subagent count, and whether it is working (and on
  which tool or file), idle, done, or waiting for you to approve a permission
  or answer a question. A question's options are on the card: tap one to
  answer it, and tap a preset ("continue", "commit", "tests", …) to send an
  idle session its next prompt. Tap the card itself for the full title, path,
  timings, the last prompt and reply, every question, all presets and a
  free-text line.
- **Spotify** – current track with album art, a tap-to-seek progress bar, a
  volume slider, and touch controls for previous / play-pause / next (via
  MPRIS, no API keys). The "up next" list scrolls, and tapping a track skips
  to it.
- **System** – CPU load and temperature, GPU load and temperature (NVIDIA or
  AMD), memory, disk, network throughput, and a load sparkline.

Design notes live in `docs/superpowers/specs/`.

## Install

```sh
sudo pacman -S --needed python uv playerctl chromium curl
git clone <this repo> ~/Dashboard
cd ~/Dashboard
uv venv && uv pip install -e .
```

Try it:

```sh
.venv/bin/python -m edgeboard            # http://127.0.0.1:8765
EDGEBOARD_DEMO=1 .venv/bin/python -m edgeboard   # canned data, no Claude/Spotify needed
```

Open the URL in any browser to check it. The mouse cursor is only hidden when
the page is opened with `?kiosk=1` (which `scripts/kiosk.sh` does); append
`?debug` to get it back on the kiosk. Collector errors appear in red under
the clock; tap a bar of the 24 h chart to read its value.

## Run on the Xeneon Edge

```sh
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now edgeboard.service edgeboard-kiosk.service
```

Edit `~/.config/systemd/user/edgeboard-kiosk.service` and set
`EDGEBOARD_DISPLAY_OFFSET` to the X,Y position of the Xeneon Edge in your monitor
layout (`xrandr --listmonitors` on X11). On Wayland, window placement is up
to the compositor; add a rule for windows with class `edgeboard` (Hyprland:
`windowrule = monitor DP-3, class:^(edgeboard)$` and `windowrule = fullscreen,
class:^(edgeboard)$`; older Hyprland versions spell it `windowrulev2`).

The kiosk unit starts with `graphical-session.target`, but systemd user
units do not see `DISPLAY` / `WAYLAND_DISPLAY` unless your session exports
them. Most desktop environments do this for you; on a bare compositor add
this to its startup (Hyprland `exec-once`, sway `exec`):

```sh
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_CURRENT_DESKTOP
```

If the browser never appears, check `journalctl --user -u edgeboard-kiosk`.

Both units restart on any exit (`Restart=always`), so a stray `pkill` or a
crash only costs a few seconds of "disconnected". To stop the dashboard on
purpose use `systemctl --user stop edgeboard` (the kiosk follows it). After a
`git pull`, `systemctl --user restart edgeboard` is enough: the page notices
the new build id in the snapshot and reloads itself.

## Data sources

| Panel    | Source                                                                                                   |
|----------|----------------------------------------------------------------------------------------------------------|
| Limits   | Claude's OAuth usage endpoint, using the token in `~/.claude/.credentials.json` (same as `/usage` in Claude Code). Without a token the panel falls back to token counts from local transcripts and is labelled "estimated". |
| Today / timeline | `~/.claude/projects/*/*.jsonl` transcript files.                                                  |
| Sessions | `~/.claude/sessions/*.json` (live processes, checked against `/proc/<pid>/cmdline`) plus transcripts modified today. A transcript without a process (`claude -p`, remote sessions) counts as working while it was written in the last 60 s and Claude is mid-turn. Subagents are the `*.jsonl` files under `<project>/<session>/subagents/`; one written in the last 60 s counts as active. Optional: [hooks](#session-state-from-hooks) for states the transcript cannot show. |
| Spotify  | `playerctl -p spotify` (MPRIS over D-Bus). Set `EDGEBOARD_SPOTIFY_PLAYER` for another player name. Spotify's MPRIS position is only refreshed on play/pause/seek, so the progress bar is interpolated client-side and can drift by a few seconds. |
| Up next  | Spotify Web API `/me/player/queue`, optional: see [Spotify queue](#spotify-queue). MPRIS does not expose the queue. |
| System   | `psutil`, `/sys/class/hwmon`, `nvidia-smi` or `/sys/class/drm/card*/device` for AMD.                     |

## Configuration

All settings are environment variables with defaults. Put them in
`~/Dashboard/.env` (copy `.env.example`; the server reads it from its working
directory or `EDGEBOARD_ENV_FILE`, and both systemd units load it via
`EnvironmentFile`). Real environment variables override the file. There are
no secrets to configure: the usage panel reads Claude Code's own OAuth token
from `~/.claude/.credentials.json`.

| Variable                    | Default                                  |
|-----------------------------|------------------------------------------|
| `EDGEBOARD_HOST` / `EDGEBOARD_PORT` | `127.0.0.1` / `8765`                     |
| `EDGEBOARD_CLAUDE_DIR`          | `~/.claude`                              |
| `EDGEBOARD_SPOTIFY_PLAYER`      | `spotify`                                |
| `EDGEBOARD_SPOTIFY_TOKEN_FILE`  | `~/.config/edgeboard/spotify-token.json` |
| `EDGEBOARD_SPOTIFY_QUEUE_INTERVAL` | `10` seconds                          |
| `EDGEBOARD_USAGE_URL`           | `https://api.anthropic.com/api/oauth/usage` |
| `EDGEBOARD_USAGE_INTERVAL`      | `60` seconds                             |
| `EDGEBOARD_TIMELINE_INTERVAL`   | `30` seconds                             |
| `EDGEBOARD_SESSIONS_INTERVAL`   | `2` seconds                              |
| `EDGEBOARD_SPOTIFY_INTERVAL`    | `1` second                               |
| `EDGEBOARD_SYSTEM_INTERVAL`     | `1` second                               |
| `EDGEBOARD_SESSIONS_SHOWN`      | `4` cards                                |
| `EDGEBOARD_DONE_SESSIONS_LIMIT` | `12`                                     |
| `EDGEBOARD_DEMO`                | `0`                                      |
| `EDGEBOARD_ALERT_SOUND`         | `0` (chime on the panel when a session needs you) |
| `EDGEBOARD_ALERT_NOTIFY`        | `0` (`notify-send` on the desktop as well)        |
| `EDGEBOARD_PRESETS`             | `label=text\|label=text` follow-up buttons for idle cards (see [Answering from the panel](#answering-from-the-panel)) |
| `EDGEBOARD_ANSWER_WAIT`         | `90` seconds the hook waits for a tap on the panel before the terminal asks |
| `EDGEBOARD_ENV_FILE`            | `.env` (relative to the working directory) |

The server has no authentication and exposes session titles, project paths
and usage data. Keep `EDGEBOARD_HOST` on `127.0.0.1`; if you must reach it from
another machine, put it behind a reverse proxy that adds auth. Requests to
`/api/*` are only answered when their `Host` (and `Origin`, when a browser
sends one) is loopback or the configured `EDGEBOARD_HOST`, so a web page open
in your desktop browser cannot drive the API or read the snapshot; a reverse
proxy therefore has to forward the original `Host` header (or bind the
server to the address the proxy uses).

## Attention alerts

The point of the panel is to know when Claude needs you. When a session card
goes from `working` to `idle` (Claude finished its turn) or to `attention`
(permission prompt or question, see the hooks below) it flashes a few times
and keeps a double border until its status changes again, and the mascot
raises its arms while any card is in that state. Two optional extras:

- `EDGEBOARD_ALERT_SOUND=1` plays a short chime on the panel (the kiosk
  browser is started with autoplay allowed, so it sounds without a tap).
- `EDGEBOARD_ALERT_NOTIFY=1` sends a desktop notification through
  `notify-send`, so the main monitor sees it too.

The Limits panel also projects the current pace: `at this pace 100% at 15:40`
in amber when the window would fill before it resets, `safe until reset`
otherwise, nothing while usage is flat. The pace is a least-squares fit over
the last 30 usage polls (about half an hour) since the window last reset.

## Session state from hooks

The transcript cannot tell that Claude is waiting for you to approve a
permission or answer a question, and it only learns about a `Stop` a while
later. Claude Code hooks can post those events straight to the dashboard,
and the same hook is what lets the panel answer questions (next section).
Add this to `~/.claude/settings.json` (merge with any hooks you already have;
the script reads `EDGEBOARD_URL` from the environment, or takes `--url`, if
you changed the port):

```json
{
  "hooks": {
    "Notification":     [{"hooks": [{"type": "command", "command": "python3 $HOME/Dashboard/scripts/edgeboard-hook.py"}]}],
    "PreToolUse":       [{"hooks": [{"type": "command", "command": "python3 $HOME/Dashboard/scripts/edgeboard-hook.py", "timeout": 120}]}],
    "PostToolUse":      [{"hooks": [{"type": "command", "command": "python3 $HOME/Dashboard/scripts/edgeboard-hook.py"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 $HOME/Dashboard/scripts/edgeboard-hook.py"}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python3 $HOME/Dashboard/scripts/edgeboard-hook.py"}]}],
    "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 $HOME/Dashboard/scripts/edgeboard-hook.py"}]}]
  }
}
```

`scripts/edgeboard-hook.py` (standard library only) forwards the JSON Claude
Code passes to hooks on stdin to `POST /api/hook`. The server keeps the
latest event per session (`session_id`, `hook_event_name`, `cwd`, plus the
event's own fields) for 10 minutes. It overrides the transcript-derived
status only while it is newer than the transcript's last line, so an
approved permission (which writes a tool result) clears the "needs
permission" card on its own:

| Event                                   | Card                              |
|-----------------------------------------|-----------------------------------|
| `Notification` / `permission_prompt`    | **attention** · needs permission  |
| `Notification` / `elicitation_dialog`   | **attention** · needs your input  |
| `PreToolUse` / `AskUserQuestion`        | **attention** · the question, with its options as buttons |
| `PreToolUse` / any other tool           | working · running `<command>`, editing `<file>`, … |
| `PostToolUse`                           | working · thinking                |
| `UserPromptSubmit`                      | working · working on your prompt  |
| `Stop`, `Notification` / `idle_prompt`  | idle · waiting for you (`Stop` also carries Claude's last reply) |
| `SessionStart` (not `compact`)          | idle · session started            |

The script exits 0 without output whenever the dashboard is down or says
nothing, so Claude Code never stalls or reports an error because of it.
Nothing is required: without hooks the cards fall back to what the
transcript says.

## Answering from the panel

Two things need no keyboard:

**Questions.** When Claude calls `AskUserQuestion`, the `PreToolUse` hook
above posts the question to the dashboard and then waits for your tap
(`EDGEBOARD_ANSWER_WAIT`, 90 s by default; pass `--wait` to the script to
override) by long-polling `GET /api/answer/<tool_use_id>`. The card shows
the question with its options; a single-choice question is answered right
on the card, a multi-part or multi-choice one through the overlay ("answer…").
`POST /api/sessions/<id>/answer` resolves the wait and the script returns
the answers to Claude Code as the tool's input, which skips the terminal
dialog. While the hook waits the terminal shows only a hook spinner; the
`terminal` button hands the question back to it at once, and so does the
timeout. After that the card says "answer in the terminal". The hook entry
needs `"timeout"` above the wait (120 s for the default 90 s) or Claude Code
kills the script first.

**Presets.** An idle session shows up to four buttons from
`EDGEBOARD_PRESETS` (`label=text|label=text`; the overlay lists them all and
adds a free-text line). `POST /api/sessions/<id>/send {text}` writes the
text into the session's inbox socket, the same channel other Claude Code
sessions use to message it (`messagingSocketPath` in
`~/.claude/sessions/<pid>.json`, Claude Code 2.1.224 or newer), and the
session starts a new turn with it. Slash commands do not run through that
channel, so phrase presets as instructions: the default `commit` preset is
"Use the /commit skill to commit the current work with a clear message."
A session running with `--dangerously-skip-permissions` holds the message
for approval in its terminal; sessions without a process (finished,
`claude -p`, cloud) have no inbox and show no buttons.

Everything the panel sends is plain text as if you had typed it; nothing
approves a permission prompt on your behalf.

## Spotify queue

The "up next" list under the player needs the Spotify Web API, because the
desktop client's MPRIS interface has no queue. It is off until you log in once:

1. Create a free app at <https://developer.spotify.com/dashboard> with the
   redirect URI `http://127.0.0.1:8766/callback`.
2. Run `python scripts/spotify_auth.py --client-id <Client ID>` and open the
   printed URL in a browser that is logged in to your Spotify account.
3. Restart the server. The script wrote `~/.config/edgeboard/spotify-token.json`
   (`EDGEBOARD_SPOTIFY_TOKEN_FILE`); the server refreshes the token itself and
   polls the queue every `EDGEBOARD_SPOTIFY_QUEUE_INTERVAL` seconds while
   something is playing. Reading the queue requires Spotify Premium.

The login uses PKCE, so no client secret is stored anywhere.

Tapping a queued track plays it by pressing `next` once per row above it
(MPRIS has no "jump to queue entry"), so the tracks before it are skipped
and the rest of the queue stays as it was.

## Development

```sh
uv pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest
```

CI (`.github/workflows/ci.yml`) runs the same two commands on every push.
`tests/` covers the transcript parser, session classification, usage windows,
projection and timeline, playerctl parsing, sensor selection, and the HTTP
routes.

`tests/test_page.py` renders the demo page in headless Chromium at the
panel's 2560×720 and checks that nothing overflows, all four columns are on
screen, the cards and Spotify title render and the console is clean. It is
skipped unless Playwright is installed:

```sh
.venv/bin/pip install -e ".[browser]" && .venv/bin/playwright install chromium
.venv/bin/pytest -m browser          # screenshot lands in tests/artifacts/
```
