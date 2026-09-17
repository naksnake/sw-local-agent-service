#!/usr/bin/env bash
# SW Local Agent Service — installer (CLAUDE.md §3, ADR-0003, ADR-0004, ADR-0012).
#
#   preflight → verify (bundle signature or Harbor signatures; the image lock; model
#   checksums) → write .env → generate secret files → load or pull images → place model
#   weights and models.yaml → docker compose up → wait healthy → print the URL and the
#   one-time administrator password.
#
# Read-only steps come first; nothing on the host changes until every one of them passed.
# Idempotent: running it twice is safe. `--dry-run` performs the read-only steps for real
# and prints what the changing steps would do.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${SLAS_PROFILE:-quickstart}"
DATA_ROOT="${SLAS_DATA_ROOT:-/AI/Agent}"
BUNDLE_DIR="${SLAS_BUNDLE_DIR:-$SCRIPT_DIR/bundle}"
REGISTRY="${SLAS_REGISTRY:-}"
COSIGN_KEY="${SLAS_COSIGN_KEY:-$SCRIPT_DIR/config/cosign.pub}"
LOCK_FILE="${SLAS_IMAGE_LOCK:-$SCRIPT_DIR/compose/images.lock.json}"
MODELS_DIR="${SLAS_MODELS_DIR:-}"
MODELS_ONLY=0
FETCH_MODELS_FIRST=0
FETCH_PLANNED_ONLY=0
MODEL_SOURCES="${SLAS_MODEL_SOURCES:-$SCRIPT_DIR/config/model-sources.txt}"
VERSION="$(grep -m1 '^version' "$SCRIPT_DIR/pyproject.toml" | sed 's/.*"\(.*\)"/\1/')"
JSON=0
DRY_RUN=0
PREFLIGHT_ONLY=0
SKIP_PREFLIGHT=0

usage() {
  cat <<EOF
Usage: ./install.sh [--profile quickstart|prod] [--data-root PATH] [--bundle DIR | --registry HOST]
                    [--models DIR] [--fetch-models] [--models-only] [--dry-run] [--preflight-only] [--json]

Installs SW Local Agent Service on this host: preflight, verification, .env and secrets,
images, model weights, services, and the sign-in URL. Nothing changes until every read-only
step passed.

  --profile PROFILE    quickstart (default) or prod. Also read from \$SLAS_PROFILE.
  --data-root PATH     Where the platform keeps its data. Default: \$SLAS_DATA_ROOT or /AI/Agent.
  --bundle DIR         Install from an offline bundle (its manifest is verified with cosign in prod).
                       Default: ./bundle when it exists.
  --registry HOST      Pull from a registry inside the perimeter (Harbor); every image is
                       verified with cosign before it is pulled (prod).
  --models DIR         Model weights fetched with scripts/fetch_models.py on a connected host.
                       Their checksums are verified, they are placed under <data root>/Models,
                       and Models/models.yaml is written from config/models.<profile>.yaml
                       when there is none. Default: ./models when it exists; also \$SLAS_MODELS_DIR.
  --fetch-models       Download the profile's model weights first, into the --models directory
                       (default ./models), from the pinned config/model-sources.txt. Resumes
                       and retries; needs a route to the hub (or HTTPS_PROXY / HF_ENDPOINT).
  --models-only        Check and place the model weights and models.yaml, then stop. For a host
                       prepared before the bundle arrives; needs neither a bundle nor a registry.
                       `./install.sh --fetch-models --models-only` is the one-command preparation.
  --dry-run            Run the read-only steps for real; print what the rest would do.
  --preflight-only     Stop after the preflight.
  --json               Print the preflight report as JSON, for scripts.
  -h, --help           Show this help.

Exit codes: 0 ready · 1 the preflight or a verification found problems · 2 wrong usage.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)        PROFILE="${2:-}"; shift 2 ;;
    --profile=*)      PROFILE="${1#*=}"; shift ;;
    --data-root)      DATA_ROOT="${2:-}"; shift 2 ;;
    --data-root=*)    DATA_ROOT="${1#*=}"; shift ;;
    --bundle)         BUNDLE_DIR="${2:-}"; shift 2 ;;
    --bundle=*)       BUNDLE_DIR="${1#*=}"; shift ;;
    --registry)       REGISTRY="${2:-}"; shift 2 ;;
    --registry=*)     REGISTRY="${1#*=}"; shift ;;
    --models)         MODELS_DIR="${2:-}"; shift 2 ;;
    --models=*)       MODELS_DIR="${1#*=}"; shift ;;
    --models-only)    MODELS_ONLY=1; shift ;;
    --fetch-models)   FETCH_MODELS_FIRST=1; shift ;;
    --lock)           LOCK_FILE="${2:-}"; shift 2 ;;
    --cosign-key)     COSIGN_KEY="${2:-}"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --preflight-only) PREFLIGHT_ONLY=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;   # tests and CI dry-runs only; printed loudly
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
What to do: install python3.12 (Debian/Ubuntu: sudo apt install python3.12), then run ./install.sh again. The bundle ships its own Python.
EOF
  exit 1
