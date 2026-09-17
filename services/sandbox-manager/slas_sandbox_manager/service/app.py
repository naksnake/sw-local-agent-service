"""`create_app()`: the sandbox-manager service (docs/api-contract-round-2.md §4).

`build_services()` wires the collaborators — the runtime socket client, the Engine API
driver, the `SandboxManager`, the toolchain manifest, the start-up isolation probe and the
reaper — and every one of them is injectable, so tests run the whole app against
`FakeContainerApi`. `GET /health` answers `{"runtime": "ok", "isolation": "gvisor"|"runc"}`;
a socket that does not answer is a three-part 503 naming `runtime`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.middleware import Middleware

from slas_container import ContainerApi, ContainerError
from slas_http.app import ASGIApp, Receive, Scope, Send, create_service_app, health_response
from slas_http.errors import ServiceError, _http_status_message, problem_response
from slas_observability.events import EventLog, StreamSink
from slas_sandbox_manager.manager import SandboxManager
from slas_sandbox_manager.runtime import (
    ContainerApiLike,
    ContainerApiRuntime,
    Isolation,
    SandboxRuntime,
    detect_isolation,
)
from slas_sandbox_manager.service import routes
from slas_sandbox_manager.service.settings import Settings
from slas_sandbox_manager.spec import Resources
from slas_sandbox_manager.terminal import TerminalSession
from slas_sandbox_manager.toolchains import Manifest, image_for, load_manifest_file

SERVICE: Final = "sandbox-manager"
__version__: Final = "0.0.1"


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class Services:
    settings: Settings
    api: ContainerApiLike
    runtime: SandboxRuntime
    manager: SandboxManager
    manifest: Manifest
    isolation: Isolation | None
    runtime_problem: str
    log: EventLog
    clock: Clock
    #: One terminal per session, kept for the transcript numbering.
    terminals: dict[str, TerminalSession] = field(default_factory=dict)
    #: Session id → ticket id, so the terminal transcript lands on the ticket.
    tickets: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @contextmanager
    def locked(self) -> Iterator[SandboxManager]:
        """The manager is not thread-safe; the reaper and the routes take this lock."""
        with self.lock:
            yield self.manager

    def probe_images(self) -> list[str]:
        """Sandbox images the isolation probe may start, newest version first per language."""
        return [
            image_for(language, version, registry=self.settings.registry)
            for language, versions in self.manifest.toolchains.items()
            for version in reversed(versions)
        ]

    def probe(self) -> Isolation | None:
        """Ping the socket and detect the isolation; remembered for `/health` and the manager."""
        try:
            isolation = detect_isolation(
                self.api,
                default_runtime=self.settings.default_runtime,
                tier=self.settings.tier,
                probe_images=self.probe_images(),
            )
        except ContainerError as exc:
            self.isolation = None
            self.runtime_problem = exc.message.what_happened
            self.log.error("runtime.down", sentence=exc.message.what_happened)
            return None
        self.isolation = isolation
        self.runtime_problem = ""
        self.manager.runsc_available = isolation.runsc_available
        self.manager.kata_available = isolation.kata_available
        report = self.log.info if isolation.runsc_available else self.log.warning
        report("runtime.detected", sentence=isolation.sentence)
        return isolation

    def checks(self) -> dict[str, str]:
        if self.isolation is None:
            return {"runtime": "down"}
        return {"runtime": "ok", "isolation": self.isolation.isolation}

    def health(self) -> JSONResponse:
        checks = self.checks()
        if checks["runtime"] != "ok":
            self.probe()  # the socket may have come back since the last request
            checks = self.checks()
        if checks["runtime"] != "ok":
            health_response(SERVICE, checks)  # raises the three-part 503 naming `runtime`
        return JSONResponse({"service": SERVICE, "ok": True, "checks": checks})


class Reaper:
    """Closes idle sandboxes every `interval_s` seconds in a daemon thread (contract §4)."""

    def __init__(self, services: Services, interval_s: float) -> None:
        self.services = services
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[str]:
        with self.services.locked() as manager:
            closed = manager.reap()
            for session_id in closed:
                self.services.terminals.pop(session_id, None)
                self.services.tickets.pop(session_id, None)
        if closed:
            self.services.log.info("sandbox.reaped", closed=closed)
        return closed

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.run_once()
            except Exception as exc:  # the loop must survive a bad runtime call
                self.services.log.error("sandbox.reap_failed", error_type=type(exc).__name__)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="slas-sandbox-reaper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def build_services(
    settings: Settings,
    *,
    api: ContainerApiLike | None = None,
    runtime: SandboxRuntime | None = None,
    clock: Clock | None = None,
    log: EventLog | None = None,
    manifest: Manifest | None = None,
    probe: bool = True,
) -> Services:
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    container_api = api if api is not None else ContainerApi(settings.runtime_socket)
    sandbox_runtime = (
        runtime
        if runtime is not None
        else ContainerApiRuntime(
            container_api,
            host_data_root=settings.host_data_root,
            container_data_root=settings.data_root,
            seccomp_profile=settings.seccomp_profile,
        )
    )
    loaded = manifest if manifest is not None else load_manifest_file(settings.toolchain_manifest)
    ticking = clock if clock is not None else SystemClock()
    manager = SandboxManager(
        runtime=sandbox_runtime,
        data_root=settings.data_root,
        clock=ticking,
        runsc_available=settings.default_runtime == "runsc",
        profile=settings.profile,
        tier=settings.tier,
        max_sessions_per_user=settings.max_sessions_per_user,
        default_ttl_s=settings.default_ttl_s,
        resources=Resources(cpus=settings.cpus, memory=settings.memory, pids=settings.pids_limit),
    )
    services = Services(
        settings=settings,
        api=container_api,
        runtime=sandbox_runtime,
        manager=manager,
        manifest=loaded,
        isolation=None,
        runtime_problem="not probed yet",
        log=event_log,
        clock=ticking,
    )
    event_log.info(
        "toolchains.loaded",
        source=loaded.source,
        registry=settings.registry,
        languages={language: versions[-1] for language, versions in loaded.toolchains.items()},
    )
    if probe:
        services.probe()
    return services


def route_table() -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
    pairs: set[tuple[str, str]] = {("GET", "/health"), ("GET", "/metrics")}
    for route in routes.router.routes:
        if isinstance(route, APIRoute):
            pairs.update((method, route.path) for method in route.methods or ())
    return sorted(pairs)


def create_app(
    settings: Settings, *, services: Services | None = None, reaper: bool = False
) -> FastAPI:
    svc = services if services is not None else build_services(settings)
    reaping = Reaper(svc, settings.reap_interval_s) if reaper else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        if reaping is not None:
            reaping.start()
        try:
            yield
        finally:
            if reaping is not None:
                reaping.stop()

    app = create_service_app(
        SERVICE, version=__version__, log=svc.log, routers=(routes.router,), checks=svc.checks
    )
    app.router.lifespan_context = lifespan
    # The shared `/health` knows only "ok" or a failure word per check; the contract wants the
    # isolation fact beside `runtime`. `HealthFacts` answers GET /health before routing, as the
    # innermost user middleware, so the trace middleware still binds and echoes the trace id.
    app.user_middleware.append(Middleware(HealthFacts, services=svc))
    app.state.services = svc
    app.state.reaper = reaping
    return app


class HealthFacts:
    """Answers `GET /health` with `{"runtime": "ok", "isolation": …}` or the three-part 503."""

    def __init__(self, app: ASGIApp, services: Services) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/health":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "GET":
            response = problem_response(405, _http_status_message(SERVICE, 405))
            await response(scope, receive, send)
            return
        try:
            response = self.services.health()
        except ServiceError as exc:
            response = problem_response(exc.status, exc.message)
        await response(scope, receive, send)
