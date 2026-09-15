"""The local alert channel (CLAUDE.md §8.2 alerting, P11): where a circuit-breaker trip, a
consensus disagreement or a Prometheus rule lands so a person sees it — on this host, with
no mail server, chat service or pager that would need a route out (INV-1).

    platform code ──► LocalAlertChannel.raise_(...) ──► ${SLAS_DATA_ROOT}/Alerts/alerts.json
    Prometheus rules ──► Alertmanager ──► webhook ──► AlertWebhookServer ──► the same file

The WebUI Home page and `slas status` read the file; an alert is open until a person
acknowledges it or Alertmanager reports it resolved. The same alert firing again within the
window bumps its count instead of adding a line.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field

from slas_observability import metrics
from slas_observability.tracing import current_trace_id
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic

Severity = Literal["info", "warning", "critical"]
Source = Literal["platform", "alertmanager"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class Alert(SlasModel):
    id: str = Field(min_length=8)
    name: str = Field(min_length=1)
    severity: Severity
    sentence: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    source: Source = "platform"
    fired_at: datetime
    last_seen_at: datetime
    count: int = Field(default=1, ge=1)
    trace_id: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.acknowledged_at is None

    def line(self) -> str:
        times = f" ×{self.count}" if self.count > 1 else ""
        state = (
            "open"
            if self.open
            else f"acknowledged by {self.acknowledged_by} at {self.acknowledged_at:%H:%M}"
        )
        return (
            f"{self.fired_at:%Y-%m-%d %H:%M} · {self.severity}{times} · {self.sentence} ({state})"
        )


def fingerprint(name: str, labels: Mapping[str, str]) -> str:
    material = name + "|" + "|".join(f"{k}={labels[k]}" for k in sorted(labels))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class AlertChannel(Protocol):
    def raise_(self, name: str, severity: Severity, sentence: str, **labels: str) -> Alert: ...


class ListAlertChannel:
    """In-memory channel for tests: every raise is kept in order."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def raise_(self, name: str, severity: Severity, sentence: str, **labels: str) -> Alert:
        now = datetime.now(UTC)
        alert = Alert(
            id=fingerprint(name, labels),
            name=name,
            severity=severity,
            sentence=sentence,
            labels=dict(labels),
            fired_at=now,
            last_seen_at=now,
            trace_id=current_trace_id(),
        )
        self.alerts.append(alert)
        metrics.inc("slas_alerts_raised_total", alert=name, severity=severity)
        return alert

    def sentences(self) -> list[str]:
        return [alert.sentence for alert in self.alerts]


