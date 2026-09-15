"""The MES adapter (CLAUDE.md §10.3 TRIGGER, §15 open decision 8): one interface, a file-drop
implementation for quickstart, a fake for tests. REST or database adapters implement the
same two calls once the integration is decided.

    Factory/MES/inbox/<any>.json      a production ticket dropped by the MES
    Factory/MES/processing/           picked up, being worked
    Factory/MES/done/                 reported back
    Factory/MES/rejected/             not a usable ticket; a .reason.txt sits beside it
    Factory/MES/outbox/<ticket_no>.json   the verdict, the ticket id and the SOP paths
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, ValidationError

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import MesTicket

Verdict = Literal["PASS", "FAIL", "line_lead"]


class MesVerdict(SlasModel):
    ticket_no: str = Field(min_length=1)
    unit_sn: str = Field(min_length=1)
    station: str = Field(min_length=1)
    verdict: Verdict
    ticket_id: str = Field(min_length=1)
    decided_by: str = Field(min_length=1)
    sentence: str = Field(min_length=1)
    sop_en: str | None = None
    sop_zh: str | None = None
    reported_at: datetime


class MesAdapter(Protocol):
    def poll(self) -> list[MesTicket]: ...

    def report(self, verdict: MesVerdict) -> None: ...


class FakeMesAdapter:
    def __init__(self, tickets: list[MesTicket] | None = None) -> None:
        self.queue = list(tickets or [])
        self.reported: list[MesVerdict] = []

    def poll(self) -> list[MesTicket]:
        tickets, self.queue = self.queue, []
        return tickets

    def report(self, verdict: MesVerdict) -> None:
        self.reported.append(verdict)


class FileDropMesAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root
        for name in ("inbox", "processing", "done", "rejected", "outbox"):
            (root / name).mkdir(parents=True, exist_ok=True)

    def poll(self) -> list[MesTicket]:
        tickets: list[MesTicket] = []
        for path in sorted((self.root / "inbox").glob("*.json")):
            try:
                ticket = MesTicket.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, ValueError) as exc:
                reason = ThreePartMessage(
                    f"{path.name} is not a usable production ticket.",
                    validation_sentence(exc) if isinstance(exc, ValidationError) else str(exc),
                    "The MES must send ticket_no, station, unit_sn and requested_by as JSON.",
                )
                shutil.move(str(path), self.root / "rejected" / path.name)
                write_atomic(
                    self.root / "rejected" / f"{path.name}.reason.txt",
                    reason.render() + "\n",
                    mode=0o644,
                )
                continue
            shutil.move(str(path), self.root / "processing" / f"{ticket.ticket_no}.json")
            tickets.append(ticket)
        return tickets

    def report(self, verdict: MesVerdict) -> None:
        write_atomic(
            self.root / "outbox" / f"{verdict.ticket_no}.json",
            verdict.model_dump_json(indent=2) + "\n",
            mode=0o644,
        )
        processing = self.root / "processing" / f"{verdict.ticket_no}.json"
        if processing.is_file():
            shutil.move(str(processing), self.root / "done" / processing.name)

    def pending(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "processing").glob("*.json"))

    def rejected(self) -> list[str]:
        return sorted(p.name for p in (self.root / "rejected").glob("*.json"))


def read_outbox(root: Path, ticket_no: str) -> MesVerdict:
    return MesVerdict.model_validate_json(
        (root / "outbox" / f"{ticket_no}.json").read_text(encoding="utf-8")
    )


def dump_verdict(verdict: MesVerdict) -> str:
    return json.dumps(verdict.model_dump(mode="json"), indent=2, ensure_ascii=False)
