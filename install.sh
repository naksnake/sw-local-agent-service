#!/usr/bin/env bash
# SW Local Agent Service — installer (CLAUDE.md §3).
#
# Phase 0: runs the preflight (`slas doctor`) and stops. Later phases add, in order:
# write .env → load images → copy models → docker compose up → wait healthy → print the
# login URL and the one-time admin password. Idempotent: running it twice is safe.
# In Phase 0 it changes nothing on the host.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${SLAS_PROFILE:-quickstart}"
DATA_ROOT="${SLAS_DATA_ROOT:-/AI/Agent}"
JSON=0

usage() {
  cat <<EOF
Usage: ./install.sh [--profile quickstart|prod] [--data-root PATH] [--preflight-only] [--json]

Installs SW Local Agent Service on this host. In Phase 0 it runs the preflight only.

  --profile PROFILE    quickstart (default) or prod. Also read from \$SLAS_PROFILE.
  --data-root PATH     Where the platform keeps its data. Default: \$SLAS_DATA_ROOT or /AI/Agent.
  --preflight-only     Stop after the preflight. (Always the case in Phase 0.)
  --json               Print the preflight report as JSON, for scripts.
  -h, --help           Show this help.

Exit codes: 0 ready · 1 the preflight found problems · 2 wrong usage.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)        PROFILE="${2:-}"; shift 2 ;;
    --profile=*)      PROFILE="${1#*=}"; shift ;;
    --data-root)      DATA_ROOT="${2:-}"; shift 2 ;;
    --data-root=*)    DATA_ROOT="${1#*=}"; shift ;;
    --preflight-only) shift ;;
    --json)           JSON=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Run ./install.sh --help to see the options." >&2
      exit 2 ;;
  esac
done

case "$PROFILE" in
  quickstart|prod) ;;
  *)
    echo "The profile \"$PROFILE\" is not known." >&2
    echo "Likely cause: a typo in --profile or in SLAS_PROFILE." >&2
    echo "What to do: use --profile quickstart (default) or --profile prod." >&2
    exit 2 ;;
esac

if [[ -z "$DATA_ROOT" ]]; then
  echo "The data root is empty." >&2
  echo "Likely cause: --data-root or SLAS_DATA_ROOT was set to nothing." >&2
  echo "What to do: pass a directory, for example --data-root /AI/Agent." >&2
  exit 2
fi

find_python() {
  local candidate
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
          >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  cat >&2 <<EOF
Python 3.12 was not found on this host.
Likely cause: the preflight runs on Python 3.12, and only an older Python (or none) is installed.
What to do: install python3.12 (Debian/Ubuntu: sudo apt install python3.12), then run ./install.sh again. From Phase 1 the bundle ships its own Python.
EOF
  exit 1
fi

# Phase 0 runs from the source tree; the bundle will ship the packages installed.
export PYTHONPATH="$SCRIPT_DIR/packages/slas-cli:$SCRIPT_DIR/packages/slas-kernel:$SCRIPT_DIR/packages/slas-schemas${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

args=(doctor --profile "$PROFILE" --data-root "$DATA_ROOT")
if [[ $JSON -eq 1 ]]; then
  args+=(--json)
fi

set +e
"$PYTHON" -m slas_cli "${args[@]}"
rc=$?
set -e

if [[ $JSON -eq 1 ]]; then
  exit "$rc"
fi

echo
case "$rc" in
  0)
    echo "Preflight passed. Nothing was changed on this host."
    echo "The remaining install steps (write .env, load images, copy models, start the services) arrive in Phase 1 of docs/DEVELOPMENT_PLAN.md."
    ;;
  1)
    echo "Preflight found problems. Nothing was changed on this host."
    echo "Fix the problems listed above, then run ./install.sh again."
    ;;
  *)
    echo "The preflight itself failed (exit code $rc). Nothing was changed on this host."
    echo "Likely cause: a bug in the installer, not a problem with your host."
    echo "What to do: run ./install.sh again; if it repeats, report the output above."
    ;;
esac
exit "$rc"
