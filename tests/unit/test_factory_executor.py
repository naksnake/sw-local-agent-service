"""P9's done-when, against fakes: a fake MES ticket triggers the 9-step loop on a fake station;
PASS needs 3 of 3 votes; a planted failure holds the station and drafts a line-lead ticket;
the EN/中文 line SOP and a station backup land on the ticket. Plus: split votes, no voters,
the line lead's decision, station leases, the file-drop MES adapter, templates in step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slas_factory_executor.executor import FactoryExecutor
from slas_factory_executor.mes import FakeMesAdapter, FileDropMesAdapter, read_outbox
from slas_factory_executor.primitives import PRIMITIVES, render_plan_schema, render_primitives_yaml
from slas_factory_executor.templates import (
    FINAL_TEST_9_STEPS,
    TemplateError,
    compile_template,
    default_templates,
    load_templates,
    render_template_yaml,
    template_from_mapping,
)
from slas_hal.credentials import FakeCredentialResolver
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext
from slas_kernel.kernel import Kernel
from slas_kernel.rca import FakeCrossChecker
from slas_kernel.skills import SkillGate
from slas_kernel.store import FileTicketStore
from slas_orchestrator.factory.agent import FactoryAgent, TriggerError
from slas_schemas.job import MesTicket, Upload
from slas_schemas.ticket import Ticket, TicketState
from slas_schemas.vote import Vote
from slas_skills.library import LIBRARY
from slas_skills.schema import parse_skill
from slas_skills.state import SkillStateStore
from slas_station_runner.fakes import FakeStation, Plant
from slas_station_runner.server import InProcessRunnerClient

REPO_ROOT = Path(__file__).resolve().parents[2]
STATION = "station-07"
KEY = "k" * 32
MES = MesTicket(ticket_no="MES-88131", station=STATION, unit_sn="SN-GX8-0100", requested_by="mes")
SECRETS = {"env:FACTORY_BATCH_KEY": KEY, "env:STATION_OPERATOR_PASSWORD": "Op3rator-Passw0rd!"}


def votes(*verdicts: str) -> list[Vote]:
    return [
        Vote(
            voter=f"voter-{i}",
            verdict=v,
            reason="every check passed"
            if v == "approve"
            else "the GPU temperature trend worries me",
            confidence=0.9,
        )
        for i, v in enumerate(verdicts, start=1)
    ]


class Line:
    """One factory line: a fake station behind its runner, the executor, the agent, the kernel."""

    def __init__(
        self,
        data_root: Path,
        *,
        plant: Plant | None = None,
        voters: list[Vote] | None = None,
        agreed: bool = True,
        no_voters: bool = False,
        skills_on: bool = True,
    ) -> None:
        if voters is None and not no_voters:
            voters = votes("approve", "approve", "approve")
        self.data_root = data_root
        self.clock = FakeClock()
        # The installation's skill library and its enablement records (ADR-0013): the two
        # shipped skills, turned on for the Factory Agent unless a test says otherwise.
        self.skills = {name: parse_skill(data) for name, data in LIBRARY.items()}
        self.skill_state = SkillStateStore(data_root / "Skills" / "library")
        for skill in self.skills.values():
            self.skill_state.record_import(skill, by="platform", now=self.clock.now())
            if skills_on and "factory" in skill.agents:
                self.skill_state.enable(skill, "factory", by="lee", now=self.clock.now())
        self.station = FakeStation(
            STATION, state_dir=data_root / "station", clock=self.clock, plant=plant
        )
        self.station.trust("factory-2026-09", KEY.encode())
        self.checker = FakeCrossChecker(voters, agreed=agreed) if voters is not None else None
        self.executor = FactoryExecutor(
            runners={STATION: InProcessRunnerClient(self.station.runner)},
            resolver=FakeCredentialResolver(SECRETS),
            signing_key_ref="env:FACTORY_BATCH_KEY",
            signing_key_id="factory-2026-09",
            data_root=data_root,
            clock=self.clock,
            cross_checker=self.checker,
        )
        self.agent = FactoryAgent(plans_dir=data_root / "Factory" / "Plans")
        self.store = FileTicketStore(data_root)
        self.plan_checker = FakeCrossChecker(
            [
                v.model_copy(update={"reason": "the loop stays within the line rules"})
                for v in votes("approve", "approve", "approve")
            ],
            agreed=True,
        )
        self.kernel = Kernel(
            data_root=data_root,
            agent=self.agent,
            executor=self.executor,
            store=self.store,
            clock=FakeClock(),
            plan_checker=self.plan_checker,
            skill_gate=SkillGate(library=self.skills, state=self.skill_state, clock=self.clock),
        )
        self.mes = FakeMesAdapter([MES])

    def run(self, mes: MesTicket = MES) -> Ticket:
        ticket = self.kernel.run(mes)
        state = self.executor.state_for(ticket.id)
        assert state is not None
        self.agent.report_verdict(ticket, state, self.mes)
        return ticket


# --- the done-when -------------------------------------------------------------------------


def test_a_fake_mes_ticket_runs_the_nine_step_loop_and_passes_with_three_votes(
    tmp_path: Path,
) -> None:
    line = Line(tmp_path)
    (mes_ticket,) = line.mes.poll()
    ticket = line.run(mes_ticket)

    assert ticket.id == "T-factory-0001" and ticket.state is TicketState.DONE
    assert ticket.title == "Final test of SN-GX8-0100 on station-07"
    assert ticket.job.target is not None and ticket.job.target.kind == "station"
    assert [r.status for r in ticket.steps] == ["done"] * 10
    assert [r.title for r in ticket.steps] == [
        "Lease the station and bind the unit",
        "Power the unit on through the fixture",
        "Log in to the station and start BurnIn",
        "Wait for BurnIn to report the test complete",
        "Read the BurnIn result",
        "Read the sensors and compare with the limits",
        "Check that the event log is empty",
        "Back up the station state",
        "Decide PASS or FAIL",
        "Release station-07",
    ]
    # The plan went to the voters before anything ran, and the verdict step asked them again.
    assert line.plan_checker.calls[0][0] == "plan_approval"
    assert line.checker is not None and line.checker.calls[0][0] == "factory_pass"
    assert line.checker.calls[0][1][0] == "Unit SN-GX8-0100 on station-07"
    assert "Event log: empty" in line.checker.calls[0][1]
    verdict = ticket.step_record("verdict")
    assert verdict is not None and verdict.observation is not None
    assert verdict.observation.summary.startswith("PASS: 3 of 3 voters say PASS.")
    assert [v.voter for v in verdict.observation.votes] == ["voter-1", "voter-2", "voter-3"]
    assert len(ticket.votes) == 6, "3 plan votes and 3 verdict votes"

    # The test-step map and the screenshot strip.
    state = line.executor.state_for(ticket.id)
    assert state is not None
    assert [c.status for c in state.cells] == ["ok"] * 10
    assert state.verdict == "PASS" and state.held is False
    assert state.sentence() == "10 of 10 steps done. Verdict: PASS (3 of 3 voters)."
    login = state.cells[2]
    assert len(login.screenshots) >= 16 and all(p.endswith(".png") for p in login.screenshots)
    assert all(Path(p).read_bytes().startswith(b"\x89PNG") for p in login.screenshots)
    assert all(
        Path(p).parent == line.executor.job_dir(ticket.id) / "screens" for p in login.screenshots
    )
    assert ticket.logs is not None and len(ticket.logs.screenshots) >= 18, (
        "screenshots reach the log bundle"
    )
    assert (
        state.result["result"] == "PASS"
        and state.sensors["gpu_temp_c"] == 61
        and state.event_log == []
    )

    # The station: powered on, logged in with the real password, test started; lease released.
    assert line.station.power == "on" and line.station.test_started
    assert line.station.login_typed == ["operator", "Op3rator-Passw0rd!"]
    assert line.executor.leases.holder(STATION, line.clock.now()) is None
    # No secret in any journal on either side.
    for path in (
        tmp_path / "station" / "runner-journal.jsonl",
        line.executor.job_dir(ticket.id) / "journal.jsonl",
    ):
        text = path.read_text(encoding="utf-8")
        assert "Op3rator-Passw0rd!" not in text and KEY not in text
    assert (
        "Op3rator-Passw0rd!" not in (tmp_path / "Tickets" / ticket.id / "journal.jsonl").read_text()
    )

    # The station backup landed on the ticket as an export.
    (backup,) = ticket.exports
    assert backup.kind == "backup"
    backup_dir = Path(backup.path)
    assert backup_dir == tmp_path / "Backups" / "stations" / STATION / ticket.id
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["versions"]["burnin"] == "3.2.1"
    assert sorted(Path(k).name for k in manifest["files"]) == ["burnin.ini", "station.log"]
    assert (backup_dir / "files").is_dir() and (backup_dir / "versions.json").is_file()

    # Production line SOP in both languages.
    assert ticket.sop is not None
    en = Path(ticket.sop.en).read_text(encoding="utf-8")
    zh = Path(ticket.sop.zh).read_text(encoding="utf-8")
    assert "Decide PASS or FAIL" in en and "SN-GX8-0100" in zh
    assert "3 of 3 voters" in en

    # The verdict went back to the MES.
    (reported,) = line.mes.reported
    assert (reported.ticket_no, reported.unit_sn, reported.verdict) == (
        "MES-88131",
        "SN-GX8-0100",
        "PASS",
    )
    assert reported.ticket_id == ticket.id and reported.decided_by == "3 of 3 voters"
    assert reported.sop_en == ticket.sop.en and reported.sop_zh == ticket.sop.zh


def test_a_planted_failure_holds_the_station_and_drafts_a_ticket_for_the_line_lead(
    tmp_path: Path,
) -> None:
    line = Line(tmp_path, plant="fail_result")
    ticket = line.run()

    assert ticket.state is TicketState.NEEDS_REVIEW
    statuses = [r.status for r in ticket.steps]
    assert statuses == ["done"] * 8 + ["failed", "pending"], "the release never ran"
    verdict = ticket.step_record("verdict")
    assert verdict is not None and verdict.observation is not None
    assert verdict.observation.summary.startswith(
        "FAIL: Unit SN-GX8-0100 failed the final test on station-07: BurnIn reported FAIL "
        "(gpu-memory). "
        "The unit stays on and station-07 is held; a ticket is drafted for the line lead."
    )
    assert line.checker is not None and line.checker.calls == [], (
        "a FAIL is never put to the voters"
    )

    state = line.executor.state_for(ticket.id)
    assert state is not None
    assert state.verdict == "FAIL" and state.held is True
    assert [c.status for c in state.cells] == ["ok"] * 8 + ["failed", "waiting"]
    assert (
        state.sentence()
        == "9 of 10 steps done. Verdict: FAIL; the station is held for the line lead."
    )
    holder = line.executor.leases.holder(STATION, line.clock.now())
    assert holder is not None and holder.ticket_id == ticket.id, "the station stays leased"
    assert line.station.power == "on", "the unit stays on"

    # The draft ticket for the line lead: a child of the run, routed to test engineering.
    (finding,) = ticket.findings
    assert finding.headline() == (
        "[Issue] Unit SN-GX8-0100 failed the final test on station-07: BurnIn reported FAIL "
        "(gpu-memory) | [Owner] TE"
    )
    child = line.store.load("T-factory-0002")
    assert child.parent == ticket.id and child.title == finding.headline()
    assert child.job.target is not None and child.job.target.ref == STATION
    assert finding.evidence[0].startswith('result: {"unit": "under-test", "result": "FAIL"')
    # Backup happened before the verdict, so the held station's state is on the ticket too.
    assert [e.kind for e in ticket.exports] == ["backup"]
    assert ticket.sop is not None and Path(ticket.sop.zh).is_file()
    (reported,) = line.mes.reported
    assert reported.verdict == "FAIL" and reported.decided_by == "the deterministic gate"

    # The line lead decides; the station is released and the decision recorded.
    decided = line.executor.decide(
        ticket.id, verdict="FAIL", by="lee", note="Scrap the unit; GPU memory."
    )
    assert decided.held is False and decided.decided_by == "lee (line lead)"
    assert decided.verdict_sentence == "FAIL: decided by lee. Scrap the unit; GPU memory."
    assert line.executor.leases.holder(STATION, line.clock.now()) is None
    journal = [
        json.loads(line)
        for line in (line.executor.job_dir(ticket.id) / "journal.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["line_lead"] == "lee" and journal[-1]["verdict"] == "FAIL"
    # A second unit can now use the station.
    again = Line(tmp_path / "again")
    assert (
        again.run(MES.model_copy(update={"ticket_no": "MES-88132", "unit_sn": "SN-GX8-0101"})).state
        is TicketState.DONE
    )


def test_a_skill_turned_off_stops_the_job_at_plan_and_nothing_reaches_the_station(
    tmp_path: Path,
) -> None:
    """ADR-0013: the kernel's gate, not the executor, decides; the station never hears of it."""
    line = Line(tmp_path, skills_on=False)
    ticket = line.kernel.run(MES)
    assert ticket.state is TicketState.FAILED and ticket.plan is None
    assert ticket.history[-1].reason == (
        "Log in to the test station and start BurnIn is not turned on for the Factory Agent."
    )
    assert line.station.power == "off" and line.station.login_typed == []
    assert line.executor.state_for(ticket.id) is None, "no step map: no step ran"
    assert line.executor.leases.holder(STATION, line.clock.now()) is None
    assert line.plan_checker.calls == [], "the voters see a plan only after the gate"
    # Turning the skill on takes effect for the very next job, with nothing restarted.
    line.skill_state.enable(
        line.skills["station-login-burnin"], "factory", by="lee", now=line.clock.now()
    )
    assert line.run().state is TicketState.DONE


