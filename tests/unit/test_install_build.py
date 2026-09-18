"""`./install.sh --build` (ADR-0014, ADR-0015): on a connected quickstart host the third-party
images are pulled by their pinned tags (by digest when the lock records one — the vLLM image),
the first-party images are built from images/<name>/Dockerfile with the repository root as
context, the sandbox images the sandbox manager lists are built the same way and their
toolchain manifest written, the filled lock lands under the data root and passes check-lock,
.env names the runtime socket that exists, the data directories are created, and the stack
starts with `compose up --pull never`. Docker is a stub that logs its argv and answers
`inspect` and `bootstrap status`; the preflight is skipped explicitly because the runner has
no GPU.

The installer runs from a mirror of the checkout whose `services/sandbox-manager` carries a
stub `slas_sandbox_manager.images` with the `list|manifest` commands of the contract (§4):
that slice lands separately, and this test pins the interface the installer expects."""

from __future__ import annotations

# ruff: noqa: E501 — long literal sentences and argv lists read better unwrapped
import hashlib
import io
import json
import os
import re
import socket
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from slas_deploy import build, sandbox_images
from slas_deploy.images import default_lock, parse_lock_json
from slas_deploy.installer import main as installer_main
from slas_deploy.installer import unhealthy_services

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
VERSION = re.search(r'^version = "(.*)"', (REPO_ROOT / "pyproject.toml").read_text(), re.M).group(1)  # type: ignore[union-attr]

# The stub answers like docker would: a RepoDigest for a pulled tag (the digest itself when the
# pull was by digest), an image ID for anything, `pending`/`done` for the api's bootstrap
# status, and the compose ps rows the test asks for.
STUB_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
last="${@: -1}"
case "$*" in
  *"inspect --format {{index .RepoDigests 0}}"*)
    if [[ "$last" == *"@sha256:"* ]]; then echo "$last"; else echo "${last%%:*}@sha256:$(printf '%s' "digest-$last" | sha256sum | cut -d' ' -f1)"; fi ;;
  *"inspect --format {{.Id}}"*) echo "sha256:$(printf '%s' "id-$last" | sha256sum | cut -d' ' -f1)" ;;
  *"bootstrap status"*) echo "${STUB_BOOTSTRAP:-pending}" ;;
  *" ps --all --format json"*) printf '%s' "${STUB_PS:-}" ;;
  "ps -aq --filter label=com.docker.compose.project=slas --filter label=com.docker.compose.service="*)
    # STUB_STALE: "<service>=<id> <service>=<id>": containers an earlier install left behind.
    for pair in ${STUB_STALE:-}; do
      [[ "${last#label=com.docker.compose.service=}" == "${pair%%=*}" ]] && echo "${pair#*=}"
    done ;;
  "compose version") exit 0 ;;
esac
exit "${STUB_DOCKER_EXIT:-0}"
"""

# The interface of docs/api-contract-round-2.md §4, as the installer calls it:
#   python -m slas_sandbox_manager.images list --registry <label>   name<TAB>tag<TAB>dockerfile
#   python -m slas_sandbox_manager.images manifest --out <path>
STUB_SANDBOX_IMAGES = '''"""Stub of the sandbox manager's image list for the installer tests."""
import argparse, json, sys

IMAGES = [
    ("sandbox-python", "3.12.6", "images/sandbox-python/Dockerfile"),
    ("sandbox-shell", "5.2.21", "images/sandbox-shell/Dockerfile"),
]


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--registry", default="local")
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "list":
        print("# name\\ttag\\tdockerfile")
        for name, version, dockerfile in IMAGES:
            print(f"{name}\\t{args.registry}/slas/{name}:{version}\\t{dockerfile}")
        return 0
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "toolchains": {"python": ["3.12.6"], "shell": ["5.2.21"]}, "companions": {}}, handle)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
SANDBOX_TAGS = ("local/slas/sandbox-python:3.12.6", "local/slas/sandbox-shell:5.2.21")


