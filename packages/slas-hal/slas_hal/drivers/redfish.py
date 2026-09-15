"""The Redfish driver: DMTF-standard URIs discovered from the service root, quirks applied.

    GET  /redfish/v1/                        → Systems, Managers, Chassis
    GET  …/Systems/<id>                      → PowerState, model, serial, BIOS, reset action
    POST …/Systems/<id>/Actions/ComputerSystem.Reset {"ResetType": …}
    GET  …/Managers/<id>                     → Manufacturer, FirmwareVersion (→ quirks)
    GET  …/Chassis/<id>/PCIeDevices (+ each member)      → link width, speed, firmware
    GET  …/Systems/<id>/LogServices/SEL/Entries (paged)   → SEL

Credentials come from the resolver per request and live only in the Authorization header
of that request. The audit trail is method, path, status and duration.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

from pydantic import Field

from slas_hal.credentials import CredentialResolver
from slas_hal.http import HttpClient, HttpError, HttpRequest, basic_auth_header
from slas_hal.model import Inventory, PcieDevice, PowerAction, PowerState, SelEntry
from slas_hal.quirks import QuirkSet, QuirkTable, default_quirks, match_quirks
from slas_hal.redfish import (
    HalError,
    load_payload,
    parse_inventory,
    parse_pcie_devices,
    parse_power_state,
    parse_sel,
)
from slas_hal.targets import TargetRecord
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage


class Endpoints(SlasModel):
    system: str
    manager: str
    chassis: str
    reset_action: str
    sel_entries: str
    pcie_devices: str


class RedfishAudit(SlasModel):
    method: str
    path: str
    status: int
    ms: int = Field(ge=0)


def _dig(data: dict[str, Any], dotted: str) -> object:
    """Follow a dotted path such as `Oem.Slas.BDF` (keys themselves contain no dots)."""
    node: object = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _link(data: dict[str, Any], key: str) -> str | None:
    """`data[key]["@odata.id"]` when present."""
    node = data.get(key)
    link = node.get("@odata.id") if isinstance(node, dict) else None
    return link if isinstance(link, str) else None


class RedfishClient:
    def __init__(
        self,
        record: TargetRecord,
        *,
        http: HttpClient,
        resolver: CredentialResolver,
        quirk_table: QuirkTable | None = None,
        timer: Callable[[], float] = time.monotonic,
        timeout_s: float = 20.0,
    ) -> None:
        self.record = record
        self.http = http
        self.resolver = resolver
        self.quirk_table = quirk_table or default_quirks()
        self.timer = timer
        self.timeout_s = timeout_s
        self.audit: list[RedfishAudit] = []
        self._endpoints: Endpoints | None = None
        self._quirks: QuirkSet | None = None
        self._manufacturer = record.vendor_hint or ""
        self._firmware = record.firmware_hint or ""

    # --- transport ----------------------------------------------------------------------

    def _url(self, path: str) -> str:
        bmc = self.record.bmc
        port = "" if bmc.https_port == 443 else f":{bmc.https_port}"
        return f"https://{bmc.host}{port}{path}"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> str:
        alias = self.record.alias
        headers = dict(
            basic_auth_header(
                self.record.bmc.user, self.resolver.resolve(self.record.bmc.password_ref)
            )
        )
        started = self.timer()
        try:
            response = self.http.send(
                HttpRequest(
                    method=method,
                    url=self._url(path),
                    headers=headers,
                    body=json.dumps(body) if body is not None else None,
                    timeout_s=self.timeout_s,
                )
            )
        except HttpError as exc:
            self.audit.append(RedfishAudit(method=method, path=path.split("?")[0], status=0, ms=0))
            raise HalError(
                ThreePartMessage(
                    f"The BMC of {alias} at {self.record.bmc.host} did not answer.",
                    f"{exc} — the lab VLAN, the address on the target record, or the BMC itself.",
                    "Check that the BMC answers on the lab VLAN, then retry; a BMC that stays "
                    "silent needs a person at the rack.",
                )
            ) from None
        elapsed = int((self.timer() - started) * 1000)
        self.audit.append(
            RedfishAudit(method=method, path=path.split("?")[0], status=response.status, ms=elapsed)
        )
        if response.status == 401:
            raise HalError(
                ThreePartMessage(
                    f"The BMC of {alias} refused the credentials.",
                    f"User {self.record.bmc.user} with the credential "
                    f"{self.record.bmc.password_ref} was not accepted.",
                    "Check the BMC user and the credential reference on the target record.",
                )
            )
        if response.status >= 400:
            raise HalError(
                ThreePartMessage(
                    f"The BMC of {alias} answered {response.status} to {method} {path}.",
                    "The BMC rejected the request or is not healthy.",
                    "Retry once; if it repeats, check the BMC's health and the quirk table for "
                    "this vendor.",
                )
            )
        return response.body

    def _get(self, path: str) -> dict[str, Any]:
        return load_payload(self._request("GET", path), what=path, target=self.record.alias)

    # --- discovery ---------------------------------------------------------------------------

    def endpoints(self) -> Endpoints:
        if self._endpoints is not None:
            return self._endpoints
        alias = self.record.alias
        root = self._get("/redfish/v1/")

        def first_member(collection_key: str) -> str:
            link = _link(root, collection_key)
            if link is None:
                raise HalError(
                    ThreePartMessage(
                        f"The BMC of {alias} lists no {collection_key} collection.",
                        "The service root has no such link; this is not a usable Redfish service.",
                        "Check the BMC firmware; the platform needs Systems, Managers and Chassis.",
                    )
                )
            collection = self._get(link)
            members = collection.get("Members")
            if not isinstance(members, list) or not members:
                raise HalError(
                    ThreePartMessage(
                        f"The BMC of {alias} has an empty {collection_key} collection.",
                        "Redfish reported no members.",
                        "Check the BMC; a server exposes at least one system, manager and chassis.",
                    )
                )
            member = members[0].get("@odata.id") if isinstance(members[0], dict) else None
            if not isinstance(member, str):
                raise HalError(
                    ThreePartMessage(
                        f"The BMC of {alias} lists a {collection_key} member without a link.",
                        "The collection member has no @odata.id.",
                        "Check the BMC's Redfish implementation.",
                    )
                )
            return member

        system_path = first_member("Systems")
        manager_path = first_member("Managers")
        chassis_path = first_member("Chassis")
        system = self._get(system_path)
        manager = self._get(manager_path)
        self._manufacturer = str(
            manager.get("Manufacturer") or system.get("Manufacturer") or self._manufacturer
        )
        self._firmware = str(manager.get("FirmwareVersion") or self._firmware)
        # Quirks are matched once, on what the BMC says about itself, never on the hint alone.
        self._quirks = match_quirks(self.quirk_table, self._manufacturer, self._firmware)
        quirks = self._quirks
        actions = system.get("Actions")
        reset_action = actions.get("#ComputerSystem.Reset") if isinstance(actions, dict) else None
        reset = reset_action.get("target") if isinstance(reset_action, dict) else None
        pcie_root = system_path if quirks.pcie_devices_under == "systems" else chassis_path
        self._endpoints = Endpoints(
            system=system_path,
            manager=manager_path,
            chassis=chassis_path,
            reset_action=reset
            if isinstance(reset, str)
            else f"{system_path}/Actions/ComputerSystem.Reset",
            sel_entries=f"{system_path}/LogServices/SEL/Entries",
            pcie_devices=f"{pcie_root}/PCIeDevices",
        )
        return self._endpoints

    def quirks(self) -> QuirkSet:
        if self._quirks is None:
            self.endpoints()  # discovery reads the vendor and matches the table
        if self._quirks is None:  # pragma: no cover — endpoints() always sets it
            self._quirks = match_quirks(self.quirk_table, self._manufacturer, self._firmware)
        return self._quirks

    # --- reads --------------------------------------------------------------------------------

    def system(self) -> dict[str, Any]:
        return self._get(self.endpoints().system)

    def manager(self) -> dict[str, Any]:
        return self._get(self.endpoints().manager)

    def power_state(self) -> PowerState:
        return parse_power_state(self.system())

    def sel(self) -> list[SelEntry]:
        path: str | None = self.endpoints().sel_entries
        members: list[Any] = []
        count: object = None
        pages = 0
        while path is not None and pages < 1000:
            page = self._get(path)
            pages += 1
            count = page.get("Members@odata.count", count)
            got = page.get("Members")
            if isinstance(got, list):
                members.extend(got)
            next_link = page.get("Members@odata.nextLink")
            path = next_link if isinstance(next_link, str) and next_link != path else None
        merged: dict[str, Any] = {"Members": members}
        if isinstance(count, int):
            merged["Members@odata.count"] = count
        if self.quirks().sel_ids_are_hex:
            for member in members:
                if isinstance(member, dict) and isinstance(member.get("Id"), str):
                    with contextlib.suppress(ValueError):
                        member["Id"] = str(int(member["Id"], 16))
        return parse_sel(merged, target=self.record.alias)

    def pcie_devices(self) -> list[PcieDevice]:
        collection = self._get(self.endpoints().pcie_devices)
        raw = collection.get("Members")
        members: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            if "PCIeInterface" not in item and isinstance(item.get("@odata.id"), str):
                item = self._get(item["@odata.id"])
            bdf = _dig(item, self.quirks().bdf_path)
            if isinstance(bdf, str):
                item.setdefault("Oem", {}).setdefault("Slas", {})["BDF"] = bdf
            members.append(item)
        payload = {"Members@odata.count": len(members), "Members": members}
        return parse_pcie_devices(payload, target=self.record.alias)

    def inventory(self, devices: list[PcieDevice] | None = None) -> Inventory:
        system = self.system()
        manager = self.manager()
        return parse_inventory(
            system, manager, devices if devices is not None else self.pcie_devices()
        )

    # --- power ---------------------------------------------------------------------------------

    def reset(self, action: PowerAction) -> str:
        """POST the reset; returns the ResetType sent. The caller has checked the arming gate."""
        reset_type = self.quirks().reset_type.get(action)
        if reset_type is None:
            raise HalError(
                ThreePartMessage(
                    f"Redfish has no reset type for the action {action!r} on {self.record.alias}.",
                    "AC cycles go through the PDU driver, not the BMC.",
                    "Configure a PDU outlet on the target record for AC cycles.",
                )
            )
        self._request("POST", self.endpoints().reset_action, {"ResetType": reset_type})
        return reset_type

    def vendor(self) -> tuple[str, str]:
        self.endpoints()
        return self._manufacturer, self._firmware