def test_the_executor_refuses_a_skill_step_the_kernel_did_not_compile(tmp_path: Path) -> None:
    line = Line(tmp_path)
    plan = compile_template(
        default_templates()["final-test-9-steps"],
        job_id="job-x",
        station=STATION,
        unit_sn="SN-1",
        mes_ticket_no="MES-1",
        now=line.clock.now(),
    )
    step = next(s for s in plan.steps if s.primitive == "skill")
    context = ExecutionContext(
        ticket_id="T-factory-0099", job_id="job-x", agent="factory", user="mes"
    )
    observation = line.executor.execute(step, context)
    assert observation.exit_code == 2
    assert (
        observation.summary == "Step login-burnin reached the station without compiled skill steps."
    )
    assert "never compiles a skill itself" in observation.stderr
    assert line.station.login_typed == []


def test_sensor_and_event_log_plants_fail_the_gate_too(tmp_path: Path) -> None:
    hot = Line(tmp_path / "hot", plant="sensor_hot").run()
    verdict = hot.step_record("verdict")
    assert verdict is not None and verdict.observation is not None
    assert "gpu_temp_c is 96, above the limit of 85" in verdict.observation.summary
    assert hot.findings[0].headline().endswith("| [Owner] TE")

    log = Line(tmp_path / "log", plant="event_log").run()
    verdict = log.step_record("verdict")
    assert verdict is not None and verdict.observation is not None
    assert (
        "the event log is not empty (2026-09-14 08:12:03 Critical: PCIe uncorrectable error slot 3)"
        in verdict.observation.summary
    )
    event = log.step_record("event-log")
    assert event is not None and event.observation is not None
    assert event.observation.summary.startswith("The event log has 1 entry:")


