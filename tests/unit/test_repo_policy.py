"""Mechanical guards for CLAUDE.md §2 and §11 that grep can enforce.

- no `shell=True` anywhere in Python (argv lists only)
- no cloud AI endpoint in code or config (INV-2)
- no `latest` image tag, every GitHub Action pinned to a commit, every CI image pinned
  to a digest (INV-8)
- every JavaScript dependency and every Python dev tool pinned to an exact version (INV-8)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

SKIP_DIRS = {".git", ".venv", ".uv-cache", "node_modules", "dist", ".pytest_cache", ".mypy_cache"}
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".toml", ".yaml", ".yml", ".json", ".sh", ".env", ".cfg"}
# Prose is allowed to name the things we reject; code and config are not.
PROSE = {REPO_ROOT / "CLAUDE.md", REPO_ROOT / "README.md"}

# Assembled at runtime so that this file does not itself match the patterns.
CLOUD_AI_HOSTS = [
    "api." + "openai.com",
    "api." + "anthropic.com",
    "openai." + "azure.com",
    "aiplatform." + "googleapis.com",
    "generativelanguage." + "googleapis.com",
    "bedrock-runtime." + "amazonaws.com",
    "api." + "smith.langchain.com",
    "api." + "wandb.ai",
    "api-inference." + "huggingface.co",
]
SHELL_TRUE = re.compile(r"shell\s*=\s*True")
LATEST_TAG = re.compile(r"(?:image:\s*|FROM\s+|docker run.*\s)\S+:latest\b")
USES_LINE = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
IMAGE_REF = re.compile(r"(?:ghcr\.io|docker\.io)/\S+")
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.]+)?$")


def repo_files(*suffixes: str) -> Iterator[Path]:
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path == THIS_FILE:
            continue
        if SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        if suffixes and path.suffix not in suffixes and path.name not in suffixes:
            continue
        yield path


def test_no_python_uses_shell_true() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in repo_files(".py")
        if SHELL_TRUE.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert offenders == [], f"argv lists only (CLAUDE.md §11): {offenders}"


def test_no_cloud_ai_endpoint_in_code_or_config() -> None:
    offenders = []
    for path in repo_files(*CODE_SUFFIXES):
        if path in PROSE or "docs" in path.relative_to(REPO_ROOT).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for host in CLOUD_AI_HOSTS:
            if host in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {host}")
    assert offenders == [], f"INV-2 forbids cloud AI services: {offenders}"


def test_no_latest_image_tags() -> None:
    offenders = []
    for path in repo_files(".yml", ".yaml", "Dockerfile", "Containerfile", ".sh"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if LATEST_TAG.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"INV-8 forbids `latest` tags: {offenders}"


def workflows() -> list[Path]:
    found = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert found, "at least one workflow is expected"
    return found


def test_every_github_action_is_pinned_to_a_commit() -> None:
    offenders = []
    for workflow in workflows():
        for match in USES_LINE.finditer(workflow.read_text(encoding="utf-8")):
            reference = match.group(1)
            if reference.startswith("./"):
                continue
            if not PINNED_ACTION.match(reference):
                offenders.append(f"{workflow.name}: {reference}")
    assert offenders == [], f"pin actions to a 40-character commit SHA (INV-8): {offenders}"


def test_every_ci_container_image_is_pinned_to_a_digest() -> None:
    offenders = []
    for workflow in workflows():
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                continue  # comments may spell out the human-readable tag
            for reference in IMAGE_REF.findall(line):
                if "@sha256:" not in reference:
                    offenders.append(f"{workflow.name}: {reference}")
    assert offenders == [], f"pin images by digest (INV-8): {offenders}"


def package_jsons() -> list[Path]:
    return [path for path in repo_files("package.json") if path.name == "package.json"]


@pytest.mark.parametrize("field", ["dependencies", "devDependencies"])
def test_javascript_dependencies_are_exact_versions(field: str) -> None:
    offenders = []
    for manifest in package_jsons():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, version in data.get(field, {}).items():
            if not EXACT_VERSION.match(version):
                offenders.append(f"{manifest.relative_to(REPO_ROOT)}: {name}@{version}")
    assert offenders == [], f"pin every dependency exactly (INV-8): {offenders}"


def test_python_dev_tools_are_pinned_exactly() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev_block = text.split("[dependency-groups]", 1)[1].split("[tool.uv]", 1)[0]
    specs = re.findall(r'"([^"]+)"', dev_block)
    assert specs, "the dev dependency group must not be empty"
    loose = [spec for spec in specs if "==" not in spec]
    assert loose == [], f"pin dev tools with == (INV-8): {loose}"


def test_no_dotenv_file_is_committed() -> None:
    assert not (REPO_ROOT / ".env").exists(), (
        "never commit .env; config/.env.example is the template"
    )
