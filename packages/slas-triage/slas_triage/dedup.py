"""Dedup and bug-ticket bookkeeping (CLAUDE.md §5.4): one finding per fingerprint, one child
ticket per finding across runs, owner and severity from the routing table when missing."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.finding import Finding
from slas_triage.fingerprint import fingerprint
from slas_triage.routing import OwnerRouting, default_routing, route_owner


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """First occurrence per fingerprint; later occurrences add "seen again" evidence."""
    seen: dict[str, Finding] = {}
    for finding in findings:
        first = seen.get(finding.fingerprint)
        if first is None:
            seen[finding.fingerprint] = finding.model_copy(deep=True)
            continue
        extra = [e for e in finding.evidence if e not in first.evidence]
        first.evidence = [*first.evidence, *extra][:20]
    return list(seen.values())


def triage(findings: list[Finding], routing: OwnerRouting | None = None) -> list[Finding]:
    """Deduplicate and route: owner, component and severity filled in where the finding has none."""
    routing = routing or default_routing()
    out: list[Finding] = []
    for finding in dedup_findings(findings):
        if finding.owner is None or finding.severity is None:
            decision = route_owner(routing, f"{finding.issue}\n" + "\n".join(finding.evidence))
            finding = finding.model_copy(
                update={
                    "owner": finding.owner or decision.owner,
                    "severity": finding.severity or decision.severity,
                    "component": finding.component or decision.component,
                }
            )
        out.append(finding)
    return out


def finding_from_sentence(
    issue: str,
    *,
    evidence: list[str] | None = None,
    owner: str | None = None,
    severity: str | None = None,
    component: str | None = None,
) -> Finding:
    """A Finding whose fingerprint comes from the masked issue sentence."""
    digest = fingerprint(issue)
    return Finding(
        id=f"F-{digest[:8]}",
        fingerprint=digest,
        issue=issue.rstrip("."),
        owner=owner,
        severity=severity,
        component=component,
        evidence=list(evidence or []),
    )


class BugIndexEntry(SlasModel):
    fingerprint: str
    ticket_id: str
    first_seen_in: str
    seen: int = Field(default=1, ge=1)


class BugIndex:
    """fingerprint → bug ticket, durable in one JSON file, so a failure seen in run after run
    stays one ticket (CLAUDE.md §5.4 "after deduplication by fingerprint")."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, BugIndexEntry]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {fp: BugIndexEntry.model_validate(item) for fp, item in raw.items()}

    def _save(self, items: dict[str, BugIndexEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            self.path,
            json.dumps({fp: e.model_dump(mode="json") for fp, e in items.items()}, indent=2) + "\n",
            mode=0o600,
        )

    def lookup(self, digest: str) -> BugIndexEntry | None:
        return self._load().get(digest)

    def record(self, digest: str, ticket_id: str, *, seen_in: str) -> BugIndexEntry:
        items = self._load()
        entry = items.get(digest)
        if entry is None:
            entry = BugIndexEntry(fingerprint=digest, ticket_id=ticket_id, first_seen_in=seen_in)
        else:
            entry = entry.model_copy(update={"seen": entry.seen + 1})
        items[digest] = entry
        self._save(items)
        return entry

    def sentence(self, digest: str) -> str:
        entry = self.lookup(digest)
        if entry is None:
            return "This is the first time this failure was seen."
        return (
            f"Seen {entry.seen} {'time' if entry.seen == 1 else 'times'}; "
            f"tracked as {entry.ticket_id} since {entry.first_seen_in}."
        )
