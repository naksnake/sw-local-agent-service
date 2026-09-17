#!/usr/bin/env bash
# Build the offline install bundle on a connected build host (ADR-0004, ADR-0012).
#
#   scripts/build-bundle.sh [--profile quickstart|prod] [--out dist/]
#
# Builds every first-party image with --network none from the vendored caches, saves every
# image the profile starts as a tarball, writes manifest.json (image IDs and file hashes),
# signs it with the release key (cosign, key-based, offline) and packs the installer, the
# compose files, the configuration and the public key. The bundle carries no private key.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
profile="quickstart"; out="$repo/dist"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
version="$(grep -m1 '^version' "$repo/pyproject.toml" | sed 's/.*"\(.*\)"/\1/')"
registry="${SLAS_REGISTRY:-registry.internal}"
stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
bundle="$stage/slas-bundle-$version"
mkdir -p "$bundle/images" "$bundle/tools"

echo "Building first-party images offline."
# Every first-party image the profile starts, from images/<name>/Dockerfile with the repository
# root as the build context (images/README.md). The sandbox images are not in the lock; they
# are built with the toolchain bundle beside them.
while IFS= read -r name; do
  [[ -f "$repo/images/$name/Dockerfile" ]] || { echo "images/$name/Dockerfile is missing; the lock names it."; exit 1; }
  docker build --network none --quiet -f "$repo/images/$name/Dockerfile" -t "$registry/slas/$name:$version" "$repo" >/dev/null
done < <(python3 -c '
import json, sys
for image in json.load(open(sys.argv[1]))["images"]:
    if image["first_party"] and sys.argv[2] in image["profiles"]:
        print(image["name"])' "$repo/compose/images.lock.json" "$profile")

echo "Saving every image the $profile profile starts."
python3 - "$repo/compose/images.lock.json" "$profile" "$registry" "$version" "$bundle" <<'PY'
import json, subprocess, sys, hashlib, pathlib
lock, profile, registry, version, bundle = sys.argv[1:6]
images = {}
for image in json.load(open(lock))["images"]:
    if profile not in image["profiles"]:
        continue
    ref = f"{registry}/{image['reference']}".replace("${SLAS_VERSION}", version)
    tar = pathlib.Path(bundle, "images", image["name"] + ".tar")
    subprocess.run(["docker", "save", "--output", str(tar), ref], check=True)
    images[ref] = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", ref], check=True, capture_output=True, text=True).stdout.strip()
json.dump(images, open(pathlib.Path(bundle, "image-ids.json"), "w"), indent=2)
PY

cp "$repo/install.sh" "$bundle/"
cp -r "$repo/compose" "$repo/config" "$repo/observability" "$repo/deploy" "$bundle/"
cp -r "$repo/packages" "$bundle/"
mkdir -p "$bundle/scripts"
cp "$repo/scripts/fetch_models.py" "$bundle/scripts/"   # install.sh --models verifies weights with it
cp "$repo/pyproject.toml" "$bundle/"
[[ -f "$repo/config/cosign.pub" ]] && cp "$repo/config/cosign.pub" "$bundle/config/cosign.pub"
command -v cosign >/dev/null && cp "$(command -v cosign)" "$bundle/tools/cosign"

echo "Writing and signing the manifest."
python3 -c '
import json, sys, pathlib
sys.path.insert(0, "'"$repo"'/packages/slas-deploy"); sys.path.insert(0, "'"$repo"'/packages/slas-schemas"); sys.path.insert(0, "'"$repo"'/packages/slas-hal")
from slas_deploy.cosign import write_manifest
bundle = pathlib.Path("'"$bundle"'")
images = json.load(open(bundle / "image-ids.json"))
write_manifest(bundle, "'"$version"'", __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(), images)
'
cosign sign-blob --key cosign.key --tlog-upload=false --yes --output-signature "$bundle/manifest.json.sig" "$bundle/manifest.json"

mkdir -p "$out"
tar -C "$stage" -czf "$out/slas-bundle-$version.tgz" "slas-bundle-$version"
echo "Bundle written to $out/slas-bundle-$version.tgz ($profile). Copy it to the host and run ./install.sh --profile $profile."
