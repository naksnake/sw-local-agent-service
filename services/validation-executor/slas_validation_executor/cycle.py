"""One power cycle as a state machine (CLAUDE.md §10.2 ACT), zero LLM.

    ARM      journal the intent: cycle n, kind, settle
    QUIESCE  sync the OS, snapshot the SEL, write the fence marker into console and syslog
    ACT      the power action through slas_hal
    SETTLE   wait for the OS over SOL/SSH, up to the boot timeout, never less than the floor
    VERIFY   snapshot and diff against the baseline: counts, PCIe width AND speed, firmware,
             AER/EDAC/MCE/Xid, new SEL

Every phase is written ahead to the cycle's own journal. A restart reads it: a cycle whose
ACT was journalled is resumed at SETTLE, never powered again (INV-6).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from slas_diff.diff import DiffReport, diff_snapshots
from slas_hal.hal import Hal
from slas_hal.model import PowerAction, Snapshot
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_validation_executor.guardrails import Guardrails

Phase = Literal["arm", "quiesce", "act", "settle", "verify"]
PHASES: tuple[Phase, ...] = ("arm", "quiesce", "act", "settle", "verify")


class Clock(Protocol):
    def now(self) -> datetime: ...


class PhaseEntry(SlasModel):
    phase: Phase
    at: datetime
    detail: dict[str, object] = Field(default_factory=dict)


class CycleJournal:
    """`Runs/<ticket>/cycles/<n>/journal.jsonl`: one line per phase, written before the phase."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[PhaseEntry]:
        if not self.path.is_file():
            return []
        return [
            PhaseEntry.model_validate(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def done(self) -> set[str]:
        return {entry.phase for entry in self.entries()}

    def append(self, phase: Phase, at: datetime, **detail: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(PhaseEntry(phase=phase, at=at, detail=detail).model_dump_json() + "\n")


class CycleResult(SlasModel):
    n: int = Field(ge=1)
    kind: str
    booted: bool
    resumed: bool = False
    settle_s: int
    diff: DiffReport | None = None
    fence: str
    sentence: str

    @property
    def clean(self) -> bool:
        return self.booted and (self.diff is None or self.diff.clean)


def power_action_for(kind: str) -> PowerAction:
    return {"warm": "graceful_restart", "dc": "off", "ac": "ac_cycle"}[kind]  # type: ignore[return-value]


def run_cycle(
    *,
    hal: Hal,
    target: str,
    ticket_id: str,
    n: int,
    kind: str,
    settle_s: int,
    guardrails: Guardrails,
    baseline: Snapshot,
    clock: Clock,
    run_dir: Path,
) -> CycleResult:
    cycle_dir = run_dir / "cycles" / f"{n:03d}"
    journal = CycleJournal(cycle_dir / "journal.jsonl")
    done = journal.done()
    resumed = bool(done)
    settle = max(settle_s, guardrails.settle_floor(kind))
    fence = f"--- slas fence {ticket_id} cycle {n} {kind} ---"

    if "arm" not in done:
        journal.append("arm", clock.now(), kind=kind, settle_s=settle, target=target)
    if "quiesce" not in done:
        hal.ssh(target, ["sync"])
        before = hal.sel(target)
        write_atomic(
            cycle_dir / "sel-before.json",
            json.dumps([e.model_dump(mode="json") for e in before], indent=2) + "\n",
            mode=0o644,
        )
        hal.fence(target, fence)
        journal.append("quiesce", clock.now(), sel_entries=len(before), fence=fence)
    if "act" not in done:
        action = power_action_for(kind)
        journal.append("act", clock.now(), action=action)  # written ahead: intent before effect
        hal.power(target, action)
        if action == "off":
            hal.power(target, "on")
    # SETTLE and VERIFY are safe to repeat: they only observe.
    booted = hal.wait_for_os(target, timeout_s=max(guardrails.boot_timeout_s, settle))
    journal.append("settle", clock.now(), booted=booted, settle_s=settle)
    diff: DiffReport | None = None
    if booted:
        snapshot = hal.snapshot(target)
        write_atomic(
            cycle_dir / "snapshot.json", snapshot.model_dump_json(indent=2) + "\n", mode=0o644
        )
        diff = diff_snapshots(baseline, snapshot, context=f"during {kind.upper()} cycle {n}")
        journal.append("verify", clock.now(), findings=len(diff.findings))
    else:
        journal.append("verify", clock.now(), skipped="the target did not boot")
    write_atomic(
        cycle_dir / "console.log",
        "\n".join(hal.console_lines(target)) + "\n",
        mode=0o644,
    )
    if not booted:
        sentence = (
            f"Cycle {n} ({kind.upper()}): the target did not come back within "
            f"{max(guardrails.boot_timeout_s, settle)} s."
        )
    elif diff is not None and not diff.clean:
        sentence = f"Cycle {n} ({kind.upper()}): booted; {diff.sentence()}"
    else:
        sentence = f"Cycle {n} ({kind.upper()}): booted; no change against the baseline."
    if resumed:
        sentence += " Resumed after an interruption without repeating the power action."
    return CycleResult(
        n=n,
        kind=kind,
        booted=booted,
        resumed=resumed,
        settle_s=settle,
        diff=diff,
        fence=fence,
        sentence=sentence,
    )
