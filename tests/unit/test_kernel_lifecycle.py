"""The NullAgent through the whole lifecycle: P2's first "done when" sentence."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from slas_kernel.agent import Agent
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext, FakeExecutor, UnknownPrimitiveError
from slas_kernel.kernel import ApprovalPendingError, Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.store import FileTicketStore, MemoryTicketStore
from slas_schemas.job import Job, MesTicket, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import Observation, TicketState

UPLOAD = Upload(filename="plan.md", uploaded_by="pat", content="# do things\n", size_bytes=12)


def make_kernel(tmp_path: Path, **kwargs: object) -> tuple[Kernel, FakeExecutor]:
    executor = FakeExecutor()
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=executor,
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )
    return kernel, executor


def test_null_agent_satisfies_the_agent_protocol_and_nothing_more() -> None:
    agent = NullAgent()
    assert isinstance(agent, Agent)
    public = {name for name in dir(agent) if not name.startswith("_")}
    assert public == {"name", "ingest", "plan", "verify", "sop_template"}


def test_full_lifecycle_creates_a_ticket_journals_five_steps_and_closes(tmp_path: Path) -> None:
    kernel, executor = make_kernel(tmp_path)
    ticket = kernel.run(UPLOAD)

    assert ticket.id == "T-null-0001"
    assert ticket.state is TicketState.DONE
    assert ticket.title == "Rehearsal for plan.md"
    assert ticket.user == "pat"
    assert [change.to_state.value for change in ticket.history] == [
        "Planned",
        "Approved",
        "Running",
        "Analysing",
        "Done",
    ]
    assert ticket.history[1].reason == "No step needs approval."
    assert executor.executed == ["s1", "s2", "s3", "s4", "s5"]
    assert [record.status for record in ticket.steps] == ["done"] * 5
    assert all(record.verdict and record.verdict.outcome == "ok" for record in ticket.steps)

    journal = kernel.journal_for(ticket.id)
    kinds = [(entry.kind, entry.step_id) for entry in journal.entries()]
    for step_id in ("s1", "s2", "s3", "s4", "s5"):
        assert kinds.index(("intent", step_id)) < kinds.index(("observation", step_id))
    assert sum(1 for kind, _ in kinds if kind == "observation") == 5
    assert journal.interrupted_step() is None

    # COLLECT: stdout and stderr on the ticket, fenced per step.
    assert ticket.logs is not None and ticket.logs.stdout_path and ticket.logs.stderr_path
    stdout = Path(ticket.logs.stdout_path).read_text(encoding="utf-8")
    stderr = Path(ticket.logs.stderr_path).read_text(encoding="utf-8")
    assert "--- step s1: Say hello ---\nhello from the null agent\n" in stdout
    assert "inputs: plan.md" in stdout
    assert "--- step s4: Warn on stderr ---\nwarning: this is only a rehearsal\n" in stderr
    assert ticket.logs.line_counts["stdout"] >= 8

    # RCA placeholder: honest and deterministic.
    assert ticket.rca is not None
    assert ticket.rca.uncertain is False and ticket.rca.confidence == 1.0
    assert re.fullmatch(r"[0-9a-f]{16}", ticket.rca.fingerprint or "")

    # SOP: both languages plus the structured source, always together (INV-13).
    assert ticket.sop is not None
    for path in (ticket.sop.en, ticket.sop.zh, ticket.sop.data):
        assert Path(path).is_file(), path
    assert Path(ticket.sop.en).parent == tmp_path / "SOP" / ticket.id

    # Persisted and reloadable from the data root layout (§4.4).
    stored = FileTicketStore(tmp_path).load(ticket.id)
    assert stored == ticket
    assert (tmp_path / "Tickets" / ticket.id / "journal.jsonl").is_file()


def test_ticket_ids_increase_across_runs_and_kernels(tmp_path: Path) -> None:
    kernel, _ = make_kernel(tmp_path)
    first = kernel.run(UPLOAD)
    kernel2, _ = make_kernel(tmp_path)
    second = kernel2.run(
        MesTicket(ticket_no="MES-9", station="ST1", unit_sn="SN1", requested_by="li")
    )
    assert (first.id, second.id) == ("T-null-0001", "T-null-0002")
    assert second.title == "Rehearsal for MES ticket MES-9"
    assert FileTicketStore(tmp_path).list_ids() == ["T-null-0001", "T-null-0002"]


def test_memory_store_works_the_same(tmp_path: Path) -> None:
    store = MemoryTicketStore()
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=FakeExecutor(),
        store=store,
        clock=FakeClock(),
    )
    ticket = kernel.run(UPLOAD)
    assert store.load(ticket.id).state is TicketState.DONE
    assert store.list_ids() == ["T-null-0001"]
    assert kernel.resume(ticket.id) == ticket, "resuming a finished ticket is a no-op"


class FailingAgent(NullAgent):
    """The NullAgent, but step 4 exits with code 2."""

    def plan(self, job: Job) -> Plan:
        plan = super().plan(job)
        plan.steps[3] = plan.steps[3].model_copy(
            update={"args": {"stderr": "boom\n", "exit_code": 2}}
        )
        return Plan.model_validate(plan.model_dump())


def test_a_failing_step_stops_the_run_and_ends_in_needs_review(tmp_path: Path) -> None:
    executor = FakeExecutor()
    kernel = Kernel(
        data_root=tmp_path,
        agent=FailingAgent(),
        executor=executor,
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
    )
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.NEEDS_REVIEW
    assert executor.executed == ["s1", "s2", "s3", "s4"], "s5 never runs after a failure"
    assert [record.status for record in ticket.steps] == [
        "done",
        "done",
        "done",
        "failed",
        "pending",
    ]
    assert ticket.steps[3].verdict is not None
    assert ticket.steps[3].verdict.sentence == "Warn on stderr exited with code 2."
    assert ticket.rca is not None and ticket.rca.uncertain
    assert ticket.rca.cause.startswith("Step 4 (Warn on stderr) did not finish as expected.")
    assert ticket.rca.evidence == ["Step 4 (Warn on stderr) failed.", "stderr: boom"]
    assert ticket.sentence() == "T-null-0001 needs your review: 3 of 5 steps finished."
    assert ticket.sop is not None and Path(ticket.sop.zh).is_file()
    assert kernel.resume(ticket.id).state is TicketState.NEEDS_REVIEW


class DestructiveAgent(NullAgent):
    def plan(self, job: Job) -> Plan:
        plan = super().plan(job)
        plan.steps[2] = plan.steps[2].model_copy(
            update={"risk": "destructive", "title": "AC cycle"}
        )
        return Plan.model_validate(plan.model_dump())


def test_destructive_steps_wait_for_a_human_approval(tmp_path: Path) -> None:
    executor = FakeExecutor()
    kernel = Kernel(
        data_root=tmp_path,
        agent=DestructiveAgent(),
        executor=executor,
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
    )
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.PLANNED
    assert executor.executed == [], "nothing runs before the approval (INV-7)"
    assert [a.step_id for a in ticket.pending_approvals] == ["s3"]
    assert ticket.sentence() == "T-null-0001 is planned and waiting: 1 step needs your approval."
    with pytest.raises(ApprovalPendingError):
        kernel.resume(ticket.id)

    approved = kernel.approve(ticket.id, "s3", decided_by="lead", note="lab is clear")
    assert approved.pending_approvals == []
    assert approved.approvals[0].decided_by == "lead"
    with pytest.raises(KeyError):
        kernel.approve(ticket.id, "s3", decided_by="lead")

    finished = kernel.resume(ticket.id)
    assert finished.state is TicketState.DONE
    assert executor.executed == ["s1", "s2", "s3", "s4", "s5"]
    assert finished.history[1].reason == "Every destructive step was approved."
    notes = [e.payload for e in kernel.journal_for(ticket.id).entries() if e.kind == "note"]
    assert {"approvals_requested": ["s3"]} in notes
    assert {"approved": "s3", "by": "lead"} in notes


def test_unknown_primitive_is_refused_by_the_executor() -> None:
    step = Step(id="s1", n=1, primitive="shell", title="nope")
    context = ExecutionContext(ticket_id="T-null-0001", job_id="job-1", agent="null", user="pat")
    with pytest.raises(UnknownPrimitiveError, match="primitive 'shell'"):
        FakeExecutor().execute(step, context)


def test_plan_for_another_job_is_refused(tmp_path: Path) -> None:
    class WrongPlanAgent(NullAgent):
        def plan(self, job: Job) -> Plan:
            plan = super().plan(job)
            return plan.model_copy(update={"job_id": "job-other"})

    kernel = Kernel(
        data_root=tmp_path,
        agent=WrongPlanAgent(),
        executor=FakeExecutor(),
        store=MemoryTicketStore(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError, match="belongs to job job-other"):
        kernel.run(UPLOAD)


def test_journal_lines_are_valid_json_with_increasing_sequence(tmp_path: Path) -> None:
    kernel, _ = make_kernel(tmp_path)
    ticket = kernel.run(UPLOAD)
    lines = (tmp_path / "Tickets" / ticket.id / "journal.jsonl").read_text().splitlines()
    sequence = [json.loads(line)["seq"] for line in lines]
    assert sequence == list(range(1, len(lines) + 1))
    assert json.loads(lines[0])["kind"] == "state"
    entries = [json.loads(line) for line in lines]
    observation = next(
        entry
        for entry in entries
        if entry.get("step_id") == "s4" and entry["kind"] == "observation"
    )
    assert observation["payload"]["stderr"] == "warning: this is only a rehearsal\n"
    assert Observation.model_validate(observation["payload"]).exit_code == 0
