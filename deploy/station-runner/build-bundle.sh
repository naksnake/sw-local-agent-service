#!/usr/bin/env bash
# Build the offline station-runner bundle on the platform host (CLAUDE.md §5.2, INV-1, INV-8).
#
#   deploy/station-runner/build-bundle.sh [--data-root /AI/Agent] [--out dist/]
#
# The bundle carries everything a station needs with no network: the runner and its
# workspace packages as wheels, their pinned third-party wheels for Linux x86_64 and
# Windows amd64 (from the offline wheel mirror `install.sh` also uses, never from PyPI at
# station install time), the installers, the systemd unit, and the platform CA
# (`slas-ca.pem`) so `enrol` can verify the factory executor. No private key, no code, no
# credential of any kind is in the bundle: a station gets its identity by redeeming a
# one-time code from Admin → Stations.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
data_root="${SLAS_DATA_ROOT:-/AI/Agent}"
out="$repo/dist"
wheel_mirror="${SLAS_WHEEL_MIRROR:-}"   # a directory or a --index-url of the offline mirror

while [ $# -gt 0 ]; do
  case "$1" in
    --data-root) data_root="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --wheel-mirror) wheel_mirror="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done

version="$(grep -m1 '^version' "$repo/services/station-runner/pyproject.toml" | sed 's/.*"\(.*\)"/\1/')"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/slas-station-runner-$version"
mkdir -p "$bundle/wheels"

echo "Building the runner and its workspace packages as wheels."
for package in slas-schemas slas-hal slas-screen slas-skills slas-station-runner; do
  (cd "$repo" && uv build --wheel --package "$package" --out-dir "$bundle/wheels" >/dev/null)
done

echo "Collecting the pinned third-party wheels for Linux and Windows."
(cd "$repo" && uv export --frozen --no-hashes --no-emit-workspace --package slas-station-runner \
  --output-file "$bundle/requirements.txt" >/dev/null)
mirror_args=()
if [ -n "$wheel_mirror" ]; then
  if [ -d "$wheel_mirror" ]; then
    mirror_args=(--no-index --find-links "$wheel_mirror")
  else
    mirror_args=(--index-url "$wheel_mirror")
  fi
fi
for platform in manylinux2014_x86_64 win_amd64; do
  uv run --no-project python -m pip download --quiet --dest "$bundle/wheels" \
    --requirement "$bundle/requirements.txt" --only-binary=:all: \
    --platform "$platform" --python-version 3.12 --implementation cp \
    "${mirror_args[@]}"
done

echo "Adding the installers and the platform CA."
cp "$here/install.sh" "$here/install.ps1" "$here/slas-station-runner.service" "$here/README.md" "$bundle/"
chmod +x "$bundle/install.sh"
ca="$data_root/Factory/ca/ca.pem"
if [ ! -f "$ca" ]; then
  echo "The platform CA is not at $ca yet. It is created the first time the factory executor starts;"
  echo "start the platform once, then build the bundle again."
  exit 1
fi
cp "$ca" "$bundle/slas-ca.pem"
printf 'version=%s\nbuilt=%s\nca_fingerprint=%s\n' "$version" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$(openssl x509 -in "$ca" -noout -fingerprint -sha256 | sed 's/.*=//')" > "$bundle/BUNDLE"

mkdir -p "$out"
tarball="$out/slas-station-runner-$version.tgz"
tar -C "$stage" -czf "$tarball" "slas-station-runner-$version"
echo "Bundle written to $tarball. Copy it to the station and run install.sh (Linux) or install.ps1 (Windows)."
