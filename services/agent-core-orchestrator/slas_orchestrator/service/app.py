"""`create_app()`: the orchestrator's FastAPI app with every collaborator injectable.

Defaults are the production wiring (contract §1, §5): the sandbox manager and the git
broker over HTTP, the gateway through `slas_llm_gateway.client.HttpGateway` when that module
is present, tickets in `FileTicketStore(data_root)`, one kernel per run built by
`build_coding_kernel`. Tests pass fakes for any of them.

The app is assembled from `slas_http.app`'s parts (trace middleware, three-part errors,
`health_response`) rather than by `create_service_app`, because the contract makes three
health checks optional and the shared ops router treats every failing check as fatal.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, cast

import httpx
import yaml
from fastapi import APIRouter, FastAPI, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute

from slas_http.app import METRICS_CONTENT_TYPE, TraceMiddleware, health_response
from slas_http.client import ServiceClient
from slas_http.errors import install_exception_handlers
from slas_kernel.clock import Clock, SystemClock
from slas_kernel.kernel import Kernel
from slas_kernel.skills import SkillGate
from slas_kernel.store import FileTicketStore, TicketStore
from slas_observability.events import EventLog, StreamSink
from slas_observability.metrics import REGISTRY
from slas_orchestrator.clients import HttpSandboxManager, TicketBoundExecutor
from slas_orchestrator.coding.agent import CodingAgent
from slas_orchestrator.coding.coder import GatewayCoder, GatewayLike
from slas_orchestrator.coding.executor import Coder, CodingExecutor, EditRequest, EditSet
from slas_orchestrator.gateway import GatewayCrossChecker
from slas_orchestrator.service import coding, skills, tickets
from slas_orchestrator.service.deps import Deps, KernelFactory, Probe
from slas_orchestrator.service.runs import RunRegistry
from slas_orchestrator.service.settings import MANDATORY_CHECKS, SERVICE_NAME, Settings
from slas_sandbox_manager.toolchains import (
    Manifest,
    ToolchainError,
    default_manifest,
    load_manifest,
)
from slas_schemas.errors import ThreePartMessage
from slas_skills.state import SkillStateStore
from slas_sop.glossary import Glossary, GlossaryError, default_glossary, glossary_from_mapping
from slas_triage.routing import OwnerRouting, OwnerRoutingError, routing_from_mapping

__version__: Final = "0.0.1"

#: The gateway client module the llm-gateway slice provides (contract §2).
GATEWAY_CLIENT_MODULE: Final = "slas_llm_gateway.client"


class _Auto:
    """`create_app(gateway=AUTO)`: connect to the gateway named in the settings."""


AUTO: Final = _Auto()


class NoGatewayCoder:
    """Stands in when no gateway is reachable: the run stops with one clear sentence."""

    def propose_edits(self, request: EditRequest) -> EditSet:
        raise NoGatewayError()


class NoGatewayError(RuntimeError):
    def __init__(self) -> None:
        self.message = ThreePartMessage(
            "The Coding Agent cannot propose edits: no model gateway is connected.",
            "The llm-gateway client is not installed in this orchestrator, or SLAS_GATEWAY_URL "
            "is empty.",
            "Check the llm-gateway container and the orchestrator's settings, then start the "
            "task again.",
        )
        super().__init__(self.message.what_happened)


def connect_gateway(settings: Settings, log: EventLog) -> GatewayLike | None:
    """`HttpGateway(settings.gateway_url)` when the client module exists; otherwise None."""
    try:
        module = importlib.import_module(GATEWAY_CLIENT_MODULE)
    except ImportError:
        log.warning(
            "gateway.client_missing",
            module=GATEWAY_CLIENT_MODULE,
            sentence="No gateway client is installed; edits and cross-checks are unavailable.",
        )
        return None
    factory: Callable[..., Any] | None = getattr(module, "HttpGateway", None)
    if factory is None:  # pragma: no cover — the module exists without the class
        log.warning("gateway.client_missing", module=GATEWAY_CLIENT_MODULE, attribute="HttpGateway")
        return None
    return cast(GatewayLike, factory(settings.gateway_url))


def http_probe(timeout_s: float) -> Probe:
    def probe(name: str, base_url: str) -> str:
        try:
            response = httpx.get(f"{base_url.rstrip('/')}/health", timeout=timeout_s)
        except httpx.HTTPError:
            return "down"
        return "ok" if response.status_code == 200 else "down"

    return probe


def load_glossary(path: Path, log: EventLog) -> Glossary:
    if not path.is_file():
        log.warning("glossary.missing", path=str(path), sentence="Using the built-in glossary.")
        return default_glossary()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return glossary_from_mapping(data, source=str(path))
    except (OSError, yaml.YAMLError, GlossaryError) as exc:
        log.warning("glossary.unreadable", path=str(path), why=str(exc)[:200])
        return default_glossary()


def load_owner_routing(path: Path, log: EventLog) -> OwnerRouting | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return routing_from_mapping(data, source=str(path))
    except (OSError, yaml.YAMLError, OwnerRoutingError) as exc:
        log.warning("owner_routing.unreadable", path=str(path), why=str(exc)[:200])
        return None


def load_toolchains(data_root: Path, log: EventLog) -> Manifest:
    try:
        return load_manifest(data_root)
    except ToolchainError as exc:
        log.warning("toolchains.unreadable", why=exc.message.what_happened)
        return default_manifest()


def build_coding_kernel(deps: Deps) -> Kernel:
    """One kernel per run: the skill library is read fresh, so an import needs no restart."""
    library = skills.load_library(deps.settings.skills_library)
    return Kernel(
        data_root=deps.data_root,
        agent=deps.coding_agent,
        executor=TicketBoundExecutor(deps.coding_executor),
        store=deps.registry.store,
        clock=deps.clock,
        glossary=deps.glossary,
        skill_gate=SkillGate(library=library, state=deps.skill_state, clock=deps.clock),
    )


def health_checks(deps: Deps) -> dict[str, str]:
    return {name: deps.probe(name, url) for name, url in deps.settings.service_urls().items()}


def ops_router(deps: Deps) -> APIRouter:
    """`/health` and `/metrics` with the contract's rule: only the gateway and the sandbox
    manager failing make the orchestrator unhealthy; the other zones are reported as they
    are. `slas_http`'s own ops router treats every non-ok check as fatal, so this one is
    built here from the same parts (`health_response` keeps the shared 503 wording)."""
    router = APIRouter()

    @router.get("/health")
    def health() -> Response:
        checks = health_checks(deps)
        health_response(SERVICE_NAME, {k: v for k, v in checks.items() if k in MANDATORY_CHECKS})
        return JSONResponse({"service": SERVICE_NAME, "ok": True, "checks": checks})

    @router.get("/metrics")
    def metrics() -> Response:
        return PlainTextResponse(REGISTRY.render(), media_type=METRICS_CONTENT_TYPE)

    return router


def assemble(service: str, log: EventLog, routers: tuple[APIRouter, ...]) -> FastAPI:
    """`slas_http.create_service_app` with this service's own ops router (see `ops_router`)."""
    app = FastAPI(
        title=f"SW Local Agent Service {service}",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.log = log
    app.state.service = service
    app.state.routers = routers
    for router in routers:
        app.include_router(router)
    install_exception_handlers(app, service)
    app.add_middleware(TraceMiddleware, log=log, service=service)
    return app


def route_table(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
    pairs: set[tuple[str, str]] = set()
    routers: tuple[APIRouter, ...] = app.state.routers
    for router in routers:
        for route in router.routes:
            if isinstance(route, APIRoute):
                pairs.update((method, route.path) for method in route.methods or ())
    return sorted(pairs)


def create_app(
    settings: Settings | None = None,
    *,
    store: TicketStore | None = None,
    clock: Clock | None = None,
    log: EventLog | None = None,
    sandbox: HttpSandboxManager | None = None,
    gateway: GatewayLike | _Auto | None = AUTO,
    broker: ServiceClient | _Auto | None = AUTO,
    coder: Coder | None = None,
    coding_agent: CodingAgent | None = None,
    kernel_factory: KernelFactory | None = None,
    registry: RunRegistry | None = None,
    probe: Probe | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    event_log = log if log is not None else EventLog(SERVICE_NAME, StreamSink())
    clock = clock or SystemClock()
    store = store if store is not None else FileTicketStore(settings.data_root)
    registry = registry or RunRegistry(
        store=store, data_root=settings.data_root, clock=clock, log=event_log
    )
    sandbox = sandbox or HttpSandboxManager(
        settings.sandbox_manager_url, data_root=settings.data_root
    )
    gateway_client = connect_gateway(settings, event_log) if isinstance(gateway, _Auto) else gateway
    broker_client = (
        ServiceClient("git-broker", settings.git_broker_url)
        if isinstance(broker, _Auto)
        else broker
    )
    coding_agent = coding_agent or CodingAgent(
        manifest=load_toolchains(settings.data_root, event_log), now=clock.now
    )
    if coder is None:
        coder = GatewayCoder(gateway_client) if gateway_client is not None else NoGatewayCoder()
    executor = CodingExecutor(
        manager=sandbox,
        coder=coder,
        cross_checker=GatewayCrossChecker(gateway_client) if gateway_client is not None else None,
    )
    deps = Deps(
        settings=settings,
        log=event_log,
        clock=clock,
        store=store,
        registry=registry,
        sandbox=sandbox,
        gateway=gateway_client,
        broker=broker_client,
        coding_agent=coding_agent,
        coding_executor=executor,
        kernel_factory=kernel_factory or build_coding_kernel,
        skill_state=SkillStateStore(settings.skills_library),
        glossary=load_glossary(settings.glossary_file, event_log),
        owner_routing=load_owner_routing(settings.owner_routing_file, event_log),
        probe=probe or http_probe(settings.probe_timeout_s),
    )
    routers = (ops_router(deps), coding.router, skills.router, tickets.router)
    # The Validation and Factory routers arrive with the executor slice; the integrator
    # includes them here (each module exports `router` and a `build(...)` helper):
    # from slas_orchestrator.service import factory, validation
    # routers = (*routers, validation.router, factory.router)
    app = assemble(SERVICE_NAME, event_log, routers)
    app.state.deps = deps
    return app
