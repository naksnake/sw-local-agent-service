"""slas_container: the Engine API client against a scripted transport, the spec bodies, the fake."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from slas_container import (
    ContainerApi,
    ContainerError,
    CreateSpec,
    ExecResult,
    FakeContainerApi,
    Mount,
)
from slas_container.api import demultiplex


def frame(kind: int, payload: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


class Engine:
    """A scripted Docker/Podman compat API."""

    def __init__(self, *, podman: bool = False) -> None:
        self.podman = podman
        self.calls: list[tuple[str, str, Any]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.exec_exit = 0
        self.slow_exec = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/info":
            if self.podman:
                return httpx.Response(200, json={"DefaultRuntime": "crun"})
            return httpx.Response(200, json={"Runtimes": {"runc": {}, "runsc": {}, "nvidia": {}}})
        if path == "/_ping":
            headers = {"Api-Version": "1.47"}
            if self.podman:
                headers["Libpod-API-Version"] = "5.2.0"
            return httpx.Response(200, text="OK", headers=headers)
        if path == "/containers/json":
            items = [
                {
                    "Id": f"id-{name}",
                    "Names": [f"/{name}"],
                    "Image": spec["Image"],
                    "State": "running",
                    "Labels": spec.get("Labels", {}),
                    "NetworkSettings": {
                        "Networks": {"slas_slas-inference": {"IPAddress": "10.9.0.5"}}
                    },
                }
                for name, spec in self.containers.items()
            ]
            return httpx.Response(200, json=items)
        if path == "/containers/create":
            name = request.url.params["name"]
            if name in self.containers:
                return httpx.Response(409, json={"message": f"name {name} is in use"})
            self.containers[name] = dict(body or {})
            return httpx.Response(201, json={"Id": f"id-{name}", "Warnings": []})
        if path.startswith("/containers/") and path.endswith("/json"):
            name = path.split("/")[2]
            if name not in self.containers:
                return httpx.Response(404, json={"message": "no such container"})
            return httpx.Response(
                200,
                json={
                    "Id": f"id-{name}",
                    "Name": f"/{name}",
                    "Config": {"Image": self.containers[name]["Image"], "Labels": {"slas": "1"}},
                    "State": {"Status": "running", "ExitCode": 0, "Health": {"Status": "healthy"}},
                    "NetworkSettings": {
                        "Networks": {"slas_slas-inference": {"IPAddress": "10.9.0.5"}}
                    },
                },
            )
        if path.endswith("/start") and path.startswith("/containers/"):
            return httpx.Response(204)
        if path.endswith("/stop"):
            return httpx.Response(304)
        if request.method == "DELETE" and path.startswith("/containers/"):
            return httpx.Response(204)
        if path.endswith("/logs"):
            return httpx.Response(200, content=frame(1, b"INFO ready\n") + frame(2, b"WARN x\n"))
        if path.startswith("/images/"):
            return httpx.Response(200 if "present" in path else 404, json={})
        if path.endswith("/exec") and path.startswith("/containers/"):
            return httpx.Response(201, json={"Id": "exec-1"})
        if path == "/exec/exec-1/start":
            if self.slow_exec:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(
                200, content=frame(1, b"hello ") + frame(1, b"world\n") + frame(2, b"warn\n")
            )
        if path == "/exec/exec-1/json":
            return httpx.Response(200, json={"ExitCode": self.exec_exit, "Running": False})
        return httpx.Response(500, text="unexpected path " + path)


def vllm_spec() -> CreateSpec:
    return CreateSpec(
        name="vllm-coder",
        image="registry.internal/vllm/vllm-openai:v0.29.0",
        argv=["--model", "/data/Models/x"],
        env={"VLLM_NO_USAGE_STATS": "1"},
        network="slas_slas-inference",
        network_aliases=["vllm-coder"],
        mounts=[Mount(source="/AI/Agent/Models", target="/data/Models", read_only=True)],
        shm_size_bytes=16 * 1024**3,
        ipc_host=True,
        gpu_ids=[0, 1],
        labels={"slas.role": "coder"},
        restart="unless-stopped",
    )


def test_spec_body_docker_and_podman_gpu_syntax() -> None:
    docker = vllm_spec().to_body("docker")
    host = docker["HostConfig"]
    assert host["DeviceRequests"] == [
        {"Driver": "nvidia", "DeviceIDs": ["0", "1"], "Capabilities": [["gpu"]]}
    ]
    assert host["Binds"] == ["/AI/Agent/Models:/data/Models:ro"]
    assert host["IpcMode"] == "host" and host["ShmSize"] == 16 * 1024**3
    assert host["CapDrop"] == ["ALL"] and "no-new-privileges" in host["SecurityOpt"]
    assert host["NetworkMode"] == "slas_slas-inference"
    assert docker["NetworkingConfig"]["EndpointsConfig"]["slas_slas-inference"]["Aliases"] == [
        "vllm-coder"
    ]
    assert docker["Env"] == ["VLLM_NO_USAGE_STATS=1"]
    podman = vllm_spec().to_body("podman")["HostConfig"]
    assert "DeviceRequests" not in podman
    assert [d["PathOnHost"] for d in podman["Devices"]] == ["nvidia.com/gpu=0", "nvidia.com/gpu=1"]


def test_spec_sandbox_body_and_refusals() -> None:
    spec = CreateSpec(
        name="slas-sbx-1",
        image="sandbox-python:3.12.6",
        argv=["sleep", "infinity"],
        runtime="runsc",
        user="10001:10001",
        workdir="/workspace",
        read_only_rootfs=True,
        tmpfs={"/tmp": "rw,nosuid,size=512m"},  # noqa: S108
        pids_limit=512,
        memory_bytes=4 * 1024**3,
        nano_cpus=2_000_000_000,
        entrypoint=["/bin/sleep"],
    )
    body = spec.to_body("docker")
    assert body["HostConfig"]["NetworkMode"] == "none"
    assert body["HostConfig"]["Runtime"] == "runsc"
    assert body["HostConfig"]["ReadonlyRootfs"] is True
    assert body["HostConfig"]["Tmpfs"] == {"/tmp": "rw,nosuid,size=512m"}  # noqa: S108
    assert body["HostConfig"]["PidsLimit"] == 512
    assert body["User"] == "10001:10001" and body["WorkingDir"] == "/workspace"
    assert body["Entrypoint"] == ["/bin/sleep"]
    assert "NetworkingConfig" not in body
    with pytest.raises(ValueError, match="identifiers"):
        CreateSpec(name="x", image="i", env={"BAD-KEY": "1"})
    with pytest.raises(ValueError):
        CreateSpec(name="/bad", image="i")


def test_demultiplex_frames_and_raw_streams() -> None:
    assert demultiplex(frame(1, b"out") + frame(2, b"err") + frame(1, b"!")) == (b"out!", b"err")
    assert demultiplex(b"plain text, no frames") == (b"plain text, no frames", b"")
    assert demultiplex(b"") == (b"", b"")


def test_api_lifecycle_over_the_scripted_engine() -> None:
    engine = Engine()
    api = ContainerApi("/run/test.sock", transport=engine.transport())
    info = api.ping()
    assert info.engine == "docker" and info.api_version == "1.47"
    assert info.sentence() == "The runtime socket is served by Docker (API 1.47)."
    assert api.runtimes() == ["nvidia", "runc", "runsc"]
    assert api.inspect("vllm-coder") is None
    assert api.create(vllm_spec()) == "id-vllm-coder"
    created = next(c for c in engine.calls if c[1] == "/containers/create")
    assert created[2]["HostConfig"]["DeviceRequests"][0]["Driver"] == "nvidia"
    api.start("vllm-coder")
    inspected = api.inspect("vllm-coder")
    assert inspected is not None and inspected.running and inspected.health == "healthy"
    assert inspected.ip_addresses == {"slas_slas-inference": "10.9.0.5"}
    listed = api.list(label="slas.role")
    assert [c.name for c in listed] == ["vllm-coder"]
    assert listed[0].labels == {"slas.role": "coder"}
    assert [c.name for c in api.running_with_label("slas.role=coder")] == ["vllm-coder"]
    assert api.logs("vllm-coder") == "INFO ready\nWARN x\n"
    assert api.image_present("registry/present:1") is True
    assert api.image_present("registry/absent:1") is False
    result = api.exec("vllm-coder", ["echo", "hi"], cwd="/workspace", user="10001", env={"A": "1"})
    assert result == ExecResult(exit_code=0, stdout="hello world\n", stderr="warn\n")
    prepared = next(c for c in engine.calls if c[1] == "/containers/vllm-coder/exec")[2]
    assert prepared["Cmd"] == ["echo", "hi"] and prepared["WorkingDir"] == "/workspace"
    assert prepared["User"] == "10001" and prepared["Env"] == ["A=1"] and prepared["Tty"] is False
    engine.exec_exit = 3
    assert api.exec("vllm-coder", ["false"]).exit_code == 3
    api.stop("vllm-coder")
    api.remove("vllm-coder")
    with pytest.raises(ValueError):
        api.exec("vllm-coder", [])
    api.close()


def test_api_refusals_are_three_parts() -> None:
    engine = Engine(podman=True)
    api = ContainerApi(transport=engine.transport())
    assert api.engine == "podman"
    assert api.runtimes() == ["crun"]
    api.create(vllm_spec())
    with pytest.raises(ContainerError) as conflict:
        api.create(vllm_spec())
    assert conflict.value.status == 409
    assert conflict.value.message.what_happened.startswith(
        "The container runtime refused to create vllm-coder"
    )
    assert "name vllm-coder is in use" in conflict.value.message.likely_cause
    engine.slow_exec = True
    api.start("vllm-coder")
    timed_out = api.exec("vllm-coder", ["sleep", "9"], timeout_s=0.5)
    assert timed_out.timed_out and timed_out.exit_code == 124 and not timed_out.ok

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no such file")

    dead = ContainerApi("/run/podman/podman.sock", transport=httpx.MockTransport(down))
    with pytest.raises(ContainerError) as unreachable:
        dead.ping()
    assert unreachable.value.message.what_happened == (
        "The container runtime socket /run/podman/podman.sock did not answer."
    )
    assert "SLAS_RUNTIME_SOCKET" in unreachable.value.message.what_to_do

    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    hung = ContainerApi(transport=httpx.MockTransport(slow))
    with pytest.raises(ContainerError) as timeout:
        hung.list()
    assert (
        timeout.value.message.what_happened
        == "The container runtime took too long to list containers."
    )


def test_fake_mirrors_the_surface() -> None:
    fake = FakeContainerApi("podman", images=["img:1"], runtimes=["crun", "runsc"])
    assert fake.ping().engine == "podman" and fake.engine == "podman"
    assert fake.runtimes() == ["crun", "runsc"]
    spec = CreateSpec(name="c1", image="img:1", labels={"slas.kind": "sandbox"}, network="net")
    fake.create(spec)
    assert fake.bodies["c1"]["HostConfig"]["NetworkMode"] == "net"
    with pytest.raises(ContainerError) as conflict:
        fake.create(spec)
    assert conflict.value.status == 409
    with pytest.raises(ContainerError) as missing:
        fake.create(CreateSpec(name="c2", image="nope:1"))
    assert missing.value.status == 404
    with pytest.raises(ContainerError):
        fake.start("ghost")
    with pytest.raises(ContainerError) as not_running:
        fake.exec("c1", ["ls"])
    assert not_running.value.status == 409
    fake.start("c1")
    fake.handle_exec(lambda name, argv: ExecResult(exit_code=7, stderr=f"{name}: {argv[0]} failed"))
    assert fake.exec("c1", ["make"]).stderr == "c1: make failed"
    assert fake.execs == [("c1", ["make"])]
    with pytest.raises(ValueError):
        fake.exec("c1", [])
    fake.set_health("c1", "healthy")
    info = fake.inspect("c1")
    assert (
        info is not None and info.health == "healthy" and info.ip_addresses == {"net": "10.0.0.2"}
    )
    assert [c.name for c in fake.list(label="slas.kind=sandbox")] == ["c1"]
    assert fake.list(label="slas.kind=other") == []
    assert [c.name for c in fake.running_with_label("slas.kind")] == ["c1"]
    assert fake.image_present("img:1") and not fake.image_present("other")
    fake.crash("c1")
    assert fake.logs("c1") == "exited with 1"
    assert fake.list(all_states=False) == []
    fake.stop("c1")
    fake.remove("c1")
    assert fake.inspect("c1") is None
    fake.down = True
    with pytest.raises(ContainerError):
        fake.list()
    fake.close()
