"""The routes of git-broker (docs/api-contract-round-2.md §7).

The acting person arrives as identity headers (`slas_http.identity`); they own every remote
they see and are the Git identity of every commit made here. Capabilities are checked
where the action executes (CLAUDE.md §11) and again inside `GitBroker`. No response ever
carries a credential: a remote is rendered with `Remote.for_ui()` (fingerprint or last four
characters only), and every sentence that could quote git output passes `redact()`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from slas_authz import AccessDeniedError, Principal, parse_capability
from slas_git.bundle import BundleInfo
from slas_git.credentials import CredentialError
from slas_git.hosts import GitHost, GitHostsError, HostNotAllowedError, hosts_from_mapping
from slas_git.redact import redact
from slas_git.remotes import Remote, RemoteError
from slas_git.workspace import (
    AGENT_TRAILER,
    TICKET_TRAILER,
    GitError,
    GitWorkspace,
    parse_trailers,
)
from slas_git_broker.broker import (
    BrokerGitExec,
    GateRefusedError,
    GitBroker,
    GitBrokerError,
    PushResult,
    git_identity,
)
from slas_git_broker.service.state import BrokerState, HostsFileReadOnlyError
from slas_git_broker.sshkey import SshKeyError
from slas_http.errors import ServiceError
from slas_http.identity import Identity, identity_of, require, require_any
from slas_schemas.errors import ThreePartMessage

router = APIRouter(prefix="/v1")

SLUG: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_SLUG: Final = 64
HISTORY_LIMIT: Final = 50
#: The two hosts CLAUDE.md §5.7 names as needing an ADR (an INV-1 exception) before use.
PUBLIC_HOSTS: Final = ("github.com", "gitlab.com")
API_BASE_BY_KIND: Final = {
    "gitlab": "https://{hostname}/api/v4",
    "gitea": "https://{hostname}/api/v1",
    "github": "https://api.{hostname}",
}

Slug = Annotated[str, PathParam(description="A project slug under Coding/<user>/Projects")]
RemoteId = Annotated[str, PathParam(alias="id")]


# --- bodies --------------------------------------------------------------------------------


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddRemoteBody(Body):
    name: str
    uri: str
    auth_type: Literal["pat", "ssh_key"]
    secret: str
    default_branch: str = "main"


class RotateBody(Body):
    secret: str


class AddHostBody(Body):
    name: str
    hostname: str
    kind: Literal["gitlab", "gitea", "github", "generic"] = "generic"
    ssh_host_key: str = ""
    api_base: str | None = None
    protocols: list[Literal["https", "ssh", "http"]] | None = None
    https_username: str | None = None
    note: str = ""


class CommitBody(Body):
    subject: str = Field(min_length=1, max_length=200)


class PushBody(Body):
    remote_id: str
    branch: str = Field(min_length=1)
    agent_authored: bool = False
    title: str | None = None
    body: str = ""
    target_branch: str | None = None


class PullBody(Body):
    remote_id: str
    branch: str | None = None


class ImportBundleBody(Body):
    file_name: str = Field(min_length=1)


# --- who is acting -------------------------------------------------------------------------


def broker_of(request: Request) -> BrokerState:
    state: BrokerState = request.app.state.broker
    return state


State = Annotated[BrokerState, Depends(broker_of)]
Acting = Annotated[Identity, Depends(identity_of)]


def principal_of(identity: Identity) -> Principal:
    """The broker's principal for the acting person: same subject, same capabilities."""
    capabilities = frozenset(
        capability
        for capability in (parse_capability(name) for name in identity.capabilities)
        if capability is not None
    )
    return Principal(
        subject=identity.user,
        display_name=identity.name,
        role="assigned",
        role_label="assigned",
        capabilities=capabilities,
    )


def check_user(user: str) -> str:
    """The user header names a directory under Coding/; nothing path-like is accepted."""
    if not user or "/" in user or "\\" in user or user.startswith(".") or user != user.strip():
        raise ServiceError(
            400,
            ThreePartMessage(
                "The request did not name a usable person.",
                "The identity header carried something that is not an account name.",
                "Use the platform's own page; if you are writing a script, go through the api.",
            ),
        )
    return user


