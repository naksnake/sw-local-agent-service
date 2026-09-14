"""What the HAL reports about a target (CLAUDE.md §5.2 `oob`/`inband`, §10.2 VERIFY).

These are the platform's own models; Redfish payloads are parsed into them in `redfish.py`
and never handed to anything else raw. A `Snapshot` is what a baseline and every VERIFY
compare (`slas_diff`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import Field

from slas_schemas.common import SlasModel

PowerState = Literal["on", "off", "unknown"]
PowerAction = Literal["on", "off", "force_off", "graceful_restart", "ac_cycle"]
SelSeverity = Literal["OK", "Warning", "Critical"]

#: Redfish `PCIeType` → transfer rate in GT/s per lane.
PCIE_GEN_GTS: Final[dict[str, float]] = {
    "Gen1": 2.5,
    "Gen2": 5.0,
    "Gen3": 8.0,
    "Gen4": 16.0,
    "Gen5": 32.0,
    "Gen6": 64.0,
}
BDF_PATTERN: Final = r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$"


class SelEntry(SlasModel):
    id: int = Field(ge=0)
    created: str
    severity: SelSeverity
    message: str
    sensor: str | None = None

    def line(self) -> str:
        return f"[{self.created}] {self.severity}: {self.message}"


class PcieDevice(SlasModel):
    bdf: str = Field(pattern=BDF_PATTERN)
    name: str = Field(min_length=1)
    vendor_id: str = ""
    device_id: str = ""
    class_code: str = ""
    #: Negotiated link width (LnkSta), lanes.
    width: int = Field(ge=1, le=32)
    #: Negotiated link speed (LnkSta), GT/s per lane.
    speed_gts: float = Field(gt=0)
    max_width: int = Field(ge=1, le=32)
    max_speed_gts: float = Field(gt=0)
    firmware: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.bdf})"

    def link(self) -> str:
        return f"x{self.width} @ {self.speed_gts:g} GT/s"


class Inventory(SlasModel):
    model: str = "unknown"
    serial: str = "unknown"
    bios_version: str = "unknown"
    bmc_version: str = "unknown"
    cpus: int = Field(default=0, ge=0)
    memory_gib: int = Field(default=0, ge=0)
    devices: list[PcieDevice] = Field(default_factory=list)

    def firmware(self) -> dict[str, str]:
        versions = {"BIOS": self.bios_version, "BMC": self.bmc_version}
        for device in self.devices:
            if device.firmware:
                versions[device.label] = device.firmware
        return versions

    def device(self, bdf: str) -> PcieDevice | None:
        for device in self.devices:
            if device.bdf == bdf:
                return device
        return None


class Counters(SlasModel):
    """Error counters read in-band; each one only ever grows between a baseline and a cycle."""

    aer: int = Field(default=0, ge=0)
    edac_ce: int = Field(default=0, ge=0)
    edac_ue: int = Field(default=0, ge=0)
    mce: int = Field(default=0, ge=0)
    xid: int = Field(default=0, ge=0)

    def as_dict(self) -> dict[str, int]:
        return {
            "AER": self.aer,
            "EDAC CE": self.edac_ce,
            "EDAC UE": self.edac_ue,
            "MCE": self.mce,
            "Xid": self.xid,
        }


class Snapshot(SlasModel):
    taken_at: datetime
    power: PowerState
    inventory: Inventory
    counters: Counters = Field(default_factory=Counters)
    sel: list[SelEntry] = Field(default_factory=list)

    def sentence(self) -> str:
        return (
            f"{self.inventory.model} {self.inventory.serial}: power {self.power}, "
            f"{len(self.inventory.devices)} PCIe devices, {len(self.sel)} SEL entries, "
            f"counters {self.counters.as_dict()}."
        )
