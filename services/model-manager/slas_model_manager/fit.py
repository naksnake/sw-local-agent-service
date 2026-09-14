"""`slas model fit`: state VRAM need versus free in a sentence before any load (CLAUDE.md §7).

Quantisation rule (§7): FP8 on Hopper and Blackwell, AWQ 4-bit on Ada and Ampere, one BF16
reference kept for eval regression.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from slas_model_manager.registry import QUANT_LABELS, ModelEntry, Quant
from slas_schemas.common import SlasModel

GpuArch = Literal["hopper", "blackwell", "ada", "ampere", "other"]

#: Assumption: runtime overhead (CUDA context, activations, scheduler) on top of the
#: registry's vram_gib. Tuned once real instances run (P3 done-when on the reference host).
RUNTIME_OVERHEAD_GIB: Final = 2.0


class GpuFacts(SlasModel):
    index: int = Field(ge=0)
    name: str = Field(min_length=1)
    arch: GpuArch = "other"
    total_gib: float = Field(gt=0)
    free_gib: float = Field(ge=0)


class FitResult(SlasModel):
    fits: bool
    gpu_index: int | None
    needed_gib: float
    free_gib: float
    sentence: str


def needed_gib(entry: ModelEntry) -> float:
    return round(entry.vram_gib + RUNTIME_OVERHEAD_GIB, 1)


def recommended_quant(arch: GpuArch) -> Quant:
    return "fp8" if arch in ("hopper", "blackwell") else "awq4"


def fit(entry: ModelEntry, gpus: list[GpuFacts]) -> FitResult:
    needed = needed_gib(entry)
    if not gpus:
        return FitResult(
            fits=False,
            gpu_index=None,
            needed_gib=needed,
            free_gib=0.0,
            sentence=(
                f"{entry.label()} needs about {needed:g} GiB of GPU memory, but no GPU was found. "
                "Install the NVIDIA driver and run `slas doctor`."
            ),
        )
    best = max(gpus, key=lambda gpu: gpu.free_gib)
    if best.free_gib >= needed:
        sentence = (
            f"{entry.label()} needs about {needed:g} GiB of GPU memory; GPU {best.index} "
            f"({best.name}) has {best.free_gib:g} GiB free, so it fits."
        )
        return FitResult(
            fits=True,
            gpu_index=best.index,
            needed_gib=needed,
            free_gib=best.free_gib,
            sentence=sentence,
        )
    shortfall = round(needed - best.free_gib, 1)
    advice = "Free some GPU memory or pick a smaller quantisation."
    better = recommended_quant(best.arch)
    if entry.quant == "bf16":
        advice = f"Use the {QUANT_LABELS[better]} build; BF16 is only for eval regression."
    return FitResult(
        fits=False,
        gpu_index=best.index,
        needed_gib=needed,
        free_gib=best.free_gib,
        sentence=(
            f"{entry.label()} needs about {needed:g} GiB of GPU memory, but the most that is free "
            f"is {best.free_gib:g} GiB on GPU {best.index} ({best.name}), {shortfall:g} GiB short. "
            f"{advice}"
        ),
    )