fi

# From the source tree the packages are imported directly; the bundle ships them installed.
# `services/sandbox-manager` is here for the stdlib-only `slas_sandbox_manager.toolchains`
# module that `slas toolchain` uses. tests/unit/test_host_cli_is_stdlib_only.py reads this
# line, so the test and the installer cannot drift apart.
export PYTHONPATH="$SCRIPT_DIR/packages/slas-cli:$SCRIPT_DIR/packages/slas-kernel:$SCRIPT_DIR/packages/slas-schemas:$SCRIPT_DIR/packages/slas-deploy:$SCRIPT_DIR/packages/slas-hal:$SCRIPT_DIR/packages/slas-observability:$SCRIPT_DIR/services/sandbox-manager${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
fi
FETCH_MODELS="$SCRIPT_DIR/scripts/fetch_models.py"   # standard library only; verifies checksums offline
if [[ -z "$MODELS_DIR" && ( -d "$SCRIPT_DIR/models" || $FETCH_MODELS_FIRST -eq 1 ) ]]; then
  MODELS_DIR="$SCRIPT_DIR/models"
fi

nothing_changed() {
  echo
  echo "$1"
  echo "Nothing was changed on this host."
  exit "${2:-1}"
}

# ---------------------------------------------------------------------------- 1 preflight
if [[ $SKIP_PREFLIGHT -eq 1 ]]; then
  echo "WARNING: the preflight was skipped (--skip-preflight). Only tests and CI dry-runs do this."
else
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
  case "$rc" in
    0) ;;
    1) nothing_changed "Preflight found problems. Fix the problems listed above, then run ./install.sh again." 1 ;;
    *)
      echo
      echo "The preflight itself failed (exit code $rc). Nothing was changed on this host."
      echo "Likely cause: a bug in the installer, not a problem with your host."
      echo "What to do: run ./install.sh again; if it repeats, report the output above."
      exit "$rc" ;;
  esac
  if [[ $PREFLIGHT_ONLY -eq 1 ]]; then
    nothing_changed "Preflight passed." 0
  fi
fi

# ---------------------------------------------------------------------------- 1b fetch the model weights (optional)
# Downloads into the staging directory, never into the data root; the running platform never
# downloads anything (INV-1). Whether the platform host may fetch while it is being prepared is
# CLAUDE.md §15 open decision (13); this step runs only when asked for with --fetch-models.
if [[ $FETCH_MODELS_FIRST -eq 1 ]]; then
  echo
  echo "Fetching the $PROFILE profile's model weights into $MODELS_DIR (from $MODEL_SOURCES)."
  fetch_args=(fetch --sources "$MODEL_SOURCES" --profile "$PROFILE" --dest "$MODELS_DIR")
  [[ $DRY_RUN -eq 1 ]] && fetch_args+=(--dry-run)
  if ! "$PYTHON" "$FETCH_MODELS" "${fetch_args[@]}"; then
    nothing_changed "Fetching the model weights did not finish; the messages above say why. Likely cause: the hub could not be reached, or the disk under $MODELS_DIR is too small. What to do: fix that and run the same command again; the fetch resumes where it stopped." 1
  fi
  if [[ $DRY_RUN -eq 1 && ! -d "$MODELS_DIR" ]]; then
    MODELS_DIR=""   # nothing was downloaded, so there is nothing to place yet
    FETCH_PLANNED_ONLY=1
  fi