def test_two_of_three_votes_or_no_voters_leave_the_decision_to_the_line_lead(
    tmp_path: Path,
) -> None:
    split = Line(tmp_path / "split", voters=votes("approve", "approve", "concern"), agreed=False)
    ticket = split.run()
    assert ticket.state is TicketState.NEEDS_REVIEW
    verdict = ticket.step_record("verdict")
    assert verdict is not None and verdict.observation is not None
    summary = verdict.observation.summary
    assert summary.startswith(
        "The line lead decides: Unit SN-GX8-0100 passed every check on station-07, but only "
        "2 of 3 voters say PASS (voter-3"
    )
    assert (
        "The unit stays on and station-07 is held; a ticket is drafted for the line lead."
        in summary
    )
    assert summary.endswith(
        "2 of 3 agree with the conclusion. The conclusion is marked uncertain in the report."
    )
    assert len(verdict.observation.votes) == 3
    state = split.executor.state_for(ticket.id)
    assert state is not None and state.verdict == "line_lead" and state.held
    assert (
        state.sentence() == "9 of 10 steps done. The voters did not agree; the line lead decides. "
        "The station is held."
    )
    assert ticket.findings[0].issue.endswith("line lead decision needed on station-07")
    assert ticket.findings[0].severity == "S3"
    assert split.mes.reported[0].verdict == "line_lead"

    nobody = Line(tmp_path / "nobody", no_voters=True)
    ticket = nobody.run()
    verdict = ticket.step_record("verdict")
    assert verdict is not None and verdict.observation is not None
    assert "no voters are configured" in verdict.observation.summary
    assert (
        nobody.executor.decide(ticket.id, verdict="PASS", by="lee", note="Checked by hand.").verdict
        == "PASS"
    )
    assert nobody.mes.reported[0].decided_by == "nobody yet"


