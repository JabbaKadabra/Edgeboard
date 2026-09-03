# xdash

A full-screen dashboard for a Corsair Xeneon Edge (2560×720 touch panel) on
EndeavourOS / Arch Linux. One Python process, one browser tab, no build step.

It shows, live:

- **Claude usage** – 5-hour and weekly limits with % used and time until
  reset, today's token totals, and a 24-hour usage histogram.
- **Claude Code sessions** – a card per session with title, project, branch,
  model, context size, and whether it is working, idle, or done.
- **Spotify** – current track with album art, progress, and touch controls for
  previous / play-pause / next (via MPRIS, no API keys).
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
.venv/bin/python -m xdash            # http://127.0.0.1:8765
XDASH_DEMO=1 .venv/bin/python -m xdash   # canned data, no Claude/Spotify needed
```

Open the URL in any browser to check it. Append `?debug` to the URL to get the
mouse cursor back (the kiosk hides it). Collector errors appear in red under
the status chips; tap a bar of the 24 h chart to read its value.

## Run on the Xeneon Edge

```sh
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now xdash.service xdash-kiosk.service
```

Edit `~/.config/systemd/user/xdash-kiosk.service` and set
`XDASH_DISPLAY_OFFSET` to the X,Y position of the Xeneon Edge in your monitor
layout (`xrandr --listmonitors` on X11). On Wayland, window placement is up
to the compositor; add a rule for windows with class `xdash` (Hyprland:
`windowrule = monitor DP-3, class:^(xdash)$` and `windowrule = fullscreen,
class:^(xdash)$`; older Hyprland versions spell it `windowrulev2`).

The kiosk unit starts with `graphical-session.target`, but systemd user
units do not see `DISPLAY` / `WAYLAND_DISPLAY` unless your session exports
them. Most desktop environments do this for you; on a bare compositor add
this to its startup (Hyprland `exec-once`, sway `exec`):

```sh
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_CURRENT_DESKTOP
```

If the browser never appears, check `journalctl --user -u xdash-kiosk`.

## Data sources

| Panel    | Source                                                                                                   |
|----------|----------------------------------------------------------------------------------------------------------|
| Limits   | Claude's OAuth usage endpoint, using the token in `~/.claude/.credentials.json` (same as `/usage` in Claude Code). Without a token the panel falls back to token counts from local transcripts and is labelled "estimated". |
| Today / timeline | `~/.claude/projects/*/*.jsonl` transcript files.                                                  |
| Sessions | `~/.claude/sessions/*.json` (live processes, checked against `/proc/<pid>/cmdline`) plus transcripts modified today. A transcript without a process (`claude -p`, remote sessions) counts as working while it was written in the last 60 s and Claude is mid-turn. |
| Spotify  | `playerctl -p spotify` (MPRIS over D-Bus). Set `XDASH_SPOTIFY_PLAYER` for another player name. Spotify's MPRIS position is only refreshed on play/pause/seek, so the progress bar is interpolated client-side and can drift by a few seconds. |
| System   | `psutil`, `/sys/class/hwmon`, `nvidia-smi` or `/sys/class/drm/card*/device` for AMD.                     |

## Configuration

All settings are environment variables with defaults:

| Variable                    | Default                                  |
|-----------------------------|------------------------------------------|
| `XDASH_HOST` / `XDASH_PORT` | `127.0.0.1` / `8765`                     |
| `XDASH_CLAUDE_DIR`          | `~/.claude`                              |
| `XDASH_SPOTIFY_PLAYER`      | `spotify`                                |
| `XDASH_USAGE_URL`           | `https://api.anthropic.com/api/oauth/usage` |
| `XDASH_USAGE_INTERVAL`      | `60` seconds                             |
| `XDASH_TIMELINE_INTERVAL`   | `30` seconds                             |
| `XDASH_SESSIONS_INTERVAL`   | `2` seconds                              |
| `XDASH_SPOTIFY_INTERVAL`    | `1` second                               |
| `XDASH_SYSTEM_INTERVAL`     | `1` second                               |
| `XDASH_DONE_SESSIONS_LIMIT` | `12`                                     |
| `XDASH_DEMO`                | `0`                                      |

The server has no authentication and exposes session titles, project paths
and usage data. Keep `XDASH_HOST` on `127.0.0.1`; if you must reach it from
another machine, put it behind a reverse proxy that adds auth.

## Development

```sh
uv pip install -e ".[dev]"
.venv/bin/pytest
```

`tests/` covers the transcript parser, session classification, usage windows
and timeline, playerctl parsing, sensor selection, and the HTTP routes.
