"""The compose files (CLAUDE.md §12, ADR-0003, ADR-0012), as Python so tests can check the
zone model on the data and the files on disk stay in step.

    compose/docker-compose.yml           the quickstart stack: every zone, every network
    compose/prod.override.yml            + vault, keycloak, loki, tempo, backup-runner,
                                         minio-init; Vault at dispatch; OIDC beside built-in
                                         auth; the Kata/Firecracker sandbox tier; pgBackRest
                                         archiving; object lock on backups and artifacts
    compose/macvlan.override.yml         validation-executor's own address on the lab VLAN
    compose/macvlan-factory.override.yml factory-executor's own address on the factory LAN

ADR-0003 rules applied everywhere: no `env_file`; secrets are files under
`${SLAS_DATA_ROOT}/secrets/` mounted as Docker secrets; third-party images are pinned by an
immutable tag whose digest and image ID live in `compose/images.lock.*`; every service is
`no-new-privileges`, `cap_drop: [ALL]` (plus what it must add back), has a healthcheck, and
only `edge` publishes a port. INV-4: no X11 socket and no `/dev/input` anywhere; the
container-runtime socket reaches only model-manager and sandbox-manager, never an agent.
"""

from __future__ import annotations

# ruff: noqa: E501 — embedded shell scripts and compose headers read better on one line
from collections.abc import Iterable, Mapping
from typing import Any, Final

from slas_deploy.images import first_party, third_party

AIRGAP_ENV: Final[dict[str, str]] = {
    "DO_NOT_TRACK": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}

DATA: Final = "${SLAS_DATA_ROOT}"
SECRETS: Final = f"{DATA}/secrets"

#: Network name → whether it is internal (no route to the host's default network).
NETWORKS: Final[dict[str, bool]] = {
    "slas-edge": False,
    "slas-frontend": True,
    "slas-backend": True,
    "slas-inference": True,
    "slas-knowledge": True,
    "slas-observability": True,
    "slas-screen": True,
    "slas-lab": False,
    "slas-factory": False,
    "slas-git": False,
}

#: Networks with exactly one member (CLAUDE.md §4.1 zones B, B', G).
SOLE_MEMBERS: Final[dict[str, str]] = {
    "slas-lab": "validation-executor",
    "slas-factory": "factory-executor",
    "slas-git": "git-broker",
}

#: Services allowed to see the container runtime socket (§12: they start containers).
RUNTIME_SOCKET_HOLDERS: Final[frozenset[str]] = frozenset({"model-manager", "sandbox-manager"})
#: The host side of that mount: rootless Podman by default; SLAS_RUNTIME_SOCKET in .env points
#: at another socket (an empty value keeps the default). Inside the container the path is fixed.
RUNTIME_SOCKET: Final = "${SLAS_RUNTIME_SOCKET:-/run/podman/podman.sock}"
RUNTIME_SOCKET_IN_CONTAINER: Final = "/run/podman/podman.sock"

#: Every file secret the installer generates (ADR-0003) plus the prod additions.
QUICKSTART_SECRETS: Final[tuple[str, ...]] = (
    "postgres_password",
    "redis_password",
    "redis.conf",
    "minio_root_password",
    "secret_key",
    "admin-initial-password",
)
PROD_SECRETS: Final[tuple[str, ...]] = (
    "vault_approle_role_id",
    "vault_approle_secret_id",
    "keycloak_admin_password",
    "keycloak_db_password",
    "oidc_client_secret",
    "pgbackrest_s3_key",
    "pgbackrest_s3_secret",
)

HEALTH_INTERVAL: Final = {"interval": "15s", "timeout": "5s", "retries": 8, "start_period": "30s"}


def _health(*argv: str) -> dict[str, Any]:
    return {"test": ["CMD", *argv], **HEALTH_INTERVAL}


