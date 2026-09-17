"""Every non-2xx answer is one three-part problem (CLAUDE.md §11, ADR-0005 §errors).

`ThreePartProblem` is the Pydantic twin of `slas_schemas.ThreePartMessage`: what happened,
the likely cause, what to do, the trace id, and on a 401 the `reason` the UI uses to choose
its sentence. Nothing else ever reaches the body — no `detail`, no stack trace.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from slas_observability.tracing import current_trace_id, new_trace_id
from slas_schemas.errors import ThreePartMessage

Reason = Literal["expired", "none"]

LOGS_API = "run `slas logs api` on the host"


class ThreePartProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what_happened: str
    likely_cause: str
    what_to_do: str
    trace_id: str
    reason: Reason | None = None

    @classmethod
    def from_message(
        cls, message: ThreePartMessage, *, reason: Reason | None = None
    ) -> ThreePartProblem:
        return cls(
            what_happened=message.what_happened,
            likely_cause=message.likely_cause,
            what_to_do=message.what_to_do,
            # Mint without binding: outside a request (a CLI, a unit test) nothing may leak
            # a trace id into the caller's context.
            trace_id=current_trace_id() or new_trace_id(),
            reason=reason,
        )

    def body(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ApiError(Exception):
    """A problem the api answers with `status` and exactly the three parts."""

    def __init__(
        self,
        status: int,
        message: ThreePartMessage,
        *,
        reason: Reason | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message.what_happened)
        self.status = status
        self.message = message
        self.reason = reason if status == 401 else None
        self.headers = headers or {}

    @classmethod
    def build(
        cls,
        status: int,
        what_happened: str,
        likely_cause: str,
        what_to_do: str,
        *,
        reason: Reason | None = None,
    ) -> ApiError:
        return cls(status, ThreePartMessage(what_happened, likely_cause, what_to_do), reason=reason)


def problem_response(
    status: int,
    message: ThreePartMessage,
    *,
    reason: Reason | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if status != 401:
        reason = None
    problem = ThreePartProblem.from_message(message, reason=reason)
    return JSONResponse(problem.body(), status_code=status, headers=headers)


UNEXPECTED = ThreePartMessage(
    "The api hit a problem it did not expect.",
    "A bug, or a service the api depends on answered something new.",
    f"Try again; if it repeats, {LOGS_API} and quote the trace id.",
)

NOT_FOUND = ThreePartMessage(
    "There is nothing at this address.",
    "The link is stale or mistyped.",
    "Go to Home.",
)

METHOD_NOT_ALLOWED = ThreePartMessage(
    "That address doesn't accept this kind of request.",
    "The page and the api disagree on how to call this route.",
    f"Reload the page and try again; if it repeats, {LOGS_API}.",
)


def _http_status_message(status: int) -> ThreePartMessage:
    if status == 404:
        return NOT_FOUND
    if status == 405:
        return METHOD_NOT_ALLOWED
    return ThreePartMessage(
        "The request could not be completed.",
        f"The api answered {status} without a sentence of its own.",
        f"Try again; if it repeats, {LOGS_API}.",
    )


def _validation_sentence(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "the request body is not what the api expects"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", "invalid"))
    return f"{location}: {message}" if location else message


def install_exception_handlers(app: FastAPI) -> None:
    """Turn every error FastAPI knows about into a three-part body."""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(exc.status, exc.message, reason=exc.reason, headers=exc.headers)

    # Starlette's own class: the router raises it for an unknown path or method, and
    # FastAPI's subclass inherits the handler.
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            exc.status_code,
            _http_status_message(exc.status_code),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            400,
            ThreePartMessage(
                f"The request couldn't be read: {_validation_sentence(exc)}.",
                "The page sent something the api didn't expect.",
                f"Reload the page and try again; if it repeats, {LOGS_API}.",
            ),
        )
