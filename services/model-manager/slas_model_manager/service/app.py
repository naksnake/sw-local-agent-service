"""`create_app(settings, …)`: the model manager as a service (ADR-0015, contract §3).

Every collaborator is injectable: the container API (the fake in tests), the health prober,
the smoke tester, the gateway client, the clock and the event log. The reconcile loop is
started by `serve`, not here, so a test drives `controller.reconcile()` by hand.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute

from slas_container import ContainerApi, ContainerError
from slas_http.app import create_service_app
from slas_http.client import ServiceClient
from slas_model_manager import __version__
from slas_model_manager.controller import Controller
from slas_model_manager.driver import ContainerApiRuntime, PatientRuntime, Prober, RuntimeApi
from slas_model_manager.service.routes import build_router
from slas_model_manager.service.settings import Settings
from slas_model_manager.smoke import HttpSmokeTester
from slas_model_manager.swap import Clock, SmokeTester
from slas_observability.events import EventLog, StreamSink

SERVICE = "model-manager"


def runtime_checks(api: RuntimeApi) -> Callable[[], dict[str, str]]:
    def checks() -> dict[str, str]:
        try:
            api.ping()
        except ContainerError:
            return {"runtime": "down"}
        return {"runtime": "ok"}

    return checks


def build_controller(
    settings: Settings,
    *,
    api: RuntimeApi | None = None,
    prober: Prober | None = None,
    smoke: SmokeTester | None = None,
    gateway: ServiceClient | None = None,
    log: EventLog | None = None,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[Controller, RuntimeApi]:
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    runtime_api: RuntimeApi = api if api is not None else ContainerApi(settings.runtime_socket)
    runtime = ContainerApiRuntime(
        runtime_api,
        image=settings.vllm_image,
        host_models_dir=settings.host_models_dir,
        network=settings.inference_network,
        shm_bytes=settings.vllm_shm_bytes,
        prober=prober,
    )
    patient = (
        PatientRuntime(runtime, timeout_s=settings.model_start_timeout_s, sleep=sleep)
        if sleep is not None
        else PatientRuntime(runtime, timeout_s=settings.model_start_timeout_s)
    )
    controller = Controller(
        runtime=runtime,
        swap_runtime=patient,
        registry_path=settings.models_file,
        gateway=gateway
        if gateway is not None
        else ServiceClient("llm-gateway", settings.gateway_url),
        gpu_ids=settings.gpu_ids,
        gpu_vram_gib=settings.gpu_vram_gib,
        image=settings.vllm_image,
        log=event_log,
        smoke=smoke if smoke is not None else HttpSmokeTester(),
        clock=clock,
        ping=runtime_api.ping,
    )
    return controller, runtime_api


def create_app(
    settings: Settings,
    *,
    api: RuntimeApi | None = None,
    prober: Prober | None = None,
    smoke: SmokeTester | None = None,
    gateway: ServiceClient | None = None,
    log: EventLog | None = None,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
) -> FastAPI:
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    controller, runtime_api = build_controller(
        settings,
        api=api,
        prober=prober,
        smoke=smoke,
        gateway=gateway,
        log=event_log,
        clock=clock,
        sleep=sleep,
    )
    app = create_service_app(
        SERVICE,
        version=__version__,
        checks=runtime_checks(runtime_api),
        log=event_log,
        routers=(build_router(controller),),
    )
    app.state.controller = controller
    app.state.settings = settings
    return app


def route_table(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract.

    FastAPI 0.141 lists an included router as a wrapper that keeps the `APIRouter` under
    `original_router`; the routes themselves are read from there.
    """
    pairs: set[tuple[str, str]] = set()

    def walk(routes: Iterable[object]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                pairs.update((method, route.path) for method in route.methods or ())
            else:
                inner = getattr(route, "original_router", None)
                if inner is not None:
                    walk(inner.routes)

    walk(app.routes)
    return sorted(pairs)
