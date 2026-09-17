"""`create_service_app()`: the FastAPI app every service starts from (ADR-0015).

No docs, redoc or OpenAPI page (nothing may load a CDN, INV-1). `GET /health` runs the
service's checks and names the failing one in a 503; `GET /metrics` renders the shared
registry. One ASGI middleware binds the caller's trace id (or mints one), echoes it on the
response and turns an unexpected exception into a three-part 500.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from fastapi import APIRouter, FastAPI, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import MutableHeaders

from slas_http.errors import ServiceError, install_exception_handlers, problem_response, unexpected
from slas_observability import tracing
from slas_observability.events import EventLog, StreamSink
from slas_observability.metrics import REGISTRY, Registry
from slas_schemas.errors import ThreePartMessage

METRICS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: A health check: name → "ok" or a short word saying what is wrong ("down", "starting").
Checks = Callable[[], dict[str, str]]


class TraceMiddleware:
    """Binds the caller's trace id (or a new one), echoes it, and catches what nobody did."""

    def __init__(self, app: ASGIApp, log: EventLog, service: str) -> None:
        self.app = app
        self.log = log
        self.service = service

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
                response = problem_response(500, unexpected(self.service))
                response.headers[tracing.SLAS_HEADER] = trace_id
                await response(scope, receive, send)
        finally:
            tracing.unbind(token)


def health_response(service: str, checks: dict[str, str]) -> Response:
    if all(state == "ok" for state in checks.values()):
        return JSONResponse({"service": service, "ok": True, "checks": checks})
    failing = [name for name, state in checks.items() if state != "ok"]
    named = " and ".join(failing)
    raise ServiceError(
        503,
        ThreePartMessage(
            f"The {service} is not healthy: {named} did not answer.",
            f"The {named} service is starting or stopped.",
            f"Wait a moment; if it repeats, run `slas logs {failing[0]}` on the host.",
        ),
    )


def ops_router(service: str, checks: Checks | None, registry: Registry) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> Response:
        return health_response(service, checks() if checks is not None else {})

    @router.get("/metrics")
    def metrics() -> Response:
        return PlainTextResponse(registry.render(), media_type=METRICS_CONTENT_TYPE)

    return router


def create_service_app(
    service: str,
    *,
    version: str = "0.0.1",
    checks: Checks | None = None,
    log: EventLog | None = None,
    registry: Registry = REGISTRY,
    routers: tuple[APIRouter, ...] = (),
) -> FastAPI:
    """A service app with health, metrics, the trace middleware and three-part errors."""
    event_log = log if log is not None else EventLog(service, StreamSink())
    app = FastAPI(
        title=f"SW Local Agent Service {service}",
        version=version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.log = event_log
    app.state.service = service
    app.include_router(ops_router(service, checks, registry))
    for router in routers:
        app.include_router(router)
    install_exception_handlers(app, service)
    app.add_middleware(TraceMiddleware, log=event_log, service=service)
    return app
