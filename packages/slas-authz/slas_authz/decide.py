"""Authorisation decisions: a principal, a capability, an answer in plain language (§9, §11)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from slas_schemas.errors import ThreePartError

from slas_authz.roles import RolesConfig


class Principal(BaseModel):
    """Who is acting. Capabilities are resolved from the role at request time, never stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1)
    email: str = Field(min_length=3)
    role: str = Field(min_length=1)
    capabilities: frozenset[str] = frozenset()

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


def principal_for(roles: RolesConfig, *, user_id: str, email: str, role: str) -> Principal:
    """Build a principal whose capabilities come from the roles file (unknown role → none)."""
    return Principal(
        user_id=user_id, email=email, role=role, capabilities=roles.capabilities_for(role)
    )


@dataclass(frozen=True)
class Allowed:
    capability: str

    @property
    def allowed(self) -> bool:
        return True


@dataclass(frozen=True)
class Denied:
    capability: str
    error: ThreePartError

    @property
    def allowed(self) -> bool:
        return False


Decision = Allowed | Denied


def authorize(roles: RolesConfig, principal: Principal, capability: str) -> Decision:
    """Allow when the principal holds the capability; otherwise a three-part explanation."""
    if principal.has(capability):
        return Allowed(capability)
    role_label = roles.label_for(principal.role)
    holders = [roles.label_for(r) for r in roles.roles_holding(capability)]
    action = roles.describe_capability(capability)
    if holders:
        who = "the role " + holders[0] if len(holders) == 1 else "the roles " + ", ".join(holders)
        what_to_do = f"Ask an administrator to give you {who} under Admin → People."
    else:
        what_to_do = (
            "No role currently allows this; an administrator can change config/rbac-roles.yaml."
        )
    return Denied(
        capability,
        ThreePartError(
            what_happened=f"Your role ({role_label}) does not allow this: {action}",
            likely_cause="Your role was chosen when your account was added.",
            what_to_do=what_to_do,
        ),
    )
