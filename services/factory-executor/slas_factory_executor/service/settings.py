"""Settings for `slas-factory-executor serve`, read from the environment once at start
(docs/api-contract-round-2.md §1 and §6; compose sets every variable).

    SLAS_DATA_ROOT          /data                       Factory/ and Backups/stations/ under it
    SLAS_BIND               0.0.0.0:8000                the address compose probes
    ENROLMENT_LISTEN        0.0.0.0:8444                TLS endpoint the stations enrol at
    ENROLMENT_HOSTS         factory-executor            names/IPs in the executor certificate
    RUNNER_MTLS_CA          /data/Factory/ca/ca.pem     the CA that signs runner certificates
    MES_ADAPTER             file_drop                   the only adapter until decision 8
    MES_POLL_INTERVAL_S     30                          the poller thread's period
    SLAS_FACTORY_SETTINGS   /etc/slas/factory.yaml      code rules, lease hours, retention
    FACTORY_BATCH_KEY_REF   env:FACTORY_BATCH_KEY       line-wide batch key for stations
    FACTORY_BATCH_KEY_ID    factory-line                enrolled without a key of their own
    SLAS_GATEWAY_URL        (empty)                     set: the verdict's voters (§5.3)
    VNC_TUNNEL_LISTEN       127.0.0.1:0                 where the operator's relay listens
    CREDENTIAL_SOURCE       env | vault; SLAS_PROFILE quickstart | prod; VAULT_* (prod)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from slas_factory_executor.settings import (
    FactorySettings,
    FactorySettingsError,
    default_settings,
    settings_from_mapping,
)
from slas_hal.credentials import CredentialError, CredentialResolver, resolver_for
from slas_http.serve import DEFAULT_BIND
from slas_observability.events import EventLog
from slas_schemas.errors import ThreePartMessage
from slas_schemas.vault import UrllibVaultHttp, VaultError, VaultKv

DEFAULT_DATA_ROOT: Final = "/data"
DEFAULT_ENROLMENT_LISTEN: Final = "0.0.0.0:8444"
DEFAULT_ENROLMENT_HOSTS: Final = "factory-executor"
DEFAULT_RUNNER_MTLS_CA: Final = "/data/Factory/ca/ca.pem"
DEFAULT_FACTORY_SETTINGS: Final = "/etc/slas/factory.yaml"
DEFAULT_BATCH_KEY_REF: Final = "env:FACTORY_BATCH_KEY"
DEFAULT_BATCH_KEY_ID: Final = "factory-line"
DEFAULT_VNC_TUNNEL_LISTEN: Final = "127.0.0.1:0"
MES_ADAPTERS: Final = ("file_drop",)


class StartError(RuntimeError):
    """The service cannot start; the message says why and what to do."""

    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


@dataclass(frozen=True)
class Settings:
    data_root: Path
    bind: str
    enrolment_listen: str
    enrolment_hosts: tuple[str, ...]
    runner_mtls_ca: Path
    mes_adapter: str
    mes_poll_interval_s: float
    factory_settings: Path
    batch_key_ref: str
    batch_key_id: str
    gateway_url: str
    vnc_tunnel_listen: str
    profile: str
    credential_source: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        profile = env.get("SLAS_PROFILE", "quickstart")
        hosts = tuple(
            part.strip()
            for part in env.get("ENROLMENT_HOSTS", DEFAULT_ENROLMENT_HOSTS).split(",")
            if part.strip()
        )
        return cls(
            data_root=Path(env.get("SLAS_DATA_ROOT", DEFAULT_DATA_ROOT)),
            bind=env.get("SLAS_BIND", DEFAULT_BIND),
            enrolment_listen=env.get("ENROLMENT_LISTEN", DEFAULT_ENROLMENT_LISTEN),
            enrolment_hosts=hosts or (DEFAULT_ENROLMENT_HOSTS,),
            runner_mtls_ca=Path(env.get("RUNNER_MTLS_CA", DEFAULT_RUNNER_MTLS_CA)),
            mes_adapter=env.get("MES_ADAPTER", "file_drop"),
            mes_poll_interval_s=float(env.get("MES_POLL_INTERVAL_S", "30")),
            factory_settings=Path(env.get("SLAS_FACTORY_SETTINGS", DEFAULT_FACTORY_SETTINGS)),
            batch_key_ref=env.get("FACTORY_BATCH_KEY_REF", DEFAULT_BATCH_KEY_REF),
            batch_key_id=env.get("FACTORY_BATCH_KEY_ID", DEFAULT_BATCH_KEY_ID),
            gateway_url=env.get("SLAS_GATEWAY_URL", "").strip(),
            vnc_tunnel_listen=env.get("VNC_TUNNEL_LISTEN", DEFAULT_VNC_TUNNEL_LISTEN),
            profile=profile,
            credential_source=env.get("CREDENTIAL_SOURCE", "vault" if profile == "prod" else "env"),
        )

    @property
    def factory_dir(self) -> Path:
        return self.data_root / "Factory"

    @property
    def ca_dir(self) -> Path:
        return self.factory_dir / "ca"

    @property
    def templates_dir(self) -> Path:
        return self.factory_dir / "Templates"

    @property
    def mes_dir(self) -> Path:
        return self.factory_dir / "mes"

    def sentences(self) -> list[str]:
        voters = (
            f"Verdict voters through the gateway at {self.gateway_url}."
            if self.gateway_url
            else "No gateway configured: every passing unit goes to the line lead."
        )
        return [
            f"Serving on {self.bind}; job files under {self.factory_dir}.",
            f"Stations enrol at {self.enrolment_listen} "
            f"(certificate for {', '.join(self.enrolment_hosts)}); runners are trusted "
            f"through {self.runner_mtls_ca}.",
            f"MES tickets arrive by {self.mes_adapter} under {self.mes_dir}, "
            f"polled every {self.mes_poll_interval_s:g} s.",
            voters,
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
                f"Check the volume that mounts {path} into the factory executor.",
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


def load_factory_settings(path: Path, log: EventLog) -> FactorySettings:
    if not path.exists():
        log.warning("factory_settings.default", path=str(path))
        return default_settings()
    try:
        return settings_from_mapping(_read_yaml(path, what="factory settings"), source=str(path))
    except FactorySettingsError as exc:
        raise StartError(exc.message) from None


def check_mes_adapter(settings: Settings) -> None:
    if settings.mes_adapter not in MES_ADAPTERS:
        raise StartError(
            ThreePartMessage(
                f"MES_ADAPTER is {settings.mes_adapter!r}, which this build does not have.",
                "Only the file-drop adapter exists until the MES integration is decided "
                "(CLAUDE.md §15, open decision 8).",
                "Set MES_ADAPTER=file_drop and drop production tickets as JSON under "
                f"{settings.mes_dir / 'inbox'}.",
            )
        )


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
    """env: and file: references (the per-station batch keys under Factory/keys) in
    quickstart; Vault with the same fallback in prod (INV-5)."""
    try:
        kv = vault_from_environ(env) if settings.credential_source == "vault" else None
        return resolver_for(settings.profile, environ=env, root=settings.data_root, kv=kv)
    except CredentialError as exc:
        raise StartError(exc.message) from None
