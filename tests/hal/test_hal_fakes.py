"""slas_hal fakes: recorded Redfish fixtures (ugly ones included), the stateful fake target,
planted failures, fence markers and the power-action record (CLAUDE.md §5.2, §11)."""

from __future__ import annotations

import pytest

from slas_hal.fakes.bmc import FakeHal, FakeTarget, Plant, fixture_text
from slas_hal.hal import CommandResult
from slas_hal.redfish import HalError, load_payload, parse_pcie_devices, parse_sel
from slas_kernel.clock import FakeClock

REF = "lab-gx8-01"
GPU3 = "0000:8a:00.0"


def make_hal(*plants: Plant, **kwargs: object) -> FakeHal:
    return FakeHal(
        [FakeTarget(REF, plants=plants)],
        clock=FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )


def dc_cycle(hal: FakeHal) -> None:
    hal.power(REF, "off")
    hal.power(REF, "on")


# --- recorded fixtures --------------------------------------------------------------------


def test_recorded_fixtures_parse_into_the_platform_models() -> None:
    target = FakeTarget(REF)
    assert target.power == "on"
    assert target.inventory.model == "SLAS-GX8"
    assert target.inventory.serial == "SN-GX8-0001"
    assert target.inventory.cpus == 2 and target.inventory.memory_gib == 1024
    assert len(target.inventory.devices) == 11
    gpu3 = target.inventory.device(GPU3)
    assert gpu3 is not None
    assert gpu3.name == "NVIDIA H100 SXM"
    assert gpu3.label == "NVIDIA H100 SXM (0000:8a:00.0)"
    assert (gpu3.width, gpu3.speed_gts, gpu3.max_width, gpu3.max_speed_gts) == (16, 32.0, 16, 32.0)
    assert gpu3.link() == "x16 @ 32 GT/s"
    assert gpu3.firmware == "96.00.5E"
    nvme = target.inventory.device("0000:e1:00.0")
    assert nvme is not None and nvme.width == 4
    firmware = target.inventory.firmware()
    assert firmware["BIOS"] == "2.4.1" and firmware["BMC"] == "1.12.0"
    assert firmware[gpu3.label] == "96.00.5E"
    assert [e.severity for e in target.sel] == ["OK", "OK", "Warning"]
    assert target.sel[2].line() == (
        "[2026-09-10T07:01:40Z] Warning: Fan 3 speed below threshold, recovered"
    )
    assert target.inventory.device("0000:ff:00.0") is None


def test_truncated_sel_is_reported_incomplete_not_half_parsed() -> None:
    payload = load_payload(fixture_text("sel_truncated.json"), what="SEL", target=REF)
    with pytest.raises(HalError) as info:
        parse_sel(payload, target=REF)
    message = info.value.message
    assert message.what_happened == (
        f"The SEL answer from {REF} is incomplete: 2 of 4000 entries arrived."
    )
    assert message.likely_cause == "The BMC truncated the collection."
    assert message.what_to_do.startswith("Retry once")


def test_a_collection_without_members_is_incomplete_too() -> None:
    payload = {"Members@odata.count": 12}
    with pytest.raises(HalError) as info:
        parse_sel(payload, target=REF)
    assert "has no Members" in info.value.message.what_happened
    assert "reported 12 entries but sent none" in info.value.message.likely_cause


def test_malformed_json_is_a_three_part_error() -> None:
    with pytest.raises(HalError) as info:
        load_payload(fixture_text("sel_malformed.json"), what="SEL", target=REF)
    message = info.value.message
    assert message.what_happened == (
        f"The BMC of {REF} answered the SEL request with something that is not JSON."
    )
    assert "cut short or corrupted" in message.likely_cause
    assert str(info.value) == message.what_happened


def test_json_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(HalError) as info:
        load_payload("[1, 2, 3]", what="system", target=REF)
    assert info.value.message.what_happened == (
        f"The BMC of {REF} answered the system request with a JSON list, not an object."
    )


def test_a_device_without_link_state_is_refused_because_verify_needs_it() -> None:
    payload = load_payload(fixture_text("pcie_no_link_state.json"), what="PCIe device", target=REF)
    with pytest.raises(HalError) as info:
        parse_pcie_devices(payload, target=REF)
    assert info.value.message.what_happened == (
        f"A PCIe device of {REF} (GPU0) has no usable link state."
    )
    assert "BDF, LanesInUse or PCIeType" in info.value.message.likely_cause


