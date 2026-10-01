"""One sandbox image per language and toolchain version, rendered from code into images/.

Two ways to build the same image (docs/api-contract-round-2.md §4, ADR-0014, ADR-0015):

    connected   the committed Dockerfiles. Each toolchain comes from a pinned upstream image
                (`python`, `gcc`, `rust`, `golang`, `node`, `bash` — by digest) or a pinned
                package (`typescript@5.9.3`, `yamllint==1.35.1`); the build checks that the
                tool reports exactly the version the toolchain manifest lists, so the images
                and `Toolchains/manifest.json` cannot drift apart. `./install.sh --build` runs
                these on a connected quickstart host.
    bundle      the offline variant: a Debian base and the toolchain copied in from the bundle's
                `toolchains/<language>/<version>/` directory; nothing is downloaded (INV-8).

Every image: `git` for local commits, the `slas-check` wrapper the plan's checks run, an
unprivileged user (uid 10001), `/workspace` as the working directory, no credential helper,
no remote, no network tooling added (INV-14). `CMD ["sleep", "infinity"]` keeps the container
idle until the manager `exec`s work into it.

Standard library only: `install.sh --build` runs `python -m slas_sandbox_manager.images list`
and `… manifest --out …` on the bare host before anything is installed:

    list      name<TAB>tag<TAB>dockerfile per image (tag = <registry>/slas/sandbox-<lang>:<ver>)
    manifest  write the toolchain manifest the images satisfy
    render    write the Dockerfiles (a unit test keeps the committed files in step)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, TextIO

from slas_sandbox_manager.toolchains import (
    BY_ID,
    LANGUAGES,
    LanguageSpec,
    Manifest,
    default_manifest,
    image_for,
    sandbox_registry,
    version_key,
)

Source = Literal["connected", "bundle"]
Flavour = Literal["debian", "alpine"]

SANDBOX_UID: Final = 10001
CHECK_SOURCE: Final = "images/sandbox-common/slas-check.sh"
CHECK_TARGET: Final = "/usr/local/bin/slas-check"

#: docker.io/library/debian:bookworm-20250908-slim, the offline-bundle base (same as the
#: screen worker).
DEBIAN_BASE: Final = (
    "docker.io/library/debian@sha256:"
    "df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3"
)

#: Companion tools installed beside a toolchain, at the versions the manifest records.
COMPANIONS: Final[dict[str, str]] = {
    "node": "22.22.2",
    "ruff": "0.16.7",
    "mypy": "2.3.1",
    "pytest": "9.1.1",
    "jsonschema": "4.23.0",
}

_HEAD: Final = (
    "HEAD registry-1.docker.io/v2/library/{name}/manifests/{tag} (Docker-Content-Digest, "
    "the multi-arch index) on 2026-09-17"
)


@dataclass(frozen=True)
class Base:
    """An upstream image pinned by digest; `tag` is what the digest was resolved from."""

    tag: str
    digest: str
    flavour: Flavour = "debian"

    @property
    def name(self) -> str:
        return self.tag.split(":", 1)[0]

    @property
    def reference(self) -> str:
        return f"docker.io/library/{self.name}@{self.digest}"

    @property
    def how(self) -> str:
        return _HEAD.format(name=self.name, tag=self.tag.split(":", 1)[1])


PYTHON_3_12: Final = Base(
    "python:3.12.6-slim-bookworm",
    "sha256:ad48727987b259854d52241fac3bc633574364867b8e20aec305e6e7f4028b26",
)
PYTHON_3_11: Final = Base(
    "python:3.11.10-slim-bookworm",
    "sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e",
)
GCC: Final = Base(
    "gcc:13.2.0", "sha256:15c73bc59ae88b3fd563ef2ec4a8743a8848a9f74362b6d116c4543c4844b6e0"
)
RUST: Final = Base(
    "rust:1.80.1-slim-bookworm",
    "sha256:907ff4b3ee7df57149ffee04f606e0a08b9b2ed3507f00a19cf3c9c0f74b7681",
)
BASH: Final = Base(
    "bash:5.2.21",
    "sha256:a422913be6a4b0ea5403d2af72eb73779b6d9ed84d0dcf85d6b4309e891a379e",
    flavour="alpine",
)
GOLANG: Final = Base(
    "golang:1.23.1-bookworm",
    "sha256:dba79eb312528369dea87532a65dbe9d4efb26439a0feacc9e7ac9b0f1c7f607",
)
NODE: Final = Base(
    f"node:{COMPANIONS['node']}-bookworm-slim",
    "sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e",
)

BASES: Final[tuple[Base, ...]] = (PYTHON_3_12, PYTHON_3_11, GCC, RUST, BASH, GOLANG, NODE)


@dataclass(frozen=True)
class Recipe:
    """How one language image is built on the connected path."""

    language: str
    version: str
    base: Base
    #: Distribution packages: apt on Debian bases, apk on Alpine. `git` is always among them.
    packages: tuple[str, ...]
    #: Pinned installs after the packages (pip, npm, rustup); each is one RUN line.
    install: tuple[str, ...] = ()
    #: Prints the tool's version; the build fails unless `version` appears in it.
    verify: str = ""
    env: dict[str, str] = field(default_factory=dict)

    @property
    def spec(self) -> LanguageSpec:
        return BY_ID[self.language]

    @property
    def newest(self) -> bool:
        return version_key(self.version) == max(
            version_key(recipe.version) for recipe in RECIPES if recipe.language == self.language
        )

    @property
    def name(self) -> str:
        if self.newest:
            return f"sandbox-{self.language}"
        return f"sandbox-{self.language}-{self.version}"

    @property
    def dockerfile(self) -> str:
        """Path from the repository root; the newest version keeps the plain name."""
        suffix = "" if self.newest else f".{self.version}"
        return f"images/sandbox-{self.language}/Dockerfile{suffix}"

    def tag(self, registry: str | None = None) -> str:
        return image_for(self.language, self.version, registry=registry)


def _python(base: Base, version: str) -> Recipe:
    pins = " ".join(f"{tool}=={COMPANIONS[tool]}" for tool in ("ruff", "mypy", "pytest"))
    return Recipe(
        "python",
        version,
        base,
        ("git", "make"),
        install=(f"pip install --no-cache-dir {pins}",),
        verify="python3 --version",
        env={"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1"},
    )


RECIPES: Final[tuple[Recipe, ...]] = (
    _python(PYTHON_3_11, "3.11.10"),
    _python(PYTHON_3_12, "3.12.6"),
    Recipe("c", "13.2.0", GCC, ("git", "make"), verify="gcc --version"),
    Recipe(
        "cpp",
        "13.2.0",
        GCC,
        ("git", "make", "cmake"),
        verify="g++ --version",
    ),
    Recipe(
        "rust",
        "1.80.1",
        RUST,
        ("git", "make", "pkg-config"),
        # The official image installs the minimal profile; the plan's lint step needs clippy.
        install=("rustup component add clippy rustfmt",),
        verify="rustc --version",
        env={"CARGO_HOME": "/scratch/.cargo", "CARGO_NET_OFFLINE": "true"},
    ),
    Recipe(
        "shell",
        "5.2.21",
        BASH,
        # coreutils: GNU `sleep infinity` (busybox's does not take the word).
        ("git", "make", "coreutils", "shellcheck", "bats"),
        verify="bash --version",
    ),
    Recipe(
        "go",
        "1.23.1",
        GOLANG,
        ("git", "make"),
        verify="go version",
        env={"GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "-mod=mod"},
    ),
    Recipe(
        "typescript",
        "5.9.3",
        NODE,
        ("git", "make"),
        install=("npm install -g typescript@5.9.3 && npm cache clean --force",),
        verify="tsc --version",
        env={"npm_config_update_notifier": "false", "npm_config_fund": "false"},
    ),
    Recipe(
        "config",
        "1.35.1",
        PYTHON_3_12,
        ("git", "make"),
        install=(
            f"pip install --no-cache-dir yamllint==1.35.1 jsonschema=={COMPANIONS['jsonschema']}",
        ),
        verify="yamllint --version",
        env={"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1"},
    ),
)


def recipes_for(language: str) -> list[Recipe]:
    return [recipe for recipe in RECIPES if recipe.language == language]


def images_manifest() -> Manifest:
    """The toolchain manifest the connected images satisfy (equal to the bundled default)."""
    toolchains: dict[str, list[str]] = {}
    for spec in LANGUAGES:
        versions = sorted((recipe.version for recipe in recipes_for(spec.id)), key=version_key)
        if versions:
            toolchains[spec.id] = versions
    return Manifest(toolchains=toolchains, companions=dict(COMPANIONS), source="<sandbox images>")


# --- rendering -------------------------------------------------------------------------------


def _packages_step(recipe: Recipe) -> str:
    packages = " ".join(recipe.packages)
    if recipe.base.flavour == "alpine":
        return (
            "# TODO(SLAS-IMAGES): pin apk package versions from a snapshot mirror in the bundle\n"
            "# build (INV-8); on the connected path this reaches the Alpine mirror (ADR-0014).\n"
            f"RUN apk add --no-cache {packages}\n"
        )
    return (
        "# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle\n"
        "# build (INV-8); on the connected path this reaches the Debian mirror (ADR-0014).\n"
        "RUN apt-get update \\\n"
        " && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        f"{packages} \\\n"
        " && rm -rf /var/lib/apt/lists/*\n"
    )


def _user_step(recipe: Recipe) -> str:
    if recipe.base.flavour == "alpine":
        add_user = f"adduser -D -u {SANDBOX_UID} -s /sbin/nologin -h /scratch workspace"
    else:
        add_user = f"useradd --create-home --uid {SANDBOX_UID} --shell /usr/sbin/nologin workspace"
    return (
        f"COPY {CHECK_SOURCE} {CHECK_TARGET}\n"
        f"RUN chmod 0755 {CHECK_TARGET} \\\n"
        f" && {add_user} \\\n"
        f" && mkdir -p /workspace /scratch /etc/slas && chown {SANDBOX_UID}:{SANDBOX_UID} "
        "/workspace /scratch\n"
    )


def _env_line(pairs: dict[str, str]) -> str:
    return "ENV " + " ".join(f"{key}={value}" for key, value in pairs.items()) + "\n"


_TAIL: Final = f"""
# No credential helper, no remote, no hooks: the sandbox commits locally and nothing else
# (INV-14). The user's identity arrives read-only at /etc/slas/gitconfig at run time.
ENV GIT_CONFIG_GLOBAL=/etc/slas/gitconfig GIT_TERMINAL_PROMPT=0 HOME=/scratch