def test_one_job_per_station_at_a_time(tmp_path: Path) -> None:
    first = Line(tmp_path, plant="fail_result")
    held = first.run()
    assert first.executor.leases.holder(STATION, first.clock.now()) is not None
    # A second job on the same station, same data root: the lease step fails at once.
    second_agent = FactoryAgent(plans_dir=tmp_path / "Factory" / "Plans")
    kernel = Kernel(
        data_root=tmp_path,
        agent=second_agent,
        executor=first.executor,
        store=first.store,
        clock=FakeClock(),
    )
    blocked = kernel.run(
        MES.model_copy(update={"ticket_no": "MES-88132", "unit_sn": "SN-GX8-0101"})
    )
    assert blocked.state is TicketState.NEEDS_REVIEW
    lease = blocked.step_record("lease")
    assert lease is not None and lease.observation is not None
    assert lease.observation.summary.startswith(
        f"{STATION} is busy: {STATION} is leased to {held.id} (mes) until"
    )
    assert [r.status for r in blocked.steps] == ["failed"] + ["pending"] * 9
    assert first.station.shell.argv_seen().count(["fixture-ctl", "power", "on"]) == 1, (
        "the fixture was not touched again"
    )


def test_label_scans_and_templates_drive_the_wizard(tmp_path: Path) -> None:
    agent = FactoryAgent(plans_dir=tmp_path / "Plans")
    job = agent.ingest(
        Upload(filename="label.txt", uploaded_by="ops", content="SN SN-GX8-0200 station station-07")
    )
    assert job.title == "Final test of SN-GX8-0200 on station-07" and job.user == "ops"
    mes = agent.mes_ticket(job.id)
    assert mes.ticket_no.startswith("manual-") and mes.payload == {"source": "label.txt"}
    with pytest.raises(TriggerError) as info:
        agent.ingest(Upload(filename="label.txt", uploaded_by="ops", content="no useful text"))
    assert info.value.message.what_happened == "label.txt names no unit and station."
    with pytest.raises(TriggerError) as info:
        agent.choose_template(job, "nope")
    assert info.value.message.what_happened == "There is no test-loop template called nope."
    template = agent.choose_template(job, "final-test-9-steps")
    assert (
        template.sentence() == "Final test, 9 steps: 9 steps, using the station-login-burnin skill."
    )
    plan = agent.plan(job)
    assert len(plan.steps) == 10 and plan.steps[0].args["unit_sn"] == "SN-GX8-0200"
    assert plan.steps[2].args["secret_refs"] == {"password": "env:STATION_OPERATOR_PASSWORD"}
    assert all(s.args["loop_steps"] == 10 for s in plan.steps)
    assert plan.summary == (
        "Final test, 9 steps for unit SN-GX8-0200 on station-07: 10 steps, using the "
        "station-login-burnin skill. PASS needs 3 of 3 voters; anything else holds the station "
        "for the line lead."
    )
    assert (tmp_path / "Plans" / plan.id / "plan.yaml").is_file()
    assert agent.sop_template().kind == "production_line"
    assert (
        agent.verify(
            plan.steps[0],
            __import__("slas_schemas.ticket", fromlist=["Observation"]).Observation(exit_code=1),
        ).outcome
        == "fail"
    )


