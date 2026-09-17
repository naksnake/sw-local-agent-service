"""The sandbox-manager service: the Engine API runtime, the isolation probe, every route of
docs/api-contract-round-2.md §4 over FakeContainerApi, the reaper, the images CLI, the cli."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slas_container import ContainerError, CreateSpec, FakeContainerApi
from slas_container import ExecResult as ContainerExecResult
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_observability.events import EventLog, ListSink
from slas_sandbox_manager import images
from slas_sandbox_manager.cli import main as cli_main
from slas_sandbox_manager.manager import PUSH_EXPLANATION
from slas_sandbox_manager.runtime import (
    ContainerApiRuntime,
    detect_isolation,
    parse_size,
    probe_runtime,
    seccomp_option,
    translate_path,
)
from slas_sandbox_manager.service.app import (
    Reaper,
    Services,
    build_services,
    create_app,
    route_table,
)
from slas_sandbox_manager.service.settings import Settings, SettingsError
from slas_sandbox_manager.spec import GITCONFIG_TARGET, SCRATCH, WORKSPACE, Mount, SandboxSpec
from slas_sandbox_manager.toolchains import default_manifest, image_for
from slas_schemas.errors import ThreePartMessage

REPO_ROOT = Path(__file__).resolve().parents[2]
THREE_PARTS = ("what_happened", "likely_cause", "what_to_do")
IMAGES = [recipe.tag("local") for recipe in images.RECIPES]
PYTHON_IMAGE = "local/slas/sandbox-python:3.12.6"
START = datetime(2026, 9, 17, 8, tzinfo=UTC)


def sandbox_spec(runtime: str = "runsc", **overrides: Any) -> SandboxSpec:
    base: dict[str, Any] = {
        "name": "slas-sbx-fan-ctl-1",
        "image": PYTHON_IMAGE,
        "runtime": runtime,
        "user": "pat",
        "slug": "fan-ctl",
        "mounts": [
            Mount(source="/data/Coding/pat/Projects/fan-ctl", target=WORKSPACE, mode="rw"),
            Mount(source="/data/Coding/pat/Container/s1", target=SCRATCH, mode="rw"),
            Mount(source="/data/Coding/pat/gitconfig", target=GITCONFIG_TARGET, mode="ro"),
        ],
        "env": {"HOME": SCRATCH, "GIT_CONFIG_GLOBAL": GITCONFIG_TARGET, "SLAS_LANGUAGE": "python"},
    }
    return SandboxSpec.model_validate({**base, **overrides})


class NoRunsc(FakeContainerApi):
    """A Docker host without gVisor: any `runsc` container is refused at create."""

    def create(self, spec: CreateSpec) -> str:
        if spec.runtime == "runsc":
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime refused to create {spec.name} from {spec.image}.",
                    "It answered 400: unknown or invalid runtime name: runsc.",
                    "Read the message above.",
                ),
                status=400,
            )
        return super().create(spec)


class StartFails(FakeContainerApi):
    def start(self, name: str) -> None:
        raise ContainerError(
            ThreePartMessage("The container runtime refused to start x.", "OCI error.", "Look.")
        )


# --- ContainerApiRuntime --------------------------------------------------------------------------


def test_runtime_body_carries_the_hardening_and_host_paths() -> None:
    api = FakeContainerApi("docker", images=IMAGES)
    runtime = ContainerApiRuntime(api, host_data_root="/AI/Agent", container_data_root="/data")
    handle = runtime.create(sandbox_spec())
    assert handle.id == "id-slas-sbx-fan-ctl-1" and handle.name == "slas-sbx-fan-ctl-1"
    body = api.bodies["slas-sbx-fan-ctl-1"]
    host = body["HostConfig"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges"]
    assert host["Runtime"] == "runsc" and host["PidsLimit"] == 512
    assert host["Memory"] == 4 * 1024**3 and host["NanoCpus"] == 2_000_000_000
    assert host["Tmpfs"] == {"/tmp": "rw,nosuid,nodev,noexec,size=512m"}  # noqa: S108
    assert host["Binds"] == [
        "/AI/Agent/Coding/pat/Projects/fan-ctl:/workspace:rw",
        "/AI/Agent/Coding/pat/Container/s1:/scratch:rw",
        "/AI/Agent/Coding/pat/gitconfig:/etc/slas/gitconfig:ro",
    ], "the runtime sees host paths, never the manager's /data"
    assert host["RestartPolicy"] == {"Name": "no"}
    assert body["User"] == "10001:10001" and body["WorkingDir"] == "/workspace"
    assert body["Cmd"] == ["sleep", "infinity"] and "Entrypoint" not in body
    assert body["Image"] == PYTHON_IMAGE
    assert body["Labels"] == {
        "slas.kind": "sandbox",
        "slas.user": "pat",
        "slas.project": "fan-ctl",
        "slas.ttl_s": "3600",
    }
    assert "GIT_CONFIG_GLOBAL=/etc/slas/gitconfig" in body["Env"]
    assert "DeviceRequests" not in host and "IpcMode" not in host, "no GPU, no host IPC"
    assert api.states["slas-sbx-fan-ctl-1"] == "running" and runtime.alive(handle)

    api.handle_exec(
        lambda name, argv: ContainerExecResult(
            exit_code=1, stdout="", stderr=f"{argv[0]}: 1 failed"
        )
    )
    result = runtime.exec(handle, ["slas-check", "test"], cwd="/workspace", timeout_s=30)
    assert not result.ok and result.stderr == "slas-check: 1 failed" and result.exit_code == 1
    assert api.execs == [("slas-sbx-fan-ctl-1", ["slas-check", "test"])]
    with pytest.raises(ValueError, match="non-empty list of strings"):
        runtime.exec(handle, [])
    with pytest.raises(ValueError, match="stdin is not carried"):
        runtime.exec(handle, ["cat"], stdin="x")
    runtime.destroy(handle)
    assert not runtime.alive(handle) and api.inspect("slas-sbx-fan-ctl-1") is None


def test_runc_fallback_seccomp_translation_and_sizes(tmp_path: Path) -> None:
    profile = tmp_path / "seccomp-sandbox.json"
    docker = FakeContainerApi("docker", images=IMAGES)
    runtime = ContainerApiRuntime(docker, host_data_root="/AI/Agent", seccomp_profile=profile)
    assert runtime.seccomp_sentence().startswith("No seccomp profile is installed at")
    assert runtime.create_spec(sandbox_spec("runc")).security_opt == []
    profile.write_text('{"defaultAction": "SCMP_ACT_ERRNO"}\n', encoding="utf-8")
    assert runtime.seccomp_sentence().endswith(f"seccomp profile {profile}.")
    docker_body = runtime.create_spec(sandbox_spec("runc")).to_body("docker")["HostConfig"]
    assert docker_body["Runtime"] == "runc"
    assert docker_body["SecurityOpt"] == [
        'seccomp={"defaultAction": "SCMP_ACT_ERRNO"}',
        "no-new-privileges",
    ], "Docker takes the profile inline"
    assert runtime.create_spec(sandbox_spec()).security_opt == [], "gVisor needs none"
    podman = ContainerApiRuntime(
        FakeContainerApi("podman", images=IMAGES),
        host_data_root="/AI/Agent",
        seccomp_profile=profile,
    )
    assert podman.create_spec(sandbox_spec("runc")).security_opt == [f"seccomp={profile}"]
    assert seccomp_option("podman", None) is None

    assert translate_path("/data/Coding/x", container_root="/data", host_root="/AI/Agent") == (
        "/AI/Agent/Coding/x"
    )
    assert translate_path("/data", container_root="/data", host_root="/AI/Agent") == "/AI/Agent"
    assert translate_path("/etc/slas/x", container_root="/data", host_root="/AI/Agent") == (
        "/etc/slas/x"
    )
    assert parse_size("512m") == 512 * 1024**2 and parse_size("2k") == 2048
    with pytest.raises(ValueError, match="not a size"):
        parse_size("lots")


def test_a_failed_create_or_start_leaves_nothing_behind() -> None:
    api = FakeContainerApi("docker", images=["other:1"])
    runtime = ContainerApiRuntime(api, host_data_root="/AI/Agent")
    with pytest.raises(ContainerError) as missing:
        runtime.create(sandbox_spec())
    assert missing.value.status == 404 and api.specs == {}
    failing = StartFails("docker", images=IMAGES)
    with pytest.raises(ContainerError, match="refused to start"):
        ContainerApiRuntime(failing, host_data_root="/AI/Agent").create(sandbox_spec())
    assert failing.specs == {}, "the created-but-never-started container was removed"


# --- detection ------------------------------------------------------------------------------------


def test_detect_isolation_probes_with_a_throwaway_container() -> None:
    api = FakeContainerApi("docker", images=IMAGES)
    found = detect_isolation(api, probe_images=IMAGES)
    assert found.runsc_available and found.isolation == "gvisor" and not found.kata_available
    assert found.sentence == (
        "The runtime socket is served by Docker (API 1.47). gVisor (runsc) is registered; "
        "sandboxes run under it."
    )
    assert api.specs == {}, "the probe container is removed again"

    without = detect_isolation(NoRunsc("docker", images=IMAGES), probe_images=IMAGES)
    assert not without.runsc_available and without.isolation == "runc"
    assert without.sentence.startswith(
        "The runtime socket is served by Docker (API 1.47). gVisor (runsc) is not registered on "
        "this host (It answered 400: unknown or invalid runtime name: runsc.), so sandboxes fall "
        "back to hardened runc"
    )
    assert "Install gVisor" in without.sentence

    unknown = detect_isolation(FakeContainerApi("podman", images=["other:1"]), probe_images=IMAGES)
    assert unknown.runsc_available, "undetermined keeps the configured default"
    assert "could not be checked yet (no sandbox image is loaded yet to probe with)" in (
        unknown.sentence
    )
    assert "sandboxes will ask for runsc as configured" in unknown.sentence

    runc_only = detect_isolation(api, default_runtime="runc", probe_images=IMAGES)
    assert not runc_only.runsc_available and "DEFAULT_RUNTIME is runc" in runc_only.sentence

    kata = detect_isolation(api, tier="kata", probe_images=IMAGES)
    assert kata.kata_available and kata.sentence.endswith(
        "The Kata/Firecracker tier is registered."
    )
    no_kata = detect_isolation(NoRunsc("docker", images=IMAGES), tier="kata", probe_images=[])
    assert not no_kata.kata_available and "(kata-fc) is not registered" in no_kata.sentence
    assert probe_runtime(api, "runsc", []) == (None, "no sandbox image is loaded yet to probe with")

    down = FakeContainerApi("docker")
    down.down = True
    with pytest.raises(ContainerError):
        detect_isolation(down)


# --- the service ----------------------------------------------------------------------------------


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "data_root": tmp_path / "data",
        "host_data_root": Path("/AI/Agent"),
        "toolchain_manifest": tmp_path / "data" / "Toolchains" / "manifest.json",
        "default_ttl_s": 600,
        "max_sessions_per_user": 2,
        "seccomp_profile": tmp_path / "missing-seccomp.json",
    }
    return Settings(**{**base, **overrides})


def service(
    tmp_path: Path, *, api: FakeContainerApi | None = None, **overrides: Any
) -> tuple[TestClient, Services, FakeContainerApi, FakeClock, ListSink]:
    fake = api if api is not None else FakeContainerApi("docker", images=IMAGES)
    clock = FakeClock(START, step=timedelta(0))
    sink = ListSink()
    settings = settings_for(tmp_path, **overrides)
    services = build_services(
        settings, api=fake, clock=clock, log=EventLog("sandbox-manager", sink)
    )
    app = create_app(settings, services=services)
    return TestClient(app, raise_server_exceptions=False), services, fake, clock, sink


def open_body(**overrides: Any) -> dict[str, Any]:
    return {
        "user": "pat",
        "slug": "fan-ctl",
        "display_name": "Pat Lin",
        "languages": [
            {"language": "python", "version": None},
            {"language": "rust", "version": "1.99"},
        ],
        "ticket_id": "T-coding-0001",
        **overrides,
    }


def test_health_reports_the_runtime_and_the_isolation(tmp_path: Path) -> None:
    client, services, fake, _, sink = service(tmp_path)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "service": "sandbox-manager",
        "ok": True,
        "checks": {"runtime": "ok", "isolation": "gvisor"},
    }
    assert client.get("/metrics").status_code == 200
    detected = [r for r in sink.records() if r["event"] == "runtime.detected"]
    assert detected and detected[0]["level"] == "info"
    assert services.manager.runsc_available

    fake.down = True
    services.isolation = None  # as if the last probe had failed
    down = client.get("/health")
    assert down.status_code == 503
    body = down.json()
    assert set(body) == {*THREE_PARTS, "trace_id"}
    assert body["what_happened"] == "The sandbox-manager is not healthy: runtime did not answer."
    fake.down = False
    assert client.get("/health").status_code == 200, "the next probe finds the socket again"


def test_without_gvisor_the_service_falls_back_to_hardened_runc(tmp_path: Path) -> None:
    client, _, fake, _, sink = service(tmp_path, api=NoRunsc("docker", images=IMAGES))
    assert client.get("/health").json()["checks"] == {"runtime": "ok", "isolation": "runc"}
    warned = [r for r in sink.records() if r["event"] == "runtime.detected"]
    assert warned[0]["level"] == "warning" and "hardened runc" in warned[0]["sentence"]
    opened = client.post("/v1/sessions", json=open_body())
    assert opened.status_code == 201, opened.text
    session = opened.json()
    assert session["handle"]["spec"]["runtime"] == "runc"
    assert session["runtime_sentence"].startswith(
        "gVisor is not installed on this host, so the sandbox runs under hardened runc"
    )
    assert fake.bodies[session["handle"]["spec"]["name"]]["HostConfig"]["Runtime"] == "runc"

    prod, _, _, _, _ = service(
        tmp_path / "prod", api=NoRunsc("docker", images=IMAGES), profile="prod"
    )
    refused = prod.post("/v1/sessions", json=open_body())
    assert refused.status_code == 503
    assert refused.json()["what_happened"] == (
        "No sandbox can be opened: gVisor is not installed on this host."
    )


def test_sessions_open_list_get_exec_close(tmp_path: Path) -> None:
    client, _, fake, _, _ = service(tmp_path)
    opened = client.post("/v1/sessions", json=open_body())
    assert opened.status_code == 201, opened.text
    session = opened.json()
    assert session["user"] == "pat" and session["slug"] == "fan-ctl"
    assert session["handle"]["spec"]["image"] == PYTHON_IMAGE, "the first language picks the image"
    assert session["handle"]["spec"]["env"]["SLAS_LANGUAGE"] == "python"
    assert session["sentence"] == (
        "Sandbox for fan-ctl is open; it closes after 10 more idle minutes. The sandbox runs "
        "under gVisor. Toolchain: Python 3.12.6 and Rust 1.80.1. Rust 1.99 isn't in the offline "
        "toolchain bundle, so the newest bundled 1.80.1 is used instead."
    )
    project = tmp_path / "data" / "Coding" / "pat" / "Projects" / "fan-ctl"
    assert project.is_dir(), "Projects/<slug> is prepared"
    gitconfig = (tmp_path / "data" / "Coding" / "pat" / "gitconfig").read_text(encoding="utf-8")
    assert "\tname = Pat Lin\n\temail = pat@slas.local\n" in gitconfig
    name = session["handle"]["spec"]["name"]
    assert fake.bodies[name]["HostConfig"]["Binds"][0] == (
        f"/AI/Agent/Coding/pat/Projects/fan-ctl:{WORKSPACE}:rw"
    ), "mounted by its host path"

    listed = client.get("/v1/sessions", params={"user": "pat", "slug": "fan-ctl"}).json()
    assert [s["id"] for s in listed] == [session["id"]] and listed[0]["alive"] is True
    assert client.get("/v1/sessions", params={"user": "lee"}).json() == []
    assert client.get("/v1/sessions", params={"slug": "other"}).json() == []
    assert len(client.get("/v1/sessions").json()) == 1
    fetched = client.get(f"/v1/sessions/{session['id']}")
    assert fetched.status_code == 200 and fetched.json()["alive"] is True
    missing = client.get("/v1/sessions/nope")
    assert missing.status_code == 404
    assert missing.json()["what_happened"] == "That sandbox is no longer open."

    fake.handle_exec(
        lambda _name, argv: ContainerExecResult(exit_code=0, stdout=" ".join(argv) + "\n")
    )
    ran = client.post(
        f"/v1/sessions/{session['id']}/exec", json={"argv": ["git", "status"], "timeout_s": 30}
    )
    assert ran.status_code == 200
    assert ran.json() == {
        "exit_code": 0,
        "stdout": "git status\n",
        "stderr": "",
        "timed_out": False,
    }
    assert fake.execs[-1] == (name, ["git", "status"])
    for bad in ("git status", [], ["git", 3], [""]):
        refused = client.post(f"/v1/sessions/{session['id']}/exec", json={"argv": bad})
        assert refused.status_code == 400, bad
        assert refused.json()["what_happened"] == "The command must be an argv list of strings."
    assert client.post("/v1/sessions/nope/exec", json={"argv": ["ls"]}).status_code == 404

    # Quota: two per person in this test; the third is a 409 in three parts.
    assert client.post("/v1/sessions", json=open_body(slug="second")).status_code == 201
    full = client.post("/v1/sessions", json=open_body(slug="third"))
    assert full.status_code == 409
    assert full.json()["what_happened"] == "You already have 2 sandboxes open, which is the limit."
    unknown = client.post("/v1/sessions", json=open_body(languages=[{"language": "cobol"}]))
    assert unknown.status_code == 400 and "cobol" in unknown.json()["what_happened"]
    assert client.post("/v1/sessions", json=open_body(languages=[])).status_code == 400

    closed = client.delete(f"/v1/sessions/{session['id']}")
    assert closed.status_code == 204 and name not in fake.specs
    assert client.get(f"/v1/sessions/{session['id']}").status_code == 404
    assert client.delete("/v1/sessions/nope").status_code == 204, "closing twice is fine"


def test_a_refusing_runtime_is_a_502_in_three_parts(tmp_path: Path) -> None:
    client, _, _, _, _ = service(tmp_path, api=FakeContainerApi("docker", images=["other:1"]))
    refused = client.post("/v1/sessions", json=open_body())
    assert refused.status_code == 502
    assert refused.json()["what_happened"].startswith("The container runtime refused to create")
    assert "no such image" in refused.json()["likely_cause"]


def test_terminal_needs_git_terminal_and_explains_push(tmp_path: Path) -> None:
    client, services, fake, _, _ = service(tmp_path)
    session = client.post("/v1/sessions", json=open_body()).json()
    url = f"/v1/sessions/{session['id']}/terminal"

    def shell(_name: str, argv: Sequence[str]) -> ContainerExecResult:
        line = argv[2]
        if line.startswith("git push"):
            return ContainerExecResult(exit_code=128, stderr="fatal: Could not resolve host")
        return ContainerExecResult(exit_code=0, stdout=f"ran: {line}\n")

    fake.handle_exec(shell)
    anonymous = client.post(url, json={"line": "ls"})
    assert anonymous.status_code == 401
    lacking = Identity("pat@slas.local", "Pat", frozenset({"git:pull"}))
    forbidden = client.post(url, json={"line": "ls"}, headers=lacking.headers())
    assert forbidden.status_code == 403
    assert forbidden.json()["what_happened"] == "Pat may not use the sandbox terminal."
    allowed = Identity("pat@slas.local", "Pat", frozenset({"git:terminal"}))
    first = client.post(url, json={"line": "git log --oneline"}, headers=allowed.headers())
    assert first.status_code == 200
    assert first.json()["n"] == 1 and first.json()["output"] == "ran: git log --oneline"
    assert fake.execs[-1] == (
        session["handle"]["spec"]["name"],
        ["bash", "-lc", "git log --oneline"],
    )
    push = client.post(url, json={"line": "git push origin main"}, headers=allowed.headers())
    assert push.json()["exit_code"] == 128
    assert push.json()["output"] == "fatal: Could not resolve host\n" + PUSH_EXPLANATION
    assert push.json()["n"] == 2, "one terminal per session keeps the numbering"
    blank = client.post(url, json={"line": "   "}, headers=allowed.headers())
    assert blank.status_code == 400 and blank.json()["what_happened"] == "There was nothing to run."
    record = tmp_path / "data" / "Tickets" / "T-coding-0001" / "terminal.jsonl"
    lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [line["n"] for line in lines] == [1, 2], "the transcript lands on the ticket"
    assert (
        client.post(
            "/v1/sessions/nope/terminal", json={"line": "ls"}, headers=allowed.headers()
        ).status_code
        == 404
    )
    client.delete(f"/v1/sessions/{session['id']}")
    assert session["id"] not in services.terminals and session["id"] not in services.tickets


def test_toolchain_routes(tmp_path: Path) -> None:
    client, _, _, _, _ = service(tmp_path, registry="registry.internal")
    listed = client.get("/v1/toolchains").json()
    assert listed["source"] == "<bundled default>"
    assert listed["manifest"]["python"] == ["3.11.10", "3.12.6"]
    resolved = client.post(
        "/v1/toolchains/resolve",
        json={"choices": [{"language": "Python", "version": "3.11"}, {"language": "go"}]},
    )
    assert resolved.status_code == 200
    assert resolved.json() == [
        {
            "language": "python",
            "label": "Python",
            "requested": "3.11",
            "version": "3.11.10",
            "honoured": True,
            "image": "registry.internal/slas/sandbox-python:3.11.10",
            "sentence": (
                "Python 3.11 pinned; the bundle has it as the newest 3.11.x, using 3.11.10."
            ),
        },
        {
            "language": "go",
            "label": "Go",
            "requested": None,
            "version": "1.23.1",
            "honoured": True,
            "image": "registry.internal/slas/sandbox-go:1.23.1",
            "sentence": "Go: no version pinned, so the newest bundled go 1.23.1 is used.",
        },
    ]
    unknown = client.post("/v1/toolchains/resolve", json={"choices": [{"language": "cobol"}]})
    assert unknown.status_code == 400
    assert unknown.json()["what_happened"] == "'cobol' is not a language the Coding Agent knows."
    detected = client.post(
        "/v1/languages/detect", json={"plan": "Port `fan_ctl.py` to Rust (`src/main.rs`)."}
    )
    assert detected.json() == {"languages": ["python", "rust"]}


def test_manifest_file_is_read_when_present(tmp_path: Path) -> None:
    manifest = tmp_path / "data" / "Toolchains" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"toolchains": {"go": ["1.23.1", "1.24.0"]}}), encoding="utf-8")
    client, services, _, _, sink = service(tmp_path)
    assert client.get("/v1/toolchains").json() == {
        "manifest": {"go": ["1.23.1", "1.24.0"]},
        "source": str(manifest),
    }
    assert services.probe_images() == [
        "local/slas/sandbox-go:1.24.0",
        "local/slas/sandbox-go:1.23.1",
    ]
    loaded = [r for r in sink.records() if r["event"] == "toolchains.loaded"]
    assert loaded[0]["languages"] == {"go": "1.24.0"} and loaded[0]["registry"] == "local"


def test_reap_route_and_reaper_thread(tmp_path: Path) -> None:
    client, services, fake, clock, sink = service(tmp_path)
    first = client.post("/v1/sessions", json=open_body()).json()
    assert client.post("/v1/reap", json={}).json() == {"closed": []}
    clock._now = START + timedelta(minutes=11)
    assert client.post("/v1/reap", json={}).json() == {"closed": [first["id"]]}
    assert first["handle"]["spec"]["name"] not in fake.specs
    assert [r for r in sink.records() if r["event"] == "sandbox.reaped"]

    second = client.post("/v1/sessions", json=open_body(slug="again")).json()
    reaper = Reaper(services, 0.01)
    assert reaper.run_once() == []
    clock._now = START + timedelta(minutes=30)
    reaper.start()
    reaper.start()  # idempotent
    deadline = time.monotonic() + 5
    while services.manager.sessions() and time.monotonic() < deadline:
        time.sleep(0.01)
    reaper.stop()
    assert services.manager.sessions() == [] and second["handle"]["spec"]["name"] not in fake.specs

    def boom() -> list[str]:
        raise RuntimeError("socket blinked")

    services.manager.reap = boom  # type: ignore[method-assign]
    broken = Reaper(services, 0.01)
    broken.start()
    deadline = time.monotonic() + 5
    while (
        not [r for r in sink.records() if r["event"] == "sandbox.reap_failed"]
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    broken.stop()
    failed = [r for r in sink.records() if r["event"] == "sandbox.reap_failed"]
    assert failed and failed[0]["error_type"] == "RuntimeError"


def test_create_app_with_reaper_starts_and_stops_it_with_the_app(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, reap_interval_s=1)
    services = build_services(
        settings,
        api=FakeContainerApi("docker", images=IMAGES),
        clock=FakeClock(START, step=timedelta(0)),
        log=EventLog("sandbox-manager", ListSink()),
    )
    app = create_app(settings, services=services, reaper=True)
    reaper: Reaper = app.state.reaper
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert reaper._thread is not None and reaper._thread.is_alive()
    assert reaper._thread is None, "stopped with the app"


def test_route_table_matches_the_contract() -> None:
    documented = {
        ("GET", "/health"),
        ("GET", "/metrics"),
        ("POST", "/v1/sessions"),
        ("GET", "/v1/sessions"),
        ("GET", "/v1/sessions/{session_id}"),
        ("POST", "/v1/sessions/{session_id}/exec"),
        ("DELETE", "/v1/sessions/{session_id}"),
        ("POST", "/v1/sessions/{session_id}/terminal"),
        ("GET", "/v1/toolchains"),
        ("POST", "/v1/toolchains/resolve"),
        ("POST", "/v1/languages/detect"),
        ("POST", "/v1/reap"),
    }
    assert set(route_table()) == documented
    assert route_table() == sorted(route_table())


def test_no_docs_page_and_unknown_routes_are_three_parts(tmp_path: Path) -> None:
    client, _, _, _, _ = service(tmp_path)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert set(client.get("/nowhere").json()) == {*THREE_PARTS, "trace_id"}


# --- settings and cli -----------------------------------------------------------------------------


def test_settings_from_env_defaults_overrides_and_refusals() -> None:
    defaults = Settings.from_env({})
    assert defaults.runtime_socket == "/run/podman/podman.sock"
    assert defaults.data_root == Path("/data") and defaults.host_data_root == Path("/AI/Agent")
    assert defaults.default_runtime == "runsc" and defaults.tier == "gvisor"
    assert defaults.registry == "local" and defaults.profile == "quickstart"
    assert defaults.toolchain_manifest == Path("/data/Toolchains/manifest.json")
    assert defaults.bind == "0.0.0.0:8000" and defaults.reap_interval_s == 60
    assert defaults.pids_limit == 512 and defaults.cpus == 2.0 and defaults.memory == "4g"

    custom = Settings.from_env(
        {
            "SLAS_RUNTIME_SOCKET": "/var/run/docker.sock",
            "SLAS_DATA_ROOT": "/srv/slas",
            "SLAS_HOST_DATA_ROOT": "/AI/Agent",
            "DEFAULT_RUNTIME": "runc",
            "SANDBOX_TIER": "kata",
            "SLAS_SANDBOX_REGISTRY": "registry.internal/",
            "SLAS_PROFILE": "prod",
            "SLAS_BIND": "0.0.0.0:9000",
            "SLAS_MAX_SESSIONS_PER_USER": "5",
            "SLAS_SANDBOX_TTL_S": "120",
            "PIDS_LIMIT": "1024",
            "SLAS_SANDBOX_CPUS": "4",
            "SLAS_SANDBOX_MEMORY": "8g",
            "SLAS_REAP_INTERVAL_S": "30",
            "SLAS_SECCOMP_PROFILE": "/etc/slas/custom.json",
        }
    )
    assert custom.runtime_socket == "/var/run/docker.sock"
    assert custom.toolchain_manifest == Path("/srv/slas/Toolchains/manifest.json")
    assert custom.default_runtime == "runc" and custom.tier == "kata" and custom.profile == "prod"
    assert custom.registry == "registry.internal" and custom.bind == "0.0.0.0:9000"
    assert custom.max_sessions_per_user == 5 and custom.default_ttl_s == 120
    assert custom.pids_limit == 1024 and custom.cpus == 4.0 and custom.memory == "8g"
    assert custom.reap_interval_s == 30 and custom.seccomp_profile == Path("/etc/slas/custom.json")
    assert Settings.from_env({"SLAS_TOOLCHAIN_MANIFEST": "/x/m.json"}).toolchain_manifest == Path(
        "/x/m.json"
    )
    for env, fragment in (
        ({"DEFAULT_RUNTIME": "docker"}, "DEFAULT_RUNTIME is 'docker'; it must be one of"),
        ({"SLAS_MAX_SESSIONS_PER_USER": "many"}, "must be a whole number"),
        ({"SLAS_SANDBOX_TTL_S": "5"}, "must be at least 60"),
        ({"SLAS_SANDBOX_CPUS": "two"}, "SLAS_SANDBOX_CPUS is 'two'"),
    ):
        with pytest.raises(SettingsError, match=fragment):
            Settings.from_env(env)


def test_cli_serve_probes_then_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[FastAPI, str]] = []
    fake = FakeContainerApi("docker", images=IMAGES)
    monkeypatch.setattr("slas_sandbox_manager.service.app.ContainerApi", lambda _socket: fake)
    out, err = io.StringIO(), io.StringIO()
    env = {
        "SLAS_DATA_ROOT": str(tmp_path),
        "SLAS_HOST_DATA_ROOT": "/AI/Agent",
        "SLAS_BIND": "0.0.0.0:8000",
    }
    code = cli_main(
        ["serve"],
        env=env,
        stdout=out,
        stderr=err,
        server=lambda app, bind: served.append((app, bind)),
    )
    assert code == 0 and len(served) == 1
    app, bind = served[0]
    assert bind == "0.0.0.0:8000" and isinstance(app, FastAPI)
    assert app.state.reaper is not None
    text = out.getvalue()
    assert text.startswith("The runtime socket is served by Docker (API 1.47). gVisor (runsc) is")
    assert (
        "sandbox images tagged local/slas/sandbox-<language>:<version>. Serving on 0.0.0.0:8000."
        in text
    )

    fake.down = True
    out = io.StringIO()
    assert cli_main(["serve"], env=env, stdout=out, stderr=err, server=lambda a, b: None) == 0
    assert "The service starts anyway and reports it on /health." in out.getvalue()

    bad = io.StringIO()
    assert cli_main(["serve"], env={"DEFAULT_RUNTIME": "nope"}, stdout=out, stderr=bad) == 2
    assert bad.getvalue().startswith("DEFAULT_RUNTIME is 'nope'")
    with pytest.raises(SystemExit):
        cli_main([], stdout=out, stderr=bad)
    scripts = (REPO_ROOT / "services" / "sandbox-manager" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'slas-sandbox-manager = "slas_sandbox_manager.cli:main"' in scripts


# --- the images CLI -------------------------------------------------------------------------------


EXPECTED_LIST = [
    "sandbox-python-3.11.10\tlocal/slas/sandbox-python:3.11.10\timages/sandbox-python/Dockerfile.3.11.10",
    "sandbox-python\tlocal/slas/sandbox-python:3.12.6\timages/sandbox-python/Dockerfile",
    "sandbox-c\tlocal/slas/sandbox-c:13.2.0\timages/sandbox-c/Dockerfile",
    "sandbox-cpp\tlocal/slas/sandbox-cpp:13.2.0\timages/sandbox-cpp/Dockerfile",
    "sandbox-rust\tlocal/slas/sandbox-rust:1.80.1\timages/sandbox-rust/Dockerfile",
    "sandbox-shell\tlocal/slas/sandbox-shell:5.2.21\timages/sandbox-shell/Dockerfile",
    "sandbox-go\tlocal/slas/sandbox-go:1.23.1\timages/sandbox-go/Dockerfile",
    "sandbox-typescript\tlocal/slas/sandbox-typescript:5.9.3\timages/sandbox-typescript/Dockerfile",
    "sandbox-config\tlocal/slas/sandbox-config:1.35.1\timages/sandbox-config/Dockerfile",
]


def test_images_cli_list_manifest_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLAS_SANDBOX_REGISTRY", raising=False)
    out = io.StringIO()
    assert images.main(["list"], stdout=out) == 0
    assert out.getvalue().splitlines() == EXPECTED_LIST
    for line in EXPECTED_LIST:
        name, tag, dockerfile = line.split("\t")
        assert (REPO_ROOT / dockerfile).is_file(), dockerfile
        assert f"-t {tag} ." in (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        assert tag.startswith("local/slas/sandbox-") and name.startswith("sandbox-")
    out = io.StringIO()
    images.main(["list", "--registry", "registry.internal"], stdout=out)
    assert out.getvalue().splitlines()[1] == (
        "sandbox-python\tregistry.internal/slas/sandbox-python:3.12.6\timages/sandbox-python/Dockerfile"
    )
    monkeypatch.setenv("SLAS_SANDBOX_REGISTRY", "harbor.internal")
    out = io.StringIO()
    images.main(["list"], stdout=out)
    assert out.getvalue().startswith(
        "sandbox-python-3.11.10\tharbor.internal/slas/sandbox-python:3.11.10\t"
    )
    assert image_for("python", "3.12.6") == "harbor.internal/slas/sandbox-python:3.12.6"
    monkeypatch.delenv("SLAS_SANDBOX_REGISTRY")

    target = tmp_path / "Toolchains" / "manifest.json"
    out = io.StringIO()
    assert images.main(["manifest", "--out", str(target)], stdout=out) == 0
    assert out.getvalue() == (
        f"Wrote the toolchain manifest to {target}: Python 3.11.10/3.12.6, C 13.2.0, C++ 13.2.0, "
        "Rust 1.80.1, Shell 5.2.21, Go 1.23.1, TypeScript 5.9.3, YAML/JSON config 1.35.1.\n"
    )
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == default_manifest().to_mapping()
    assert written["toolchains"] == {
        "python": ["3.11.10", "3.12.6"],
        "c": ["13.2.0"],
        "cpp": ["13.2.0"],
        "rust": ["1.80.1"],
        "shell": ["5.2.21"],
        "go": ["1.23.1"],
        "typescript": ["5.9.3"],
        "config": ["1.35.1"],
    }

    out = io.StringIO()
    assert images.main(["render", "--root", str(tmp_path)], stdout=out) == 0
    assert out.getvalue().count("wrote ") == len(EXPECTED_LIST)
    for rel, content in images.image_files().items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == content
    images.main(["render", "--root", str(tmp_path / "bundle"), "--source", "bundle"], stdout=out)
    assert "COPY toolchains/go/1.23.1/" in (
        tmp_path / "bundle" / "images" / "sandbox-go" / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert images.build_parser().prog == "python -m slas_sandbox_manager.images"

    for base in images.BASES:
        assert base.reference.startswith("docker.io/library/") and "@sha256:" in base.reference
        assert base.how.startswith("HEAD registry-1.docker.io/v2/library/")
        readme = (REPO_ROOT / "images" / "README.md").read_text(encoding="utf-8")
        assert base.tag in readme and base.digest in readme, (
            f"images/README.md must list {base.tag}"
        )


def test_images_module_is_standard_library_only(tmp_path: Path) -> None:
    """install.sh runs `python -m slas_sandbox_manager.images` on the bare host."""
    pythonpath = os.pathsep.join(
        str(REPO_ROOT / part) for part in ("services/sandbox-manager", "packages/slas-schemas")
    )
    result = subprocess.run(
        [sys.executable, "-S", "-m", "slas_sandbox_manager.images", "list"],
        env={"PYTHONPATH": pythonpath, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == EXPECTED_LIST
