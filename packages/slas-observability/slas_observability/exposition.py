"""`GET /metrics` and `GET /health` for every service, standard library only.

Each service starts one `MetricsServer` on its backend address; Prometheus scrapes it
(`observability/prometheus/prometheus.yml`). Nothing else is served here and no request
body is read, so this endpoint carries no data a scraper could inject.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slas_observability.metrics import REGISTRY, Registry

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _Handler(BaseHTTPRequestHandler):
    registry: Registry
    service: str
    server_version = "slas-metrics"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self._send(200, self.registry.render().encode("utf-8"), CONTENT_TYPE)
            return
        if self.path == "/health":
            body = json.dumps({"service": self.service, "ok": True}).encode("utf-8")
            self._send(200, body, "application/json")
            return
        self._send(404, b"Not found. This port serves /metrics and /health only.\n", "text/plain")


class MetricsServer:
    def __init__(
        self, service: str, *, bind: str = "0.0.0.0:8000", registry: Registry = REGISTRY
    ) -> None:
        host, _, port = bind.rpartition(":")
        handler = type("MetricsHandler", (_Handler,), {"registry": registry, "service": service})
        self._server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), handler)  # noqa: S104
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
