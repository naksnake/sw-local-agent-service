"""What the factory service wires around `FactoryExecutor` (docs/api-contract-round-2.md §6).

RunnerTable        station name → `MtlsRunnerClient`, read from the registry on every
                   lookup, so a station enrolled after start is reachable (INV-9)
StationKeys        station name → (batch key reference, key id) from enrolment
VncWatcher         one `VncTunnel` per station for the operator's watch-and-take-over
MesPoller          the thread that moves MES tickets from inbox to processing
templates          the shipped template copied to Factory/Templates, every template read
GatewayCrossChecker  the verdict's voters through the LLM gateway; degrades to "the line
                   lead decides" when the gateway does not answer (INV-11)
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Final, Protocol

import yaml

from slas_factory_executor.mes import FileDropMesAdapter
from slas_factory_executor.stations import (
    EnrolmentService,
    StationError,
    StationRecord,
    StationRegistry,
)
from slas_factory_executor.templates import (
    TEMPLATES,
    TemplateError,
    TestLoopTemplate,
    load_templates,
    render_template_yaml,
    template_from_mapping,
)
from slas_http.client import ServiceClient
from slas_http.errors import ServiceError
from slas_observability.events import EventLog
from slas_schemas.job import MesTicket
from slas_schemas.vote import ConsensusVerdict
from slas_station_runner.server import MtlsRunnerClient, RunnerClient, VncTunnel

TEMPLATE_SUFFIXES: Final = (".yaml", ".yml", ".template.json")


# --- stations -------------------------------------------------------------------------------------


class RunnerTable(Mapping[str, RunnerClient]):
    """The executor's runner table as a live view of the registry: an enrolled station has a
    client to its `runner_url` with the executor's certificate; anything else is a KeyError,
    which the executor turns into its "no station runner is configured" sentence."""

    def __init__(
        self, registry: StationRegistry, *, certfile: Path, keyfile: Path, cafile: Path
    ) -> None:
        self.registry = registry
        self.certfile = certfile
        self.keyfile = keyfile
        self.cafile = cafile
        self._clients: dict[str, MtlsRunnerClient] = {}
        self._lock = threading.Lock()

    def _record(self, name: str) -> StationRecord:
        try:
            record = self.registry.get(name)
        except StationError:
            raise KeyError(name) from None
        if not record.enrolled or record.runner_url is None:
            raise KeyError(name)
        return record

    def __getitem__(self, name: str) -> RunnerClient:
        record = self._record(name)
        url = str(record.runner_url).rstrip("/")
        with self._lock:
            client = self._clients.get(name)
            if client is None or client.url != url:
                client = MtlsRunnerClient(
                    url,
                    certfile=str(self.certfile),
                    keyfile=str(self.keyfile),
                    cafile=str(self.cafile),
                )
                self._clients[name] = client
            return client

    def __iter__(self) -> Iterator[str]:
        return iter([r.name for r in self.registry.list() if r.enrolled])

    def __len__(self) -> int:
        return sum(1 for r in self.registry.list() if r.enrolled)


class StationKeys(MutableMapping[str, tuple[str, str]]):
    """Per-station (key_ref, key_id): what enrolment recorded, plus anything set by hand."""

    def __init__(self, registry: StationRegistry, enrolment: EnrolmentService) -> None:
        self.registry = registry
        self.enrolment = enrolment
        self._overlay: dict[str, tuple[str, str]] = {}

    def __getitem__(self, name: str) -> tuple[str, str]:
        if name in self._overlay:
            return self._overlay[name]
        try:
            record = self.registry.get(name)
        except StationError:
            raise KeyError(name) from None
        if record.batch_key_id is None:
            raise KeyError(name)
        return self.enrolment.batch_key_ref(name), record.batch_key_id

    def __setitem__(self, name: str, value: tuple[str, str]) -> None:
        self._overlay[name] = value

    def __delitem__(self, name: str) -> None:
        del self._overlay[name]

    def __iter__(self) -> Iterator[str]:
        names = {r.name for r in self.registry.list() if r.batch_key_id is not None}
        return iter(sorted(names | set(self._overlay)))

    def __len__(self) -> int:
        return len(list(iter(self)))


class Watcher(Protocol):
    def watch(self, record: StationRecord | None, station: str) -> tuple[str | None, str | None]:
        """(watch_url, watch_problem): exactly one of the two is set."""
        ...


class NoWatcher:
    """Tests and a service without an executor certificate: always a sentence, never a port."""

    def watch(self, record: StationRecord | None, station: str) -> tuple[str | None, str | None]:
        problem = watch_problem(record, station)
        return None, problem or (
            f"The executor has no certificate to relay {station}'s screen; enrolment is "
            "not set up on this service."
        )


def watch_problem(record: StationRecord | None, station: str) -> str | None:
    if record is None:
        return (
            f"{station} is not registered under Admin → Stations, so there is no screen to watch."
        )
    if not record.vnc.enabled:
        return f"VNC is not enabled on {station}. Enable it under Admin → Stations and re-enrol."
    if not record.enrolled or record.runner_url is None:
        return f"{station} is not enrolled yet, so there is no runner to relay its screen."
    return None


class VncWatcher:
    """One relay per station: the operator's noVNC attaches to a local port, every connection
    becomes an mTLS `/vnc` stream to the runner (ADR-0010)."""

    def __init__(
        self, *, certfile: Path, keyfile: Path, cafile: Path, listen: str = "127.0.0.1:0"
    ) -> None:
        self.certfile = certfile
        self.keyfile = keyfile
        self.cafile = cafile
        self.listen = listen
        self.tunnels: dict[str, VncTunnel] = {}
        self._lock = threading.Lock()

    def watch(self, record: StationRecord | None, station: str) -> tuple[str | None, str | None]:
        problem = watch_problem(record, station)
        if problem is not None or record is None or record.runner_url is None:
            return None, problem
        url = record.runner_url.rstrip("/")
        with self._lock:
            tunnel = self.tunnels.get(station)
            if tunnel is not None and tunnel.url != url:
                tunnel.stop()
                tunnel = None
            if tunnel is None:
                tunnel = VncTunnel(
                    url,
                    certfile=str(self.certfile),
                    keyfile=str(self.keyfile),
                    cafile=str(self.cafile),
                    listen=self.listen,
                )
                tunnel.start()
                self.tunnels[station] = tunnel
        return f"vnc://127.0.0.1:{tunnel.port}", None

    def sentence_for(self, station: str) -> str | None:
        tunnel = self.tunnels.get(station)
        return tunnel.sentence() if tunnel is not None else None

    def stop(self) -> None:
        with self._lock:
            for tunnel in self.tunnels.values():
                tunnel.stop()
            self.tunnels.clear()


# --- MES ------------------------------------------------------------------------------------------


def pending_tickets(adapter: FileDropMesAdapter) -> list[MesTicket]:
    """The production tickets picked up and not yet reported, as the wizard lists them."""
    tickets: list[MesTicket] = []
    for name in adapter.pending():
        path = adapter.root / "processing" / f"{name}.json"
        try:
            tickets.append(MesTicket.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # moved or rewritten while we looked; the next poll lists it
    return tickets


class MesPoller:
    """Moves inbox tickets to processing every `interval_s`; `/health` reads `state()`."""

    def __init__(self, adapter: FileDropMesAdapter, *, interval_s: float, log: EventLog) -> None:
        self.adapter = adapter
        self.interval_s = interval_s
        self.log = log
        self.polls = 0
        self.received: list[str] = []
        self._state = "ok"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> str:
        return self._state

    def poll_once(self) -> list[MesTicket]:
        self.polls += 1
        try:
            tickets = self.adapter.poll()
        except OSError as exc:
            self._state = "down"
            self.log.warning("mes.poll_failed", error=str(exc)[:200])
            return []
        self._state = "ok"
        for ticket in tickets:
            self.received.append(ticket.ticket_no)
            self.log.info(
                "mes.ticket_received",
                ticket_no=ticket.ticket_no,
                station=ticket.station,
                unit_sn=ticket.unit_sn,
            )
        return tickets

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="slas-mes-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


# --- templates ------------------------------------------------------------------------------------


def ensure_shipped_templates(templates_dir: Path) -> list[Path]:
    """Copy the shipped templates into `Factory/Templates` when the directory holds none, so
    a line edits a file it can see. Returns the files written (empty when nothing changed)."""
    templates_dir.mkdir(parents=True, exist_ok=True)
    if any(p.is_file() and p.name.endswith(TEMPLATE_SUFFIXES) for p in templates_dir.iterdir()):
        return []
    written: list[Path] = []
    for name, data in TEMPLATES.items():
        path = templates_dir / f"{name}.yaml"
        path.write_text(render_template_yaml(data), encoding="utf-8")
        written.append(path)
    return written


def templates_in(templates_dir: Path, log: EventLog | None = None) -> list[TestLoopTemplate]:
    """The shipped templates, every `*.template.json` and every `*.yaml` under the directory;
    a file that does not validate is logged and skipped, never a broken page."""
    templates = load_templates(templates_dir)
    if templates_dir.is_dir():
        for path in sorted(templates_dir.glob("*.y*ml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                template = template_from_mapping(data, source=str(path))
            except (TemplateError, yaml.YAMLError, OSError) as exc:
                if log is not None:
                    log.warning("templates.skipped", path=str(path), error=str(exc)[:200])
                continue
            templates[template.id] = template
    return sorted(templates.values(), key=lambda t: t.id)


# --- the voters -----------------------------------------------------------------------------------


class GatewayCrossChecker:
    """`CrossChecker` over the gateway's `POST /v1/cross-check` (§2 of the contract). The
    evidence lines become one user message, as the orchestrator's checker does. A gateway
    that does not answer yields a degraded verdict with no votes, so the deterministic gate
    holds the unit for the line lead instead of failing the step (INV-11)."""

    def __init__(self, client: ServiceClient, *, log: EventLog | None = None) -> None:
        self.client = client
        self.log = log

    def cross_check(self, decision: str, evidence: list[str]) -> ConsensusVerdict:
        content = "Evidence:\n" + "\n".join(f"- {line}" for line in evidence)
        try:
            payload = self.client.post(
                "/v1/cross-check",
                {"decision": decision, "evidence": [{"role": "user", "content": content}]},
            )
            return ConsensusVerdict.model_validate(payload)
        except (ServiceError, ValueError) as exc:
            reason = exc.message.what_happened if isinstance(exc, ServiceError) else str(exc)[:200]
            if self.log is not None:
                self.log.warning("cross_check.unavailable", decision=decision, reason=reason)
            return ConsensusVerdict(
                decision=decision,
                rule="unanimous",
                votes=[],
                agreed=False,
                degraded=True,
                sentence=f"Not cross-checked: {reason} The line lead decides.",
                concerns=[],
            )
