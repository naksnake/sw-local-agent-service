# Sandbox image for Go (go 1.23.1) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step. Build
# from the repository root:  docker build -f images/sandbox-go/Dockerfile -t local/slas/sandbox-go:1.23.1 .
# Connected build (ADR-0014): the toolchain comes from the pinned upstream image and the
# pinned installs below; the offline bundle variant is rendered with --source bundle.
# Base pinned by digest (INV-8): golang:1.23.1-bookworm
FROM docker.io/library/golang@sha256:dba79eb312528369dea87532a65dbe9d4efb26439a0feacc9e7ac9b0f1c7f607

# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle
# build (INV-8); on the connected path this reaches the Debian mirror (ADR-0014).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git make \
 && rm -rf /var/lib/apt/lists/*

ENV SLAS_LANGUAGE=go SLAS_TOOLCHAIN_VERSION=1.23.1 GOTOOLCHAIN=local GOPROXY=off GOFLAGS=-mod=mod
# The image provides exactly the version the toolchain manifest lists; the build fails
# otherwise, so the images and Toolchains/manifest.json cannot drift apart.
RUN go version 2>&1 | grep -F "1.23.1"

COPY images/sandbox-common/slas-check.sh /usr/local/bin/slas-check
RUN chmod 0755 /usr/local/bin/slas-check \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin workspace \
 && mkdir -p /workspace /scratch /etc/slas && chown 10001:10001 /workspace /scratch

# No credential helper, no remote, no hooks: the sandbox commits locally and nothing else
# (INV-14). The user's identity arrives read-only at /etc/slas/gitconfig at run time.
ENV GIT_CONFIG_GLOBAL=/etc/slas/gitconfig GIT_TERMINAL_PROMPT=0 HOME=/scratch

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT []
CMD ["sleep", "infinity"]
