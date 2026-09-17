"""`./install.sh --build` (ADR-0014): on a connected quickstart host the third-party images are
pulled by their pinned tags, the first-party images are built from images/<name>/Dockerfile
with the repository root as context, the filled lock lands under the data root and passes
check-lock, and the stack starts with `compose up --pull never`. Docker is a stub that logs
its argv and answers `inspect` and `bootstrap status`; the preflight is skipped explicitly
because the runner has no GPU."""

from __future__ import annotations

# ruff: noqa: E501 — long literal sentences and argv lists read better unwrapped
import hashlib
import io
import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from slas_deploy import build
from slas_deploy.images import default_lock, parse_lock_json
from slas_deploy.installer import main as installer_main
from slas_deploy.installer import unhealthy_services

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
VERSION = re.search(r'^version = "(.*)"', (REPO_ROOT / "pyproject.toml").read_text(), re.M).group(1)  # type: ignore[union-attr]

# The stub answers like docker would: a RepoDigest for a pulled tag, an image ID for anything,
# `pending`/`done` for the api's bootstrap status, and the compose ps rows the test asks for.
STUB_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
last="${@: -1}"
case "$*" in
  *"inspect --format {{index .RepoDigests 0}}"*) echo "${last%%:*}@sha256:$(printf '%s' "digest-$last" | sha256sum | cut -d' ' -f1)" ;;
  *"inspect --format {{.Id}}"*) echo "sha256:$(printf '%s' "id-$last" | sha256sum | cut -d' ' -f1)" ;;
  *"bootstrap status"*) echo "${STUB_BOOTSTRAP:-pending}" ;;
  *" ps --all --format json"*) printf '%s' "${STUB_PS:-}" ;;
  "compose version") exit 0 ;;
