"""`./install.sh --profile prod`: the read-only steps run for real against stub tools, nothing
changes until they pass, and a bad signature stops everything before docker is asked for
anything. The runner has no Docker and no GPU, so the preflight is skipped explicitly."""

from __future__ import annotations

# ruff: noqa: E501 — long literal sentences and argv lists read better unwrapped
import json
import os
import subprocess
from pathlib import Path

import pytest

from slas_deploy.cosign import write_manifest
from slas_deploy.images import default_lock

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"

STUB_COSIGN = """#!/usr/bin/env bash
echo "cosign $*" >> "$STUB_LOG"
exit "${STUB_COSIGN_EXIT:-0}"
"""
STUB_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
exit 0
"""


def make_bundle(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    (bundle / "images").mkdir(parents=True)
    ids: dict[str, str] = {}
    lock = default_lock()
    pinned = []
    for image in lock.images:
        image_id = "sha256:" + format(abs(hash(image.name)) % (16**16), "016x") * 4
        ref = f"registry.internal/{image.reference}".replace("${SLAS_VERSION}", "0.0.1")
        ids[ref] = image_id
        (bundle / "images" / f"{image.name}.tar").write_bytes(image.name.encode())
        pinned.append(
            image.model_copy(update={"digest": "sha256:" + "c" * 64, "image_id": image_id})
        )
    write_manifest(bundle, "0.0.1", "2026-09-14T10:00:00Z", ids)
    (bundle / "manifest.json.sig").write_text("fake-signature\n")
    lock_path = tmp_path / "images.lock.json"
    lock_path.write_text(
        json.dumps(lock.model_copy(update={"images": pinned}).model_dump(mode="json"))
    )
    return bundle, lock_path


def run_install(
    tmp_path: Path, *args: str, cosign_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name, body in (("cosign", STUB_COSIGN), ("docker", STUB_DOCKER)):
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    key = tmp_path / "cosign.pub"
    key.write_text("-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLAS_") and k != "PYTHONPATH"}
    env.update(
        {
            "PATH": f"{stubs}:{env['PATH']}",
            "STUB_LOG": str(log),
            "STUB_COSIGN_EXIT": str(cosign_exit),
        }
    )
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--profile", "prod", "--skip-preflight", "--cosign-key", str(key), *args],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=180, check=False,
    )  # fmt: skip
    return result, [line for line in log.read_text().splitlines() if line]


def test_help_names_the_prod_options() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    for option in (
        "--profile",
        "--bundle DIR",
        "--registry HOST",
        "--models DIR",
        "--models-only",
        "--dry-run",
        "--preflight-only",
    ):
        assert option in result.stdout


def test_prod_dry_run_verifies_the_bundle_then_only_describes_the_changes(tmp_path: Path) -> None:
    bundle, lock = make_bundle(tmp_path)
    data_root = tmp_path / "data"
    result, calls = run_install(
        tmp_path,
        "--dry-run",
        "--bundle",
        str(bundle),
        "--lock",
        str(lock),
        "--data-root",
        str(data_root),
        "--registry",
        "",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "WARNING: the preflight was skipped" in out
    order = [
        "Verifying what will be installed (prod profile).",
        "Verified the bundle manifest signature",
        "The bundle carries every image the prod profile starts",
        f"Would write {data_root}/.env (prod keys",
        "Would create the missing secret files",
        "Would run: docker load --quiet --input",
        "Would run: docker compose --project-name slas",
        "-f " + str(REPO_ROOT / "compose" / "prod.override.yml"),
        "up -d --pull never --remove-orphans",
        "exec -T vault sh /vault/bootstrap.sh",
        "Dry run finished: every read-only step passed; nothing was changed on this host.",
    ]
    position = -1
    for marker in order:
        found = out.find(marker, position + 1)
        assert found > position, f"{marker!r} missing or out of order in:\n{out}"
        position = found
    assert not data_root.exists(), "a dry run writes nothing"
    assert len(calls) == 1 and calls[0].startswith("cosign verify-blob --key")
    assert "--insecure-ignore-tlog --private-infrastructure" in calls[0]
    assert not any(c.startswith("docker") for c in calls), "docker is only described in a dry run"


def test_a_bad_manifest_signature_stops_before_anything_is_touched(tmp_path: Path) -> None:
    bundle, lock = make_bundle(tmp_path)
    data_root = tmp_path / "data"
    result, calls = run_install(
        tmp_path,
        "--bundle",
        str(bundle),
        "--lock",
        str(lock),
        "--data-root",
        str(data_root),
        "--registry",
        "",
        cosign_exit=1,
    )
    assert result.returncode == 1
    assert "The signature on the bundle manifest does not verify." in result.stdout
    assert "Nothing was changed on this host." in result.stdout
    assert not data_root.exists()
    assert len(calls) == 1 and calls[0].startswith("cosign verify-blob")


def test_the_repository_lock_as_shipped_refuses_to_start_the_prod_profile(tmp_path: Path) -> None:
    bundle, _ = make_bundle(tmp_path)
    result, _ = run_install(
        tmp_path,
        "--dry-run",
        "--bundle",
        str(bundle),
        "--data-root",
        str(tmp_path / "data"),
        "--registry",
        "",
    )
    assert result.returncode == 1
    assert "not pinned" in result.stdout and "scripts/lock-images.sh" in result.stdout
    assert "Nothing was changed on this host." in result.stdout


def test_registry_mode_verifies_every_image_signature_before_pulling(tmp_path: Path) -> None:
    _, lock = make_bundle(tmp_path)
    result, calls = run_install(
        tmp_path,
        "--dry-run",
        "--registry",
        "harbor.internal",
        "--lock",
        str(lock),
        "--data-root",
        str(tmp_path / "data"),
        "--bundle",
        str(tmp_path / "nowhere"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verifies = [c for c in calls if c.startswith("cosign verify --key")]
    assert len(verifies) == len(default_lock().for_profile("prod"))
    assert any("harbor.internal/library/postgres@sha256:" in c for c in verifies)
    assert any("harbor.internal/slas/api:0.0.1" in c for c in verifies)
    assert "Every image is signed by the release key." in result.stdout
    assert (
        "Would run: docker compose --project-name slas" in result.stdout
        and "pull --quiet" in result.stdout
    )


@pytest.mark.parametrize("profile", ["quickstart"])
def test_quickstart_without_a_bundle_says_what_to_do(tmp_path: Path, profile: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLAS_") and k != "PYTHONPATH"}
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--profile", profile, "--skip-preflight", "--bundle", str(tmp_path / "none"), "--data-root", str(tmp_path / "d")],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=120, check=False,
    )  # fmt: skip
    assert result.returncode == 1
    assert (
        "No bundle was found" in result.stdout
        and "Nothing was changed on this host." in result.stdout
    )
