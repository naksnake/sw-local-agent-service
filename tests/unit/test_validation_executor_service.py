"""The validation executor behind HTTP (docs/api-contract-round-2.md §6): the P7 run driven
step by step through `POST /v1/execute` by a kernel on the other side of the wire, the run
views with the console tail, the targets join, the arming gate with its capability, one
step at a time per ticket, the route table against the contract, and the `serve` command."""

from __future__ import annotations

import io
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.testclient import TestClient

from slas_hal.fakes.bmc import FakeHal, FakeTarget, Plant
from slas_hal.targets import BmcAccess, SshAccess, TargetRecord, TargetRegistry
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext
from slas_observability import tracing
from slas_observability.events import EventLog, ListSink
from slas_schemas.plan import Risk, Step
from slas_schemas.ticket import Observation, TicketState
from slas_validation_executor import cli
from slas_validation_executor.guardrails import DEFAULT_GUARDRAILS, render_guardrails_yaml
from slas_validation_executor.service.app import create_app, route_table
from slas_validation_executor.service.routes import CONSOLE_TAIL_LINES, target_row
from slas_validation_executor.service.settings import Settings
from tests.unit.test_validation_executor import GPU3, TARGET, UPLOAD_25, Rig

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
THREE_PARTS: Final = ("what_happened", "likely_cause", "what_to_do", "trace_id")
APPROVER: Final = Identity("lee@slas.local", "Lee", frozenset({"approve:destructive"}))
VIEWER: Final = Identity("pat@slas.local", "Pat", frozenset({"git:pull"}))
CONTEXT: Final = {
    "ticket_id": "T-validation-0007",
    "job_id": "j7",
    "agent": "validation",
    "user": "pat",
}


