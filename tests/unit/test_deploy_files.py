"""The compose files, the image lock and every prod configuration file are rendered from code
and obey the zone model (CLAUDE.md §4.1, §12; ADR-0003; ADR-0012)."""

from __future__ import annotations

# ruff: noqa: E501 — long literal sentences and argv lists read better unwrapped
import io
import json
import subprocess
from pathlib import Path

import pytest

from slas_deploy import compose, images, keycloak, vault
from slas_deploy.images import DEFAULT_IMAGES, LockError, check_lock, default_lock
from slas_deploy.render import main as render_main
from slas_deploy.render import rendered_files, write_all
from slas_observability.metrics import METRIC_NAMES  # noqa: F401 — the observability files exist

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_MOUNTS = ("/tmp/.X11-unix", "/dev/input", "docker.sock")  # noqa: S108


def test_every_deployment_file_is_in_step_with_the_code() -> None:
    files = rendered_files()
    assert len(files) == 29
    for relative, content in files.items():
        path = REPO_ROOT / relative
        assert path.is_file(), f"{relative} is missing; run `uv run python -m slas_deploy.render`"
        assert path.read_text(encoding="utf-8") == content, f"{relative} drifted; regenerate it"
        if relative.endswith(".sh"):
            assert path.stat().st_mode & 0o111, f"{relative} must be executable"
            subprocess.run(["sh", "-n", str(path)], check=True, timeout=30)


