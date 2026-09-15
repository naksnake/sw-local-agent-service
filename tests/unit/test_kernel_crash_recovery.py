"""P2's second "done when": kill the run after step 3, restart, resume at step 4."""

from __future__ import annotations

from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.store import FileTicketStore
from slas_schemas.job import Upload
from slas_schemas.plan import Step
from slas_schemas.ticket import Ticket, TicketState

UPLOAD = Upload(filename="plan.md", uploaded_by="pat")


class ProcessKilled(BaseException):
    """Stands in for SIGKILL: nothing after it runs, not even `except Exception`."""


def kernel_with(tmp_path: Path, executor: FakeExecutor, **kwargs: object) -> Kernel:
    return Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=executor,
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_killed_after_step_3_resumes_at_step_4(tmp_path: Path) -> None:
    def kill_after_step_3(ticket: Ticket, step: Step) -> None:
        if step.n == 3:
            raise ProcessKilled

    first_process = FakeExecutor()
    kernel = kernel_with(tmp_path, first_process, after_step=kill_after_step_3)
    with pytest.raises(ProcessKilled):
        kernel.run(UPLOAD)
    assert first_process.executed == ["s1", "s2", "s3"]

    # What the crashed process left behind.
    store = FileTicketStore(tmp_path)
    crashed = store.load("T-null-0001")
    assert crashed.state is TicketState.RUNNING
    assert [r.status for r in crashed.steps] == ["done", "done", "done", "pending", "pending"]
    journal = kernel.journal_for(crashed.id)
    assert set(journal.completed_steps()) == {"s1", "s2", "s3"}
    assert journal.interrupted_step() is None

    # A new process, a new executor: only steps 4 and 5 run.
    second_process = FakeExecutor()
    restarted = kernel_with(tmp_path, second_process)
    ticket = restarted.resume("T-null-0001")

    assert second_process.executed == ["s4", "s5"]
    assert ticket.state is TicketState.DONE
    assert [r.status for r in ticket.steps] == ["done"] * 5
    observations = [
        e for e in restarted.journal_for(ticket.id).entries() if e.kind == "observation"
    ]
    assert [e.step_id for e in observations] == ["s1", "s2", "s3", "s4", "s5"], (
        "exactly one observation per step, none repeated"
    )
    assert ticket.history[2].to_state is TicketState.RUNNING
    assert ticket.history[3].to_state is TicketState.ANALYSING
    assert ticket.sop is not None and Path(ticket.sop.en).is_file()


def test_killed_in_the_middle_of_step_4_re_performs_step_4(tmp_path: Path) -> None:
    first_process = FakeExecutor(fail_before_observing={"s4"})
    kernel = kernel_with(tmp_path, first_process)
    with pytest.raises(RuntimeError, match="simulated crash while performing s4"):
        kernel.run(UPLOAD)
    assert first_process.executed == ["s1", "s2", "s3", "s4"]

    journal = kernel.journal_for("T-null-0001")
    assert journal.interrupted_step() == "s4", "an intent without an observation"
    crashed = FileTicketStore(tmp_path).load("T-null-0001")
    assert crashed.steps[3].status == "running"

    second_process = FakeExecutor()
    ticket = kernel_with(tmp_path, second_process).resume("T-null-0001")

    assert second_process.executed == ["s4", "s5"], "the interrupted step is redone, not skipped"
    assert ticket.state is TicketState.DONE
    entries = journal.step_entries("s4")
    assert [e.kind for e in entries] == ["intent", "intent", "observation"]
    assert entries[1].payload["note"] == "re-performing after an interruption"
    assert journal.interrupted_step() is None


def test_journal_is_the_truth_when_the_ticket_file_is_behind(tmp_path: Path) -> None:
    """Observation journalled, then killed before ticket.json was saved: nothing is redone."""
    executor = FakeExecutor()
    kernel = kernel_with(tmp_path, executor)
    ticket = kernel.run(UPLOAD)
    store = FileTicketStore(tmp_path)

    # Rewind the saved ticket to "running, step 5 still pending" while the journal has all 5.
    behind = store.load(ticket.id)
    behind = behind.model_copy(
        update={
            "state": TicketState.RUNNING,
            "history": behind.history[:3],
            "logs": None,
            "rca": None,
            "sop": None,
        }
    )
    behind.steps[4].status = "pending"
    behind.steps[4].observation = None
    behind.steps[4].verdict = None
    store.save(behind)

    resumed_executor = FakeExecutor()
    resumed = kernel_with(tmp_path, resumed_executor).resume(ticket.id)
    assert resumed_executor.executed == [], "the journal already holds step 5's observation"
    assert resumed.state is TicketState.DONE
    assert resumed.steps[4].status == "done"
    assert resumed.steps[4].observation is not None
    assert resumed.steps[4].observation.stdout == "done\n"


def test_killed_while_analysing_resumes_the_analysis(tmp_path: Path) -> None:
    executor = FakeExecutor()
    kernel = kernel_with(tmp_path, executor)
    ticket = kernel.run(UPLOAD)
    store = FileTicketStore(tmp_path)
    analysing = store.load(ticket.id).model_copy(
        update={"state": TicketState.ANALYSING, "history": ticket.history[:4], "sop": None}
    )
    store.save(analysing)

    resumed = kernel_with(tmp_path, FakeExecutor()).resume(ticket.id)
    assert resumed.state is TicketState.DONE
    assert resumed.sop is not None
    assert resumed.history[-1].to_state is TicketState.DONE


def test_resume_of_an_unknown_ticket_says_so(tmp_path: Path) -> None:
    kernel = kernel_with(tmp_path, FakeExecutor())
    with pytest.raises(KeyError, match="There is no ticket T-null-0099"):
        kernel.resume("T-null-0099")
