"""Vault's own configuration for the prod profile (ADR-0012): the server file and one
least-privilege policy per service. Rendered to `config/vault/`.

    vault.hcl                  file storage under the data root, one TLS listener on the backend
                               network, no telemetry, no UI
    policies/<service>.hcl     what each AppRole may read or write under the `slas` KV mount

The KV layout under mount `slas`:
    lab/<target>/bmc, lab/<target>/ssh      target credentials (validation-executor reads)
    factory/<station>/operator              station credentials (factory-executor reads)
    git/<ref>                               remote credentials by reference (git-broker rw)
    platform/<name>                         the api's own secrets (api reads)
"""

from __future__ import annotations

from typing import Final

KV_MOUNT: Final = "slas"

POLICIES: Final[dict[str, list[tuple[str, tuple[str, ...]]]]] = {
    "validation-executor": [
        (f"{KV_MOUNT}/data/lab/*", ("read",)),
        (f"{KV_MOUNT}/metadata/lab/*", ("list",)),
    ],
    "factory-executor": [
        (f"{KV_MOUNT}/data/factory/*", ("read",)),
        (f"{KV_MOUNT}/metadata/factory/*", ("list",)),
    ],
    "git-broker": [
        (f"{KV_MOUNT}/data/git/*", ("create", "read", "update")),
        (f"{KV_MOUNT}/metadata/git/*", ("read", "delete", "list")),
    ],
    "api": [
        (f"{KV_MOUNT}/data/platform/*", ("read",)),
    ],
}


def vault_hcl() -> str:
    return (
        "# Vault for SW Local Agent Service, prod profile (ADR-0012). Rendered from\n"
        "# slas_deploy.vault; a unit test keeps file and code in step. File storage under the\n"
        "# data root; one TLS listener on the backend network; no telemetry, no UI, no egress.\n"
        "ui = false\n"
        "disable_mlock = false\n"
        'storage "file" {\n'
        '  path = "/vault/file"\n'
        "}\n"
        'listener "tcp" {\n'
        '  address       = "0.0.0.0:8200"\n'
        '  tls_cert_file = "/vault/tls/vault.crt"\n'
        '  tls_key_file  = "/vault/tls/vault.key"\n'
        '  tls_min_version = "tls12"\n'
        "}\n"
        'api_addr = "https://vault:8200"\n'
        "telemetry {\n"
        "  disable_hostname = true\n"
        '  prometheus_retention_time = "0s"\n'
        "}\n"
        'log_level = "info"\n'
        'log_format = "json"\n'
    )


def policy_hcl(service: str) -> str:
    rules = POLICIES[service]
    lines = [
        f"# Vault policy for {service} (ADR-0012): the least it needs under the {KV_MOUNT} mount.",
        "# Rendered from slas_deploy.vault; a unit test keeps file and code in step.",
    ]
    for path, capabilities in rules:
        caps = ", ".join(f'"{c}"' for c in capabilities)
        lines += [f'path "{path}" {{', f"  capabilities = [{caps}]", "}"]
    return "\n".join(lines) + "\n"


def bootstrap_commands() -> list[list[str]]:
    """What `deploy/prod/vault-bootstrap.sh` runs once after `vault operator init`: enable KV
    v2 and AppRole, write the policies, create one role per service. argv only."""
    commands: list[list[str]] = [
        ["vault", "secrets", "enable", "-path", KV_MOUNT, "-version=2", "kv"],
        ["vault", "auth", "enable", "approle"],
    ]
    for service in POLICIES:
        commands.append(["vault", "policy", "write", service, f"/vault/policies/{service}.hcl"])
        commands.append(
            [
                "vault",
                "write",
                f"auth/approle/role/{service}",
                f"token_policies={service}",
                "token_ttl=1h",
                "token_max_ttl=8h",
                "secret_id_ttl=0",
                "secret_id_num_uses=0",
            ]
        )
    return commands
