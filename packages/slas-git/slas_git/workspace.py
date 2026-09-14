"""Workspace-local Git — Method 2 of the Hybrid Git Control Engine (CLAUDE.md §5.7).

Everything here runs `git` as argv through a `GitExec`: inside the sandbox via the
sandbox-manager exec API in production, a local subprocess in tests. Every invocation
carries the hardening flags the broker uses too, so repository config written by a model or
a user never executes code (`core.hooksPath=/var/empty`, no credential helper, no
fsmonitor, no include). Agent commits carry `Slas-Agent` and `Slas-Ticket` trailers so the
history shows what the agent did versus the person.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Final, Protocol

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

HARDENING_ENV: Final[dict[str, str]] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/false",
    "SSH_ASKPASS": "/bin/false",
}
#: CLAUDE.md §5.7 also lists `-c include.path=`; git refuses it on the command line
#: ("relative config includes must come from files"), so includes are neutralised another
#: way: GIT_CONFIG_NOSYSTEM plus a platform-owned GIT_CONFIG_GLOBAL leave only the repo's own
#: .git/config, which the broker scans for `include`/`includeIf` before any operation.
HARDENING_ARGS: Final[tuple[str, ...]] = (
    "-c",
    "core.hooksPath=/var/empty",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "core.sshCommand=/bin/false",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=always",
)
PUSH_EXPLANATION: Final = "Push happens from the Git panel, which uses your saved remote."
AGENT_TRAILER: Final = "Slas-Agent"
TICKET_TRAILER: Final = "Slas-Ticket"


class CommandResult(SlasModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class GitExec(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
    ) -> CommandResult: ...


class LocalGitExec:
    """Runs git on this machine with a minimal environment. Used by tests and by the
    platform's own repositories (docs remotes); the sandbox path goes through exec."""

    def __init__(self, timeout_s: int = 120) -> None:
        self.timeout_s = timeout_s
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
    ) -> CommandResult:
        self.calls.append(list(argv))
        minimal = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": cwd, "LANG": "C.UTF-8"}
        completed = subprocess.run(  # noqa: S603 — argv list, no shell, hardened env
            list(argv),
            cwd=cwd,
            env={**minimal, **env},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )
        return CommandResult(
            exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
        )


