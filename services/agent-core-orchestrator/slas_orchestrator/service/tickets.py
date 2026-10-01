"""`/v1/tickets`: the Home lists and one ticket in full (contract §5)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi import Path as PathParam

from slas_http.identity import identity_of
from slas_orchestrator.service.deps import deps_of, load_ticket, may_see
from slas_orchestrator.service.views import ticket_row

router = APIRouter(prefix="/v1/tickets")


@router.get("")
def list_tickets(request: Request) -> list[dict[str, Any]]:
    identity = identity_of(request)
    deps = deps_of(request)
    return [
        ticket_row(ticket, deps.registry.state_of(ticket.id))
        for ticket in deps.tickets()
        if may_see(identity, ticket)
    ]


@router.get("/{id}")
def get_ticket(request: Request, ticket_id: str = PathParam(alias="id")) -> dict[str, Any]:
    identity = identity_of(request)
    ticket = load_ticket(deps_of(request), identity, ticket_id)
    return ticket.model_dump(mode="json")
