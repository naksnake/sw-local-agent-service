# slas-triage

Fingerprint, dedup and owner routing for findings (CLAUDE.md §5.4, §10.2); code, never a
model, decides what is the same failure.

| Module | What it does |
|---|---|
| `fingerprint.py` | Masks numbers, hex and PCI addresses before hashing, so the same degradation at cycle 14 and cycle 25, or on another slot, share one fingerprint. |
| `routing.py` | The deterministic owner / component / severity table rendered to `config/owner-routing.yaml` (moved here from `slas_kernel.rca`, which re-exports it). |
| `dedup.py` | `dedup_findings` (first sighting wins, evidence merged up to 20), `triage` (dedup + routing where a finding has no owner), `finding_from_sentence`, and `BugIndex`: fingerprint → bug ticket in `Tickets/bug-index.json`, so a failure seen run after run stays one ticket. |
