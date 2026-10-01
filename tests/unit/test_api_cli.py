"""`slas-api migrate | bootstrap status | user add | user list | serve` against SQLite."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from slas_api import cli
from slas_api.db import make_engine
from slas_api.models import AuditRow, Person
from slas_api.settings import ADMIN_INITIAL_PASSWORD_SECRET, Settings
from tests.unit.api_harness import INITIAL_PASSWORD, make_settings


def run(settings: Settings, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err, settings=settings)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    made = make_settings(tmp_path)
    (made.slas_secrets_dir / ADMIN_INITIAL_PASSWORD_SECRET).write_text(INITIAL_PASSWORD, "utf-8")
    return made


def people(settings: Settings) -> list[Person]:
    with Session(make_engine(settings.database_url())) as db:
        return list(db.scalars(select(Person).order_by(Person.email)).all())


def audit(settings: Settings) -> list[AuditRow]:
    with Session(make_engine(settings.database_url())) as db:
        return list(db.scalars(select(AuditRow).order_by(AuditRow.id)).all())


def test_migrate_creates_the_administrator_once(settings: Settings) -> None:
    code, out, err = run(settings, "migrate")
    assert code == 0 and err == ""
    assert out.splitlines() == [
        "Migrations are up to date (sqlite).",
        "Created admin@slas.local; sign in with the one-time password from install.sh and "
        "choose a new one.",
    ]
    code, out, _ = run(settings, "migrate")
    assert code == 0 and out.splitlines()[1] == "1 person is registered."
    [admin] = people(settings)
    assert admin.email == "admin@slas.local" and admin.must_change_password
    assert admin.role == "administrator" and admin.bootstrap_consumed_at is None
    assert INITIAL_PASSWORD not in admin.password_hash
    rows = audit(settings)
    assert [r.action for r in rows] == ["person.bootstrapped"]
    assert rows[0].via == "installer" and rows[0].actor == "system" and rows[0].trace_id
    secret = settings.slas_secrets_dir / ADMIN_INITIAL_PASSWORD_SECRET
    assert secret.read_text(encoding="utf-8") == INITIAL_PASSWORD, "never rewritten"


def test_migrate_without_the_secret_says_so_and_still_succeeds(settings: Settings) -> None:
    (settings.slas_secrets_dir / ADMIN_INITIAL_PASSWORD_SECRET).unlink()
    code, out, _ = run(settings, "migrate")
    assert code == 0
    assert "No administrator was created: the secret admin-initial-password is missing." in out
    assert people(settings) == []
    code, out, _ = run(settings, "bootstrap", "status")
    assert code == 0 and out == "pending\n"


def test_bootstrap_status_is_pending_until_the_password_changes(settings: Settings) -> None:
    run(settings, "migrate")
    assert run(settings, "bootstrap", "status")[1] == "pending\n"
    with Session(make_engine(settings.database_url())) as db:
        admin = db.scalar(select(Person))
        assert admin is not None
        admin.must_change_password = False
        admin.bootstrap_consumed_at = admin.created_at
        db.commit()
    assert run(settings, "bootstrap", "status")[1] == "done\n"


def test_user_add_prints_a_one_time_password_once(settings: Settings) -> None:
    run(settings, "migrate")
    code, out, err = run(
        settings,
        "user",
        "add",
        "--email",
        "Ana@Lab.local",
        "--display-name",
        "Ana Lin",
        "--role",
        "engineer",
    )
    assert code == 0 and err == ""
    first, second = out.splitlines()
    assert first.startswith(
        "Ana Lin (ana@lab.local) can sign in as Engineer with the one-time password: "
    )
    one_time = first.rsplit(" ", 1)[1]
    assert len(one_time) >= 16
    assert (
        second == "It works once; they choose their own at first sign-in. You won't see it again."
    )
    ana = next(p for p in people(settings) if p.email == "ana@lab.local")
    assert ana.must_change_password and ana.role == "engineer"
    row = audit(settings)[-1]
    assert row.action == "person.added" and row.via == "cli" and row.actor == "system"
    assert one_time not in row.detail and json.loads(row.detail)["one_time_password_issued"]


def test_user_add_reads_the_password_from_stdin(settings: Settings) -> None:
    run(settings, "migrate")
    code, out, _ = run(
        settings,
        "user",
        "add",
        "--email",
        "pat@lab.local",
        "--display-name",
        "Pat",
        "--role",
        "viewer",
        "--password-stdin",
        stdin="pats-long-password-1\n",
    )
    assert code == 0
    assert out == "Pat (pat@lab.local) can sign in as Viewer with the password you provided.\n"
    pat = next(p for p in people(settings) if p.email == "pat@lab.local")
    assert not pat.must_change_password
    assert "pats-long-password-1" not in pat.password_hash
    code, out, err = run(
        settings,
        "user",
        "add",
        "--email",
        "short@lab.local",
        "--display-name",
        "S",
        "--role",
        "viewer",
        "--password-stdin",
        stdin="short\n",
    )
    assert code == 1 and out == ""
    assert err.splitlines()[0] == (
        "The password is too short: it has 5 characters and needs at least 12."
    )


def test_user_add_errors_are_three_lines_and_exit_1(settings: Settings) -> None:
    run(settings, "migrate")
    code, out, err = run(
        settings,
        "user",
        "add",
        "--email",
        "admin@slas.local",
        "--display-name",
        "Twice",
        "--role",
        "engineer",
    )
    assert code == 1 and out == ""
    assert err.splitlines() == [
        "Someone already signs in as admin@slas.local.",
        "Likely cause: The address belongs to an existing person, maybe switched off.",
        "What to do: Use another address, or switch the existing account back on.",
    ]
    code, _, err = run(
        settings, "user", "add", "--email", "x@lab.local", "--display-name", "X", "--role", "boss"
    )
    assert code == 1 and err.startswith("There is no role called boss.")


def test_user_list_is_one_line_per_person(settings: Settings) -> None:
    run(settings, "migrate")
    code, out, _ = run(settings, "user", "list")
    assert code == 0
    assert out == "admin@slas.local\tadministrator\tmust choose a password\tlast sign-in never\n"
    run(
        settings,
        "user",
        "add",
        "--email",
        "pat@lab.local",
        "--display-name",
        "Pat",
        "--role",
        "viewer",
        "--password-stdin",
        stdin="pats-long-password-1\n",
    )
    lines = run(settings, "user", "list")[1].splitlines()
    assert lines[1] == "pat@lab.local\tviewer\tcan sign in\tlast sign-in never"


def test_user_list_on_an_empty_table(settings: Settings) -> None:
    (settings.slas_secrets_dir / ADMIN_INITIAL_PASSWORD_SECRET).unlink()
    run(settings, "migrate")
    assert run(settings, "user", "list")[1].startswith("Nobody is registered yet;")


def test_serve_migrates_then_runs_uvicorn_on_the_bind_address(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(app: object, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    code, out, _ = run(settings, "serve")
    assert code == 0 and "Migrations are up to date (sqlite)." in out
    assert len(calls) == 1
    assert calls[0]["host"] == "0.0.0.0" and calls[0]["port"] == 8000  # noqa: S104
    assert calls[0]["app"].title == "SW Local Agent Service api"
    assert [p.email for p in people(settings)] == ["admin@slas.local"]


def test_main_reads_settings_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = make_settings(tmp_path)
    monkeypatch.setenv("SLAS_DATA_ROOT", str(made.slas_data_root))
    monkeypatch.setenv("SLAS_SECRETS_DIR", str(made.slas_secrets_dir))
    monkeypatch.setenv("SLAS_DATABASE_URL", made.slas_database_url)
    monkeypatch.setenv("SLAS_ARGON2_PROFILE", "test")
    monkeypatch.setenv("SLAS_ROLES_FILE", str(made.slas_roles_file))
    out = io.StringIO()
    assert cli.main(["migrate"], stdout=out, stderr=io.StringIO()) == 0
    assert "Migrations are up to date (sqlite)." in out.getvalue()
    assert "No administrator was created" in out.getvalue()
