"""git-broker end to end against a fake Git host on loopback (§5.7, INV-14).

Add a remote with a token → test connection → clone → commit (with a malicious pre-push
hook in the workspace) → push a branch → merge request opened. Then: the hook never ran,
and the token appears in no argv, URL, audit row, remotes file, workspace config or server
access log. Refusals: secret in the diff, protected branch, unknown host, wrong token.
"""

from __future__ import annotations

import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_authz import AccessDeniedError, Capability, Principal
from slas_git.audit import AuditLog
from slas_git.credentials import EncryptedFileStore, FakeSealer, derive_key
from slas_git.hostapi import UrllibHttpClient
from slas_git.hosts import GitHost, GitHosts, HostNotAllowedError
from slas_git.redact import find_secrets
from slas_git.remotes import RemoteStore
from slas_git.workspace import (
    CommandResult,
    GitWorkspace,
    Identity,
    LocalGitExec,
    agent_trailers,
)
from slas_git_broker.askpass import install_askpass, token_pipe
from slas_git_broker.broker import GateRefusedError, GitBroker, GitBrokerError
from slas_git_broker.runner import FakeProcessRunner, LocalProcessRunner
from slas_git_broker.sshkey import SshKeyError, key_file
from slas_kernel.clock import FakeClock
from slas_kernel.rca import FakeCrossChecker
from slas_schemas.vote import Vote

from .fake_git_server import FakeGitHost

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TOKEN = "glpat-Zq8xw2Yv7Rt4Ks9Lm3Np6Bd"  # a fake token for the fake server
KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAAFAKEKEYFAKEKEYFAKEKEY\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
ENGINEER = Principal(
    subject="pat",
    display_name="Pat Lin",
    role="engineer",
    role_label="Engineer",
    capabilities=frozenset(
        {
            Capability.GIT_REMOTE_MANAGE,
            Capability.GIT_CLONE,
            Capability.GIT_PULL,
            Capability.GIT_PUSH_BRANCH,
            Capability.GIT_BUNDLE,
        }
    ),
)
VIEWER = Principal(
    subject="vic", display_name="Vic", role="viewer", role_label="Viewer", capabilities=frozenset()
)


class Bench:
    def __init__(
        self,
        tmp_path: Path,
        server: FakeGitHost,
        *,
        runner: LocalProcessRunner | FakeProcessRunner | None = None,
        hosts: GitHosts | None = None,
    ) -> None:
        self.tmp = tmp_path
        self.server = server
        self.runner = runner or LocalProcessRunner()
        self.clock = FakeClock(datetime(2026, 9, 14, 9, tzinfo=UTC))
        self.audit = AuditLog(tmp_path / "Git" / "audit.jsonl")
        self.credentials = EncryptedFileStore(
            tmp_path / "Git" / "credentials.json", FakeSealer(derive_key("s" * 43))
        )
        self.remotes = RemoteStore(tmp_path / "Git" / "remotes.json")
        self.key_dir = tmp_path / "run" / "slas-keys"
        self.checker = FakeCrossChecker(
            [Vote(voter=f"v{i}", verdict="approve", reason="ok", confidence=0.9) for i in range(3)],
            agreed=True,
        )
        self.hosts = hosts or GitHosts(
            hosts=[
                GitHost(
                    name="fake",
                    hostname="127.0.0.1",
                    kind="gitlab",
                    api_base=f"{server.state.base_url}/api/v4",
                    protocols=["http"],
                    https_username="oauth2",
                ),
                GitHost(
                    name="ssh-lab",
                    hostname="ssh.internal",
                    protocols=["ssh"],
                    ssh_host_key="ssh.internal ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE",
                ),
            ]
        )
        self.broker = GitBroker(
            runner=self.runner,
            remotes=self.remotes,
            credentials=self.credentials,
            hosts=self.hosts,
            audit=self.audit,
            key_dir=self.key_dir,
            askpass_path=install_askpass(tmp_path / "bin"),
            http=UrllibHttpClient(),
            clock=self.clock,
            data_root=tmp_path,
            cross_checker=self.checker,
        )

    def repo_url(self, path: str = "firmware/bmc") -> str:
        return f"{self.server.state.base_url}/{path}.git"

    def sinks(self) -> dict[str, str]:
        """Every place a credential could have leaked to, as text."""
        out = {
            "audit file": (self.tmp / "Git" / "audit.jsonl").read_text(encoding="utf-8")
            if (self.tmp / "Git" / "audit.jsonl").exists()
            else "",
            "audit sentences": "\n".join(row.sentence() for row in self.audit.read_all()),
            "process argv": "\n".join(" ".join(argv) for argv in self.runner.argv_seen),
            "remotes file": (self.tmp / "Git" / "remotes.json").read_text(encoding="utf-8"),
            "credentials file": (self.tmp / "Git" / "credentials.json").read_text(encoding="utf-8"),
            "server access log": "\n".join(self.server.state.access_log),
            "server api bodies": json.dumps(self.server.state.api_calls),
        }
        for config in self.tmp.rglob(".git/config"):
            out[f"workspace config {config.parent.parent.name}"] = config.read_text(
                encoding="utf-8"
            )
        return out


