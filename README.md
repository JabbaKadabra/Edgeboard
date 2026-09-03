# edgeboard

A full-screen dashboard for a Corsair Xeneon Edge (2560×720 touch panel) on
EndeavourOS / Arch Linux. One Python process, one browser tab, no build step.

It shows, live:

- **Claude usage** – 5-hour and weekly limits with % used and time until
  reset, today's token totals, and a 24-hour usage histogram.
- **Claude Code sessions** – a card per session with title, project, branch,
  model, context size, and whether it is working, idle, or done.
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

## Data sources

| Panel    | Source                                                                                                   |
|----------|----------------------------------------------------------------------------------------------------------|
| Limits   | Claude's OAuth usage endpoint, using the token in `~/.claude/.credentials.json` (same as `/usage` in Claude Code). Without a token the panel falls back to token counts from local transcripts and is labelled "estimated". |
| Today / timeline | `~/.claude/projects/*/*.jsonl` transcript files.                                                  |
| Sessions | `~/.claude/sessions/*.json` (live processes, checked against `/proc/<pid>/cmdline`) plus transcripts modified today. A transcript without a process (`claude -p`, remote sessions) counts as working while it was written in the last 60 s and Claude is mid-turn. |
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
| `EDGEBOARD_ENV_FILE`            | `.env` (relative to the working directory) |

The server has no authentication and exposes session titles, project paths
and usage data. Keep `EDGEBOARD_HOST` on `127.0.0.1`; if you must reach it from
another machine, put it behind a reverse proxy that adds auth.

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
.venv/bin/pytest
```

`tests/` covers the transcript parser, session classification, usage windows
and timeline, playerctl parsing, sensor selection, and the HTTP routes.
