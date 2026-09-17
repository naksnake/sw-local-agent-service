"""Sign-in, sessions, the must-change gate, error bodies, trace ids and the ops routes."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from slas_api import routes
from slas_api.app import route_table
from slas_api.service import bootstrap_status
from slas_observability.tracing import format_traceparent
from tests.unit.api_harness import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    INITIAL_PASSWORD,
    REPO_ROOT,
    WEBUI,
    Harness,
    make_harness,
)

PROBLEM_KEYS = {"what_happened", "likely_cause", "what_to_do", "trace_id"}
HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)


def assert_problem(
    response: httpx.Response, status: int, *, reason: str | None = None
) -> dict[str, str]:
    assert response.status_code == status, response.text
    body: dict[str, str] = response.json()
    expected = PROBLEM_KEYS | ({"reason"} if status == 401 else set())
    assert set(body) == expected, body
    assert HEX32.match(body["trace_id"])
    assert body["trace_id"] == response.headers["X-Slas-Trace-Id"]
    if reason is not None:
        assert body["reason"] == reason
    return body


# --- public and sign-in ---------------------------------------------------------------------


def test_public_installation_needs_no_sign_in(harness: Harness) -> None:
    response = harness.client.get("/api/v1/public/installation")
    assert response.status_code == 200
    assert response.json() == {
        "installation_name": "SW Local Agent Service",
        "auth_modes": ["builtin"],
        "version": "1.2.3",
        "session_lifetime_hours": 8,
    }


def test_sign_in_returns_person_and_a_hardened_cookie(harness: Harness) -> None:
    response = harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD)
    assert response.status_code == 200, response.text
    person = response.json()
    assert person["email"] == ADMIN_EMAIL
    assert person["display_name"] == "Administrator"
    assert person["role"] == "administrator" and person["role_label"] == "Administrator"
    assert "admin:people" in person["capabilities"] and "admin:settings" in person["capabilities"]
    assert person["must_change_password"] is True and person["is_active"] is True
    assert person["last_sign_in_at"] == "2026-09-17T08:00:00Z"
    assert set(person) == {
        "id",
        "email",
        "display_name",
        "role",
        "role_label",
        "capabilities",
        "must_change_password",
        "is_active",
        "last_sign_in_at",
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-slas_session=")
    lowered = cookie.lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/", "max-age=28800"):
        assert flag in lowered, cookie
    assert "domain=" not in lowered


def test_email_is_matched_case_insensitively(harness: Harness) -> None:
    assert harness.sign_in("Admin@SLAS.local", INITIAL_PASSWORD).status_code == 200


def test_wrong_password_unknown_email_and_switched_off_all_look_the_same(
    harness: Harness,
) -> None:
    wrong = assert_problem(harness.sign_in(ADMIN_EMAIL, "not-the-password"), 401, reason="none")
    unknown = assert_problem(harness.sign_in("nobody@slas.local", INITIAL_PASSWORD), 401)
    assert wrong["what_happened"] == "That email and password don't match."
    assert wrong["likely_cause"] == "A typo, or the password was changed."
    assert wrong["what_to_do"] == "Try again, or ask an administrator to reset your password."
    assert {k: v for k, v in unknown.items() if k != "trace_id"} == {
        k: v for k, v in wrong.items() if k != "trace_id"
    }

    admin = harness.sign_in_admin(harness.new_client())
    _, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    ana = harness.client_for("ana@lab.local", one_time, "anas-own-long-password")
    ana_id = ana.get("/api/v1/me").json()["id"]
    assert (
        admin.patch(
            f"/api/v1/admin/people/{ana_id}", json={"is_active": False}, headers=WEBUI
        ).status_code
        == 200
    )
    off = assert_problem(
        harness.sign_in("ana@lab.local", "anas-own-long-password", client=harness.new_client()),
        401,
        reason="none",
    )
    assert off["what_happened"] == wrong["what_happened"]


def test_ten_failures_in_fifteen_minutes_block_the_email(harness: Harness) -> None:
    for _ in range(10):
        assert harness.sign_in(ADMIN_EMAIL, "wrong-wrong-wrong").status_code == 401
    blocked = assert_problem(harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD), 429)
    assert blocked["what_happened"] == "Too many sign-in attempts in the last 15 minutes."
    assert blocked["likely_cause"] == "Several wrong passwords were tried for this account."
    assert blocked["what_to_do"] == "Wait and try again."
    harness.clock.advance(minutes=15, seconds=1)
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD).status_code == 200


def test_ten_failures_from_one_address_block_the_address(harness: Harness) -> None:
    for n in range(10):
        response = harness.sign_in(f"guess{n}@lab.local", "wrong-wrong-wrong", address="10.0.0.9")
        assert response.status_code == 401
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, address="10.0.0.9").status_code == 429
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, address="10.0.0.10").status_code == 200


def test_a_successful_sign_in_clears_the_email_counter(harness: Harness) -> None:
    for _ in range(9):
        harness.sign_in(ADMIN_EMAIL, "wrong-wrong-wrong", address="10.0.0.1")
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, address="10.0.0.1").status_code == 200
    for _ in range(9):
        harness.sign_in(ADMIN_EMAIL, "wrong-wrong-wrong", address="10.0.0.2")
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, address="10.0.0.2").status_code == 200
    # the address counter is not cleared by a success: 9 + 9 from one address would block it
    for _ in range(9):
        harness.sign_in(ADMIN_EMAIL, "wrong-wrong-wrong", address="10.0.0.1")
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, address="10.0.0.1").status_code == 429


def test_sign_in_fails_closed_when_the_rate_limiter_is_down(harness: Harness) -> None:
    harness.throttle.available = False
    body = assert_problem(harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD), 503)
    assert body["what_happened"] == "Sign-in is paused for a moment."
    assert body["likely_cause"] == "The service that counts sign-in attempts didn't answer."
    assert body["what_to_do"] == (
        "Try again in a minute; if it repeats, run `slas logs redis` on the host."
    )
    assert harness.records("signin.throttle_unavailable")


def test_sign_in_body_must_be_complete(harness: Harness) -> None:
    response = harness.client.post("/api/v1/session", json={"email": ADMIN_EMAIL}, headers=WEBUI)
    body = assert_problem(response, 400)
    assert body["what_happened"].startswith("The request couldn't be read: password")


# --- sessions --------------------------------------------------------------------------------


def test_me_without_a_cookie_is_401_reason_none(harness: Harness) -> None:
    body = assert_problem(harness.client.get("/api/v1/me"), 401, reason="none")
    assert body["what_happened"] == "You're not signed in."


def test_session_slides_and_then_expires_with_reason_expired(harness: Harness) -> None:
    harness.sign_in_admin()
    harness.clock.advance(hours=7)
    assert harness.client.get("/api/v1/me").status_code == 200
    harness.clock.advance(hours=7)
    assert harness.client.get("/api/v1/me").status_code == 200, "last_seen slid the expiry"
    harness.clock.advance(hours=8, seconds=1)
    body = assert_problem(harness.client.get("/api/v1/me"), 401, reason="expired")
    assert body["what_happened"] == "Your sign-in ended after 8 hours."
    # the row is gone: the same cookie now reads as "none"
    assert_problem(harness.client.get("/api/v1/me"), 401, reason="none")


def test_sign_out_clears_the_cookie_and_ends_the_session(harness: Harness) -> None:
    harness.sign_in_admin()
    response = harness.client.delete("/api/v1/session", headers=WEBUI)
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("__host-slas_session=") and "max-age=0" in cookie
    assert_problem(harness.client.get("/api/v1/me"), 401, reason="none")
    assert harness.client.delete("/api/v1/session", headers=WEBUI).status_code == 204


def test_must_change_password_gate(harness: Harness) -> None:
    harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD)
    gated = assert_problem(harness.client.get("/api/v1/admin/people"), 403)
    assert gated["what_happened"] == "Choose a new password first."
    assert harness.client.get("/api/v1/models").status_code == 403
    assert harness.client.get("/api/v1/me").status_code == 200

    short = harness.client.post(
        "/api/v1/me/password",
        json={"current_password": INITIAL_PASSWORD, "new_password": "eight888"},
        headers=WEBUI,
    )
    body = assert_problem(short, 400)
    assert body["what_happened"] == (
        "The password is too short: it has 8 characters and needs at least 12."
    )
    assert body["what_to_do"] == "Add a few more words."

    same = harness.client.post(
        "/api/v1/me/password",
        json={"current_password": INITIAL_PASSWORD, "new_password": ADMIN_EMAIL.upper()},
        headers=WEBUI,
    )
    assert assert_problem(same, 400)["what_happened"] == "The password can't be your email address."

    wrong = harness.client.post(
        "/api/v1/me/password",
        json={"current_password": "not-it-at-all", "new_password": ADMIN_PASSWORD},
        headers=WEBUI,
    )
    assert assert_problem(wrong, 401, reason="none")["what_happened"] == (
        "The current password isn't right."
    )

    other = harness.new_client()
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, client=other).status_code == 200
    changed = harness.client.post(
        "/api/v1/me/password",
        json={"current_password": INITIAL_PASSWORD, "new_password": ADMIN_PASSWORD},
        headers=WEBUI,
    )
    assert changed.status_code == 200 and changed.json()["must_change_password"] is False
    assert harness.client.get("/api/v1/admin/people").status_code == 200, "this session stays"
    assert_problem(other.get("/api/v1/me"), 401, reason="none")
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, client=other).status_code == 401
    assert harness.sign_in(ADMIN_EMAIL, ADMIN_PASSWORD, client=other).status_code == 200


def test_bootstrap_status_moves_from_pending_to_done(harness: Harness) -> None:
    assert bootstrap_status(harness.services) == "pending"
    harness.sign_in_admin()
    assert bootstrap_status(harness.services) == "done"
    actions = [row.action for row in harness.audit_rows()]
    assert "person.bootstrapped" in actions and "bootstrap.consumed" in actions
    consumed = next(row for row in harness.audit_rows() if row.action == "bootstrap.consumed")
    assert consumed.via == "webui" and consumed.actor == ADMIN_EMAIL


# --- cross-cutting ---------------------------------------------------------------------------


def test_state_changing_requests_need_x_requested_with(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/session", json={"email": ADMIN_EMAIL, "password": INITIAL_PASSWORD}
    )
    body = assert_problem(response, 403)
    assert body["what_happened"] == "The request didn't come from the SW Local Agent Service page."
    harness.sign_in_admin()
    assert harness.client.patch("/api/v1/admin/settings", json={}).status_code == 403
    assert harness.client.delete("/api/v1/session").status_code == 403
    assert harness.client.get("/api/v1/me").status_code == 200, "GET needs no header"


def test_trace_id_is_accepted_or_minted_and_always_echoed(harness: Harness) -> None:
    given = "0af7651916cd43dd8448eb211c80319c"
    response = harness.client.get("/api/v1/public/installation", headers={"X-Slas-Trace-Id": given})
    assert response.headers["X-Slas-Trace-Id"] == given
    parent = format_traceparent("4bf92f3577b34da6a3ce929d0e0e4736")
    response = harness.client.get("/health", headers={"traceparent": parent})
    assert response.headers["X-Slas-Trace-Id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    response = harness.client.get("/health")
    assert HEX32.match(response.headers["X-Slas-Trace-Id"])
    failed = harness.sign_in(ADMIN_EMAIL, "nope-nope-nope-nope")
    assert failed.json()["trace_id"] == failed.headers["X-Slas-Trace-Id"]


def test_unknown_routes_and_methods_answer_in_three_parts(harness: Harness) -> None:
    assert assert_problem(harness.client.get("/api/v1/nowhere"), 404)["what_happened"] == (
        "There is nothing at this address."
    )
    assert_problem(harness.client.get("/docs"), 404)
    assert_problem(harness.client.get("/openapi.json"), 404)
    assert_problem(harness.client.get("/redoc"), 404)
    assert_problem(harness.client.patch("/api/v1/me", headers=WEBUI), 405)


def test_an_unexpected_exception_becomes_a_generic_500(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_settings: object) -> dict[str, object]:
        raise RuntimeError("secret detail that must not leak")

    monkeypatch.setattr(routes, "models_view", explode)
    harness.sign_in_admin()
    response = harness.client.get("/api/v1/models")
    body = assert_problem(response, 500)
    assert body["what_happened"] == "The api hit a problem it did not expect."
    assert "slas logs api" in body["what_to_do"]
    assert "secret detail" not in response.text and "Traceback" not in response.text
    logged = harness.records("request.unexpected_error")
    assert logged and logged[0]["trace_id"] == body["trace_id"]
    assert logged[0]["error_type"] == "RuntimeError" and "secret detail" not in str(logged)


def test_route_table_matches_the_contract() -> None:
    contract = (REPO_ROOT / "docs" / "api-contract.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(GET|POST|PATCH|DELETE) (/[^\s`]+)`", contract))
    assert documented, "the contract names its routes in backticks"
    assert set(route_table()) == documented
    assert route_table() == sorted(route_table())
    assert ("GET", "/health") in route_table() and ("GET", "/metrics") in route_table()


def test_health_is_200_with_both_checks(harness: Harness) -> None:
    response = harness.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "service": "api",
        "ok": True,
        "checks": {"postgres": "ok", "redis": "ok"},
    }


def test_health_names_the_failing_check(harness: Harness, tmp_path: Path) -> None:
    harness.throttle.available = False
    body = assert_problem(harness.client.get("/health"), 503)
    assert body["what_happened"] == "The api is not healthy: redis did not answer."
    assert "slas logs redis" in body["what_to_do"]
    harness.throttle.available = True

    broken = make_harness(
        tmp_path / "broken",
        bootstrap_admin=False,
        migrate_first=False,
        slas_database_url=f"sqlite+pysqlite:///{tmp_path}/does/not/exist/api.db",
    )
    assert broken.records("settings.start_failed"), "start-up work never stops the api"
    body = assert_problem(broken.client.get("/health"), 503)
    assert body["what_happened"] == "The api is not healthy: postgres did not answer."


def test_metrics_is_prometheus_text(harness: Harness) -> None:
    response = harness.client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE slas_agent_turns_total counter" in response.text


def test_home_lists_are_empty_in_round_one(harness: Harness) -> None:
    harness.sign_in_admin()
    for path in ("/api/v1/coding/tasks", "/api/v1/validation/runs", "/api/v1/factory/jobs"):
        response = harness.client.get(path)
        assert response.status_code == 200 and response.json() == [], path
    assert_problem(harness.new_client().get("/api/v1/coding/tasks"), 401, reason="none")


# --- models ----------------------------------------------------------------------------------


QUICKSTART = REPO_ROOT / "config" / "models.quickstart.yaml"


def test_models_route_reads_the_registry_on_every_request(harness: Harness) -> None:
    harness.sign_in_admin()
    missing = harness.client.get("/api/v1/models")
    assert missing.status_code == 200
    body = missing.json()
    assert body["models"] == [] and body["roles"] == {} and body["voters"] == []
    assert body["problem"]["what_happened"] == "The model registry Models/models.yaml is missing."
    assert set(body["problem"]) == PROBLEM_KEYS
    assert body["sentence"] == body["problem"]["what_happened"]

    models_dir = harness.settings.models_dir
    models_dir.mkdir(parents=True)
    (models_dir / "models.yaml").write_text(QUICKSTART.read_text(encoding="utf-8"), "utf-8")
    (models_dir / "bge-m3").mkdir()
    (models_dir / "bge-m3" / "SHA256SUMS").write_text("abc  model.safetensors\n", "utf-8")
    listed = harness.client.get("/api/v1/models").json()
    assert listed["problem"] is None
    assert listed["roles"]["coder"] == "qwen3.8-27b-fp8"
    assert listed["voters"] == ["deepseek-v4-flash", "qwen3.8-27b-fp8"]
    by_id = {model["id"]: model for model in listed["models"]}
    assert by_id["bge-m3"]["present"] is True and by_id["deepseek-v4-flash"]["present"] is False
    assert set(by_id["bge-m3"]) == {
        "id",
        "display_name",
        "family",
        "path",
        "quant",
        "vram_gib",
        "context",
        "roles",
        "present",
    }
    assert listed["sentence"].startswith("4 models;")

    (models_dir / "models.yaml").write_text("version: 1\nmodels: [\n", "utf-8")
    broken = harness.client.get("/api/v1/models").json()
    assert broken["models"] == []
    assert (
        broken["problem"]["what_happened"]
        == "The model registry Models/models.yaml is not valid YAML."
    )

    (models_dir / "models.yaml").write_text("version: 1\nmodels: []\n", "utf-8")
    invalid = harness.client.get("/api/v1/models").json()
    assert invalid["problem"]["what_happened"] == (
        "The model registry Models/models.yaml could not be used."
    )


def test_no_response_carries_a_password_or_a_hash(harness: Harness) -> None:
    admin = harness.sign_in_admin()
    _, one_time = harness.add_person("ana@lab.local", "Ana", "engineer", client=admin)
    texts = [
        admin.get("/api/v1/me").text,
        admin.get("/api/v1/admin/people").text,
        admin.get("/api/v1/admin/settings").text,
        admin.get("/api/v1/public/installation").text,
        harness.sign_in(ADMIN_EMAIL, ADMIN_PASSWORD, client=harness.new_client()).text,
    ]
    for text in texts:
        assert "$argon2" not in text and "password_hash" not in text
        assert ADMIN_PASSWORD not in text and INITIAL_PASSWORD not in text
        assert one_time not in text, "a one-time password appears once, in the Add result only"
    for row in harness.audit_rows():
        assert one_time not in row.detail and ADMIN_PASSWORD not in row.detail
        assert INITIAL_PASSWORD not in row.detail and "$argon2" not in row.detail
    for record in harness.records():
        blob = str(record)
        assert one_time not in blob and ADMIN_PASSWORD not in blob and INITIAL_PASSWORD not in blob


def test_cookie_secure_flag_is_a_setting_that_defaults_on(tmp_path: Path) -> None:
    relaxed = make_harness(tmp_path, slas_cookie_secure=False)
    client = TestClient(relaxed.app, base_url="http://testserver")
    response = relaxed.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD, client=client)
    assert response.status_code == 200
    assert "secure" not in response.headers["set-cookie"].lower()
    assert client.get("/api/v1/me").status_code == 200, "plain http works only when relaxed"


def test_a_development_server_without_secure_uses_a_prefixless_cookie(tmp_path: Path) -> None:
    """A `__Host-` cookie is dropped by browsers unless it is Secure, so plain-http development
    gets `slas_session`; the production name keeps the prefix (ADR-0007)."""
    dev = make_harness(tmp_path / "dev", slas_cookie_secure=False)
    response = dev.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD)
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("slas_session=") and "secure" not in cookie.lower()
    assert dev.client.get("/api/v1/me").status_code == 200, "the browser sends it back"
    assert routes.session_cookie_name(dev.services) == "slas_session"
    assert routes.SESSION_COOKIE == "__Host-slas_session"
