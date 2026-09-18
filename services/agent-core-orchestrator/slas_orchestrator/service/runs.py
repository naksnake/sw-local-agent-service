"""Kernel runs as threads inside the orchestrator (ADR-0015 §5, contract §5).

`RunRegistry.start()` runs one `Kernel.run()` (or `resume()`) in a daemon thread and
returns as soon as the ticket exists, so the route can answer with its id and the page can
poll. The kernel allocates the id itself when it saves the new ticket; `TrackingStore` wraps
the ticket store, serialises writers, and tells the registry which job became which ticket.

A run that raises leaves the ticket where the kernel left it, with an open intent in the
journal. The registry then moves the ticket to Failed with one plain sentence, journalled,
so the page shows why instead of a run that never ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from slas_kernel.clock import Clock, SystemClock
from slas_kernel.journal import Journal
from slas_kernel.store import TicketStore
from slas_observability import tracing
from slas_observability.events import EventLog
from slas_schemas.common import AgentName
from slas_schemas.errors import ThreePartMessage
from slas_schemas.ticket import Ticket, TicketState

#: How long a route waits for the kernel to create the ticket before answering 503.
DEFAULT_WAIT_S: Final = 15.0
MAX_SENTENCE_CHARS: Final = 500


def failure_sentence(exc: BaseException) -> str:
    """What happened and why, for a run that raised, from the three-part message when there is
    one. The cause rides along because for a refusal from the container runtime it is the only
    part that names the problem ("It answered 400: no such runtime runsc")."""
    message = getattr(exc, "message", None)
    if isinstance(message, ThreePartMessage):
        cause = message.likely_cause.strip()
        text = message.what_happened.strip()
        if cause and cause not in text:
            text = f"{text} {cause}"
        return text[:MAX_SENTENCE_CHARS]
    text = str(exc).strip()
    if text:
        return f"The run stopped: {text}"[:MAX_SENTENCE_CHARS]
    return f"The run stopped with {type(exc).__name__}."


@dataclass
class RunState:
    job_id: str
    ticket_id: str | None = None
    running: bool = True
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    thread: threading.Thread | None = field(default=None, repr=False)
    _ticket_known: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def finished(self) -> bool:
        return not self.running


class RunStartError(RuntimeError):
    """The run could not be started; `status` is the HTTP status the route answers with."""

    def __init__(self, message: ThreePartMessage, *, status: int) -> None:
        super().__init__(message.what_happened)
        self.message = message
        self.status = status


class TrackingStore:
    """The kernel's ticket store: one writer at a time, and every new ticket announced."""

    def __init__(self, inner: TicketStore, on_save: Callable[[Ticket], None]) -> None:
        self.inner = inner
        self._on_save = on_save
        self._lock = threading.RLock()

    def next_ticket_id(self, agent: AgentName) -> str:
        with self._lock:
            return self.inner.next_ticket_id(agent)

    def save(self, ticket: Ticket) -> None:
        with self._lock:
            self.inner.save(ticket)
        self._on_save(ticket)

    def load(self, ticket_id: str) -> Ticket:
        with self._lock:
            return self.inner.load(ticket_id)

    def list_ids(self) -> list[str]:
        with self._lock:
            return self.inner.list_ids()


class RunRegistry:
    def __init__(
        self,
        *,
        store: TicketStore,
        data_root: Path,
        clock: Clock | None = None,
        log: EventLog | None = None,
    ) -> None:
        self.data_root = data_root
        self.clock = clock or SystemClock()
        self.log = log
        self.store = TrackingStore(store, self._saved)
        self._lock = threading.Lock()
        self._by_job: dict[str, RunState] = {}
        self._by_ticket: dict[str, RunState] = {}

    # --- what the routes ask ----------------------------------------------------------

    def state_of(self, ticket_id: str) -> RunState | None:
        with self._lock:
            return self._by_ticket.get(ticket_id)

    def running_ids(self) -> list[str]:
        with self._lock:
            return [t for t, run in self._by_ticket.items() if run.running]

    def forget(self, ticket_id: str) -> bool:
        """Drop the finished run of a ticket that was removed; False while it is still running."""
        with self._lock:
            state = self._by_ticket.get(ticket_id)
            if state is None:
                return True
            if state.running:
                return False
            del self._by_ticket[ticket_id]
            self._by_job.pop(state.job_id, None)
            return True

    def start(
        self,
        job_id: str,
        run: Callable[[], Ticket],
        *,
        ticket_id: str | None = None,
        wait_s: float = DEFAULT_WAIT_S,
    ) -> RunState:
        """Run `run()` in a daemon thread; return once the ticket exists (or the run failed).

        `ticket_id` is given for a resume, where the ticket already exists.
        """
        state = RunState(job_id=job_id, ticket_id=ticket_id, started_at=self.clock.now())
        with self._lock:
            self._by_job[job_id] = state
            if ticket_id is not None:
                self._by_ticket[ticket_id] = state
                state._ticket_known.set()
        trace_id = tracing.current_trace_id()
        thread = threading.Thread(
            target=self._worker,
            args=(state, run, trace_id),
            name=f"slas-run-{job_id}",
            daemon=True,
        )
        state.thread = thread
        thread.start()
        if not state._ticket_known.wait(wait_s):
            raise RunStartError(
                ThreePartMessage(
                    "The task was accepted but its ticket did not appear in time.",
                    "The kernel is still ingesting the plan, or the ticket store is slow.",
                    "Open the Coding page in a moment; the task shows up in the list when "
                    "its ticket exists.",
                ),
                status=503,
            )
        if state.ticket_id is None:
            raise RunStartError(
                ThreePartMessage(
                    state.error or "The task could not be started.",
                    "The kernel stopped before it created a ticket.",
                    "Fix the plan or the breakdown and start the task again.",
                ),
                status=400,
            )
        return state

    # --- internals ---------------------------------------------------------------------

    def _saved(self, ticket: Ticket) -> None:
        with self._lock:
            state = self._by_job.get(ticket.job.id)
            if state is None or state.ticket_id is not None:
                return
            state.ticket_id = ticket.id
            self._by_ticket[ticket.id] = state
        state._ticket_known.set()

    def _worker(self, state: RunState, run: Callable[[], Ticket], trace_id: str | None) -> None:
        with tracing.trace(trace_id):
            try:
                ticket = run()
                if state.ticket_id is None:
                    self._saved(ticket)
            except Exception as exc:
                sentence = failure_sentence(exc)
                state.error = sentence
                if self.log is not None:
                    self.log.error(
                        "run.failed",
                        job_id=state.job_id,
                        ticket_id=state.ticket_id,
                        error_type=type(exc).__name__,
                        sentence=sentence,
                    )
                if state.ticket_id is not None:
                    self._fail_ticket(state.ticket_id, sentence)
            finally:
                state.finished_at = self.clock.now()
                state.running = False
                state._ticket_known.set()

    def _fail_ticket(self, ticket_id: str, sentence: str) -> None:
        """Close an interrupted ticket as Failed, with the sentence in the journal."""
        try:
            ticket = self.store.load(ticket_id)
        except KeyError:
            return
        if ticket.finished:
            return
        journal = Journal(self.data_root / "Tickets" / ticket_id / "journal.jsonl", self.clock)
        try:
            change = ticket.transition(TicketState.FAILED, self.clock.now(), sentence)
        except ValueError:  # pragma: no cover — every non-terminal state may fail
            return
        journal.append(
            "state",
            ticket.id,
            {"from": change.from_state.value, "to": change.to_state.value, "reason": sentence},
        )
        self.store.save(ticket)