class FakeGitExec:
    """Answers scripted results by argv suffix; records every call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, dict[str, str], str | None]] = []
        self._scripts: dict[tuple[str, ...], CommandResult] = {}

    def script(self, *suffix: str, result: CommandResult) -> None:
        self._scripts[tuple(suffix)] = result

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), cwd, dict(env), stdin))
        bare = _strip_hardening(argv)
        for suffix, result in self._scripts.items():
            if tuple(bare[: len(suffix)]) == suffix:
                return result
        return CommandResult(exit_code=0)


def _strip_hardening(argv: Sequence[str]) -> list[str]:
    parts = list(argv)
    if parts and parts[0] == "git":
        parts = parts[1:]
    while len(parts) >= 2 and parts[0] == "-c":
        parts = parts[2:]
    return parts


class GitError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class Identity(SlasModel):
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")

    @classmethod
    def for_user(cls, user: str, display_name: str) -> Identity:
        return cls(name=display_name, email=f"{user}@slas.local")


class Commit(SlasModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    subject: str
    body: str = ""
    trailers: dict[str, str] = Field(default_factory=dict)

    @property
    def by_agent(self) -> bool:
        return AGENT_TRAILER in self.trailers


def agent_trailers(agent: str, ticket_id: str) -> dict[str, str]:
    return {AGENT_TRAILER: agent, TICKET_TRAILER: ticket_id}


def parse_trailers(body: str) -> dict[str, str]:
    """`Key: value` lines in the last paragraph of a commit body."""
    paragraphs = [p for p in body.strip().split("\n\n") if p.strip()]
    if not paragraphs:
        return {}
    trailers: dict[str, str] = {}
    for line in paragraphs[-1].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and " " not in key.strip():
            trailers[key.strip()] = value.strip()
        else:
            return {}
    return trailers


class GitWorkspace:
    def __init__(self, exec_: GitExec, *, cwd: str, identity: Identity | None = None) -> None:
        self.exec = exec_
        self.cwd = cwd
        self.identity = identity

    # --- plumbing ----------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(HARDENING_ENV)
        if self.identity is not None:
            env.update(
                GIT_AUTHOR_NAME=self.identity.name,
                GIT_AUTHOR_EMAIL=self.identity.email,
                GIT_COMMITTER_NAME=self.identity.name,
                GIT_COMMITTER_EMAIL=self.identity.email,
            )
        return env

    def git(self, *args: str, stdin: str | None = None, check: bool = True) -> CommandResult:
        argv = ["git", *HARDENING_ARGS, *args]
        result = self.exec.run(argv, cwd=self.cwd, env=self._env(), stdin=stdin)
        if check and result.exit_code != 0:
            raise GitError(
                ThreePartMessage(
                    f"git {args[0]} did not finish in {self.cwd}.",
                    result.stderr.strip().splitlines()[-1]
                    if result.stderr.strip()
                    else f"git exited with code {result.exit_code}.",
                    "Open the Terminal tab to look at the repository, then try again.",
                )
            )
        return result

    # --- queries -----------------------------------------------------------------------

    def is_repo(self) -> bool:
        return self.git("rev-parse", "--is-inside-work-tree", check=False).exit_code == 0

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def head_sha(self) -> str | None:
        result = self.git("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.exit_code == 0 else None

    def has_changes(self) -> bool:
        return bool(self.git("status", "--porcelain", "--untracked-files=all").stdout.strip())

    def status(self) -> list[tuple[str, str]]:
        lines = self.git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        return [(line[:2].strip() or "??", line[3:]) for line in lines if len(line) > 3]

    def remotes(self) -> list[str]:
        return [r for r in self.git("remote").stdout.split() if r]

    def diff(self, base: str, *, stat: bool = False) -> str:
        args = ["diff", "--no-color", "--no-ext-diff"]
        if stat:
            args.append("--stat")
        return self.git(*args, base, "HEAD").stdout

    def log(self, limit: int = 20) -> list[Commit]:
        if self.head_sha() is None:
            return []
        raw = self.git("log", f"--max-count={limit}", "--format=%H%x1f%s%x1f%b%x1e").stdout
        commits: list[Commit] = []
        for record in raw.split("\x1e"):
            if not record.strip():
                continue
            sha, subject, body = [*record.strip("\n").split("\x1f"), "", ""][:3]
            commits.append(
                Commit(sha=sha.strip(), subject=subject, body=body, trailers=parse_trailers(body))
            )
        return commits

    # --- changes -----------------------------------------------------------------------

    def init(self, *, default_branch: str = "main") -> None:
        if self.is_repo():
            return
        self.git("init", "--quiet", f"--initial-branch={default_branch}")

    def checkout_branch(self, name: str) -> str:
        """Switch to `name`, creating it from HEAD if needed. Returns the branch name."""
        exists = self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        if exists.exit_code == 0:
            self.git("checkout", "--quiet", name)
        elif self.head_sha() is None:
            self.git("checkout", "--quiet", "--orphan", name)
        else:
            self.git("checkout", "--quiet", "-b", name)
        return name

    def add_all(self) -> None:
        self.git("add", "--all")

    def commit(
        self, subject: str, *, body: str = "", trailers: Mapping[str, str] | None = None
    ) -> str:
        """Commit the index with the message on stdin; returns the new sha."""
        if not subject.strip():
            raise ValueError("a commit needs a subject")
        message = subject.strip()
        if body.strip():
            message += "\n\n" + body.strip()
        if trailers:
            message += "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers.items())
        self.git("commit", "--quiet", "--no-verify", "--file=-", stdin=message + "\n")
        sha = self.head_sha()
        if sha is None:  # pragma: no cover — commit succeeded, HEAD exists
            raise GitError(
                ThreePartMessage("The commit left no HEAD.", "", "Check the repository.")
            )
        return sha

    def explain_push(self) -> str:
        return PUSH_EXPLANATION
