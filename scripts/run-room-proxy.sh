#!/usr/bin/env bash
# Starts the TEV room proxy on loopback (default 127.0.0.1:8788) and publishes it on the
# tailnet with `tailscale serve` (HTTPS 443, tailnet only, never Funnel). Idempotent.
set -u
RUN_DIR="${NOEMA_RUN_DIR:-/workspace/noema-run}"
PORT="${NOEMA_ROOM_PROXY_PORT:-8788}"
PY="${NOEMA_PYTHON:-/tmp/nv/bin/python}"
cd "$(dirname "$0")/.." || exit 1
umask 077
if ! pgrep -f -- "room_proxy:app" >/dev/null; then
  nohup "$PY" -m uvicorn noema.room_proxy:app --host 127.0.0.1 --port "$PORT" --no-access-log \
    >> "$RUN_DIR/proxy.log" 2>&1 < /dev/null &
  echo $! > "$RUN_DIR/proxy.pid"
fi
TS="tailscale"; [ -n "${TS_SOCKET:-}" ] && TS="tailscale --socket=$TS_SOCKET"
if command -v tailscale >/dev/null && $TS status >/dev/null 2>&1; then
  $TS serve status 2>/dev/null | grep -q "127.0.0.1:$PORT" || $TS serve --bg --https=443 "http://127.0.0.1:$PORT" >/dev/null 2>&1
  $TS funnel status 2>/dev/null | grep -qi "funnel on" && echo "$(date -Is) WARNING funnel is on; owner bypass ignores funnel traffic" >> "$RUN_DIR/proxy.log"
fi
# heartbeat marker: keep log activity inside the 1h cadence
echo "$(date -Is) room_proxy alive port $PORT" >> "$RUN_DIR/proxy.log"
