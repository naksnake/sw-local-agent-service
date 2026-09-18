"""`FakeContainerApi`: the `ContainerApi` surface in memory, for tests (CLAUDE.md §11)."""

from __future__ import annotations

from builtins import list as builtin_list
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from slas_container.api import ContainerError, ContainerInfo, EngineInfo, ExecResult
from slas_container.spec import CreateSpec, Engine
from slas_schemas.errors import ThreePartMessage

ExecHandler = Callable[[str, Sequence[str]], ExecResult]


class FakeContainerApi:
    def __init__(
        self,
        engine: Engine = "docker",
        *,
        images: Sequence[str] = (),
        runtimes: Sequence[str] = ("runc", "runsc", "nvidia"),
    ) -> None:
        self._engine_kind: Engine = engine
        self._runtimes = list(runtimes)
        self.specs: dict[str, CreateSpec] = {}
        self.bodies: dict[str, dict[str, Any]] = {}
        self.states: dict[str, str] = {}
        self.healths: dict[str, str | None] = {}
        self.images: set[str] = set(images)
        self.execs: list[tuple[str, list[str]]] = []
        self.logs_text: dict[str, str] = {}
        self.restart_counts: dict[str, int] = {}
        self.exit_codes: dict[str, int] = {}
        self._handler: ExecHandler | None = None
        self.down = False

    # --- scripting -----------------------------------------------------------------------

    def handle_exec(self, handler: ExecHandler) -> None:
        self._handler = handler

    def set_health(self, name: str, health: str | None) -> None:
        self.healths[name] = health

    def crash(self, name: str, exit_code: int = 1) -> None:
        self.states[name] = "exited"
        self.healths[name] = None
        self.exit_codes[name] = exit_code
        self.logs_text.setdefault(name, f"exited with {exit_code}")

    def restarting(self, name: str, *, times: int, exit_code: int = 1) -> None:
        """The container exited `times` times with `exit_code` and the restart policy
        started it again each time; it is running (again) now."""
        self.states[name] = "running"
        self.healths[name] = None
        self.restart_counts[name] = times
        self.exit_codes[name] = exit_code
        self.logs_text.setdefault(name, f"exited with {exit_code}")

    def _require_up(self) -> None:
        if self.down:
            raise ContainerError(
                ThreePartMessage(
                    "The container runtime socket did not answer.",
                    "The daemon is stopped.",
                    "Start it.",
                )
            )

    # --- surface -------------------------------------------------------------------------

    def close(self) -> None:
        return None

    def ping(self) -> EngineInfo:
        self._require_up()
        return EngineInfo(engine=self._engine_kind, api_version="1.47")

    @property
    def engine(self) -> Engine:
        return self._engine_kind

    def runtimes(self) -> builtin_list[str]:
        self._require_up()
        return sorted(self._runtimes)

    def _info(self, name: str) -> ContainerInfo:
        spec = self.specs[name]
        return ContainerInfo(
            id=f"id-{name}",
            name=name,
            image=spec.image,
            state=self.states[name],
            health=self.healths.get(name),
            labels=dict(spec.labels),
            ip_addresses={spec.network: "10.0.0.2"} if spec.network != "none" else {},
            exit_code=self.exit_codes.get(name),
            restart_count=self.restart_counts.get(name, 0),
        )

    def list(self, *, label: str | None = None, all_states: bool = True) -> list[ContainerInfo]:
        self._require_up()
        out = []
        for name, spec in self.specs.items():
            if label is not None:
                key, _, value = label.partition("=")
                if key not in spec.labels or (value and spec.labels[key] != value):
                    continue
            if not all_states and self.states[name] != "running":
                continue
            out.append(self._info(name))
        return out

    def inspect(self, name: str) -> ContainerInfo | None:
        self._require_up()
        return self._info(name) if name in self.specs else None

    def create(self, spec: CreateSpec) -> str:
        self._require_up()
        if spec.name in self.specs:
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime refused to create {spec.name}.",
                    "It answered 409: a container with that name exists.",
                    "Remove it first.",
                ),
                status=409,
            )
        if self.images and spec.image not in self.images:
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime refused to create {spec.name} from {spec.image}.",
                    "It answered 404: no such image.",
                    "Load or build the image first.",
                ),
                status=404,
            )
        self.specs[spec.name] = spec
        self.bodies[spec.name] = spec.to_body(self._engine_kind)
        self.states[spec.name] = "created"
        self.healths[spec.name] = None
        return f"id-{spec.name}"

    def start(self, name: str) -> None:
        self._require_up()
        if name not in self.specs:
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime refused to start {name}.",
                    "It answered 404: no such container.",
                    "Create it first.",
                ),
                status=404,
            )
        self.states[name] = "running"

    def stop(self, name: str, *, timeout_s: int = 30) -> None:
        self._require_up()
        if name in self.specs:
            self.states[name] = "exited"

    def remove(self, name: str, *, force: bool = True) -> None:
        self._require_up()
        self.specs.pop(name, None)
        self.bodies.pop(name, None)
        self.states.pop(name, None)
        self.healths.pop(name, None)
        self.exit_codes.pop(name, None)
        self.restart_counts.pop(name, None)

    def logs(self, name: str, *, tail: int = 200) -> str:
        self._require_up()
        return self.logs_text.get(name, "")

    def image_present(self, reference: str) -> bool:
        self._require_up()
        return not self.images or reference in self.images

    def exec(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 600.0,
    ) -> ExecResult:
        self._require_up()
        if not argv:
            raise ValueError("argv must not be empty")
        if self.states.get(name) != "running":
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime refused to run {argv[0]} in {name}.",
                    "It answered 409: the container is not running.",
                    "Start it first.",
                ),
                status=409,
            )
        self.execs.append((name, list(argv)))
        if self._handler is not None:
            return self._handler(name, argv)
        return ExecResult(exit_code=0, stdout="")

    def running_with_label(self, label: str) -> builtin_list[ContainerInfo]:
        return [info for info in self.list(label=label, all_states=False) if info.running]