def record(alias: str = TARGET) -> TargetRecord:
    return TargetRecord(
        alias=alias,
        bmc=BmcAccess(host="10.20.30.40", user="slas-validation", password_ref="env:BMC_PW"),
        ssh=SshAccess(
            host="10.20.30.41",
            user="slas",
            private_key_ref="env:SSH_KEY",
            known_hosts_line="10.20.30.41 ssh-ed25519 AAAA",
        ),
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


def make(
    tmp_path: Path, hal: FakeHal, **kernel_kwargs: object
) -> tuple[TestClient, Rig, TargetRegistry, ListSink]:
    rig = Rig(tmp_path, hal, **kernel_kwargs)
    registry = TargetRegistry(tmp_path / "Validation" / "targets.json")
    registry.put(record())
    sink = ListSink()
    app = create_app(
        executor=rig.executor,
        registry=registry,
        log=EventLog("validation-executor", sink),
        clock=FakeClock(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    rig.kernel.executor = OverHttp(client)
    return client, rig, registry, sink


def step(n: int, primitive: str, risk: Risk = "safe", **args: object) -> dict[str, Any]:
    return Step(
        id=f"s{n}",
        n=n,
        primitive=primitive,
        title=f"{primitive} {n}",
        args={"target": TARGET, **args},
        risk=risk,
    ).model_dump(mode="json")


def events(sink: ListSink, name: str) -> list[dict[str, Any]]:
    return [r for r in sink.records() if r["event"] == name]


# --- the P7 run, over the wire --------------------------------------------------------------------


def test_the_25_cycle_run_over_http_matches_the_in_process_rig(tmp_path: Path) -> None:
    hal = FakeHal(
        [FakeTarget(TARGET, plants=[Plant(at_cycle=14, kind="pcie_width", bdf=GPU3, width=8)])],
        clock=FakeClock(),
    )
    client, rig, _, sink = make(tmp_path, hal)
    ticket = rig.run(UPLOAD_25)

    assert ticket.id == "T-validation-0001" and ticket.state is TicketState.NEEDS_REVIEW
    assert len(ticket.steps) == 30 and all(r.status == "done" for r in ticket.steps)
    assert len(events(sink, "execute.done")) == 30
    assert events(sink, "execute.refused") == []

    # Hardware: the same 25 off/on pairs and 25 fence markers as in-process.
    records = hal.target(TARGET).power_records
    assert len(records) == 50 and [r.action for r in records[:2]] == ["off", "on"]
    assert len(hal.fence_markers) == 25
    assert hal.fence_markers[13][1] == f"--- slas fence {ticket.id} cycle 14 dc ---"

    # The LED map, once through the executor and once through the route.
    state = rig.executor.state_for(ticket.id)
    assert state is not None
    assert [c.status for c in state.cycles] == ["ok"] * 13 + ["finding"] * 12
    assert state.sentence() == "25 of 25 cycles done: 12 with findings."
    view = client.get(f"/v1/runs/{ticket.id}").json()
    assert view["ticket_id"] == ticket.id and view["target"] == TARGET
    assert view["cycles"] == state.model_dump(mode="json")["cycles"]
    assert view["sentence"] == state.sentence() and view["aborted"] is False
    console = (rig.executor.run_dir(ticket.id) / "console.log").read_text(encoding="utf-8")
    assert view["console_tail"] == console.splitlines()[-CONSOLE_TAIL_LINES:]
    assert len(view["console_tail"]) == 40 and console.count("Linux version") == 25
    assert any("Linux version" in line for line in view["console_tail"])

    runs = client.get("/v1/runs").json()
    assert [r["ticket_id"] for r in runs] == [ticket.id]
    assert runs[0]["state"]["cycles"][13]["status"] == "finding"
    assert runs[0]["sentence"] == "25 of 25 cycles done: 12 with findings."
    (finding,) = ticket.findings
    assert finding.issue.startswith("PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0)")

    # The lease was taken and released over the wire too.
    (row,) = client.get("/v1/targets").json()
    assert row["ref"] == TARGET and row["free"] is True and row["holder"] is None
    assert row["armed"] is False and row["sentence"].endswith("power actions not armed. Free.")


def test_a_hand_built_sequence_shows_the_holder_while_leased_and_the_tail_after_collecting(
    tmp_path: Path,
) -> None:
    hal = FakeHal(
        [FakeTarget(TARGET, plants=[Plant(at_cycle=2, kind="pcie_width", bdf=GPU3, width=8)])],
        clock=FakeClock(),
    )
    client, _, _, _ = make(tmp_path, hal)
    ticket_id = CONTEXT["ticket_id"]

    def post(body: dict[str, Any]) -> Any:
        return client.post("/v1/execute", json={"step": body, "context": CONTEXT})

    leased = post(step(1, "lease_target"))
    assert leased.status_code == 200
    assert leased.json()["summary"].startswith("Leased lab-gx8-01 for this run.")
    (row,) = client.get("/v1/targets").json()
    assert row["free"] is False and row["holder"] == f"{ticket_id} (pat)"
    assert f"{TARGET} is leased to {ticket_id} (pat) until" in row["sentence"]

    # Out of order: a cycle before the baseline is a 409, not a stack trace.
    early = post(step(2, "power_cycle", "caution", kind="dc", cycle=1))
    assert early.status_code == 409
    body = early.json()
    assert set(body) == set(THREE_PARTS)
    assert body["what_happened"].startswith("Step s2 (power_cycle 2) cannot run yet:")
    assert "no baseline" in body["what_happened"]
    assert hal.target(TARGET).power_records == []

    assert post(step(3, "console_on")).status_code == 200
    baseline = post(step(4, "baseline_snapshot"))
    assert baseline.json()["summary"].startswith("Baseline recorded.")
    for n in (1, 2, 3):
        cycle = post(
            step(4 + n, "power_cycle", "caution", kind="dc", cycle=n, total_cycles=3, settle_s=10)
        )
        assert cycle.status_code == 200 and cycle.json()["exit_code"] == 0
    assert len(hal.target(TARGET).power_records) == 6
    assert [m for _, m in hal.fence_markers] == [
        f"--- slas fence {ticket_id} cycle {n} dc ---" for n in (1, 2, 3)
    ]

    view = client.get(f"/v1/runs/{ticket_id}").json()
    assert [c["status"] for c in view["cycles"]] == ["ok", "finding", "finding"]
    assert view["console_tail"] == [], "nothing collected yet"
    collected = post(step(8, "collect_logs"))
    assert collected.json()["summary"].startswith("Collected ")
    view = client.get(f"/v1/runs/{ticket_id}").json()
    assert 0 < len(view["console_tail"]) <= CONSOLE_TAIL_LINES
    assert view["console_tail"][-1] == f"--- slas fence {ticket_id} cycle 3 dc ---" or any(
        "Linux version" in line for line in view["console_tail"]
    )
    assert view["sentence"] == "3 of 3 cycles done: 2 with findings."

    released = post(step(9, "release_target"))
    assert released.json()["summary"] == f"Released {TARGET}."
    (row,) = client.get("/v1/targets").json()
    assert row["free"] is True and row["holder"] is None

    # The refusals, every one three parts.
    unknown = post(step(10, "shell"))
    assert unknown.status_code == 400
    assert unknown.json()["what_happened"] == (
        "Step s10 uses the primitive 'shell', which this executor does not know."
    )
    nowhere = post({**step(11, "sel_snapshot"), "args": {"target": "lab-nope"}})
    assert nowhere.status_code == 502
    assert nowhere.json()["what_happened"] == "There is no target called lab-nope."
    missing = client.get("/v1/runs/T-validation-9999")
    assert missing.status_code == 404
    assert missing.json()["what_happened"] == "There is no validation run for T-validation-9999."
    malformed = client.post("/v1/execute", json={"step": {"id": "x"}, "context": CONTEXT})
    assert malformed.status_code == 400
    assert malformed.json()["what_happened"].startswith("The request couldn't be read")
    assert client.get("/health").json()["checks"] == {"hal": "ok", "syslog": "ok"}
    traced = client.get("/v1/runs", headers={tracing.SLAS_HEADER: "cd" * 16})
    assert traced.headers[tracing.SLAS_HEADER] == "cd" * 16


def test_one_step_at_a_time_per_ticket(tmp_path: Path) -> None:
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())
    client, _, _, sink = make(tmp_path, hal)
    locks = client.app.state.services.locks  # type: ignore[attr-defined]
    held = locks.lock_for(CONTEXT["ticket_id"])
    assert held.acquire(blocking=False)
    try:
        busy = client.post(
            "/v1/execute", json={"step": step(1, "sel_snapshot"), "context": CONTEXT}
        )
        assert busy.status_code == 409
        assert busy.json()["what_happened"] == (
            "A step of T-validation-0007 is already running on the validation-executor."
        )
        assert busy.json()["what_to_do"].endswith("nothing was started twice.")
        other = client.post(
            "/v1/execute",
            json={"step": step(1, "sel_snapshot"), "context": {**CONTEXT, "ticket_id": "T-v-2"}},
        )
        assert other.status_code == 200, "another ticket is not blocked"
    finally:
        held.release()
    again = client.post("/v1/execute", json={"step": step(1, "sel_snapshot"), "context": CONTEXT})
    assert again.status_code == 200
    assert again.json()["summary"] == "SEL read: 3 entries, worst severity Warning."
    assert hal.target(TARGET).ssh_calls == []
    assert len(events(sink, "execute.done")) == 2


# --- the arming gate ------------------------------------------------------------------------------


def test_arm_and_disarm_need_the_approve_destructive_capability(tmp_path: Path) -> None:
    hal = FakeHal([FakeTarget(TARGET)], clock=FakeClock())
    client, _, registry, sink = make(tmp_path, hal)
    anonymous = client.post(f"/v1/targets/{TARGET}/arm", json={"note": "x"})
    assert anonymous.status_code == 401
    assert anonymous.json()["what_happened"] == "The request did not say who is acting."
    refused = client.post(f"/v1/targets/{TARGET}/arm", json={"note": "x"}, headers=VIEWER.headers())
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == f"Pat may not arm power actions on {TARGET}."
    assert registry.get(TARGET).power_actions_enabled is False

    armed = client.post(
        f"/v1/targets/{TARGET}/arm",
        json={"note": "Rack 4 is clear."},
        headers=APPROVER.headers(),
    )
    assert armed.status_code == 200
    row = armed.json()
    assert row["armed"] is True and row["free"] is True
    assert "armed by Lee at 2026-09-14 08:00" in row["sentence"]
    stored = registry.get(TARGET)
    assert stored.power_actions_enabled and stored.armed is not None
    assert stored.armed.by == "Lee" and stored.armed.note == "Rack 4 is clear."
    assert client.get("/v1/targets").json()[0]["armed"] is True
    assert events(sink, "target.armed")[0]["by"] == "lee@slas.local"

    disarmed = client.post(f"/v1/targets/{TARGET}/disarm", json={}, headers=APPROVER.headers())
    assert disarmed.status_code == 200 and disarmed.json()["armed"] is False
    assert registry.get(TARGET).armed is None
    unknown = client.post("/v1/targets/lab-nope/arm", json={}, headers=APPROVER.headers())
    assert unknown.status_code == 404
    assert unknown.json()["what_happened"] == "There is no target called lab-nope."
    assert target_row(record("lab-x"), None)["model"] == "server", "no vendor hint: the kind"


# --- the contract ---------------------------------------------------------------------------------


def contract_routes(section: str, until: str) -> set[tuple[str, str]]:
    text = (REPO_ROOT / "docs" / "api-contract-round-2.md").read_text(encoding="utf-8")
    start = text.index(section)
    end = text.index(until, start)
    return set(re.findall(r"`(GET|POST|PUT|DELETE) (/v1/[^\s`·]+)`", text[start:end]))


def test_route_table_matches_the_contract() -> None:
    documented = contract_routes("### validation-executor", "### factory-executor")
    assert ("POST", "/v1/execute") in documented and ("GET", "/v1/runs/{id}") in documented
    table = set(route_table())
    assert documented <= table
    assert table - documented == {
        ("GET", "/health"),
        ("GET", "/metrics"),
        ("POST", "/v1/targets/{alias}/disarm"),  # the contract writes "arm · /disarm"
    }
    assert route_table() == sorted(route_table())


# --- the serve command ----------------------------------------------------------------------------


def capture() -> tuple[list[tuple[FastAPI, str]], Callable[[FastAPI, str], None]]:
    served: list[tuple[FastAPI, str]] = []

    def runner(app: FastAPI, bind: str) -> None:
        served.append((app, bind))

    return served, runner


def test_settings_read_the_environment_with_the_compose_defaults() -> None:
    defaults = Settings.from_environ({})
    assert defaults.data_root == Path("/data") and defaults.bind == "0.0.0.0:8000"
    assert defaults.syslog_listen == "0.0.0.0:5514"
    assert defaults.guardrail_policy == Path("/etc/slas/guardrails.yaml")
    assert defaults.bmc_quirks == Path("/etc/slas/bmc-quirks.yaml")
    assert defaults.profile == "quickstart" and defaults.credential_source == "env"
    prod = Settings.from_environ({"SLAS_PROFILE": "prod"})
    assert prod.credential_source == "vault"
    assert Settings.from_environ({"SLAS_DATA_ROOT": "/AI/Agent"}).sentences()[0] == (
        "Serving on 0.0.0.0:8000; run files under /AI/Agent/Validation."
    )
    assert cli.parser().parse_args(["serve"]).command == "serve"
    assert cli.main([], stdout=io.StringIO()) == cli.EXIT_USAGE
    assert cli.main(["--help"], stdout=io.StringIO()) == cli.EXIT_OK


def test_serve_wires_the_real_hal_and_is_healthy_and_idle_without_targets(tmp_path: Path) -> None:
    guardrails = tmp_path / "guardrails.yaml"
    guardrails.write_text(render_guardrails_yaml(DEFAULT_GUARDRAILS), encoding="utf-8")
    env = {
        "SLAS_DATA_ROOT": str(tmp_path / "data"),
        "SLAS_BIND": "127.0.0.1:18000",
        "SYSLOG_LISTEN": "127.0.0.1:0",
        "GUARDRAIL_POLICY": str(guardrails),
        "BMC_QUIRKS": str(tmp_path / "missing-quirks.yaml"),
    }
    served, runner = capture()
    out = io.StringIO()
    assert cli.main(["serve"], environ=env, stdout=out, runner=runner) == cli.EXIT_OK
    ((app, bind),) = served
    assert bind == "127.0.0.1:18000"
    assert out.getvalue().splitlines()[1] == "Syslog from the targets on 127.0.0.1:0."
    try:
        client = TestClient(app, raise_server_exceptions=False)
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["checks"] == {"hal": "ok", "syslog": "ok"}
        assert client.get("/v1/targets").json() == [] and client.get("/v1/runs").json() == []
        assert app.state.syslog.port > 0
        # The registry the service reads is the one `slas target add` writes.
        services = app.state.services
        assert services.registry.path == tmp_path / "data" / "Validation" / "targets.json"
        assert services.leases.path == tmp_path / "data" / "Validation" / "leases.json"
        assert services.executor.guardrails.max_cycles_per_run == 100
        assert services.executor.hal.quirk_table is not None
        assert services.executor.hal.quirk_table.quirks[0].id == "dmtf-defaults"
        services.registry.put(record())
        (row,) = client.get("/v1/targets").json()
        assert row["ref"] == TARGET and row["free"] is True, "a target added later is seen"
    finally:
        app.state.syslog.stop()


def test_serve_says_what_is_wrong_and_stops(tmp_path: Path) -> None:
    served, runner = capture()
    base = {"SLAS_DATA_ROOT": str(tmp_path / "data"), "SYSLOG_LISTEN": "127.0.0.1:0"}

    broken = tmp_path / "broken.yaml"
    broken.write_text("max_cycles_per_run: -4\n", encoding="utf-8")
    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**base, "GUARDRAIL_POLICY": str(broken)}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM and served == []
    assert out.getvalue().startswith(f"The guardrails in {broken} could not be used.\n")

    not_yaml = tmp_path / "not.yaml"
    not_yaml.write_text("quirks: [unclosed\n", encoding="utf-8")
    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**base, "BMC_QUIRKS": str(not_yaml)}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM
    assert out.getvalue().startswith(f"The BMC quirks file {not_yaml} is not valid YAML.\n")

    bad_quirks = tmp_path / "quirks.yaml"
    bad_quirks.write_text("quirks:\n  - id: Bad\n    note: x\n", encoding="utf-8")
    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**base, "BMC_QUIRKS": str(bad_quirks)}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM and "could not be used" in out.getvalue()

    unreadable = tmp_path / "dir.yaml"
    unreadable.mkdir()
    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**base, "GUARDRAIL_POLICY": str(unreadable)}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM and "could not be read" in out.getvalue()

    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**base, "CREDENTIAL_SOURCE": "vault"}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_PROBLEM
    assert out.getvalue().startswith(
        "CREDENTIAL_SOURCE is vault, but this service has no Vault client.\n"
    )
    out = io.StringIO()
    code = cli.main(
        ["serve"],
        environ={
            **base,
            "CREDENTIAL_SOURCE": "vault",
            "VAULT_ADDR": "https://vault.invalid:8200",
            "VAULT_ROLE_ID_FILE": str(tmp_path / "no-role"),
            "VAULT_SECRET_ID_FILE": str(tmp_path / "no-secret"),
        },
        stdout=out,
        runner=runner,
    )
    assert code == cli.EXIT_PROBLEM
    assert out.getvalue().startswith("The AppRole secret files are not readable.\n")

    # A syslog port that cannot be bound leaves the service up and says so in /health.
    out = io.StringIO()
    code = cli.main(
        ["serve"], environ={**base, "SYSLOG_LISTEN": "256.0.0.1:0"}, stdout=out, runner=runner
    )
    assert code == cli.EXIT_OK
    ((app, _),) = served
    health = TestClient(app, raise_server_exceptions=False).get("/health")
    assert health.status_code == 503
    assert health.json()["what_happened"] == (
        "The validation-executor is not healthy: syslog did not answer."
    )
    assert socket.gethostbyname("localhost")  # the interpreter still resolves names
