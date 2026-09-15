"""In-band access over the system `ssh` binary, argv only, key on tmpfs for one command.

    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<pinned>
        -o IdentitiesOnly=yes -o PasswordAuthentication=no -o ConnectTimeout=10
        -i <key> -p <port> <user>@<host> -- <argv…>

Also the in-band parsers VERIFY relies on: `lspci -D -vv -nn` for link width AND speed per
BDF (LnkSta), and `dmesg` for the AER / EDAC / MCE / Xid counters.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from slas_hal.credentials import CredentialResolver
from slas_hal.drivers.keyfile import ssh_files
from slas_hal.drivers.process import ProcessRunner
from slas_hal.hal import CommandResult
from slas_hal.model import Counters, PcieDevice
from slas_hal.redfish import HalError
from slas_hal.targets import TargetRecord
from slas_schemas.errors import ThreePartMessage

_LSPCI_HEADER: Final = re.compile(
    r"^([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]) (.+?)(?: \[([0-9a-f]{4})\])?: (.+?)"
    r"(?: \[([0-9a-f]{4}):([0-9a-f]{4})\])?(?: \(rev [0-9a-f]+\))?$"
)
_LNKSTA: Final = re.compile(r"LnkSta:\s*Speed\s+([\d.]+)GT/s(?: \([^)]*\))?,\s*Width\s+x(\d+)")
_LNKCAP: Final = re.compile(r"LnkCap:.*?Speed\s+([\d.]+)GT/s,\s*Width\s+x(\d+)")

COUNTER_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "aer": re.compile(r"\bAER\b|pcieport .*(?:Corrected|Uncorrected) error", re.IGNORECASE),
    "edac_ce": re.compile(r"\bEDAC\b.*\bCE\b"),
    "edac_ue": re.compile(r"\bEDAC\b.*\bUE\b"),
    "mce": re.compile(r"\bmce: \[Hardware Error\]|Machine check", re.IGNORECASE),
    "xid": re.compile(r"NVRM: Xid"),
}


class SshClient:
    def __init__(
        self,
        record: TargetRecord,
        *,
        runner: ProcessRunner,
        resolver: CredentialResolver,
        key_dir: Path,
        connect_timeout_s: int = 10,
    ) -> None:
        self.record = record
        self.runner = runner
        self.resolver = resolver
        self.key_dir = key_dir
        self.connect_timeout_s = connect_timeout_s
        self.audit: list[tuple[list[str], int]] = []

    def _access(self) -> tuple[str, str, int, str, str]:
        ssh = self.record.ssh
        if ssh is None:
            raise HalError(
                ThreePartMessage(
                    f"{self.record.alias} has no SSH access configured.",
                    "In-band steps (counters, lspci, stress tools, the fence marker in syslog) "
                    "need SSH to the target's OS.",
                    "Add SSH access to the target record: host, user, a pinned host key and a "
                    "private-key reference.",
                )
            )
        return ssh.host, ssh.user, ssh.port, ssh.private_key_ref, ssh.known_hosts_line

    def run(self, argv: Sequence[str], *, timeout_s: int = 600) -> CommandResult:
        host, user, port, key_ref, known = self._access()
        with ssh_files(
            self.key_dir,
            self.resolver.resolve(key_ref),
            known_hosts_line=known,
            tag=self.record.alias,
            connect_timeout_s=self.connect_timeout_s,
        ) as files:
            full = ["ssh", *files.options, "-p", str(port), f"{user}@{host}", "--", *argv]
            result = self.runner.run(full, env={}, timeout_s=timeout_s)
        self.audit.append((list(argv), result.exit_code))
        return result

    def reachable(self) -> bool:
        return self.run(["true"], timeout_s=self.connect_timeout_s + 5).exit_code == 0

    def wait_for_os(
        self,
        *,
        timeout_s: int,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        poll_s: float = 5.0,
    ) -> bool:
        deadline = now() + timedelta(seconds=timeout_s)
        while True:
            if self.reachable():
                return True
            if now() >= deadline:
                return False
            sleep(poll_s)

    def counters(self) -> Counters:
        result = self.run(["dmesg"])
        if result.exit_code != 0:
            raise HalError(
                ThreePartMessage(
                    f"Reading the kernel log on {self.record.alias} failed "
                    f"(exit {result.exit_code}).",
                    (result.stderr.strip().splitlines() or ["dmesg printed nothing"])[-1],
                    "Check that the SSH user may read the kernel ring buffer (dmesg_restrict).",
                )
            )
        return parse_dmesg_counters(result.stdout)

    def devices(self) -> list[PcieDevice]:
        result = self.run(["lspci", "-D", "-vv", "-nn"])
        if result.exit_code != 0:
            raise HalError(
                ThreePartMessage(
                    f"lspci failed on {self.record.alias} (exit {result.exit_code}).",
                    (result.stderr.strip().splitlines() or ["lspci printed nothing"])[-1],
                    "Install pciutils on the target image, or run the SSH user with enough "
                    "rights to read link status.",
                )
            )
        return parse_lspci(result.stdout)

    def fence(self, marker: str) -> CommandResult:
        return self.run(["logger", "-t", "slas", marker], timeout_s=30)

    def sync(self) -> CommandResult:
        return self.run(["sync"], timeout_s=120)


def parse_dmesg_counters(text: str) -> Counters:
    counts = dict.fromkeys(COUNTER_PATTERNS, 0)
    for line in text.splitlines():
        for name, pattern in COUNTER_PATTERNS.items():
            if pattern.search(line):
                counts[name] += 1
    return Counters(**counts)


def parse_lspci(text: str) -> list[PcieDevice]:
    """`lspci -D -vv -nn`: one device per header line; LnkSta gives width AND speed."""
    devices: list[PcieDevice] = []
    current: dict[str, object] | None = None

    def flush() -> None:
        if current is None:
            return
        if "width" not in current or "speed_gts" not in current:
            return  # a device without a link (host bridge, PCI legacy): not a PCIe link
        devices.append(PcieDevice.model_validate(current))

    for line in text.splitlines():
        header = _LSPCI_HEADER.match(line)
        if header is not None:
            flush()
            bdf, _class, class_code, name, vendor_id, device_id = header.groups()
            current = {
                "bdf": bdf.lower(),
                "name": name.strip(),
                "vendor_id": f"0x{vendor_id}" if vendor_id else "",
                "device_id": f"0x{device_id}" if device_id else "",
                "class_code": f"0x{class_code}00" if class_code else "",
            }
            continue
        if current is None:
            continue
        sta = _LNKSTA.search(line)
        if sta is not None:
            current["speed_gts"] = float(sta.group(1))
            current["width"] = int(sta.group(2))
            current.setdefault("max_speed_gts", float(sta.group(1)))
            current.setdefault("max_width", int(sta.group(2)))
            continue
        cap = _LNKCAP.search(line)
        if cap is not None:
            current["max_speed_gts"] = float(cap.group(1))
            current["max_width"] = int(cap.group(2))
    flush()
    return devices
