"""ipmitool and ssh as argv-only children, the in-band parsers, SOL capture and the syslog
receiver. No BMC, no network beyond loopback, no binaries."""

from __future__ import annotations

import socket
import time
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from slas_hal.credentials import CredentialError, EnvCredentialResolver, FakeCredentialResolver
from slas_hal.drivers.ipmi import IpmiTool, parse_sel_list
from slas_hal.drivers.process import (
    FakeProcessRunner,
    FakeStreamRunner,
    LocalProcessRunner,
    remote_argv,
)
from slas_hal.drivers.sol import SolCapture
from slas_hal.drivers.ssh import SshClient, parse_dmesg_counters, parse_lspci
from slas_hal.drivers.syslog import SyslogReceiver, parse_syslog
from slas_hal.hal import CommandResult
from slas_hal.redfish import HalError
from slas_hal.targets import BmcAccess, SshAccess, TargetRecord
from slas_kernel.clock import FakeClock

ALIAS = "lab-gx8-01"
PASSWORD = "Bmc-Passw0rd-XYZ!"
KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZSBrZXkgZm9yIHRlc3Rz\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
KNOWN = (
    "10.20.30.41 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak"
)

RECORD = TargetRecord(
    alias=ALIAS,
    bmc=BmcAccess(host="10.20.30.40", user="slas-validation", password_ref="env:BMC_PW"),
    ssh=SshAccess(
        host="10.20.30.41",
        user="slas",
        private_key_ref="env:LAB_SSH_KEY",
        port=2222,
        known_hosts_line=KNOWN,
    ),
)
RESOLVER = FakeCredentialResolver({"env:BMC_PW": PASSWORD, "env:LAB_SSH_KEY": KEY})

IPMI_SEL = """\
   1 | 09/10/2026 | 07:00:00 | System Event #0x01 | Timestamp Clock Sync | Asserted
   2 | 09/10/2026 | 07:00:12 | System Firmware Progress #0x02 | System boot completed | Asserted
  1a | 09/10/2026 | 07:01:40 | Fan #0x30 | Lower Critical going low | Asserted
  1b | 09/10/2026 | 07:02:00 | Temperature #0x31 | Upper Non-critical going high | Asserted
garbage line without pipes
"""

LSPCI = """\
0000:00:00.0 Host bridge [0600]: Intel Corporation Device [8086:09a2] (rev 04)
\tSubsystem: Intel Corporation Device [8086:0000]
\tControl: I/O- Mem+ BusMaster+
0000:8a:00.0 3D controller [0302]: NVIDIA Corporation GH100 [H100 SXM5 80GB] [10de:2330] (rev a1)
\tSubsystem: NVIDIA Corporation Device [10de:16c1]
\tCapabilities: [78] Express (v2) Endpoint, MSI 00
\t\tLnkCap:\tPort #0, Speed 32GT/s, Width x16, ASPM not supported
\t\tLnkSta:\tSpeed 16GT/s (downgraded), Width x8 (downgraded)
0000:e1:00.0 Non-Volatile memory controller [0108]: Samsung NVMe SSD Controller PM1743 [144d:a826]
\tCapabilities: [70] Express (v2) Endpoint, MSI 00
\t\tLnkCap:\tPort #0, Speed 32GT/s, Width x4, ASPM L1
\t\tLnkSta:\tSpeed 32GT/s, Width x4
"""

DMESG = """\
[    1.204311] pci 0000:8a:00.0: [10de:2330] type 00 class 0x030200
[  120.1] pcieport 0000:80:01.0: AER: Corrected error received: 0000:8a:00.0
[  120.2] pcieport 0000:80:01.0: AER: Corrected error received: 0000:8a:00.0
[  130.0] EDAC MC0: 1 CE memory read error on CPU_SrcID#0_MC#0_Chan#1_DIMM#0
[  131.0] EDAC MC0: 1 UE memory read error on CPU_SrcID#0_MC#0_Chan#1_DIMM#0
[  140.0] mce: [Hardware Error]: Machine check events logged
[  150.0] NVRM: Xid (PCI:0000:8a:00): 79, GPU has fallen off the bus.
"""


# --- ipmitool ---------------------------------------------------------------------------------


