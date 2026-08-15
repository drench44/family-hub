#!/bin/bash
# Family Hub wall kiosk launcher (Raspberry Pi, X11/Openbox session).
# See docs/raspberry-pi-kiosk.md for the full setup. Install to ~/kiosk.sh,
# make it executable, and autostart it (docs/kiosk/familyhub-kiosk.desktop).
export DISPLAY=:0

# ── EDIT THIS ── point it at wherever your hub is reachable from the Pi:
URL="http://CHANGE-ME:8138"

# Let the desktop settle, then paint the root black so nothing shows behind us
sleep 3
command -v xsetroot >/dev/null && xsetroot -solid black

# No screen blanking / no power management on the display
xset s off
xset s noblank
xset -dpms

# Hide the mouse cursor at all times, including when a touch tap moves it.
# unclutter-xfixes (not classic unclutter) is what supports --hide-on-touch.
pkill -x unclutter 2>/dev/null
pkill -f unclutter-xfixes 2>/dev/null
unclutter-xfixes --timeout 1 --hide-on-touch &

# Clear any "Chromium didn't shut down correctly" restore prompt from a hard power-off
PREF="$HOME/.config/chromium/Default/Preferences"
[ -f "$PREF" ] && sed -i \
  -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
  -e 's/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREF"

# Keep the wall up 24/7: relaunch Chromium if it ever exits or crashes
while true; do
  chromium-browser \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=Translate \
    --no-first-run \
    --password-store=basic \
    --check-for-update-interval=31536000 \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --ignore-gpu-blocklist \
    --enable-gpu-rasterization \
    --enable-zero-copy \
    --disable-background-timer-throttling \
    "$URL"
  sleep 3
done
