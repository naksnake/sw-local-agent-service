"""Every job is a ticket (CLAUDE.md §5.4).

    T-<agent>-<seq>   Open → Planned → Approved → Running → Analysing → Needs review → Done | Failed

Created at ingest, never by hand. The state machine lives here so the kernel, the API and
the UI agree on it; the kernel is the only writer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import Field

from slas_schemas.common import AgentName, SlasModel
from slas_schemas.finding import Finding
from slas_schemas.ids import TicketId
from slas_schemas.job import Job
from slas_schemas.plan import Plan
from slas_schemas.vote import Vote


class TicketState(StrEnum):
    OPEN = "Open"
    PLANNED = "Planned"
    APPROVED = "Approved"
    RUNNING = "Running"
    ANALYSING = "Analysing"
    NEEDS_REVIEW = "Needs review"
    DONE = "Done"
    FAILED = "Failed"


TRANSITIONS: Final[dict[TicketState, frozenset[TicketState]]] = {
    TicketState.OPEN: frozenset({TicketState.PLANNED, TicketState.FAILED}),
    TicketState.PLANNED: frozenset({TicketState.APPROVED, TicketState.FAILED}),
    TicketState.APPROVED: frozenset({TicketState.RUNNING, TicketState.FAILED}),
    TicketState.RUNNING: frozenset({TicketState.ANALYSING, TicketState.FAILED}),
    TicketState.ANALYSING: frozenset(
        {TicketState.NEEDS_REVIEW, TicketState.DONE, TicketState.FAILED}
    ),
    TicketState.NEEDS_REVIEW: frozenset({TicketState.DONE, TicketState.FAILED}),
    TicketState.DONE: frozenset(),
    TicketState.FAILED: frozenset(),
}

TERMINAL_STATES: Final[frozenset[TicketState]] = frozenset({TicketState.DONE, TicketState.FAILED})


def can_transition(current: TicketState, target: TicketState) -> bool:
    return target in TRANSITIONS[current]


class IllegalTransitionError(ValueError):
    def __init__(self, ticket_id: str, current: TicketState, target: TicketState) -> None:
        allowed = ", ".join(sorted(state.value for state in TRANSITIONS[current])) or "nothing"
        super().__init__(
            f"{ticket_id} is {current.value} and cannot become {target.value}; "
            f"from {current.value} it can only become {allowed}."
        )
        self.ticket_id = ticket_id
        self.current = current
        self.target = target


class StateChange(SlasModel):
    from_state: TicketState
    to_state: TicketState
    at: datetime
    reason: str = Field(min_length=1)


class Approval(SlasModel):
    """A per-run human decision on one destructive step (INV-7)."""

    step_id: str
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    approved: bool | None = None
    note: str = ""

    @property
    def pending(self) -> bool:
        return self.approved is None


class Export(SlasModel):
    kind: Literal["zip", "sop", "report", "bundle"]
    path: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Observation(SlasModel):
    """What the executor saw after performing one step.

    `votes` and `exports` let a kernel-side executor hand a cross-check verdict or an
    artifact back; the kernel copies them onto the ticket (agents never write tickets).
    """

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    screenshots: list[str] = Field(default_factory=list)
    summary: str = ""
    votes: list[Vote] = Field(default_factory=list)
    exports: list[Export] = Field(default_factory=list)
    #: Findings a VERIFY produced; the kernel deduplicates them by fingerprint onto the ticket.
    findings: list[Finding] = Field(default_factory=list)


class StepVerdict(SlasModel):
    """The agent's judgement of one observation (`Agent.verify`)."""

    outcome: Literal["ok", "fail", "retry"]
    sentence: str = Field(min_length=1)


StepStatus = Literal["pending", "running", "done", "failed", "skipped"]


class StepRecord(SlasModel):
    step_id: str
    n: int = Field(ge=1)
    title: str
    status: StepStatus = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    observation: Observation | None = None
    verdict: StepVerdict | None = None


class LogBundle(SlasModel):
    stdout_path: str | None = None
    stderr_path: str | None = None
    console_path: str | None = None
    screenshots: list[str] = Field(default_factory=list)
    line_counts: dict[str, int] = Field(default_factory=dict)


class Rca(SlasModel):
    cause: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    uncertain: bool = True
    fingerprint: str | None = None


class SopRefs(SlasModel):
    """Both renderings and the structured source, always together (INV-13)."""

    en: str
    zh: str
    data: str


class Ticket(SlasModel):
    id: TicketId
    agent: AgentName
    user: str
    title: str
    state: TicketState = TicketState.OPEN
    job: Job
    plan: Plan | None = None
    approvals: list[Approval] = Field(default_factory=list)
    votes: list[Vote] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    logs: LogBundle | None = None
    findings: list[Finding] = Field(default_factory=list)
    rca: Rca | None = None
    sop: SopRefs | None = None
    exports: list[Export] = Field(default_factory=list)
    parent: TicketId | None = None
    created_at: datetime
    updated_at: datetime
    history: list[StateChange] = Field(default_factory=list)

    def transition(self, target: TicketState, at: datetime, reason: str) -> StateChange:
        if not can_transition(self.state, target):
            raise IllegalTransitionError(self.id, self.state, target)
        change = StateChange(from_state=self.state, to_state=target, at=at, reason=reason)
        self.history.append(change)
        self.state = target
        self.updated_at = at
        return change

    def step_record(self, step_id: str) -> StepRecord | None:
        for record in self.steps:
            if record.step_id == step_id:
                return record
        return None

    @property
    def pending_approvals(self) -> list[Approval]:
        return [approval for approval in self.approvals if approval.pending]

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES

    def sentence(self) -> str:
        """One line for lists and the activity feed (CLAUDE.md §9)."""
        total = len(self.plan.steps) if self.plan else 0
        done = sum(1 for record in self.steps if record.status == "done")
        if self.state is TicketState.RUNNING:
            current = next((r for r in self.steps if r.status == "running"), None)
            where = f"step {current.n} of {total}" if current else f"{done} of {total} steps done"
            return f"{self.id} is running: {where}."
        if self.state is TicketState.PLANNED and self.pending_approvals:
            count = len(self.pending_approvals)
            noun = "step needs" if count == 1 else "steps need"
            return f"{self.id} is planned and waiting: {count} {noun} your approval."
        if self.state is TicketState.NEEDS_REVIEW:
            return f"{self.id} needs your review: {done} of {total} steps finished."
        return f"{self.id} is {self.state.value.lower()}."
