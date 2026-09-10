"""The repository layout matches CLAUDE.md §13 and every Python member imports."""

import importlib
from pathlib import Path

import pytest

SECTION_13_PATHS = [
    "CLAUDE.md",
    "README.md",
    "install.sh",
    "docs/DEVELOPMENT_PLAN.md",
    "docs/PROMPTS.md",
    "docs/adr",
    "docs/runbooks",
    "docs/ui",
    "docs/ui-demo",
    "docs/golden-set",
    "apps/webui",
    "apps/api",
    "services/agent-core-orchestrator",
    "services/llm-gateway",
    "services/model-manager",
    "services/sandbox-manager",
    "services/screen-worker",
    "services/git-broker",
    "services/validation-executor",
    "services/factory-executor",
    "services/station-runner",
    "services/local-search-api",
    "services/edge",
    "packages/slas-kernel",
    "packages/slas-schemas",
    "packages/slas-authz",
    "packages/slas-hal",
    "packages/slas-skills",
    "packages/slas-screen",
    "packages/slas-git",
    "packages/slas-diff",
    "packages/slas-triage",
    "packages/slas-rag",
    "packages/slas-sop",
    "packages/slas-eval",
    "packages/slas-cli",
    "plans/schema",
    "plans/primitives",
    "skills/schema",
    "skills/library",
    "templates/factory",
    "images",
    "compose",
    "config/.env.example",
    "tests/unit",
    "tests/integration",
    "tests/hal",
    "tests/screen",
    "tests/skills",
    "tests/eval",
    "tests/e2e",
    "tests/deploy",
]

PYTHON_MODULES = [
    "slas_kernel",
    "slas_schemas",
    "slas_authz",
    "slas_hal",
    "slas_skills",
    "slas_screen",
    "slas_git",
    "slas_diff",
    "slas_triage",
    "slas_rag",
    "slas_sop",
    "slas_eval",
    "slas_cli",
    "slas_api",
    "slas_orchestrator",
    "slas_llm_gateway",
    "slas_model_manager",
    "slas_sandbox_manager",
    "slas_screen_worker",
    "slas_git_broker",
    "slas_validation_executor",
    "slas_factory_executor",
    "slas_station_runner",
    "slas_local_search_api",
]


@pytest.mark.parametrize("relative", SECTION_13_PATHS)
def test_section_13_path_exists(repo_root: Path, relative: str) -> None:
    assert (repo_root / relative).exists(), f"CLAUDE.md §13 names {relative}"


@pytest.mark.parametrize("module", PYTHON_MODULES)
def test_member_imports(module: str) -> None:
    imported = importlib.import_module(module)
    assert imported.__doc__, f"{module} needs a module docstring naming its CLAUDE.md section"


@pytest.mark.parametrize("module", PYTHON_MODULES)
def test_member_ships_type_marker(repo_root: Path, module: str) -> None:
    markers = list(repo_root.glob(f"*/*/{module}/py.typed"))
    assert markers, f"{module} must ship py.typed so mypy --strict checks its consumers"


def test_no_glob_in_workspace_matches_edge(repo_root: Path) -> None:
    """services/edge is a reverse-proxy config, not a Python package (pyproject excludes it)."""
    assert not (repo_root / "services/edge/pyproject.toml").exists()
    assert "services/edge" in (repo_root / "pyproject.toml").read_text(encoding="utf-8")
