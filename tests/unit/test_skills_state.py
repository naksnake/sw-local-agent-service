"""ADR-0013: the per-agent enablement record lives beside the skill file, starts off, admits
only the agents the author listed, survives a re-import that does not grow, and never
travels with an export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_skills.exporter import export_skill
from slas_skills.library import LIBRARY, SEL_COLLECT_CLEAR, STATION_LOGIN_BURNIN
from slas_skills.schema import parse_skill
from slas_skills.state import SkillState, SkillStateError, SkillStateStore, skill_risk

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 15, 8, 30, tzinfo=UTC)


def test_a_fresh_import_is_off_everywhere_and_the_record_sits_beside_the_skill_file(
    tmp_path: Path,
) -> None:
    store = SkillStateStore(tmp_path / "Skills" / "library")
    skill = parse_skill(STATION_LOGIN_BURNIN)
    assert store.load(skill.id) is None and store.is_enabled(skill.id, "factory") is False
    state = store.record_import(skill, by="lee", now=NOW)
    assert store.path(skill.id) == tmp_path / "Skills" / "library" / f"{skill.id}.state.json"
    assert store.path(skill.id).is_file()
    assert state.enabled == {} and state.sentence() == "Off for every agent."
    assert state.agents == ["factory"] and state.requires == ["screen", "ssh"]
    assert state.risk == "caution" and skill_risk(skill) == "caution", "ssh is caution"
    on_disk = json.loads(store.path(skill.id).read_text(encoding="utf-8"))
    assert on_disk["imported_by"] == "lee" and on_disk["version"] == "1.0.0"


def test_a_switch_exists_only_for_agents_the_author_listed(tmp_path: Path) -> None:
    store = SkillStateStore(tmp_path)
    skill = parse_skill(STATION_LOGIN_BURNIN)
    store.record_import(skill, by="lee", now=NOW)
    state = store.enable(skill, "factory", by="pat", now=LATER)
    assert state.is_enabled("factory") and store.is_enabled(skill.id, "factory")
    assert state.sentence() == "On for the Factory Agent since 15 September 2026."
    assert store.load(skill.id) == state, "read on use: the file is the record"
    with pytest.raises(SkillStateError) as refused:
        store.enable(skill, "validation", by="pat", now=LATER)
    assert refused.value.message.what_happened == (
        "Log in to the test station and start BurnIn can't be turned on for the Validation Agent."
    )
    assert refused.value.message.likely_cause == "Its author allows only the Factory Agent."
    assert store.is_enabled(skill.id, "validation") is False
    assert store.disable(skill.id, "factory").enabled == {}
    assert store.disable(skill.id, "factory").enabled == {}, "turning off twice is fine"
    with pytest.raises(SkillStateError) as missing:
        store.enable(parse_skill(SEL_COLLECT_CLEAR), "factory", by="pat", now=LATER)
    assert "no record for the skill sel-collect-clear" in missing.value.message.what_happened


def test_a_re_import_keeps_the_switches_unless_the_skill_grew(tmp_path: Path) -> None:
    store = SkillStateStore(tmp_path)
    skill = parse_skill(SEL_COLLECT_CLEAR)
    store.record_import(skill, by="lee", now=NOW)
    store.enable(skill, "validation", by="lee", now=NOW)
    store.enable(skill, "factory", by="lee", now=NOW)

    # A newer version that changes nothing about agents, capabilities or risk keeps them.
    same_shape = SEL_COLLECT_CLEAR["skill"] | {"version": "1.3.0", "description": "tidier"}
    kept = store.record_import(parse_skill({"skill": same_shape}), by="lee", now=LATER)
    assert kept.version == "1.3.0" and set(kept.enabled) == {"validation", "factory"}
    assert kept.reset_reason is None

    # One that adds a destructive step and a new agent starts off, and says why.
    grown = SEL_COLLECT_CLEAR["skill"] | {
        "version": "2.0.0",
        "agents": ["validation", "factory", "coding"],
        "steps": [
            *SEL_COLLECT_CLEAR["skill"]["steps"],
            {"redfish": {"target": "{{ target }}", "action": "power_off"}},
        ],
    }
    reset = store.record_import(parse_skill({"skill": grown}), by="lee", now=LATER)
    assert reset.enabled == {} and reset.risk == "destructive"
    assert reset.reset_reason == (
        "Version 2.0.0 works with more agents, is riskier (caution → destructive) compared with "
        "1.3.0, so it starts off for every agent."
    )
    # Turning it on again clears the note.
    assert (
        store.enable(parse_skill({"skill": grown}), "factory", by="lee", now=LATER).reset_reason
        is None
    )


def test_the_record_never_travels_with_an_export(tmp_path: Path) -> None:
    store = SkillStateStore(tmp_path)
    for data in LIBRARY.values():
        skill = parse_skill(data)
        store.record_import(skill, by="platform", now=NOW)
        for agent in skill.agents:
            store.enable(skill, agent, by="lee", now=NOW)
    for data in LIBRARY.values():
        exported = export_skill(parse_skill(data))
        assert "enabled" not in exported.yaml and "state.json" not in exported.yaml
        assert "imported_by" not in exported.yaml
    # The model round-trips, so a later move to a table (ADR-0013) changes nothing above.
    state = store.load("sel-collect-clear")
    assert state is not None
    assert SkillState.model_validate_json(state.model_dump_json()) == state