USER {SANDBOX_UID}:{SANDBOX_UID}
WORKDIR /workspace
ENTRYPOINT []
CMD ["sleep", "infinity"]
"""


def render_connected_dockerfile(recipe: Recipe, *, registry: str | None = None) -> str:
    spec = recipe.spec
    install = "".join(f"RUN {line}\n" for line in recipe.install)
    env = _env_line(
        {"SLAS_LANGUAGE": spec.id, "SLAS_TOOLCHAIN_VERSION": recipe.version, **recipe.env}
    )
    verify = (
        "# The image provides exactly the version the toolchain manifest lists; the build fails\n"
        "# otherwise, so the images and Toolchains/manifest.json cannot drift apart.\n"
        f'RUN {recipe.verify} 2>&1 | grep -F "{recipe.version}"\n'
    )
    return (
        f"# Sandbox image for {spec.label} ({spec.tool} {recipe.version}) — CLAUDE.md §4.1 "
        "Zone A.\n"
        "# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step. Build\n"
        f"# from the repository root:  docker build -f {recipe.dockerfile} -t "
        f"{recipe.tag(registry)} .\n"
        "# Connected build (ADR-0014): the toolchain comes from the pinned upstream image and the\n"
        "# pinned installs below; the offline bundle variant is rendered with --source bundle.\n"
        f"# Base pinned by digest (INV-8): {recipe.base.tag}\n"
        f"FROM {recipe.base.reference}\n\n"
        + _packages_step(recipe)
        + "\n"
        + install
        + env
        + verify
        + "\n"
        + _user_step(recipe)
        + _TAIL
    )


_BUNDLE_PACKAGES: Final[dict[str, str]] = {
    "python": "git make",
    "c": "git make",
    "cpp": "git make cmake",
    "rust": "git make pkg-config",
    "shell": "git make",
    "go": "git make",
    "typescript": "git make",
    "config": "git make python3",
}


def render_bundle_dockerfile(spec: LanguageSpec, version: str) -> str:
    """The offline variant: the bundle's `toolchains/<language>/<version>/` copied in."""
    packages = _BUNDLE_PACKAGES[spec.id]
    return f"""# Sandbox image for {spec.label} ({spec.tool} {version}) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images (--source bundle). Build from the repository root
# with the bundle's toolchains/ directory beside it; nothing is downloaded (INV-8).
# Base pinned by digest (INV-8): debian:bookworm-20250908-slim
FROM {DEBIAN_BASE}

# The bundle build installs from a vendored apt cache at pinned versions.
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {packages} \\
 && rm -rf /var/lib/apt/lists/*

# The bundled toolchain: toolchains/{spec.id}/{version}/ from the offline bundle.
COPY toolchains/{spec.id}/{version}/ /opt/toolchain/
ENV PATH="/opt/toolchain/bin:${{PATH}}" SLAS_LANGUAGE={spec.id} SLAS_TOOLCHAIN_VERSION={version}

COPY {CHECK_SOURCE} {CHECK_TARGET}
RUN chmod 0755 {CHECK_TARGET} \\
 && useradd --create-home --uid {SANDBOX_UID} --shell /usr/sbin/nologin workspace \\
 && mkdir -p /workspace /scratch /etc/slas && chown {SANDBOX_UID}:{SANDBOX_UID} /workspace /scratch
{_TAIL}"""


