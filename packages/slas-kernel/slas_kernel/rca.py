"""The RCA pipeline (CLAUDE.md §5.4): kernel code, identical for every agent.

    normalise ──► fingerprint ──► retrieve ──► draft ──► consensus ──► owner routing
     (code)         (code)        (slas_rag)   (model)   (router)        (code)

Code does what needs exactness: log normalisation, the fingerprint used for dedup, and the
owner/severity routing table (`config/owner-routing.yaml`). A model drafts the cause and
its evidence, schema-constrained, and the Consensus Router checks the conclusion; a
disagreement marks the RCA "uncertain" and the ticket still goes to a human (INV-11). The
drafter and cross-checker are protocols the orchestrator wires to the LLM gateway, which
redacts every log line before a model sees it (INV-5). RCA never blocks a run and never
performs an action.
"""

from __future__ import annotations

import re
from typing import Final, Protocol

from pydantic import Field

from slas_rag.retrieval import Hit
from slas_schemas.common import SlasModel
from slas_schemas.finding import Finding
from slas_schemas.ticket import Rca, Ticket
from slas_schemas.vote import ConsensusVerdict, Vote
from slas_triage.fingerprint import fingerprint
from slas_triage.routing import (
    DEFAULT_OWNER_ROUTING,
    OWNER_ROUTING_FILE_HEADER,
    Owner,
    OwnerRouting,
    OwnerRoutingError,
    OwnerRule,
    RoutingDecision,
    default_routing,
    render_owner_routing_yaml,
    route_owner,
    routing_from_mapping,
)

# --- normalise ---------------------------------------------------------------------------

# PCI addresses, hex addresses and every run of digits, including ones glued to words (GPU3).
_NOISE = re.compile(
    r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?\b|0x[0-9a-fA-F]+|\d+"
)
_TIMESTAMP = re.compile(
    r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*"
    r"|^\[\s*\d+\.\d+\]\s*"  # dmesg
    r"|^[A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2} "  # syslog "Sep 10 12:00:00 "
)
_FENCE = re.compile(r"^--- step \S+: .* ---$")
_ERROR = re.compile(
    r"error|fail|fatal|panic|\bxid\b|\bmce\b|\bedac\b|\baer\b|timeout|timed out|lost|degraded"
    r"|critical|uncorrectable|traceback|segfault|not found|denied|exited with",
    re.IGNORECASE,
)
MAX_SIGNATURE_LINES: Final = 5
MAX_LOG_LINES: Final = 200


class NormalisedLog(SlasModel):
    #: Distinct lines in first-seen order, timestamps stripped, whitespace collapsed.
    lines: list[str] = Field(default_factory=list)
    #: How often each distinct line occurred (a burst of 400 identical AER lines is one line).
    counts: dict[str, int] = Field(default_factory=dict)
    #: The lines that look like errors, in order; empty when nothing did.
    error_lines: list[str] = Field(default_factory=list)
    total_lines: int = Field(ge=0)

    @property
    def signature(self) -> str:
        """The text the fingerprint, the retrieval query and the routing table work on."""
        chosen = self.error_lines[:MAX_SIGNATURE_LINES] or self.lines[-MAX_SIGNATURE_LINES:]
        return "\n".join(chosen)


def normalise_log(text: str) -> NormalisedLog:
    lines: list[str] = []
    counts: dict[str, int] = {}
    errors: list[str] = []
    total = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or _FENCE.match(stripped):
            continue
        total += 1
        line = re.sub(r"\s+", " ", _TIMESTAMP.sub("", stripped)).strip()
        if not line:
            continue
        if line in counts:
            counts[line] += 1
            continue
        counts[line] = 1
        lines.append(line)
        if _ERROR.search(line):
            errors.append(line)
    return NormalisedLog(lines=lines, counts=counts, error_lines=errors, total_lines=total)


def normalise_ticket(ticket: Ticket) -> NormalisedLog:
    """The failed step's output first; if nothing failed, every step's stderr."""
    failed = [record for record in ticket.steps if record.status == "failed"]
    parts: list[str] = []
    for record in failed or ticket.steps:
        if record.observation is None:
            continue
        parts.append(record.observation.stderr)
        if failed:
            parts.append(record.observation.stdout)
            if record.verdict is not None:
                parts.append(record.verdict.sentence)
    return normalise_log("\n".join(parts))


# --- draft, consensus, pipeline -------------------------------------------------------------


class RcaRequest(SlasModel):
    """What the drafter sees: the signature, the normalised lines, and retrieved passages.

    Secrets never reach here unredacted: the gateway redacts at its boundary (INV-5), and the
    request carries opaque ticket and step identifiers only.
    """

    ticket_id: str
    title: str
    failed_step: str | None = None
    signature: str
    log_lines: list[str] = Field(default_factory=list, max_length=MAX_LOG_LINES)
    retrieved: list[str] = Field(default_factory=list)


