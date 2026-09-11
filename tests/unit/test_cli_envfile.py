"""`.env` parsing and private writes: round trips, comments, quoting, mode 0600, atomicity."""

from __future__ import annotations

import stat
from pathlib import Path

from slas_cli.envfile import parse_env, read_env, render_env, write_private_file


def test_parse_ignores_comments_blanks_and_strips_quotes() -> None:
    text = (
        "# header\n\nSLAS_PROFILE=quickstart\nSLAS_DATA_ROOT='/AI/Agent'\nX=\"a b\"\nBROKEN LINE\n"
    )
    assert parse_env(text) == {
        "SLAS_PROFILE": "quickstart",
        "SLAS_DATA_ROOT": "/AI/Agent",
        "X": "a b",
    }


def test_render_round_trips_and_quotes_values_that_need_it() -> None:
    values = {"A": "1", "B": "has space", "C": 'q"uote', "D": "x#y"}
    text = render_env(values, header="written by install.sh\n\nkeep me")
    assert text.startswith("# written by install.sh\n#\n# keep me\n")
    assert parse_env(text) == values


def test_read_env_of_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_env(tmp_path / ".env") == {}


def test_write_private_file_sets_mode_0600_and_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "sub" / ".env"
    write_private_file(target, "A=1\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    write_private_file(target, "A=2\n")
    assert target.read_text(encoding="utf-8") == "A=2\n"
    leftovers = [p for p in target.parent.iterdir() if p.name != ".env"]
    assert leftovers == [], "no temporary file may remain after a write"


def test_write_private_file_honours_an_explicit_mode(tmp_path: Path) -> None:
    target = tmp_path / "root.crt"
    write_private_file(target, "cert", mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