fi

# ---------------------------------------------------------------------------- 2 verify (read-only)
if [[ $MODELS_ONLY -eq 0 ]]; then   # --models-only needs neither a bundle nor a registry
echo
echo "Verifying what will be installed ($PROFILE profile)."
SOURCE=""
if [[ -n "$REGISTRY" ]]; then
  SOURCE="registry"
elif [[ -d "$BUNDLE_DIR" ]]; then
  SOURCE="bundle"
elif [[ "$PROFILE" == "prod" ]]; then
  REGISTRY="harbor.internal"
  SOURCE="registry"
else
  nothing_changed "No bundle was found at $BUNDLE_DIR and no --registry was given. Likely cause: the bundle tarball was not unpacked next to install.sh. What to do: tar xzf slas-bundle-<version>.tgz here, or pass --bundle DIR." 1
fi
REGISTRY="${REGISTRY:-registry.internal}"

if [[ "$PROFILE" == "prod" ]]; then
  if ! command -v cosign >/dev/null 2>&1; then
    nothing_changed "cosign is not installed, and the prod profile verifies every image with it. Likely cause: the host was prepared without the bundle's tools/ directory. What to do: install cosign (pinned build in the bundle), then run ./install.sh again." 1
  fi
  if [[ ! -f "$COSIGN_KEY" ]]; then
    nothing_changed "The release public key $COSIGN_KEY is not on this host. Likely cause: config/cosign.pub was not copied with the release. What to do: copy it from the release you are installing and compare its fingerprint with the release notes." 1
  fi
fi

if [[ "$SOURCE" == "bundle" ]]; then
  if [[ "$PROFILE" == "prod" ]]; then
    if [[ ! -f "$BUNDLE_DIR/manifest.json" || ! -f "$BUNDLE_DIR/manifest.json.sig" ]]; then
      nothing_changed "The bundle at $BUNDLE_DIR has no signed manifest. Likely cause: an incomplete download or a bundle built without signing. What to do: download the complete release bundle again." 1
    fi
    if ! cosign verify-blob --key "$COSIGN_KEY" --signature "$BUNDLE_DIR/manifest.json.sig" \
         --insecure-ignore-tlog --private-infrastructure "$BUNDLE_DIR/manifest.json" >/dev/null 2>&1; then
      nothing_changed "The signature on the bundle manifest does not verify. Likely cause: the bundle was altered after signing, or the key is not the release key. What to do: download the bundle again and compare the key fingerprint with the release notes." 1
    fi
    echo "Verified the bundle manifest signature with $COSIGN_KEY."
  fi
  if ! "$PYTHON" -m slas_deploy.installer check-manifest --lock "$LOCK_FILE" \
       --manifest "$BUNDLE_DIR/manifest.json" --profile "$PROFILE" --registry "$REGISTRY"; then
    nothing_changed "The bundle does not match the image lock." 1
  fi
else
  if ! "$PYTHON" -m slas_deploy.installer check-lock --lock "$LOCK_FILE" --profile "$PROFILE"; then
    nothing_changed "The image lock does not pin every image the $PROFILE profile starts." 1
  fi
  if [[ "$PROFILE" == "prod" ]]; then
    echo "Verifying every image's signature in $REGISTRY."
    while IFS= read -r ref; do
      [[ -z "$ref" ]] && continue
      if ! cosign verify --key "$COSIGN_KEY" --insecure-ignore-tlog --private-infrastructure "$ref" >/dev/null 2>&1; then
        nothing_changed "$ref is not signed by the release key. Likely cause: Harbor holds an image that is not from the release, or its cosign artifact is missing. What to do: push the release's signed images to Harbor and run ./install.sh again." 1
      fi
    done < <("$PYTHON" - "$LOCK_FILE" "$PROFILE" "$REGISTRY" "$VERSION" <<'PY'
import json, sys
lock, profile, registry, version = sys.argv[1:5]
for image in json.load(open(lock))["images"]:
    if profile in image["profiles"]:
        ref = f"{registry}/{image['reference']}".replace("${SLAS_VERSION}", version)
        digest = image.get("digest")
        print(f"{ref.split(':')[0]}@{digest}" if digest and not image["first_party"] else ref)
PY
)
    echo "Every image is signed by the release key."
  fi
