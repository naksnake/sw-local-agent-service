"""The container runtime boundary and the vLLM container spec (CLAUDE.md §7, §12).

`--enable-prefix-caching` and structured outputs with the xgrammar backend are mandatory on
every generate instance (CLAUDE.md §7); the airgap environment is mandatory on every container
(INV-1, INV-2). The backend travels as `--structured-outputs-config {"backend": "xgrammar"}`:
vLLM removed the older `--guided-decoding-backend` flag, and an instance given it exits at
start with "unrecognized arguments" before it loads a single weight.
The Podman driver implementing `ContainerRuntime` arrives with its approved dependency;
tests run against `FakeRuntime`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
#: The JSON vLLM's `--structured-outputs-config` takes (a fixed string, never built from
#: input); `backend` names the grammar engine every generate instance must use.
STRUCTURED_OUTPUTS_CONFIG: Final = '{"backend": "xgrammar"}'
MANDATORY_GENERATE_FLAGS: Final[tuple[str, ...]] = (
    "--enable-prefix-caching",
    "--structured-outputs-config",
)
GENERATE_ROLES: Final[frozenset[str]] = frozenset({"coder", "planner", "triage"})
INFERENCE_NETWORK: Final = "slas-inference"

#: What a vLLM instance does: chat completions, embeddings, or reranking scores
#: (docs/api-contract-round-2.md §3). vLLM removed `--task`; a pooling instance is asked for
#: with `--runner pooling`, and an embedding model is converted with `--convert embed`. A
#: reranker (a sequence-classification cross-encoder) scores as it is, so `score` carries
#: the runner alone. Neither carries the generate flags.
VllmTask = Literal["generate", "embed", "score"]
TASK_FOR_ROLE: Final[dict[str, VllmTask]] = {"embed": "embed", "rerank": "score"}
POOLING_FLAGS: Final[dict[VllmTask, tuple[str, ...]]] = {
    "embed": ("--runner", "pooling", "--convert", "embed"),
    "score": ("--runner", "pooling"),
}


#: The smallest context the manager falls back to when a model's stated context does not fit
#: the GPU's KV cache; below this a coding task cannot hold a plan and a diff.
MIN_CONTEXT: Final = 8192
#: What vLLM logs before exiting when `--max-model-len` exceeds what the KV cache can hold
#: after the weights are loaded. It exits within a minute or two of starting; with
#: `restart: unless-stopped` that is an endless loop unless the context shrinks.
KV_CACHE_TOO_SMALL: Final[tuple[str, ...]] = (
    # Every vLLM version's wording ends with this advice; the two before it are the
    # older and the newer statement of the problem, the last the cache-block variant.
    "decreasing `max_model_len`",
    "larger than the maximum number of tokens that can be stored in KV cache",
    "larger than the available KV cache memory",
    "No available memory for the cache blocks",
)
#: Newer vLLM says how long a context would fit; the manager never goes above that.
KV_CACHE_ESTIMATE: Final = re.compile(r"estimated maximum model length is (\d+)")
#: vLLM's flag for eager execution: no torch.compile, no CUDA graph capture. Slower, but it
#: sidesteps a compiler or graph-capture crash on a GPU the build does not know well.
EAGER_FLAG: Final = "--enforce-eager"


def context_of(argv: Sequence[str]) -> int | None:
    """The `--max-model-len` a container was created with, or None."""
    for i, flag in enumerate(argv[:-1]):
        value = str(argv[i + 1])
        if flag == "--max-model-len" and value.isdigit():
            return int(value)
    return None


def kv_cache_too_small(lines: Sequence[str]) -> bool:
    return any(marker in line for line in lines for marker in KV_CACHE_TOO_SMALL)


def kv_cache_estimate(lines: Sequence[str]) -> int | None:
    """The context vLLM itself estimated would fit, rounded down to 1024 tokens, or None."""
    for line in reversed(lines):
        found = KV_CACHE_ESTIMATE.search(line)
        if found:
            return int(found.group(1)) // 1024 * 1024
    return None


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

    def stop(self, ref: ContainerRef, *, timeout_s: int = 30) -> None: ...

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
    max_model_len: int | None = None,
    extra_args: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> ContainerSpec:
    """The vLLM container for `entry` as `name` on `gpu_ids`.

    `task` decides the flags: a generate instance carries the mandatory prefix-caching and
    xgrammar flags (CLAUDE.md §7); an embed or score instance carries the pooling runner
    flags and none of them. `generate=False` keeps the older meaning "no generate flags" for
    callers that have no task to name. `max_model_len` lowers the registry's context when
    the GPU's KV cache could not hold it (the registry's value stays the cap).
    """
    # `extra_args` and `extra_env` are the remedies the manager learnt from this instance's
    # crashes (controller.REMEDIES): appended last, so they win over the defaults.
    context = entry.context if max_model_len is None else min(entry.context, max_model_len)
    # The model is `vllm serve`'s positional argument; `--model` is deprecated there.
    argv = [
        f"{models_dir}/{entry.path}",
        "--served-model-name",
        entry.id,
        "--max-model-len",
        str(context),
        "--tensor-parallel-size",
        str(len(gpu_ids)),
    ]
    if entry.quant == "fp8":
        argv += ["--quantization", "fp8"]
    elif entry.quant == "awq4":
        argv += ["--quantization", "awq_marlin"]
    if task != "generate":
        argv += list(POOLING_FLAGS[task])
    elif generate:
        argv += [
            "--enable-prefix-caching",
            "--structured-outputs-config",
            STRUCTURED_OUTPUTS_CONFIG,
        ]
    argv += list(extra_args)
    return ContainerSpec(
        name=name,
        image=image,
        argv=argv,
        env={
            **AIRGAP_ENV,
            "CUDA_VISIBLE_DEVICES": ",".join(str(i) for i in gpu_ids),
            **dict(extra_env or {}),
        },
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

    def stop(self, ref: ContainerRef, *, timeout_s: int = 30) -> None:
        self._running.pop(ref.name, None)
        self.stopped.append(ref.name)

    def is_healthy(self, ref: ContainerRef) -> bool:
        return ref.name in self._running and self.health.get(ref.name, True)

    def running(self) -> list[ContainerRef]:
        return list(self._running.values())
