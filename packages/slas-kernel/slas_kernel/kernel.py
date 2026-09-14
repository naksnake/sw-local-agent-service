"""The Agent Kernel: the lifecycle every agent runs (CLAUDE.md §5.1, ADR-0001).

    INGEST → TICKET → PLAN → (approval) → ACT → COLLECT → RCA → SOP → CLOSE

The kernel owns tickets, the write-ahead journal, log collection, RCA, SOP rendering and
approvals. An agent contributes only `ingest`, `plan`, `verify` and `sop_template`.

Crash recovery: the ticket is saved after every step and the journal is written ahead of
every action. `resume(ticket_id)` reads both and continues from the first step without a
journalled observation — re-performing a step that was interrupted mid-way, never one that
finished.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from slas_kernel.agent import Agent
from slas_kernel.clock import Clock, SystemClock
from slas_kernel.executor import Executor
from slas_kernel.journal import Journal
from slas_kernel.logs import collect_logs
from slas_kernel.rca import placeholder_rca
from slas_kernel.sop import build_sop_model, render_sop
from slas_kernel.store import TicketStore
from slas_schemas.job import Job, MesTicket, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import (
    Approval,
    Observation,
    StepRecord,
    Ticket,
    TicketState,
)

AfterStepHook = Callable[[Ticket, Step], None]


class ApprovalPendingError(RuntimeError):
    def __init__(self, ticket: Ticket) -> None:
        super().__init__(ticket.sentence())
        self.ticket = ticket


class Kernel:
    def __init__(
        self,
        *,
        data_root: Path,
        agent: Agent,
        executor: Executor,
        store: TicketStore,
        clock: Clock | None = None,
        after_step: AfterStepHook | None = None,
    ) -> None:
        self.data_root = data_root
        self.agent = agent
        self.executor = executor
        self.store = store
        self.clock = clock or SystemClock()
        # Test seam: called after each step's observation is journalled and the ticket saved.
        self._after_step = after_step

    # --- paths ---------------------------------------------------------------------------

    def ticket_dir(self, ticket_id: str) -> Path:
        return self.data_root / "Tickets" / ticket_id

    def sop_dir(self, ticket_id: str) -> Path:
        return self.data_root / "SOP" / ticket_id

    def journal_for(self, ticket_id: str) -> Journal:
        return Journal(self.ticket_dir(ticket_id) / "journal.jsonl", self.clock)

    # --- lifecycle -----------------------------------------------------------------------

    def run(self, raw: Upload | MesTicket) -> Ticket:
        """INGEST → TICKET → PLAN, then act unless a destructive step needs approval."""
        job = self.agent.ingest(raw)
        ticket = self._create_ticket(job)
        journal = self.journal_for(ticket.id)
        journal.append("state", ticket.id, {"state": ticket.state.value, "reason": "ingested"})

        plan = self.agent.plan(job)
        self._attach_plan(ticket, plan)
        self._transition(ticket, journal, TicketState.PLANNED, plan.sentence())

        if plan.destructive:
            now = self.clock.now()
            ticket.approvals = [
                Approval(step_id=step.id, requested_at=now) for step in plan.destructive_steps()
            ]
            journal.append(
                "note",
                ticket.id,
                {"approvals_requested": [a.step_id for a in ticket.approvals]},
            )
            self.store.save(ticket)
            return ticket

        self._transition(ticket, journal, TicketState.APPROVED, "No step needs approval.")
        return self._act_and_close(ticket, journal)

    def approve(self, ticket_id: str, step_id: str, decided_by: str, note: str = "") -> Ticket:
        """Record one human approval (INV-7). Does not start the run; call `resume()`."""
        ticket = self.store.load(ticket_id)
        for approval in ticket.approvals:
            if approval.step_id == step_id and approval.pending:
                approval.decided_at = self.clock.now()
                approval.decided_by = decided_by
                approval.approved = True
                approval.note = note
                self.journal_for(ticket.id).append(
                    "note",
                    ticket.id,
                    {"approved": step_id, "by": decided_by},
                    step_id=step_id,
                )
                self.store.save(ticket)
                return ticket
        raise KeyError(f"{ticket_id} has no pending approval for step {step_id}")

    def resume(self, ticket_id: str) -> Ticket:
        """Continue a ticket after a crash or an approval, from the journal."""
        ticket = self.store.load(ticket_id)
        if ticket.finished:
            return ticket
        journal = self.journal_for(ticket.id)
        if ticket.state is TicketState.PLANNED:
            if ticket.pending_approvals:
                raise ApprovalPendingError(ticket)
            self._transition(
                ticket, journal, TicketState.APPROVED, "Every destructive step was approved."
            )
        if ticket.state in (TicketState.APPROVED, TicketState.RUNNING):
            return self._act_and_close(ticket, journal)
        if ticket.state is TicketState.ANALYSING:
            return self._analyse_and_close(ticket, journal)
        return ticket

    # --- internals -----------------------------------------------------------------------

    def _create_ticket(self, job: Job) -> Ticket:
        now = self.clock.now()
        ticket = Ticket(
            id=self.store.next_ticket_id(job.agent),
            agent=job.agent,
            user=job.user,
            title=job.title,
            job=job,
            created_at=now,
            updated_at=now,
        )
        self.store.save(ticket)
        return ticket

    def _attach_plan(self, ticket: Ticket, plan: Plan) -> None:
        if plan.job_id != ticket.job.id:
            raise ValueError(f"plan {plan.id} belongs to job {plan.job_id}, not {ticket.job.id}")
        ticket.plan = plan
        ticket.steps = [
            StepRecord(step_id=step.id, n=step.n, title=step.title) for step in plan.steps
        ]

    def _transition(
        self, ticket: Ticket, journal: Journal, target: TicketState, reason: str
    ) -> None:
        change = ticket.transition(target, self.clock.now(), reason)
        journal.append(
            "state",
            ticket.id,
            {"from": change.from_state.value, "to": change.to_state.value, "reason": reason},
        )
        self.store.save(ticket)

    def _act_and_close(self, ticket: Ticket, journal: Journal) -> Ticket:
        if ticket.state is TicketState.APPROVED:
            self._transition(ticket, journal, TicketState.RUNNING, "Starting the steps.")
        stopped_early = self._act(ticket, journal)
        reason = (
            "A step did not finish as expected; collecting logs."
            if stopped_early
            else "Every step finished; collecting logs."
        )
        self._transition(ticket, journal, TicketState.ANALYSING, reason)
        return self._analyse_and_close(ticket, journal)

    def _act(self, ticket: Ticket, journal: Journal) -> bool:
        """ACT: perform every step not yet observed. Returns True if a step failed."""
        assert ticket.plan is not None  # noqa: S101 — a Running ticket always has a plan
        completed = journal.completed_steps()
        interrupted = journal.interrupted_step()
        for step in ticket.plan.steps:
            record = ticket.step_record(step.id)
            if record is None:  # pragma: no cover — records are created with the plan
                raise RuntimeError(f"{ticket.id} has no record for step {step.id}")
            if record.status == "failed":
                return True
            if step.id in completed:
                if record.status != "done":
                    self._restore_from_journal(record, completed[step.id].payload, ticket)
                continue
            if record.status == "skipped":
                continue
            if self._perform(ticket, journal, step, record, resumed=step.id == interrupted):
                return True
        return False

    def _perform(
        self, ticket: Ticket, journal: Journal, step: Step, record: StepRecord, *, resumed: bool
    ) -> bool:
        started = self.clock.now()
        record.status = "running"
        record.started_at = started
        record.observation = None
        record.verdict = None
        self.store.save(ticket)
        payload = {"primitive": step.primitive, "args": step.args, "risk": step.risk}
        if resumed:
            payload["note"] = "re-performing after an interruption"
        journal.append("intent", ticket.id, payload, step_id=step.id)

        observation = self.executor.execute(step)  # a crash here leaves an open intent
        journal.append(
            "observation",
            ticket.id,
            observation.model_dump(mode="json"),
            step_id=step.id,
        )
        verdict = self.agent.verify(step, observation)
        record.observation = observation
        record.verdict = verdict
        record.finished_at = self.clock.now()
        record.status = "done" if verdict.outcome == "ok" else "failed"
        ticket.updated_at = record.finished_at
        self.store.save(ticket)
        if self._after_step is not None:
            self._after_step(ticket, step)
        return record.status == "failed"

    def _restore_from_journal(
        self, record: StepRecord, payload: dict[str, object], ticket: Ticket
    ) -> None:
        """A step observed before a crash but not saved on the ticket: rebuild it, don't redo it."""
        assert ticket.plan is not None  # noqa: S101
        observation = Observation.model_validate(payload)
        record.observation = observation
        record.verdict = self.agent.verify(ticket.plan.step(record.step_id), observation)
        record.status = "done" if record.verdict.outcome == "ok" else "failed"
        record.finished_at = record.finished_at or self.clock.now()

    def _analyse_and_close(self, ticket: Ticket, journal: Journal) -> Ticket:
        """COLLECT → RCA → SOP → CLOSE."""
        ticket.logs = collect_logs(ticket, self.ticket_dir(ticket.id) / "logs")
        journal.append("note", ticket.id, {"logs": ticket.logs.model_dump(mode="json")})
        ticket.rca = placeholder_rca(ticket)
        journal.append("note", ticket.id, {"rca": ticket.rca.model_dump(mode="json")})
        failed = any(record.status == "failed" for record in ticket.steps)
        final_state = TicketState.NEEDS_REVIEW if failed else TicketState.DONE
        model = build_sop_model(ticket, self.agent.sop_template(), final_state=final_state)
        ticket.sop = render_sop(model, self.sop_dir(ticket.id))
        journal.append("note", ticket.id, {"sop": ticket.sop.model_dump(mode="json")})
        self.store.save(ticket)

        if failed:
            self._transition(
                ticket,
                journal,
                TicketState.NEEDS_REVIEW,
                "A step failed; the logs, the analysis and the SOP are attached for review.",
            )
        else:
            self._transition(
                ticket,
                journal,
                TicketState.DONE,
                "Every step finished; the logs, the analysis and the SOP are attached.",
            )
        return ticket


def utc_iso(moment: datetime) -> str:
    return moment.isoformat()
