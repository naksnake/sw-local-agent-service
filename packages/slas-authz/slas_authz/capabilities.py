"""The closed set of capabilities a person can hold (CLAUDE.md §5.6, §5.7, ADR-0006).

Skills consume the first five (`requires:` in a skill file, §6.1); the `git:*` names come
from §5.7; `admin:*` gates the Phase 1 pages; the last three are reserved for later phases
so that roles can already name them. Nothing else exists: an unknown name in a roles file
is an error, never a silently ignored string.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Capability(StrEnum):
    SCREEN = "screen"
    SSH = "ssh"
    REDFISH = "redfish"
    FILES = "files"
    NETWORK = "network"
    GIT_REMOTE_MANAGE = "git:remote_manage"
    GIT_CLONE = "git:clone"
    GIT_PULL = "git:pull"
    GIT_PUSH_BRANCH = "git:push_branch"
    GIT_PUSH_PROTECTED = "git:push_protected"
    GIT_BUNDLE = "git:bundle"
    GIT_TERMINAL = "git:terminal"
    GIT_HOSTS_MANAGE = "git:hosts_manage"
    ADMIN_PEOPLE = "admin:people"
    ADMIN_SETTINGS = "admin:settings"
    APPROVE_DESTRUCTIVE = "approve:destructive"
    FACTORY_VERDICT = "factory:verdict"
    FACTORY_CONTROL = "factory:control"
    FACTORY_STATIONS_MANAGE = "factory:stations_manage"
    MODEL_MANAGE = "model:manage"


#: What holding the capability lets a person do, as a verb phrase that completes
#: "<Name> may …" and "<Name> may not …".
DESCRIPTIONS: Final[dict[Capability, str]] = {
    Capability.SCREEN: "operate a screen on a virtual display or a test station",
    Capability.SSH: "run commands on a target over SSH",
    Capability.REDFISH: "read and control a server's BMC over Redfish",
    Capability.FILES: "read and write files in a job's workspace",
    Capability.NETWORK: "let a sandbox reach the allowlisted network",
    Capability.GIT_REMOTE_MANAGE: "add, rotate and delete their own Git remotes",
    Capability.GIT_CLONE: "clone from a saved Git remote",
    Capability.GIT_PULL: "pull from a saved Git remote",
    Capability.GIT_PUSH_BRANCH: "push a branch and open a merge request",
    Capability.GIT_PUSH_PROTECTED: "push directly to a protected branch",
    Capability.GIT_BUNDLE: "export and import Git bundles for offline transfer",
    Capability.GIT_TERMINAL: "open a terminal inside a sandbox",
    Capability.GIT_HOSTS_MANAGE: "manage the allowlist of Git hosts",
    Capability.ADMIN_PEOPLE: "manage who can sign in and what role they have",
    Capability.ADMIN_SETTINGS: "change platform settings",
    Capability.APPROVE_DESTRUCTIVE: (
        "approve destructive steps such as an AC cycle or a firmware flash"
    ),
    Capability.FACTORY_VERDICT: "decide a factory PASS or FAIL when the voters disagree",
    Capability.FACTORY_CONTROL: "watch a test station live and take it over from a running job",
    Capability.FACTORY_STATIONS_MANAGE: (
        "add test stations, issue their enrolment codes and tune them"
    ),
    Capability.MODEL_MANAGE: "swap and roll back models",
}

#: The names a skill file may list under `requires:` (CLAUDE.md §6.1).
SKILL_REQUIRES: Final[frozenset[Capability]] = frozenset(
    {
        Capability.SCREEN,
        Capability.SSH,
        Capability.REDFISH,
        Capability.FILES,
        Capability.NETWORK,
    }
)

#: Granted to no role by default (CLAUDE.md §5.7 push policy).
DEFAULT_OFF: Final[frozenset[Capability]] = frozenset({Capability.GIT_PUSH_PROTECTED})


def parse_capability(name: str) -> Capability | None:
    """The capability for a string from a roles or skill file, or None if unknown."""
    try:
        return Capability(name)
    except ValueError:
        return None