def mirror_checkout(tmp_path: Path, *, sandbox_cli: bool) -> Path:
    """A symlink mirror of the repository. With `sandbox_cli` the sandbox manager carries the
    small stub `images` module (a fixed two-image listing the assertions know); without it the
    module is absent, the case of a checkout whose sandbox manager has no `images list` yet."""
    mirror = tmp_path / "repo"
    if mirror.exists():
        return mirror
    mirror.mkdir()
    for entry in REPO_ROOT.iterdir():
        if entry.name == "services":
            continue
        (mirror / entry.name).symlink_to(entry)
    services = mirror / "services"
    services.mkdir()
    for entry in (REPO_ROOT / "services").iterdir():
        if entry.name != "sandbox-manager":
            (services / entry.name).symlink_to(entry)
    package = services / "sandbox-manager" / "slas_sandbox_manager"
    package.mkdir(parents=True)
    for entry in (REPO_ROOT / "services" / "sandbox-manager" / "slas_sandbox_manager").iterdir():
        if entry.name != "images.py":
            (package / entry.name).symlink_to(entry)
    if sandbox_cli:
        (package / "images.py").write_text(STUB_SANDBOX_IMAGES)
    return mirror


def fake_docker_socket(tmp_path: Path) -> Path:
    """A unix socket standing in for /var/run/docker.sock (nothing listens behind it)."""
    path = tmp_path / "d.sock"
    if not path.exists():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.close()
    return path


