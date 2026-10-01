"""The factory executor behind HTTP (docs/api-contract-round-2.md §6): the nine-step loop
driven through `POST /v1/execute` by a kernel on the other side of the wire, watch and take
over, the line lead's decision, the stations join, Admin → Stations, templates, the MES
inbox, the runner and key tables as live views, the route table and `serve`."""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from fastapi.testclient import TestClient

from slas_factory_executor import cli
from slas_factory_executor.mes import FileDropMesAdapter
from slas_factory_executor.service.app import create_app, route_table
from slas_factory_executor.service.collaborators import (
    GatewayCrossChecker,
    MesPoller,
    NoWatcher,
    RunnerTable,
    StationKeys,
    VncWatcher,
    ensure_shipped_templates,
    templates_in,
)
from slas_factory_executor.service.settings import Settings
from slas_factory_executor.stations import (
    EnrolmentService,
    FakeCa,
    StationRecord,
    StationRegistry,
)
from slas_factory_executor.templates import FINAL_TEST_9_STEPS, render_template_yaml
from slas_http.client import ServiceClient
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext
from slas_observability.events import EventLog, ListSink
from slas_schemas.job import MesTicket
from slas_schemas.plan import Step
from slas_schemas.ticket import Observation, TicketState
from slas_station_runner.control import Controller
from slas_station_runner.protocol import EnrolmentRequest
from slas_station_runner.runner import VncSettings
from slas_station_runner.server import MtlsRunnerClient
from tests.unit.test_factory_executor import MES, STATION, Line
from tests.unit.test_validation_executor_service import capture, contract_routes

THREE_PARTS: Final = ("what_happened", "likely_cause", "what_to_do", "trace_id")
ADMIN: Final = Identity(
    "admin@slas.local",
    "Admin",
    frozenset({"factory:stations_manage", "factory:control", "factory:verdict"}),
)
ENGINEER: Final = Identity("lee@slas.local", "Lee", frozenset({"factory:control"}))
LEAD: Final = Identity("lead@slas.local", "Lead", frozenset({"factory:verdict"}))
VIEWER: Final = Identity("pat@slas.local", "Pat", frozenset({"git:pull"}))
needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to mint certificates"
)


