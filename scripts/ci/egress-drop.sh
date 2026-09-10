#!/usr/bin/env bash
# Run the whole test suite with no network at all (CLAUDE.md INV-1, INV-8, §11 "egress-DROP").
# Dependencies must already be installed from the lockfiles.
#
#   scripts/ci/egress-drop.sh --netns   # CI and local: run the suite inside an empty network
#                                       # namespace (needs sudo or root to create it)
#   scripts/ci/egress-drop.sh --run     # (internal) assume the block is in place, run the suite
#
# The namespace has no interfaces except loopback and no routes, so a hidden network
# dependency fails immediately instead of hanging. Only the suite is isolated; the CI runner
# agent keeps its own network, which a host-wide iptables DROP would also cut (that stalls the
# job because the runner can no longer report to GitHub).
#
# The script first proves the block works: a connection attempt to a public host must fail.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/../.." && pwd)
cd "$HERE"

canary() {
  if curl -sS --max-time 5 -o /dev/null https://example.com/ 2>/dev/null; then
    echo "egress-drop: the canary connection SUCCEEDED; the network is not blocked." >&2
    exit 1
  fi
  if python3 - <<'EOF' 2>/dev/null; then
import socket
socket.setdefaulttimeout(5)
socket.create_connection(("1.1.1.1", 443)).close()
EOF
    echo "egress-drop: raw TCP canary SUCCEEDED; the network is not blocked." >&2
    exit 1
  fi
  echo "egress-drop: canary confirmed, no network."
}

run_suite() {
  # Fail fast on anything that would otherwise look for the network.
  export UV_OFFLINE=1 COREPACK_ENABLE_NETWORK=0 npm_config_update_notifier=false
  export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 DO_NOT_TRACK=1
  canary
  echo "egress-drop: python tests"
  uv run --offline pytest
  echo "egress-drop: node tests"
  pnpm -r --offline test
  echo "egress-drop: install.sh preflight"
  tmp=$(mktemp -d)
  rc=0
  sh ./install.sh --data-root "$tmp/data" --edge-port 0 || rc=$?
  # 0 = passed, 2 = a blocking check such as no container engine on this runner; both are
  # valid preflight outcomes. Anything else is a script failure.
  case "$rc" in 0|2) ;; *) echo "install.sh exited $rc" >&2; exit "$rc" ;; esac
  echo "egress-drop: done"
}

case "${1:-}" in
  --netns)
    me=$(id -un)
    # Create the namespace as root, bring loopback up for tests that bind localhost later,
    # then return to the invoking user with the same PATH and HOME so caches are found.
    inner='
      if command -v ip >/dev/null 2>&1; then ip link set lo up 2>/dev/null || true; fi
      if [ "$(id -un)" = "$1" ]; then
        exec env PATH="$2" HOME="$3" bash "$4" --run
      fi
      exec sudo -E -u "$1" env PATH="$2" HOME="$3" bash "$4" --run
    '
    script="$HERE/scripts/ci/egress-drop.sh"
    if [ "$(id -u)" -eq 0 ]; then
      exec unshare -n -- bash -c "$inner" netns "$me" "$PATH" "$HOME" "$script"
    fi
    exec sudo -E unshare -n -- bash -c "$inner" netns "$me" "$PATH" "$HOME" "$script"
    ;;
  --run)
    run_suite
    ;;
  *)
    sed -n '2,13p' "$0" >&2
    exit 2
    ;;
esac