def _service(
    name: str,
    image: str,
    *,
    networks: Iterable[str],
    environment: Mapping[str, str] | None = None,
    volumes: Iterable[str] = (),
    secrets: Iterable[str] = (),
    healthcheck: dict[str, Any] | None = None,
    depends_on: Iterable[str] = (),
    command: list[str] | None = None,
    user: str = "${SLAS_UID}:${SLAS_GID}",
    cap_add: Iterable[str] = (),
    ports: Iterable[str] = (),
    tmpfs: Iterable[str] = (),
    read_only: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    service: dict[str, Any] = {
        "image": image,
        "restart": "unless-stopped",
        "user": user,
        "networks": list(networks),
        "environment": {**AIRGAP_ENV, **(environment or {})},
        "logging": {"driver": "json-file", "options": {"max-size": "50m", "max-file": "5"}},
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "healthcheck": healthcheck or _health("true"),
    }
    if list(cap_add):
        service["cap_add"] = list(cap_add)
    if read_only:
        service["read_only"] = True
    if list(tmpfs):
        service["tmpfs"] = list(tmpfs)
    if list(volumes):
        service["volumes"] = list(volumes)
    if list(secrets):
        service["secrets"] = list(secrets)
    if list(depends_on):
        service["depends_on"] = {dep: {"condition": "service_healthy"} for dep in depends_on}
    if command is not None:
        service["command"] = command
    if list(ports):
        service["ports"] = list(ports)
    if extra:
        service.update(extra)
    return service


def _slas(name: str, **kwargs: Any) -> dict[str, Any]:
    """A first-party service: our image, /metrics on 8000 for Prometheus (P11)."""
    kwargs.setdefault("healthcheck", _health("slas-health", "http://127.0.0.1:8000/health"))
    return _service(name, first_party(name), **kwargs)


BACKEND_CORE: Final = ("postgres", "redis", "minio")


def base_compose() -> dict[str, Any]:
    services: dict[str, Any] = {}

    services["edge"] = _service(
        "edge",
        first_party("edge"),
        networks=["slas-edge", "slas-frontend"],
        ports=["${SLAS_HTTPS_PORT}:443"],
        environment={"SLAS_TLS_MODE": "${SLAS_TLS_MODE}", "SLAS_TLS_NAMES": "${SLAS_TLS_NAMES}"},
        volumes=[f"{DATA}/tls:/data/tls"],
        cap_add=["NET_BIND_SERVICE"],
        healthcheck=_health("slas-health", "https://127.0.0.1/healthz", "--insecure-local"),
        depends_on=["webui", "api"],
        # Port 443 as ${SLAS_UID}:${SLAS_GID}: a non-root uid does not keep NET_BIND_SERVICE
        # across Caddy's execve (no ambient capabilities), so the container's own port
        # threshold is lowered instead. It affects this network namespace only.
        extra={"sysctls": {"net.ipv4.ip_unprivileged_port_start": "0"}},
    )
    services["webui"] = _slas(
        "webui",
        networks=["slas-frontend"],
        read_only=True,
        tmpfs=["/tmp"],  # noqa: S108 — the container's own tmpfs
    )
    services["api"] = _slas(
        "api",
        networks=["slas-frontend", "slas-backend", "slas-observability"],
        environment={
            "SLAS_PROFILE": "${SLAS_PROFILE}",
            "SLAS_DATA_ROOT": "/data",
            "SLAS_SOP_CHINESE": "${SLAS_SOP_CHINESE}",
            "SLAS_AUTH_MODES": "builtin",
            "POSTGRES_DB": "${POSTGRES_DB}",
            "POSTGRES_USER": "${POSTGRES_USER}",
            "SLAS_ROLES_FILE": "/etc/slas/rbac-roles.yaml",
        },
        volumes=[f"{DATA}:/data", "../config/rbac-roles.yaml:/etc/slas/rbac-roles.yaml:ro"],
        secrets=["postgres_password", "redis_password", "secret_key", "admin-initial-password"],
        depends_on=[*BACKEND_CORE],
    )
    services["agent-core-orchestrator"] = _slas(
        "agent-core-orchestrator",
        networks=[
            "slas-backend",
            "slas-inference",
            "slas-knowledge",
            "slas-screen",
            "slas-observability",
        ],
        environment={
            "SLAS_DATA_ROOT": "/data",
            "SLAS_GLOSSARY": "/etc/slas/glossary.yaml",
            "SLAS_OWNER_ROUTING": "/etc/slas/owner-routing.yaml",
        },
        volumes=[
            f"{DATA}:/data",
            "../docs/glossary.yaml:/etc/slas/glossary.yaml:ro",
            "../config/owner-routing.yaml:/etc/slas/owner-routing.yaml:ro",
        ],
        depends_on=["api", "llm-gateway"],
    )
    services["screen-worker"] = _slas(
        "screen-worker",
        networks=["slas-screen", "slas-frontend"],
        environment={"DISPLAYS_PER_WORKER": "8", "ACTION_RATE_LIMIT": "10"},
        extra={"shm_size": "1g"},
    )
    services["llm-gateway"] = _slas(
        "llm-gateway",
        networks=["slas-backend", "slas-inference", "slas-observability"],
        environment={
            "SCHEMA_ENFORCE": "strict",
            "CONSENSUS_DEFAULT_VOTERS": "3",
            "CONSENSUS_TOKEN_BUDGET_PCT": "5",
            "SLAS_CONSENSUS_FILE": "/etc/slas/consensus.yaml",
            "SLAS_REDACTION_FILE": "/etc/slas/redaction.yaml",
        },
        volumes=[
            "../config/consensus.yaml:/etc/slas/consensus.yaml:ro",
            "../config/redaction.yaml:/etc/slas/redaction.yaml:ro",
        ],
    )
    services["model-manager"] = _slas(
        "model-manager",
        networks=["slas-backend", "slas-inference"],
        environment={"SLAS_GPU_IDS": "${SLAS_GPU_IDS}", "VLLM_NO_USAGE_STATS": "1"},
        volumes=[
            f"{RUNTIME_SOCKET}:{RUNTIME_SOCKET_IN_CONTAINER}",
            f"{DATA}/Models:/data/Models",
        ],
    )
    services["vector-db"] = _service(
        "vector-db",
        third_party("qdrant"),
        networks=["slas-knowledge", "slas-observability"],
        environment={"QDRANT__TELEMETRY_DISABLED": "true"},
        volumes=[f"{DATA}/qdrant:/qdrant/storage"],
        healthcheck=_health("slas-health", "http://127.0.0.1:6333/readyz"),
    )
    services["local-search-api"] = _slas(
        "local-search-api",
        networks=["slas-knowledge", "slas-inference"],
        environment={"SEARCH_MODE": "internal_corpus"},
        volumes=[f"{DATA}/Knowledge:/data/Knowledge:ro"],
        depends_on=["vector-db"],
    )
    services["sandbox-manager"] = _slas(
        "sandbox-manager",
        networks=["slas-backend"],
        environment={
            "DEFAULT_RUNTIME": "runsc",
            "SANDBOX_TIER": "gvisor",
            "DEFAULT_NETWORK": "none",
            "PIDS_LIMIT": "512",
        },
        volumes=[
            f"{RUNTIME_SOCKET}:{RUNTIME_SOCKET_IN_CONTAINER}",
            f"{DATA}/Coding:/data/Coding",
        ],
    )
    services["git-broker"] = _slas(
        "git-broker",
        networks=["slas-backend", "slas-git"],
        environment={
            "GIT_HOSTS_ALLOWLIST": "/etc/slas/git-hosts.yaml",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "CRED_STORE": "postgres+aesgcm",
        },
        volumes=[
            f"{DATA}/Coding:/data/Coding",
            "../config/git-hosts.yaml:/etc/slas/git-hosts.yaml:ro",
        ],
        secrets=["secret_key", "postgres_password"],
        tmpfs=["/run/slas-keys:mode=700,size=16m"],
        depends_on=["postgres"],
    )
    services["validation-executor"] = _slas(
        "validation-executor",
        networks=["slas-backend", "slas-observability", "slas-lab"],
        environment={
            "GUARDRAIL_POLICY": "/etc/slas/guardrails.yaml",
            "BMC_QUIRKS": "/etc/slas/bmc-quirks.yaml",
            "SYSLOG_LISTEN": "0.0.0.0:5514",
            "LLM_IN_CONTROL_LOOP": "false",
            "CREDENTIAL_SOURCE": "env",
        },
        volumes=[
            f"{DATA}/Validation:/data/Validation",
            f"{DATA}/Tickets:/data/Tickets",
            "../config/guardrails.yaml:/etc/slas/guardrails.yaml:ro",
            "../config/bmc-quirks.yaml:/etc/slas/bmc-quirks.yaml:ro",
        ],
        tmpfs=["/run/slas-keys:mode=700,size=16m"],
    )
    services["factory-executor"] = _slas(
        "factory-executor",
        networks=["slas-backend", "slas-observability", "slas-factory"],
        environment={
            "MES_ADAPTER": "file_drop",
            "RUNNER_MTLS_CA": "/data/Factory/ca/ca.pem",
            "LLM_IN_CONTROL_LOOP": "false",
            "CREDENTIAL_SOURCE": "env",
            "SLAS_FACTORY_SETTINGS": "/etc/slas/factory.yaml",
        },
        volumes=[
            f"{DATA}/Factory:/data/Factory",
            f"{DATA}/Backups/stations:/data/Backups/stations",
            f"{DATA}/Tickets:/data/Tickets",
            "../config/factory.yaml:/etc/slas/factory.yaml:ro",
        ],
    )
    services["postgres"] = _service(
        "postgres",
        third_party("postgres"),
        networks=["slas-backend", "slas-knowledge"],
        user="999:999",
        environment={
            "POSTGRES_DB": "${POSTGRES_DB}",
            "POSTGRES_USER": "${POSTGRES_USER}",
            "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_password",
        },
        volumes=["postgres_data:/var/lib/postgresql/data"],
        secrets=["postgres_password"],
        cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
        healthcheck=_health("pg_isready", "-U", "${POSTGRES_USER}", "-d", "${POSTGRES_DB}"),
    )
    services["redis"] = _service(
        "redis",
        third_party("redis"),
        networks=["slas-backend"],
        user="999:999",
        command=["redis-server", "/run/secrets/redis.conf"],
        volumes=["redis_data:/data"],
        secrets=["redis.conf"],
        healthcheck=_health("redis-cli", "-a", "$(cat /run/secrets/redis_password)", "ping"),
    )
    services["redis"]["secrets"] = ["redis.conf", "redis_password"]
    services["minio"] = _service(
        "minio",
        third_party("minio"),
        networks=["slas-backend"],
        command=["server", "/data", "--console-address", ":9001"],
        environment={
            "MINIO_ROOT_USER": "${MINIO_ROOT_USER}",
            "MINIO_ROOT_PASSWORD_FILE": "/run/secrets/minio_root_password",
            "MINIO_UPDATE": "off",
            "MINIO_BROWSER": "off",
        },
        volumes=["minio_data:/data"],
        secrets=["minio_root_password"],
        healthcheck=_health("mc", "ready", "local"),
    )
    services["prometheus"] = _service(
        "prometheus",
        third_party("prometheus"),
        networks=["slas-observability", "slas-inference", "slas-backend", "slas-knowledge"],
        user="65534:65534",
        command=[
            "--config.file=/etc/prometheus/prometheus.yml",
            "--storage.tsdb.path=/prometheus",
            "--storage.tsdb.retention.time=30d",
            "--web.enable-lifecycle",
        ],
        volumes=[
            "../observability/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
            "../observability/prometheus/rules.yml:/etc/prometheus/rules.yml:ro",
            "prometheus_data:/prometheus",
        ],
        healthcheck=_health("slas-health", "http://127.0.0.1:9090/-/ready"),
    )
    services["alertmanager"] = _service(
        "alertmanager",
        third_party("alertmanager"),
        networks=["slas-observability", "slas-backend"],
        user="65534:65534",
        command=[
            "--config.file=/etc/alertmanager/alertmanager.yml",
            "--storage.path=/alertmanager",
        ],
        volumes=[
            "../observability/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro",
            "alertmanager_data:/alertmanager",
        ],
        healthcheck=_health("slas-health", "http://127.0.0.1:9093/-/ready"),
    )
    services["grafana"] = _service(
        "grafana",
        third_party("grafana"),
        networks=["slas-frontend", "slas-observability"],
        user="472:472",
        environment={
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
            "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
            "GF_INSTALL_PLUGINS": "",
            "GF_SERVER_ROOT_URL": "%(protocol)s://%(domain)s/grafana/",
            "GF_SERVER_SERVE_FROM_SUB_PATH": "true",
            "GF_AUTH_ANONYMOUS_ENABLED": "false",
            "GF_SECURITY_ADMIN_PASSWORD__FILE": "/run/secrets/grafana_admin_password",
        },
        volumes=[
            "../observability/grafana/provisioning:/etc/grafana/provisioning:ro",
            "../observability/grafana/dashboards:/etc/grafana/dashboards:ro",
            "grafana_data:/var/lib/grafana",
        ],
        secrets=["grafana_admin_password"],
        healthcheck=_health("slas-health", "http://127.0.0.1:3000/api/health"),
        depends_on=["prometheus"],
    )
    services["dcgm-exporter"] = _service(
        "dcgm-exporter",
        third_party("dcgm-exporter"),
        networks=["slas-observability"],
        user="0:0",
        cap_add=["SYS_ADMIN"],
        extra={
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [{"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]
                    }
                }
            }
        },
        healthcheck=_health("slas-health", "http://127.0.0.1:9400/metrics"),
    )
    services["node-exporter"] = _service(
        "node-exporter",
        third_party("node-exporter"),
        networks=["slas-observability"],
        user="65534:65534",
        command=[
            "--path.rootfs=/host",
            "--collector.disable-defaults",
            "--collector.cpu",
            "--collector.meminfo",
            "--collector.filesystem",
            "--collector.diskstats",
            "--collector.netdev",
            "--collector.loadavg",
        ],
        volumes=["/:/host:ro,rslave"],
        extra={"pid": "host"},
        healthcheck=_health("slas-health", "http://127.0.0.1:9100/metrics"),
    )
    services["postgres-exporter"] = _service(
        "postgres-exporter",
        third_party("postgres-exporter"),
        networks=["slas-observability", "slas-backend"],
        user="65534:65534",
        environment={
            "DATA_SOURCE_URI": "postgres:5432/${POSTGRES_DB}?sslmode=disable",
            "DATA_SOURCE_USER": "${POSTGRES_USER}",
            "DATA_SOURCE_PASS_FILE": "/run/secrets/postgres_password",
        },
        secrets=["postgres_password"],
        healthcheck=_health("slas-health", "http://127.0.0.1:9187/metrics"),
        depends_on=["postgres"],
    )

    secrets = {
        name: {"file": f"{SECRETS}/{name}"}
        for name in (*QUICKSTART_SECRETS, "grafana_admin_password")
    }
    return {
        "name": "slas",
        "networks": {
            name: ({"internal": True} if internal else {}) for name, internal in NETWORKS.items()
        },
        "volumes": {
            name: {}
            for name in (
                "postgres_data",
                "redis_data",
                "minio_data",
                "prometheus_data",
                "alertmanager_data",
                "grafana_data",
            )
        },
        "secrets": secrets,
        "services": services,
    }


