"""The PDU behind AC cycles (CLAUDE.md §15 open decision 3): one protocol, drivers per model.

Only the fake exists. The PDU model for the lab has not been named, and its protocol (SNMP
private MIB, a REST API, a serial console) decides both the driver and whether a dependency
is needed — so the model-specific driver is an explicit TODO, not a guess (CLAUDE.md §0.4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Literal, Protocol

from slas_hal.credentials import CredentialResolver
from slas_hal.drivers.process import ProcessRunner
from slas_hal.redfish import HalError
from slas_hal.targets import PduOutlet
from slas_schemas.errors import ThreePartMessage

OutletState = Literal["on", "off", "unknown"]


class PduDriver(Protocol):
    def outlet_state(self, outlet: int) -> OutletState: ...

    def outlet_off(self, outlet: int) -> None: ...

    def outlet_on(self, outlet: int) -> None: ...

    def cycle(self, outlet: int, *, off_s: int) -> None: ...


class FakePdu:
    def __init__(self, *, sleep: Callable[[float], None] | None = None) -> None:
        self.states: dict[int, OutletState] = {}
        self.actions: list[tuple[str, int]] = []
        self.sleep = sleep or (lambda _s: None)

    def outlet_state(self, outlet: int) -> OutletState:
        return self.states.get(outlet, "on")

    def outlet_off(self, outlet: int) -> None:
        self.actions.append(("off", outlet))
        self.states[outlet] = "off"

    def outlet_on(self, outlet: int) -> None:
        self.actions.append(("on", outlet))
        self.states[outlet] = "on"

    def cycle(self, outlet: int, *, off_s: int) -> None:
        self.outlet_off(outlet)
        self.sleep(off_s)
        self.outlet_on(outlet)


PduFactory = Callable[[PduOutlet, CredentialResolver, ProcessRunner], PduDriver]

# TODO(SLAS-HAL): register the lab's PDU model here once it is named; its protocol decides
# whether an SNMP library (a dependency to ask about) or a plain HTTPS client is needed.
PDU_DRIVERS: Final[dict[str, PduFactory]] = {}


def pdu_for(outlet: PduOutlet, *, resolver: CredentialResolver, runner: ProcessRunner) -> PduDriver:
    factory = PDU_DRIVERS.get(outlet.driver)
    if factory is None:
        known = ", ".join(sorted(PDU_DRIVERS)) or "none yet"
        raise HalError(
            ThreePartMessage(
                f"There is no PDU driver called {outlet.driver!r}.",
                f"Drivers available in this build: {known}. The lab's PDU model has not been "
                "named, so AC cycles cannot be performed by the platform yet.",
                "Name the PDU model so its driver can be written; until then use DC cycles, or "
                "perform the AC cycle by hand and record it on the ticket.",
            )
        )
    return factory(outlet, resolver, runner)
