"""The RCA pipeline (§5.4): normalise → fingerprint → retrieve → draft → consensus → owner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.rca import (
    DEFAULT_OWNER_ROUTING,
    OWNER_ROUTING_FILE_HEADER,
    FakeCrossChecker,
    FakeDrafter,
    OwnerRoutingError,
    RcaDraft,
    RcaPipeline,
    default_routing,
    fingerprint,
    normalise_log,
    normalise_ticket,
    render_owner_routing_yaml,
    route_owner,
    routing_from_mapping,
)
from slas_kernel.store import FileTicketStore
from slas_rag.documents import Document
from slas_rag.index import FakeEmbedder, MemoryTextIndex, MemoryVectorIndex
from slas_rag.retrieval import HybridRetriever
from slas_schemas.finding import Finding
from slas_schemas.job import Job, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import Observation, StepRecord, StepVerdict, Ticket
from slas_schemas.vote import Vote, VoteVerdict

REPO_ROOT = Path(__file__).resolve().parents[2]

_XID = "host kernel: NVRM: Xid (PCI:0000:8a:00): 79, pid=1234, GPU has fallen off the bus."
_AER = "pcieport 0000:80:01.0: AER: Corrected error received: 0000:8a:00.0"
XID_LOG = "\n".join(
    [
        f"2026-09-10T12:00:01Z {_XID}",
        f"2026-09-10T12:00:01Z {_XID}",
        f"[   12.345678] {_AER}",
        f"[   12.345679] {_AER}",
        f"[   12.345680] {_AER}",
        "Sep 10 12:00:02 host validation: PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle 17",
        "Sep 10 12:00:02 host validation: settle finished",
        "",
    ]
)


def votes(*verdicts: VoteVerdict) -> list[Vote]:
    return [
        Vote(
            voter=f"voter-{i}",
            verdict=v,
            reason="riser" if v == "approve" else "could be the GPU itself",
            confidence=0.8,
        )
        for i, v in enumerate(verdicts, start=1)
    ]


def failed_ticket(stderr: str = XID_LOG) -> Ticket:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    job = Job(
        id="job-1", agent="validation", user="pat", title="DC cycle run", inputs=[], created_at=now
    )
    plan = Plan(
        id="plan-1",
        job_id="job-1",
        summary="two steps",
        steps=[
            Step(id="baseline", n=1, primitive="fake", title="Baseline snapshot"),
            Step(id="cycle-17", n=2, primitive="fake", title="DC cycle 17"),
        ],
        created_at=now,
    )
    ticket = Ticket(
        id="T-validation-0007",
        agent="validation",
        user="pat",
        title="DC cycle run",
        job=job,
        plan=plan,
        created_at=now,
        updated_at=now,
    )
    ticket.steps = [
        StepRecord(
            step_id="baseline",
            n=1,
            title="Baseline snapshot",
            status="done",
            observation=Observation(exit_code=0, stdout="ok\n"),
        ),
        StepRecord(
            step_id="cycle-17",
            n=2,
            title="DC cycle 17",
            status="failed",
            observation=Observation(exit_code=3, stderr=stderr),
            verdict=StepVerdict(outcome="fail", sentence="DC cycle 17 exited with code 3."),
        ),
    ]
    return ticket


def knowledge() -> HybridRetriever:
    retriever = HybridRetriever(
        embedder=FakeEmbedder(), vectors=MemoryVectorIndex(), text=MemoryTextIndex()
    )
    retriever.ingest(
        Document(
            id="t-validation-0042",
            title="T-validation-0042 report",
            collection="past-reports",
            text=(
                "PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle. "
                "Root cause: retimer firmware on riser slot 3."
            ),
        )
    )
    retriever.ingest(
        Document(
            id="thermal-note",
            title="Thermal note",
            collection="runbooks",
            text="Fans ramp at 70 °C; a thermal slowdown appears in nvidia-smi above 85 °C.",
        )
    )
    return retriever


# --- normalise + fingerprint ----------------------------------------------------------------


def test_normalise_strips_timestamps_dedups_bursts_and_finds_error_lines() -> None:
    log = normalise_log(XID_LOG)
    assert log.total_lines == 7 and len(log.lines) == 4, "two bursts collapsed, one fence-free"
    assert log.lines[0].startswith("host kernel: NVRM: Xid")
    assert log.counts[log.lines[1]] == 3, "the three AER lines are one line seen three times"
    assert log.lines[3] == "host validation: settle finished"
    assert [line.split(": ", 1)[0] for line in log.error_lines] == [
        "host kernel",
        "pcieport 0000:80:01.0",
        "host validation",
    ]
    assert log.signature.splitlines()[0].startswith("host kernel: NVRM: Xid")
    assert "settle finished" not in log.signature
    quiet = normalise_log("--- step s1: hello ---\nall good\nstill good\n")
    assert quiet.error_lines == [] and quiet.signature == "all good\nstill good"
    assert normalise_log("").signature == ""


def test_fingerprint_is_stable_across_numbers_and_addresses() -> None:
    a = normalise_log(XID_LOG).signature
    b = normalise_log(
        XID_LOG.replace("8a:00", "c1:00").replace("GPU3", "GPU5").replace("cycle 17", "cycle 3")
    ).signature
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint("EDAC MC0: 1 UE on DIMM_A1")


# --- owner routing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "owner", "component", "severity"),
    [
        ("PCIe link lost on GPU3 during DC cycle", "EE", "PCIe", "S2"),
        ("NVRM: Xid 79 GPU has fallen off the bus", "SW", "GPU driver", "S2"),
        ("EDAC MC0: 1 UE on DIMM_A1", "EE", "Memory", "S2"),
        ("boot timeout after 900 s", "FW", "Boot", "S1"),
        ("PSU 2 reports AC power loss", "EE", "Power", "S1"),
        ("thermal slowdown reported", "ME", "Thermal", "S2"),
        ("Redfish returned 500 for Systems/1", "FW", "BMC", "S3"),
        ("nvme0: I/O error, dev nvme0n1", "FW", "Storage", "S3"),
        ("eth0: link flap detected", "EE", "Network", "S3"),
        ("BurnIn window not found on station 4", "TE", "Test station", "S3"),
        ("Traceback (most recent call last)", "SW", "Test software", "S3"),
    ],
)
def test_default_routing_names_the_owner(
    text: str, owner: str, component: str, severity: str
) -> None:
    decision = route_owner(default_routing(), text)
    assert (decision.owner, decision.component, decision.severity) == (owner, component, severity)
    assert decision.sentence().startswith(f"Routed to {owner} ({component}, {severity}) by rule ")


def test_routing_without_a_match_is_your_call_and_the_file_is_in_step() -> None:
    decision = route_owner(default_routing(), "the coffee machine is empty")
    assert (
        decision.owner is None
        and decision.sentence() == "No routing rule matched; the owner is your call."
    )
    expected = render_owner_routing_yaml(DEFAULT_OWNER_ROUTING, header=OWNER_ROUTING_FILE_HEADER)
    assert (REPO_ROOT / "config" / "owner-routing.yaml").read_text(encoding="utf-8") == expected
    assert "  - id: pcie-link\n    owner: EE\n" in expected
    with pytest.raises(OwnerRoutingError) as bad_owner:
        routing_from_mapping(
            {
                "version": 1,
                "owners": {"EE": "x"},
                "rules": [
                    {
                        "id": "a",
                        "owner": "QA",
                        "component": "c",
                        "severity": "S3",
                        "description": "d",
                        "patterns": ["x"],
                    }
                ],
            }
        )
    assert "names owner 'QA'" in bad_owner.value.message.likely_cause
    with pytest.raises(OwnerRoutingError, match="could not be used"):
        routing_from_mapping(
            {
                "version": 1,
                "owners": {"EE": "x"},
                "rules": [
                    {
                        "id": "a",
                        "owner": "EE",
                        "component": "c",
                        "severity": "S3",
                        "description": "d",
                        "patterns": ["("],
                    }
                ],
            }
        )
    with pytest.raises(OwnerRoutingError) as duplicate:
        routing_from_mapping(
            {
                "version": 1,
                "owners": {"EE": "x"},
                "rules": [
                    {
                        "id": "a",
                        "owner": "EE",
                        "component": "c",
                        "severity": "S3",
                        "description": "d",
                        "patterns": ["x"],
                    },
                    {
                        "id": "a",
                        "owner": "EE",
                        "component": "c",
                        "severity": "S3",
                        "description": "d",
                        "patterns": ["y"],
                    },
                ],
            }
        )
    assert "appears twice" in duplicate.value.message.likely_cause


# --- the pipeline -----------------------------------------------------------------------------


def test_pipeline_produces_a_cited_cause_votes_and_an_owner() -> None:
    drafter = FakeDrafter(
        RcaDraft(
            cause=(
                "The retimer firmware on riser slot 3 dropped the PCIe link to GPU3 during "
                "the DC cycle."
            ),
            evidence=["Xid 79 and AER corrected errors precede the link loss."],
            component="riser",
            confidence=0.9,
        )
    )
    checker = FakeCrossChecker(votes("approve", "approve", "concern"), agreed=True)
    pipeline = RcaPipeline(drafter=drafter, cross_checker=checker, retriever=knowledge())
    ticket = failed_ticket()
    result = pipeline.analyse(ticket)

    assert result.rca.cause.startswith("The retimer firmware on riser slot 3")
    assert result.rca.confidence == 0.9 and result.rca.uncertain is False
    assert result.rca.fingerprint == fingerprint(normalise_ticket(ticket).signature)
    assert result.citations[0] == "T-validation-0042 report §1", "the past report is cited first"
    assert "See T-validation-0042 report §1" in result.rca.evidence
    assert any(e.startswith("Cross-check: 2 of 3 agree") for e in result.rca.evidence)
    assert len(result.votes) == 3 and result.verdict is not None and result.verdict.agreed

    # The drafter saw redactable evidence only through the request object, with citations.
    request = drafter.requests[0]
    assert (
        request.ticket_id == "T-validation-0007" and request.failed_step == "Step 2 (DC cycle 17)"
    )
    assert request.retrieved[0].startswith("T-validation-0042 report §1: PCIe link lost")
    assert len(request.log_lines) == 5, "four distinct log lines plus the verdict sentence"
    # Voters saw the signature and the proposed cause, never each other (the checker is given
    # one evidence list and returns one verdict).
    decision, evidence = checker.calls[0]
    assert decision == "rca_conclusion" and evidence[0].startswith("Failure signature:\n")
    assert evidence[1].startswith("Proposed cause: The retimer firmware")

    # Deterministic owner routing, not the model's opinion.
    (finding,) = result.findings
    assert finding.headline() == (
        "[Issue] The retimer firmware on riser slot 3 dropped the PCIe link to GPU3 during the "
        "DC cycle | [Owner] EE"
    )
    assert finding.owner == "EE" and finding.component == "PCIe" and finding.severity == "S2"
    assert (
        finding.evidence[0].startswith("host kernel: NVRM: Xid")
        and finding.evidence[1] == "T-validation-0042 report §1"
    )
    assert result.routing is not None and result.routing.rule_id == "pcie-link"
    assert result.sentence.startswith("Root cause: The retimer firmware")
    assert "2 of 3 agree with the conclusion." in result.sentence
    assert result.sentence.endswith("Routed to EE (PCIe, S2) by rule pcie-link.")


def test_disagreement_marks_the_rca_uncertain_and_caps_confidence() -> None:
    checker = FakeCrossChecker(votes("approve", "concern", "reject"), agreed=False)
    pipeline = RcaPipeline(drafter=FakeDrafter(), cross_checker=checker)
    result = pipeline.analyse(failed_ticket())
    assert result.rca.uncertain is True and result.rca.confidence == 0.5
    assert "The conclusion is marked uncertain in the report." in result.sentence
    assert result.findings[0].owner == "EE", "rules are ordered: pcie-link (AER) before gpu-xid"


def test_without_a_cross_checker_the_rca_says_it_is_one_models_view() -> None:
    result = RcaPipeline(drafter=FakeDrafter()).analyse(failed_ticket())
    assert result.rca.uncertain is True and result.votes == [] and result.verdict is None
    assert "Not cross-checked: no voters are configured" in result.sentence


def test_a_failed_drafter_never_blocks_the_ticket() -> None:
    checker = FakeCrossChecker(votes("approve"), agreed=True)
    pipeline = RcaPipeline(drafter=FakeDrafter(fail=True), cross_checker=checker)
    result = pipeline.analyse(failed_ticket())
    assert result.rca.cause.startswith("Step 2 (DC cycle 17) did not finish as expected.")
    assert result.rca.confidence == 0.0 and result.rca.uncertain
    assert result.findings == [], "no finding is invented from a placeholder cause"
    assert checker.calls == [], "nothing to cross-check without a draft"
    assert any("did not answer" in e for e in result.rca.evidence)
    assert "could not be drafted" in result.sentence


def test_a_repeated_fingerprint_adds_no_second_finding() -> None:
    ticket = failed_ticket()
    digest = fingerprint(normalise_ticket(ticket).signature)
    ticket.findings = [
        Finding(id="F-existing", fingerprint=digest, issue="already known", owner="EE")
    ]
    result = RcaPipeline(drafter=FakeDrafter()).analyse(ticket)
    assert result.findings == []
    assert "matches a finding already on the ticket" in result.sentence


def test_nothing_failed_means_nothing_to_analyse() -> None:
    ticket = failed_ticket()
    ticket.steps[1].status = "done"
    result = RcaPipeline(drafter=FakeDrafter()).analyse(ticket)
    assert result.rca.cause == "Every step finished as expected; there is nothing to analyse."
    assert result.findings == [] and result.rca.confidence == 1.0


def test_signature_falls_back_to_the_step_title_when_the_failure_left_no_output() -> None:
    ticket = failed_ticket(stderr="")
    ticket.steps[1].verdict = None
    drafter = FakeDrafter()
    result = RcaPipeline(drafter=drafter).analyse(ticket)
    assert result.rca.fingerprint == fingerprint("DC cycle 17 failed")
    assert drafter.requests[0].signature == "DC cycle 17 failed"
    assert result.rca.cause == "DC cycle 17 failed caused the failure."


# --- through the kernel ----------------------------------------------------------------------


def test_kernel_runs_the_pipeline_and_attaches_votes_and_findings(tmp_path: Path) -> None:
    class FailingNullAgent(NullAgent):
        def plan(self, job: Job) -> Plan:
            plan = super().plan(job)
            steps = list(plan.steps)
            steps[3] = steps[3].model_copy(update={"args": {"stderr": XID_LOG, "exit_code": 2}})
            return plan.model_copy(update={"steps": steps})

    drafter = FakeDrafter(
        RcaDraft(
            cause="The riser retimer dropped the PCIe link.", evidence=["Xid 79"], confidence=0.85
        )
    )
    checker = FakeCrossChecker(votes("approve", "approve", "approve"), agreed=True)
    kernel = Kernel(
        data_root=tmp_path,
        agent=FailingNullAgent(),
        executor=FakeExecutor(),
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
        rca_pipeline=RcaPipeline(
            drafter=drafter, cross_checker=checker, retriever=knowledge(), retrieve_limit=1
        ),
    )
    ticket = kernel.run(Upload(filename="plan.md", uploaded_by="pat"))
    assert ticket.state.value == "Needs review"
    assert ticket.rca is not None and ticket.rca.cause == "The riser retimer dropped the PCIe link."
    assert [v.voter for v in ticket.votes] == ["voter-1", "voter-2", "voter-3"]
    assert [f.headline() for f in ticket.findings] == [
        "[Issue] The riser retimer dropped the PCIe link | [Owner] EE"
    ]
    # The SOP carries the finding and the fingerprint, identically in both languages.
    sop_dir = tmp_path / "SOP" / ticket.id
    en = (sop_dir / "sop.en.md").read_text(encoding="utf-8")
    zh = (sop_dir / "sop.zh-Hant.md").read_text(encoding="utf-8")
    assert "- [Issue] The riser retimer dropped the PCIe link | [Owner] EE" in en
    assert "- [Issue] The riser retimer dropped the PCIe link | [Owner] EE" in zh
    assert (
        f"- fingerprint: {ticket.rca.fingerprint}" in en
        and f"- fingerprint: {ticket.rca.fingerprint}" in zh
    )
    assert "- Review the findings and decide whether a bug ticket is needed." in en
    # The journal records the analysis as a note with the sentence and the citations.
    entries = [
        json.loads(line)
        for line in (tmp_path / "Tickets" / ticket.id / "journal.jsonl").read_text().splitlines()
    ]
    note = next(e for e in entries if e["kind"] == "note" and "rca_sentence" in e["payload"])
    assert note["payload"]["citations"] == ["T-validation-0042 report §1"]
    assert note["payload"]["votes"] == 3
    # Running the analysis again (a resume) adds no duplicate finding.
    ticket = kernel.resume(ticket.id)
    assert len(ticket.findings) == 1
