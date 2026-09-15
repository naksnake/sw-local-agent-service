"""IMPORT: schema, whitelist, capability check, risk classification, approval sentence."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from slas_authz import Principal, default_roles
from slas_skills.importer import import_skill, walk
from slas_skills.library import SEL_COLLECT_CLEAR, STATION_LOGIN_BURNIN
from slas_skills.primitives import PRIMITIVES, SCREEN_PRIMITIVES
from slas_skills.schema import SkillImportError, parse_skill

ROLES = default_roles()


def person(role: str) -> Principal:
    return Principal.from_role(f"{role}@slas.local", "Pat Lin", ROLES.roles[role])


def power_off_skill() -> dict[str, Any]:
    return {
        "skill": {
            "id": "power-off-target",
            "name": "Power off the target",
            "version": "0.1.0",
            "agents": ["validation"],
            "requires": ["redfish"],
            "inputs": {"target": {"type": "target_ref", "required": True}},
            "steps": [
                {
                    "redfish": {"target": "{{ target }}", "action": "get_power_state"},
                    "id": "before",
                },
                {"redfish": {"target": "{{ target }}", "action": "power_off"}, "id": "off"},
            ],
        }
    }


def test_the_two_section_6_3_examples_import() -> None:
    burnin = import_skill(STATION_LOGIN_BURNIN, principal=person("engineer"))
    assert burnin.skill.id == "station-login-burnin"
    assert burnin.risk == "caution", "ssh is caution; nothing destructive"
    assert burnin.destructive_steps == [] and burnin.approval_sentence is None
    assert burnin.sentence == (
        "Log in to the test station and start BurnIn v1.0.0: 9 steps, needs screen, ssh, "
        "risk caution."
    )
    assert burnin.skill.inputs["password"].type == "secret"
    assert burnin.skill.on_failure == "screenshot_and_stop"
    assert burnin.skill.outputs["burnin_status"].source == "status"

    sel = import_skill(SEL_COLLECT_CLEAR, principal=person("engineer"))
    assert sel.risk == "caution" and sel.skill.version == "1.2.0"
    assert [step.primitive for step in sel.skill.steps] == ["redfish", "copy", "assert"]
    assert sel.skill.steps[0].id == "sel"


def test_a_power_off_step_shows_the_approval_requirement_at_import() -> None:
    report = import_skill(power_off_skill(), principal=person("engineer"))
    assert report.risk == "destructive" and report.needs_approval
    assert report.destructive_steps == ["redfish power_off on {{ target }}"]
    assert report.approval_sentence == (
        "This skill contains 1 destructive step (redfish power_off on {{ target }}) and will "
        "ask for your approval every time it runs."
    )


def test_a_viewer_cannot_import_a_skill_that_needs_redfish() -> None:
    with pytest.raises(SkillImportError) as raised:
        import_skill(SEL_COLLECT_CLEAR, principal=person("viewer"))
    message = raised.value.message
    assert (
        message.what_happened
        == "You can't import Collect and clear the BMC event log: it needs redfish."
    )
    assert message.likely_cause.startswith("Your Viewer role does not include it")
    assert "never adds capabilities" in message.likely_cause


def test_there_is_no_shell_primitive() -> None:
    assert "shell" not in PRIMITIVES and "eval" not in PRIMITIVES and "sudo" not in PRIMITIVES
    data = copy.deepcopy(SEL_COLLECT_CLEAR)
    data["skill"]["steps"].append({"shell": {"command": "rm -rf /"}})
    with pytest.raises(SkillImportError) as raised:
        parse_skill(data, source="evil.skill.yaml")
    assert raised.value.message.what_happened == (
        "Step steps[3] in evil.skill.yaml uses `shell`, which is not a skill primitive."
    )
    assert "no shell, eval, python, download or sudo" in raised.value.message.likely_cause


@pytest.mark.parametrize(
    ("step", "fragment"),
    [
        ({"click": {}}, "needs a target"),
        ({"type": {}}, "is missing text"),
        ({"key": {"press": "Enter", "hard": True}}, "arguments it does not take: hard"),
        (
            {"redfish": {"target": "t", "action": "flash_firmware"}},
            "unknown action 'flash_firmware'",
        ),
        ({"wait": {"seconds": 4000}}, "waits 4000 seconds"),
        ({"run": {"command": "ls -la"}}, "not an argv list"),
        ({"click": {"text": "a"}, "key": {"press": "b"}}, "exactly one primitive"),
        ("just a string", "is not a mapping"),
        ({"if": {"condition": "1 == 1"}}, "is missing then"),
    ],
)
def test_step_problems_are_reported_by_path(step: Any, fragment: str) -> None:
    data = {
        "skill": {
            "id": "t",
            "name": "T",
            "version": "1.0.0",
            "agents": ["coding"],
            "requires": ["screen", "files", "redfish"],
            "steps": [step],
        }
    }
    with pytest.raises(SkillImportError) as raised:
        parse_skill(data, source="t.skill.yaml")
    assert fragment in raised.value.message.what_happened, raised.value.message


def test_skill_level_problems() -> None:
    with pytest.raises(SkillImportError, match="is not a skill file"):
        parse_skill({"not": "a skill"})
    base: dict[str, Any] = {
        "id": "t",
        "name": "T",
        "version": "1.0.0",
        "agents": ["coding"],
        "steps": [{"wait": {"seconds": 1}}],
    }
    with pytest.raises(SkillImportError) as bad_version:
        parse_skill({"skill": {**base, "version": "1.0"}})
    assert "version" in bad_version.value.message.likely_cause
    with pytest.raises(SkillImportError) as bad_requires:
        parse_skill({"skill": {**base, "requires": ["admin:people"]}})
    assert "requires may only list" in bad_requires.value.message.likely_cause
    with pytest.raises(SkillImportError, match="unknown capability 'laser'"):
        parse_skill({"skill": {**base, "requires": ["laser"]}})
    with pytest.raises(SkillImportError) as bad_agent:
        parse_skill({"skill": {**base, "agents": ["null"]}})
    assert "agents" in bad_agent.value.message.likely_cause
    with pytest.raises(SkillImportError, match="must be a non-empty list"):
        parse_skill({"skill": {**base, "steps": []}})


def test_requires_must_cover_every_primitive() -> None:
    data = copy.deepcopy(STATION_LOGIN_BURNIN)
    data["skill"]["requires"] = ["screen"]  # ssh step present, ssh not required
    with pytest.raises(SkillImportError) as raised:
        import_skill(data, source="burnin.skill.yaml")
    assert raised.value.message.what_happened == (
        "burnin.skill.yaml uses `ssh` at steps[8] but does not require ssh."
    )
    assert raised.value.message.what_to_do == "Add ssh to `requires:` or remove the step."


def test_run_and_copy_accept_files_or_ssh() -> None:
    skill = {
        "skill": {
            "id": "r",
            "name": "R",
            "version": "1.0.0",
            "agents": ["coding"],
            "requires": ["files"],
            "steps": [
                {"run": {"command": ["make", "test"], "cwd": "."}},
                {"copy": {"from": "a", "to": "b"}},
            ],
        }
    }
    report = import_skill(skill)
    assert report.risk == "caution"
    skill["skill"]["requires"] = ["ssh"]
    assert import_skill(skill).risk == "caution"


def test_nested_control_steps_are_walked_and_validated() -> None:
    data = {
        "skill": {
            "id": "n",
            "name": "N",
            "version": "1.0.0",
            "agents": ["coding"],
            "requires": ["screen"],
            "steps": [
                {
                    "if": {
                        "condition": "{{ mode }} == 'gui'",
                        "then": [{"click": {"text": "Start"}}],
                        "else": [
                            {"wait": {"seconds": 1}},
                            {"foreach": {"items": [1, 2], "then": [{"key": {"press": "Tab"}}]}},
                        ],
                    },
                    "id": "branch",
                }
            ],
            "inputs": {"mode": {"type": "string", "default": "gui"}},
        }
    }
    report = import_skill(data)
    paths = [path for path, _ in walk(report.skill.steps)]
    assert paths == [
        "steps[0]",
        "steps[0].then[0]",
        "steps[0].else[0]",
        "steps[0].else[1]",
        "steps[0].else[1].then[0]",
    ]
    assert report.skill.step_ids() == ["branch"]
    assert set(SCREEN_PRIMITIVES) >= {
        "click",
        "key",
        "type",
        "focus_window",
        "wait_for",
        "screenshot",
        "assert_visible",
        "scroll",
    }
