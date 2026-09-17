"""`create_app()`: the git-broker service app (docs/api-contract-round-2.md §7, ADR-0015).

Wiring per CLAUDE.md §5.7: the credential store is `EncryptedFileStore` under
`${SLAS_DATA_ROOT}/.git-broker/credentials.json`, sealed with a key derived from
`SLAS_SECRET_KEY`; the audit log is `.git-broker/audit.jsonl`; the host allowlist is read from
`GIT_HOSTS_ALLOWLIST` and written back by `POST /v1/hosts`. Every collaborator is injectable
so the tests run the same app against the fake Git host on loopback.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute

from slas_git.audit import AuditLog
from slas_git.credentials import (
    AesGcmSealer,
    CredentialError,
    EncryptedFileStore,
    FakeSealer,
    Sealer,
    derive_key,
)
from slas_git.gate import CrossChecker
from slas_git.hostapi import HttpClient, UrllibHttpClient
from slas_git.hosts import GitHosts
from slas_git.redact import redact
from slas_git.remotes import RemoteStore
from slas_git_broker.askpass import install_askpass
from slas_git_broker.broker import Clock, GitBroker
from slas_git_broker.runner import LocalProcessRunner, ProcessRunner
from slas_git_broker.service import routes
from slas_git_broker.service.settings import Settings
from slas_git_broker.service.state import BrokerState, HostsFile
from slas_http.app import create_service_app
from slas_observability.events import EventLog, StreamSink
from slas_schemas.errors import ThreePartMessage

SERVICE: Final = "git-broker"
VERSION: Final = "0.0.1"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SealerUnavailableError(CredentialError):
    """The configured sealer cannot run on this host; credential routes answer 503."""


class UnavailableSealer:
    """Stands in when AES-GCM is configured but its package is not installed: nothing can be
    sealed or opened, and every attempt says why in three parts."""

    name = "unavailable"

    def __init__(self, message: ThreePartMessage) -> None:
        self.message = message

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes:
        raise SealerUnavailableError(self.message)

    def open(self, ciphertext: bytes, *, aad: bytes) -> bytes:
        raise SealerUnavailableError(self.message)


def build_sealer(kind: str, secret_key: str) -> Sealer:
    """`aes-gcm` needs the `cryptography` package; until it is approved the service still
    serves projects, hosts and bundles, and says so on every credential route."""
    key = derive_key(secret_key)
    if kind == "fake-for-tests":
        return FakeSealer(key)
    try:
        return AesGcmSealer(key)
    except CredentialError as exc:
        return UnavailableSealer(exc.message)


def build_broker(
    settings: Settings,
    *,
    hosts: GitHosts,
    sealer: Sealer,
    runner: ProcessRunner | None = None,
    http: HttpClient | None = None,
    clock: Clock | None = None,
    cross_checker: CrossChecker | None = None,
) -> GitBroker:
    return GitBroker(
        runner=runner or LocalProcessRunner(base_path=settings.git_path),
        remotes=RemoteStore(settings.remotes_path),
        credentials=EncryptedFileStore(settings.credentials_path, sealer),
        hosts=hosts,
        audit=AuditLog(settings.audit_path),
        key_dir=settings.key_dir,
        askpass_path=install_askpass(settings.askpass_directory),
        http=http or UrllibHttpClient(ca_bundle=settings.ca_bundle),
        clock=clock or SystemClock(),
        data_root=settings.data_root,
        cross_checker=cross_checker,
    )


def health_checks(state: BrokerState, *, git_path: str | None) -> Callable[[], dict[str, str]]:
    """`git` on the runner's PATH, a writable `.git-broker/` directory, a usable sealer."""

    def checks() -> dict[str, str]:
        path = git_path or getattr(state.broker.runner, "base_path", None) or os.defpath
        git_ok = shutil.which("git", path=path) is not None
        store_dir = state.broker.audit.path.parent
        try:
            store_dir.mkdir(parents=True, exist_ok=True)
            store_ok = os.access(store_dir, os.W_OK)
        except OSError:
            store_ok = False
        sealer = getattr(state.broker.credentials, "sealer", None)
        sealer_ok = sealer is None or sealer.name != UnavailableSealer.name
        return {
            "git": "ok" if git_ok else "missing",
            "store": "ok" if store_ok else "unwritable",
            "sealer": "ok" if sealer_ok else "missing",
        }

    return checks


def create_app(
    *,
    broker: GitBroker,
    hosts: HostsFile,
    log: EventLog | None = None,
    git_path: str | None = None,
) -> FastAPI:
    """The service app over an already-built broker; `hosts` is the allowlist file the
    broker's `hosts` were loaded from (the caller keeps the two in step)."""
    event_log = log if log is not None else EventLog(SERVICE, StreamSink(), redact=redact)
    state = BrokerState(broker, hosts, event_log)
    app = create_service_app(
        SERVICE,
        version=VERSION,
        checks=health_checks(state, git_path=git_path),
        log=event_log,
        routers=(routes.router,),
    )
    app.state.broker = state
    return app


def create_app_from_settings(settings: Settings, *, log: EventLog | None = None) -> FastAPI:
    """What `slas-git-broker serve` runs: settings → sealer → hosts → broker → app.

    Raises `CredentialError` when no secret key is available and `GitHostsError` when the
    allowlist file is unusable; both carry three parts for the operator.
    """
    event_log = log if log is not None else EventLog(SERVICE, StreamSink(), redact=redact)
    sealer = build_sealer(settings.sealer, settings.read_secret_key())
    if sealer.name == UnavailableSealer.name:
        event_log.warning(
            "sealer.unavailable",
            sentence="Credential routes answer 503 until the AES-GCM sealer is available.",
        )
    elif sealer.name == FakeSealer.name:
        event_log.warning(
            "sealer.fake",
            sentence="SLAS_SEALER=fake-for-tests obfuscates credentials; it is not encryption.",
        )
    hosts_file = HostsFile(settings.hosts_file)
    hosts = hosts_file.load()
    if not settings.hosts_file.exists():
        event_log.warning(
            "hosts.defaults",
            path=str(settings.hosts_file),
            sentence="The allowlist file is absent; the shipped defaults are in use.",
        )
    broker = build_broker(settings, hosts=hosts, sealer=sealer)
    return create_app(broker=broker, hosts=hosts_file, log=event_log, git_path=settings.git_path)


def route_table() -> list[tuple[str, str]]:
    """Every (method, path) the app serves, sorted; a test compares it with the contract."""
    pairs: set[tuple[str, str]] = {("GET", "/health"), ("GET", "/metrics")}
    for route in routes.router.routes:
        if isinstance(route, APIRoute):
            pairs.update((method, route.path) for method in route.methods or ())
    return sorted(pairs)
