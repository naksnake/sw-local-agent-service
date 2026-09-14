"""RCA placeholder (Phase 2): a deterministic fingerprint and an honest sentence.

The real pipeline — normalise → fingerprint → retrieve → draft → consensus → owner routing
(CLAUDE.md §5.4) — arrives in Phase 5. The fingerprint already follows the rule that
dedup is deterministic code, never a model.
"""

from __future__ import annotations

import hashlib
import re

from slas_schemas.ticket import Rca, Ticket

# Hex addresses and every run of digits, including ones glued to words such as GPU3.
_NOISE = re.compile(r"0x[0-9a-fA-F]+|\d+")


def fingerprint(text: str) -> str:
    """A stable 16-hex fingerprint of a failure text with numbers and addresses masked."""
    normalised = _NOISE.sub("#", text).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def placeholder_rca(ticket: Ticket) -> Rca:
    failed = [record for record in ticket.steps if record.status == "failed"]
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
            f"Step {first.n} ({first.title}) did not finish as expected. Root-cause analysis "
            "over logs and knowledge arrives in Phase 5; until then this is a placeholder."
        ),
        evidence=evidence,
        confidence=0.0,
        uncertain=True,
        fingerprint=fingerprint(stderr or first.title),
    )
