"""A stateful fake target and `FakeHal` (CLAUDE.md §11: tests run against recorded fakes).

The fake plays back the recorded Redfish fixtures, scripts a boot over SOL, and applies
planted failures at a chosen cycle: a PCIe link coming back narrower or slower, an Xid, a
new SEL entry, a boot that never reaches the OS. Every power action is recorded so a test
can prove a crash-recovered cycle did not power-cycle twice.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from importlib import resources
from typing import Literal, Protocol

from pydantic import Field

from slas_hal.hal import CommandResult, PowerRecord
from slas_hal.model import (
    Counters,
    Inventory,
    PcieDevice,
    PowerAction,
    PowerState,
    SelEntry,
    Snapshot,
)
from slas_hal.redfish import (
    HalError,
    load_payload,
    parse_inventory,
    parse_pcie_devices,
    parse_power_state,
    parse_sel,
)
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

PlantKind = Literal[
    "pcie_width", "pcie_speed", "xid", "sel", "boot_fail", "firmware", "device_missing"
]


def fixture_text(name: str) -> str:
    return resources.files("slas_hal.fakes").joinpath("fixtures", name).read_text(encoding="utf-8")


class Plant(SlasModel):
    """A failure the fake introduces when the target comes back from power cycle `at_cycle`."""

    at_cycle: int = Field(ge=1)
    kind: PlantKind
    bdf: str | None = None
    width: int | None = None
    speed_gts: float | None = None
    message: str | None = None
    #: How many cycles the effect lasts; None means it persists (a real degradation does).
    for_cycles: int | None = None

    def active(self, cycle: int) -> bool:
        if cycle < self.at_cycle:
            return False
        return self.for_cycles is None or cycle < self.at_cycle + self.for_cycles


class FakeTarget:
    """One server as the fake BMC and the fake OS see it."""

    def __init__(
        self,
        ref: str,
        *,
        plants: Sequence[Plant] = (),
        boot_seconds: int = 90,
        fixtures: dict[str, str] | None = None,
    ) -> None:
        self.ref = ref
        self.plants = list(plants)
        self.boot_seconds = boot_seconds
        self.fixtures = fixtures or {}
        self.power: PowerState = parse_power_state(self._payload("system.json", "system"))
        self.base_devices = parse_pcie_devices(
            self._payload("pcie_devices.json", "PCIe device"), target=ref
        )
        self.inventory = parse_inventory(
            self._payload("system.json", "system"),
            self._payload("manager.json", "manager"),
            list(self.base_devices),
        )
        self.sel: list[SelEntry] = parse_sel(self._payload("sel.json", "SEL"), target=ref)
        self.counters = Counters()
        self.cycles_done = 0
        self.booted = True
        self.console: list[str] = []
        self.syslog: list[str] = []
        self.power_records: list[PowerRecord] = []
        self.ssh_calls: list[list[str]] = []
        self.console_active = False

    def _payload(self, name: str, what: str) -> dict[str, object]:
        text = self.fixtures.get(name) or fixture_text(name)
        return load_payload(text, what=what, target=self.ref)

    # --- state changes --------------------------------------------------------------------

    def _sel_add(self, severity: str, message: str, when: str) -> None:
        next_id = max((e.id for e in self.sel), default=0) + 1
        self.sel.append(
            SelEntry(
                id=next_id, created=when, severity=severity, message=message, sensor="System Event"
            )
        )

    def apply_power(self, action: PowerAction, now: datetime) -> None:
        when = now.isoformat().replace("+00:00", "Z")
        self.power_records.append(
            PowerRecord(target=self.ref, action=action, n=len(self.power_records) + 1)
        )
        if action in ("off", "force_off"):
            self.power = "off"
            self.booted = False
            self._sel_add(
                "OK", "Power off requested" if action == "off" else "Power off forced", when
            )
            return
        if action == "ac_cycle":
            self.power = "off"
            self._sel_add("OK", "AC power lost, restored", when)
        # on · graceful_restart · ac_cycle all lead to a boot
        self.power = "on"
        self.cycles_done += 1
        self._sel_add("OK", "System boot initiated", when)
        self._apply_plants(when)
        self.console.extend(self.boot_lines())

    def _apply_plants(self, when: str) -> None:
        devices = [d.model_copy() for d in self.base_devices]
        self.booted = True
        for plant in self.plants:
            if not plant.active(self.cycles_done):
                continue
            if plant.kind in ("pcie_width", "pcie_speed"):
                for index, device in enumerate(devices):
                    if device.bdf == plant.bdf:
                        update: dict[str, object] = {}
                        if plant.width is not None:
                            update["width"] = plant.width
                        if plant.speed_gts is not None:
                            update["speed_gts"] = plant.speed_gts
                        devices[index] = device.model_copy(update=update)
            elif plant.kind == "device_missing":
                devices = [d for d in devices if d.bdf != plant.bdf]
            elif plant.kind == "firmware":
                for index, device in enumerate(devices):
                    if device.bdf == plant.bdf:
                        devices[index] = device.model_copy(
                            update={"firmware": plant.message or "0.0.0"}
                        )
            elif plant.kind == "xid" and plant.at_cycle == self.cycles_done:
                self.counters = self.counters.model_copy(update={"xid": self.counters.xid + 1})
                self.syslog.append(
                    f"NVRM: Xid (PCI:{plant.bdf or '0000:00:00'}): 79, GPU has fallen off the bus."
                )
            elif plant.kind == "sel" and plant.at_cycle == self.cycles_done:
                self._sel_add("Critical", plant.message or "Uncorrectable error detected", when)
            elif plant.kind == "boot_fail":
                self.booted = False
        self.inventory = self.inventory.model_copy(update={"devices": devices})

    def boot_lines(self) -> list[str]:
        lines = fixture_text("sol_boot.txt").splitlines()
        if not self.booted:
            cut = next((i for i, line in enumerate(lines) if "Linux version" in line), len(lines))
            return [*lines[:cut], "POST: PCIe training error on slot 3", "*** system halted ***"]
        return lines

    def snapshot(self, now: datetime) -> Snapshot:
        return Snapshot(
            taken_at=now,
            power=self.power,
            inventory=self.inventory.model_copy(deep=True),
            counters=self.counters.model_copy(),
            sel=[e.model_copy() for e in self.sel],
        )


class Clock(Protocol):
    def now(self) -> datetime: ...


class FakeHal:
    """`Hal` over a set of `FakeTarget`s. `crash_once_in` makes one method raise the first time
    it is called, the way a killed executor looks to the journal."""

    def __init__(
        self, targets: Sequence[FakeTarget], *, clock: Clock, crash_once_in: str | None = None
    ) -> None:
        self._targets = {t.ref: t for t in targets}
        self.clock = clock
        self.crash_once_in = crash_once_in
        self.fence_markers: list[tuple[str, str]] = []
        self.on_ssh: Callable[[FakeTarget, Sequence[str]], CommandResult] | None = None

    def target(self, ref: str) -> FakeTarget:
        try:
            return self._targets[ref]
        except KeyError:
            raise HalError(
                ThreePartMessage(
                    f"There is no target called {ref}.",
                    "The plan names a server the lab inventory does not know.",
                    "Pick a target from the Validation page's list.",
                )
            ) from None

    def _maybe_crash(self, method: str) -> None:
        if self.crash_once_in == method:
            self.crash_once_in = None
            raise RuntimeError(f"simulated crash during {method}")

    def power_state(self, target: str) -> PowerState:
        return self.target(target).power

    def power(self, target: str, action: PowerAction) -> None:
        self._maybe_crash("power")
        self.target(target).apply_power(action, self.clock.now())

    def sel(self, target: str) -> list[SelEntry]:
        return [e.model_copy() for e in self.target(target).sel]

    def inventory(self, target: str) -> Inventory:
        return self.target(target).inventory.model_copy(deep=True)

    def counters(self, target: str) -> Counters:
        return self.target(target).counters.model_copy()

    def snapshot(self, target: str) -> Snapshot:
        self._maybe_crash("snapshot")
        return self.target(target).snapshot(self.clock.now())

    def console_on(self, target: str) -> None:
        self.target(target).console_active = True

    def console_lines(self, target: str, *, since: int = 0) -> list[str]:
        return list(self.target(target).console[since:])

    def fence(self, target: str, marker: str) -> None:
        t = self.target(target)
        t.console.append(marker)
        t.syslog.append(marker)
        self.fence_markers.append((target, marker))

    def wait_for_os(self, target: str, *, timeout_s: int) -> bool:
        self._maybe_crash("wait_for_os")
        t = self.target(target)
        return t.power == "on" and t.booted and t.boot_seconds <= timeout_s

    def ssh(self, target: str, argv: Sequence[str], *, timeout_s: int = 600) -> CommandResult:
        t = self.target(target)
        t.ssh_calls.append(list(argv))
        if self.on_ssh is not None:
            return self.on_ssh(t, argv)
        if not t.booted:
            return CommandResult(exit_code=255, stderr="ssh: connect to host: No route to host")
        if list(argv[:1]) == ["logger"]:
            t.syslog.append(" ".join(argv[1:]))
        return CommandResult(exit_code=0, stdout=f"ok: {' '.join(argv)}\n")

    def devices_of(self, target: str) -> list[PcieDevice]:
        return list(self.target(target).inventory.devices)
