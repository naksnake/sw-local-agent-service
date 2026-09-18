"""The model manager as a service: driver, placement, controller, routes, smoke tester, CLI.

Everything runs against `FakeContainerApi`, a scripted gateway (httpx.MockTransport), a fake
health prober and `FakeSmokeTester` (CLAUDE.md §11: no live model, no real runtime).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slas_container import ContainerError, CreateSpec, FakeContainerApi
from slas_container.spec import Engine
from slas_http.client import ServiceClient
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_model_manager import cli, placement
from slas_model_manager.controller import Controller
from slas_model_manager.driver import (
    ContainerApiRuntime,
    HttpProber,
    PatientRuntime,
    instance_url,
)
from slas_model_manager.registry import (
    PROFILE_REGISTRIES,
    RegistryError,
    profile_registry,
    render_registry_yaml,
)
from slas_model_manager.runtime import ContainerRef, task_for_role, vllm_spec
from slas_model_manager.service.app import create_app, route_table
from slas_model_manager.service.settings import Settings, parse_gpu_ids, parse_size
from slas_model_manager.smoke import HttpSmokeTester
from slas_model_manager.swap import FakeSmokeTester, SmokeResult
from slas_observability.events import EventLog, ListSink
from slas_schemas.errors import ThreePartMessage

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "registry.internal/vllm/vllm-openai:v0.29.0-x86_64-cu129"
HOST_MODELS = "/AI/Agent/Models"
NETWORK = "slas_slas-inference"
START = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
QUICKSTART = profile_registry("quickstart")
MANAGER = Identity("pat@slas.local", "Pat", frozenset({"model:manage"}))
VIEWER = Identity("sam@slas.local", "Sam", frozenset({"models:view"}))
THREE_PARTS = {"what_happened", "likely_cause", "what_to_do", "trace_id"}


# --- fakes ------------------------------------------------------------------------------------


class FakeProber:
    """Healthy for the instance names in `healthy`; records every URL asked."""

    def __init__(self, *, everything: bool = False) -> None:
        self.healthy: set[str] = set()
        self.everything = everything
        self.asked: list[str] = []

    def __call__(self, url: str) -> bool:
        self.asked.append(url)
        name = url.removeprefix("http://").split(":")[0]
        return self.everything or name in self.healthy


class FakeGateway:
    """Records every `PUT /v1/instances`; can be taken down."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.down = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        assert request.method == "PUT" and request.url.path == "/v1/instances"
        body = json.loads(request.content)
        self.bodies.append(body)
        return httpx.Response(200, json={"roles": body["routes"]["roles"]})


