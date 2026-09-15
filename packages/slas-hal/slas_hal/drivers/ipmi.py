"""ipmitool as an argv-only child process; the password travels in IPMI_PASSWORD (`-E`).

    ipmitool -I lanplus -H <bmc> -p <port> -U <user> -E chassis power status|on|off|soft|reset
    ipmitool … sel list
    ipmitool … sol activate            (streamed by `SolCapture`)

IPMI has no graceful restart: `graceful_restart` maps to `power reset`, a hard reset, and
the audit says so. Prefer Redfish for power unless the quirk table says otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from slas_hal.credentials import CredentialResolver
from slas_hal.drivers.process import ProcessRunner
from slas_hal.hal import CommandResult
from slas_hal.model import PowerAction, PowerState, SelEntry, SelSeverity
from slas_hal.redfish import HalError
from slas_hal.targets import TargetRecord
from slas_schemas.errors import ThreePartMessage

POWER_COMMANDS: Final[dict[str, str]] = {
    "on": "on",
    "off": "soft",
    "force_off": "off",
    "graceful_restart": "reset",  # IPMI has no graceful restart; this is a hard reset
}
_STATUS = re.compile(r"Chassis Power is (on|off)", re.IGNORECASE)
_SEL_LINE = re.compile(
    r"^\s*([0-9a-fA-F]+)\s*\|\s*(\d{2})/(\d{2})/(\d{4})\s*\|\s*(\d{2}:\d{2}:\d{2})\s*\|\s*([^|]*?)\s*\|\s*(.*)$"
)
_CRITICAL = re.compile(r"(?<!non-)critical|uncorrectable|non-recoverable|fail|fatal", re.IGNORECASE)
_WARNING = re.compile(r"warning|threshold|lower|upper|predictive|degraded", re.IGNORECASE)


class IpmiTool:
    def __init__(
        self, record: TargetRecord, *, runner: ProcessRunner, resolver: CredentialResolver
    ) -> None:
        self.record = record
        self.runner = runner
        self.resolver = resolver
        self.audit: list[tuple[str, int]] = []

    def argv(self, *command: str) -> list[str]:
        bmc = self.record.bmc
        return [
            "ipmitool",
            "-I",
            "lanplus",
            "-H",
            bmc.host,
            "-p",
            str(bmc.ipmi_port),
            "-U",
            bmc.user,
            "-E",
            *command,
        ]

    def env(self) -> dict[str, str]:
        return {"IPMI_PASSWORD": self.resolver.resolve(self.record.bmc.password_ref)}

    def _run(self, *command: str, timeout_s: int = 60) -> CommandResult:
        result = self.runner.run(self.argv(*command), env=self.env(), timeout_s=timeout_s)
        self.audit.append((" ".join(command), result.exit_code))
        alias = self.record.alias
        if result.exit_code == 127:
            raise HalError(
                ThreePartMessage(
                    "ipmitool is not installed in the validation executor.",
                    "The executor image is missing the ipmitool package.",
                    "Rebuild the validation-executor image; IPMI power and SOL need it.",
                )
            )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise HalError(
                ThreePartMessage(
                    f"ipmitool {' '.join(command)} failed on {alias} (exit {result.exit_code}).",
                    detail[-1] if detail else "ipmitool printed nothing.",
                    "Check the BMC address, the IPMI user's privilege level and that IPMI over "
                    "LAN is enabled on the BMC.",
                )
            )
        return result

    def power_state(self) -> PowerState:
        result = self._run("chassis", "power", "status")
        match = _STATUS.search(result.stdout)
        if match is None:
            return "unknown"
        return "on" if match.group(1).lower() == "on" else "off"

    def power(self, action: PowerAction) -> str:
        """Returns the ipmitool sub-command sent. The caller has checked the arming gate."""
        command = POWER_COMMANDS.get(action)
        if command is None:
            raise HalError(
                ThreePartMessage(
                    f"IPMI cannot perform {action!r} on {self.record.alias}.",
                    "AC cycles go through the PDU driver, not the BMC.",
                    "Configure a PDU outlet on the target record for AC cycles.",
                )
            )
        self._run("chassis", "power", command)
        return command

    def sel(self) -> list[SelEntry]:
        result = self._run("sel", "list")
        return parse_sel_list(result.stdout)

    def sol_argv(self) -> Sequence[str]:
        return self.argv("sol", "activate")


def parse_sel_list(text: str) -> list[SelEntry]:
    """`ipmitool sel list` lines such as
    `  1a | 09/10/2026 | 07:00:00 | Fan #0x30 | Lower Critical going low | Asserted`."""
    entries: list[SelEntry] = []
    for line in text.splitlines():
        match = _SEL_LINE.match(line)
        if match is None:
            continue
        raw_id, month, day, year, clock, sensor, rest = match.groups()
        try:
            entry_id = int(raw_id, 16)
        except ValueError:
            entry_id = 0
        message = " | ".join(part.strip() for part in rest.split("|") if part.strip())
        severity: SelSeverity = "OK"
        if _CRITICAL.search(message):
            severity = "Critical"
        elif _WARNING.search(message):
            severity = "Warning"
        entries.append(
            SelEntry(
                id=entry_id,
                created=f"{year}-{month}-{day}T{clock}Z",
                severity=severity,
                message=message or "(no message)",
                sensor=sensor.strip() or None,
            )
        )
    return entries
