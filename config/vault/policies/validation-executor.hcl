# Vault policy for validation-executor (ADR-0012): the least it needs under the slas mount.
# Rendered from slas_deploy.vault; a unit test keeps file and code in step.
path "slas/data/lab/*" {
  capabilities = ["read"]
}
path "slas/metadata/lab/*" {
  capabilities = ["list"]
}