def test_templates_and_primitives_are_validated_and_in_step(tmp_path: Path) -> None:
    assert (REPO_ROOT / "templates" / "factory" / "final-test-9-steps.yaml").read_text(
        encoding="utf-8"
    ) == render_template_yaml(FINAL_TEST_9_STEPS)
    assert (REPO_ROOT / "plans" / "primitives" / "factory.yaml").read_text(
        encoding="utf-8"
    ) == render_primitives_yaml()
    assert (REPO_ROOT / "plans" / "schema" / "factory-plan.schema.json").read_text(
        encoding="utf-8"
    ) == render_plan_schema()
    schema = json.loads(render_plan_schema())
    assert {v["properties"]["primitive"]["const"] for v in schema["$defs"]["step"]["oneOf"]} == set(
        PRIMITIVES
    )
    assert PRIMITIVES["station_config_change"].risk == "destructive"

    templates = default_templates()
    assert list(templates) == ["final-test-9-steps"]
    with pytest.raises(TemplateError) as info:
        template_from_mapping(
            {
                **FINAL_TEST_9_STEPS,
                "steps": [{"id": "x", "primitive": "shell", "title": "t", "args": {}}],
            }
        )
    assert "unknown verb 'shell'" in info.value.message.what_happened
    with pytest.raises(TemplateError) as info:
        template_from_mapping(
            {
                **FINAL_TEST_9_STEPS,
                "steps": [{"id": "x", "primitive": "read_result", "title": "t", "args": {}}],
            }
        )
    assert (
        info.value.message.what_happened
        == "Step x of final-test-9-steps is missing station, command for read_result."
    )
    with pytest.raises(TemplateError):
        template_from_mapping({"id": "bad"})

    # A line's own template as JSON under Factory/Templates joins the shipped one.
    custom = {
        **FINAL_TEST_9_STEPS,
        "id": "quick-check",
        "name": "Quick check",
        "skills": [],
        "steps": FINAL_TEST_9_STEPS["steps"][:2],
    }
    (tmp_path / "quick-check.template.json").write_text(json.dumps(custom), encoding="utf-8")
    loaded = load_templates(tmp_path)
    assert sorted(loaded) == ["final-test-9-steps", "quick-check"]
    plan = compile_template(
        loaded["quick-check"],
        job_id="j",
        station="station-09",
        unit_sn="SN1",
        mes_ticket_no="M1",
        now=FakeClock().now(),
    )
    assert [s.primitive for s in plan.steps] == [
        "lease_station",
        "station_command",
        "release_station",
    ]
    assert plan.steps[0].args == {
        "station": "station-09",
        "unit_sn": "SN1",
        "mes_ticket_no": "M1",
        "loop_step": 1,
        "loop_steps": 3,
    }


