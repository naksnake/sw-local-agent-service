"""Every non-2xx answer of a service is one three-part problem (CLAUDE.md §11).

`ThreePartProblem` is the Pydantic twin of `slas_schemas.ThreePartMessage` plus the trace
id. Nothing else ever reaches a body: no `detail`, no stack trace. The api has its own copy
of this shape with the session `reason`; the two bodies are wire-compatible.
"""

from __future__ import annotations

from typing import Any, Self

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from slas_observability.tracing import current_trace_id, new_trace_id
from slas_schemas.errors import ThreePartMessage


class ThreePartProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what_happened: str
    likely_cause: str
    what_to_do: str
    trace_id: str

    @classmethod
    def from_message(cls, message: ThreePartMessage) -> ThreePartProblem:
        return cls(
            what_happened=message.what_happened,
            likely_cause=message.likely_cause,
            what_to_do=message.what_to_do,
            # Mint without binding: outside a request nothing may leak a trace id into the
            # caller's context.
            trace_id=current_trace_id() or new_trace_id(),
        )

    def body(self) -> dict[str, Any]:
        return self.model_dump()


class ServiceError(Exception):
    """A problem a service answers with `status` and exactly the three parts."""

    def __init__(
        self, status: int, message: ThreePartMessage, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message.what_happened)
        self.status = status
        self.message = message
        self.headers = headers or {}

    @classmethod
    def build(cls, status: int, what_happened: str, likely_cause: str, what_to_do: str) -> Self:
        return cls(status, ThreePartMessage(what_happened, likely_cause, what_to_do))


def problem_response(
    status: int, message: ThreePartMessage, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        ThreePartProblem.from_message(message).body(), status_code=status, headers=headers
    )


def logs_hint(service: str) -> str:
    return f"run `slas logs {service}` on the host"


def unexpected(service: str) -> ThreePartMessage:
    return ThreePartMessage(
        f"The {service} hit a problem it did not expect.",
        "A bug, or a service it depends on answered something new.",
        f"Try again; if it repeats, {logs_hint(service)} and quote the trace id.",
    )


def _http_status_message(service: str, status: int) -> ThreePartMessage:
    if status == 404:
        return ThreePartMessage(
            "There is nothing at this address.",
            "The link is stale or mistyped.",
            "Go to Home.",
        )
    if status == 405:
        return ThreePartMessage(
            "That address doesn't accept this kind of request.",
            f"The caller and the {service} disagree on how to call this route.",
            f"Reload the page and try again; if it repeats, {logs_hint(service)}.",
        )
    return ThreePartMessage(
        "The request could not be completed.",
        f"The {service} answered {status} without a sentence of its own.",
        f"Try again; if it repeats, {logs_hint(service)}.",
    )


def validation_sentence(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "the request body is not what the service expects"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", "invalid"))
    return f"{location}: {message}" if location else message


def install_exception_handlers(app: FastAPI, service: str) -> None:
    """Turn every error FastAPI knows about into a three-part body."""

    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return problem_response(exc.status, exc.message, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            exc.status_code,
            _http_status_message(service, exc.status_code),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            400,
            ThreePartMessage(
                f"The request couldn't be read: {validation_sentence(exc)}.",
                f"The caller sent something the {service} didn't expect.",
                f"Reload the page and try again; if it repeats, {logs_hint(service)}.",
            ),
        )
