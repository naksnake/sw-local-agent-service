"""git-broker over HTTP (docs/api-contract-round-2.md §7) against the fake Git host.

The same wiring as `test_git_broker_flow.Bench`, behind `create_app()` and FastAPI's
TestClient: remotes (the token never in a body), hosts (rw and read-only file), a project's
status/commit/history with the person's identity, push through the gate to the fake host
and its merge request, a refused push in three parts, pull, bundles, slug and ownership
refusals, capability refusals, the route table against the contract, and the CLI. At the end
every response body, every log line and every file is grepped for the secrets the test used.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from slas_git.credentials import CredentialError, FakeSealer
from slas_git.hostapi import UrllibHttpClient
from slas_git.hosts import GitHost, GitHosts
from slas_git.redact import find_secrets, redact
from slas_git.workspace import GitWorkspace, LocalGitExec, agent_trailers
from slas_git.workspace import Identity as GitIdentity
from slas_git_broker import cli
from slas_git_broker.broker import git_identity
from slas_git_broker.runner import LocalProcessRunner
from slas_git_broker.service.app import (
    UnavailableSealer,
    build_broker,
    build_sealer,
    create_app,
    create_app_from_settings,
    route_table,
)
from slas_git_broker.service.settings import Settings
from slas_git_broker.service.state import HostsFile, HostsFileReadOnlyError
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.rca import FakeCrossChecker
from slas_observability.events import EventLog, ListSink
from slas_schemas.errors import ThreePartMessage
from slas_schemas.vote import Vote

from .fake_git_server import FakeGitHost

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REPO_ROOT = Path(__file__).resolve().parents[2]
THREE_PARTS = ("what_happened", "likely_cause", "what_to_do", "trace_id")
TOKEN = "glpat-Zq8xw2Yv7Rt4Ks9Lm3Np6Bd"  # a fake token for the fake server
ROTATED = "glpat-Rot4t3dRot4t3dRot4t3dQ"
SECRET_KEY = "k" * 43
ENGINEER = Identity(
    "pat@example.com",
    "Pat Lin",
    frozenset({"git:remote_manage", "git:clone", "git:pull", "git:push_branch", "git:bundle"}),
)
LEAD = Identity("pat@example.com", "Pat Lin", ENGINEER.capabilities | {"git:push_protected"})
VIEWER = Identity("vic@example.com", "Vic", frozenset())
ADMIN = Identity("ada@example.com", "Ada Ops", frozenset({"admin:people", "git:hosts_manage"}))


def fake_hosts(server: FakeGitHost) -> GitHosts:
    return GitHosts(
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


class ServiceBench:
    """The broker behind `create_app()`, on a temporary data root, talking to the fake host."""

    def __init__(self, tmp_path: Path, server: FakeGitHost) -> None:
        self.tmp = tmp_path
        self.server = server
        self.settings = Settings(
            data_root=tmp_path / "data",
            secrets_dir=tmp_path / "secrets",
            hosts_file=tmp_path / "etc" / "git-hosts.yaml",
            key_dir=tmp_path / "run" / "slas-keys",
            sealer="fake-for-tests",
        )
        self.settings.secrets_dir.mkdir(parents=True)
        (self.settings.secrets_dir / "secret_key").write_text(SECRET_KEY + "\n", encoding="utf-8")
        self.hosts_file = HostsFile(self.settings.hosts_file)
        self.hosts_file.path.parent.mkdir(parents=True)
        self.hosts_file.save(fake_hosts(server))
        self.runner = LocalProcessRunner()
        self.clock = FakeClock(datetime(2026, 9, 17, 9, tzinfo=UTC))
        self.checker = FakeCrossChecker(
            [Vote(voter=f"v{i}", verdict="approve", reason="ok", confidence=0.9) for i in range(3)],
            agreed=True,
        )
        self.broker = build_broker(
            self.settings,
            hosts=self.hosts_file.load(),
            sealer=build_sealer("fake-for-tests", SECRET_KEY),
            runner=self.runner,
            http=UrllibHttpClient(),
            clock=self.clock,
            cross_checker=self.checker,
        )
        self.sink = ListSink()
        self.log = EventLog("git-broker", self.sink, redact=redact)
        self.app = create_app(broker=self.broker, hosts=self.hosts_file, log=self.log)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.bodies: list[str] = []

    def call(
        self, method: str, path: str, identity: Identity | None = None, body: Any = None
    ) -> Any:
        headers = identity.headers() if identity is not None else {}
        response = self.client.request(method, path, json=body, headers=headers)
        self.bodies.append(response.text)
        return response

    def repo_url(self, path: str = "firmware/bmc") -> str:
        return f"{self.server.state.base_url}/{path}.git"

    def project(self, user: str, slug: str) -> Path:
        return self.broker.project_dir(user, slug)

    def workspace(self, user: str, slug: str, name: str = "Pat Lin") -> GitWorkspace:
        return GitWorkspace(
            LocalGitExec(), cwd=str(self.project(user, slug)), identity=git_identity(user, name)
        )

    def sinks(self) -> dict[str, str]:
        """Every place a credential could have leaked to, as text."""
        out = {
            "response bodies": "\n".join(self.bodies),
            "log lines": "\n".join(self.sink.lines),
            "process argv": "\n".join(" ".join(argv) for argv in self.runner.argv_seen),
            "process env": json.dumps(self.runner.env_seen),
            "server access log": "\n".join(self.server.state.access_log),
            "server api bodies": json.dumps(self.server.state.api_calls),
        }
        for name in ("audit.jsonl", "remotes.json", "credentials.json"):
            path = self.settings.broker_dir / name
            out[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        for config in self.settings.data_root.rglob(".git/config"):
            out[f"workspace config {config.parent.parent.name}"] = config.read_text(
                encoding="utf-8"
            )
        return out

    def assert_clean(self, *secrets: str) -> None:
        for name, text in self.sinks().items():
            assert find_secrets(text, [TOKEN, ROTATED, *secrets]) == [], f"{name} leaks a secret"


def problem(response: Any, status: int) -> dict[str, str]:
    assert response.status_code == status, response.text
    body = response.json()
    assert tuple(body) == THREE_PARTS and all(body[key] for key in THREE_PARTS)
    return dict(body)


@pytest.fixture
def bench(tmp_path: Path) -> Any:
    with FakeGitHost(tmp_path / "server", token=TOKEN) as server:
        server.create_repo("firmware/bmc")
        yield ServiceBench(tmp_path, server)


# --- the contract ---------------------------------------------------------------------------


def test_route_table_matches_the_contract() -> None:
    contract = (REPO_ROOT / "docs" / "api-contract-round-2.md").read_text(encoding="utf-8")
    section = contract.split("## 7. git-broker", 1)[1].split("\n## 8.", 1)[0]
    documented = set(re.findall(r"`(GET|POST|PUT|DELETE) (/[^\s`]+)`", section))
    assert documented, "the contract names its routes in backticks"
    assert set(route_table()) == documented | {("GET", "/health"), ("GET", "/metrics")}
    assert route_table() == sorted(route_table())


def test_health_names_its_checks_and_metrics_render(bench: ServiceBench) -> None:
    health = bench.call("GET", "/health")
    assert health.status_code == 200
    assert health.json() == {
        "service": "git-broker",
        "ok": True,
        "checks": {"git": "ok", "store": "ok", "sealer": "ok"},
    }
    assert bench.call("GET", "/metrics").status_code == 200
    assert bench.settings.broker_dir.is_dir()


# --- remotes --------------------------------------------------------------------------------


def test_remotes_add_list_test_rotate_delete_without_echoing_the_secret(
    bench: ServiceBench,
) -> None:
    assert problem(bench.call("GET", "/v1/remotes"), 401)["what_happened"].startswith(
        "The request did not say who is acting."
    )
    body = {"name": "gitlab-firmware", "uri": bench.repo_url(), "auth_type": "pat", "secret": TOKEN}
    refused = problem(bench.call("POST", "/v1/remotes", VIEWER, body), 403)
    assert refused["what_happened"] == "Vic may not add a Git remote."

    created = bench.call("POST", "/v1/remotes", ENGINEER, body)
    assert created.status_code == 201, created.text
    remote = created.json()
    assert set(remote) == {
        "id",
        "name",
        "uri",
        "host",
        "auth_type",
        "fingerprint",
        "default_branch",
        "last_used",
        "expires_at",
        "sentence",
    }
    assert remote["fingerprint"] == f"…{TOKEN[-4:]}" and remote["last_used"] is None
    assert remote["sentence"] == (
        f"gitlab-firmware: {bench.repo_url()}, token …p6Bd, default branch main."
    )
    assert TOKEN not in created.text and "credential_ref" not in created.text
    remote_id = remote["id"]

    listed = bench.call("GET", "/v1/remotes", ENGINEER)
    assert [row["id"] for row in listed.json()] == [remote_id]
    assert bench.call("GET", "/v1/remotes", VIEWER).json() == [], "a person sees only their own"

    duplicate = problem(bench.call("POST", "/v1/remotes", ENGINEER, body), 400)
    assert duplicate["what_happened"] == "You already have a remote called gitlab-firmware."
    github = problem(
        bench.call(
            "POST",
            "/v1/remotes",
            ENGINEER,
            {**body, "name": "gh", "uri": "https://github.com/octo/repo.git"},
        ),
        403,
    )
    assert github["what_happened"] == (
        "github.com is not an allowed Git host. Allowed: 127.0.0.1, ssh.internal."
    )

    assert (
        problem(bench.call("POST", f"/v1/remotes/{remote_id}/test", VIEWER, {}), 403)[
            "what_happened"
        ]
        == "Vic may not test a Git remote."
    )
    tested = bench.call("POST", f"/v1/remotes/{remote_id}/test", ENGINEER, {})
    assert tested.status_code == 200 and tested.json() == {
        "ok": True,
        "branches": ["main"],
        "sentence": "Connected to gitlab-firmware: 1 branch, including main.",
    }
    assert bench.call("GET", "/v1/remotes", ENGINEER).json()[0]["last_used"] is not None
    trace = tested.headers["X-Slas-Trace-Id"]
    rows = bench.broker.audit.read_all()
    assert [row.op for row in rows] == ["add_remote", "ls_remote"]
    assert rows[-1].trace_id == trace and rows[0].trace_id is not None

    rotated = bench.call("POST", f"/v1/remotes/{remote_id}/rotate", ENGINEER, {"secret": ROTATED})
    assert rotated.status_code == 200 and rotated.json()["fingerprint"] == f"…{ROTATED[-4:]}"
    assert ROTATED not in rotated.text
    bad = bench.call("POST", f"/v1/remotes/{remote_id}/test", ENGINEER, {})
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert bad.json()["sentence"].startswith(
        "gitlab-firmware did not accept the credential (…t3dQ)"
    )
    assert problem(
        bench.call("POST", f"/v1/remotes/{remote_id}/rotate", VIEWER, {"secret": ROTATED}), 403
    )

    unknown = problem(
        bench.call("POST", "/v1/remotes/rem-0000000000000000/test", ENGINEER, {}), 404
    )
    assert unknown["what_happened"] == "That remote is not one of yours."
    deleted = bench.call("DELETE", f"/v1/remotes/{remote_id}", ENGINEER)
    assert deleted.status_code == 200 and deleted.json() == {
        "sentence": "Removed gitlab-firmware; its credential was deleted with it."
    }
    assert bench.call("GET", "/v1/remotes", ENGINEER).json() == []
    assert problem(bench.call("DELETE", f"/v1/remotes/{remote_id}", ENGINEER), 404)
    assert not (bench.settings.credentials_path.read_text(encoding="utf-8").strip("{}\n ")), (
        "the credential left with the remote"
    )
    events = [json.loads(line)["event"] for line in bench.sink.lines]
    assert events == [
        "git.remote_added",
        "git.ls_remote",
        "git.credential_rotated",
        "git.ls_remote",
        "git.remote_deleted",
    ]
    bench.assert_clean()


def test_a_bad_body_is_read_back_without_the_secret(bench: ServiceBench) -> None:
    response = bench.call(
        "POST",
        "/v1/remotes",
        ENGINEER,
        {"name": "x", "uri": bench.repo_url(), "auth_type": "pat", "secret": TOKEN, "extra": 1},
    )
    body = problem(response, 400)
    assert body["what_happened"].startswith("The request couldn't be read: extra")
    assert TOKEN not in response.text
    bench.assert_clean()


# --- hosts ----------------------------------------------------------------------------------


def test_hosts_list_and_add_through_the_file(bench: ServiceBench) -> None:
    listed = bench.call("GET", "/v1/hosts", VIEWER)
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["hostname"] for row in rows] == ["127.0.0.1", "ssh.internal"]
    assert rows[1]["ssh_host_key_pinned"] is True and rows[0]["ssh_host_key_pinned"] is False
    assert rows[1]["sentence"] == "ssh.internal: plain Git over ssh."
    assert set(rows[0]) == {
        "name",
        "hostname",
        "kind",
        "api_base",
        "protocols",
        "ssh_host_key",
        "https_username",
        "note",
        "ssh_host_key_pinned",
        "sentence",
    }

    entry = {
        "name": "gitea-lab",
        "hostname": "Gitea.Lab.Internal",
        "kind": "gitea",
        "ssh_host_key": "gitea.lab.internal ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE",
    }
    assert problem(bench.call("POST", "/v1/hosts", VIEWER, entry), 403)["what_happened"] == (
        "Vic may not change the allowlist of Git hosts."
    )
    added = bench.call("POST", "/v1/hosts", ADMIN, entry)
    assert added.status_code == 201, added.text
    host = added.json()
    assert host["hostname"] == "gitea.lab.internal" and host["protocols"] == ["https", "ssh"]
    assert host["api_base"] == "https://gitea.lab.internal/api/v1"
    assert host["ssh_host_key_pinned"] is True
    assert host["sentence"] == "gitea.lab.internal: Gitea over https and ssh."
    text = bench.hosts_file.path.read_text(encoding="utf-8")
    assert text.startswith("# Git host allowlist") and "hostname: gitea.lab.internal" in text
    assert [h.hostname for h in bench.hosts_file.load().hosts][-1] == "gitea.lab.internal"
    assert bench.broker.hosts.find("gitea.lab.internal") is not None, "the broker uses it now"

    # The new host is allowed at once: a remote on it seals without touching the network.
    remote = bench.call(
        "POST",
        "/v1/remotes",
        ENGINEER,
        {
            "name": "lab-docs",
            "uri": "https://gitea.lab.internal/docs/sop.git",
            "auth_type": "pat",
            "secret": TOKEN,
        },
    )
    assert remote.status_code == 201 and remote.json()["host"] == "gitea.lab.internal"

    duplicate = problem(bench.call("POST", "/v1/hosts", ADMIN, entry), 400)
    assert "listed twice" in duplicate["likely_cause"]
    public = problem(
        bench.call("POST", "/v1/hosts", ADMIN, {"name": "gh", "hostname": "github.com"}), 403
    )
    assert public["what_happened"] == (
        "github.com is a public host and needs an ADR before it can be allowed."
    )
    bad_name = problem(
        bench.call("POST", "/v1/hosts", ADMIN, {"name": "Bad Name", "hostname": "x.internal"}), 400
    )
    assert (
        bad_name["what_happened"]
        == "The Git host allowlist in Admin → Git hosts could not be used."
    )
    bench.assert_clean()


def test_a_read_only_allowlist_refuses_in_three_parts(bench: ServiceBench) -> None:
    before = bench.broker.hosts
    bench.app.state.broker.hosts_file = HostsFile(bench.tmp / "nowhere" / "git-hosts.yaml")
    refused = problem(
        bench.call("POST", "/v1/hosts", ADMIN, {"name": "gl", "hostname": "gitlab.lab.internal"}),
        409,
    )
    assert refused["what_happened"].startswith("gitlab.lab.internal was not added")
    assert refused["likely_cause"] == (
        "The allowlist is mounted read-only; edit config/git-hosts.yaml on the host."
    )
    assert bench.broker.hosts is before, "nothing changed in memory either"


def test_hosts_file_load_save_and_errors(tmp_path: Path) -> None:
    hosts_file = HostsFile(tmp_path / "absent.yaml")
    assert [h.hostname for h in hosts_file.load().hosts] == ["gitlab.internal", "gitea.internal"]
    assert hosts_file.writable() is True
    (tmp_path / "bad.yaml").write_text("hosts: [\n", encoding="utf-8")
    with pytest.raises(Exception, match="could not be read") as yaml_error:
        HostsFile(tmp_path / "bad.yaml").load()
    assert "not valid YAML" in yaml_error.value.message.likely_cause  # type: ignore[attr-defined]
    (tmp_path / "wrong.yaml").write_text("version: 1\nhosts: []\n", encoding="utf-8")
    assert HostsFile(tmp_path / "wrong.yaml").load().hosts == []
    (tmp_path / "invalid.yaml").write_text("version: 1\nhosts: [{name: 1}]\n", encoding="utf-8")
    with pytest.raises(Exception, match="could not be used"):
        HostsFile(tmp_path / "invalid.yaml").load()
    with pytest.raises(HostsFileReadOnlyError) as ro:
        HostsFile(tmp_path / "missing" / "dir" / "hosts.yaml").save(
            hosts_file.load(), hostname="x.internal"
        )
    assert isinstance(ro.value.__cause__, OSError)
    assert ro.value.message.what_happened.startswith("x.internal was not added")


# --- projects: status, commit, history ------------------------------------------------------


def test_status_commit_history_carry_the_persons_identity(bench: ServiceBench) -> None:
    user = ENGINEER.user
    project = bench.project(user, "demo")
    project.mkdir(parents=True)
    (project / "README.md").write_text("# demo\n", encoding="utf-8")

    status = bench.call("GET", "/v1/projects/demo/status", ENGINEER)
    assert status.status_code == 200, status.text
    assert status.json() == {
        "branch": "main",
        "entries": [{"path": "README.md", "state": "??"}],
        "sentence": "On main with 1 changed file.",
    }
    assert (project / ".git").is_dir(), "the project was initialised for the person"
    assert bench.call("GET", "/v1/projects/demo/history", ENGINEER).json() == []

    committed = bench.call("POST", "/v1/projects/demo/commit", ENGINEER, {"subject": "Add readme"})
    assert committed.status_code == 201, committed.text
    commit = committed.json()
    assert set(commit) == {"sha", "subject", "author", "when", "by_agent", "ticket_id", "sentence"}
    assert commit["subject"] == "Add readme" and commit["author"] == "Pat Lin"
    assert commit["by_agent"] is False and commit["ticket_id"] is None
    assert commit["sentence"] == f"{commit['sha'][:10]} Add readme — by Pat Lin."
    shown = subprocess.run(
        ["git", "-C", str(project), "log", "-1", "--format=%an <%ae>%n%cn <%ce>"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert shown.stdout.splitlines() == ["Pat Lin <pat@example.com>", "Pat Lin <pat@example.com>"]
    nothing = problem(
        bench.call("POST", "/v1/projects/demo/commit", ENGINEER, {"subject": "Again"}), 409
    )
    assert nothing["what_happened"] == "There is nothing to commit in demo."
    assert problem(bench.call("POST", "/v1/projects/demo/commit", ENGINEER, {"subject": ""}), 400)

    # The agent committed in the sandbox with trailers; history tells the two apart.
    workspace = bench.workspace(user, "demo")
    (project / "fan.py").write_text("def rpm() -> int:\n    return 3000\n", encoding="utf-8")
    workspace.add_all()
    agent_sha = workspace.commit(
        "Add fan control", trailers=agent_trailers("coding", "T-coding-0001")
    )
    history = bench.call("GET", "/v1/projects/demo/history", ENGINEER).json()
    assert [row["sha"] for row in history] == [agent_sha, commit["sha"]]
    assert history[0]["by_agent"] is True and history[0]["ticket_id"] == "T-coding-0001"
    assert history[0]["sentence"] == (
        f"{agent_sha[:10]} Add fan control — by the Coding Agent for T-coding-0001."
    )
    assert bench.call("GET", "/v1/projects/demo/status", ENGINEER).json()["sentence"] == (
        "On main; nothing to commit."
    )


def test_slug_ownership_and_identity_refusals(bench: ServiceBench) -> None:
    bench.project(ENGINEER.user, "demo").mkdir(parents=True)
    bad = problem(bench.call("GET", "/v1/projects/Demo_1/status", ENGINEER), 400)
    assert bad["what_happened"] == "'Demo_1' is not a project name the broker accepts."
    assert bad["likely_cause"].startswith("Project names are lowercase letters")
    assert problem(bench.call("GET", "/v1/projects/-x/status", ENGINEER), 400)
    assert problem(bench.call("GET", f"/v1/projects/{'a' * 65}/status", ENGINEER), 400)
    missing = problem(bench.call("GET", "/v1/projects/nope/status", ENGINEER), 404)
    assert missing["what_happened"] == "There is no project called nope."
    assert problem(bench.call("GET", "/v1/projects/demo/status"), 401)

    # Another person's project: refused, unless admin:people, and never for a write.
    other = f"/v1/projects/demo/status?user={ENGINEER.user}"
    assert problem(bench.call("GET", other, VIEWER), 403)["what_happened"] == (
        "Vic may not look at the projects of pat@example.com."
    )
    assert problem(bench.call("GET", "/v1/projects/demo/status", VIEWER), 404)
    assert bench.call("GET", other, ADMIN).status_code == 200
    assert bench.call("GET", f"/v1/projects/demo/history?user={ENGINEER.user}", ADMIN).json() == []
    assert problem(bench.call("POST", "/v1/projects/demo/commit", ADMIN, {"subject": "x"}), 404)

    # A user header that is not an account name never becomes a path.
    odd = Identity("../pat", "Odd", frozenset())
    assert problem(bench.call("GET", "/v1/projects/demo/status", odd), 400)["what_happened"] == (
        "The request did not name a usable person."
    )


# --- projects: push, pull, bundles ----------------------------------------------------------


def test_push_pull_and_bundles_over_the_fake_host(bench: ServiceBench) -> None:
    user = ENGINEER.user
    created = bench.call(
        "POST",
        "/v1/remotes",
        ENGINEER,
        {"name": "gitlab-firmware", "uri": bench.repo_url(), "auth_type": "pat", "secret": TOKEN},
    )
    remote_id = created.json()["id"]
    # No clone route is in the contract (§7); the wizard's clone arrives through the broker.
    from slas_git_broker.service.routes import principal_of

    bench.broker.clone(principal_of(ENGINEER), remote_id, "bmc")
    project = bench.project(user, "bmc")
    assert (project / "README.md").read_text(encoding="utf-8") == "# firmware/bmc\n"
    assert bench.call("GET", "/v1/projects/bmc/status", ENGINEER).json()["branch"] == "main"

    workspace = bench.workspace(user, "bmc")
    workspace.checkout_branch("slas/T-coding-0001")
    (project / "bmc").mkdir()
    (project / "bmc" / "fan.py").write_text(
        "def rpm() -> int:\n    return 3000\n", encoding="utf-8"
    )
    workspace.add_all()
    sha = workspace.commit("Add fan control", trailers=agent_trailers("coding", "T-coding-0001"))

    push_body = {"remote_id": remote_id, "branch": "slas/T-coding-0001", "agent_authored": True}
    assert (
        problem(bench.call("POST", "/v1/projects/bmc/push", VIEWER, push_body), 403)[
            "what_happened"
        ]
        == "Vic may not push a branch."
    )
    pushed = bench.call("POST", "/v1/projects/bmc/push", ENGINEER, {**push_body, "title": "Fan"})
    assert pushed.status_code == 200, pushed.text
    result = pushed.json()
    review_url = f"{bench.server.state.base_url}/-/merge_requests/1"
    assert set(result) == {"ok", "branch", "sha", "sentence", "merge_request", "gate", "review_url"}
    assert result["ok"] is True and result["sha"] == sha and result["review_url"] == review_url
    assert result["merge_request"] == {"url": review_url, "number": 1, "title": "Fan"}
    assert [check["name"] for check in result["gate"]] == [
        "path_scope",
        "hooks",
        "submodules",
        "symlinks",
        "size",
        "lfs",
        "secrets",
        "branch_policy",
        "cross_check",
    ]
    assert all(check["ok"] for check in result["gate"])
    assert all(set(check) == {"name", "ok", "sentence"} for check in result["gate"])
    assert result["sentence"] == (
        f"Pushed slas/T-coding-0001 ({sha[:10]}) to gitlab-firmware. "
        f"Opened a review request: {review_url}"
    )
    assert bench.checker.calls and bench.checker.calls[0][0] == "code_change"
    rows = bench.broker.audit.read_all()
    assert rows[-1].op == "push_branch" and rows[-1].sha == sha
    assert rows[-1].trace_id == pushed.headers["X-Slas-Trace-Id"]

    # A secret in the diff: refused in three parts, nothing reaches the host.
    workspace.checkout_branch("leaky")
    (project / "settings.py").write_text('API = "glpat-abcdefghijklmnopqrst"\n', encoding="utf-8")
    workspace.add_all()
    workspace.commit("Leak")
    refused = problem(
        bench.call(
            "POST", "/v1/projects/bmc/push", ENGINEER, {"remote_id": remote_id, "branch": "leaky"}
        ),
        409,
    )
    assert refused["what_happened"] == "The push was refused by the validation gate."
    assert "settings.py:1 (gitlab_pat)" in refused["likely_cause"]
    assert refused["what_to_do"] == "Fix what the checks name, commit again and push again."
    assert bench.broker.audit.read_all()[-1].result == "refused"
    on_host = subprocess.run(
        [
            "git",
            "-C",
            str(bench.tmp / "server" / "firmware" / "bmc.git"),
            "branch",
            "--list",
            "leaky",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert on_host.stdout.strip() == ""

    # The protected branch needs git:push_protected, checked before anything runs.
    workspace.git("checkout", "--quiet", "main")
    (project / "note.txt").write_text("direct\n", encoding="utf-8")
    workspace.add_all()
    workspace.commit("Direct to main")
    main_body = {"remote_id": remote_id, "branch": "main"}
    protected = problem(bench.call("POST", "/v1/projects/bmc/push", ENGINEER, main_body), 403)
    assert protected["what_happened"] == (
        "Pat Lin may not push directly to the protected branch main."
    )
    direct = bench.call("POST", "/v1/projects/bmc/push", LEAD, main_body)
    assert direct.status_code == 200 and direct.json()["merge_request"] is None
    assert direct.json()["review_url"] is None
    unknown_branch = problem(
        bench.call(
            "POST", "/v1/projects/bmc/push", ENGINEER, {"remote_id": remote_id, "branch": "nope"}
        ),
        409,
    )
    assert unknown_branch["what_happened"] == "bmc has no branch called nope."
    assert problem(
        bench.call(
            "POST",
            "/v1/projects/bmc/push",
            ENGINEER,
            {"remote_id": "rem-0000000000000000", "branch": "main"},
        ),
        404,
    )

    # Pull.
    assert problem(
        bench.call("POST", "/v1/projects/bmc/pull", VIEWER, {"remote_id": remote_id}), 403
    )
    pulled = bench.call("POST", "/v1/projects/bmc/pull", ENGINEER, {"remote_id": remote_id})
    assert pulled.status_code == 200, pulled.text
    assert set(pulled.json()) == {"sentence"}
    assert pulled.json()["sentence"].startswith("Pulled main from gitlab-firmware; bmc is at ")

    # Bundles: out of one project, into another.
    assert problem(bench.call("POST", "/v1/projects/bmc/bundle/export", VIEWER, {}), 403)
    exported = bench.call("POST", "/v1/projects/bmc/bundle/export", ENGINEER, {})
    assert exported.status_code == 201, exported.text
    bundle = exported.json()
    assert set(bundle) == {"path", "sha256", "size_bytes", "refs", "file_name", "sentence"}
    assert Path(bundle["path"]).parent == bench.settings.data_root / "Coding" / user / "Bundles"
    assert bundle["file_name"].startswith("bmc-2026") and bundle["file_name"].endswith(".bundle")
    assert "refs/heads/main" in bundle["refs"] and bundle["sentence"].startswith("Bundle bmc-")
    imported = bench.call(
        "POST", "/v1/projects/bmc-copy/bundle/import", ENGINEER, {"file_name": bundle["file_name"]}
    )
    assert imported.status_code == 200, imported.text
    assert sorted(imported.json()["branches"]) == ["leaky", "main", "slas/T-coding-0001"]
    assert imported.json()["sentences"][0].startswith("Imported 3 branches from bmc-")
    assert imported.json()["sentences"][1].startswith("Nothing was merged")
    bad_name = problem(
        bench.call(
            "POST", "/v1/projects/bmc-copy/bundle/import", ENGINEER, {"file_name": "../x.bundle"}
        ),
        400,
    )
    assert bad_name["what_happened"] == "'../x.bundle' is not a bundle file name."
    missing = problem(
        bench.call(
            "POST", "/v1/projects/bmc-copy/bundle/import", ENGINEER, {"file_name": "gone.bundle"}
        ),
        404,
    )
    assert (
        missing["what_happened"] == "There is no bundle called gone.bundle in your Bundles folder."
    )
    ops = [row.op for row in bench.broker.audit.read_all()]
    assert ops[-4:] == ["push_branch", "pull", "bundle_export", "bundle_import"]
    rows = bench.broker.audit.read_all()
    assert all(row.trace_id for row in rows if row.op != "clone"), "every request left its id"
    assert next(row for row in rows if row.op == "clone").trace_id is None, (
        "the clone ran outside a request"
    )

    # The CI grep: token and key shapes in no body, log line, argv, env, file or server log.
    bench.assert_clean("glpat-abcdefghijklmnopqrst")
    assert all(json.loads(line)["trace_id"] for line in bench.sink.lines if "git." in line), (
        "every event carries the request's trace id"
    )


# --- sealer, settings, cli --------------------------------------------------------------------


def test_unavailable_sealer_makes_credential_routes_503_and_health_says_so(
    tmp_path: Path,
) -> None:
    with FakeGitHost(tmp_path / "server", token=TOKEN) as server:
        settings = Settings(data_root=tmp_path / "data", hosts_file=tmp_path / "hosts.yaml")
        hosts_file = HostsFile(settings.hosts_file)
        hosts_file.save(fake_hosts(server))
        sealer = build_sealer("aes-gcm", SECRET_KEY)
        if not isinstance(sealer, UnavailableSealer):  # pragma: no cover — cryptography present
            pytest.skip("the cryptography package is installed here")
        broker = build_broker(settings, hosts=hosts_file.load(), sealer=sealer)
        client = TestClient(
            create_app(broker=broker, hosts=hosts_file), raise_server_exceptions=False
        )
        health = client.get("/health")
        body = problem(health, 503)
        assert body["what_happened"] == "The git-broker is not healthy: sealer did not answer."
        refused = client.post(
            "/v1/remotes",
            json={
                "name": "gl",
                "uri": f"{server.state.base_url}/a/b.git",
                "auth_type": "pat",
                "secret": TOKEN,
            },
            headers=ENGINEER.headers(),
        )
        body = problem(refused, 503)
        assert body["what_happened"] == "The AES-GCM sealer is not available on this host."
        assert TOKEN not in refused.text
        # Projects and hosts still work.
        assert client.get("/v1/hosts", headers=VIEWER.headers()).status_code == 200
        with pytest.raises(CredentialError):
            sealer.open(b"", aad=b"")


def test_settings_from_environ_and_the_secret_key(tmp_path: Path) -> None:
    default = Settings.from_environ({})
    assert default.data_root == Path("/data") and default.hosts_file == Path(
        "/etc/slas/git-hosts.yaml"
    )
    assert default.credentials_path == Path("/data/.git-broker/credentials.json")
    assert default.audit_path == Path("/data/.git-broker/audit.jsonl")
    assert default.askpass_directory == Path("/data/.git-broker/bin")
    assert default.key_dir == Path("/run/slas-keys") and default.bind == "0.0.0.0:8000"
    custom = Settings.from_environ(
        {
            "SLAS_DATA_ROOT": str(tmp_path),
            "SLAS_SECRETS_DIR": str(tmp_path / "s"),
            "GIT_HOSTS_ALLOWLIST": str(tmp_path / "h.yaml"),
            "SLAS_KEY_DIR": str(tmp_path / "keys"),
            "SLAS_ASKPASS_DIR": str(tmp_path / "bin"),
            "SLAS_SEALER": "fake-for-tests",
            "SLAS_CA_BUNDLE": "/etc/ssl/slas-ca.pem",
            "SLAS_GIT_PATH": "/opt/git/bin",
            "SLAS_BIND": "127.0.0.1:9000",
        }
    )
    assert (
        custom.askpass_directory == tmp_path / "bin" and custom.ca_bundle == "/etc/ssl/slas-ca.pem"
    )
    assert custom.git_path == "/opt/git/bin" and custom.bind == "127.0.0.1:9000"
    with pytest.raises(ValueError, match="SLAS_SEALER must be one of"):
        Settings.from_environ({"SLAS_SEALER": "rot13"})
    with pytest.raises(CredentialError) as missing:
        custom.read_secret_key({})
    assert missing.value.message.what_happened == "SLAS_SECRET_KEY is not set for git-broker."
    assert custom.read_secret_key({"SLAS_SECRET_KEY": SECRET_KEY}) == SECRET_KEY
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "secret_key").write_text(SECRET_KEY + "\n", encoding="utf-8")
    assert custom.read_secret_key({}) == SECRET_KEY
    assert isinstance(build_sealer("fake-for-tests", SECRET_KEY), FakeSealer)


def test_create_app_from_settings_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        hosts_file=tmp_path / "absent" / "git-hosts.yaml",
        key_dir=tmp_path / "keys",
        sealer="fake-for-tests",
    )
    settings.secrets_dir.mkdir()
    (settings.secrets_dir / "secret_key").write_text(SECRET_KEY, encoding="utf-8")
    sink = ListSink()
    app = create_app_from_settings(settings, log=EventLog("git-broker", sink))
    events = [json.loads(line)["event"] for line in sink.lines]
    assert events == ["sealer.fake", "hosts.defaults"]
    client = TestClient(app, raise_server_exceptions=False)
    hosts = client.get("/v1/hosts", headers=VIEWER.headers()).json()
    assert [h["hostname"] for h in hosts] == ["gitlab.internal", "gitea.internal"]
    assert (settings.askpass_directory / "slas-askpass").is_file()

    served: list[tuple[Any, str]] = []
    assert (
        cli.main(["serve"], run=lambda app, bind: served.append((app, bind)), settings=settings)
        == 0
    )
    assert served and served[0][1] == "0.0.0.0:8000" and served[0][0].state.service == "git-broker"
    with pytest.raises(SystemExit) as usage:
        cli.parse_args([])
    assert usage.value.code == 2
    assert cli.parse_args(["serve"]).command == "serve"

    no_key = Settings(
        data_root=tmp_path / "d2", secrets_dir=tmp_path / "none", sealer="fake-for-tests"
    )
    capsys.readouterr()  # the first start's own warning event went to stderr
    assert cli.main(["serve"], run=lambda *_: None, settings=no_key, err=sys.stderr) == 1
    assert capsys.readouterr().err.splitlines() == [
        "SLAS_SECRET_KEY is not set for git-broker.",
        "install.sh writes it into the data root's .env; the broker reads it at start.",
        "Run ./install.sh again, or set SLAS_SECRET_KEY in .env and restart git-broker.",
    ]
    (tmp_path / "bad.yaml").write_text("hosts: [\n", encoding="utf-8")
    bad_hosts = Settings(
        data_root=tmp_path / "d3",
        secrets_dir=settings.secrets_dir,
        hosts_file=tmp_path / "bad.yaml",
        sealer="fake-for-tests",
    )
    assert cli.main(["serve"], run=lambda *_: None, settings=bad_hosts) == 1
    assert "could not be read" in capsys.readouterr().err
    out = io.StringIO()
    cli.explain(ThreePartMessage("a", "b", "c"), out)
    assert out.getvalue() == "a\nb\nc\n"


def test_git_identity_uses_the_email_and_falls_back_to_the_subject() -> None:
    assert git_identity("pat@example.com", "Pat Lin") == GitIdentity(
        name="Pat Lin", email="pat@example.com"
    )
    assert git_identity("pat", "") == GitIdentity(name="pat", email="pat@slas.local")
