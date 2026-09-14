"""Roles: named bundles of capabilities loaded from `config/rbac-roles.yaml` (ADR-0006).

Role *definitions* are configuration and live in that file; *assigning* a role to a person
is data in the database. This module validates a roles mapping, holds the default role set
that ships with the platform, and renders it back so a test can keep the shipped file and
the code in step. Parsing YAML is one function away and arrives with the api (pyyaml is a
dependency that needs approval); everything here is stdlib.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from slas_authz.capabilities import DEFAULT_OFF, Capability, parse_capability
from slas_schemas.errors import ThreePartMessage

ROLES_FILE_VERSION: Final = 1
RESERVED_ROLE_IDS: Final[frozenset[str]] = frozenset({"system"})
_ROLE_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class RolesError(ValueError):
    """A roles file that cannot be used. `message` explains it in three parts."""

    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


@dataclass(frozen=True, slots=True)
class Role:
    id: str
    label: str
    description: str
    capabilities: frozenset[Capability]

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class RoleSet:
    roles: Mapping[str, Role]
    default_role: str
    source: str = "<memory>"

    def get(self, role_id: str) -> Role | None:
        return self.roles.get(role_id)

    def default(self) -> Role:
        return self.roles[self.default_role]

    def ids(self) -> tuple[str, ...]:
        return tuple(self.roles)

    def describe(self, role_id: str) -> str:
        """One sentence per role for the UI and the CLI."""
        role = self.roles.get(role_id)
        if role is None:
            return f"There is no role called {role_id}."
        if not role.capabilities:
            return f"{role.label}: {role.description} This role can only look."
        count = len(role.capabilities)
        noun = "capability" if count == 1 else "capabilities"
        return f"{role.label}: {role.description} Holds {count} {noun}."


def _error(what_happened: str, likely_cause: str, what_to_do: str) -> RolesError:
    return RolesError(ThreePartMessage(what_happened, likely_cause, what_to_do))


def roles_from_mapping(data: object, *, source: str = "<memory>") -> RoleSet:
    """Validate a parsed roles file and turn it into a RoleSet, or raise RolesError."""
    fix = f"Fix {source} and try again; the format is documented in config/README.md."
    if not isinstance(data, Mapping):
        raise _error(
            f"The roles file {source} is not a mapping.",
            "The file is empty, a list, or plain text.",
            fix,
        )
    if data.get("version") != ROLES_FILE_VERSION:
        raise _error(
            f"The roles file {source} has version {data.get('version')!r}, "
            f"expected {ROLES_FILE_VERSION}.",
            "The file was written for another release, or the version line is missing.",
            fix,
        )
    raw_roles = data.get("roles")
    if not isinstance(raw_roles, Mapping) or not raw_roles:
        raise _error(
            f"The roles file {source} defines no roles.",
            "The `roles:` section is missing or empty.",
            fix,
        )
    roles: dict[str, Role] = {}
    for role_id, raw in raw_roles.items():
        if not isinstance(role_id, str) or not _ROLE_ID.match(role_id):
            raise _error(
                f"The role id {role_id!r} in {source} is not allowed.",
                "Role ids are lowercase letters, digits and underscores, starting with a letter.",
                fix,
            )
        if role_id in RESERVED_ROLE_IDS:
            raise _error(
                f"The role id {role_id!r} in {source} is reserved.",
                "The platform uses it for its own internal principal.",
                fix,
            )
        if not isinstance(raw, Mapping):
            raise _error(
                f"The role {role_id} in {source} is not a mapping.",
                "Each role needs label, description and capabilities.",
                fix,
            )
        label = raw.get("label")
        description = raw.get("description", "")
        raw_caps = raw.get("capabilities", [])
        if not isinstance(label, str) or not label.strip():
            raise _error(
                f"The role {role_id} in {source} has no label.",
                "Every role needs a label people will see, such as Engineer.",
                fix,
            )
        if not isinstance(description, str):
            raise _error(
                f"The description of role {role_id} in {source} is not text.",
                "Descriptions are one sentence in quotes.",
                fix,
            )
        if not isinstance(raw_caps, list) or not all(isinstance(c, str) for c in raw_caps):
            raise _error(
                f"The capabilities of role {role_id} in {source} are not a list of names.",
                "Write them as a YAML list, one capability per line.",
                fix,
            )
        capabilities: set[Capability] = set()
        for name in raw_caps:
            capability = parse_capability(name)
            if capability is None:
                known = ", ".join(c.value for c in Capability)
                raise _error(
                    f"The role {role_id} in {source} names an unknown capability {name!r}.",
                    f"Capabilities are a fixed list: {known}.",
                    fix,
                )
            if capability in capabilities:
                raise _error(
                    f"The role {role_id} in {source} lists {name} twice.",
                    "A copy-paste slip.",
                    fix,
                )
            capabilities.add(capability)
        roles[role_id] = Role(role_id, label.strip(), description.strip(), frozenset(capabilities))
    default_role = data.get("default_role")
    if not isinstance(default_role, str) or default_role not in roles:
        raise _error(
            f"The default role {default_role!r} in {source} is not one of the defined roles.",
            "`default_role:` must name a role from the `roles:` section; new people get it.",
            fix,
        )
    return RoleSet(roles=roles, default_role=default_role, source=source)


def render_roles_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    """Render a roles mapping as the YAML the platform ships; strings are JSON-quoted.

    Restricted to this file's shape on purpose, so the shipped `config/rbac-roles.yaml`
    can be compared against `DEFAULT_ROLES` without a YAML parser.
    """
    role_set = roles_from_mapping(data)  # only valid role sets are rendered
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {ROLES_FILE_VERSION}")
    lines.append(f"default_role: {role_set.default_role}")
    lines.append("roles:")
    raw_roles = data["roles"]
    if not isinstance(raw_roles, Mapping):  # pragma: no cover — validated above
        raise TypeError("roles must be a mapping")
    for role_id, raw in raw_roles.items():
        if not isinstance(raw, Mapping):  # pragma: no cover — validated above
            raise TypeError(f"role {role_id} must be a mapping")
        lines.append(f"  {role_id}:")
        lines.append(f"    label: {json.dumps(raw['label'], ensure_ascii=False)}")
        description = raw.get("description", "")
        lines.append(f"    description: {json.dumps(description, ensure_ascii=False)}")
        caps = raw.get("capabilities", [])
        if not isinstance(caps, list):  # pragma: no cover — validated above
            raise TypeError(f"capabilities of {role_id} must be a list")
        if caps:
            lines.append("    capabilities:")
            lines.extend(f"      - {name}" for name in caps)
        else:
            lines.append("    capabilities: []")
    return "\n".join(lines) + "\n"


def _caps(*capabilities: Capability) -> list[str]:
    return [capability.value for capability in capabilities]


_ENGINEER: Final = _caps(
    Capability.SCREEN,
    Capability.SSH,
    Capability.REDFISH,
    Capability.FILES,
    Capability.NETWORK,
    Capability.GIT_REMOTE_MANAGE,
    Capability.GIT_CLONE,
    Capability.GIT_PULL,
    Capability.GIT_PUSH_BRANCH,
    Capability.GIT_BUNDLE,
    Capability.GIT_TERMINAL,
    Capability.APPROVE_DESTRUCTIVE,
    Capability.FACTORY_CONTROL,
)

#: The role set the platform ships (ADR-0006). `config/rbac-roles.yaml` is rendered from it.
DEFAULT_ROLES: Final[dict[str, object]] = {
    "version": ROLES_FILE_VERSION,
    "default_role": "engineer",
    "roles": {
        "administrator": {
            "label": "Administrator",
            "description": "Manages people, settings, Git hosts, test stations and models, "
            "and can do everything an engineer can.",
            "capabilities": [c.value for c in Capability if c not in DEFAULT_OFF],
        },
        "engineer": {
            "label": "Engineer",
            "description": "Runs coding tasks, validation runs and factory jobs, and "
            "approves destructive steps.",
            "capabilities": _ENGINEER,
        },
        "line_lead": {
            "label": "Line lead",
            "description": "An engineer who also decides a factory PASS or FAIL when the "
            "voters disagree.",
            "capabilities": [*_ENGINEER, Capability.FACTORY_VERDICT.value],
        },
        "viewer": {
            "label": "Viewer",
            "description": "Reads tickets, runs and reports.",
            "capabilities": [],
        },
    },
}

ROLES_FILE_HEADER: Final = (
    "Roles and capabilities for SW Local Agent Service (CLAUDE.md §5.7, ADR-0006).\n"
    "Rendered from slas_authz.roles.DEFAULT_ROLES; a unit test keeps file and code in step.\n"
    "Role definitions are configuration: the api reads this file read-only and picks up an\n"
    "edit on the next request. Assigning a role to a person is data in the database.\n"
    "git:push_protected is granted to nobody by default (§5.7). Changing the role set needs\n"
    "an ADR (§15)."
)


def default_roles() -> RoleSet:
    return roles_from_mapping(DEFAULT_ROLES, source="config/rbac-roles.yaml")
