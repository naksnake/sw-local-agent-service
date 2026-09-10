"""Every ADR follows docs/adr/0000-template.md and P0's two ADRs are accepted."""

import re
from pathlib import Path

import pytest

REQUIRED_SECTIONS = ("## Context", "## Decision", "## Consequences", "## Invariants touched")
STATUS_RE = re.compile(r"^Status:\s*(proposed|accepted|superseded by ADR-\d{4})\s*$", re.M)
DATE_RE = re.compile(r"^Date:\s*\d{4}-\d{2}-\d{2}\s*$", re.M)


def adr_files(repo_root: Path) -> list[Path]:
    return sorted(
        p
        for p in (repo_root / "docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md")
        if not p.name.startswith("0000")
    )


def test_p0_adrs_exist(repo_root: Path) -> None:
    names = {p.name for p in adr_files(repo_root)}
    assert "0001-agent-kernel.md" in names
    assert "0002-screen-worker-not-host-x11.md" in names


@pytest.mark.parametrize("name", ["0001-agent-kernel.md", "0002-screen-worker-not-host-x11.md"])
def test_p0_adrs_are_accepted(repo_root: Path, name: str) -> None:
    text = (repo_root / "docs/adr" / name).read_text(encoding="utf-8")
    match = STATUS_RE.search(text)
    assert match is not None, f"{name} has no Status line"
    assert match.group(1) == "accepted"


def test_every_adr_follows_the_template(repo_root: Path) -> None:
    files = adr_files(repo_root)
    assert files, "docs/adr has no ADRs"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert text.startswith(f"# ADR-{path.name[:4]}: "), (
            f"{path.name} title must start with its number"
        )
        assert STATUS_RE.search(text), (
            f"{path.name}: Status must be proposed, accepted or superseded"
        )
        assert DATE_RE.search(text), f"{path.name}: Date must be YYYY-MM-DD"
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{path.name} lacks {section}"
        assert re.search(r"INV-\d+", text), f"{path.name} must name the invariants it touches"


def test_adr_numbers_are_unique(repo_root: Path) -> None:
    numbers = [p.name[:4] for p in adr_files(repo_root)]
    assert len(numbers) == len(set(numbers))
