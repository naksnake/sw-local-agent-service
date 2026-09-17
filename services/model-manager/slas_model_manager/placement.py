"""Greedy GPU placement of the desired instances (docs/api-contract-round-2.md §3).

Deterministic: role instances before voters, the biggest first, names as the tie-break.
Each instance needs `fit.needed_gib` (its `vram_gib` plus the runtime overhead) against a
per-GPU capacity (`SLAS_GPU_VRAM_GIB`). One that fits on a GPU goes to the GPU with the most
memory left (lowest id on a tie), so the load spreads. One that exceeds a GPU takes several
in tensor parallel, each holding an equal share. Instances that already run are pinned to
their GPUs and are never moved. What does not fit is reported with a sentence and is never
started.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import Field

from slas_model_manager.fit import needed_gib
from slas_model_manager.registry import Registry, instance_name, voter_instance_name
from slas_schemas.common import SlasModel

DEFAULT_GPU_VRAM_GIB = 270.0  # an HGX B300 GPU has 288 GB HBM3e; 18 GB headroom for the driver

# TODO(SLAS-MODELS): vLLM's tensor parallel size must divide the model's attention heads;
# the registry has no field for it yet (CLAUDE.md §15, decision 14), so the GPU count here is
# the smallest that holds the memory. Add `tensor_parallel` to ModelEntry with that ADR.


class Candidate(SlasModel):
    instance: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    kind: Literal["role", "voter"]
    role: str | None = None
    needed_gib: float = Field(gt=0)
    label: str = Field(min_length=1)


class Placed(SlasModel):
    instance: str
    model_id: str
    gpu_ids: list[int] = Field(min_length=1)


class Unplaced(SlasModel):
    instance: str
    model_id: str
    sentence: str = Field(min_length=1)


class PlacementResult(SlasModel):
    placed: list[Placed] = Field(default_factory=list)
    unplaced: list[Unplaced] = Field(default_factory=list)
    free_gib: dict[int, float] = Field(default_factory=dict, description="GPU id → GiB left")
    occupants: dict[int, list[str]] = Field(default_factory=dict, description="GPU id → instances")

    def gpu_ids_for(self, instance: str) -> list[int] | None:
        for item in self.placed:
            if item.instance == instance:
                return list(item.gpu_ids)
        return None

    def sentence_for(self, instance: str) -> str | None:
        for item in self.unplaced:
            if item.instance == instance:
                return item.sentence
        return None


def candidates(registry: Registry, only: Iterable[str] | None = None) -> list[Candidate]:
    """Every desired instance in placement order; `only` narrows it to those names."""
    wanted = set(only) if only is not None else None
    out: list[Candidate] = []
    for role, model_id in registry.roles.items():
        entry = registry.model(model_id)
        out.append(
            Candidate(
                instance=instance_name(role),
                model_id=model_id,
                kind="role",
                role=role,
                needed_gib=needed_gib(entry),
                label=entry.label(),
            )
        )
    for model_id in registry.voters:
        entry = registry.model(model_id)
        out.append(
            Candidate(
                instance=voter_instance_name(model_id),
                model_id=model_id,
                kind="voter",
                needed_gib=needed_gib(entry),
                label=entry.label(),
            )
        )
    if wanted is not None:
        out = [c for c in out if c.instance in wanted]
    return sorted(out, key=lambda c: (c.kind != "role", -c.needed_gib, c.instance))


def place(
    wanted: Sequence[Candidate],
    *,
    gpu_ids: Sequence[int],
    capacity_gib: float = DEFAULT_GPU_VRAM_GIB,
    pinned: Mapping[str, tuple[Sequence[int], float]] | None = None,
) -> PlacementResult:
    """Place `wanted` on `gpu_ids`; `pinned` is instance → (its GPUs, its needed GiB)."""
    free: dict[int, float] = dict.fromkeys(sorted(set(gpu_ids)), capacity_gib)
    occupants: dict[int, list[str]] = {gpu: [] for gpu in free}
    for name, (ids, needed) in sorted((pinned or {}).items()):
        share = needed / max(len(ids), 1)
        for gpu in ids:
            free.setdefault(gpu, 0.0)
            free[gpu] -= share
            occupants.setdefault(gpu, []).append(name)
    result = PlacementResult()
    for candidate in wanted:
        chosen = _choose(candidate.needed_gib, free, capacity_gib)
        if chosen is None:
            result.unplaced.append(
                Unplaced(
                    instance=candidate.instance,
                    model_id=candidate.model_id,
                    sentence=_no_room_sentence(candidate, free, capacity_gib),
                )
            )
            continue
        share = candidate.needed_gib / len(chosen)
        for gpu in chosen:
            free[gpu] -= share
            occupants[gpu].append(candidate.instance)
        result.placed.append(
            Placed(instance=candidate.instance, model_id=candidate.model_id, gpu_ids=chosen)
        )
    result.free_gib = {gpu: round(left, 1) for gpu, left in sorted(free.items())}
    result.occupants = {gpu: list(names) for gpu, names in sorted(occupants.items())}
    return result


def gpu_count_for(needed: float, capacity_gib: float) -> int:
    return max(1, math.ceil(needed / capacity_gib)) if capacity_gib > 0 else 1


def _choose(needed: float, free: Mapping[int, float], capacity_gib: float) -> list[int] | None:
    if not free:
        return None
    count = gpu_count_for(needed, capacity_gib)
    share = needed / count
    # The GPUs with the most memory left first, lowest id on a tie: the load spreads.
    ranked = sorted(free.items(), key=lambda item: (-item[1], item[0]))
    roomy = [gpu for gpu, left in ranked if left >= share]
    if len(roomy) < count:
        return None
    return sorted(roomy[:count])


def _no_room_sentence(candidate: Candidate, free: Mapping[int, float], capacity_gib: float) -> str:
    needed = candidate.needed_gib
    fix = (
        "Add a GPU, raise SLAS_GPU_VRAM_GIB in .env if it understates the cards, or pick a "
        "smaller build on the Models page."
    )
    if not free:
        return (
            f"{candidate.label} needs about {needed:g} GiB of GPU memory, but SLAS_GPU_IDS "
            f"names no GPU; {candidate.instance} was not started. Set SLAS_GPU_IDS in .env."
        )
    count = gpu_count_for(needed, capacity_gib)
    if count == 1:
        gpu, left = max(free.items(), key=lambda item: (item[1], -item[0]))
        return (
            f"{candidate.label} needs about {needed:g} GiB of GPU memory, but the most left on "
            f"one GPU after the other models is {max(left, 0):g} GiB (GPU {gpu}); "
            f"{candidate.instance} was not started. {fix}"
        )
    share = needed / count
    roomy = sum(1 for left in free.values() if left >= share)
    return (
        f"{candidate.label} needs about {needed:g} GiB of GPU memory, which is {count} GPUs of "
        f"{capacity_gib:g} GiB in tensor parallel, but only {roomy} of {len(free)} GPUs have "
        f"{share:g} GiB left; {candidate.instance} was not started. {fix}"
    )
