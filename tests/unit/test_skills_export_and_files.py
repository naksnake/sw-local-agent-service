"""EXPORT with secrets stripped and a content hash; the shipped schema and library in step."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from slas_skills.exporter import export_skill
from slas_skills.jsonschema import build_skill_schema, render_skill_schema
from slas_skills.library import LIBRARY, LIBRARY_HEADER, STATION_LOGIN_BURNIN
from slas_skills.primitives import PRIMITIVES
from slas_skills.schema import parse_skill
from slas_skills.yamlout import render_skill_yaml, skill_to_mapping

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_export_strips_secret_defaults_and_hashes_the_content() -> None:
    data = copy.deepcopy(STATION_LOGIN_BURNIN)
    data["skill"]["inputs"]["password"]["default"] = "hunter2hunter2"
    skill = parse_skill(data)
    exported = export_skill(skill)
    assert "hunter2hunter2" not in exported.yaml and "hunter2hunter2" not in exported.canonical_json
    assert exported.yaml.splitlines()[1] == f"# sha256: {exported.sha256}"
    assert exported.sentence().startswith("Exported station-login-burnin (")
    # The hash depends only on the skill's content, not on the secret that was stripped.
    assert export_skill(parse_skill(STATION_LOGIN_BURNIN)).sha256 == exported.sha256
    # What was exported imports back unchanged.
    reimported = parse_skill(json.loads(exported.canonical_json))
    assert skill_to_mapping(reimported) == skill_to_mapping(parse_skill(STATION_LOGIN_BURNIN))
    assert export_skill(reimported).sha256 == exported.sha256


def test_library_files_are_rendered_from_code() -> None:
    for skill_id, mapping in LIBRARY.items():
        path = REPO_ROOT / "skills" / "library" / f"{skill_id}.skill.yaml"
        expected = render_skill_yaml(mapping, header=LIBRARY_HEADER)
        assert path.read_text(encoding="utf-8") == expected, path
        # The rendered file uses the §6.3 style: one step per line as `- verb: { … }`.
        assert (
            "    - focus_window: { title: Login }" in expected or skill_id != "station-login-burnin"
        )
        assert (
            '    - redfish: { target: "{{ target }}", action: get_sel }' in expected
            or skill_id != "sel-collect-clear"
        )


def test_yaml_renderer_matches_the_parsed_skill_shape() -> None:
    skill = parse_skill(STATION_LOGIN_BURNIN)
    text = render_skill_yaml(skill_to_mapping(skill))
    assert text.startswith("skill:\n  id: station-login-burnin\n")
    assert "  requires: [screen, ssh]\n" in text
    assert "    password: { type: secret, required: true }\n" in text
    ssh_line = '    - ssh: { target: "{{ station }}", command: [burnin-ctl, status, --json] }\n'
    assert ssh_line + "      id: status\n" in text
    assert "  outputs:\n    burnin_status: { from: status }\n" in text


def test_schema_file_is_rendered_from_the_primitive_table() -> None:
    path = REPO_ROOT / "skills" / "schema" / "skill.schema.json"
    assert path.read_text(encoding="utf-8") == render_skill_schema()
    schema = build_skill_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    variants = schema["$defs"]["step"]["oneOf"]
    verbs = [next(iter(v["required"])) for v in variants]
    assert verbs == list(PRIMITIVES)
    assert "shell" not in verbs
    for variant in variants:
        assert variant["additionalProperties"] is False
        verb = variant["required"][0]
        assert variant["properties"][verb]["additionalProperties"] is False
    redfish = next(v for v in variants if v["required"] == ["redfish"])["properties"]["redfish"]
    assert redfish["properties"]["action"]["enum"] == sorted(
        [
            "get_power_state",
            "get_sel",
            "get_inventory",
            "power_on",
            "power_off",
            "force_off",
            "graceful_restart",
        ]
    )
    click = next(v for v in variants if v["required"] == ["click"])["properties"]["click"]
    assert {"required": ["x", "y"]} in click["anyOf"]
    skill_def = schema["$defs"]["skill"]
    assert skill_def["properties"]["steps"]["maxItems"] == 200
    assert skill_def["properties"]["requires"]["items"]["enum"] == [
        "files",
        "network",
        "redfish",
        "screen",
        "ssh",
    ]
    assert skill_def["required"] == ["id", "name", "version", "agents", "steps"]
