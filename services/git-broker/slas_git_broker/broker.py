"""git-broker: the only holder of Git credentials and the only route to a remote (§5.7, INV-14).

Every operation: authorise → resolve the remote (owner-scoped) → allowlist the host →
decrypt the credential in memory for this one call → run `git` as argv with the hardening
flags → record one audit row → wipe. A push first passes the validation gate; an
agent-authored push also passes the Consensus Router. A branch push opens a merge request
when the host's API is known. Nothing here ever returns a credential.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import Field

from slas_authz import Capability, Principal
from slas_authz.decide import require
from slas_git.audit import AuditLog, AuditRow, Result
from slas_git.bundle import BundleInfo, export_bundle, import_bundle
from slas_git.credentials import CredentialStore, token_fingerprint
from slas_git.gate import (
    CrossChecker,
    GatePolicy,
    GateReport,
    RegexSecretScanner,
    SecretScanner,
    validate_push,
)
from slas_git.hostapi import (
    HostApiError,
    HttpClient,
    MergeRequest,
    merge_request_spec,
    parse_merge_request,
)
from slas_git.hosts import GitHost, GitHosts, host_for
from slas_git.redact import redact
from slas_git.remotes import Remote, RemoteStore, add_remote
from slas_git.workspace import (
    HARDENING_ENV,
    CommandResult,
    GitError,
    GitWorkspace,
    Identity,
    transport_args,
)
from slas_git_broker.askpass import token_pipe
from slas_git_broker.runner import ProcessRunner
from slas_git_broker.sshkey import key_file
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class Clock(Protocol):
    def now(self) -> datetime: ...


class GitBrokerError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class GateRefusedError(GitBrokerError):
    def __init__(self, report: GateReport) -> None:
        self.report = report
        super().__init__(
            ThreePartMessage(
                "The push was refused by the validation gate.",
                report.sentence(),
                "Fix what the checks name, commit again and push again.",
            )
        )


class BrokerGitExec:
    """GitExec over the broker's ProcessRunner with per-operation env and inherited fds."""

    def __init__(
        self, runner: ProcessRunner, *, extra_env: Mapping[str, str], pass_fds: Sequence[int]
    ) -> None:
        self.runner = runner
        self.extra_env = dict(extra_env)
        self.pass_fds = tuple(pass_fds)

    def run(
        self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], stdin: str | None = None
    ) -> CommandResult:
        return self.runner.run(
            argv, cwd=cwd, env={**env, **self.extra_env}, stdin=stdin, pass_fds=self.pass_fds
        )


class TokenGitExec:
    """GitExec for a token remote: every git process gets its own freshly filled pipe.

    git reads the askpass answer to EOF, so a pipe serves exactly one process; an operation
    such as push (ls-remote, then push) needs one per call. The token stays in this object's
    memory only for the operation and is never written anywhere.
    """

    def __init__(
        self, runner: ProcessRunner, *, token: str, askpass_path: Path, username: str
    ) -> None:
        self.runner = runner
        self._token = token
        self.askpass_path = askpass_path
        self.username = username

    def run(
        self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], stdin: str | None = None
    ) -> CommandResult:
        with token_pipe(self._token) as (read_fd, pipe_env):
            merged = {
                **env,
                **pipe_env,
                "GIT_ASKPASS": str(self.askpass_path),
                "SLAS_ASKPASS_USERNAME": self.username,
            }
            return self.runner.run(argv, cwd=cwd, env=merged, stdin=stdin, pass_fds=(read_fd,))


class ConnectionReport(SlasModel):
    ok: bool
    branches: list[str] = Field(default_factory=list)
    sentence: str


class CloneResult(SlasModel):
    path: str
    branch: str
    sentence: str


class PushResult(SlasModel):
    branch: str
    sha: str
    gate: GateReport
    merge_request: MergeRequest | None = None
    sentence: str


