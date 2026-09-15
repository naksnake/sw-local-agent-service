"""The capability set is closed and matches CLAUDE.md §5.7 and §6.1."""

from __future__ import annotations

from slas_authz import (
    DEFAULT_OFF,
    DESCRIPTIONS,
    SKILL_REQUIRES,
    Capability,
    parse_capability,
)

GIT_CAPABILITIES_FROM_SECTION_5_7 = {
    "git:remote_manage",
    "git:clone",
    "git:pull",
    "git:push_branch",
    "git:push_protected",
    "git:bundle",
    "git:terminal",
    "git:hosts_manage",
}
SKILL_REQUIRES_FROM_SECTION_6_1 = {"screen", "ssh", "redfish", "files", "network"}


def test_every_git_capability_from_claude_md_exists() -> None:
    values = {capability.value for capability in Capability}
    assert values >= GIT_CAPABILITIES_FROM_SECTION_5_7


def test_skill_requires_names_are_exactly_the_section_6_1_list() -> None:
    assert {capability.value for capability in SKILL_REQUIRES} == SKILL_REQUIRES_FROM_SECTION_6_1


def test_every_capability_has_a_description_that_reads_as_a_verb_phrase() -> None:
    assert set(DESCRIPTIONS) == set(Capability)
    for capability, text in DESCRIPTIONS.items():
        assert text and text[0].islower(), capability
        assert not text.endswith("."), capability


def test_push_protected_is_the_only_default_off_capability() -> None:
    assert set(DEFAULT_OFF) == {Capability.GIT_PUSH_PROTECTED}


def test_parse_capability_is_strict() -> None:
    assert parse_capability("git:clone") is Capability.GIT_CLONE
    assert parse_capability("GIT:CLONE") is None
    assert parse_capability("shell") is None, "there is no shell capability (§6.2)"
    assert parse_capability("") is None


def test_capabilities_are_strings_for_yaml_and_json() -> None:
    assert Capability.ADMIN_PEOPLE.value == "admin:people"
    assert str(Capability.SCREEN) == "screen"
    assert f"{Capability.GIT_CLONE}" == "git:clone"
