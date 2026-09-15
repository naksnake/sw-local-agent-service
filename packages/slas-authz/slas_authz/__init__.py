"""Roles and capabilities; authz runs where the action executes (CLAUDE.md §11, ADR-0006).

Phase 1 ships the closed capability set, the role model with validation, the shipped
default roles, principals and pure allow/deny decisions. Loading `config/rbac-roles.yaml`
from disk is one function that arrives with the api once its YAML dependency is approved.
"""

from slas_authz.capabilities import (
    DEFAULT_OFF,
    DESCRIPTIONS,
    SKILL_REQUIRES,
    Capability,
    parse_capability,
)
from slas_authz.decide import AccessDeniedError, Decision, decide, require
from slas_authz.principal import SYSTEM, Principal
from slas_authz.roles import (
    DEFAULT_ROLES,
    ROLES_FILE_HEADER,
    Role,
    RolesError,
    RoleSet,
    default_roles,
    render_roles_yaml,
    roles_from_mapping,
)

__version__ = "0.0.1"

__all__ = [
    "DEFAULT_OFF",
    "DEFAULT_ROLES",
    "DESCRIPTIONS",
    "ROLES_FILE_HEADER",
    "SKILL_REQUIRES",
    "SYSTEM",
    "AccessDeniedError",
    "Capability",
    "Decision",
    "Principal",
    "Role",
    "RoleSet",
    "RolesError",
    "__version__",
    "decide",
    "default_roles",
    "parse_capability",
    "render_roles_yaml",
    "require",
    "roles_from_mapping",
]