def test_render_writes_everywhere_and_prints_a_count(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert len(write_all(tmp_path)) == 29
    assert render_main([str(tmp_path / "again")]) == 0
    assert "29 files written" in capsys.readouterr().out


def test_the_base_stack_follows_the_zone_model_and_adr_0003() -> None:
    doc = compose.base_compose()
    services = doc["services"]
    assert set(doc["networks"]) == set(compose.NETWORKS)
    for network, sole in compose.SOLE_MEMBERS.items():
        members = [s for s in services if network in compose.networks_of(doc, s)]
        assert members == [sole], f"{network} must have exactly {sole}"
    assert compose.networks_of(doc, "screen-worker") == ["slas-frontend", "slas-screen"]
    assert "slas-inference" not in compose.networks_of(doc, "sandbox-manager")
    assert "slas-inference" not in compose.networks_of(doc, "screen-worker")
    for name, service in services.items():
        assert "env_file" not in service, name
        assert service["security_opt"] == ["no-new-privileges:true"], name
        assert service["cap_drop"] == ["ALL"], name
        assert "healthcheck" in service, name
        probe = service["healthcheck"]["test"]
        first_party = "/slas/" in service["image"]
        # slas-health exists only in first-party images; a third-party container probes with
        # what its own image ships (wget, curl, bash's /dev/tcp) or its own binary.
        assert ("slas-health" in probe) == first_party, (name, probe)
        if probe[0] == "CMD":
            assert not any("$(" in part for part in probe), (name, "CMD form runs no shell")
        assert not service["image"].endswith(":latest"), name
        assert "${SLAS_REGISTRY}/" in service["image"], name
        for key, value in service.get("environment", {}).items():
            assert "PASSWORD" not in key or key.endswith("_FILE"), (name, key)
            assert not str(value).startswith("sk-"), (name, key)
        for volume in service.get("volumes", []):
            assert not any(part in volume for part in FORBIDDEN_MOUNTS), (name, volume)
            if compose.RUNTIME_SOCKET in volume:
                assert name in compose.RUNTIME_SOCKET_HOLDERS, name
        if name != "edge":
            assert "ports" not in service, f"only edge publishes a port, not {name}"
    assert services["edge"]["ports"] == ["${SLAS_HTTPS_PORT}:443"]
    assert services["edge"]["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": "0"}
    assert services["edge"]["environment"]["SLAS_TLS_MODE"] == "${SLAS_TLS_MODE}"
    for holder in compose.RUNTIME_SOCKET_HOLDERS:
        assert (
            f"{compose.RUNTIME_SOCKET}:{compose.RUNTIME_SOCKET_IN_CONTAINER}"
            in services[holder]["volumes"]
        )
    assert "SLAS_RUNTIME_SOCKET" in (REPO_ROOT / "config" / ".env.example").read_text()
    assert set(doc["secrets"]) == {*compose.QUICKSTART_SECRETS, "grafana_admin_password"}
    for secret in doc["secrets"].values():
        assert secret["file"].startswith("${SLAS_DATA_ROOT}/secrets/")
    assert services["git-broker"]["environment"]["CRED_STORE"] == "postgres+aesgcm"
    assert services["validation-executor"]["environment"]["LLM_IN_CONTROL_LOOP"] == "false"
    assert services["api"]["environment"]["SLAS_AUTH_MODES"] == "builtin"
    # ADR-0017: the api tells the WebUI which agents this install starts; the executors of the
    # optional agents sit behind compose profiles and start only when SLAS_AGENTS names them.
    assert services["api"]["environment"]["SLAS_AGENTS"] == "${SLAS_AGENTS:-coding}"
    assert services["validation-executor"]["profiles"] == ["validation"]
    assert services["factory-executor"]["profiles"] == ["factory"]
    assert services["vector-db"]["profiles"] == ["knowledge"]
    assert services["local-search-api"]["profiles"] == ["knowledge"]
    assert "profiles" not in services["agent-core-orchestrator"]
    assert "profiles" not in services["sandbox-manager"]
    assert set(services) >= {
        "edge", "webui", "api", "agent-core-orchestrator", "screen-worker", "llm-gateway",
        "model-manager", "vector-db", "local-search-api", "sandbox-manager", "git-broker",
        "validation-executor", "factory-executor", "postgres", "redis", "minio",
        "prometheus", "alertmanager", "grafana", "dcgm-exporter", "node-exporter",
        "postgres-exporter",
    }  # fmt: skip


def test_the_prod_override_adds_vault_oidc_kata_and_pitr() -> None:
    doc = compose.prod_override()
    services = doc["services"]
    assert {"vault", "keycloak", "loki", "tempo", "backup-runner", "minio-init"} <= set(services)
    assert compose.networks_of(doc, "vault") == ["slas-backend"]
    assert "slas-frontend" in compose.networks_of(doc, "keycloak")
    api = services["api"]["environment"]
    assert api["SLAS_AUTH_MODES"] == "builtin,oidc"
    assert api["SLAS_OIDC_ISSUER"].endswith("/auth/realms/slas")
    assert api["SLAS_OIDC_CLIENT_SECRET_FILE"] == "/run/secrets/oidc_client_secret"
    assert services["git-broker"]["environment"]["CRED_STORE"] == "vault"
    for executor in ("validation-executor", "factory-executor"):
        env = services[executor]["environment"]
        assert env["CREDENTIAL_SOURCE"] == "vault"
        assert env["VAULT_ROLE_ID_FILE"] == "/run/secrets/vault_approle_role_id"
        assert "VAULT_TOKEN" not in env, "tokens come from AppRole login at start, never .env"
    assert services["sandbox-manager"]["environment"]["DEFAULT_RUNTIME"] == "kata-fc"
    assert services["sandbox-manager"]["environment"]["SANDBOX_TIER"] == "kata"
    assert (
        services["postgres"]["image"] == "${SLAS_REGISTRY}/slas/postgres-pgbackrest:${SLAS_VERSION}"
    )
    assert (
        "../config/postgres/prod.conf:/etc/postgresql/prod.conf:ro"
        in services["postgres"]["volumes"]
    )
    assert services["backup-runner"]["healthcheck"]["test"] == [
        "CMD",
        "pgbackrest",
        "--stanza=slas",
        "check",
    ]
    assert set(doc["secrets"]) == set(compose.PROD_SECRETS)
    for name in ("vault", "keycloak", "loki", "tempo", "backup-runner"):
        assert "ports" not in services[name], name
        assert services[name]["cap_drop"] == ["ALL"], name
    assert services["minio-init"]["restart"] == "on-failure"


def test_macvlan_overlays_give_each_executor_its_own_address() -> None:
    lab = compose.macvlan_lab()
    factory = compose.macvlan_factory()
    assert lab["networks"]["slas-lab"]["driver"] == "macvlan"
    assert "SLAS_LAB_IFACE" in lab["networks"]["slas-lab"]["driver_opts"]["parent"]
    assert (
        "SLAS_LAB_EXECUTOR_IP"
        in lab["services"]["validation-executor"]["networks"]["slas-lab"]["ipv4_address"]
    )
    assert factory["networks"]["slas-factory"]["driver"] == "macvlan"
    assert (
        "SLAS_FACTORY_EXECUTOR_IP"
        in factory["services"]["factory-executor"]["networks"]["slas-factory"]["ipv4_address"]
    )
    assert (
        factory["services"]["factory-executor"]["environment"]["ENROLMENT_LISTEN"] == "0.0.0.0:8444"
    )
    assert set(lab["services"]) == {"validation-executor"} and set(factory["services"]) == {
        "factory-executor"
    }
    text = (REPO_ROOT / "compose" / "macvlan-factory.override.yml").read_text()
    assert (
        '"${SLAS_FACTORY_IFACE:?set SLAS_FACTORY_IFACE to the host NIC on the factory LAN}"' in text
    )


def test_the_image_lock_pins_tags_but_refuses_to_start_unpinned_images() -> None:
    lock = default_lock()
    assert all(
        ":" in image.reference and not image.reference.endswith(":latest") for image in lock.images
    )
    assert len({image.name for image in lock.images}) == len(lock.images)
    used = {
        service["image"]
        for doc in (compose.base_compose(), compose.prod_override())
        for service in doc["services"].values()
        if "image" in service
    }
    locked = {image.compose_ref() for image in lock.images}
    assert used <= locked, used - locked
    assert lock.sentence("quickstart").startswith("0 of ")
    assert "not pinned: postgres, redis, minio" in lock.sentence("quickstart")
    with pytest.raises(LockError) as exc:
        check_lock(lock, "prod")
    assert exc.value.message.what_to_do.startswith(
        "On a connected build host run scripts/lock-images.sh"
    )
    on_disk = json.loads((REPO_ROOT / "compose" / "images.lock.json").read_text())
    assert [i["name"] for i in on_disk["images"]] == [i.name for i in DEFAULT_IMAGES]
    prod_only = {i.name for i in lock.images if i.profiles == ["prod"]}
    assert prod_only == {"mc", "vault", "keycloak", "loki", "tempo", "postgres-pgbackrest"}
    assert all(i["started_by"] in ("compose", "model-manager") for i in on_disk["images"])
    assert "started_by: model-manager" in (REPO_ROOT / "compose" / "images.lock.yaml").read_text()


def test_the_vllm_image_is_locked_and_pulled_but_never_a_compose_service() -> None:
    """Contract round 2 §9: the vLLM image is in the lock for both profiles, pulled by the
    digest the lock records, handed to model-manager as SLAS_VLLM_IMAGE, and no compose file
    starts it — the model manager does, one container per role and voter."""
    lock = default_lock()
    vllm = next(image for image in lock.images if image.name == "vllm")
    assert vllm.reference == "vllm/vllm-openai:v0.29.0-x86_64-cu129"
    assert vllm.upstream == "docker.io/vllm/vllm-openai:v0.29.0-x86_64-cu129"
    assert vllm.digest == "sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a"
    assert vllm.pull_reference == f"docker.io/vllm/vllm-openai@{vllm.digest}"
    assert vllm.profiles == ["quickstart", "prod"] and not vllm.first_party
    assert vllm.started_by == "model-manager" and not vllm.pinned, "pinned needs the image ID too"
    assert images.started_by_model_manager(lock.images) == [vllm]
    assert vllm not in images.started_by_compose(lock.images)
    postgres = next(image for image in lock.images if image.name == "postgres")
    assert postgres.pull_reference == postgres.upstream, "no digest known: pulled by its tag"
    compose_refs = {
        service["image"]
        for doc in (compose.base_compose(), compose.prod_override())
        for service in doc["services"].values()
        if "image" in service
    }
    assert vllm.compose_ref() not in compose_refs
    assert {image.compose_ref() for image in images.started_by_compose(lock.images)} >= compose_refs
    model_manager = compose.base_compose()["services"]["model-manager"]["environment"]
    assert (
        model_manager["SLAS_VLLM_IMAGE"]
        == vllm.compose_ref()
        == "${SLAS_REGISTRY}/vllm/vllm-openai:v0.29.0-x86_64-cu129"
    )
    # The lock the bundle and --build produce carries it like the others.
    assert "vllm" in [
        i["name"]
        for i in json.loads((REPO_ROOT / "compose" / "images.lock.json").read_text())["images"]
    ]


def test_round_2_wiring_of_the_compose_services() -> None:
    """docs/api-contract-round-2.md §1, §3, §4, §6, §7, §8, §9 on the rendered stack."""
    doc = compose.base_compose()
    services = doc["services"]
    for service, variables in compose.URL_READERS.items():
        environment = services[service]["environment"]
        for variable in variables:
            assert environment[variable] == compose.SERVICE_URLS[variable], (service, variable)
        others = set(compose.SERVICE_URLS) - set(variables)
        assert not others & set(environment), f"{service} reads only the URLs the contract lists"
    for variable, url in compose.SERVICE_URLS.items():
        target = url.removeprefix("http://").split(":")[0]
        assert target in services and url.endswith(":8000"), variable
        assert "slas-backend" in compose.networks_of(doc, target), (
            f"{target} must be reachable on the backend network"
        )
    for service in (
        "api",
        "agent-core-orchestrator",
        "llm-gateway",
        "model-manager",
        "sandbox-manager",
        "git-broker",
        "validation-executor",
        "factory-executor",
    ):
        assert services[service]["environment"]["SLAS_BIND"] == "0.0.0.0:8000", service
        assert services[service]["healthcheck"]["test"] == [
            "CMD",
            "slas-health",
            "http://127.0.0.1:8000/health",
        ], service

    # model-manager (§3): the inference network by its real name, the host Models directory,
    # the vLLM image, the GPU budget and the reconcile interval; the runtime socket path inside.
    assert doc["networks"]["slas-inference"] == {"internal": True, "name": "slas_slas-inference"}
    assert (
        compose.INFERENCE_NETWORK_NAME == f"{doc['name']}_slas-inference" == "slas_slas-inference"
    )
    assert (REPO_ROOT / "install.sh").read_text().count("--project-name slas") == 1, (
        "compose names the network <project>_slas-inference"
    )
    mm = services["model-manager"]["environment"]
    assert mm["SLAS_INFERENCE_NETWORK"] == "slas_slas-inference"
    assert mm["SLAS_HOST_MODELS_DIR"] == "${SLAS_DATA_ROOT}/Models"
    assert mm["SLAS_GPU_VRAM_GIB"] == "${SLAS_GPU_VRAM_GIB:-270}"
    assert mm["SLAS_RECONCILE_INTERVAL_S"] == "30" and mm["SLAS_VLLM_SHM"] == "16g"
    assert (
        mm["SLAS_RUNTIME_SOCKET"]
        == compose.RUNTIME_SOCKET_IN_CONTAINER
        == "/run/podman/podman.sock"
    )
    assert (
        mm["SLAS_DATA_ROOT"] == "/data"
        and "${SLAS_DATA_ROOT}/Models:/data/Models" in services["model-manager"]["volumes"]
    )
    assert "slas-inference" in compose.networks_of(doc, "model-manager"), "it probes vllm-* itself"
    assert "SLAS_GPU_VRAM_GIB=270" in (REPO_ROOT / "config" / ".env.example").read_text()

    # sandbox-manager (§4).
    sm = services["sandbox-manager"]["environment"]
    assert sm["SLAS_HOST_DATA_ROOT"] == "${SLAS_DATA_ROOT}" and sm["SLAS_DATA_ROOT"] == "/data"
    assert sm["SLAS_TOOLCHAIN_MANIFEST"] == "/data/Toolchains/manifest.json"
    assert sm["SLAS_SANDBOX_REGISTRY"] == "${SLAS_REGISTRY}"
    assert sm["SLAS_SANDBOX_USER"] == "${SLAS_UID}:${SLAS_GID}", (
        "sandboxes write what the platform owns"
    )
    assert (
        sm["SLAS_RUNTIME_SOCKET"] == "/run/podman/podman.sock" and sm["DEFAULT_RUNTIME"] == "runsc"
    )
    assert services["sandbox-manager"]["volumes"] == [
        f"{compose.RUNTIME_SOCKET}:{compose.RUNTIME_SOCKET_IN_CONTAINER}",
        "${SLAS_DATA_ROOT}/Coding:/data/Coding",
        "${SLAS_DATA_ROOT}/Toolchains:/data/Toolchains:ro",
    ]
    assert "sandbox-manager" in services["agent-core-orchestrator"]["depends_on"]

    # git-broker (§7): its store under .git-broker, Coding, git-hosts.yaml writable.
    gb = services["git-broker"]
    assert gb["environment"]["SLAS_DATA_ROOT"] == "/data"
    assert "${SLAS_DATA_ROOT}/.git-broker:/data/.git-broker" in gb["volumes"]
    assert "${SLAS_DATA_ROOT}/Coding:/data/Coding" in gb["volumes"]
    assert "../config/git-hosts.yaml:/etc/slas/git-hosts.yaml:rw" in gb["volumes"]
    prod_gb = compose.prod_override()["services"]["git-broker"]["volumes"]
    assert (
        "../config/git-hosts.yaml:/etc/slas/git-hosts.yaml:rw" in prod_gb
        and "${SLAS_DATA_ROOT}/.git-broker:/data/.git-broker" in prod_gb
    )

    # The executors (§6).
    ve = services["validation-executor"]
    assert ve["environment"]["SLAS_TARGETS_REGISTRY"] == "/data/Validation/targets.json"
    assert {
        "${SLAS_DATA_ROOT}/Validation:/data/Validation",
        "${SLAS_DATA_ROOT}/Tickets:/data/Tickets",
    } <= set(ve["volumes"])
    fe = services["factory-executor"]
    assert fe["environment"]["SLAS_FACTORY_TEMPLATES"] == "/etc/slas/templates"
    assert (
        fe["environment"]["ENROLMENT_LISTEN"] == "0.0.0.0:8444"
        and fe["environment"]["STATION_RUNNER_PORT"] == "8443"
    )
    assert fe["environment"]["MES_INBOX"] == "/data/Factory/mes/inbox"
    assert fe["environment"]["RUNNER_MTLS_CA"] == "/data/Factory/ca/ca.pem"
    assert {
        "${SLAS_DATA_ROOT}/Factory:/data/Factory",
        "${SLAS_DATA_ROOT}/Tickets:/data/Tickets",
        "../templates/factory:/etc/slas/templates:ro",
    } <= set(fe["volumes"])
    assert "ports" not in fe, (
        "enrolment listens on the factory network; the macvlan overlay gives it an address, no host port"
    )
    assert (REPO_ROOT / "templates" / "factory").is_dir()

    # The orchestrator mounts the whole data root (§5); the api reads the §8 URLs.
    assert "${SLAS_DATA_ROOT}:/data" in services["agent-core-orchestrator"]["volumes"]
    assert "${SLAS_DATA_ROOT}:/data" in services["api"]["volumes"]

    # Every bind-mount source under the data root is a directory install.sh creates first.
    sources = set()
    for service in services.values():
        for volume in service.get("volumes", []):
            source = volume.split(":", 1)[0]
            if source.startswith("${SLAS_DATA_ROOT}/"):
                sources.add(source.removeprefix("${SLAS_DATA_ROOT}/"))
    created = set(compose.DATA_DIRECTORIES)
    for source in sources:
        covered = (
            source in created
            or any(d.startswith(f"{source}/") for d in created)  # a parent of a created one
            or any(source.startswith(f"{d}/") for d in created)  # a file inside a created one
        )
        assert covered, (
            f"install.sh must create {source} under the data root before compose mounts it"
        )
    for directory in ("Tickets", "Skills/library", "SOP", "Factory/mes/inbox", "Factory/ca"):
        assert directory in created

    # Prometheus scrapes the voters; prod mounts the prod rendering with its third voter.
    prometheus_yml = (REPO_ROOT / "observability" / "prometheus" / "prometheus.yml").read_text()
    assert "vllm-voter-deepseek-v4-flash:8000" in prometheus_yml
    prod = compose.prod_override()["services"]["prometheus"]["volumes"]
    assert (
        "../observability/prometheus/prometheus.prod.yml:/etc/prometheus/prometheus.yml:ro" in prod
    )
    assert (REPO_ROOT / "observability" / "prometheus" / "prometheus.prod.yml").is_file()


def test_vault_policies_are_least_privilege_and_the_realm_maps_the_platform_roles() -> None:
    assert set(vault.POLICIES) == {"validation-executor", "factory-executor", "git-broker", "api"}
    assert vault.policy_hcl("validation-executor").count('path "slas/data/lab/*"') == 1
    assert '"delete"' not in vault.policy_hcl("validation-executor")
    assert '"create", "read", "update"' in vault.policy_hcl("git-broker")
    assert "ui = false" in vault.vault_hcl() and "tls_cert_file" in vault.vault_hcl()
    commands = vault.bootstrap_commands()
    assert commands[0] == ["vault", "secrets", "enable", "-path", "slas", "-version=2", "kv"]
    assert any(c[:3] == ["vault", "write", "auth/approle/role/git-broker"] for c in commands)
    realm = keycloak.realm_export()
    assert realm["realm"] == "slas" and realm["bruteForceProtected"] is True
    assert {r["name"] for r in realm["roles"]["realm"]} == {
        "administrator",
        "engineer",
        "line_lead",
        "viewer",
    }
    client = realm["clients"][0]
    assert client["clientId"] == "slas-webui" and client["publicClient"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["protocolMappers"][0]["config"]["claim.name"] == "slas_roles"
    assert realm["identityProviders"] == [] and realm["defaultRoles"] == ["engineer"]


def test_installer_writes_env_and_secrets_idempotently(tmp_path: Path) -> None:
    from slas_deploy.installer import compose_files, main, write_env, write_secrets
    from slas_schemas.envfile import read_env

    target = tmp_path / "data" / ".env"
    changed = write_env(
        example=REPO_ROOT / "config" / ".env.example",
        target=target,
        profile="prod",
        data_root=tmp_path / "data",
        version="0.0.1",
        registry="harbor.internal",
        uid=1000,
        gid=1000,
        tls_names="127.0.0.1,localhost",
        public_host="slas.lab.internal",
    )
    assert "SLAS_AUTH_MODES" in changed and "SLAS_PROFILE" in changed
    assert "SLAS_SANDBOX_TIER" in changed
    env = read_env(target)
    assert env.get("SLAS_PROFILE") == "prod" and env.get("SLAS_SANDBOX_TIER") == "kata"
    assert env.get("SLAS_BACKUP_RETENTION_DAYS") == "90"
    assert target.stat().st_mode & 0o777 == 0o600
    env.set("SLAS_BACKUP_RETENTION_DAYS", "365")
    target.write_text(env.render())
    second = write_env(
        example=REPO_ROOT / "config" / ".env.example", target=target, profile="prod",
        data_root=tmp_path / "data", version="0.0.2", registry="harbor.internal", uid=1000,
        gid=1000, tls_names="x", public_host="y",
    )  # fmt: skip
    assert second == ["SLAS_VERSION"], "a second run changes only what the installer owns"
    assert read_env(target).get("SLAS_BACKUP_RETENTION_DAYS") == "365"

    created = write_secrets(tmp_path / "data" / "secrets", "prod")
    assert set(created) == {
        *compose.QUICKSTART_SECRETS,
        "grafana_admin_password",
        *compose.PROD_SECRETS,
    }
    assert (tmp_path / "data" / "secrets").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "data" / "secrets" / "secret_key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data" / "secrets" / "postgres_password").stat().st_mode & 0o777 == 0o644
    redis_conf = (tmp_path / "data" / "secrets" / "redis.conf").read_text()
    assert redis_conf.startswith("requirepass ") and "protected-mode yes" in redis_conf
    assert write_secrets(tmp_path / "data" / "secrets", "prod") == []

    files = compose_files("prod", read_env(target), tmp_path / "compose")
    assert [Path(f).name for f in files] == ["docker-compose.yml", "prod.override.yml"]
    env.set("SLAS_LAB_IFACE", "eno2")
    env.set("SLAS_FACTORY_IFACE", "eno3")
    assert [Path(f).name for f in compose_files("prod", env, tmp_path / "compose")] == [
        "docker-compose.yml", "prod.override.yml", "macvlan.override.yml", "macvlan-factory.override.yml",
    ]  # fmt: skip

    out = io.StringIO()
    assert (
        main(
            [
                "check-lock",
                "--lock",
                str(REPO_ROOT / "compose/images.lock.json"),
                "--profile",
                "prod",
            ],
            stdout=out,
        )
        == 1
    )
    assert "not pinned" in out.getvalue() and "scripts/lock-images.sh" in out.getvalue()
