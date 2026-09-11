"""Small role sets for tests (CLAUDE.md §0.3: tests run against fakes, never a live file)."""

from __future__ import annotations

from slas_authz.roles import RolesConfig


def roles_from(
    mapping: dict[str, list[str]], *, extra_capabilities: tuple[str, ...] = ()
) -> RolesConfig:
    """Build a valid RolesConfig from `{role: [capabilities]}`; labels are derived from names."""
    catalogue = {
        name: {"description": f"Test capability {name}."}
        for role_caps in mapping.values()
        for name in role_caps
    }
    for name in extra_capabilities:
        catalogue.setdefault(name, {"description": f"Test capability {name}."})
    return RolesConfig.model_validate(
        {
            "version": 1,
            "capabilities": catalogue,
            "roles": {
                role: {
                    "label": role.replace("-", " ").capitalize(),
                    "description": f"Test role {role}.",
                    "capabilities": caps,
                }
                for role, caps in mapping.items()
            },
        }
    )


def minimal_roles() -> RolesConfig:
    """An administrator who can manage people and settings, and a viewer who can only read."""
    return roles_from(
        {
            "administrator": ["users:manage", "settings:read", "settings:manage"],
            "viewer": ["settings:read"],
        }
    )