def run_install(
    tmp_path: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    sandbox_cli: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    stub = stubs / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    repo = mirror_checkout(tmp_path, sandbox_cli=sandbox_cli)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLAS_") and k != "PYTHONPATH"}
    env.update({"PATH": f"{stubs}:{env['PATH']}", "STUB_LOG": str(log)})
    # No Podman socket on this runner; a fake Docker socket stands where Docker's would be.
    env.update(
        {
            "SLAS_PODMAN_SOCKET": str(tmp_path / "no-podman.sock"),
            "SLAS_DOCKER_SOCKET": str(fake_docker_socket(tmp_path)),
        }
    )
    env.update(env_extra or {})
    data_root = tmp_path / "data"
    result = subprocess.run(
        ["bash", str(repo / "install.sh"), "--skip-preflight", "--build", "--data-root", str(data_root), *args],
        capture_output=True, text=True, env=env, cwd=repo, timeout=300, check=False,
    )  # fmt: skip
    return result, [line for line in log.read_text().splitlines() if line], data_root


def quickstart_images() -> tuple[list[str], list[str]]:
    """First-party names and third-party pull references the quickstart profile needs."""
    lock = default_lock()
    wanted = lock.for_profile("quickstart", ("coding",))  # the default agents (ADR-0017)
    return (
        [i.name for i in wanted if i.first_party],
        [i.pull_reference for i in wanted if not i.first_party],
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
    for pulled in third_party:
        assert f"Would pull {pulled} and tag it local/" in out
    assert (
        "Would pull docker.io/vllm/vllm-openai@sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a and tag it local/vllm/vllm-openai:v0.29.0-x86_64-cu129. The model manager starts it per role and voter; compose never does."
        in out
    ), "the vLLM image is pulled by the digest the lock records and named as the model manager's"
    assert "Would build local/slas/postgres-pgbackrest" not in out, (
        "prod-only images are not built for quickstart"
    )
    assert "Would build local/slas/validation-executor" not in out, "off by default (ADR-0017)"
    assert "Would build local/slas/factory-executor" not in out
    assert "Would build local/slas/local-search-api" not in out, "the knowledge base is off too"
    assert "Would pull docker.io/qdrant/qdrant" not in out, "the knowledge base's image too"
    assert "Compose profiles:" not in out
    assert "hashicorp/vault" not in out
    assert (
        f"Would write the filled image lock to {data_root}/images.lock.json ({len(first_party)} built, {len(third_party)} pulled); it is never committed."
        in out
    )
    for tag in SANDBOX_TAGS:
        name = tag.split("/")[-1].split(":")[0]
        assert f"Would build the sandbox image {tag} from images/{name}/Dockerfile." in out
    docker_sock = fake_docker_socket(tmp_path)
    order = [
        "Verifying what will be installed (quickstart profile).",
        "Agents: coding. The validation executor, the factory executor and the knowledge base (Qdrant and local search) stay off; enable them with --agents coding,validation,factory,knowledge.",
        "Images are built from this checkout and pulled by their pinned tags after the read-only checks (ADR-0014)",
        "Building the first-party images from",
        "the running platform still has no egress (ADR-0014)",
        "Would pull docker.io/library/postgres:16.6",
        "Would check that lock",
        f"Building the Coding Agent's sandbox images and writing the toolchain manifest to {data_root}/Toolchains/manifest.json.",
        "Would build the sandbox image local/slas/sandbox-python:3.12.6",
        f"Would write the sandbox image lock to {data_root}/sandbox-images.lock.json (2 built) and the toolchain manifest to {data_root}/Toolchains/manifest.json; neither is committed.",
        f"No Podman socket at {tmp_path}/no-podman.sock, so SLAS_RUNTIME_SOCKET={docker_sock} in .env points model-manager and sandbox-manager at Docker's socket; nothing else sees it (INV-4).",
        f"Would write {data_root}/.env (quickstart keys, registry local, version {VERSION}, SLAS_RUNTIME_SOCKET={docker_sock}; keys you set are kept).",
        f"Would create the missing data directories under {data_root}: Coding, Toolchains, .git-broker, Tickets, Skills/library, SOP, Validation, Factory/Templates, Factory/mes/inbox, Factory/ca, Models, Knowledge, Backups/stations, qdrant, tls.",
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
    # Docker is only asked read-only questions: its compose version, and whether an earlier
    # install left a container of a part that is off (ADR-0017); nothing is removed.
    assert calls[0] == "docker compose version"
    assert all(c.startswith("docker ps -aq --filter ") for c in calls[1:]), calls
    assert not any(c.startswith("docker rm") for c in calls)


def test_agents_flag_adds_the_executors_and_their_compose_profiles(tmp_path: Path) -> None:
    """ADR-0017: `--agents coding,validation,factory` builds both executor images and starts
    their compose profiles; an unknown agent stops before anything is verified."""
    result, _calls, _data_root = run_install(
        tmp_path, "--dry-run", "--agents", "coding,validation,factory"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert (
        "Agents: coding, validation, factory. The knowledge base (Qdrant and local search) "
        "stays off; enable it with --agents coding,validation,factory,knowledge." in out
    )
    assert f"Would build local/slas/validation-executor:{VERSION}" in out
    assert f"Would build local/slas/factory-executor:{VERSION}" in out
    assert "Compose profiles: validation,factory (the executors of the agents you chose)." in out

    only_factory, _, _ = run_install(tmp_path, "--dry-run", "--agents", "factory")
    assert only_factory.returncode == 0, only_factory.stdout + only_factory.stderr
    assert "Agents: coding, factory." in only_factory.stdout
    assert "Would build local/slas/validation-executor" not in only_factory.stdout
    assert "Compose profiles: factory (" in only_factory.stdout

    unknown, calls, _ = run_install(tmp_path, "--dry-run", "--agents", "coding,screen")
    assert unknown.returncode == 2
    assert 'The agent "screen" is not known.' in unknown.stderr
    assert calls == [], "the refusal comes before docker is asked anything"


def test_dry_run_without_the_sandbox_image_list_says_so_and_goes_on(tmp_path: Path) -> None:
    """A checkout whose sandbox manager does not publish `images list` yet: the dry run names
    the gap and finishes; a real run stops before the stack starts."""
    result, calls, data_root = run_install(tmp_path, "--dry-run", sandbox_cli=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "The sandbox image list could not be read (`python -m slas_sandbox_manager.images list`"
        in result.stdout
    )
    assert (
        "In a real run this stops the install; the sandbox images would be built from images/sandbox-*/Dockerfile"
        in result.stdout
    )
    assert "Dry run finished" in result.stdout and not data_root.exists()

    result, calls, data_root = run_install(tmp_path, sandbox_cli=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "Likely cause: This checkout's sandbox manager does not publish its image list yet"
        in result.stdout
    )
    assert "Building the sandbox images did not finish" in result.stdout
    assert "the stack was not started" in result.stdout
    assert not any(" up -d" in c for c in calls)
    assert (data_root / "images.lock.json").exists(), "the platform images already built are kept"
    assert (
        not (data_root / "sandbox-images.lock.json").exists() and not (data_root / ".env").exists()
    )


def test_build_pulls_tags_builds_writes_a_pinned_lock_and_starts_the_stack(tmp_path: Path) -> None:
    result, calls, data_root = run_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    repo = tmp_path / "repo"
    first_party, third_party = quickstart_images()
    for pulled in third_party:
        assert f"docker pull --quiet {pulled}" in calls
        reference = next(i for i in default_lock().images if i.pull_reference == pulled).reference
        assert f"docker tag {pulled} local/{reference}" in calls
        assert f"Pulled {pulled} (sha256:" in out
    for name in first_party:
        assert (
            f"docker build --file {repo}/images/{name}/Dockerfile --tag local/slas/{name}:{VERSION} {repo}"
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
    assert lock.unpinned("quickstart", ("coding",)) == [], "every image the install starts"
    assert {i.name for i in lock.unpinned("quickstart")} == {
        "validation-executor",
        "factory-executor",
        "local-search-api",
        "qdrant",
    }, "the parts that are off were not built or pulled (ADR-0017)"
    assert {i.name for i in lock.unpinned("prod")} == {
        "validation-executor",
        "factory-executor",
        "local-search-api",
        "qdrant",
        "mc",
        "vault",
        "keycloak",
        "loki",
        "tempo",
        "postgres-pgbackrest",
    }
    for image in lock.for_profile("quickstart", ("coding",)):
        assert image.image_id and image.image_id.startswith("sha256:")
        assert (image.digest is None) is image.first_party
    vllm = next(i for i in lock.images if i.name == "vllm")
    assert vllm.started_by == "model-manager" and vllm.pinned
    assert (
        vllm.digest == "sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a"
    ), "the pull by digest keeps the digest the lock records"

    # The sandbox images: built from the mirror root, locked beside the image lock, and the
    # toolchain manifest they satisfy written where the sandbox manager reads it.
    for tag in SANDBOX_TAGS:
        name = tag.split("/")[-1].split(":")[0]
        assert f"docker build --file {repo}/images/{name}/Dockerfile --tag {tag} {repo}" in calls
        assert f"Built {tag} from images/{name}/Dockerfile (image ID sha256:" in out
    sandbox_lock = json.loads((data_root / "sandbox-images.lock.json").read_text())
    assert sandbox_lock["registry"] == "local"
    assert [i["tag"] for i in sandbox_lock["images"]] == list(SANDBOX_TAGS)
    assert all(i["image_id"].startswith("sha256:") for i in sandbox_lock["images"])
    assert (
        f"Wrote the sandbox image lock to {data_root}/sandbox-images.lock.json: 2 images built."
        in out
    )
    manifest = json.loads((data_root / "Toolchains" / "manifest.json").read_text())
    assert manifest["toolchains"] == {"python": ["3.12.6"], "shell": ["5.2.21"]}
    assert f"Wrote the toolchain manifest to {data_root}/Toolchains/manifest.json" in out

    # The runtime socket: no Podman socket, so .env names Docker's; the data directories the
    # services bind-mount exist and belong to this user.
    docker_sock = fake_docker_socket(tmp_path)
    assert (
        f"SLAS_RUNTIME_SOCKET={docker_sock} in .env points model-manager and sandbox-manager at Docker's socket"
        in out
    )
    env_text = (data_root / ".env").read_text()
    assert f"SLAS_RUNTIME_SOCKET={docker_sock}\n" in env_text
    for relative in (
        "Toolchains",
        ".git-broker",
        "Tickets",
        "Skills/library",
        "SOP",
        "Validation",
        "Factory/Templates",
        "Factory/mes/inbox",
        "Factory/ca",
        "Coding",
        "Models",
        "qdrant",
    ):
        assert (data_root / relative).is_dir(), relative
        assert (data_root / relative).stat().st_uid == os.getuid()
    assert f"data directories under {data_root} as uid {os.getuid()}" in out
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
    assert "SLAS_TLS_NAMES=127.0.0.1,localhost," in env_text, "the edge answers to the host's names"
    assert "SLAS_PUBLIC_HOST=" in env_text and "SLAS_PUBLIC_HOST=\n" not in env_text
    assert "The certificate is self-signed: the browser asks once whether to continue." in out
    password = (data_root / "secrets" / "admin-initial-password").read_text().strip()
    assert f"one-time password: {password}" in out

    # Once the administrator chose a password, the one-time password is not shown again. The
    # second run keeps the runtime socket .env already names and finds every directory in place.
    result, _, _ = run_install(tmp_path, env_extra={"STUB_BOOTSTRAP": "done"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already chose a password" in result.stdout and password not in result.stdout
    assert f"SLAS_RUNTIME_SOCKET is set to {docker_sock};" in result.stdout
    assert f"Every data directory under {data_root} exists." in result.stdout


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
    assert not any(c.startswith("docker rm ") for c in calls), "nothing stale, nothing removed"


def test_containers_of_the_parts_that_are_off_are_removed_and_do_not_hold_the_wait(
    tmp_path: Path,
) -> None:
    """ADR-0017: an earlier install started the knowledge base; this one (coding only) removes
    its containers before `compose up`, since compose leaves a service whose profile is off
    alone, and the health wait ignores them even when `ps --all` still lists one."""
    rows = "\n".join(
        json.dumps(row)
        for row in (
            {"Service": "postgres", "State": "running", "Health": "healthy"},
            {"Service": "vector-db", "State": "restarting", "Health": ""},
            {"Service": "local-search-api", "State": "created", "Health": ""},
        )
    )
    result, calls, data_root = run_install(
        tmp_path,
        env_extra={
            "STUB_STALE": "vector-db=aaa111 local-search-api=bbb222",
            "STUB_PS": rows,
            "SLAS_HEALTH_WAIT_S": "0",
            "SLAS_HEALTH_POLL_S": "0",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert (
        "Removing the vector-db container an earlier install started: vector-db is off now "
        f"(SLAS_AGENTS=coding); its data under {data_root} stays." in out
    )
    assert "Removing the local-search-api container an earlier install started" in out
    assert "Removing the validation-executor" not in out, "nothing of it was there"
    assert "docker rm -f aaa111" in calls and "docker rm -f bbb222" in calls
    removals = [i for i, c in enumerate(calls) if c.startswith("docker rm -f ")]
    up = next(i for i, c in enumerate(calls) if " up -d --pull never --remove-orphans" in c)
    assert max(removals) < up, "stale containers go before the stack starts"
    assert "not healthy" not in out and "SW Local Agent Service is up." in out
    assert any(c.startswith("docker compose") and " ps --all --format json" in c for c in calls)


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
    assert unhealthy_services(json.dumps(rows), ignore=["grafana", "backup-runner"]) == [
        "api",
        "edge",
        "slas-vector-db-1",
    ]


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
            if "@sha256:" in argv[-1]:
                return build.Completed(0, argv[-1] + "\n")  # pulled by digest: that digest
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
    vllm = next(i for i in filled.images if i.name == "vllm")
    assert vllm.pinned and vllm.started_by == "model-manager"
    assert [
        "docker",
        "pull",
        "--quiet",
        "docker.io/vllm/vllm-openai@sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a",
    ] in docker.calls, "an image whose digest the lock records is pulled by that digest"
    assert "Pulled docker.io/vllm/vllm-openai@sha256:3e10e8189823" in out.getvalue()
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


# --- the sandbox images, the runtime socket and the data directories (ADR-0015) ---------------


class FakeHostTools(FakeDocker):
    """FakeDocker plus the sandbox manager's `images list|manifest` commands."""

    def __init__(self, listing: str = "", failing: tuple[str, ...] = ()) -> None:
        super().__init__(failing)
        self.listing = listing
        self.manifests: list[Path] = []

    def run(self, argv: Sequence[str], *, capture: bool = True) -> build.Completed:
        if len(argv) > 3 and list(argv[1:3]) == ["-m", sandbox_images.LISTING_MODULE]:
            self.calls.append(list(argv))
            if argv[3] == "list":
                return build.Completed(0, self.listing)
            out = Path(argv[list(argv).index("--out") + 1])
            out.write_text('{"version": 1, "toolchains": {"python": ["3.12.6"]}}')
            self.manifests.append(out)
            return build.Completed(0, f"Wrote {out}\n")
        return super().run(argv, capture=capture)


LISTING = (
    "# name\ttag\tdockerfile\n"
    "sandbox-python\tlocal/slas/sandbox-python:3.12.6\timages/sandbox-python/Dockerfile\n"
    "sandbox-go\tlocal/slas/sandbox-go:1.23.1\timages/sandbox-go/Dockerfile\n"
)


def run_sandbox(
    tmp_path: Path, runner: build.Runner, *, dry_run: bool, repo: Path | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    code = sandbox_images.run(
        python="/venv/bin/python", registry="local", repo=repo or tmp_path,
        lock_out=tmp_path / "d" / "sandbox-images.lock.json",
        manifest_out=tmp_path / "d" / "Toolchains" / "manifest.json",
        dry_run=dry_run, runner=runner, out=out,
    )  # fmt: skip
    return code, out.getvalue()


def test_sandbox_images_are_listed_built_locked_and_the_manifest_written(tmp_path: Path) -> None:
    tools = FakeHostTools(LISTING)
    code, text = run_sandbox(tmp_path, tools, dry_run=False)
    assert code == 0, text
    assert tools.calls[0] == [
        "/venv/bin/python",
        "-m",
        "slas_sandbox_manager.images",
        "list",
        "--registry",
        "local",
    ]
    assert [
        "docker",
        "build",
        "--file",
        str(tmp_path / "images/sandbox-python/Dockerfile"),
        "--tag",
        "local/slas/sandbox-python:3.12.6",
        str(tmp_path),
    ] in tools.calls
    assert [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "local/slas/sandbox-go:1.23.1",
    ] in tools.calls
    manifest_path = tmp_path / "d" / "Toolchains" / "manifest.json"
    assert tools.calls[-1] == [
        "/venv/bin/python",
        "-m",
        "slas_sandbox_manager.images",
        "manifest",
        "--out",
        str(manifest_path),
    ]
    lock = json.loads((tmp_path / "d" / "sandbox-images.lock.json").read_text())
    assert [(i["name"], i["tag"], i["dockerfile"]) for i in lock["images"]] == [
        ("sandbox-python", "local/slas/sandbox-python:3.12.6", "images/sandbox-python/Dockerfile"),
        ("sandbox-go", "local/slas/sandbox-go:1.23.1", "images/sandbox-go/Dockerfile"),
    ]
    assert all(i["image_id"].startswith("sha256:") for i in lock["images"])
    assert "never committed" in lock["note"].lower() and lock["registry"] == "local"
    assert tools.manifests == [manifest_path]
    assert (
        "Built local/slas/sandbox-python:3.12.6 from images/sandbox-python/Dockerfile (image ID sha256:"
        in text
    )
    assert "Wrote the sandbox image lock to" in text and "2 images built." in text
    assert "Wrote the toolchain manifest to" in text and "resolved against" in text

    code, described = run_sandbox(tmp_path, FakeHostTools(LISTING), dry_run=True)
    assert code == 0
    assert described == (
        "Would build the sandbox image local/slas/sandbox-python:3.12.6 from images/sandbox-python/Dockerfile.\n"
        "Would build the sandbox image local/slas/sandbox-go:1.23.1 from images/sandbox-go/Dockerfile.\n"
        f"Would write the sandbox image lock to {tmp_path}/d/sandbox-images.lock.json (2 built) and the toolchain manifest to {manifest_path}; neither is committed.\n"
    )


def test_sandbox_image_problems_are_three_part_errors(tmp_path: Path) -> None:
    for bad, fragment in (
        (
            "sandbox-python\tlocal/slas/sandbox-python:3.12.6\n",
            "is not `name<TAB>tag<TAB>dockerfile`",
        ),
        (
            "sandbox-python\tlocal/slas/sandbox-python:latest\timages/x/Dockerfile\n",
            "is not a pinned tag",
        ),
        ("sandbox-python\tno tag here\timages/x/Dockerfile\n", "is not a pinned tag"),
    ):
        with pytest.raises(build.BuildError) as exc:
            sandbox_images.parse_listing(bad)
        assert fragment in exc.value.message.what_happened
        assert exc.value.message.likely_cause and exc.value.message.what_to_do
    assert sandbox_images.parse_listing("# only a comment\n\n") == []
    code, text = run_sandbox(tmp_path, FakeHostTools("# only a comment\n"), dry_run=False)
    assert code == 0 and text.startswith("The sandbox manager lists no sandbox image")

    code, text = run_sandbox(
        tmp_path,
        FakeHostTools(LISTING, failing=("docker build --file",)),
        dry_run=False,
        repo=Path("/repo"),
    )
    assert code == 1
    assert text.startswith(
        "Building the sandbox image local/slas/sandbox-python:3.12.6 did not finish (docker exited 1"
    )
    assert "Likely cause: A build step failed" in text and "./install.sh --build" in text
    assert not (tmp_path / "d" / "sandbox-images.lock.json").exists()

    class NoListing(FakeHostTools):
        def run(self, argv: Sequence[str], *, capture: bool = True) -> build.Completed:
            if "list" in argv:
                return build.Completed(
                    1, "", "No module named slas_sandbox_manager.images.__main__"
                )
            return super().run(argv, capture=capture)

    code, text = run_sandbox(tmp_path, NoListing(), dry_run=True)
    assert code == 0, "a dry run names the gap and goes on"
    assert (
        "The sandbox image list could not be read (`python -m slas_sandbox_manager.images list` exited 1: No module named"
        in text
    )
    code, text = run_sandbox(tmp_path, NoListing(), dry_run=False)
    assert code == 1
    assert (
        "What to do: Update the checkout to a revision where services/sandbox-manager ships `images list|manifest`"
        in text
    )
    code, text = run_sandbox(tmp_path, FakeHostTools(""), dry_run=False)
    assert code == 1 and "exited 0: printed nothing" in text, (
        "a module without the command prints nothing"
    )


def test_runtime_socket_choice_prefers_podman_then_docker_then_says_neither(tmp_path: Path) -> None:
    from slas_deploy.installer import choose_runtime_socket

    podman, docker = tmp_path / "p.sock", tmp_path / "d.sock"
    neither = choose_runtime_socket("", podman=str(podman), docker=str(docker))
    assert neither.path == "" and neither.sentence.startswith(
        f"Neither {podman} nor {docker} exists yet"
    )
    for path in (podman, docker):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.close()
    both = choose_runtime_socket("", podman=str(podman), docker=str(docker))
    assert both.path == "" and both.sentence.startswith(
        f"Podman's socket {podman} serves the container runtime"
    )
    podman.unlink()
    only_docker = choose_runtime_socket("", podman=str(podman), docker=str(docker))
    assert (
        only_docker.path == str(docker) and only_docker.key_value == f"SLAS_RUNTIME_SOCKET={docker}"
    )
    assert only_docker.sentence == (
        f"No Podman socket at {podman}, so SLAS_RUNTIME_SOCKET={docker} in .env points model-manager and sandbox-manager at Docker's socket; nothing else sees it (INV-4)."
    )
    (tmp_path / "plain-file").write_text("not a socket")
    assert (
        choose_runtime_socket(
            "", podman=str(tmp_path / "plain-file"), docker=str(tmp_path / "plain-file")
        ).path
        == ""
    )
    kept = choose_runtime_socket(
        " /run/user/1000/podman/podman.sock ", podman=str(podman), docker=str(docker)
    )
    assert kept.path == "/run/user/1000/podman/podman.sock"
    assert kept.sentence.startswith(
        "SLAS_RUNTIME_SOCKET is set to /run/user/1000/podman/podman.sock;"
    )

    out = io.StringIO()
    assert (
        installer_main(
            [
                "runtime-socket",
                "--configured",
                "",
                "--podman",
                str(podman),
                "--docker",
                str(docker),
                "--json",
            ],
            stdout=out,
        )
        == 0
    )
    assert json.loads(out.getvalue()) == {"path": str(docker), "sentence": only_docker.sentence}


def test_write_env_records_the_runtime_socket_only_while_it_is_at_the_default(
    tmp_path: Path,
) -> None:
    from slas_deploy.installer import write_env
    from slas_schemas.envfile import read_env

    target = tmp_path / ".env"

    def write(runtime_socket: str | None) -> list[str]:
        return write_env(
            example=REPO_ROOT / "config" / ".env.example", target=target, profile="quickstart", data_root=tmp_path,
            version="1", registry="local", uid=1, gid=1, tls_names="x", public_host="y", runtime_socket=runtime_socket,
        )  # fmt: skip

    changed = write("/var/run/docker.sock")
    assert "SLAS_RUNTIME_SOCKET" in changed
    assert read_env(target).get("SLAS_RUNTIME_SOCKET") == "/var/run/docker.sock"
    assert read_env(target).get("SLAS_GPU_VRAM_GIB") == "270", "the template's default rides along"
    env = read_env(target)
    env.set("SLAS_RUNTIME_SOCKET", "/run/user/1000/podman/podman.sock")
    target.write_text(env.render())
    assert write("/var/run/docker.sock") == []
    assert read_env(target).get("SLAS_RUNTIME_SOCKET") == "/run/user/1000/podman/podman.sock", (
        "a person's choice is kept"
    )
    assert write(None) == []


def test_write_env_merges_tls_names_and_replaces_the_public_host_only_when_chosen(
    tmp_path: Path,
) -> None:
    """A browser may use the host's IP: the edge's names can only grow, and the sign-in name
    changes only when the person names it (--public-host)."""
    from slas_deploy.installer import merge_names, write_env
    from slas_schemas.envfile import read_env

    assert merge_names("", "127.0.0.1,localhost,rex") == "127.0.0.1,localhost,rex"
    assert merge_names("127.0.0.1,localhost,rex", "127.0.0.1, localhost,rex,10.1.2.3,rex") == (
        "127.0.0.1,localhost,rex,10.1.2.3"
    )
    assert merge_names("lab.example, rex", "127.0.0.1,rex") == "lab.example,rex,127.0.0.1"

    target = tmp_path / ".env"

    def write(tls_names: str, public_host: str, *, chosen: bool = False) -> list[str]:
        return write_env(
            example=REPO_ROOT / "config" / ".env.example", target=target, profile="quickstart", data_root=tmp_path,
            version="1", registry="local", uid=1, gid=1, tls_names=tls_names, public_host=public_host,
            public_host_chosen=chosen,
        )  # fmt: skip

    # First install: the host's name only (as installs before the host's addresses were added).
    changed = write("127.0.0.1,localhost,rex", "rex")
    assert {"SLAS_TLS_NAMES", "SLAS_PUBLIC_HOST"} <= set(changed)
    env = read_env(target)
    assert (
        env.get("SLAS_TLS_NAMES") == "127.0.0.1,localhost,rex"
        and env.get("SLAS_PUBLIC_HOST") == "rex"
    )
    # A person adds a name by hand; the next run keeps it and adds the address it learnt.
    env.set("SLAS_TLS_NAMES", "127.0.0.1,localhost,rex,lab.example")
    target.write_text(env.render())
    assert write("127.0.0.1,localhost,rex,10.1.2.3,rex", "rex") == ["SLAS_TLS_NAMES"]
    env = read_env(target)
    assert env.get("SLAS_TLS_NAMES") == "127.0.0.1,localhost,rex,lab.example,10.1.2.3"
    assert env.get("SLAS_PUBLIC_HOST") == "rex", "not chosen on this run, so the file's name stays"
    # The same run again changes nothing; --public-host <ip> replaces the sign-in name.
    assert write("127.0.0.1,localhost,rex,10.1.2.3", "rex") == []
    assert write("127.0.0.1,localhost,rex,10.1.2.3", "10.1.2.3", chosen=True) == [
        "SLAS_PUBLIC_HOST"
    ]
    assert read_env(target).get("SLAS_PUBLIC_HOST") == "10.1.2.3"


def test_data_dirs_are_created_once_as_this_user(tmp_path: Path) -> None:
    from slas_deploy.compose import DATA_DIRECTORIES
    from slas_deploy.installer import create_data_dirs

    root = tmp_path / "data"
    (root / "Tickets").mkdir(parents=True)
    created = create_data_dirs(root)
    assert set(created) == set(DATA_DIRECTORIES) - {"Tickets"}
    for relative in DATA_DIRECTORIES:
        assert (root / relative).is_dir() and (root / relative).stat().st_uid == os.getuid()
    assert create_data_dirs(root) == []
    out = io.StringIO()
    assert installer_main(["data-dirs", "--root", str(root)], stdout=out) == 0
    assert out.getvalue() == f"Every data directory under {root} exists.\n"
    dry = io.StringIO()
    assert (
        installer_main(["data-dirs", "--root", str(tmp_path / "nowhere"), "--dry-run"], stdout=dry)
        == 0
    )
    assert dry.getvalue().startswith(
        f"Would create the missing data directories under {tmp_path}/nowhere: Coding, Toolchains, .git-broker"
    )
    assert not (tmp_path / "nowhere").exists()
