"""The container runtime boundary and the vLLM container spec (CLAUDE.md §7, §12).

`--enable-prefix-caching` and `--guided-decoding-backend xgrammar` are mandatory on every
generate instance; the airgap environment is mandatory on every container (INV-1, INV-2).
The Podman driver implementing `ContainerRuntime` arrives with its approved dependency;
tests run against `FakeRuntime`.
"""

from __future__ import annotations

from typing import Final, Literal, Protocol

from pydantic import Field, model_validator

from slas_model_manager.registry import ModelEntry
from slas_schemas.common import SlasModel

AIRGAP_ENV: Final[dict[str, str]] = {
    "DO_NOT_TRACK": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "VLLM_NO_USAGE_STATS": "1",
}
MANDATORY_GENERATE_FLAGS: Final[tuple[str, ...]] = (
    "--enable-prefix-caching",
    "--guided-decoding-backend",
)
GENERATE_ROLES: Final[frozenset[str]] = frozenset({"coder", "planner", "triage"})
INFERENCE_NETWORK: Final = "slas-inference"

#: What a vLLM instance does: chat completions, embeddings, or reranking scores
#: (docs/api-contract-round-2.md §3: embed and rerank entries start with `--task embed` /
#: `--task score` and none of the generate flags).
VllmTask = Literal["generate", "embed", "score"]
TASK_FOR_ROLE: Final[dict[str, VllmTask]] = {"embed": "embed", "rerank": "score"}


def task_for_role(role: str | None) -> VllmTask:
    """The vLLM task an instance serving `role` runs; a voter (no role) generates."""
    if role is None:
        return "generate"
    return TASK_FOR_ROLE.get(role, "generate")


class ContainerSpec(SlasModel):
    name: str = Field(pattern=r"^vllm-[a-z0-9][a-z0-9._-]*$")
    image: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    network: str = INFERENCE_NETWORK
    gpu_ids: list[int] = Field(min_length=1)
    shm_size: str = "16g"
    model_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _airgapped_and_pinned(self) -> ContainerSpec:
        missing = [key for key, value in AIRGAP_ENV.items() if self.env.get(key) != value]
        if missing:
            raise ValueError(f"{self.name}: airgap environment missing {', '.join(missing)}")
        if self.image.endswith(":latest") or (
            "@sha256:" not in self.image and ":" not in self.image
        ):
            raise ValueError(f"{self.name}: image must be pinned by tag or digest, never latest")
        if self.network != INFERENCE_NETWORK:
            raise ValueError(f"{self.name}: inference containers join only {INFERENCE_NETWORK}")
        return self


class ContainerRef(SlasModel):
    id: str = Field(min_length=1)
    spec: ContainerSpec

    @property
    def name(self) -> str:
        return self.spec.name


class ContainerRuntime(Protocol):
    def start(self, spec: ContainerSpec) -> ContainerRef: ...

    def stop(self, ref: ContainerRef) -> None: ...

    def is_healthy(self, ref: ContainerRef) -> bool: ...

    def running(self) -> list[ContainerRef]: ...


def vllm_spec(
    entry: ModelEntry,
    *,
    name: str,
    gpu_ids: list[int],
    image: str,
    models_dir: str = "/data/Models",
    generate: bool = True,
    task: VllmTask = "generate",
) -> ContainerSpec:
    """The vLLM container for `entry` as `name` on `gpu_ids`.

    `task` decides the flags: a generate instance carries the mandatory prefix-caching and
    xgrammar flags (CLAUDE.md §7); an embed or score instance carries `--task` and none of
    them. `generate=False` keeps the older meaning "no generate flags" for callers that have
    no task to name.
    """
    argv = [
        "--model",
        f"{models_dir}/{entry.path}",
        "--served-model-name",
        entry.id,
        "--max-model-len",
        str(entry.context),
        "--tensor-parallel-size",
        str(len(gpu_ids)),
    ]
    if entry.quant == "fp8":
        argv += ["--quantization", "fp8"]
    elif entry.quant == "awq4":
        argv += ["--quantization", "awq_marlin"]
    if task != "generate":
        argv += ["--task", task]
    elif generate:
        argv += ["--enable-prefix-caching", "--guided-decoding-backend", "xgrammar"]
    return ContainerSpec(
        name=name,
        image=image,
        argv=argv,
        env={**AIRGAP_ENV, "CUDA_VISIBLE_DEVICES": ",".join(str(i) for i in gpu_ids)},
        gpu_ids=list(gpu_ids),
        model_id=entry.id,
    )


class FakeRuntime:
    """Containers as records; health is scripted per name."""

    def __init__(self) -> None:
        self._running: dict[str, ContainerRef] = {}
        self.health: dict[str, bool] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._counter = 0

    def set_health(self, name: str, healthy: bool) -> None:
        self.health[name] = healthy

    def start(self, spec: ContainerSpec) -> ContainerRef:
        if spec.name in self._running:
            raise RuntimeError(f"a container named {spec.name} is already running")
        self._counter += 1
        ref = ContainerRef(id=f"c{self._counter:03d}", spec=spec)
        self._running[spec.name] = ref
        self.started.append(spec.name)
        return ref

    def stop(self, ref: ContainerRef) -> None:
        self._running.pop(ref.name, None)
        self.stopped.append(ref.name)

    def is_healthy(self, ref: ContainerRef) -> bool:
        return ref.name in self._running and self.health.get(ref.name, True)

    def running(self) -> list[ContainerRef]:
        return list(self._running.values())
