"""slas_triage: fingerprint, dedup, owner routing and the durable bug index (CLAUDE.md §5.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slas_schemas.finding import Finding
from slas_triage.dedup import BugIndex, dedup_findings, finding_from_sentence, triage
from slas_triage.fingerprint import fingerprint, normalise
from slas_triage.routing import default_routing, route_owner


def test_fingerprint_masks_numbers_hex_and_pci_addresses() -> None:
    a = "PCIe link width changed on GPU3 (0000:8a:00.0): x16 → x8 during DC cycle 14"
    b = "PCIe link width changed on GPU7 (0000:de:00.0): x16 → x4 during DC cycle 25"
    assert normalise(a) == "pcie link width changed on gpu# (#): x# → x# during dc cycle #"
    assert fingerprint(a) == fingerprint(b)
    assert len(fingerprint(a)) == 16
    assert fingerprint("Xid 79 at 0xdeadbeef") == fingerprint("Xid 31 at 0x1")
    assert fingerprint("EDAC UE count rose") != fingerprint("MCE count rose")


def test_finding_from_sentence_strips_the_full_stop_and_keeps_what_is_given() -> None:
    finding = finding_from_sentence(
        "PCIe link width changed on GPU3 (0000:8a:00.0): x16 → x8 during DC cycle 14.",
        evidence=["cycle 14: x16 → x8"],
        severity="S2",
        component="NVIDIA H100 SXM",
    )
    assert finding.issue.endswith("during DC cycle 14")
    assert finding.id == f"F-{finding.fingerprint[:8]}"
    assert (finding.owner, finding.severity, finding.component) == (None, "S2", "NVIDIA H100 SXM")
    assert finding.headline() == (
        "[Issue] PCIe link width changed on GPU3 (0000:8a:00.0): x16 → x8 during DC cycle 14 "
        "| [Owner] unassigned"
    )


def test_dedup_keeps_the_first_and_merges_evidence_up_to_twenty() -> None:
    findings = [
        finding_from_sentence(
            f"PCIe link width changed on GPU3 (0000:8a:00.0): x16 → x8 during DC cycle {n}",
            evidence=[f"cycle {n}: x16 → x8"],
        )
        for n in range(14, 40)
    ]
    findings.append(finding_from_sentence("Xid 79 on GPU3", evidence=["dmesg line"]))
    deduped = dedup_findings(findings)
    assert len(deduped) == 2
    first, xid = deduped
    assert first.issue.endswith("cycle 14"), "the first sighting names the finding"
    assert len(first.evidence) == 20 and first.evidence[0] == "cycle 14: x16 → x8"
    assert xid.evidence == ["dmesg line"]
    assert findings[0].evidence == ["cycle 14: x16 → x8"], "inputs are not mutated"


def test_triage_routes_owner_component_and_severity_where_missing() -> None:
    pcie = finding_from_sentence("PCIe link width changed on GPU3: x16 → x8 during DC cycle 14")
    boot = finding_from_sentence("lab-gx8-01 failed to boot 3 times in a row", severity="S1")
    already = finding_from_sentence("Fan 3 stopped", owner="ME", severity="S3", component="Fan")
    mystery = finding_from_sentence("Something odd happened")
    routed = triage([pcie, pcie, boot, already, mystery])
    assert [(f.owner, f.severity, f.component) for f in routed] == [
        ("EE", "S2", "PCIe"),
        ("FW", "S1", "Boot"),
        ("ME", "S3", "Fan"),
        (None, None, None),
    ]
    assert routed[0].headline().endswith("| [Owner] EE")
    assert routed[3].headline().endswith("| [Owner] unassigned")
    decision = route_owner(default_routing(), "NVRM: Xid (PCI:0000:8a:00.0): 79")
    assert (decision.owner, decision.component) == ("SW", "GPU driver")


def test_bug_index_is_durable_and_counts_sightings(tmp_path: Path) -> None:
    index = BugIndex(tmp_path / "Tickets" / "bug-index.json")
    digest = fingerprint("PCIe link width changed on GPU3: x16 → x8")
    assert index.lookup(digest) is None
    assert index.sentence(digest) == "This is the first time this failure was seen."
    entry = index.record(digest, "T-validation-0002", seen_in="T-validation-0001")
    assert (entry.ticket_id, entry.seen, entry.first_seen_in) == (
        "T-validation-0002",
        1,
        "T-validation-0001",
    )
    again = BugIndex(tmp_path / "Tickets" / "bug-index.json")
    assert again.record(digest, "T-validation-0002", seen_in="T-validation-0003").seen == 2
    assert again.sentence(digest) == (
        "Seen 2 times; tracked as T-validation-0002 since T-validation-0001."
    )
    raw = json.loads((tmp_path / "Tickets" / "bug-index.json").read_text(encoding="utf-8"))
    assert raw[digest]["seen"] == 2
    assert (tmp_path / "Tickets" / "bug-index.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="fingerprint"):
        Finding(id="F-1", fingerprint="not-hex", issue="x")
