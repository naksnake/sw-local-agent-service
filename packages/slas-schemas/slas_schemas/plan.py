"""Plans and steps: what a deterministic executor performs (CLAUDE.md §5.1, INV-3, INV-7).

A model may compile a plan; only code runs it. Every step names a primitive from a
whitelist (§6.2 for skills, `plans/primitives` for Validation and Factory) and carries a risk
class; a destructive step needs a per-run human approval before the kernel acts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import Field, model_validator

from slas_schemas.common import SlasModel

Risk = Literal["safe", "caution", "destructive"]

#: Hard cap on steps after loop expansion (CLAUDE.md §6.1).
MAX_STEPS: Final = 200


class Step(SlasModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    n: int = Field(ge=1)
    primitive: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    # Arguments are primitive-specific JSON; the executor validates them at dispatch.
    args: dict[str, Any] = Field(default_factory=dict)
    risk: Risk = "safe"
    when: str | None = None

    @property
    def needs_approval(self) -> bool:
        return self.risk == "destructive"


class Plan(SlasModel):
    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    created_at: datetime

    @model_validator(mode="after")
    def _steps_are_numbered_and_unique(self) -> Plan:
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"step ids must be unique; repeated: {', '.join(duplicates)}")
        for index, step in enumerate(self.steps, start=1):
            if step.n != index:
                raise ValueError(f"step {step.id} is numbered {step.n} but is the {index}th step")
        return self

    @property
    def destructive(self) -> bool:
        return any(step.needs_approval for step in self.steps)

    def destructive_steps(self) -> list[Step]:
        return [step for step in self.steps if step.needs_approval]

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def sentence(self) -> str:
        count = len(self.steps)
        noun = "step" if count == 1 else "steps"
        destructive = self.destructive_steps()
        if not destructive:
            return f"{count} {noun}, none destructive."
        names = ", ".join(step.title for step in destructive)
        verb = "needs" if len(destructive) == 1 else "need"
        return f"{count} {noun}; {len(destructive)} {verb} your approval: {names}."
