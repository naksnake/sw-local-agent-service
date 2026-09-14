"""What arrives at INGEST and the normalised Job the kernel works on (CLAUDE.md §5.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from slas_schemas.common import AgentName, SlasModel


class Upload(SlasModel):
    """A file a person dropped into a wizard: plan.md, suite.xlsx, a label scan."""

    filename: str = Field(min_length=1)
    uploaded_by: str = Field(min_length=1)
    content_type: str = "application/octet-stream"
    path: str | None = None
    content: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class MesTicket(SlasModel):
    """A production ticket from the manufacturing execution system (CLAUDE.md §10.3)."""

    ticket_no: str = Field(min_length=1)
    station: str = Field(min_length=1)
    unit_sn: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    payload: dict[str, str] = Field(default_factory=dict)


class InputRef(SlasModel):
    name: str = Field(min_length=1)
    kind: Literal["upload", "mes_ticket", "inline"]
    path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


class TargetRef(SlasModel):
    """An opaque reference to the machine the agent works on; never a credential (INV-5)."""

    kind: Literal["sandbox", "server", "station"]
    ref: str = Field(min_length=1)


class Job(SlasModel):
    id: str = Field(min_length=1)
    agent: AgentName
    user: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    inputs: list[InputRef] = Field(default_factory=list)
    target: TargetRef | None = None
    created_at: datetime