esac
exit "${STUB_DOCKER_EXIT:-0}"
"""


def run_install(
    tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    stub = stubs / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLAS_") and k != "PYTHONPATH"}
    env.update({"PATH": f"{stubs}:{env['PATH']}", "STUB_LOG": str(log)})
    env.update(env_extra or {})
    data_root = tmp_path / "data"
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--skip-preflight", "--build", "--data-root", str(data_root), *args],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=300, check=False,
    )  # fmt: skip
    return result, [line for line in log.read_text().splitlines() if line], data_root


def quickstart_images() -> tuple[list[str], list[str]]:
    lock = default_lock()
    wanted = lock.for_profile("quickstart")
    return (
        [i.name for i in wanted if i.first_party],
        [i.upstream for i in wanted if not i.first_party],
    )


def test_help_names_build() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "--build " in result.stdout and "ADR-0014" in result.stdout
    assert "./install.sh --build --fetch-models" in result.stdout


def test_build_dry_run_describes_every_pull_and_build_and_touches_nothing(tmp_path: Path) -> None:
    result, calls, data_root = run_install(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    first_party, third_party = quickstart_images()
    for name in first_party:
        assert f"Would build local/slas/{name}:{VERSION} from images/{name}/Dockerfile." in out
    for upstream in third_party:
        assert f"Would pull {upstream} and tag it local/" in out
    assert "Would build local/slas/postgres-pgbackrest" not in out, (
        "prod-only images are not built for quickstart"
    )
    assert "hashicorp/vault" not in out
    assert (
        f"Would write the filled image lock to {data_root}/images.lock.json ({len(first_party)} built, {len(third_party)} pulled); it is never committed."
        in out
    )
    order = [
        "Verifying what will be installed (quickstart profile).",
        "Images are built from this checkout and pulled by their pinned tags after the read-only checks (ADR-0014)",
        "Building the first-party images from",
        "the running platform still has no egress (ADR-0014)",
        "Would pull docker.io/library/postgres:16.6",
        "Would check that lock",
        f"Would write {data_root}/.env (quickstart keys, registry local, version {VERSION}",
        "Would run: docker compose --project-name slas",
        "up -d --pull never --remove-orphans",
        "Dry run finished: every read-only step passed; nothing was changed on this host.",
    ]
    position = -1
    for marker in order:
        found = out.find(marker, position + 1)
        assert found > position, f"{marker!r} missing or out of order in:\n{out}"
        position = found
    assert "pull --quiet" not in out.split("Would run:", 1)[1], (
        "no compose pull: the images are local"
    )
    assert "Would run: docker load" not in out
    assert not data_root.exists(), "a dry run writes nothing"
    assert calls == ["docker compose version"], calls


def test_build_pulls_tags_builds_writes_a_pinned_lock_and_starts_the_stack(tmp_path: Path) -> None:
    result, calls, data_root = run_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    first_party, third_party = quickstart_images()
    for upstream in third_party:
        assert f"docker pull --quiet {upstream}" in calls
        reference = next(i for i in default_lock().images if i.upstream == upstream).reference
        assert f"docker tag {upstream} local/{reference}" in calls
        assert f"Pulled {upstream} (sha256:" in out
    for name in first_party:
        assert (
            f"docker build --file {REPO_ROOT}/images/{name}/Dockerfile --tag local/slas/{name}:{VERSION} {REPO_ROOT}"
            in calls
        )
        assert (
            f"Built local/slas/{name}:{VERSION} from images/{name}/Dockerfile (image ID sha256:"
            in out
        )
    assert not any("postgres-pgbackrest" in c for c in calls)
    assert not any("quay.io/minio/mc" in c for c in calls), "prod-only images are not pulled"

    lock_path = data_root / "images.lock.json"
    assert (
        f"Wrote the filled image lock to {lock_path}: {len(first_party)} images built from this checkout, {len(third_party)} pulled and tagged for local."
        in out
    )
    lock = parse_lock_json(lock_path.read_text())
    assert lock.unpinned("quickstart") == []
    assert {i.name for i in lock.unpinned("prod")} == {
        "mc",
        "vault",
        "keycloak",
        "loki",
        "tempo",
        "postgres-pgbackrest",
    }
    for image in lock.for_profile("quickstart"):
        assert image.image_id and image.image_id.startswith("sha256:")
        assert (image.digest is None) is image.first_party
    check = io.StringIO()
    assert (
        installer_main(
            ["check-lock", "--lock", str(lock_path), "--profile", "quickstart"], stdout=check
        )
        == 0
    )
    assert (
        f"All {len(first_party) + len(third_party)} images the quickstart profile starts are pinned"
        in check.getvalue()
    )
    assert (
        f"All {len(first_party) + len(third_party)} images the quickstart profile starts are pinned by digest."
        in out
    )

    env_text = (data_root / ".env").read_text()
    assert "SLAS_REGISTRY=local\n" in env_text and f"SLAS_VERSION={VERSION}\n" in env_text
    assert (data_root / "tls").is_dir() and (data_root / "secrets" / "secret_key").is_file()
    ups = [
        c
        for c in calls
        if c.startswith("docker compose") and " up -d --pull never --remove-orphans" in c
    ]
    assert len(ups) == 1 and f"--env-file {data_root}/.env" in ups[0]
    assert not any(c.startswith("docker compose") and c.endswith(" pull --quiet") for c in calls)
    assert not any(c.startswith("docker load") for c in calls)
    assert any(" exec -T api slas-api bootstrap status" in c for c in calls)
    assert "SW Local Agent Service is up." in out
    password = (data_root / "secrets" / "admin-initial-password").read_text().strip()
    assert f"one-time password: {password}" in out

    # Once the administrator chose a password, the one-time password is not shown again.
    result, _, _ = run_install(tmp_path, env_extra={"STUB_BOOTSTRAP": "done"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already chose a password" in result.stdout and password not in result.stdout


def test_a_failed_build_stops_before_the_stack_starts(tmp_path: Path) -> None:
    result, calls, data_root = run_install(tmp_path, env_extra={"STUB_DOCKER_EXIT": "1"})
    assert result.returncode == 1
    assert (
        "Pulling docker.io/library/postgres:16.6 did not finish (docker exited 1" in result.stdout
    )
    assert "Building or pulling the images did not finish" in result.stdout
    assert "the stack was not started" in result.stdout
    assert not any(" up -d" in c for c in calls)
    assert not (data_root / "images.lock.json").exists() and not (data_root / ".env").exists()


def test_build_is_refused_for_the_prod_profile(tmp_path: Path) -> None:
    result, calls, _ = run_install(tmp_path, "--profile", "prod")
    assert result.returncode == 2
    assert "--build is for the quickstart profile only." in result.stderr
    assert "ADR-0014" in result.stderr and result.stdout == ""
    assert not calls


def test_unhealthy_services_after_the_wait_are_named_with_their_logs(tmp_path: Path) -> None:
    rows = "\n".join(
        json.dumps(row)
        for row in (
            {"Service": "postgres", "State": "running", "Health": "healthy"},
            {"Service": "api", "State": "restarting", "Health": ""},
            {"Service": "llm-gateway", "State": "running", "Health": "starting"},
            {"Service": "minio-init", "State": "exited", "ExitCode": 0, "Health": ""},
        )
    )
    result, calls, _ = run_install(
        tmp_path, env_extra={"STUB_PS": rows, "SLAS_HEALTH_WAIT_S": "0", "SLAS_HEALTH_POLL_S": "0"}
    )
    assert result.returncode == 1, result.stdout + result.stderr
    out = result.stdout
    assert "After 0 s these services are not healthy: api llm-gateway." in out
    assert "Likely cause: a slow first start" in out and "What to do: read the lines below" in out
    assert "--- api: last 20 log lines" in out and "--- llm-gateway: last 20 log lines" in out
    assert any(c.endswith(" logs --tail 20 --no-color api") for c in calls)
    assert any(c.endswith(" logs --tail 20 --no-color llm-gateway") for c in calls)
    assert "bootstrap status" not in " ".join(calls)


# --- the Python behind the script -------------------------------------------------------------


def test_unhealthy_services_reads_both_compose_ps_formats() -> None:
    rows = [
        {"Service": "postgres", "State": "running", "Health": "healthy"},
        {"Service": "webui", "State": "running", "Health": ""},
        {"Service": "api", "State": "restarting", "Health": ""},
        {"Service": "edge", "State": "running", "Health": "starting"},
        {"Service": "grafana", "State": "running", "Health": "unhealthy"},
        {"Service": "minio-init", "State": "exited", "ExitCode": 0},
        {"Service": "backup-runner", "State": "exited", "ExitCode": 3},
        {"Name": "slas-vector-db-1", "State": "created"},
    ]
    expected = ["api", "edge", "grafana", "backup-runner", "slas-vector-db-1"]
    assert unhealthy_services("\n".join(json.dumps(r) for r in rows)) == expected
    assert unhealthy_services(json.dumps(rows)) == expected
    assert unhealthy_services(json.dumps(rows[0])) == []
    assert unhealthy_services("") == [] and unhealthy_services("  \n") == []


class FakeDocker:
    """Answers docker like the stub does; fails the argv whose prefix is in `failing`."""

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.failing = failing

    def run(self, argv: Sequence[str], *, capture: bool = True) -> build.Completed:
        self.calls.append(list(argv))
        if any(" ".join(argv).startswith(prefix) for prefix in self.failing):
            return build.Completed(1, "", "Error response from daemon: manifest unknown")
        if "{{index .RepoDigests 0}}" in argv:
            digest = hashlib.sha256(argv[-1].encode()).hexdigest()
            return build.Completed(0, f"{argv[-1].split(':')[0]}@sha256:{digest}\n")
        if "{{.Id}}" in argv:
            return build.Completed(
                0, "sha256:" + hashlib.sha256(("id" + argv[-1]).encode()).hexdigest() + "\n"
            )
        return build.Completed(0)


def test_build_images_fills_the_lock_and_names_every_image(tmp_path: Path) -> None:
    docker = FakeDocker()
    out = io.StringIO()
    filled = build.build_images(
        default_lock(),
        "quickstart",
        registry="local",
        version="9.9.9",
        repo=tmp_path,
        runner=docker,
        out=out,
    )
    assert filled.unpinned("quickstart") == []
    api = next(i for i in filled.images if i.name == "api")
    assert api.pinned and api.digest is None and str(api.image_id).startswith("sha256:")
    postgres = next(i for i in filled.images if i.name == "postgres")
    assert postgres.pinned and str(postgres.digest).startswith("sha256:")
    vault = next(i for i in filled.images if i.name == "vault")
    assert not vault.pinned, "another profile's image is left as it was"
    assert [
        "docker",
        "build",
        "--file",
        str(tmp_path / "images/api/Dockerfile"),
        "--tag",
        "local/slas/api:9.9.9",
        str(tmp_path),
    ] in docker.calls
    assert [
        "docker",
        "tag",
        "docker.io/library/postgres:16.6",
        "local/library/postgres:16.6",
    ] in docker.calls
    text = out.getvalue()
    assert "Built local/slas/api:9.9.9 from images/api/Dockerfile (image ID sha256:" in text
    assert (
        "Pulled docker.io/library/postgres:16.6 (sha256:" in text
        and "tagged it local/library/postgres:16.6" in text
    )

    described = io.StringIO()
    build.describe(
        build.plan(default_lock(), "quickstart", registry="r", version="1", repo=tmp_path),
        out=described,
        lock_path=tmp_path / "l.json",
    )
    assert "Would build r/slas/edge:1 from images/edge/Dockerfile." in described.getvalue()
    assert (
        "Would pull quay.io/prometheus/prometheus:v3.2.1 and tag it r/prom/prometheus:v3.2.1."
        in described.getvalue()
    )


def test_a_failing_docker_step_is_a_three_part_error() -> None:
    docker = FakeDocker(failing=("docker build --file",))
    with pytest.raises(build.BuildError) as exc:
        build.build_images(
            default_lock(),
            "quickstart",
            registry="local",
            version="1",
            repo=Path("/repo"),
            runner=docker,
            out=io.StringIO(),
        )
    message = exc.value.message
    assert message.what_happened.startswith(
        "Building local/slas/edge:1 did not finish (docker exited 1: Error response from daemon: manifest unknown)."
    )
    assert "A build step failed" in message.likely_cause
    assert "./install.sh --build" in message.what_to_do

    docker = FakeDocker(failing=("docker pull",))
    with pytest.raises(build.BuildError) as exc:
        build.build_images(
            default_lock(),
            "quickstart",
            registry="local",
            version="1",
            repo=Path("/repo"),
            runner=docker,
            out=io.StringIO(),
        )
    assert exc.value.message.what_happened.startswith(
        "Pulling docker.io/library/postgres:16.6 did not finish"
    )
    assert "route to the registry" in exc.value.message.likely_cause


def test_a_first_party_image_counts_as_pinned_by_its_image_id_alone() -> None:
    lock = default_lock()
    api = next(i for i in lock.images if i.name == "api")
    postgres = next(i for i in lock.images if i.name == "postgres")
    image_id = "sha256:" + "a" * 64
    assert not api.pinned and not postgres.pinned
    assert api.model_copy(update={"image_id": image_id}).pinned
    assert not api.model_copy(update={"image_id": "not-a-digest"}).pinned
    assert not postgres.model_copy(update={"image_id": image_id}).pinned, (
        "third-party needs the digest too"
    )
    assert postgres.model_copy(update={"image_id": image_id, "digest": "sha256:" + "b" * 64}).pinned
