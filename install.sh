#!/bin/sh
# SW Local Agent Service installer (CLAUDE.md §3).
#
# Phase 0: preflight only. The full pipeline is
#   preflight (slas doctor) → generate .env → load images → copy models →
#   compose up → wait healthy → print URL and one-time admin password
# and the steps after preflight arrive in Phase 1 (docs/DEVELOPMENT_PLAN.md).
# This script is idempotent and, in Phase 0, changes nothing on the host.
set -eu

usage() {
  cat <<'EOF'
Usage: ./install.sh [--profile quickstart|prod] [--data-root PATH] [--edge-port N] [--json]

Runs the preflight check of this host and prints a plain-language report.
  --profile    deployment profile (default quickstart)
  --data-root  where platform data will live (default $SLAS_DATA_ROOT or /AI/Agent)
  --edge-port  port the web interface will use (default 443)
  --json       machine-readable report (for CI)
EOF
}

three_part_error() {
  # $1 what happened, $2 likely cause, $3 what to do (CLAUDE.md §11)
  printf 'Blocked  %s\n         What happened: %s\n         Likely cause:  %s\n         What to do:    %s\n' \
    "$1" "$1" "$2" "$3" >&2
}

PROFILE=quickstart
DATA_ROOT="${SLAS_DATA_ROOT:-/AI/Agent}"
EDGE_PORT=443
JSON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) [ $# -ge 2 ] || { usage >&2; exit 2; }; PROFILE=$2; shift 2 ;;
    --data-root) [ $# -ge 2 ] || { usage >&2; exit 2; }; DATA_ROOT=$2; shift 2 ;;
    --edge-port) [ $# -ge 2 ] || { usage >&2; exit 2; }; EDGE_PORT=$2; shift 2 ;;
    --json) JSON=--json; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'install.sh: unknown option %s\n\n' "$1" >&2
      usage >&2
      exit 2 ;;
  esac
done

case "$PROFILE" in
  quickstart|prod) ;;
  *)
    three_part_error "The profile \"$PROFILE\" is not one the installer knows." \
      "Only quickstart and prod exist (CLAUDE.md §3)." \
      "Run ./install.sh with --profile quickstart or --profile prod."
    exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)

# The preflight is pure standard-library Python so it can run before anything is installed.
PY=""
for candidate in python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    PY=$candidate
    break
  fi
done
if [ -z "$PY" ]; then
  three_part_error "The preflight could not start." \
    "Python 3.12 or newer was not found on this host; the preflight is written in Python." \
    "Install python3.12 from the offline bundle's OS packages, then run ./install.sh again."
  exit 2
fi

export PYTHONPATH="$HERE/packages/slas-cli${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
rc=0
"$PY" -m slas_cli doctor --data-root "$DATA_ROOT" --profile "$PROFILE" --edge-port "$EDGE_PORT" $JSON || rc=$?

if [ "$rc" -eq 0 ] && [ -z "$JSON" ]; then
  printf '\nThe remaining install steps (configuration, images, models, start) arrive in Phase 1 of docs/DEVELOPMENT_PLAN.md; nothing was changed on this host.\n'
elif [ "$rc" -ne 0 ] && [ -z "$JSON" ]; then
  printf '\nInstallation did not start; nothing was changed on this host.\n'
fi
exit "$rc"