# --- the prod profile ---------------------------------------------------------------------------

VAULT_ADDR: Final = "https://vault:8200"
KEYCLOAK_ISSUER: Final = "https://${SLAS_PUBLIC_HOST}/auth/realms/slas"


def prod_override() -> dict[str, Any]:
    """`docker compose -f docker-compose.yml -f prod.override.yml` (CLAUDE.md §3 prod column)."""
    services: dict[str, Any] = {}

    services["vault"] = _service(
        "vault",
        third_party("vault"),
        networks=["slas-backend"],
        user="100:1000",
        command=["server", "-config=/vault/config/vault.hcl"],
        environment={"VAULT_ADDR": VAULT_ADDR, "SKIP_SETCAP": "true"},
        volumes=[
            "../config/vault/vault.hcl:/vault/config/vault.hcl:ro",
            "../config/vault/policies:/vault/policies:ro",
            f"{DATA}/vault:/vault/file",
            f"{DATA}/tls/vault:/vault/tls:ro",
        ],
        cap_add=["IPC_LOCK"],
        healthcheck=_health("vault", "status", "-tls-skip-verify"),
    )
    services["keycloak"] = _service(
        "keycloak",
        third_party("keycloak"),
        networks=["slas-frontend", "slas-backend"],
        user="1000:1000",
        command=["start", "--optimized", "--import-realm"],
        environment={
            "KC_DB": "postgres",
            "KC_DB_URL": "jdbc:postgresql://postgres:5432/keycloak",
            "KC_DB_USERNAME": "keycloak",
            "KC_DB_PASSWORD_FILE": "/run/secrets/keycloak_db_password",
            "KC_HOSTNAME": "https://${SLAS_PUBLIC_HOST}/auth",
            "KC_HTTP_RELATIVE_PATH": "/auth",
            "KC_HTTP_ENABLED": "true",
            "KC_PROXY_HEADERS": "xforwarded",
            "KC_HEALTH_ENABLED": "true",
            "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
            "KC_BOOTSTRAP_ADMIN_PASSWORD_FILE": "/run/secrets/keycloak_admin_password",
        },
        volumes=["../config/keycloak/slas-realm.json:/opt/keycloak/data/import/slas-realm.json:ro"],
        secrets=["keycloak_admin_password", "keycloak_db_password"],
        healthcheck=_health("slas-health", "http://127.0.0.1:9000/auth/health/ready"),
        depends_on=["postgres"],
    )
    services["loki"] = _service(
        "loki",
        third_party("loki"),
        networks=["slas-observability"],
        user="10001:10001",
        command=["-config.file=/etc/loki/loki.yml"],
        volumes=["../config/loki/loki.yml:/etc/loki/loki.yml:ro", "loki_data:/loki"],
        healthcheck=_health("slas-health", "http://127.0.0.1:3100/ready"),
    )
    services["tempo"] = _service(
        "tempo",
        third_party("tempo"),
        networks=["slas-observability", "slas-backend"],
        user="10001:10001",
        command=["-config.file=/etc/tempo/tempo.yml"],
        volumes=["../config/tempo/tempo.yml:/etc/tempo/tempo.yml:ro", "tempo_data:/var/tempo"],
        healthcheck=_health("slas-health", "http://127.0.0.1:3200/ready"),
    )
    services["backup-runner"] = _service(
        "backup-runner",
        first_party("postgres-pgbackrest"),
        networks=["slas-backend"],
        user="999:999",
        command=["/usr/local/bin/backup-runner.sh"],
        environment={
            "PGBACKREST_STANZA": "slas",
            "BACKUP_FULL_CRON": "${SLAS_BACKUP_FULL_CRON}",
            "BACKUP_DIFF_CRON": "${SLAS_BACKUP_DIFF_CRON}",
            "SLAS_DATA_ROOT": "/data",
        },
        volumes=[
            "postgres_data:/var/lib/postgresql/data",
            "../config/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro",
            "../deploy/prod/backup-runner.sh:/usr/local/bin/backup-runner.sh:ro",
            f"{DATA}/Backups:/data/Backups",
            f"{DATA}/qdrant:/data/qdrant:ro",
        ],
        secrets=["pgbackrest_s3_key", "pgbackrest_s3_secret"],
        healthcheck=_health("pgbackrest", "--stanza=slas", "check"),
        depends_on=["postgres", "minio"],
    )
    services["minio-init"] = _service(
        "minio-init",
        third_party("mc"),
        networks=["slas-backend"],
        command=["/usr/local/bin/minio-init.sh"],
        environment={
            "MINIO_ROOT_USER": "${MINIO_ROOT_USER}",
            "BACKUP_RETENTION_DAYS": "${SLAS_BACKUP_RETENTION_DAYS}",
            "ARTIFACT_RETENTION_DAYS": "${SLAS_ARTIFACT_RETENTION_DAYS}",
        },
        volumes=["../deploy/prod/minio-init.sh:/usr/local/bin/minio-init.sh:ro"],
        secrets=["minio_root_password", "pgbackrest_s3_key", "pgbackrest_s3_secret"],
        depends_on=["minio"],
        extra={"restart": "on-failure"},
    )

    # Changes to quickstart services.
    services["postgres"] = {
        "image": first_party("postgres-pgbackrest"),
        "command": [
            "postgres",
            "-c",
            "config_file=/etc/postgresql/prod.conf",
        ],
        "volumes": [
            "postgres_data:/var/lib/postgresql/data",
            "../config/postgres/prod.conf:/etc/postgresql/prod.conf:ro",
            "../config/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro",
        ],
        "secrets": ["postgres_password", "pgbackrest_s3_key", "pgbackrest_s3_secret"],
    }
    services["api"] = {
        "environment": {
            "SLAS_AUTH_MODES": "builtin,oidc",
            "SLAS_OIDC_ISSUER": KEYCLOAK_ISSUER,
            "SLAS_OIDC_CLIENT_ID": "slas-webui",
            "SLAS_OIDC_CLIENT_SECRET_FILE": "/run/secrets/oidc_client_secret",
            "SLAS_OIDC_ROLE_CLAIM": "slas_roles",
            "SLAS_ARTIFACT_RETENTION_DAYS": "${SLAS_ARTIFACT_RETENTION_DAYS}",
            "SLAS_ARTIFACT_BUCKET": "slas-artifacts",
            "SLAS_TRACES_ENDPOINT": "http://tempo:4318/v1/traces",
        },
        "secrets": [
            "postgres_password",
            "redis_password",
            "secret_key",
            "admin-initial-password",
            "oidc_client_secret",
        ],
        "depends_on": {
            **{dep: {"condition": "service_healthy"} for dep in BACKEND_CORE},
            "keycloak": {"condition": "service_healthy"},
        },
    }
    services["edge"] = {"environment": {"SLAS_AUTH_UPSTREAM": "http://keycloak:8080"}}
    services["git-broker"] = {
        "environment": {
            "CRED_STORE": "vault",
            "VAULT_ADDR": VAULT_ADDR,
            "VAULT_CACERT": "/vault-ca/ca.crt",
            "VAULT_ROLE_ID_FILE": "/run/secrets/vault_approle_role_id",
            "VAULT_SECRET_ID_FILE": "/run/secrets/vault_approle_secret_id",
            "VAULT_KV_MOUNT": "slas",
        },
        "volumes": [
            f"{DATA}/Coding:/data/Coding",
            "../config/git-hosts.yaml:/etc/slas/git-hosts.yaml:ro",
            f"{DATA}/tls/vault/ca.crt:/vault-ca/ca.crt:ro",
        ],
        "secrets": ["vault_approle_role_id", "vault_approle_secret_id"],
        "depends_on": {"vault": {"condition": "service_healthy"}},
    }
    for executor in ("validation-executor", "factory-executor"):
        services[executor] = {
            "environment": {
                "CREDENTIAL_SOURCE": "vault",
                "VAULT_ADDR": VAULT_ADDR,
                "VAULT_CACERT": "/vault-ca/ca.crt",
                "VAULT_ROLE_ID_FILE": "/run/secrets/vault_approle_role_id",
                "VAULT_SECRET_ID_FILE": "/run/secrets/vault_approle_secret_id",
                "VAULT_KV_MOUNT": "slas",
            },
            "secrets": ["vault_approle_role_id", "vault_approle_secret_id"],
            "depends_on": {"vault": {"condition": "service_healthy"}},
        }
    services["sandbox-manager"] = {
        "environment": {
            "DEFAULT_RUNTIME": "kata-fc",
            "SANDBOX_TIER": "kata",
            "REQUIRE_GVISOR": "true",
        }
    }
    services["screen-worker"] = {"environment": {"DISPLAY_ISOLATION": "kata"}}
    for service in (
        "llm-gateway",
        "agent-core-orchestrator",
        "validation-executor",
        "factory-executor",
    ):
        services.setdefault(service, {}).setdefault("environment", {})["SLAS_TRACES_ENDPOINT"] = (
            "http://tempo:4318/v1/traces"
        )
    services["grafana"] = {
        "environment": {"GF_AUTH_GENERIC_OAUTH_ENABLED": "false"},
        "volumes": [
            "../observability/grafana/provisioning:/etc/grafana/provisioning:ro",
            "../observability/grafana/dashboards:/etc/grafana/dashboards:ro",
            "../config/loki/grafana-datasources.yml:/etc/grafana/provisioning/datasources/loki-tempo.yml:ro",
            "grafana_data:/var/lib/grafana",
        ],
    }

    return {
        "volumes": {"loki_data": {}, "tempo_data": {}},
        "secrets": {name: {"file": f"{SECRETS}/{name}"} for name in PROD_SECRETS},
        "services": services,
    }


