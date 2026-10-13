"""The Coding Agent's deterministic executor — kernel-side code (INV-3, §10.1 ACT/CHECK).

    toolchain     record the resolved toolchain (the first feed line)
    sandbox_open  open a sandbox on Projects/<slug>, init the repo, remember the base commit
    skill         a skill the person picked, compiled by the kernel's gate: its `run` steps
                  become checks every task's iterations must pass
    iterate       retrieve → model proposes edits (and commands to run) → apply → run the
                  commands → lint/type/build/test in the sandbox; stop after 3 iterations
                  without progress and ask the human, or when the person presses Stop
    commit        local commit on branch slas/<ticket> with Slas-Agent / Slas-Ticket trailers;
                  the change set is kept as Artifacts/<ticket>/changes.diff for the page
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
    Commit,
    GitExec,
    GitWorkspace,
    Identity,
    agent_trailers,
)
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.rca import CrossChecker
from slas_kernel.skills import compiled_from_step
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
#: Commands the model may ask for per iteration, and their bounds (ADR-0021).
MAX_COMMANDS: Final = 4
COMMAND_TIMEOUT_S: Final = 120
MAX_COMMAND_OUTPUT_CHARS: Final = 3_000
#: The change set the Changes view shows, next to the task's ZIP.
CHANGES_FILE: Final = "changes.diff"
MAX_CHANGES_CHARS: Final = 2_000_000
#: Shells refuse a `-c` line: commands are argv, never a shell line (CLAUDE.md §6.2).
_SHELLS: Final = frozenset({"sh", "bash", "dash", "zsh", "ash", "ksh"})
#: Git subcommands that only read; the agent itself commits (ADR-0021).
_GIT_READ: Final = frozenset(
    {"status", "log", "diff", "show", "grep", "ls-files", "blame", "rev-parse", "branch"}
)
MAX_SNAPSHOT_FILES: Final = 60
MAX_SNAPSHOT_BYTES: Final = 64 * 1024
EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_DIFF_CHARS: Final = 40_000
_SKIP_DIRS: Final = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "target",
        ".venv",
        "build",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
    }
)


class CheckResult(SlasModel):
    kind: str
    description: str
    exit_code: int
    output: str = ""
    #: The exit code that counts as passing (a skill's `expect_exit`).
    expected: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == self.expected


class Command(SlasModel):
    """A command the model wants run in the sandbox after its edits: argv, never a line."""

    argv: list[str] = Field(min_length=1, max_length=64)
    why: str = ""


class CommandRun(SlasModel):
    argv: list[str]
    exit_code: int
    output: str = ""


class EditRequest(SlasModel):
    task: TaskItem
    iteration: int = Field(ge=1)
    languages: list[str]
    #: Current text files in the workspace (bounded), for the model's context.
    files: dict[str, str]
    failures: list[CheckResult] = Field(default_factory=list)
    #: The person's whole plan.md: a task line alone ("plan", "coding") says too little.
    plan: str = ""
    #: Every file path in the project, so the model knows what exists beyond the snapshot.
    paths: list[str] = Field(default_factory=list)
    #: The commands the model asked for last iteration, with what they printed.
    command_runs: list[CommandRun] = Field(default_factory=list)
    #: The project's recent commits, newest first: what earlier tasks and prompts changed.
    history: list[str] = Field(default_factory=list)


class Replacement(SlasModel):
    """One targeted edit: `find` must occur exactly once in `path`, and becomes `replace`."""

    path: str
    find: str = Field(min_length=1)
    replace: str = ""


class EditSet(SlasModel):
    """What the model proposes, in the order it writes it: first its approach (a few short
    steps, so it reasons before it writes), then targeted replacements in existing files,
    then whole files (path → complete content, or None to delete), then one sentence."""

    approach: list[str] = Field(default_factory=list, max_length=12)
    replacements: list[Replacement] = Field(default_factory=list)
    files: dict[str, str | None] = Field(default_factory=dict)
    #: Run after the edits, before the checks; the output comes back next iteration.
    commands: list[Command] = Field(default_factory=list, max_length=MAX_COMMANDS)
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


def _confined(root: Path, rel: str) -> Path:
    """The path `rel` names inside the project; anything else stops the step."""
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
    return target


def apply_replacements(project_dir: Path, edits: EditSet) -> tuple[list[str], list[str]]:
    """Apply the targeted edits in order. Returns (changed paths, problems): a `find` that is
    missing or ambiguous is skipped and named, so the model can correct it next iteration
    instead of the whole proposal being lost."""
    root = project_dir.resolve()
    changed: list[str] = []
    problems: list[str] = []
    for item in edits.replacements:
        target = _confined(root, item.path)
        if not target.is_file():
            problems.append(f"{item.path} does not exist; write it as a whole file instead.")
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(item.find)
        if count != 1:
            where = "does not occur" if count == 0 else f"occurs {count} times"
            first = item.find.strip().splitlines()[0][:80] if item.find.strip() else item.find
            problems.append(
                f"In {item.path} the text to replace {where} (it starts {first!r}); copy it "
                "exactly from the file and include enough lines to make it unique."
            )
            continue
        updated = text.replace(item.find, item.replace, 1)
        if updated != text:
            target.write_text(updated, encoding="utf-8")
            if item.path not in changed:
                changed.append(item.path)
    return changed, problems


def apply_edits(project_dir: Path, edits: EditSet) -> list[str]:
    """Write the model's whole-file edits inside the project only; `.git` and escapes are
    refused."""
    root = project_dir.resolve()
    changed: list[str] = []
    for rel, content in edits.files.items():
        target = _confined(root, rel)
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


#: Build and tool output that never belongs in a commit, whatever the project's .gitignore says.
TOOL_CACHE_PATTERNS: Final = (
    "__pycache__/",
    "*.py[cod]",
    ".ruff_cache/",
    ".mypy_cache/",
    ".pytest_cache/",
)


def exclude_tool_caches(project_dir: Path) -> bool:
    """Add TOOL_CACHE_PATTERNS to `.git/info/exclude` (the repository's private ignore list,
    so the person's own files are untouched). Returns whether the file changed."""
    exclude = project_dir / ".git" / "info" / "exclude"
    if not (project_dir / ".git").is_dir():
        return False
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    missing = [p for p in TOOL_CACHE_PATTERNS if p not in current.splitlines()]
    if not missing:
        return False
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not current or current.endswith("\n") else "\n"
    header = "# tool caches, added by the Coding Agent\n"
    exclude.write_text(current + prefix + header + "\n".join(missing) + "\n", encoding="utf-8")
    return True


def missing_files(project_dir: Path, names: Sequence[str]) -> list[str]:
    """The task's named files that do not exist yet. A name with a directory must exist at
    that path; a bare name (`__main__.py`) may sit in any directory of the project."""
    missing: list[str] = []
    for name in names:
        if "/" in name:
            if not (project_dir / name).is_file():
                missing.append(name)
            continue
        found = any(
            not (_SKIP_DIRS & set(path.relative_to(project_dir).parts)) and path.is_file()
            for path in project_dir.rglob(name)
        )
        if not found:
            missing.append(name)
    return missing


def fix_script_modes(project_dir: Path) -> list[str]:
    """Make the executable bit follow the shebang; returns the paths whose mode changed.

    The model writes contents, never modes, so it cannot fix a check like ruff's EXE001
    ("shebang is present but file is not executable") or EXE002; the agent does it.
    """
    changed: list[str] = []
    for path in sorted(project_dir.rglob("*")):
        rel = path.relative_to(project_dir)
        if not path.is_file() or path.is_symlink() or _SKIP_DIRS & set(rel.parts):
            continue
        with path.open("rb") as handle:
            shebang = handle.read(2) == b"#!"
        mode = path.stat().st_mode
        executable = bool(mode & 0o111)
        if shebang and not executable:
            path.chmod(mode | ((mode & 0o444) >> 2))  # x wherever r is set
            changed.append(str(rel))
        elif not shebang and executable and path.suffix == ".py":
            path.chmod(mode & ~0o111)
            changed.append(str(rel))
    return changed


def project_paths(project_dir: Path) -> list[str]:
    """Every file of the project the model may care about, sorted; tool output left out."""
    paths: list[str] = []
    for path in sorted(project_dir.rglob("*")):
        rel = path.relative_to(project_dir)
        if path.is_file() and not path.is_symlink() and not (_SKIP_DIRS & set(rel.parts)):
            paths.append(str(rel))
    return paths


def snapshot(project_dir: Path, *, first: Sequence[str] = ()) -> dict[str, str]:
    """Text files of the project, bounded. Paths in `first` (the files a task names, the
    files the last iteration changed) are read before the rest, so they are never the ones
    the bound leaves out."""
    files: dict[str, str] = {}
    paths = project_paths(project_dir)
    wanted = [p for p in paths if p in first or Path(p).name in first]
    for rel_text in wanted + [p for p in paths if p not in wanted]:
        if len(files) >= MAX_SNAPSHOT_FILES:
            break
        data = (project_dir / rel_text).read_bytes()
        if len(data) > MAX_SNAPSHOT_BYTES or b"\x00" in data:
            continue
        files[rel_text] = data.decode("utf-8", errors="replace")
    return files


def refused_command(argv: Sequence[str]) -> str | None:
    """Why a command the model asked for is not run, or None when it may run."""
    name = Path(argv[0]).name if argv else ""
    if name in _SHELLS and any(a == "-c" or a.startswith("-c") for a in argv[1:]):
        return (
            "a shell line is never run; write the steps into a script file in the project "
            "and run that file instead"
        )
    if name in ("sudo", "su", "doas"):
        return "nothing runs as another user in the sandbox"
    if name == "git":
        sub = ""
        rest = list(argv[1:])
        while rest:
            word = rest.pop(0)
            if word in ("-C", "-c", "--git-dir", "--work-tree"):
                rest = rest[1:]  # the option's value
            elif not word.startswith("-"):
                sub = word
                break
        if sub not in _GIT_READ:
            return (
                f"git {sub or '(no subcommand)'} changes the repository; the agent commits "
                "for you after the checks pass, so only read-only git commands run"
            )
    return None


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


#: How many of the project's commits the model sees, and how much of each.
HISTORY_COMMITS: Final = 12
MAX_HISTORY_CHARS: Final = 3_000


def history_lines(commits: Sequence[Commit]) -> list[str]:
    """One line per commit, newest first: its subject and the task lines in its body
    (trailers left out), bounded so a long history never crowds out the files."""
    lines: list[str] = []
    used = 0
    for commit in commits:
        body = [
            line.strip()
            for line in commit.body.splitlines()
            if line.strip() and ":" not in line.split(" ", 1)[0]
        ]
        text = f"{commit.sha[:8]} {commit.subject}" + (f" ({'; '.join(body)})" if body else "")
        text = text[:400]
        if used + len(text) > MAX_HISTORY_CHARS:
            break
        lines.append(text)
        used += len(text)
    return lines


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
#: (ticket id, sentence): one live line in the task's activity feed while a step runs.
Progress = Callable[[str, str], None]


def _names(paths: Sequence[str], limit: int = 6) -> str:
    shown = ", ".join(paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def _first_problem_line(output: str) -> str:
    """The line of a check's output a person would read first: an error, else the last."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(word in lowered for word in ("error", "failed", "assert", "traceback")):
            return line[:160]
    return lines[-1][:160] if lines else ""


class CodingExecutor:
    def __init__(
        self,
        *,
        manager: SandboxAccess,
        coder: Coder,
        cross_checker: CrossChecker | None = None,
        git_access: GitAccess | None = None,
        display_names: Mapping[str, str] | None = None,
        progress: Progress | None = None,
    ) -> None:
        self.manager = manager
        #: Where live lines go while a step runs (the ticket's journal in the service).
        self.progress = progress
        self.coder = coder
        self.cross_checker = cross_checker
        self._git_access = git_access or (
            lambda session: (SandboxGitExec(self.manager, session.id), WORKSPACE)
        )
        self.display_names = dict(display_names or {})
        self._sessions: dict[str, Session] = {}
        self._base: dict[str, str] = {}
        self._branches: dict[str, str] = {}
        #: Checks the ticket's skills added, run after the language's own in every iteration.
        self._skill_checks: dict[str, list[dict[str, object]]] = {}
        #: The project's recent commits, read when the sandbox opens.
        self._history: dict[str, list[str]] = {}
        #: Tickets someone asked to stop, with who asked.
        self._stops: dict[str, str] = {}

    # --- dispatch ---------------------------------------------------------------------

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        handlers = {
            "toolchain": self._toolchain,
            "sandbox_open": self._sandbox_open,
            "skill": self._skill,
            "iterate": self._iterate,
            "commit": self._commit,
            "export_zip": self._export_zip,
            "cross_check": self._cross_check,
        }
        handler = handlers.get(step.primitive)
        if handler is None:
            raise UnknownPrimitiveError(step)
        return handler(step, context)

    def request_stop(self, ticket_id: str, by: str) -> None:
        """Ask a running task to stop: it does after the model call in flight, without
        applying that answer, and the task waits for review with its files as they are."""
        self._stops[ticket_id] = by

    def session_for(self, ticket_id: str) -> Session | None:
        return self._sessions.get(ticket_id)

    def forget(self, ticket_id: str) -> Session | None:
        """Drop what the executor remembers of a removed ticket; returns the session it held,
        if any, so the caller can close it."""
        self._base.pop(ticket_id, None)
        self._branches.pop(ticket_id, None)
        self._skill_checks.pop(ticket_id, None)
        self._history.pop(ticket_id, None)
        self._stops.pop(ticket_id, None)
        return self._sessions.pop(ticket_id, None)

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

    def _say(self, context: ExecutionContext, sentence: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(context.ticket_id, sentence)
        except Exception:  # a live line never stops the work
            return

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
        self._history[context.ticket_id] = history_lines(workspace.log(HISTORY_COMMITS))
        return Observation(
            exit_code=0,
            summary=f"{session.runtime_sentence} {session.handle.spec.sentence()}",
            stdout=f"project={self._project(session)}\nbase={self._base[context.ticket_id]}\n",
        )

    def _skill(self, step: Step, context: ExecutionContext) -> Observation:
        compiled = compiled_from_step(step)
        skill_id = str(step.args.get("skill_id", ""))
        if compiled is None:
            return Observation(
                exit_code=2,
                summary=f"The skill {skill_id} was not compiled, so the Coding Agent can't use it.",
            )
        checks: list[dict[str, object]] = []
        for inner in compiled.plan.steps:
            command = inner.args.get("command")
            if inner.primitive != "run" or not isinstance(command, list):
                return Observation(
                    exit_code=2,
                    summary=(
                        f"The skill {skill_id} uses `{inner.primitive}`, which the Coding "
                        "Agent's sandbox can't perform; only `run` steps work for coding. "
                        "Remove the skill from the task or use it with another agent."
                    ),
                )
            cwd = str(inner.args.get("cwd") or "").strip("/")
            if ".." in Path(cwd).parts:
                return Observation(
                    exit_code=2,
                    summary=f"The skill {skill_id} runs in {cwd}, outside the project.",
                )
            argv = [str(a) for a in command]
            checks.append(
                {
                    "kind": skill_id,
                    "argv": argv,
                    "description": f"{skill_id}: {' '.join(argv)}",
                    "expect_exit": int(inner.args.get("expect_exit", 0) or 0),
                    "timeout_s": int(inner.args.get("timeout_s", 600) or 600),
                    "cwd": f"{WORKSPACE}/{cwd}" if cwd else WORKSPACE,
                }
            )
        self._skill_checks.setdefault(context.ticket_id, []).extend(checks)
        commands = "; ".join(str(c["description"]).split(": ", 1)[1] for c in checks)
        return Observation(
            exit_code=0,
            summary=(
                f"The skill {skill_id} adds {len(checks)} "
                f"{'check' if len(checks) == 1 else 'checks'} to every task: {commands}."
            ),
        )

    def _run_checks(self, session: Session, checks: list[dict[str, object]]) -> list[CheckResult]:
        results: list[CheckResult] = []
        for check in checks:
            raw_argv = check["argv"]
            argv = [str(a) for a in raw_argv] if isinstance(raw_argv, list) else []
            result = self.manager.exec(
                session.id,
                argv,
                cwd=str(check.get("cwd") or WORKSPACE),
                timeout_s=int(str(check.get("timeout_s") or 600)),
            )
            output = (result.stdout + result.stderr).strip()
            results.append(
                CheckResult(
                    kind=str(check["kind"]),
                    description=str(check["description"]),
                    exit_code=result.exit_code if not result.timed_out else 124,
                    output=output[-4000:],
                    expected=int(str(check.get("expect_exit") or 0)),
                )
            )
        return results

    def _iterate(self, step: Step, context: ExecutionContext) -> Observation:
        session = self._require_session(context)
        project = self._project(session)
        task = TaskItem.model_validate(step.args["task"])
        checks = [
            *step.args.get("checks", []),
            *self._skill_checks.get(context.ticket_id, []),
        ]
        max_iterations = int(step.args.get("max_iterations", 6))
        languages = [str(lang) for lang in step.args.get("languages", [])]
        plan_text = str(step.args.get("plan", ""))
        failures: list[CheckResult] = []
        log: list[str] = []
        stall = 0
        last_signature: str | None = None
        recent: list[str] = []
        ran: list[CommandRun] = []
        history = self._history.get(context.ticket_id, [])

        for iteration in range(1, max_iterations + 1):
            if context.ticket_id in self._stops:
                return self._stopped(context, task, iteration - 1, log)
            request = EditRequest(
                task=task,
                iteration=iteration,
                languages=languages,
                files=snapshot(project, first=[*task.files, *recent]),
                failures=failures,
                plan=plan_text,
                paths=project_paths(project),
                command_runs=ran,
                history=history,
            )
            self._say(
                context,
                f"Iteration {iteration} of {max_iterations}: "
                + (
                    "reading the project and writing the code…"
                    if iteration == 1
                    else f"fixing {', '.join(r.kind for r in failures) or 'the task'}…"
                ),
            )
            edits = self.coder.propose_edits(request)
            if context.ticket_id in self._stops:
                # The answer that was in flight is not applied (like Esc in Claude Code).
                return self._stopped(context, task, iteration - 1, log)
            if edits.approach:
                self._say(context, "Approach: " + " · ".join(a.strip() for a in edits.approach))
            try:
                changed, problems = apply_replacements(project, edits)
                changed += [rel for rel in apply_edits(project, edits) if rel not in changed]
            except EditError as exc:
                return Observation(
                    exit_code=2, summary=exc.message.what_happened, stdout="\n".join(log)
                )
            changed += [rel for rel in fix_script_modes(project) if rel not in changed]
            recent = list(changed)
            self._say(
                context,
                (f"Changed {_names(changed)}" if changed else "No file changed")
                + (f": {edits.note.strip()}" if edits.note.strip() else ".")
                + (" Running the checks…" if checks and not edits.commands else ""),
            )
            ran = self._run_commands(session, context, edits.commands)
            results = self._run_checks(session, checks)
            if problems:
                results.append(
                    CheckResult(
                        kind="edit",
                        description="every targeted edit applied",
                        exit_code=1,
                        output="\n".join(problems),
                    )
                )
            absent = missing_files(project, task.files)
            if absent:
                # T-coding-0018 passed every check without __main__.py and examples/.
                results.append(
                    CheckResult(
                        kind="files",
                        description="the files the task names exist",
                        exit_code=1,
                        output="The task names files that do not exist yet: "
                        + ", ".join(absent)
                        + ". Write them.",
                    )
                )
            elif not snapshot(project):
                # Every check passes on an empty project; that is not the task done.
                results.append(
                    CheckResult(
                        kind="files",
                        description="the project has files",
                        exit_code=1,
                        output="The project is still empty: write the files the plan asks for.",
                    )
                )
            failures = [r for r in results if not r.ok]
            passed = ", ".join(f"{r.kind} ok" for r in results if r.ok) or "no checks"
            failed = ", ".join(f"{r.kind} failed" for r in failures)
            if failures:
                worst = failures[0]
                line = _first_problem_line(worst.output)
                self._say(
                    context,
                    f"Iteration {iteration}: {failed}"
                    + (f" — {worst.kind}: {line}" if line else "")
                    + (f" ({passed})." if passed != "no checks" else "."),
                )
            log.append(
                f"iteration {iteration}: {len(changed)} file(s) changed; {passed}"
                + (f"; {failed}" if failed else "")
            )
            if not failures:
                # The model asked to see what its new code does: one more look, unless
                # nothing changed or this was the last iteration.
                if ran and changed and iteration < max_iterations:
                    self._say(
                        context, f"Iteration {iteration}: {passed}; reading the command output…"
                    )
                    continue
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

    def _stopped(
        self, context: ExecutionContext, task: TaskItem, done: int, log: list[str]
    ) -> Observation:
        by = self._stops.pop(context.ticket_id, "someone")
        when = (
            f"after {done} {'iteration' if done == 1 else 'iterations'}"
            if done
            else "before its first iteration"
        )
        return Observation(
            exit_code=4,
            summary=(
                f"{task.title}: stopped {when} because {by} pressed Stop. The files stay as "
                "they are in the project; ask for a change to carry on."
            ),
            stdout="\n".join(log) + ("\n" if log else ""),
        )

    def _run_commands(
        self, session: Session, context: ExecutionContext, commands: Sequence[Command]
    ) -> list[CommandRun]:
        """The commands the model asked for, run in the sandbox as argv (ADR-0021)."""
        runs: list[CommandRun] = []
        for command in commands[:MAX_COMMANDS]:
            argv = [str(a) for a in command.argv]
            shown = " ".join(argv)[:160]
            why = refused_command(argv)
            if why is not None:
                runs.append(CommandRun(argv=argv, exit_code=126, output=f"Not run: {why}."))
                self._say(context, f"Did not run `{shown}`: {why}.")
                continue
            self._say(context, f"Running `{shown}`…")
            result = self.manager.exec(session.id, argv, timeout_s=COMMAND_TIMEOUT_S)
            exit_code = 124 if result.timed_out else result.exit_code
            output = (result.stdout + result.stderr).strip()
            if len(output) > MAX_COMMAND_OUTPUT_CHARS:
                half = MAX_COMMAND_OUTPUT_CHARS // 2
                output = f"{output[:half]}\n[… output shortened …]\n{output[-half:]}"
            if result.timed_out:
                output += f"\n(stopped after {COMMAND_TIMEOUT_S} s)"
            runs.append(CommandRun(argv=argv, exit_code=exit_code, output=output))
        return runs

    def _commit(self, step: Step, context: ExecutionContext) -> Observation:
        session = self._require_session(context)
        workspace = self._workspace(session, context.user)
        branch = workspace.checkout_branch(f"slas/{context.ticket_id}")
        self._branches[context.ticket_id] = branch
        # A reviewer rightly objected to committed __pycache__ files (T-coding-0015).
        exclude_tool_caches(self._project(session))
        workspace.add_all()
        workspace.untrack_ignored()
        if not workspace.has_changes():
            return Observation(exit_code=0, summary=f"Nothing new to commit on {branch}.")
        sha = workspace.commit(
            str(step.args.get("subject") or "Changes by the Coding Agent"),
            body=str(step.args.get("body", "")),
            trailers=agent_trailers("coding", context.ticket_id),
        )
        self._keep_changes(workspace, context)
        return Observation(
            exit_code=0,
            summary=(
                f"Committed {sha[:10]} on branch {branch} with Slas-Agent and Slas-Ticket trailers."
            ),
            stdout=f"{sha}\n",
        )

    def _keep_changes(self, workspace: GitWorkspace, context: ExecutionContext) -> None:
        """Artifacts/<ticket>/changes.diff: what the task changed, for the Changes view."""
        base = self._base.get(context.ticket_id, EMPTY_TREE)
        diff = workspace.diff(base)
        if not diff.strip():
            return
        folder = self.manager.artifacts_dir(context.user, context.ticket_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / CHANGES_FILE).write_text(diff[:MAX_CHANGES_CHARS], encoding="utf-8")

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
