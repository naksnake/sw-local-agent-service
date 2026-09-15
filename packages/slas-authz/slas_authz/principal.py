"""Who is acting: a person with a role, or the platform itself (ADR-0006)."""

from __future__ import annotations

from dataclasses import dataclass

from slas_authz.capabilities import Capability
from slas_authz.roles import Role


@dataclass(frozen=True, slots=True)
class Principal:
    """A snapshot of who is acting and what they may do, taken once per request."""

    subject: str
    display_name: str
    role: str
    role_label: str
    capabilities: frozenset[Capability]
    is_system: bool = False

    @classmethod
    def from_role(cls, subject: str, display_name: str, role: Role) -> Principal:
        return cls(subject, display_name, role.id, role.label, role.capabilities)

    def can(self, capability: Capability) -> bool:
        return self.is_system or capability in self.capabilities


#: The platform acting for itself: the installer's bootstrap, `slas user add` on the host.
SYSTEM = Principal(
    subject="system",
    display_name="SW Local Agent Service",
    role="system",
    role_label="System",
    capabilities=frozenset(Capability),
    is_system=True,
)
