"""The reconcile applier: registry → desired instances → placement → containers → gateway.

One `Controller` per service. Every tick (and on `POST /v1/reconcile`) it loads
`Models/models.yaml`, plans against what the runtime has, places what must start, starts
and stops containers, probes health, and publishes the instance table to the gateway
(`PUT /v1/instances`). It keeps a state and a sentence per instance for the Models page.
Nothing here raises past `reconcile()`: a runtime, gateway or registry problem is recorded
as a sentence, logged as an event, and retried next tick (the service stays up).

Swaps run in a thread (a model takes minutes to load); the swap's route lives in the
controller's router and wins over the registry until the registry is edited or the manager
restarts, when reconciliation restores the registry's assignment.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import Field

from slas_container import ContainerError, EngineInfo
from slas_http.client import ServiceClient
from slas_http.errors import ServiceError
from slas_llm_gateway.routing import ROLES, RoleRouter, Routes
from slas_model_manager import placement
from slas_model_manager.driver import ManagedRuntime, instance_url
from slas_model_manager.fit import GpuFacts, fit, needed_gib
from slas_model_manager.reconcile import Action, desired_instances, plan_reconcile
from slas_model_manager.reconcile import sentence as actions_sentence
from slas_model_manager.registry import (
    ModelEntry,
    Registry,
    RegistryError,
    instance_name,
    read_registry_file,
    registry_from_mapping,
    write_registry_file,
)
from slas_model_manager.runtime import (
    MIN_CONTEXT,
    ContainerRef,
    ContainerRuntime,
    context_of,
    kv_cache_estimate,
    kv_cache_too_small,
    task_for_role,
    vllm_spec,
)
from slas_model_manager.swap import Clock, SmokeTester, SwapError, SwapManager, SwapRecord
from slas_observability.events import EventLog
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

InstanceStatus = Literal["starting", "healthy", "unhealthy", "stopped", "failed"]
DEFAULT_MODELS_FILE: Final = "/data/Models/models.yaml"
DEFAULT_RECONCILE_INTERVAL_S: Final = 30.0
MODEL_MANAGE: Final = "model:manage"
#: The role whose instance the Coding Agent needs first (CLAUDE.md §1.4, ADR-0017).
CODER_ROLE: Final = "coder"
#: A running container the restart policy has started this many times after failed exits
#: is a crash loop, reported `failed` with its log tail, not "loading".
CRASH_LOOP_RESTARTS: Final = 3
#: A vLLM instance that is still loading ignores SIGTERM until the runtime kills it, and it
#: has nothing to lose; pausing one waits this long, not the 30 s a serving instance gets.
PAUSE_STOP_TIMEOUT_S: Final = 2
#: How far back the manager reads a restarted container's log for the reason it exited,
#: and what an error line looks like there (Python tracebacks, vLLM errors, the OOM killer).
CRASH_EVIDENCE_LINES: Final = 400
#: How many lines of evidence a row carries at most, and how far apart two error lines may
#: be and still belong to the same failed attempt.
CRASH_EVIDENCE_KEEP: Final = 40
CRASH_CLUSTER_GAP: Final = 30
ERROR_LINE: Final = re.compile(
    r"Traceback|Error|ERROR|CRITICAL|Killed|out of memory|OutOfMemory|No available memory|"
    r"not enough memory|Not enough memory"
)
#: An exception's own message line ("ValueError: …"), as opposed to a traceback frame.
EXCEPTION_LINE: Final = re.compile(r"\b\w*(Error|Exception|Killed)\b: ")
#: What a log line carries before its message: vLLM's process prefix and the logger's
#: level, time and location. Stripped for classification only, never from the evidence.
LOG_PREFIX: Final = re.compile(
    r"^(\(\S+ pid=\d+\) ?)?((ERROR|INFO|WARNING|DEBUG|CRITICAL) \d\d-\d\d \d\d:\d\d:\d\d"
    r" \[[^\]]*\] ?|(ERROR|INFO|WARNING|DEBUG|CRITICAL) )?"
)  # one space at most after each part: the message's own indentation must survive
FRAME_LINE: Final = re.compile(r'^\s*File ".*", line \d+')
CARET_LINE: Final = re.compile(r"^\s*[\^~]+\s*$")
TRACEBACK_HEAD: Final = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")


def _without_frames(lines: Sequence[str]) -> list[str]:
    """The log lines minus Python traceback frames: `File "…", line n`, the source line
    under it, the caret line, and the "Traceback" heading. Messages stay."""
    kept: list[str] = []
    after_frame = False
    for line in lines:
        body = LOG_PREFIX.sub("", line)
        if FRAME_LINE.match(body):
            after_frame = True
            continue
        if after_frame and body.startswith(" "):
            after_frame = False
            continue  # the source line quoted under the frame
        after_frame = False
        if CARET_LINE.match(body) or TRACEBACK_HEAD.match(body) or not body.strip():
            continue
        kept.append(line)
    return kept


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class InstanceState(SlasModel):
    name: str
    model_id: str
    display_name: str
    kind: Literal["role", "voter"]
    role: str | None = None
    gpu_ids: list[int] = Field(default_factory=list)
    state: InstanceStatus
    sentence: str
    url: str
    #: Whether a container exists for it; only those are published to the gateway.
    container: bool = True

    @property
    def healthy(self) -> bool:
        return self.state == "healthy"


class ReconcileReport(SlasModel):
    actions: list[Action] = Field(default_factory=list)
    sentence: str


class Controller:
    def __init__(
        self,
        *,
        runtime: ManagedRuntime,
        registry_path: Path,
        gateway: ServiceClient | None,
        gpu_ids: Sequence[int],
        gpu_vram_gib: float,
        image: str,
        log: EventLog,
        smoke: SmokeTester,
        swap_runtime: ContainerRuntime | None = None,
        clock: Clock | None = None,
        ping: Callable[[], EngineInfo] | None = None,
        coder_first: bool = True,
    ) -> None:
        self.runtime = runtime
        #: Start the coder's instance alone; the others wait until it answers (or fails).
        #: Seven instances reading 450 GB of weights at once kept the first host's coder
        #: loading for a quarter of an hour; the Coding Agent needs that one instance first.
        self.coder_first = coder_first
        self._starting_since: dict[str, datetime] = {}
        #: Restart-policy restarts per running container (0 for one that never exited).
        self._restarts: dict[str, int] = {}
        #: Per instance, a context below the registry's that the GPU's KV cache can hold,
        #: learnt from a crash loop and kept across manager restarts from the container's argv.
        self._context_cap: dict[str, int] = {}
        self._context_note: dict[str, str] = {}
        self.registry_path = registry_path
        self.gateway = gateway
        self.gpu_ids = list(gpu_ids)
        self.gpu_vram_gib = gpu_vram_gib
        self.image = image
        self.log = log
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._ping = ping
        self.router = RoleRouter(Routes())
        self.swaps = SwapManager(
            runtime=swap_runtime if swap_runtime is not None else runtime,
            smoke=smoke,
            router=self.router,
            clock=self.clock,
            image=image,
        )
        self._lock = threading.RLock()
        self._states: dict[str, InstanceState] = {}
        self._ever_healthy: set[str] = set()
        self._registry: Registry | None = None
        self._registry_assignment: tuple[Any, ...] | None = None
        self._problem: ThreePartMessage | None = None
        self._gateway_problem: ThreePartMessage | None = None
        self._engine = "unknown"
        self._swap_thread: threading.Thread | None = None
        self._swap_role: str | None = None
        self._loop_stop = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self.ticks = 0

    # --- registry -----------------------------------------------------------------------------

    def load_registry(self) -> Registry:
        source = str(self.registry_path)
        try:
            text = self.registry_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RegistryError(
                ThreePartMessage(
                    f"The model registry {source} could not be read.",
                    f"The file is missing or unreadable ({type(exc).__name__}).",
                    "Run `./install.sh` again, which writes the profile's registry when none "
                    "exists, or create it from services/model-manager/models.example.yaml.",
                )
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RegistryError(
                ThreePartMessage(
                    f"The model registry {source} could not be used.",
                    f"It is not valid YAML ({str(exc).splitlines()[0][:120]}).",
                    f"Fix {source} on the Models page or by hand.",
                )
            ) from exc
        return registry_from_mapping(data, source=source)

    def registry(self) -> Registry | None:
        return self._registry

    # --- roles and voters from the Models page (contract §3 PUT /v1/roles, INV-9) -------------

    def weights_present(self, entry: ModelEntry) -> bool:
        """`Models/<path>/SHA256SUMS` beside the registry file: what the fetch writes last."""
        return (self.registry_path.parent / entry.path / "SHA256SUMS").is_file()

    def set_roles(
        self, roles: Mapping[str, str | None] | None, voters: Sequence[str] | None
    ) -> dict[str, Any]:
        """Change who serves which role and who votes; only the keys given change. The file
        is rewritten atomically with every other field kept, then reconciled at once."""
        with self._lock:
            if roles is None and voters is None:
                raise ServiceError(
                    400,
                    ThreePartMessage(
                        "Nothing to change.",
                        "The request named neither a role nor the voters.",
                        "Pick a model for a role or tick the voters, then save.",
                    ),
                )
            self._refuse_if_swapping()
            try:
                data, header = read_registry_file(self.registry_path)
            except RegistryError as exc:
                raise ServiceError(503, exc.message) from exc
            registry = registry_from_mapping(data, source=str(self.registry_path))
            models = [dict(m) for m in data.get("models", []) if isinstance(m, dict)]
            by_id = {str(m.get("id")): m for m in models}
            assigned: dict[str, str] = dict(data.get("roles") or {})
            changed: list[str] = []
            for role, model_id in (roles or {}).items():
                if role not in ROLES:
                    raise ServiceError(
                        400,
                        ThreePartMessage(
                            f"{role} is not a role.",
                            f"The roles are {', '.join(ROLES)}.",
                            "Pick a role from the Models page.",
                        ),
                    )
                if model_id is None:
                    if assigned.pop(role, None) is not None:
                        changed.append(f"{role} is no longer served")
                    continue
                entry = self._present_entry(registry, model_id)
                assigned[role] = model_id
                declared = list(by_id[model_id].get("roles") or [])
                if role not in declared:
                    by_id[model_id]["roles"] = [*declared, role]
                changed.append(f"{role} → {entry.display_name}")
            if voters is not None:
                if len(set(voters)) != len(voters):
                    raise ServiceError(
                        400,
                        ThreePartMessage(
                            "A model is listed twice among the voters.",
                            "Voters are distinct models, from different families where possible.",
                            "Tick each model once.",
                        ),
                    )
                names = [self._present_entry(registry, v).display_name for v in voters]
                data["voters"] = list(voters)
                changed.append(
                    f"voters: {', '.join(names)}"
                    if names
                    else "no voters (cross-checks are flagged)"
                )
            data["roles"] = assigned
            data["models"] = models
            try:
                written = write_registry_file(self.registry_path, data, header=header)
            except RegistryError as exc:
                raise ServiceError(400, exc.message) from exc
            self.log.info("registry.roles_changed", changes=changed)
            report = self.reconcile()
            families = len(set(written.voter_families()))
            sentence = (
                f"Saved: {'; '.join(changed) or 'nothing changed'}. "
                f"{len(written.voters)} {'voter' if len(written.voters) == 1 else 'voters'} from "
                f"{families} {'family' if families == 1 else 'families'}. {report.sentence}"
            )
            return {
                "sentence": sentence,
                "roles": dict(written.roles),
                "voters": list(written.voters),
                "models": [
                    {
                        "id": m.id,
                        "display_name": m.display_name,
                        "family": m.family,
                        "quant": m.quant,
                        "roles": list(m.roles),
                        "present": self.weights_present(m),
                    }
                    for m in written.models
                ],
            }

    def _present_entry(self, registry: Registry, model_id: str) -> ModelEntry:
        try:
            entry = registry.model(model_id)
        except KeyError:
            raise ServiceError(
                400,
                ThreePartMessage(
                    f"There is no model called {model_id} in the registry.",
                    "The id is mistyped, or the entry was removed from Models/models.yaml.",
                    "Pick a model from the Models page.",
                ),
            ) from None
        if not self.weights_present(entry):
            raise ServiceError(
                400,
                ThreePartMessage(
                    f"The weights of {entry.display_name} are not here yet.",
                    f"Models/{entry.path}/SHA256SUMS does not exist, so an instance could not "
                    "start.",
                    "Add the model from this page (or run ./install.sh --models) first, then "
                    "assign it.",
                ),
            )
        return entry

    # --- the loop -----------------------------------------------------------------------------

    def start_loop(self, interval_s: float = DEFAULT_RECONCILE_INTERVAL_S) -> None:
        if self._loop_thread is not None:
            return
        self._loop_stop.clear()
        self._loop_thread = threading.Thread(
            target=self._loop, args=(interval_s,), name="slas-reconcile", daemon=True
        )
        self._loop_thread.start()

    def stop_loop(self) -> None:
        self._loop_stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)
            self._loop_thread = None

    def _loop(self, interval_s: float) -> None:  # pragma: no cover — a thread; `tick` is tested
        while not self._loop_stop.is_set():
            self.tick()
            self._loop_stop.wait(interval_s)

    def tick(self) -> ReconcileReport:
        """One loop iteration; whatever goes wrong becomes a sentence, never an exception."""
        try:
            return self.reconcile()
        except Exception as exc:  # the loop must survive anything
            self.log.error("reconcile.unexpected_error", error_type=type(exc).__name__)
            self._problem = ThreePartMessage(
                "Reconciling the model instances hit a problem it did not expect.",
                f"A bug or a runtime answer nobody foresaw ({type(exc).__name__}).",
                "It is retried every tick; if it repeats, run `slas logs model-manager` on the "
                "host and quote the trace id.",
            )
            return ReconcileReport(actions=[], sentence=self._problem.what_happened)

    # --- reconcile ----------------------------------------------------------------------------

    def reconcile(self) -> ReconcileReport:
        with self._lock:
            self.ticks += 1
            self._probe_engine()
            try:
                registry = self.load_registry()
            except RegistryError as exc:
                self._problem = exc.message
                self.log.warning("registry.unusable", reason=exc.message.likely_cause)
                return ReconcileReport(actions=[], sentence=self._sentence_of(exc.message))
            if not self.image:
                self._problem = ThreePartMessage(
                    "No vLLM image is configured, so no model instance can start.",
                    "SLAS_VLLM_IMAGE is empty in the model manager's environment.",
                    "Set SLAS_VLLM_IMAGE in .env to the pinned vLLM image from the image lock "
                    "and run `docker compose up -d model-manager`.",
                )
                self._registry = registry
                return ReconcileReport(actions=[], sentence=self._sentence_of(self._problem))
            self._registry = registry
            self._sync_router(registry)
            try:
                actions = self._apply(registry)
            except ContainerError as exc:
                self._problem = exc.message
                self.log.error("runtime.failed", what=exc.message.what_happened)
                return ReconcileReport(actions=[], sentence=self._sentence_of(exc.message))
            self._problem = None
            self._publish(registry)
            return ReconcileReport(actions=actions, sentence=actions_sentence(actions))

    def _probe_engine(self) -> None:
        if self._ping is None:
            return
        try:
            self._engine = self._ping().engine
        except ContainerError:
            self._engine = "unknown"

    def _sync_router(self, registry: Registry) -> None:
        assignment = (tuple(sorted(registry.roles.items())), tuple(registry.voters))
        if self._registry_assignment == assignment:
            return
        # The registry changed: it is the one truth again; any swap route is superseded.
        self._registry_assignment = assignment
        self.router = RoleRouter(registry.routes())
        self.swaps.router = self.router
        self.swaps.records.clear()

    def _apply(self, registry: Registry) -> list[Action]:
        known = self.runtime.running()
        refs = {ref.name: ref for ref in known}
        statuses = {name: self._container_status(ref) for name, ref in refs.items()}
        # A container someone stopped by hand is planned as absent: the driver replaces it.
        visible = [ref for ref in known if statuses[ref.name] != "stopped"]
        # A container's command and image are fixed when it is created. One left from an
        # older model manager (other vLLM flags, another image) is replaced, or the fix that
        # the new manager carries never reaches it and it keeps crashing with the old flags.
        outdated: dict[str, str] = {}
        for candidate in visible:
            reason = self._outdated_reason(registry, candidate, statuses[candidate.name])
            if reason is not None:
                outdated[candidate.name] = reason
        actions = self._respect_swaps(
            plan_reconcile(registry, visible, outdated=outdated), registry, refs
        )
        desired = desired_instances(registry)
        starting = [a for a in actions if a.kind == "start"]
        coder_name = instance_name(CODER_ROLE)
        gate = self._coder_gate_holds(starting, statuses, desired)
        deferred = {a.name for a in starting if a.name != coder_name} if gate else set()
        starting = [a for a in starting if a.name not in deferred]
        waiting_reason = f"waits until {coder_name} answers"
        # The report says what happened this tick: a deferred start is not a start.
        actions = [
            a.model_copy(update={"kind": "keep", "reason": waiting_reason})
            if a.name in deferred
            else a
            for a in actions
        ]
        # Instances that were already loading beside the coder (containers from before this
        # manager started) are paused the same way: stopped now, started when the coder
        # answers. Otherwise a manager restart on a host where all seven load at once would
        # leave the coder competing for the disk exactly as before.
        paused: set[str] = set()
        for name in sorted(statuses):
            if (
                gate
                and name != coder_name
                and name in desired
                and statuses[name] == "starting"
                and name not in self._ever_healthy
                and any(a.kind == "keep" and a.name == name for a in actions)
            ):
                actions = [a for a in actions if a.name != name]
                actions.append(
                    Action(
                        kind="stop",
                        name=name,
                        model_id=desired[name],
                        reason=f"{waiting_reason}; it was loading beside the coder",
                    )
                )
                deferred.add(name)
                paused.add(name)
                self.log.info("instance.paused", instance=name, reason=waiting_reason)
        touched = {a.name for a in actions if a.kind != "keep"}
        pinned = {
            name: (ref.spec.gpu_ids, self._needed(registry, ref.spec.model_id))
            for name, ref in refs.items()
            if name not in touched
        }
        placed = placement.place(
            placement.candidates(registry, only=[a.name for a in starting]),
            gpu_ids=self.gpu_ids,
            capacity_gib=self.gpu_vram_gib,
            pinned=pinned,
        )
        fresh: dict[str, InstanceState] = {}
        for name in deferred:
            model_id = desired[name]
            entry = registry.model(model_id)
            kind, role = self._kind_and_role(name)
            fresh[name] = InstanceState(
                name=name,
                model_id=model_id,
                display_name=entry.display_name,
                kind=kind,
                role=role,
                state="starting",
                sentence=(
                    f"{entry.display_name} waits its turn: {coder_name} loads first so the "
                    "Coding Agent's model is ready soonest; this instance starts when the coder "
                    "answers."
                ),
                url=instance_url(name),
                container=False,
            )

        for action in actions:
            if action.kind == "stop":
                ref = refs.pop(action.name) if action.name in refs else None
                statuses.pop(action.name, None)
                if ref is not None:
                    timeout_s = PAUSE_STOP_TIMEOUT_S if action.name in paused else 30
                    self.runtime.stop(ref, timeout_s=timeout_s)
                    self.log.info("instance.stopped", instance=action.name, reason=action.reason)
                self._ever_healthy.discard(action.name)
        for action in starting:
            model_id = action.model_id or desired[action.name]
            entry = registry.model(model_id)
            kind, role = self._kind_and_role(action.name)
            gpu_ids = placed.gpu_ids_for(action.name)
            if gpu_ids is None:
                fresh[action.name] = InstanceState(
                    name=action.name,
                    model_id=model_id,
                    display_name=entry.display_name,
                    kind=kind,
                    role=role,
                    state="failed",
                    sentence=placed.sentence_for(action.name) or "It does not fit on any GPU.",
                    url=instance_url(action.name),
                    container=False,
                )
                self.log.warning("instance.no_room", instance=action.name, model=model_id)
                continue
            spec = vllm_spec(
                entry,
                name=action.name,
                gpu_ids=gpu_ids,
                image=self.image,
                task=task_for_role(role),
                max_model_len=self._context_cap.get(action.name),
            )
            try:
                refs[action.name] = self.runtime.start(spec)
            except ContainerError as exc:
                fresh[action.name] = InstanceState(
                    name=action.name,
                    model_id=model_id,
                    display_name=entry.display_name,
                    kind=kind,
                    role=role,
                    gpu_ids=gpu_ids,
                    state="failed",
                    sentence=self._sentence_of(exc.message),
                    url=instance_url(action.name),
                    container=False,
                )
                # The runtime's own sentences go in the log: an operator reading
                # `slas logs model-manager` must see why, not only that it failed.
                self.log.error(
                    "instance.start_failed",
                    instance=action.name,
                    model=model_id,
                    image=self.image,
                    gpus=gpu_ids,
                    what_happened=exc.message.what_happened,
                    likely_cause=exc.message.likely_cause,
                    what_to_do=exc.message.what_to_do,
                )
                continue
            self._ever_healthy.discard(action.name)
            self.log.info("instance.started", instance=action.name, model=model_id, gpus=gpu_ids)
            statuses[action.name] = self._container_status(refs[action.name])

        for name, ref in sorted(refs.items()):
            fresh[name] = self._observe(registry, ref, statuses.get(name))
        self._states = fresh
        return actions

    def _coder_gate_holds(
        self,
        starting: list[Action],
        statuses: Mapping[str, InstanceStatus | None],
        desired: Mapping[str, str],
    ) -> bool:
        """Whether every other instance waits while the coder's loads (coder first).

        The gate holds while the coder is being started this tick or is running without
        having answered yet; a coder that crashed (or keeps crashing), does not fit or is
        not in the registry holds nobody back.
        """
        coder_name = instance_name(CODER_ROLE)
        if not self.coder_first or coder_name not in desired:
            return False
        coder_starts_now = any(a.name == coder_name for a in starting)
        coder_loading = (
            statuses.get(coder_name) == "starting" and coder_name not in self._ever_healthy
        )
        return coder_starts_now or coder_loading

    def _outdated_reason(
        self, registry: Registry, ref: ContainerRef, status: InstanceStatus | None
    ) -> str | None:
        """Why a container serving the right model must still be replaced, or None."""
        try:
            entry = registry.model(ref.spec.model_id)
        except KeyError:
            return None  # the plan stops it for the model mismatch
        name = ref.name
        _, role = self._kind_and_role(name)
        current = context_of(ref.spec.argv) or entry.context
        # A context below the registry's was learnt from an earlier crash loop (by this
        # manager or the one before it): keep it rather than replace the container with
        # the registry's value and crash the same way again.
        if current < entry.context and name not in self._context_cap:
            self._context_cap[name] = current
            self._context_note[name] = self._context_sentence(current, entry.context)
        shrunk = self._shrink_context_after_crash(entry, ref, status)
        if shrunk is not None:
            return shrunk
        expected = vllm_spec(
            entry,
            name=name,
            gpu_ids=list(ref.spec.gpu_ids),
            image=self.image,
            task=task_for_role(role),
            max_model_len=self._context_cap.get(name),
        )
        if ref.spec.image != self.image:
            return f"runs {ref.spec.image}, but the image lock names {self.image}"
        if list(ref.spec.argv) != list(expected.argv):
            return (
                "was created with other vLLM flags than the model manager now uses; a "
                "container's command is fixed at creation"
            )
        return None

    def _shrink_context_after_crash(
        self, entry: ModelEntry, ref: ContainerRef, status: InstanceStatus | None
    ) -> str | None:
        """A crash loop whose log says the context does not fit the KV cache is started
        again with half the context (never below MIN_CONTEXT); the reason to replace it,
        or None when this is not that crash or the floor is reached."""
        name = ref.name
        if status != "failed" or self._restarts.get(name, 0) < CRASH_LOOP_RESTARTS:
            return None
        evidence = self._crash_evidence(name)
        if not kv_cache_too_small(evidence):
            return None
        current = context_of(ref.spec.argv) or entry.context
        if current <= MIN_CONTEXT:
            return None
        # Half the context, and never above what vLLM itself estimated would fit.
        smaller = current // 2
        estimate = kv_cache_estimate(evidence)
        if estimate is not None:
            smaller = min(smaller, estimate)
        smaller = max(MIN_CONTEXT, smaller)
        self._context_cap[name] = smaller
        self._context_note[name] = self._context_sentence(smaller, entry.context)
        self.log.warning(
            "instance.context_reduced", instance=name, was=current, now=smaller, cap=entry.context
        )
        return (
            f"crashed because a context of {current} tokens does not fit in the GPU's KV "
            f"cache next to the weights; started again with {smaller}"
        )

    @staticmethod
    def _context_sentence(context: int, cap: int) -> str:
        return (
            f"It runs with a context of {context} tokens: the registry's {cap} did not fit "
            "in the GPU's KV cache next to the weights."
        )

    def _respect_swaps(
        self, actions: list[Action], registry: Registry, refs: Mapping[str, ContainerRef]
    ) -> list[Action]:
        """Leave a role alone while a swap is in flight or a swap candidate serves it.

        A `vllm-<role>-<model>` container that no swap accounts for (the registry was
        edited, or the manager restarted and lost the swap) is stopped: the registry is the
        one truth again and `vllm-<role>` serves the role.
        """
        protected: set[str] = set()
        serving: set[str] = set()
        busy: set[str] = set()
        for role in registry.roles:
            base = instance_name(role)
            current = self.router.instance_for(role) if role in self.router.routes.roles else base
            if current != base and current in refs:
                protected.add(base)
                serving.add(current)
            if self._swap_role == role and self.swap_in_progress():
                protected.add(base)
                busy.add(role)
        kept = [a for a in actions if a.name not in protected or a.kind == "keep"]
        for name, ref in sorted(refs.items()):
            kind, owner = self._kind_and_role(name)
            if kind != "role" or owner is None or name == instance_name(owner):
                continue
            if name in serving or owner in busy:
                continue
            kept.append(
                Action(
                    kind="stop",
                    name=name,
                    model_id=ref.spec.model_id,
                    reason="left over from a swap the registry has since superseded",
                )
            )
        return kept

    def _needed(self, registry: Registry, model_id: str) -> float:
        try:
            return needed_gib(registry.model(model_id))
        except KeyError:
            return 0.0

    def _kind_and_role(self, name: str) -> tuple[Literal["role", "voter"], str | None]:
        for role in ROLES:
            if name == instance_name(role) or name.startswith(f"{instance_name(role)}-"):
                return "role", role
        return "voter", None

    def _container_status(self, ref: ContainerRef) -> InstanceStatus | None:
        info = self.runtime.state_of(ref.name)
        if info is None:
            self._restarts.pop(ref.name, None)
            return None
        self._restarts[ref.name] = info.restart_count if info.restarted else 0
        if info.running:
            if self.runtime.is_healthy(ref):
                return "healthy"
            # `restart: unless-stopped` starts a crashed vLLM again within seconds, so a
            # crash loop reads as "running" nearly all the time. The first host showed seven
            # instances "loading" for a quarter of an hour that way. The restart count says
            # what the state cannot; a few restarts after failed exits is a crash, not a load.
            if self._restarts[ref.name] >= CRASH_LOOP_RESTARTS:
                return "failed"
            return "starting"
        if info.state == "exited" and info.exit_code == 0:
            return "stopped"
        return "failed"

    def _observe(
        self, registry: Registry, ref: ContainerRef, status: InstanceStatus | None
    ) -> InstanceState:
        name = ref.name
        entry = self._entry_for(registry, ref.spec.model_id)
        kind, role = self._kind_and_role(name)
        display = entry.display_name if entry is not None else ref.spec.model_id
        if status is None:
            state: InstanceStatus = "failed"
            sentence = f"The container {name} disappeared; it is started again next tick."
        elif status == "healthy":
            state = "healthy"
            self._ever_healthy.add(name)
            served = f"the {role} role" if role else "cross-checks as a voter"
            sentence = f"{display} is serving {served} at {instance_url(name)}."
            if name in self._context_note:
                sentence += " " + self._context_note[name]
            if role is not None and entry is not None:
                self.swaps.register_serving(role, entry, ref)
        elif status == "starting" and name in self._ever_healthy:
            state = "unhealthy"
            sentence = (
                f"{display} runs but stopped answering its health check; the gateway routes "
                "around it until it recovers."
            )
        elif status == "starting":
            state = "starting"
            sentence = self._loading_sentence(name, display)
        elif status == "stopped":
            state = "stopped"
            sentence = f"{display} was stopped outside the platform; it is started again next tick."
        else:
            state = "failed"
            restarts = self._restarts.get(name, 0)
            lines = self._crash_evidence(name) if restarts else self.runtime.last_log_lines(name)
            tail = "\n".join(lines) if lines else "the runtime kept no log lines"
            if restarts >= CRASH_LOOP_RESTARTS:
                sentence = (
                    f"{display} keeps crashing: the runtime started it {restarts} times and it "
                    f"exited each time, so it never finishes loading. What it logged before "
                    f"it last exited:\n{tail}"
                )
            else:
                sentence = f"{display} crashed. Last log lines:\n{tail}"
        if state != "starting":
            self._starting_since.pop(name, None)
        return InstanceState(
            name=name,
            model_id=ref.spec.model_id,
            display_name=display,
            kind=kind,
            role=role,
            gpu_ids=list(ref.spec.gpu_ids),
            state=state,
            sentence=sentence,
            url=instance_url(name),
        )

    def _loading_sentence(self, name: str, display: str) -> str:
        """How long the instance has been loading and what vLLM last logged, so a person
        watching the Models page (or the install) sees progress, not a frozen line."""
        since = self._starting_since.setdefault(name, self.clock.now())
        minutes = int((self.clock.now() - since).total_seconds() // 60)
        text = f"{display} is loading its weights"
        if minutes >= 1:
            text += f" for {minutes} min"
        text += "; not answering yet."
        if name in self._context_note:
            text += " " + self._context_note[name]
        restarts = self._restarts.get(name, 0)
        if restarts:
            times = "once" if restarts == 1 else f"{restarts} times"
            text += f" It exited {times} before and the runtime started it again."
            # The first exception of the failed attempt is the root cause; the API server's
            # later "Engine core initialization failed" only points back at it.
            errors = [line for line in self._crash_evidence(name) if EXCEPTION_LINE.search(line)]
            if errors:
                text += f" Before it last exited it logged: {errors[0].strip()[:200]}"
        lines = [line for line in self.runtime.last_log_lines(name, 5) if line.strip()]
        if lines:
            text += f" Last log line: {lines[-1].strip()[:200]}"
        return text

    def _crash_evidence(
        self, name: str, *, before: int = 4, after: int = 1, limit: int = CRASH_EVIDENCE_KEEP
    ) -> list[str]:
        """The messages of the last failed attempt a restarted container logged.

        The runtime keeps one log across restarts, so the tail of a crash-looping container
        is the fresh attempt's start-up lines, not the reason it died. The reason is in the
        last cluster of error lines before that: vLLM's engine process logs its own traceback
        ("EngineCore failed to start" and the exception) and the API server then logs a second
        traceback that only says "see root cause above". The cluster is taken whole, the
        traceback frames (`File "…"`, the code line under each, the carets) are dropped so the
        exception messages stand out, and a long remainder keeps its head, where the root
        cause is, and its tail. Falls back to the last lines when nothing looks like an error.
        """
        lines = self.runtime.last_log_lines(name, CRASH_EVIDENCE_LINES)
        hits = [i for i, line in enumerate(lines) if ERROR_LINE.search(line)]
        if not hits:
            return lines[-limit:]
        last = hits[-1]
        first = last
        for i in reversed(hits[:-1]):
            if first - i > CRASH_CLUSTER_GAP:
                break
            first = i
        window = _without_frames(lines[max(0, first - before) : last + after + 1])
        if len(window) <= limit:
            return window
        head, tail = limit - 10, 9
        left_out = len(window) - head - tail
        return [*window[:head], f"… {left_out} more traceback lines left out …", *window[-tail:]]

    @staticmethod
    def _entry_for(registry: Registry, model_id: str) -> ModelEntry | None:
        try:
            return registry.model(model_id)
        except KeyError:
            return None

    # --- the gateway --------------------------------------------------------------------------

    def instances_payload(self) -> dict[str, Any]:
        instances = [
            {"name": s.name, "url": s.url, "model_id": s.model_id, "healthy": s.healthy}
            for s in sorted(self._states.values(), key=lambda s: s.name)
            if s.container
        ]
        return {"instances": instances, "routes": self.router.routes.model_dump()}

    def _publish(self, registry: Registry) -> None:
        if self.gateway is None:
            return
        try:
            self.gateway.put("/v1/instances", self.instances_payload())
        except ServiceError as exc:
            self._gateway_problem = exc.message
            self.log.warning("gateway.publish_failed", what=exc.message.what_happened)
            return
        self._gateway_problem = None

    # --- status -------------------------------------------------------------------------------

    def states(self) -> list[InstanceState]:
        with self._lock:
            return sorted(self._states.values(), key=lambda s: (s.kind != "role", s.name))

    def status(self) -> dict[str, Any]:
        with self._lock:
            states = self.states()
            gpus: dict[int, list[str]] = {gpu: [] for gpu in self.gpu_ids}
            for state in states:
                for gpu in state.gpu_ids:
                    gpus.setdefault(gpu, []).append(state.name)
            return {
                "sentence": self._status_sentence(states),
                "engine": self._engine,
                "gpus": [{"id": gpu, "instances": names} for gpu, names in sorted(gpus.items())],
                "instances": [self._instance_row(state) for state in states],
            }

    def _instance_row(self, state: InstanceState) -> dict[str, Any]:
        row = state.model_dump(exclude={"container"})
        if state.role is not None and self._swap_role == state.role and self.swap_in_progress():
            row["sentence"] = self.swaps.status(state.role)
        return row

    def _status_sentence(self, states: list[InstanceState]) -> str:
        if self._problem is not None:
            return self._sentence_of(self._problem)
        if self._registry is None:
            return "The model manager has not reconciled yet."
        if not states:
            return "The registry names no instance; nothing runs."
        healthy = sum(1 for s in states if s.state == "healthy")
        parts = [f"{healthy} of {len(states)} model instances are healthy"]
        labelled = (("starting", "loading"), ("unhealthy", "not answering"), ("failed", "failed"))
        for label, verb in labelled:
            names = [s.name for s in states if s.state == label]
            if names:
                parts.append(f"{', '.join(names)} {verb}")
        text = "; ".join(parts) + "."
        if self._gateway_problem is not None:
            text += f" {self._gateway_problem.what_happened} {self._gateway_problem.what_to_do}"
        return text

    @staticmethod
    def _sentence_of(message: ThreePartMessage) -> str:
        return f"{message.what_happened} {message.what_to_do}"

    # --- fit ----------------------------------------------------------------------------------

    def fit(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            registry = self._registry
            if registry is None:
                registry = self._load_or_raise()
            try:
                entry = registry.model(model_id)
            except KeyError:
                raise ServiceError(
                    404,
                    ThreePartMessage(
                        f"There is no model called {model_id} in the registry.",
                        "The id is mistyped, or the entry was removed from Models/models.yaml.",
                        "Pick a model from the Models page.",
                    ),
                ) from None
            used: dict[int, float] = dict.fromkeys(self.gpu_ids, 0.0)
            for state in self._states.values():
                if state.state in ("failed", "stopped") or not state.gpu_ids:
                    continue
                share = self._needed(registry, state.model_id) / len(state.gpu_ids)
                for gpu in state.gpu_ids:
                    used[gpu] = used.get(gpu, 0.0) + share
            gpus = [
                GpuFacts(
                    index=gpu,
                    name=f"{self.gpu_vram_gib:g} GiB configured",
                    total_gib=self.gpu_vram_gib,
                    free_gib=max(self.gpu_vram_gib - taken, 0.0),
                )
                for gpu, taken in sorted(used.items())
            ]
            result = fit(entry, gpus)
            return {"fits": result.fits, "sentence": result.sentence}

    def _load_or_raise(self) -> Registry:
        try:
            registry = self.load_registry()
        except RegistryError as exc:
            raise ServiceError(503, exc.message) from exc
        self._registry = registry
        return registry

    # --- swaps --------------------------------------------------------------------------------

    def swap_in_progress(self) -> bool:
        thread = self._swap_thread
        return thread is not None and thread.is_alive()

    def begin_swap(self, role: str, candidate_id: str) -> SwapRecord:
        with self._lock:
            registry = self._registry if self._registry is not None else self._load_or_raise()
            if role not in ROLES:
                raise ServiceError(
                    400,
                    ThreePartMessage(
                        f"{role} is not a role.",
                        f"The roles are {', '.join(ROLES)}.",
                        "Pick a role from the Models page.",
                    ),
                )
            try:
                entry = registry.model(candidate_id)
            except KeyError:
                raise ServiceError(
                    404,
                    ThreePartMessage(
                        f"There is no model called {candidate_id} in the registry.",
                        "The id is mistyped, or the entry was removed from Models/models.yaml.",
                        "Pick a model from the Models page.",
                    ),
                ) from None
            if role not in entry.roles:
                raise ServiceError(
                    409,
                    ThreePartMessage(
                        f"{entry.display_name} cannot serve {role}.",
                        "Models/models.yaml declares it for "
                        f"{', '.join(entry.roles) or 'no role'}.",
                        "Pick a model declared for this role, or add the role to its entry.",
                    ),
                )
            self._refuse_if_swapping()
            candidate_name = f"{instance_name(role)}-{entry.id}"
            pinned = {
                s.name: (s.gpu_ids, self._needed(registry, s.model_id))
                for s in self._states.values()
                if s.gpu_ids and s.state not in ("failed", "stopped")
            }
            placed = placement.place(
                [
                    placement.Candidate(
                        instance=candidate_name,
                        model_id=entry.id,
                        kind="role",
                        role=role,
                        needed_gib=needed_gib(entry),
                        label=entry.label(),
                    )
                ],
                gpu_ids=self.gpu_ids,
                capacity_gib=self.gpu_vram_gib,
                pinned=pinned,
            )
            gpu_ids = placed.gpu_ids_for(candidate_name)
            if gpu_ids is None:
                raise ServiceError(
                    409,
                    ThreePartMessage(
                        f"{entry.display_name} cannot start alongside the current {role}.",
                        placed.sentence_for(candidate_name) or "No GPU has room for it.",
                        "Stop an instance you do not need, or pick a smaller build.",
                    ),
                )
            current = self.swaps.serving(role)
            incumbent = current[0].display_name if current else "nothing"
            record = SwapRecord(
                role=role,
                candidate=entry,
                incumbent=current[0] if current else None,
                incumbent_ref=current[1] if current else None,
                phase="starting",
                started_at=self.clock.now(),
                progress=[
                    f"Starting {entry.display_name} alongside the current {role} ({incumbent})…"
                ],
            )
            self._swap_role = role
            self._swap_thread = threading.Thread(
                target=self._run_swap, args=(role, entry, gpu_ids), name="slas-swap", daemon=True
            )
            self._swap_thread.start()
            return record

    def _refuse_if_swapping(self) -> None:
        if self.swap_in_progress():
            raise ServiceError(
                409,
                ThreePartMessage(
                    f"A swap of {self._swap_role} is in progress.",
                    "Only one swap runs at a time; a model takes minutes to load.",
                    "Watch the Models page; try again when it reads done or failed.",
                ),
            )

    def _run_swap(self, role: str, entry: ModelEntry, gpu_ids: list[int]) -> None:
        try:
            record = self.swaps.swap(role, entry, gpu_ids=gpu_ids)
            self.log.info("swap.finished", role=role, candidate=entry.id, phase=record.phase)
        except (SwapError, ContainerError) as exc:
            self.log.error(
                "swap.failed", role=role, candidate=entry.id, what=exc.message.what_happened
            )
            self._note_swap_failure(role, entry, exc.message)
        except Exception as exc:  # a thread must not die silently
            self.log.error("swap.unexpected_error", role=role, error_type=type(exc).__name__)
        self.tick()

    def _note_swap_failure(self, role: str, entry: ModelEntry, message: ThreePartMessage) -> None:
        record = self.swaps.records.get(role)
        if record is None:
            record = SwapRecord(
                role=role, candidate=entry, phase="failed", started_at=self.clock.now()
            )
            self.swaps.records[role] = record
        record.phase = "failed"
        record.progress.append(self._sentence_of(message))

    def begin_rollback(self, role: str) -> SwapRecord:
        with self._lock:
            self._refuse_if_swapping()
            record = self.swaps.records.get(role)
            problem = self._rollback_problem(role, record)
            if problem is not None:
                raise ServiceError(409, problem)
            assert record is not None and record.incumbent is not None  # noqa: S101 — checked above
            record.progress.append(f"Rolling {role} back to {record.incumbent.display_name}…")
            # A snapshot: the thread mutates the live record from here on.
            snapshot = record.model_copy(deep=True)
            self._swap_role = role
            self._swap_thread = threading.Thread(
                target=self._run_rollback, args=(role,), name="slas-rollback", daemon=True
            )
            self._swap_thread.start()
            return snapshot

    def _rollback_problem(self, role: str, record: SwapRecord | None) -> ThreePartMessage | None:
        """The refusals `SwapManager.rollback` would raise, checked before the thread starts."""
        if record is None or record.phase != "done":
            return ThreePartMessage(
                f"There is no completed swap of {role} to roll back.",
                "Either nothing was swapped, or the swap failed and the previous model kept "
                "serving.",
                "Check the Models page for what is serving the role now.",
            )
        if record.incumbent is None or record.incumbent_ref is None:
            return ThreePartMessage(
                f"{role} had no previous model, so there is nothing to roll back to.",
                f"{record.candidate.display_name} was the first model assigned to it.",
                "Swap in another model instead.",
            )
        if record.rollback_until is not None and self.clock.now() > record.rollback_until:
            return ThreePartMessage(
                f"The rollback window for {role} closed at "
                f"{record.rollback_until.strftime('%Y-%m-%d %H:%M')}.",
                "Rollbacks are kept for 24 hours after a swap.",
                f"Swap {record.incumbent.display_name} back in as a new swap instead.",
            )
        return None

    def _run_rollback(self, role: str) -> None:
        try:
            record = self.swaps.rollback(role)
            self.log.info("rollback.finished", role=role, phase=record.phase)
        except (SwapError, ContainerError) as exc:
            self.log.error("rollback.failed", role=role, what=exc.message.what_happened)
            failed = self.swaps.records.get(role)
            if failed is not None:
                failed.progress.append(self._sentence_of(exc.message))
        except Exception as exc:  # a thread must not die silently
            self.log.error("rollback.unexpected_error", role=role, error_type=type(exc).__name__)
        self.tick()

    def swap_record(self, role: str) -> SwapRecord | None:
        return self.swaps.records.get(role)
