"""The Coding Agent's deterministic executor — kernel-side code (INV-3, §10.1 ACT/CHECK).

    toolchain     record the resolved toolchain (the first feed line)
    sandbox_open  open a sandbox on Projects/<slug>, init the repo, remember the base commit
    iterate       retrieve → model proposes edits → apply → lint/type/build/test in the
                  sandbox; stop after 3 iterations without progress and ask the human
    commit        local commit on branch slas/<ticket> with Slas-Agent / Slas-Ticket trailers
    export_zip    Artifacts/<ticket>/<slug>.zip, sha256 on the ticket
    cross_check   the final diff to the Consensus Router (majority; concerns surfaced)

The model proposes edits; code applies them, runs the checks and decides whether progress
was made. The sandbox never holds a credential or a route to a remote; every git call goes
through `slas_git.workspace` with its hardening flags.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol

from pydantic import Field

from slas_git.workspace import (
    CommandResult,
    GitExec,
    GitWorkspace,
    Identity,
    agent_trailers,
)
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.rca import CrossChecker
from slas_orchestrator.coding.breakdown import TaskItem
from slas_orchestrator.coding.export import zip_project
from slas_sandbox_manager.manager import Session
from slas_sandbox_manager.runtime import ExecResult
from slas_sandbox_manager.spec import WORKSPACE
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Step
from slas_schemas.ticket import Observation

STALL_LIMIT: Final = 3
MAX_SNAPSHOT_FILES: Final = 60
MAX_SNAPSHOT_BYTES: Final = 64 * 1024
EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_DIFF_CHARS: Final = 40_000
_SKIP_DIRS: Final = frozenset({".git", "__pycache__", "node_modules", "target", ".venv", "build"})


class CheckResult(SlasModel):
    kind: str
    description: str
    exit_code: int
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class EditRequest(SlasModel):
    task: TaskItem
    iteration: int = Field(ge=1)
    languages: list[str]
    #: Current text files in the workspace (bounded), for the model's context.
    files: dict[str, str]
    failures: list[CheckResult] = Field(default_factory=list)


class EditSet(SlasModel):
    """What the model proposes: path → new content, or None to delete."""

    files: dict[str, str | None] = Field(default_factory=dict)
    note: str = ""


class Coder(Protocol):
    def propose_edits(self, request: EditRequest) -> EditSet: ...


class FakeCoder:
    """Scripted edit sets, one per call; when the script runs out it proposes nothing."""

    def __init__(self, *edits: EditSet) -> None:
        self.script = list(edits)
        self.requests: list[EditRequest] = []

    def propose_edits(self, request: EditRequest) -> EditSet:
        self.requests.append(request)
        return self.script.pop(0) if self.script else EditSet()


class EditError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def apply_edits(project_dir: Path, edits: EditSet) -> list[str]:
    """Write the model's edits inside the project only; `.git` and escapes are refused."""
    root = project_dir.resolve()
    changed: list[str] = []
    for rel, content in edits.files.items():
        if not rel or rel.startswith(("/", "~")) or ".." in Path(rel).parts:
            raise EditError(
                ThreePartMessage(
                    f"The model tried to write outside the project: {rel}.",
                    "Edits are confined to the project directory.",
                    "The step stops here; review the model's proposal on the ticket.",
                )
            )
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
            raise EditError(
                ThreePartMessage(
                    f"The model tried to write to {rel}.",
                    "The repository metadata and anything outside the project are off limits.",
                    "The step stops here; review the model's proposal on the ticket.",
                )
            )
        if content is None:
            if target.exists():
                target.unlink()
                changed.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != content:
            target.write_text(content, encoding="utf-8")
            changed.append(rel)
    return changed


