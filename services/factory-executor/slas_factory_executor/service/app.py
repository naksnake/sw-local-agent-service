"""`create_app()`: the factory executor behind HTTP (docs/api-contract-round-2.md §6).

Every collaborator is injected so a test runs the real `FactoryExecutor` over a
`FakeStation`; `slas-factory-executor serve` wires the CA, the enrolment endpoint, the MES
poller and the mTLS runner clients (cli.py). One step at a time per ticket, as on the
validation side: a second concurrent call for the same ticket is a three-part 409.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI
from fastapi.routing import APIRoute

from slas_factory_executor.executor import FactoryExecutor
from slas_factory_executor.mes import FileDropMesAdapter
from slas_factory_executor.service import routes
from slas_factory_executor.service.collaborators import NoWatcher, Watcher
from slas_factory_executor.stations import EnrolmentService, StationRegistry
from slas_http.app import create_service_app, ops_router
from slas_http.errors import ServiceError
from slas_kernel.clock import SystemClock
from slas_kernel.leases import LeaseTable
from slas_observability.events import EventLog, StreamSink
from slas_observability.metrics import REGISTRY
from slas_schemas.errors import ThreePartMessage

SERVICE: str = "factory-executor"


class Clock(Protocol):
    def now(self) -> datetime: ...


class TicketLocks:
    """One lock per ticket id; a step already running for that ticket is a three-part 409."""

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, ticket_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(ticket_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[ticket_id] = lock
            return lock

    @contextmanager
    def hold(self, ticket_id: str) -> Iterator[None]:
        lock = self.lock_for(ticket_id)
        if not lock.acquire(blocking=False):
            raise ServiceError(
                409,
                ThreePartMessage(
                    f"A step of {ticket_id} is already running on the {self.service}.",
                    "The kernel sends one step at a time; a second call arrived before the "
                    "first one finished, or a retry overlapped it.",
                    "Wait for the running step to finish; nothing was started twice.",
                ),
            )
        try:
            yield
        finally:
            lock.release()


@dataclass
class Services:
    executor: FactoryExecutor
    registry: StationRegistry
    enrolment: EnrolmentService
    mes: FileDropMesAdapter
    templates_dir: Path
    leases: LeaseTable
    watcher: Watcher
    clock: Clock
    log: EventLog
    locks: TicketLocks = field(default_factory=TicketLocks)


def create_app(
    *,
    executor: FactoryExecutor,
    registry: StationRegistry,
    enrolment: EnrolmentService,
    mes: FileDropMesAdapter,
    templates_dir: Path,
    leases: LeaseTable | None = None,
    watcher: Watcher | None = None,
    log: EventLog | None = None,
    clock: Clock | None = None,
    enrolment_state: Callable[[], str] | None = None,
    mes_state: Callable[[], str] | None = None,
    version: str = "0.0.1",
) -> FastAPI:
    """The service app. `enrolment_state` and `mes_state` answer "ok" or "down" for
    `/health`; without the threads (tests) both read "ok"."""
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())

    def checks() -> dict[str, str]:
        return {
            "enrolment": enrolment_state() if enrolment_state is not None else "ok",
            "mes": mes_state() if mes_state is not None else "ok",
        }

    app = create_service_app(
        SERVICE, version=version, checks=checks, log=event_log, routers=(routes.router,)
    )
    app.state.services = Services(
        executor=executor,
        registry=registry,
        enrolment=enrolment,
        mes=mes,
        templates_dir=templates_dir,
        leases=leases if leases is not None else executor.leases,
        watcher=watcher if watcher is not None else NoWatcher(),
        clock=clock if clock is not None else SystemClock(),
        log=event_log,
    )
    return app


def route_table() -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
    pairs: set[tuple[str, str]] = set()
    for router in (ops_router(SERVICE, None, REGISTRY), routes.router):
        for route in router.routes:
            if isinstance(route, APIRoute):
                pairs.update((method, route.path) for method in route.methods or ())
    return sorted(pairs)
