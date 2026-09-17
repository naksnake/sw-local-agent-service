"""The offline toolchain bundle and the resolver from CLAUDE.md §10.1.

The user picks languages only; a version is optional. The resolver picks the newest bundled
version, honours a pinned version when the bundle has it, and otherwise says so in one
sentence and falls back — it never guesses a version that isn't installed.

Standard library only: `slas toolchain list|add` runs on the bare host before anything is
installed (see tests/unit/test_host_cli_is_stdlib_only.py), and the Coding Agent imports the
same module. The bundle manifest is JSON at `${SLAS_DATA_ROOT}/Toolchains/manifest.json`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from slas_schemas.errors import ThreePartMessage

MANIFEST_NAME: Final = "manifest.json"
#: Sandbox images are tagged `<registry>/slas/sandbox-<language>:<version>`, the same shape
#: `install.sh --build` gives every first-party image (`${SLAS_REGISTRY}/slas/<name>:<tag>`,
#: ADR-0014). The registry label comes from SLAS_SANDBOX_REGISTRY; "local" is what `--build`
#: tags with.
REGISTRY_ENV: Final = "SLAS_SANDBOX_REGISTRY"
DEFAULT_REGISTRY: Final = "local"
_VERSION = re.compile(r"^\d+(?:\.\d+){0,3}$")


@dataclass(frozen=True)
class Check:
    kind: str  # lint · type · build · test · validate
    argv: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class LanguageSpec:
    id: str
    label: str
    tool: str  # what the version number refers to
    aliases: tuple[str, ...]
    extensions: tuple[str, ...]
    keywords: tuple[str, ...]
    checks: tuple[Check, ...]
    #: Bundled alongside, at a fixed version (recorded on the ticket with the main tool).
    companions: tuple[str, ...] = ()


def _check(kind: str, description: str) -> Check:
    # Every image ships `slas-check`, which runs the language's tool over the workspace;
    # the plan shows the description so the person knows what actually runs.
    return Check(kind=kind, argv=("slas-check", kind), description=description)


LANGUAGES: Final[tuple[LanguageSpec, ...]] = (
    LanguageSpec(
        "python",
        "Python",
        "python",
        ("py", "python3"),
        (".py", ".pyi"),
        ("pytest", "pyproject", "pip ", "uv ", "def ", "import "),
        (_check("lint", "ruff check ."), _check("type", "mypy ."), _check("test", "pytest -q")),
        companions=("ruff", "mypy", "pytest"),
    ),
    LanguageSpec(
        "c",
        "C",
        "gcc",
        ("gcc",),
        (".c", ".h"),
        ("#include <stdio.h>", "gcc ", "makefile"),
        (_check("build", "make"), _check("test", "make test")),
        companions=("make", "clang"),
    ),
    LanguageSpec(
        "cpp",
        "C++",
        "g++",
        ("c++", "cxx", "g++"),
        (".cpp", ".cc", ".hpp", ".cxx"),
        ("#include <iostream>", "g++ ", "cmake", "std::"),
        (_check("build", "cmake --build build"), _check("test", "ctest --test-dir build")),
        companions=("cmake", "clang"),
    ),
    LanguageSpec(
        "rust",
        "Rust",
        "rustc",
        ("rs", "cargo"),
        (".rs",),
        ("cargo ", "cargo.toml", "fn main", "rustc"),
        (
            _check("lint", "cargo clippy -- -D warnings"),
            _check("build", "cargo build"),
            _check("test", "cargo test"),
        ),
        companions=("cargo", "clippy", "rustfmt"),
    ),
    LanguageSpec(
        "shell",
        "Shell",
        "bash",
        ("bash", "sh"),
        (".sh", ".bash"),
        ("#!/bin/bash", "#!/usr/bin/env bash", "shellcheck", "set -euo"),
        (_check("lint", "shellcheck on every *.sh"), _check("test", "bats tests/")),
        companions=("shellcheck", "bats"),
    ),
    LanguageSpec(
        "go",
        "Go",
        "go",
        ("golang",),
        (".go",),
        ("go mod", "package main", "go test", "func main"),
        (
            _check("lint", "go vet ./..."),
            _check("build", "go build ./..."),
            _check("test", "go test ./..."),
        ),
    ),
    LanguageSpec(
        "typescript",
        "TypeScript",
        "typescript",
        ("ts", "tsx", "node"),
        (".ts", ".tsx"),
        ("tsc", "package.json", "npm ", "pnpm ", "node "),
        (_check("type", "tsc --noEmit"), _check("test", "npm test")),
        companions=("node",),
    ),
    LanguageSpec(
        "config",
        "YAML/JSON config",
        "yamllint",
        ("yaml", "json", "yml"),
        (".yaml", ".yml", ".json"),
        ("yamllint", "jsonschema", "$schema"),
        (_check("lint", "yamllint ."), _check("validate", "every *.json parses; schemas apply")),
        companions=("jsonschema",),
    ),
)
BY_ID: Final[dict[str, LanguageSpec]] = {spec.id: spec for spec in LANGUAGES}

#: What the offline bundle ships. `slas toolchain add` extends the manifest on a host.
DEFAULT_MANIFEST: Final[dict[str, object]] = {
    "version": 1,
    "toolchains": {
        "python": ["3.11.10", "3.12.6"],
        "c": ["13.2.0"],
        "cpp": ["13.2.0"],
        "rust": ["1.80.1"],
        "shell": ["5.2.21"],
        "go": ["1.23.1"],
        "typescript": ["5.9.3"],
        "config": ["1.35.1"],
    },
    "companions": {
        "node": "22.22.2",
        "ruff": "0.16.7",
        "mypy": "2.3.1",
        "pytest": "9.1.1",
        "jsonschema": "4.23.0",
    },
}


class ToolchainError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def language_spec(language: str) -> LanguageSpec:
    key = language.strip().lower()
    for spec in LANGUAGES:
        if key in (spec.id, spec.label.lower(), *spec.aliases):
            return spec
    known = ", ".join(spec.label for spec in LANGUAGES)
    raise ToolchainError(
        ThreePartMessage(
            f"{language!r} is not a language the Coding Agent knows.",
            f"The bundled toolchains cover {known}.",
            "Pick one of those; a new language needs a sandbox image and an ADR.",
        )
    )


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


@dataclass
class Manifest:
    toolchains: dict[str, list[str]] = field(default_factory=dict)
    companions: dict[str, str] = field(default_factory=dict)
    source: str = "<bundled default>"

    @classmethod
    def from_mapping(cls, data: object, *, source: str = "<memory>") -> Manifest:
        if not isinstance(data, dict) or not isinstance(data.get("toolchains"), dict):
            raise ToolchainError(
                ThreePartMessage(
                    f"The toolchain manifest in {source} could not be read.",
                    "It must be a JSON object with a `toolchains` mapping of language → versions.",
                    "Restore it from the bundle, or run `slas toolchain add` to rebuild it.",
                )
            )
        toolchains: dict[str, list[str]] = {}
        for language, versions in data["toolchains"].items():
            spec = language_spec(str(language))
            if not isinstance(versions, list) or not versions:
                raise ToolchainError(
                    ThreePartMessage(
                        f"{source} lists no versions for {spec.label}.",
                        "Every language in the manifest needs at least one bundled version.",
                        f"Add one with `slas toolchain add {spec.id} <version> <archive>`.",
                    )
                )
            for version in versions:
                if not _VERSION.match(str(version)):
                    raise ToolchainError(
                        ThreePartMessage(
                            f"{spec.label} version {version!r} in {source} is not a version "
                            "number.",
                            "Versions are dotted numbers such as 3.12.6.",
                            "Fix the manifest entry.",
                        )
                    )
            toolchains[spec.id] = sorted({str(v) for v in versions}, key=version_key)
        companions_raw = data.get("companions", {})
        companions = (
            {str(k): str(v) for k, v in companions_raw.items()}
            if isinstance(companions_raw, dict)
            else {}
        )
        return cls(toolchains=toolchains, companions=companions, source=source)

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "toolchains": {k: list(v) for k, v in self.toolchains.items()},
            "companions": dict(self.companions),
        }

    def versions(self, language: str) -> list[str]:
        return list(self.toolchains.get(language_spec(language).id, []))

    def newest(self, language: str) -> str | None:
        versions = self.versions(language)
        return versions[-1] if versions else None

    def add(self, language: str, version: str) -> bool:
        spec = language_spec(language)
        if not _VERSION.match(version):
            raise ToolchainError(
                ThreePartMessage(
                    f"{version!r} is not a version number.",
                    "Versions are dotted numbers such as 3.12.6.",
                    "Give the version the archive contains.",
                )
            )
        versions = self.toolchains.setdefault(spec.id, [])
        if version in versions:
            return False
        versions.append(version)
        versions.sort(key=version_key)
        return True

    def sentences(self) -> list[str]:
        lines: list[str] = []
        for spec in LANGUAGES:
            versions = self.toolchains.get(spec.id)
            if not versions:
                lines.append(f"{spec.label}: not in the bundle.")
                continue
            newest = versions[-1]
            listed = ", ".join(versions)
            lines.append(f"{spec.label} ({spec.tool}): {listed}; newest {newest}.")
        return lines


def default_manifest() -> Manifest:
    return Manifest.from_mapping(DEFAULT_MANIFEST, source="<bundled default>")


def manifest_path(data_root: Path) -> Path:
    return data_root / "Toolchains" / MANIFEST_NAME


def load_manifest(data_root: Path) -> Manifest:
    return load_manifest_file(manifest_path(data_root))


def load_manifest_file(path: Path) -> Manifest:
    """The manifest at `path` (SLAS_TOOLCHAIN_MANIFEST); the bundled default when absent."""
    if not path.is_file():
        return default_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolchainError(
            ThreePartMessage(
                f"The toolchain manifest at {path} could not be read.",
                str(exc),
                "Restore it from the bundle, or delete it to fall back to the bundled default.",
            )
        ) from exc
    return Manifest.from_mapping(data, source=str(path))


def save_manifest(data_root: Path, manifest: Manifest) -> Path:
    path = manifest_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest.to_mapping(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def add_toolchain(data_root: Path, language: str, version: str, archive: Path) -> str:
    """Copy an archive into Toolchains/<language>/<version>/ and record it. Returns a sentence."""
    spec = language_spec(language)
    if not archive.is_file():
        raise ToolchainError(
            ThreePartMessage(
                f"{archive} is not a file.",
                "A toolchain is added from an archive that was copied onto this host.",
                "Check the path and try again.",
            )
        )
    manifest = load_manifest(data_root)
    added = manifest.add(spec.id, version)
    target_dir = data_root / "Toolchains" / spec.id / version
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, target_dir / archive.name)
    save_manifest(data_root, manifest)
    if not added:
        return f"{spec.label} {version} was already in the bundle; the archive was replaced."
    return (
        f"Added {spec.label} {version} to the bundle; it is now "
        f"{'the newest' if manifest.newest(spec.id) == version else 'available for pinning'}."
    )


# --- resolution ---------------------------------------------------------------------------


def sandbox_registry(registry: str | None = None) -> str:
    """The sandbox images' registry label: the argument, else SLAS_SANDBOX_REGISTRY, else local."""
    chosen = (registry or os.environ.get(REGISTRY_ENV, "")).strip().rstrip("/")
    return chosen or DEFAULT_REGISTRY


