#!/bin/sh
# One-time Vault bootstrap for the prod profile (ADR-0012), run by install.sh after
# `vault operator init` inside the vault container. Rendered from slas_deploy.render;
# a unit test keeps file and code in step. Idempotent: every step tolerates
# 'already exists'.
set -u
export VAULT_ADDR="${VAULT_ADDR:-https://127.0.0.1:8200}"
export VAULT_CACERT="${VAULT_CACERT:-/vault/tls/ca.crt}"
vault secrets enable -path slas -version=2 kv 2>&1 | grep -v 'already' || true
vault auth enable approle 2>&1 | grep -v 'already' || true
vault policy write validation-executor /vault/policies/validation-executor.hcl 2>&1 | grep -v 'already' || true
vault write auth/approle/role/validation-executor token_policies=validation-executor token_ttl=1h token_max_ttl=8h secret_id_ttl=0 secret_id_num_uses=0 2>&1 | grep -v 'already' || true
vault policy write factory-executor /vault/policies/factory-executor.hcl 2>&1 | grep -v 'already' || true
vault write auth/approle/role/factory-executor token_policies=factory-executor token_ttl=1h token_max_ttl=8h secret_id_ttl=0 secret_id_num_uses=0 2>&1 | grep -v 'already' || true
vault policy write git-broker /vault/policies/git-broker.hcl 2>&1 | grep -v 'already' || true
vault write auth/approle/role/git-broker token_policies=git-broker token_ttl=1h token_max_ttl=8h secret_id_ttl=0 secret_id_num_uses=0 2>&1 | grep -v 'already' || true
vault policy write api /vault/policies/api.hcl 2>&1 | grep -v 'already' || true
vault write auth/approle/role/api token_policies=api token_ttl=1h token_max_ttl=8h secret_id_ttl=0 secret_id_num_uses=0 2>&1 | grep -v 'already' || true
echo "Vault is bootstrapped: KV mount slas, AppRole per service, one policy each."
