"""Admin → People and Admin → Settings: the routes, their rules, the audit trail, the mirror."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slas_api.runtime_settings import MARKER
from tests.unit.api_harness import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    WEBUI,
    Harness,
    make_harness,
)
from tests.unit.test_api_session import assert_problem

OTP = re.compile(r"^[a-z2-9]{4}(-[a-z2-9]{4}){3}$")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)


@pytest.fixture
def admin(harness: Harness) -> TestClient:
    return harness.sign_in_admin()


# --- people ----------------------------------------------------------------------------------


def test_add_person_returns_a_one_time_password_that_works_once(
    harness: Harness, admin: TestClient
) -> None:
    person, one_time = harness.add_person("Ana@Lab.local", "Ana Lin", "engineer", client=admin)
    assert person["email"] == "ana@lab.local", "stored lowercased"
    assert person["must_change_password"] is True and person["role_label"] == "Engineer"
    assert OTP.match(one_time) and len(one_time) >= 16
    ana = harness.client_for("ana@lab.local", one_time, "anas-own-long-password")
    assert ana.get("/api/v1/me").json()["must_change_password"] is False
    again = harness.sign_in("ana@lab.local", one_time, client=harness.new_client())
    assert again.status_code == 401, "the one-time password stopped working"


def test_people_are_listed_by_display_name(harness: Harness, admin: TestClient) -> None:
    harness.add_person("zoe@lab.local", "Zoe", "viewer", client=admin)
    harness.add_person("bo@lab.local", "Bo", "line_lead", client=admin)
    names = [p["display_name"] for p in admin.get("/api/v1/admin/people").json()]
    assert names == ["Administrator", "Bo", "Zoe"]


def test_add_person_rejects_duplicates_bad_emails_and_unknown_roles(
    harness: Harness, admin: TestClient
) -> None:
    harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    duplicate = admin.post(
        "/api/v1/admin/people",
        json={"email": "ANA@lab.local", "display_name": "Ana again", "role": "viewer"},
        headers=WEBUI,
    )
    body = assert_problem(duplicate, 409)
    assert body["what_happened"] == "Someone already signs in as ana@lab.local."
    assert body["likely_cause"] == "The address belongs to an existing person, maybe switched off."

    bad_email = admin.post(
        "/api/v1/admin/people",
        json={"email": "not-an-email", "display_name": "X", "role": "viewer"},
        headers=WEBUI,
    )
    assert assert_problem(bad_email, 400)["what_happened"] == (
        "That doesn't look like an email address."
    )

    unknown_role = admin.post(
        "/api/v1/admin/people",
        json={"email": "x@lab.local", "display_name": "X", "role": "boss"},
        headers=WEBUI,
    )
    body = assert_problem(unknown_role, 400)
    assert body["what_happened"] == "There is no role called boss."
    assert "Administrator (administrator)" in body["likely_cause"]
    assert "Line lead (line_lead)" in body["likely_cause"]

    empty_name = admin.post(
        "/api/v1/admin/people",
        json={"email": "y@lab.local", "display_name": "  ", "role": "viewer"},
        headers=WEBUI,
    )
    assert assert_problem(empty_name, 400)["what_happened"] == "The name is empty."


def test_people_routes_need_admin_people(harness: Harness, admin: TestClient) -> None:
    _, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    ana = harness.client_for("ana@lab.local", one_time, "anas-own-long-password")
    body = assert_problem(ana.get("/api/v1/admin/people"), 403)
    assert body["what_happened"] == "Ana may not manage who can sign in and what role they have."
    assert body["likely_cause"] == "The Engineer role does not include admin:people."
    assert body["what_to_do"] == "Ask an administrator to make the change for you."
    assert ana.get("/api/v1/admin/settings").status_code == 200, "anyone signed in may read"
    denied = ana.patch("/api/v1/admin/settings", json={"installation_name": "X"}, headers=WEBUI)
    assert assert_problem(denied, 403)["likely_cause"] == (
        "The Engineer role does not include admin:settings."
    )


def test_change_role_applies_on_the_next_request(harness: Harness, admin: TestClient) -> None:
    person, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    ana = harness.client_for("ana@lab.local", one_time, "anas-own-long-password")
    changed = admin.patch(
        f"/api/v1/admin/people/{person['id']}", json={"role": "line_lead"}, headers=WEBUI
    )
    assert changed.status_code == 200 and changed.json()["role_label"] == "Line lead"
    me = ana.get("/api/v1/me").json()
    assert me["role"] == "line_lead" and "factory:verdict" in me["capabilities"]
    unknown = admin.patch(
        f"/api/v1/admin/people/{person['id']}", json={"role": "boss"}, headers=WEBUI
    )
    assert assert_problem(unknown, 400)["what_happened"] == "There is no role called boss."
    missing = admin.patch("/api/v1/admin/people/no-such-id", json={"role": "viewer"}, headers=WEBUI)
    assert assert_problem(missing, 404)["what_happened"] == "There is no person with that id."


def test_switching_off_revokes_sessions_and_switching_on_restores_them(
    harness: Harness, admin: TestClient
) -> None:
    person, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    ana = harness.client_for("ana@lab.local", one_time, "anas-own-long-password")
    off = admin.patch(
        f"/api/v1/admin/people/{person['id']}", json={"is_active": False}, headers=WEBUI
    )
    assert off.status_code == 200 and off.json()["is_active"] is False
    assert_problem(ana.get("/api/v1/me"), 401, reason="none")
    assert harness.sign_in("ana@lab.local", "anas-own-long-password", client=ana).status_code == 401
    on = admin.patch(
        f"/api/v1/admin/people/{person['id']}", json={"is_active": True}, headers=WEBUI
    )
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert harness.sign_in("ana@lab.local", "anas-own-long-password", client=ana).status_code == 200


def test_last_administrator_rules(harness: Harness, admin: TestClient) -> None:
    admin_id = admin.get("/api/v1/me").json()["id"]
    self_off = admin.patch(
        f"/api/v1/admin/people/{admin_id}", json={"is_active": False}, headers=WEBUI
    )
    body = assert_problem(self_off, 400)
    assert body["what_happened"] == "You can't switch off your own account."
    assert body["likely_cause"] == "You're signed in with it." and body["what_to_do"] == (
        "Ask another administrator."
    )
    demote = admin.patch(
        f"/api/v1/admin/people/{admin_id}", json={"role": "engineer"}, headers=WEBUI
    )
    body = assert_problem(demote, 400)
    assert body["what_happened"] == "You can't change the last administrator's role."
    assert (
        body["likely_cause"] == "Without an administrator nobody could manage people or settings."
    )

    bo, one_time = harness.add_person("bo@lab.local", "Bo", "administrator", client=admin)
    bo_client = harness.client_for("bo@lab.local", one_time, "bos-own-long-password")
    first_off = bo_client.patch(
        f"/api/v1/admin/people/{admin_id}", json={"is_active": False}, headers=WEBUI
    )
    assert first_off.status_code == 200, "two administrators: switching one off is allowed"
    last_off = bo_client.patch(
        f"/api/v1/admin/people/{admin_id}", json={"is_active": True}, headers=WEBUI
    )
    assert last_off.status_code == 200
    admin2 = harness.sign_in_admin(harness.new_client())
    only_off = admin2.patch(
        f"/api/v1/admin/people/{bo['id']}", json={"is_active": False}, headers=WEBUI
    )
    assert only_off.status_code == 200
    now_last = admin2.patch(
        f"/api/v1/admin/people/{admin_id}", json={"role": "viewer"}, headers=WEBUI
    )
    assert assert_problem(now_last, 400)["what_happened"] == (
        "You can't change the last administrator's role."
    )
    body = assert_problem(
        bo_client.patch(
            f"/api/v1/admin/people/{admin_id}", json={"is_active": False}, headers=WEBUI
        ),
        401,
    )
    assert body["reason"] == "none", "Bo was switched off, so Bo's session is gone"


def test_password_reset_revokes_sessions_and_forces_a_change(
    harness: Harness, admin: TestClient
) -> None:
    person, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    ana = harness.client_for("ana@lab.local", one_time, "anas-own-long-password")
    reset = admin.post(f"/api/v1/admin/people/{person['id']}/password-reset", headers=WEBUI)
    assert reset.status_code == 200
    assert set(reset.json()) == {"one_time_password"} and OTP.match(
        reset.json()["one_time_password"]
    )
    assert_problem(ana.get("/api/v1/me"), 401, reason="none")
    assert harness.sign_in("ana@lab.local", "anas-own-long-password", client=ana).status_code == 401
    again = harness.sign_in("ana@lab.local", reset.json()["one_time_password"], client=ana)
    assert again.status_code == 200 and again.json()["must_change_password"] is True
    listed = admin.get("/api/v1/admin/people").json()
    assert next(p for p in listed if p["id"] == person["id"])["must_change_password"] is True
    missing = admin.post("/api/v1/admin/people/no-such-id/password-reset", headers=WEBUI)
    assert_problem(missing, 404)


def test_every_change_writes_an_audit_row_without_a_password(
    harness: Harness, admin: TestClient
) -> None:
    trace = "9f86d081884c7d659a2feaa0c55ad015"
    person, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    admin.patch(
        f"/api/v1/admin/people/{person['id']}",
        json={"role": "viewer", "is_active": False},
        headers={**WEBUI, "X-Slas-Trace-Id": trace},
    )
    admin.post(f"/api/v1/admin/people/{person['id']}/password-reset", headers=WEBUI)
    admin.patch("/api/v1/admin/settings", json={"installation_name": "Lab 3"}, headers=WEBUI)
    rows = harness.audit_rows()
    actions = [row.action for row in rows]
    assert actions == [
        "person.bootstrapped",
        "person.password_changed",
        "bootstrap.consumed",
        "person.added",
        "person.role_changed",
        "person.switched_off",
        "person.password_reset",
        "settings.changed",
    ]
    by_action = {row.action: row for row in rows}
    assert by_action["person.bootstrapped"].via == "installer"
    assert by_action["person.bootstrapped"].actor == "system"
    assert {row.via for row in rows[1:]} == {"webui"}
    assert by_action["person.added"].actor == ADMIN_EMAIL
    assert by_action["person.added"].subject == "ana@lab.local"
    assert json.loads(by_action["person.added"].detail) == {
        "display_name": "Ana",
        "one_time_password_issued": True,
        "role": "engineer",
    }
    assert by_action["person.role_changed"].trace_id == trace
    assert by_action["person.switched_off"].trace_id == trace
    assert json.loads(by_action["person.role_changed"].detail) == {
        "from": "engineer",
        "to": "viewer",
    }
    assert json.loads(by_action["settings.changed"].detail) == {
        "changed": {"installation_name": "Lab 3"}
    }
    for row in rows:
        assert one_time not in row.detail and ADMIN_PASSWORD not in row.detail
        assert "password" not in json.loads(row.detail)
        assert row.trace_id is not None and re.match(r"^[0-9a-f]{32}$", row.trace_id)


# --- settings --------------------------------------------------------------------------------


HOST_ENV = (
    "# SW Local Agent Service — configuration\n"
    "SLAS_PROFILE=quickstart\n"
    "\n"
    "# Where all data lives.\n"
    "SLAS_DATA_ROOT=/AI/Agent\n"
    "SLAS_HTTPS_PORT=443\n"
    "SLAS_TLS_MODE=self-signed\n"
    'SLAS_TLS_NAMES="127.0.0.1 localhost lab3.internal"\n'
    "SLAS_VERSION=1.0.0\n"
    "POSTGRES_PASSWORD=do-not-touch-me\n"
    "UNKNOWN_TO_US=some-other-tool\n"
)


def test_settings_shape_and_install_facts(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        initial_env=HOST_ENV,
        slas_https_port="",
        slas_tls_mode="",
        slas_tls_names="",
        slas_version="",
    )
    admin = harness.sign_in_admin()
    body = admin.get("/api/v1/admin/settings").json()
    assert body["runtime"] == {
        "installation_name": "SW Local Agent Service",
        "chinese_variant": "zh-Hant",
        "session_lifetime_hours": 8,
    }
    assert body["install"] == {
        "profile": "quickstart",
        "data_root": "/AI/Agent",
        "https_port": "443",
        "tls_mode": "self-signed",
        "tls_names": ["127.0.0.1", "localhost", "lab3.internal"],
        "version": "1.0.0",
    }


def test_patch_settings_applies_now_and_mirrors_into_env(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, initial_env=HOST_ENV)
    admin = harness.sign_in_admin()
    at_start = harness.env_file.read_text(encoding="utf-8")
    assert at_start.startswith(HOST_ENV), "start-up mirror touched nothing above the marker"
    assert at_start == HOST_ENV + (
        f"\n{MARKER}\n"
        'SLAS_INSTALLATION_NAME="SW Local Agent Service"\n'
        "SLAS_SOP_CHINESE=zh-Hant\n"
        "SLAS_SESSION_LIFETIME_HOURS=8\n"
    )
    os.chmod(harness.env_file, 0o640)

    response = admin.patch(
        "/api/v1/admin/settings",
        json={"installation_name": "Lab 3", "session_lifetime_hours": 24},
        headers=WEBUI,
    )
    assert response.status_code == 200
    assert response.json() == {
        "runtime": {
            "installation_name": "Lab 3",
            "chinese_variant": "zh-Hant",
            "session_lifetime_hours": 24,
        },
        "notice": None,
    }
    public = admin.get("/api/v1/public/installation").json()
    assert public["installation_name"] == "Lab 3" and public["session_lifetime_hours"] == 24

    after = harness.env_file.read_text(encoding="utf-8")
    assert after.startswith(HOST_ENV), "every other line is byte for byte the same"
    assert after == HOST_ENV + (
        f'\n{MARKER}\nSLAS_INSTALLATION_NAME="Lab 3"\nSLAS_SOP_CHINESE=zh-Hant\n'
        "SLAS_SESSION_LIFETIME_HOURS=24\n"
    )
    assert (harness.env_file.stat().st_mode & 0o777) == 0o640, "mode preserved"

    variant = admin.patch(
        "/api/v1/admin/settings", json={"chinese_variant": "zh-Hans"}, headers=WEBUI
    )
    assert variant.json()["runtime"]["chinese_variant"] == "zh-Hans"
    assert "SLAS_SOP_CHINESE=zh-Hans\n" in harness.env_file.read_text(encoding="utf-8")

    fresh = harness.sign_in(ADMIN_EMAIL, ADMIN_PASSWORD, client=harness.new_client())
    assert "max-age=86400" in fresh.headers["set-cookie"].lower(), "new sign-ins get 24 hours"


def test_patch_settings_rejects_values_out_of_range(harness: Harness, admin: TestClient) -> None:
    long_name = admin.patch(
        "/api/v1/admin/settings", json={"installation_name": "x" * 90}, headers=WEBUI
    )
    body = assert_problem(long_name, 400)
    assert body["what_happened"] == "The name is too long: it has 90 characters, the limit is 60."
    assert body["what_to_do"] == "Shorten it."
    empty = admin.patch("/api/v1/admin/settings", json={"installation_name": "  "}, headers=WEBUI)
    assert assert_problem(empty, 400)["what_happened"] == "The name is empty."
    hours = admin.patch(
        "/api/v1/admin/settings", json={"session_lifetime_hours": 500}, headers=WEBUI
    )
    assert assert_problem(hours, 400)["what_happened"] == (
        "A sign-in can't last 500 hours: the range is 1 to 168."
    )
    zero = admin.patch(
        "/api/v1/admin/settings", json={"session_lifetime_hours": "0"}, headers=WEBUI
    )
    assert_problem(zero, 400)
    variant = admin.patch("/api/v1/admin/settings", json={"chinese_variant": "zh"}, headers=WEBUI)
    assert assert_problem(variant, 400)["likely_cause"] == (
        "The choices are zh-Hant (Traditional) and zh-Hans (Simplified)."
    )
    unknown = admin.patch("/api/v1/admin/settings", json={"theme": "dark"}, headers=WEBUI)
    assert_problem(unknown, 400)
    unchanged = admin.get("/api/v1/admin/settings").json()["runtime"]
    assert unchanged["installation_name"] == "SW Local Agent Service"
    nothing = admin.patch("/api/v1/admin/settings", json={}, headers=WEBUI)
    assert nothing.status_code == 200 and nothing.json()["notice"] is None
    assert [r.action for r in harness.audit_rows()].count("settings.changed") == 0


def test_mirror_failure_answers_200_with_the_notice(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, initial_env=HOST_ENV)
    admin = harness.sign_in_admin()
    harness.env_file.unlink()
    harness.env_file.mkdir()  # a directory where the file should be: every write fails
    response = admin.patch(
        "/api/v1/admin/settings", json={"installation_name": "Lab 3"}, headers=WEBUI
    )
    assert response.status_code == 200
    assert response.json()["runtime"]["installation_name"] == "Lab 3"
    assert response.json()["notice"] == (
        "Saved, but the copy in `.env` couldn't be written. "
        "The data root isn't writable by the api service. "
        f"The settings apply now; fix permissions on {harness.env_file} so they survive a "
        "reinstall."
    )
    assert admin.get("/api/v1/admin/settings").json()["runtime"]["installation_name"] == "Lab 3"
    assert harness.records("settings.mirror_failed")


def test_env_seeds_only_an_empty_table_and_the_database_wins(tmp_path: Path) -> None:
    seeded_env = HOST_ENV + (
        f'{MARKER}\nSLAS_INSTALLATION_NAME="Lab 3"\nSLAS_SOP_CHINESE=zh-Hans\n'
        "SLAS_SESSION_LIFETIME_HOURS=24\n"
    )
    harness = make_harness(tmp_path, initial_env=seeded_env)
    assert harness.records("settings.seeded")
    public = harness.client.get("/api/v1/public/installation").json()
    assert public["installation_name"] == "Lab 3" and public["session_lifetime_hours"] == 24
    assert harness.env_file.read_text(encoding="utf-8") == seeded_env, "nothing to rewrite"

    # drift: someone edits .env by hand; the database still wins and the next start converges
    harness.env_file.write_text(
        seeded_env.replace('SLAS_INSTALLATION_NAME="Lab 3"', "SLAS_INSTALLATION_NAME=Elsewhere"),
        encoding="utf-8",
    )
    assert harness.client.get("/api/v1/public/installation").json()["installation_name"] == "Lab 3"
    restarted = make_harness(tmp_path, bootstrap_admin=False)
    assert not restarted.records("settings.seeded")
    assert (
        restarted.client.get("/api/v1/public/installation").json()["installation_name"] == "Lab 3"
    )
    assert 'SLAS_INSTALLATION_NAME="Lab 3"' in harness.env_file.read_text(encoding="utf-8")


def test_invalid_env_values_fall_back_to_defaults_when_seeding(tmp_path: Path) -> None:
    bad_env = f"{MARKER}\nSLAS_INSTALLATION_NAME={'x' * 70}\nSLAS_SESSION_LIFETIME_HOURS=999\n"
    harness = make_harness(tmp_path, initial_env=bad_env)
    public = harness.client.get("/api/v1/public/installation").json()
    assert public["installation_name"] == "SW Local Agent Service"
    assert public["session_lifetime_hours"] == 8
