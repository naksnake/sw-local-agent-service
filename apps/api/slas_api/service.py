"""The service layer both the routes and `slas-api` share (ADR-0007).

People, sessions, the audit trail and the bootstrap administrator live here; the routes add
HTTP (cookies, the request's address, the `webui` via) and the CLI adds argv and stdin (the
`SYSTEM` principal, the `cli` via). Every sentence a person may read comes from
`docs/ui/sign-in.md` and `docs/ui/admin-people.md`. Nothing here logs or stores a password.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from slas_api.authz import RolesLoader, principal_for
from slas_api.db import session_scope
from slas_api.errors import LOGS_API, ApiError
from slas_api.models import (
    BOOTSTRAP_ADMIN_EMAIL,
    BOOTSTRAP_ADMIN_NAME,
    BOOTSTRAP_ADMIN_ROLE,
    AuditRow,
    Person,
    SessionRow,
    as_utc,
    new_id,
)
from slas_api.runtime_settings import detail_json, read_runtime
from slas_api.security import (
    Passwords,
    check_password_policy,
    new_one_time_password,
    new_session_token,
    token_sha256,
)
from slas_api.settings import ADMIN_INITIAL_PASSWORD_SECRET, Settings
from slas_api.throttle import Throttle, ThrottleUnavailableError
from slas_authz import SYSTEM, Capability, Principal, RoleSet
from slas_observability import tracing
from slas_observability.events import EventLog
from slas_observability.tracing import current_trace_id
from slas_schemas.errors import ThreePartMessage

Via = Literal["webui", "cli", "installer"]
Clock = Callable[[], datetime]

_EMAIL: Final = re.compile(r"^[^@\s]+@[^@\s]+$")

# --- sentences (docs/ui/sign-in.md, docs/ui/admin-people.md) ---------------------------------

WRONG_CREDENTIALS: Final = ThreePartMessage(
    "That email and password don't match.",
    "A typo, or the password was changed.",
    "Try again, or ask an administrator to reset your password.",
)
TOO_MANY_ATTEMPTS: Final = ThreePartMessage(
    "Too many sign-in attempts in the last 15 minutes.",
    "Several wrong passwords were tried for this account.",
    "Wait and try again.",
)
RATE_LIMITER_DOWN: Final = ThreePartMessage(
    "Sign-in is paused for a moment.",
    "The service that counts sign-in attempts didn't answer.",
    "Try again in a minute; if it repeats, run `slas logs redis` on the host.",
)
SESSION_NONE: Final = ThreePartMessage(
    "You're not signed in.",
    "There is no sign-in for this browser, or it was signed out.",
    "Sign in to continue.",
)
MUST_CHANGE_PASSWORD: Final = ThreePartMessage(
    "Choose a new password first.",
    "Your password was made for one use.",
    "Pick one only you know, then come back.",
)
CURRENT_PASSWORD_WRONG: Final = ThreePartMessage(
    "The current password isn't right.",
    "A typo, or the password was changed.",
    "Type your current password again.",
)
PERSON_NOT_FOUND: Final = ThreePartMessage(
    "There is no person with that id.",
    "They may have been removed, or the link is stale.",
    "Reload the People page.",
)
INVALID_EMAIL: Final = ThreePartMessage(
    "That doesn't look like an email address.",
    "A missing @ or domain.",
    "Check it and try again.",
)
EMPTY_NAME: Final = ThreePartMessage(
    "The name is empty.",
    "Everything was deleted from the field.",
    "Type the person's name as it should appear on their commits and tickets.",
)
SWITCH_OFF_SELF: Final = ThreePartMessage(
    "You can't switch off your own account.",
    "You're signed in with it.",
    "Ask another administrator.",
)
LAST_ADMIN_SWITCH_OFF: Final = ThreePartMessage(
    "You can't switch off the last administrator.",
    "Without an administrator nobody could manage people or settings.",
    "Make someone else an administrator first.",
)
LAST_ADMIN_ROLE: Final = ThreePartMessage(
    "You can't change the last administrator's role.",
    "Without an administrator nobody could manage people or settings.",
    "Make someone else an administrator first.",
)


def session_expired(hours: int) -> ThreePartMessage:
    span = "1 hour" if hours == 1 else f"{hours} hours"
    return ThreePartMessage(
        f"Your sign-in ended after {span}.",
        "Sign-ins last a fixed time so that a forgotten browser can't be used by someone else.",
        "Sign in again to continue.",
    )


def duplicate_email(email: str) -> ThreePartMessage:
    return ThreePartMessage(
        f"Someone already signs in as {email}.",
        "The address belongs to an existing person, maybe switched off.",
        "Use another address, or switch the existing account back on.",
    )


def unknown_role(role: str, roles: RoleSet) -> ThreePartMessage:
    labels = ", ".join(f"{r.label} ({r.id})" for r in roles.roles.values())
    return ThreePartMessage(
        f"There is no role called {role}.",
        f"The roles are {labels}.",
        "Choose one of them.",
    )


# --- shared context ------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Services:
    """Everything a request or a CLI command needs, built once per process."""

    settings: Settings
    engine: Engine
    roles: RolesLoader
    passwords: Passwords
    log: EventLog
    clock: Clock = utc_now
    version: str = "0.0.1"


class PersonOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    display_name: str
    role: str
    role_label: str
    capabilities: list[str]
    must_change_password: bool
    is_active: bool
    last_sign_in_at: str | None


def format_ts(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return None if aware is None else aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def person_out(person: Person, roles: RoleSet) -> PersonOut:
    principal = principal_for(person, roles)
    return PersonOut(
        id=person.id,
        email=person.email,
        display_name=person.display_name,
        role=person.role,
        role_label=principal.role_label,
        capabilities=sorted(c.value for c in principal.capabilities),
        must_change_password=person.must_change_password,
        is_active=person.is_active,
        last_sign_in_at=format_ts(person.last_sign_in_at),
    )


def audit(
    db: Session,
    *,
    actor: Principal,
    action: str,
    subject: str,
    detail: Mapping[str, object],
    via: Via,
    now: datetime,
) -> None:
    """One row per change. `detail` is what the caller passes; never a password."""
    forbidden = {"password", "one_time_password", "password_hash", "token", "new_password"}
    if forbidden & set(detail):
        raise ValueError("audit detail must not carry a password or token")
    db.add(
        AuditRow(
            at=now,
            actor=actor.subject,
            action=action,
            subject=subject,
            detail=detail_json(detail),
            via=via,
            trace_id=current_trace_id(),
        )
    )


def normalise_email(email: str) -> str:
    return email.strip().lower()


# --- people --------------------------------------------------------------------------------


def list_people(db: Session) -> list[Person]:
    return list(db.scalars(select(Person).order_by(Person.display_name, Person.email)).all())


def get_person(db: Session, person_id: str) -> Person:
    person = db.get(Person, person_id)
    if person is None:
        raise ApiError(404, PERSON_NOT_FOUND)
    return person


def find_by_email(db: Session, email: str) -> Person | None:
    return db.scalar(select(Person).where(Person.email == normalise_email(email)))


def _holds_admin(person: Person, roles: RoleSet) -> bool:
    role = roles.get(person.role)
    return role is not None and role.can(Capability.ADMIN_PEOPLE)


def active_administrators(db: Session, roles: RoleSet) -> int:
    return sum(
        1
        for person in db.scalars(select(Person).where(Person.is_active.is_(True))).all()
        if _holds_admin(person, roles)
    )


def revoke_sessions(db: Session, person_id: str, *, keep: str | None = None) -> int:
    statement = delete(SessionRow).where(SessionRow.person_id == person_id)
    if keep is not None:
        statement = statement.where(SessionRow.id != keep)
    result = cast("CursorResult[Any]", db.execute(statement))
    return int(result.rowcount or 0)


def add_person(
    svc: Services,
    db: Session,
    *,
    actor: Principal,
    via: Via,
    email: str,
    display_name: str,
    role: str,
    password: str | None = None,
) -> tuple[Person, str | None]:
    """Add a person. Returns the row and the one-time password when one was generated."""
    roles = svc.roles.current()
    address = normalise_email(email)
    if not _EMAIL.match(address):
        raise ApiError(400, INVALID_EMAIL)
    name = display_name.strip()
    if not name:
        raise ApiError(400, EMPTY_NAME)
    if roles.get(role) is None:
        raise ApiError(400, unknown_role(role, roles))
    if find_by_email(db, address) is not None:
        raise ApiError(409, duplicate_email(address))
    one_time: str | None = None
    if password is None:
        one_time = new_one_time_password()
        secret = one_time
        must_change = True
    else:
        problem = check_password_policy(password, address)
        if problem is not None:
            raise ApiError(400, problem)
        secret = password
        must_change = False
    now = svc.clock()
    person = Person(
        id=new_id(),
        email=address,
        display_name=name,
        role=role,
        password_hash=svc.passwords.hash(secret),
        must_change_password=must_change,
        is_active=True,
        created_at=now,
    )
    db.add(person)
    audit(
        db,
        actor=actor,
        action="person.added",
        subject=address,
        detail={
            "display_name": name,
            "role": role,
            "one_time_password_issued": one_time is not None,
        },
        via=via,
        now=now,
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, duplicate_email(address)) from exc
    return person, one_time


def update_person(
    svc: Services,
    db: Session,
    *,
    actor: Principal,
    via: Via,
    person_id: str,
    role: str | None = None,
    is_active: bool | None = None,
) -> Person:
    roles = svc.roles.current()
    person = get_person(db, person_id)
    now = svc.clock()
    if role is not None and roles.get(role) is None:
        raise ApiError(400, unknown_role(role, roles))
    losing_admin = person.is_active and _holds_admin(person, roles)
    if is_active is False:
        if person.email == actor.subject and not actor.is_system:
            raise ApiError(400, SWITCH_OFF_SELF)
        if losing_admin and active_administrators(db, roles) <= 1:
            raise ApiError(400, LAST_ADMIN_SWITCH_OFF)
    if role is not None and role != person.role and losing_admin:
        new_role = roles.get(role)
        if (
            new_role is not None
            and not new_role.can(Capability.ADMIN_PEOPLE)
            and active_administrators(db, roles) <= 1
        ):
            raise ApiError(400, LAST_ADMIN_ROLE)
    if role is not None and role != person.role:
        detail = {"from": person.role, "to": role}
        person.role = role
        audit(
            db,
            actor=actor,
            action="person.role_changed",
            subject=person.email,
            detail=detail,
            via=via,
            now=now,
        )
    if is_active is not None and is_active != person.is_active:
        person.is_active = is_active
        revoked = 0 if is_active else revoke_sessions(db, person.id)
        audit(
            db,
            actor=actor,
            action="person.switched_on" if is_active else "person.switched_off",
            subject=person.email,
            detail={"sessions_revoked": revoked},
            via=via,
            now=now,
        )
    db.flush()
    return person


def reset_password(
    svc: Services, db: Session, *, actor: Principal, via: Via, person_id: str
) -> tuple[Person, str]:
    person = get_person(db, person_id)
    now = svc.clock()
    one_time = new_one_time_password()
    person.password_hash = svc.passwords.hash(one_time)
    person.must_change_password = True
    revoked = revoke_sessions(db, person.id)
    audit(
        db,
        actor=actor,
        action="person.password_reset",
        subject=person.email,
        detail={"sessions_revoked": revoked},
        via=via,
        now=now,
    )
    db.flush()
    return person, one_time


def change_own_password(
    svc: Services,
    db: Session,
    *,
    person: Person,
    via: Via,
    current_password: str,
    new_password: str,
    keep_session: str | None,
) -> Person:
    if not svc.passwords.verify(person.password_hash, current_password):
        raise ApiError(401, CURRENT_PASSWORD_WRONG, reason="none")
    problem = check_password_policy(new_password, person.email)
    if problem is not None:
        raise ApiError(400, problem)
    now = svc.clock()
    person.password_hash = svc.passwords.hash(new_password)
    was_one_time = person.must_change_password
    person.must_change_password = False
    revoked = revoke_sessions(db, person.id, keep=keep_session)
    principal = principal_for(person, svc.roles.current())
    audit(
        db,
        actor=principal,
        action="person.password_changed",
        subject=person.email,
        detail={"sessions_revoked": revoked, "replaced_one_time_password": was_one_time},
        via=via,
        now=now,
    )
    if person.email == BOOTSTRAP_ADMIN_EMAIL and person.bootstrap_consumed_at is None:
        person.bootstrap_consumed_at = now
        audit(
            db,
            actor=principal,
            action="bootstrap.consumed",
            subject=person.email,
            detail={},
            via=via,
            now=now,
        )
    db.flush()
    return person


# --- sessions ------------------------------------------------------------------------------


def sign_in(
    svc: Services,
    db: Session,
    throttle: Throttle,
    *,
    email: str,
    password: str,
    address: str,
) -> tuple[Person, str, int]:
    """Verify, throttle, open a session. Returns the person, the raw token and the lifetime."""
    address_key = normalise_email(email)
    try:
        if throttle.is_blocked(address_key, address):
            raise ApiError(429, TOO_MANY_ATTEMPTS)
    except ThrottleUnavailableError as exc:
        svc.log.warning("signin.throttle_unavailable", error=str(exc))
        raise ApiError(503, RATE_LIMITER_DOWN) from exc
    person = find_by_email(db, address_key)
    verified = svc.passwords.verify(person.password_hash if person else None, password)
    if person is None or not verified or not person.is_active:
        try:
            throttle.record_failure(address_key, address)
        except ThrottleUnavailableError as exc:
            svc.log.warning("signin.throttle_unavailable", error=str(exc))
            raise ApiError(503, RATE_LIMITER_DOWN) from exc
        svc.log.info("signin.failed", known=person is not None)
        raise ApiError(401, WRONG_CREDENTIALS, reason="none")
    if svc.passwords.needs_rehash(person.password_hash):
        person.password_hash = svc.passwords.hash(password)
    try:
        throttle.clear(address_key)
    except ThrottleUnavailableError as exc:
        svc.log.warning("signin.throttle_unavailable", error=str(exc))
    now = svc.clock()
    hours = read_runtime(db).session_lifetime_hours
    token = new_session_token()
    db.add(
        SessionRow(
            id=new_id(),
            person_id=person.id,
            token_sha256=token_sha256(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=hours),
        )
    )
    person.last_sign_in_at = now
    db.flush()
    svc.log.info("signin.ok", role=person.role)
    return person, token, hours


def resolve_session(svc: Services, db: Session, token: str | None) -> tuple[Person, SessionRow]:
    """The person behind a cookie, or a 401 with `reason` set. Slides the expiry."""
    if not token:
        raise ApiError(401, SESSION_NONE, reason="none")
    row = db.scalar(select(SessionRow).where(SessionRow.token_sha256 == token_sha256(token)))
    if row is None:
        raise ApiError(401, SESSION_NONE, reason="none")
    now = svc.clock()
    expires_at = as_utc(row.expires_at)
    last_seen = as_utc(row.last_seen_at)
    if expires_at is None or last_seen is None or expires_at <= now:
        # expires_at - last_seen_at is the lifetime chosen at sign-in; sliding keeps it constant
        lifetime = (
            expires_at - last_seen
            if expires_at is not None and last_seen is not None
            else timedelta(hours=read_runtime(db).session_lifetime_hours)
        )
        db.delete(row)
        db.commit()
        hours = max(1, round(lifetime.total_seconds() / 3600))
        raise ApiError(401, session_expired(hours), reason="expired")
    person = db.get(Person, row.person_id)
    if person is None or not person.is_active:
        revoke_sessions(db, row.person_id)
        db.commit()
        raise ApiError(401, SESSION_NONE, reason="none")
    lifetime = expires_at - last_seen
    row.last_seen_at = now
    row.expires_at = now + lifetime
    return person, row


def sign_out(db: Session, token: str | None) -> bool:
    if not token:
        return False
    statement = delete(SessionRow).where(SessionRow.token_sha256 == token_sha256(token))
    result = cast("CursorResult[Any]", db.execute(statement))
    return bool(result.rowcount)


# --- bootstrap -----------------------------------------------------------------------------


def bootstrap(svc: Services) -> str:
    """Create admin@slas.local when the people table is empty. Returns a sentence."""
    with tracing.trace(current_trace_id()), session_scope(svc.engine) as db:
        count = db.scalar(select(func.count()).select_from(Person)) or 0
        if count > 0:
            return f"{count} {'person is' if count == 1 else 'people are'} registered."
        initial = svc.settings.read_secret(ADMIN_INITIAL_PASSWORD_SECRET)
        if not initial:
            sentence = (
                f"No administrator was created: the secret {ADMIN_INITIAL_PASSWORD_SECRET} is "
                f"missing. Run ./install.sh again; if it repeats, {LOGS_API}."
            )
            svc.log.warning("bootstrap.secret_missing", secret=ADMIN_INITIAL_PASSWORD_SECRET)
            return sentence
        now = svc.clock()
        db.add(
            Person(
                id=new_id(),
                email=BOOTSTRAP_ADMIN_EMAIL,
                display_name=BOOTSTRAP_ADMIN_NAME,
                role=BOOTSTRAP_ADMIN_ROLE,
                password_hash=svc.passwords.hash(initial),
                must_change_password=True,
                is_active=True,
                created_at=now,
            )
        )
        audit(
            db,
            actor=SYSTEM,
            action="person.bootstrapped",
            subject=BOOTSTRAP_ADMIN_EMAIL,
            detail={"role": BOOTSTRAP_ADMIN_ROLE},
            via="installer",
            now=now,
        )
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return "Another api instance created the administrator first."
    svc.log.info("bootstrap.created", email=BOOTSTRAP_ADMIN_EMAIL)
    return (
        f"Created {BOOTSTRAP_ADMIN_EMAIL}; sign in with the one-time password from "
        "install.sh and choose a new one."
    )


BootstrapStatus = Literal["pending", "done"]


def bootstrap_status(svc: Services) -> BootstrapStatus:
    with session_scope(svc.engine) as db:
        admin = find_by_email(db, BOOTSTRAP_ADMIN_EMAIL)
        if admin is None:
            return "pending"
        if admin.bootstrap_consumed_at is None and admin.must_change_password:
            return "pending"
        return "done"