def check_slug(slug: str) -> str:
    if not SLUG.match(slug) or len(slug) > MAX_SLUG:
        raise ServiceError(
            400,
            ThreePartMessage(
                f"{slug!r} is not a project name the broker accepts.",
                "Project names are lowercase letters, digits and dashes, starting with a letter "
                "or digit, such as bmc-firmware.",
                "Pick the project from the Coding page, or rename it.",
            ),
        )
    return slug


def owner_of(identity: Identity, user: str | None) -> str:
    """Whose project: the acting person's, or another person's when `admin:people` allows."""
    check_user(identity.user)
    if user is None or user == identity.user:
        return identity.user
    require(identity, "admin:people", verb=f"look at the projects of {user}")
    return check_user(user)


# --- errors --------------------------------------------------------------------------------


@contextmanager
def broker_errors() -> Iterator[None]:
    """Every refusal the engine raises, as the three-part answer the contract promises."""
    try:
        yield
    except AccessDeniedError as exc:
        message = exc.decision.message
        assert message is not None  # noqa: S101 — a denied decision always carries one
        raise ServiceError(403, message) from exc
    except HostNotAllowedError as exc:
        raise ServiceError(403, exc.message) from exc
    except GitHostsError as exc:
        raise ServiceError(400, exc.message) from exc
    except RemoteError as exc:
        status = 404 if exc.message.what_happened.startswith("That remote is not one of") else 400
        raise ServiceError(status, exc.message) from exc
    except CredentialError as exc:
        status = 503 if "sealer" in exc.message.what_happened else 409
        raise ServiceError(status, exc.message) from exc
    except GateRefusedError as exc:
        raise ServiceError(409, exc.message) from exc
    except (GitBrokerError, GitError, SshKeyError) as exc:
        raise ServiceError(409, exc.message) from exc


# --- views ---------------------------------------------------------------------------------


def remote_view(remote: Remote) -> dict[str, object]:
    return {**remote.for_ui(), "sentence": remote.sentence()}


def host_view(host: GitHost) -> dict[str, object]:
    return {
        **host.model_dump(mode="json"),
        "ssh_host_key_pinned": bool(host.ssh_host_key),
        "sentence": host.sentence(),
    }


def commit_view(sha: str, author: str, when: str, subject: str, body: str) -> dict[str, object]:
    trailers = parse_trailers(body)
    by_agent = AGENT_TRAILER in trailers
    ticket_id = trailers.get(TICKET_TRAILER)
    who = author
    if by_agent:
        who = f"the {trailers[AGENT_TRAILER].capitalize()} Agent"
        if ticket_id:
            who += f" for {ticket_id}"
    return {
        "sha": sha,
        "subject": subject,
        "author": author,
        "when": when,
        "by_agent": by_agent,
        "ticket_id": ticket_id,
        "sentence": f"{sha[:10]} {subject} — by {who}.",
    }


def push_view(result: PushResult) -> dict[str, object]:
    merge_request = result.merge_request
    return {
        "ok": True,
        "branch": result.branch,
        "sha": result.sha,
        "sentence": result.sentence,
        "merge_request": merge_request.model_dump(mode="json") if merge_request else None,
        "gate": [check.model_dump(mode="json") for check in result.gate.checks],
        "review_url": merge_request.url if merge_request else None,
    }


def bundle_view(info: BundleInfo) -> dict[str, object]:
    return {
        **info.model_dump(mode="json"),
        "file_name": Path(info.path).name,
        "sentence": info.sentence(),
    }


# --- the workspace of a project --------------------------------------------------------------


