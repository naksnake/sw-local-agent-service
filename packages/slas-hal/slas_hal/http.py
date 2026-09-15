"""HTTPS to a BMC (Redfish) behind one small protocol so tests use a fake server.

The Basic Authorization header is built in memory by the driver; the audit trail records
method, path, status and duration and never a header or a body.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol

from pydantic import Field

from slas_schemas.common import SlasModel


class HttpRequest(SlasModel):
    method: str = Field(pattern=r"^(GET|POST|PATCH|DELETE)$")
    url: str = Field(pattern=r"^https?://")
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout_s: float = Field(default=20.0, gt=0)


class HttpResponse(SlasModel):
    status: int
    body: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class HttpError(RuntimeError):
    """Transport failure: no answer, TLS refused, connection reset."""


class HttpClient(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class UrllibHttpClient:
    """Standard-library HTTPS. TLS is verified against `ca_bundle` (the platform bundle by
    default); a BMC with a self-signed certificate needs its CA file on the target record."""

    def __init__(self, *, ca_bundle: str | None = None) -> None:
        self.ca_bundle = ca_bundle

    def send(self, request: HttpRequest) -> HttpResponse:
        data = request.body.encode("utf-8") if request.body is not None else None
        headers: dict[str, str] = {"Accept": "application/json", **request.headers}
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(  # noqa: S310 — scheme fixed to http(s) by the model
            request.url, data=data, headers=headers, method=request.method
        )
        context = (
            ssl.create_default_context(cafile=self.ca_bundle)
            if request.url.startswith("https")
            else None
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — the target record names the host
                req, timeout=request.timeout_s, context=context
            ) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read().decode("utf-8", "replace"),
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                body=exc.read().decode("utf-8", "replace"),
                headers={k.lower(): v for k, v in exc.headers.items()},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpError(
                str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)
            ) from exc


class RouteHandler(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


class FakeHttpClient:
    """Hands every request to a handler (a `FakeRedfishServer`) and keeps a request log
    without the Authorization header, the way the driver's audit trail should."""

    def __init__(self, handler: RouteHandler, *, unreachable: bool = False) -> None:
        self.handler = handler
        self.unreachable = unreachable
        self.requests: list[tuple[str, str]] = []
        self.auth_headers_seen: list[str] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        if self.unreachable:
            raise HttpError("[Errno 113] No route to host")
        self.requests.append((request.method, request.url))
        for name, value in request.headers.items():
            if name.lower() == "authorization":
                self.auth_headers_seen.append(value)
        return self.handler(request)


def basic_auth_header(user: str, password: str) -> Mapping[str, str]:
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}