class LocalAlertChannel:
    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        dedupe_window: timedelta = timedelta(hours=4),
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.dedupe_window = dedupe_window
        self._lock = threading.Lock()

    # --- storage --------------------------------------------------------------------------

    def _load(self) -> list[Alert]:
        if not self.path.is_file():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        return [Alert.model_validate(item) for item in raw]

    def _save(self, alerts: list[Alert]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [alert.model_dump(mode="json") for alert in alerts]
        write_atomic(self.path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", 0o644)

    # --- raising --------------------------------------------------------------------------

    def raise_(
        self,
        name: str,
        severity: Severity,
        sentence: str,
        *,
        source: Source = "platform",
        **labels: str,
    ) -> Alert:
        now = self.clock.now()
        with self._lock:
            alerts = self._load()
            key = fingerprint(name, labels)
            for i, existing in enumerate(alerts):
                same = existing.id == key and existing.open
                recent = now - existing.last_seen_at <= self.dedupe_window
                if same and recent:
                    alerts[i] = existing.model_copy(
                        update={
                            "count": existing.count + 1,
                            "last_seen_at": now,
                            "sentence": sentence,
                        }
                    )
                    self._save(alerts)
                    return alerts[i]
            alert = Alert(
                id=key,
                name=name,
                severity=severity,
                sentence=sentence,
                labels=dict(labels),
                source=source,
                fired_at=now,
                last_seen_at=now,
                trace_id=current_trace_id(),
            )
            alerts.append(alert)
            self._save(alerts)
        metrics.inc("slas_alerts_raised_total", alert=name, severity=severity)
        return alert

    def acknowledge(self, alert_id: str, *, by: str) -> Alert:
        with self._lock:
            alerts = self._load()
            for i, alert in enumerate(alerts):
                if alert.id == alert_id and alert.open:
                    alerts[i] = alert.model_copy(
                        update={"acknowledged_by": by, "acknowledged_at": self.clock.now()}
                    )
                    self._save(alerts)
                    return alerts[i]
        raise KeyError(alert_id)

    # --- reading --------------------------------------------------------------------------

    def all(self) -> list[Alert]:
        return self._load()

    def open(self) -> list[Alert]:
        return [alert for alert in self._load() if alert.open]

    def sentence(self) -> str:
        open_alerts = self.open()
        if not open_alerts:
            return "No alert is open."
        critical = sum(1 for a in open_alerts if a.severity == "critical")
        head = f"{len(open_alerts)} {'alert is' if len(open_alerts) == 1 else 'alerts are'} open"
        if critical:
            head += f" ({critical} critical)"
        newest = max(enumerate(open_alerts), key=lambda p: (p[1].last_seen_at, p[0]))[1]
        return f"{head}; the newest: {newest.sentence}"

    # --- Alertmanager webhook -------------------------------------------------------------

    def ingest_alertmanager(self, payload: Mapping[str, Any]) -> list[Alert]:
        """Alertmanager's webhook body: `alerts[]` with labels, annotations, status."""
        handled: list[Alert] = []
        for item in payload.get("alerts", []):
            labels = {str(k): str(v) for k, v in dict(item.get("labels", {})).items()}
            name = labels.pop("alertname", "unnamed")
            severity_text = labels.pop("severity", "warning")
            severity: Severity = "warning"
            if severity_text == "info":
                severity = "info"
            elif severity_text == "critical":
                severity = "critical"
            annotations = dict(item.get("annotations", {}))
            sentence = str(
                annotations.get("summary")
                or annotations.get("description")
                or f"{name} fired in Prometheus."
            )
            if str(item.get("status", "firing")) == "resolved":
                key = fingerprint(name, labels)
                try:
                    handled.append(self.acknowledge(key, by="Prometheus (resolved)"))
                except KeyError:
                    continue
                continue
            handled.append(self.raise_(name, severity, sentence, source="alertmanager", **labels))
        return handled


class _WebhookHandler(BaseHTTPRequestHandler):
    channel: LocalAlertChannel
    server_version = "slas-alerts"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/internal/alerts":
            self._json(
                404, {"what_happened": "Not found.", "what_to_do": "POST to /internal/alerts."}
            )
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024 * 1024:
            self._json(413, {"what_happened": "The alert payload is empty or too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self._json(400, {"what_happened": "The alert payload is not JSON."})
            return
        handled = self.channel.ingest_alertmanager(payload if isinstance(payload, dict) else {})
        self._json(200, {"accepted": len(handled)})


class AlertWebhookServer:
    """Receives Alertmanager's webhook on the backend network and writes the local channel."""

    def __init__(self, channel: LocalAlertChannel, *, bind: str = "127.0.0.1:0") -> None:
        host, _, port = bind.rpartition(":")
        handler = type("WebhookHandler", (_WebhookHandler,), {"channel": channel})
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


#: Where Alertmanager posts inside the backend network (the api mounts the same handler).
ALERT_WEBHOOK_URL = "http://api:8000/internal/alerts"


def alertmanager_config() -> dict[str, Any]:
    """`observability/alertmanager/alertmanager.yml`: one receiver, the local channel."""
    return {
        "global": {"resolve_timeout": "5m"},
        "route": {
            "receiver": "slas-local",
            "group_by": ["alertname", "instance", "decision", "station"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": [
                {
                    "matchers": ['severity="critical"'],
                    "receiver": "slas-local",
                    "group_wait": "10s",
                    "repeat_interval": "1h",
                }
            ],
        },
        "receivers": [
            {
                "name": "slas-local",
                "webhook_configs": [
                    {"url": ALERT_WEBHOOK_URL, "send_resolved": True, "max_alerts": 50}
                ],
            }
        ],
    }
