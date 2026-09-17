"""HTTP clients for the services the orchestrator calls (docs/api-contract-round-2.md §4).

`HttpSandboxManager` gives the Coding executor what the in-process `SandboxManager` gave it
(`SandboxAccess`): open, exec and close_session go to the sandbox-manager service; the project,
artifact and identity paths are computed here, because both containers mount the same
`${SLAS_DATA_ROOT}` at `/data`. The ticket a sandbox is opened for travels in the session
request; the executor never learns about HTTP, so the kernel-side `TicketBoundExecutor`
binds the current ticket around every step.
"""

from __future__ import annotations

import contextvars
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import httpx

from slas_http.client import DEFAULT_TIMEOUT_S, ServiceClient
from slas_kernel.executor import ExecutionContext, Executor
from slas_sandbox_manager.manager import Session
from slas_sandbox_manager.runtime import ExecResult
from slas_sandbox_manager.spec import WORKSPACE
from slas_schemas.plan import Step
from slas_schemas.ticket import Observation

#: Opening a sandbox may pull the image's layers from the local store and start gVisor.
OPEN_TIMEOUT_S: Final = 180.0
#: Room for the sandbox manager to stop a command at its own timeout and answer.
EXEC_TIMEOUT_MARGIN_S: Final = 30.0

#: The ticket the current step runs for; read when a sandbox is opened (contract §4).
current_ticket_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "slas_current_ticket_id", default=None
)


def version_from_image(image: str) -> str | None:
    """`registry.internal/slas/sandbox-python:3.12.6` → `3.12.6`; a digest has no version."""
    _, _, tag = image.rpartition("/")
    if ":" not in tag or "@sha256:" in image:
        return None
    return tag.rsplit(":", 1)[1] or None


class HttpSandboxManager(ServiceClient):
    """`SandboxAccess` over the sandbox-manager's routes."""

    def __init__(
        self,
        base_url: str,
        *,
        data_root: Path,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__("sandbox-manager", base_url, timeout_s=timeout_s, transport=transport)
        self.data_root = data_root

    # --- paths (local: the same /data as the sandbox manager) ------------------------

    def coding_root(self, user: str) -> Path:
        return self.data_root / "Coding" / user

    def project_dir(self, user: str, slug: str) -> Path:
        return self.coding_root(user) / "Projects" / slug

    def gitconfig_path(self, user: str) -> Path:
        return self.coding_root(user) / "gitconfig"

    def artifacts_dir(self, user: str, run_id: str) -> Path:
        return self.coding_root(user) / "Artifacts" / run_id

    def prepare_project(self, user: str, slug: str, *, display_name: str) -> Path:
        """The project directory, created if missing. The identity file is the sandbox
        manager's to write; it does so when the session opens."""
        project = self.project_dir(user, slug)
        project.mkdir(parents=True, exist_ok=True)
        return project

    # --- sessions ----------------------------------------------------------------------

    @staticmethod
    def _session(payload: Any) -> Session:
        data = dict(payload)
        data.pop("sentence", None)  # the wire adds a sentence and, on GET, `alive`
        data.pop("alive", None)
        return Session.model_validate(data)

    def open(
        self,
        user: str,
        slug: str,
        *,
        image: str,
        language: str,
        display_name: str,
        ttl_s: int | None = None,
    ) -> Session:
        body: dict[str, Any] = {
            "user": user,
            "slug": slug,
            "display_name": display_name,
            "languages": [{"language": language, "version": version_from_image(image)}],
        }
        ticket_id = current_ticket_id.get()
        if ticket_id is not None:
            body["ticket_id"] = ticket_id
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        return self._session(self.post("/v1/sessions", body, timeout_s=OPEN_TIMEOUT_S))

    def session(self, session_id: str) -> Session:
        """`GET /v1/sessions/{id}`; `get()` stays the HTTP verb of `ServiceClient`."""
        return self._session(self.get(f"/v1/sessions/{session_id}"))

    def exec(
        self,
        session_id: str,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        if not argv or any(not isinstance(part, str) for part in argv):
            raise ValueError("argv must be a non-empty list of strings")
        body: dict[str, Any] = {"argv": list(argv), "cwd": cwd, "timeout_s": timeout_s}
        if stdin is not None:
            # Commit messages travel on stdin (`git commit --file=-`), never as argv (§11).
            body["stdin"] = stdin
        payload = self.post(
            f"/v1/sessions/{session_id}/exec", body, timeout_s=timeout_s + EXEC_TIMEOUT_MARGIN_S
        )
        return ExecResult.model_validate(payload)

    def close_session(self, session_id: str) -> None:
        """`DELETE /v1/sessions/{id}`; `close()` stays the client's own shutdown."""
        self.delete(f"/v1/sessions/{session_id}")


class TicketBoundExecutor:
    """Binds the ticket of each step so `HttpSandboxManager.open` can name it (contract §4)."""

    def __init__(self, inner: Executor) -> None:
        self.inner = inner

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        token = current_ticket_id.set(context.ticket_id)
        try:
            return self.inner.execute(step, context)
        finally:
            current_ticket_id.reset(token)
