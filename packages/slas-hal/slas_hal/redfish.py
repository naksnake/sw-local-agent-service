"""Redfish payloads → HAL models. Tolerant of extra fields, strict about what it needs.

A BMC answers with JSON that is sometimes malformed or cut short; each case becomes a
three-part `HalError` rather than a traceback or a half-filled model. The fixtures under
`fakes/fixtures/` are recorded answers in exactly these shapes, ugly ones included.
"""

from __future__ import annotations

import json
from typing import Any

from slas_hal.model import PCIE_GEN_GTS, Inventory, PcieDevice, PowerState, SelEntry, SelSeverity
from slas_schemas.errors import ThreePartMessage


class HalError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def load_payload(text: str, *, what: str, target: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise HalError(
            ThreePartMessage(
                f"The BMC of {target} answered the {what} request with something that is not JSON.",
                f"The answer was cut short or corrupted ({exc}).",
                "Retry once; if it repeats, check the BMC's health and firmware version.",
            )
        ) from exc
    if not isinstance(data, dict):
        raise HalError(
            ThreePartMessage(
                f"The BMC of {target} answered the {what} request with a JSON "
                f"{type(data).__name__}, not an object.",
                "Redfish resources are JSON objects.",
                "Check the BMC's Redfish implementation; the platform cannot use this answer.",
            )
        )
    return data


def _members(data: dict[str, Any], *, what: str, target: str) -> list[dict[str, Any]]:
    members = data.get("Members")
    count = data.get("Members@odata.count")
    if not isinstance(members, list):
        raise HalError(
            ThreePartMessage(
                f"The {what} answer from {target} is incomplete: it has no Members.",
                f"The BMC reported {count if count is not None else 'an unknown number of'} "
                "entries but sent none; the collection was probably truncated.",
                "Retry once; if it repeats, read the collection page by page or clear the SEL.",
            )
        )
    if isinstance(count, int) and count != len(members):
        raise HalError(
            ThreePartMessage(
                f"The {what} answer from {target} is incomplete: "
                f"{len(members)} of {count} entries arrived.",
                "The BMC truncated the collection.",
                "Retry once; if it repeats, read the collection page by page or clear the SEL.",
            )
        )
    return [member for member in members if isinstance(member, dict)]


def parse_power_state(data: dict[str, Any]) -> PowerState:
    state = str(data.get("PowerState", "")).lower()
    if state == "on":
        return "on"
    if state in ("off", "poweringoff"):
        return "off"
    return "unknown"


def parse_sel(data: dict[str, Any], *, target: str) -> list[SelEntry]:
    entries: list[SelEntry] = []
    for member in _members(data, what="SEL", target=target):
        severity_raw = str(member.get("Severity", "OK"))
        severity: SelSeverity = "Warning"
        if severity_raw == "OK":
            severity = "OK"
        elif severity_raw == "Critical":
            severity = "Critical"
        try:
            entry_id = int(str(member.get("Id", "0")))
        except ValueError:
            entry_id = 0
        entries.append(
            SelEntry(
                id=entry_id,
                created=str(member.get("Created", "")),
                severity=severity,
                message=str(member.get("Message", "")).strip() or "(no message)",
                sensor=str(member["SensorType"]) if member.get("SensorType") else None,
            )
        )
    return entries


def _speed(pcie_type: object) -> float | None:
    return PCIE_GEN_GTS.get(str(pcie_type)) if pcie_type else None


def parse_pcie_devices(data: dict[str, Any], *, target: str) -> list[PcieDevice]:
    devices: list[PcieDevice] = []
    for member in _members(data, what="PCIe device", target=target):
        interface = member.get("PCIeInterface") or {}
        oem = (member.get("Oem") or {}).get("Slas") or {}
        bdf = str(oem.get("BDF", "")).lower()
        width = interface.get("LanesInUse")
        speed = _speed(interface.get("PCIeType"))
        if not bdf or not isinstance(width, int) or speed is None:
            raise HalError(
                ThreePartMessage(
                    f"A PCIe device of {target} ({member.get('Id', '?')}) has no usable "
                    "link state.",
                    "The BMC did not report its BDF, LanesInUse or PCIeType.",
                    "Check the BMC's PCIe inventory support; VERIFY needs width and speed "
                    "per device.",
                )
            )
        devices.append(
            PcieDevice(
                bdf=bdf,
                name=str(member.get("Name") or member.get("Id") or bdf),
                vendor_id=str(oem.get("VendorId", "")),
                device_id=str(oem.get("DeviceId", "")),
                class_code=str(oem.get("ClassCode", "")),
                width=width,
                speed_gts=speed,
                max_width=int(interface.get("MaxLanes") or width),
                max_speed_gts=_speed(interface.get("MaxPCIeType")) or speed,
                firmware=str(member["FirmwareVersion"]) if member.get("FirmwareVersion") else None,
            )
        )
    return devices


def parse_inventory(
    system: dict[str, Any], manager: dict[str, Any], devices: list[PcieDevice]
) -> Inventory:
    return Inventory(
        model=str(system.get("Model") or "unknown"),
        serial=str(system.get("SerialNumber") or "unknown"),
        bios_version=str(system.get("BiosVersion") or "unknown"),
        bmc_version=str(manager.get("FirmwareVersion") or "unknown"),
        cpus=int((system.get("ProcessorSummary") or {}).get("Count") or 0),
        memory_gib=int((system.get("MemorySummary") or {}).get("TotalSystemMemoryGiB") or 0),
        devices=devices,
    )
