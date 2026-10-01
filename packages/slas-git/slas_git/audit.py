"""One audit row per remote operation (CLAUDE.md §5.7 audit): user, remote, op, branch,
commit, result, duration. Every string is redacted before it is written."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from slas_git.redact import redact
from slas_schemas.common import SlasModel

Result = Literal["ok", "refused", "failed"]


class AuditRow(SlasModel):
    at: datetime
    user: str = Field(min_length=1)
    remote_id: str | None = None
    remote_name: str | None = None
    op: str = Field(min_length=1)
    branch: str | None = None
    sha: str | None = None
    result: Result
    duration_s: float = Field(ge=0)
    detail: str = ""
    #: The request's trace id when the operation ran inside a service request (ADR-0015).
    trace_id: str | None = None

    def sentence(self) -> str:
        where = f" to {self.remote_name}" if self.remote_name else ""
        what = (
            f" ({self.branch}" + (f" @ {self.sha[:10]}" if self.sha else "") + ")"
            if self.branch
            else ""
        )
        verdict = {"ok": "finished", "refused": "was refused", "failed": "failed"}[self.result]
        tail = f": {self.detail}" if self.detail else "."
        return f"{self.user}: {self.op}{where}{what} {verdict} in {self.duration_s:.1f} s{tail}"


class AuditLog:
    """JSON lines, one per operation, appended; redaction happens here, at the boundary."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[AuditRow] = []

    def record(self, row: AuditRow) -> AuditRow:
        clean = row.model_copy(update={"detail": redact(row.detail)})
        self.rows.append(clean)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return clean

    def read_all(self) -> list[AuditRow]:
        if not self.path.is_file():
            return []
        return [
            AuditRow.model_validate(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
