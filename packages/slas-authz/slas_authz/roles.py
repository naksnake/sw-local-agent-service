"""The roles file: schema, validation and questions about it (CLAUDE.md §5.7, §11, ADR-0005).

`RolesConfig` is the parsed `config/rbac-roles.yaml`. Code asks it for the capabilities of a
role and never compares role names; the file is data a site may reshape.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*(:[a-z][a-z0-9_]*)?$")
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Capabilities the installation cannot lose without locking itself out (ADR-0005).
BOOTSTRAP_CAPABILITIES: frozenset[str] = frozenset({"users:manage", "settings:manage"})


class CapabilityDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    phase: int | None = Field(default=None, ge=0, le=12)


class RoleDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    capabilities: tuple[str, ...] = ()


class RolesConfig(BaseModel):
    """Validated roles file. Invalid content raises `pydantic.ValidationError` with a reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    capabilities: dict[str, CapabilityDef]
    roles: dict[str, RoleDef]

    @model_validator(mode="after")
    def _check_names_and_references(self) -> RolesConfig:
        for name in self.capabilities:
            if not CAPABILITY_RE.match(name):
                raise ValueError(
                    f"capability {name!r} is not a valid name (letters, digits, _ and one ':')"
                )
        if not self.roles:
            raise ValueError("the roles file defines no roles")
        for role_name, role in self.roles.items():
            if not ROLE_NAME_RE.match(role_name):
                raise ValueError(f"role {role_name!r} is not a valid name (lowercase slug)")
            unknown = [c for c in role.capabilities if c not in self.capabilities]
            if unknown:
                raise ValueError(
                    f"role {role_name!r} names capabilities that are not in the catalogue: "
                    + ", ".join(sorted(unknown))
                )
            duplicates = {c for c in role.capabilities if role.capabilities.count(c) > 1}
            if duplicates:
                raise ValueError(
                    f"role {role_name!r} lists a capability twice: " + ", ".join(sorted(duplicates))
                )
        holders = [
            role_name
            for role_name, role in self.roles.items()
            if BOOTSTRAP_CAPABILITIES <= set(role.capabilities)
        ]
        if not holders:
            raise ValueError(
                "no role holds both users:manage and settings:manage; the installation would "
                "lock itself out"
            )
        return self

    def role_names(self) -> tuple[str, ...]:
        return tuple(self.roles)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def capabilities_for(self, role: str) -> frozenset[str]:
        """Capabilities of a role; an unknown role has none (fail closed)."""
        definition = self.roles.get(role)
        if definition is None:
            return frozenset()
        return frozenset(definition.capabilities)

    def label_for(self, role: str) -> str:
        definition = self.roles.get(role)
        return definition.label if definition is not None else role

    def describe_capability(self, capability: str) -> str:
        definition = self.capabilities.get(capability)
        return definition.description if definition is not None else capability

    def roles_holding(self, capability: str) -> tuple[str, ...]:
        return tuple(name for name, role in self.roles.items() if capability in role.capabilities)
