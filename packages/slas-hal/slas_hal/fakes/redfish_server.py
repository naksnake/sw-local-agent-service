"""A fake Redfish service over a `FakeTarget`, answering DMTF-standard URIs in memory.

The real driver is tested against this exactly as it would talk to a BMC: Basic auth, the
service root, Systems / Managers / Chassis collections, the ComputerSystem.Reset action, a
paged SEL with `Members@odata.nextLink`, PCIe devices as links to follow. Faults can be
planted per path: a status code, a cut-off JSON body, or a SEL count that lies.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from slas_hal.fakes.bmc import FakeTarget, fixture_text
from slas_hal.http import HttpRequest, HttpResponse
from slas_hal.model import PCIE_GEN_GTS, PcieDevice

GEN_BY_GTS = {gts: gen for gen, gts in PCIE_GEN_GTS.items()}

#: Redfish ResetType → the fake target's power action.
RESET_TYPES = {
    "On": "on",
    "ForceOff": "force_off",
    "GracefulShutdown": "off",
    "GracefulRestart": "graceful_restart",
    "ForceRestart": "graceful_restart",
    "PowerCycle": "graceful_restart",
}


class Clock(Protocol):
    def now(self) -> datetime: ...


def _json(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status=status, body=json.dumps(payload), headers={"content-type": "application/json"}
    )


def device_id(device: PcieDevice) -> str:
    return "dev-" + device.bdf.replace(":", "-").replace(".", "-")


class FakeRedfishServer:
    def __init__(
        self,
        target: FakeTarget,
        *,
        user: str,
        password: str,
        clock: Clock,
        sel_page_size: int = 2,
        expand_devices: bool = False,
    ) -> None:
        self.target = target
        self.user = user
        self.password = password
        self.clock = clock
        self.sel_page_size = sel_page_size
        self.expand_devices = expand_devices
        self.requests: list[tuple[str, str]] = []
        self.reset_types: list[str] = []
        self.fail_next: dict[str, int] = {}
        self.malformed_next: set[str] = set()
        self.lie_about_sel_count = False
        self.unauthorised_attempts = 0

    # --- routing --------------------------------------------------------------------------

    def __call__(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        self.requests.append((request.method, path))
        expected = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        if request.headers.get("Authorization") != f"Basic {expected}":
            self.unauthorised_attempts += 1
            return _json(401, {"error": {"message": "Unauthorized"}})
        for prefix, status in list(self.fail_next.items()):
            if path.startswith(prefix):
                del self.fail_next[prefix]
                return _json(status, {"error": {"message": f"planted {status}"}})
        if path in self.malformed_next:
            self.malformed_next.discard(path)
            return HttpResponse(
                status=200, body='{"@odata.id": "' + path + '", "Members": [{"Id": "1", "Mess'
            )
        if request.method == "POST":
            return self._post(path, request.body or "")
        return self._get(path, query)

    def _get(self, path: str, query: dict[str, list[str]]) -> HttpResponse:
        if path == "/redfish/v1":
            return _json(
                200,
                {
                    "@odata.id": "/redfish/v1/",
                    "RedfishVersion": "1.15.0",
                    "Systems": {"@odata.id": "/redfish/v1/Systems"},
                    "Managers": {"@odata.id": "/redfish/v1/Managers"},
                    "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
                },
            )
        if path == "/redfish/v1/Systems":
            return _json(200, self._collection(path, ["/redfish/v1/Systems/1"]))
        if path == "/redfish/v1/Systems/1":
            system = json.loads(fixture_text("system.json"))
            system["PowerState"] = "On" if self.target.power == "on" else "Off"
            system["Actions"] = {
                "#ComputerSystem.Reset": {
                    "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                    "ResetType@Redfish.AllowableValues": list(RESET_TYPES),
                }
            }
            system["LogServices"] = {"@odata.id": "/redfish/v1/Systems/1/LogServices"}
            return _json(200, system)
        if path == "/redfish/v1/Managers":
            return _json(200, self._collection(path, ["/redfish/v1/Managers/bmc"]))
        if path == "/redfish/v1/Managers/bmc":
            return _json(200, json.loads(fixture_text("manager.json")))
        if path == "/redfish/v1/Chassis":
            return _json(200, self._collection(path, ["/redfish/v1/Chassis/1"]))
        if path == "/redfish/v1/Chassis/1":
            return _json(
                200,
                {
                    "@odata.id": path,
                    "Id": "1",
                    "PCIeDevices": {"@odata.id": "/redfish/v1/Chassis/1/PCIeDevices"},
                },
            )
        if path == "/redfish/v1/Chassis/1/PCIeDevices":
            devices = self.target.inventory.devices
            members = [
                self._device(d) if self.expand_devices else {"@odata.id": f"{path}/{device_id(d)}"}
                for d in devices
            ]
            return _json(
                200,
                {"@odata.id": path, "Members@odata.count": len(members), "Members": members},
            )
        if path.startswith("/redfish/v1/Chassis/1/PCIeDevices/"):
            wanted = path.rsplit("/", 1)[1]
            for device in self.target.inventory.devices:
                if device_id(device) == wanted:
                    return _json(200, self._device(device))
            return _json(404, {"error": {"message": f"{wanted} not found"}})
        if path == "/redfish/v1/Systems/1/LogServices/SEL/Entries":
            return self._sel_page(path, query)
        return _json(404, {"error": {"message": f"{path} not found"}})

    def _post(self, path: str, body: str) -> HttpResponse:
        if path != "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset":
            return _json(405, {"error": {"message": "POST not allowed"}})
        try:
            reset_type = str(json.loads(body or "{}").get("ResetType", ""))
        except ValueError:
            return _json(400, {"error": {"message": "body is not JSON"}})
        action = RESET_TYPES.get(reset_type)
        if action is None:
            return _json(400, {"error": {"message": f"ResetType {reset_type!r} is not allowed"}})
        self.reset_types.append(reset_type)
        self.target.apply_power(action, self.clock.now())  # type: ignore[arg-type]
        return HttpResponse(status=204)

    # --- payload builders -----------------------------------------------------------------

    @staticmethod
    def _collection(path: str, members: list[str]) -> dict[str, object]:
        return {
            "@odata.id": path,
            "Members@odata.count": len(members),
            "Members": [{"@odata.id": m} for m in members],
        }

    def _device(self, device: PcieDevice) -> dict[str, object]:
        return {
            "@odata.id": f"/redfish/v1/Chassis/1/PCIeDevices/{device_id(device)}",
            "Id": device_id(device),
            "Name": device.name,
            "FirmwareVersion": device.firmware,
            "PCIeInterface": {
                "LanesInUse": device.width,
                "MaxLanes": device.max_width,
                "PCIeType": GEN_BY_GTS.get(device.speed_gts, "Gen5"),
                "MaxPCIeType": GEN_BY_GTS.get(device.max_speed_gts, "Gen5"),
            },
            "Oem": {
                "Slas": {
                    "BDF": device.bdf,
                    "VendorId": device.vendor_id,
                    "DeviceId": device.device_id,
                    "ClassCode": device.class_code,
                }
            },
        }

    def _sel_page(self, path: str, query: dict[str, list[str]]) -> HttpResponse:
        entries = self.target.sel
        skip = int(query.get("$skip", ["0"])[0])
        page = entries[skip : skip + self.sel_page_size]
        payload: dict[str, object] = {
            "@odata.id": path,
            "Members@odata.count": len(entries) + (1 if self.lie_about_sel_count else 0),
            "Members": [
                {
                    "Id": str(e.id),
                    "Created": e.created,
                    "Severity": e.severity,
                    "Message": e.message,
                    "SensorType": e.sensor,
                    "EntryType": "SEL",
                }
                for e in page
            ],
        }
        if skip + self.sel_page_size < len(entries):
            payload["Members@odata.nextLink"] = f"{path}?$skip={skip + self.sel_page_size}"
        return _json(200, payload)