def image_for(language: str, version: str, *, registry: str | None = None) -> str:
    """One sandbox image per language and toolchain version (INV-8: never latest).

    `<registry>/slas/sandbox-<language>:<version>` — the tag `python -m
    slas_sandbox_manager.images list` prints and `install.sh --build` builds.
    """
    return f"{sandbox_registry(registry)}/slas/sandbox-{language_spec(language).id}:{version}"


@dataclass(frozen=True)
class Resolution:
    language: str
    label: str
    requested: str | None
    version: str
    honoured: bool
    sentence: str
    image: str
    checks: tuple[Check, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "language": self.language,
            "label": self.label,
            "requested": self.requested,
            "version": self.version,
            "honoured": self.honoured,
            "image": self.image,
            "sentence": self.sentence,
        }


def resolve(
    language: str, requested: str | None, manifest: Manifest, *, registry: str | None = None
) -> Resolution:
    spec = language_spec(language)
    versions = manifest.versions(spec.id)
    if not versions:
        raise ToolchainError(
            ThreePartMessage(
                f"No {spec.label} toolchain is in the offline bundle.",
                f"The manifest at {manifest.source} lists none.",
                f"Add one with `slas toolchain add {spec.id} <version> <archive>`.",
            )
        )
    newest = versions[-1]
    wanted = (requested or "").strip()
    if not wanted:
        return Resolution(
            spec.id,
            spec.label,
            None,
            newest,
            True,
            f"{spec.label}: no version pinned, so the newest bundled {spec.tool} {newest} is used.",
            image_for(spec.id, newest, registry=registry),
            spec.checks,
        )
    exact = [v for v in versions if v == wanted]
    prefix = [v for v in versions if v.startswith(wanted + ".")]
    if exact or prefix:
        chosen = exact[0] if exact else prefix[-1]
        how = "exactly" if exact else f"as the newest {wanted}.x"
        return Resolution(
            spec.id,
            spec.label,
            wanted,
            chosen,
            True,
            f"{spec.label} {wanted} pinned; the bundle has it {how}, using {chosen}.",
            image_for(spec.id, chosen, registry=registry),
            spec.checks,
        )
    return Resolution(
        spec.id,
        spec.label,
        wanted,
        newest,
        False,
        f"{spec.label} {wanted} isn't in the offline toolchain bundle, so the newest bundled "
        f"{newest} is used instead.",
        image_for(spec.id, newest, registry=registry),
        spec.checks,
    )


