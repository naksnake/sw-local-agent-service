#!/usr/bin/env bash
# Screen worker entrypoint (Phase 4). Starts one display session from the environment so the
# image can be smoke-tested, plus /health and /metrics on 8000 for the compose healthcheck
# and Prometheus; the orchestrator-facing session API arrives with the api dependencies
# (ADR-0005). argv only, no shell interpolation of untrusted input.
set -euo pipefail

SESSION_ID="${SLAS_SESSION_ID:-smoke}"
DISPLAY_NO="${SLAS_DISPLAY:-10}"
GEOMETRY="${SLAS_GEOMETRY:-1920x1080x24}"
VNC_PORT=$((5900 + DISPLAY_NO))
NOVNC_PORT=$((6080 + DISPLAY_NO))

echo "Session ${SESSION_ID}: display :${DISPLAY_NO} (${GEOMETRY}); watch it on port ${NOVNC_PORT}."

HEALTH_PID=""
if python3 -c 'import slas_observability.serve' 2>/dev/null; then
  python3 -m slas_observability.serve screen-worker --bind "${SLAS_HEALTH_BIND:-0.0.0.0:8000}" &
  HEALTH_PID=$!
else
  echo "slas_observability is not on PYTHONPATH; /health and /metrics are not served."
fi

Xvfb ":${DISPLAY_NO}" -screen 0 "${GEOMETRY}" -nolisten tcp -noreset &
XVFB_PID=$!
sleep 1
x11vnc -display ":${DISPLAY_NO}" -rfbport "${VNC_PORT}" -localhost -forever -shared -nopw -noxdamage -quiet &
VNC_PID=$!
websockify --web /usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
NOVNC_PID=$!

# shellcheck disable=SC2064 — the pids are known now; the trap must not re-expand later
trap "kill ${NOVNC_PID} ${VNC_PID} ${XVFB_PID} ${HEALTH_PID} 2>/dev/null || true" EXIT INT TERM
wait "${XVFB_PID}"
