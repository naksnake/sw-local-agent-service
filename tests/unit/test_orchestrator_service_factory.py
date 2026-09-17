"""The orchestrator's `/v1/factory` routes over a REAL `FactoryExecutor` behind a fake
executor *service*: a label parses into a trigger (or a problem), stations and templates are
listed, a job runs the nine-step loop on a fake station and passes with 3 of 3 votes, a
planted failure holds the station until the line lead decides (factory:verdict), the operator
pauses and resumes the runner (factory:control), and the MES gets the verdict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from slas_factory_executor.executor import FactoryExecutor, JobState, StepCell
from slas_factory_executor.mes import FakeMesAdapter
from slas_hal.credentials import FakeCredentialResolver
from slas_http.app import create_service_app
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.kernel import Kernel
from slas_kernel.rca import FakeCrossChecker
from slas_kernel.skills import SkillGate
from slas_kernel.store import FileTicketStore
from slas_orchestrator.factory.agent import FactoryAgent
from slas_orchestrator.remote import ExecutorReader, HttpExecutor
from slas_orchestrator.service import factory, validation
from slas_orchestrator.service.factory import FactoryDeps
from slas_orchestrator.service.models import JobRules, JobView
from slas_orchestrator.service.validation import WatchedTicketStore
from slas_orchestrator.service.views_factory import (
    job_view,
    plan_rules_sentence,
    rules_sentence,
    station_views,
    waiting_cells,
)
from slas_schemas.job import Job, MesTicket, TargetRef
from slas_schemas.ticket import Ticket, TicketState
from slas_schemas.vote import Vote
from slas_skills.library import LIBRARY
from slas_skills.schema import parse_skill
from slas_skills.state import SkillStateStore
from slas_station_runner.control import Controller
from slas_station_runner.fakes import FakeStation, Plant
from slas_station_runner.server import InProcessRunnerClient
from tests.unit.orchestrator_harness import SyncRunner, factory_executor_app, service_client

STATION = "station-07"
KEY = "k" * 32
MES = MesTicket(ticket_no="MES-88131", station=STATION, unit_sn="SN-GX8-0100", requested_by="mes")
SECRETS = {"env:FACTORY_BATCH_KEY": KEY, "env:STATION_OPERATOR_PASSWORD": "Op3rator-Passw0rd!"}
RULES = {"voters": 3, "on_fail": "hold", "export_sop": True, "backup_station": True}

OPS = Identity("ops@slas.local", "Ops", frozenset())
LEE = Identity("lee@slas.local", "Lee", frozenset({"factory:verdict", "factory:control"}))


def votes(*verdicts: str) -> list[Vote]:
    return [
        Vote(
            voter=f"voter-{i}",
            verdict=v,
            reason="every check passed" if v == "approve" else "the GPU temperature worries me",
            confidence=0.9,
        )
        for i, v in enumerate(verdicts, start=1)
    ]


class Bench:
    """The orchestrator app with the Factory router over a fake factory-executor service that
    performs steps with the real executor on a fake station."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        plant: Plant | None = None,
        voters: list[Vote] | None = None,
        agreed: bool = True,
        vnc: bool = True,
    ) -> None:
        self.clock = FakeClock()
        skills = {name: parse_skill(data) for name, data in LIBRARY.items()}
        skill_state = SkillStateStore(tmp_path / "Skills" / "library")
        for skill in skills.values():
            skill_state.record_import(skill, by="platform", now=self.clock.now())
            if "factory" in skill.agents:
                skill_state.enable(skill, "factory", by="lee", now=self.clock.now())
        self.station = FakeStation(
            STATION, state_dir=tmp_path / "station", clock=self.clock, plant=plant
        )
        self.station.trust("factory-2026-09", KEY.encode())
        self.station.runner.controller = Controller(STATION, self.clock, poll_s=0.01)
        self.station.runner.config.vnc.enabled = vnc
        self.checker = FakeCrossChecker(
            voters if voters is not None else votes("approve", "approve", "approve"), agreed=agreed
        )
        self.executor = FactoryExecutor(
            runners={STATION: InProcessRunnerClient(self.station.runner)},
            resolver=FakeCredentialResolver(SECRETS),
            signing_key_ref="env:FACTORY_BATCH_KEY",
            signing_key_id="factory-2026-09",
            data_root=tmp_path,
            clock=self.clock,
            cross_checker=self.checker,
        )
        self.agent = FactoryAgent(plans_dir=tmp_path / "Factory" / "Plans")
        self.pending = [MES, MES.model_copy(update={"ticket_no": "MES-88132", "unit_sn": "SN-1"})]
        self.executor_app = factory_executor_app(
            self.executor, {STATION: self.station}, self.agent.templates, self.pending
        )
        self.client = service_client("factory-executor", self.executor_app)
        self.store = WatchedTicketStore(FileTicketStore(tmp_path))
        self.plan_checker = FakeCrossChecker(
            [
                v.model_copy(update={"reason": "the loop stays within the line rules"})
                for v in votes("approve", "approve", "approve")
            ],
            agreed=True,
        )
        self.kernel = Kernel(
            data_root=tmp_path,
            agent=self.agent,
            executor=HttpExecutor(self.client),
            store=self.store,
            clock=FakeClock(),
            plan_checker=self.plan_checker,
            skill_gate=SkillGate(library=skills, state=skill_state, clock=self.clock),
        )
        self.runner = SyncRunner()
        self.mes = FakeMesAdapter()
        self.deps = FactoryDeps(
            kernel=self.kernel,
            agent=self.agent,
            store=self.store,
            executor_reader=ExecutorReader(self.client),
            runner=self.runner,
            mes=self.mes,
        )
        self.app = create_service_app(
            "agent-core-orchestrator", routers=(validation.router, factory.router)
        )
        self.app.state.factory = self.deps
        self.http = TestClient(self.app, raise_server_exceptions=False)

    def start(
        self,
        trigger: MesTicket = MES,
        template_id: str = "final-test-9-steps",
        rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.http.post(
            "/v1/factory/jobs",
            json={
                "trigger": trigger.model_dump(mode="json"),
                "template_id": template_id,
                "rules": rules if rules is not None else RULES,
            },
            headers=OPS.headers(),
        )
        assert response.status_code == 200, response.text
        return dict(response.json())


# --- the wizard -----------------------------------------------------------------------------------


def test_mes_tickets_labels_stations_and_templates_feed_the_wizard(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    tickets = bench.http.get("/v1/factory/mes-tickets", headers=OPS.headers()).json()
    assert [t["ticket_no"] for t in tickets] == ["MES-88131", "MES-88132"]
    assert MesTicket.model_validate(tickets[0]) == MES

    ok = bench.http.post(
        "/v1/factory/labels/parse",
        json={"text": "SN SN-GX8-0200 station station-07"},
        headers=OPS.headers(),
    )
    assert ok.status_code == 200
    assert set(ok.json()) == {"trigger"}, "no null problem key travels"
    trigger = ok.json()["trigger"]
    assert trigger["unit_sn"] == "SN-GX8-0200" and trigger["station"] == STATION
    assert (
        trigger["ticket_no"].startswith("manual-") and trigger["requested_by"] == "ops@slas.local"
    )
    assert trigger["payload"] == {"source": "label.txt"}
    bad = bench.http.post(
        "/v1/factory/labels/parse", json={"text": "no useful text"}, headers=OPS.headers()
    )
    assert set(bad.json()) == {"problem"}
    assert bad.json()["problem"].startswith(
        "label.txt names no unit and station. A label scan or manual entry must carry"
    )
    assert bad.json()["problem"].endswith("Scan the label again or type both values in the wizard.")
    assert bench.http.post("/v1/factory/labels/parse", json={"text": "x"}).status_code == 401

    stations = bench.http.get("/v1/factory/stations", headers=OPS.headers()).json()
    assert stations == [
        {
            "name": STATION,
            "description": "Fake station station-07",
            "free": True,
            "holder": None,
            "enrolled": True,
            "sentence": "station-07: free.",
        }
    ]
    templates = bench.http.get("/v1/factory/templates", headers=OPS.headers()).json()
    assert [t["id"] for t in templates] == ["final-test-9-steps"]
    assert templates[0]["name"] == "Final test, 9 steps"
    assert templates[0]["skills"] == ["station-login-burnin"]
    assert templates[0]["steps"][0] == "Lease the station and bind the unit"
    assert len(templates[0]["steps"]) == 9
    assert templates[0]["sentence"] == (
        "Final test, 9 steps: 9 steps, using the station-login-burnin skill."
    )
    assert templates[0]["description"].startswith("Power the unit on, log in to the station")


# --- jobs -----------------------------------------------------------------------------------------


def test_a_job_runs_the_nine_step_loop_through_the_remote_executor_and_passes_with_three_votes(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path)
    view = bench.start()

    assert view["ticket_id"] == "T-factory-0001" and view["state"] == "Done"
    assert view["title"] == "Final test of SN-GX8-0100 on station-07"
    assert (view["station"], view["unit_sn"], view["mes_ticket_no"]) == (
        STATION,
        "SN-GX8-0100",
        "MES-88131",
    )
    assert view["sentence"] == "10 of 10 steps done. Verdict: PASS (3 of 3 voters)."
    assert view["verdict"] == "PASS" and view["held"] is False
    assert view["decided_by"] == "3 of 3 voters"
    assert view["verdict_sentence"].startswith("PASS: 3 of 3 voters say PASS.")
    assert [c["status"] for c in view["cells"]] == ["ok"] * 10
    assert [c["title"] for c in view["cells"]][-2:] == ["Decide PASS or FAIL", "Release station-07"]
    login = view["cells"][2]
    assert len(login["screenshots"]) >= 16 and login["screenshot"] == login["screenshots"][-1]
    assert all(Path(p).suffix == ".png" for p in login["screenshots"])
    assert view["cells"][0]["screenshot"] is None
    verdict_votes = [v for v in view["votes"] if v.endswith("every check passed")]
    assert verdict_votes == [f"voter-{i} approves: every check passed" for i in (1, 2, 3)]
    assert len(view["votes"]) == 6, "3 plan votes and 3 verdict votes"
    assert view["draft_ticket_id"] is None
    assert view["backup_path"] == str(
        tmp_path / "Backups" / "stations" / STATION / "T-factory-0001"
    )
    assert view["rules_sentence"] == (
        "PASS needs 3 of 3 voters; anything else holds the station for the line lead. "
        "The SOP is exported in English and Chinese. The station state is backed up."
    )
    assert bench.runner.started == [bench.store.load("T-factory-0001").job.id]

    # The station really ran: powered on, logged in, test started, lease released; the
    # verdict step's evidence went to the voters; the MES got the verdict.
    assert bench.station.power == "on" and bench.station.test_started
    assert bench.station.login_typed == ["operator", "Op3rator-Passw0rd!"]
    assert bench.executor.leases.holder(STATION, bench.clock.now()) is None
    assert bench.checker.calls[0][0] == "factory_pass"
    (reported,) = bench.mes.reported
    assert (reported.ticket_no, reported.verdict, reported.ticket_id) == (
        "MES-88131",
        "PASS",
        "T-factory-0001",
    )
    assert reported.sop_zh is not None and reported.sop_zh.endswith("sop.zh-Hant.md")

    # The list and the detail agree; a validation ticket id is not a job.
    listed = bench.http.get("/v1/factory/jobs", headers=OPS.headers()).json()
    assert [j["ticket_id"] for j in listed] == ["T-factory-0001"]
    assert JobView.model_validate(listed[0]).cells == JobView.model_validate(view).cells
    detail = bench.http.get("/v1/factory/jobs/T-factory-0001", headers=OPS.headers()).json()
    assert detail == view
    assert (
        bench.http.get("/v1/factory/jobs/T-factory-0009", headers=OPS.headers()).status_code == 404
    )

    # An unknown template is a 400 before anything is created.
    refused = bench.http.post(
        "/v1/factory/jobs",
        json={"trigger": MES.model_dump(mode="json"), "template_id": "nope", "rules": RULES},
        headers=OPS.headers(),
    )
    assert refused.status_code == 400
    assert refused.json()["what_happened"] == "There is no test-loop template called nope."
    assert bench.store.list_ids() == ["T-factory-0001"]


def test_rules_only_the_backup_is_a_choice_and_the_sentence_says_so(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    view = bench.start(
        rules={"voters": 2, "on_fail": "continue", "export_sop": False, "backup_station": False}
    )
    assert view["state"] == "Done" and view["verdict"] == "PASS"
    assert [c["title"] for c in view["cells"]] == [
        "Lease the station and bind the unit",
        "Power the unit on through the fixture",
        "Log in to the station and start BurnIn",
        "Wait for BurnIn to report the test complete",
        "Read the BurnIn result",
        "Read the sensors and compare with the limits",
        "Check that the event log is empty",
        "Decide PASS or FAIL",
        "Release station-07",
    ]
    assert view["backup_path"] is None
    assert view["rules_sentence"] == (
        "PASS needs 3 of 3 voters; anything else holds the station for the line lead. "
        "The SOP is exported in English and Chinese. The station state is not backed up. "
        "Voters, on-fail behaviour and SOP export are fixed on this installation; the other "
        "values you sent were not applied."
    )
    # The variant never shows in the wizard's template list; the shipped one does.
    templates = bench.http.get("/v1/factory/templates", headers=OPS.headers()).json()
    assert [t["id"] for t in templates] == ["final-test-9-steps"]
    assert "final-test-9-steps-no-backup" in bench.agent.templates
    # Re-reading the job derives the same sentence from its plan, minus the note.
    again = bench.http.get("/v1/factory/jobs/T-factory-0001", headers=OPS.headers()).json()
    assert again["rules_sentence"].endswith("The station state is not backed up.")
    template = bench.agent.templates["final-test-9-steps"]
    assert rules_sentence(JobRules(), template).endswith("The station state is backed up.")


def test_a_planted_failure_holds_the_station_until_the_line_lead_decides(tmp_path: Path) -> None:
    bench = Bench(tmp_path, plant="fail_result")
    view = bench.start()
    assert view["state"] == "Needs review" and view["verdict"] == "FAIL" and view["held"] is True
    assert (
        view["sentence"]
        == "9 of 10 steps done. Verdict: FAIL; the station is held for the line lead."
    )
    assert [c["status"] for c in view["cells"]] == ["ok"] * 8 + ["failed", "waiting"]
    assert view["cells"][8]["sentence"].startswith(
        "FAIL: Unit SN-GX8-0100 failed the final test on station-07: BurnIn reported FAIL "
        "(gpu-memory)."
    )
    assert view["decided_by"] == "the deterministic gate"
    assert view["draft_ticket_id"] == "T-factory-0002"
    assert bench.mes.reported[0].verdict == "FAIL"
    stations = bench.http.get("/v1/factory/stations", headers=OPS.headers()).json()
    assert stations[0]["free"] is False
    assert stations[0]["holder"] == (
        "station-07 is leased to T-factory-0001 (mes) until 2026-09-14 16:00."
    )
    assert bench.checker.calls == [], "a FAIL is never put to the voters"

    # The line lead's decision needs the capability; the draft ticket is not a job.
    refused = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/decide",
        json={"verdict": "FAIL", "note": "Scrap it."},
        headers=OPS.headers(),
    )
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == "Ops may not decide PASS or FAIL for a unit."
    assert (
        bench.http.get("/v1/factory/jobs/T-factory-0002", headers=OPS.headers()).status_code == 404
    )
    decided = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/decide",
        json={"verdict": "FAIL", "note": "Scrap the unit; GPU memory."},
        headers=LEE.headers(),
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["verdict"] == "FAIL" and body["held"] is False
    assert body["decided_by"] == "Lee (line lead)"
    assert body["verdict_sentence"] == "FAIL: decided by Lee. Scrap the unit; GPU memory."
    assert body["state"] == "Needs review", "the ticket stays for review; the station is free"
    assert bench.executor.leases.holder(STATION, bench.clock.now()) is None
    assert bench.mes.reported[-1].verdict == "FAIL" and bench.mes.reported[-1].decided_by == (
        "Lee (line lead)"
    )
    assert len(bench.mes.reported) == 2
    missing = bench.http.post(
        "/v1/factory/jobs/T-factory-0042/decide",
        json={"verdict": "PASS", "note": ""},
        headers=LEE.headers(),
    )
    assert missing.status_code == 404
    assert missing.json()["what_happened"] == "There is no factory job T-factory-0042."


def test_split_votes_leave_the_verdict_to_the_line_lead_who_may_pass_it(tmp_path: Path) -> None:
    bench = Bench(tmp_path, voters=votes("approve", "approve", "concern"), agreed=False)
    view = bench.start()
    assert view["verdict"] == "line_lead" and view["held"] is True
    assert view["sentence"] == (
        "9 of 10 steps done. The voters did not agree; the line lead decides. The station is held."
    )
    assert "voter-3 has a concern: the GPU temperature worries me" in view["votes"]
    passed = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/decide",
        json={"verdict": "PASS", "note": "Checked by hand."},
        headers=LEE.headers(),
    ).json()
    assert passed["verdict"] == "PASS" and passed["verdict_sentence"] == (
        "PASS: decided by Lee. Checked by hand."
    )


def test_the_operator_pauses_resumes_and_aborts_the_runner_and_learns_where_to_watch(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path)
    bench.start()
    refused = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "pause"}, headers=OPS.headers()
    )
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == "Ops may not pause, resume or abort a station."

    paused = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "pause"}, headers=LEE.headers()
    )
    assert paused.status_code == 200, paused.text
    assert paused.json() == {
        "sentence": "Lee has taken over station-07; the runner sends no input until it is resumed.",
        "watch_url": "vnc://127.0.0.1:5901 (relayed over mTLS to https://station-07:8443)",
        "watch_problem": None,
        "station": STATION,
        "paused": True,
        "aborted": False,
        "by": "Lee",
    }
    status = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "status"}, headers=LEE.headers()
    ).json()
    assert status["paused"] is True and status["by"] == "Lee"
    resumed = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "resume"}, headers=LEE.headers()
    ).json()
    assert resumed["sentence"] == "The runner drives station-07." and resumed["paused"] is False
    aborted = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "abort"}, headers=LEE.headers()
    ).json()
    assert (
        aborted["aborted"] is True and aborted["sentence"] == "Lee aborted the run on station-07."
    )
    bad_verb = bench.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "dance"}, headers=LEE.headers()
    )
    assert bad_verb.status_code == 400
    missing = bench.http.post(
        "/v1/factory/jobs/T-factory-0042/control", json={"verb": "pause"}, headers=LEE.headers()
    )
    assert missing.status_code == 404

    # A station with VNC off says why watching is not possible.
    quiet = Bench(tmp_path / "quiet", vnc=False)
    quiet.start()
    answer = quiet.http.post(
        "/v1/factory/jobs/T-factory-0001/control", json={"verb": "pause"}, headers=LEE.headers()
    ).json()
    assert answer["watch_url"] is None
    assert answer["watch_problem"] == (
        "VNC is not enabled on station-07. The station record has VNC off. "
        "Enable it under Admin → Stations and re-enrol."
    )