def test_full_push_flow_over_the_fake_host_with_a_hostile_hook(tmp_path: Path) -> None:
    with FakeGitHost(tmp_path / "server", token=TOKEN) as server:
        server.create_repo("firmware/bmc")
        bench = Bench(tmp_path, server)
        broker = bench.broker

        remote = broker.add_remote(
            ENGINEER, name="gitlab-firmware", uri=bench.repo_url(), auth_type="pat", secret=TOKEN
        )
        assert remote.fingerprint == "…m3Np6Bd"[-5:] or remote.fingerprint == f"…{TOKEN[-4:]}"
        assert remote.uri == bench.repo_url() and TOKEN not in remote.uri

        report = broker.test_connection(ENGINEER, remote.id)
        assert report.ok and report.branches == ["main"]
        assert report.sentence == "Connected to gitlab-firmware: 1 branch, including main."
        assert server.state.auth_failures >= 1, (
            "git tried without a credential first, then used GIT_ASKPASS"
        )

        clone = broker.clone(ENGINEER, remote.id, "bmc")
        project = Path(clone.path)
        assert (project / "README.md").read_text() == "# firmware/bmc\n"
        assert (
            clone.sentence == "Cloned gitlab-firmware into bmc; the sandbox sees it at /workspace."
        )
        ws = GitWorkspace(
            LocalGitExec(), cwd=str(project), identity=Identity.for_user("pat", "Pat Lin")
        )
        origin = ws.git("remote", "get-url", "origin").stdout.strip()
        assert origin == bench.repo_url() and "@" not in origin

        # A hostile hook in the workspace, and a change committed by the agent.
        hook = project / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho HOOK_RAN > hook-ran.txt\nexit 1\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
        ws.checkout_branch("slas/T-coding-0001")
        (project / "bmc" / "fan.py").parent.mkdir()
        (project / "bmc" / "fan.py").write_text(
            "def rpm() -> int:\n    return 3000\n", encoding="utf-8"
        )
        ws.add_all()
        sha = ws.commit("Add fan control", trailers=agent_trailers("coding", "T-coding-0001"))

        result = broker.push_branch(
            ENGINEER,
            remote.id,
            "bmc",
            "slas/T-coding-0001",
            agent_authored=True,
            title="Add fan control",
        )
        assert result.sha == sha and result.gate.passed and result.merge_request is not None
        assert result.merge_request.url == f"{server.state.base_url}/-/merge_requests/1"
        assert result.sentence == (
            f"Pushed slas/T-coding-0001 ({sha[:10]}) to gitlab-firmware. Opened a review request: "
            f"{server.state.base_url}/-/merge_requests/1"
        )
        assert not (project / "hook-ran.txt").exists(), (
            "the workspace's pre-push hook did not execute"
        )
        assert server.state.api_calls[0]["body"]["source_branch"] == "slas/T-coding-0001"
        assert server.state.api_calls[0]["body"]["target_branch"] == "main"
        assert server.state.api_calls[0]["path"] == "/api/v4/projects/firmware%2Fbmc/merge_requests"
        pushed = LocalGitExec().run(
            [
                "git",
                "-C",
                str(tmp_path / "server" / "firmware" / "bmc.git"),
                "rev-parse",
                "refs/heads/slas/T-coding-0001",
            ],
            cwd=str(tmp_path),
            env={"GIT_CONFIG_NOSYSTEM": "1"},
        )
        assert pushed.stdout.strip() == sha
        assert bench.checker.calls and bench.checker.calls[0][0] == "code_change"

        rows = bench.audit.read_all()
        assert [row.op for row in rows] == ["add_remote", "ls_remote", "clone", "push_branch"]
        assert (
            all(row.result == "ok" for row in rows)
            and rows[-1].sha == sha
            and rows[-1].branch == "slas/T-coding-0001"
        )
        assert rows[-1].detail == f"{server.state.base_url}/-/merge_requests/1"

        # The CI grep: the token is in no sink, and nothing token- or key-shaped is either.
        for name, text in bench.sinks().items():
            assert find_secrets(text, [TOKEN]) == [], f"{name} leaks a credential"
        # The fd number reaches git through the environment; the token itself never does.
        askpass_env = [env for env in bench.runner.env_seen if "SLAS_ASKPASS_FD" in env]
        assert askpass_env and all(TOKEN not in json.dumps(env) for env in askpass_env)

        # Bundles: out of one project, into another.
        info = broker.export_bundle(ENGINEER, "bmc")
        assert Path(info.path).parent == tmp_path / "Coding" / "pat" / "Bundles"
        branches = broker.import_bundle(ENGINEER, "bmc-copy", Path(info.path))
        assert sorted(branches) == ["main", "slas/T-coding-0001"]
        assert [row.op for row in bench.audit.read_all()][-2:] == ["bundle_export", "bundle_import"]


