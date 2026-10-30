#!/bin/sh
set -eu
url=http://100.100.177.81:8767/visualizer/display
while [ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] || ! curl --silent --fail --max-time 3 "$url" >/dev/null; do
    sleep 2
done
# V3D's accelerated Canvas 2D can leave diagonal seams across the 4K gradients.
# Rasterize 2D Canvas in software; WebGL/video keep their normal graphics path.
# Chromium can keep running on an "Aw, Snap!" page after its renderer crashes.
# Watch the page heartbeat as well as the browser process so systemd recovers it.
exec /usr/bin/python3 "$(dirname "$0")/visualizer_kiosk.py" "${url%/visualizer/display}/api/visualizer/settings" \
    /usr/bin/chromium --disable-accelerated-2d-canvas --ozone-platform=wayland --kiosk --app="$url" \
    --user-data-dir="$HOME/.local/share/octet-visualizer" --no-first-run \
    --no-default-browser-check --noerrdialogs --disable-session-crashed-bubble \
    --autoplay-policy=no-user-gesture-required --password-store=basic \
    --disable-features=Translate,MediaRouter
