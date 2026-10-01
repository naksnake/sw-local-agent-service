"""A running api for the unit tests: SQLite, an in-memory throttle, a fixed clock, a log sink.

`make_harness(tmp_path)` migrates, bootstraps `admin@slas.local` from a secret file and hands
back a `TestClient` over https (so the Secure cookie is sent). Nothing here reaches a network
or a real Postgres; `slas_api.db.migrate()` runs the real Alembic scripts on SQLite.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from slas_api.app import build_services, create_app
from slas_api.db import make_engine, migrate
from slas_api.models import AuditRow
from slas_api.service import Downstream, Services, bootstrap
from slas_api.settings import ADMIN_INITIAL_PASSWORD_SECRET, Settings
from slas_api.throttle import MemoryThrottle
from slas_http import ServiceClient
from slas_observability.events import EventLog, ListSink

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLES_FILE = REPO_ROOT / "config" / "rbac-roles.yaml"

ADMIN_EMAIL = "admin@slas.local"
INITIAL_PASSWORD = "Initial-One-Time-Pass-1"
ADMIN_PASSWORD = "a-brand-new-admin-password"
WEBUI = {"X-Requested-With": "slas-webui"}
BASE_URL = "https://testserver"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 17, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data_root = tmp_path / "data"
    secrets = tmp_path / "secrets"
    data_root.mkdir(parents=True, exist_ok=True)
    secrets.mkdir(parents=True, exist_ok=True)
    roles_file = tmp_path / "rbac-roles.yaml"
    if not roles_file.exists():
        shutil.copy(ROLES_FILE, roles_file)
    values: dict[str, Any] = {
        "slas_data_root": data_root,
        "slas_secrets_dir": secrets,
        "slas_database_url": f"sqlite+pysqlite:///{tmp_path / 'api.db'}",
        "slas_argon2_profile": "test",
        "slas_roles_file": roles_file,
        "slas_version": "1.2.3",
        "slas_https_port": "8443",
        "slas_tls_mode": "self-signed",
        "slas_tls_names": "127.0.0.1, localhost, lab3.internal",
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class Harness:
    settings: Settings
    services: Services
    throttle: MemoryThrottle
    clock: FakeClock
    sink: ListSink
    app: FastAPI
    client: TestClient
    admin_password: str = INITIAL_PASSWORD
    people: dict[str, str] = field(default_factory=dict)

    # --- clients -----------------------------------------------------------------------------

    def new_client(self) -> TestClient:
        return TestClient(self.app, base_url=BASE_URL)

    def sign_in(
        self,
        email: str,
        password: str,
        *,
        client: TestClient | None = None,
        address: str | None = None,
    ) -> httpx.Response:
        headers = dict(WEBUI)
        if address is not None:
            headers["X-Forwarded-For"] = address
        response: httpx.Response = (client or self.client).post(
            "/api/v1/session", json={"email": email, "password": password}, headers=headers
        )
        return response

    def sign_in_admin(self, client: TestClient | None = None) -> TestClient:
        """Signs in as the administrator, replacing the one-time password the first time."""
        target = client or self.client
        response = self.sign_in(ADMIN_EMAIL, self.admin_password, client=target)
        assert response.status_code == 200, response.text
        if response.json()["must_change_password"]:
            changed = target.post(
                "/api/v1/me/password",
                json={"current_password": self.admin_password, "new_password": ADMIN_PASSWORD},
                headers=WEBUI,
            )
            assert changed.status_code == 200, changed.text
            self.admin_password = ADMIN_PASSWORD
        return target

    def add_person(
        self, email: str, display_name: str, role: str, *, client: TestClient | None = None
    ) -> tuple[dict[str, Any], str]:
        response = (client or self.client).post(
            "/api/v1/admin/people",
            json={"email": email, "display_name": display_name, "role": role},
            headers=WEBUI,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.people[email.lower()] = body["one_time_password"]
        return body["person"], body["one_time_password"]

    def client_for(self, email: str, one_time_password: str, new_password: str) -> TestClient:
        """A fresh client signed in as `email`, past the one-time password."""
        client = self.new_client()
        response = self.sign_in(email, one_time_password, client=client)
        assert response.status_code == 200, response.text
        changed = client.post(
            "/api/v1/me/password",
            json={"current_password": one_time_password, "new_password": new_password},
            headers=WEBUI,
        )
        assert changed.status_code == 200, changed.text
        return client

    # --- inspection --------------------------------------------------------------------------

    def records(self, event: str | None = None) -> list[dict[str, Any]]:
        records = self.sink.records()
        return [r for r in records if event is None or r["event"] == event]

    def audit_rows(self) -> list[AuditRow]:
        with Session(self.services.engine) as db:
            return list(db.scalars(select(AuditRow).order_by(AuditRow.id)).all())

    @property
    def env_file(self) -> Path:
        return self.settings.env_file


#: The compose service name of every downstream, as `slas logs <service>` names it.
DOWNSTREAM_SERVICES = {
    "orchestrator": "agent-core-orchestrator",
    "git_broker": "git-broker",
    "sandbox_manager": "sandbox-manager",
    "factory_executor": "factory-executor",
    "model_manager": "model-manager",
    "model_fetcher": "model-fetcher",
}


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no downstream in this test", request=request)


def fake_downstream(
    handlers: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
) -> Downstream:
    """A `Downstream` over `httpx.MockTransport`: one handler per field, unreachable otherwise."""
    given = handlers or {}
    clients = {
        field_name: ServiceClient(
            service,
            f"http://{service}:8000",
            transport=httpx.MockTransport(given.get(field_name, _unreachable)),
        )
        for field_name, service in DOWNSTREAM_SERVICES.items()
    }
    return Downstream(**clients)


def make_harness(
    tmp_path: Path,
    *,
    initial_env: str | None = None,
    bootstrap_admin: bool = True,
    migrate_first: bool = True,
    downstream: Downstream | None = None,
    **overrides: Any,
) -> Harness:
    settings = make_settings(tmp_path, **overrides)
    if bootstrap_admin:
        (settings.slas_secrets_dir / ADMIN_INITIAL_PASSWORD_SECRET).write_text(
            INITIAL_PASSWORD + "\n", encoding="utf-8"
        )
    if initial_env is not None:
        settings.env_file.write_text(initial_env, encoding="utf-8")
    sink = ListSink()
    clock = FakeClock()
    engine = make_engine(settings.database_url())
    if migrate_first:
        migrate(engine)
    services = build_services(
        settings,
        engine=engine,
        clock=clock,
        log=EventLog("api", sink),
        # No unit test reaches a network: every downstream is a fake unless given.
        downstream=downstream if downstream is not None else fake_downstream(),
    )
    if bootstrap_admin:
        bootstrap(services)
    throttle = MemoryThrottle(clock)
    app = create_app(settings, throttle=throttle, services=services)
    client = TestClient(app, base_url=BASE_URL)
    return Harness(settings, services, throttle, clock, sink, app, client)


def problem_keys(body: dict[str, Any]) -> set[str]:
    return set(body)