def test_odd_sel_values_degrade_gracefully() -> None:
    payload = {
        "Members@odata.count": 2,
        "Members": [
            {"Id": "x", "Severity": "Fatal", "Message": "   "},
            "not an object",
            {
                "Id": "7",
                "Severity": "Critical",
                "Message": "Uncorrectable ECC",
                "SensorType": "Mem",
            },
        ],
    }
    with pytest.raises(HalError):
        parse_sel(payload, target=REF)  # 3 members announced as 2: incomplete
    payload["Members@odata.count"] = 3
    entries = parse_sel(payload, target=REF)
    assert [(e.id, e.severity, e.message, e.sensor) for e in entries] == [
        (0, "Warning", "(no message)", None),
        (7, "Critical", "Uncorrectable ECC", "Mem"),
    ]


def test_a_target_built_from_a_bad_fixture_fails_loudly() -> None:
    with pytest.raises(HalError):
        FakeTarget(REF, fixtures={"sel.json": fixture_text("sel_truncated.json")})


# --- the stateful fake -----------------------------------------------------------------------


def test_a_dc_cycle_records_off_then_on_and_boots() -> None:
    hal = make_hal()
    hal.power(REF, "off")
    assert hal.power_state(REF) == "off"
    assert hal.wait_for_os(REF, timeout_s=900) is False
    hal.power(REF, "on")
    assert hal.power_state(REF) == "on"
    assert hal.wait_for_os(REF, timeout_s=900) is True
    assert hal.wait_for_os(REF, timeout_s=30) is False, "the fixture boots in 90 s"
    records = hal.target(REF).power_records
    assert [(r.action, r.n) for r in records] == [("off", 1), ("on", 2)]
    console = hal.console_lines(REF)
    assert any("Linux version" in line for line in console)
    assert [e.message for e in hal.sel(REF)][-2:] == [
        "Power off requested",
        "System boot initiated",
    ]
    snapshot = hal.snapshot(REF)
    assert snapshot.sentence().startswith(
        "SLAS-GX8 SN-GX8-0001: power on, 11 PCIe devices, 5 SEL entries"
    )


def test_force_off_graceful_restart_and_ac_cycle_are_distinct_actions() -> None:
    hal = make_hal()
    hal.power(REF, "force_off")
    assert hal.sel(REF)[-1].message == "Power off forced"
    hal.power(REF, "graceful_restart")
    hal.power(REF, "ac_cycle")
    assert [r.action for r in hal.target(REF).power_records] == [
        "force_off",
        "graceful_restart",
        "ac_cycle",
    ]
    assert hal.target(REF).cycles_done == 2
    assert "AC power lost, restored" in [e.message for e in hal.sel(REF)]


def test_planted_pcie_width_degradation_appears_from_its_cycle_on_and_persists() -> None:
    hal = make_hal(Plant(at_cycle=2, kind="pcie_width", bdf=GPU3, width=8))

    def width_of(bdf: str) -> int:
        device = hal.inventory(REF).device(bdf)
        assert device is not None
        return device.width

    dc_cycle(hal)
    assert width_of(GPU3) == 16
    dc_cycle(hal)
    assert width_of(GPU3) == 8
    assert width_of("0000:1a:00.0") == 16, "only the planted device changes"
    assert len(hal.inventory(REF).devices) == 11
    dc_cycle(hal)
    assert width_of(GPU3) == 8, "a real degradation does not heal itself"


def test_a_bounded_plant_clears_after_its_cycles() -> None:
    hal = make_hal(Plant(at_cycle=1, kind="pcie_speed", bdf=GPU3, speed_gts=16.0, for_cycles=1))
    dc_cycle(hal)
    gpu3 = hal.inventory(REF).device(GPU3)
    assert gpu3 is not None and gpu3.speed_gts == 16.0 and gpu3.width == 16
    dc_cycle(hal)
    gpu3 = hal.inventory(REF).device(GPU3)
    assert gpu3 is not None and gpu3.speed_gts == 32.0


