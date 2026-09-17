"""Password hashing, the policy, tokens, throttling, settings, roles loading and problems."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError

from slas_api.authz import RolesLoader, principal_for, require
from slas_api.errors import ApiError, ThreePartProblem
from slas_api.models import Person
from slas_api.security import (
    Passwords,
    check_password_policy,
    new_one_time_password,
    new_session_token,
    token_sha256,
)
from slas_api.settings import Settings
from slas_api.throttle import (
    FAILURE_LIMIT,
    MemoryThrottle,
    RedisThrottle,
    ThrottleUnavailableError,
)
from slas_authz import Capability, default_roles
from slas_observability.events import EventLog, ListSink
from slas_observability.tracing import trace
from slas_schemas.errors import ThreePartMessage
from tests.unit.api_harness import ROLES_FILE, FakeClock

# --- passwords -------------------------------------------------------------------------------


def test_the_default_argon2_profile_is_production() -> None:
    assert Settings(slas_secrets_dir=Path("/nonexistent")).slas_argon2_profile == "production"
    hasher = Passwords()
    assert hasher.profile == "production"
    assert hasher.parameters == {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}
    digest = hasher.hash("a-long-enough-password")
    assert digest.startswith("$argon2id$") and "m=65536,t=3,p=4" in digest
    assert hasher.verify(digest, "a-long-enough-password")


def test_the_test_profile_is_explicit_and_cheaper() -> None:
    hasher = Passwords("test")
    assert hasher.parameters["memory_cost"] < 64 * 1024
    digest = hasher.hash("a-long-enough-password")
    assert hasher.verify(digest, "a-long-enough-password")
    assert not hasher.verify(digest, "a-different-password")
    assert not hasher.verify(None, "anything"), "no person: verified against a decoy, still False"
    assert not hasher.verify("not-a-hash", "anything")
    assert hasher.needs_rehash("not-a-hash")
    assert not hasher.needs_rehash(digest)
    assert Passwords().needs_rehash(digest), "a test-profile hash is rehashed in production"


def test_password_policy_sentences() -> None:
    assert check_password_policy("twelve chars", "ana@lab.local") is None
    short = check_password_policy("eight888", "ana@lab.local")
    assert short is not None
    assert short.what_happened == (
        "The password is too short: it has 8 characters and needs at least 12."
    )
    assert short.what_to_do == "Add a few more words."
    one = check_password_policy("x", "ana@lab.local")
    assert one is not None and "it has 1 character and" in one.what_happened
    same = check_password_policy("Ana@Lab.Local", "ana@lab.local")
    assert same is not None
    assert same.what_happened == "The password can't be your email address."
    assert same.what_to_do == "Choose something only you know."


def test_tokens_and_one_time_passwords() -> None:
    token = new_session_token()
    assert len(token) >= 43, "256 bits, urlsafe"
    assert token != new_session_token()
    assert re.match(r"^[0-9a-f]{64}$", token_sha256(token))
    assert token_sha256(token) == token_sha256(token) != token_sha256(token + "x")
    for _ in range(50):
        one_time = new_one_time_password()
        assert len(one_time) >= 16
        assert re.match(r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$", one_time)
        assert not set("01lIoO") & set(one_time)


# --- throttling ------------------------------------------------------------------------------


def test_memory_throttle_counts_per_email_and_per_address_within_the_window() -> None:
    clock = FakeClock()
    limiter = MemoryThrottle(clock)
    for _ in range(FAILURE_LIMIT - 1):
        limiter.record_failure("Ana@Lab.local", "10.0.0.1")
    assert not limiter.is_blocked("ana@lab.local", "10.0.0.1")
    limiter.record_failure("ana@lab.local", "10.0.0.2")
    assert limiter.is_blocked("ana@lab.local", "10.0.0.3"), "the email is blocked everywhere"
    assert not limiter.is_blocked("bo@lab.local", "10.0.0.1"), "9 from that address: not yet"
    limiter.record_failure("bo@lab.local", "10.0.0.1")
    assert limiter.is_blocked("zoe@lab.local", "10.0.0.1"), "the address is now blocked"
    clock.advance(minutes=15, seconds=1)
    assert not limiter.is_blocked("ana@lab.local", "10.0.0.1")
    limiter.record_failure("ana@lab.local", "10.0.0.1")
    limiter.clear("ana@lab.local")
    assert not limiter.is_blocked("ana@lab.local", "10.0.0.9")
    assert limiter.ping()
    limiter.available = False
    assert not limiter.ping()
    with pytest.raises(ThrottleUnavailableError):
        limiter.is_blocked("ana@lab.local", "10.0.0.1")
    with pytest.raises(ThrottleUnavailableError):
        limiter.record_failure("ana@lab.local", "10.0.0.1")
    with pytest.raises(ThrottleUnavailableError):
        limiter.clear("ana@lab.local")


class FakePipeline:
    def __init__(self, store: dict[str, int], ttls: dict[str, int]) -> None:
        self.store, self.ttls = store, ttls

    def incr(self, key: str) -> None:
        self.store[key] = self.store.get(key, 0) + 1

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        if not (nx and key in self.ttls):
            self.ttls[key] = seconds

    def execute(self) -> list[object]:
        return []


class FakeRedis:
    """Enough of redis-py for the throttle, plus a switch that makes every call fail."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("Connection refused")

    def mget(self, keys: list[str]) -> list[bytes | None]:
        self._check()
        return [str(self.store[k]).encode() if k in self.store else None for k in keys]

    def pipeline(self) -> FakePipeline:
        self._check()
        return FakePipeline(self.store, self.ttls)

    def delete(self, key: str) -> int:
        self._check()
        return 1 if self.store.pop(key, None) is not None else 0

    def ping(self) -> bool:
        self._check()
        return True


