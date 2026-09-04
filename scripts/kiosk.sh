#!/usr/bin/env bash
# Launch Chromium in kiosk mode on the Corsair Xeneon Edge.
#
# Environment:
#   EDGEBOARD_URL             dashboard URL (default http://127.0.0.1:8765)
#   EDGEBOARD_DISPLAY_OFFSET  "X,Y" position of the Xeneon Edge in the desktop layout (default 0,0)
#   EDGEBOARD_DISPLAY_SIZE    "W,H" panel size (default 2560,720)
#   EDGEBOARD_BROWSER         browser binary (default: chromium, then google-chrome-stable, brave)
#   EDGEBOARD_KIOSK_DEBUG_PORT  open Chromium's DevTools protocol on this loopback port (default: off);
#                             tests/test_kiosk.py drives the live panel through it (pytest -m kiosk)
#
# Under X11 the --window-position flag places the window on the right output.
# Under Wayland compositors window placement is up to the compositor; add a
# window rule for the "edgeboard" app class (e.g. in Hyprland:
#   windowrule = monitor DP-3, class:^(edgeboard)$
#   windowrule = fullscreen, class:^(edgeboard)$ )
# or run this from that monitor's workspace.
set -euo pipefail

URL="${EDGEBOARD_URL:-http://127.0.0.1:8765}"
OFFSET="${EDGEBOARD_DISPLAY_OFFSET:-0,0}"
SIZE="${EDGEBOARD_DISPLAY_SIZE:-2560,720}"
PROFILE="${XDG_STATE_HOME:-$HOME/.local/state}/edgeboard-kiosk"
DEBUG_PORT="${EDGEBOARD_KIOSK_DEBUG_PORT:-}"

pick_browser() {
  if [[ -n "${EDGEBOARD_BROWSER:-}" ]]; then echo "$EDGEBOARD_BROWSER"; return; fi
  for b in chromium google-chrome-stable brave; do
    if command -v "$b" >/dev/null 2>&1; then echo "$b"; return; fi
  done
  echo "no Chromium-based browser found (pacman -S chromium)" >&2
  exit 1
}

BROWSER="$(pick_browser)"

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "neither DISPLAY nor WAYLAND_DISPLAY is set; import them into the user session first" >&2
  echo "  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_CURRENT_DESKTOP" >&2
  exit 1
fi

# Wait for the server so the kiosk never shows a connection error page.
server_up() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$URL/api/state" >/dev/null 2>&1
  else
    local hostport=${URL#*://}; hostport=${hostport%%/*}
    local host=${hostport%%:*} port=${hostport##*:}
    [[ "$host" == "$port" ]] && port=80
    (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null
  fi
}
for _ in $(seq 1 60); do
  if server_up; then break; fi
  sleep 1
done

# ?kiosk=1 makes the page hide the mouse cursor (add ?debug to get it back).
case "$URL" in *\?*) KIOSK_URL="$URL&kiosk=1" ;; *) KIOSK_URL="$URL?kiosk=1" ;; esac

# The DevTools port has no auth: loopback only, and only when asked for.
DEBUG_FLAGS=()
if [[ -n "$DEBUG_PORT" ]]; then
  DEBUG_FLAGS=(--remote-debugging-port="$DEBUG_PORT" --remote-debugging-address=127.0.0.1)
fi

exec "$BROWSER" \
  --kiosk "$KIOSK_URL" "${DEBUG_FLAGS[@]}" \
  --class=edgeboard --user-data-dir="$PROFILE" \
  --window-position="$OFFSET" --window-size="$SIZE" \
  --touch-events=enabled --disable-pinch --enable-features=OverlayScrollbar \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-features=TranslateUI --overscroll-history-navigation=0 \
  --autoplay-policy=no-user-gesture-required --no-first-run --password-store=basic
