# Vault for SW Local Agent Service, prod profile (ADR-0012). Rendered from
# slas_deploy.vault; a unit test keeps file and code in step. File storage under the
# data root; one TLS listener on the backend network; no telemetry, no UI, no egress.
ui = false
disable_mlock = false
storage "file" {
  path = "/vault/file"
}
listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_cert_file = "/vault/tls/vault.crt"
  tls_key_file  = "/vault/tls/vault.key"
  tls_min_version = "tls12"
}
api_addr = "https://vault:8200"
telemetry {
  disable_hostname = true
  prometheus_retention_time = "0s"
}
log_level = "info"
log_format = "json"