# --- macvlan overlays ------------------------------------------------------------------------------


def _macvlan(network: str, prefix: str, service: str, note: str) -> dict[str, Any]:
    return {
        "networks": {
            network: {
                "driver": "macvlan",
                "driver_opts": {
                    "parent": f"${{{prefix}_IFACE:?set {prefix}_IFACE to the host NIC on the {note}}}"
                },
                "ipam": {
                    "config": [
                        {
                            "subnet": f"${{{prefix}_SUBNET:?set {prefix}_SUBNET, e.g. 10.20.30.0/24}}",
                            "gateway": f"${{{prefix}_GATEWAY:?set {prefix}_GATEWAY}}",
                        }
                    ]
                },
            }
        },
        "services": {
            service: {
                "networks": {
                    network: {
                        "ipv4_address": f"${{{prefix}_EXECUTOR_IP:?set {prefix}_EXECUTOR_IP, "
                        f"the executor's address on the {note}}}"
                    }
                },
                "ports": [],
            }
        },
    }


def macvlan_lab() -> dict[str, Any]:
    overlay = _macvlan("slas-lab", "SLAS_LAB", "validation-executor", "lab VLAN")
    overlay["services"]["validation-executor"]["environment"] = {"SYSLOG_LISTEN": "0.0.0.0:5514"}
    return overlay