class BlockingSmoke:
    """A smoke test that waits until the test lets it finish."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def smoke(self, instance: str) -> SmokeResult:
        self.entered.set()
        self.release.wait(5)
        return SmokeResult(ok=True, sentence=f"Smoke test passed: {instance} answered.")


class ExitCodeFake(FakeContainerApi):
    """`FakeContainerApi` whose `inspect` reports a scripted exit code."""

    def __init__(self, engine: Engine = "docker") -> None:
        super().__init__(engine)
        self.exit_codes: dict[str, int] = {}

    def inspect(self, name: str) -> Any:
        info = super().inspect(name)
        if info is not None and name in self.exit_codes:
            return info.model_copy(update={"exit_code": self.exit_codes[name]})
        return info


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        engine: Engine = "docker",
        api: FakeContainerApi | None = None,
        gpu_ids: tuple[int, ...] = (0, 1, 2, 3),
        gpu_vram_gib: float = 280.0,
        image: str = IMAGE,
        registry: dict[str, object] | None = None,
        smoke: Any = None,
        coder_first: bool = False,
    ) -> None:
        self.models_file = tmp_path / "Models" / "models.yaml"
        self.models_file.parent.mkdir(parents=True, exist_ok=True)
        data = registry if registry is not None else PROFILE_REGISTRIES["quickstart"]
        self.models_file.write_text(render_registry_yaml(data), encoding="utf-8")
        self.settings = Settings(
            models_file=self.models_file,
            gpu_ids=gpu_ids,
            gpu_vram_gib=gpu_vram_gib,
            vllm_image=image,
            host_models_dir=HOST_MODELS,
            inference_network=NETWORK,
            start_coder_first=coder_first,
        )
        self.api = api if api is not None else FakeContainerApi(engine)
        self.prober = FakeProber()
        self.gateway = FakeGateway()
        self.smoke = smoke if smoke is not None else FakeSmokeTester()
        self.sink = ListSink()
        self.clock = FakeClock(START, step=timedelta(seconds=0))
        self.app: FastAPI = create_app(
            self.settings,
            api=self.api,
            prober=self.prober,
            smoke=self.smoke,
            gateway=_gateway_client(self.gateway),
            log=EventLog("model-manager", self.sink),
            clock=self.clock,
            sleep=lambda _s: None,
        )
        self.controller: Controller = self.app.state.controller
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.sink.records() if r["event"] == name]

    def status(self) -> dict[str, Any]:
        response = self.client.get("/v1/status")
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    def rows(self) -> dict[str, dict[str, Any]]:
        return {row["name"]: row for row in self.status()["instances"]}

    def wait_swap(self) -> None:
        thread = self.controller._swap_thread  # the test controls the thread
        assert thread is not None
        thread.join(5)
        assert not thread.is_alive()


def _gateway_client(gateway: FakeGateway) -> ServiceClient:
    return ServiceClient("llm-gateway", "http://llm-gateway:8000", transport=gateway.transport())


def assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == THREE_PARTS
    return body


# --- driver -----------------------------------------------------------------------------------


def coder_spec(name: str = "vllm-coder", gpu_ids: list[int] | None = None) -> Any:
    return vllm_spec(
        QUICKSTART.model("qwen3.8-27b-fp8"), name=name, gpu_ids=gpu_ids or [2, 3], image=IMAGE
    )


def test_driver_translates_the_spec_per_contract() -> None:
    api = FakeContainerApi("docker")
    prober = FakeProber()
    runtime = ContainerApiRuntime(
        api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=prober
    )
    spec = coder_spec()
    create = runtime.create_spec(spec)
    assert create.name == "vllm-coder" and create.network_aliases == ["vllm-coder"]
    assert create.network == NETWORK and create.ipc_host and create.restart == "unless-stopped"
    assert create.shm_size_bytes == 16 * 1024**3
    assert [m.bind() for m in create.mounts] == [f"{HOST_MODELS}:/data/Models:ro"]
    assert create.gpu_ids == [2, 3], "the device request selects the real GPUs"
    assert create.env["CUDA_VISIBLE_DEVICES"] == "0,1", "renumbered inside the container"
    assert create.env["VLLM_NO_USAGE_STATS"] == "1" and create.env["HF_HUB_OFFLINE"] == "1"
    assert create.labels["slas.kind"] == "vllm"
    assert create.labels["slas.instance"] == "vllm-coder"
    assert create.labels["slas.model"] == "qwen3.8-27b-fp8"
    assert create.labels["slas.gpu_ids"] == "2,3"
    assert json.loads(create.labels["slas.argv"]) == spec.argv

    ref = runtime.start(spec)
    assert ref.name == "vllm-coder" and api.states["vllm-coder"] == "running"
    body = api.bodies["vllm-coder"]
    assert body["HostConfig"]["DeviceRequests"] == [
        {"Driver": "nvidia", "DeviceIDs": ["2", "3"], "Capabilities": [["gpu"]]}
    ]
    assert body["HostConfig"]["Binds"] == [f"{HOST_MODELS}:/data/Models:ro"]
    assert body["HostConfig"]["IpcMode"] == "host" and body["HostConfig"]["ShmSize"] == 16 * 1024**3
    assert body["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    assert body["NetworkingConfig"]["EndpointsConfig"][NETWORK]["Aliases"] == ["vllm-coder"]
    assert "CUDA_VISIBLE_DEVICES=0,1" in body["Env"]

    assert not runtime.is_healthy(ref), "running but the health check does not answer yet"
    assert prober.asked == ["http://vllm-coder:8000"]
    prober.healthy.add("vllm-coder")
    assert runtime.is_healthy(ref)
    api.crash("vllm-coder")
    assert not runtime.is_healthy(ref), "a crashed container is never healthy"
    api.logs_text["vllm-coder"] = "\n".join(f"line {i}" for i in range(50)) + "\n\n"
    assert runtime.last_log_lines("vllm-coder") == [f"line {i}" for i in range(10, 50)]
    assert runtime.last_log_lines("vllm-coder", 3) == ["line 47", "line 48", "line 49"]
    state = runtime.state_of("vllm-coder")
    assert state is not None and state.state == "exited"

    runtime.stop(ref)
    assert api.inspect("vllm-coder") is None, "stop removes the container so the name is free"
    assert runtime.state_of("vllm-coder") is None


def test_driver_uses_cdi_devices_on_podman_and_maps_gpu_ids() -> None:
    api = FakeContainerApi("podman")
    runtime = ContainerApiRuntime(
        api,
        image=IMAGE,
        host_models_dir=HOST_MODELS,
        network=NETWORK,
        gpu_ids_to_devices=lambda ids: [i + 4 for i in ids],
        prober=FakeProber(),
    )
    runtime.start(coder_spec(gpu_ids=[0, 1]))
    host = api.bodies["vllm-coder"]["HostConfig"]
    assert "DeviceRequests" not in host
    assert [d["PathOnHost"] for d in host["Devices"]] == ["nvidia.com/gpu=4", "nvidia.com/gpu=5"]
    assert "CUDA_VISIBLE_DEVICES=0,1" in api.bodies["vllm-coder"]["Env"]


def test_driver_replaces_a_leftover_container_and_rebuilds_refs_from_labels() -> None:
    api = FakeContainerApi("docker")
    leftover = CreateSpec(name="vllm-coder", image="old:1", labels={"slas.kind": "vllm"})
    api.create(leftover)
    api.create(CreateSpec(name="not-ours", image="x:1", labels={"slas.kind": "sandbox"}))
    api.create(CreateSpec(name="vllm-broken", image="x:1", labels={"slas.kind": "vllm"}))
    runtime = ContainerApiRuntime(
        api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=FakeProber()
    )
    spec = coder_spec()
    ref = runtime.start(spec)
    assert api.specs["vllm-coder"].image == IMAGE, "the leftover was removed and recreated"

    fresh = ContainerApiRuntime(
        api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=FakeProber()
    )
    running = fresh.running()
    assert [r.name for r in running] == ["vllm-coder"], "labels without a model are skipped"
    rebuilt = running[0]
    assert rebuilt.spec.model_id == spec.model_id and rebuilt.spec.gpu_ids == [2, 3]
    assert rebuilt.spec.argv == spec.argv and rebuilt.spec.image == IMAGE
    assert rebuilt.id == ref.id

    # An image reported by id (no tag) falls back to the configured image; bad labels skip.
    api.specs["vllm-coder"] = api.specs["vllm-coder"].model_copy(update={"image": "sha256abc"})
    fresh2 = ContainerApiRuntime(
        api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=FakeProber()
    )
    assert fresh2.running()[0].spec.image == IMAGE
    api.specs["vllm-coder"] = api.specs["vllm-coder"].model_copy(
        update={"labels": {**api.specs["vllm-coder"].labels, "slas.gpu_ids": "a,b"}}
    )
    assert (
        ContainerApiRuntime(
            api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=FakeProber()
        ).running()
        == []
    )
    api.specs["vllm-coder"] = api.specs["vllm-coder"].model_copy(
        update={
            "labels": {**api.specs["vllm-coder"].labels, "slas.gpu_ids": "0", "slas.argv": "[]"}
        }
    )
    assert (
        ContainerApiRuntime(
            api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=FakeProber()
        ).running()
        == []
    )


def test_driver_raises_other_runtime_refusals() -> None:
    api = FakeContainerApi("docker", images=["other:1"])
    runtime = ContainerApiRuntime(
        api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=FakeProber()
    )
    with pytest.raises(ContainerError) as raised:
        runtime.start(coder_spec())
    assert raised.value.status == 404
    api.down = True
    assert runtime.last_log_lines("vllm-coder") == []


def test_http_prober_is_200_or_nothing() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "vllm-coder":
            return httpx.Response(200, json={"ok": True})
        if request.url.host == "vllm-slow":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(503)

    prober = HttpProber(transport=httpx.MockTransport(handle))
    assert prober(instance_url("vllm-coder"))
    assert not prober(instance_url("vllm-planner"))
    assert not prober(instance_url("vllm-slow"))
    prober.close()


def test_patient_runtime_waits_for_the_weights_to_load() -> None:
    api = FakeContainerApi("docker")
    prober = FakeProber()
    inner = ContainerApiRuntime(
        api, image=IMAGE, host_models_dir=HOST_MODELS, network=NETWORK, prober=prober
    )
    ticks = {"now": 0.0, "armed": 1.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        ticks["now"] += seconds
        if len(slept) == 3 and ticks["armed"]:
            ticks["armed"] = 0.0
            prober.healthy.add("vllm-coder")

    patient = PatientRuntime(
        inner, timeout_s=60.0, interval_s=5.0, sleep=sleep, monotonic=lambda: ticks["now"]
    )
    ref = patient.start(coder_spec())
    assert patient.is_healthy(ref) and slept == [5.0, 5.0, 5.0]
    assert [r.name for r in patient.running()] == ["vllm-coder"]
    assert patient.last_log_lines("vllm-coder") == []
    state = patient.state_of("vllm-coder")
    assert state is not None and state.running

    prober.healthy.clear()
    slept.clear()
    assert not patient.is_healthy(ref), "gives up after the timeout"
    assert sum(slept) >= 60.0
    api.crash("vllm-coder")
    slept.clear()
    assert not patient.is_healthy(ref) and slept == [], "a dead container is not waited for"
    patient.stop(ref)
    assert api.inspect("vllm-coder") is None


def test_vllm_spec_task_flags_for_embed_and_rerank() -> None:
    assert task_for_role("embed") == "embed" and task_for_role("rerank") == "score"
    assert task_for_role("coder") == "generate" and task_for_role(None) == "generate"
    embed = vllm_spec(
        QUICKSTART.model("bge-m3"), name="vllm-embed", gpu_ids=[0], image=IMAGE, task="embed"
    )
    # vLLM's current pooling flags: `--task` is gone, and an instance given it exits at start.
    assert embed.argv[-4:] == ["--runner", "pooling", "--convert", "embed"]
    assert "--task" not in embed.argv
    assert "--enable-prefix-caching" not in embed.argv
    assert "--structured-outputs-config" not in embed.argv
    score = vllm_spec(
        QUICKSTART.model("bge-reranker-v2-m3"),
        name="vllm-rerank",
        gpu_ids=[0],
        image=IMAGE,
        task="score",
    )
    assert score.argv[-2:] == ["--runner", "pooling"] and "--convert" not in score.argv
    generate = vllm_spec(
        QUICKSTART.model("qwen3.8-27b-fp8"), name="vllm-coder", gpu_ids=[0], image=IMAGE
    )
    assert "--runner" not in generate.argv and "--enable-prefix-caching" in generate.argv


# --- placement --------------------------------------------------------------------------------


def test_placement_orders_roles_before_voters_biggest_first_and_spreads() -> None:
    wanted = placement.candidates(QUICKSTART)
    assert [c.instance for c in wanted] == [
        "vllm-triage",
        "vllm-coder",
        "vllm-planner",
        "vllm-embed",
        "vllm-rerank",
        "vllm-voter-deepseek-v4-flash",
        "vllm-voter-qwen3.8-27b-fp8",
    ]
    result = placement.place(wanted, gpu_ids=[0, 1, 2], capacity_gib=280.0)
    assert result.unplaced == []
    assert {p.instance: p.gpu_ids for p in result.placed} == {
        "vllm-triage": [0],
        "vllm-coder": [1],
        "vllm-planner": [2],
        "vllm-embed": [1],
        "vllm-rerank": [2],
        "vllm-voter-deepseek-v4-flash": [2],
        "vllm-voter-qwen3.8-27b-fp8": [1],
    }
    assert result.free_gib == {0: 98.0, 1: 191.0, 2: 52.0}
    assert result.occupants[0] == ["vllm-triage"]
    assert result.gpu_ids_for("vllm-embed") == [1] and result.gpu_ids_for("nope") is None
    assert result.sentence_for("vllm-embed") is None
    again = placement.place(wanted, gpu_ids=[2, 0, 1, 1], capacity_gib=280.0)
    assert again.placed == result.placed, "deterministic whatever the id order"


def test_placement_takes_several_gpus_for_a_big_model_and_honours_pinned() -> None:
    prod = profile_registry("prod")
    wanted = placement.candidates(prod, only=["vllm-planner", "vllm-coder"])
    assert [c.instance for c in wanted] == ["vllm-planner", "vllm-coder"]
    pinned = {"vllm-triage": ([4], 182.0)}
    result = placement.place(wanted, gpu_ids=range(8), capacity_gib=280.0, pinned=pinned)
    assert result.gpu_ids_for("vllm-planner") == [0, 1, 2, 3], "902 GiB is four GPUs of 280"
    assert result.gpu_ids_for("vllm-coder") == [5], "GPU 4 is taken by the pinned triage"
    assert result.occupants[4] == ["vllm-triage"] and result.free_gib[4] == 98.0
    assert placement.gpu_count_for(902.0, 280.0) == 4 and placement.gpu_count_for(5.0, 0.0) == 1

    short = placement.place(wanted, gpu_ids=[0, 1, 2], capacity_gib=280.0)
    assert short.gpu_ids_for("vllm-planner") is None
    assert short.sentence_for("vllm-planner") == (
        "DeepSeek-V4 Pro (FP8) needs about 902 GiB of GPU memory, which is 4 GPUs of 280 GiB in "
        "tensor parallel, but only 3 of 3 GPUs have 225.5 GiB left; vllm-planner was not "
        "started. Add a GPU, raise SLAS_GPU_VRAM_GIB in .env if it understates the cards, or "
        "pick a smaller build on the Models page."
    )
    none = placement.place(wanted, gpu_ids=[], capacity_gib=280.0)
    assert [u.instance for u in none.unplaced] == ["vllm-planner", "vllm-coder"]
    assert "SLAS_GPU_IDS names no GPU" in none.unplaced[0].sentence


# --- the controller through the routes ----------------------------------------------------------


def test_quickstart_registry_starts_every_instance_and_publishes_when_healthy(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    report = h.client.post("/v1/reconcile", json={}).json()
    assert {a["kind"] for a in report["actions"]} == {"start"}
    assert report["sentence"].startswith("To match the registry: start vllm-coder, vllm-planner")
    assert sorted(h.api.specs) == [
        "vllm-coder",
        "vllm-embed",
        "vllm-planner",
        "vllm-rerank",
        "vllm-triage",
        "vllm-voter-deepseek-v4-flash",
        "vllm-voter-qwen3.8-27b-fp8",
    ]
    assert all(state == "running" for state in h.api.states.values())

    triage = h.api.bodies["vllm-triage"]
    assert triage["HostConfig"]["DeviceRequests"] == [
        {"Driver": "nvidia", "DeviceIDs": ["0"], "Capabilities": [["gpu"]]}
    ]
    assert triage["HostConfig"]["Binds"] == [f"{HOST_MODELS}:/data/Models:ro"]
    assert triage["HostConfig"]["NetworkMode"] == NETWORK
    assert triage["Labels"]["slas.model"] == "deepseek-v4-flash"
    assert "CUDA_VISIBLE_DEVICES=0" in triage["Env"] and "VLLM_NO_USAGE_STATS=1" in triage["Env"]
    assert triage["Cmd"][:1] == ["/data/Models/deepseek-v4-flash"], "positional for vllm serve"
    assert "--enable-prefix-caching" in triage["Cmd"]
    embed = h.api.bodies["vllm-embed"]["Cmd"]
    assert embed[-4:] == ["--runner", "pooling", "--convert", "embed"]
    assert "--enable-prefix-caching" not in embed
    assert h.api.bodies["vllm-rerank"]["Cmd"][-2:] == ["--runner", "pooling"]

    status = h.status()
    assert status["engine"] == "docker"
    assert status["sentence"].startswith("0 of 7 model instances are healthy; ")
    assert {g["id"]: g["instances"] for g in status["gpus"]} == {
        0: ["vllm-triage"],
        1: ["vllm-coder", "vllm-voter-qwen3.8-27b-fp8"],
        2: ["vllm-planner"],
        3: ["vllm-embed", "vllm-rerank", "vllm-voter-deepseek-v4-flash"],
    }, "roles first, biggest first, each to the GPU with the most memory left"
    rows = h.rows()
    assert rows["vllm-coder"]["state"] == "starting" and rows["vllm-coder"]["kind"] == "role"
    assert rows["vllm-coder"]["role"] == "coder" and rows["vllm-coder"]["gpu_ids"] == [1]
    assert rows["vllm-coder"]["url"] == "http://vllm-coder:8000"
    assert rows["vllm-voter-deepseek-v4-flash"]["kind"] == "voter"
    assert rows["vllm-voter-deepseek-v4-flash"]["role"] is None
    assert rows["vllm-voter-deepseek-v4-flash"]["display_name"] == "DeepSeek-V4 Flash"
    assert "loading its weights" in rows["vllm-planner"]["sentence"]

    first = h.gateway.bodies[0]
    assert first["routes"] == QUICKSTART.routes().model_dump()
    assert all(not row["healthy"] for row in first["instances"])
    assert [row["name"] for row in first["instances"]] == sorted(h.api.specs)

    h.prober.everything = True
    h.controller.reconcile()
    assert h.status()["sentence"] == "7 of 7 model instances are healthy."
    latest = h.gateway.bodies[-1]
    assert all(row["healthy"] for row in latest["instances"])
    coder_row = next(r for r in latest["instances"] if r["name"] == "vllm-coder")
    assert coder_row == {
        "name": "vllm-coder",
        "url": "http://vllm-coder:8000",
        "model_id": "qwen3.8-27b-fp8",
        "healthy": True,
    }
    assert h.rows()["vllm-coder"]["sentence"] == (
        "Qwen3.8-27B is serving the coder role at http://vllm-coder:8000."
    )
    voter = h.rows()["vllm-voter-qwen3.8-27b-fp8"]["sentence"]
    assert voter.endswith("as a voter at http://vllm-voter-qwen3.8-27b-fp8:8000.")
    assert h.events("instance.started") and h.events("instance.started")[0]["gpus"]


def test_reconcile_is_idempotent_and_survives_a_manager_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.controller.reconcile()
    created = dict(h.api.bodies)
    report = h.controller.reconcile()
    assert {a.kind for a in report.actions} == {"keep"}
    assert report.sentence == "Every model instance matches the registry; nothing to do."
    assert h.api.bodies == created
    assert len(h.gateway.bodies) == 2, "the table is published every tick, idempotently"

    # A new manager over the same runtime knows its containers again from the labels.
    again = Harness(tmp_path, api=h.api)
    report = again.controller.reconcile()
    assert {a.kind for a in report.actions} == {"keep"}
    assert sorted(h.api.specs) == sorted(created)


def test_a_container_created_with_old_flags_is_replaced_by_a_new_model_manager(
    tmp_path: Path,
) -> None:
    """The first host: vLLM containers created by an earlier manager kept crashing with
    `--guided-decoding-backend` after the manager itself was fixed, because a container's
    command is fixed at creation and a matching name and model read as "keep". A fresh
    manager now compares the flags (and the image) and replaces what differs."""
    h = Harness(tmp_path)
    h.prober.everything = True
    h.controller.reconcile()
    old_argv = [
        "--model",
        "/data/Models/qwen3.8-27b-fp8",
        "--served-model-name",
        "qwen3.8-27b-fp8",
        "--max-model-len",
        "131072",
        "--tensor-parallel-size",
        "1",
        "--quantization",
        "fp8",
        "--enable-prefix-caching",
        "--guided-decoding-backend",
        "xgrammar",
    ]
    h.api.specs["vllm-coder"].labels["slas.argv"] = json.dumps(old_argv)
    h.api.crash("vllm-coder", exit_code=2)
    h.api.logs_text["vllm-coder"] = "vllm: error: unrecognized arguments: --guided-decoding-backend"

    again = Harness(tmp_path, api=h.api)  # the manager restarted with the fixed image
    again.prober.everything = True
    report = again.controller.reconcile()
    kinds = {(a.kind, a.name) for a in report.actions}
    assert ("stop", "vllm-coder") in kinds and ("start", "vllm-coder") in kinds
    stop = next(a for a in report.actions if a.kind == "stop" and a.name == "vllm-coder")
    assert stop.reason.startswith("was created with other vLLM flags")
    assert all(a.kind == "keep" for a in report.actions if a.name != "vllm-coder")
    cmd = h.api.bodies["vllm-coder"]["Cmd"]
    assert "--guided-decoding-backend" not in cmd and "--structured-outputs-config" in cmd
    assert h.api.states["vllm-coder"] == "running"
    assert again.rows()["vllm-coder"]["state"] == "healthy"

    # An image change (a new lock) replaces every instance the same way.
    newer = Harness(tmp_path, api=h.api, image=IMAGE.replace("v0.29.0", "v0.30.0"))
    newer.prober.everything = True
    replaced = {a.name for a in newer.controller.reconcile().actions if a.kind == "stop"}
    assert "vllm-coder" in replaced and "vllm-embed" in replaced


def test_the_coder_loads_first_and_the_other_instances_wait_until_it_answers(
    tmp_path: Path,
) -> None:
    """The first host: seven vLLM instances reading their weights at once kept the coder
    loading past the install's 15 min wait. With SLAS_START_CODER_FIRST the manager starts
    only vllm-coder, reports the others as waiting, and starts them once the coder answers."""
    h = Harness(tmp_path, coder_first=True)
    report = h.controller.reconcile()
    assert sorted(h.api.specs) == ["vllm-coder"]
    assert {a.name for a in report.actions if a.kind == "start"} == {"vllm-coder"}
    waiting = next(a for a in report.actions if a.name == "vllm-planner")
    assert waiting.kind == "keep" and waiting.reason == "waits until vllm-coder answers"
    rows = h.rows()
    assert rows["vllm-coder"]["state"] == "starting"
    assert rows["vllm-planner"]["state"] == "starting"
    assert rows["vllm-planner"]["sentence"].startswith(
        "Qwen3.8-27B waits its turn: vllm-coder loads first"
    )
    assert len(rows) == 7, "every desired instance has a row while the coder loads"
    published = {r["name"] for r in h.gateway.bodies[-1]["instances"]}
    assert published == {"vllm-coder"}, "only containers are published to the gateway"

    # Still loading next tick: nothing else starts.
    h.controller.reconcile()
    assert sorted(h.api.specs) == ["vllm-coder"]

    # The coder answers: the rest start on the same tick.
    h.prober.healthy = {"vllm-coder"}
    report = h.controller.reconcile()
    assert len(h.api.specs) == 7
    assert {a.name for a in report.actions if a.kind == "start"} == set(h.api.specs) - {
        "vllm-coder"
    }
    assert h.rows()["vllm-coder"]["state"] == "healthy"
    assert h.rows()["vllm-planner"]["state"] == "starting"

    # A crashed coder holds nobody back: the gate is for a loading coder only.
    crashed = Harness(tmp_path, coder_first=True)
    crashed.controller.reconcile()
    crashed.api.crash("vllm-coder", exit_code=1)
    crashed.controller.reconcile()
    assert len(crashed.api.specs) == 7

    # The default harness (SLAS_START_CODER_FIRST=0) starts everything at once.
    at_once = Harness(tmp_path)
    at_once.controller.reconcile()
    assert len(at_once.api.specs) == 7


def test_instances_already_loading_beside_the_coder_are_paused_until_it_answers(
    tmp_path: Path,
) -> None:
    """The first host again: the seven containers existed before the fixed manager started,
    so a gate on new starts alone changed nothing. Instances found loading beside a loading
    coder are stopped and wait their turn; a container that was healthy once is left alone."""
    before = Harness(tmp_path)  # an older manager started everything at once
    before.controller.reconcile()
    assert len(before.api.specs) == 7

    h = Harness(tmp_path, api=before.api, coder_first=True)
    report = h.controller.reconcile()
    assert sorted(h.api.specs) == ["vllm-coder"]
    paused = {a.name: a.reason for a in report.actions if a.kind == "stop"}
    assert set(paused) == {
        "vllm-embed",
        "vllm-planner",
        "vllm-rerank",
        "vllm-triage",
        "vllm-voter-deepseek-v4-flash",
        "vllm-voter-qwen3.8-27b-fp8",
    }
    assert paused["vllm-planner"] == (
        "waits until vllm-coder answers; it was loading beside the coder"
    )
    rows = h.rows()
    assert rows["vllm-planner"]["state"] == "starting"
    assert "waits its turn" in rows["vllm-planner"]["sentence"]
    assert [e["instance"] for e in h.events("instance.paused")] == sorted(paused)
    # A loading vLLM ignores SIGTERM; the first host waited 30 s per pause. A pause kills fast.
    assert {h.api.stop_timeouts[name] for name in paused} == {2}

    h.prober.healthy = {"vllm-coder"}
    h.controller.reconcile()
    assert len(h.api.specs) == 7

    # An instance the manager saw healthy is never paused: it merely lost its health check.
    steady = Harness(tmp_path, coder_first=True)
    steady.prober.everything = True
    steady.controller.reconcile()  # the coder, alone
    steady.controller.reconcile()  # it answers: the rest start
    steady.controller.reconcile()  # and answer too
    assert len(steady.api.specs) == 7
    assert all(row["state"] == "healthy" for row in steady.rows().values())
    steady.prober.everything = False
    steady.prober.healthy = set()
    steady.api.crash("vllm-coder", exit_code=0)  # stopped by hand: started again, loading
    steady.controller.reconcile()
    assert len(steady.api.specs) == 7 and not steady.events("instance.paused")
    assert steady.rows()["vllm-planner"]["state"] == "unhealthy"


def test_a_container_the_runtime_keeps_restarting_is_a_crash_not_a_load(tmp_path: Path) -> None:
    """vLLM containers run with `restart: unless-stopped`, so one that exits at start is
    running again within seconds and its state reads "running" nearly all the time. The
    restart count tells the manager it is crashing; the Models page and the install say so
    with the log tail instead of "loading" for a quarter of an hour."""
    h = Harness(tmp_path, coder_first=True)
    h.controller.reconcile()
    h.api.logs_text["vllm-coder"] = "RuntimeError: CUDA driver too old for this vLLM build"

    h.api.restarting("vllm-coder", times=1, exit_code=1)
    h.controller.reconcile()
    row = h.rows()["vllm-coder"]
    assert row["state"] == "starting"
    assert "It exited once before and the runtime started it again." in row["sentence"]
    assert sorted(h.api.specs) == ["vllm-coder"], "one restart still holds the gate"

    # Docker resets the exit code to 0 once the container runs again: the count decides.
    h.api.restarting("vllm-coder", times=3, exit_code=0)
    h.controller.reconcile()
    row = h.rows()["vllm-coder"]
    assert row["state"] == "failed"
    assert row["sentence"] == (
        "Qwen3.8-27B keeps crashing: the runtime started it 3 times and it exited each "
        "time, so it never finishes loading. What it logged before it last exited:\n"
        "RuntimeError: CUDA driver too old for this vLLM build"
    )
    assert h.api.states["vllm-coder"] == "running", "the restart policy owns the container"
    assert len(h.api.specs) == 7, "a crashing coder holds nobody back"
    assert h.status()["sentence"].startswith("0 of 7 model instances are healthy; ")

    # Once it answers, the count no longer matters.
    h.prober.healthy = {"vllm-coder"}
    h.controller.reconcile()
    assert h.rows()["vllm-coder"]["state"] == "healthy"


VLLM_RESTART_LOG = "\n".join(
    [
        "(APIServer pid=1) INFO 09-18 07:56:28 [model.py:2021] Using max model len 131072",
        "(EngineCore_DP0 pid=71) INFO Loading weights took 95.2 seconds",
        "(EngineCore_DP0 pid=71) ERROR EngineCore failed to start.",
        "(EngineCore_DP0 pid=71) ERROR Traceback (most recent call last):",
        '(EngineCore_DP0 pid=71) ERROR   File "/vllm/v1/core/kv_cache_utils.py", line 900',
        "(EngineCore_DP0 pid=71) ERROR ValueError: To serve at least one request with the "
        "models's max seq len (131072), (34.00 GiB KV cache is needed, which is larger than "
        "the available KV cache memory (21.50 GiB). Based on the available memory, the "
        "estimated maximum model length is 82944. Try increasing `gpu_memory_utilization` or "
        "decreasing `max_model_len` when initializing the engine.",
        "(APIServer pid=1) RuntimeError: Engine core initialization failed. See root cause above.",
        "(APIServer pid=1) INFO 09-18 07:57:53 [model.py:2021] Using max model len 131072",
        "(APIServer pid=1) [transformers] The `use_fast` parameter is deprecated.",
    ]
)


def test_a_crash_loop_shows_the_error_before_the_last_exit_not_the_fresh_start(
    tmp_path: Path,
) -> None:
    """The first host: the install's progress line showed "Using max model len" three times
    in seven minutes, then deprecation warnings, because the runtime keeps one log across
    restarts and the tail is always the newest attempt's start-up. The evidence is the
    window around the last error line before it."""
    h = Harness(tmp_path, coder_first=True)
    h.controller.reconcile()
    # The same shape of log, but not the KV-cache error: the context must stay as it is.
    unrelated = VLLM_RESTART_LOG.replace("larger than the", "bigger than the").replace(
        "decreasing `max_model_len`", "another engine"
    )
    h.api.logs_text["vllm-coder"] = unrelated
    h.api.restarting("vllm-coder", times=2)
    h.controller.reconcile()
    sentence = h.rows()["vllm-coder"]["sentence"]
    assert "It exited 2 times before and the runtime started it again." in sentence
    # The root cause is the first exception of the attempt, not the API server's pointer.
    assert (
        "Before it last exited it logged: (EngineCore_DP0 pid=71) ERROR ValueError: To serve"
        in sentence
    )
    assert sentence.endswith(
        "Last log line: (APIServer pid=1) [transformers] The `use_fast` parameter is deprecated."
    )

    h.api.restarting("vllm-coder", times=3)
    h.controller.reconcile()
    row = h.rows()["vllm-coder"]
    assert row["state"] == "failed"
    lines = row["sentence"].split("\n")
    assert lines[0].endswith("What it logged before it last exited:")
    assert lines[1].endswith("Using max model len 131072"), "the failed attempt, from its start"
    assert any("ValueError" in line for line in lines)
    assert lines[-1].endswith("Using max model len 131072"), "one line after the error, no more"
    assert not any("use_fast" in line for line in lines), "the fresh attempt's warnings are noise"
    assert not h.events("instance.context_reduced")


EC = "(EngineCore_DP0 pid=71) ERROR 09-18 08:00:50 [core.py:822] "
AP = "(APIServer pid=1) "
ENGINE_CORE_CRASH = "\n".join(
    [
        AP + "INFO 09-18 08:00:43 [model.py:2021] Using max model len 131072",
        "(EngineCore_DP0 pid=71) INFO 09-18 08:00:48 [gpu_model_runner.py:2653] Loading model",
        EC + "EngineCore failed to start.",
        EC + "Traceback (most recent call last):",
        EC + '  File "/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/core.py", line 813',
        EC + "    engine_core = EngineCoreProc(*args)",
        EC + "                  ^^^^^^^^^^^^^^^^^^^^^^^",
        EC
        + '  File "/usr/local/lib/python3.12/dist-packages/vllm/v1/worker/gpu_worker.py", line 2',
        EC + "    torch.cuda.set_device(self.device)",
        EC + "RuntimeError: CUDA error: no kernel image is available for execution on the device",
        AP + "Traceback (most recent call last):",
        AP + '  File "/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/utils.py", line 1320',
        AP + "    raise RuntimeError(",
        AP + "RuntimeError: Engine core initialization failed. See root cause above. "
        "Failed core proc(s): {}",
        AP + "INFO 09-18 08:02:10 [model.py:2021] Using max model len 131072",
        AP + "[transformers] The `use_fast` parameter is deprecated.",
    ]
)


def test_crash_evidence_keeps_the_engine_root_cause_and_drops_the_traceback_frames(
    tmp_path: Path,
) -> None:
    """The first host, round three: the row showed twelve lines of the API server's traceback
    ending in "See root cause above", and the root cause (the engine process's own exception,
    logged earlier) was cut off. The evidence is the whole failed attempt's error cluster
    minus the frames, so both exceptions fit and the first one is the cause."""
    h = Harness(tmp_path, coder_first=True)
    h.controller.reconcile()
    h.api.logs_text["vllm-coder"] = ENGINE_CORE_CRASH
    h.api.restarting("vllm-coder", times=87)
    h.controller.reconcile()
    row = h.rows()["vllm-coder"]
    assert row["state"] == "failed"
    lines = row["sentence"].split("\n")
    assert lines[0].startswith("Qwen3.8-27B keeps crashing: the runtime started it 87 times")
    body = lines[1:]
    assert any(line.endswith("EngineCore failed to start.") for line in body)
    assert any(
        line.endswith(
            "RuntimeError: CUDA error: no kernel image is available for execution on the device"
        )
        for line in body
    )
    assert any("Engine core initialization failed" in line for line in body)
    assert not any('File "' in line for line in body), "frames are noise"
    assert not any("^^^^" in line for line in body)
    assert not any("Traceback (most recent call last)" in line for line in body)
    assert not any("torch.cuda.set_device" in line for line in body), "the code under a frame"
    assert not any("use_fast" in line for line in body), "the fresh attempt is not the evidence"
    assert body[-1].endswith("Using max model len 131072"), "one line after the last error"
    assert body[0].endswith("Using max model len 131072"), "a few lines before the first"

    # The loading sentence names the root cause, not the pointer to it.
    h.api.restarting("vllm-coder", times=1)
    h.controller.reconcile()
    sentence = h.rows()["vllm-coder"]["sentence"]
    assert (
        "Before it last exited it logged: (EngineCore_DP0 pid=71) ERROR 09-18 08:00:50 "
        "[core.py:822] RuntimeError: CUDA error: no kernel image"
    ) in sentence

    # A very long cluster keeps its head (the cause) and its tail, and says what it dropped.
    long_log = ENGINE_CORE_CRASH.replace(
        "(APIServer pid=1) Traceback (most recent call last):",
        "\n".join(f"(APIServer pid=1) ERROR frame {i} in a very deep stack" for i in range(80)),
    )
    h.api.logs_text["vllm-coder"] = long_log
    h.api.restarting("vllm-coder", times=88)
    h.controller.reconcile()
    body = h.rows()["vllm-coder"]["sentence"].split("\n")[1:]
    assert len(body) == 40
    assert any("RuntimeError: CUDA error" in line for line in body)
    assert any("Engine core initialization failed" in line for line in body)
    assert any(
        line.startswith("… ") and line.endswith(" more traceback lines left out …") for line in body
    )


def test_a_known_crash_signature_gets_a_cause_and_a_host_fix(tmp_path: Path) -> None:
    """The first host: eight B300 SXM GPUs and no Fabric Manager. vLLM's engine died with
    CUDA error 802 and the row showed a traceback; it now says what that means and what to
    run on the host, in the three-part shape every error has (CLAUDE.md §11)."""
    h = Harness(tmp_path, coder_first=True)
    h.controller.reconcile()
    h.api.logs_text["vllm-coder"] = "\n".join(
        [
            EC + "EngineCore failed to start.",
            EC + "    torch._C._cuda_init()",
            EC + "RuntimeError: Unexpected error from cudaGetDeviceCount(). Did you run some "
            "cuda functions before calling NumCudaDevices() that might have already set an "
            "error? Error 802: system not yet initialized",
            AP + "RuntimeError: Engine core initialization failed. See root cause above. "
            "Failed core proc(s): {}",
        ]
    )
    h.api.restarting("vllm-coder", times=87)
    h.controller.reconcile()
    sentence = h.rows()["vllm-coder"]["sentence"]
    first, _, evidence = sentence.partition("\n")
    assert first == (
        "Qwen3.8-27B keeps crashing: the runtime started it 87 times and it exited each time, "
        "so it never finishes loading. Likely cause: This is an NVSwitch system (SXM GPUs) and "
        "NVIDIA Fabric Manager is not running, so CUDA cannot initialize although nvidia-smi "
        "works. What to do: On the host: sudo apt install nvidia-fabricmanager-<driver major> "
        "(e.g. 595 for driver 595.x), sudo systemctl enable --now nvidia-fabricmanager, then "
        "wait for the next reconcile; `slas doctor` checks this as GPU fabric. What it logged "
        "before it last exited:"
    )
    assert "Error 802: system not yet initialized" in evidence
    assert not h.events("instance.context_reduced"), "not a KV-cache problem"

    # A crash the table does not know keeps the plain sentence.
    h.api.logs_text["vllm-planner"] = "RuntimeError: something new"
    h.api.restarting("vllm-planner", times=3)
    h.prober.healthy = {"vllm-coder"}
    h.controller.reconcile()
    assert "Likely cause" not in h.rows()["vllm-planner"]["sentence"]


def test_a_context_that_does_not_fit_the_kv_cache_is_halved_and_kept(tmp_path: Path) -> None:
    """vLLM refuses to start when `--max-model-len` needs more KV cache than the GPU has left
    beside the weights, and exits within a minute or two; with `restart: unless-stopped`
    that loops forever and reads as "loading". The manager halves the context (never below
    8192), starts the instance again, says so, and keeps the value across its own restarts."""
    h = Harness(tmp_path, coder_first=True)
    h.controller.reconcile()
    h.api.logs_text["vllm-coder"] = VLLM_RESTART_LOG
    h.api.restarting("vllm-coder", times=3)
    report = h.controller.reconcile()
    stop = next(a for a in report.actions if a.kind == "stop" and a.name == "vllm-coder")
    assert stop.reason == (
        "crashed because a context of 131072 tokens does not fit in the GPU's KV cache next "
        "to the weights; started again with 65536"
    )
    cmd = h.api.bodies["vllm-coder"]["Cmd"]
    assert cmd[cmd.index("--max-model-len") + 1] == "65536"
    assert "--structured-outputs-config" in cmd, "every other flag is unchanged"
    row = h.rows()["vllm-coder"]
    assert row["state"] == "starting"
    assert (
        "It runs with a context of 65536 tokens: the registry's 131072 did not fit in the "
        "GPU's KV cache next to the weights."
    ) in row["sentence"]
    reduced = h.events("instance.context_reduced")
    assert [(e["was"], e["now"], e["cap"]) for e in reduced] == [(131072, 65536, 131072)]
    assert sorted(h.api.specs) == ["vllm-coder"], "the gate still holds: the coder is loading"

    # A manager restart reads the smaller context from the container and keeps it.
    again = Harness(tmp_path, api=h.api, coder_first=True)
    report = again.controller.reconcile()
    assert all(a.kind == "keep" for a in report.actions if a.name == "vllm-coder")
    assert "context of 65536 tokens" in again.rows()["vllm-coder"]["sentence"]

    # It answers: the sentence carries the context, and the others start.
    again.prober.healthy = {"vllm-coder"}
    again.controller.reconcile()
    row = again.rows()["vllm-coder"]
    assert row["state"] == "healthy" and "context of 65536 tokens" in row["sentence"]
    assert len(again.api.specs) == 7

    # Still too big: halve again, down to the floor, then stop shrinking and report.
    floor = Harness(tmp_path, coder_first=True)
    floor.controller.reconcile()
    for expected in ("65536", "32768", "16384", "8192"):
        floor.api.logs_text["vllm-coder"] = VLLM_RESTART_LOG
        floor.api.restarting("vllm-coder", times=3)
        floor.controller.reconcile()
        cmd = floor.api.bodies["vllm-coder"]["Cmd"]
        assert cmd[cmd.index("--max-model-len") + 1] == expected
    floor.api.logs_text["vllm-coder"] = VLLM_RESTART_LOG
    floor.api.restarting("vllm-coder", times=3)
    report = floor.controller.reconcile()
    assert not [a for a in report.actions if a.kind == "stop"], "8192 is the floor"
    assert floor.rows()["vllm-coder"]["state"] == "failed"

    # An unrelated crash never touches the context.
    other = Harness(tmp_path, coder_first=True)
    other.controller.reconcile()
    other.api.logs_text["vllm-coder"] = "RuntimeError: CUDA driver too old for this vLLM build"
    other.api.restarting("vllm-coder", times=3)
    report = other.controller.reconcile()
    assert not [a for a in report.actions if a.kind == "stop"]
    assert not other.events("instance.context_reduced")


def test_a_loading_instance_says_for_how_long_and_what_vllm_last_logged(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.controller.reconcile()
    row = h.rows()["vllm-coder"]
    assert row["sentence"] == "Qwen3.8-27B is loading its weights; not answering yet."

    h.clock._now += timedelta(minutes=7, seconds=30)  # the test owns the clock
    h.api.logs_text["vllm-coder"] = (
        "INFO Starting to load model /data/Models/qwen3.8-27b-fp8...\n"
        "INFO Loading weights took 310.2 seconds\n"
        "INFO Capturing CUDA graphs (mixed prefill-decode, PIECEWISE): 40%\n\n"
    )
    h.controller.reconcile()
    row = h.rows()["vllm-coder"]
    assert row["state"] == "starting"
    assert row["sentence"] == (
        "Qwen3.8-27B is loading its weights for 7 min; not answering yet. "
        "Last log line: INFO Capturing CUDA graphs (mixed prefill-decode, PIECEWISE): 40%"
    )

    # Once healthy the timer is forgotten; a later restart counts from zero again.
    h.prober.healthy = {"vllm-coder"}
    h.controller.reconcile()
    assert h.rows()["vllm-coder"]["state"] == "healthy"
    assert "vllm-coder" not in h.controller._starting_since


def test_crashed_container_is_reported_failed_with_its_last_forty_log_lines(
    tmp_path: Path,
) -> None:
    fake = ExitCodeFake()
    h = Harness(tmp_path, api=fake)
    h.prober.everything = True
    h.controller.reconcile()
    h.api.crash("vllm-embed", exit_code=137)
    h.api.logs_text["vllm-embed"] = "\n".join(f"INFO step {i}" for i in range(60))
    h.controller.reconcile()
    row = h.rows()["vllm-embed"]
    assert row["state"] == "failed"
    lines = row["sentence"].split("\n")
    assert lines[0] == "BGE-M3 crashed. Last log lines:"
    assert lines[1:] == [f"INFO step {i}" for i in range(20, 60)]
    assert h.api.states["vllm-embed"] == "exited", "the runtime's restart policy owns crashes"
    assert h.status()["sentence"] == "6 of 7 model instances are healthy; vllm-embed failed."
    published = {r["name"]: r["healthy"] for r in h.gateway.bodies[-1]["instances"]}
    assert published["vllm-embed"] is False and published["vllm-coder"] is True

    # Health lost after it was once healthy reads as unhealthy, not starting.
    h.prober.everything = False
    h.prober.healthy = {"vllm-coder"}
    h.controller.reconcile()
    rows = h.rows()
    assert rows["vllm-triage"]["state"] == "unhealthy"
    assert "stopped answering its health check" in rows["vllm-triage"]["sentence"]
    assert h.status()["sentence"].startswith("1 of 7 model instances are healthy; ")

    # Stopped by hand (exit 0) is different: the manager starts it again.
    h.api.crash("vllm-rerank", exit_code=0)
    fake.exit_codes["vllm-rerank"] = 0
    report = h.controller.reconcile()
    assert ("start", "vllm-rerank") in {(a.kind, a.name) for a in report.actions}
    assert h.api.states["vllm-rerank"] == "running"
    fake.exit_codes.clear()
    assert h.rows()["vllm-rerank"]["state"] == "starting", "never healthy since its restart"


def test_an_entry_that_does_not_fit_is_never_started_and_gets_the_fit_sentence(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, gpu_ids=(0,), gpu_vram_gib=100.0)
    h.controller.reconcile()
    assert "vllm-triage" not in h.api.specs and "vllm-voter-deepseek-v4-flash" not in h.api.specs
    assert "vllm-voter-qwen3.8-27b-fp8" not in h.api.specs
    assert sorted(h.api.specs) == ["vllm-coder", "vllm-embed", "vllm-planner", "vllm-rerank"]
    rows = h.rows()
    assert rows["vllm-triage"]["state"] == "failed" and rows["vllm-triage"]["gpu_ids"] == []
    assert rows["vllm-triage"]["sentence"].startswith(
        "DeepSeek-V4 Flash (FP8) needs about 182 GiB of GPU memory, which is 2 GPUs of 100 GiB "
        "in tensor parallel, but only 1 of 1 GPUs have 91 GiB left; vllm-triage was not started."
    )
    assert rows["vllm-voter-qwen3.8-27b-fp8"]["sentence"].startswith(
        "Qwen3.8-27B (FP8) needs about 42 GiB of GPU memory, but the most left on one GPU after "
        "the other models is 7 GiB (GPU 0); vllm-voter-qwen3.8-27b-fp8 was not started."
    )
    assert h.events("instance.no_room")
    published = [r["name"] for r in h.gateway.bodies[-1]["instances"]]
    assert "vllm-triage" not in published, "the gateway never hears of what does not run"
    # Idempotent: the next tick plans the same starts and still starts nothing new.
    h.controller.reconcile()
    assert sorted(h.api.specs) == ["vllm-coder", "vllm-embed", "vllm-planner", "vllm-rerank"]
    fit = h.client.get("/v1/fit", params={"model": "deepseek-v4-flash"}).json()
    assert fit["fits"] is False and "182 GiB" in fit["sentence"]


def test_registry_changes_stop_and_start_what_differs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.prober.everything = True
    h.controller.reconcile()
    data: dict[str, Any] = json.loads(json.dumps(PROFILE_REGISTRIES["quickstart"]))
    data["roles"]["planner"] = "deepseek-v4-flash"
    data["voters"] = ["deepseek-v4-flash"]
    h.models_file.write_text(render_registry_yaml(data), encoding="utf-8")
    report = h.controller.reconcile()
    kinds = {(a.kind, a.name) for a in report.actions}
    assert ("stop", "vllm-planner") in kinds and ("start", "vllm-planner") in kinds
    assert ("stop", "vllm-voter-qwen3.8-27b-fp8") in kinds
    assert h.api.specs["vllm-planner"].labels["slas.model"] == "deepseek-v4-flash"
    assert "vllm-voter-qwen3.8-27b-fp8" not in h.api.specs
    assert h.events("instance.stopped")[0]["reason"].startswith("serves qwen3.8-27b-fp8")
    assert h.gateway.bodies[-1]["routes"]["voters"] == ["vllm-voter-deepseek-v4-flash"]
    assert "vllm-voter-qwen3.8-27b-fp8" not in h.rows()


def test_problems_become_sentences_never_crashes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.status()["sentence"] == "The model manager has not reconciled yet."
    h.models_file.unlink()
    report = h.controller.tick()
    assert report.actions == []
    assert report.sentence.startswith(f"The model registry {h.models_file} could not be read.")
    assert h.status()["sentence"] == report.sentence
    assert h.events("registry.unusable")
    h.models_file.write_text("models: [\n", encoding="utf-8")
    with pytest.raises(RegistryError) as raised:
        h.controller.load_registry()
    assert "not valid YAML" in raised.value.message.likely_cause
    h.models_file.write_text("version: 1\nmodels: []\n", encoding="utf-8")
    assert "could not be used" in h.controller.tick().sentence
    h.models_file.write_text(render_registry_yaml(PROFILE_REGISTRIES["quickstart"]))

    h.api.down = True
    body = assert_problem(h.client.get("/health"), 503)
    assert body["what_happened"] == "The model-manager is not healthy: runtime did not answer."
    report = h.controller.tick()
    assert report.actions == [] and "did not answer" in report.sentence
    assert h.status()["engine"] == "unknown" and h.events("runtime.failed")
    h.api.down = False
    assert h.client.get("/health").json()["checks"] == {"runtime": "ok"}
    assert {a.kind for a in h.controller.tick().actions} == {"start"}

    h.gateway.down = True
    h.controller.tick()
    assert "The llm-gateway did not answer." in h.status()["sentence"]
    assert h.events("gateway.publish_failed")
    h.gateway.down = False
    h.controller.tick()
    assert "did not answer" not in h.status()["sentence"]

    def boom() -> list[ContainerRef]:
        raise RuntimeError("nobody expected this")

    h.controller.runtime.running = boom  # type: ignore[method-assign]
    report = h.controller.tick()
    assert report.sentence == "Reconciling the model instances hit a problem it did not expect."
    assert h.events("reconcile.unexpected_error")[0]["error_type"] == "RuntimeError"


def test_a_start_the_runtime_refuses_is_a_failed_instance_with_the_runtime_sentence(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, api=FakeContainerApi("docker", images=["other:1"]))
    report = h.controller.reconcile()
    assert {a.kind for a in report.actions} == {"start"} and h.api.specs == {}
    rows = h.rows()
    assert all(row["state"] == "failed" for row in rows.values())
    assert rows["vllm-coder"]["gpu_ids"] == [1], "it was placed; the runtime said no"
    assert rows["vllm-coder"]["sentence"].startswith(
        f"The container runtime refused to create vllm-coder from {IMAGE}."
    )
    failed = h.events("instance.start_failed")
    assert failed, "the log names every instance the runtime refused"
    assert failed[0]["what_happened"].startswith("The container runtime refused to create")
    assert failed[0]["likely_cause"] and failed[0]["what_to_do"], "with the reason and the fix"
    assert failed[0]["image"] == IMAGE
    assert h.gateway.bodies[-1]["instances"] == [], "nothing runs, so nothing is published"
    assert "7 model instances" in h.status()["sentence"]


def test_no_vllm_image_is_a_sentence_not_a_start(tmp_path: Path) -> None:
    h = Harness(tmp_path, image="")
    report = h.controller.reconcile()
    assert report.actions == [] and h.api.specs == {}
    assert report.sentence.startswith("No vLLM image is configured")
    assert "SLAS_VLLM_IMAGE" in h.status()["sentence"]
    assert h.controller.registry() is not None


def test_swap_and_rollback_run_in_a_thread_and_the_route_wins_until_the_registry_changes(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.prober.everything = True
    h.controller.reconcile()
    headers = MANAGER.headers()

    assert_problem(h.client.post("/v1/swap", json={"role": "planner", "candidate": "x"}), 401)
    forbidden = assert_problem(
        h.client.post(
            "/v1/swap", json={"role": "planner", "candidate": "x"}, headers=VIEWER.headers()
        ),
        403,
    )
    assert forbidden["what_happened"] == "Sam may not swap a model."
    unknown = assert_problem(
        h.client.post("/v1/swap", json={"role": "planner", "candidate": "ghost"}, headers=headers),
        404,
    )
    assert unknown["what_happened"] == "There is no model called ghost in the registry."
    assert_problem(
        h.client.post("/v1/swap", json={"role": "dj", "candidate": "bge-m3"}, headers=headers), 400
    )
    cannot = assert_problem(
        h.client.post(
            "/v1/swap", json={"role": "coder", "candidate": "deepseek-v4-flash"}, headers=headers
        ),
        409,
    )
    assert cannot["what_happened"] == "DeepSeek-V4 Flash cannot serve coder."
    assert_problem(h.client.post("/v1/swap", json={"role": "coder"}, headers=headers), 400)
    nothing = assert_problem(
        h.client.post("/v1/rollback", json={"role": "planner"}, headers=headers), 409
    )
    assert nothing["what_happened"] == "There is no completed swap of planner to roll back."

    response = h.client.post(
        "/v1/swap", json={"role": "planner", "candidate": "deepseek-v4-flash"}, headers=headers
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["phase"] == "starting" and record["role"] == "planner"
    assert record["candidate"]["id"] == "deepseek-v4-flash"
    assert record["incumbent"]["id"] == "qwen3.8-27b-fp8"
    assert record["progress"] == [
        "Starting DeepSeek-V4 Flash alongside the current planner (Qwen3.8-27B)…"
    ]
    h.wait_swap()
    done = h.controller.swap_record("planner")
    assert done is not None
    assert done.phase == "done", done.progress
    candidate = "vllm-planner-deepseek-v4-flash"
    assert h.api.states[candidate] == "running" and "vllm-planner" not in h.api.specs
    assert h.api.specs[candidate].gpu_ids == [2], "placed where the GPUs had room"
    assert h.smoke.asked == [candidate]
    assert h.events("swap.finished")[0]["phase"] == "done"

    # Reconciliation respects the swap: no restart of vllm-planner, the route points at it.
    report = h.controller.reconcile()
    assert not any(a.name == "vllm-planner" for a in report.actions)
    assert h.gateway.bodies[-1]["routes"]["roles"]["planner"] == candidate
    names = [r["name"] for r in h.gateway.bodies[-1]["instances"]]
    assert candidate in names and "vllm-planner" not in names
    rows = h.rows()
    assert rows[candidate]["role"] == "planner" and rows[candidate]["state"] == "healthy"
    assert "vllm-planner" not in rows

    # Rollback: the incumbent comes back, the candidate is drained.
    response = h.client.post("/v1/rollback", json={"role": "planner"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["progress"][-1] == "Rolling planner back to Qwen3.8-27B…"
    h.wait_swap()
    rolled = h.controller.swap_record("planner")
    assert rolled is not None and rolled.phase == "rolled_back"
    assert "vllm-planner" in h.api.specs and candidate not in h.api.specs
    assert h.gateway.bodies[-1]["routes"]["roles"]["planner"] == "vllm-planner"
    assert h.events("rollback.finished")

    # After a rollback there is nothing left to roll back; a closed window is refused too.
    assert_problem(h.client.post("/v1/rollback", json={"role": "planner"}, headers=headers), 409)
    h.client.post(
        "/v1/swap", json={"role": "planner", "candidate": "deepseek-v4-flash"}, headers=headers
    )
    h.wait_swap()
    h.clock._now = START + timedelta(hours=25)
    closed = assert_problem(
        h.client.post("/v1/rollback", json={"role": "planner"}, headers=headers), 409
    )
    assert closed["what_happened"] == "The rollback window for planner closed at 2026-09-18 09:00."

    # Editing the registry makes it the truth again: the swap's route is superseded.
    data: dict[str, Any] = json.loads(json.dumps(PROFILE_REGISTRIES["quickstart"]))
    data["roles"]["planner"] = "deepseek-v4-flash"
    h.models_file.write_text(render_registry_yaml(data), encoding="utf-8")
    report = h.controller.reconcile()
    kinds = {(a.kind, a.name) for a in report.actions}
    assert ("start", "vllm-planner") in kinds and ("stop", candidate) in kinds
    assert (
        candidate not in h.api.specs
        and h.api.specs["vllm-planner"].labels["slas.model"] == "deepseek-v4-flash"
    )
    assert h.controller.swap_record("planner") is None
    assert h.gateway.bodies[-1]["routes"]["roles"]["planner"] == "vllm-planner"


def test_only_one_swap_at_a_time_and_a_failed_swap_reads_as_failed(tmp_path: Path) -> None:
    blocking = BlockingSmoke()
    h = Harness(tmp_path, smoke=blocking)
    h.prober.everything = True
    h.controller.reconcile()
    headers = MANAGER.headers()
    first = h.client.post(
        "/v1/swap", json={"role": "planner", "candidate": "deepseek-v4-flash"}, headers=headers
    )
    assert first.status_code == 200
    assert blocking.entered.wait(5)
    busy = assert_problem(
        h.client.post(
            "/v1/swap", json={"role": "coder", "candidate": "qwen3.8-27b-fp8"}, headers=headers
        ),
        409,
    )
    assert busy["what_happened"] == "A swap of planner is in progress."
    assert_problem(h.client.post("/v1/rollback", json={"role": "planner"}, headers=headers), 409)
    row = h.rows()["vllm-planner"]
    assert row["sentence"] == "Smoke-testing DeepSeek-V4 Flash…"
    report = h.controller.reconcile()
    assert not any(a.name == "vllm-planner" and a.kind != "keep" for a in report.actions)
    blocking.release.set()
    h.wait_swap()
    record = h.controller.swap_record("planner")
    assert record is not None and record.phase == "done"

    # No room for a second candidate alongside everything that runs.
    tight = Harness(tmp_path, gpu_ids=(0, 1), gpu_vram_gib=280.0)
    tight.prober.everything = True
    tight.controller.reconcile()
    full = assert_problem(
        tight.client.post(
            "/v1/swap", json={"role": "planner", "candidate": "deepseek-v4-flash"}, headers=headers
        ),
        409,
    )
    assert full["what_happened"] == "DeepSeek-V4 Flash cannot start alongside the current planner."
    assert "182 GiB" in full["likely_cause"]

    # The runtime refusing the candidate is a failed swap with a sentence, not a dead thread.
    tight.api.images = {IMAGE}
    tight.controller.image = "registry.internal/vllm/missing:v1"
    tight.controller.swaps.image = tight.controller.image
    response = tight.client.post(
        "/v1/swap", json={"role": "coder", "candidate": "qwen3.8-27b-fp8"}, headers=headers
    )
    assert response.status_code == 200
    tight.wait_swap()
    failed = tight.controller.swap_record("coder")
    assert failed is not None and failed.phase == "failed"
    assert "refused to create" in failed.progress[-1]
    assert tight.events("swap.failed")


def test_fit_route_and_unknown_model(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    fits = h.client.get("/v1/fit", params={"model": "qwen3.8-27b-fp8"}).json()
    assert fits == {
        "fits": True,
        "sentence": "Qwen3.8-27B (FP8) needs about 42 GiB of GPU memory; GPU 0 (280 GiB "
        "configured) has 280 GiB free, so it fits.",
    }
    h.prober.everything = True
    h.controller.reconcile()
    after = h.client.get("/v1/fit", params={"model": "deepseek-v4-flash"}).json()
    assert (
        after["fits"] is True and "GPU 2 (280 GiB configured) has 238 GiB free" in after["sentence"]
    )
    missing = assert_problem(h.client.get("/v1/fit", params={"model": "ghost"}), 404)
    assert missing["what_happened"] == "There is no model called ghost in the registry."
    assert_problem(h.client.get("/v1/fit"), 400)
    fresh = Harness(tmp_path)
    fresh.models_file.unlink()
    assert_problem(fresh.client.get("/v1/fit", params={"model": "x"}), 503)


def test_route_table_matches_the_contract(tmp_path: Path) -> None:
    contract = (REPO_ROOT / "docs" / "api-contract-round-2.md").read_text(encoding="utf-8")
    section = contract.split("## 3. model-manager", 1)[1].split("## 3b.", 1)[0]
    documented = set(re.findall(r"`(GET|POST|PUT) (/v1/[^\s`?]+)`", section))
    documented.discard(("PUT", "/v1/instances"))  # the gateway's route, named in the prose
    assert documented == {
        ("GET", "/v1/status"),
        ("POST", "/v1/reconcile"),
        ("POST", "/v1/swap"),
        ("POST", "/v1/rollback"),
        ("GET", "/v1/fit"),
        ("PUT", "/v1/roles"),
    }
    h = Harness(tmp_path)
    table = route_table(h.app)
    assert table == sorted(table)
    assert set(table) == documented | {("GET", "/health"), ("GET", "/metrics")}
    assert h.client.get("/metrics").status_code == 200
    assert h.client.get("/docs").status_code == 404


# --- roles and voters from the Models page (PUT /v1/roles, ADR-0018 part B) -------------------


def weights_for(h: Harness, *paths: str) -> None:
    for path in paths:
        target = h.models_file.parent / path
        target.mkdir(parents=True, exist_ok=True)
        (target / "SHA256SUMS").write_text("deadbeef  model.safetensors\n")


def test_put_roles_rewrites_the_registry_keeps_the_rest_and_reconciles(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.models_file.write_text(
        render_registry_yaml(PROFILE_REGISTRIES["quickstart"], header="Kept header\nline two"),
        encoding="utf-8",
    )
    weights_for(h, "deepseek-v4-flash", "qwen3.8-27b-fp8", "bge-m3", "bge-reranker-v2-m3")
    h.controller.reconcile()
    assert h.api.specs["vllm-planner"].labels["slas.model"] == "qwen3.8-27b-fp8"

    # planner → DeepSeek-V4 Flash (declared for it), coder stays, voters unchanged.
    response = h.client.put(
        "/v1/roles", json={"roles": {"planner": "deepseek-v4-flash"}}, headers=MANAGER.headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["roles"]["planner"] == "deepseek-v4-flash" and body["roles"]["coder"] == (
        "qwen3.8-27b-fp8"
    )
    assert body["voters"] == ["deepseek-v4-flash", "qwen3.8-27b-fp8"]
    assert body["sentence"].startswith(
        "Saved: planner → DeepSeek-V4 Flash. 2 voters from 2 families. To match the registry: "
    )
    assert "stop vllm-planner" in body["sentence"] and "start vllm-planner" in body["sentence"]
    assert h.api.specs["vllm-planner"].labels["slas.model"] == "deepseek-v4-flash", (
        "reconciled at once"
    )
    text = h.models_file.read_text()
    assert text.startswith("# Kept header\n# line two\nversion: 1\n"), "the header stays"
    assert "  planner: deepseek-v4-flash" in text and "  coder: qwen3.8-27b-fp8" in text
    assert "vram_gib: 180" in text and "context: 131072" in text, "every other field kept"
    assert not h.models_file.with_name("models.yaml.tmp").exists()
    assert h.events("registry.roles_changed")[0]["changes"] == ["planner → DeepSeek-V4 Flash"]

    # A role the model was not declared for: the assignment becomes the declaration.
    body = h.client.put(
        "/v1/roles", json={"roles": {"triage": "qwen3.8-27b-fp8"}}, headers=MANAGER.headers()
    ).json()
    qwen = next(m for m in body["models"] if m["id"] == "qwen3.8-27b-fp8")
    assert qwen["roles"] == ["coder", "planner", "triage"] and qwen["present"] is True

    # Voters: replaced as a whole; a null role is unassigned; nothing else moves.
    body = h.client.put(
        "/v1/roles",
        json={"roles": {"rerank": None}, "voters": ["qwen3.8-27b-fp8"]},
        headers=MANAGER.headers(),
    ).json()
    assert "rerank" not in body["roles"] and body["voters"] == ["qwen3.8-27b-fp8"]
    assert body["sentence"].startswith(
        "Saved: rerank is no longer served; voters: Qwen3.8-27B. 1 voter from 1 family."
    )
    assert "vllm-rerank" not in h.api.specs or h.api.states["vllm-rerank"] != "running"
    assert "vllm-voter-deepseek-v4-flash" not in h.api.specs or (
        h.api.states["vllm-voter-deepseek-v4-flash"] != "running"
    )


def test_put_roles_refusals_in_three_parts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    weights_for(h, "deepseek-v4-flash", "qwen3.8-27b-fp8", "bge-m3")

    def put(body: dict[str, Any], who: Identity = MANAGER) -> httpx.Response:
        response: httpx.Response = h.client.put("/v1/roles", json=body, headers=who.headers())
        return response

    assert_problem(put({"roles": {"coder": "qwen3.8-27b-fp8"}}, VIEWER), 403)
    assert_problem(h.client.put("/v1/roles", json={}), 401)
    assert assert_problem(put({}), 400)["what_happened"] == "Nothing to change."
    unknown_role = assert_problem(put({"roles": {"poet": "qwen3.8-27b-fp8"}}), 400)
    assert unknown_role["what_happened"] == "poet is not a role."
    unknown_model = assert_problem(put({"roles": {"coder": "ghost"}}), 400)
    assert unknown_model["what_happened"] == "There is no model called ghost in the registry."
    absent = assert_problem(put({"roles": {"rerank": "bge-reranker-v2-m3"}}), 400)
    assert absent["what_happened"] == "The weights of BGE Reranker v2 M3 are not here yet."
    assert "Add the model from this page" in absent["what_to_do"]
    twice = assert_problem(put({"voters": ["bge-m3", "bge-m3"]}), 400)
    assert twice["what_happened"] == "A model is listed twice among the voters."
    assert h.models_file.read_text() == render_registry_yaml(PROFILE_REGISTRIES["quickstart"]), (
        "a refusal leaves the file as it was"
    )
    # While a swap is in flight the registry is not rewritten under it.
    blocking = BlockingSmoke()
    busy = Harness(tmp_path, smoke=blocking)
    weights_for(busy, "deepseek-v4-flash", "qwen3.8-27b-fp8")
    busy.prober.everything = True
    busy.controller.reconcile()
    started = busy.client.post(
        "/v1/swap",
        json={"role": "planner", "candidate": "deepseek-v4-flash"},
        headers=MANAGER.headers(),
    )
    assert started.status_code == 200, started.text
    blocking.entered.wait(5)
    in_flight = assert_problem(
        busy.client.put(
            "/v1/roles", json={"roles": {"coder": "deepseek-v4-flash"}}, headers=MANAGER.headers()
        ),
        409,
    )
    assert in_flight["what_happened"] == "A swap of planner is in progress."
    blocking.release.set()
    busy.wait_swap()
    # A registry file that cannot be read is a 503, not a rewrite.
    broken = Harness(tmp_path)
    broken.models_file.write_text("models: [")
    assert_problem(
        broken.client.put("/v1/roles", json={"voters": []}, headers=MANAGER.headers()), 503
    )


# --- the smoke tester -----------------------------------------------------------------------


def test_http_smoke_tester_lists_the_model_then_asks_for_one_word() -> None:
    seen: list[tuple[str, Any]] = []
    answers: dict[str, Any] = {"models": 200, "chat": 200, "text": "ready"}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.host == "vllm-down":
            raise httpx.ConnectError("refused")
        if request.url.path == "/v1/models":
            if answers["models"] == 200:
                return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b-fp8"}]})
            return httpx.Response(int(answers["models"]), json={"data": []})
        assert request.url.path == "/v1/chat/completions"
        if answers["chat"] != 200:
            return httpx.Response(int(answers["chat"]), text="error")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": answers["text"]}, "finish_reason": "stop"}]},
        )

    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 0.25
        return clock["now"]

    tester = HttpSmokeTester(transport=httpx.MockTransport(handle), monotonic=monotonic)
    result = tester.smoke("vllm-coder-qwen3.8-27b-fp8")
    assert result.ok
    assert result.sentence == (
        "Smoke test passed: vllm-coder-qwen3.8-27b-fp8 answered a chat completion in 250 ms."
    )
    assert seen[0][0] == "/v1/models"
    body = seen[1][1]
    assert body["model"] == "qwen3.8-27b-fp8" and body["max_tokens"] == 8
    assert body["messages"] == [{"role": "user", "content": "Reply with the single word: ready"}]

    answers["text"] = ""
    assert tester.smoke("vllm-x").sentence == "vllm-x answered a chat completion without any text."
    answers["chat"] = 500
    assert tester.smoke("vllm-x").sentence == "vllm-x answered 500 to a chat completion."
    answers["models"] = 200
    answers["models"] = 404
    assert "did not name the model it serves" in tester.smoke("vllm-x").sentence
    assert tester.smoke("vllm-down").sentence == (
        "vllm-down did not answer the smoke test (ConnectError)."
    )
    tester.close()

    def empty(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{}]})
        return httpx.Response(200, text="not json")

    odd = HttpSmokeTester(transport=httpx.MockTransport(empty))
    assert not odd.smoke("vllm-odd").ok


# --- settings and the CLI ------------------------------------------------------------------


def test_settings_read_the_environment_with_the_contract_defaults() -> None:
    default = Settings.from_env({})
    assert default.runtime_socket == "/run/podman/podman.sock"
    assert str(default.models_file) == "/data/Models/models.yaml"
    assert default.gateway_url == "http://llm-gateway:8000"
    assert default.gpu_ids == (0, 1, 2, 3) and default.gpu_vram_gib == 270.0
    assert default.host_models_dir == "/AI/Agent/Models"
    assert default.inference_network == "slas_slas-inference"
    assert default.vllm_image == "" and default.vllm_shm_bytes == 16 * 1024**3
    assert default.reconcile_interval_s == 30.0 and default.model_start_timeout_s == 900.0
    assert default.bind == "0.0.0.0:8000"

    custom = Settings.from_env(
        {
            "SLAS_RUNTIME_SOCKET": "/var/run/docker.sock",
            "SLAS_MODELS_FILE": "/tmp/models.yaml",  # noqa: S108
            "SLAS_GATEWAY_URL": "http://gw:8000/",
            "SLAS_GPU_IDS": "4, 5,x,5",
            "SLAS_GPU_VRAM_GIB": "280",
            "SLAS_DATA_ROOT": "/srv/slas/",
            "SLAS_VLLM_IMAGE": IMAGE,
            "SLAS_VLLM_SHM": "8GiB",
            "SLAS_RECONCILE_INTERVAL_S": "nope",
            "SLAS_MODEL_START_TIMEOUT_S": "120",
            "SLAS_BIND": "127.0.0.1:9000",
        }
    )
    assert custom.gpu_ids == (4, 5) and custom.gpu_vram_gib == 280.0
    assert custom.host_models_dir == "/srv/slas/Models", "derived from SLAS_DATA_ROOT"
    assert custom.vllm_shm_bytes == 8 * 1024**3 and custom.reconcile_interval_s == 30.0
    assert custom.model_start_timeout_s == 120.0 and custom.bind == "127.0.0.1:9000"
    explicit = Settings.from_env({"SLAS_HOST_MODELS_DIR": "/mnt/models", "SLAS_GPU_IDS": ""})
    assert explicit.host_models_dir == "/mnt/models" and explicit.gpu_ids == (0, 1, 2, 3)
    assert parse_size("512m") == 512 * 1024**2 and parse_size("17179869184") == 17179869184
    assert parse_size("bad") == 16 * 1024**3 and parse_size("2t") == 2 * 1024**4
    assert parse_gpu_ids("") == [] and parse_gpu_ids("0,1,1,x") == [0, 1]


def test_cli_serve_builds_the_app_starts_the_loop_and_hands_off_to_uvicorn(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models.yaml"
    models.write_text(render_registry_yaml(PROFILE_REGISTRIES["quickstart"]), encoding="utf-8")
    served: list[tuple[FastAPI, str]] = []
    api = FakeContainerApi("docker")
    out: list[str] = []

    class Out:
        def write(self, text: str) -> int:
            out.append(text)
            return len(text)

    def runner(app: FastAPI, bind: str) -> None:
        served.append((app, bind))
        app.state.controller.stop_loop()

    code = cli.main(
        ["serve"],
        environ={
            "SLAS_MODELS_FILE": str(models),
            "SLAS_BIND": "127.0.0.1:8123",
            "SLAS_VLLM_IMAGE": IMAGE,
            "SLAS_RECONCILE_INTERVAL_S": "0.05",
        },
        runner=runner,
        api=api,
        stdout=Out(),  # type: ignore[arg-type]
    )
    assert code == 0 and len(served) == 1
    app, bind = served[0]
    assert bind == "127.0.0.1:8123" and app.state.settings.vllm_image == IMAGE
    assert out[0].startswith(
        "Serving the model manager on 127.0.0.1:8123; reconciling every 0.05 s"
    )
    assert app.state.controller.ticks >= 1, "the loop ran before the runner stopped it"
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["dance"])


def test_three_part_message_helper_is_used_for_sentences() -> None:
    message = ThreePartMessage("A.", "B.", "C.")
    assert Controller._sentence_of(message) == "A. C."
