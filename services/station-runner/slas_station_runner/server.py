"""mTLS between the factory executor and a station runner, standard library only.

    server   `ThreadingHTTPServer` + `ssl` with `CERT_REQUIRED`: a client without a
             certificate signed by the platform CA is refused at the handshake
    client   `urllib` with the executor's client certificate and the CA pinned

POST /batch carries a `SignedBatch` and returns a `BatchResult`; GET /health answers with
the station name. Errors come back as three-part JSON. `RunnerClient` is the protocol the
executor talks to; `InProcessRunnerClient` skips the network for tests and the fake station.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
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


def relay(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until either side closes (the VNC relay, P10)."""

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    forward = threading.Thread(target=pump, args=(a, b), daemon=True)
    backward = threading.Thread(target=pump, args=(b, a), daemon=True)
    forward.start()
    backward.start()
    forward.join()
    backward.join()


class _Handler(BaseHTTPRequestHandler):
    runner: StationRunner  # set on the server class per instance
    vnc_port: int | None = None
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
        if self.path == "/vnc":
            self._vnc()
            return
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

    def _vnc(self) -> None:
        """Relay the station's local VNC server to the (mTLS-authenticated) operator side."""
        if self.vnc_port is None:
            self._json(
                404,
                ThreePartMessage(
                    "VNC is not enabled on this station.",
                    "The station record has vnc.enabled false.",
                    "Enable it under Admin → Stations and re-enrol.",
                ).as_dict(),
            )
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", self.vnc_port), timeout=5)
        except OSError as exc:
            self._json(
                502,
                ThreePartMessage(
                    "The station's VNC server is not answering.",
                    str(exc),
                    "Check that the VNC server runs on the station (x11vnc or TightVNC) on "
                    "the configured port.",
                ).as_dict(),
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        upstream.settimeout(None)
        relay(self.connection, upstream)


class RunnerServer:
    """Serves one runner over mTLS in a background thread; `port` is known after `start()`."""

    def __init__(
        self,
        runner: StationRunner,
        *,
        bind: str,
        certfile: str,
        keyfile: str,
        cafile: str,
        vnc_port: int | None = None,
    ) -> None:
        host, _, port = bind.rpartition(":")
        handler = type("Handler", (_Handler,), {"runner": runner, "vnc_port": vnc_port})
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


class VncTunnel:
    """The operator side of the relay: a local port that noVNC (the screen worker) attaches
    to; every connection becomes one mTLS-authenticated `/vnc` stream to the runner."""

    def __init__(
        self,
        url: str,
        *,
        certfile: str,
        keyfile: str,
        cafile: str,
        listen: str = "127.0.0.1:0",
    ) -> None:
        self.url = url.rstrip("/")
        self.context = client_context(certfile=certfile, keyfile=keyfile, cafile=cafile)
        host, _, port = listen.rpartition(":")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host or "127.0.0.1", int(port)))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connections = 0

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    def sentence(self) -> str:
        return (
            f"Watch the station at vnc://127.0.0.1:{self.port} (relayed over mTLS to {self.url})."
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.connections += 1
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        parts = urllib.parse.urlsplit(self.url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 443
        try:
            raw = socket.create_connection((host, port), timeout=10)
            upstream = self.context.wrap_socket(raw, server_hostname=host)
            upstream.sendall(
                f"POST /vnc HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                chunk = upstream.recv(1)
                if not chunk:
                    break
                head += chunk
            if not re.match(rb"HTTP/1\.[01] 200 ", head):
                client.close()
                upstream.close()
                return
            upstream.settimeout(None)
            client.settimeout(None)
            relay(client, upstream)
        except OSError:
            client.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._listener.close()
