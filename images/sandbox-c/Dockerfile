# Sandbox image for C (gcc 13.2.0) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step.
# The toolchain is copied from the offline bundle at build time; nothing is downloaded.
# Base pinned by digest (INV-8): debian:bookworm-20250908-slim
FROM docker.io/library/debian@sha256:df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3

# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle build
# (INV-8); the platform builds images offline from a vendored apt cache.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates make \
 && rm -rf /var/lib/apt/lists/*

# The bundled toolchain: Toolchains/c/13.2.0/ from the offline bundle.
COPY toolchains/c/13.2.0/ /opt/toolchain/
ENV PATH="/opt/toolchain/bin:${PATH}" SLAS_LANGUAGE=c SLAS_TOOLCHAIN_VERSION=13.2.0

COPY sandbox-common/slas-check.sh /usr/local/bin/slas-check
RUN chmod 0755 /usr/local/bin/slas-check \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin workspace \
 && mkdir -p /workspace /scratch /etc/slas && chown 10001:10001 /workspace /scratch

# No credential helper, no remote, no hooks: the sandbox commits locally and nothing else
# (INV-14). The user's identity arrives read-only at /etc/slas/gitconfig at run time.
ENV GIT_CONFIG_GLOBAL=/etc/slas/gitconfig GIT_TERMINAL_PROMPT=0 HOME=/scratch

USER 10001:10001
WORKDIR /workspace
CMD ["sleep", "infinity"]
