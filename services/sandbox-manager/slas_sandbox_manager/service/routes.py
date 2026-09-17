"""The `/v1` routes of the sandbox manager (docs/api-contract-round-2.md §4).

| Route | Body → Answer |
|---|---|
| `POST /v1/sessions` | open a sandbox on `Projects/<slug>` → `Session` + `sentence` |
| `GET /v1/sessions?user=&slug=` | the open sessions, filtered → `[Session + alive]` |
| `GET /v1/sessions/{id}` | `Session` + `alive` |
| `POST /v1/sessions/{id}/exec` | `{"argv": [str], "cwd"?, "timeout_s"?}` → `ExecResult` |
| `DELETE /v1/sessions/{id}` | 204 |
| `POST /v1/sessions/{id}/terminal` | `{"line"}` → `TerminalLine`; capability `git:terminal` |
| `GET /v1/toolchains` | `{"manifest", "source"}` |
| `POST /v1/toolchains/resolve` | `{"choices": [{"language", "version"}]}` → `[Resolution record]` |
| `POST /v1/languages/detect` | `{"plan"}` → `{"languages"}` |
| `POST /v1/reap` | `{}` → `{"closed"}` |

Every refusal is a three-part `ServiceError`; the sandbox's own errors keep their sentences.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from slas_container import ContainerError
from slas_http.errors import ServiceError
from slas_http.identity import identity_of, require
from slas_sandbox_manager.manager import SandboxError, Session
from slas_sandbox_manager.spec import WORKSPACE, HardeningError
from slas_sandbox_manager.terminal import TerminalSession
from slas_sandbox_manager.toolchains import (
    ToolchainError,
    detect_languages,
    resolve_all,
    toolchain_sentence,
)
from slas_schemas.errors import ThreePartMessage

if TYPE_CHECKING:
    from slas_sandbox_manager.service.app import Services

router = APIRouter(prefix="/v1")


# --- bodies ------------------------------------------------------------------------------------


class LanguageChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=1)
    version: str | None = None


class OpenSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str = Field(min_length=1)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1)
    languages: list[LanguageChoice] = Field(min_length=1)
    ticket_id: str = ""
    ttl_s: int | None = Field(default=None, ge=60, le=86400)


class ExecBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: Any
    cwd: str = WORKSPACE
    timeout_s: int = Field(default=600, ge=1, le=3600)


class TerminalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: str


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: list[LanguageChoice]


class DetectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str


# --- errors ------------------------------------------------------------------------------------


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def _from_sandbox_error(exc: SandboxError) -> ServiceError:
    what = exc.message.what_happened
    if "no longer open" in what:
        status = 404
    elif what.startswith("No sandbox can be opened"):
        status = 503
    else:
        status = 409
    return ServiceError(status, exc.message)


def _argv_error() -> ServiceError:
    return ServiceError(
        400,
        ThreePartMessage(
            "The command must be an argv list of strings.",
            "A shell line or an empty list was sent; the sandbox never joins strings into a shell "
            "command (CLAUDE.md §11).",
            'Send {"argv": ["git", "status"]}.',
        ),
    )


def _session_view(services: Services, session: Session) -> dict[str, Any]:
    with services.locked():
        alive = services.runtime.alive(session.handle)
    return {**session.model_dump(mode="json"), "alive": alive}


# --- sessions ----------------------------------------------------------------------------------


@router.post("/sessions", status_code=201)
def open_session(body: OpenSession, request: Request) -> dict[str, Any]:
    services = _services(request)
    try:
        resolutions = resolve_all(
            {choice.language: choice.version for choice in body.languages},
            services.manifest,
            registry=services.settings.registry,
        )
    except ToolchainError as exc:
        raise ServiceError(400, exc.message) from exc
    primary = resolutions[0]
    try:
        with services.locked() as manager:
            session = manager.open(
                body.user,
                body.slug,
                image=primary.image,
                language=primary.language,
                display_name=body.display_name,
                ttl_s=body.ttl_s,
            )
            if body.ticket_id:
                services.tickets[session.id] = body.ticket_id
    except SandboxError as exc:
        raise _from_sandbox_error(exc) from exc
    except HardeningError as exc:
        raise ServiceError(400, exc.message) from exc
    except ContainerError as exc:
        raise ServiceError(502, exc.message) from exc
    services.log.info(
        "sandbox.opened",
        session=session.id,
        user=body.user,
        slug=body.slug,
        image=primary.image,
        runtime=session.handle.spec.runtime,
        ticket_id=body.ticket_id,
    )
    sentence = " ".join(
        part
        for part in (
            session.sentence(services.clock.now()),
            session.runtime_sentence,
            toolchain_sentence(resolutions),
        )
        if part
    )
    return {**session.model_dump(mode="json"), "sentence": sentence}


@router.get("/sessions")
def list_sessions(request: Request, user: str = "", slug: str = "") -> list[dict[str, Any]]:
    services = _services(request)
    with services.locked() as manager:
        sessions = manager.sessions(user=user or None, slug=slug or None)
    return [_session_view(services, session) for session in sessions]


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    services = _services(request)
    try:
        with services.locked() as manager:
            session = manager.get(session_id)
    except SandboxError as exc:
        raise _from_sandbox_error(exc) from exc
    return _session_view(services, session)


@router.post("/sessions/{session_id}/exec")
def exec_in_session(session_id: str, body: ExecBody, request: Request) -> dict[str, Any]:
    services = _services(request)
    argv = body.argv
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or not part for part in argv)
    ):
        raise _argv_error()
    try:
        # Not under the lock: a build or test may run for minutes, and one sandbox's command
        # must not hold up another's. The manager's bookkeeping is attribute writes.
        result = services.manager.exec(session_id, argv, cwd=body.cwd, timeout_s=body.timeout_s)
    except SandboxError as exc:
        raise _from_sandbox_error(exc) from exc
    except ContainerError as exc:
        raise ServiceError(502, exc.message) from exc
    return result.model_dump(mode="json")


@router.delete("/sessions/{session_id}", status_code=204)
def close_session(session_id: str, request: Request) -> Response:
    services = _services(request)
    try:
        with services.locked() as manager:
            manager.close(session_id)
    except ContainerError as exc:
        raise ServiceError(502, exc.message) from exc
    services.terminals.pop(session_id, None)
    services.tickets.pop(session_id, None)
    services.log.info("sandbox.closed", session=session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/terminal")
def terminal_line(session_id: str, body: TerminalBody, request: Request) -> dict[str, Any]:
    services = _services(request)
    identity = identity_of(request)
    require(identity, "git:terminal", verb="use the sandbox terminal")
    with services.locked() as manager:
        try:
            manager.get(session_id)
        except SandboxError as exc:
            raise _from_sandbox_error(exc) from exc
        terminal = services.terminals.get(session_id)
        if terminal is None:
            ticket_id = services.tickets.get(session_id)
            record = (
                services.settings.data_root / "Tickets" / ticket_id / "terminal.jsonl"
                if ticket_id
                else None
            )
            terminal = TerminalSession(
                manager, session_id, clock=services.clock, record_path=record
            )
            services.terminals[session_id] = terminal
    try:
        entry = terminal.run(body.line)  # outside the lock, like exec
    except SandboxError as exc:
        raise _from_sandbox_error(exc) from exc
    except ContainerError as exc:
        raise ServiceError(502, exc.message) from exc
    if entry is None:
        raise ServiceError(
            400,
            ThreePartMessage(
                "There was nothing to run.",
                "The line was empty.",
                "Type a command and press Enter.",
            ),
        )
    return entry.model_dump(mode="json")


# --- toolchains ---------------------------------------------------------------------------------


@router.get("/toolchains")
def toolchains(request: Request) -> dict[str, Any]:
    manifest = _services(request).manifest
    return {
        "manifest": {k: list(v) for k, v in manifest.toolchains.items()},
        "source": manifest.source,
    }


@router.post("/toolchains/resolve")
def resolve_toolchains(body: ResolveBody, request: Request) -> list[dict[str, Any]]:
    services = _services(request)
    try:
        resolutions = resolve_all(
            {choice.language: choice.version for choice in body.choices},
            services.manifest,
            registry=services.settings.registry,
        )
    except ToolchainError as exc:
        raise ServiceError(400, exc.message) from exc
    return [resolution.to_record() for resolution in resolutions]


@router.post("/languages/detect")
def detect(body: DetectBody) -> dict[str, list[str]]:
    return {"languages": detect_languages(body.plan)}


# --- housekeeping -------------------------------------------------------------------------------


@router.post("/reap")
def reap(request: Request) -> dict[str, list[str]]:
    services = _services(request)
    with services.locked() as manager:
        closed = manager.reap()
        for session_id in closed:
            services.terminals.pop(session_id, None)
            services.tickets.pop(session_id, None)
    if closed:
        services.log.info("sandbox.reaped", closed=closed)
    return {"closed": closed}
