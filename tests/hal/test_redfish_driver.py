"""The Redfish driver against the fake Redfish service: discovery, paging, links, quirks,
auth failures, planted faults, and an audit trail that never carries a header."""

from __future__ import annotations

import pytest

from slas_hal.credentials import FakeCredentialResolver
from slas_hal.drivers.redfish import RedfishClient
from slas_hal.fakes.bmc import FakeTarget, Plant
from slas_hal.fakes.redfish_server import FakeRedfishServer
from slas_hal.http import FakeHttpClient, HttpError, HttpRequest, HttpResponse
from slas_hal.quirks import QuirkTable, default_quirks, match_quirks, quirks_from_mapping
from slas_hal.redfish import HalError
from slas_hal.targets import BmcAccess, TargetRecord
from slas_kernel.clock import FakeClock

ALIAS = "lab-gx8-01"
PASSWORD = "Bmc-Passw0rd-XYZ!"
GPU3 = "0000:8a:00.0"


def record(**bmc: object) -> TargetRecord:
    return TargetRecord(
        alias=ALIAS,
        bmc=BmcAccess(
            host="10.20.30.40",
            user="slas-validation",
            password_ref="env:LAB_GX8_01_BMC_PASSWORD",
            **bmc,
        ),
    )


class Rig:
    def __init__(
        self, *plants: Plant, quirk_table: QuirkTable | None = None, **kwargs: object
    ) -> None:
        self.target = FakeTarget(ALIAS, plants=plants)
        self.clock = FakeClock()
        self.server = FakeRedfishServer(
            self.target,
            user="slas-validation",
            password=PASSWORD,
            clock=self.clock,
            **kwargs,  # type: ignore[arg-type]
        )
        self.http = FakeHttpClient(self.server)
        self.resolver = FakeCredentialResolver({"env:LAB_GX8_01_BMC_PASSWORD": PASSWORD})
        self.client = RedfishClient(
            record(),
            http=self.http,
            resolver=self.resolver,
            quirk_table=quirk_table,
            timer=lambda: 0.0,
        )


def test_discovery_finds_the_standard_uris_and_applies_the_vendor_quirks() -> None:
    rig = Rig()
    endpoints = rig.client.endpoints()
    assert endpoints.system == "/redfish/v1/Systems/1"
    assert endpoints.manager == "/redfish/v1/Managers/bmc"
    assert endpoints.reset_action == "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
    assert endpoints.sel_entries == "/redfish/v1/Systems/1/LogServices/SEL/Entries"
    assert endpoints.pcie_devices == "/redfish/v1/Chassis/1/PCIeDevices"
    assert rig.client.vendor() == ("SW Local Agent Service test fixture", "1.12.0")
    quirks = rig.client.quirks()
    assert quirks.applied == ["dmtf-defaults", "slas-fixture"]
    assert quirks.sel_page_size == 2 and quirks.bdf_path == "Oem.Slas.BDF"
    assert [path for _, path in rig.http.requests][:2] == [
        "https://10.20.30.40/redfish/v1/",
        "https://10.20.30.40/redfish/v1/Systems",
    ]
    assert rig.client.endpoints() is endpoints, "discovered once"


def test_power_state_sel_paging_and_device_links() -> None:
    rig = Rig()
    assert rig.client.power_state() == "on"
    sel = rig.client.sel()
    assert [e.id for e in sel] == [1, 2, 3]
    paged = [p for _, p in rig.server.requests if "SEL/Entries" in p]
    assert len(paged) == 2, "3 entries in pages of 2: the nextLink was followed"
    assert any("$skip=2" in url for _, url in rig.http.requests)
    devices = rig.client.pcie_devices()
    assert len(devices) == 11
    gpu3 = next(d for d in devices if d.bdf == GPU3)
    assert (gpu3.name, gpu3.width, gpu3.speed_gts, gpu3.firmware) == (
        "NVIDIA H100 SXM",
        16,
        32.0,
        "96.00.5E",
    )
    member_gets = [
        p for _, p in rig.server.requests if p.startswith("/redfish/v1/Chassis/1/PCIeDevices/")
    ]
    assert len(member_gets) == 11, "each member link is followed"
    inventory = rig.client.inventory(devices)
    assert (inventory.model, inventory.serial, inventory.bmc_version) == (
        "SLAS-GX8",
        "SN-GX8-0001",
        "1.12.0",
    )
    assert rig.client.inventory().firmware()["BMC"] == "1.12.0"


