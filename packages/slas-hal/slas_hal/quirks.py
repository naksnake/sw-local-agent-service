"""BMC quirk shims keyed by vendor and firmware (CLAUDE.md §15 open decision 4).

A BMC fleet is heterogeneous: one generation pages the SEL, another puts PCIe devices under
Systems instead of Chassis, a third needs IPMI for power because its Redfish reset returns
500. The Redfish driver asks `match_quirks(manufacturer, firmware)` once and reads the merged
`QuirkSet`; the table is rendered to `config/bmc-quirks.yaml` and a test keeps them in step.

Only the fixture vendor and the defaults are filled in. Every real vendor entry needs a
recorded answer from that BMC first (CLAUDE.md §0.4): no URI, field path or behaviour here
is guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import Field, ValidationError

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage

PcieLocation = Literal["chassis", "systems"]


class QuirkSet(SlasModel):
    """The merged behaviour the driver reads. Every field has the DMTF-standard default."""

    #: Follow `Members@odata.nextLink` and pass `$skip`/`$top`; None = no paging needed.
    sel_page_size: int | None = Field(default=None, ge=1, le=1000)
    #: Where PCIeDevices live: `/redfish/v1/Chassis/<id>/PCIeDevices` (standard) or Systems.
    pcie_devices_under: PcieLocation = "chassis"
    #: Dotted path to the BDF inside a PCIeDevice member; the fixture BMC uses Oem.Slas.BDF.
    bdf_path: str = "Oem.Slas.BDF"
    #: Redfish ResetType per HAL action; a vendor that rejects GracefulShutdown maps it to ForceOff.
    reset_type: dict[str, str] = Field(
        default_factory=lambda: {
            "on": "On",
            "off": "GracefulShutdown",
            "force_off": "ForceOff",
            "graceful_restart": "GracefulRestart",
        }
    )
    #: Use ipmitool for power actions instead of the Redfish reset action.
    use_ipmi_for_power: bool = False
    #: Seconds to wait after a power-off before the BMC reports the true state.
    power_off_settle_s: int = Field(default=0, ge=0, le=300)
    #: The BMC reports LanesInUse as the maximum, so link width must come from lspci in-band.
    lanes_in_use_unreliable: bool = False
    #: SEL entry ids are hex strings ("0x1a") rather than decimal.
    sel_ids_are_hex: bool = False
    applied: list[str] = Field(default_factory=list)


class Quirk(SlasModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    #: Regex over the Manager/System Manufacturer field; empty matches every vendor.
    manufacturer: str = ""
    #: Inclusive firmware range as dotted versions; None = any.
    firmware_min: str | None = None
    firmware_max: str | None = None
    note: str = Field(min_length=1)
    set: dict[str, object] = Field(default_factory=dict)

    def matches(self, manufacturer: str, firmware: str) -> bool:
        if self.manufacturer and not re.search(self.manufacturer, manufacturer, re.IGNORECASE):
            return False
        version = _version_key(firmware)
        if self.firmware_min is not None and version < _version_key(self.firmware_min):
            return False
        return not (self.firmware_max is not None and version > _version_key(self.firmware_max))


class QuirkTable(SlasModel):
    version: int = 1
    quirks: list[Quirk] = Field(default_factory=list)


class QuirkError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def _version_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) or (0,)


def quirks_from_mapping(data: object, *, source: str = "<memory>") -> QuirkTable:
    try:
        return QuirkTable.model_validate(data)
    except ValidationError as exc:
        raise QuirkError(
            ThreePartMessage(
                f"The BMC quirk table in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; each quirk needs an id, a note and the fields it sets.",
            )
        ) from exc


def match_quirks(table: QuirkTable, manufacturer: str, firmware: str) -> QuirkSet:
    """Later matching quirks override earlier ones; `applied` lists them for the audit."""
    merged: dict[str, object] = {}
    applied: list[str] = []
    for quirk in table.quirks:
        if quirk.matches(manufacturer, firmware):
            merged.update(quirk.set)
            applied.append(quirk.id)
    try:
        return QuirkSet.model_validate({**merged, "applied": applied})
    except ValidationError as exc:
        raise QuirkError(
            ThreePartMessage(
                f"The quirks matched for {manufacturer or 'an unknown vendor'} set an "
                "unknown or invalid field.",
                validation_sentence(exc),
                "Fix config/bmc-quirks.yaml; the allowed fields are those of QuirkSet.",
            )
        ) from exc


DEFAULT_QUIRKS: Final[dict[str, object]] = {
    "version": 1,
    "quirks": [
        {
            "id": "dmtf-defaults",
            "manufacturer": "",
            "note": "Every BMC starts from the DMTF-standard behaviour; later entries override.",
            "set": {},
        },
        {
            "id": "slas-fixture",
            "manufacturer": r"SW Local Agent Service test fixture",
            "note": "The recorded fixture BMC: BDF under Oem.Slas, SEL served in pages of 2.",
            "set": {"bdf_path": "Oem.Slas.BDF", "sel_page_size": 2},
        },
        # TODO(SLAS-HAL): add one entry per real BMC generation after recording its answers
        # (manufacturer string, firmware range, and only the fields its recording proves).
    ],
}

QUIRKS_FILE_HEADER: Final = (
    "BMC quirk shims for SW Local Agent Service (CLAUDE.md §15 open decision 4).\n"
    "Rendered from slas_hal.quirks.DEFAULT_QUIRKS; a unit test keeps file and code in step.\n"
    "Matching is by Manufacturer regex and inclusive firmware range; later entries override\n"
    "earlier ones. Add a real vendor only from a recorded answer, never from memory."
)


def default_quirks() -> QuirkTable:
    return quirks_from_mapping(DEFAULT_QUIRKS, source="config/bmc-quirks.yaml")


def render_quirks_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    table = quirks_from_mapping(data)
    lines = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    lines.append(f"version: {table.version}")
    lines.append("quirks:")
    for quirk in table.quirks:
        lines.append(f"  - id: {quirk.id}")
        lines.append(f"    manufacturer: {json.dumps(quirk.manufacturer, ensure_ascii=False)}")
        if quirk.firmware_min is not None:
            lines.append(f"    firmware_min: {json.dumps(quirk.firmware_min)}")
        if quirk.firmware_max is not None:
            lines.append(f"    firmware_max: {json.dumps(quirk.firmware_max)}")
        lines.append(f"    note: {json.dumps(quirk.note, ensure_ascii=False)}")
        if quirk.set:
            lines.append("    set:")
            for key, value in quirk.set.items():
                lines.append(f"      {key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append("    set: {}")
    return "\n".join(lines) + "\n"
