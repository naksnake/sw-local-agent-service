"""Blue/green model swap with a smoke test and a 24-hour rollback (CLAUDE.md §7, INV-9).

vLLM has no native hot-swap; the manager starts the candidate alongside the incumbent,
smoke-tests it, switches the gateway route, drains the incumbent, and keeps what it needs
to bring the incumbent back for a day. Every phase is a sentence the Models page shows.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import Field

from slas_llm_gateway.routing import RoleRouter
from slas_model_manager.registry import ModelEntry, instance_name
from slas_model_manager.runtime import ContainerRef, ContainerRuntime, vllm_spec
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

SwapPhase = Literal[
    "starting", "smoke_testing", "switching", "draining", "done", "failed", "rolled_back", "expired"
]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SmokeResult(SlasModel):
    ok: bool
    sentence: str = Field(min_length=1)


class SmokeTester(Protocol):
    def smoke(self, instance: str) -> SmokeResult: ...


class FakeSmokeTester:
    def __init__(self) -> None:
        self.results: dict[str, SmokeResult] = {}
        self.asked: list[str] = []

    def script(self, instance: str, ok: bool, sentence: str) -> None:
        self.results[instance] = SmokeResult(ok=ok, sentence=sentence)

    def smoke(self, instance: str) -> SmokeResult:
        self.asked.append(instance)
        return self.results.get(
            instance, SmokeResult(ok=True, sentence="Smoke test passed: 3 of 3 prompts answered.")
        )


class SwapRecord(SlasModel):
    role: str
    candidate: ModelEntry
    candidate_ref: ContainerRef | None = None
    incumbent: ModelEntry | None = None
    incumbent_ref: ContainerRef | None = None
    phase: SwapPhase
    started_at: datetime
    switched_at: datetime | None = None
    rollback_until: datetime | None = None
    progress: list[str] = Field(default_factory=list)

    def sentence(self) -> str:
        return self.progress[-1] if self.progress else f"Swapping {self.role}…"


class SwapError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class SwapManager:
    def __init__(
        self,
        *,
        runtime: ContainerRuntime,
        smoke: SmokeTester,
        router: RoleRouter,
        clock: Clock,
        image: str,
        rollback_window: timedelta = timedelta(hours=24),
    ) -> None:
        self.runtime = runtime
        self.smoke = smoke
        self.router = router
        self.clock = clock
        self.image = image
        self.rollback_window = rollback_window
        self.records: dict[str, SwapRecord] = {}
        self._serving: dict[str, tuple[ModelEntry, ContainerRef]] = {}

    # --- bookkeeping ------------------------------------------------------------------

    def serving(self, role: str) -> tuple[ModelEntry, ContainerRef] | None:
        return self._serving.get(role)

    def register_serving(self, role: str, entry: ModelEntry, ref: ContainerRef) -> None:
        """Tell the manager what already serves a role (after reconciliation or a restart)."""
        self._serving[role] = (entry, ref)

    def status(self, role: str) -> str:
        record = self.records.get(role)
        if record is None:
            current = self._serving.get(role)
            if current is None:
                return f"Nothing is serving {role}."
            return f"{current[0].display_name} is serving {role}."
        return record.sentence()

    # --- the swap -----------------------------------------------------------------------

    def swap(self, role: str, candidate: ModelEntry, *, gpu_ids: list[int]) -> SwapRecord:
        if role not in candidate.roles:
            raise SwapError(
                ThreePartMessage(
                    f"{candidate.display_name} cannot serve {role}.",
                    "Models/models.yaml declares it for "
                    f"{', '.join(candidate.roles) or 'no role'}.",
                    "Pick a model declared for this role, or add the role to its entry.",
                )
            )
        current = self._serving.get(role)
        now = self.clock.now()
        record = SwapRecord(
            role=role,
            candidate=candidate,
            incumbent=current[0] if current else None,
            incumbent_ref=current[1] if current else None,
            phase="starting",
            started_at=now,
        )
        self.records[role] = record
        incumbent_name = current[0].display_name if current else "nothing"
        record.progress.append(
            f"Starting {candidate.display_name} alongside the current {role} ({incumbent_name})…"
        )

        candidate_name = f"{instance_name(role)}-{candidate.id}"
        spec = vllm_spec(candidate, name=candidate_name, gpu_ids=gpu_ids, image=self.image)
        ref = self.runtime.start(spec)
        record.candidate_ref = ref
        if not self.runtime.is_healthy(ref):
            self.runtime.stop(ref)
            record.phase = "failed"
            record.progress.append(
                f"{candidate.display_name} did not become healthy; it was stopped and "
                f"{incumbent_name} keeps serving {role}. Run `slas logs {candidate_name}`."
            )
            return record

        record.phase = "smoke_testing"
        record.progress.append(f"Smoke-testing {candidate.display_name}…")
        result = self.smoke.smoke(candidate_name)
        if not result.ok:
            self.runtime.stop(ref)
            record.phase = "failed"
            record.progress.append(
                f"Smoke test failed: {result.sentence} {candidate.display_name} was stopped and "
                f"{incumbent_name} keeps serving {role}."
            )
            return record
        record.progress.append(result.sentence)

        record.phase = "switching"
        self.router.switch(role, candidate_name)
        record.switched_at = self.clock.now()
        record.rollback_until = record.switched_at + self.rollback_window
        self._serving[role] = (candidate, ref)

        record.phase = "draining"
        if current is not None:
            self.runtime.stop(current[1])
            record.progress.append(f"{current[0].display_name} was drained and stopped.")

        record.phase = "done"
        until = record.rollback_until.strftime("%Y-%m-%d %H:%M")
        record.progress.append(
            f"{role} is now served by {candidate.display_name}. "
            + (
                f"{current[0].display_name} can be restored until {until}."
                if current
                else "There was no previous model to roll back to."
            )
        )
        return record

    def rollback(self, role: str) -> SwapRecord:
        record = self.records.get(role)
        if record is None or record.phase != "done":
            raise SwapError(
                ThreePartMessage(
                    f"There is no completed swap of {role} to roll back.",
                    "Either nothing was swapped, or the swap failed and the previous model "
                    "kept serving.",
                    "Check the Models page for what is serving the role now.",
                )
            )
        if record.incumbent is None or record.incumbent_ref is None:
            raise SwapError(
                ThreePartMessage(
                    f"{role} had no previous model, so there is nothing to roll back to.",
                    f"{record.candidate.display_name} was the first model assigned to it.",
                    "Swap in another model instead.",
                )
            )
        now = self.clock.now()
        if record.rollback_until is not None and now > record.rollback_until:
            record.phase = "expired"
            raise SwapError(
                ThreePartMessage(
                    f"The rollback window for {role} closed at "
                    f"{record.rollback_until.strftime('%Y-%m-%d %H:%M')}.",
                    "Rollbacks are kept for 24 hours after a swap.",
                    f"Swap {record.incumbent.display_name} back in as a new swap instead.",
                )
            )
        restored = self.runtime.start(record.incumbent_ref.spec)
        if not self.runtime.is_healthy(restored):
            self.runtime.stop(restored)
            raise SwapError(
                ThreePartMessage(
                    f"{record.incumbent.display_name} did not come back healthy.",
                    "The previous container could not start again.",
                    f"{record.candidate.display_name} keeps serving {role}; run "
                    f"`slas logs {record.incumbent_ref.name}`.",
                )
            )
        self.router.switch(role, record.incumbent_ref.name)
        if record.candidate_ref is not None:
            self.runtime.stop(record.candidate_ref)
        self._serving[role] = (record.incumbent, restored)
        record.phase = "rolled_back"
        record.progress.append(
            f"Rolled back: {record.incumbent.display_name} is serving {role} again and "
            f"{record.candidate.display_name} was stopped."
        )
        return record
