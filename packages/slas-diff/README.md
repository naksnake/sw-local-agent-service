# slas-diff

Baseline vs snapshot diff for VERIFY (CLAUDE.md §10.2): `diff_snapshots(baseline, current,
context="during DC cycle 14")` returns one `DiffFinding` sentence per change — a device that
disappeared or appeared, PCIe link **width** and **speed** as separate findings, firmware of
what is present on both sides, AER/EDAC/MCE/Xid counters that grew, new non-OK SEL entries
and a power state that differs. Deterministic; no model. Tested property-style with a seeded
generator (`tests/unit/test_diff_props.py`) until Hypothesis is an approved dependency.
