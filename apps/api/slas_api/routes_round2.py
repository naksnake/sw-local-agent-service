"""The browser-facing routes of docs/api-contract-round-2.md §8: pure proxying with authz.

Every route below is one row of `ROUTES`: the browser's method and path, the service it
forwards to, the path there, and the capability the person must hold *before* the api
forwards (the downstream checks again where the action executes). The table is explicit so
`route_table()` and the contract test stay meaningful; nothing is a catch-all. Every route
sits behind `Ready` (a valid session past the one-time password) and, for POST, PUT and
DELETE, behind the `X-Requested-With` middleware like the round-1 routes.

One route is not a plain forward: `POST /git/projects/{slug}/terminal` first asks the
sandbox manager which session the person holds for the slug, then sends the line to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response

from slas_api import proxy
from slas_api.authz import require
from slas_api.errors import ApiError
from slas_api.routes import Auth, Ready, ServicesDep
from slas_api.service import Downstream
from slas_authz import Capability
from slas_schemas.errors import ThreePartMessage

Service = Literal[
    "orchestrator", "git_broker", "sandbox_manager", "factory_executor", "model_manager"
]
Method = Literal["GET", "POST", "PUT", "DELETE"]

round2 = APIRouter(prefix="/api/v1")

JsonBody = Annotated[Any, Depends(proxy.json_body)]


@dataclass(frozen=True, slots=True)
class ProxyRoute:
    method: Method
    path: str  # under /api/v1, with {params} named as in `downstream_path`
    service: Service
    downstream_path: str
    #: Capabilities of which the person needs at least one; empty means signed in is enough.
    requires: tuple[Capability, ...] = ()

    @property
    def browser_path(self) -> str:
        return f"/api/v1{self.path}"


C = Capability

#: Contract §8 read against §3, §5, §6 and §7: the whole browser table except the terminal.
ROUTES: Final[tuple[ProxyRoute, ...]] = (
    # --- Coding (orchestrator §5) ---------------------------------------------------------
    ProxyRoute("POST", "/coding/languages/detect", "orchestrator", "/v1/coding/languages/detect"),
    ProxyRoute("POST", "/coding/propose", "orchestrator", "/v1/coding/propose"),
    ProxyRoute(
        "POST", "/coding/toolchains/resolve", "orchestrator", "/v1/coding/toolchains/resolve"
    ),
    ProxyRoute("GET", "/coding/remotes", "orchestrator", "/v1/coding/remotes"),
    ProxyRoute("GET", "/coding/skills", "orchestrator", "/v1/coding/skills"),
    ProxyRoute("POST", "/coding/tasks", "orchestrator", "/v1/coding/tasks"),
    ProxyRoute("GET", "/coding/tasks", "orchestrator", "/v1/coding/tasks"),
    ProxyRoute("GET", "/coding/tasks/{ticket_id}", "orchestrator", "/v1/coding/tasks/{ticket_id}"),
    # --- Validation (orchestrator §5) -----------------------------------------------------
    ProxyRoute("POST", "/validation/suites/parse", "orchestrator", "/v1/validation/suites/parse"),
    ProxyRoute("GET", "/validation/targets", "orchestrator", "/v1/validation/targets"),
    ProxyRoute("POST", "/validation/preview", "orchestrator", "/v1/validation/preview"),
    ProxyRoute("POST", "/validation/runs", "orchestrator", "/v1/validation/runs"),
    ProxyRoute(
        "POST",
        "/validation/runs/{id}/approve",
        "orchestrator",
        "/v1/validation/runs/{id}/approve",
        (C.APPROVE_DESTRUCTIVE,),
    ),
    ProxyRoute("GET", "/validation/runs", "orchestrator", "/v1/validation/runs"),
    ProxyRoute("GET", "/validation/runs/{id}", "orchestrator", "/v1/validation/runs/{id}"),
    # --- Factory (orchestrator §5) --------------------------------------------------------
    ProxyRoute("GET", "/factory/mes-tickets", "orchestrator", "/v1/factory/mes-tickets"),
    ProxyRoute("POST", "/factory/labels/parse", "orchestrator", "/v1/factory/labels/parse"),
    ProxyRoute("GET", "/factory/stations", "orchestrator", "/v1/factory/stations"),
    ProxyRoute("GET", "/factory/templates", "orchestrator", "/v1/factory/templates"),
    ProxyRoute("POST", "/factory/jobs", "orchestrator", "/v1/factory/jobs"),
    ProxyRoute(
        "POST",
        "/factory/jobs/{id}/decide",
        "orchestrator",
        "/v1/factory/jobs/{id}/decide",
        (C.FACTORY_VERDICT,),
    ),
    ProxyRoute(
        "POST",
        "/factory/jobs/{id}/control",
        "orchestrator",
        "/v1/factory/jobs/{id}/control",
        (C.FACTORY_CONTROL,),
    ),
    ProxyRoute("GET", "/factory/jobs", "orchestrator", "/v1/factory/jobs"),
    ProxyRoute("GET", "/factory/jobs/{id}", "orchestrator", "/v1/factory/jobs/{id}"),
    # --- Skills and tickets (orchestrator §5) ---------------------------------------------
    ProxyRoute("GET", "/skills", "orchestrator", "/v1/skills"),
    ProxyRoute("POST", "/skills/import", "orchestrator", "/v1/skills/import"),
    ProxyRoute("POST", "/skills/{id}/enable", "orchestrator", "/v1/skills/{id}/enable"),
    ProxyRoute("POST", "/skills/{id}/disable", "orchestrator", "/v1/skills/{id}/disable"),
    ProxyRoute("GET", "/skills/{id}/export", "orchestrator", "/v1/skills/{id}/export"),
    ProxyRoute("GET", "/tickets", "orchestrator", "/v1/tickets"),
    ProxyRoute("GET", "/tickets/{id}", "orchestrator", "/v1/tickets/{id}"),
    # --- Git remotes and hosts (git-broker §7) --------------------------------------------
    ProxyRoute("GET", "/git/remotes", "git_broker", "/v1/remotes"),
    ProxyRoute("POST", "/git/remotes", "git_broker", "/v1/remotes", (C.GIT_REMOTE_MANAGE,)),
    ProxyRoute(
        "POST",
        "/git/remotes/{id}/rotate",
        "git_broker",
        "/v1/remotes/{id}/rotate",
        (C.GIT_REMOTE_MANAGE,),
    ),
    ProxyRoute(
        "DELETE", "/git/remotes/{id}", "git_broker", "/v1/remotes/{id}", (C.GIT_REMOTE_MANAGE,)
    ),
    ProxyRoute(
        "POST",
        "/git/remotes/{id}/test",
        "git_broker",
        "/v1/remotes/{id}/test",
        (C.GIT_CLONE, C.GIT_PULL),
    ),
    ProxyRoute("GET", "/git/hosts", "git_broker", "/v1/hosts"),
    ProxyRoute("POST", "/git/hosts", "git_broker", "/v1/hosts", (C.GIT_HOSTS_MANAGE,)),
    # --- Git projects (git-broker §7) -----------------------------------------------------
    ProxyRoute("GET", "/git/projects/{slug}/status", "git_broker", "/v1/projects/{slug}/status"),
    ProxyRoute("POST", "/git/projects/{slug}/commit", "git_broker", "/v1/projects/{slug}/commit"),
    ProxyRoute("GET", "/git/projects/{slug}/history", "git_broker", "/v1/projects/{slug}/history"),
    ProxyRoute(
        "POST",
        "/git/projects/{slug}/push",
        "git_broker",
        "/v1/projects/{slug}/push",
        (C.GIT_PUSH_BRANCH,),
    ),
    ProxyRoute(
        "POST", "/git/projects/{slug}/pull", "git_broker", "/v1/projects/{slug}/pull", (C.GIT_PULL,)
    ),
    ProxyRoute(
        "POST",
        "/git/projects/{slug}/bundle/export",
        "git_broker",
        "/v1/projects/{slug}/bundle/export",
        (C.GIT_BUNDLE,),
    ),
    ProxyRoute(
        "POST",
        "/git/projects/{slug}/bundle/import",
        "git_broker",
        "/v1/projects/{slug}/bundle/import",
        (C.GIT_BUNDLE,),
    ),
    # --- Admin → Stations (factory-executor §6) -------------------------------------------
    ProxyRoute("GET", "/stations", "factory_executor", "/v1/station-records"),
    ProxyRoute(
        "POST", "/stations", "factory_executor", "/v1/station-records", (C.FACTORY_STATIONS_MANAGE,)
    ),
    ProxyRoute(
        "PUT",
        "/stations/{name}/tuning",
        "factory_executor",
        "/v1/station-records/{name}/tuning",
        (C.FACTORY_STATIONS_MANAGE,),
    ),
    ProxyRoute(
        "POST",
        "/stations/{name}/code",
        "factory_executor",
        "/v1/station-records/{name}/code",
        (C.FACTORY_STATIONS_MANAGE,),
    ),
    ProxyRoute(
        "POST",
        "/stations/{name}/revoke",
        "factory_executor",
        "/v1/station-records/{name}/revoke",
        (C.FACTORY_STATIONS_MANAGE,),
    ),
    ProxyRoute(
        "DELETE",
        "/stations/{name}",
        "factory_executor",
        "/v1/station-records/{name}",
        (C.FACTORY_STATIONS_MANAGE,),
    ),
    # --- Models page (model-manager §3) ---------------------------------------------------
    ProxyRoute("GET", "/models/status", "model_manager", "/v1/status"),
    ProxyRoute("POST", "/models/swap", "model_manager", "/v1/swap", (C.MODEL_MANAGE,)),
    ProxyRoute("POST", "/models/rollback", "model_manager", "/v1/rollback", (C.MODEL_MANAGE,)),
)

#: The terminal route, documented with the table but wired by hand below.
TERMINAL_PATH: Final = "/git/projects/{slug}/terminal"
TERMINAL_REQUIRES: Final = (C.GIT_TERMINAL,)


def client_for(downstream: Downstream, service: Service) -> Any:
    return getattr(downstream, service)


def fill_path(template: str, params: dict[str, Any]) -> str:
    """The downstream path with every {param} replaced by its URL-safe value."""
    return template.format(**{key: quote(str(value), safe="") for key, value in params.items()})


def check_capabilities(auth: Auth, requires: tuple[Capability, ...]) -> None:
    """Refuse before any downstream call. One capability: the authz sentence; several: any."""
    if len(requires) == 1:
        require(auth.principal, requires[0])
    else:
        proxy.require_any(auth.principal, requires)


def _endpoint_for(route: ProxyRoute) -> Callable[..., Response]:
    def endpoint(request: Request, auth: Ready, svc: ServicesDep, body: JsonBody) -> Response:
        check_capabilities(auth, route.requires)
        payload = proxy.forward(
            client_for(svc.downstream, route.service),
            route.method,
            fill_path(route.downstream_path, dict(request.path_params)),
            principal=auth.principal,
            body=body,
            params=proxy.query_of(request),
        )
        return proxy.as_response(payload)

    endpoint.__name__ = f"proxy_{route.method.lower()}_{route.path.strip('/').replace('/', '_')}"
    return endpoint


for _route in ROUTES:
    round2.add_api_route(_route.path, _endpoint_for(_route), methods=[_route.method])


# --- the terminal: two steps through the sandbox manager --------------------------------------


def no_sandbox(slug: str) -> ThreePartMessage:
    return ThreePartMessage(
        f"No sandbox is open for {slug}.",
        "The coding task that opened it has finished, or the sandbox closed after its idle time.",
        "Start a coding task or open the project first.",
    )


def first_session_id(answer: Any) -> str | None:
    """The id of the first session in the sandbox manager's answer, whatever its wrapping."""
    rows = answer.get("sessions") if isinstance(answer, dict) else answer
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            return str(row["id"])
    return None


@round2.post(TERMINAL_PATH)
def terminal_line(slug: str, auth: Ready, svc: ServicesDep, body: JsonBody) -> Response:
    check_capabilities(auth, TERMINAL_REQUIRES)
    manager = svc.downstream.sandbox_manager
    sessions = proxy.forward(
        manager,
        "GET",
        "/v1/sessions",
        principal=auth.principal,
        params={"user": auth.person.email, "slug": slug},
    )
    session_id = first_session_id(sessions)
    if session_id is None:
        raise ApiError(409, no_sandbox(slug))
    answer = proxy.forward(
        manager,
        "POST",
        f"/v1/sessions/{quote(session_id, safe='')}/terminal",
        principal=auth.principal,
        body=body,
    )
    return proxy.as_response(answer)


def browser_routes() -> list[tuple[str, str, tuple[Capability, ...]]]:
    """(method, browser path, capabilities) for every round-2 route, sorted; docs and tests."""
    rows = [(r.method, r.browser_path, r.requires) for r in ROUTES]
    rows.append(("POST", f"/api/v1{TERMINAL_PATH}", TERMINAL_REQUIRES))
    return sorted(rows)
