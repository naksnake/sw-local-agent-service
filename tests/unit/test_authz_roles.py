"""Roles: the shipped set, validation with three-part errors, and the file kept in step."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from slas_authz import (
    DEFAULT_ROLES,
    ROLES_FILE_HEADER,
    Capability,
    RolesError,
    default_roles,
    render_roles_yaml,
    roles_from_mapping,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLES_FILE = REPO_ROOT / "config" / "rbac-roles.yaml"


def mapping() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_ROLES)


def test_shipped_roles_file_is_rendered_from_the_default_role_set() -> None:
    expected = render_roles_yaml(DEFAULT_ROLES, header=ROLES_FILE_HEADER)
    assert ROLES_FILE.read_text(encoding="utf-8") == expected, (
        "config/rbac-roles.yaml drifted from slas_authz.roles.DEFAULT_ROLES; "
        "regenerate it with render_roles_yaml() and write an ADR if the set changed"
    )


def test_default_role_set_matches_adr_0006() -> None:
    roles = default_roles()
    assert roles.ids() == ("administrator", "engineer", "line_lead", "viewer")
    assert roles.default_role == "engineer"
    admin, engineer, lead, viewer = (roles.roles[i] for i in roles.ids())
    assert admin.can(Capability.ADMIN_PEOPLE) and admin.can(Capability.ADMIN_SETTINGS)
    assert admin.can(Capability.GIT_HOSTS_MANAGE) and admin.can(Capability.MODEL_MANAGE)
    assert not engineer.can(Capability.ADMIN_PEOPLE)
    assert not engineer.can(Capability.GIT_HOSTS_MANAGE)
    assert engineer.can(Capability.GIT_PUSH_BRANCH) and engineer.can(Capability.APPROVE_DESTRUCTIVE)
    assert lead.capabilities == engineer.capabilities | {Capability.FACTORY_VERDICT}
    assert viewer.capabilities == frozenset()
    assert all(not role.can(Capability.GIT_PUSH_PROTECTED) for role in roles.roles.values())
    assert admin.capabilities >= engineer.capabilities


def test_describe_is_a_sentence_per_role() -> None:
    roles = default_roles()
    assert roles.describe("viewer") == (
        "Viewer: Reads tickets, runs and reports. This role can only look."
    )
    assert roles.describe("engineer").startswith("Engineer: Runs coding tasks")
    assert roles.describe("engineer").endswith("Holds 13 capabilities.")
    assert roles.describe("nope") == "There is no role called nope."
    assert roles.default().id == "engineer"
    assert roles.get("nope") is None


def error_for(data: object) -> RolesError:
    with pytest.raises(RolesError) as raised:
        roles_from_mapping(data, source="tmp/roles.yaml")
    return raised.value


def test_not_a_mapping() -> None:
    error = error_for(["administrator"])
    assert error.message.what_happened == "The roles file tmp/roles.yaml is not a mapping."
    assert "config/README.md" in error.message.what_to_do


def test_wrong_version() -> None:
    data = mapping()
    data["version"] = 2
    assert "version 2, expected 1" in error_for(data).message.what_happened
    del data["version"]
    assert "version None" in error_for(data).message.what_happened


def test_no_roles() -> None:
    data = mapping()
    data["roles"] = {}
    assert (
        error_for(data).message.what_happened == "The roles file tmp/roles.yaml defines no roles."
    )


@pytest.mark.parametrize("role_id", ["Admin", "1st", "with-dash", "", "a" * 41])
def test_bad_role_id(role_id: str) -> None:
    data = mapping()
    data["roles"][role_id] = {"label": "X", "description": "", "capabilities": []}
    assert "is not allowed" in error_for(data).message.what_happened


def test_reserved_role_id() -> None:
    data = mapping()
    data["roles"]["system"] = {"label": "X", "description": "", "capabilities": []}
    assert "reserved" in error_for(data).message.what_happened


def test_role_must_be_a_mapping_with_a_label() -> None:
    data = mapping()
    data["roles"]["viewer"] = "read only"
    assert "is not a mapping" in error_for(data).message.what_happened
    data["roles"]["viewer"] = {"description": "x", "capabilities": []}
    assert "has no label" in error_for(data).message.what_happened
    data["roles"]["viewer"] = {"label": "   ", "capabilities": []}
    assert "has no label" in error_for(data).message.what_happened
    data["roles"]["viewer"] = {"label": "Viewer", "description": 3, "capabilities": []}
    assert "is not text" in error_for(data).message.what_happened


def test_capabilities_must_be_a_list_of_known_names_without_duplicates() -> None:
    data = mapping()
    data["roles"]["viewer"]["capabilities"] = "screen"
    assert "not a list of names" in error_for(data).message.what_happened
    data["roles"]["viewer"]["capabilities"] = ["screen", "shell"]
    error = error_for(data)
    assert error.message.what_happened == (
        "The role viewer in tmp/roles.yaml names an unknown capability 'shell'."
    )
    assert "git:push_protected" in error.message.likely_cause
    data["roles"]["viewer"]["capabilities"] = ["screen", "screen"]
    assert "lists screen twice" in error_for(data).message.what_happened


def test_default_role_must_exist() -> None:
    data = mapping()
    data["default_role"] = "guest"
    error = error_for(data)
    assert error.message.what_happened == (
        "The default role 'guest' in tmp/roles.yaml is not one of the defined roles."
    )
    del data["default_role"]
    assert "default role None" in error_for(data).message.what_happened


def test_render_refuses_an_invalid_mapping_and_renders_empty_capabilities() -> None:
    with pytest.raises(RolesError):
        render_roles_yaml({"version": 1})
    text = render_roles_yaml(
        {
            "version": 1,
            "default_role": "only",
            "roles": {"only": {"label": 'Say "hi"', "description": "中文", "capabilities": []}},
        }
    )
    assert text == (
        "version: 1\ndefault_role: only\nroles:\n  only:\n"
        '    label: "Say \\"hi\\""\n    description: "中文"\n    capabilities: []\n'
    )


def test_rendered_file_parses_back_with_a_minimal_yaml_reading() -> None:
    """Until pyyaml is approved, check the shape the way a YAML parser would see it."""
    text = ROLES_FILE.read_text(encoding="utf-8")
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert body[0] == "version: 1"
    assert body[1] == "default_role: engineer"
    assert body[2] == "roles:"
    role_lines = [line for line in body if line.startswith("  ") and not line.startswith("    ")]
    assert role_lines == ["  administrator:", "  engineer:", "  line_lead:", "  viewer:"]
    listed = {line.strip()[2:] for line in body if line.startswith("      - ")}
    assert listed <= {capability.value for capability in Capability}
    assert "git:push_protected" not in listed