def snapshot(project_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(project_dir.rglob("*")):
        if len(files) >= MAX_SNAPSHOT_FILES:
            break
        rel = path.relative_to(project_dir)
        if not path.is_file() or path.is_symlink() or _SKIP_DIRS & set(rel.parts):
            continue
        data = path.read_bytes()
        if len(data) > MAX_SNAPSHOT_BYTES or b"\x00" in data:
            continue
        files[str(rel)] = data.decode("utf-8", errors="replace")
    return files


class SandboxAccess(Protocol):
    """What the executor needs from the sandbox manager (ADR-0015).

    The in-process `slas_sandbox_manager.manager.SandboxManager` satisfies it, and so does
    the orchestrator's HTTP client (`slas_orchestrator.clients.HttpSandboxManager`): open
    and exec travel over the wire, while the two paths are computed locally because both
    containers mount the same `${SLAS_DATA_ROOT}`.
    """

    def project_dir(self, user: str, slug: str) -> Path: ...

    def artifacts_dir(self, user: str, run_id: str) -> Path: ...

    def open(
        self,
        user: str,
        slug: str,
        *,
        image: str,
        language: str,
        display_name: str,
        ttl_s: int | None = None,
    ) -> Session: ...

    def exec(
        self,
        session_id: str,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult: ...


class SandboxGitExec:
    """Runs git inside the sandbox through the manager's exec API (argv only)."""

    def __init__(self, manager: SandboxAccess, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
    ) -> CommandResult:
        result = self.manager.exec(self.session_id, list(argv), cwd=cwd, stdin=stdin)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)


#: How the executor reaches git for a session: (exec, cwd). Default: inside the sandbox.
GitAccess = Callable[[Session], tuple[GitExec, str]]


class CodingExecutor:
    def __init__(
        self,
        *,
        manager: SandboxAccess,
        coder: Coder,
        cross_checker: CrossChecker | None = None,
        git_access: GitAccess | None = None,
        display_names: Mapping[str, str] | None = None,
    ) -> None:
        self.manager = manager
        self.coder = coder
        self.cross_checker = cross_checker
        self._git_access = git_access or (
            lambda session: (SandboxGitExec(self.manager, session.id), WORKSPACE)
        )
        self.display_names = dict(display_names or {})
        self._sessions: dict[str, Session] = {}
        self._base: dict[str, str] = {}
        self._branches: dict[str, str] = {}

    # --- dispatch ---------------------------------------------------------------------

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        handlers = {
            "toolchain": self._toolchain,
            "sandbox_open": self._sandbox_open,
            "iterate": self._iterate,
            "commit": self._commit,
            "export_zip": self._export_zip,
            "cross_check": self._cross_check,
        }
        handler = handlers.get(step.primitive)
        if handler is None:
            raise UnknownPrimitiveError(step)
        return handler(step, context)

    def session_for(self, ticket_id: str) -> Session | None:
        return self._sessions.get(ticket_id)

    def _workspace(self, session: Session, user: str) -> GitWorkspace:
        exec_, cwd = self._git_access(session)
        identity = Identity.for_user(user, self.display_names.get(user, user))
        return GitWorkspace(exec_, cwd=cwd, identity=identity)

    def _require_session(self, context: ExecutionContext) -> Session:
        session = self._sessions.get(context.ticket_id)
        if session is None:
            raise RuntimeError(f"{context.ticket_id} has no open sandbox; run sandbox_open first")
        return session

    def _project(self, session: Session) -> Path:
        # Computed here, not read from the session's mounts: over HTTP the mount source is
        # the path as the sandbox manager (or the host) sees it, while this process reads
        # the project under its own `${SLAS_DATA_ROOT}`.
        return Path(self.manager.project_dir(session.user, session.slug))

    # --- steps -------------------------------------------------------------------------

    def _toolchain(self, step: Step, context: ExecutionContext) -> Observation:
        records = step.args.get("resolutions", [])
        return Observation(
            exit_code=0,
            stdout="".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            summary=str(step.args.get("sentence") or step.title),
        )

    def _sandbox_open(self, step: Step, context: ExecutionContext) -> Observation:
        slug = str(step.args["slug"])
        session = self.manager.open(
            context.user,
            slug,
            image=str(step.args["image"]),
            language=str(step.args["language"]),
            display_name=self.display_names.get(context.user, context.user),
        )
        self._sessions[context.ticket_id] = session
        workspace = self._workspace(session, context.user)
        workspace.init()
        self._base[context.ticket_id] = workspace.head_sha() or EMPTY_TREE
        return Observation(
            exit_code=0,
            summary=f"{session.runtime_sentence} {session.handle.spec.sentence()}",
            stdout=f"project={self._project(session)}\nbase={self._base[context.ticket_id]}\n",
        )

    def _run_checks(self, session: Session, checks: list[dict[str, object]]) -> list[CheckResult]:
        results: list[CheckResult] = []
        for check in checks:
            raw_argv = check["argv"]
            argv = [str(a) for a in raw_argv] if isinstance(raw_argv, list) else []
            result = self.manager.exec(session.id, argv)
            output = (result.stdout + result.stderr).strip()
            results.append(
                CheckResult(
                    kind=str(check["kind"]),
                    description=str(check["description"]),
                    exit_code=result.exit_code if not result.timed_out else 124,
                    output=output[-4000:],
                )
            )
        return results

    def _iterate(self, step: Step, context: ExecutionContext) -> Observation:
        session = self._require_session(context)
        project = self._project(session)
        task = TaskItem.model_validate(step.args["task"])
        checks = list(step.args.get("checks", []))
        max_iterations = int(step.args.get("max_iterations", 6))
        languages = [str(lang) for lang in step.args.get("languages", [])]
        failures: list[CheckResult] = []
        log: list[str] = []
        stall = 0
        last_signature: str | None = None

        for iteration in range(1, max_iterations + 1):
            request = EditRequest(
                task=task,
                iteration=iteration,
                languages=languages,
                files=snapshot(project),
                failures=failures,
            )
            edits = self.coder.propose_edits(request)
            try:
                changed = apply_edits(project, edits)
            except EditError as exc:
                return Observation(
                    exit_code=2, summary=exc.message.what_happened, stdout="\n".join(log)
                )
            results = self._run_checks(session, checks)
            failures = [r for r in results if not r.ok]
            passed = ", ".join(f"{r.kind} ok" for r in results if r.ok) or "no checks"
            failed = ", ".join(f"{r.kind} failed" for r in failures)
            log.append(
                f"iteration {iteration}: {len(changed)} file(s) changed; {passed}"
                + (f"; {failed}" if failed else "")
            )
            if not failures:
                return Observation(
                    exit_code=0,
                    summary=(
                        f"{task.title}: done after {iteration} "
                        f"{'iteration' if iteration == 1 else 'iterations'}; {passed}."
                    ),
                    stdout="\n".join(log) + "\n",
                )
            signature = hashlib.sha256(
                "\n".join(f"{r.kind}:{r.exit_code}:{r.output[-500:]}" for r in failures).encode()
            ).hexdigest()
            if not changed or signature == last_signature:
                stall += 1
            else:
                stall = 0
            last_signature = signature
            if stall >= STALL_LIMIT:
                return Observation(
                    exit_code=3,
                    summary=(
                        f"{task.title}: stopped after {iteration} iterations because the last "
                        f"{STALL_LIMIT} made no progress ({failed}). A person needs to look at it."
                    ),
                    stdout="\n".join(log) + "\n",
                    stderr="\n".join(f"[{r.kind}] {r.output}" for r in failures),
                )
        return Observation(
            exit_code=3,
            summary=(
                f"{task.title}: not finished after {max_iterations} iterations "
                f"({failed}). A person needs to look at it."
            ),
            stdout="\n".join(log) + "\n",
            stderr="\n".join(f"[{r.kind}] {r.output}" for r in failures),
        )

    def _commit(self, step: Step, context: ExecutionContext) -> Observation:
        session = self._require_session(context)
        workspace = self._workspace(session, context.user)
        branch = workspace.checkout_branch(f"slas/{context.ticket_id}")
        self._branches[context.ticket_id] = branch
        workspace.add_all()
        if not workspace.has_changes():
            return Observation(exit_code=0, summary=f"Nothing new to commit on {branch}.")
        sha = workspace.commit(
            str(step.args.get("subject") or "Changes by the Coding Agent"),
            body=str(step.args.get("body", "")),
            trailers=agent_trailers("coding", context.ticket_id),
        )
        return Observation(
            exit_code=0,
            summary=(
                f"Committed {sha[:10]} on branch {branch} with Slas-Agent and Slas-Ticket trailers."
            ),
            stdout=f"{sha}\n",
        )

    def _export_zip(self, step: Step, context: ExecutionContext) -> Observation:
        session = self._require_session(context)
        out = self.manager.artifacts_dir(context.user, context.ticket_id) / f"{session.slug}.zip"
        export = zip_project(self._project(session), out)
        return Observation(
            exit_code=0,
            summary=f"ZIP exported to {export.path} (sha256 {export.sha256[:12]}…).",  # type: ignore[index]
            exports=[export],
        )

    def _cross_check(self, step: Step, context: ExecutionContext) -> Observation:
        session = self._require_session(context)
        workspace = self._workspace(session, context.user)
        base = self._base.get(context.ticket_id, EMPTY_TREE)
        diff = workspace.diff(base) if workspace.head_sha() else ""
        stat = workspace.diff(base, stat=True) if workspace.head_sha() else ""
        if not diff.strip():
            return Observation(
                exit_code=0, summary="Nothing changed, so there is nothing to cross-check."
            )
        if self.cross_checker is None:
            return Observation(
                exit_code=3,
                summary=(
                    "Not cross-checked: no voters are configured, so the change needs your own "
                    "review before it is used."
                ),
                stdout=stat,
            )
        verdict = self.cross_checker.cross_check(
            str(step.args.get("decision", "code_change")),
            [f"Task: {step.title}", f"Diff stat:\n{stat}", f"Diff:\n{diff[:MAX_DIFF_CHARS]}"],
        )
        return Observation(
            exit_code=0 if verdict.agreed else 3,
            summary=verdict.sentence,
            stdout=stat,
            votes=list(verdict.votes),
        )
