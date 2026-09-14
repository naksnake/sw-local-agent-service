# Vault policy for factory-executor (ADR-0012): the least it needs under the slas mount.
# Rendered from slas_deploy.vault; a unit test keeps file and code in step.
path "slas/data/factory/*" {
  capabilities = ["read"]
}
path "slas/metadata/factory/*" {
  capabilities = ["list"]
}
