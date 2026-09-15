"""P7's done-when, against fakes: a 25-cycle DC run with a PCIe degradation planted at cycle
14 → the LED map shows it, one deduplicated bug ticket with 3 votes and an EN/中文 SOP; a kill
mid-cycle resumes at the right cycle without a second power action; three boot failures abort;
an AC plan is blocked until approved; one run per target."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slas_hal.fakes.bmc import FakeHal, FakeTarget, Plant
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.kernel import ApprovalPendingError, Kernel
from slas_kernel.rca import FakeCrossChecker, FakeDrafter, RcaPipeline
from slas_kernel.store import FileTicketStore
from slas_orchestrator.validation.agent import ValidationAgent
from slas_schemas.job import Upload
from slas_schemas.plan import Step
from slas_schemas.ticket import Ticket, TicketState
from slas_schemas.vote import Vote
from slas_validation_executor.cycle import CycleJournal, power_action_for
from slas_validation_executor.executor import RunState, ValidationExecutor
from slas_validation_executor.leases import LeaseError, LeaseTable

TARGET = "lab-gx8-01"
GPU3 = "0000:8a:00.0"
SUITE_25 = "# GX8 DC cycling\n\n- DC cycle x25, settle 60 s\n"
UPLOAD_25 = Upload(filename="gx8.md", uploaded_by="pat", content=SUITE_25, size_bytes=48)
HEADLINE = (
    "[Issue] PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 "
    "during DC cycle 14 | [Owner] EE"
)


def votes(reason: str) -> list[Vote]:
    return [
        Vote(voter=f"voter-{i}", verdict="approve", reason=reason, confidence=0.9)
        for i in (1, 2, 3)
    ]


class Rig:
    """One kernel process: agent + executor + kernel over a data root and a (shared) FakeHal."""

    def __init__(self, data_root: Path, hal: FakeHal, **kernel_kwargs: object) -> None:
        self.hal = hal
        self.agent = ValidationAgent(plans_dir=data_root / "Validation" / "Plans")
        self.executor = ValidationExecutor(hal=hal, data_root=data_root, clock=FakeClock())
        self.store = FileTicketStore(data_root)
        self.kernel = Kernel(
            data_root=data_root,
            agent=self.agent,
            executor=self.executor,
            store=self.store,
            clock=FakeClock(),
            **kernel_kwargs,  # type: ignore[arg-type]
        )

    def run(self, upload: Upload, target: str = TARGET) -> Ticket:
        job = self.agent.ingest(upload)
        self.agent.choose_target(job, target)
        return self.kernel.run(upload)


def phases(run_dir: Path, n: int) -> list[str]:
    return [
        e.phase for e in CycleJournal(run_dir / "cycles" / f"{n:03d}" / "journal.jsonl").entries()
    ]


# --- the done-when -----------------------------------------------------------------------------


def test_25_dc_cycles_with_a_pcie_degradation_at_cycle_14_yield_one_deduplicated_ticket(
    tmp_path: Path,
) -> None:
    hal = FakeHal(
        [FakeTarget(TARGET, plants=[Plant(at_cycle=14, kind="pcie_width", bdf=GPU3, width=8)])],
        clock=FakeClock(),
    )
    plan_checker = FakeCrossChecker(votes("The plan stays within the guardrails."), agreed=True)
    rca_checker = FakeCrossChecker(votes("The riser retimer is the likely cause."), agreed=True)
    rig = Rig(
        tmp_path,
        hal,
        plan_checker=plan_checker,
        rca_pipeline=RcaPipeline(drafter=FakeDrafter(), cross_checker=rca_checker),
    )
    ticket = rig.run(UPLOAD_25)

    # The run: 30 steps, every one done; the run ends in Needs review because of the finding.
    assert ticket.id == "T-validation-0001"
    assert ticket.state is TicketState.NEEDS_REVIEW
    assert len(ticket.steps) == 30 and all(r.status == "done" for r in ticket.steps)
    assert ticket.history[-1].reason.startswith(
        "Every step finished, but 1 finding needs your review"
    )
    assert ticket.job.target is not None and ticket.job.target.ref == TARGET

    # The plan went through the Consensus Router before anything ran (§5.3, unanimous).
    assert plan_checker.calls[0][0] == "plan_approval"
    assert plan_checker.calls[0][1][0].startswith("GX8 DC cycling on lab-gx8-01: 25 power cycles")
    assert "4. DC cycle 1 of 25 [caution]" in plan_checker.calls[0][1]
    assert [v.voter for v in ticket.votes[:3]] == ["voter-1", "voter-2", "voter-3"]

    # Hardware: 25 off/on pairs, one fence marker before each power action, nothing else.
    records = hal.target(TARGET).power_records
    assert len(records) == 50
    assert [r.action for r in records[:4]] == ["off", "on", "off", "on"]
    assert [m for _, m in hal.fence_markers][13] == f"--- slas fence {ticket.id} cycle 14 dc ---"
    assert len(hal.fence_markers) == 25

    # The LED cycle map: 13 clean cycles, then the degradation stays visible to the end.
    state = rig.executor.state_for(ticket.id)
    assert state is not None
    assert [c.status for c in state.cycles[:13]] == ["ok"] * 13
    assert [c.status for c in state.cycles[13:]] == ["finding"] * 12
    assert state.cycles[13].sentence == (
        "Cycle 14 (DC): booted; 1 change against the baseline during DC cycle 14: "
        "PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14."
    )
    assert state.sentence() == "25 of 25 cycles done: 12 with findings."
    run_dir = rig.executor.run_dir(ticket.id)
    assert RunState.model_validate_json((run_dir / "cycles.json").read_text()) == state
    assert phases(run_dir, 14) == ["arm", "quiesce", "act", "settle", "verify"]
    assert (run_dir / "cycles" / "014" / "sel-before.json").is_file()
    assert (run_dir / "cycles" / "014" / "snapshot.json").is_file()
    assert (run_dir / "baseline" / "snapshot.json").is_file()
    console = (run_dir / "console.log").read_text(encoding="utf-8")
    assert f"--- slas fence {ticket.id} cycle 14 dc ---" in console
    assert console.count("Linux version") == 25

    # Findings: twelve sightings, one finding, evidence per cycle, routed to EE.
    (finding,) = ticket.findings
    assert finding.issue == (
        "PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14"
    )
    assert (finding.owner, finding.severity, finding.component) == ("EE", "S2", "NVIDIA H100 SXM")
    assert finding.evidence[:2] == ["cycle 14: x16 → x8", "cycle 15: x16 → x8"]
    assert len(finding.evidence) == 12
    assert finding.ticket_id == "T-validation-0002"

    # One child bug ticket in the [Issue] … | [Owner] … format, carrying the 3 diagnosis votes.
    child = rig.store.load("T-validation-0002")
    assert child.title == HEADLINE
    assert child.parent == ticket.id and child.state is TicketState.OPEN
    assert child.job.target is not None and child.job.target.ref == TARGET
    assert [v.reason for v in child.votes] == ["The riser retimer is the likely cause."] * 3
    assert child.rca is not None and child.rca == ticket.rca
    assert child.findings[0].headline() == HEADLINE
    with pytest.raises(KeyError):
        rig.store.load("T-validation-0003")
    index = json.loads((tmp_path / "Tickets" / "bug-index.json").read_text(encoding="utf-8"))
    assert [(e["ticket_id"], e["seen"]) for e in index.values()] == [("T-validation-0002", 1)]

    # Both languages, always together (INV-13).
    assert ticket.sop is not None
    en = Path(ticket.sop.en).read_text(encoding="utf-8")
    zh = Path(ticket.sop.zh).read_text(encoding="utf-8")
    assert Path(ticket.sop.en).name == "sop.en.md" and Path(ticket.sop.zh).name == "sop.zh-Hant.md"
    assert f"- {HEADLINE}" in en and f"- {HEADLINE}" in zh
    assert "0000:8a:00.0" in zh, "identifiers are copied by code, never translated"
    notes = [
        json.loads(line)
        for line in (tmp_path / "Tickets" / ticket.id / "journal.jsonl").read_text().splitlines()
    ]
    assert any(n["payload"].get("bug_tickets") == ["T-validation-0002"] for n in notes)
    assert any("plan_cross_check" in n["payload"] for n in notes)

    # A second run against a fresh target with the same fault: the finding is linked to the
    # existing bug ticket, no third ticket is drafted.
    fresh = FakeHal(
        [FakeTarget(TARGET, plants=[Plant(at_cycle=2, kind="pcie_width", bdf=GPU3, width=8)])],
        clock=FakeClock(),
    )
    second = Rig(tmp_path, fresh).run(
        Upload(filename="gx8-again.md", uploaded_by="pat", content="- DC cycle x3\n")
    )
    assert second.id == "T-validation-0003"
    assert second.findings[0].ticket_id == "T-validation-0002"
    with pytest.raises(KeyError):
        rig.store.load("T-validation-0004")
    index = json.loads((tmp_path / "Tickets" / "bug-index.json").read_text(encoding="utf-8"))
    assert [(e["ticket_id"], e["seen"]) for e in index.values()] == [("T-validation-0002", 2)]


def test_a_kill_mid_cycle_resumes_at_that_cycle_without_a_second_power_action(
    tmp_path: Path,
) -> None:
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())

    def arm_the_crash(ticket: Ticket, step: Step) -> None:
        if step.id == "cycle-006":
            hal.crash_once_in = "wait_for_os"  # cycle 7 dies after ACT, before SETTLE

    first = Rig(tmp_path, hal, after_step=arm_the_crash)
    with pytest.raises(RuntimeError, match="simulated crash during wait_for_os"):
        first.run(UPLOAD_25)

    crashed = first.store.load("T-validation-0001")
    assert crashed.state is TicketState.RUNNING
    assert crashed.step_record("cycle-007") is not None
    assert crashed.step_record("cycle-007").status == "running"  # type: ignore[union-attr]
    assert first.kernel.journal_for(crashed.id).interrupted_step() == "cycle-007"
    assert len(hal.target(TARGET).power_records) == 14, "cycle 7 was powered before the crash"
    run_dir = first.executor.run_dir(crashed.id)
    assert phases(run_dir, 7) == ["arm", "quiesce", "act"]

    # A new process: new executor, new kernel, same hardware and the same journals on disk.
    second = Rig(tmp_path, hal)
    ticket = second.kernel.resume("T-validation-0001")
    assert ticket.state is TicketState.DONE
    assert len(hal.target(TARGET).power_records) == 50, "cycle 7 was not powered twice"
    assert phases(run_dir, 7) == ["arm", "quiesce", "act", "settle", "verify"]
    entries = second.kernel.journal_for(ticket.id).step_entries("cycle-007")
    assert [e.kind for e in entries] == ["intent", "intent", "observation"]
    state = second.executor.state_for(ticket.id)
    assert state is not None and [c.status for c in state.cycles] == ["ok"] * 25
    assert state.cycles[6].sentence == (
        "Cycle 7 (DC): booted; no change against the baseline. "
        "Resumed after an interruption without repeating the power action."
    )
    assert len(hal.fence_markers) == 25
    assert ticket.findings == []
    assert ticket.history[-1].reason.startswith("Every step finished; the logs")


def test_three_boot_failures_in_a_row_abort_the_run_and_hold_it_for_a_person(
    tmp_path: Path,
) -> None:
    hal = FakeHal(
        [FakeTarget(TARGET, plants=[Plant(at_cycle=3, kind="boot_fail")])], clock=FakeClock()
    )
    rig = Rig(tmp_path, hal)
    ticket = rig.run(UPLOAD_25)
    assert ticket.state is TicketState.NEEDS_REVIEW
    assert ticket.history[-1].reason.startswith("A step failed")
    statuses = [r.status for r in ticket.steps]
    assert statuses[3:8] == ["done", "done", "done", "done", "failed"], "cycles 1-4 done, 5 failed"
    assert statuses[8:] == ["pending"] * 22, "nothing after the abort ran"
    assert len(hal.target(TARGET).power_records) == 10
    state = rig.executor.state_for(ticket.id)
    assert state is not None and state.aborted
    assert [c.status for c in state.cycles[:6]] == [
        "ok",
        "ok",
        "failed",
        "failed",
        "failed",
        "waiting",
    ]
    assert state.sentence() == "5 of 25 cycles done: 3 did not boot."
    record = ticket.step_record("cycle-005")
    assert record is not None and record.observation is not None
    assert record.observation.exit_code == 3
    assert record.observation.summary == (
        "Cycle 5 (DC): the target did not come back within 900 s. That is 3 boot failures in a "
        "row; the run is aborted (guardrail consecutive_failure_abort=3). A person needs to look "
        "at the target."
    )
    (finding,) = ticket.findings
    assert finding.headline() == (
        "[Issue] lab-gx8-01 failed to boot 3 times in a row during DC cycling | [Owner] FW"
    )
    assert finding.severity == "S1"
    assert len(finding.evidence) == 3
    child = rig.store.load("T-validation-0002")
    assert child.parent == ticket.id and child.votes == []
    assert phases(rig.executor.run_dir(ticket.id), 5) == [
        "arm",
        "quiesce",
        "act",
        "settle",
        "verify",
    ]
    assert not (rig.executor.run_dir(ticket.id) / "cycles" / "005" / "snapshot.json").exists()


def test_an_ac_cycle_plan_is_blocked_until_every_step_is_approved(tmp_path: Path) -> None:
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())
    rig = Rig(tmp_path, hal)
    ticket = rig.run(
        Upload(filename="ac.md", uploaded_by="pat", content="- AC cycle x2, approved\n")
    )
    assert ticket.state is TicketState.PLANNED
    assert [a.step_id for a in ticket.pending_approvals] == ["cycle-001", "cycle-002"]
    assert ticket.sentence().startswith("T-validation-0001")
    assert hal.target(TARGET).power_records == [], "nothing touched the hardware"
    with pytest.raises(ApprovalPendingError):
        rig.kernel.resume(ticket.id)
    rig.kernel.approve(ticket.id, "cycle-001", "lee", "Rack 4 is clear.")
    with pytest.raises(ApprovalPendingError):
        rig.kernel.resume(ticket.id)
    assert hal.target(TARGET).power_records == []
    rig.kernel.approve(ticket.id, "cycle-002", "lee")

    done = rig.kernel.resume(ticket.id)
    assert done.state is TicketState.DONE
    assert [r.action for r in hal.target(TARGET).power_records] == ["ac_cycle", "ac_cycle"]
    assert done.history[1].reason == "Every destructive step was approved."
    assert all(a.approved and a.decided_by == "lee" for a in done.approvals)
    run_dir = rig.executor.run_dir(done.id)
    journal = CycleJournal(run_dir / "cycles" / "001" / "journal.jsonl").entries()
    assert journal[0].detail == {"kind": "ac", "settle_s": 30, "target": TARGET}
    assert journal[2].detail == {"action": "ac_cycle"}
    assert power_action_for("warm") == "graceful_restart"


def test_one_run_per_target_at_a_time(tmp_path: Path) -> None:
    table = LeaseTable(tmp_path / "leases.json")
    now = FakeClock().now()
    lease = table.acquire(TARGET, ticket_id="T-validation-0001", user="pat", now=now, max_hours=72)
    assert lease.sentence() == (
        "lab-gx8-01 is leased to T-validation-0001 (pat) until 2026-09-17 08:00."
    )
    with pytest.raises(LeaseError) as info:
        table.acquire(TARGET, ticket_id="T-validation-0002", user="lee", now=now, max_hours=72)
    assert info.value.message.what_happened == f"{TARGET} is busy: {lease.sentence()}"
    assert (
        table.acquire(TARGET, ticket_id="T-validation-0001", user="pat", now=now, max_hours=1).since
        == lease.since
    )
    assert table.free_targets([TARGET, "lab-gx8-02"], now) == ["lab-gx8-02"]
    assert table.release(TARGET, ticket_id="T-validation-0009") is False
    assert table.release(TARGET, ticket_id="T-validation-0001") is True
    assert table.holder(TARGET, now) is None

    # Through the executor: the second run's lease step fails with the busy sentence.
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())
    executor = ValidationExecutor(hal=hal, data_root=tmp_path, clock=FakeClock())
    step = Step(id="lease", n=1, primitive="lease_target", title="Lease", args={"target": TARGET})
    first = executor.execute(
        step,
        ExecutionContext(
            ticket_id="T-validation-0001", job_id="j1", agent="validation", user="pat"
        ),
    )
    assert first.exit_code == 0 and first.summary.startswith("Leased lab-gx8-01 for this run.")
    second = executor.execute(
        step,
        ExecutionContext(
            ticket_id="T-validation-0002", job_id="j2", agent="validation", user="lee"
        ),
    )
    assert second.exit_code == 1 and second.summary.startswith("lab-gx8-01 is busy:")
    assert second.stderr.startswith("Pick another target")


# --- the other primitives ------------------------------------------------------------------------


def context() -> ExecutionContext:
    return ExecutionContext(
        ticket_id="T-validation-0001", job_id="j1", agent="validation", user="pat"
    )


def step(n: int, primitive: str, risk: str = "safe", **args: object) -> Step:
    return Step(
        id=f"s{n}",
        n=n,
        primitive=primitive,
        title=f"{primitive} {n}",
        args={"target": TARGET, **args},
        risk=risk,
    )


def test_snapshots_tools_and_log_collection(tmp_path: Path) -> None:
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())
    executor = ValidationExecutor(hal=hal, data_root=tmp_path, clock=FakeClock())
    sel = executor.execute(step(1, "sel_snapshot"), context())
    assert sel.summary == "SEL read: 3 entries, worst severity Warning."
    assert sel.stdout.splitlines()[0] == "[2026-09-10T07:00:00Z] OK: System boot initiated"
    assert list(
        (tmp_path / "Validation" / "Runs" / "T-validation-0001" / "findings").glob("sel-*.json")
    )
    inventory = executor.execute(step(2, "inventory_snapshot"), context())
    assert inventory.summary == (
        "Inventory read: SLAS-GX8 SN-GX8-0001, 11 PCIe devices, BIOS 2.4.1, BMC 1.12.0."
    )
    stress = executor.execute(
        step(3, "stress", "caution", tool="stress-ng", args=["--cpu", "8"], duration_s=5), context()
    )
    assert stress.summary == "stress-ng finished on lab-gx8-01 (exit 0)."
    assert hal.target(TARGET).ssh_calls[-1] == ["stress-ng", "--cpu", "8"]
    hal.on_ssh = lambda target, argv: __import__(
        "slas_hal.hal", fromlist=["CommandResult"]
    ).CommandResult(exit_code=4, stderr="nvqual: GPU3 failed")
    diag = executor.execute(step(4, "run_diag", "caution", tool="nvqual"), context())
    assert diag.exit_code == 4 and diag.summary == "nvqual failed on lab-gx8-01 (exit 4)."
    hal.on_ssh = None
    hal.console_on(TARGET)
    hal.fence(TARGET, "marker")
    collected = executor.execute(step(5, "collect_logs"), context())
    assert collected.summary == "Collected 1 console lines."
    assert (
        tmp_path / "Validation" / "Runs" / "T-validation-0001" / "console.log"
    ).read_text() == "marker\n"
    release = executor.execute(step(6, "release_target"), context())
    assert release.summary == "lab-gx8-01 was not leased to this run; nothing to release."
    with pytest.raises(UnknownPrimitiveError):
        executor.execute(step(7, "shell"), context())
    with pytest.raises(RuntimeError, match="has no baseline"):
        executor.execute(step(8, "power_cycle", "caution", kind="dc", cycle=1), context())


def test_destructive_primitives_refuse_to_run_unless_marked_destructive(tmp_path: Path) -> None:
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())
    executor = ValidationExecutor(hal=hal, data_root=tmp_path, clock=FakeClock())
    unmarked = executor.execute(
        step(1, "firmware_flash", "caution", component="BMC", image_ref="bmc-1.13"), context()
    )
    assert unmarked.exit_code == 2
    assert (
        unmarked.summary
        == "Step 1 (firmware_flash 1) is firmware_flash but was not marked destructive."
    )
    assert hal.target(TARGET).ssh_calls == [] and hal.fence_markers == []
    marked = executor.execute(
        step(2, "firmware_flash", "destructive", component="BMC", image_ref="bmc-1.13"), context()
    )
    assert marked.exit_code == 0
    assert marked.summary == "firmware_flash 2: done on lab-gx8-01 (approved for this run)."
    assert hal.target(TARGET).ssh_calls == [
        ["firmware_flash", "component=BMC", "image_ref=bmc-1.13"]
    ]
    assert hal.fence_markers == [(TARGET, "--- slas fence T-validation-0001 firmware_flash ---")]
