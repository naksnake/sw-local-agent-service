"""Write-ahead journal entries: intent → action → observation (INV-6)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from slas_schemas.common import SlasModel

JournalKind = Literal["intent", "action", "observation", "state", "note"]


class JournalEntry(SlasModel):
    seq: int = Field(ge=1)
    at: datetime
    ticket_id: str
    kind: JournalKind
    step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
