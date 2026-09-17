# Sandbox image for Shell (bash 5.2.21) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step. Build
# from the repository root:  docker build -f images/sandbox-shell/Dockerfile -t local/slas/sandbox-shell:5.2.21 .
# Connected build (ADR-0014): the toolchain comes from the pinned upstream image and the
# pinned installs below; the offline bundle variant is rendered with --source bundle.
# Base pinned by digest (INV-8): bash:5.2.21
FROM docker.io/library/bash@sha256:a422913be6a4b0ea5403d2af72eb73779b6d9ed84d0dcf85d6b4309e891a379e

# TODO(SLAS-IMAGES): pin apk package versions from a snapshot mirror in the bundle
# build (INV-8); on the connected path this reaches the Alpine mirror (ADR-0014).
RUN apk add --no-cache git make coreutils shellcheck bats

ENV SLAS_LANGUAGE=shell SLAS_TOOLCHAIN_VERSION=5.2.21
# The image provides exactly the version the toolchain manifest lists; the build fails
# otherwise, so the images and Toolchains/manifest.json cannot drift apart.
RUN bash --version 2>&1 | grep -F "5.2.21"

COPY images/sandbox-common/slas-check.sh /usr/local/bin/slas-check
RUN chmod 0755 /usr/local/bin/slas-check \
 && adduser -D -u 10001 -s /sbin/nologin -h /scratch workspace \
 && mkdir -p /workspace /scratch /etc/slas && chown 10001:10001 /workspace /scratch

# No credential helper, no remote, no hooks: the sandbox commits locally and nothing else
# (INV-14). The user's identity arrives read-only at /etc/slas/gitconfig at run time.
ENV GIT_CONFIG_GLOBAL=/etc/slas/gitconfig GIT_TERMINAL_PROMPT=0 HOME=/scratch

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT []
CMD ["sleep", "infinity"]
