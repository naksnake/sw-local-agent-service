"""Settings for `slas-validation-executor serve`, read from the environment once at start
(docs/api-contract-round-2.md §1 and §6; compose sets every variable).

    SLAS_DATA_ROOT      /data                       Validation/ lives under it
    SLAS_BIND           0.0.0.0:8000                the address compose probes
    SYSLOG_LISTEN       0.0.0.0:5514                UDP syslog from the targets (lab VLAN)
    GUARDRAIL_POLICY    /etc/slas/guardrails.yaml   CLAUDE.md §10.2 limits
    BMC_QUIRKS          /etc/slas/bmc-quirks.yaml   BMC quirk shims (open decision 4)
    CREDENTIAL_SOURCE   env | vault                 read by slas_hal.credentials.resolver_for
    SLAS_PROFILE        quickstart | prod
    VAULT_ADDR, VAULT_CACERT, VAULT_KV_MOUNT, VAULT_ROLE_ID_FILE, VAULT_SECRET_ID_FILE (prod)

Files are loaded with `yaml.safe_load` and validated by the models that render them; a
missing file means the shipped defaults, a broken file stops the start with a sentence.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from slas_hal.credentials import CredentialError, CredentialResolver, resolver_for
from slas_hal.quirks import QuirkError, QuirkTable, default_quirks, quirks_from_mapping
from slas_http.serve import DEFAULT_BIND
from slas_observability.events import EventLog
from slas_schemas.errors import ThreePartMessage
from slas_schemas.vault import UrllibVaultHttp, VaultError, VaultKv
from slas_validation_executor.guardrails import (
    GuardrailError,
    Guardrails,
    default_guardrails,
    guardrails_from_mapping,
)

DEFAULT_DATA_ROOT: Final = "/data"
DEFAULT_SYSLOG_LISTEN: Final = "0.0.0.0:5514"
DEFAULT_GUARDRAIL_POLICY: Final = "/etc/slas/guardrails.yaml"
DEFAULT_BMC_QUIRKS: Final = "/etc/slas/bmc-quirks.yaml"


class StartError(RuntimeError):
    """The service cannot start; the message says why and what to do."""

    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


@dataclass(frozen=True)
class Settings:
    data_root: Path
    bind: str
    syslog_listen: str
    guardrail_policy: Path
    bmc_quirks: Path
    profile: str
    credential_source: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        profile = env.get("SLAS_PROFILE", "quickstart")
        return cls(
            data_root=Path(env.get("SLAS_DATA_ROOT", DEFAULT_DATA_ROOT)),
            bind=env.get("SLAS_BIND", DEFAULT_BIND),
            syslog_listen=env.get("SYSLOG_LISTEN", DEFAULT_SYSLOG_LISTEN),
            guardrail_policy=Path(env.get("GUARDRAIL_POLICY", DEFAULT_GUARDRAIL_POLICY)),
            bmc_quirks=Path(env.get("BMC_QUIRKS", DEFAULT_BMC_QUIRKS)),
            profile=profile,
            credential_source=env.get("CREDENTIAL_SOURCE", "vault" if profile == "prod" else "env"),
        )

    @property
    def validation_dir(self) -> Path:
        return self.data_root / "Validation"

    def sentences(self) -> list[str]:
        return [
            f"Serving on {self.bind}; run files under {self.validation_dir}.",
            f"Syslog from the targets on {self.syslog_listen}.",
            f"Guardrails from {self.guardrail_policy}, BMC quirks from {self.bmc_quirks}.",
            f"Credentials resolve from {self.credential_source} ({self.profile} profile).",
        ]


def _read_yaml(path: Path, *, what: str) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StartError(
            ThreePartMessage(
                f"The {what} file {path} could not be read.",
                str(exc),
                f"Check the volume that mounts {path} into the validation executor.",
            )
        ) from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StartError(
            ThreePartMessage(
                f"The {what} file {path} is not valid YAML.",
                str(exc).splitlines()[0] if str(exc) else "the parser gave no detail",
                f"Fix {path}; the shipped copy under config/ is a valid starting point.",
            )
        ) from None


def load_guardrails(path: Path, log: EventLog) -> Guardrails:
    if not path.exists():
        log.warning("guardrails.default", path=str(path))
        return default_guardrails()
    try:
        return guardrails_from_mapping(_read_yaml(path, what="guardrails"), source=str(path))
    except GuardrailError as exc:
        raise StartError(exc.message) from None


def load_quirks(path: Path, log: EventLog) -> QuirkTable:
    if not path.exists():
        log.warning("quirks.default", path=str(path))
        return default_quirks()
    try:
        return quirks_from_mapping(_read_yaml(path, what="BMC quirks"), source=str(path))
    except QuirkError as exc:
        raise StartError(exc.message) from None


def vault_from_environ(env: Mapping[str, str]) -> VaultKv | None:
    """The Vault client of the prod profile, logged in with AppRole; None without VAULT_ADDR."""
    address = env.get("VAULT_ADDR", "").strip()
    if not address:
        return None
    cacert = env.get("VAULT_CACERT", "").strip()
    kv = VaultKv(
        address,
        mount=env.get("VAULT_KV_MOUNT", "slas"),
        http=UrllibVaultHttp(cafile=Path(cacert) if cacert else None),
    )
    role_file = env.get("VAULT_ROLE_ID_FILE", "/run/secrets/vault_approle_role_id")
    secret_file = env.get("VAULT_SECRET_ID_FILE", "/run/secrets/vault_approle_secret_id")
    try:
        kv.login_from_files(Path(role_file), Path(secret_file))
    except VaultError as exc:
        raise StartError(exc.message) from None
    return kv


def build_resolver(settings: Settings, env: Mapping[str, str]) -> CredentialResolver:
    """env: and file: references in quickstart; Vault with the same fallback in prod (INV-5)."""
    try:
        kv = vault_from_environ(env) if settings.credential_source == "vault" else None
        return resolver_for(settings.profile, environ=env, root=settings.data_root, kv=kv)
    except CredentialError as exc:
        raise StartError(exc.message) from None