def render_dockerfile(
    spec: LanguageSpec, version: str, *, source: Source = "connected", registry: str | None = None
) -> str:
    if source == "bundle":
        return render_bundle_dockerfile(spec, version)
    for recipe in recipes_for(spec.id):
        if recipe.version == version:
            return render_connected_dockerfile(recipe, registry=registry)
    known = ", ".join(recipe.version for recipe in recipes_for(spec.id)) or "none"
    raise ValueError(
        f"no connected recipe builds {spec.label} {version}; the images provide {known}. "
        "Add a Recipe with a pinned base or build the bundle variant."
    )


def image_files(
    manifest: Manifest | None = None, *, source: Source = "connected", registry: str | None = None
) -> dict[str, str]:
    """Path from the repository root → Dockerfile content, one per image."""
    if source == "bundle":
        manifest = manifest or default_manifest()
        return {
            f"images/sandbox-{BY_ID[language].id}/Dockerfile": render_bundle_dockerfile(
                BY_ID[language], versions[-1]
            )
            for language, versions in manifest.toolchains.items()
        }
    return {
        recipe.dockerfile: render_connected_dockerfile(recipe, registry=registry)
        for recipe in RECIPES
    }


# --- CLI -------------------------------------------------------------------------------------


def list_lines(registry: str | None = None) -> list[str]:
    """`name<TAB>tag<TAB>dockerfile` per image, in the language order of CLAUDE.md §9."""
    ordered = sorted(
        RECIPES,
        key=lambda r: ([s.id for s in LANGUAGES].index(r.language), version_key(r.version)),
    )
    return [f"{recipe.name}\t{recipe.tag(registry)}\t{recipe.dockerfile}" for recipe in ordered]


