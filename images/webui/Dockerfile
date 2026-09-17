# webui image: the built WebUI served by Caddy on :8000, plain HTTP behind the edge
# (CLAUDE.md §9, §12; ADR-0003, ADR-0009, ADR-0014). Build from the repository root:
#   docker build -f images/webui/Dockerfile -t <registry>/slas/webui:<version> .
# Bases pinned by digest (INV-8): node:22.22.2-bookworm-slim (build stage, the same image CI
# uses), caddy:2.11.4 (Alpine). JavaScript dependencies come from pnpm-lock.yaml (--frozen-lockfile).
FROM docker.io/library/node@sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e AS build
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/webui/ apps/webui/
RUN corepack enable \
 && pnpm install --frozen-lockfile \
 && pnpm --filter @slas/webui build

FROM docker.io/library/caddy@sha256:13ba145cba2f3e28fa801994876e4c086d1b95d5aa2a520a734765ffb6b12017
COPY --from=build /src/apps/webui/dist /srv
COPY images/webui/Caddyfile /etc/caddy/Caddyfile
COPY images/slas-health/slas-health /usr/local/bin/slas-health
RUN chmod 0755 /usr/local/bin/slas-health
# compose runs this container read-only as ${SLAS_UID}:${SLAS_GID} with a tmpfs at /tmp;
# Caddy's autosave and data directories point there so no uid needs a writable layer.
ENV XDG_CONFIG_HOME=/tmp XDG_DATA_HOME=/tmp HOME=/tmp
USER 10001:10001
EXPOSE 8000
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