class OverHttp:
    """The kernel's `Executor` over the service: what the orchestrator's remote executor does."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        response = self.client.post(
            "/v1/execute",
            json={"step": step.model_dump(mode="json"), "context": context.model_dump(mode="json")},
        )
        if response.status_code != 200:
            raise RuntimeError(response.json()["what_happened"])
        return Observation.model_validate(response.json())


class Wire:
    """A `Line` whose kernel reaches the executor only through the service."""

    def __init__(self, tmp_path: Path, **line_kwargs: Any) -> None:
        self.line = Line(tmp_path, **line_kwargs)
        self.registry = StationRegistry(tmp_path / "Factory" / "stations.json")
        self.enrolment = EnrolmentService(
            self.registry, ca=FakeCa(), clock=self.line.clock, data_root=tmp_path
        )
        self.mes = FileDropMesAdapter(tmp_path / "Factory" / "mes")
        self.templates_dir = tmp_path / "Factory" / "Templates"
        self.sink = ListSink()
        self.app = create_app(
            executor=self.line.executor,
            registry=self.registry,
            enrolment=self.enrolment,
            mes=self.mes,
            templates_dir=self.templates_dir,
            log=EventLog("factory-executor", self.sink),
            clock=self.line.clock,
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.line.kernel.executor = OverHttp(self.client)

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.sink.records() if r["event"] == name]


# --- the nine-step loop, over the wire ------------------------------------------------------------


def test_the_nine_step_loop_over_http_passes_with_three_votes(tmp_path: Path) -> None:
    wire = Wire(tmp_path)
    ticket = wire.line.run()
    assert ticket.id == "T-factory-0001" and ticket.state is TicketState.DONE
    assert [r.status for r in ticket.steps] == ["done"] * 10
    assert len(wire.events("execute.done")) == 10 and wire.events("execute.refused") == []
    assert wire.line.station.power == "on" and wire.line.station.test_started
    assert wire.line.station.login_typed == ["operator", "Op3rator-Passw0rd!"]

    rows = wire.client.get("/v1/jobs").json()
    assert [r["ticket_id"] for r in rows] == [ticket.id]
    assert rows[0]["verdict"] == "PASS" and rows[0]["held"] is False
    assert rows[0]["sentence"] == "10 of 10 steps done. Verdict: PASS (3 of 3 voters)."
    view = wire.client.get(f"/v1/jobs/{ticket.id}").json()
    assert view == rows[0]
    assert [c["status"] for c in view["cells"]] == ["ok"] * 10
    assert len(view["cells"][2]["screenshots"]) >= 16
    assert view["unit_sn"] == "SN-GX8-0100" and view["station"] == STATION
    (reported,) = wire.line.mes.reported
    assert reported.verdict == "PASS" and reported.decided_by == "3 of 3 voters"

    missing = wire.client.get("/v1/jobs/T-factory-9999")
    assert missing.status_code == 404 and set(missing.json()) == set(THREE_PARTS)
    assert missing.json()["what_happened"] == "There is no factory job for T-factory-9999."
    unknown = wire.client.post(
        "/v1/execute",
        json={
            "step": Step(
                id="x", n=1, primitive="shell", title="x", args={"station": STATION}
            ).model_dump(mode="json"),
            "context": {
                "ticket_id": "T-factory-0002",
                "job_id": "j",
                "agent": "factory",
                "user": "mes",
            },
        },
    )
    assert unknown.status_code == 400
    assert unknown.json()["what_happened"] == (
        "Step x uses the primitive 'shell', which this executor does not know."
    )
    assert wire.client.get("/health").json()["checks"] == {"enrolment": "ok", "mes": "ok"}


def test_a_step_already_running_for_the_ticket_is_a_409(tmp_path: Path) -> None:
    wire = Wire(tmp_path)
    lock = wire.app.state.services.locks.lock_for("T-factory-0001")
    assert lock.acquire(blocking=False)
    try:
        busy = wire.client.post(
            "/v1/execute",
            json={
                "step": Step(
                    id="lease",
                    n=1,
                    primitive="lease_station",
                    title="Lease",
                    args={"station": STATION},
                ).model_dump(mode="json"),
                "context": {
                    "ticket_id": "T-factory-0001",
                    "job_id": "j",
                    "agent": "factory",
                    "user": "mes",
                },
            },
        )
    finally:
        lock.release()
    assert busy.status_code == 409
    assert busy.json()["what_happened"] == (
        "A step of T-factory-0001 is already running on the factory-executor."
    )
    assert wire.line.executor.leases.holder(STATION, wire.line.clock.now()) is None


# --- watch and take over --------------------------------------------------------------------------


def test_control_needs_factory_control_and_says_where_to_watch(tmp_path: Path) -> None:
    wire = Wire(tmp_path)
    wire.line.station.runner.controller = Controller(STATION, wire.line.clock, poll_s=0.01)
    ticket = wire.line.run()
    url = f"/v1/jobs/{ticket.id}/control"

    assert wire.client.post(url, json={"verb": "pause"}).status_code == 401
    refused = wire.client.post(url, json={"verb": "pause"}, headers=VIEWER.headers())
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == f"Pat may not take over the station of {ticket.id}."

    paused = wire.client.post(url, json={"verb": "pause"}, headers=ENGINEER.headers())
    assert paused.status_code == 200
    body = paused.json()
    assert body["kind"] == "control" and body["ok"] is True and body["control"]["paused"] is True
    assert body["sentence"] == (
        "Lee has taken over station-07; the runner sends no input until it is resumed."
    )
    assert body["watch_url"] is None
    assert body["watch_problem"] == (
        "station-07 is not registered under Admin → Stations, so there is no screen to watch."
    )

    wire.registry.put(StationRecord(name=STATION))
    status = wire.client.post(url, json={"verb": "status", "by": "lee"}, headers=ENGINEER.headers())
    assert status.json()["watch_problem"] == (
        "VNC is not enabled on station-07. Enable it under Admin → Stations and re-enrol."
    )
    tuned = wire.client.put(
        f"/v1/station-records/{STATION}/tuning",
        json={"vnc_enabled": True},
        headers=ADMIN.headers(),
    )
    assert tuned.status_code == 200 and tuned.json()["vnc"]["enabled"] is True
    resumed = wire.client.post(url, json={"verb": "resume"}, headers=ENGINEER.headers())
    assert resumed.json()["sentence"] == "The runner drives station-07."
    assert resumed.json()["watch_problem"] == (
        "station-07 is not enrolled yet, so there is no runner to relay its screen."
    )

    bad = wire.client.post(url, json={"verb": "reboot"}, headers=ENGINEER.headers())
    assert bad.status_code == 400 and "verb" in bad.json()["what_happened"]
    gone = wire.client.post(
        "/v1/jobs/T-factory-9999/control", json={"verb": "pause"}, headers=ENGINEER.headers()
    )
    assert gone.status_code == 404
    journal = (tmp_path / "Factory" / "Jobs" / ticket.id / "journal.jsonl").read_text()
    controls = [json.loads(line) for line in journal.splitlines() if '"control"' in line]
    assert [(c["control"], c["by"]) for c in controls] == [
        ("pause", "Lee"),
        ("status", "lee"),
        ("resume", "Lee"),
    ]
    assert [e["verb"] for e in wire.events("job.control")] == ["pause", "status", "resume"]

    # A station without control support is a 502 with the runner's sentence.
    wire.line.station.runner.controller = None
    refused = wire.client.post(url, json={"verb": "pause"}, headers=ENGINEER.headers())
    assert refused.status_code == 502
    assert refused.json()["what_happened"] == "station-07 has no operator control enabled."


# --- the line lead --------------------------------------------------------------------------------


def test_decide_needs_factory_verdict_and_releases_the_held_station(tmp_path: Path) -> None:
    wire = Wire(tmp_path, plant="fail_result")
    wire.registry.put(StationRecord(name=STATION, description="Final test, line 2"))
    ticket = wire.line.run()
    assert ticket.state is TicketState.NEEDS_REVIEW
    view = wire.client.get(f"/v1/jobs/{ticket.id}").json()
    assert view["verdict"] == "FAIL" and view["held"] is True
    assert (
        view["sentence"]
        == "9 of 10 steps done. Verdict: FAIL; the station is held for the line lead."
    )

    (row,) = wire.client.get("/v1/stations").json()
    assert row == {
        "name": STATION,
        "description": "Final test, line 2",
        "free": False,
        "holder": f"{ticket.id} (mes)",
        "enrolled": False,
        "sentence": (
            f"station-07: not enrolled yet. station-07 is leased to {ticket.id} (mes) until "
            "2026-09-14 16:00."
        ),
    }

    url = f"/v1/jobs/{ticket.id}/decide"
    assert wire.client.post(url, json={"verdict": "FAIL"}).status_code == 401
    refused = wire.client.post(url, json={"verdict": "FAIL"}, headers=ENGINEER.headers())
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == f"Lee may not decide the verdict of {ticket.id}."
    assert wire.client.get(f"/v1/jobs/{ticket.id}").json()["held"] is True

    decided = wire.client.post(
        url, json={"verdict": "FAIL", "note": "Scrap the unit; GPU memory."}, headers=LEAD.headers()
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["held"] is False and body["decided_by"] == "Lead (line lead)"
    assert body["verdict_sentence"] == "FAIL: decided by Lead. Scrap the unit; GPU memory."
    assert (
        body["sentence"]
        == "9 of 10 steps done. Verdict: FAIL; the station is held for the line lead."
    )
    (row,) = wire.client.get("/v1/stations").json()
    assert row["free"] is True and row["holder"] is None and row["sentence"].endswith("Free.")
    bad = wire.client.post(url, json={"verdict": "MAYBE"}, headers=LEAD.headers())
    assert bad.status_code == 400
    gone = wire.client.post(
        "/v1/jobs/T-factory-9999/decide", json={"verdict": "PASS"}, headers=LEAD.headers()
    )
    assert gone.status_code == 404
    assert wire.events("job.decided")[0]["by"] == "Lead"

    nobody = Wire(tmp_path / "nobody", no_voters=True)
    held = nobody.line.run()
    passed = nobody.client.post(
        f"/v1/jobs/{held.id}/decide",
        json={"verdict": "PASS", "by": "lee", "note": "Checked by hand."},
        headers=LEAD.headers(),
    )
    assert passed.json()["verdict"] == "PASS" and passed.json()["decided_by"] == "lee (line lead)"


# --- Admin → Stations -----------------------------------------------------------------------------


def test_station_records_crud_code_and_revoke_need_factory_stations_manage(tmp_path: Path) -> None:
    wire = Wire(tmp_path)
    client = wire.client
    assert client.get("/v1/station-records").status_code == 401
    refused = client.get("/v1/station-records", headers=VIEWER.headers())
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == "Pat may not see the station records."
    assert (
        client.post(
            "/v1/station-records", json={"name": STATION}, headers=ENGINEER.headers()
        ).status_code
        == 403
    )

    headers = ADMIN.headers()
    assert client.get("/v1/station-records", headers=headers).json() == []
    added = client.post(
        "/v1/station-records",
        json={"name": STATION, "description": "Final test, line 2"},
        headers=headers,
    )
    assert added.status_code == 200
    row = added.json()
    assert row["name"] == STATION and row["enrolled"] is False
    assert row["sentence"] == "station-07: not enrolled yet."
    assert row["vnc"]["enabled"] is False and row["screen"]["window_match"] == "contains"
    again = client.post("/v1/station-records", json={"name": STATION}, headers=headers)
    assert again.status_code == 409
    assert again.json()["what_happened"] == "There is already a station called station-07."
    bad = client.post("/v1/station-records", json={"name": "Station 7"}, headers=headers)
    assert bad.status_code == 400

    tuned = client.put(
        f"/v1/station-records/{STATION}/tuning",
        json={
            "screen": {"window_match": "prefix", "action_settle_s": 0.3},
            "retention": {"keep_days": 7, "keep_failed_days": 30, "max_per_job": 50},
            "vnc_enabled": True,
        },
        headers=headers,
    )
    assert tuned.status_code == 200
    body = tuned.json()
    assert body["screen"]["window_match"] == "prefix" and body["screen"]["action_settle_s"] == 0.3
    assert body["retention"]["keep_days"] == 7 and body["vnc"]["enabled"] is True
    stored = wire.registry.get(STATION)
    assert stored.vnc == VncSettings(enabled=True) and stored.retention.max_per_job == 50
    assert (
        client.put("/v1/station-records/nope/tuning", json={}, headers=headers).status_code == 404
    )

    issued = client.post(f"/v1/station-records/{STATION}/code", headers=headers)
    assert issued.status_code == 200
    code = issued.json()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code["code"])
    assert code["sentence"].startswith(
        f"Enter this code on station-07 within 15 minutes: {code['code']}."
    )
    assert wire.registry.code_for(STATION) is not None
    assert wire.registry.code_for(STATION).issued_by == "admin@slas.local"  # type: ignore[union-attr]
    assert client.post("/v1/station-records/nope/code", headers=headers).status_code == 404
    assert code["code"] not in json.dumps(wire.sink.records()), "the code is never logged"

    # The station redeems the code (the enrolment endpoint's job); the records show it.
    grant = wire.enrolment.redeem(
        EnrolmentRequest(station=STATION, code=code["code"], runner_url="https://10.20.0.7:8443")
    )
    assert grant.config["screen"]["window_match"] == "prefix"
    (record,) = client.get("/v1/station-records", headers=headers).json()
    assert record["enrolled"] is True and record["runner_url"] == "https://10.20.0.7:8443"
    (station,) = client.get("/v1/stations").json()
    assert station["enrolled"] is True and station["free"] is True
    key_path = tmp_path / "Factory" / "keys" / f"{STATION}.key"
    assert key_path.is_file()

    revoked = client.post(f"/v1/station-records/{STATION}/revoke", headers=headers)
    assert revoked.status_code == 200 and revoked.json()["enrolled"] is False
    assert not key_path.exists() and wire.registry.code_for(STATION) is None
    assert client.post("/v1/station-records/nope/revoke", headers=headers).status_code == 404

    removed = client.delete(f"/v1/station-records/{STATION}", headers=headers)
    assert removed.status_code == 200
    assert removed.json()["sentence"] == (
        "station-07 was removed; its enrolment and batch key are revoked."
    )
    assert client.get("/v1/station-records", headers=headers).json() == []
    assert client.delete(f"/v1/station-records/{STATION}", headers=headers).status_code == 404
    assert [e["event"] for e in wire.sink.records() if e["event"].startswith("station.")] == [
        "station.added",
        "station.tuned",
        "station.code_issued",
        "station.revoked",
        "station.removed",
    ]


# --- templates and the MES ------------------------------------------------------------------------


def test_templates_are_copied_to_the_data_root_and_read_back_with_a_lines_own(
    tmp_path: Path,
) -> None:
    wire = Wire(tmp_path)
    assert not wire.templates_dir.exists()
    rows = wire.client.get("/v1/templates").json()
    assert [r["id"] for r in rows] == ["final-test-9-steps"]
    assert (
        rows[0]["sentence"] == "Final test, 9 steps: 9 steps, using the station-login-burnin skill."
    )
    assert [s["primitive"] for s in rows[0]["steps"]][:2] == ["lease_station", "station_command"]
    shipped = wire.templates_dir / "final-test-9-steps.yaml"
    assert shipped.read_text(encoding="utf-8") == render_template_yaml(FINAL_TEST_9_STEPS)
    assert ensure_shipped_templates(wire.templates_dir) == [], "copied once, never overwritten"

    custom = {
        **FINAL_TEST_9_STEPS,
        "id": "quick-check",
        "name": "Quick check",
        "skills": [],
        "steps": FINAL_TEST_9_STEPS["steps"][:2],
    }
    (wire.templates_dir / "quick-check.yaml").write_text(
        render_template_yaml(custom), encoding="utf-8"
    )
    (wire.templates_dir / "broken.yaml").write_text("- not: [a template\n", encoding="utf-8")
    (wire.templates_dir / "wrong.yaml").write_text("id: wrong\n", encoding="utf-8")
    (wire.templates_dir / "json-one.template.json").write_text(
        json.dumps({**custom, "id": "json-one", "name": "From JSON"}), encoding="utf-8"
    )
    rows = wire.client.get("/v1/templates").json()
    assert [r["id"] for r in rows] == ["final-test-9-steps", "json-one", "quick-check"]
    assert rows[2]["sentence"] == "Quick check: 2 steps."
    skipped = sorted(Path(e["path"]).name for e in wire.events("templates.skipped"))
    assert skipped == ["broken.yaml", "wrong.yaml"]
    assert [t.id for t in templates_in(tmp_path / "nowhere")] == ["final-test-9-steps"]


def test_mes_pending_lists_the_tickets_the_poller_picked_up(tmp_path: Path) -> None:
    wire = Wire(tmp_path)
    inbox = wire.mes.root / "inbox"
    (inbox / "b.json").write_text(MES.model_dump_json(), encoding="utf-8")
    (inbox / "a.json").write_text('{"ticket_no": "MES-1"}', encoding="utf-8")
    assert wire.client.get("/v1/mes/pending").json() == [], "nothing picked up yet"

    poller = MesPoller(wire.mes, interval_s=3600, log=EventLog("factory-executor", wire.sink))
    (ticket,) = poller.poll_once()
    assert ticket.ticket_no == "MES-88131" and poller.state() == "ok"
    assert wire.client.get("/v1/mes/pending").json() == [MES.model_dump(mode="json")]
    assert wire.mes.rejected() == ["a.json"]
    assert wire.events("mes.ticket_received")[0]["ticket_no"] == "MES-88131"
    poller.start()
    poller.start()  # idempotent
    poller.stop()
    assert poller.polls >= 2

    (wire.mes.root / "processing" / "MES-88131.json").write_text("{broken", encoding="utf-8")
    assert wire.client.get("/v1/mes/pending").json() == [], "a half-written file is skipped"

    def broken_poll() -> list[MesTicket]:
        raise OSError("inbox unreadable")

    poller.adapter.poll = broken_poll  # type: ignore[method-assign]
    assert poller.poll_once() == [] and poller.state() == "down"
    assert wire.events("mes.poll_failed")[0]["error"] == "inbox unreadable"


# --- the collaborators ----------------------------------------------------------------------------


def test_runner_and_key_tables_are_live_views_of_the_registry(tmp_path: Path) -> None:
    registry = StationRegistry(tmp_path / "Factory" / "stations.json")
    enrolment = EnrolmentService(registry, ca=FakeCa(), clock=FakeClock(), data_root=tmp_path)
    table = RunnerTable(
        registry,
        certfile=tmp_path / "e.pem",
        keyfile=tmp_path / "e.key",
        cafile=tmp_path / "ca.pem",
    )
    keys = StationKeys(registry, enrolment)
    assert len(table) == 0 and list(table) == [] and len(keys) == 0
    with pytest.raises(KeyError):
        table[STATION]
    with pytest.raises(KeyError):
        keys[STATION]
    registry.put(StationRecord(name=STATION))
    with pytest.raises(KeyError):
        table[STATION]  # added but not enrolled
    assert STATION not in keys and keys.get(STATION, ("line", "id")) == ("line", "id")

    issued = enrolment.issue(STATION, by="admin")
    grant = enrolment.redeem(
        EnrolmentRequest(station=STATION, code=issued.code, runner_url="https://10.20.0.7:8443")
    )
    assert list(table) == [STATION] and len(table) == 1
    assert keys[STATION] == ("file:Factory/keys/station-07.key", grant.batch_key_id)
    assert list(keys) == [STATION] and len(keys) == 1
    keys["station-08"] = ("env:OTHER", "other-key")
    assert keys["station-08"] == ("env:OTHER", "other-key") and sorted(keys) == [
        STATION,
        "station-08",
    ]
    del keys["station-08"]
    assert "station-08" not in keys

    watcher = NoWatcher()
    assert watcher.watch(registry.get(STATION), STATION) == (
        None,
        "VNC is not enabled on station-07. Enable it under Admin → Stations and re-enrol.",
    )
    registry.put(registry.get(STATION).model_copy(update={"vnc": VncSettings(enabled=True)}))
    _, problem = watcher.watch(registry.get(STATION), STATION)
    assert problem is not None and problem.startswith("The executor has no certificate")


@needs_openssl
def test_the_runner_table_builds_mtls_clients_and_the_watcher_opens_one_tunnel_per_station(
    tmp_path: Path,
) -> None:
    from tests.unit.test_station_runner import make_certs

    certs = make_certs(tmp_path / "certs")
    registry = StationRegistry(tmp_path / "Factory" / "stations.json")
    enrolment = EnrolmentService(registry, ca=FakeCa(), clock=FakeClock(), data_root=tmp_path)
    registry.put(StationRecord(name=STATION, vnc=VncSettings(enabled=True)))
    issued = enrolment.issue(STATION, by="admin")
    enrolment.redeem(
        EnrolmentRequest(station=STATION, code=issued.code, runner_url="https://127.0.0.1:1/")
    )
    table = RunnerTable(
        registry,
        certfile=Path(certs["executor"] + ".pem"),
        keyfile=Path(certs["executor"] + ".key"),
        cafile=Path(certs["ca"] + ".pem"),
    )
    client = table[STATION]
    assert isinstance(client, MtlsRunnerClient) and client.url == "https://127.0.0.1:1"
    assert table[STATION] is client, "one client, cached"
    registry.put(registry.get(STATION).model_copy(update={"runner_url": "https://127.0.0.1:2"}))
    moved_client = table[STATION]
    assert isinstance(moved_client, MtlsRunnerClient) and moved_client is not client
    assert moved_client.url == "https://127.0.0.1:2"

    watcher = VncWatcher(
        certfile=Path(certs["executor"] + ".pem"),
        keyfile=Path(certs["executor"] + ".key"),
        cafile=Path(certs["ca"] + ".pem"),
    )
    try:
        url, problem = watcher.watch(registry.get(STATION), STATION)
        assert problem is None and url is not None and url.startswith("vnc://127.0.0.1:")
        assert watcher.watch(registry.get(STATION), STATION) == (url, None), "the same tunnel"
        sentence = watcher.sentence_for(STATION)
        assert sentence is not None and sentence.startswith("Watch the station at vnc://127.0.0.1:")
        assert sentence.endswith("(relayed over mTLS to https://127.0.0.1:2).")
        registry.put(registry.get(STATION).model_copy(update={"runner_url": "https://127.0.0.1:3"}))
        moved, _ = watcher.watch(registry.get(STATION), STATION)
        assert moved != url, "a re-enrolled station gets a fresh tunnel"
        assert watcher.sentence_for("station-99") is None
        assert watcher.watch(None, "station-99")[1] is not None
    finally:
        watcher.stop()
    assert watcher.tunnels == {}


def test_the_gateway_cross_checker_returns_the_verdict_or_degrades_to_the_line_lead() -> None:
    seen: list[dict[str, Any]] = []
    verdict = {
        "decision": "factory_pass",
        "rule": "unanimous",
        "votes": [
            {"voter": f"voter-{i}", "verdict": "approve", "reason": "ok", "confidence": 0.9}
            for i in (1, 2, 3)
        ],
        "agreed": True,
        "sentence": "3 of 3 say PASS.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if payload["decision"] == "down":
            return httpx.Response(
                503,
                json={
                    "what_happened": "No voter answered.",
                    "likely_cause": "x",
                    "what_to_do": "y",
                    "trace_id": "t",
                },
            )
        if payload["decision"] == "garbage":
            return httpx.Response(200, json={"nonsense": True})
        return httpx.Response(200, json=verdict)

    sink = ListSink()
    checker = GatewayCrossChecker(
        ServiceClient(
            "llm-gateway", "http://llm-gateway:8000", transport=httpx.MockTransport(handler)
        ),
        log=EventLog("factory-executor", sink),
    )
    result = checker.cross_check("factory_pass", ["Unit SN-1 on station-07", "Event log: empty"])
    assert result.agreed and len(result.votes) == 3 and result.degraded is False
    assert seen[0] == {
        "decision": "factory_pass",
        "evidence": [
            {"role": "user", "content": "Evidence:\n- Unit SN-1 on station-07\n- Event log: empty"}
        ],
    }
    down = checker.cross_check("down", ["x"])
    assert down.degraded and down.votes == [] and down.agreed is False
    assert down.sentence == "Not cross-checked: No voter answered. The line lead decides."
    garbage = checker.cross_check("garbage", ["x"])
    assert garbage.degraded and garbage.sentence.startswith("Not cross-checked:")
    assert [e["decision"] for e in sink.records() if e["event"] == "cross_check.unavailable"] == [
        "down",
        "garbage",
    ]


# --- the contract ---------------------------------------------------------------------------------


def test_route_table_matches_the_contract() -> None:
    documented = contract_routes("### factory-executor", "## 7. git-broker")
    assert ("POST", "/v1/execute") in documented
    assert ("DELETE", "/v1/station-records/{name}") in documented
    table = set(route_table())
    assert documented <= table
    assert table - documented == {("GET", "/health"), ("GET", "/metrics")}
    assert route_table() == sorted(route_table())


# --- the serve command ----------------------------------------------------------------------------


def test_settings_read_the_environment_with_the_compose_defaults() -> None:
    defaults = Settings.from_environ({})
    assert defaults.data_root == Path("/data") and defaults.bind == "0.0.0.0:8000"
    assert defaults.enrolment_listen == "0.0.0.0:8444"
    assert defaults.enrolment_hosts == ("factory-executor",)
    assert defaults.runner_mtls_ca == Path("/data/Factory/ca/ca.pem")
    assert defaults.mes_adapter == "file_drop" and defaults.mes_poll_interval_s == 30
    assert defaults.factory_settings == Path("/etc/slas/factory.yaml")
    assert (
        defaults.batch_key_ref == "env:FACTORY_BATCH_KEY"
        and defaults.batch_key_id == "factory-line"
    )
    assert defaults.gateway_url == "" and defaults.vnc_tunnel_listen == "127.0.0.1:0"
    assert defaults.credential_source == "env"
    assert defaults.mes_dir == Path("/data/Factory/mes")
    assert defaults.templates_dir == Path("/data/Factory/Templates")
    assert defaults.sentences()[3] == (
        "No gateway configured: every passing unit goes to the line lead."
    )
    custom = Settings.from_environ(
        {
            "ENROLMENT_HOSTS": "10.30.0.5, factory.internal,",
            "SLAS_GATEWAY_URL": "http://llm-gateway:8000",
        }
    )
    assert custom.enrolment_hosts == ("10.30.0.5", "factory.internal")
    assert custom.sentences()[3] == "Verdict voters through the gateway at http://llm-gateway:8000."
    assert Settings.from_environ({"ENROLMENT_HOSTS": " , "}).enrolment_hosts == (
        "factory-executor",
    )
    assert cli.parser().parse_args(["serve"]).command == "serve"
    assert cli.main([], stdout=io.StringIO()) == cli.EXIT_USAGE
    assert cli.main(["--help"], stdout=io.StringIO()) == cli.EXIT_OK


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "SLAS_DATA_ROOT": str(tmp_path / "data"),
        "SLAS_BIND": "127.0.0.1:18001",
        "ENROLMENT_LISTEN": "127.0.0.1:0",
        "MES_POLL_INTERVAL_S": "3600",
        "SLAS_FACTORY_SETTINGS": str(tmp_path / "missing-factory.yaml"),
        "SLAS_GATEWAY_URL": "http://llm-gateway:8000",
    }


def test_build_app_with_a_fake_ca_serves_and_reports_the_enrolment_endpoint_down(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    settings = Settings.from_environ(env)
    sink = ListSink()
    app = cli.build_app(settings, environ=env, log=EventLog("factory-executor", sink), ca=FakeCa())
    try:
        client = TestClient(app, raise_server_exceptions=False)
        health = client.get("/health")
        assert health.status_code == 503, "a fake certificate cannot serve TLS"
        assert health.json()["what_happened"] == (
            "The factory-executor is not healthy: enrolment did not answer."
        )
        assert [e["event"] for e in sink.records() if e["event"] == "enrolment.not_listening"]
        assert (
            tmp_path / "data" / "Factory" / "ca" / "executor.pem"
        ).stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "data" / "Factory" / "Templates" / "final-test-9-steps.yaml").is_file()
        assert (tmp_path / "data" / "Factory" / "mes" / "inbox").is_dir()
        assert client.get("/v1/stations").json() == [] and client.get("/v1/jobs").json() == []
        assert [t["id"] for t in client.get("/v1/templates").json()] == ["final-test-9-steps"]
        assert client.get("/v1/mes/pending").json() == []
        services = app.state.services
        assert services.registry.path == tmp_path / "data" / "Factory" / "stations.json"
        assert services.leases.path == tmp_path / "data" / "Factory" / "leases.json"
        assert isinstance(services.executor.runners, RunnerTable)
        assert isinstance(services.executor.station_keys, StationKeys)
        assert isinstance(services.executor.cross_checker, GatewayCrossChecker)
        assert services.executor.lease_hours == 8 and app.state.enrolment_server is None
        assert app.state.mes_poller.polls >= 1
    finally:
        app.state.mes_poller.stop()


def test_serve_says_what_is_wrong_and_stops(tmp_path: Path) -> None:
    served, runner = capture()
    env = environment(tmp_path)
    out = io.StringIO()
    code = cli.main(["serve"], environ={**env, "MES_ADAPTER": "rest"}, stdout=out, runner=runner)
    assert code == cli.EXIT_PROBLEM and served == []
    assert out.getvalue().startswith("MES_ADAPTER is 'rest', which this build does not have.\n")

    broken = tmp_path / "factory.yaml"
    broken.write_text("vnc_port: 0\n", encoding="utf-8")
    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**env, "SLAS_FACTORY_SETTINGS": str(broken)}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM
    assert out.getvalue().startswith(f"The factory settings in {broken} could not be used.\n")

    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**env, "CREDENTIAL_SOURCE": "vault"}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM and "no Vault client" in out.getvalue()

    # Without openssl the CA cannot be created: a sentence, not a traceback.
    out = io.StringIO()
    code = cli.main(["serve"], environ={**env, "PATH": "/nonexistent"}, stdout=out, runner=runner)
    if shutil.which("openssl") is None:
        assert code == cli.EXIT_PROBLEM
        assert out.getvalue().startswith("The platform CA could not issue a certificate.\n")


@needs_openssl
def test_serve_creates_the_ca_and_listens_for_enrolment(tmp_path: Path) -> None:
    served, runner = capture()
    env = environment(tmp_path)
    (tmp_path / "factory.yaml").write_text(
        "version: 1\nenrolment_code_ttl_minutes: 5\nstation_lease_hours: 4\n", encoding="utf-8"
    )
    env["SLAS_FACTORY_SETTINGS"] = str(tmp_path / "factory.yaml")
    out = io.StringIO()
    assert cli.main(["serve"], environ=env, stdout=out, runner=runner) == cli.EXIT_OK
    ((app, bind),) = served
    assert bind == "127.0.0.1:18001"
    assert out.getvalue().splitlines()[1].startswith("Stations enrol at 127.0.0.1:0")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["checks"] == {"enrolment": "ok", "mes": "ok"}
        ca_dir = tmp_path / "data" / "Factory" / "ca"
        assert (ca_dir / "ca.pem").is_file() and (ca_dir / "ca.key").stat().st_mode & 0o777 == 0o600
        assert (ca_dir / "executor.pem").is_file() and (ca_dir / "executor.key").is_file()
        assert not list((ca_dir / "work").glob("*")) if (ca_dir / "work").exists() else True
        assert app.state.enrolment_server.port > 0
        services = app.state.services
        assert services.executor.lease_hours == 4
        assert services.enrolment.code_ttl.total_seconds() == 300
        assert isinstance(services.watcher, VncWatcher)
        assert client.get("/v1/stations").json() == []

        # A second start reuses the CA and the executor certificate.
        first_ca = (ca_dir / "ca.pem").read_bytes()
        first_cert = (ca_dir / "executor.pem").read_bytes()
        again = cli.build_app(Settings.from_environ(env), environ=env)
        try:
            assert (ca_dir / "ca.pem").read_bytes() == first_ca
            assert (ca_dir / "executor.pem").read_bytes() == first_cert
        finally:
            again.state.enrolment_server.stop()
            again.state.mes_poller.stop()
    finally:
        app.state.enrolment_server.stop()
        app.state.mes_poller.stop()
