"""The toolchain resolver (§10.1), the bundle manifest, `slas toolchain list|add`, images."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from slas_cli.cli import EXIT_OK, EXIT_PROBLEMS, main
from slas_cli.doctor.fakes import FakeHost
from slas_sandbox_manager.images import DEBIAN_BASE, image_files, render_dockerfile
from slas_sandbox_manager.toolchains import (
    BY_ID,
    LANGUAGES,
    Manifest,
    ToolchainError,
    add_toolchain,
    default_manifest,
    detect_languages,
    image_for,
    language_spec,
    load_manifest,
    manifest_path,
    resolve,
    resolve_all,
    save_manifest,
    toolchain_sentence,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_eight_languages_from_claude_md_are_known() -> None:
    assert [spec.label for spec in LANGUAGES] == [
        "Python",
        "C",
        "C++",
        "Rust",
        "Shell",
        "Go",
        "TypeScript",
        "YAML/JSON config",
    ]
    assert language_spec("Python").id == "python" and language_spec("c++").id == "cpp"
    assert language_spec("golang").id == "go" and language_spec(" TS ").id == "typescript"
    with pytest.raises(ToolchainError) as raised:
        language_spec("cobol")
    assert raised.value.message.what_happened == "'cobol' is not a language the Coding Agent knows."
    assert "Python, C, C++, Rust, Shell, Go, TypeScript, YAML/JSON config" in (
        raised.value.message.likely_cause
    )
    for spec in LANGUAGES:
        assert all(check.argv[0] == "slas-check" for check in spec.checks), (
            "argv, never a shell line"
        )


def test_resolver_picks_newest_honours_pins_and_explains_fallbacks() -> None:
    manifest = default_manifest()
    newest = resolve("python", None, manifest)
    assert newest.version == "3.12.6" and newest.honoured
    assert newest.sentence == (
        "Python: no version pinned, so the newest bundled python 3.12.6 is used."
    )
    assert newest.image == "registry.internal/slas/sandbox-python:3.12.6"

    pinned = resolve("python", "3.11.10", manifest)
    assert pinned.version == "3.11.10" and pinned.honoured
    assert pinned.sentence == "Python 3.11.10 pinned; the bundle has it exactly, using 3.11.10."
    prefix = resolve("python", "3.11", manifest)
    assert prefix.version == "3.11.10"
    assert (
        prefix.sentence
        == "Python 3.11 pinned; the bundle has it as the newest 3.11.x, using 3.11.10."
    )

    # The P6 done-when: pin Rust 1.99 → a sentence says it isn't available and 1.80 is used.
    rust = resolve("Rust", "1.99", manifest)
    assert not rust.honoured and rust.version == "1.80.1"
    assert rust.sentence == (
        "Rust 1.99 isn't in the offline toolchain bundle, so the newest bundled 1.80.1 is used "
        "instead."
    )
    assert rust.to_record() == {
        "language": "rust",
        "requested": "1.99",
        "version": "1.80.1",
        "honoured": False,
        "image": "registry.internal/slas/sandbox-rust:1.80.1",
        "sentence": rust.sentence,
    }

    both = resolve_all({"python": None, "rust": "1.99", "py": "3.11"}, manifest)
    assert [r.language for r in both] == ["python", "rust"], "aliases collapse to one entry"
    assert toolchain_sentence(both) == (
        "Toolchain: Python 3.12.6 and Rust 1.80.1. Rust 1.99 isn't in the offline toolchain "
        "bundle, so the newest bundled 1.80.1 is used instead."
    )
    assert toolchain_sentence([newest]) == "Toolchain: Python 3.12.6."
    assert toolchain_sentence([]) == "No language was chosen."

    empty = Manifest(toolchains={})
    with pytest.raises(ToolchainError, match="No Python toolchain is in the offline bundle"):
        resolve("python", None, empty)


def test_manifest_round_trips_and_rejects_bad_shapes(tmp_path: Path) -> None:
    assert load_manifest(tmp_path).source == "<bundled default>", "no file → bundled default"
    manifest = default_manifest()
    assert manifest.add("python", "3.13.1") and not manifest.add("python", "3.13.1")
    assert manifest.newest("python") == "3.13.1"
    path = save_manifest(tmp_path, manifest)
    assert path == manifest_path(tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded.versions("python") == ["3.11.10", "3.12.6", "3.13.1"]
    assert loaded.companions["node"] == "22.22.2"
    assert loaded.sentences()[0] == "Python (python): 3.11.10, 3.12.6, 3.13.1; newest 3.13.1."
    assert Manifest(toolchains={"c": ["13.2.0"]}).sentences()[0] == "Python: not in the bundle."

    with pytest.raises(ToolchainError, match="is not a version number"):
        manifest.add("python", "latest")
    with pytest.raises(ToolchainError, match="could not be read"):
        Manifest.from_mapping(["nope"])
    with pytest.raises(ToolchainError, match="lists no versions for Rust"):
        Manifest.from_mapping({"toolchains": {"rust": []}})
    with pytest.raises(ToolchainError, match="is not a version number"):
        Manifest.from_mapping({"toolchains": {"rust": ["stable"]}})
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ToolchainError, match="could not be read"):
        load_manifest(tmp_path)


def test_add_toolchain_copies_the_archive_and_records_it(tmp_path: Path) -> None:
    archive = tmp_path / "python-3.13.1.tar.zst"
    archive.write_bytes(b"not really a toolchain")
    sentence = add_toolchain(tmp_path, "python", "3.13.1", archive)
    assert sentence == "Added Python 3.13.1 to the bundle; it is now the newest."
    assert (
        tmp_path / "Toolchains" / "python" / "3.13.1" / archive.name
    ).read_bytes() == archive.read_bytes()
    assert load_manifest(tmp_path).newest("python") == "3.13.1"
    assert add_toolchain(tmp_path, "python", "3.13.1", archive).endswith(
        "the archive was replaced."
    )
    assert add_toolchain(tmp_path, "python", "3.10.14", archive).endswith("available for pinning.")
    with pytest.raises(ToolchainError, match="is not a file"):
        add_toolchain(tmp_path, "python", "3.9.1", tmp_path / "missing.tar")


def test_detect_languages_from_fences_filenames_and_keywords() -> None:
    plan = (
        "# Fan controller\n\n- Parse `config.yaml` and expose it in `fan_ctl.py`.\n"
        "- Port the hot loop to Rust (`src/main.rs`) and call it from Python.\n\n"
        "```python\nimport fan_ctl\n```\n"
    )
    assert detect_languages(plan)[:2] == ["python", "rust"]
    assert "config" in detect_languages(plan)
    assert detect_languages("nothing to see here") == []
    assert detect_languages("```cobol\nDISPLAY 'hi'\n```") == []


def test_slas_toolchain_list_and_add(tmp_path: Path) -> None:
    out = io.StringIO()
    code = main(
        ["toolchain", "--data-root", str(tmp_path), "list"], host=FakeHost.healthy(), stdout=out
    )
    assert code == EXIT_OK
    text = out.getvalue()
    assert text.startswith("Toolchains in the offline bundle (<bundled default>):\n")
    assert "  Rust (rustc): 1.80.1; newest 1.80.1.\n" in text
    assert text.endswith("pin one to use it if the bundle has it.\n")

    archive = tmp_path / "rust-1.81.0.tar.zst"
    archive.write_bytes(b"x")
    out = io.StringIO()
    code = main(
        ["toolchain", "--data-root", str(tmp_path), "add", "rust", "1.81.0", str(archive)],
        host=FakeHost.healthy(),
        stdout=out,
    )
    assert (
        code == EXIT_OK
        and out.getvalue() == "Added Rust 1.81.0 to the bundle; it is now the newest.\n"
    )
    saved = json.loads(manifest_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["toolchains"]["rust"] == ["1.80.1", "1.81.0"]

    out = io.StringIO()
    code = main(
        ["toolchain", "--data-root", str(tmp_path), "add", "cobol", "1.0.0", str(archive)],
        host=FakeHost.healthy(),
        stdout=out,
    )
    assert code == EXIT_PROBLEMS
    assert out.getvalue().splitlines()[0] == "'cobol' is not a language the Coding Agent knows."
    assert len(out.getvalue().splitlines()) == 3, "three-part error"


def test_sandbox_dockerfiles_are_rendered_from_code() -> None:
    files = image_files()
    assert set(files) == {f"sandbox-{spec.id}/Dockerfile" for spec in LANGUAGES}
    for rel, content in files.items():
        path = REPO_ROOT / "images" / rel
        assert path.read_text(encoding="utf-8") == content, path
        assert f"FROM {DEBIAN_BASE}" in content and ":latest" not in content
        assert "COPY toolchains/" in content and "--network" not in content
        assert 'CMD ["sleep", "infinity"]' in content
        assert "GIT_CONFIG_GLOBAL=/etc/slas/gitconfig" in content
    rust = render_dockerfile(BY_ID["rust"], "1.80.1")
    assert "COPY toolchains/rust/1.80.1/ /opt/toolchain/" in rust
    assert "SLAS_LANGUAGE=rust SLAS_TOOLCHAIN_VERSION=1.80.1" in rust
    assert image_for("rust", "1.80.1") == "registry.internal/slas/sandbox-rust:1.80.1"
    script = REPO_ROOT / "images" / "sandbox-common" / "slas-check.sh"
    assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script.read_text(encoding="utf-8")