class RcaDraft(SlasModel):
    """The schema the model's answer is constrained to (guided_json)."""

    cause: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    component: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class Drafter(Protocol):
    def draft(self, request: RcaRequest) -> RcaDraft: ...


class CrossChecker(Protocol):
    """The Consensus Router seen from the kernel: a decision name and the evidence lines."""

    def cross_check(self, decision: str, evidence: list[str]) -> ConsensusVerdict: ...


class Retriever(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[Hit]: ...


class RcaResult(SlasModel):
    rca: Rca
    findings: list[Finding] = Field(default_factory=list)
    votes: list[Vote] = Field(default_factory=list)
    verdict: ConsensusVerdict | None = None
    citations: list[str] = Field(default_factory=list)
    routing: RoutingDecision | None = None
    sentence: str = Field(min_length=1)


def _first_sentence(text: str) -> str:
    match = re.match(r"\s*(.+?[.!?])(?:\s|$)", text, re.DOTALL)
    sentence = (match.group(1) if match else text).strip()
    return sentence.rstrip(".") if len(sentence) <= 160 else sentence[:157].rstrip() + "…"


class RcaPipeline:
    def __init__(
        self,
        *,
        drafter: Drafter,
        cross_checker: CrossChecker | None = None,
        retriever: Retriever | None = None,
        routing: OwnerRouting | None = None,
        retrieve_limit: int = 5,
    ) -> None:
        self.drafter = drafter
        self.cross_checker = cross_checker
        self.retriever = retriever
        self.routing = routing or default_routing()
        self.retrieve_limit = retrieve_limit

    def analyse(self, ticket: Ticket) -> RcaResult:
        failed = [record for record in ticket.steps if record.status == "failed"]
        if not failed and not ticket.findings:
            rca = placeholder_rca(ticket)
            return RcaResult(rca=rca, sentence=rca.cause)

        # 1 · normalise  2 · fingerprint
        log = normalise_ticket(ticket)
        if failed:
            signature = log.signature or f"{failed[0].title} failed"
            digest = fingerprint(signature)
            failed_step = f"Step {failed[0].n} ({failed[0].title})"
        else:
            # Every step finished but VERIFY reported findings (§10.2): the first finding is
            # the signature and keeps its fingerprint, so no second finding is invented.
            lead = ticket.findings[0]
            signature = "\n".join([lead.issue, *lead.evidence[: MAX_SIGNATURE_LINES - 1]])
            digest = lead.fingerprint
            failed_step = "No step failed; VERIFY reported findings"

        # 3 · retrieve similar past tickets and knowledge
        hits: list[Hit] = []
        if self.retriever is not None:
            hits = self.retriever.search(signature, limit=self.retrieve_limit)
        citations = [hit.citation() for hit in hits]

        # 4 · draft (model, schema-constrained; failure falls back to the honest placeholder)
        request = RcaRequest(
            ticket_id=ticket.id,
            title=ticket.title,
            failed_step=failed_step,
            signature=signature,
            log_lines=log.lines[:MAX_LOG_LINES],
            retrieved=[f"{hit.citation()}: {hit.chunk.text}" for hit in hits],
        )
        draft_failed: str | None = None
        try:
            draft = self.drafter.draft(request)
        except Exception as exc:  # RCA never blocks the run, whatever the model client raised
            draft_failed = str(exc)
            fallback = placeholder_rca(ticket)
            draft = RcaDraft(cause=fallback.cause, evidence=fallback.evidence, confidence=0.0)

        # 5 · consensus on the conclusion (input to a human, never an authorisation)
        verdict: ConsensusVerdict | None = None
        if self.cross_checker is not None and draft_failed is None:
            verdict = self.cross_checker.cross_check(
                "rca_conclusion",
                [
                    f"Failure signature:\n{signature}",
                    f"Proposed cause: {draft.cause}",
                    *draft.evidence,
                ],
            )
        agreed = verdict is not None and verdict.agreed
        uncertain = draft_failed is not None or not agreed
        confidence = draft.confidence if agreed else min(draft.confidence, 0.5)

        # 6 · owner routing (deterministic)
        routing = route_owner(self.routing, f"{signature}\n{draft.cause}\n{draft.component or ''}")

        evidence = [*draft.evidence]
        evidence.extend(f"See {citation}" for citation in citations)
        if verdict is not None:
            evidence.append(f"Cross-check: {verdict.sentence}")
        if draft_failed is not None:
            evidence.append(f"The drafting model did not answer: {draft_failed}")
        rca = Rca(
            cause=draft.cause,
            evidence=evidence,
            confidence=confidence,
            uncertain=uncertain,
            fingerprint=digest,
        )

        findings: list[Finding] = []
        already = {finding.fingerprint for finding in ticket.findings}
        if digest not in already and draft_failed is None:
            findings.append(
                Finding(
                    id=f"F-{digest[:8]}",
                    fingerprint=digest,
                    issue=_first_sentence(draft.cause),
                    owner=routing.owner,
                    severity=routing.severity,
                    component=routing.component or draft.component,
                    evidence=[signature.splitlines()[0], *citations],
                )
            )

        parts = [f"Root cause: {draft.cause}"]
        if verdict is not None:
            parts.append(verdict.sentence)
        elif draft_failed is None:
            parts.append(
                "Not cross-checked: no voters are configured, so treat this as one model's view."
            )
        else:
            parts.append(
                "Root-cause analysis could not be drafted; the failure is attached for review."
            )
        parts.append(routing.sentence())
        if digest in already:
            parts.append(
                "This failure matches a finding already on the ticket; no new finding was added."
            )
        return RcaResult(
            rca=rca,
            findings=findings,
            votes=list(verdict.votes) if verdict is not None else [],
            verdict=verdict,
            citations=citations,
            routing=routing,
            sentence=" ".join(parts),
        )


# --- fakes and the placeholder --------------------------------------------------------------


def placeholder_rca(ticket: Ticket) -> Rca:
    """What a ticket gets when no RCA pipeline is configured, or nothing failed."""
    failed = [record for record in ticket.steps if record.status == "failed"]
    if not failed and ticket.findings:
        count = len(ticket.findings)
        return Rca(
            cause=(
                f"Every step finished, but VERIFY reported {count} "
                f"{'finding' if count == 1 else 'findings'}. No root-cause pipeline is "
                "configured for this kernel, so the cause has not been analysed."
            ),
            evidence=[finding.issue for finding in ticket.findings[:MAX_SIGNATURE_LINES]],
            confidence=0.0,
            uncertain=True,
            fingerprint=ticket.findings[0].fingerprint,
        )
    if not failed:
        return Rca(
            cause="Every step finished as expected; there is nothing to analyse.",
            evidence=[f"{len(ticket.steps)} steps finished with exit code 0."],
            confidence=1.0,
            uncertain=False,
            fingerprint=fingerprint("no failure"),
        )
    first = failed[0]
    observation = first.observation
    stderr = observation.stderr if observation else ""
    evidence = [f"Step {first.n} ({first.title}) failed."]
    if stderr.strip():
        evidence.append(f"stderr: {stderr.strip().splitlines()[-1]}")
    return Rca(
        cause=(
            f"Step {first.n} ({first.title}) did not finish as expected. No root-cause pipeline "
            "is configured for this kernel, so the cause has not been analysed."
        ),
        evidence=evidence,
        confidence=0.0,
        uncertain=True,
        fingerprint=fingerprint(stderr or first.title),
    )


class FakeDrafter:
    def __init__(self, draft: RcaDraft | None = None, *, fail: bool = False) -> None:
        self.draft_value = draft
        self.fail = fail
        self.requests: list[RcaRequest] = []

    def draft(self, request: RcaRequest) -> RcaDraft:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("the triage instance did not answer")
        if self.draft_value is not None:
            return self.draft_value
        return RcaDraft(
            cause=f"{request.signature.splitlines()[0]} caused the failure.",
            evidence=[request.signature.splitlines()[0]],
            confidence=0.8,
        )


class FakeCrossChecker:
    """Returns a scripted verdict; records what evidence the voters were shown."""

    def __init__(self, votes: list[Vote], *, agreed: bool, sentence: str | None = None) -> None:
        self.votes = votes
        self.agreed = agreed
        self.sentence = sentence
        self.calls: list[tuple[str, list[str]]] = []

    def cross_check(self, decision: str, evidence: list[str]) -> ConsensusVerdict:
        self.calls.append((decision, list(evidence)))
        approvals = sum(1 for vote in self.votes if vote.verdict == "approve")
        sentence = self.sentence or (
            f"{approvals} of {len(self.votes)} agree with the conclusion."
            + ("" if self.agreed else " The conclusion is marked uncertain in the report.")
        )
        return ConsensusVerdict(
            decision=decision,
            rule="majority",
            votes=self.votes,
            agreed=self.agreed,
            sentence=sentence,
            concerns=[
                f"{v.voter} has a concern: {v.reason}" for v in self.votes if v.verdict != "approve"
            ],
        )


__all__ = [
    "DEFAULT_OWNER_ROUTING",
    "MAX_LOG_LINES",
    "MAX_SIGNATURE_LINES",
    "OWNER_ROUTING_FILE_HEADER",
    "CrossChecker",
    "Drafter",
    "FakeCrossChecker",
    "FakeDrafter",
    "NormalisedLog",
    "Owner",
    "OwnerRouting",
    "OwnerRoutingError",
    "OwnerRule",
    "RcaDraft",
    "RcaPipeline",
    "RcaRequest",
    "RcaResult",
    "Retriever",
    "RoutingDecision",
    "default_routing",
    "fingerprint",
    "normalise_log",
    "normalise_ticket",
    "placeholder_rca",
    "render_owner_routing_yaml",
    "route_owner",
    "routing_from_mapping",
]
