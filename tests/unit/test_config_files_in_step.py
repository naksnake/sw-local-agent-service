"""Shipped YAML files are rendered from code; these tests fail when either side drifts."""

from __future__ import annotations

from pathlib import Path

from slas_hal.primitives import render_plan_schema, render_primitives_yaml
from slas_llm_gateway.consensus import (
    CONSENSUS_FILE_HEADER,
    DEFAULT_CONSENSUS,
    render_consensus_yaml,
)
from slas_llm_gateway.redaction import (
    DEFAULT_REDACTION,
    REDACTION_FILE_HEADER,
    render_redaction_yaml,
)
from slas_model_manager.registry import EXAMPLE_REGISTRY, REGISTRY_FILE_HEADER, render_registry_yaml
from slas_validation_executor.guardrails import (
    DEFAULT_GUARDRAILS,
    GUARDRAILS_FILE_HEADER,
    render_guardrails_yaml,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_guardrails_yaml_is_in_step() -> None:
    expected = render_guardrails_yaml(DEFAULT_GUARDRAILS, header=GUARDRAILS_FILE_HEADER)
    assert (REPO_ROOT / "config" / "guardrails.yaml").read_text(encoding="utf-8") == expected
    assert "max_cycles_per_run: 100\nmin_settle_s: 10\nmin_ac_settle_s: 30\n" in expected
    assert "requires_approval: [ac_cycle, firmware_flash, secure_erase, bios_reset, " in expected


def test_bmc_quirks_yaml_is_in_step() -> None:
    from slas_hal.quirks import DEFAULT_QUIRKS, QUIRKS_FILE_HEADER, render_quirks_yaml

    expected = render_quirks_yaml(DEFAULT_QUIRKS, header=QUIRKS_FILE_HEADER)
    assert (REPO_ROOT / "config" / "bmc-quirks.yaml").read_text(encoding="utf-8") == expected
    assert "  - id: dmtf-defaults\n" in expected and "  - id: slas-fixture\n" in expected
    assert '      bdf_path: "Oem.Slas.BDF"\n      sel_page_size: 2\n' in expected


def test_plan_schema_and_primitives_are_in_step() -> None:
    schema_path = REPO_ROOT / "plans" / "schema" / "plan.schema.json"
    assert schema_path.read_text(encoding="utf-8") == render_plan_schema()
    primitives_path = REPO_ROOT / "plans" / "primitives" / "validation.yaml"
    assert primitives_path.read_text(encoding="utf-8") == render_primitives_yaml()
    assert '"primitive": {\n              "const": "power_cycle"' in render_plan_schema()
    assert "  power_cycle:\n" in render_primitives_yaml()


def test_consensus_yaml_is_in_step() -> None:
    expected = render_consensus_yaml(DEFAULT_CONSENSUS, header=CONSENSUS_FILE_HEADER)
    assert (REPO_ROOT / "config" / "consensus.yaml").read_text(encoding="utf-8") == expected
    assert "  ticket_diagnosis:\n    decision: ticket_diagnosis" in expected
    assert "    fields:\n      - owner\n      - severity\n      - root_cause" in expected


def test_redaction_yaml_is_in_step() -> None:
    expected = render_redaction_yaml(DEFAULT_REDACTION, header=REDACTION_FILE_HEADER)
    assert (REPO_ROOT / "config" / "redaction.yaml").read_text(encoding="utf-8") == expected
    assert expected.count("  - name: ") == 9


def test_models_example_yaml_is_in_step() -> None:
    expected = render_registry_yaml(EXAMPLE_REGISTRY, header=REGISTRY_FILE_HEADER)
    path = REPO_ROOT / "services" / "model-manager" / "models.example.yaml"
    assert path.read_text(encoding="utf-8") == expected
    assert "roles:\n  coder: qwen2.5-coder-32b-awq" in expected
    assert "voters:\n  - qwen2.5-coder-32b-awq\n  - deepseek-v3-fp8\n  - kimi-k2-awq\n" in expected


def test_factory_yaml_is_in_step() -> None:
    from slas_factory_executor.settings import (
        DEFAULT_FACTORY_SETTINGS,
        FACTORY_FILE_HEADER,
        render_factory_yaml,
    )

    expected = render_factory_yaml(DEFAULT_FACTORY_SETTINGS, header=FACTORY_FILE_HEADER)
    assert (REPO_ROOT / "config" / "factory.yaml").read_text(encoding="utf-8") == expected
    assert "screenshot_retention:\n  keep_days: 30\n  keep_failed_days: 180\n" in expected
    assert "enrolment_code_ttl_minutes: 15\nenrolment_max_attempts: 5\nvnc_port: 5900\n" in expected
