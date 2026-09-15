"""The one interface every hardware action goes through (CLAUDE.md §0.3, §5.2, INV-3).

The executor calls these methods; a model never does. Drivers (Redfish over HTTPS, IPMI,
SSH, a PDU) implement the protocol in P8; `slas_hal.fakes` implements it for every test.
Every method takes an opaque target reference: credentials are resolved by the driver at
dispatch and never appear here (INV-5).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import Field

from slas_hal.model import Counters, Inventory, PowerAction, PowerState, SelEntry, Snapshot
from slas_schemas.common import SlasModel


class CommandResult(SlasModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class Hal(Protocol):
    def power_state(self, target: str) -> PowerState: ...

    def power(self, target: str, action: PowerAction) -> None: ...

    def sel(self, target: str) -> list[SelEntry]: ...

    def inventory(self, target: str) -> Inventory: ...

    def counters(self, target: str) -> Counters: ...

    def snapshot(self, target: str) -> Snapshot: ...

    def console_on(self, target: str) -> None: ...

    def console_lines(self, target: str, *, since: int = 0) -> list[str]: ...

    def fence(self, target: str, marker: str) -> None: ...

    def wait_for_os(self, target: str, *, timeout_s: int) -> bool: ...

    def ssh(self, target: str, argv: Sequence[str], *, timeout_s: int = 600) -> CommandResult: ...


class PowerRecord(SlasModel):
    """One power action as the journal and the audit see it."""

    target: str
    action: PowerAction
    n: int = Field(ge=1)