def test_redis_throttle_uses_counters_with_a_window_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes: list[FakeRedis] = []

    def factory(**kwargs: Any) -> FakeRedis:
        fake = FakeRedis(**kwargs)
        fakes.append(fake)
        return fake

    monkeypatch.setattr(redis, "Redis", factory)  # the same module object throttle.py imported
    limiter = RedisThrottle("redis", 6379, "s3cret")
    fake = fakes[0]
    assert fake.kwargs["password"] == "s3cret" and fake.kwargs["host"] == "redis"
    for _ in range(FAILURE_LIMIT):
        limiter.record_failure("ana@lab.local", "10.0.0.1")
    assert set(fake.ttls.values()) == {15 * 60}
    assert all("ana@lab.local" not in key for key in fake.store), "emails are hashed in keys"
    assert limiter.is_blocked("ana@lab.local", "10.0.0.7")
    assert limiter.is_blocked("bo@lab.local", "10.0.0.1")
    assert not limiter.is_blocked("bo@lab.local", "10.0.0.2")
    limiter.clear("ana@lab.local")
    assert not limiter.is_blocked("ana@lab.local", "10.0.0.7")
    assert limiter.ping()
    fake.down = True
    assert not limiter.ping()
    with pytest.raises(ThrottleUnavailableError):
        limiter.is_blocked("ana@lab.local", "10.0.0.1")
    with pytest.raises(ThrottleUnavailableError):
        limiter.record_failure("ana@lab.local", "10.0.0.1")
    with pytest.raises(ThrottleUnavailableError):
        limiter.clear("ana@lab.local")


# --- settings --------------------------------------------------------------------------------


def test_database_url_is_assembled_from_the_secret_file(tmp_path: Path) -> None:
    (tmp_path / "postgres_password").write_text("pg-secret-value\n", encoding="utf-8")
    settings = Settings(slas_secrets_dir=tmp_path, slas_database_url="")
    url = settings.database_url()
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "postgres" and url.port == 5432 and url.database == "slas"
    assert url.username == "slas" and url.password == "pg-secret-value"
    assert "pg-secret-value" not in str(url), "SQLAlchemy hides the password when rendered"
    assert "pg-secret-value" not in repr(settings)
    assert settings.redis_password() is None, "no redis secret file: None, never a crash"
    assert settings.read_secret("missing") is None
    assert settings.auth_modes() == ["builtin"]
    assert Settings(slas_auth_modes="builtin, oidc").auth_modes() == ["builtin", "oidc"]
    override = Settings(slas_database_url="sqlite+pysqlite:///x.db")
    assert override.database_url().get_backend_name() == "sqlite"


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLAS_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SLAS_POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("SLAS_ARGON2_PROFILE", "test")
    monkeypatch.setenv("SLAS_COOKIE_SECURE", "false")
    settings = Settings()
    assert settings.slas_data_root == tmp_path
    assert settings.env_file == tmp_path / ".env"
    assert settings.models_file == tmp_path / "Models" / "models.yaml"
    assert settings.slas_postgres_host == "db.internal"
    assert settings.slas_argon2_profile == "test"
    assert settings.slas_cookie_secure is False