def test_expanded_collections_need_no_member_requests() -> None:
    rig = Rig(expand_devices=True)
    assert len(rig.client.pcie_devices()) == 11
    assert not any(
        p.startswith("/redfish/v1/Chassis/1/PCIeDevices/") for _, p in rig.server.requests
    )


def test_reset_maps_actions_to_reset_types_and_changes_the_target() -> None:
    rig = Rig(Plant(at_cycle=1, kind="pcie_width", bdf=GPU3, width=8))
    assert rig.client.reset("off") == "GracefulShutdown"
    assert rig.target.power == "off" and rig.client.power_state() == "off"
    assert rig.client.reset("on") == "On"
    assert rig.client.reset("force_off") == "ForceOff"
    assert rig.client.reset("graceful_restart") == "GracefulRestart"
    assert rig.server.reset_types == ["GracefulShutdown", "On", "ForceOff", "GracefulRestart"]
    gpu3 = next(d for d in rig.client.pcie_devices() if d.bdf == GPU3)
    assert gpu3.width == 8, "the planted degradation is visible through Redfish"
    with pytest.raises(HalError) as info:
        rig.client.reset("ac_cycle")
    assert info.value.message.likely_cause == "AC cycles go through the PDU driver, not the BMC."


def test_credentials_travel_in_the_header_and_never_in_the_audit() -> None:
    rig = Rig()
    rig.client.power_state()
    assert rig.resolver.asked and all(
        r == "env:LAB_GX8_01_BMC_PASSWORD" for r in rig.resolver.asked
    )
    assert rig.http.auth_headers_seen and all(
        h.startswith("Basic ") for h in rig.http.auth_headers_seen
    )
    dumped = " ".join(f"{a.method} {a.path} {a.status} {a.ms}" for a in rig.client.audit)
    assert PASSWORD not in dumped and "Basic" not in dumped and "Authorization" not in dumped
    assert rig.client.audit[0].model_dump() == {
        "method": "GET",
        "path": "/redfish/v1/",
        "status": 200,
        "ms": 0,
    }
    assert rig.server.unauthorised_attempts == 0


def test_wrong_credentials_are_a_sentence_not_a_401() -> None:
    rig = Rig()
    rig.client.resolver = FakeCredentialResolver({"env:LAB_GX8_01_BMC_PASSWORD": "wrong"})
    with pytest.raises(HalError) as info:
        rig.client.power_state()
    assert info.value.message.what_happened == f"The BMC of {ALIAS} refused the credentials."
    assert "env:LAB_GX8_01_BMC_PASSWORD" in info.value.message.likely_cause
    assert "wrong" not in info.value.message.render()
    assert rig.server.unauthorised_attempts == 1


def test_unreachable_bmc_planted_errors_and_cut_off_json() -> None:
    rig = Rig()
    rig.http.unreachable = True
    with pytest.raises(HalError) as info:
        rig.client.power_state()
    assert info.value.message.what_happened == f"The BMC of {ALIAS} at 10.20.30.40 did not answer."
    assert "No route to host" in info.value.message.likely_cause
    assert rig.client.audit[-1].status == 0
    rig.http.unreachable = False

    rig.server.fail_next["/redfish/v1/Systems/1/LogServices/SEL"] = 500
    with pytest.raises(HalError) as info:
        rig.client.sel()
    assert info.value.message.what_happened == (
        f"The BMC of {ALIAS} answered 500 to GET /redfish/v1/Systems/1/LogServices/SEL/Entries."
    )
    assert rig.client.sel(), "the planted failure was for one request only"

    rig.server.malformed_next.add("/redfish/v1/Systems/1/LogServices/SEL/Entries")
    with pytest.raises(HalError) as info:
        rig.client.sel()
    assert "something that is not JSON" in info.value.message.what_happened

    rig.server.lie_about_sel_count = True
    with pytest.raises(HalError) as info:
        rig.client.sel()
    assert "is incomplete: 3 of 4 entries arrived" in info.value.message.what_happened