def test_refusals_secret_in_diff_protected_branch_bad_token_unknown_host(tmp_path: Path) -> None:
    with FakeGitHost(tmp_path / "server", token=TOKEN) as server:
        server.create_repo("firmware/bmc")
        bench = Bench(tmp_path, server)
        broker = bench.broker
        remote = broker.add_remote(
            ENGINEER, name="gitlab-firmware", uri=bench.repo_url(), auth_type="pat", secret=TOKEN
        )
        broker.clone(ENGINEER, remote.id, "bmc")
        project = broker.project_dir("pat", "bmc")
        ws = GitWorkspace(
            LocalGitExec(), cwd=str(project), identity=Identity.for_user("pat", "Pat Lin")
        )

        ws.checkout_branch("leaky")
        (project / "settings.py").write_text(
            'API = "glpat-abcdefghijklmnopqrst"\n', encoding="utf-8"
        )
        ws.add_all()
        ws.commit("Leak")
        with pytest.raises(GateRefusedError) as refused:
            broker.push_branch(ENGINEER, remote.id, "bmc", "leaky")
        assert refused.value.message.what_happened == "The push was refused by the validation gate."
        assert "settings.py:1 (gitlab_pat)" in refused.value.message.likely_cause
        assert bench.audit.read_all()[-1].result == "refused"
        listed = LocalGitExec().run(
            [
                "git",
                "-C",
                str(tmp_path / "server" / "firmware" / "bmc.git"),
                "branch",
                "--list",
                "leaky",
            ],
            cwd=str(tmp_path),
            env={"GIT_CONFIG_NOSYSTEM": "1"},
        )
        assert listed.stdout.strip() == "", "nothing reached the host"

        ws.git("checkout", "--quiet", "main")
        (project / "note.txt").write_text("direct\n", encoding="utf-8")
        ws.add_all()
        ws.commit("Direct to main")
        with pytest.raises(GateRefusedError) as protected:
            broker.push_branch(ENGINEER, remote.id, "bmc", "main")
        assert "need git:push_protected" in protected.value.message.likely_cause
        lead = Principal(
            subject="pat",
            display_name="Pat Lin",
            role="lead",
            role_label="Lead",
            capabilities=ENGINEER.capabilities | {Capability.GIT_PUSH_PROTECTED},
        )
        direct = broker.push_branch(lead, remote.id, "bmc", "main")
        assert direct.merge_request is None and direct.gate.passed

        with pytest.raises(GitBrokerError, match="has no branch called nope"):
            broker.push_branch(ENGINEER, remote.id, "bmc", "nope")

        broker.rotate_credential(ENGINEER, remote.id, "glpat-wrongwrongwrongwrong")
        bad = broker.test_connection(ENGINEER, remote.id)
        assert not bad.ok and bad.sentence.startswith(
            "gitlab-firmware did not accept the credential (…rong)"
        )
        with pytest.raises(
            GitBrokerError, match="Pulling main from gitlab-firmware did not finish"
        ):
            broker.pull(ENGINEER, remote.id, "bmc")

        with pytest.raises(HostNotAllowedError) as host:
            broker.add_remote(
                ENGINEER,
                name="gh",
                uri="https://github.com/octo/repo.git",
                auth_type="pat",
                secret=TOKEN,
            )
        assert (
            host.value.message.what_happened
            == "github.com is not an allowed Git host. Allowed: 127.0.0.1, ssh.internal."
        )

        with pytest.raises(AccessDeniedError):
            broker.add_remote(VIEWER, name="x", uri=bench.repo_url(), auth_type="pat", secret=TOKEN)
        with pytest.raises(AccessDeniedError):
            broker.push_branch(VIEWER, remote.id, "bmc", "main")
        assert (
            broker.delete_remote(ENGINEER, remote.id)
            == "Removed gitlab-firmware; its credential was deleted with it."
        )
        for name, text in bench.sinks().items():
            assert find_secrets(text, [TOKEN, "glpat-wrongwrongwrongwrong"]) == [], (
                f"{name} leaks a credential"
            )


