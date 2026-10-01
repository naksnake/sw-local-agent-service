"""`create_app(settings, …)`: the model fetcher as a service (ADR-0018, contract §3b).

Every collaborator is injectable: the hub factory (a loopback fake in tests), the clock, the
event log, and whether fetches run in threads. Health never calls the hub: `models_dir` says
whether the directory is writable and `hub` reads "not checked".
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute

from slas_http.app import create_service_app
from slas_model_fetcher import __version__
from slas_model_fetcher.fetcher import Clock, FetchManager, HubFactory
from slas_model_fetcher.service.routes import build_router
from slas_model_fetcher.service.settings import Settings
from slas_observability.events import EventLog, StreamSink

SERVICE: Final = "model-fetcher"
HUB_NOT_CHECKED: Final = "not checked"
HEALTHY_STATES: Final = frozenset({"ok", HUB_NOT_CHECKED})


def health_checks(settings: Settings) -> Callable[[], dict[str, str]]:
    def checks() -> dict[str, str]:
        try:
            settings.models_dir.mkdir(parents=True, exist_ok=True)
            writable = os.access(settings.models_dir, os.W_OK)
        except OSError:
            writable = False
        return {"models_dir": "ok" if writable else "unwritable", "hub": HUB_NOT_CHECKED}

    return checks


def build_manager(
    settings: Settings,
    *,
    hub_factory: HubFactory | None = None,
    log: EventLog | None = None,
    clock: Clock | None = None,
    threads: bool = True,
) -> FetchManager:
    return FetchManager(
        models_dir=settings.models_dir,
        hub_factory=hub_factory if hub_factory is not None else settings.hub,
        hosts=settings.allowed_hosts,
        log=log if log is not None else EventLog(SERVICE, StreamSink()),
        clock=clock,
        threads=threads,
    )


def create_app(
    settings: Settings,
    *,
    hub_factory: HubFactory | None = None,
    log: EventLog | None = None,
    clock: Clock | None = None,
    threads: bool = True,
) -> FastAPI:
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    manager = build_manager(
        settings, hub_factory=hub_factory, log=event_log, clock=clock, threads=threads
    )
    app = create_service_app(
        SERVICE,
        version=__version__,
        checks=health_checks(settings),
        log=event_log,
        routers=(build_router(manager),),
        healthy_states=HEALTHY_STATES,
    )
    app.state.manager = manager
    app.state.settings = settings
    return app


def route_table(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
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