def write_manifest(out: Path) -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = images_manifest()
    out.write_text(json.dumps(manifest.to_mapping(), indent=2) + "\n", encoding="utf-8")
    languages = ", ".join(
        f"{BY_ID[language].label} {'/'.join(versions)}"
        for language, versions in manifest.toolchains.items()
    )
    return f"Wrote the toolchain manifest to {out}: {languages}."


def write_dockerfiles(root: Path, *, source: Source = "connected") -> list[Path]:
    written: list[Path] = []
    for rel, content in image_files(source=source).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m slas_sandbox_manager.images", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="name<TAB>tag<TAB>dockerfile per sandbox image")
    listing.add_argument(
        "--registry",
        default=None,
        help="registry label for the tags (default: $SLAS_SANDBOX_REGISTRY or "
        f"{sandbox_registry()})",
    )
    manifest = commands.add_parser(
        "manifest", help="write the toolchain manifest the images satisfy"
    )
    manifest.add_argument("--out", required=True, type=Path)
    render = commands.add_parser("render", help="write the Dockerfiles under <root>/images/")
    render.add_argument("--root", default=Path.cwd(), type=Path)
    render.add_argument("--source", choices=("connected", "bundle"), default="connected")
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "list":
        for line in list_lines(args.registry):
            out.write(line + "\n")
        return 0
    if args.command == "manifest":
        out.write(write_manifest(args.out) + "\n")
        return 0
    for path in write_dockerfiles(args.root, source=args.source):
        out.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — `python -m slas_sandbox_manager.images`
    sys.exit(main())