def test_ssh_remote_uses_a_shredded_key_file_and_pinned_known_hosts(tmp_path: Path) -> None:
    with FakeGitHost(tmp_path / "server", token=TOKEN) as server:
        runner = FakeProcessRunner()
        runner.script(
            "ssh-keygen",
            result=CommandResult(exit_code=0, stdout="256 SHA256:AbCdEf comment (ED25519)\n"),
        )
        runner.script(
            "ls-remote",
            result=CommandResult(
                exit_code=0, stdout="0123456789abcdef0123456789abcdef01234567\trefs/heads/main\n"
            ),
        )
        bench = Bench(tmp_path, server, runner=runner)
        remote = bench.broker.add_remote(
            ENGINEER,
            name="deploy",
            uri="git@ssh.internal:lab/tools.git",
            auth_type="ssh_key",
            secret=KEY,
        )
        assert (
            remote.fingerprint == "SHA256:AbCdEf"
            and remote.uri == "ssh://git@ssh.internal/lab/tools.git"
        )
        report = bench.broker.test_connection(ENGINEER, remote.id)
        assert report.ok and report.branches == ["main"]
        argv, _cwd, env, fds = runner.calls[-1]
        assert argv[-2:] == ["ls-remote", "--heads"] or argv[-3] == "ls-remote"
        command = env["GIT_SSH_COMMAND"]
        assert f"-i {bench.key_dir}/key-{remote.id}-" in command
        assert (
            "-o IdentitiesOnly=yes -o StrictHostKeyChecking=yes" in command
            and "UserKnownHostsFile=" in command
        )
        assert fds == () and all(KEY.strip() not in value for value in env.values())
        assert list(bench.key_dir.iterdir()) == [], (
            "the key file and the known_hosts file were shredded"
        )
        for name, text in bench.sinks().items():
            assert find_secrets(text, [KEY.strip()]) == [], f"{name} leaks the key"


def test_key_file_is_0600_and_gone_after_use_even_on_failure(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    with (
        pytest.raises(RuntimeError, match="boom"),
        key_file(key_dir, KEY, known_hosts_line="h ssh-ed25519 AAAA", tag="t") as ssh,
    ):
        path = Path(ssh.key_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text().startswith(
            "-----BEGIN"
        )
        assert Path(ssh.known_hosts_path).read_text() == "h ssh-ed25519 AAAA\n"
        assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
        raise RuntimeError("boom")
    assert list(key_dir.iterdir()) == []
    with (
        pytest.raises(SshKeyError, match="No pinned host key"),
        key_file(key_dir, KEY, known_hosts_line="", tag="t"),
    ):
        pass


def test_token_pipe_and_askpass_hand_the_token_to_git_only(tmp_path: Path) -> None:
    askpass = install_askpass(tmp_path / "bin")
    assert stat.S_IMODE(askpass.stat().st_mode) == 0o755
    import subprocess
    import sys

    with token_pipe(TOKEN) as (fd, env):
        assert env["SLAS_ASKPASS_FD"] == str(fd) and TOKEN not in json.dumps(env)
        answer = subprocess.run(
            [sys.executable, str(askpass), "Password for 'http://oauth2@127.0.0.1': "],
            env={**env, "PATH": "/usr/bin:/bin"},
            pass_fds=(fd,),
            capture_output=True,
            text=True,
            check=True,
        )
        assert answer.stdout == TOKEN + "\n"
    username = subprocess.run(
        [sys.executable, str(askpass), "Username for 'http://127.0.0.1': "],
        env={
            "SLAS_ASKPASS_FD": "0",
            "SLAS_ASKPASS_USERNAME": "x-access-token",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert username.stdout == "x-access-token\n"
