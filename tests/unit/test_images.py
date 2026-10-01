"""Every first-party image the lock names has a Dockerfile under images/<name>/, built from the
repository root; every base is pinned by digest (INV-8); every image carries slas-health; the
Python service images are rendered from one template (ADR-0014)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from slas_deploy import dockerfiles
from slas_deploy.images import DEFAULT_IMAGES

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGES = REPO_ROOT / "images"
FIRST_PARTY = [image.name for image in DEFAULT_IMAGES if image.first_party]
DIGEST_REF = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
FROM_LINE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE | re.MULTILINE)


def dockerfile(name: str) -> str:
    return (IMAGES / name / "Dockerfile").read_text(encoding="utf-8")


def test_the_lock_names_the_images_the_task_expects() -> None:
    assert set(FIRST_PARTY) == {
        "edge", "webui", "api", "agent-core-orchestrator", "llm-gateway", "model-manager",
        "model-fetcher", "sandbox-manager", "screen-worker", "git-broker", "validation-executor",
        "factory-executor", "local-search-api", "postgres-pgbackrest",
    }  # fmt: skip
    # The model fetcher is the one quickstart-only image (ADR-0018): prod never starts it.
    fetcher = next(image for image in DEFAULT_IMAGES if image.name == "model-fetcher")
    assert fetcher.profiles == ["quickstart"] and fetcher.first_party
    rendered = {service.name for service in dockerfiles.PYTHON_SERVICES}
    assert rendered | set(dockerfiles.HAND_WRITTEN) == set(FIRST_PARTY)
    assert not rendered & set(dockerfiles.HAND_WRITTEN)


@pytest.mark.parametrize("name", FIRST_PARTY)
def test_every_first_party_image_has_a_dockerfile_pinned_by_digest_with_slas_health(
    name: str,
) -> None:
    path = IMAGES / name / "Dockerfile"
    assert path.is_file(), f"{path} is missing"
    text = path.read_text(encoding="utf-8")
    stages: set[str] = set()
    froms = FROM_LINE.findall(text)
    assert froms, f"{name}: no FROM line"
    known = {ref for _, ref in dockerfiles.BASES.values()}
    for reference, stage in froms:
        if reference in stages:
            continue  # FROM <stage>
        if reference.startswith("${SLAS_REGISTRY}/"):
            assert ":" in reference and not reference.endswith(":latest"), (name, reference)
        else:
            assert DIGEST_REF.match(reference), f"{name}: {reference} is not pinned by digest"
            assert reference in known, f"{name}: {reference} is not in dockerfiles.BASES"
        if stage:
            stages.add(stage)
    assert ":latest" not in text
    assert f"COPY {dockerfiles.HEALTH_SOURCE} {dockerfiles.HEALTH_TARGET}" in text, (
        f"{name} must carry slas-health"
    )
    assert "docker build -f images/" in text, f"{name}: say how it is built (repository root)"


def test_python_service_dockerfiles_are_rendered_from_one_template() -> None:
    for service in dockerfiles.PYTHON_SERVICES:
        text = dockerfile(service.name)
        assert text == dockerfiles.python_service_dockerfile(service), (
            f"images/{service.name}/Dockerfile drifted; run `uv run python -m slas_deploy.render`"
        )
        assert f"uv sync --frozen --no-dev --no-editable --package {service.package}" in text
        assert "COPY pyproject.toml uv.lock ./" in text
        assert f"USER {dockerfiles.IMAGE_UID}:{dockerfiles.IMAGE_UID}" in text
        assert "EXPOSE 8000" in text
        cmd = ", ".join(f'"{part}"' for part in service.cmd)
        assert text.rstrip().endswith(f"CMD [{cmd}]")
    by_name = {service.name: service for service in dockerfiles.PYTHON_SERVICES}
    assert by_name["api"].cmd == ("slas-api", "serve")
    assert by_name["api"].package == "slas-api"
    # Contract round 2 §1: each service's console script `serve` is its container command.
    for name, script in {
        "llm-gateway": "slas-gateway",
        "model-manager": "slas-model-manager",
        "sandbox-manager": "slas-sandbox-manager",
        "agent-core-orchestrator": "slas-orchestrator",
        "git-broker": "slas-git-broker",
        "validation-executor": "slas-validation-executor",
        "factory-executor": "slas-factory-executor",
    }.items():
        assert by_name[name].cmd == (script, "serve"), name
        assert dockerfile(name).rstrip().endswith(f'CMD ["{script}", "serve"]'), name
    # Services whose entrypoint has not landed keep the placeholder health runner.
    assert by_name["local-search-api"].cmd == (
        "python", "-m", "slas_observability.serve", "local-search-api",
    )  # fmt: skip
    assert "git" in by_name["git-broker"].apt
    assert "openssl" in by_name["factory-executor"].apt, "the station CA is driven with openssl"
    assert "openssh-client openssl" in dockerfile("factory-executor")
    assert "COPY apps/api/ apps/api/" in dockerfile("api")


def test_hand_written_images_serve_what_compose_and_the_edge_expect() -> None:
    edge = dockerfile("edge")
    assert 'ENTRYPOINT ["/usr/local/bin/slas-edge"]' in edge and "EXPOSE 443" in edge
    assert "XDG_DATA_HOME=/data/tls" in edge, "the CA must live on the tls volume"
    entrypoint = (IMAGES / "edge" / "slas-edge").read_text(encoding="utf-8")
    subprocess.run(["sh", "-n", str(IMAGES / "edge" / "slas-edge")], check=True, timeout=30)
    for fragment in (
        "tls internal",
        "tls /data/tls/server.crt /data/tls/server.key",
        "reverse_proxy api:8000",
        "reverse_proxy grafana:3000",
        "reverse_proxy webui:8000",
        "handle /healthz",
        "handle /api/*",
        "handle /grafana/*",
        "skip_install_trust",
        "admin off",
        "root /data/tls/caddy",
        "tr ',' ' '",
    ):
        assert fragment in entrypoint, fragment
    assert "handle_path /grafana" not in entrypoint, "Grafana serves from the /grafana sub-path"

    webui = dockerfile("webui")
    assert "corepack enable" in webui and "pnpm install --frozen-lockfile" in webui
    assert "pnpm --filter @slas/webui build" in webui
    assert "COPY --from=build /src/apps/webui/dist /srv" in webui
    caddyfile = (IMAGES / "webui" / "Caddyfile").read_text(encoding="utf-8")
    assert ":8000 {" in caddyfile and "auto_https off" in caddyfile
    assert "try_files {path} /index.html" in caddyfile and "handle /health" in caddyfile

    screen = dockerfile("screen-worker")
    assert "COPY services/screen-worker/entrypoint.sh /usr/local/bin/slas-screen-worker" in screen
    assert (
        "COPY packages/slas-observability/slas_observability /opt/slas/slas_observability" in screen
    )
    assert not (REPO_ROOT / "services" / "screen-worker" / "Dockerfile").exists(), (
        "the screen-worker Dockerfile lives under images/ with the other first-party images"
    )
    entry = (REPO_ROOT / "services" / "screen-worker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "python3 -m slas_observability.serve screen-worker" in entry
    subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "services" / "screen-worker" / "entrypoint.sh")],
        check=True,
        timeout=30,
    )


def _run_screen_entrypoint(
    tmp_path: Path, *, lock_pid: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the screen-worker entrypoint with stub Xvfb, x11vnc, websockify and python3 (no
    display, no network), against a lock directory of the test's own."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("Xvfb", "x11vnc", "websockify"):
        (bin_dir / name).write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "python3").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    x_tmp = tmp_path / "xtmp"
    (x_tmp / ".X11-unix").mkdir(parents=True)
    if lock_pid is not None:
        (x_tmp / ".X97-lock").write_text(f"{lock_pid:>10}\n", encoding="utf-8")
        (x_tmp / ".X11-unix" / "X97").write_text("", encoding="utf-8")
    return subprocess.run(
        ["bash", str(REPO_ROOT / "services" / "screen-worker" / "entrypoint.sh")],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "SLAS_DISPLAY": "97",
            "SLAS_X_TMP": str(x_tmp),
            "SLAS_SESSION_ID": "test",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_screen_worker_entrypoint_removes_a_stale_xvfb_lock_but_respects_a_live_one(
    tmp_path: Path,
) -> None:
    """The first host: the screen-worker container restarted, /tmp kept the previous Xvfb's
    lock, and Xvfb refused to start ("Server is already active for display 10") on every
    restart. A lock whose pid is dead is removed; one whose pid lives stops the start."""
    stale = _run_screen_entrypoint(tmp_path / "stale", lock_pid="2147483000")
    assert stale.returncode == 0, stale.stderr
    assert "Removing the stale lock" in stale.stdout
    assert not (tmp_path / "stale" / "xtmp" / ".X97-lock").exists()
    assert not (tmp_path / "stale" / "xtmp" / ".X11-unix" / "X97").exists()

    live = _run_screen_entrypoint(tmp_path / "live", lock_pid=str(os.getpid()))
    assert live.returncode == 1
    assert "already served by process" in live.stdout
    assert (tmp_path / "live" / "xtmp" / ".X97-lock").exists(), "a live server's lock is kept"

    fresh = _run_screen_entrypoint(tmp_path / "fresh", lock_pid=None)
    assert fresh.returncode == 0 and "stale lock" not in fresh.stdout


def test_base_digests_are_documented_and_the_context_is_kept_small() -> None:
    readme = (IMAGES / "README.md").read_text(encoding="utf-8")
    for tag, reference in dockerfiles.BASES.values():
        digest = reference.rsplit("@", 1)[1]
        assert tag in readme and digest in readme, f"images/README.md must list {tag} {digest}"
    for script in ("slas-gateway serve", "slas-model-manager serve", "slas-orchestrator serve"):
        assert script in readme, f"images/README.md must name the container command {script}"
    vllm = next(image for image in DEFAULT_IMAGES if image.name == "vllm")
    assert vllm.reference in readme and "model manager" in readme, (
        "images/README.md must say the vLLM image is started by the model manager"
    )
    ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".git", ".venv", "**/node_modules", ".claude", "models"):
        assert entry in ignore, f".dockerignore must exclude {entry}"
    build_bundle = (REPO_ROOT / "scripts" / "build-bundle.sh").read_text(encoding="utf-8")
    assert (
        '-f "$repo/images/$name/Dockerfile"' in build_bundle
        and '"$repo" >/dev/null' in build_bundle
    )


def test_agents_choose_which_executor_images_start() -> None:
    """ADR-0017: coding is always on; validation and factory bring their executor images."""
    from slas_deploy.images import (
        AgentsError,
        compose_profiles,
        default_lock,
        images_off_for,
        parse_agents,
        services_off_for,
    )

    assert parse_agents(None) == ("coding",) and parse_agents("  ") == ("coding",)
    assert parse_agents("validation") == ("coding", "validation")
    assert parse_agents("factory, coding,factory") == ("coding", "factory")
    assert parse_agents("coding,validation,factory") == ("coding", "validation", "factory")
    assert parse_agents("knowledge") == ("coding", "knowledge")
    with pytest.raises(AgentsError) as raised:
        parse_agents("coding,screen")
    assert raised.value.message.what_happened == 'The agent "screen" is not known.'
    assert images_off_for(("coding",)) == {
        "validation-executor",
        "factory-executor",
        "local-search-api",
        "qdrant",
    }
    assert images_off_for(("coding", "validation", "knowledge")) == {"factory-executor"}
    # The containers the installer removes when a part is off (the compose service names).
    assert services_off_for(("coding",)) == [
        "validation-executor",
        "factory-executor",
        "vector-db",
        "local-search-api",
    ]
    assert services_off_for(("coding", "knowledge")) == ["validation-executor", "factory-executor"]
    assert services_off_for(("coding", "validation", "factory", "knowledge")) == []
    # Quickstart adds the model fetcher's `fetch` profile (ADR-0018); prod never does, and
    # removes a leftover model-fetcher container like the parts that are off.
    assert compose_profiles(("coding",)) == ["fetch"]
    assert compose_profiles(("coding",), "prod") == []
    assert compose_profiles(("coding", "factory")) == ["factory", "fetch"]
    assert compose_profiles(("coding", "validation", "factory", "knowledge"), "prod") == [
        "validation",
        "factory",
        "knowledge",
    ]
    assert services_off_for(("coding",), "prod")[-1] == "model-fetcher"
    assert "model-fetcher" not in services_off_for(("coding",), "quickstart")
    from slas_deploy.images import profiles_sentence

    assert profiles_sentence([]) == ""
    assert profiles_sentence(["fetch"]) == (
        "Compose profiles: fetch (the model fetcher of the quickstart profile, ADR-0018)."
    )
    assert profiles_sentence(["validation", "factory"]) == (
        "Compose profiles: validation,factory (the parts you chose with --agents)."
    )
    assert profiles_sentence(["factory", "fetch"]) == (
        "Compose profiles: factory,fetch (the parts you chose with --agents, and the model "
        "fetcher of the quickstart profile, ADR-0018)."
    )

    lock = default_lock()
    everything = {image.name for image in lock.for_profile("quickstart")}
    coding_only = {image.name for image in lock.for_profile("quickstart", ("coding",))}
    assert everything - coding_only == {
        "validation-executor",
        "factory-executor",
        "local-search-api",
        "qdrant",
    }
    assert "agent-core-orchestrator" in coding_only and "sandbox-manager" in coding_only
