"""The repository keeps the layout from CLAUDE.md §13, and every workspace member declares
what it imports: a service image is `uv sync --package <member>` with its dependency closure
and nothing else, so an `slas_*` import outside the closure starts a container that exits
with `ModuleNotFoundError`."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SLAS_IMPORT = re.compile(r"^\s*(?:from|import)\s+(slas_[a-z0-9_]+)", re.MULTILINE)
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def workspace_members() -> dict[str, Path]:
    """Distribution name → member directory, from the root `[tool.uv.workspace]`."""
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members: dict[str, Path] = {}
    for pattern in root["tool"]["uv"]["workspace"]["members"]:
        for directory in sorted(REPO_ROOT.glob(pattern)):
            manifest = directory / "pyproject.toml"
            if manifest.is_file():
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
                members[data["project"]["name"]] = directory
    return members


def declared_dependencies(directory: Path) -> set[str]:
    data = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    names = set()
    for requirement in data["project"].get("dependencies", []):
        match = REQUIREMENT_NAME.match(requirement)
        assert match, (directory, requirement)
        names.add(match.group(0).lower())
    return names


def dependency_closure(name: str, members: dict[str, Path]) -> set[str]:
    closure: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in closure or current not in members:
            continue
        closure.add(current)
        pending.extend(declared_dependencies(members[current]))
    return closure


def modules_of(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.glob("slas_*")
        if path.is_dir() and (path / "__init__.py").is_file()
    )


def imported_slas_modules(module: Path) -> set[str]:
    found: set[str] = set()
    for source in module.rglob("*.py"):
        found.update(SLAS_IMPORT.findall(source.read_text(encoding="utf-8")))
    return found


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


def test_every_workspace_member_declares_the_slas_packages_it_imports() -> None:
    members = workspace_members()
    assert {"slas-sandbox-manager", "slas-orchestrator", "slas-api"} <= set(members)
    module_owner = {
        module.name: name for name, directory in members.items() for module in modules_of(directory)
    }
    assert module_owner["slas_orchestrator"] == "slas-orchestrator"
    missing: list[str] = []
    for name, directory in sorted(members.items()):
        closure = dependency_closure(name, members)
        for module in modules_of(directory):
            for imported in sorted(imported_slas_modules(module)):
                owner = module_owner.get(imported)
                assert owner is not None, f"{module}: {imported} is not a workspace package"
                if owner not in closure:
                    missing.append(f"{name} imports {imported} but does not depend on {owner}")
    assert missing == [], "\n".join(missing)


def test_branding_lives_where_claude_md_says() -> None:
    branding = REPO_ROOT / "packages/slas-kernel/slas_kernel/branding.py"
    assert branding.is_file()
    assert 'PRODUCT_NAME: Final = "SW Local Agent Service"' in branding.read_text()
