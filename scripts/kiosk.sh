#!/usr/bin/env bash
# Launch Chromium in kiosk mode on the Corsair Xeneon Edge.
#
# Environment:
#   XDASH_URL             dashboard URL (default http://127.0.0.1:8765)
#   XDASH_DISPLAY_OFFSET  "X,Y" position of the Xeneon Edge in the desktop layout (default 0,0)
#   XDASH_DISPLAY_SIZE    "W,H" panel size (default 2560,720)
#   XDASH_BROWSER         browser binary (default: chromium, then google-chrome-stable, brave)
#
# Under X11 the --window-position flag places the window on the right output.
# Under Wayland compositors window placement is up to the compositor; add a
# window rule for the "xdash" app id (e.g. in Hyprland: windowrulev2 =
# monitor DP-3, title:^(xdash)$ ) or run this from that monitor's workspace.
set -euo pipefail

URL="${XDASH_URL:-http://127.0.0.1:8765}"
OFFSET="${XDASH_DISPLAY_OFFSET:-0,0}"
SIZE="${XDASH_DISPLAY_SIZE:-2560,720}"
PROFILE="${XDG_CACHE_HOME:-$HOME/.cache}/xdash-kiosk"

pick_browser() {
  if [[ -n "${XDASH_BROWSER:-}" ]]; then echo "$XDASH_BROWSER"; return; fi
  for b in chromium google-chrome-stable brave; do
    if command -v "$b" >/dev/null 2>&1; then echo "$b"; return; fi
  done
  echo "no Chromium-based browser found (pacman -S chromium)" >&2
  exit 1
}

BROWSER="$(pick_browser)"

# Wait for the server so the kiosk never shows a connection error page.
for _ in $(seq 1 60); do
  if curl -fsS "$URL/api/state" >/dev/null 2>&1; then break; fi
  sleep 1
done

exec "$BROWSER" \
  --kiosk "$URL" \
  --class=xdash --user-data-dir="$PROFILE" \
  --window-position="$OFFSET" --window-size="$SIZE" \
  --touch-events=enabled --enable-features=OverlayScrollbar \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-features=TranslateUI --overscroll-history-navigation=0 \
  --autoplay-policy=no-user-gesture-required --no-first-run --password-store=basic
