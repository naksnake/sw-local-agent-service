"""The fetch records and the manager that runs one fetch per background thread (ADR-0018).

A `FetchRecord` is the wire shape of `GET /v1/fetches/{id}`: state, bytes and files done and
total, one sentence a person can read, and the three parts when it failed. The manager
validates the link and the id up front (refusals are 400/409 before a thread starts), then
in the thread: plan (the hub's listing and the disk check) → download (progress after every
chunk) → import (SHA256SUMS, manifest.json, the registry entry). Cancel is honoured between
files; every finished file stays and a new fetch of the same link resumes. Nothing here
starts an instance: the model manager does that once a role is assigned.
"""

from __future__ import annotations

import io
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import Field

from slas_fetch import (
    FetchCancelledError,
    FetchError,
    Hub,
    Progress,
    check_disk,
    fetch_planned,
    human,
    plan_model,
    write_manifest,
)
from slas_http.errors import ServiceError
from slas_model_fetcher.links import (
    HubLink,
    LinkError,
    parse_link,
    quant_of,
    registry_entry,
    slug_of,
    unique_id,
    valid_id,
)
from slas_model_fetcher.registry_import import ids_in_use, import_model
from slas_model_manager.registry import RegistryError
from slas_observability.events import EventLog
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

FetchState = Literal["planning", "downloading", "importing", "done", "failed", "cancelled"]
RUNNING: Final[frozenset[str]] = frozenset({"planning", "downloading", "importing"})


class Problem(SlasModel):
    what_happened: str
    likely_cause: str
    what_to_do: str

    @classmethod
    def of(cls, message: ThreePartMessage) -> Problem:
        return cls(
            what_happened=message.what_happened,
            likely_cause=message.likely_cause,
            what_to_do=message.what_to_do,
        )


class FetchRecord(SlasModel):
    id: str
    link: str
    model_id: str
    repo: str
    revision: str
    display_name: str
    state: FetchState
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    sentence: str
    started_at: datetime
    finished_at: datetime | None = None
    by: str
    problem: Problem | None = None
    #: The registry entry as written, once the import is done (estimates included).
    entry: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self.state in RUNNING


class StartBody(SlasModel):
    link: str = Field(min_length=1)
    id: str | None = None


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


HubFactory = Callable[[], Hub]


