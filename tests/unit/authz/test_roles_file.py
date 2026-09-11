"""The shipped config/rbac-roles.yaml is valid and says what CLAUDE.md and the demo promise."""

from __future__ import annotations

from pathlib import Path

import pytest
from slas_authz import RolesConfig, RolesFileError, load_roles_file, load_roles_text
from slas_authz.roles import BOOTSTRAP_CAPABILITIES

DEMO_ROLES = {"administrator", "validation-engineer", "factory-lead", "firmware-engineer", "viewer"}
GIT_CAPABILITIES = {
    "git:remote_manage",
    "git:clone",
    "git:pull",
    "git:push_branch",
    "git:push_protected",
    "git:bundle",
    "git:terminal",
    "git:hosts_manage",
}
SKILL_CAPABILITIES = {"screen", "ssh", "redfish", "files"}


@pytest.fixture(scope="module")
def shipped(repo_root: Path) -> RolesConfig:
    return load_roles_file(repo_root / "config/rbac-roles.yaml")


def test_shipped_file_has_the_demo_roles(shipped: RolesConfig) -> None:
    assert set(shipped.role_names()) == DEMO_ROLES
    assert shipped.label_for("validation-engineer") == "Validation engineer"


def test_shipped_file_declares_every_capability_named_in_claude_md(shipped: RolesConfig) -> None:
    assert GIT_CAPABILITIES <= set(shipped.capabilities), "CLAUDE.md §5.7 capabilities"
    assert SKILL_CAPABILITIES <= set(shipped.capabilities), "CLAUDE.md §6.1 requires values"
    assert BOOTSTRAP_CAPABILITIES <= set(shipped.capabilities)


def test_administrator_holds_every_capability_except_protected_push(shipped: RolesConfig) -> None:
    admin = shipped.capabilities_for("administrator")
    assert admin == set(shipped.capabilities) - {"git:push_protected"}, (
        "git:push_protected is off by default for everyone (CLAUDE.md §5.7)"
    )


def test_viewer_is_read_only(shipped: RolesConfig) -> None:
    viewer = shipped.capabilities_for("viewer")
    assert viewer == {"settings:read", "tickets:read"}


def test_every_role_can_read_settings_and_phase_1_capabilities_are_marked(
    shipped: RolesConfig,
) -> None:
    for role in shipped.role_names():
        assert "settings:read" in shipped.capabilities_for(role), role
    for name in ("users:manage", "settings:read", "settings:manage"):
        assert shipped.capabilities[name].phase == 1


def test_unknown_role_has_no_capabilities(shipped: RolesConfig) -> None:
    assert shipped.capabilities_for("chief-wizard") == frozenset()
    assert shipped.label_for("chief-wizard") == "chief-wizard"


def test_a_role_naming_an_unknown_capability_is_refused_with_three_parts() -> None:
    text = """
version: 1
capabilities:
  users:manage: { description: "x" }
  settings:manage: { description: "y" }
roles:
  administrator: { label: A, description: d, capabilities: [users:manage, settings:manage, fly] }
"""
    with pytest.raises(RolesFileError) as info:
        load_roles_text(text)
    err = info.value.error
    assert "fly" in err.what_happened
    assert err.likely_cause and err.what_to_do


def test_a_file_without_a_bootstrap_role_is_refused() -> None:
    text = """
version: 1
capabilities:
  users:manage: { description: "x" }
  settings:manage: { description: "y" }
roles:
  viewer: { label: V, description: d, capabilities: [users:manage] }
"""
    with pytest.raises(RolesFileError) as info:
        load_roles_text(text)
    assert "lock itself out" in info.value.error.what_happened


@pytest.mark.parametrize(
    "text",
    ["", "- a\n- b\n", "version: 2\ncapabilities: {}\nroles: {}\n", "version: 1\nroles: {}\n"],
)
def test_malformed_documents_are_refused(text: str) -> None:
    with pytest.raises(RolesFileError):
        load_roles_text(text)


def test_yaml_syntax_error_is_reported_as_a_sentence() -> None:
    with pytest.raises(RolesFileError) as info:
        load_roles_text("version: 1\nroles: [unclosed\n")
    assert "could not be read as YAML" in info.value.error.what_happened


BAD_CAPABILITY_NAME = """
version: 1
capabilities:
  Users:Manage: { description: x }
  settings:manage: { description: y }
roles:
  administrator: { label: A, description: d, capabilities: [Users:Manage, settings:manage] }
"""

BAD_ROLE_NAME = """
version: 1
capabilities:
  users:manage: { description: x }
  settings:manage: { description: y }
roles:
  Admin Role: { label: A, description: d, capabilities: [users:manage, settings:manage] }
"""


@pytest.mark.parametrize("text", [BAD_CAPABILITY_NAME, BAD_ROLE_NAME])
def test_bad_names_are_refused(text: str) -> None:
    with pytest.raises(RolesFileError) as info:
        load_roles_text(text)
    assert "not a valid name" in info.value.error.what_happened