class GitBroker:
    def __init__(
        self,
        *,
        runner: ProcessRunner,
        remotes: RemoteStore,
        credentials: CredentialStore,
        hosts: GitHosts,
        audit: AuditLog,
        key_dir: Path,
        askpass_path: Path,
        http: HttpClient,
        clock: Clock,
        data_root: Path,
        policy: GatePolicy | None = None,
        scanner: SecretScanner | None = None,
        cross_checker: CrossChecker | None = None,
    ) -> None:
        self.runner = runner
        self.remotes = remotes
        self.credentials = credentials
        self.hosts = hosts
        self.audit = audit
        self.key_dir = key_dir
        self.askpass_path = askpass_path
        self.http = http
        self.clock = clock
        self.data_root = data_root
        self.policy = policy or GatePolicy()
        self.scanner = scanner or RegexSecretScanner()
        self.cross_checker = cross_checker

    # --- paths and helpers ---------------------------------------------------------------

    def project_dir(self, user: str, slug: str) -> Path:
        return self.data_root / "Coding" / user / "Projects" / slug

    def bundles_dir(self, user: str) -> Path:
        return self.data_root / "Coding" / user / "Bundles"

    def _workspace(
        self, path: Path, principal: Principal, exec_: BrokerGitExec | None = None
    ) -> GitWorkspace:
        exec_ = exec_ or BrokerGitExec(self.runner, extra_env={}, pass_fds=())
        return GitWorkspace(
            exec_,
            cwd=str(path),
            identity=Identity.for_user(principal.subject, principal.display_name),
        )

    def _remote_and_host(self, principal: Principal, remote_id: str) -> tuple[Remote, GitHost]:
        remote = self.remotes.get(remote_id, owner=principal.subject)
        host = host_for(remote.parsed(), self.hosts)
        now = self.clock.now()
        if remote.expires_at is not None and now >= remote.expires_at:
            raise GitBrokerError(
                ThreePartMessage(
                    f"The credential for {remote.name} expired on {remote.expires_at:%Y-%m-%d}.",
                    "You set an expiry when you saved it.",
                    "Rotate the credential under Settings → Git remotes.",
                )
            )
        return remote, host

    def _with_credential(
        self, stack: ExitStack, remote: Remote, host: GitHost, principal: Principal
    ) -> tuple[BrokerGitExec | TokenGitExec, str, str | None]:
        """Decrypt for this operation only. Returns (exec, url for git, token or None)."""
        secret = self.credentials.reveal(remote.credential_ref, owner=principal.subject)
        uri = remote.parsed()
        if remote.auth_type == "pat":
            exec_ = TokenGitExec(
                self.runner,
                token=secret,
                askpass_path=self.askpass_path,
                username=host.https_username,
            )
            return exec_, uri.https_url(host.https_username), secret
        ssh = stack.enter_context(
            key_file(self.key_dir, secret, known_hosts_line=host.ssh_host_key or "", tag=remote.id)
        )
        return BrokerGitExec(self.runner, extra_env=ssh.env, pass_fds=()), uri.ssh_url(), None

    def _audit(
        self,
        principal: Principal,
        op: str,
        started: float,
        result: Result,
        *,
        remote: Remote | None = None,
        branch: str | None = None,
        sha: str | None = None,
        detail: str = "",
    ) -> AuditRow:
        return self.audit.record(
            AuditRow(
                at=self.clock.now(),
                user=principal.subject,
                remote_id=remote.id if remote else None,
                remote_name=remote.name if remote else None,
                op=op,
                branch=branch,
                sha=sha,
                result=result,
                duration_s=max(0.0, time.monotonic() - started),
                detail=redact(detail),
            )
        )

    def _touch(self, remote: Remote) -> None:
        self.remotes.save(remote.model_copy(update={"last_used": self.clock.now()}))

    # --- remotes -------------------------------------------------------------------------

    def add_remote(
        self,
        principal: Principal,
        *,
        name: str,
        uri: str,
        auth_type: str,
        secret: str,
        default_branch: str = "main",
    ) -> Remote:
        require(principal, Capability.GIT_REMOTE_MANAGE)
        started = time.monotonic()
        fingerprint = None
        if auth_type == "ssh_key":
            fingerprint = self._ssh_fingerprint(secret, tag="new")
        remote, _host = add_remote(
            owner=principal.subject,
            name=name,
            uri=uri,
            auth_type=auth_type,  # type: ignore[arg-type]
            secret=secret,
            hosts=self.hosts,
            credentials=self.credentials,
            remotes=self.remotes,
            now=self.clock.now(),
            default_branch=default_branch,
            ssh_fingerprint=fingerprint,
        )
        self._audit(
            principal,
            "add_remote",
            started,
            "ok",
            remote=remote,
            detail=f"{remote.auth_type} {remote.fingerprint}",
        )
        return remote

    def _ssh_fingerprint(self, private_key: str, *, tag: str) -> str:
        """`ssh-keygen -lf` on the key, written 0600 for the moment it takes; else unverified."""
        try:
            with key_file(self.key_dir, private_key, known_hosts_line="pinned", tag=tag) as ssh:
                result = self.runner.run(
                    ["ssh-keygen", "-l", "-f", ssh.key_path], cwd=str(self.key_dir), env={}
                )
        except (OSError, RuntimeError):
            return "SHA256:(unverified)"
        parts = result.stdout.split()
        return (
            parts[1]
            if result.exit_code == 0 and len(parts) >= 2 and parts[1].startswith("SHA256:")
            else "SHA256:(unverified)"
        )

    def rotate_credential(self, principal: Principal, remote_id: str, secret: str) -> Remote:
        require(principal, Capability.GIT_REMOTE_MANAGE)
        started = time.monotonic()
        remote = self.remotes.get(remote_id, owner=principal.subject)
        fingerprint = (
            token_fingerprint(secret)
            if remote.auth_type == "pat"
            else self._ssh_fingerprint(secret, tag=remote.id)
        )
        self.credentials.rotate(
            remote.credential_ref,
            secret.strip(),
            owner=principal.subject,
            fingerprint=fingerprint,
            now=self.clock.now(),
        )
        updated = remote.model_copy(update={"fingerprint": fingerprint})
        self.remotes.save(updated)
        self._audit(
            principal, "rotate_credential", started, "ok", remote=updated, detail=fingerprint
        )
        return updated

    def delete_remote(self, principal: Principal, remote_id: str) -> str:
        require(principal, Capability.GIT_REMOTE_MANAGE)
        started = time.monotonic()
        remote = self.remotes.delete(remote_id, owner=principal.subject)
        self.credentials.delete(remote.credential_ref, owner=principal.subject)
        self._audit(principal, "delete_remote", started, "ok", remote=remote)
        return f"Removed {remote.name}; its credential was deleted with it."

    # --- operations ----------------------------------------------------------------------

    def test_connection(self, principal: Principal, remote_id: str) -> ConnectionReport:
        started = time.monotonic()
        remote, host = self._remote_and_host(principal, remote_id)
        with ExitStack() as stack:
            exec_, url, _ = self._with_credential(stack, remote, host, principal)
            result = exec_.run(
                ["git", *transport_args(remote.parsed().scheme), "ls-remote", "--heads", url],
                cwd=str(self.data_root),
                env=HARDENING_ENV,
            )
        if result.exit_code != 0:
            self._audit(
                principal,
                "ls_remote",
                started,
                "failed",
                remote=remote,
                detail=result.stderr.strip()[-300:],
            )
            return ConnectionReport(ok=False, sentence=self._explain_failure(remote, result))
        branches = [
            line.split("refs/heads/", 1)[1]
            for line in result.stdout.splitlines()
            if "refs/heads/" in line
        ]
        self._audit(
            principal, "ls_remote", started, "ok", remote=remote, detail=f"{len(branches)} branches"
        )
        self._touch(remote)
        count = len(branches)
        return ConnectionReport(
            ok=True,
            branches=branches,
            sentence=f"Connected to {remote.name}: {count} {'branch' if count == 1 else 'branches'}"
            + (
                f", including {remote.default_branch}."
                if remote.default_branch in branches
                else "."
            ),
        )

    def _explain_failure(self, remote: Remote, result: CommandResult) -> str:
        err = redact(result.stderr.strip())
        if "Authentication failed" in err or "403" in err or "401" in err:
            return (
                f"{remote.name} did not accept the credential ({remote.fingerprint}). "
                "Rotate it under Settings → Git remotes; the value stored may be wrong or expired."
            )
        if "Could not resolve host" in err or "unable to access" in err:
            return (
                f"{remote.name} could not be reached from git-broker. Check that {remote.host} "
                "is up and listed under Admin → Git hosts."
            )
        last = err.splitlines()[-1] if err else "git gave no reason."
        return f"{remote.name} did not answer as expected: {last}"

    def clone(self, principal: Principal, remote_id: str, slug: str) -> CloneResult:
        require(principal, Capability.GIT_CLONE)
        started = time.monotonic()
        remote, host = self._remote_and_host(principal, remote_id)
        target = self.project_dir(principal.subject, slug)
        if target.exists() and any(target.iterdir()):
            raise GitBrokerError(
                ThreePartMessage(
                    f"The project {slug} already has files.",
                    "A clone needs an empty project directory.",
                    "Pull into the existing project instead, or choose another project name.",
                )
            )
        target.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            exec_, url, _ = self._with_credential(stack, remote, host, principal)
            result = exec_.run(
                [
                    "git",
                    *transport_args(remote.parsed().scheme),
                    "clone",
                    "--quiet",
                    "--branch",
                    remote.default_branch,
                    url,
                    str(target),
                ],
                cwd=str(target.parent),
                env=HARDENING_ENV,
            )
        if result.exit_code != 0:
            self._audit(
                principal,
                "clone",
                started,
                "failed",
                remote=remote,
                detail=result.stderr.strip()[-300:],
            )
            raise GitBrokerError(
                ThreePartMessage(
                    f"Cloning {remote.name} did not finish.",
                    self._explain_failure(remote, result),
                    "Fix the cause and try again.",
                )
            )
        # The workspace keeps a credential-free URL: nothing in it can be used without the broker.
        self._workspace(target, principal).git(
            "remote",
            "set-url",
            "origin",
            remote.parsed().https_url() if remote.auth_type == "pat" else remote.parsed().ssh_url(),
        )
        sha = self._workspace(target, principal).head_sha() or ""
        self._audit(
            principal,
            "clone",
            started,
            "ok",
            remote=remote,
            branch=remote.default_branch,
            sha=sha or None,
        )
        self._touch(remote)
        return CloneResult(
            path=str(target),
            branch=remote.default_branch,
            sentence=f"Cloned {remote.name} into {slug}; the sandbox sees it at /workspace.",
        )

    def pull(
        self, principal: Principal, remote_id: str, slug: str, branch: str | None = None
    ) -> str:
        require(principal, Capability.GIT_PULL)
        started = time.monotonic()
        remote, host = self._remote_and_host(principal, remote_id)
        target = self.project_dir(principal.subject, slug)
        wanted = branch or remote.default_branch
        with ExitStack() as stack:
            exec_, url, _ = self._with_credential(stack, remote, host, principal)
            result = exec_.run(
                [
                    "git",
                    *transport_args(remote.parsed().scheme),
                    "pull",
                    "--quiet",
                    "--ff-only",
                    url,
                    wanted,
                ],
                cwd=str(target),
                env=HARDENING_ENV,
            )
        if result.exit_code != 0:
            self._audit(
                principal,
                "pull",
                started,
                "failed",
                remote=remote,
                branch=wanted,
                detail=result.stderr.strip()[-300:],
            )
            raise GitBrokerError(
                ThreePartMessage(
                    f"Pulling {wanted} from {remote.name} did not finish.",
                    "The branch has diverged, or the remote did not answer: "
                    + redact(
                        result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
                    ),
                    "Commit or stash local changes and try again.",
                )
            )
        sha = self._workspace(target, principal).head_sha()
        self._audit(principal, "pull", started, "ok", remote=remote, branch=wanted, sha=sha)
        self._touch(remote)
        return f"Pulled {wanted} from {remote.name}; {slug} is at {(sha or '')[:10]}."

    def push_branch(
        self,
        principal: Principal,
        remote_id: str,
        slug: str,
        branch: str,
        *,
        agent_authored: bool = False,
        open_review: bool = True,
        target_branch: str | None = None,
        title: str | None = None,
        body: str = "",
    ) -> PushResult:
        require(principal, Capability.GIT_PUSH_BRANCH)
        started = time.monotonic()
        remote, host = self._remote_and_host(principal, remote_id)
        target_dir = self.project_dir(principal.subject, slug)
        target = target_branch or remote.default_branch
        protected = branch == remote.default_branch
        workspace = self._workspace(target_dir, principal)
        head = workspace.git("rev-parse", "--verify", f"refs/heads/{branch}", check=False)
        if head.exit_code != 0:
            raise GitBrokerError(
                ThreePartMessage(
                    f"{slug} has no branch called {branch}.",
                    "Only a branch that exists locally can be pushed.",
                    "Commit on the branch first, or pick another.",
                )
            )
        sha = head.stdout.strip()

        with ExitStack() as stack:
            exec_, url, token = self._with_credential(stack, remote, host, principal)
            # The gate compares against what the remote already has on the target branch.
            listed = exec_.run(
                [
                    "git",
                    *transport_args(remote.parsed().scheme),
                    "ls-remote",
                    url,
                    f"refs/heads/{target}",
                ],
                cwd=str(target_dir),
                env=HARDENING_ENV,
            )
            remote_sha = (
                listed.stdout.split()[0]
                if listed.exit_code == 0 and listed.stdout.strip()
                else None
            )
            base = (
                remote_sha
                if remote_sha
                and workspace.git("cat-file", "-e", remote_sha, check=False).exit_code == 0
                else EMPTY_TREE
            )
            report = validate_push(
                workspace,
                base=base,
                head=sha,
                policy=self.policy,
                scanner=self.scanner,
                agent_authored=agent_authored,
                cross_checker=self.cross_checker,
                protected_branch=protected,
                may_push_protected=principal.can(Capability.GIT_PUSH_PROTECTED),
            )
            if not report.passed:
                self._audit(
                    principal,
                    "push_branch",
                    started,
                    "refused",
                    remote=remote,
                    branch=branch,
                    sha=sha,
                    detail=report.sentence(),
                )
                raise GateRefusedError(report)
            pushed = exec_.run(
                [
                    "git",
                    *transport_args(remote.parsed().scheme),
                    "push",
                    "--quiet",
                    url,
                    f"refs/heads/{branch}:refs/heads/{branch}",
                ],
                cwd=str(target_dir),
                env=HARDENING_ENV,
            )
            if pushed.exit_code != 0:
                self._audit(
                    principal,
                    "push_branch",
                    started,
                    "failed",
                    remote=remote,
                    branch=branch,
                    sha=sha,
                    detail=pushed.stderr.strip()[-300:],
                )
                raise GitBrokerError(
                    ThreePartMessage(
                        f"Pushing {branch} to {remote.name} did not finish.",
                        self._explain_failure(remote, pushed),
                        "Fix the cause and push again.",
                    )
                )
            merge_request: MergeRequest | None = None
            note = ""
            if open_review and not protected:
                if token is None:
                    note = (
                        " SSH remotes carry no API token, so open the review request on the "
                        "host yourself."
                    )
                else:
                    try:
                        spec = merge_request_spec(
                            host,
                            remote.parsed(),
                            source_branch=branch,
                            target_branch=target,
                            title=title or f"{slug}: {branch}",
                            body=body,
                            token=token,
                        )
                        merge_request = parse_merge_request(
                            host, self.http.send(spec), title=title or f"{slug}: {branch}"
                        )
                    except HostApiError as exc:
                        note = " " + exc.message.what_happened + " " + exc.message.what_to_do
        self._audit(
            principal,
            "push_branch",
            started,
            "ok",
            remote=remote,
            branch=branch,
            sha=sha,
            detail=(merge_request.url if merge_request else note.strip()),
        )
        self._touch(remote)
        sentence = f"Pushed {branch} ({sha[:10]}) to {remote.name}."
        if merge_request is not None:
            sentence += f" {merge_request.sentence()}"
        return PushResult(
            branch=branch,
            sha=sha,
            gate=report,
            merge_request=merge_request,
            sentence=sentence + note,
        )

    # --- bundles -------------------------------------------------------------------------

    def export_bundle(
        self, principal: Principal, slug: str, *, name: str | None = None
    ) -> BundleInfo:
        require(principal, Capability.GIT_BUNDLE)
        started = time.monotonic()
        workspace = self._workspace(self.project_dir(principal.subject, slug), principal)
        out = (
            self.bundles_dir(principal.subject)
            / f"{name or slug}-{self.clock.now():%Y%m%d-%H%M%S}.bundle"
        )
        try:
            info = export_bundle(workspace, out)
        except GitError as exc:
            self._audit(
                principal, "bundle_export", started, "failed", detail=exc.message.likely_cause
            )
            raise GitBrokerError(exc.message) from exc
        self._audit(principal, "bundle_export", started, "ok", detail=info.sentence())
        return info

    def import_bundle(self, principal: Principal, slug: str, path: Path) -> list[str]:
        require(principal, Capability.GIT_BUNDLE)
        started = time.monotonic()
        target = self.project_dir(principal.subject, slug)
        target.mkdir(parents=True, exist_ok=True)
        workspace = self._workspace(target, principal)
        workspace.init()
        try:
            branches = import_bundle(workspace, path)
        except GitError as exc:
            self._audit(
                principal, "bundle_import", started, "failed", detail=exc.message.likely_cause
            )
            raise GitBrokerError(exc.message) from exc
        self._audit(principal, "bundle_import", started, "ok", detail=", ".join(branches))
        return branches
