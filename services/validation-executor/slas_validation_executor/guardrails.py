"""Guardrails (CLAUDE.md §10.2): `config/guardrails.yaml`, rendered from code.

    max_cycles_per_run 100 · min_settle_s 10 · min_ac_settle_s 30 · boot_timeout_s 900 ·
    consecutive_failure_abort 3 · exclusive lease · max_run_hours 72 ·
    requires_approval [ac_cycle, firmware_flash, secure_erase, bios_reset, raid_reconfigure]

The compiler checks a plan against them before it exists; the executor checks again at every
cycle. Hardware is physical and finite (§1.2), so the limits are not advisory.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from pydantic import Field, ValidationError

from slas_hal.primitives import APPROVAL_KINDS, approval_kind
from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Plan


class Guardrails(SlasModel):
    version: int = 1
    max_cycles_per_run: int = Field(default=100, ge=1, le=1000)
    min_settle_s: int = Field(default=10, ge=1)
    min_ac_settle_s: int = Field(default=30, ge=1)
    boot_timeout_s: int = Field(default=900, ge=30)
    consecutive_failure_abort: int = Field(default=3, ge=1)
    exclusive_lease: bool = True
    max_run_hours: int = Field(default=72, ge=1)
    requires_approval: list[str] = Field(default_factory=lambda: list(APPROVAL_KINDS))

    def settle_floor(self, kind: str) -> int:
        return self.min_ac_settle_s if kind == "ac" else self.min_settle_s

    def sentences(self) -> list[str]:
        return [
            f"At most {self.max_cycles_per_run} power cycles per run.",
            f"At least {self.min_settle_s} s settle after a warm or DC cycle, "
            f"{self.min_ac_settle_s} s after AC.",
            f"A boot that takes longer than {self.boot_timeout_s // 60} minutes counts as failed.",
            f"{self.consecutive_failure_abort} boot failures in a row abort the run.",
            "One run per target at a time." if self.exclusive_lease else "Targets may be shared.",
            f"A run stops after {self.max_run_hours} hours.",
            "Needs your approval every run: " + ", ".join(self.requires_approval) + ".",
        ]


class GuardrailError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def guardrails_from_mapping(data: object, *, source: str = "<memory>") -> Guardrails:
    try:
        return Guardrails.model_validate(data)
    except ValidationError as exc:
        raise GuardrailError(
            ThreePartMessage(
                f"The guardrails in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; every limit is a positive number.",
            )
        ) from exc


DEFAULT_GUARDRAILS: Final[dict[str, object]] = {
    "version": 1,
    "max_cycles_per_run": 100,
    "min_settle_s": 10,
    "min_ac_settle_s": 30,
    "boot_timeout_s": 900,
    "consecutive_failure_abort": 3,
    "exclusive_lease": True,
    "max_run_hours": 72,
    "requires_approval": list(APPROVAL_KINDS),
}

GUARDRAILS_FILE_HEADER: Final = (
    "Validation guardrails for SW Local Agent Service (CLAUDE.md §10.2).\n"
    "Rendered from slas_validation_executor.guardrails.DEFAULT_GUARDRAILS; a unit test keeps\n"
    "file and code in step. The plan compiler refuses a plan that exceeds them and the executor\n"
    "checks them again at every cycle. Hardware is physical and finite (§1.2)."
)


def default_guardrails() -> Guardrails:
    return guardrails_from_mapping(DEFAULT_GUARDRAILS, source="config/guardrails.yaml")


def render_guardrails_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    g = guardrails_from_mapping(data)
    lines = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    lines += [
        f"version: {g.version}",
        f"max_cycles_per_run: {g.max_cycles_per_run}",
        f"min_settle_s: {g.min_settle_s}",
        f"min_ac_settle_s: {g.min_ac_settle_s}",
        f"boot_timeout_s: {g.boot_timeout_s}",
        f"consecutive_failure_abort: {g.consecutive_failure_abort}",
        f"exclusive_lease: {json.dumps(g.exclusive_lease)}",
        f"max_run_hours: {g.max_run_hours}",
        f"requires_approval: [{', '.join(g.requires_approval)}]",
    ]
    return "\n".join(lines) + "\n"


def check_plan(plan: Plan, guardrails: Guardrails) -> list[str]:
    """Every way the plan breaks a guardrail, as sentences. Empty means it may run."""
    problems: list[str] = []
    cycles = [s for s in plan.steps if s.primitive == "power_cycle"]
    if len(cycles) > guardrails.max_cycles_per_run:
        problems.append(
            f"The plan has {len(cycles)} power cycles; "
            f"the limit is {guardrails.max_cycles_per_run} per run."
        )
    for step in cycles:
        kind = str(step.args.get("kind", "dc"))
        settle = int(step.args.get("settle_s", guardrails.settle_floor(kind)))
        floor = guardrails.settle_floor(kind)
        if settle < floor:
            problems.append(
                f"Step {step.n} settles {settle} s after a {kind.upper()} cycle; "
                f"the minimum is {floor} s."
            )
    for step in plan.steps:
        if step.primitive not in ("power_cycle",) and step.primitive.startswith(
            ("lease", "console", "baseline")
        ):
            continue
        approval = (
            approval_kind(step.primitive, step.args)
            if step.primitive in APPROVAL_KINDS or step.primitive == "power_cycle"
            else None
        )
        if approval in guardrails.requires_approval and step.risk != "destructive":
            problems.append(
                f"Step {step.n} ({step.title}) is {approval}, which needs approval, "
                "but is not marked destructive."
            )
    return problems
