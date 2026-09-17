"""The routes of docs/api-contract.md, round 1. HTTP only; the work is in `service.py`.

Two authentication dependencies: `signed_in` (a valid session) and `ready` (a valid session
whose person has already replaced a one-time password). `GET /me`, `POST /me/password`,
`DELETE /session`, the public route and health use the first or none; everything else the
second (the `must_change_password` gate).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from slas_api import service
from slas_api.authz import principal_for, require
from slas_api.db import postgres_ok
from slas_api.errors import ApiError
from slas_api.models import Person, SessionRow
from slas_api.registry_view import models_view
from slas_api.runtime_settings import (
    apply_changes,
    install_facts,
    mirror_to_env,
    notice_sentence,
    read_runtime,
)
from slas_api.service import Services
from slas_api.throttle import Throttle
from slas_authz import Capability, Principal
from slas_observability.metrics import REGISTRY
from slas_schemas.errors import ThreePartMessage

SESSION_COOKIE = "__Host-slas_session"
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

ops = APIRouter()
api = APIRouter(prefix="/api/v1")


# --- dependencies --------------------------------------------------------------------------


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def get_throttle(request: Request) -> Throttle:
    throttle: Throttle = request.app.state.throttle
    return throttle


def get_db(request: Request) -> Iterator[Session]:
    services = get_services(request)
    db = Session(services.engine, expire_on_commit=False)
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def client_address(request: Request) -> str:
    """The caller's address. The edge is the only way in, so its forwarded header is trusted."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@dataclass(slots=True)
class Auth:
    person: Person
    session: SessionRow
    principal: Principal


ServicesDep = Annotated[Services, Depends(get_services)]
DbDep = Annotated[Session, Depends(get_db)]
ThrottleDep = Annotated[Throttle, Depends(get_throttle)]


def signed_in(request: Request, svc: ServicesDep, db: DbDep) -> Auth:
    person, row = service.resolve_session(svc, db, request.cookies.get(SESSION_COOKIE))
    return Auth(person, row, principal_for(person, svc.roles.current()))


SignedIn = Annotated[Auth, Depends(signed_in)]


def ready(auth: SignedIn) -> Auth:
    if auth.person.must_change_password:
        raise ApiError(403, service.MUST_CHANGE_PASSWORD)
    return auth


Ready = Annotated[Auth, Depends(ready)]


def _set_session_cookie(response: Response, svc: Services, token: str, hours: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=hours * 3600,
        path="/",
        secure=svc.settings.slas_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response, svc: Services) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=svc.settings.slas_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _person(svc: Services, person: Person) -> dict[str, Any]:
    return service.person_out(person, svc.roles.current()).model_dump()


# --- health and metrics (not under /api, no auth) ------------------------------------------


@ops.get("/health")
def health(svc: ServicesDep, throttle: ThrottleDep) -> Response:
    checks = {
        "postgres": "ok" if postgres_ok(svc.engine) else "down",
        "redis": "ok" if throttle.ping() else "down",
    }
    if all(state == "ok" for state in checks.values()):
        return JSONResponse({"service": "api", "ok": True, "checks": checks})
    failing = [name for name, state in checks.items() if state != "ok"]
    named = " and ".join(failing)
    raise ApiError(
        503,
        ThreePartMessage(
            f"The api is not healthy: {named} did not answer.",
            f"The {named} service is starting or stopped.",
            f"Wait a moment; if it repeats, run `slas logs {failing[0]}` on the host.",
        ),
    )


@ops.get("/metrics")
def metrics() -> Response:
    return PlainTextResponse(REGISTRY.render(), media_type=METRICS_CONTENT_TYPE)


# --- public --------------------------------------------------------------------------------


@api.get("/public/installation")
def public_installation(svc: ServicesDep, db: DbDep) -> dict[str, Any]:
    runtime = read_runtime(db)
    return {
        "installation_name": runtime.installation_name,
        "auth_modes": svc.settings.auth_modes(),
        "version": install_facts(svc.settings, svc.version).version,
        "session_lifetime_hours": runtime.session_lifetime_hours,
    }


# --- session -------------------------------------------------------------------------------


class SignInBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str


class PasswordChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


@api.post("/session")
def create_session(
    body: SignInBody,
    request: Request,
    response: Response,
    svc: ServicesDep,
    db: DbDep,
    throttle: ThrottleDep,
) -> dict[str, Any]:
    person, token, hours = service.sign_in(
        svc,
        db,
        throttle,
        email=body.email,
        password=body.password,
        address=client_address(request),
    )
    _set_session_cookie(response, svc, token, hours)
    return _person(svc, person)


