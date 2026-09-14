"""The write-ahead journal: `intent → action → observation` per step (INV-6).

One append-only JSON-lines file per ticket. The kernel writes an `intent` entry before it
performs a step and an `observation` entry after; a step is complete only when its
observation is on disk. After a crash, `completed_steps()` and `interrupted_step()` tell the
kernel exactly where to resume.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from slas_kernel.clock import Clock
from slas_schemas.journal import JournalEntry, JournalKind


class Journal:
    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self._clock = clock

    def entries(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        entries: list[JournalEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(JournalEntry.model_validate_json(line))
        return entries

    def append(
        self,
        kind: JournalKind,
        ticket_id: str,
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
    ) -> JournalEntry:
        """Append one entry and flush it to disk before returning."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = JournalEntry(
            seq=len(self.entries()) + 1,
            at=self._clock.now(),
            ticket_id=ticket_id,
            kind=kind,
            step_id=step_id,
            payload=payload or {},
        )
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def completed_steps(self) -> dict[str, JournalEntry]:
        """Step id → its observation entry, for every step whose observation was journalled."""
        return {
            entry.step_id: entry
            for entry in self.entries()
            if entry.kind == "observation" and entry.step_id is not None
        }

    def interrupted_step(self) -> str | None:
        """The step whose intent was journalled without an observation, if any."""
        open_intent: str | None = None
        for entry in self.entries():
            if entry.step_id is None:
                continue
            if entry.kind == "intent":
                open_intent = entry.step_id
            elif entry.kind == "observation" and entry.step_id == open_intent:
                open_intent = None
        return open_intent

    def step_entries(self, step_id: str) -> list[JournalEntry]:
        return [entry for entry in self.entries() if entry.step_id == step_id]
