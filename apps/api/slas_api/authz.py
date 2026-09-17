"""Roles from `config/rbac-roles.yaml`, a principal per request, a 403 in three parts.

Role definitions are configuration (ADR-0006): the file is re-read when its mtime changes,
an invalid edit keeps the last good set and is logged as a three-part message, and the
shipped default set is the fallback before any file has loaded. Which role a person holds
is data in the `people` table.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from slas_api.errors import ApiError
from slas_api.models import Person
from slas_authz import (
    Capability,
    Principal,
    RolesError,
    RoleSet,
    decide,
    default_roles,
    roles_from_mapping,
)
from slas_observability.events import EventLog
from slas_schemas.errors import ThreePartMessage


class RolesLoader:
    def __init__(self, path: Path, log: EventLog) -> None:
        self.path = path
        self._log = log
        self._roles: RoleSet | None = None
        self._mtime_ns: int | None = None

    def current(self) -> RoleSet:
        """The role set in force: re-read on an mtime change, last good one on a bad edit."""
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if self._roles is not None and mtime_ns == self._mtime_ns:
            return self._roles
        loaded = self._load(mtime_ns)
        self._mtime_ns = mtime_ns
        if loaded is not None:
            self._roles = loaded
        elif self._roles is None:
            self._roles = default_roles()
            self._log.warning(
                "roles.defaults_in_force",
                path=str(self.path),
                sentence="The shipped default roles are in force until the file is fixed.",
            )
        return self._roles

    def _load(self, mtime_ns: int | None) -> RoleSet | None:
        source = str(self.path)
        if mtime_ns is None:
            self._log.warning(
                "roles.file_missing",
                path=source,
                sentence=f"The roles file {source} is not there; the last good set stays in force.",
            )
            return None
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            roles = roles_from_mapping(data, source=source)
        except RolesError as exc:
            self._log_problem(exc.message)
            return None
        except (OSError, yaml.YAMLError) as exc:
            self._log_problem(
                ThreePartMessage(
                    f"The roles file {source} could not be read.",
                    f"{type(exc).__name__}: {exc}".splitlines()[0],
                    f"Fix {source}; the last good set stays in force until then.",
                )
            )
            return None
        self._log.info("roles.loaded", path=source, roles=list(roles.ids()))
        return roles

    def _log_problem(self, message: ThreePartMessage) -> None:
        self._log.warning(
            "roles.invalid",
            path=str(self.path),
            what_happened=message.what_happened,
            likely_cause=message.likely_cause,
            what_to_do=message.what_to_do,
        )


def principal_for(person: Person, roles: RoleSet) -> Principal:
    """The per-request snapshot. A role removed from the file grants nothing."""
    role = roles.get(person.role)
    if role is None:
        return Principal(
            subject=person.email,
            display_name=person.display_name,
            role=person.role,
            role_label=person.role.replace("_", " ").capitalize(),
            capabilities=frozenset(),
        )
    return Principal.from_role(person.email, person.display_name, role)


def require(principal: Principal, capability: Capability) -> None:
    decision = decide(principal, capability)
    if not decision.allowed and decision.message is not None:
        raise ApiError(403, decision.message)
