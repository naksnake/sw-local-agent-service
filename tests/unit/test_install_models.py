"""`./install.sh --models DIR`: the weights are checked before anything changes, placed under
<data root>/Models, and Models/models.yaml comes from the profile's template once and is
never overwritten. Docker is a stub that logs its argv; the preflight is skipped explicitly
because the runner has no GPU."""

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
    (root / "manifest.json").write_text(json.dumps({"models": [{"path": n} for n in names]}))
    return root


def run_install(
    tmp_path: Path, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    bundle, lock = make_bundle(tmp_path)
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLAS_") and k != "PYTHONPATH"}
    env.update({"PATH": f"{stubs}:{env['PATH']}", "STUB_LOG": str(log)})
    data_root = tmp_path / "data"
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--profile", "quickstart", "--skip-preflight", "--bundle", str(bundle), "--lock", str(lock), "--data-root", str(data_root), "--registry", "", *args],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=180, check=False,
    )  # fmt: skip
    return result, [line for line in log.read_text().splitlines() if line], data_root


def test_dry_run_checks_the_weights_and_says_what_it_would_copy_and_write(tmp_path: Path) -> None:
    models = make_models(tmp_path / "carried", "bge-m3", "demo-extra")
    result, calls, data_root = run_install(tmp_path, "--dry-run", "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert f"Checking the model weights in {models} against their checksums" in out
    assert (
        "bge-m3 matches its checksums." not in out
        and "bge-m3, demo-extra matches its checksums." in out
    )
    assert (
        "Would copy 2 models (" in out
        and f"from {models} into {data_root}/Models: bge-m3 demo-extra." in out
    )
    assert f"Would write {data_root}/Models/models.yaml from config/models.quickstart.yaml." in out
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


def test_install_places_the_weights_writes_models_yaml_once_and_is_idempotent(
    tmp_path: Path,
) -> None:
    models = make_models(tmp_path / "carried", "bge-m3", "demo-extra")
    result, calls, data_root = run_install(tmp_path, "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "Copying 2 models (" in out and "Large models take a while." in out
    assert "bge-m3, demo-extra matches its checksums." in out
    target = data_root / "Models"
    for name in ("bge-m3", "demo-extra"):
        assert (target / name / "SHA256SUMS").read_text() == (
            models / name / "SHA256SUMS"
        ).read_text()
        assert (target / name / "model.safetensors").read_bytes() == (
            models / name / "model.safetensors"
        ).read_bytes()
        assert not (target / f"{name}.part").exists()
    assert (target / "manifest.json").read_text() == (models / "manifest.json").read_text()
    template = (REPO_ROOT / "config" / "models.quickstart.yaml").read_text()
    assert (target / "models.yaml").read_text() == template
    assert f"Wrote {target}/models.yaml from the quickstart template." in out
    assert "models.yaml names deepseek-v4-flash, but no weights for it are here yet." in out
    assert any(c.startswith("docker compose") and " up -d --pull never" in c for c in calls)
    assert "SW Local Agent Service is up." in out

    # Run again: the registry the operator may have edited stays, nothing is copied twice.
    (target / "models.yaml").write_text(template + "# edited by the operator\n")
    result, _, _ = run_install(tmp_path, "--models", str(models))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"2 models are already under {target}: bge-m3 demo-extra." in result.stdout
    assert "Copying" not in result.stdout
    assert f"Kept {target}/models.yaml as it is" in result.stdout
    assert (target / "models.yaml").read_text().endswith("# edited by the operator\n")

    # Without --models, the weights already in place are still reported.
    result, _, _ = run_install(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"2 models are already under {target}" in result.stdout
    assert "No model weights were given" not in result.stdout


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


def test_bad_or_missing_weights_stop_before_anything_changes(tmp_path: Path) -> None:
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