def test_the_other_plant_kinds() -> None:
    hal = make_hal(
        Plant(at_cycle=1, kind="xid", bdf=GPU3),
        Plant(at_cycle=1, kind="sel", message="Uncorrectable PCIe error on slot 3"),
        Plant(at_cycle=1, kind="device_missing", bdf="0000:e1:00.0"),
        Plant(at_cycle=1, kind="firmware", bdf="0000:41:00.0", message="28.40.0001"),
    )
    dc_cycle(hal)
    assert hal.counters(REF).xid == 1
    assert any("NVRM: Xid (PCI:0000:8a:00.0)" in line for line in hal.target(REF).syslog)
    critical = [e for e in hal.sel(REF) if e.severity == "Critical"]
    assert [e.message for e in critical] == ["Uncorrectable PCIe error on slot 3"]
    assert len(hal.inventory(REF).devices) == 10
    assert hal.inventory(REF).device("0000:e1:00.0") is None
    nic0 = hal.inventory(REF).device("0000:41:00.0")
    assert nic0 is not None and nic0.firmware == "28.40.0001"
    dc_cycle(hal)
    assert hal.counters(REF).xid == 1, "an Xid is an event, counted once"
    assert len([e for e in hal.sel(REF) if e.severity == "Critical"]) == 1


def test_a_boot_failure_never_reaches_the_os() -> None:
    hal = make_hal(Plant(at_cycle=1, kind="boot_fail"))
    dc_cycle(hal)
    assert hal.wait_for_os(REF, timeout_s=900) is False
    console = hal.console_lines(REF)
    assert console[-1] == "*** system halted ***"
    assert "POST: PCIe training error on slot 3" in console
    assert not any("Linux version" in line for line in console)
    result = hal.ssh(REF, ["uptime"])
    assert result.exit_code == 255 and "No route to host" in result.stderr


def test_fence_marker_lands_in_console_and_syslog() -> None:
    hal = make_hal()
    hal.console_on(REF)
    hal.fence(REF, "--- slas fence T-validation-0001 cycle 1 dc ---")
    target = hal.target(REF)
    assert target.console_active
    assert target.console[-1] == "--- slas fence T-validation-0001 cycle 1 dc ---"
    assert target.syslog[-1] == "--- slas fence T-validation-0001 cycle 1 dc ---"
    assert hal.fence_markers == [(REF, "--- slas fence T-validation-0001 cycle 1 dc ---")]
    assert hal.console_lines(REF, since=len(target.console)) == []


def test_ssh_records_argv_and_can_be_scripted() -> None:
    hal = make_hal()
    ok = hal.ssh(REF, ["logger", "hello", "from", "the", "test"])
    assert ok.exit_code == 0 and ok.stdout == "ok: logger hello from the test\n"
    assert hal.target(REF).syslog[-1] == "hello from the test"
    hal.on_ssh = lambda target, argv: CommandResult(exit_code=7, stderr=f"{argv[0]} refused")
    scripted = hal.ssh(REF, ["stress-ng", "--cpu", "8"], timeout_s=5)
    assert (scripted.exit_code, scripted.stderr) == (7, "stress-ng refused")
    assert hal.target(REF).ssh_calls == [
        ["logger", "hello", "from", "the", "test"],
        ["stress-ng", "--cpu", "8"],
    ]
    assert len(hal.devices_of(REF)) == 11


def test_unknown_target_is_a_sentence_not_a_key_error() -> None:
    hal = make_hal()
    with pytest.raises(HalError) as info:
        hal.power_state("lab-nope")
    assert info.value.message.what_happened == "There is no target called lab-nope."
    assert info.value.message.what_to_do == "Pick a target from the Validation page's list."


def test_crash_once_in_raises_exactly_once() -> None:
    hal = make_hal(crash_once_in="wait_for_os")
    with pytest.raises(RuntimeError, match="simulated crash during wait_for_os"):
        hal.wait_for_os(REF, timeout_s=900)
    assert hal.wait_for_os(REF, timeout_s=900) is True
    hal.crash_once_in = "power"
    with pytest.raises(RuntimeError, match="simulated crash during power"):
        hal.power(REF, "off")
    assert hal.target(REF).power_records == [], "a crash before the action leaves no record"


def test_plant_activity_window() -> None:
    forever = Plant(at_cycle=3, kind="pcie_width", bdf=GPU3, width=8)
    assert [forever.active(n) for n in (1, 2, 3, 4, 99)] == [False, False, True, True, True]
    bounded = Plant(at_cycle=3, kind="xid", for_cycles=2)
    assert [bounded.active(n) for n in (2, 3, 4, 5)] == [False, True, True, False]
