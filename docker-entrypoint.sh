#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
export DATA_DIR="${DATA_DIR:-/app/data}"
PORT="${PORT:-3000}"
mkdir -p "$DATA_DIR"

Xvfb "$DISPLAY" -screen 0 1280x900x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &

PASS_FILE="$DATA_DIR/.browser_password"
if [ -n "${BROWSER_PASSWORD:-}" ]; then
  printf '%s' "$BROWSER_PASSWORD" > "$PASS_FILE"
elif [ ! -s "$PASS_FILE" ]; then
  python - <<'PY' > "$PASS_FILE"
import secrets
print(secrets.token_urlsafe(12), end="")
PY
fi
BROWSER_PASSWORD="$(cat "$PASS_FILE")"
chmod 600 "$PASS_FILE"
VNC_AUTH="$DATA_DIR/.vnc_passwd"
x11vnc -storepasswd "$BROWSER_PASSWORD" "$VNC_AUTH" >/dev/null

x11vnc \
  -display "$DISPLAY" \
  -forever -shared -rfbport 5900 \
  -rfbauth "$VNC_AUTH" \
  -noxdamage -repeat \
  >/tmp/x11vnc.log 2>&1 &

websockify --web=/usr/share/novnc "0.0.0.0:$PORT" 127.0.0.1:5900 \
  >/tmp/novnc.log 2>&1 &

printf '\n=== Remote Google login ===\n'
printf 'Open: https://%s/vnc.html?autoconnect=true&resize=remote\n' "${DOMAIN:-YOUR_BOTHOST_DOMAIN}"
printf 'VNC password: %s\n' "$BROWSER_PASSWORD"
printf '===========================\n\n'

exec python main.py
