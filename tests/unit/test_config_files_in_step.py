"""Shipped YAML files are rendered from code; these tests fail when either side drifts."""

from __future__ import annotations

from pathlib import Path

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

REPO_ROOT = Path(__file__).resolve().parents[2]


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
