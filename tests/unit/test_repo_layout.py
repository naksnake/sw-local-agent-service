"""The repository keeps the layout from CLAUDE.md §13."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DIRECTORIES = [
    "apps/api",
    "apps/webui",
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
    "plans",
    "skills",
    "templates/factory",
    "images",
    "compose",
    "config",
    "tests/unit",
    "tests/integration",
    "tests/hal",
    "tests/screen",
    "tests/skills",
    "tests/eval",
    "tests/e2e",
    "tests/deploy",
    "docs/adr",
    "docs/runbooks",
    "docs/ui",
    "docs/ui-demo",
    "docs/golden-set",
]

FILES = [
    "CLAUDE.md",
    "README.md",
    "install.sh",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
    "playwright.config.ts",
    "config/.env.example",
    "docs/DEVELOPMENT_PLAN.md",
    "docs/PROMPTS.md",
    ".github/workflows/ci.yml",
]


@pytest.mark.parametrize("relative", DIRECTORIES)
def test_directory_exists(relative: str) -> None:
    assert (REPO_ROOT / relative).is_dir(), f"{relative} is missing (CLAUDE.md §13)"


@pytest.mark.parametrize("relative", FILES)
def test_file_exists(relative: str) -> None:
    assert (REPO_ROOT / relative).is_file(), f"{relative} is missing"


def test_every_python_package_has_a_typed_module_matching_its_name() -> None:
    for package_dir in sorted((REPO_ROOT / "packages").glob("slas-*")):
        module = package_dir / package_dir.name.replace("-", "_")
        assert (package_dir / "pyproject.toml").is_file(), package_dir
        assert (module / "__init__.py").is_file(), module
        assert (module / "py.typed").is_file(), f"{module} must ship py.typed"


def test_branding_lives_where_claude_md_says() -> None:
    branding = REPO_ROOT / "packages/slas-kernel/slas_kernel/branding.py"
    assert branding.is_file()
    assert 'PRODUCT_NAME: Final = "SW Local Agent Service"' in branding.read_text()
