"""`create_app()`: the validation executor behind HTTP (docs/api-contract-round-2.md §6).

Every collaborator is injected so a test runs the real `ValidationExecutor` over a
`FakeHal`; `slas-validation-executor serve` wires `RealHal`, the syslog receiver, the
guardrails and the quirks (cli.py). One step at a time per ticket: the kernel calls
sequentially, and a second concurrent call for the same ticket is refused with a 409 rather
than letting two power actions race on one target (INV-6).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from fastapi import FastAPI
from fastapi.routing import APIRoute

from slas_hal.targets import TargetRegistry
from slas_http.app import create_service_app, ops_router
from slas_http.errors import ServiceError
from slas_kernel.clock import SystemClock
from slas_kernel.leases import LeaseTable
from slas_observability.events import EventLog, StreamSink
from slas_observability.metrics import REGISTRY
from slas_schemas.errors import ThreePartMessage
from slas_validation_executor.executor import ValidationExecutor
from slas_validation_executor.service import routes

SERVICE: str = "validation-executor"


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
    executor: ValidationExecutor
    registry: TargetRegistry
    leases: LeaseTable
    clock: Clock
    log: EventLog
    locks: TicketLocks = field(default_factory=TicketLocks)


def create_app(
    *,
    executor: ValidationExecutor,
    registry: TargetRegistry,
    leases: LeaseTable | None = None,
    log: EventLog | None = None,
    clock: Clock | None = None,
    syslog_state: Callable[[], str] | None = None,
    version: str = "0.0.1",
) -> FastAPI:
    """The service app. `syslog_state` answers "ok" or "down" for `/health`; without a
    receiver (tests) the check reads "ok"."""
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())

    def checks() -> dict[str, str]:
        return {"hal": "ok", "syslog": syslog_state() if syslog_state is not None else "ok"}

    app = create_service_app(
        SERVICE, version=version, checks=checks, log=event_log, routers=(routes.router,)
    )
    app.state.services = Services(
        executor=executor,
        registry=registry,
        leases=leases if leases is not None else executor.leases,
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
