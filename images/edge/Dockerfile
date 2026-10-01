# edge image: Caddy with TLS in front of the WebUI, the API and Grafana (CLAUDE.md §4.2, §12;
# ADR-0003, ADR-0014). The only service that publishes a port. Build from the repository root:
#   docker build -f images/edge/Dockerfile -t <registry>/slas/edge:<version> .
# Base pinned by digest (INV-8): caddy:2.11.4 (Alpine)
FROM docker.io/library/caddy@sha256:13ba145cba2f3e28fa801994876e4c086d1b95d5aa2a520a734765ffb6b12017

# curl: slas-health probes the edge's own self-signed certificate at https://127.0.0.1/healthz.
# TODO(SLAS-IMAGES): pin the apk package version from a snapshot mirror in the bundle build (INV-8).
RUN apk add --no-cache curl

COPY images/slas-health/slas-health /usr/local/bin/slas-health
COPY images/edge/slas-edge /usr/local/bin/slas-edge
RUN chmod 0755 /usr/local/bin/slas-health /usr/local/bin/slas-edge \
 && mkdir -p /data/tls && chmod 0777 /data/tls

# Everything Caddy writes (its local CA, issued certificates, autosave.json) lives under
# /data/tls, which compose bind-mounts from ${SLAS_DATA_ROOT}/tls so the CA survives a
# recreate. compose runs the container as ${SLAS_UID}:${SLAS_GID}; the entrypoint writes the
# Caddyfile to /tmp. Port 443 for any uid: compose sets net.ipv4.ip_unprivileged_port_start=0.
ENV XDG_DATA_HOME=/data/tls XDG_CONFIG_HOME=/data/tls HOME=/data/tls \
    SLAS_TLS_MODE=self-signed SLAS_TLS_NAMES=localhost
EXPOSE 443
ENTRYPOINT ["/usr/local/bin/slas-edge"]
