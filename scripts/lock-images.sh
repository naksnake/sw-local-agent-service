#!/usr/bin/env bash
# Fill compose/images.lock.* on a connected build host (ADR-0003, ADR-0012, INV-8).
#
#   scripts/lock-images.sh [--sign]
#
# For every third-party image in the lock: pull the pinned tag from its upstream, record the
# manifest digest and the image ID, retag it for ${SLAS_REGISTRY}. With --sign, sign every
# image with the release key (cosign, key-based, no transparency log) after pushing it to
# Harbor. First-party images are built by scripts/build-bundle.sh and recorded there. The
# result is committed: the lock in git is what install.sh trusts.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
lock="$repo/compose/images.lock.json"
registry="${SLAS_REGISTRY:-harbor.internal}"
sign=0
[[ "${1:-}" == "--sign" ]] && sign=1

python3 - "$lock" "$registry" "$sign" <<'PY'
import json, subprocess, sys
lock_path, registry, sign = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
lock = json.load(open(lock_path))
def run(*argv):
    return subprocess.run(list(argv), check=True, capture_output=True, text=True).stdout.strip()
for image in lock["images"]:
    if image["first_party"]:
        continue
    upstream = image["upstream"]
    print(f"pull {upstream}")
    run("docker", "pull", "--quiet", upstream)
    digest = run("docker", "inspect", "--format", "{{index .RepoDigests 0}}", upstream).split("@", 1)[1]
    image_id = run("docker", "inspect", "--format", "{{.Id}}", upstream)
    target = f"{registry}/{image['reference']}"
    run("docker", "tag", upstream, target)
    image["digest"], image["image_id"] = digest, image_id
    if sign:
        run("docker", "push", "--quiet", target)
        run("cosign", "sign", "--key", "cosign.key", "--tlog-upload=false", "--yes", f"{target.split(':')[0]}@{digest}")
        image["signed_by"] = "slas-release"
    print(f"  {digest} {image_id}")
json.dump(lock, open(lock_path, "w"), indent=2)
open(lock_path, "a").write("\n")
PY
echo "Lock filled. Render the YAML twin and commit both: uv run python -m slas_deploy.render && git add compose/images.lock.*"
