"""Roles and capabilities; authz runs where the action executes (CLAUDE.md §5.7, §11, ADR-0005).

Load `config/rbac-roles.yaml` with `RolesLoader`, build a `Principal` from a user's role with
`principal_for`, and ask `authorize` whether a capability is held. Denials are three-part
sentences the UI renders as they are.
"""

from slas_authz.decide import Allowed, Decision, Denied, Principal, authorize, principal_for
from slas_authz.loader import RolesFileError, RolesLoader, load_roles_file, load_roles_text
from slas_authz.roles import BOOTSTRAP_CAPABILITIES, CapabilityDef, RoleDef, RolesConfig

__all__ = [
    "BOOTSTRAP_CAPABILITIES",
    "Allowed",
    "CapabilityDef",
    "Decision",
    "Denied",
    "Principal",
    "RoleDef",
    "RolesConfig",
    "RolesFileError",
    "RolesLoader",
    "authorize",
    "load_roles_file",
    "load_roles_text",
    "principal_for",
]
