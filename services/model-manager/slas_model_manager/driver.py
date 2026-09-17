"""`ContainerApiRuntime`: the `ContainerRuntime` over the runtime socket (ADR-0015, contract §3).

Translates the manager's `ContainerSpec` into a `slas_container.CreateSpec`: the instance
name is the container name and its alias on the inference network (so `http://vllm-coder:8000`
resolves for the gateway and Prometheus), the host models directory is mounted read-only at
`/data/Models`, the GPUs come from the placement and `CUDA_VISIBLE_DEVICES` is renumbered
`0..n-1` because the device request already selects them. Labels carry what a restarted
manager needs to know its containers again: kind, instance, model, GPUs and argv.

Health is two facts: the container runs and `GET http://<instance>:8000/health` answers 200.
The prober is injectable; `HttpProber` is the real one (httpx, two seconds).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Final, Protocol

import httpx

from slas_container import ContainerError, ContainerInfo, CreateSpec, EngineInfo, Mount
from slas_model_manager.runtime import AIRGAP_ENV, ContainerRef, ContainerRuntime, ContainerSpec

KIND_LABEL: Final = "slas.kind"
INSTANCE_LABEL: Final = "slas.instance"
MODEL_LABEL: Final = "slas.model"
GPU_LABEL: Final = "slas.gpu_ids"
ARGV_LABEL: Final = "slas.argv"
VLLM_KIND: Final = "vllm"
VLLM_LABEL_FILTER: Final = f"{KIND_LABEL}={VLLM_KIND}"

VLLM_PORT: Final = 8000
MODELS_MOUNT: Final = "/data/Models"
DEFAULT_SHM_BYTES: Final = 16 * 1024**3
HEALTH_TIMEOUT_S: Final = 2.0
LOG_TAIL_LINES: Final = 40

#: True when the instance at this base URL answers its health check.
Prober = Callable[[str], bool]
#: Placement GPU ids → the device ids the runtime knows (identity unless the host renumbers).
DeviceMapper = Callable[[Sequence[int]], list[int]]


def instance_url(name: str) -> str:
    return f"http://{name}:{VLLM_PORT}"


def identity_devices(gpu_ids: Sequence[int]) -> list[int]:
    return list(gpu_ids)


class RuntimeApi(Protocol):
    """The part of `slas_container.ContainerApi` the driver uses; the fake has the same."""

    def ping(self) -> EngineInfo: ...

    def list(self, *, label: str | None = None, all_states: bool = True) -> list[ContainerInfo]: ...

    def inspect(self, name: str) -> ContainerInfo | None: ...

    def create(self, spec: CreateSpec) -> str: ...

    def start(self, name: str) -> None: ...

    def stop(self, name: str, *, timeout_s: int = 30) -> None: ...

    def remove(self, name: str, *, force: bool = True) -> None: ...

    def logs(self, name: str, *, tail: int = 200) -> str: ...


class ManagedRuntime(ContainerRuntime, Protocol):
    """`ContainerRuntime` plus what the controller needs to explain a container's state."""

    def state_of(self, name: str) -> ContainerInfo | None: ...

    def last_log_lines(self, name: str, n: int = LOG_TAIL_LINES) -> list[str]: ...


