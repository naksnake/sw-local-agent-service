"""Phase 0 ends with two accepted ADRs written to the template in CLAUDE.md §15."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[2] / "docs" / "adr"
REQUIRED_SECTIONS = ("## Context", "## Decision", "## Consequences", "## Invariants touched")
PHASE_0_ADRS = {
    "0001-agent-kernel.md": "ADR-0001",
    "0002-screen-worker-not-host-x11.md": "ADR-0002",
}


def adr_files() -> list[Path]:
    return sorted(path for path in ADR_DIR.glob("*.md") if not path.name.startswith("0000"))


@pytest.mark.parametrize(("filename", "identifier"), sorted(PHASE_0_ADRS.items()))
def test_phase_0_adrs_are_accepted(filename: str, identifier: str) -> None:
    path = ADR_DIR / filename
    assert path.is_file(), f"{filename} is required by docs/DEVELOPMENT_PLAN.md P0"
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"# {identifier}: ")
    assert re.search(r"^Status: accepted$", text, re.MULTILINE), f"{filename} is not accepted"
    assert re.search(r"^Date: \d{4}-\d{2}-\d{2}$", text, re.MULTILINE)


@pytest.mark.parametrize("path", adr_files(), ids=lambda path: path.name)
def test_every_adr_follows_the_template(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in text, f"{path.name} lacks '{section}'"
    assert re.search(r"INV-\d+", text), f"{path.name} must name the invariants it touches"


def test_adr_numbers_are_unique_and_match_filenames() -> None:
    seen: dict[str, str] = {}
    for path in adr_files():
        number = path.name.split("-", 1)[0]
        assert number.isdigit() and len(number) == 4, path.name
        assert number not in seen, f"{path.name} reuses the number of {seen[number]}"
        seen[number] = path.name
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith(f"# ADR-{number}: "), first_line
