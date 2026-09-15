"""One trace id from the WebUI to the executor (CLAUDE.md §8.2, §11).

The WebUI mints a W3C `traceparent` for every request; the api accepts it (or mints one),
binds it to the request's context, and every hop after that — the kernel, the gateway, the
vLLM call, the executor — reads `current_trace_id()` and forwards it in `traceparent` and
`X-Slas-Trace-Id`. Journal entries and JSON events carry the same id, so one grep over the
sinks shows a whole run. Standard library only; OpenTelemetry can replace the plumbing
without changing a call site because nothing here is exported beyond these functions.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Final

TRACEPARENT_HEADER: Final = "traceparent"
SLAS_HEADER: Final = "X-Slas-Trace-Id"

_TRACE_ID: Final = re.compile(r"^[0-9a-f]{32}$")
_TRACEPARENT: Final = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE: Final = "0" * 32
_ZERO_SPAN: Final = "0" * 16

_current: ContextVar[str | None] = ContextVar("slas_trace_id", default=None)


def new_trace_id() -> str:
    while True:
        candidate = secrets.token_hex(16)
        if candidate != _ZERO_TRACE:
            return candidate


def new_span_id() -> str:
    while True:
        candidate = secrets.token_hex(8)
        if candidate != _ZERO_SPAN:
            return candidate


def is_trace_id(value: str) -> bool:
    return bool(_TRACE_ID.match(value)) and value != _ZERO_TRACE


def format_traceparent(trace_id: str, span_id: str | None = None, *, sampled: bool = True) -> str:
    if not is_trace_id(trace_id):
        raise ValueError(f"{trace_id!r} is not a trace id (32 lowercase hex digits, not zero).")
    return f"00-{trace_id}-{span_id or new_span_id()}-{'01' if sampled else '00'}"


def parse_traceparent(value: str) -> str | None:
    """The trace id inside a `traceparent`, or None when the header is not one."""
    match = _TRACEPARENT.match(value.strip())
    if match is None:
        return None
    trace_id, span_id = match.group(1), match.group(2)
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id


def trace_id_from_headers(headers: Mapping[str, str]) -> str | None:
    """Accepts `traceparent` first, then `X-Slas-Trace-Id`; header names are case-insensitive."""
    lowered = {key.lower(): value for key, value in headers.items()}
    parent = lowered.get(TRACEPARENT_HEADER)
    if parent is not None:
        parsed = parse_traceparent(parent)
        if parsed is not None:
            return parsed
    plain = lowered.get(SLAS_HEADER.lower(), "").strip().lower()
    return plain if is_trace_id(plain) else None


def current_trace_id() -> str | None:
    return _current.get()


def ensure_trace_id() -> str:
    """The current trace id, minting and binding one when the context has none."""
    existing = _current.get()
    if existing is not None:
        return existing
    minted = new_trace_id()
    _current.set(minted)
    return minted


def bind(trace_id: str) -> Token[str | None]:
    if not is_trace_id(trace_id):
        raise ValueError(f"{trace_id!r} is not a trace id.")
    return _current.set(trace_id)


def unbind(token: Token[str | None]) -> None:
    _current.reset(token)


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    """Bind `trace_id` (or a new one) for the block; restores the previous binding after."""
    chosen = trace_id or new_trace_id()
    token = bind(chosen)
    try:
        yield chosen
    finally:
        unbind(token)


def outbound_headers(trace_id: str | None = None) -> dict[str, str]:
    """Headers for the next hop: a fresh span under the current (or given) trace."""
    chosen = trace_id or ensure_trace_id()
    return {TRACEPARENT_HEADER: format_traceparent(chosen), SLAS_HEADER: chosen}


def accept(headers: Mapping[str, str]) -> str:
    """What an inbound edge does: take the caller's trace id or mint one, bind it, return it."""
    inbound = trace_id_from_headers(headers) or new_trace_id()
    _current.set(inbound)
    return inbound
