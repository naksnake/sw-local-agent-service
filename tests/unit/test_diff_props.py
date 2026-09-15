"""slas_diff: property-style tests with a seeded generator (Hypothesis is not an approved
dependency yet; the generator below covers the same ground deterministically).

    diff(x, x) is clean · every planted change yields exactly one finding of its kind ·
    width AND speed are separate findings · numbers and BDFs do not change the fingerprint
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from slas_diff.diff import DiffFinding, DiffReport, diff_snapshots
from slas_hal.fakes.bmc import FakeTarget
from slas_hal.model import PCIE_GEN_GTS, Counters, PcieDevice, SelEntry, Snapshot
from slas_triage.fingerprint import fingerprint

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
GPU3 = "0000:8a:00.0"
COUNTER_FIELDS = {
    "AER": "aer",
    "EDAC CE": "edac_ce",
    "EDAC UE": "edac_ue",
    "MCE": "mce",
    "Xid": "xid",
}
TRIALS = 150


def baseline() -> Snapshot:
    return FakeTarget("lab-gx8-01").snapshot(NOW)


def narrower(rng: random.Random, device: PcieDevice) -> PcieDevice:
    choices = [w for w in (1, 2, 4, 8, 16) if w < device.width]
    return device.model_copy(update={"width": rng.choice(choices)})


def slower(rng: random.Random, device: PcieDevice) -> PcieDevice:
    choices = [s for s in PCIE_GEN_GTS.values() if s < device.speed_gts]
    return device.model_copy(update={"speed_gts": rng.choice(choices)})


def mutate(rng: random.Random, base: Snapshot) -> tuple[Snapshot, dict[str, int]]:
    """A random set of independent changes and how many findings of each kind they imply."""
    devices = list(base.inventory.devices)
    bdfs = [d.bdf for d in devices]
    rng.shuffle(bdfs)
    n_width = rng.randint(0, 3)
    n_speed = rng.randint(0, 3)
    n_missing = rng.randint(0, 2)
    n_firmware = rng.randint(0, 2)
    # Disjoint device sets keep the expectation exact: one finding per (device, kind).
    picked = iter(bdfs)
    width_set = {next(picked) for _ in range(n_width)}
    speed_set = {next(picked) for _ in range(n_speed)}
    missing_set = {next(picked) for _ in range(n_missing)}
    firmware_set = {next(picked) for _ in range(n_firmware)}
    # A device may lose width AND speed at once: both must be reported.
    both = rng.random() < 0.3
    if both and width_set:
        speed_set.add(next(iter(width_set)))

    changed: list[PcieDevice] = []
    for device in devices:
        if device.bdf in missing_set:
            continue
        if device.bdf in width_set:
            device = narrower(rng, device)
        if device.bdf in speed_set:
            device = slower(rng, device)
        if device.bdf in firmware_set:
            device = device.model_copy(
                update={"firmware": f"{device.firmware}-b{rng.randint(1, 9)}"}
            )
        changed.append(device)

    counters = base.counters.model_dump()
    bumped = [name for name in COUNTER_FIELDS if rng.random() < 0.3]
    for name in bumped:
        counters[COUNTER_FIELDS[name]] += rng.randint(1, 5)

    sel = list(base.sel)
    new_bad = 0
    for _ in range(rng.randint(0, 3)):
        severity = rng.choice(["OK", "Warning", "Critical"])
        sel.append(
            SelEntry(
                id=len(sel) + 1,
                created=f"2026-09-14T08:{len(sel):02d}:00Z",
                severity=severity,
                message=f"Planted event {len(sel)}",
            )
        )
        new_bad += severity != "OK"

    power_changed = rng.random() < 0.1
    mutated = base.model_copy(
        update={
            "inventory": base.inventory.model_copy(update={"devices": changed}),
            "counters": Counters(**counters),
            "sel": sel,
            "power": "off" if power_changed else base.power,
        }
    )
    expected = {
        "device_missing": len(missing_set),
        "device_added": 0,
        "pcie_width": len(width_set),
        "pcie_speed": len(speed_set),
        "firmware": len(firmware_set),
        "counter": len(bumped),
        "sel": new_bad,
        "power": int(power_changed),
    }
    return mutated, expected


def by_kind(report: DiffReport) -> dict[str, int]:
    counts = dict.fromkeys(
        (
            "device_missing",
            "device_added",
            "pcie_width",
            "pcie_speed",
            "firmware",
            "counter",
            "sel",
            "power",
        ),
        0,
    )
    for finding in report.findings:
        counts[finding.kind] += 1
    return counts


def test_a_snapshot_against_itself_is_always_clean() -> None:
    rng = random.Random(20260914)
    base = baseline()
    assert diff_snapshots(base, base).clean
    for _ in range(TRIALS):
        mutated, _ = mutate(rng, base)
        report = diff_snapshots(mutated, mutated, context="during DC cycle 1")
        assert report.clean
        assert report.sentence() == "No change against the baseline during DC cycle 1."


def test_every_planted_change_yields_exactly_one_finding_of_its_kind() -> None:
    rng = random.Random(1414)
    base = baseline()
    seen_double = False
    for _ in range(TRIALS):
        mutated, expected = mutate(rng, base)
        report = diff_snapshots(base, mutated, context="during DC cycle 14")
        assert by_kind(report) == expected, report.sentence()
        for finding in report.findings:
            assert (
                finding.message.endswith(" during DC cycle 14.")
                or ": Planted event" in finding.message
            )
            assert finding.severity in ("S1", "S2", "S3")
        width_bdfs = {f.bdf for f in report.findings if f.kind == "pcie_width"}
        speed_bdfs = {f.bdf for f in report.findings if f.kind == "pcie_speed"}
        if width_bdfs & speed_bdfs:
            seen_double = True
        assert all(
            f.severity == "S2" for f in report.findings if f.kind in ("pcie_width", "pcie_speed")
        )
        assert all(f.severity == "S1" for f in report.findings if f.kind == "device_missing")
    assert seen_double, "the generator must exercise width AND speed on one device"


def test_the_reverse_diff_swaps_missing_for_added_and_marks_recoveries_lower() -> None:
    rng = random.Random(7)
    base = baseline()
    for _ in range(TRIALS):
        mutated, expected = mutate(rng, base)
        reverse = by_kind(diff_snapshots(mutated, base))
        assert reverse["device_added"] == expected["device_missing"]
        assert reverse["device_missing"] == 0
        assert reverse["pcie_width"] == expected["pcie_width"]
        assert reverse["pcie_speed"] == expected["pcie_speed"]
        assert reverse["counter"] == 0, "counters only ever grow; a lower count is not a finding"
        assert reverse["sel"] == 0, "entries the baseline already had are not new"
        report = diff_snapshots(mutated, base)
        assert all(
            f.severity == "S3" for f in report.findings if f.kind in ("pcie_width", "pcie_speed")
        ), "a link that comes back wider or faster is worth a look, not an alarm"


def test_the_planted_gpu3_degradation_reads_as_one_sentence_per_dimension() -> None:
    base = baseline()
    gpu3 = base.inventory.device(GPU3)
    assert gpu3 is not None
    degraded = gpu3.model_copy(update={"width": 8, "speed_gts": 16.0})
    devices = [degraded if d.bdf == GPU3 else d for d in base.inventory.devices]
    current = base.model_copy(
        update={"inventory": base.inventory.model_copy(update={"devices": devices})}
    )
    report = diff_snapshots(base, current, context="during DC cycle 14")
    assert [f.message for f in report.findings] == [
        "PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14.",
        "PCIe link speed changed on NVIDIA H100 SXM (0000:8a:00.0): 32 GT/s → 16 GT/s "
        "during DC cycle 14.",
    ]
    width = report.findings[0]
    assert (width.bdf, width.component, width.before, width.after) == (
        GPU3,
        "NVIDIA H100 SXM",
        "x16",
        "x8",
    )
    assert report.sentence().startswith(
        "2 changes against the baseline during DC cycle 14: PCIe link width"
    )
    # Cycle 15 and another slot share the fingerprint: dedup is by shape, not by number.
    later = report.findings[0].message.replace("cycle 14", "cycle 15")
    other_slot = later.replace("0000:8a:00.0", "0000:1a:00.0").replace("x8", "x4")
    assert fingerprint(width.message) == fingerprint(later) == fingerprint(other_slot)
    assert fingerprint(width.message) != fingerprint(report.findings[1].message)


def test_firmware_counters_sel_and_power_sentences() -> None:
    base = baseline()
    devices = [
        d.model_copy(update={"firmware": "97.00.01"}) if d.bdf == GPU3 else d
        for d in base.inventory.devices
    ]
    current = base.model_copy(
        update={
            "inventory": base.inventory.model_copy(
                update={"devices": devices, "bios_version": "2.5.0"}
            ),
            "counters": Counters(aer=2, edac_ue=1),
            "sel": [
                *base.sel,
                SelEntry(
                    id=4,
                    created="2026-09-14T08:05:00Z",
                    severity="Critical",
                    message="PCIe bus fatal error",
                    sensor="PCI",
                ),
                SelEntry(
                    id=5,
                    created="2026-09-14T08:05:01Z",
                    severity="OK",
                    message="System boot initiated",
                ),
            ],
            "power": "off",
        }
    )
    report = diff_snapshots(base, current)
    assert [f.message for f in report.findings] == [
        "Firmware of BIOS changed: 2.4.1 → 2.5.0.",
        "Firmware of NVIDIA H100 SXM (0000:8a:00.0) changed: 96.00.5E → 97.00.01.",
        "AER count rose from 0 to 2.",
        "EDAC UE count rose from 0 to 1.",
        "New Critical SEL entry: PCIe bus fatal error.",
        "Power state is off, the baseline was on.",
    ]
    assert [f.severity for f in report.findings] == ["S2", "S2", "S2", "S1", "S1", "S1"]
    sel = report.findings[4]
    assert (sel.component, sel.after) == ("PCI", "PCIe bus fatal error")
    assert report.sentence().startswith("6 changes against the baseline: Firmware of BIOS changed")
    assert DiffFinding(kind="power", message="x", severity="S1").sentence() == "x"


def test_a_missing_device_is_reported_once_not_also_as_lost_firmware() -> None:
    base = baseline()
    devices = [d for d in base.inventory.devices if d.bdf != GPU3]
    current = base.model_copy(
        update={"inventory": base.inventory.model_copy(update={"devices": devices})}
    )
    report = diff_snapshots(base, current, context="during DC cycle 3")
    assert [f.kind for f in report.findings] == ["device_missing"]
    assert (
        report.findings[0].message
        == "NVIDIA H100 SXM (0000:8a:00.0) disappeared during DC cycle 3."
    )
    assert report.findings[0].before == "x16 @ 32 GT/s" and report.findings[0].after == "absent"
    assert report.sentence() == (
        "1 change against the baseline during DC cycle 3: "
        "NVIDIA H100 SXM (0000:8a:00.0) disappeared during DC cycle 3."
    )
