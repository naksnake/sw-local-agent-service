"""The Git host allowlist (CLAUDE.md §5.7 egress): `config/git-hosts.yaml`, rendered from code.

`git-broker` is the only member of the `slas-git` network and may reach only the hosts
listed here. Quickstart ships the two internal hosts; `github.com` and `gitlab.com` need an
ADR (an INV-1 exception) and a per-host entry. A remote whose host is not listed fails with
one plain sentence, and a URI that carries a credential is refused before anything else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage

HostKind = Literal["gitlab", "gitea", "github", "generic"]
#: `http` exists only for a loopback host (the fake Git server in tests); real hosts use
#: https or ssh, and the validators below refuse `http` anywhere else.
Protocol = Literal["https", "ssh", "http"]
LOOPBACK: Final = ("127.0.0.1", "localhost", "::1")
DEFAULT_PROTOCOLS: Final[tuple[Protocol, ...]] = ("https", "ssh")

_SCP_LIKE = re.compile(r"^(?P<user>[\w.-]+)@(?P<host>[\w.-]+):(?P<path>[\w./-]+?)(?:\.git)?/?$")
_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")


class GitHost(SlasModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    hostname: str = Field(min_length=1)
    kind: HostKind = "generic"
    #: Base URL of the host's REST API, when the kind is known (MR/PR creation).
    api_base: str | None = None
    protocols: list[Protocol] = Field(default_factory=lambda: list(DEFAULT_PROTOCOLS))
    #: One `known_hosts` line, pinned at configuration time; SSH is refused without it.
    ssh_host_key: str | None = None
    #: The account name git sends with a token over HTTPS (the token is never in the URL).
    https_username: str = "oauth2"
    note: str = ""

    @model_validator(mode="after")
    def _shape(self) -> GitHost:
        if not _HOSTNAME.match(self.hostname.lower()):
            raise ValueError(f"{self.hostname!r} is not a hostname")
        if not self.protocols:
            raise ValueError(f"{self.name}: at least one protocol must be allowed")
        loopback = self.hostname.lower() in LOOPBACK
        if "http" in self.protocols and not loopback:
            raise ValueError(f"{self.name}: plain http is only allowed for a loopback host")
        if self.api_base is not None and not (
            self.api_base.startswith("https://")
            or (loopback and self.api_base.startswith("http://"))
        ):
            raise ValueError(f"{self.name}: api_base must be an https URL")
        return self

    def sentence(self) -> str:
        protocols = " and ".join(self.protocols)
        kind = {"gitlab": "GitLab", "gitea": "Gitea", "github": "GitHub", "generic": "plain Git"}[
            self.kind
        ]
        return f"{self.hostname}: {kind} over {protocols}."


class GitHosts(SlasModel):
    version: int = 1
    hosts: list[GitHost]

    @model_validator(mode="after")
    def _unique(self) -> GitHosts:
        seen: set[str] = set()
        for host in self.hosts:
            for key in (host.name, host.hostname.lower()):
                if key in seen:
                    raise ValueError(f"host {key!r} is listed twice")
                seen.add(key)
        return self

    def allowed_names(self) -> list[str]:
        return [host.hostname for host in self.hosts]

    def find(self, hostname: str) -> GitHost | None:
        wanted = hostname.lower()
        for host in self.hosts:
            if host.hostname.lower() == wanted:
                return host
        return None


class GitHostsError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class HostNotAllowedError(PermissionError):
    def __init__(self, hostname: str, hosts: GitHosts) -> None:
        allowed = ", ".join(hosts.allowed_names()) or "none"
        self.message = ThreePartMessage(
            f"{hostname} is not an allowed Git host. Allowed: {allowed}.",
            "git-broker reaches only the hosts an administrator listed under Admin → Git hosts "
            "(config/git-hosts.yaml); a public host needs an ADR first.",
            "Use a listed host, or ask an administrator to add this one.",
        )
        super().__init__(self.message.what_happened)
        self.hostname = hostname


class RemoteUri(SlasModel):
    """A parsed remote address; never carries a credential."""

    scheme: Protocol
    hostname: str
    port: int | None = None
    #: `group/project` without a leading slash or `.git`.
    path: str
    ssh_user: str | None = None

    @property
    def netloc(self) -> str:
        return f"{self.hostname}:{self.port}" if self.port else self.hostname

    def https_url(self, username: str | None = None) -> str:
        """The web address git is given; `username` is an account name, never a secret."""
        who = f"{username}@" if username else ""
        scheme = "http" if self.scheme == "http" else "https"
        return f"{scheme}://{who}{self.netloc}/{self.path}.git"

    def ssh_url(self) -> str:
        return f"ssh://{self.ssh_user or 'git'}@{self.netloc}/{self.path}.git"

    @property
    def owner_and_repo(self) -> tuple[str, str]:
        head, _, tail = self.path.rpartition("/")
        return head, tail


def parse_remote_uri(uri: str) -> RemoteUri:
    text = uri.strip()
    scp = _SCP_LIKE.match(text)
    if scp and "://" not in text:
        return RemoteUri(
            scheme="ssh",
            hostname=scp.group("host").lower(),
            path=scp.group("path").strip("/"),
            ssh_user=scp.group("user"),
        )
    parts = urlsplit(text)
    plain_http_ok = parts.scheme == "http" and (parts.hostname or "").lower() in LOOPBACK
    if (parts.scheme not in ("https", "ssh") and not plain_http_ok) or not parts.hostname:
        raise GitHostsError(
            ThreePartMessage(
                f"{uri!r} is not a Git address the broker accepts.",
                "Use https://host/group/project.git, ssh://git@host/group/project.git or "
                "git@host:group/project.git.",
                "Fix the address.",
            )
        )
    if parts.password is not None or (parts.scheme != "ssh" and parts.username):
        raise GitHostsError(
            ThreePartMessage(
                "The address carries a credential.",
                "Tokens never go in a URL; the broker injects them at dispatch (INV-14).",
                "Remove the user:token@ part and paste the token in its own field.",
            )
        )
    path = parts.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or "/" not in path:
        raise GitHostsError(
            ThreePartMessage(
                f"{uri!r} has no group/project path.",
                "A remote names a repository, for example gitlab.internal/firmware/bmc.git.",
                "Fix the address.",
            )
        )
    return RemoteUri(
        scheme=parts.scheme,
        hostname=parts.hostname.lower(),
        port=parts.port,
        path=path,
        ssh_user=parts.username if parts.scheme == "ssh" else None,
    )


def host_for(uri: RemoteUri, hosts: GitHosts) -> GitHost:
    host = hosts.find(uri.hostname)
    if host is None:
        raise HostNotAllowedError(uri.hostname, hosts)
    if uri.scheme not in host.protocols:
        raise GitHostsError(
            ThreePartMessage(
                f"{uri.hostname} does not allow {uri.scheme}.",
                f"Admin → Git hosts allows {' and '.join(host.protocols)} for this host.",
                "Use the allowed protocol, or ask an administrator to change the host entry.",
            )
        )
    if uri.scheme == "ssh" and not host.ssh_host_key:
        raise GitHostsError(
            ThreePartMessage(
                f"{uri.hostname} has no pinned SSH host key.",
                "SSH is only used with StrictHostKeyChecking against a key an administrator "
                "pinned (CLAUDE.md §5.7).",
                "Ask an administrator to add the host key under Admin → Git hosts, or use https.",
            )
        )
    return host


def hosts_from_mapping(data: object, *, source: str = "<memory>") -> GitHosts:
    try:
        return GitHosts.model_validate(data)
    except ValidationError as exc:
        raise GitHostsError(
            ThreePartMessage(
                f"The Git host allowlist in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; every host needs a name, a hostname and at least one protocol.",
            )
        ) from exc


DEFAULT_GIT_HOSTS: Final[dict[str, object]] = {
    "version": 1,
    "hosts": [
        {
            "name": "gitlab-internal",
            "hostname": "gitlab.internal",
            "kind": "gitlab",
            "api_base": "https://gitlab.internal/api/v4",
            "protocols": ["https", "ssh"],
            "https_username": "oauth2",
            "note": "Quickstart default. Pin ssh_host_key before allowing ssh.",
        },
        {
            "name": "gitea-internal",
            "hostname": "gitea.internal",
            "kind": "gitea",
            "api_base": "https://gitea.internal/api/v1",
            "protocols": ["https"],
            "https_username": "git",
            "note": "Quickstart default.",
        },
    ],
}

GIT_HOSTS_FILE_HEADER: Final = (
    "Git host allowlist for SW Local Agent Service (CLAUDE.md §5.7).\n"
    "Rendered from slas_git.hosts.DEFAULT_GIT_HOSTS; a unit test keeps file and code in step.\n"
    "git-broker is the only member of the slas-git network and reaches only these hosts.\n"
    "github.com or gitlab.com need an ADR (INV-1 exception) before they are added here."
)


def default_hosts() -> GitHosts:
    return hosts_from_mapping(DEFAULT_GIT_HOSTS, source="config/git-hosts.yaml")


def render_git_hosts_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    hosts = hosts_from_mapping(data)
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {hosts.version}")
    lines.append("hosts:")
    for host in hosts.hosts:
        lines.append(f"  - name: {host.name}")
        lines.append(f"    hostname: {host.hostname}")
        lines.append(f"    kind: {host.kind}")
        if host.api_base:
            lines.append(f"    api_base: {json.dumps(host.api_base)}")
        lines.append(f"    protocols: [{', '.join(host.protocols)}]")
        lines.append(f"    https_username: {host.https_username}")
        if host.ssh_host_key:
            lines.append(f"    ssh_host_key: {json.dumps(host.ssh_host_key)}")
        if host.note:
            lines.append(f"    note: {json.dumps(host.note, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"