def test_a_job_that_never_reached_the_station_shows_a_waiting_map(tmp_path: Path) -> None:
    """A second job on a held station: the lease step fails at once, and a job the executor
    has no state for (here: read before any step) still renders from the plan."""
    bench = Bench(tmp_path, plant="fail_result")
    bench.start()
    blocked = bench.start(MES.model_copy(update={"ticket_no": "MES-88132", "unit_sn": "SN-2"}))
    assert blocked["ticket_id"] == "T-factory-0003" and blocked["state"] == "Needs review"
    assert [c["status"] for c in blocked["cells"]] == ["failed"] + ["waiting"] * 9
    assert blocked["cells"][0]["sentence"].startswith("station-07 is busy:")
    assert blocked["unit_sn"] == "SN-2" and blocked["verdict"] is None
    ticket = bench.store.load("T-factory-0003")
    assert ticket.state is TicketState.NEEDS_REVIEW
    # The view of a ticket without executor state falls back to the plan.
    plain = job_view(ticket, None)
    assert [c.status for c in plain.cells] == ["waiting"] * 10
    assert plain.unit_sn == "SN-2" and plain.mes_ticket_no == "MES-88132"
    assert plain.sentence == "T-factory-0003 needs your review: 0 of 10 steps finished."


def test_factory_views_fall_back_when_the_executor_or_the_plan_is_missing() -> None:
    assert plan_rules_sentence(None).startswith("PASS needs 3 of 3 voters")
    assert waiting_cells(None) == []
    rows = station_views([{"description": "nameless"}, {"name": "station-09", "holder": "busy"}])
    assert [(r.name, r.free, r.sentence) for r in rows] == [
        ("station-09", False, "station-09 is busy.")
    ]
    now = FakeClock().now()
    job = Job(
        id="job-x",
        agent="factory",
        user="mes",
        title="Final test",
        target=TargetRef(kind="station", ref=STATION),
        created_at=now,
    )
    ticket = Ticket(
        id="T-factory-0001",
        agent="factory",
        user="mes",
        title="Final test",
        job=job,
        created_at=now,
        updated_at=now,
    )
    bare = job_view(ticket, None)
    assert bare.cells == [] and bare.unit_sn == "" and bare.mes_ticket_no == ""
    assert bare.sentence == "T-factory-0001 is open." and bare.station == STATION
    assert bare.rules_sentence == plan_rules_sentence(None)
    # A running job: the ticket's sentence and the executor's map together.
    for target in (TicketState.PLANNED, TicketState.APPROVED, TicketState.RUNNING):
        ticket.transition(target, now, "test")
    state = JobState(
        ticket_id=ticket.id,
        station=STATION,
        unit_sn="SN-9",
        cells=[StepCell(n=1, title="Lease", status="ok"), StepCell(n=2, title="Power on")],
    )
    running = job_view(ticket, state)
    assert running.sentence == "T-factory-0001 is running: 0 of 0 steps done. 1 of 2 steps done."
    assert running.unit_sn == "SN-9" and [c.status for c in running.cells] == ["ok", "waiting"]
