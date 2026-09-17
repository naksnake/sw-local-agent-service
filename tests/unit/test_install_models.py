"""`./install.sh --models DIR`: the weights are checked before anything changes, placed under
<data root>/Models (hard links on one volume, a verified copy otherwise), and
Models/models.yaml comes from the profile's template once and is never overwritten.
`--models-only` does the same without a bundle. Docker is a stub that logs its argv; the
preflight is skipped explicitly because the runner has no GPU."""

from __future__ import annotations

# ruff: noqa: E501 — long literal sentences and argv lists read better unwrapped
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from slas_deploy.cosign import write_manifest
from slas_deploy.images import default_lock

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
VERSION = re.search(r'^version = "(.*)"', (REPO_ROOT / "pyproject.toml").read_text(), re.M).group(1)  # type: ignore[union-attr]

STUB_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
exit 0
"""
# A cp whose hard-link form fails, as it does across bind mounts: the installer must fall back.
STUB_CP_NO_LINKS = """#!/usr/bin/env bash
for a in "$@"; do [[ "$a" == "-al" ]] && exit 1; done
exec /bin/cp "$@"
"""
# A cp that copies, then damages the staged weights: the in-place verification must catch it.
STUB_CP_DAMAGING = """#!/usr/bin/env bash
for a in "$@"; do [[ "$a" == "-al" ]] && exit 1; done
/bin/cp "$@" || exit $?
dest="${@: -1}"
[[ -f "$dest/model.safetensors" ]] && printf 'damaged' > "$dest/model.safetensors"
exit 0
"""
WEIGHTS = bytes(range(256)) * 64


def make_bundle(tmp_path: Path) -> tuple[Path, Path]:
    """A quickstart bundle whose image ids match a pinned copy of the lock."""
    bundle = tmp_path / "bundle"
    (bundle / "images").mkdir(parents=True, exist_ok=True)  # a test may install twice
    ids: dict[str, str] = {}
    lock = default_lock()
    pinned = []
    for image in lock.images:
        image_id = "sha256:" + format(abs(hash(image.name)) % (16**16), "016x") * 4
        ref = f"registry.internal/{image.reference}".replace("${SLAS_VERSION}", VERSION)
        ids[ref] = image_id
        (bundle / "images" / f"{image.name}.tar").write_bytes(image.name.encode())
        pinned.append(
            image.model_copy(update={"digest": "sha256:" + "c" * 64, "image_id": image_id})
        )
    write_manifest(bundle, VERSION, "2026-09-16T10:00:00Z", ids)
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(
        json.dumps(lock.model_copy(update={"images": pinned}).model_dump(mode="json"))
    )
    return bundle, lock_path


def make_models(root: Path, *names: str) -> Path:
    """What scripts/fetch_models.py leaves behind: <model>/files + SHA256SUMS, one manifest."""
    for name in names:
        model = root / name
        model.mkdir(parents=True)
        files = {
            "model.safetensors": WEIGHTS + name.encode(),
            "config.json": b'{"architectures": ["Demo"]}\n',
        }
        for filename, content in files.items():
            (model / filename).write_bytes(content)
        (model / "SHA256SUMS").write_text(
            "".join(f"{hashlib.sha256(c).hexdigest()}  {f}\n" for f, c in sorted(files.items()))
        )
    manifest = root / "manifest.json"
    known = json.loads(manifest.read_text())["models"] if manifest.exists() else []
    manifest.write_text(
        json.dumps({"models": [*known, *({"path": n, "commit": "c1"} for n in names)]})
    )
    return root


def run_install(
    tmp_path: Path,
    *args: str,
    with_bundle: bool = True,
    cp_stub: str | None = None,
    env_extra: dict[str, str] | None = None,
    script: Path = INSTALL_SH,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name, body in (("docker", STUB_DOCKER), ("cp", cp_stub)):
        path = stubs / name
        if body is None:
            path.unlink(missing_ok=True)
            continue
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLAS_") and k != "PYTHONPATH"}
    env.update({"PATH": f"{stubs}:{env['PATH']}", "STUB_LOG": str(log)})
    env.update(env_extra or {})
    data_root = tmp_path / "data"
    argv = ["bash", str(script), "--skip-preflight", "--data-root", str(data_root)]
    if with_bundle:
        bundle, lock = make_bundle(tmp_path)
        argv += [
            "--profile",
            "quickstart",
            "--bundle",
            str(bundle),
            "--lock",
            str(lock),
            "--registry",
            "",
        ]
    result = subprocess.run(
        [*argv, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
        check=False,
    )
    return result, [line for line in log.read_text().splitlines() if line], data_root


def inode(path: Path) -> int:
    return path.stat().st_ino


def test_dry_run_checks_the_weights_and_says_what_it_would_copy_and_write(tmp_path: Path) -> None:
    models = make_models(tmp_path / "carried", "bge-m3", "demo-extra")
    result, calls, data_root = run_install(tmp_path, "--dry-run", "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert f"Checking the model weights in {models} against their checksums" in out
    assert "bge-m3, demo-extra matches its checksums." in out
    assert (
        "Would copy 2 models (" in out
        and f"from {models} into {data_root}/Models: bge-m3 demo-extra." in out
    )
    assert f"Would write {data_root}/Models/models.yaml from config/models.quickstart.yaml." in out
    assert "Quickstart declares two voters from two model families" in out
    for missing in ("deepseek-v4-flash", "qwen3.8-27b-fp8", "bge-reranker-v2-m3"):
        assert f"models.yaml names {missing}, but no weights for it are here yet." in out
    assert "models.yaml names bge-m3," not in out
    assert (
        out.index("Would run: docker load")
        < out.index("Would copy 2 models")
        < out.index("Would run: docker compose")
    )
    assert not data_root.exists(), "a dry run writes nothing"
    assert not calls, "docker is only described in a dry run"


def test_install_links_the_weights_on_one_volume_writes_models_yaml_once_and_is_idempotent(
    tmp_path: Path,
) -> None:
    models = make_models(tmp_path / "carried", "bge-m3", "demo-extra")
    result, calls, data_root = run_install(tmp_path, "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "Copying 2 models (" in out and "Large models take a while." in out
    target = data_root / "Models"
    assert (
        f"Placed 2 models under {target}: 2 linked on the same volume, 0 copied and verified again."
        in out
    )
    for name in ("bge-m3", "demo-extra"):
        assert (target / name / "SHA256SUMS").read_text() == (
            models / name / "SHA256SUMS"
        ).read_text()
        assert inode(target / name / "model.safetensors") == inode(
            models / name / "model.safetensors"
        ), "hard links"
        assert not (target / f"{name}.part").exists()
    assert {m["path"] for m in json.loads((target / "manifest.json").read_text())["models"]} == {
        "bge-m3",
        "demo-extra",
    }
    template = (REPO_ROOT / "config" / "models.quickstart.yaml").read_text()
    assert (target / "models.yaml").read_text() == template
    assert (
        f"Wrote {target}/models.yaml from the quickstart template; it assumes GPUs of about 288 GB"
        in out
    )
    assert "models.yaml names deepseek-v4-flash, but no weights for it are here yet." in out
    assert any(c.startswith("docker compose") and " up -d --pull never" in c for c in calls)
    assert "SW Local Agent Service is up." in out

    # Run again with a third model, a stale .part and an edited registry: only the new model
    # is checked and placed, the registry stays, the manifest gains the new entry.
    make_models(tmp_path / "carried", "third")
    (target / "third.part").mkdir()
    (target / "third.part" / "junk").write_text("left over")
    (target / "models.yaml").write_text(template + "# edited by the operator\n")
    result, _, _ = run_install(tmp_path, "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "third matches its checksums." in result.stdout and "bge-m3, " not in result.stdout
    assert f"Placed 1 model under {target}: 1 linked" in result.stdout
    assert f"2 models are already under {target}: bge-m3 demo-extra." in result.stdout
    assert f"Kept {target}/models.yaml as it is" in result.stdout
    assert (target / "models.yaml").read_text().endswith("# edited by the operator\n")
    assert not (target / "third.part").exists() and (target / "third" / "SHA256SUMS").exists()
    assert {m["path"] for m in json.loads((target / "manifest.json").read_text())["models"]} == {
        "bge-m3",
        "demo-extra",
        "third",
    }

    # Without --models, the weights already in place are still reported.
    result, _, _ = run_install(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"3 models are already under {target}" in result.stdout
    assert "No model weights were given" not in result.stdout


def test_when_links_are_not_possible_the_weights_are_copied_and_verified_again(
    tmp_path: Path,
) -> None:
    models = make_models(tmp_path / "carried", "bge-m3")
    result, _, data_root = run_install(tmp_path, "--models", str(models), cp_stub=STUB_CP_NO_LINKS)
    assert result.returncode == 0, result.stdout + result.stderr
    target = data_root / "Models"
    assert (
        f"Placed 1 model under {target}: 0 linked on the same volume, 1 copied and verified again."
        in result.stdout
    )
    assert result.stdout.count("bge-m3 matches its checksums.") == 2, "before and after the copy"
    assert inode(target / "bge-m3" / "model.safetensors") != inode(
        models / "bge-m3" / "model.safetensors"
    )
    assert (target / "bge-m3" / "model.safetensors").read_bytes() == (
        models / "bge-m3" / "model.safetensors"
    ).read_bytes()


def test_a_copy_that_comes_out_wrong_is_removed_and_redone_on_the_next_run(tmp_path: Path) -> None:
    models = make_models(tmp_path / "carried", "bge-m3")
    result, _, data_root = run_install(tmp_path, "--models", str(models), cp_stub=STUB_CP_DAMAGING)
    assert result.returncode == 1
    target = data_root / "Models"
    assert "bge-m3/model.safetensors does not match its checksum." in result.stdout
    assert (
        f"Some copied model weights under {target} did not match their checksums and were removed."
        in result.stdout
    )
    assert "Likely cause: a read error on the source disk" in result.stdout
    assert (
        "What to do: check both disks, then run ./install.sh again; the removed models are copied anew."
        in result.stdout
    )
    assert not (target / "bge-m3").exists() and not (target / "bge-m3.part").exists()
    assert not (target / "models.yaml").exists()

    result, _, _ = run_install(tmp_path, "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Copying 1 model (" in result.stdout and (target / "bge-m3" / "SHA256SUMS").exists()


def test_weights_fetched_straight_into_the_data_root_are_not_copied(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    models = make_models(data_root / "Models", "bge-m3")
    result, _, _ = run_install(tmp_path, "--dry-run", "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"1 model is already under {data_root}/Models: bge-m3." in result.stdout
    assert "Would copy" not in result.stdout and "Checking the model weights" not in result.stdout
    assert (
        f"Would write {data_root}/Models/models.yaml from config/models.quickstart.yaml."
        in result.stdout
    )


def test_bad_missing_or_conflicting_weights_stop_before_anything_changes(tmp_path: Path) -> None:
    models = make_models(tmp_path / "carried", "bge-m3")
    (models / "bge-m3" / "model.safetensors").write_bytes(b"cut short")
    result, calls, data_root = run_install(tmp_path, "--models", str(models))
    assert result.returncode == 1
    assert "bge-m3/model.safetensors does not match its checksum." in result.stdout
    assert f"The model weights in {models} do not match their checksums." in result.stdout
    assert "Nothing was changed on this host." in result.stdout
    assert not data_root.exists() and not calls

    result, _, data_root = run_install(tmp_path, "--models", str(tmp_path / "nowhere"))
    assert result.returncode == 1
    assert f"The models directory {tmp_path / 'nowhere'} does not exist." in result.stdout
    assert "--profile quickstart --dest <dir>" in result.stdout
    assert not data_root.exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    result, _, data_root = run_install(tmp_path, "--models", str(empty))
    assert result.returncode == 1
    assert f"No model weights were found in {empty}: no <model>/SHA256SUMS." in result.stdout
    assert not data_root.exists()

    # A directory already under Models/ without SHA256SUMS, or with a different revision.
    good = make_models(tmp_path / "good", "bge-m3")
    (data_root / "Models" / "bge-m3").mkdir(parents=True)
    (data_root / "Models" / "bge-m3" / "old.bin").write_bytes(b"?")
    result, _, _ = run_install(tmp_path, "--models", str(good))
    assert result.returncode == 1
    assert f"{data_root}/Models/bge-m3 exists but has no SHA256SUMS" in result.stdout
    assert "Nothing was changed on this host." in result.stdout
    (data_root / "Models" / "bge-m3" / "SHA256SUMS").write_text("0" * 64 + "  model.safetensors\n")
    result, _, _ = run_install(tmp_path, "--models", str(good))
    assert result.returncode == 1
    assert f"{data_root}/Models/bge-m3 holds a different revision of bge-m3" in result.stdout
    assert "the pin for bge-m3 in config/model-sources.txt was moved" in result.stdout
    assert not (data_root / ".env").exists()


def test_models_only_places_the_weights_without_a_bundle(tmp_path: Path) -> None:
    models = make_models(tmp_path / "carried", "bge-m3")
    result, calls, data_root = run_install(
        tmp_path,
        "--profile",
        "prod",
        "--models-only",
        "--dry-run",
        f"--models={models}",
        with_bundle=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Would copy 1 model (" in result.stdout
    assert (
        f"Would write {data_root}/Models/models.yaml from config/models.prod.yaml." in result.stdout
    )
    assert "Quickstart declares" not in result.stdout
    assert (
        "Dry run finished: the model weights check out; nothing was changed on this host."
        in result.stdout
    )
    assert (
        "Verifying what will be installed" not in result.stdout and "No bundle" not in result.stdout
    )
    assert not data_root.exists() and not calls

    result, calls, data_root = run_install(
        tmp_path, "--profile", "prod", "--models-only", "--models", str(models), with_bundle=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    target = data_root / "Models"
    assert f"Placed 1 model under {target}" in result.stdout
    assert (target / "models.yaml").read_text() == (
        REPO_ROOT / "config" / "models.prod.yaml"
    ).read_text()
    assert "Wrote " in result.stdout and "from the prod template" in result.stdout
    assert "models.yaml names minimax-m2.7, but no weights for it are here yet." in result.stdout
    assert (
        f"The model weights are in place under {target}. Run ./install.sh with the bundle to install the platform"
        in result.stdout
    )
    assert not (data_root / ".env").exists() and not (data_root / "secrets").exists() and not calls

    result, _, _ = run_install(tmp_path, "--models-only", with_bundle=False)
    assert result.returncode == 2 and "--models-only needs the weights" in result.stdout


def test_the_default_models_directory_and_the_environment_variable_are_found(
    tmp_path: Path,
) -> None:
    # A mirror of the repository with a models/ directory next to install.sh.
    mirror = tmp_path / "repo"
    mirror.mkdir()
    for entry in REPO_ROOT.iterdir():
        (mirror / entry.name).symlink_to(entry)
    make_models(mirror / "models", "bge-m3")
    result, _, data_root = run_install(
        tmp_path, "--models-only", "--dry-run", with_bundle=False, script=mirror / "install.sh"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"from {mirror}/models into {data_root}/Models: bge-m3." in result.stdout

    elsewhere = make_models(tmp_path / "elsewhere", "demo-extra")
    result, _, _ = run_install(
        tmp_path,
        "--models-only",
        "--dry-run",
        with_bundle=False,
        env_extra={"SLAS_MODELS_DIR": str(elsewhere)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"from {elsewhere} into" in result.stdout and "demo-extra" in result.stdout


def test_without_weights_the_install_goes_on_and_says_how_to_add_them(tmp_path: Path) -> None:
    assert not (REPO_ROOT / "models").exists(), (
        "the default ./models must not exist in the repository"
    )
    result, _, _ = run_install(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "No model weights were given: no --models DIR and no ./models next to install.sh."
        in result.stdout
    )
    assert "then run ./install.sh --models <dir>; nothing else needs to change." in result.stdout
    assert "models.yaml" not in result.stdout


def test_fetch_models_downloads_then_places_in_one_command(tmp_path: Path) -> None:
    """`./install.sh --fetch-models --models-only`: the one-command preparation."""
    from tests.unit.test_fetch_models import FakeHub

    hub = FakeHub().start()
    try:
        sources = tmp_path / "sources.txt"
        sources.write_text("[quickstart]\ntiny demo/tiny\n")
        env = {
            "HF_ENDPOINT": hub.endpoint,
            "SLAS_MODEL_SOURCES": str(sources),
            "SLAS_MODELS_DIR": str(tmp_path / "staged"),
        }
        result, calls, data_root = run_install(
            tmp_path,
            "--fetch-models",
            "--models-only",
            "--dry-run",
            with_bundle=False,
            env_extra=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            f"Fetching the quickstart profile's model weights into {tmp_path / 'staged'}"
            in result.stdout
        )
        assert "Dry run: nothing was downloaded." in result.stdout
        assert "the fetch plan above checks out" in result.stdout
        assert "No model weights were given" not in result.stdout
        assert not (tmp_path / "staged").exists() and not data_root.exists()

        result, calls, data_root = run_install(
            tmp_path, "--fetch-models", "--models-only", with_bundle=False, env_extra=env
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Done: 1 model, 1.0 MiB," in result.stdout
        assert (tmp_path / "staged" / "tiny" / "SHA256SUMS").exists()
        assert f"Placed 1 model under {data_root}/Models" in result.stdout
        assert (data_root / "Models" / "tiny" / "model.safetensors").exists()
        assert (data_root / "Models" / "models.yaml").exists() and not calls

        hub.gated = True  # the hub refuses: the install stops in three parts, nothing placed twice
        result, _, _ = run_install(
            tmp_path, "--fetch-models", "--models-only", with_bundle=False, env_extra=env
        )
        assert result.returncode == 1
        assert "The hub refused" in result.stdout
        assert "Fetching the model weights did not finish" in result.stdout
        assert "Nothing was changed on this host." in result.stdout
    finally:
        hub.stop()
