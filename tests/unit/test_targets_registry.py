"""Target records carry references, never secrets; the registry arms and disarms."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from slas_hal.credentials import check_ref
from slas_hal.targets import (
    ArmingError,
    BmcAccess,
    PduOutlet,
    SshAccess,
    TargetError,
    TargetRecord,
    TargetRegistry,
)

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


def record(alias: str = "lab-gx8-01") -> TargetRecord:
    return TargetRecord(
        alias=alias,
        bmc=BmcAccess(host="10.20.30.40", user="slas-validation", password_ref="env:LAB_BMC_PW"),
        ssh=SshAccess(
            host="10.20.30.41",
            user="slas",
            private_key_ref="vault:kv/lab/gx8-01/ssh",
            known_hosts_line="10.20.30.41 ssh-ed25519 AAAA",
        ),
        pdu=PduOutlet(driver="acme-pdu", host="10.20.30.50", outlet=7, credential_ref="env:PDU"),
        vendor_hint="ACME",
    )


def test_a_record_refuses_a_literal_secret_where_a_reference_belongs() -> None:
    with pytest.raises(ValidationError, match="not a reference"):
        BmcAccess(host="bmc", user="root", password_ref="hunter2-in-the-clear")
    with pytest.raises(ValidationError, match="not a reference"):
        PduOutlet(driver="x", host="pdu", outlet=1, credential_ref="password=abc")
    assert check_ref("env:LAB_BMC_PW") == "env:LAB_BMC_PW"
    assert check_ref("file:/run/secrets/bmc") == "file:/run/secrets/bmc"
    with pytest.raises(ValidationError):
        TargetRecord(alias="Bad Alias", bmc=BmcAccess(host="bmc", user="u", password_ref="env:X"))
    with pytest.raises(ValidationError):
        BmcAccess(host="bmc", user="u", password_ref="env:X", pinned_cert_sha256="short")


def test_registry_round_trips_arms_and_disarms(tmp_path: Path) -> None:
    registry = TargetRegistry(tmp_path / "Validation" / "targets.json")
    assert registry.list() == []
    with pytest.raises(TargetError) as info:
        registry.get("lab-gx8-01")
    assert info.value.message.what_happened == "There is no target called lab-gx8-01."

    registry.put(record())
    registry.put(record("lab-gx4-02"))
    assert [r.alias for r in registry.list()] == ["lab-gx4-02", "lab-gx8-01"]
    assert (tmp_path / "Validation" / "targets.json").stat().st_mode & 0o777 == 0o600
    text = (tmp_path / "Validation" / "targets.json").read_text(encoding="utf-8")
    assert "env:LAB_BMC_PW" in text and "hunter" not in text

    stored = registry.get("lab-gx8-01")
    assert stored.power_actions_enabled is False and stored.armed is None
    assert stored.sentence() == (
        "lab-gx8-01: BMC 10.20.30.40 as slas-validation; SSH 10.20.30.41 as slas; "
        "PDU outlet 7 on 10.20.30.50; power actions not armed."
    )
    with pytest.raises(ArmingError) as info:
        registry.require_armed("lab-gx8-01")
    assert info.value.message.likely_cause.startswith("Nobody has confirmed")

    armed = registry.arm("lab-gx8-01", by="lee", at=NOW, note="Rack 4 is clear.")
    assert armed.power_actions_enabled and armed.armed is not None
    assert (armed.armed.by, armed.armed.note) == ("lee", "Rack 4 is clear.")
    assert (
        registry.require_armed("lab-gx8-01")
        .sentence()
        .endswith("armed by lee at 2026-09-14 08:00.")
    )
    assert registry.get("lab-gx4-02").power_actions_enabled is False, "arming is per target"

    disarmed = registry.disarm("lab-gx8-01")
    assert disarmed.power_actions_enabled is False and disarmed.armed is None
    assert registry.remove("lab-gx4-02") is True and registry.remove("lab-gx4-02") is False
    assert [r.alias for r in registry.list()] == ["lab-gx8-01"]
    bare = TargetRecord(alias="lab-x", bmc=BmcAccess(host="bmc", user="u", password_ref="env:X"))
    assert bare.sentence() == "lab-x: BMC bmc as u; no SSH; no PDU; power actions not armed."
