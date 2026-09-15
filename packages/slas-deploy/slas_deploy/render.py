"""`python -m slas_deploy.render [repo root]` — write every deployment file from code.

compose/docker-compose.yml, prod.override.yml, macvlan.override.yml,
macvlan-factory.override.yml, images.lock.yaml, images.lock.json
config/vault/vault.hcl, config/vault/policies/<service>.hcl
config/keycloak/slas-realm.json
config/pgbackrest.conf, config/postgres/prod.conf
config/loki/loki.yml, config/loki/grafana-datasources.yml, config/tempo/tempo.yml
deploy/prod/minio-init.sh, deploy/prod/backup-runner.sh, deploy/prod/vault-bootstrap.sh
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from slas_deploy import compose, keycloak, observability_prod, pgbackrest, vault
from slas_deploy.images import default_lock, render_lock_json, render_lock_yaml
from slas_observability import yamlish


def _yaml(document: object, header: str) -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    return "\n".join(lines) + "\n" + yamlish.dump(document)


def rendered_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for name, builder in compose.COMPOSE_FILES.items():
        files[f"compose/{name}"] = _yaml(builder(), compose.HEADERS[name])
    lock = default_lock()
    files["compose/images.lock.yaml"] = render_lock_yaml(lock)
    files["compose/images.lock.json"] = render_lock_json(lock)
    files["config/vault/vault.hcl"] = vault.vault_hcl()
    for service in vault.POLICIES:
        files[f"config/vault/policies/{service}.hcl"] = vault.policy_hcl(service)
    files["config/keycloak/slas-realm.json"] = (
        json.dumps(keycloak.realm_export(), indent=2, ensure_ascii=False) + "\n"
    )
    files["config/pgbackrest.conf"] = pgbackrest.pgbackrest_conf()
    files["config/postgres/prod.conf"] = pgbackrest.postgres_prod_conf()
    files["config/loki/loki.yml"] = _yaml(
        observability_prod.loki_config(),
        "Loki for the prod profile (ADR-0012). Rendered from slas_deploy.observability_prod; a\n"
        "unit test keeps file and code in step. Filesystem storage, 30 days, no analytics.",
    )
    files["config/loki/grafana-datasources.yml"] = _yaml(
        observability_prod.grafana_datasources(),
        "Grafana datasources for Loki and Tempo (prod). Rendered from\n"
        "slas_deploy.observability_prod; a unit test keeps file and code in step.",
    )
    files["config/tempo/tempo.yml"] = _yaml(
        observability_prod.tempo_config(),
        "Tempo for the prod profile (ADR-0012): OTLP in, local blocks, 30 days, no usage report.\n"
        "Rendered from slas_deploy.observability_prod; a unit test keeps file and code in step.",
    )
    files["deploy/prod/minio-init.sh"] = pgbackrest.minio_init_script()
    files["deploy/prod/backup-runner.sh"] = pgbackrest.backup_runner_script()
    files["deploy/prod/vault-bootstrap.sh"] = vault_bootstrap_script()
    return files


def vault_bootstrap_script() -> str:
    lines = [
        "#!/bin/sh",
        "# One-time Vault bootstrap for the prod profile (ADR-0012), run by install.sh after",
        "# `vault operator init` inside the vault container. Rendered from slas_deploy.render;",
        "# a unit test keeps file and code in step. Idempotent: every step tolerates",
        "# 'already exists'.",
        "set -u",
        'export VAULT_ADDR="${VAULT_ADDR:-https://127.0.0.1:8200}"',
        'export VAULT_CACERT="${VAULT_CACERT:-/vault/tls/ca.crt}"',
    ]
    for argv in vault.bootstrap_commands():
        quoted = " ".join(_sh_quote(part) for part in argv)
        lines.append(f"{quoted} 2>&1 | grep -v 'already' || true")
    lines.append(
        'echo "Vault is bootstrapped: KV mount slas, AppRole per service, one policy each."'
    )
    return "\n".join(lines) + "\n"


def _sh_quote(text: str) -> str:
    if all(ch.isalnum() or ch in "-_./=:" for ch in text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def write_all(root: Path) -> list[Path]:
    written: list[Path] = []
    for relative, content in rendered_files().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if relative.endswith(".sh"):
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path(os.getcwd())
    written = write_all(root)
    for path in written:
        print(path)
    print(f"{len(written)} files written under {root}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
