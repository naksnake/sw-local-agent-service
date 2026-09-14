"""Ticket identifiers: `T-<agent>-<seq>` (CLAUDE.md §5.4)."""

from __future__ import annotations

import re
from typing import Annotated, Final, cast

from pydantic import StringConstraints

from slas_schemas.common import AgentName

TICKET_ID_PATTERN: Final = r"^T-(coding|validation|factory|null)-\d{4,}$"
_TICKET_ID = re.compile(TICKET_ID_PATTERN)

TicketId = Annotated[str, StringConstraints(pattern=TICKET_ID_PATTERN)]


def make_ticket_id(agent: AgentName, seq: int) -> str:
    if seq < 1:
        raise ValueError("ticket sequence numbers start at 1")
    return f"T-{agent}-{seq:04d}"


def parse_ticket_id(ticket_id: str) -> tuple[AgentName, int]:
    match = _TICKET_ID.match(ticket_id)
    if match is None:
        raise ValueError(f"{ticket_id!r} is not a ticket id (expected T-<agent>-<number>)")
    agent, seq = ticket_id[2:].rsplit("-", 1)
    return cast(AgentName, agent), int(seq)