def test_ipmitool_argv_carries_no_password_and_the_env_does() -> None:
    runner = FakeProcessRunner()
    runner.on(
        "ipmitool",
        "chassis",
        "power",
        "status",
        result=CommandResult(exit_code=0, stdout="Chassis Power is on\n"),
    )
    ipmi = IpmiTool(RECORD, runner=runner, resolver=RESOLVER)
    assert ipmi.power_state() == "on"
    argv, env = runner.calls[0]
    assert argv == [
        "ipmitool",
        "-I",
        "lanplus",
        "-H",
        "10.20.30.40",
        "-p",
        "623",
        "-U",
        "slas-validation",
        "-E",
        "chassis",
        "power",
        "status",
    ]
    assert env == {"IPMI_PASSWORD": PASSWORD}
    assert PASSWORD not in " ".join(argv)
    assert ipmi.sol_argv()[-2:] == ["sol", "activate"]
    assert ipmi.audit == [("chassis power status", 0)]


def test_ipmitool_power_mapping_and_failures() -> None:
    runner = FakeProcessRunner()
    runner.on(
        "ipmitool",
        "chassis",
        "power",
        "soft",
        result=CommandResult(exit_code=0, stdout="Chassis Power Control: Soft\n"),
    )
    runner.on("ipmitool", "chassis", "power", "reset", result=CommandResult(exit_code=0))
    runner.on(
        "ipmitool",
        "chassis",
        "power",
        "status",
        result=CommandResult(exit_code=0, stdout="Chassis Power is off\n"),
    )
    runner.on("ipmitool", "sel", "list", result=CommandResult(exit_code=0, stdout=IPMI_SEL))
    ipmi = IpmiTool(RECORD, runner=runner, resolver=RESOLVER)
    assert ipmi.power("off") == "soft"
    assert ipmi.power("graceful_restart") == "reset", "IPMI has no graceful restart"
    assert ipmi.power_state() == "off"
    entries = ipmi.sel()
    assert [(e.id, e.severity, e.sensor) for e in entries] == [
        (1, "OK", "System Event #0x01"),
        (2, "OK", "System Firmware Progress #0x02"),
        (26, "Critical", "Fan #0x30"),
        (27, "Warning", "Temperature #0x31"),
    ]
    assert entries[2].created == "2026-09-10T07:01:40Z"
    assert entries[2].message == "Lower Critical going low | Asserted"
    with pytest.raises(HalError) as info:
        ipmi.power("ac_cycle")
    assert "PDU driver" in info.value.message.likely_cause

    missing = FakeProcessRunner()
    with pytest.raises(HalError) as info:
        IpmiTool(RECORD, runner=missing, resolver=RESOLVER).power_state()
    assert (
        info.value.message.what_happened == "ipmitool is not installed in the validation executor."
    )

    failing = FakeProcessRunner()
    failing.on(
        "ipmitool",
        "chassis",
        "power",
        "status",
        result=CommandResult(
            exit_code=1, stderr="Error: Unable to establish IPMI v2 / RMCP+ session\n"
        ),
    )
    with pytest.raises(HalError) as info:
        IpmiTool(RECORD, runner=failing, resolver=RESOLVER).power_state()
    assert (
        info.value.message.what_happened
        == f"ipmitool chassis power status failed on {ALIAS} (exit 1)."
    )
    assert info.value.message.likely_cause == "Error: Unable to establish IPMI v2 / RMCP+ session"
    assert parse_sel_list("") == []


# --- ssh ------------------------------------------------------------------------------------


