#!/usr/bin/env bash
# Run the whole test suite with all outbound network traffic blocked (CLAUDE.md INV-1,
# INV-8, §11 "egress-DROP"). Dependencies must already be installed from the lockfiles.
#
#   scripts/ci/egress-drop.sh --iptables   # CI runner: drop OUTPUT with iptables (needs sudo)
#   scripts/ci/egress-drop.sh --netns      # local: re-exec inside a fresh network namespace
#   scripts/ci/egress-drop.sh --run        # (internal) assume the block is in place, run suite
#
# The script first proves the block works: a connection attempt to a public host must fail.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/../.." && pwd)
cd "$HERE"

canary() {
  # Any successful connection means the block is not in place; fail loudly.
  if curl -sS --max-time 5 -o /dev/null https://example.com/ 2>/dev/null; then
    echo "egress-drop: the canary connection SUCCEEDED; outbound traffic is not blocked." >&2
    exit 1
  fi
  if python3 - <<'EOF' 2>/dev/null; then
import socket
socket.setdefaulttimeout(5)
socket.create_connection(("1.1.1.1", 443)).close()
EOF
    echo "egress-drop: raw TCP canary SUCCEEDED; outbound traffic is not blocked." >&2
    exit 1
  fi
  echo "egress-drop: canary confirmed, outbound traffic is blocked."
}

run_suite() {
  canary
  echo "egress-drop: python tests"
  uv run --offline pytest
  echo "egress-drop: node tests"
  pnpm -r --offline test
  echo "egress-drop: install.sh preflight"
  tmp=$(mktemp -d)
  sh ./install.sh --data-root "$tmp/data" --edge-port 0 || rc=$?
  rc=${rc:-0}
  # 0 = passed, 2 = a blocking check such as no container engine on this runner; both are
  # valid preflight outcomes. Anything else is a script failure.
  case "$rc" in 0|2) ;; *) echo "install.sh exited $rc" >&2; exit "$rc" ;; esac
  echo "egress-drop: done"
}

case "${1:-}" in
  --iptables)
    sudo iptables -I OUTPUT 1 -o lo -j ACCEPT
    sudo iptables -P OUTPUT DROP
    sudo ip6tables -I OUTPUT 1 -o lo -j ACCEPT
    sudo ip6tables -P OUTPUT DROP
    run_suite
    ;;
  --netns)
    # unshare -n gives an empty network namespace: no routes, loopback down. Tests that bind
    # 0.0.0.0 still work; nothing can leave.
    exec unshare -n "$0" --run
    ;;
  --run)
    run_suite
    ;;
  *)
    sed -n '2,10p' "$0" >&2
    exit 2
    ;;
esac
