"""`RealHal` end to end: the same 25-cycle run as P7, this time through the Redfish driver
against the fake Redfish service, ssh/ipmitool through the fake process runner and SOL
through the fake stream — then the CI grep proves no credential reached any sink. Also: the
arming gate, the PDU path, and the lspci fallback."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from slas_hal.credentials import FakeCredentialResolver
from slas_hal.drivers import pdu as pdu_module
from slas_hal.drivers.pdu import FakePdu
from slas_hal.drivers.process import FakeProcessRunner, FakeStreamRunner, remote_argv
from slas_hal.drivers.real import RealHal
from slas_hal.drivers.syslog import SyslogReceiver
from slas_hal.fakes.bmc import FakeTarget, Plant
from slas_hal.fakes.redfish_server import FakeRedfishServer
from slas_hal.hal import CommandResult
from slas_hal.http import FakeHttpClient
from slas_hal.model import PowerAction
from slas_hal.quirks import quirks_from_mapping
from slas_hal.redfish import HalError
from slas_hal.sinkcheck import find_secrets, scan_tree
from slas_hal.sinkcheck import main as sinkcheck_main
from slas_hal.targets import (
    ArmingError,
    BmcAccess,
    PduOutlet,
    SshAccess,
    TargetRecord,
    TargetRegistry,
)
from slas_kernel.clock import FakeClock
from slas_kernel.kernel import Kernel
from slas_kernel.store import FileTicketStore
from slas_orchestrator.validation.agent import ValidationAgent
from slas_schemas.job import Upload
from slas_schemas.ticket import TicketState
from slas_validation_executor.executor import ValidationExecutor

ALIAS = "lab-gx8-01"
GPU3 = "0000:8a:00.0"
PASSWORD = "Bmc-Passw0rd-XYZ!"
KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZSBrZXkgZm9yIHRlc3Rz\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
KNOWN = (
    "10.20.30.41 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak"
)
SECRETS = {"env:LAB_GX8_01_BMC_PASSWORD": PASSWORD, "env:LAB_GX8_01_SSH_KEY": KEY}
UPLOAD_25 = Upload(
    filename="gx8.md",
    uploaded_by="pat",
    content="# GX8 DC cycling\n\n- DC cycle x25, settle 60 s\n",
)


def lspci_text(target: FakeTarget) -> str:
    out = ["0000:00:00.0 Host bridge [0600]: Intel Corporation Device [8086:09a2] (rev 04)"]
    for d in target.inventory.devices:
        out.append(f"{d.bdf} 3D controller [0302]: {d.name} [{d.vendor_id[2:]}:{d.device_id[2:]}]")
        out.append(f"\t\tLnkCap:\tPort #0, Speed {d.max_speed_gts:g}GT/s, Width x{d.max_width}")
        out.append(f"\t\tLnkSta:\tSpeed {d.speed_gts:g}GT/s, Width x{d.width}")
    return "\n".join(out) + "\n"


class LabRig:
    """A fake lab: one target behind a fake BMC, fake OS over ssh, fake SOL, real drivers."""

    def __init__(
        self, data_root: Path, *plants: Plant, quirk_table: object = None, pdu: bool = False
    ) -> None:
        self.data_root = data_root
        self.target = FakeTarget(ALIAS, plants=plants)
        self.clock = FakeClock()
        self.server = FakeRedfishServer(
            self.target, user="slas-validation", password=PASSWORD, clock=self.clock
        )
        self.http = FakeHttpClient(self.server)
        self.runner = FakeProcessRunner()
        self.stream = FakeStreamRunner(lambda: self.target.console)
        self.resolver = FakeCredentialResolver(SECRETS)
        self.registry = TargetRegistry(data_root / "Validation" / "targets.json")
        self.registry.put(
            TargetRecord(
                alias=ALIAS,
                bmc=BmcAccess(
                    host="10.20.30.40",
                    user="slas-validation",
                    password_ref="env:LAB_GX8_01_BMC_PASSWORD",
                ),
                ssh=SshAccess(
                    host="10.20.30.41",
                    user="slas",
                    private_key_ref="env:LAB_GX8_01_SSH_KEY",
                    known_hosts_line=KNOWN,
                ),
                pdu=PduOutlet(
                    driver="fake-pdu", host="10.20.30.50", outlet=7, credential_ref="env:LAB_PDU"
                )
                if pdu
                else None,
            )
        )
        self.syslog = SyslogReceiver(
            listen="127.0.0.1:0", sink=data_root / "Validation" / "Syslog" / "lab.jsonl"
        )
        self.slept: list[float] = []
        self.hal = RealHal(
            self.registry,
            http=self.http,
            runner=self.runner,
            stream_runner=self.stream,
            resolver=self.resolver,
            clock=self.clock,
            data_root=data_root,
            sleep=self.slept.append,
            quirk_table=quirk_table,  # type: ignore[arg-type]
            syslog=self.syslog,
        )
        self._script_os()

    def _script_os(self) -> None:
        target = self.target

        def if_up(exit_ok: CommandResult) -> object:
            def handler(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
                if target.power == "on" and target.booted:
                    return exit_ok
                return CommandResult(exit_code=255, stderr="ssh: connect to host: No route to host")

            return handler

        self.runner.on("ssh", "true", handler=if_up(CommandResult(exit_code=0)))  # type: ignore[arg-type]
        self.runner.on("ssh", "sync", handler=if_up(CommandResult(exit_code=0)))  # type: ignore[arg-type]
        self.runner.on(
            "ssh",
            "dmesg",
            handler=lambda argv, env: CommandResult(
                exit_code=0, stdout="\n".join(target.syslog) + "\n"
            ),
        )
        self.runner.on(
            "ssh",
            "lspci",
            handler=lambda argv, env: CommandResult(exit_code=0, stdout=lspci_text(target)),
        )

        def logger(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
            target.syslog.append(" ".join(remote_argv(argv)[3:]))
            return CommandResult(exit_code=0)

        self.runner.on("ssh", "logger", handler=logger)


def test_an_unarmed_target_refuses_every_power_action_before_touching_the_bmc(
    tmp_path: Path,
) -> None:
    rig = LabRig(tmp_path)
    assert rig.hal.power_state(ALIAS) == "on", "reads are fine"
    with pytest.raises(ArmingError) as info:
        rig.hal.power(ALIAS, "off")
    assert info.value.message.what_happened == f"Power actions on {ALIAS} are not armed."
    assert info.value.message.what_to_do == (
        f"Confirm the target is free, run `slas target arm {ALIAS}`, then start the run again."
    )
    assert rig.server.reset_types == [] and rig.hal.power_records == []
    assert not any(method == "POST" for method, _ in rig.server.requests)
    assert not (tmp_path / "Validation" / "Power").exists(), "not even an intent is journalled"
    assert rig.registry.get(ALIAS).sentence().endswith("power actions not armed.")


def test_the_p7_run_through_the_real_drivers_leaves_no_credential_in_any_sink(
    tmp_path: Path,
) -> None:
    rig = LabRig(tmp_path, Plant(at_cycle=14, kind="pcie_width", bdf=GPU3, width=8))
    rig.registry.arm(ALIAS, by="lee", at=rig.clock.now(), note="Rack 4 confirmed free.")
    rig.syslog.start()
    try:
        agent = ValidationAgent(plans_dir=tmp_path / "Validation" / "Plans")
        executor = ValidationExecutor(hal=rig.hal, data_root=tmp_path, clock=FakeClock())
        kernel = Kernel(
            data_root=tmp_path,
            agent=agent,
            executor=executor,
            store=FileTicketStore(tmp_path),
            clock=FakeClock(),
        )
        job = agent.ingest(UPLOAD_25)
        agent.choose_target(job, ALIAS)
        ticket = kernel.run(UPLOAD_25)
    finally:
        rig.syslog.stop()

    assert ticket.state is TicketState.NEEDS_REVIEW
    (finding,) = ticket.findings
    assert finding.headline() == (
        "[Issue] PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 "
        "during DC cycle 14 | [Owner] EE"
    )
    assert FileTicketStore(tmp_path).load("T-validation-0002").parent == ticket.id
    # Power went through Redfish: 25 × (GracefulShutdown, On), nothing else.
    assert rig.server.reset_types == ["GracefulShutdown", "On"] * 25
    assert [r.action for r in rig.hal.power_records][:2] == ["off", "on"]
    assert len(rig.hal.power_records) == 50
    # Every action was journalled ahead (INV-6): intent, then action, per power record.
    journal = [
        json.loads(line)
        for line in (tmp_path / "Validation" / "Power" / f"{ALIAS}.jsonl").read_text().splitlines()
    ]
    intents = [e for e in journal if "intent" in e]
    actions = [e for e in journal if "action" in e]
    assert len(intents) == 50 and len(actions) == 50
    assert actions[0]["how"] == "redfish ResetType GracefulShutdown"
    assert journal.index(intents[0]) < journal.index(actions[0])
    # The LED map and the per-cycle files are the same as with the in-memory fake.
    state = executor.state_for(ticket.id)
    assert state is not None and state.sentence() == "25 of 25 cycles done: 12 with findings."
    # SOL: the console file has the boot lines and every fence marker; syslog got them too.
    console = (tmp_path / "Validation" / "Console" / f"{ALIAS}.log").read_text(encoding="utf-8")
    assert console.count("Linux version") == 25
    assert f"--- slas fence {ticket.id} cycle 14 dc ---" in console
    fences = [line for line in rig.syslog.lines() if "slas fence" in line.message]
    assert len(fences) == 25
    assert any("--- slas fence" in line for line in rig.target.syslog), "logger reached the OS too"
    # In-band: the OS was polled over ssh, sync ran before every power-off, dmesg after boot.
    remote = [remote_argv(argv)[0] for argv, _ in rig.runner.calls]
    assert remote.count("sync") == 25 and remote.count("true") >= 25 and remote.count("dmesg") == 26
    assert all(env == {} for argv, env in rig.runner.calls if argv[0] == "ssh")
    assert rig.stream.started[0][1] == {"IPMI_PASSWORD": PASSWORD}, (
        "SOL's password is env, not argv"
    )
    assert PASSWORD not in " ".join(" ".join(argv) for argv, _ in rig.runner.calls)
    assert PASSWORD not in " ".join(" ".join(argv) for argv, _ in rig.stream.started)
    assert rig.hal.write_audit(ALIAS).is_file()

    # The CI grep: nothing under the data root carries the password, the key, or any
    # credential shape — tickets, journals, SOPs, console, syslog, power journal, audit.
    hits = scan_tree(tmp_path, known=[PASSWORD, KEY])
    assert hits == [], "\n".join(h.sentence() for h in hits)
    assert len(list(tmp_path.rglob("*"))) > 100, "the grep had plenty to look at"
    assert sinkcheck_main([str(tmp_path), "--known", PASSWORD]) == 0
    if os.environ.get("SLAS_SINK_DIR"):
        # CI keeps the sinks and greps them again with `python -m slas_hal.sinkcheck`.
        shutil.copytree(tmp_path, os.environ["SLAS_SINK_DIR"], dirs_exist_ok=True)

    # …and it does bite when something leaks.
    leak = tmp_path / "Validation" / "Runs" / "leak.log"
    leak.write_text(f"debug: connecting with IPMI_PASSWORD={PASSWORD}\n", encoding="utf-8")
    (tmp_path / "Validation" / "Runs" / "key.log").write_text(KEY, encoding="utf-8")
    hits = scan_tree(tmp_path, known=[PASSWORD])
    assert {h.path: h.names for h in hits} == {
        "Validation/Runs/key.log": ("private_key_header",),
        "Validation/Runs/leak.log": (
            "known secret (Bmc…)",
            "ipmi_password_env",
        ),
    }
    assert sinkcheck_main([str(tmp_path)]) == 1
    assert sinkcheck_main([str(tmp_path / "nope")]) == 2
    assert find_secrets("Authorization: Basic abc [redacted:authorization_header]") == [
        "authorization_header"
    ]
    assert find_secrets("Authorization: [redacted:authorization_header]") == []


def test_ac_cycle_goes_through_the_pdu_and_needs_a_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = LabRig(tmp_path, pdu=True)
    rig.registry.arm(ALIAS, by="lee", at=rig.clock.now())
    rig.resolver.secrets["env:LAB_PDU"] = "pdu-secret"
    with pytest.raises(HalError) as info:
        rig.hal.power(ALIAS, "ac_cycle")
    assert info.value.message.what_happened == "There is no PDU driver called 'fake-pdu'."
    assert "has not been named" in info.value.message.likely_cause
    journal = [
        json.loads(line)
        for line in (tmp_path / "Validation" / "Power" / f"{ALIAS}.jsonl").read_text().splitlines()
    ]
    assert journal[0]["intent"] == "ac_cycle" and journal[1]["failed"].startswith(
        "There is no PDU driver"
    )

    pdu = FakePdu(sleep=rig.slept.append)
    monkeypatch.setitem(pdu_module.PDU_DRIVERS, "fake-pdu", lambda outlet, resolver, runner: pdu)
    rig.hal._drivers.clear()
    rig.hal.power(ALIAS, "ac_cycle")
    assert pdu.actions == [("off", 7), ("on", 7)]
    assert rig.slept == [30], "the outlet stays off for the configured time"
    assert [r.action for r in rig.hal.power_records] == ["ac_cycle"]
    assert rig.server.reset_types == [], "the BMC was not asked to do an AC cycle"

    bare = LabRig(tmp_path / "bare")
    bare.registry.arm(ALIAS, by="lee", at=bare.clock.now())
    with pytest.raises(HalError) as info:
        bare.hal.power(ALIAS, "ac_cycle")
    assert info.value.message.what_happened == f"{ALIAS} has no PDU outlet configured."


def test_quirks_route_power_through_ipmi_and_link_state_through_lspci(tmp_path: Path) -> None:
    table = quirks_from_mapping(
        {
            "quirks": [
                {
                    "id": "fixture-needs-ipmi",
                    "manufacturer": "test fixture",
                    "note": "Redfish reset returns 500 here; LanesInUse reports the maximum",
                    "set": {
                        "use_ipmi_for_power": True,
                        "lanes_in_use_unreliable": True,
                        "power_off_settle_s": 5,
                    },
                }
            ]
        }
    )
    rig = LabRig(
        tmp_path, Plant(at_cycle=1, kind="pcie_width", bdf=GPU3, width=4), quirk_table=table
    )
    rig.registry.arm(ALIAS, by="lee", at=rig.clock.now())
    target = rig.target

    def ipmi_power(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
        assert env == {"IPMI_PASSWORD": PASSWORD}
        actions: dict[str, PowerAction] = {
            "soft": "off",
            "on": "on",
            "off": "force_off",
            "reset": "graceful_restart",
        }
        target.apply_power(actions[argv[-1]], rig.clock.now())
        return CommandResult(exit_code=0, stdout="Chassis Power Control: ok\n")

    rig.runner.on("ipmitool", "chassis", "power", handler=ipmi_power)
    rig.hal.power(ALIAS, "off")
    rig.hal.power(ALIAS, "on")
    assert rig.server.reset_types == [], "Redfish was not asked"
    ipmi_calls = [argv[-3:] for argv, _ in rig.runner.calls if argv[0] == "ipmitool"]
    assert ipmi_calls == [["chassis", "power", "soft"], ["chassis", "power", "on"]]
    assert rig.slept == [5], "the quirk's settle after power-off"
    journal = [
        json.loads(line)
        for line in (tmp_path / "Validation" / "Power" / f"{ALIAS}.jsonl").read_text().splitlines()
    ]
    assert journal[1]["how"] == "ipmitool chassis power soft"

    inventory = rig.hal.inventory(ALIAS)
    gpu3 = inventory.device(GPU3)
    assert gpu3 is not None and gpu3.width == 4, "read in-band, where LnkSta is trustworthy"
    assert any("lspci" in remote_argv(argv) for argv, _ in rig.runner.calls)
    notes = [e for e in journal if "note" in e] + [
        json.loads(line)
        for line in (tmp_path / "Validation" / "Power" / f"{ALIAS}.jsonl").read_text().splitlines()
        if "note" in line
    ]
    assert any(e.get("note") == "PCIe link state read in-band via lspci" for e in notes)
    assert (inventory.model, inventory.bios_version) == ("SLAS-GX8", "2.4.1"), (
        "the rest still from Redfish"
    )
    snapshot = rig.hal.snapshot(ALIAS)
    assert snapshot.power == "on" and len(snapshot.sel) >= 3


def test_without_ssh_the_bmc_power_state_stands_in_for_the_os(tmp_path: Path) -> None:
    rig = LabRig(tmp_path)
    record = rig.registry.get(ALIAS).model_copy(update={"ssh": None})
    rig.registry.put(record)
    rig.registry.arm(ALIAS, by="lee", at=rig.clock.now())
    assert rig.hal.counters(ALIAS).as_dict()["Xid"] == 0, "no in-band access: counters stay zero"
    rig.hal.power(ALIAS, "off")
    assert rig.hal.wait_for_os(ALIAS, timeout_s=30) is False
    rig.hal.power(ALIAS, "on")
    assert rig.hal.wait_for_os(ALIAS, timeout_s=30) is True
    rig.hal.fence(ALIAS, "marker")  # no SOL started, no ssh: still journalled
    assert not any(argv[0] == "ssh" for argv, _ in rig.runner.calls)
    assert rig.hal.console_lines(ALIAS) == []
    with pytest.raises(HalError):
        rig.hal.ssh(ALIAS, ["true"])
