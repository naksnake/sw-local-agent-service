"""mTLS between the factory executor and a station runner, standard library only.

    server   `ThreadingHTTPServer` + `ssl` with `CERT_REQUIRED`: a client without a
             certificate signed by the platform CA is refused at the handshake
    client   `urllib` with the executor's client certificate and the CA pinned

POST /batch carries a `SignedBatch` and returns a `BatchResult`; GET /health answers with
the station name. Errors come back as three-part JSON. `RunnerClient` is the protocol the
executor talks to; `InProcessRunnerClient` skips the network for tests and the fake station.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from slas_schemas.errors import ThreePartMessage
from slas_station_runner.protocol import BatchError, BatchResult, SignedBatch, three_part
from slas_station_runner.runner import StationRunner


class RunnerClient(Protocol):
    def send(self, signed: SignedBatch) -> BatchResult: ...


class InProcessRunnerClient:
    def __init__(self, runner: StationRunner) -> None:
        self.runner = runner
        self.sent: list[str] = []

    def send(self, signed: SignedBatch) -> BatchResult:
        self.sent.append(signed.batch.batch_id)
        return self.runner.handle(signed)


def server_context(*, certfile: str, keyfile: str, cafile: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=cafile)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def client_context(*, certfile: str, keyfile: str, cafile: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


class _Handler(BaseHTTPRequestHandler):
    runner: StationRunner  # set on the server class per instance
    server_version = "slas-station-runner"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return  # the runner journal is the log; nothing about a batch goes to stderr

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"station": self.runner.config.station, "ok": True})
            return
        self._json(
            404,
            ThreePartMessage(
                "Not found.", f"{self.path} is not a runner endpoint.", "Use /batch or /health."
            ).as_dict(),
        )

    def do_POST(self) -> None:
        if self.path != "/batch":
            self._json(
                404,
                ThreePartMessage(
                    "Not found.", f"{self.path} is not a runner endpoint.", "POST to /batch."
                ).as_dict(),
            )
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16 * 1024 * 1024:
            self._json(
                413,
                ThreePartMessage(
                    "The batch is empty or too large.",
                    "A batch is a few kilobytes of JSON.",
                    "Send one compiled skill or one command per batch.",
                ).as_dict(),
            )
            return
        raw = self.rfile.read(length)
        try:
            signed = SignedBatch.model_validate_json(raw)
        except ValueError as exc:
            self._json(
                400,
                ThreePartMessage(
                    "The batch could not be read.", str(exc)[:300], "Send a SignedBatch as JSON."
                ).as_dict(),
            )
            return
        try:
            result = self.runner.handle(signed)
        except BatchError as exc:
            self._json(403, exc.message.as_dict())
            return
        self._json(200, result.model_dump(mode="json"))


class RunnerServer:
    """Serves one runner over mTLS in a background thread; `port` is known after `start()`."""

    def __init__(
        self, runner: StationRunner, *, bind: str, certfile: str, keyfile: str, cafile: str
    ) -> None:
        host, _, port = bind.rpartition(":")
        handler = type("Handler", (_Handler,), {"runner": runner})
        self._server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), handler)  # noqa: S104
        self._server.socket = server_context(
            certfile=certfile, keyfile=keyfile, cafile=cafile
        ).wrap_socket(self._server.socket, server_side=True)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class MtlsRunnerClient:
    def __init__(
        self, url: str, *, certfile: str, keyfile: str, cafile: str, timeout_s: float = 60.0
    ) -> None:
        self.url = url.rstrip("/")
        self.context = client_context(certfile=certfile, keyfile=keyfile, cafile=cafile)
        self.timeout_s = timeout_s
        self.sent: list[str] = []

    def send(self, signed: SignedBatch) -> BatchResult:
        self.sent.append(signed.batch.batch_id)
        body = signed.model_dump_json().encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 — https only, CA pinned in the context
            f"{self.url}/batch",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout_s, context=self.context
            ) as response:
                return BatchResult.model_validate_json(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except ValueError:
                payload = {}
            raise BatchError(three_part(payload if isinstance(payload, dict) else {})) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise BatchError(
                ThreePartMessage(
                    f"The station runner at {self.url} did not answer.",
                    f"{reason}",
                    "Check that the runner is up on the station and that both certificates are "
                    "signed by the platform CA.",
                )
            ) from None