def macvlan_factory() -> dict[str, Any]:
    overlay = _macvlan("slas-factory", "SLAS_FACTORY", "factory-executor", "factory LAN")
    overlay["services"]["factory-executor"]["environment"] = {
        "ENROLMENT_LISTEN": "0.0.0.0:8444",
        "STATION_RUNNER_PORT": "8443",
    }
    return overlay


COMPOSE_FILES: Final[dict[str, Any]] = {
    "docker-compose.yml": base_compose,
    "prod.override.yml": prod_override,
    "macvlan.override.yml": macvlan_lab,
    "macvlan-factory.override.yml": macvlan_factory,
}

HEADERS: Final[dict[str, str]] = {
    "docker-compose.yml": (
        "The quickstart stack (CLAUDE.md §12, ADR-0003). Rendered from slas_deploy.compose; a\n"
        "unit test keeps file and code in step. No env_file: each service names the install-time\n"
        "keys it reads from .env; secrets are files under ${SLAS_DATA_ROOT}/secrets. Third-party\n"
        "images are pinned by an immutable tag whose digest and image ID live in\n"
        "compose/images.lock.*; install.sh refuses to start an image the lock does not pin."
    ),
    "prod.override.yml": (
        "The prod profile (CLAUDE.md §3, ADR-0012): Vault (credentials resolved at dispatch),\n"
        "Keycloak OIDC beside the built-in accounts, Loki and Tempo, pgBackRest archiving to MinIO\n"
        "with object lock, the Kata/Firecracker sandbox tier. Apply with\n"
        "  docker compose -f compose/docker-compose.yml -f compose/prod.override.yml up -d\n"
        "Rendered from slas_deploy.compose; a unit test keeps file and code in step."
    ),
    "macvlan.override.yml": (
        "macvlan overlay for the lab VLAN (CLAUDE.md §4.1 Zone B, §12 slas-lab). Gives\n"
        "validation-executor its own address on the lab VLAN so BMCs, target OSes and their syslog\n"
        "reach it directly and nothing else on the platform host is on that VLAN. Values come from\n"
        ".env (SLAS_LAB_*); the parent interface is the host NIC physically on the lab VLAN.\n"
        "  docker compose -f compose/docker-compose.yml -f compose/macvlan.override.yml up -d\n"
        "Rendered from slas_deploy.compose; a unit test keeps file and code in step."
    ),
    "macvlan-factory.override.yml": (
        "macvlan overlay for the factory LAN (CLAUDE.md §4.1 Zone B', §12 slas-factory). Gives\n"
        "factory-executor its own address so station runners reach the enrolment endpoint and the\n"
        "executor reaches the runners; nothing else on the platform host is on that LAN. Values\n"
        "come from .env (SLAS_FACTORY_*).\n"
        "  docker compose -f compose/docker-compose.yml -f compose/macvlan-factory.override.yml up -d\n"
        "Rendered from slas_deploy.compose; a unit test keeps file and code in step."
    ),
}


def service_names(document: Mapping[str, Any]) -> list[str]:
    return sorted(document.get("services", {}))


def networks_of(document: Mapping[str, Any], service: str) -> list[str]:
    networks = document["services"][service].get("networks", [])
    return sorted(networks if isinstance(networks, list) else networks.keys())
