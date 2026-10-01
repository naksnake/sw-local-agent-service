"""Forwarding a browser request to one service of the stack (docs/api-contract-round-2.md §8).

The api owns the session; the other services own the work. For a signed-in person past the
one-time password the api builds an `Identity` from the principal, forwards method, path,
query and JSON body with `ServiceClient.request`, and hands the answer back unchanged. A
downstream three-part error keeps its status and its sentences (no re-wording); a service
that does not answer becomes the 503 `ServiceUnreachableError` already carries, which names
the service and the `slas logs <service>` command. Nothing here touches hardware or a model
(CLAUDE.md §11: apps/api does authz, tickets and approvals only).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from slas_api.errors import LOGS_API, ApiError
from slas_authz import DESCRIPTIONS, Capability, Principal
from slas_http import Identity, ServiceClient, ServiceError
from slas_schemas.errors import ThreePartMessage

BODY_NOT_JSON = ThreePartMessage(
    "The request body is not JSON.",
    "The page sent something the api could not read.",
    f"Reload the page and try again; if it repeats, {LOGS_API}.",
)


def identity_for(principal: Principal) -> Identity:
    """The identity headers a downstream service reads: email, display name, capabilities."""
    return Identity(
        principal.subject,
        principal.display_name,
        frozenset(capability.value for capability in principal.capabilities),
    )


async def json_body(request: Request) -> Any:
    """FastAPI dependency: the request's JSON as-is, or None when there is no body."""
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, BODY_NOT_JSON) from exc


def query_of(request: Request) -> dict[str, str] | None:
    params = dict(request.query_params)
    return params or None


def forward(
    client: ServiceClient,
    method: str,
    path: str,
    *,
    principal: Principal,
    body: Any = None,
    params: Mapping[str, str] | None = None,
) -> Any:
    """One downstream call as the acting person. Returns the decoded JSON (None on 204)."""
    try:
        return client.request(
            method, path, body=body, params=params, identity=identity_for(principal)
        )
    except ServiceError as exc:
        # Same status, same three sentences: the downstream already spoke to the person.
        # `ServiceUnreachableError` is a `ServiceError` whose 503 names the service.
        raise ApiError(exc.status, exc.message) from exc


def as_response(payload: Any) -> Response:
    """The downstream JSON with a 200, or an empty 204 when the downstream sent no body."""
    if payload is None:
        return Response(status_code=204)
    return JSONResponse(payload)


def require_any(principal: Principal, capabilities: tuple[Capability, ...]) -> None:
    """A 403 in three parts unless the person holds at least one of the capabilities."""
    if not capabilities or any(principal.can(capability) for capability in capabilities):
        return
    verbs = " or ".join(DESCRIPTIONS[capability] for capability in capabilities)
    names = ", ".join(capability.value for capability in capabilities)
    raise ApiError(
        403,
        ThreePartMessage(
            f"{principal.display_name} may not {verbs}.",
            f"The {principal.role_label} role includes none of {names}.",
            "Ask an administrator to give you a role that includes one of them.",
        ),
    )
