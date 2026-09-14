"""Where tickets live: in memory for tests, on disk under `Tickets/<id>/` (CLAUDE.md §4.4).

The file store is the Phase 2 truth for a single orchestrator process. The Ticket Service in
`apps/api` (Phase 1/2, Postgres) implements the same protocol; the kernel does not care which.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from slas_schemas.common import AgentName
from slas_schemas.envfile import write_atomic
from slas_schemas.ids import make_ticket_id
from slas_schemas.ticket import Ticket


class TicketNotFoundError(KeyError):
    def __init__(self, ticket_id: str) -> None:
        super().__init__(ticket_id)
        self.ticket_id = ticket_id

    def __str__(self) -> str:
        return f"There is no ticket {self.ticket_id}."


class TicketStore(Protocol):
    def next_ticket_id(self, agent: AgentName) -> str: ...

    def save(self, ticket: Ticket) -> None: ...

    def load(self, ticket_id: str) -> Ticket: ...

    def list_ids(self) -> list[str]: ...


class MemoryTicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._sequence: dict[str, int] = {}

    def next_ticket_id(self, agent: AgentName) -> str:
        self._sequence[agent] = self._sequence.get(agent, 0) + 1
        return make_ticket_id(agent, self._sequence[agent])

    def save(self, ticket: Ticket) -> None:
        self._tickets[ticket.id] = ticket.model_copy(deep=True)

    def load(self, ticket_id: str) -> Ticket:
        try:
            return self._tickets[ticket_id].model_copy(deep=True)
        except KeyError:
            raise TicketNotFoundError(ticket_id) from None

    def list_ids(self) -> list[str]:
        return sorted(self._tickets)


class FileTicketStore:
    """`${SLAS_DATA_ROOT}/Tickets/<ticket-id>/ticket.json`, written atomically."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.tickets_dir = data_root / "Tickets"
        self._sequence_path = self.tickets_dir / ".sequence.json"

    def ticket_dir(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.ticket_dir(ticket_id) / "ticket.json"

    def next_ticket_id(self, agent: AgentName) -> str:
        # Single-writer by design in Phase 2 (one orchestrator process); the Postgres store
        # takes over allocation when several writers exist.
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        sequence: dict[str, int] = {}
        if self._sequence_path.exists():
            sequence = json.loads(self._sequence_path.read_text(encoding="utf-8"))
        sequence[agent] = sequence.get(agent, 0) + 1
        write_atomic(self._sequence_path, json.dumps(sequence, indent=2) + "\n", mode=0o600)
        return make_ticket_id(agent, sequence[agent])

    def save(self, ticket: Ticket) -> None:
        self.ticket_dir(ticket.id).mkdir(parents=True, exist_ok=True)
        write_atomic(
            self._ticket_path(ticket.id),
            ticket.model_dump_json(indent=2) + "\n",
            mode=0o600,
        )

    def load(self, ticket_id: str) -> Ticket:
        path = self._ticket_path(ticket_id)
        if not path.exists():
            raise TicketNotFoundError(ticket_id)
        return Ticket.model_validate_json(path.read_text(encoding="utf-8"))

    def list_ids(self) -> list[str]:
        if not self.tickets_dir.exists():
            return []
        return sorted(
            child.name
            for child in self.tickets_dir.iterdir()
            if child.is_dir() and (child / "ticket.json").exists()
        )
