"""What the routes reach: the broker, the allowlist file and the event log.

`HostsFile` is `config/git-hosts.yaml` as git-broker sees it — read at start, rewritten by
Admin → Git hosts through `render_git_hosts_yaml`. It is written in place rather than
renamed over, because a single bind-mounted file cannot be replaced; when the mount is
read-only the save fails in three parts and the operator edits the file on the host.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from slas_git.hosts import (
    GIT_HOSTS_FILE_HEADER,
    GitHosts,
    GitHostsError,
    default_hosts,
    hosts_from_mapping,
    render_git_hosts_yaml,
)
from slas_git_broker.broker import GitBroker
from slas_observability.events import EventLog
from slas_schemas.errors import ThreePartMessage


class HostsFileReadOnlyError(OSError):
    def __init__(self, path: Path, hostname: str) -> None:
        self.message = ThreePartMessage(
            f"{hostname} was not added: the allowlist at {path} could not be written.",
            "The allowlist is mounted read-only; edit config/git-hosts.yaml on the host.",
            "Add the host to config/git-hosts.yaml next to compose/ and restart git-broker, or "
            "mount the file rw so Admin → Git hosts can write it.",
        )
        super().__init__(self.message.what_happened)


class HostsFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> GitHosts:
        """The allowlist, or the shipped defaults when the file is absent; invalid → three parts."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default_hosts()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise GitHostsError(
                ThreePartMessage(
                    f"The Git host allowlist in {self.path} could not be read.",
                    f"It is not valid YAML: {str(exc).splitlines()[0]}",
                    f"Fix {self.path} and restart git-broker.",
                )
            ) from exc
        return hosts_from_mapping(data, source=str(self.path))

    def writable(self) -> bool:
        target = self.path if self.path.exists() else self.path.parent
        return os.access(target, os.W_OK)

    def save(self, hosts: GitHosts, *, hostname: str = "the host") -> None:
        text = render_git_hosts_yaml(hosts.model_dump(mode="json"), header=GIT_HOSTS_FILE_HEADER)
        try:
            with self.path.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise HostsFileReadOnlyError(self.path, hostname) from exc


class BrokerState:
    """Reached through `request.app.state.broker`."""

    def __init__(self, broker: GitBroker, hosts_file: HostsFile, log: EventLog) -> None:
        self.broker = broker
        self.hosts_file = hosts_file
        self.log = log

    def replace_hosts(self, hosts: GitHosts) -> None:
        self.broker.hosts = hosts