def test_file_drop_mes_adapter_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "Factory" / "MES"
    adapter = FileDropMesAdapter(root)
    (root / "inbox" / "b.json").write_text(MES.model_dump_json(), encoding="utf-8")
    (root / "inbox" / "a.json").write_text('{"ticket_no": "MES-1"}', encoding="utf-8")
    (root / "inbox" / "c.json").write_text("{not json", encoding="utf-8")
    tickets = adapter.poll()
    assert [t.ticket_no for t in tickets] == ["MES-88131"]
    assert adapter.pending() == ["MES-88131"] and adapter.rejected() == ["a.json", "c.json"]
    reason = (root / "rejected" / "a.json.reason.txt").read_text(encoding="utf-8")
    assert reason.startswith("a.json is not a usable production ticket.\nLikely cause:")
    assert "ticket_no, station, unit_sn and requested_by" in reason
    assert adapter.poll() == [], "the inbox is empty now"

    line = Line(tmp_path)
    ticket = line.kernel.run(tickets[0])
    state = line.executor.state_for(ticket.id)
    assert state is not None
    verdict = line.agent.report_verdict(ticket, state, adapter)
    assert verdict.verdict == "PASS"
    assert adapter.pending() == [] and (root / "done" / "MES-88131.json").is_file()
    outbox = read_outbox(root, "MES-88131")
    assert (
        outbox == verdict and outbox.sop_zh is not None and outbox.sop_zh.endswith("sop.zh-Hant.md")
    )
