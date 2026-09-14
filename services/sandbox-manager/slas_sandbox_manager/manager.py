"""Sessions: open a sandbox on a project, run commands in it, close it on TTL (§4.4, §5.7).

    Coding/<user>/Projects/<slug>/      the durable repo, bind-mounted rw at /workspace
    Coding/<user>/Container/<session>/  ephemeral scratch, reaped with the session
    Coding/<user>/gitconfig             per-user identity, mounted ro; no credential helper

Remotes and credentials are not on disk here (INV-14). The manager writes the identity file
itself, so a sandbox can never be opened with somebody else's name on its commits.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import Field

from slas_observability import metrics
from slas_sandbox_manager.runtime import ExecResult, SandboxHandle, SandboxRuntime
from slas_sandbox_manager.spec import (
    GITCONFIG_TARGET,
    SCRATCH,
    WORKSPACE,
    Mount,
    Resources,
    SandboxSpec,
    default_env,
)
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage

Profile = Literal["quickstart", "prod"]
_SLUG = re.compile(r"[^a-z0-9]+")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SandboxError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class Session(SlasModel):
    id: str = Field(min_length=1)
    user: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    handle: SandboxHandle
    opened_at: datetime
    last_used_at: datetime
    expires_at: datetime
    runtime_sentence: str

    @property
    def project_dir(self) -> str:
        return next(m.source for m in self.handle.spec.mounts if m.target == WORKSPACE)

    @property
    def scratch_dir(self) -> str:
        return next(m.source for m in self.handle.spec.mounts if m.target == SCRATCH)

    def sentence(self, now: datetime) -> str:
        left = int((self.expires_at - now).total_seconds())
        if left <= 0:
            return f"Sandbox for {self.slug} has expired and will be closed."
        minutes = max(left // 60, 1)
        return f"Sandbox for {self.slug} is open; it closes after {minutes} more idle minutes."


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("a project name needs at least one letter or digit")
    return slug[:48]


def render_gitconfig(display_name: str, email: str) -> str:
    """The identity every commit in the sandbox carries; no helper, no remote, no hook path."""
    return (
        "# Written by SW Local Agent Service for this user; edits are overwritten.\n"
        "[user]\n"
        f"\tname = {display_name}\n"
        f"\temail = {email}\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
        "[credential]\n"
        "\thelper =\n"
        "[core]\n"
        "\thooksPath = /var/empty\n"
        "[safe]\n"
        f"\tdirectory = {WORKSPACE}\n"
        "[advice]\n"
        "\tdetachedHead = false\n"
    )


PUSH_EXPLANATION: Final = "Push happens from the Git panel, which uses your saved remote."


class SandboxManager:
    def __init__(
        self,
        *,
        runtime: SandboxRuntime,
        data_root: Path,
        clock: Clock,
        runsc_available: bool,
        profile: Profile = "quickstart",
        max_sessions_per_user: int = 3,
        default_ttl_s: int = 3600,
        resources: Resources | None = None,
    ) -> None:
        self.runtime = runtime
        self.data_root = data_root
        self.clock = clock
        self.runsc_available = runsc_available
        self.profile = profile
        self.max_sessions_per_user = max_sessions_per_user
        self.default_ttl_s = default_ttl_s
        self.resources = resources or Resources()
        self._sessions: dict[str, Session] = {}
        self._counter = 0

    # --- paths ------------------------------------------------------------------------

    def coding_root(self, user: str) -> Path:
        return self.data_root / "Coding" / user

    def project_dir(self, user: str, slug: str) -> Path:
        return self.coding_root(user) / "Projects" / slug

    def gitconfig_path(self, user: str) -> Path:
        return self.coding_root(user) / "gitconfig"

    def artifacts_dir(self, user: str, run_id: str) -> Path:
        return self.coding_root(user) / "Artifacts" / run_id

    # --- projects ---------------------------------------------------------------------

    def prepare_project(self, user: str, slug: str, *, display_name: str) -> Path:
        """Create the project directory and the user's identity file; idempotent."""
        project = self.project_dir(user, slug)
        project.mkdir(parents=True, exist_ok=True)
        write_atomic(
            self.gitconfig_path(user),
            render_gitconfig(display_name, f"{user}@slas.local"),
            mode=0o644,
        )
        return project

    # --- sessions ---------------------------------------------------------------------

    def runtime_choice(self) -> tuple[str, str]:
        if self.runsc_available:
            return "runsc", "The sandbox runs under gVisor."
        if self.profile == "prod":
            raise SandboxError(
                ThreePartMessage(
                    "No sandbox can be opened: gVisor is not installed on this host.",
                    "The prod profile requires gVisor for code sandboxes (CLAUDE.md §3).",
                    "Install runsc, or run the quickstart profile, which falls back to "
                    "hardened runc.",
                )
            )
        return "runc", (
            "gVisor is not installed on this host, so the sandbox runs under hardened runc: "
            "its own user namespace, a seccomp profile, no network and no capabilities."
        )

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
        mine = self.sessions_for(user)
        if len(mine) >= self.max_sessions_per_user:
            raise SandboxError(
                ThreePartMessage(
                    f"You already have {len(mine)} sandboxes open, which is the limit.",
                    "Each person may keep a few sandboxes open at once so a host is never "
                    "filled by one user.",
                    "Close one from the Coding page, or wait for an idle one to expire.",
                )
            )
        runtime, runtime_sentence = self.runtime_choice()
        project = self.prepare_project(user, slug, display_name=display_name)
        self._counter += 1
        now = self.clock.now()
        session_id = f"{slug}-{now.strftime('%Y%m%d%H%M%S')}-{self._counter}"
        scratch = self.coding_root(user) / "Container" / session_id
        scratch.mkdir(parents=True, exist_ok=True)
        ttl = ttl_s or self.default_ttl_s
        spec = SandboxSpec(
            name=f"slas-sbx-{session_id}",
            image=image,
            runtime=runtime,
            user=user,
            slug=slug,
            mounts=[
                Mount(source=str(project), target=WORKSPACE, mode="rw"),
                Mount(source=str(scratch), target=SCRATCH, mode="rw"),
                Mount(source=str(self.gitconfig_path(user)), target=GITCONFIG_TARGET, mode="ro"),
            ],
            env=default_env(language=language),
            resources=self.resources,
            ttl_s=ttl,
        )
        handle = self.runtime.create(spec)
        session = Session(
            id=session_id,
            user=user,
            slug=slug,
            handle=handle,
            opened_at=now,
            last_used_at=now,
            expires_at=now + timedelta(seconds=ttl),
            runtime_sentence=runtime_sentence,
        )
        self._sessions[session_id] = session
        metrics.set_gauge("slas_sandbox_sessions_open", len(self._sessions))
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SandboxError(
                ThreePartMessage(
                    "That sandbox is no longer open.",
                    "It was closed, or it expired after being idle.",
                    "Open the project again from the Coding page; your files and commits are kept.",
                )
            ) from None

    def sessions_for(self, user: str) -> list[Session]:
        return [s for s in self._sessions.values() if s.user == user]

    def exec(
        self,
        session_id: str,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        session = self.get(session_id)
        now = self.clock.now()
        if now >= session.expires_at:
            self.close(session_id)
            raise SandboxError(
                ThreePartMessage(
                    f"The sandbox for {session.slug} expired after being idle.",
                    f"Sandboxes close after {session.handle.spec.ttl_s // 60} idle minutes.",
                    "Open the project again; your files and commits are kept.",
                )
            )
        if not argv or any(not isinstance(part, str) for part in argv):
            raise ValueError("argv must be a non-empty list of strings")
        result = self.runtime.exec(
            session.handle, list(argv), cwd=cwd, timeout_s=timeout_s, stdin=stdin
        )
        session.last_used_at = now
        session.expires_at = now + timedelta(seconds=session.handle.spec.ttl_s)
        return result

    def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        metrics.set_gauge("slas_sandbox_sessions_open", len(self._sessions))
        if session is None:
            return
        if self.runtime.alive(session.handle):
            self.runtime.destroy(session.handle)
        _remove_tree(Path(session.scratch_dir))

    def reap(self) -> list[str]:
        """Close every expired session; returns their ids."""
        now = self.clock.now()
        expired = [s.id for s in self._sessions.values() if now >= s.expires_at]
        for session_id in expired:
            self.close(session_id)
        return expired

    def sentence(self) -> str:
        count = len(self._sessions)
        return f"{count} {'sandbox is' if count == 1 else 'sandboxes are'} open on this host."


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()
