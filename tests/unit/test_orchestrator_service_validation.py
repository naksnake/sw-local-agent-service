"""The orchestrator's `/v1/validation` routes over a REAL `ValidationExecutor` behind a fake
executor *service*: the suite parses with destructive flags, the preview carries guardrails
and votes without running anything, a DC-cycle suite runs to Needs review with the LED map
and the console tail, an AC-cycle suite stops at Planned until a person with
`approve:destructive` approves, targets show who holds them, and the route table matches the
contract. `HttpExecutor` is exercised end to end (the kernel's step crosses HTTP as JSON)."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from slas_hal.fakes.bmc import FakeHal, FakeTarget, Plant
from slas_http.app import create_service_app
from slas_http.client import ServiceClient
from slas_http.errors import ServiceError
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.kernel import Kernel
from slas_kernel.rca import FakeCrossChecker, FakeDrafter, RcaPipeline
from slas_kernel.store import FileTicketStore, MemoryTicketStore
from slas_orchestrator.remote import ExecutorReader, HttpExecutor
from slas_orchestrator.service import factory, validation
from slas_orchestrator.service.models import SuiteView
from slas_orchestrator.service.validation import ValidationDeps, WatchedTicketStore
from slas_orchestrator.service.views_validation import run_view, target_views, waiting_cells
from slas_orchestrator.validation.agent import ValidationAgent
from slas_schemas.job import Job, TargetRef
from slas_schemas.plan import Step
from slas_schemas.ticket import Approval, Ticket, TicketState
from slas_schemas.vote import Vote
from slas_validation_executor.executor import CycleCell, RunState, ValidationExecutor
from tests.unit.orchestrator_harness import (
    SyncRunner,
    service_client,
    validation_executor_app,
)
from tests.unit.test_plan_compiler import build_xlsx

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "docs" / "api-contract-round-2.md"
TARGET = "lab-gx8-01"
OTHER = "lab-gx4-01"
GPU3 = "0000:8a:00.0"
SUITE_MD = "# GX8 DC cycling\n\n- DC cycle x25, settle 60 s\n"
TABLE_MD = (
    "# GX8 mixed suite\n\n"
    "| Step | Action | Parameters | Cycles | Approved |\n"
    "|---|---|---|---|---|\n"
    "| Record baseline | baseline | | 1 | |\n"
    "| DC cycle | DC cycle | settle_s=60 | 3 | |\n"
    "| AC cycle | AC cycle | | 2 | yes |\n"
    "| Flash the BMC | firmware flash | component=BMC; image=bmc-1.13 | 1 | no |\n"
)

PAT = Identity("pat@slas.local", "Pat", frozenset({"validation:run"}))
LEE = Identity("lee@slas.local", "Lee", frozenset({"approve:destructive"}))


def votes(reason: str, *verdicts: str) -> list[Vote]:
    verdicts = verdicts or ("approve", "approve", "approve")
    return [
        Vote(voter=f"voter-{i}", verdict=v, reason=reason, confidence=0.9)
        for i, v in enumerate(verdicts, start=1)
    ]


class Bench:
    """The orchestrator app with the Validation router over a fake validation-executor service
    that performs steps with the real executor on a FakeHal."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        plants: list[Plant] | None = None,
        plan_checker: FakeCrossChecker | None = None,
        rca: bool = True,
    ) -> None:
        self.data_root = tmp_path
        self.targets = [FakeTarget(TARGET, plants=plants or []), FakeTarget(OTHER)]
        self.hal = FakeHal(self.targets, clock=FakeClock())
        self.executor = ValidationExecutor(hal=self.hal, data_root=tmp_path, clock=FakeClock())
        self.executor_app = validation_executor_app(self.executor, self.hal, self.targets)
        self.client = service_client("validation-executor", self.executor_app)
        self.agent = ValidationAgent(plans_dir=tmp_path / "Validation" / "Plans")
        self.store = WatchedTicketStore(FileTicketStore(tmp_path))
        self.plan_checker = plan_checker
        self.kernel = Kernel(
            data_root=tmp_path,
            agent=self.agent,
            executor=HttpExecutor(self.client),
            store=self.store,
            clock=FakeClock(),
            plan_checker=plan_checker,
            rca_pipeline=(
                RcaPipeline(
                    drafter=FakeDrafter(),
                    cross_checker=FakeCrossChecker(votes("The riser retimer."), agreed=True),
                )
                if rca
                else None
            ),
        )
        self.runner = SyncRunner()
        self.deps = ValidationDeps(
            kernel=self.kernel,
            agent=self.agent,
            store=self.store,
            executor_reader=ExecutorReader(self.client),
            runner=self.runner,
        )
        self.app = create_service_app(
            "agent-core-orchestrator", routers=(validation.router, factory.router)
        )
        self.app.state.validation = self.deps
        self.http = TestClient(self.app, raise_server_exceptions=False)

    def parse(self, text: str, filename: str = "suite.md") -> dict[str, Any]:
        response = self.http.post(
            "/v1/validation/suites/parse",
            json={"filename": filename, "text": text},
            headers=PAT.headers(),
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def start(self, suite: dict[str, Any], target: str = TARGET) -> dict[str, Any]:
        response = self.http.post(
            "/v1/validation/runs", json={"suite": suite, "target": target}, headers=PAT.headers()
        )
        assert response.status_code == 200, response.text
        return dict(response.json())


# --- suites ---------------------------------------------------------------------------------------


def test_md_suite_parses_with_destructive_flags_and_problems_are_sentences(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    view = bench.parse(TABLE_MD, "gx8-mixed.md")
    assert view["problem"] is None
    assert view["title"] == "GX8 mixed suite" and view["source"] == "gx8-mixed.md"
    assert view["sentence"] == "GX8 mixed suite: 4 items, 7 cycles in total."
    rows = [(i["title"], i["cycles"], i["destructive"], i["approved"]) for i in view["items"]]
    assert rows == [
        ("Record baseline", 1, False, False),
        ("DC cycle", 3, False, False),
        ("AC cycle", 2, True, True),
        ("Flash the BMC", 1, True, False),
    ]
    assert view["items"][1]["params"] == {"settle_s": "60"}
    assert view["items"][3]["params"] == {"component": "BMC", "image": "bmc-1.13"}
    assert view["items"][2]["sentence"] == "3. AC cycle ×2"
    assert SuiteView.model_validate(view).items[1].action == "DC cycle"

    empty = bench.parse("", "empty.md")
    assert empty["items"] == [] and empty["title"] == "empty"
    assert empty["problem"] == (
        "empty.md is empty. A suite needs a title and at least one item. "
        "Add the items and upload again."
    )
    missing = bench.http.post(
        "/v1/validation/suites/parse", json={"filename": "x.md"}, headers=PAT.headers()
    )
    assert missing.json()["problem"].startswith("x.md arrived without its content.")
    without_identity = bench.http.post(
        "/v1/validation/suites/parse", json={"filename": "x.md", "text": SUITE_MD}
    )
    assert without_identity.status_code == 401


def test_xlsx_suite_arrives_as_base64_and_is_parsed_from_a_temp_file(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    workbook = tmp_path / "gx8-suite.xlsx"
    build_xlsx(
        workbook,
        [
            ["Step", "Action", "Parameters", "Cycles", "Approved"],
            ["Baseline", "record baseline", "", 1, ""],
            ["AC cycle", "AC power cycle", "settle 45 s", 4, "yes"],
        ],
    )
    encoded = base64.b64encode(workbook.read_bytes()).decode("ascii")
    response = bench.http.post(
        "/v1/validation/suites/parse",
        json={"filename": "gx8-suite.xlsx", "content_base64": encoded},
        headers=PAT.headers(),
    )
    view = response.json()
    assert response.status_code == 200 and view["problem"] is None
    assert view["title"] == "gx8 suite" and view["source"] == "gx8-suite.xlsx"
    assert [(i["title"], i["cycles"], i["destructive"]) for i in view["items"]] == [
        ("Baseline", 1, False),
        ("AC cycle", 4, True),
    ]
    assert view["items"][1]["params"] == {"settle_s": "45"}

    no_file = bench.http.post(
        "/v1/validation/suites/parse", json={"filename": "s.xlsx"}, headers=PAT.headers()
    )
    assert no_file.json()["problem"].startswith("s.xlsx arrived without its file.")
    bad = bench.http.post(
        "/v1/validation/suites/parse",
        json={"filename": "s.xlsx", "content_base64": "not*base64"},
        headers=PAT.headers(),
    )
    assert bad.status_code == 400 and bad.json()["what_happened"] == "s.xlsx could not be decoded."
    not_zip = bench.http.post(
        "/v1/validation/suites/parse",
        json={"filename": "s.xlsx", "content_base64": base64.b64encode(b"zzz").decode()},
        headers=PAT.headers(),
    )
    assert not_zip.json()["problem"].startswith("s.xlsx is not an .xlsx file.")


# --- targets and preview -------------------------------------------------------------------------


def test_targets_pass_through_with_free_and_holder(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.executor.leases.acquire(
        OTHER, ticket_id="T-validation-0007", user="lee", now=FakeClock().now(), max_hours=72
    )
    response = bench.http.get("/v1/validation/targets", headers=PAT.headers())
    assert response.status_code == 200
    rows = response.json()
    assert [(r["ref"], r["model"], r["free"]) for r in rows] == [
        (TARGET, "SLAS-GX8", True),
        (OTHER, "SLAS-GX8", False),
    ]
    assert rows[0]["holder"] is None
    assert (
        rows[1]["holder"]
        == "lab-gx4-01 is leased to T-validation-0007 (lee) until 2026-09-17 08:00."
    )
    assert rows[1]["armed"] is True and rows[1]["sentence"].startswith("lab-gx4-01: SLAS-GX8;")


def test_preview_compiles_with_guardrails_and_votes_without_running(tmp_path: Path) -> None:
    checker = FakeCrossChecker(votes("The plan stays within the guardrails."), agreed=True)
    bench = Bench(tmp_path, plan_checker=checker)
    suite = bench.parse(SUITE_MD, "gx8.md")
    response = bench.http.post(
        "/v1/validation/preview", json={"suite": suite, "target": TARGET}, headers=PAT.headers()
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["sentence"] == (
        "GX8 DC cycling on lab-gx8-01: 25 power cycles and 1 suite items, 30 steps. "
        "Nothing destructive."
    )
    assert preview["step_count"] == 30 and preview["cycle_count"] == 25
    assert preview["destructive_steps"] == []
    assert preview["guardrails"][0] == "At most 100 power cycles per run."
    assert preview["guardrails"][-1].startswith("Needs your approval every run: ac_cycle,")
    assert preview["cross_check"]["agreed"] is True
    assert [v["voter"] for v in preview["cross_check"]["votes"]] == [
        "voter-1",
        "voter-2",
        "voter-3",
    ]
    assert preview["steps"][3] == {
        "id": "cycle-001",
        "title": "DC cycle 1 of 25",
        "destructive": False,
    }
    # The same evidence the kernel shows the voters before ACT (§5.3) — and nothing ran.
    assert len(checker.calls) == 1 and checker.calls[0][0] == "plan_approval"
    assert checker.calls[0][1][0] == preview["sentence"]
    assert checker.calls[0][1][4] == "4. DC cycle 1 of 25 [caution]"
    assert len(checker.calls[0][1]) == 31
    assert bench.hal.target(TARGET).power_records == []
    assert bench.store.list_ids() == []

    # An AC suite: destructive steps are named; a suite with an unknown action is a 400.
    ac = bench.parse("- AC cycle x2, approved\n", "ac.md")
    ac_preview = bench.http.post(
        "/v1/validation/preview", json={"suite": ac, "target": TARGET}, headers=PAT.headers()
    ).json()
    assert ac_preview["destructive_steps"] == ["AC cycle 1 of 2", "AC cycle 2 of 2"]
    assert ac_preview["sentence"].endswith(
        "2 destructive steps need your approval before the run starts."
    )
    unknown = bench.parse("- Dance a jig\n", "jig.md")
    refused = bench.http.post(
        "/v1/validation/preview", json={"suite": unknown, "target": TARGET}, headers=PAT.headers()
    )
    assert refused.status_code == 400
    assert (
        refused.json()["what_happened"]
        == "Step 1 (Dance a jig) names no known action: 'Dance a jig'."
    )
    problem = bench.parse("", "empty.md")
    no_items = bench.http.post(
        "/v1/validation/preview", json={"suite": problem, "target": TARGET}, headers=PAT.headers()
    )
    assert (
        no_items.status_code == 400
        and no_items.json()["what_happened"] == "empty.md has no items to run."
    )
    # Without a plan checker the preview says so with null.
    plain = Bench(tmp_path / "plain")
    assert (
        plain.http.post(
            "/v1/validation/preview", json={"suite": suite, "target": TARGET}, headers=PAT.headers()
        ).json()["cross_check"]
        is None
    )


# --- runs -----------------------------------------------------------------------------------------


def test_a_dc_cycle_suite_runs_to_needs_review_through_the_remote_executor(tmp_path: Path) -> None:
    checker = FakeCrossChecker(votes("The plan stays within the guardrails."), agreed=True)
    bench = Bench(
        tmp_path,
        plants=[Plant(at_cycle=14, kind="pcie_width", bdf=GPU3, width=8)],
        plan_checker=checker,
    )
    suite = bench.parse(SUITE_MD, "gx8.md")
    view = bench.start(suite)

    assert view["ticket_id"] == "T-validation-0001"
    assert view["state"] == "Needs review" and view["target"] == TARGET
    assert view["title"] == "GX8 DC cycling"
    assert view["sentence"] == "25 of 25 cycles done: 12 with findings."
    assert view["pending_approvals"] == []
    statuses = [c["status"] for c in view["cells"]]
    assert statuses == ["ok"] * 13 + ["finding"] * 12
    assert view["cells"][13]["kind"] == "dc" and view["cells"][13]["sentence"].startswith(
        "Cycle 14 (DC): booted; 1 change against the baseline"
    )
    assert view["console_tail"], "the console tail is present"
    assert "--- slas fence T-validation-0001 cycle 25 dc ---" in view["console_tail"]
    assert len(view["console_tail"]) <= 40
    assert view["findings"] == [
        "[Issue] PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 "
        "during DC cycle 14 | [Owner] EE"
    ]
    assert view["finding_details"] == [
        {
            "sentence": "PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 "
            "during DC cycle 14",
            "owner": "EE",
            "ticket_id": "T-validation-0002",
        }
    ]
    assert view["votes"][:3] == [
        f"voter-{i} approves: The plan stays within the guardrails." for i in (1, 2, 3)
    ]
    assert len(view["votes"]) == 6, "plan votes and RCA votes"

    # The run went through the wire: the executor performed 30 steps on the FakeHal, the plan
    # was compiled from the round-tripped suite (25 cycles, settle 60 s), and the kernel's
    # ticket carries every observation.
    ticket = bench.store.load("T-validation-0001")
    assert bench.runner.started == [ticket.job.id]
    assert ticket.state is TicketState.NEEDS_REVIEW
    assert len(ticket.steps) == 30 and all(r.status == "done" for r in ticket.steps)
    assert ticket.plan is not None and ticket.plan.steps[3].args["settle_s"] == 60
    assert len(bench.hal.target(TARGET).power_records) == 50
    assert ticket.job.inputs[0].name == "gx8.md"

    # The list and the detail agree; the bug ticket is not listed as a run.
    listed = bench.http.get("/v1/validation/runs", headers=PAT.headers()).json()
    assert [r["ticket_id"] for r in listed] == ["T-validation-0001"]
    assert listed[0]["cells"] == view["cells"] and listed[0]["console_tail"] == []
    detail = bench.http.get("/v1/validation/runs/T-validation-0001", headers=PAT.headers()).json()
    assert detail == view
    missing = bench.http.get("/v1/validation/runs/T-validation-0099", headers=PAT.headers())
    assert missing.status_code == 404
    assert missing.json()["what_happened"] == "There is no validation run T-validation-0099."
    child = bench.http.get("/v1/validation/runs/T-validation-0002", headers=PAT.headers())
    assert child.status_code == 404, "a bug ticket is not a run"

    # A second run of the same suite on the other target: newest first.
    second = bench.start(suite, OTHER)
    assert second["ticket_id"] == "T-validation-0003" and second["state"] == "Done"
    assert [
        r["ticket_id"] for r in bench.http.get("/v1/validation/runs", headers=PAT.headers()).json()
    ] == [
        "T-validation-0003",
        "T-validation-0001",
    ]


def test_an_ac_cycle_suite_waits_at_planned_until_someone_with_the_capability_approves(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path, rca=False)
    suite = bench.parse("# AC test\n\n- AC cycle x2, approved\n", "ac.md")
    view = bench.start(suite)
    assert view["state"] == "Planned"
    assert view["pending_approvals"] == ["AC cycle 1 of 2", "AC cycle 2 of 2"]
    assert (
        view["sentence"] == "T-validation-0001 is planned and waiting: 2 steps need your approval."
    )
    assert [c["status"] for c in view["cells"]] == ["waiting", "waiting"], "the map before any step"
    assert [c["kind"] for c in view["cells"]] == ["ac", "ac"]
    assert view["console_tail"] == [] and bench.hal.target(TARGET).power_records == []

    refused = bench.http.post(
        "/v1/validation/runs/T-validation-0001/approve", json={}, headers=PAT.headers()
    )
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == "Pat may not approve destructive steps."
    assert bench.store.load("T-validation-0001").pending_approvals, "nothing was approved"

    approved = bench.http.post(
        "/v1/validation/runs/T-validation-0001/approve", json={}, headers=LEE.headers()
    )
    assert approved.status_code == 200, approved.text
    done = approved.json()
    assert done["state"] == "Done" and done["pending_approvals"] == []
    assert [c["status"] for c in done["cells"]] == ["ok", "ok"]
    assert done["sentence"] == "2 of 2 cycles done."
    assert [r.action for r in bench.hal.target(TARGET).power_records] == ["ac_cycle", "ac_cycle"]
    ticket = bench.store.load("T-validation-0001")
    assert all(a.approved and a.decided_by == "Lee" for a in ticket.approvals)
    assert ticket.history[1].reason == "Every destructive step was approved."
    assert bench.runner.started == [ticket.job.id, ticket.job.id]

    again = bench.http.post(
        "/v1/validation/runs/T-validation-0001/approve", json={}, headers=LEE.headers()
    )
    assert again.status_code == 409
    assert again.json()["what_happened"] == "T-validation-0001 has nothing waiting for approval."
    nowhere = bench.http.post(
        "/v1/validation/runs/T-validation-0042/approve", json={}, headers=LEE.headers()
    )
    assert nowhere.status_code == 404


def test_starting_a_suite_the_compiler_refuses_is_a_400_and_no_ticket_exists(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path)
    unapproved = bench.parse("- AC cycle x2\n", "ac.md")
    refused = bench.http.post(
        "/v1/validation/runs", json={"suite": unapproved, "target": TARGET}, headers=PAT.headers()
    )
    assert refused.status_code == 400
    assert refused.json()["what_happened"].startswith("Step 1 (AC cycle) is destructive (ac_cycle)")
    assert bench.store.list_ids() == [] and bench.runner.started == []
    empty = bench.http.post(
        "/v1/validation/runs",
        json={"suite": bench.parse("", "e.md"), "target": TARGET},
        headers=PAT.headers(),
    )
    assert empty.status_code == 400 and empty.json()["what_happened"] == "e.md has no items to run."
    bad_body = bench.http.post(
        "/v1/validation/runs", json={"target": TARGET}, headers=PAT.headers()
    )
    assert bad_body.status_code == 400 and bad_body.json()["what_happened"].startswith(
        "The request couldn't be read"
    )


# --- the remote executor --------------------------------------------------------------------------


def test_http_executor_carries_errors_and_unknown_primitives_across_the_wire(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path)
    executor = HttpExecutor(bench.client)
    context = ExecutionContext(
        ticket_id="T-validation-0001", job_id="j1", agent="validation", user="pat"
    )
    lease = Step(id="lease", n=1, primitive="lease_target", title="Lease", args={"target": TARGET})
    observation = executor.execute(lease, context)
    assert observation.exit_code == 0 and observation.summary.startswith(
        "Leased lab-gx8-01 for this run."
    )
    with pytest.raises(UnknownPrimitiveError) as unknown:
        executor.execute(
            Step(id="s7", n=7, primitive="shell", title="shell 7", args={"target": TARGET}), context
        )
    assert unknown.value.step.id == "s7"
    # A crash inside the executor is a three-part 500 the kernel journals as the step's failure.
    with pytest.raises(ServiceError) as crashed:
        executor.execute(
            Step(
                id="s8",
                n=8,
                primitive="power_cycle",
                title="cycle",
                args={"target": TARGET, "kind": "dc", "cycle": 1},
            ),
            context,
        )
    assert crashed.value.status == 500
    assert (
        crashed.value.message.what_happened
        == "The validation-executor hit a problem it did not expect."
    )
    # The reader: a run the executor never touched is None, not an error.
    reader = ExecutorReader(bench.client)
    assert reader.run_state("T-validation-0099") is None
    assert list(reader.list_runs()) == ["T-validation-0001"], "the crashed cycle left its map"
    assert reader.job_state("T-factory-0001") is None
    unreachable = HttpExecutor(
        ServiceClient("validation-executor", "http://127.0.0.1:9", timeout_s=0.2)
    )
    with pytest.raises(ServiceError) as down:
        unreachable.execute(lease, context)
    assert (
        down.value.status == 503
        and down.value.message.what_happened == "The validation-executor did not answer."
    )


def test_the_watched_store_reports_the_ticket_a_job_gets_and_ignores_bug_tickets() -> None:
    store = WatchedTicketStore(MemoryTicketStore())
    waiter = store.expect("job-1")
    assert waiter.wait(0.01) is None
    ticket_id = store.next_ticket_id("validation")
    assert ticket_id == "T-validation-0001" and store.list_ids() == []
    now = FakeClock().now()
    job = Job(id="job-1", agent="validation", user="pat", title="t", created_at=now)
    child = Ticket(
        id="T-validation-0002",
        agent="validation",
        user="pat",
        title="bug",
        job=job,
        parent="T-validation-0001",
        created_at=now,
        updated_at=now,
    )
    store.save(child)
    assert waiter.wait(0.01) is None, "a child ticket never resolves a run's waiter"
    parent = Ticket(
        id="T-validation-0001",
        agent="validation",
        user="pat",
        title="run",
        job=job,
        created_at=now,
        updated_at=now,
    )
    store.save(parent)
    assert waiter.wait(0.01) == "T-validation-0001"
    assert store.load("T-validation-0001").title == "run"
    assert store.list_ids() == ["T-validation-0001", "T-validation-0002"]


# --- the contract ---------------------------------------------------------------------------------


def contract_routes(prefix: str) -> set[tuple[str, str]]:
    text = CONTRACT.read_text(encoding="utf-8")
    found = re.findall(rf"`(GET|POST|PUT|DELETE) ({re.escape(prefix)}[^`\s]*)`", text)
    return {(method, re.sub(r"\{[^}]+\}", "{}", path)) for method, path in found}


def app_routes(prefix: str) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for router in (validation.router, factory.router):
        for route in router.routes:
            path = str(getattr(route, "path", ""))
            methods = getattr(route, "methods", None) or set()
            if path.startswith(prefix):
                for method in methods:
                    routes.add((str(method), re.sub(r"\{[^}]+\}", "{}", path)))
    return routes


def test_the_route_tables_match_the_contract() -> None:
    assert app_routes("/v1/validation") == contract_routes("/v1/validation")
    assert app_routes("/v1/factory") == contract_routes("/v1/factory")
    assert len(contract_routes("/v1/validation")) == 7
    assert len(contract_routes("/v1/factory")) == 9


def test_validation_views_fall_back_when_the_executor_or_the_plan_is_missing() -> None:
    assert waiting_cells(None) == []
    rows = target_views([{"model": "no ref"}, {"alias": "lab-gx4-02", "holder": "busy"}])
    assert [(r.ref, r.free, r.sentence) for r in rows] == [
        ("lab-gx4-02", False, "lab-gx4-02 is busy.")
    ]
    now = FakeClock().now()
    job = Job(
        id="job-x",
        agent="validation",
        user="pat",
        title="GX8",
        target=TargetRef(kind="server", ref=TARGET),
        created_at=now,
    )
    ticket = Ticket(
        id="T-validation-0001",
        agent="validation",
        user="pat",
        title="GX8",
        job=job,
        approvals=[Approval(step_id="cycle-001", requested_at=now)],
        created_at=now,
        updated_at=now,
    )
    bare = run_view(ticket, None, [])
    assert bare.cells == [] and bare.pending_approvals == ["cycle-001"], "no plan: the step id"
    assert bare.sentence == "T-validation-0001 is open." and bare.target == TARGET
    for target in (TicketState.PLANNED, TicketState.APPROVED, TicketState.RUNNING):
        ticket.transition(target, now, "test")
    state = RunState(
        ticket_id=ticket.id,
        target=TARGET,
        cycles=[CycleCell(n=1, kind="dc", status="ok"), CycleCell(n=2, kind="dc")],
    )
    running = run_view(ticket, state, ["line"])
    assert running.sentence == (
        "T-validation-0001 is running: 0 of 0 steps done. 1 of 2 cycles done."
    )
    assert running.console_tail == ["line"]
    assert [c.status for c in running.cells] == ["ok", "waiting"]
