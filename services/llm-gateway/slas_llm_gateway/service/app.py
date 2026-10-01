"""`create_app()`: the gateway as a FastAPI service on `slas_http` (ADR-0015).

Every collaborator is injectable: tests pass a `Gateway` over `FakeVllm`, or an
`HttpxVllmClient` with a mock transport, a `FakeClock` and a `ListSink` log. Production
(`slas-gateway serve`) builds them from `Settings`.

Before the model manager's first `PUT /v1/instances` the routes are the role names mapped
to `vllm-<role>` and the instance table is empty; every completion answers 503 in three
parts until an instance for the role is healthy. The health check is `{"routes": "ok" |
"empty"}` and an empty table is still healthy: the gateway is up, the models are the model
manager's business.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Final

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from slas_http.app import create_service_app
from slas_llm_gateway import __version__
from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.consensus import TokenBudget
from slas_llm_gateway.gateway import Gateway
from slas_llm_gateway.instances import Clock, InstanceTable
from slas_llm_gateway.routing import ROLES, RoleRouter, Routes
from slas_llm_gateway.service.routes import GatewayService, build_router
from slas_llm_gateway.service.settings import Settings, load_consensus_rules, load_redactor
from slas_llm_gateway.vllm import HttpxVllmClient, VllmClient
from slas_observability.alerts import AlertChannel, SystemClock
from slas_observability.events import EventLog, StreamSink

SERVICE: Final = "llm-gateway"
#: Health states that still mean "up" (the contract's `"empty"` table).
HEALTHY_STATES: Final = frozenset({"ok", "empty"})
BREAKER_FAILURES: Final = 2
BREAKER_COOLDOWN: Final = timedelta(minutes=5)


def default_routes() -> Routes:
    """Every role points at `vllm-<role>` until the model manager says otherwise."""
    return Routes(roles={role: f"vllm-{role}" for role in ROLES}, voters=[])


def health_checks(instances: InstanceTable) -> dict[str, str]:
    return {"routes": "empty" if instances.empty() else "ok"}


def build_gateway(
    settings: Settings,
    *,
    instances: InstanceTable,
    clock: Clock,
    log: EventLog,
    client: VllmClient | None = None,
    channel: AlertChannel | None = None,
) -> Gateway:
    rules = load_consensus_rules(settings.consensus_file, log)
    redactor = load_redactor(settings.redaction_file, log)
    pct = (
        settings.token_budget_pct
        if settings.token_budget_pct is not None
        else rules.token_budget_pct
    )

    def alert(sentence: str) -> None:
        log.warning("consensus.alert", sentence=sentence)

    return Gateway(
        client=client
        if client is not None
        else HttpxVllmClient(instances, timeout_s=settings.vllm_timeout_s),
        router=RoleRouter(default_routes()),
        redactor=redactor,
        breaker=CircuitBreaker(
            clock, failure_threshold=BREAKER_FAILURES, cooldown=BREAKER_COOLDOWN
        ),
        rules=rules,
        budget=TokenBudget(clock, daily_tokens=settings.daily_tokens, pct=pct),
        alert=alert,
        channel=channel,
    )


def _install_health(app: FastAPI, checks: Callable[[], dict[str, str]]) -> None:
    """Serve `/health` with the gateway's own notion of healthy.

    `slas_http.app.health_response` treats every state other than "ok" as failing; the
    contract wants `"empty"` reported and still healthy. Until `slas_http` learns a set of
    healthy states, a route with that notion is placed ahead of the shared one, so it is
    the one that matches.
    """

    def health() -> JSONResponse:
        states = checks()
        ok = all(state in HEALTHY_STATES for state in states.values())
        return JSONResponse(
            {"service": SERVICE, "ok": ok, "checks": states}, status_code=200 if ok else 503
        )

    app.add_api_route("/health", health, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())


def _api_routes(routes: list[BaseRoute]) -> Iterator[APIRoute]:
    """Every `APIRoute`, descending into the routers `include_router` wrapped."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None)
        if isinstance(inner, APIRouter):
            yield from _api_routes(inner.routes)


def create_app(
    *,
    gateway: Gateway | None = None,
    instances: InstanceTable | None = None,
    log: EventLog | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    client: VllmClient | None = None,
    channel: AlertChannel | None = None,
) -> FastAPI:
    """The gateway app. Pass `gateway=` to control everything; otherwise it is built from
    `settings` (or the environment) over the given `instances`, `clock` and `client`."""
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    ticking = clock if clock is not None else SystemClock()
    table = instances if instances is not None else InstanceTable(ticking)
    if gateway is None:
        gateway = build_gateway(
            settings if settings is not None else Settings.from_env(),
            instances=table,
            clock=ticking,
            log=event_log,
            client=client,
            channel=channel,
        )
    service = GatewayService(gateway=gateway, instances=table, log=event_log)
    app = create_service_app(
        SERVICE,
        version=__version__,
        log=event_log,
        routers=(build_router(service),),
    )
    _install_health(app, lambda: health_checks(table))
    app.state.gateway = gateway
    app.state.instances = table
    return app


def route_table(app: FastAPI | None = None) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
    target = app if app is not None else create_app(settings=Settings(), log=_quiet_log())
    pairs: set[tuple[str, str]] = set()
    for route in _api_routes(target.router.routes):
        pairs.update((method, route.path) for method in route.methods or ())
    return sorted(pairs)


class _NullSink:
    def write(self, line: str) -> None:
        pass


def _quiet_log() -> EventLog:
    return EventLog(SERVICE, _NullSink())
