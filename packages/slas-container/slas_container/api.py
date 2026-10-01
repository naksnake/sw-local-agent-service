"""`ContainerApi`: the Docker Engine / Podman compat API over the runtime socket.

Unversioned paths (`/containers/json`) so the same client works against Docker and Podman;
`ping()` reads which one answers. `exec` attaches without a TTY and demultiplexes the
8-byte-framed stream into stdout and stderr, then reads the exit code. Every failure is a
three-part `ContainerError`; a missing container on `stop`/`remove` is not a failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

import httpx
from pydantic import Field

from slas_container.spec import CreateSpec, Engine
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

DEFAULT_SOCKET: Final = "/run/podman/podman.sock"
_STDOUT: Final = 1
_STDERR: Final = 2


class ContainerError(RuntimeError):
    def __init__(self, message: ThreePartMessage, *, status: int | None = None) -> None:
        super().__init__(message.what_happened)
        self.message = message
        self.status = status


class EngineInfo(SlasModel):
    engine: Engine
    api_version: str = ""

    def sentence(self) -> str:
        name = "Docker" if self.engine == "docker" else "Podman"
        return f"The runtime socket is served by {name} (API {self.api_version or 'unknown'})."


class ContainerInfo(SlasModel):
    id: str
    name: str
    image: str = ""
    state: str = Field(default="", description="created, running, exited, …")
    health: str | None = Field(default=None, description="healthy, unhealthy, starting")
    labels: dict[str, str] = Field(default_factory=dict)
    ip_addresses: dict[str, str] = Field(default_factory=dict, description="network → IP")
    #: The last exit code; for a running container the code of its previous exit (0 if none).
    exit_code: int | None = None
    #: How many times the restart policy started the container again after it exited
    #: (`RestartCount` from inspect; the list route does not carry it, so 0 there).
    restart_count: int = 0

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def restarted(self) -> bool:
        """Running now, but only because the restart policy started it again after it exited.

        The exit code says nothing here: Docker resets `State.ExitCode` to 0 the moment the
        container runs again, so a crash loop reads as running with exit 0 and a growing
        `RestartCount`. The count is the evidence.
        """
        return self.running and self.restart_count > 0


class ExecResult(SlasModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def demultiplex(payload: bytes) -> tuple[bytes, bytes]:
    """Split a non-TTY attach stream into (stdout, stderr); a raw stream is all stdout."""
    out, err = bytearray(), bytearray()
    offset = 0
    while offset + 8 <= len(payload):
        kind = payload[offset]
        if kind not in (0, _STDOUT, _STDERR) or payload[offset + 1 : offset + 4] != b"\x00\x00\x00":
            # Not framed at all (a TTY stream or a plain body): hand it back whole.
            return bytes(payload), b""
        size = int.from_bytes(payload[offset + 4 : offset + 8], "big")
        chunk = payload[offset + 8 : offset + 8 + size]
        (err if kind == _STDERR else out).extend(chunk)
        offset += 8 + size
    if offset < len(payload) and not out and not err:
        return bytes(payload), b""
    return bytes(out), bytes(err)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return response.text.strip()[:300]
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return str(body["message"])[:300]
    return response.text.strip()[:300]


class ContainerApi:
    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        *,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self._client = httpx.Client(
            base_url="http://slas-runtime",
            transport=transport if transport is not None else httpx.HTTPTransport(uds=socket_path),
            timeout=timeout_s,
        )
        self._engine: EngineInfo | None = None

    def close(self) -> None:
        self._client.close()

    # --- plumbing ----------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
        timeout_s: float | None = None,
        ok: Sequence[int] = (200, 201, 204),
        tolerate: Sequence[int] = (),
        doing: str = "talk to the container runtime",
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                path,
                params=dict(params) if params else None,
                json=body,
                timeout=timeout_s if timeout_s is not None else self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime took too long to {doing}.",
                    "The runtime is busy (pulling, starting a large container) or hung.",
                    "Wait a moment and try again; check `systemctl status docker` or "
                    "`podman.socket` on the host.",
                )
            ) from exc
        except httpx.HTTPError as exc:
            raise ContainerError(
                ThreePartMessage(
                    f"The container runtime socket {self.socket_path} did not answer.",
                    "The socket is not mounted, the daemon is stopped, or the service user may "
                    "not read it.",
                    "On the host run `slas doctor`; set SLAS_RUNTIME_SOCKET in .env to the "
                    "socket the runtime serves (Docker: /var/run/docker.sock) and run "
                    "`docker compose up -d` again.",
                )
            ) from exc
        if response.status_code in ok or response.status_code in tolerate:
            return response
        raise ContainerError(
            ThreePartMessage(
                f"The container runtime refused to {doing}.",
                f"It answered {response.status_code}: {_detail(response) or 'no message'}.",
                "Read the message above; `slas logs model-manager` or `slas logs "
                "sandbox-manager` on the host has the full exchange.",
            ),
            status=response.status_code,
        )

    # --- engine --------------------------------------------------------------------------

    def ping(self) -> EngineInfo:
        response = self._call("GET", "/_ping", doing="answer a ping")
        engine: Engine = "podman" if "libpod-api-version" in response.headers else "docker"
        version = response.headers.get("api-version", "") or response.headers.get(
            "libpod-api-version", ""
        )
        self._engine = EngineInfo(engine=engine, api_version=version)
        return self._engine

    def runtimes(self) -> list[str]:
        """The OCI runtimes the engine knows (`runc`, `runsc`, `nvidia`…), from `GET /info`."""
        response = self._call("GET", "/info", doing="describe the runtime")
        payload = response.json()
        known = payload.get("Runtimes") if isinstance(payload, dict) else None
        if isinstance(known, dict):
            return sorted(str(name) for name in known)
        # Podman's compat /info lists its OCI runtime by name only.
        default = payload.get("DefaultRuntime") if isinstance(payload, dict) else None
        return [str(default)] if default else []

    @property
    def engine(self) -> Engine:
        info = self._engine if self._engine is not None else self.ping()
        return info.engine

    # --- containers ----------------------------------------------------------------------

    @staticmethod
    def _info_from_list(item: Mapping[str, Any]) -> ContainerInfo:
        names = item.get("Names") or []
        name = str(names[0]).lstrip("/") if names else str(item.get("Id", ""))[:12]
        networks = ((item.get("NetworkSettings") or {}).get("Networks")) or {}
        return ContainerInfo(
            id=str(item.get("Id", "")),
            name=name,
            image=str(item.get("Image", "")),
            state=str(item.get("State", "")).lower(),
            labels={str(k): str(v) for k, v in (item.get("Labels") or {}).items()},
            ip_addresses={
                str(net): str(cfg.get("IPAddress", ""))
                for net, cfg in networks.items()
                if isinstance(cfg, Mapping) and cfg.get("IPAddress")
            },
        )

    @staticmethod
    def _info_from_inspect(item: Mapping[str, Any]) -> ContainerInfo:
        state = item.get("State") or {}
        health = state.get("Health") or {}
        networks = ((item.get("NetworkSettings") or {}).get("Networks")) or {}
        config = item.get("Config") or {}
        return ContainerInfo(
            id=str(item.get("Id", "")),
            name=str(item.get("Name", "")).lstrip("/"),
            image=str(config.get("Image", "") or item.get("Image", "")),
            state=str(state.get("Status", "")).lower(),
            health=str(health["Status"]).lower() if health.get("Status") else None,
            labels={str(k): str(v) for k, v in (config.get("Labels") or {}).items()},
            ip_addresses={
                str(net): str(cfg.get("IPAddress", ""))
                for net, cfg in networks.items()
                if isinstance(cfg, Mapping) and cfg.get("IPAddress")
            },
            exit_code=int(state["ExitCode"]) if state.get("ExitCode") is not None else None,
            restart_count=int(item.get("RestartCount") or 0),
        )

    def list(self, *, label: str | None = None, all_states: bool = True) -> list[ContainerInfo]:
        filters: dict[str, list[str]] = {}
        if label is not None:
            filters["label"] = [label]
        params = {"all": "true" if all_states else "false"}
        if filters:
            params["filters"] = json.dumps(filters)
        response = self._call("GET", "/containers/json", params=params, doing="list containers")
        items = response.json()
        return [self._info_from_list(item) for item in items] if isinstance(items, list) else []

    def inspect(self, name: str) -> ContainerInfo | None:
        response = self._call(
            "GET", f"/containers/{name}/json", tolerate=(404,), doing=f"inspect {name}"
        )
        if response.status_code == 404:
            return None
        return self._info_from_inspect(response.json())

    def create(self, spec: CreateSpec) -> str:
        response = self._call(
            "POST",
            "/containers/create",
            params={"name": spec.name},
            body=spec.to_body(self.engine),
            doing=f"create {spec.name} from {spec.image}",
        )
        payload = response.json()
        return str(payload.get("Id", "")) if isinstance(payload, dict) else ""

    def start(self, name: str) -> None:
        self._call(
            "POST", f"/containers/{name}/start", ok=(204,), tolerate=(304,), doing=f"start {name}"
        )

    def stop(self, name: str, *, timeout_s: int = 30) -> None:
        self._call(
            "POST",
            f"/containers/{name}/stop",
            params={"t": str(timeout_s)},
            ok=(204,),
            tolerate=(304, 404),
            timeout_s=float(timeout_s) + self.timeout_s,
            doing=f"stop {name}",
        )

    def remove(self, name: str, *, force: bool = True) -> None:
        self._call(
            "DELETE",
            f"/containers/{name}",
            params={"force": "true" if force else "false", "v": "true"},
            ok=(204,),
            tolerate=(404,),
            doing=f"remove {name}",
        )

    def logs(self, name: str, *, tail: int = 200) -> str:
        response = self._call(
            "GET",
            f"/containers/{name}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
            doing=f"read the logs of {name}",
        )
        out, err = demultiplex(response.content)
        return (out + err).decode("utf-8", errors="replace")

    def image_present(self, reference: str) -> bool:
        response = self._call(
            "GET", f"/images/{reference}/json", tolerate=(404,), doing=f"look up {reference}"
        )
        return response.status_code != 404

    # --- exec ----------------------------------------------------------------------------

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
        """Run argv inside a running container; never a shell line (CLAUDE.md §11)."""
        if not argv:
            raise ValueError("argv must not be empty")
        body: dict[str, Any] = {
            "AttachStdin": False,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "Cmd": list(argv),
        }
        if cwd is not None:
            body["WorkingDir"] = cwd
        if user is not None:
            body["User"] = user
        if env:
            body["Env"] = [f"{key}={value}" for key, value in env.items()]
        created = self._call(
            "POST", f"/containers/{name}/exec", body=body, doing=f"prepare a command in {name}"
        )
        exec_id = str(created.json().get("Id", ""))
        try:
            started = self._call(
                "POST",
                f"/exec/{exec_id}/start",
                body={"Detach": False, "Tty": False},
                timeout_s=timeout_s,
                doing=f"run {argv[0]} in {name}",
            )
        except ContainerError as exc:
            if "took too long" in exc.message.what_happened:
                return ExecResult(
                    exit_code=124,
                    stderr=f"{argv[0]} did not finish within {timeout_s:g} s.",
                    timed_out=True,
                )
            raise
        out, err = demultiplex(started.content)
        inspected = self._call(
            "GET", f"/exec/{exec_id}/json", doing=f"read the exit code in {name}"
        )
        payload = inspected.json()
        code = payload.get("ExitCode") if isinstance(payload, dict) else None
        return ExecResult(
            exit_code=int(code) if code is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )

    # --- convenience -----------------------------------------------------------------------

    def running_with_label(self, label: str) -> Iterator[ContainerInfo]:
        for info in self.list(label=label, all_states=False):
            if info.running:
                yield info
