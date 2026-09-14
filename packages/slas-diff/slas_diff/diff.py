"""Baseline vs snapshot (CLAUDE.md §10.2 VERIFY): device counts, PCIe width AND speed,
firmware, AER/EDAC/MCE/Xid counters, new SEL entries. Deterministic; one sentence per finding.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from slas_hal.model import Snapshot
from slas_schemas.common import SlasModel
from slas_schemas.finding import Severity

DiffKind = Literal[
    "device_missing",
    "device_added",
    "pcie_width",
    "pcie_speed",
    "firmware",
    "counter",
    "sel",
    "power",
]


class DiffFinding(SlasModel):
    kind: DiffKind
    message: str = Field(min_length=1)
    severity: Severity
    bdf: str | None = None
    component: str | None = None
    before: str = ""
    after: str = ""

    def sentence(self) -> str:
        return self.message


class DiffReport(SlasModel):
    findings: list[DiffFinding] = Field(default_factory=list)
    context: str = ""

    @property
    def clean(self) -> bool:
        return not self.findings

    def sentence(self) -> str:
        if self.clean:
            return f"No change against the baseline{(' ' + self.context) if self.context else ''}."
        count = len(self.findings)
        return (
            f"{count} {'change' if count == 1 else 'changes'} against the baseline"
            + (f" {self.context}: " if self.context else ": ")
            + " ".join(f.message for f in self.findings)
        )


def diff_snapshots(baseline: Snapshot, current: Snapshot, *, context: str = "") -> DiffReport:
    """Everything that differs in a way an engineer cares about. `context` reads like
    "during DC cycle 14" and is copied into every sentence."""
    where = f" {context}" if context else ""
    findings: list[DiffFinding] = []
    before = {d.bdf: d for d in baseline.inventory.devices}
    after = {d.bdf: d for d in current.inventory.devices}

    for bdf in sorted(set(before) - set(after)):
        device = before[bdf]
        findings.append(
            DiffFinding(
                kind="device_missing",
                message=f"{device.label} disappeared{where}.",
                severity="S1",
                bdf=bdf,
                component=device.name,
                before=device.link(),
                after="absent",
            )
        )
    for bdf in sorted(set(after) - set(before)):
        device = after[bdf]
        findings.append(
            DiffFinding(
                kind="device_added",
                message=f"{device.label} appeared{where}; the baseline did not have it.",
                severity="S3",
                bdf=bdf,
                component=device.name,
                before="absent",
                after=device.link(),
            )
        )
    for bdf in sorted(set(before) & set(after)):
        old, new = before[bdf], after[bdf]
        if new.width != old.width:
            findings.append(
                DiffFinding(
                    kind="pcie_width",
                    message=(
                        f"PCIe link width changed on {old.label}: "
                        f"x{old.width} → x{new.width}{where}."
                    ),
                    severity="S2" if new.width < old.width else "S3",
                    bdf=bdf,
                    component=old.name,
                    before=f"x{old.width}",
                    after=f"x{new.width}",
                )
            )
        if new.speed_gts != old.speed_gts:
            findings.append(
                DiffFinding(
                    kind="pcie_speed",
                    message=(
                        f"PCIe link speed changed on {old.label}: {old.speed_gts:g} GT/s → "
                        f"{new.speed_gts:g} GT/s{where}."
                    ),
                    severity="S2" if new.speed_gts < old.speed_gts else "S3",
                    bdf=bdf,
                    component=old.name,
                    before=f"{old.speed_gts:g} GT/s",
                    after=f"{new.speed_gts:g} GT/s",
                )
            )

    # Firmware of what is present on both sides; a device that vanished is already S1 above.
    old_fw = baseline.inventory.firmware()
    new_fw = current.inventory.firmware()
    for name in sorted(set(old_fw) & set(new_fw)):
        if old_fw[name] != new_fw[name]:
            findings.append(
                DiffFinding(
                    kind="firmware",
                    message=(
                        f"Firmware of {name} changed: {old_fw.get(name, 'none')} → "
                        f"{new_fw.get(name, 'none')}{where}."
                    ),
                    severity="S2",
                    component=name,
                    before=old_fw.get(name, "none"),
                    after=new_fw.get(name, "none"),
                )
            )

    for name, old_value in baseline.counters.as_dict().items():
        new_value = current.counters.as_dict()[name]
        if new_value > old_value:
            findings.append(
                DiffFinding(
                    kind="counter",
                    message=f"{name} count rose from {old_value} to {new_value}{where}.",
                    severity="S1" if name in ("EDAC UE", "MCE", "Xid") else "S2",
                    component=name,
                    before=str(old_value),
                    after=str(new_value),
                )
            )

    known = {(e.id, e.created, e.message) for e in baseline.sel}
    new_entries = [e for e in current.sel if (e.id, e.created, e.message) not in known]
    for entry in new_entries:
        if entry.severity == "OK":
            continue  # a boot writes OK entries every cycle; only warnings and worse are findings
        findings.append(
            DiffFinding(
                kind="sel",
                message=f"New {entry.severity} SEL entry{where}: {entry.message}.",
                severity="S1" if entry.severity == "Critical" else "S3",
                component=entry.sensor or "SEL",
                before="",
                after=entry.message,
            )
        )

    if current.power != baseline.power:
        findings.append(
            DiffFinding(
                kind="power",
                message=(
                    f"Power state is {current.power}, the baseline was {baseline.power}{where}."
                ),
                severity="S1",
                component="power",
                before=baseline.power,
                after=current.power,
            )
        )
    return DiffReport(findings=findings, context=context)
