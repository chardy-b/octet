#!/bin/sh
set -eu
url=http://100.100.177.81:8767/visualizer/display
while [ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] || ! curl --silent --fail --max-time 3 "$url" >/dev/null; do
    sleep 2
done
exec /usr/bin/chromium --ozone-platform=wayland --kiosk --app="$url" \
    --user-data-dir="$HOME/.local/share/octet-visualizer" --no-first-run \
    --no-default-browser-check --noerrdialogs --disable-session-crashed-bubble \
    --autoplay-policy=no-user-gesture-required --password-store=basic \
    --disable-features=Translate,MediaRouter
