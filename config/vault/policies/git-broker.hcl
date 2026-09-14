# Vault policy for git-broker (ADR-0012): the least it needs under the slas mount.
# Rendered from slas_deploy.vault; a unit test keeps file and code in step.
path "slas/data/git/*" {
  capabilities = ["create", "read", "update"]
}
path "slas/metadata/git/*" {
  capabilities = ["read", "delete", "list"]
}