fi

fi  # MODELS_ONLY

# ---------------------------------------------------------------------------- 2b model weights (read-only)
# Weights are fetched by scripts/fetch_models.py on a connected host (INV-1). Here they are only
# checked; copying and models.yaml happen with the other changes below, or alone with
# --models-only on a host prepared before the bundle arrives.
MODELS_TARGET="$DATA_ROOT/Models"
MODELS_TEMPLATE="$SCRIPT_DIR/config/models.$PROFILE.yaml"
MODELS_TO_COPY=()
MODELS_PRESENT=()
MODELS_COPY_BYTES=0
MODELS_SAME_VOLUME=0
list_models() {  # the model directories under $1: those that carry a SHA256SUMS
  local dir
  for dir in "$1"/*/; do
    [[ -f "$dir/SHA256SUMS" ]] && basename "$dir"
  done
  return 0
}
nearest_existing() {  # the closest existing ancestor of $1, for df and stat
  local p="$1"
  while [[ ! -e "$p" ]]; do p="$(dirname "$p")"; done
  printf '%s\n' "$p"
}
human_size() { numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || echo "$1 bytes"; }
if [[ -n "$MODELS_DIR" ]]; then
  if [[ ! -d "$MODELS_DIR" ]]; then
    nothing_changed "The models directory $MODELS_DIR does not exist. Likely cause: a typo in --models or SLAS_MODELS_DIR, or the weights were not copied to this host yet. What to do: on a connected host run scripts/fetch_models.py fetch --sources config/model-sources.txt --profile $PROFILE --dest <dir>, bring <dir> here, and pass --models <dir>." 1
  fi
  MODELS_DIR="$(realpath "$MODELS_DIR")"
  mapfile -t found_models < <(list_models "$MODELS_DIR")
  if [[ ${#found_models[@]} -eq 0 ]]; then
    nothing_changed "No model weights were found in $MODELS_DIR: no <model>/SHA256SUMS. Likely cause: the directory is not the --dest of scripts/fetch_models.py, or the fetch did not finish. What to do: run the fetch again on the connected host; it resumes and writes SHA256SUMS when a model is complete." 1
  fi
  for name in "${found_models[@]}"; do
    if [[ -f "$MODELS_TARGET/$name/SHA256SUMS" ]]; then
      if cmp -s "$MODELS_DIR/$name/SHA256SUMS" "$MODELS_TARGET/$name/SHA256SUMS"; then
        MODELS_PRESENT+=("$name")
      else
        nothing_changed "$MODELS_TARGET/$name holds a different revision of $name than $MODELS_DIR/$name: their SHA256SUMS differ. Likely cause: the pin for $name in config/model-sources.txt was moved and the model was fetched again. What to do: move the old directory away (for example to $MODELS_TARGET/$name.old) or delete it, then run ./install.sh again; the new revision is copied in its place." 1
      fi
    elif [[ -e "$MODELS_TARGET/$name" ]]; then
      nothing_changed "$MODELS_TARGET/$name exists but has no SHA256SUMS, so the installer cannot tell what it holds. Likely cause: a copy made by hand, or a copy that was cut short. What to do: move or delete that directory, then run ./install.sh again." 1
    else
      MODELS_TO_COPY+=("$name")
    fi
  done
  if [[ ${#MODELS_TO_COPY[@]} -gt 0 ]]; then
    echo
    echo "Checking the model weights in $MODELS_DIR against their checksums (large models take a few minutes)."
    verify_args=(verify --dest "$MODELS_DIR")
    for name in "${MODELS_TO_COPY[@]}"; do verify_args+=(--model "$name"); done
    if ! "$PYTHON" "$FETCH_MODELS" "${verify_args[@]}"; then
      nothing_changed "The model weights in $MODELS_DIR do not match their checksums. Likely cause: an interrupted copy or fetch. What to do: copy the listed files again (or run the fetch again on the connected host), then run ./install.sh again." 1
    fi
    copy_paths=()
    for name in "${MODELS_TO_COPY[@]}"; do copy_paths+=("$MODELS_DIR/$name"); done
    MODELS_COPY_BYTES="$(du -sbc "${copy_paths[@]}" 2>/dev/null | tail -1 | cut -f1 || true)"
    MODELS_COPY_BYTES="${MODELS_COPY_BYTES:-0}"
    target_parent="$(nearest_existing "$MODELS_TARGET")"
    if [[ "$(stat -L -c %d "$MODELS_DIR")" == "$(stat -L -c %d "$target_parent")" ]]; then
      MODELS_SAME_VOLUME=1   # hard links are tried first: instant, and they cost no space
    else
      free_bytes="$(df --output=avail -B1 "$target_parent" | tail -1 | tr -d ' ')"
      if [[ "$MODELS_COPY_BYTES" -gt "$free_bytes" ]]; then
        nothing_changed "Not enough free disk under $MODELS_TARGET for the model weights: $(human_size "$MODELS_COPY_BYTES") to copy, $(human_size "$free_bytes") free. Likely cause: the data root's volume is smaller than this set of models. What to do: free space, mount a larger volume at $MODELS_TARGET, or fetch the weights straight into $MODELS_TARGET; then run ./install.sh again." 1
      fi
    fi
  fi
else
  if [[ -d "$MODELS_TARGET" ]]; then
    mapfile -t MODELS_PRESENT < <(list_models "$MODELS_TARGET")
  fi
  if [[ ${#MODELS_PRESENT[@]} -eq 0 && $FETCH_PLANNED_ONLY -eq 0 ]]; then
    echo
    echo "No model weights were given: no --models DIR and no ./models next to install.sh."
    echo "The platform installs without models. Fetch them on a connected host with scripts/fetch_models.py fetch --sources config/model-sources.txt --profile $PROFILE --dest <dir>, then run ./install.sh --models <dir>; nothing else needs to change."
  fi
fi

place_models() {  # the changing half of the model step: copy, verify, manifest, models.yaml
  local name src noun linked copied failed registry_from_template wanted have
  if [[ ${#MODELS_TO_COPY[@]} -gt 0 ]]; then
    noun="model"; [[ ${#MODELS_TO_COPY[@]} -gt 1 ]] && noun="models"
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would copy ${#MODELS_TO_COPY[@]} $noun ($(human_size "$MODELS_COPY_BYTES")) from $MODELS_DIR into $MODELS_TARGET: ${MODELS_TO_COPY[*]}."
    else
      mkdir -p "$MODELS_TARGET"
      echo "Copying ${#MODELS_TO_COPY[@]} $noun ($(human_size "$MODELS_COPY_BYTES")) into $MODELS_TARGET: ${MODELS_TO_COPY[*]}. Large models take a while."
      linked=()
      copied=()
      for name in "${MODELS_TO_COPY[@]}"; do
        src="$(realpath "$MODELS_DIR/$name")"
        rm -rf "$MODELS_TARGET/$name.part"   # a copy this script left unfinished earlier
        if [[ $MODELS_SAME_VOLUME -eq 1 ]] && cp -al "$src" "$MODELS_TARGET/$name.part" 2>/dev/null; then
          linked+=("$name")
        else
          rm -rf "$MODELS_TARGET/$name.part"
          if ! cp -a --reflink=auto "$src" "$MODELS_TARGET/$name.part"; then
            rm -rf "$MODELS_TARGET/$name.part"
            echo "Copying $name into $MODELS_TARGET did not finish."
            echo "Likely cause: the disk ran out of space, or a file could not be read."
            echo "What to do: check the free space under $MODELS_TARGET and the messages above, then run ./install.sh again; the models already placed are kept."
            exit 1
          fi
          copied+=("$name")
        fi
        mv -T "$MODELS_TARGET/$name.part" "$MODELS_TARGET/$name"
      done
      failed=0
      for name in ${copied[@]+"${copied[@]}"}; do
        if ! "$PYTHON" "$FETCH_MODELS" verify --dest "$MODELS_TARGET" --model "$name"; then
          rm -rf "$MODELS_TARGET/$name"
          failed=1
        fi
      done
      if [[ $failed -eq 1 ]]; then
        echo "Some copied model weights under $MODELS_TARGET did not match their checksums and were removed."
        echo "Likely cause: a read error on the source disk or a write error on the destination during the copy."
        echo "What to do: check both disks, then run ./install.sh again; the removed models are copied anew."
        exit 1
      fi
      echo "Placed ${#MODELS_TO_COPY[@]} $noun under $MODELS_TARGET: ${#linked[@]} linked on the same volume, ${#copied[@]} copied and verified again."
      if [[ -f "$MODELS_DIR/manifest.json" ]]; then
        "$PYTHON" "$FETCH_MODELS" merge-manifest --dest "$MODELS_TARGET" --from "$MODELS_DIR/manifest.json"
      fi
    fi
  fi
  if [[ ${#MODELS_PRESENT[@]} -gt 0 ]]; then
    noun="model is"; [[ ${#MODELS_PRESENT[@]} -gt 1 ]] && noun="models are"
    echo "${#MODELS_PRESENT[@]} $noun already under $MODELS_TARGET: ${MODELS_PRESENT[*]}."
  fi
  if [[ $(( ${#MODELS_TO_COPY[@]} + ${#MODELS_PRESENT[@]} )) -gt 0 ]]; then
    registry_from_template=0
    if [[ -f "$MODELS_TARGET/models.yaml" ]]; then
      echo "Kept $MODELS_TARGET/models.yaml as it is; the $PROFILE template was not applied over your registry."
    elif [[ $DRY_RUN -eq 1 ]]; then
      echo "Would write $MODELS_TARGET/models.yaml from config/models.$PROFILE.yaml."
      registry_from_template=1
    else
      cp "$MODELS_TEMPLATE" "$MODELS_TARGET/models.yaml"
      echo "Wrote $MODELS_TARGET/models.yaml from the $PROFILE template; it assumes GPUs of about 288 GB (HGX B300 class). Change roles on the Models page or in that file; no restart is needed."
      registry_from_template=1
    fi
    if [[ $registry_from_template -eq 1 ]]; then
      if [[ "$PROFILE" == "quickstart" ]]; then
        echo "Quickstart declares two voters from two model families, so every cross-check is reported as a weaker check until a third family is added (config/models.prod.yaml shows one)."
      fi
      # The template names the weights the profile expects; say which are not here yet.
      while IFS= read -r wanted; do
        [[ -z "$wanted" ]] && continue
        have=0
        for name in ${MODELS_TO_COPY[@]+"${MODELS_TO_COPY[@]}"} ${MODELS_PRESENT[@]+"${MODELS_PRESENT[@]}"}; do
          [[ "$name" == "$wanted" ]] && have=1
        done
        if [[ $have -eq 0 ]]; then
          echo "models.yaml names $wanted, but no weights for it are here yet. Fetch it with scripts/fetch_models.py and run ./install.sh --models <dir> again, or remove it from models.yaml."
        fi
      done < <(sed -n 's/^    path: "\(.*\)"$/\1/p' "$MODELS_TEMPLATE")
    fi
  fi
}

if [[ $MODELS_ONLY -eq 1 ]]; then
  if [[ $FETCH_PLANNED_ONLY -eq 1 ]]; then
    nothing_changed "Dry run finished: the fetch plan above checks out; run the same command without --dry-run to download and place the weights." 0
  fi
  if [[ -z "$MODELS_DIR" ]]; then
    nothing_changed "--models-only needs the weights: pass --models DIR or put them in ./models next to install.sh." 2
  fi
  echo
  place_models
  echo
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "Dry run finished: the model weights check out; nothing was changed on this host."
  else
    echo "The model weights are in place under $MODELS_TARGET. Run ./install.sh with the bundle to install the platform; it finds them there."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------- 3 the changes
run_or_print() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "Would run: $*"
  else
    "$@"
  fi
}

TLS_NAMES="127.0.0.1,localhost,$(hostname -f 2>/dev/null || hostname)"
PUBLIC_HOST="${SLAS_PUBLIC_HOST:-$(hostname -f 2>/dev/null || hostname)}"
ENV_FILE="$DATA_ROOT/.env"

echo
if [[ $DRY_RUN -eq 1 ]]; then
  echo "Would write $ENV_FILE ($PROFILE keys, registry $REGISTRY, version $VERSION; keys you set are kept)."
  echo "Would create the missing secret files under $DATA_ROOT/secrets (0600)."
else
  mkdir -p "$DATA_ROOT"
  "$PYTHON" -m slas_deploy.installer write-env --example "$SCRIPT_DIR/config/.env.example" \
    --target "$ENV_FILE" --profile "$PROFILE" --data-root "$DATA_ROOT" --version "$VERSION" \
    --registry "$REGISTRY" --uid "$(id -u)" --gid "$(id -g)" --tls-names "$TLS_NAMES" \
    --public-host "$PUBLIC_HOST"
  "$PYTHON" -m slas_deploy.installer secrets --dir "$DATA_ROOT/secrets" --profile "$PROFILE"
fi

if [[ "$SOURCE" == "bundle" ]]; then
  for tarball in "$BUNDLE_DIR"/images/*.tar; do
    [[ -e "$tarball" ]] || continue
    run_or_print docker load --quiet --input "$tarball"
  done
fi

# ---------------------------------------------------------------------------- 3b model weights
place_models

if [[ $DRY_RUN -eq 1 ]]; then
  COMPOSE_FILES=("$SCRIPT_DIR/compose/docker-compose.yml")
  [[ "$PROFILE" == "prod" ]] && COMPOSE_FILES+=("$SCRIPT_DIR/compose/prod.override.yml")
else
  mapfile -t COMPOSE_FILES < <("$PYTHON" -m slas_deploy.installer compose-files --env "$ENV_FILE" \
    --profile "$PROFILE" --compose-dir "$SCRIPT_DIR/compose" | "$PYTHON" -c 'import json,sys; print("\n".join(json.load(sys.stdin)))')
fi
COMPOSE=(docker compose --project-name slas --project-directory "$SCRIPT_DIR/compose")
for f in "${COMPOSE_FILES[@]}"; do COMPOSE+=(-f "$f"); done
if [[ $DRY_RUN -eq 0 ]]; then COMPOSE+=(--env-file "$ENV_FILE"); fi

if [[ "$SOURCE" == "registry" ]]; then
  run_or_print "${COMPOSE[@]}" pull --quiet
fi
run_or_print "${COMPOSE[@]}" up -d --pull never --remove-orphans

if [[ "$PROFILE" == "prod" ]]; then
  run_or_print "${COMPOSE[@]}" exec -T vault sh /vault/bootstrap.sh
fi

# ---------------------------------------------------------------------------- 4 wait and report
if [[ $DRY_RUN -eq 1 ]]; then
  echo "Would wait until every service reports healthy, then print https://$PUBLIC_HOST and the one-time administrator password."
  echo
  echo "Dry run finished: every read-only step passed; nothing was changed on this host."
  exit 0
fi

echo "Waiting for the services to report healthy."
for _ in $(seq 1 60); do
  unhealthy="$("${COMPOSE[@]}" ps --format json | "$PYTHON" -c '
import json, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
bad = [r.get("Service") or r.get("Name") for r in rows
       if (r.get("State") != "running" and r.get("State") != "exited") or (r.get("Health") not in ("", "healthy", None))]
print(" ".join(str(b) for b in bad))' 2>/dev/null || echo "compose")"
  if [[ -z "$unhealthy" ]]; then
    break
  fi
  sleep 5
done
if [[ -n "${unhealthy:-}" ]]; then
  echo "Some services are not healthy yet: $unhealthy."
  echo "Likely cause: a slow first start (models, Keycloak) or a failed dependency."
  echo "What to do: run \`slas status\` in a minute; \`slas logs <service>\` shows why one is not up."
  exit 1
fi

echo
echo "SW Local Agent Service is up. Sign in at https://$PUBLIC_HOST"
ADMIN_PASSWORD_FILE="$DATA_ROOT/secrets/admin-initial-password"
if [[ -f "$ADMIN_PASSWORD_FILE" ]]; then
  echo "Administrator: admin@slas.local — one-time password: $(cat "$ADMIN_PASSWORD_FILE") (you will choose a new one at first sign-in)."
fi
if [[ "$PROFILE" == "prod" ]]; then
  echo "Keycloak: https://$PUBLIC_HOST/auth (realm slas). Vault: unseal keys were printed by the bootstrap; store them offline now."
fi
exit 0