class HttpProber:
    """`GET <url>/health` with a short timeout; anything but 200 is unhealthy."""

    def __init__(
        self, *, timeout_s: float = HEALTH_TIMEOUT_S, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def __call__(self, url: str) -> bool:
        try:
            return self._client.get(f"{url}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()


def vllm_labels(spec: ContainerSpec) -> dict[str, str]:
    return {
        KIND_LABEL: VLLM_KIND,
        INSTANCE_LABEL: spec.name,
        MODEL_LABEL: spec.model_id,
        GPU_LABEL: ",".join(str(i) for i in spec.gpu_ids),
        ARGV_LABEL: json.dumps(spec.argv),
    }


class ContainerApiRuntime:
    def __init__(
        self,
        api: RuntimeApi,
        *,
        image: str,
        host_models_dir: str,
        network: str,
        shm_bytes: int = DEFAULT_SHM_BYTES,
        gpu_ids_to_devices: DeviceMapper = identity_devices,
        prober: Prober | None = None,
    ) -> None:
        self.api = api
        self.image = image
        self.host_models_dir = host_models_dir
        self.network = network
        self.shm_bytes = shm_bytes
        self.gpu_ids_to_devices = gpu_ids_to_devices
        self.prober: Prober = prober if prober is not None else HttpProber()
        self._specs: dict[str, ContainerSpec] = {}

    # --- translation ---------------------------------------------------------------------

    def create_spec(self, spec: ContainerSpec) -> CreateSpec:
        devices = self.gpu_ids_to_devices(spec.gpu_ids)
        env = dict(spec.env)
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(len(devices)))
        return CreateSpec(
            name=spec.name,
            image=spec.image,
            argv=list(spec.argv),
            env=env,
            network=self.network,
            network_aliases=[spec.name],
            mounts=[Mount(source=self.host_models_dir, target=MODELS_MOUNT, read_only=True)],
            shm_size_bytes=self.shm_bytes,
            ipc_host=True,
            gpu_ids=devices,
            labels=vllm_labels(spec),
            restart="unless-stopped",
        )

    def _spec_from(self, info: ContainerInfo) -> ContainerSpec | None:
        known = self._specs.get(info.name)
        if known is not None:
            return known
        labels = info.labels
        if labels.get(KIND_LABEL) != VLLM_KIND or not labels.get(MODEL_LABEL):
            return None
        try:
            gpu_ids = [int(part) for part in labels.get(GPU_LABEL, "").split(",") if part]
            argv = json.loads(labels.get(ARGV_LABEL, "[]"))
        except ValueError:
            return None
        if not gpu_ids or not isinstance(argv, list) or not argv:
            return None
        # A listing may name the image by its id; the spec insists on a pinned reference.
        image = info.image if ":" in info.image else self.image
        try:
            spec = ContainerSpec(
                name=labels.get(INSTANCE_LABEL) or info.name,
                image=image,
                argv=[str(part) for part in argv],
                env=self._airgap_env(gpu_ids),
                gpu_ids=gpu_ids,
                model_id=labels[MODEL_LABEL],
            )
        except ValueError:
            return None
        self._specs[spec.name] = spec
        return spec

    @staticmethod
    def _airgap_env(gpu_ids: Sequence[int]) -> dict[str, str]:
        return {**AIRGAP_ENV, "CUDA_VISIBLE_DEVICES": ",".join(str(i) for i in gpu_ids)}

    # --- ContainerRuntime ----------------------------------------------------------------

    def start(self, spec: ContainerSpec) -> ContainerRef:
        create = self.create_spec(spec)
        try:
            container_id = self.api.create(create)
        except ContainerError as exc:
            if exc.status != 409:
                raise
            # A container of that name is left over (stopped, crashed, an older image):
            # replace it, since the registry is the one truth of what should run.
            self.api.remove(spec.name, force=True)
            container_id = self.api.create(create)
        self.api.start(spec.name)
        self._specs[spec.name] = spec
        return ContainerRef(id=container_id or f"name:{spec.name}", spec=spec)

    def stop(self, ref: ContainerRef) -> None:
        self.api.stop(ref.name)
        self.api.remove(ref.name, force=True)
        self._specs.pop(ref.name, None)

    def is_healthy(self, ref: ContainerRef) -> bool:
        info = self.api.inspect(ref.name)
        if info is None or not info.running:
            return False
        return self.prober(instance_url(ref.name))

    def running(self) -> list[ContainerRef]:
        """Every vLLM container the runtime knows, whatever its state.

        The controller reads each one's state afterwards: a crashed container must stay
        visible so its last log lines can be reported, not be silently recreated.
        """
        refs: list[ContainerRef] = []
        for info in self.api.list(label=VLLM_LABEL_FILTER, all_states=True):
            spec = self._spec_from(info)
            if spec is not None:
                refs.append(ContainerRef(id=info.id or f"name:{info.name}", spec=spec))
        return refs

    # --- ManagedRuntime ------------------------------------------------------------------

    def state_of(self, name: str) -> ContainerInfo | None:
        return self.api.inspect(name)

    def last_log_lines(self, name: str, n: int = LOG_TAIL_LINES) -> list[str]:
        try:
            text = self.api.logs(name, tail=n)
        except ContainerError:
            return []
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        return lines[-n:]


class PatientRuntime:
    """The same runtime, but `is_healthy` waits for a model to finish loading.

    The swap manager asks once after `start()`; a vLLM instance takes minutes to load its
    weights, so the swap's runtime polls until the instance answers, the container dies, or
    `timeout_s` passes. Reconciliation keeps using the impatient runtime: it runs every tick.
    """

    def __init__(
        self,
        inner: ManagedRuntime,
        *,
        timeout_s: float,
        interval_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.timeout_s = timeout_s
        self.interval_s = interval_s
        self._sleep = sleep
        self._monotonic = monotonic

    def start(self, spec: ContainerSpec) -> ContainerRef:
        return self.inner.start(spec)

    def stop(self, ref: ContainerRef) -> None:
        self.inner.stop(ref)

    def running(self) -> list[ContainerRef]:
        return self.inner.running()

    def state_of(self, name: str) -> ContainerInfo | None:
        return self.inner.state_of(name)

    def last_log_lines(self, name: str, n: int = LOG_TAIL_LINES) -> list[str]:
        return self.inner.last_log_lines(name, n)

    def is_healthy(self, ref: ContainerRef) -> bool:
        started = self._monotonic()
        while True:
            if self.inner.is_healthy(ref):
                return True
            info = self.inner.state_of(ref.name)
            if info is None or not info.running:
                return False
            if self._monotonic() - started >= self.timeout_s:
                return False
            self._sleep(self.interval_s)
