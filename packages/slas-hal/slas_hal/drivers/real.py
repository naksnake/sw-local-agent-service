"""`RealHal`: the `Hal` protocol over real drivers, one target record per alias.

    power_state / sel / inventory     Redfish (lspci in-band when the BMC lacks link state)
    power                             arming gate → Redfish reset, or ipmitool per quirk;
                                      ac_cycle → the PDU driver; every action journalled ahead
    counters / ssh / wait_for_os      ssh, argv only
    console_on / console_lines        SOL capture (ipmitool sol activate)
    fence                             SOL annotation + syslog annotation + `logger` on the target

The executor never sees a credential and never calls a driver directly.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from slas_hal.credentials import CredentialResolver
from slas_hal.drivers.ipmi import IpmiTool
from slas_hal.drivers.pdu import PduDriver, pdu_for
from slas_hal.drivers.process import ProcessRunner, StreamRunner
from slas_hal.drivers.redfish import RedfishClient
from slas_hal.drivers.sol import SolCapture
from slas_hal.drivers.ssh import SshClient
from slas_hal.drivers.syslog import SyslogReceiver
from slas_hal.hal import CommandResult, PowerRecord
from slas_hal.http import HttpClient
from slas_hal.model import Counters, Inventory, PowerAction, PowerState, SelEntry, Snapshot
from slas_hal.quirks import QuirkTable
from slas_hal.redfish import HalError
from slas_hal.targets import TargetRecord, TargetRegistry
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage


class Clock(Protocol):
    def now(self) -> datetime: ...


class _Drivers:
    def __init__(self) -> None:
        self.redfish: RedfishClient | None = None
        self.ipmi: IpmiTool | None = None
        self.ssh: SshClient | None = None
        self.sol: SolCapture | None = None
        self.pdu: PduDriver | None = None


class RealHal:
    def __init__(
        self,
        registry: TargetRegistry,
        *,
        http: HttpClient,
        runner: ProcessRunner,
        stream_runner: StreamRunner,
        resolver: CredentialResolver,
        clock: Clock,
        data_root: Path,
        sleep: Callable[[float], None],
        quirk_table: QuirkTable | None = None,
        syslog: SyslogReceiver | None = None,
        key_dir: Path | None = None,
        pdu_off_s: int = 30,
        poll_s: float = 5.0,
    ) -> None:
        self.registry = registry
        self.http = http
        self.runner = runner
        self.stream_runner = stream_runner
        self.resolver = resolver
        self.clock = clock
        self.data_root = data_root
        self.sleep = sleep
        self.quirk_table = quirk_table
        self.syslog = syslog
        self.key_dir = key_dir or data_root / "Validation" / ".keys"
        self.pdu_off_s = pdu_off_s
        self.poll_s = poll_s
        self.power_records: list[PowerRecord] = []
        self._drivers: dict[str, _Drivers] = {}

    # --- wiring -----------------------------------------------------------------------------

    def record(self, alias: str) -> TargetRecord:
        return self.registry.get(alias)

    def _for(self, alias: str) -> _Drivers:
        drivers = self._drivers.get(alias)
        if drivers is None:
            drivers = _Drivers()
            self._drivers[alias] = drivers
        return drivers

    def redfish(self, alias: str) -> RedfishClient:
        drivers = self._for(alias)
        if drivers.redfish is None:
            drivers.redfish = RedfishClient(
                self.record(alias),
                http=self.http,
                resolver=self.resolver,
                quirk_table=self.quirk_table,
            )
        return drivers.redfish

    def ipmi(self, alias: str) -> IpmiTool:
        drivers = self._for(alias)
        if drivers.ipmi is None:
            drivers.ipmi = IpmiTool(self.record(alias), runner=self.runner, resolver=self.resolver)
        return drivers.ipmi

    def ssh_client(self, alias: str) -> SshClient:
        drivers = self._for(alias)
        if drivers.ssh is None:
            drivers.ssh = SshClient(
                self.record(alias), runner=self.runner, resolver=self.resolver, key_dir=self.key_dir
            )
        return drivers.ssh

    def pdu(self, alias: str) -> PduDriver:
        drivers = self._for(alias)
        if drivers.pdu is None:
            outlet = self.record(alias).pdu
            if outlet is None:
                raise HalError(
                    ThreePartMessage(
                        f"{alias} has no PDU outlet configured.",
                        "An AC cycle needs a PDU outlet the platform can switch.",
                        "Add the PDU outlet to the target record, or use DC cycles.",
                    )
                )
            drivers.pdu = pdu_for(outlet, resolver=self.resolver, runner=self.runner)
        return drivers.pdu

    # --- journal (INV-6) ----------------------------------------------------------------------

    def _journal(self, alias: str, entry: dict[str, object]) -> None:
        path = self.data_root / "Validation" / "Power" / f"{alias}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": self.clock.now().isoformat(), **entry}) + "\n")

    # --- Hal -----------------------------------------------------------------------------------

    def power_state(self, target: str) -> PowerState:
        return self.redfish(target).power_state()

    def power(self, target: str, action: PowerAction) -> None:
        record = self.registry.require_armed(target)  # ArmingError before anything else
        self._journal(target, {"intent": action})
        try:
            if action == "ac_cycle":
                outlet = record.pdu.outlet if record.pdu else 0
                self.pdu(target).cycle(outlet, off_s=self.pdu_off_s)
                how = f"pdu outlet {outlet} off {self.pdu_off_s} s then on"
            elif self.redfish(target).quirks().use_ipmi_for_power:
                how = f"ipmitool chassis power {self.ipmi(target).power(action)}"
            else:
                how = f"redfish ResetType {self.redfish(target).reset(action)}"
        except HalError as exc:
            self._journal(target, {"action": action, "failed": exc.message.what_happened})
            raise
        self.power_records.append(
            PowerRecord(target=target, action=action, n=len(self.power_records) + 1)
        )
        self._journal(target, {"action": action, "how": how})
        settle = self.redfish(target).quirks().power_off_settle_s
        if settle and action in ("off", "force_off"):
            self.sleep(settle)

    def sel(self, target: str) -> list[SelEntry]:
        return self.redfish(target).sel()

    def inventory(self, target: str) -> Inventory:
        redfish = self.redfish(target)
        devices = None
        try:
            if redfish.quirks().lanes_in_use_unreliable:
                raise HalError(ThreePartMessage("quirk: lanes unreliable", "quirk", "use lspci"))
            devices = redfish.pcie_devices()
        except HalError as exc:
            if self.record(target).ssh is None:
                raise
            # The BMC cannot give link width and speed; the OS can, when it is up.
            devices = self.ssh_client(target).devices()
            self._journal(
                target,
                {
                    "note": "PCIe link state read in-band via lspci",
                    "because": exc.message.what_happened,
                },
            )
        return redfish.inventory(devices)

    def counters(self, target: str) -> Counters:
        if self.record(target).ssh is None:
            return Counters()
        return self.ssh_client(target).counters()

    def snapshot(self, target: str) -> Snapshot:
        return Snapshot(
            taken_at=self.clock.now(),
            power=self.power_state(target),
            inventory=self.inventory(target),
            counters=self.counters(target),
            sel=self.sel(target),
        )

    def console_on(self, target: str) -> None:
        drivers = self._for(target)
        if drivers.sol is None:
            ipmi = self.ipmi(target)
            drivers.sol = SolCapture(
                target,
                runner=self.stream_runner,
                argv=ipmi.sol_argv(),
                env=ipmi.env(),
                sink=self.data_root / "Validation" / "Console" / f"{target}.log",
                clock=self.clock,
            )
        drivers.sol.start()

    def console_lines(self, target: str, *, since: int = 0) -> list[str]:
        sol = self._for(target).sol
        return sol.lines(since=since) if sol is not None else []

    def console_off(self, target: str) -> None:
        sol = self._for(target).sol
        if sol is not None:
            sol.stop()

    def fence(self, target: str, marker: str) -> None:
        sol = self._for(target).sol
        if sol is not None:
            sol.annotate(marker)
        if self.syslog is not None:
            self.syslog.annotate(marker, now=self.clock.now())
        if self.record(target).ssh is not None:
            # Best effort: the OS may be down at this point of the cycle.
            with contextlib.suppress(HalError):
                self.ssh_client(target).fence(marker)
        self._journal(target, {"fence": marker})

    def wait_for_os(self, target: str, *, timeout_s: int) -> bool:
        if self.record(target).ssh is not None:
            return self.ssh_client(target).wait_for_os(
                timeout_s=timeout_s, now=self.clock.now, sleep=self.sleep, poll_s=self.poll_s
            )
        # No in-band access: the BMC's power state is the best signal there is.
        from datetime import timedelta

        deadline = self.clock.now() + timedelta(seconds=timeout_s)
        while True:
            if self.power_state(target) == "on":
                return True
            if self.clock.now() >= deadline:
                return False
            self.sleep(self.poll_s)

    def ssh(self, target: str, argv: Sequence[str], *, timeout_s: int = 600) -> CommandResult:
        return self.ssh_client(target).run(argv, timeout_s=timeout_s)

    # --- audit --------------------------------------------------------------------------------

    def write_audit(self, alias: str) -> Path:
        drivers = self._for(alias)
        path = self.data_root / "Validation" / "Audit" / f"{alias}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "redfish": [a.model_dump() for a in (drivers.redfish.audit if drivers.redfish else [])],
            "ipmi": drivers.ipmi.audit if drivers.ipmi else [],
            "ssh": drivers.ssh.audit if drivers.ssh else [],
            "power": [r.model_dump() for r in self.power_records if r.target == alias],
        }
        write_atomic(path, json.dumps(payload, indent=2) + "\n", mode=0o644)
        return path
