"""The one .env writer: lossless, targeted, atomic."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from slas_schemas.envfile import EnvFile, generate_secret, parse_line, read_env, write_atomic

SAMPLE = """# SW Local Agent Service — configuration
# Deployment profile: quickstart (default) or prod.
SLAS_PROFILE=quickstart

# Where all data lives.
SLAS_DATA_ROOT=/AI/Agent
export SLAS_HTTPS_PORT=443
QUOTED="a value with spaces and a \\"quote\\""
SINGLE='keep $this literal'
TRAILING=value # a comment after the value
EMPTY=
UNKNOWN_TO_US=some-other-tool
"""


def test_parse_and_render_round_trip_is_byte_for_byte() -> None:
    assert EnvFile.parse(SAMPLE).render() == SAMPLE
    assert EnvFile.parse("").render() == ""
    assert EnvFile.parse("A=1").render() == "A=1\n"


def test_values_are_decoded_like_a_shell_would() -> None:
    env = EnvFile.parse(SAMPLE)
    assert env.get("SLAS_PROFILE") == "quickstart"
    assert env.get("SLAS_HTTPS_PORT") == "443"
    assert env.get("QUOTED") == 'a value with spaces and a "quote"'
    assert env.get("SINGLE") == "keep $this literal"
    assert env.get("TRAILING") == "value"
    assert env.get("EMPTY") == ""
    assert env.get("MISSING") is None
    assert "UNKNOWN_TO_US" in env
    assert env.keys()[:3] == ["SLAS_PROFILE", "SLAS_DATA_ROOT", "SLAS_HTTPS_PORT"]


def test_comments_and_blank_lines_have_no_key() -> None:
    assert parse_line("# KEY=not an assignment").key is None
    assert parse_line("").key is None
    assert parse_line("   ").key is None
    assert parse_line("not an assignment").key is None
    assert parse_line("  export A=b").key == "A"


def test_set_replaces_the_last_assignment_in_place_and_touches_nothing_else() -> None:
    env = EnvFile.parse(SAMPLE)
    assert env.set("SLAS_DATA_ROOT", "/srv/slas") is True
    rendered = env.render()
    assert rendered == SAMPLE.replace("SLAS_DATA_ROOT=/AI/Agent", "SLAS_DATA_ROOT=/srv/slas")
    assert env.set("SLAS_DATA_ROOT", "/srv/slas") is False, "an identical value is a no-op"


def test_duplicate_keys_last_one_wins_and_is_the_one_replaced() -> None:
    env = EnvFile.parse("A=1\nA=2\n")
    assert env.get("A") == "2"
    env.set("A", "3")
    assert env.render() == "A=1\nA=3\n"


def test_set_appends_when_missing() -> None:
    env = EnvFile.parse("A=1\n")
    env.set("B", "2")
    assert env.render() == "A=1\nB=2\n"


def test_set_under_marker_creates_the_marker_once_and_groups_keys_under_it() -> None:
    marker = "# managed by Admin → Settings"
    env = EnvFile.parse("A=1\n")
    env.set("SLAS_INSTALLATION_NAME", "Lab 3", under_marker=marker)
    env.set("SLAS_SOP_CHINESE", "zh-Hans", under_marker=marker)
    assert env.render() == (
        'A=1\n\n# managed by Admin → Settings\nSLAS_INSTALLATION_NAME="Lab 3"\n'
        "SLAS_SOP_CHINESE=zh-Hans\n"
    )
    # A later key lands under the marker even when other sections follow it.
    env2 = EnvFile.parse("# managed by Admin → Settings\nX=1\n\n# other\nY=2\n")
    env2.set("Z", "3", under_marker=marker)
    assert env2.render() == "# managed by Admin → Settings\nX=1\nZ=3\n\n# other\nY=2\n"
    # An existing key is replaced in place, wherever it is.
    env2.set("Y", "4", under_marker=marker)
    assert env2.get("Y") == "4"
    assert env2.render().count("Y=") == 1


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "with space",
        'quote " inside',
        "back\\slash",
        "",
        "semi;colon",
        "hash # tag",
        "中文",
    ],
)
def test_values_that_need_quoting_survive_a_round_trip(value: str) -> None:
    env = EnvFile()
    env.set("K", value)
    assert EnvFile.parse(env.render()).get("K") == value


def test_bare_values_are_written_without_quotes() -> None:
    env = EnvFile()
    env.set("K", "/AI/Agent:443,zh-Hant")
    assert env.render() == "K=/AI/Agent:443,zh-Hant\n"


def test_invalid_key_is_refused() -> None:
    with pytest.raises(ValueError, match=r"not a valid \.env key"):
        EnvFile().set("BAD KEY", "x")


def test_remove() -> None:
    env = EnvFile.parse("A=1\nB=2\nA=3\n")
    assert env.remove("A") is True
    assert env.render() == "B=2\n"
    assert env.remove("A") is False


def test_write_atomic_writes_content_and_mode(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    write_atomic(target, "A=1\n", mode=0o600)
    assert target.read_text(encoding="utf-8") == "A=1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    write_atomic(target, "A=2\n", mode=0o644)
    assert read_env(target).get("A") == "2"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == [".env"], "no temp file left behind"


def test_write_atomic_leaves_the_old_file_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".env"
    target.write_text("A=old\n", encoding="utf-8")

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, "A=new\n")
    assert target.read_text(encoding="utf-8") == "A=old\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"], "the temp file was cleaned up"


def test_generate_secret_is_long_urlsafe_and_unique() -> None:
    values = {generate_secret() for _ in range(200)}
    assert len(values) == 200
    for value in values:
        assert len(value) >= 40
        assert value.isascii()
        assert " " not in value and "\n" not in value
    assert len(generate_secret(16)) >= 20
