"""`ServiceClient`: how one service calls another (docs/api-contract-round-2.md).

JSON in and out. Every request carries the trace headers of the current context and, when
given, the acting person's identity headers. A three-part body comes back as a
`ServiceError` with the same status and sentences, so the caller can pass it on unchanged;
a connection failure or a timeout becomes a three-part 503 that names the service and the
`slas logs` command. Nothing is retried here: a step that has a physical effect must not run
twice because a socket blinked (INV-6).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

import httpx

from slas_http.errors import ServiceError
from slas_http.identity import Identity
from slas_observability import tracing
from slas_schemas.errors import ThreePartMessage

DEFAULT_TIMEOUT_S: Final = 30.0
#: One kernel step may wait for a server to boot (boot_timeout_s 900) and settle.
LONG_TIMEOUT_S: Final = 3900.0


class ServiceUnreachableError(ServiceError):
    def __init__(self, service: str, detail: str) -> None:
        super().__init__(
            503,
            ThreePartMessage(
                f"The {service} did not answer.",
                f"It is starting, stopped, or the request took too long ({detail}).",
                f"Wait a moment and try again; if it repeats, run `slas logs {service}` "
                "on the host.",
            ),
        )
        self.service = service


class ServiceClient:
    """A JSON client for one service of the stack."""

    def __init__(
        self,
        service: str,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    # --- verbs -------------------------------------------------------------------------

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        identity: Identity | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("GET", path, params=params, identity=identity, timeout_s=timeout_s)

    def post(
        self,
        path: str,
        body: Any = None,
        *,
        identity: Identity | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("POST", path, body=body, identity=identity, timeout_s=timeout_s)

    def put(
        self,
        path: str,
        body: Any = None,
        *,
        identity: Identity | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("PUT", path, body=body, identity=identity, timeout_s=timeout_s)

    def delete(
        self, path: str, *, identity: Identity | None = None, timeout_s: float | None = None
    ) -> Any:
        return self.request("DELETE", path, identity=identity, timeout_s=timeout_s)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
        identity: Identity | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        headers = tracing.outbound_headers()
        if identity is not None:
            headers.update(identity.headers())
        try:
            response = self._client.request(
                method,
                path,
                json=body,
                params=dict(params) if params else None,
                headers=headers,
                timeout=timeout_s if timeout_s is not None else self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ServiceUnreachableError(self.service, f"timed out: {exc!s}"[:200]) from exc
        except httpx.HTTPError as exc:
            raise ServiceUnreachableError(self.service, type(exc).__name__) from exc
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            payload: Any = None
        else:
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                if not response.is_success:
                    raise self._error_from(response.status_code, None) from exc
                raise ServiceUnreachableError(
                    self.service, f"answered {response.status_code} without JSON"
                ) from exc
        if response.is_success:
            return payload
        raise self._error_from(response.status_code, payload)

    def _error_from(self, status: int, payload: Any) -> ServiceError:
        if isinstance(payload, dict) and all(
            isinstance(payload.get(key), str) and payload[key].strip()
            for key in ("what_happened", "likely_cause", "what_to_do")
        ):
            return ServiceError(
                status,
                ThreePartMessage(
                    str(payload["what_happened"]),
                    str(payload["likely_cause"]),
                    str(payload["what_to_do"]),
                ),
            )
        return ServiceError(
            status,
            ThreePartMessage(
                f"The {self.service} refused the request.",
                f"It answered {status} without a sentence of its own.",
                f"Try again; if it repeats, run `slas logs {self.service}` on the host.",
            ),
        )
