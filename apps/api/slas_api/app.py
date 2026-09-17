"""The FastAPI application (ADR-0005). `create_app()` takes every collaborator so tests run
it against SQLite, an in-memory throttle and a fixed clock.

No docs, redoc or OpenAPI page is served (nothing may load a CDN, INV-1). Two small ASGI
middlewares wrap everything: one binds the trace id and turns an unexpected exception into
a three-part 500; the other refuses a state-changing request that does not carry
`X-Requested-With: slas-webui`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from datetime import datetime
from typing import Any, Final

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import Engine
from starlette.datastructures import MutableHeaders

from slas_api import routes
from slas_api.authz import RolesLoader
from slas_api.db import make_engine, session_scope
from slas_api.errors import UNEXPECTED, install_exception_handlers, problem_response
from slas_api.runtime_settings import mirror_to_env, read_runtime, seed_if_empty
from slas_api.security import Passwords
from slas_api.service import Services, utc_now
from slas_api.settings import Settings
from slas_api.throttle import RedisThrottle, Throttle
from slas_observability import tracing
from slas_observability.events import EventLog, StreamSink
from slas_schemas.errors import ThreePartMessage

__version__: Final = "0.0.1"
REQUESTED_WITH_HEADER: Final = "x-requested-with"
REQUESTED_WITH_VALUE: Final = "slas-webui"
STATE_CHANGING: Final = frozenset({"POST", "PATCH", "DELETE", "PUT"})

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

NOT_FROM_WEBUI: Final = ThreePartMessage(
    "The request didn't come from the SW Local Agent Service page.",
    "The X-Requested-With header is missing, so a form on another site or a script may "
    "have sent it.",
    "Use the platform's own page; if you are writing a script, send "
    "`X-Requested-With: slas-webui`.",
)


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return str(value.decode("latin-1"))
    return None


class TraceMiddleware:
    """Binds the caller's trace id (or a new one), echoes it, and catches what nobody did."""

    def __init__(self, app: ASGIApp, log: EventLog) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]
        }
        trace_id = tracing.trace_id_from_headers(headers) or tracing.new_trace_id()
        token = tracing.bind(trace_id)
        started = False

        async def send_with_trace(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)[tracing.SLAS_HEADER] = trace_id
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_trace)
            except Exception as exc:
                self.log.error(
                    "request.unexpected_error",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    error_type=type(exc).__name__,
                )
                if started:
                    raise
                response = problem_response(500, UNEXPECTED)
                response.headers[tracing.SLAS_HEADER] = trace_id
                await response(scope, receive, send)
        finally:
            tracing.unbind(token)


class RequestedWithMiddleware:
    """POST, PATCH and DELETE must say they come from the WebUI (ADR-0007)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method", "GET").upper() in STATE_CHANGING
            and _header(scope, REQUESTED_WITH_HEADER) != REQUESTED_WITH_VALUE
        ):
            response = problem_response(403, NOT_FROM_WEBUI)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def route_table() -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
    pairs: set[tuple[str, str]] = set()
    for router in (routes.ops, routes.api):
        for route in router.routes:
            if isinstance(route, APIRoute):
                pairs.update((method, route.path) for method in route.methods or ())
    return sorted(pairs)


def build_services(
    settings: Settings,
    *,
    engine: Engine | None = None,
    clock: Callable[[], datetime] | None = None,
    log: EventLog | None = None,
) -> Services:
    event_log = log if log is not None else EventLog("api", StreamSink())
    return Services(
        settings=settings,
        engine=engine if engine is not None else make_engine(settings.database_url()),
        roles=RolesLoader(settings.slas_roles_file, event_log),
        passwords=Passwords(settings.slas_argon2_profile),
        log=event_log,
        clock=clock if clock is not None else utc_now,
        version=__version__,
    )


def settings_at_start(services: Services) -> None:
    """Seed the settings table from `.env` when it is empty, then mirror the database out."""
    try:
        with session_scope(services.engine) as db:
            seeded = seed_if_empty(db, services.settings.env_file, services.clock())
            runtime = read_runtime(db)
        notice = mirror_to_env(runtime, services.settings.env_file)
    except Exception as exc:  # a start-up seed or mirror never stops the api
        services.log.warning("settings.start_failed", error_type=type(exc).__name__)
        return
    if seeded:
        services.log.info("settings.seeded", source=str(services.settings.env_file))
    if notice is not None:
        services.log.warning("settings.mirror_failed", what_happened=notice.what_happened)


def create_app(
    settings: Settings,
    *,
    engine: Engine | None = None,
    throttle: Throttle | None = None,
    clock: Callable[[], datetime] | None = None,
    log: EventLog | None = None,
    services: Services | None = None,
) -> FastAPI:
    svc = services or build_services(settings, engine=engine, clock=clock, log=log)
    limiter: Throttle = (
        throttle
        if throttle is not None
        else RedisThrottle(
            settings.slas_redis_host, settings.slas_redis_port, settings.redis_password()
        )
    )
    app = FastAPI(
        title="SW Local Agent Service api",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = svc
    app.state.throttle = limiter
    app.include_router(routes.ops)
    app.include_router(routes.api)
    install_exception_handlers(app)
    app.add_middleware(RequestedWithMiddleware)
    app.add_middleware(TraceMiddleware, log=svc.log)
    settings_at_start(svc)
    return app