@api.delete("/session", status_code=204)
def delete_session(request: Request, svc: ServicesDep, db: DbDep) -> Response:
    service.sign_out(db, request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    _clear_session_cookie(response, svc)
    return response


@api.get("/me")
def me(auth: SignedIn, svc: ServicesDep) -> dict[str, Any]:
    return _person(svc, auth.person)


@api.post("/me/password")
def change_password(
    body: PasswordChangeBody, auth: SignedIn, svc: ServicesDep, db: DbDep
) -> dict[str, Any]:
    person = service.change_own_password(
        svc,
        db,
        person=auth.person,
        via="webui",
        current_password=body.current_password,
        new_password=body.new_password,
        keep_session=auth.session.id,
    )
    return _person(svc, person)


# --- people --------------------------------------------------------------------------------


class NewPersonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    display_name: str
    role: str


class PatchPersonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    is_active: bool | None = None


@api.get("/admin/people")
def list_people(auth: Ready, svc: ServicesDep, db: DbDep) -> list[dict[str, Any]]:
    require(auth.principal, Capability.ADMIN_PEOPLE)
    return [_person(svc, person) for person in service.list_people(db)]


@api.post("/admin/people", status_code=201)
def add_person(body: NewPersonBody, auth: Ready, svc: ServicesDep, db: DbDep) -> dict[str, Any]:
    require(auth.principal, Capability.ADMIN_PEOPLE)
    person, one_time = service.add_person(
        svc,
        db,
        actor=auth.principal,
        via="webui",
        email=body.email,
        display_name=body.display_name,
        role=body.role,
    )
    return {"person": _person(svc, person), "one_time_password": one_time}


PersonId = Annotated[str, Path(alias="id")]


@api.patch("/admin/people/{id}")
def patch_person(
    person_id: PersonId, body: PatchPersonBody, auth: Ready, svc: ServicesDep, db: DbDep
) -> dict[str, Any]:
    require(auth.principal, Capability.ADMIN_PEOPLE)
    person = service.update_person(
        svc,
        db,
        actor=auth.principal,
        via="webui",
        person_id=person_id,
        role=body.role,
        is_active=body.is_active,
    )
    return _person(svc, person)


@api.post("/admin/people/{id}/password-reset")
def reset_person_password(
    person_id: PersonId, auth: Ready, svc: ServicesDep, db: DbDep
) -> dict[str, Any]:
    require(auth.principal, Capability.ADMIN_PEOPLE)
    _, one_time = service.reset_password(
        svc, db, actor=auth.principal, via="webui", person_id=person_id
    )
    return {"one_time_password": one_time}


# --- settings ------------------------------------------------------------------------------


class PatchSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_name: str | None = None
    chinese_variant: str | None = None
    session_lifetime_hours: int | str | None = None


@api.get("/admin/settings")
def get_settings(_auth: Ready, svc: ServicesDep, db: DbDep) -> dict[str, Any]:
    return {
        "runtime": read_runtime(db).model_dump(),
        "install": install_facts(svc.settings, svc.version).model_dump(),
    }


@api.patch("/admin/settings")
def patch_settings(
    body: PatchSettingsBody, auth: Ready, svc: ServicesDep, db: DbDep
) -> dict[str, Any]:
    require(auth.principal, Capability.ADMIN_SETTINGS)
    changes = {key: value for key, value in body.model_dump().items() if value is not None}
    before = read_runtime(db).as_strings()
    now = svc.clock()
    runtime = apply_changes(db, changes, now)
    after = runtime.as_strings()
    changed = {key: after[key] for key in after if before[key] != after[key]}
    if changed:
        service.audit(
            db,
            actor=auth.principal,
            action="settings.changed",
            subject="runtime",
            detail={"changed": changed},
            via="webui",
            now=now,
        )
    db.commit()
    notice = mirror_to_env(runtime, svc.settings.env_file)
    if notice is not None:
        svc.log.warning("settings.mirror_failed", what_happened=notice.what_happened)
    return {"runtime": runtime.model_dump(), "notice": notice_sentence(notice)}


# --- models and the Home lists -------------------------------------------------------------


@api.get("/models")
def models(_auth: Ready, svc: ServicesDep) -> dict[str, Any]:
    return models_view(svc.settings)


@api.get("/coding/tasks")
def coding_tasks(_auth: Ready) -> list[dict[str, Any]]:
    return []


@api.get("/validation/runs")
def validation_runs(_auth: Ready) -> list[dict[str, Any]]:
    return []


@api.get("/factory/jobs")
def factory_jobs(_auth: Ready) -> list[dict[str, Any]]:
    return []