def project_workspace(broker: GitBroker, owner: str, slug: str, identity: Identity) -> GitWorkspace:
    """The person's repository under Coding/<owner>/Projects/<slug>, initialised if the
    directory exists without one (project creation, CLAUDE.md §5.7 Method 2)."""
    path = broker.project_dir(owner, slug)
    if not path.is_dir():
        raise ServiceError(
            404,
            ThreePartMessage(
                f"There is no project called {slug}.",
                "No coding task or clone created it, or it belongs to someone else.",
                "Start a coding task or clone a remote into it first.",
            ),
        )
    workspace = GitWorkspace(
        BrokerGitExec(broker.runner, extra_env={}, pass_fds=()),
        cwd=str(path),
        identity=git_identity(identity.user, identity.name),
    )
    workspace.init()
    return workspace


def current_branch(workspace: GitWorkspace) -> str:
    """The checked-out branch, also on an unborn one; `HEAD` when detached."""
    result = workspace.git("symbolic-ref", "--short", "-q", "HEAD", check=False)
    return result.stdout.strip() if result.exit_code == 0 and result.stdout.strip() else "HEAD"


def history(workspace: GitWorkspace, limit: int = HISTORY_LIMIT) -> list[dict[str, object]]:
    if workspace.head_sha() is None:
        return []
    raw = workspace.git(
        "log", f"--max-count={limit}", "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    ).stdout
    commits: list[dict[str, object]] = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        sha, author, when, subject, body = [*record.strip("\n").split("\x1f"), "", "", "", ""][:5]
        commits.append(commit_view(sha.strip(), author, when, subject, body))
    return commits


# --- remotes -------------------------------------------------------------------------------


@router.get("/remotes")
def list_remotes(identity: Acting, state: State) -> list[dict[str, object]]:
    owner = check_user(identity.user)
    return [remote_view(remote) for remote in state.broker.remotes.list_for(owner)]


@router.post("/remotes", status_code=201)
def add_remote(body: AddRemoteBody, identity: Acting, state: State) -> dict[str, object]:
    require(identity, "git:remote_manage", verb="add a Git remote")
    check_user(identity.user)
    with broker_errors():
        remote = state.broker.add_remote(
            principal_of(identity),
            name=body.name,
            uri=body.uri,
            auth_type=body.auth_type,
            secret=body.secret,
            default_branch=body.default_branch,
        )
    state.log.info(
        "git.remote_added",
        user=identity.user,
        remote_id=remote.id,
        host=remote.host,
        auth_type=remote.auth_type,
    )
    return remote_view(remote)


@router.post("/remotes/{id}/rotate")
def rotate_remote(
    remote_id: RemoteId, body: RotateBody, identity: Acting, state: State
) -> dict[str, object]:
    require(identity, "git:remote_manage", verb="rotate a Git credential")
    check_user(identity.user)
    with broker_errors():
        remote = state.broker.rotate_credential(principal_of(identity), remote_id, body.secret)
    state.log.info("git.credential_rotated", user=identity.user, remote_id=remote.id)
    return remote_view(remote)


@router.delete("/remotes/{id}")
def delete_remote(remote_id: RemoteId, identity: Acting, state: State) -> dict[str, object]:
    require(identity, "git:remote_manage", verb="delete a Git remote")
    check_user(identity.user)
    with broker_errors():
        sentence = state.broker.delete_remote(principal_of(identity), remote_id)
    state.log.info("git.remote_deleted", user=identity.user, remote_id=remote_id)
    return {"sentence": sentence}


@router.post("/remotes/{id}/test")
def test_remote(remote_id: RemoteId, identity: Acting, state: State) -> dict[str, object]:
    require_any(identity, ("git:clone", "git:pull"), verb="test a Git remote")
    check_user(identity.user)
    with broker_errors():
        report = state.broker.test_connection(principal_of(identity), remote_id)
    state.log.info("git.ls_remote", user=identity.user, remote_id=remote_id, ok=report.ok)
    return {"ok": report.ok, "branches": report.branches, "sentence": redact(report.sentence)}


# --- hosts ---------------------------------------------------------------------------------


@router.get("/hosts")
def list_hosts(identity: Acting, state: State) -> list[dict[str, object]]:
    return [host_view(host) for host in state.broker.hosts.hosts]


@router.post("/hosts", status_code=201)
def add_host(body: AddHostBody, identity: Acting, state: State) -> dict[str, object]:
    require(identity, "git:hosts_manage", verb="change the allowlist of Git hosts")
    hostname = body.hostname.strip().lower()
    if hostname in PUBLIC_HOSTS or any(hostname.endswith(f".{name}") for name in PUBLIC_HOSTS):
        raise ServiceError(
            403,
            ThreePartMessage(
                f"{hostname} is a public host and needs an ADR before it can be allowed.",
                "Reaching a public Git host is an exception to INV-1 (no external network "
                "dependency), and CLAUDE.md §5.7 asks for an ADR per host.",
                "Write the ADR under docs/adr, then add the host to config/git-hosts.yaml.",
            ),
        )
    key = body.ssh_host_key.strip()
    protocols = body.protocols or (["https", "ssh"] if key else ["https"])
    entry: dict[str, object] = {
        "name": body.name.strip(),
        "hostname": hostname,
        "kind": body.kind,
        "protocols": protocols,
        "note": body.note,
    }
    template = API_BASE_BY_KIND.get(body.kind)
    api_base = body.api_base or (template.format(hostname=hostname) if template else None)
    if api_base:
        entry["api_base"] = api_base
    if key:
        entry["ssh_host_key"] = key
    if body.https_username:
        entry["https_username"] = body.https_username
    current = state.broker.hosts
    with broker_errors():
        hosts = hosts_from_mapping(
            {
                "version": current.version,
                "hosts": [*(host.model_dump(mode="json") for host in current.hosts), entry],
            },
            source="Admin → Git hosts",
        )
    try:
        state.hosts_file.save(hosts, hostname=hostname)
    except HostsFileReadOnlyError as exc:
        raise ServiceError(409, exc.message) from exc
    state.replace_hosts(hosts)
    host = hosts.find(hostname)
    assert host is not None  # noqa: S101 — the entry was just validated into the list
    state.log.info("git.host_added", user=identity.user, hostname=hostname, kind=host.kind)
    return host_view(host)


# --- projects: local git -------------------------------------------------------------------


@router.get("/projects/{slug}/status")
def project_status(
    slug: Slug,
    identity: Acting,
    state: State,
    user: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    owner = owner_of(identity, user)
    check_slug(slug)
    with broker_errors():
        workspace = project_workspace(state.broker, owner, slug, identity)
        branch = current_branch(workspace)
        entries = [{"path": path, "state": code} for code, path in workspace.status()]
    count = len(entries)
    sentence = (
        f"On {branch}; nothing to commit."
        if not entries
        else f"On {branch} with {count} changed {'file' if count == 1 else 'files'}."
    )
    return {"branch": branch, "entries": entries, "sentence": sentence}


@router.post("/projects/{slug}/commit", status_code=201)
def project_commit(
    slug: Slug, body: CommitBody, identity: Acting, state: State
) -> dict[str, object]:
    owner = owner_of(identity, None)
    check_slug(slug)
    with broker_errors():
        workspace = project_workspace(state.broker, owner, slug, identity)
        workspace.add_all()
        if not workspace.has_changes():
            raise ServiceError(
                409,
                ThreePartMessage(
                    f"There is nothing to commit in {slug}.",
                    "No file changed since the last commit.",
                    "Edit files in the Terminal or let the agent work, then commit again.",
                ),
            )
        sha = workspace.commit(body.subject.strip())
        latest = history(workspace, limit=1)
    state.log.info("git.commit", user=identity.user, slug=slug, sha=sha)
    return latest[0]


@router.get("/projects/{slug}/history")
def project_history(
    slug: Slug,
    identity: Acting,
    state: State,
    user: Annotated[str | None, Query()] = None,
) -> list[dict[str, object]]:
    owner = owner_of(identity, user)
    check_slug(slug)
    with broker_errors():
        workspace = project_workspace(state.broker, owner, slug, identity)
        return history(workspace)


# --- projects: through the broker -----------------------------------------------------------


@router.post("/projects/{slug}/push")
def project_push(slug: Slug, body: PushBody, identity: Acting, state: State) -> dict[str, object]:
    require(identity, "git:push_branch", verb="push a branch")
    owner = owner_of(identity, None)
    check_slug(slug)
    principal = principal_of(identity)
    with broker_errors():
        remote = state.broker.remotes.get(body.remote_id, owner=owner)
        if body.branch == remote.default_branch:
            require(
                identity,
                "git:push_protected",
                verb=f"push directly to the protected branch {remote.default_branch}",
            )
        project_workspace(state.broker, owner, slug, identity)
        result = state.broker.push_branch(
            principal,
            body.remote_id,
            slug,
            body.branch,
            agent_authored=body.agent_authored,
            title=body.title,
            body=body.body,
            target_branch=body.target_branch,
        )
    state.log.info(
        "git.push",
        user=identity.user,
        slug=slug,
        remote_id=body.remote_id,
        branch=result.branch,
        sha=result.sha,
        review_url=result.merge_request.url if result.merge_request else None,
    )
    return push_view(result)


@router.post("/projects/{slug}/pull")
def project_pull(slug: Slug, body: PullBody, identity: Acting, state: State) -> dict[str, object]:
    require(identity, "git:pull", verb="pull from a Git remote")
    owner = owner_of(identity, None)
    check_slug(slug)
    with broker_errors():
        project_workspace(state.broker, owner, slug, identity)
        sentence = state.broker.pull(principal_of(identity), body.remote_id, slug, body.branch)
    state.log.info("git.pull", user=identity.user, slug=slug, remote_id=body.remote_id)
    return {"sentence": redact(sentence)}


@router.post("/projects/{slug}/bundle/export", status_code=201)
def bundle_export(slug: Slug, identity: Acting, state: State) -> dict[str, object]:
    require(identity, "git:bundle", verb="export a Git bundle")
    owner = owner_of(identity, None)
    check_slug(slug)
    with broker_errors():
        project_workspace(state.broker, owner, slug, identity)
        info = state.broker.export_bundle(principal_of(identity), slug)
    state.log.info("git.bundle_export", user=identity.user, slug=slug, file=Path(info.path).name)
    return bundle_view(info)


@router.post("/projects/{slug}/bundle/import")
def bundle_import(
    slug: Slug, body: ImportBundleBody, identity: Acting, state: State
) -> dict[str, object]:
    require(identity, "git:bundle", verb="import a Git bundle")
    owner = owner_of(identity, None)
    check_slug(slug)
    name = body.file_name.strip()
    if Path(name).name != name or name.startswith(".") or not name.endswith(".bundle"):
        raise ServiceError(
            400,
            ThreePartMessage(
                f"{name!r} is not a bundle file name.",
                "A bundle is a .bundle file placed in your Bundles folder; folders are not "
                "accepted here.",
                "Copy the file into Bundles/ and give its name only.",
            ),
        )
    path = state.broker.bundles_dir(owner) / name
    if not path.is_file():
        raise ServiceError(
            404,
            ThreePartMessage(
                f"There is no bundle called {name} in your Bundles folder.",
                "It was not copied there yet, or it has another name.",
                f"Copy the file to Coding/{owner}/Bundles/ on the host, then import it again.",
            ),
        )
    with broker_errors():
        branches = state.broker.import_bundle(principal_of(identity), slug, path)
    count = len(branches)
    sentences = [
        f"Imported {count} {'branch' if count == 1 else 'branches'} from {name} into {slug}: "
        + (", ".join(branches) if branches else "none")
        + ".",
        "Nothing was merged; the branches are under bundle/<name> until you merge them in the "
        "Terminal.",
    ]
    state.log.info("git.bundle_import", user=identity.user, slug=slug, file=name, branches=count)
    return {"branches": branches, "sentences": sentences}
