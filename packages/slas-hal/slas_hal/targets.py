"""Target records and the arming gate (CLAUDE.md §1.2 value 2, §5.2 `oob`/`inband`, INV-5).

    TargetRecord  alias · BMC address and user · SSH address and user · optional PDU outlet
                  · credential REFERENCES only · vendor hint · power_actions_enabled

`power_actions_enabled` is false for every new target. A person confirms the machine is
free and runs `slas target arm <alias>`; until then every driver refuses to change power,
whatever the plan says. Arming is recorded with who and when, and cleared by `disarm`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator

from slas_hal.credentials import check_ref
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage

ALIAS: Final = r"^[a-z][a-z0-9-]{1,62}$"
HOSTNAME: Final = r"^[A-Za-z0-9.-]{1,253}$|^\[[0-9A-Fa-f:.]+\]$"


class TargetError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class BmcAccess(SlasModel):
    host: str = Field(pattern=HOSTNAME)
    user: str = Field(min_length=1)
    password_ref: str
    https_port: int = Field(default=443, ge=1, le=65535)
    ipmi_port: int = Field(default=623, ge=1, le=65535)
    #: Path to the CA bundle that signed the BMC certificate; None = the platform bundle.
    ca_bundle: str | None = None
    #: A BMC with a self-signed certificate an administrator pinned by fingerprint.
    pinned_cert_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("password_ref")
    @classmethod
    def _reference_only(cls, value: str) -> str:
        return check_ref(value)


class SshAccess(SlasModel):
    host: str = Field(pattern=HOSTNAME)
    user: str = Field(min_length=1)
    private_key_ref: str
    port: int = Field(default=22, ge=1, le=65535)
    #: The `known_hosts` line an administrator pinned; ssh runs StrictHostKeyChecking=yes.
    known_hosts_line: str = Field(min_length=1)

    @field_validator("private_key_ref")
    @classmethod
    def _reference_only(cls, value: str) -> str:
        return check_ref(value)


class PduOutlet(SlasModel):
    #: The driver name from `slas_hal.drivers.pdu`; the model-specific driver is a TODO(SLAS-HAL).
    driver: str = Field(min_length=1)
    host: str = Field(pattern=HOSTNAME)
    outlet: int = Field(ge=1, le=64)
    credential_ref: str

    @field_validator("credential_ref")
    @classmethod
    def _reference_only(cls, value: str) -> str:
        return check_ref(value)


class Arming(SlasModel):
    by: str = Field(min_length=1)
    at: datetime
    note: str = ""


class TargetRecord(SlasModel):
    alias: str = Field(pattern=ALIAS)
    bmc: BmcAccess
    ssh: SshAccess | None = None
    pdu: PduOutlet | None = None
    #: `manufacturer`/`firmware` hints the quirk layer uses before the first Redfish answer.
    vendor_hint: str | None = None
    firmware_hint: str | None = None
    kind: Literal["server"] = "server"
    #: The gate: false until a person confirms the machine is free (`slas target arm`).
    power_actions_enabled: bool = False
    armed: Arming | None = None

    def sentence(self) -> str:
        parts = [f"{self.alias}: BMC {self.bmc.host} as {self.bmc.user}"]
        parts.append(f"SSH {self.ssh.host} as {self.ssh.user}" if self.ssh else "no SSH")
        parts.append(f"PDU outlet {self.pdu.outlet} on {self.pdu.host}" if self.pdu else "no PDU")
        if self.power_actions_enabled and self.armed is not None:
            parts.append(f"armed by {self.armed.by} at {self.armed.at:%Y-%m-%d %H:%M}")
        else:
            parts.append("power actions not armed")
        return "; ".join(parts) + "."


class ArmingError(TargetError):
    def __init__(self, alias: str) -> None:
        super().__init__(
            ThreePartMessage(
                f"Power actions on {alias} are not armed.",
                "Nobody has confirmed that the machine is free; every new target starts this "
                "way, and `slas target disarm` puts it back.",
                f"Confirm the target is free, run `slas target arm {alias}`, then start the "
                "run again.",
            )
        )


class TargetRegistry:
    """`${SLAS_DATA_ROOT}/Validation/targets.json`: every record, mode 0600, atomic writes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, TargetRecord]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {alias: TargetRecord.model_validate(item) for alias, item in raw.items()}

    def _save(self, items: dict[str, TargetRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            self.path,
            json.dumps({a: r.model_dump(mode="json") for a, r in items.items()}, indent=2) + "\n",
            mode=0o600,
        )

    def list(self) -> list[TargetRecord]:
        return sorted(self._load().values(), key=lambda r: r.alias)

    def get(self, alias: str) -> TargetRecord:
        try:
            return self._load()[alias]
        except KeyError:
            raise TargetError(
                ThreePartMessage(
                    f"There is no target called {alias}.",
                    "The lab inventory does not know it.",
                    "Add it with `slas target add`, or pick one from `slas target list`.",
                )
            ) from None

    def put(self, record: TargetRecord) -> TargetRecord:
        items = self._load()
        items[record.alias] = record
        self._save(items)
        return record

    def remove(self, alias: str) -> bool:
        items = self._load()
        if alias not in items:
            return False
        del items[alias]
        self._save(items)
        return True

    def arm(self, alias: str, *, by: str, at: datetime, note: str = "") -> TargetRecord:
        record = self.get(alias).model_copy(
            update={"power_actions_enabled": True, "armed": Arming(by=by, at=at, note=note)}
        )
        return self.put(record)

    def disarm(self, alias: str) -> TargetRecord:
        record = self.get(alias).model_copy(update={"power_actions_enabled": False, "armed": None})
        return self.put(record)

    def require_armed(self, alias: str) -> TargetRecord:
        record = self.get(alias)
        if not record.power_actions_enabled:
            raise ArmingError(alias)
        return record