def resolve_all(
    choices: dict[str, str | None], manifest: Manifest, *, registry: str | None = None
) -> list[Resolution]:
    """Resolve every chosen language, in the order given; duplicates collapse."""
    seen: set[str] = set()
    out: list[Resolution] = []
    for language, requested in choices.items():
        spec = language_spec(language)
        if spec.id in seen:
            continue
        seen.add(spec.id)
        out.append(resolve(spec.id, requested, manifest, registry=registry))
    return out


def toolchain_sentence(resolutions: list[Resolution]) -> str:
    if not resolutions:
        return "No language was chosen."
    parts = [f"{r.label} {r.version}" for r in resolutions]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    fallbacks = [r for r in resolutions if not r.honoured]
    head = f"Toolchain: {listed}."
    if fallbacks:
        return head + " " + " ".join(r.sentence for r in fallbacks)
    return head


# --- detection -----------------------------------------------------------------------------

_FENCE = re.compile(r"^```\s*([A-Za-z+#]+)", re.MULTILINE)
_EXT = re.compile(r"(?<![\w/])[\w./-]+(\.[A-Za-z]{1,4})\b")


def detect_languages(text: str) -> list[str]:
    """Languages a plan talks about, from fenced code blocks, file names and keywords."""
    found: dict[str, int] = {}
    lowered = text.lower()
    for match in _FENCE.finditer(text):
        try:
            spec = language_spec(match.group(1))
        except ToolchainError:
            continue
        found[spec.id] = found.get(spec.id, 0) + 3
    for match in _EXT.finditer(text):
        ext = match.group(1).lower()
        for spec in LANGUAGES:
            if ext in spec.extensions:
                found[spec.id] = found.get(spec.id, 0) + 2
    for spec in LANGUAGES:
        for keyword in spec.keywords:
            if keyword.lower() in lowered:
                found[spec.id] = found.get(spec.id, 0) + 1
    ordered = sorted(found, key=lambda lang: (-found[lang], [s.id for s in LANGUAGES].index(lang)))
    return ordered