# --- roles -----------------------------------------------------------------------------------


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_roles_loader_reloads_on_mtime_and_keeps_the_last_good_set(tmp_path: Path) -> None:
    path = tmp_path / "rbac-roles.yaml"
    path.write_text(ROLES_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    sink = ListSink()
    loader = RolesLoader(path, EventLog("api", sink))
    first = loader.current()
    assert first.ids() == ("administrator", "engineer", "line_lead", "viewer")
    assert loader.current() is first, "no mtime change, no re-read"

    path.write_text("version: 1\nroles: {}\n", encoding="utf-8")
    _bump_mtime(path)
    assert loader.current() is first, "an invalid edit keeps the last good set"
    problem = next(r for r in sink.records() if r["event"] == "roles.invalid")
    assert problem["what_happened"].endswith("defines no roles.")
    assert problem["likely_cause"] and problem["what_to_do"]

    path.write_text("version: 1\ndefault_role: [\n", encoding="utf-8")
    _bump_mtime(path)
    assert loader.current() is first
    assert sum(1 for r in sink.records() if r["event"] == "roles.invalid") == 2

    path.write_text(
        "version: 1\ndefault_role: only\nroles:\n  only:\n    label: Only\n"
        "    description: The one role.\n    capabilities: [admin:people]\n",
        encoding="utf-8",
    )
    _bump_mtime(path)
    reloaded = loader.current()
    assert reloaded.ids() == ("only",)

    path.unlink()
    assert loader.current() is reloaded, "a missing file keeps the last good set"
    assert any(r["event"] == "roles.file_missing" for r in sink.records())


def test_roles_loader_falls_back_to_the_shipped_defaults_before_any_file_loaded(
    tmp_path: Path,
) -> None:
    sink = ListSink()
    loader = RolesLoader(tmp_path / "absent.yaml", EventLog("api", sink))
    assert loader.current().ids() == default_roles().ids()
    assert any(r["event"] == "roles.defaults_in_force" for r in sink.records())


def test_principal_for_a_role_that_left_the_file_holds_nothing() -> None:
    roles = default_roles()
    person = Person(
        id="p1",
        email="ana@lab.local",
        display_name="Ana",
        role="line_lead",
        password_hash="x",
        must_change_password=False,
        is_active=True,
        created_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    principal = principal_for(person, roles)
    assert principal.role_label == "Line lead" and principal.can(Capability.FACTORY_VERDICT)
    person.role = "retired_role"
    orphan = principal_for(person, roles)
    assert orphan.capabilities == frozenset() and orphan.role_label == "Retired role"
    with pytest.raises(ApiError) as denied:
        require(orphan, Capability.ADMIN_PEOPLE)
    assert denied.value.status == 403
    assert denied.value.message.likely_cause == (
        "The Retired role role does not include admin:people."
    )
    require(principal, Capability.SCREEN)


# --- problems --------------------------------------------------------------------------------


def test_three_part_problem_body_and_reason_rules() -> None:
    message = ThreePartMessage("What.", "Why.", "Do.")
    with trace("0af7651916cd43dd8448eb211c80319c"):
        problem = ThreePartProblem.from_message(message)
        assert problem.body() == {
            "what_happened": "What.",
            "likely_cause": "Why.",
            "what_to_do": "Do.",
            "trace_id": "0af7651916cd43dd8448eb211c80319c",
        }
        assert (
            ThreePartProblem.from_message(message, reason="expired").body()["reason"] == "expired"
        )
    minted = ThreePartProblem.from_message(message)
    assert re.match(r"^[0-9a-f]{32}$", minted.trace_id)
    assert ApiError(403, message, reason="none").reason is None, "reason only travels on 401"
    assert ApiError(401, message, reason="expired").reason == "expired"
    built = ApiError.build(409, "A.", "B.", "C.")
    assert built.status == 409 and built.message == ThreePartMessage("A.", "B.", "C.")
    assert str(built) == "A."


def test_problems_never_leak_a_trace_id_into_the_callers_context() -> None:
    from slas_observability.tracing import current_trace_id

    assert current_trace_id() is None
    ThreePartProblem.from_message(ThreePartMessage("What.", "Why.", "Do."))
    assert current_trace_id() is None