class FetchManager:
    def __init__(
        self,
        *,
        models_dir: Path,
        hub_factory: HubFactory,
        hosts: tuple[str, ...],
        log: EventLog,
        clock: Clock | None = None,
        threads: bool = True,
        models_file: Path | None = None,
    ) -> None:
        self.models_dir = models_dir
        self.models_file = models_file if models_file is not None else models_dir / "models.yaml"
        self.hub_factory = hub_factory
        self.hosts = hosts
        self.log = log
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.threads = threads
        self._lock = threading.RLock()
        self._records: dict[str, FetchRecord] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    # --- the table ----------------------------------------------------------------------------

    def records(self) -> list[FetchRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)

    def get(self, fetch_id: str) -> FetchRecord:
        with self._lock:
            record = self._records.get(fetch_id)
        if record is None:
            raise ServiceError(
                404,
                ThreePartMessage(
                    f"There is no fetch called {fetch_id}.",
                    "It was removed, or the model fetcher restarted since it was started.",
                    "Reload the Models page; a fetch that was cut short resumes when started "
                    "again.",
                ),
            )
        return record

    def _fetching_ids(self) -> list[str]:
        return [r.model_id for r in self._records.values() if r.running]

    # --- start ----------------------------------------------------------------------------------

    def start(self, body: StartBody, *, by: str) -> FetchRecord:
        try:
            link = parse_link(body.link, hosts=self.hosts)
            quant_of(link.repo)  # refuses an unadmitted quantisation before a byte moves
        except LinkError as exc:
            raise ServiceError(400, exc.message) from exc
        with self._lock:
            try:
                taken = ids_in_use(self.models_file, self._fetching_ids())
            except RegistryError as exc:
                raise ServiceError(503, exc.message) from exc
            if body.id is not None and body.id.strip():
                wanted = body.id.strip()
                if not valid_id(wanted):
                    raise ServiceError(
                        400,
                        ThreePartMessage(
                            f"{wanted!r} is not a registry id.",
                            "An id is lowercase letters, digits, dots, dashes and underscores, "
                            "starting with a letter or digit.",
                            "Use something like qwen3.8-27b-fp8, or leave the field empty.",
                        ),
                    )
                if wanted in self._fetching_ids():
                    raise ServiceError(
                        409,
                        ThreePartMessage(
                            f"{wanted} is being fetched already.",
                            "Another fetch with this registry id is still running.",
                            "Watch that fetch on the Models page, or cancel it first.",
                        ),
                    )
                if wanted in taken:
                    raise ServiceError(
                        409,
                        ThreePartMessage(
                            f"{wanted} is already in the model registry.",
                            "A model with this id was registered earlier.",
                            "Give the new model another registry id, or remove the old entry "
                            "from Models/models.yaml first.",
                        ),
                    )
                model_id = wanted
            else:
                default = slug_of(link.repo)
                if default in self._fetching_ids():
                    raise ServiceError(
                        409,
                        ThreePartMessage(
                            f"{default} is being fetched already.",
                            "Another fetch of this model is still running.",
                            "Watch that fetch on the Models page, or cancel it first.",
                        ),
                    )
                model_id = unique_id(default, taken)
            fetch_id = f"f-{secrets.token_hex(4)}"
            record = FetchRecord(
                id=fetch_id,
                link=body.link.strip(),
                model_id=model_id,
                repo=link.repo_id,
                revision=link.revision,
                display_name=link.repo,
                state="planning",
                sentence=f"Asking the hub what {link.repo_id} contains…",
                started_at=self.clock.now(),
                by=by,
            )
            self._records[fetch_id] = record
            self._cancel[fetch_id] = threading.Event()
        self.log.info("fetch.started", fetch=fetch_id, model=model_id, repo=link.repo_id, by=by)
        if self.threads:
            thread = threading.Thread(
                target=self._run, args=(fetch_id, link), name=f"slas-fetch-{model_id}", daemon=True
            )
            with self._lock:
                self._threads[fetch_id] = thread
            thread.start()
        else:
            self._run(fetch_id, link)
        return self.get(fetch_id)

    # --- cancel or remove -----------------------------------------------------------------------

    def cancel_or_remove(self, fetch_id: str) -> dict[str, str]:
        record = self.get(fetch_id)
        if record.running:
            self._cancel[fetch_id].set()
            self._update(
                fetch_id,
                sentence=(
                    f"Cancelling the fetch of {record.display_name} after the current file; "
                    "what was fetched stays on disk."
                ),
            )
            return {"sentence": f"The fetch of {record.display_name} stops after the current file."}
        with self._lock:
            self._records.pop(fetch_id, None)
            self._cancel.pop(fetch_id, None)
            self._threads.pop(fetch_id, None)
        return {"sentence": f"Removed the record of {record.display_name}; the files stay."}

    def wait(self, fetch_id: str, timeout_s: float = 30.0) -> None:
        """Tests: block until the fetch's thread has finished."""
        thread = self._threads.get(fetch_id)
        if thread is not None:
            thread.join(timeout_s)

    # --- the thread -----------------------------------------------------------------------------

    def _update(self, fetch_id: str, **changes: Any) -> FetchRecord:
        with self._lock:
            record = self._records[fetch_id].model_copy(update=changes)
            self._records[fetch_id] = record
            return record

    def _cancelled(self, fetch_id: str) -> bool:
        event = self._cancel.get(fetch_id)
        return event is not None and event.is_set()

    def _run(self, fetch_id: str, link: HubLink) -> None:
        record = self.get(fetch_id)
        log = io.StringIO()
        try:
            hub = self.hub_factory()
            source = link.source(record.model_id)
            plan = plan_model(hub, source, self.models_dir, log=log)
            check_disk([plan], self.models_dir, log=log)
            self._update(
                fetch_id,
                state="downloading",
                bytes_total=plan.bytes_total,
                files_total=len(plan.files),
                sentence=self._downloading_sentence(
                    record.display_name, 0, plan.bytes_total, 0, len(plan.files)
                ),
            )
            if self._cancelled(fetch_id):
                raise FetchCancelledError(record.model_id)

            def on_progress(progress: Progress) -> None:
                self._update(
                    fetch_id,
                    bytes_done=progress.bytes_done,
                    bytes_total=progress.bytes_total,
                    files_done=progress.files_done,
                    files_total=progress.files_total,
                    sentence=self._downloading_sentence(
                        record.display_name,
                        progress.bytes_done,
                        progress.bytes_total,
                        progress.files_done,
                        progress.files_total,
                    ),
                )

            fetched = fetch_planned(
                hub,
                plan,
                self.models_dir,
                log=log,
                progress=on_progress,
                cancel=lambda: self._cancelled(fetch_id),
            )
            self._update(
                fetch_id,
                state="importing",
                bytes_done=plan.bytes_total,
                files_done=len(plan.files),
                sentence=(
                    f"Verified {record.display_name} ({human(fetched.bytes)}, "
                    f"{len(fetched.files)} files); registering it as {record.model_id}…"
                ),
            )
            write_manifest(self.models_dir, [fetched], now=self.clock.now())
            entry = registry_entry(
                link,
                model_id=record.model_id,
                total_bytes=fetched.bytes,
                model_dir=self.models_dir / record.model_id,
            )
            import_model(self.models_file, entry)
            self._update(
                fetch_id,
                state="done",
                finished_at=self.clock.now(),
                entry=entry,
                sentence=(
                    f"{record.display_name} is here ({human(fetched.bytes)}, "
                    f"{len(fetched.files)} files) and registered as {record.model_id}; give it "
                    f"a role on this page to start it. Its GPU memory is estimated at "
                    f"{entry['vram_gib']:g} GiB from the file sizes; correct it in the registry "
                    "if you know better."
                ),
            )
            self.log.info("fetch.done", fetch=fetch_id, model=record.model_id, bytes=fetched.bytes)
        except FetchCancelledError:
            current = self.get(fetch_id)
            self._update(
                fetch_id,
                state="cancelled",
                finished_at=self.clock.now(),
                sentence=(
                    f"The fetch of {record.display_name} was cancelled after "
                    f"{human(current.bytes_done)} of {human(current.bytes_total)}; the files "
                    "stay, and fetching the same link again resumes."
                ),
            )
            self.log.info("fetch.cancelled", fetch=fetch_id, model=record.model_id)
        except FetchError as exc:
            self._fail(
                fetch_id, ThreePartMessage(exc.what_happened, exc.likely_cause, exc.what_to_do)
            )
        except RegistryError as exc:
            self._fail(fetch_id, exc.message)
        except Exception as exc:  # a thread must not die silently
            self.log.error("fetch.unexpected_error", fetch=fetch_id, error_type=type(exc).__name__)
            self._fail(
                fetch_id,
                ThreePartMessage(
                    f"Fetching {record.display_name} hit a problem it did not expect.",
                    f"A bug, or the hub answered something new ({type(exc).__name__}).",
                    "Try again; if it repeats, run `slas logs model-fetcher` on the host and "
                    "quote the trace id.",
                ),
            )

    def _fail(self, fetch_id: str, message: ThreePartMessage) -> None:
        self.log.warning("fetch.failed", fetch=fetch_id, what=message.what_happened)
        self._update(
            fetch_id,
            state="failed",
            finished_at=self.clock.now(),
            problem=Problem.of(message),
            sentence=f"{message.what_happened} {message.what_to_do}",
        )

    @staticmethod
    def _downloading_sentence(
        display: str, done: int, total: int, files_done: int, files_total: int
    ) -> str:
        return (
            f"Downloading {display}: {human(done)} of {human(total)}, "
            f"{files_done} of {files_total} files."
        )