def test_ssh_argv_is_hardened_and_the_key_lives_only_during_the_call(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    seen: dict[str, object] = {}

    def capture(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
        key_path = Path(argv[argv.index("-i") + 1])
        seen["key_text"] = key_path.read_text(encoding="utf-8")
        seen["key_mode"] = key_path.stat().st_mode & 0o777
        seen["known"] = Path(
            next(a for a in argv if a.startswith("UserKnownHostsFile=")).split("=", 1)[1]
        ).read_text()
        seen["files"] = sorted(p.name for p in key_dir.iterdir())
        return CommandResult(exit_code=0, stdout="ok\n")

    runner = FakeProcessRunner()
    runner.on("ssh", "uptime", handler=capture)
    client = SshClient(RECORD, runner=runner, resolver=RESOLVER, key_dir=key_dir)
    result = client.run(["uptime", "-p"])
    assert result.stdout == "ok\n"
    argv, env = runner.calls[0]
    assert argv[0] == "ssh" and env == {}
    options = " ".join(argv)
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "IdentitiesOnly=yes",
        "PasswordAuthentication=no",
        "ConnectTimeout=10",
    ):
        assert option in options
    assert argv[-6:] == ["-p", "2222", "slas@10.20.30.41", "--", "uptime", "-p"]
    assert remote_argv(argv) == ["uptime", "-p"]
    assert KEY not in options, "the key is a file, never argv"
    assert seen["key_text"] == KEY + "\n" and seen["key_mode"] == 0o600
    assert seen["known"] == KNOWN + "\n"
    assert len(seen["files"]) == 2  # type: ignore[arg-type]
    assert list(key_dir.iterdir()) == [], "key and known_hosts are shredded after the call"
    assert (key_dir.stat().st_mode & 0o777) == 0o700
    assert client.audit == [(["uptime", "-p"], 0)]


def test_ssh_without_access_configured_is_a_sentence(tmp_path: Path) -> None:
    bare = RECORD.model_copy(update={"ssh": None})
    client = SshClient(bare, runner=FakeProcessRunner(), resolver=RESOLVER, key_dir=tmp_path)
    with pytest.raises(HalError) as info:
        client.run(["true"])
    assert info.value.message.what_happened == f"{ALIAS} has no SSH access configured."


def test_wait_for_os_polls_until_reachable_or_the_deadline(tmp_path: Path) -> None:
    runner = FakeProcessRunner()
    attempts = {"n": 0}

    def flaky(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
        attempts["n"] += 1
        return CommandResult(exit_code=0 if attempts["n"] >= 3 else 255)

    runner.on("ssh", "true", handler=flaky)
    client = SshClient(RECORD, runner=runner, resolver=RESOLVER, key_dir=tmp_path)
    clock = FakeClock(step=timedelta(seconds=10))
    slept: list[float] = []
    assert client.wait_for_os(timeout_s=900, now=clock.now, sleep=slept.append, poll_s=5) is True
    assert attempts["n"] == 3 and slept == [5, 5]

    never = FakeProcessRunner()
    never.on("ssh", "true", result=CommandResult(exit_code=255))
    client = SshClient(RECORD, runner=never, resolver=RESOLVER, key_dir=tmp_path)
    assert (
        client.wait_for_os(
            timeout_s=30, now=FakeClock(step=timedelta(seconds=10)).now, sleep=lambda s: None
        )
        is False
    )


def test_counters_and_devices_come_from_dmesg_and_lspci(tmp_path: Path) -> None:
    runner = FakeProcessRunner()
    runner.on("ssh", "dmesg", result=CommandResult(exit_code=0, stdout=DMESG))
    runner.on("ssh", "lspci", result=CommandResult(exit_code=0, stdout=LSPCI))
    runner.on("ssh", "logger", result=CommandResult(exit_code=0))
    client = SshClient(RECORD, runner=runner, resolver=RESOLVER, key_dir=tmp_path)
    counters = client.counters()
    assert counters.as_dict() == {"AER": 2, "EDAC CE": 1, "EDAC UE": 1, "MCE": 1, "Xid": 1}
    devices = client.devices()
    assert [d.bdf for d in devices] == ["0000:8a:00.0", "0000:e1:00.0"], (
        "the host bridge has no link"
    )
    gpu = devices[0]
    assert (gpu.width, gpu.speed_gts, gpu.max_width, gpu.max_speed_gts) == (8, 16.0, 16, 32.0)
    assert (gpu.vendor_id, gpu.device_id, gpu.class_code) == ("0x10de", "0x2330", "0x030200")
    assert gpu.name == "NVIDIA Corporation GH100 [H100 SXM5 80GB]"
    assert devices[1].link() == "x4 @ 32 GT/s"
    assert remote_argv(runner.calls[1][0]) == ["lspci", "-D", "-vv", "-nn"]
    assert client.fence("--- slas fence T-1 cycle 1 dc ---").exit_code == 0
    assert remote_argv(runner.calls[2][0]) == [
        "logger",
        "-t",
        "slas",
        "--- slas fence T-1 cycle 1 dc ---",
    ]
    assert parse_dmesg_counters("").as_dict() == {
        "AER": 0,
        "EDAC CE": 0,
        "EDAC UE": 0,
        "MCE": 0,
        "Xid": 0,
    }
    assert parse_lspci("") == []

    failing = FakeProcessRunner()
    failing.on(
        "ssh",
        "dmesg",
        result=CommandResult(
            exit_code=1, stderr="dmesg: read kernel buffer failed: Operation not permitted"
        ),
    )
    with pytest.raises(HalError) as info:
        SshClient(RECORD, runner=failing, resolver=RESOLVER, key_dir=tmp_path).counters()
    assert "dmesg_restrict" in info.value.message.what_to_do


def test_local_process_runner_runs_argv_without_a_shell() -> None:
    runner = LocalProcessRunner()
    result = runner.run(["echo", "$HOME", "hello"], env={})
    assert result.exit_code == 0 and result.stdout == "$HOME hello\n", "no shell expansion"
    missing = runner.run(["slas-no-such-binary"], env={})
    assert missing.exit_code == 127
    assert runner.argv_seen[0] == ["echo", "$HOME", "hello"]


def test_env_credential_resolver_reads_env_refs_only() -> None:
    resolver = EnvCredentialResolver({"LAB_BMC": "s3cret-value"})
    assert resolver.resolve("env:LAB_BMC") == "s3cret-value"
    with pytest.raises(CredentialError) as info:
        resolver.resolve("vault:kv/lab/bmc")
    assert "cannot resolve vault: references" in info.value.message.what_happened
    assert "env:KV_LAB_BMC" in info.value.message.what_to_do
    with pytest.raises(CredentialError) as info:
        resolver.resolve("env:MISSING")
    assert info.value.message.what_happened == "The credential env:MISSING is not set."
    with pytest.raises(CredentialError):
        resolver.resolve("plain-password")


# --- SOL and syslog --------------------------------------------------------------------------


def test_sol_capture_drains_the_stream_and_annotates_fences(tmp_path: Path) -> None:
    lines = ["BIOS Version 2.4.1", "POST: memory training ... done"]
    runner = FakeStreamRunner(lambda: lines)
    capture = SolCapture(
        ALIAS,
        runner=runner,
        argv=["ipmitool", "-E", "sol", "activate"],
        env={"IPMI_PASSWORD": PASSWORD},
        sink=tmp_path / "Console" / f"{ALIAS}.log",
        clock=FakeClock(),
    )
    active_before = capture.active
    assert capture.lines() == [] and active_before is False
    capture.start()
    active_after = capture.active
    assert active_after is True
    assert runner.started[0][1] == {"IPMI_PASSWORD": PASSWORD}
    assert capture.lines()[1:] == lines
    lines.append("[    0.000000] Linux version 6.8.0-slas")
    capture.annotate("--- slas fence T-1 cycle 1 dc ---")
    assert capture.lines(since=3) == [
        "[    0.000000] Linux version 6.8.0-slas",
        "--- slas fence T-1 cycle 1 dc ---",
    ]
    capture.stop()
    assert not capture.active
    text = (tmp_path / "Console" / f"{ALIAS}.log").read_text(encoding="utf-8")
    assert text.startswith("--- slas sol capture started 2026-09-14T08:00:00+00:00 ---\n")
    assert text.rstrip().endswith("--- slas sol capture stopped 2026-09-14T08:00:01+00:00 ---")
    assert PASSWORD not in text
    capture.start()  # a second start after stop opens a new stream
    assert len(runner.handles) == 2
    capture.stop()


def test_syslog_receiver_parses_both_formats_over_loopback(tmp_path: Path) -> None:
    receiver = SyslogReceiver(listen="127.0.0.1:0", sink=tmp_path / "Syslog" / "lab.jsonl")
    receiver.start()
    try:
        assert receiver.port != 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(
                b"<13>Sep 14 08:00:00 gx8-01 kernel: NVRM: Xid (PCI:0000:8a:00): 79",
                ("127.0.0.1", receiver.port),
            )
            sock.sendto(
                b"<134>1 2026-09-14T08:00:01Z gx8-01 slas - - - --- slas fence T-1 cycle 1 dc ---",
                ("127.0.0.1", receiver.port),
            )
        deadline = time.monotonic() + 5
        while len(receiver.lines()) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        receiver.annotate("--- slas fence T-1 cycle 2 dc ---", now=FakeClock().now())
    finally:
        receiver.stop()
    lines = receiver.lines()
    assert [(line.host, line.facility, line.severity) for line in lines] == [
        ("gx8-01", 1, 5),
        ("gx8-01", 16, 6),
        ("slas", 1, 6),
    ]
    assert lines[0].message == "kernel: NVRM: Xid (PCI:0000:8a:00): 79"
    assert lines[1].message == "--- slas fence T-1 cycle 1 dc ---"
    assert lines[0].line().endswith("gx8-01: kernel: NVRM: Xid (PCI:0000:8a:00): 79")
    assert receiver.lines(since=2)[0].source == "slas"
    sink = (tmp_path / "Syslog" / "lab.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(sink) == 3
    receiver.stop()  # idempotent
    plain = parse_syslog("no priority at all", source="x", received_at=FakeClock().now())
    assert (plain.host, plain.message, plain.severity) == ("-", "no priority at all", 6)
