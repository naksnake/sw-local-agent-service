"""One sandbox image per language, rendered from code into images/sandbox-<language>/.

Every image: the pinned Debian base, the bundled toolchain copied in at build time (no
network, INV-8), `git` for local commits, the `slas-check` wrapper the plan's checks run,
an unprivileged user and `/workspace` as the working directory. A unit test keeps the
rendered Dockerfiles in step with this module.
"""

from __future__ import annotations

from typing import Final

from slas_sandbox_manager.toolchains import BY_ID, LanguageSpec, Manifest, default_manifest

#: docker.io/library/debian:bookworm-20250908-slim, the same base as the screen worker.
DEBIAN_BASE: Final = (
    "docker.io/library/debian@sha256:"
    "df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3"
)
SANDBOX_UID: Final = 10001

_APT_PACKAGES: Final[dict[str, str]] = {
    "python": "git ca-certificates make",
    "c": "git ca-certificates make",
    "cpp": "git ca-certificates make",
    "rust": "git ca-certificates make pkg-config",
    "shell": "git ca-certificates make",
    "go": "git ca-certificates make",
    "typescript": "git ca-certificates make",
    "config": "git ca-certificates make python3",
}


def render_dockerfile(spec: LanguageSpec, version: str) -> str:
    packages = _APT_PACKAGES[spec.id]
    return f"""# Sandbox image for {spec.label} ({spec.tool} {version}) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step.
# The toolchain is copied from the offline bundle at build time; nothing is downloaded.
# Base pinned by digest (INV-8): debian:bookworm-20250908-slim
FROM {DEBIAN_BASE}

# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle build
# (INV-8); the platform builds images offline from a vendored apt cache.
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {packages} \\
 && rm -rf /var/lib/apt/lists/*

# The bundled toolchain: Toolchains/{spec.id}/{version}/ from the offline bundle.
COPY toolchains/{spec.id}/{version}/ /opt/toolchain/
ENV PATH="/opt/toolchain/bin:${{PATH}}" SLAS_LANGUAGE={spec.id} SLAS_TOOLCHAIN_VERSION={version}

COPY sandbox-common/slas-check.sh /usr/local/bin/slas-check
RUN chmod 0755 /usr/local/bin/slas-check \\
 && useradd --create-home --uid {SANDBOX_UID} --shell /usr/sbin/nologin workspace \\
 && mkdir -p /workspace /scratch /etc/slas && chown {SANDBOX_UID}:{SANDBOX_UID} /workspace /scratch

# No credential helper, no remote, no hooks: the sandbox commits locally and nothing else
# (INV-14). The user's identity arrives read-only at /etc/slas/gitconfig at run time.
ENV GIT_CONFIG_GLOBAL=/etc/slas/gitconfig GIT_TERMINAL_PROMPT=0 HOME=/scratch

USER {SANDBOX_UID}:{SANDBOX_UID}
WORKDIR /workspace
CMD ["sleep", "infinity"]
"""


def image_files(manifest: Manifest | None = None) -> dict[str, str]:
    """Relative path under images/ → content, for the newest bundled version of each language."""
    manifest = manifest or default_manifest()
    files: dict[str, str] = {}
    for language, versions in manifest.toolchains.items():
        spec = BY_ID[language]
        files[f"sandbox-{spec.id}/Dockerfile"] = render_dockerfile(spec, versions[-1])
    return files
