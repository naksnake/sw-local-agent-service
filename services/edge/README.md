# services/edge

Caddy with TLS in front of the WebUI, the API and Grafana; the only service that publishes a
port (CLAUDE.md §4.2, §12). There is no code here: the image is `images/edge/` (Dockerfile
and the `slas-edge` entrypoint that writes the Caddyfile from `SLAS_TLS_MODE` and
`SLAS_TLS_NAMES`), described in `images/README.md`.
