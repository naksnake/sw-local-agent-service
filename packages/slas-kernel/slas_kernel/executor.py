"""The deterministic executor boundary (INV-3): code performs steps, never a model.

Phase 2 ships only the `fake` primitive, which returns a scripted observation. Real
primitives arrive with the sandbox (P6), `slas_hal` (P7) and the screen driver (P4), each
behind this same protocol.
"""

from __future__ import annotations

from typing import Protocol

from slas_schemas.plan import Step
from slas_schemas.ticket import Observation


class UnknownPrimitiveError(ValueError):
    def __init__(self, step: Step) -> None:
        super().__init__(
            f"Step {step.id} uses the primitive {step.primitive!r}, which this executor does "
            "not know."
        )
        self.step = step


class Executor(Protocol):
    def execute(self, step: Step) -> Observation: ...


class FakeExecutor:
    """Performs `fake` steps: args `stdout`, `stderr`, `exit_code` become the observation.

    `fail_before_observing` names steps whose execution raises — the way a killed process
    looks to the journal: an intent with no observation.
    """

    def __init__(self, fail_before_observing: set[str] | None = None) -> None:
        self.executed: list[str] = []
        self.fail_before_observing = set(fail_before_observing or ())

    def execute(self, step: Step) -> Observation:
        if step.primitive != "fake":
            raise UnknownPrimitiveError(step)
        self.executed.append(step.id)
        if step.id in self.fail_before_observing:
            self.fail_before_observing.discard(step.id)
            raise RuntimeError(f"simulated crash while performing {step.id}")
        exit_code = int(step.args.get("exit_code", 0))
        stdout = str(step.args.get("stdout", ""))
        stderr = str(step.args.get("stderr", ""))
        return Observation(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            summary=f"{step.title}: exit {exit_code}.",
        )