def test_a_service_root_without_collections_is_refused() -> None:
    def broken(request: HttpRequest) -> HttpResponse:
        return HttpResponse(status=200, body='{"@odata.id": "/redfish/v1/"}')

    client = RedfishClient(
        record(),
        http=FakeHttpClient(broken),
        resolver=FakeCredentialResolver({"env:LAB_GX8_01_BMC_PASSWORD": PASSWORD}),
    )
    with pytest.raises(HalError) as info:
        client.endpoints()
    assert info.value.message.what_happened == f"The BMC of {ALIAS} lists no Systems collection."


def test_quirk_table_matching_overrides_and_hex_sel_ids() -> None:
    table = quirks_from_mapping(
        {
            "quirks": [
                {"id": "base", "note": "defaults", "set": {}},
                {
                    "id": "acme-old",
                    "manufacturer": "acme",
                    "firmware_max": "2.9.99",
                    "note": "old ACME firmware pages the SEL and rejects graceful shutdown",
                    "set": {
                        "sel_page_size": 50,
                        "reset_type": {"off": "ForceOff", "on": "On"},
                        "use_ipmi_for_power": False,
                        "sel_ids_are_hex": True,
                    },
                },
                {
                    "id": "acme-3",
                    "manufacturer": "acme",
                    "firmware_min": "3.0",
                    "note": "ACME 3.x: PCIe devices under Systems",
                    "set": {"pcie_devices_under": "systems", "lanes_in_use_unreliable": True},
                },
                {
                    "id": "fixture-hex",
                    "manufacturer": "test fixture",
                    "firmware_min": "1.0",
                    "firmware_max": "1.99",
                    "note": "the fixture BMC, when it reports SEL ids in hex",
                    "set": {"sel_ids_are_hex": True},
                },
            ]
        }
    )
    old = match_quirks(table, "ACME Inc.", "2.4.1")
    assert old.applied == ["base", "acme-old"]
    assert old.sel_page_size == 50 and old.reset_type["off"] == "ForceOff"
    new = match_quirks(table, "ACME Inc.", "3.1.0")
    assert new.applied == ["base", "acme-3"]
    assert new.pcie_devices_under == "systems" and new.lanes_in_use_unreliable
    assert new.sel_page_size is None, "an unmatched quirk leaves the default"
    other = match_quirks(table, "Other", "1.0")
    assert other.applied == ["base"] and other.reset_type["off"] == "GracefulShutdown"
    assert match_quirks(default_quirks(), "", "").applied == ["dmtf-defaults"]

    # The driver matches on what the BMC says about itself, not on the record's hint: asking
    # for the quirks runs discovery first, and the fixture vendor wins over "ACME Inc.".
    rig = Rig(quirk_table=table)
    rig.client.record = rig.client.record.model_copy(
        update={"vendor_hint": "ACME Inc.", "firmware_hint": "2.4.1"}
    )
    assert rig.client.quirks().applied == ["base", "fixture-hex"]
    assert rig.client.endpoints().system == "/redfish/v1/Systems/1"
    assert rig.client.quirks().sel_ids_are_hex is True
    assert [e.id for e in rig.client.sel()] == [1, 2, 3], "hex ids are converted"
    assert len([p for _, p in rig.server.requests if p == "/redfish/v1"]) == 1, "discovered once"

    with pytest.raises(Exception, match="unknown or invalid field"):
        match_quirks(
            quirks_from_mapping({"quirks": [{"id": "x", "note": "n", "set": {"nope": 1}}]}),
            "",
            "",
        )


def test_http_error_wraps_transport_failures() -> None:
    with pytest.raises(HttpError):
        FakeHttpClient(lambda r: HttpResponse(status=200), unreachable=True).send(
            HttpRequest(method="GET", url="https://bmc/redfish/v1/")
        )
