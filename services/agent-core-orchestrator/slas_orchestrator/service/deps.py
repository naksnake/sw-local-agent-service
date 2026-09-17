"""What the routes share: the collaborators `create_app()` assembled, and small helpers.

Every collaborator is injectable (contract §1). The routes reach them through
`deps_of(request)`; nothing is a module global, so two apps in one test process never share
state.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from fastapi import Request

from slas_http.client import ServiceClient
from slas_http.errors import ServiceError
from slas_http.identity import Identity
from slas_kernel.clock import Clock
from slas_kernel.journal import Journal
from slas_kernel.kernel import Kernel
from slas_kernel.store import TicketNotFoundError, TicketStore
from slas_observability.events import EventLog
from slas_orchestrator.clients import HttpSandboxManager
from slas_orchestrator.coding.agent import CodingAgent
from slas_orchestrator.coding.coder import GatewayLike
from slas_orchestrator.coding.executor import CodingExecutor
from slas_orchestrator.service.runs import RunRegistry
from slas_orchestrator.service.settings import Settings
from slas_schemas.errors import ThreePartMessage
from slas_schemas.ticket import Ticket
from slas_skills.state import SkillStateStore
from slas_sop.glossary import Glossary
from slas_triage.routing import OwnerRouting

#: Who may see everybody's tickets and tasks (contract §5: "unless admin:people").
SEE_ALL_CAPABILITY: Final = "admin:people"

#: A health probe: (check name, base URL) → "ok" or a short word saying what is wrong.
Probe = Callable[[str, str], str]

_UNSAFE = re.compile(r"[^a-z0-9._-]+")


class KernelFactory(Protocol):
    def __call__(self, deps: Deps) -> Kernel: ...


@dataclass
class Deps:
    settings: Settings
    log: EventLog
    clock: Clock
    #: The read side for the routes; kernels write through `registry.store`.
    store: TicketStore
    registry: RunRegistry
    sandbox: HttpSandboxManager
    gateway: GatewayLike | None
    broker: ServiceClient | None
    coding_agent: CodingAgent
    coding_executor: CodingExecutor
    kernel_factory: KernelFactory
    skill_state: SkillStateStore
    glossary: Glossary | None
    owner_routing: OwnerRouting | None
    probe: Probe

    @property
    def data_root(self) -> Path:
        return self.settings.data_root

    def journal_for(self, ticket_id: str) -> Journal:
        return Journal(self.data_root / "Tickets" / ticket_id / "journal.jsonl", self.clock)

    def tickets(self) -> list[Ticket]:
        """Every ticket on disk, newest first."""
        tickets: list[Ticket] = []
        for ticket_id in self.store.list_ids():
            try:
                tickets.append(self.store.load(ticket_id))
            except (KeyError, ValueError):  # a half-written or foreign file is not a ticket
                self.log.warning("ticket.unreadable", ticket_id=ticket_id)
        tickets.sort(key=lambda t: (t.created_at, t.id), reverse=True)
        return tickets


def deps_of(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


def workspace_user(identity: Identity) -> str:
    """The person's name on disk and on tickets: `Coding/<user>/`, `<user>@slas.local`.

    The api forwards the email; the local part is the workspace user (CLAUDE.md §4.4,
    §5.7), lowercased and reduced to `[a-z0-9._-]`.
    """
    local = identity.user.split("@", 1)[0].strip().lower()
    safe = _UNSAFE.sub("-", local).strip("-.")
    return safe or "user"


def owns(identity: Identity, ticket: Ticket) -> bool:
    return ticket.user in (identity.user, workspace_user(identity))


def may_see(identity: Identity, ticket: Ticket) -> bool:
    return identity.has(SEE_ALL_CAPABILITY) or owns(identity, ticket)


def not_found(ticket_id: str) -> ServiceError:
    return ServiceError(
        404,
        ThreePartMessage(
            f"There is no ticket {ticket_id}.",
            "It was never created here, or the link is stale.",
            "Go to Home and open the ticket from the list.",
        ),
    )


def load_ticket(deps: Deps, identity: Identity, ticket_id: str) -> Ticket:
    """The ticket, or a three-part 404 — also when it belongs to somebody else."""
    try:
        ticket = deps.store.load(ticket_id)
    except (TicketNotFoundError, KeyError):
        raise not_found(ticket_id) from None
    if not may_see(identity, ticket):
        raise not_found(ticket_id)
    return ticket


def problem(status: int, exc: Any, *, fallback: ThreePartMessage) -> ServiceError:
    """A domain error carrying a `ThreePartMessage` becomes the answer; anything else falls back."""
    message = getattr(exc, "message", None)
    if isinstance(message, ThreePartMessage):
        return ServiceError(status, message)
    return ServiceError(status, fallback)
