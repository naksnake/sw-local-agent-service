"""The rest of the prod profile against fakes: OIDC sign-in beside built-in accounts, the
Kata/Firecracker sandbox tier, the prod doctor checks, cosign verification, pgBackRest
commands, the restore drill and `slas backup`."""

from __future__ import annotations

# ruff: noqa: E501 — long literal sentences and argv lists read better unwrapped
import io
import json
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slas_authz.oidc import OidcClient, OidcError, OidcSettings, auth_modes, pkce_pair
from slas_authz.roles import default_roles
from slas_cli.cli import main as cli_main
from slas_cli.doctor.checks import DoctorSettings, check_kata_tier, check_signature_tooling
from slas_cli.doctor.fakes import FakeHost
from slas_cli.doctor.host import CommandResult
from slas_deploy.cosign import CosignVerifier, SignatureError, verify_bundle, write_manifest
from slas_deploy.images import BundleManifest, LockedImage, check_manifest, default_lock
from slas_deploy.pgbackrest import (
    BackupError,
    BackupRunner,
    RestoreDrill,
    Steps,
    backup_runner_script,
    minio_init_script,
    pgbackrest_conf,
)
from slas_hal.drivers.process import FakeProcessRunner
from slas_hal.hal import CommandResult as HalResult
from slas_kernel.clock import FakeClock
from slas_sandbox_manager.manager import SandboxError, SandboxManager
from slas_sandbox_manager.runtime import FakeSandboxRuntime
from slas_sandbox_manager.spec import SandboxSpec, podman_argv

ISSUER = "https://slas.lab.internal/auth/realms/slas"


# --- OIDC -----------------------------------------------------------------------------------------


class FakeKeycloak:
    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.client_secret = "kc-client-secret"
        self.posts: list[dict[str, str]] = []

    def get_json(self, url: str, headers: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        if url.endswith("/.well-known/openid-configuration"):
            return 200, {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
                "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
            }
        if url.endswith("/userinfo"):
            token = headers.get("Authorization", "").removeprefix("Bearer ")
            claims = self.codes.get(token)
            return (200, claims) if claims else (401, {"error": "invalid_token"})
        return 404, {}

    def post_form(
        self, url: str, form: Mapping[str, str], headers: Mapping[str, str]
    ) -> tuple[int, dict[str, Any]]:
        self.posts.append(dict(form))
        if form.get("client_secret") != self.client_secret:
            return 401, {
                "error": "unauthorized_client",
                "error_description": "Invalid client secret",
            }
        if form.get("code") not in self.codes:
            return 400, {"error": "invalid_grant"}
        return 200, {"access_token": form["code"], "token_type": "Bearer"}


def test_oidc_login_round_trip_maps_realm_roles_to_platform_roles() -> None:
    kc = FakeKeycloak()
    kc.codes["code-lee"] = {
        "sub": "u-1",
        "email": "Lee@Example.com",
        "name": "Lee",
        "slas_roles": ["offline_access", "line_lead"],
    }
    kc.codes["code-pat"] = {
        "sub": "u-2",
        "email": "pat@example.com",
        "preferred_username": "pat",
        "realm_access": {"roles": ["nobody"]},
    }
    settings = OidcSettings(
        issuer=ISSUER,
        client_id="slas-webui",
        redirect_uri="https://slas.lab.internal/auth/callback",
    )
    client = OidcClient(settings, http=kc, client_secret="kc-client-secret", roles=default_roles())
    start = client.start_login()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.url).query)
    assert query["response_type"] == ["code"] and query["code_challenge_method"] == ["S256"]
    assert query["state"] == [start.state] and query["nonce"] == [start.nonce]
    assert query["scope"] == ["openid profile email"]
    identity = client.finish_login(code="code-lee", returned_state=start.state, expected=start)
    assert identity.email == "lee@example.com" and identity.role == "line_lead"
    assert identity.sentence() == "Lee (lee@example.com) signed in through Keycloak as line_lead."
    assert kc.posts[-1]["code_verifier"] == start.pkce.verifier
    default = client.finish_login(code="code-pat", returned_state=start.state, expected=start)
    assert default.role == "engineer" and default.display_name == "pat"
    with pytest.raises(OidcError) as state:
        client.finish_login(code="code-lee", returned_state="tampered", expected=start)
    assert state.value.message.what_happened == "The sign-in did not come back the way it left."
    with pytest.raises(OidcError) as bad_code:
        client.finish_login(code="nope", returned_state=start.state, expected=start)
    assert bad_code.value.message.what_happened == "Keycloak did not exchange the sign-in code."
    wrong_secret = OidcClient(settings, http=kc, client_secret="wrong", roles=default_roles())
    with pytest.raises(OidcError) as secret:
        wrong_secret.finish_login(code="code-lee", returned_state=start.state, expected=start)
    assert secret.value.message.likely_cause == "Invalid client secret"
    with pytest.raises(OidcError) as no_email:
        client.identity_from_claims({"sub": "u-3"})
    assert "names no account" in no_email.value.message.what_happened
    other = OidcClient(
        settings.model_copy(update={"issuer": "https://other/realms/x"}),
        http=kc,
        client_secret="kc-client-secret",
        roles=default_roles(),
    )
    with pytest.raises(OidcError) as issuer:
        other.discover()
    assert "different issuer" in issuer.value.message.what_happened
    pair = pkce_pair()
    assert len(pair.verifier) >= 43 and pair.challenge != pair.verifier
    assert auth_modes("oidc, builtin") == ["builtin", "oidc"]
    with pytest.raises(OidcError):
        auth_modes("ldap")


# --- the Kata tier and the prod doctor checks --------------------------------------------------------


def test_the_kata_tier_runs_micro_vms_or_says_what_is_missing(tmp_path: Path) -> None:
    def manager(**kwargs: Any) -> SandboxManager:
        return SandboxManager(
            runtime=FakeSandboxRuntime(),
            data_root=tmp_path,
            clock=FakeClock(),
            runsc_available=True,
            profile="prod",
            **kwargs,
        )

    runtime, sentence = manager(tier="kata", kata_available=True).runtime_choice()
    assert runtime == "kata-fc" and "Firecracker" in sentence
    with pytest.raises(SandboxError) as exc:
        manager(tier="kata", kata_available=False).runtime_choice()
    assert (
        exc.value.message.what_happened
        == "No sandbox can be opened: the Kata/Firecracker tier is not installed."
    )
    assert "SANDBOX_TIER=gvisor" in exc.value.message.what_to_do
    assert manager(tier="gvisor").runtime_choice()[0] == "runsc"
    spec = SandboxSpec(
        name="slas-sbx-1",
        image="registry.internal/slas/sandbox-python:3.12.6",
        runtime="kata-fc",
        user="pat",
        slug="bmc",
        mounts=[
            {"source": "/a", "target": "/workspace", "mode": "rw"},
            {"source": "/b", "target": "/scratch", "mode": "rw"},
            {"source": "/c", "target": "/etc/slas/gitconfig", "mode": "ro"},
        ],
    )
    argv = podman_argv(spec)
    assert "--runtime" in argv and argv[argv.index("--runtime") + 1] == "kata-fc"
    assert "Kata micro-VM on Firecracker" in spec.sentence()

    prod = DoctorSettings(profile="prod", data_root="/AI/Agent")
    host = FakeHost.healthy()
    assert check_signature_tooling(host, prod).status == "fail"
    assert "cosign was not found" in check_signature_tooling(host, prod).summary
    host.commands["cosign"] = "/usr/local/bin/cosign"
    assert check_signature_tooling(host, prod).status == "ok"
    kata = check_kata_tier(host, prod)
    assert kata.status == "warn" and "Kata Containers and Firecracker not found" in kata.summary
    host.commands["kata-runtime"] = "/usr/bin/kata-runtime"
    host.commands["firecracker"] = "/usr/bin/firecracker"
    assert check_kata_tier(host, prod).status == "ok"
    quick = DoctorSettings(profile="quickstart", data_root="/AI/Agent")
    assert check_signature_tooling(FakeHost.healthy(), quick).status == "ok"
    assert check_kata_tier(FakeHost.healthy(), quick).status == "ok"


# --- cosign and the bundle -----------------------------------------------------------------------------


def test_cosign_verifies_the_bundle_manifest_then_every_file_and_refuses_tampering(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "images").mkdir(parents=True)
    (bundle / "images" / "postgres.tar").write_bytes(b"not really a tarball")
    write_manifest(
        bundle,
        "0.0.1",
        "2026-09-14T10:00:00Z",
        {"registry.internal/library/postgres:16.6": "sha256:" + "a" * 64},
    )
    (bundle / "manifest.json.sig").write_text("MEUCIQ…fake…")
    key = tmp_path / "cosign.pub"
    key.write_text("-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n")
    runner = FakeProcessRunner()
    runner.on("cosign", "verify-blob", result=HalResult(exit_code=0, stdout="Verified OK\n"))
    runner.on("cosign", "verify", result=HalResult(exit_code=0, stdout="[]\n"))
    verifier = CosignVerifier(runner, public_key=key)
    manifest = verify_bundle(bundle, verifier)
    assert manifest.version == "0.0.1" and "images/postgres.tar" in manifest.files
    argv = runner.calls[0][0]
    assert argv[:3] == ["cosign", "verify-blob", "--key"] and "--insecure-ignore-tlog" in argv
    assert "--private-infrastructure" in argv and argv[-1].endswith("manifest.json")
    verifier.verify_image("harbor.internal/library/postgres@sha256:" + "a" * 64)
    assert runner.calls[-1][0][:2] == ["cosign", "verify"]

    (bundle / "images" / "postgres.tar").write_bytes(b"tampered")
    with pytest.raises(SignatureError) as tampered:
        verify_bundle(bundle, verifier)
    assert (
        tampered.value.message.what_happened
        == "images/postgres.tar in the bundle does not match the signed manifest."
    )
    bad = FakeProcessRunner()
    bad.on(
        "cosign",
        result=HalResult(
            exit_code=1, stderr="Error: invalid signature when validating ASN.1 encoded signature\n"
        ),
    )
    with pytest.raises(SignatureError) as unsigned:
        verify_bundle(bundle, CosignVerifier(bad, public_key=key))
    assert unsigned.value.message.what_happened == "The signature on manifest.json does not verify."
    assert "invalid signature" in unsigned.value.message.likely_cause
    with pytest.raises(SignatureError) as no_key:
        CosignVerifier(runner, public_key=tmp_path / "missing.pub").verify_image(
            "x@sha256:" + "b" * 64
        )
    assert no_key.value.message.what_happened.startswith("The release public key")
    with pytest.raises(SignatureError) as incomplete:
        verify_bundle(tmp_path / "empty", verifier)
    assert "has no manifest.json" in incomplete.value.message.what_happened

    # A pinned lock against a manifest: matching IDs pass, a different ID or a missing image is named.
    lock = default_lock()
    pinned = [
        img.model_copy(
            update={
                "digest": "sha256:" + "c" * 64,
                "image_id": "sha256:" + ("a" if img.name == "postgres" else "d") * 64,
            }
        )
        for img in lock.images
    ]
    lock = lock.model_copy(update={"images": pinned})
    ids = {
        f"registry.internal/{img.reference}".replace("${SLAS_VERSION}", "0.0.1"): str(img.image_id)
        for img in pinned
    }
    full = BundleManifest(version="0.0.1", built_at="x", images=ids)
    assert check_manifest(lock, full, "prod") == []
    ids.pop("registry.internal/library/redis:7.4.2")
    ids["registry.internal/library/postgres:16.6"] = "sha256:" + "f" * 64
    problems = check_manifest(
        lock, BundleManifest(version="0.0.1", built_at="x", images=ids), "quickstart"
    )
    assert (
        problems[0]
        == "registry.internal/library/postgres:16.6 has image ID sha256:ffffffffffff…, the lock expects sha256:aaaaaaaaaaaa…"
    )
    assert "registry.internal/library/redis:7.4.2 is not in the bundle" in problems
    assert LockedImage(name="x", reference="a/b:1", upstream="up/x").pinned is False


# --- pgBackRest, the drill and `slas backup` -------------------------------------------------------------


def test_pgbackrest_argv_configuration_and_the_object_lock_script() -> None:
    conf = pgbackrest_conf()
    assert "repo1-s3-bucket=slas-backups" in conf and "repo1-retention-full=4" in conf
    assert "repo1-s3-key-file=/run/secrets/pgbackrest_s3_key" in conf
    assert "password" not in conf.lower().replace("key-secret-file", "")
    init = minio_init_script()
    assert "mc mb --ignore-existing --with-lock slas/slas-backups" in init
    assert (
        'mc retention set --default compliance "${BACKUP_RETENTION_DAYS}d" slas/slas-backups'
        in init
    )
    assert (
        'mc retention set --default governance "${ARTIFACT_RETENTION_DAYS}d" slas/slas-artifacts'
        in init
    )
    assert 'pgbackrest --stanza="$STANZA" --type="$1" backup' in backup_runner_script()

    runner = FakeProcessRunner()
    info = [
        {
            "name": "slas",
            "backup": [
                {
                    "label": "20260914-020000F",
                    "type": "full",
                    "timestamp": {"start": 1789344000, "stop": 1789344600},
                    "info": {"size": 1024},
                },
                {
                    "label": "20260915-020000F_20260915-020000D",
                    "type": "diff",
                    "timestamp": {"start": 1789430400, "stop": 1789430500},
                    "info": {"size": 128},
                },
            ],
        }
    ]
    runner.on("docker", result=HalResult(exit_code=0, stdout=json.dumps(info)))
    backups = BackupRunner(runner, prefix=["docker", "compose", "exec", "-T", "backup-runner"])
    assert backups.backup("full") == "The full backup finished and is in the locked repository."
    assert backups.calls[0] == [
        "docker",
        "compose",
        "exec",
        "-T",
        "backup-runner",
        "pgbackrest",
        "--stanza=slas",
        "--type",
        "full",
        "backup",
    ]
    listed = backups.info()
    assert [b.kind for b in listed] == ["full", "diff"] and listed[0].size_bytes == 1024
    assert backups.restore(target_time=datetime(2026, 9, 15, 1, 30, tzinfo=UTC)).startswith(
        "PostgreSQL was restored to 2026-09-15 01:30 UTC"
    )
    assert "--type=time" in backups.calls[-1] and "--target-action=promote" in backups.calls[-1]
    assert any(a.startswith("--target=2026-09-15 01:30:00") for a in backups.calls[-1])
    failing = FakeProcessRunner()
    failing.on(
        "pgbackrest",
        result=HalResult(exit_code=52, stderr="ERROR: [052]: unable to find a valid repository\n"),
    )
    with pytest.raises(BackupError) as exc:
        BackupRunner(failing).check()
    assert exc.value.message.what_happened == "The repository check did not finish."
    assert "unable to find a valid repository" in exc.value.message.likely_cause


def test_the_restore_drill_records_every_phase_and_the_rto(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 14, 3, 0, tzinfo=UTC), step=timedelta(seconds=30))
    runner = FakeProcessRunner()
    runner.on("docker", result=HalResult(exit_code=0, stdout="ok\n"))
    runner.on("pgbackrest", result=HalResult(exit_code=0))
    steps = Steps(
        stop_writers=["docker", "compose", "stop", "api"], stop_postgres=["docker", "compose", "stop", "postgres"],
        start_postgres=["docker", "compose", "start", "postgres"], wait_ready=["docker", "compose", "exec", "-T", "postgres", "pg_isready"],
        verify=["docker", "compose", "exec", "-T", "api", "slas-api", "verify-restore"], start_writers=["docker", "compose", "start", "api"],
    )  # fmt: skip
    drill = RestoreDrill(
        BackupRunner(runner),
        runner,
        steps,
        clock=clock,
        data_root=tmp_path,
        environment="rehearsal against fakes",
    )
    record = drill.run(target_time=datetime(2026, 9, 14, 2, 30, tzinfo=UTC))
    assert record.ok and [p.name for p in record.phases] == [
        "stop writers", "stop postgres", "pgbackrest restore", "start postgres", "wait until ready", "verify the platform's data", "start writers",
    ]  # fmt: skip
    assert all(p.seconds == 30 for p in record.phases)
    # the fake clock ticks 30 s per reading: one at the start, two per phase, one at the end
    assert record.rto_seconds == 30 * (2 * len(record.phases) + 1)
    assert (
        record.sentence()
        == "Restore drill on 2026-09-14 (rehearsal against fakes): the platform came back to 2026-09-14 02:30 in 7.5 minutes across 7 phases."
    )
    saved = list((tmp_path / "Backups" / "drills").glob("*.json"))
    assert (
        len(saved) == 1
        and json.loads(saved[0].read_text())["environment"] == "rehearsal against fakes"
    )

    broken = FakeProcessRunner()
    broken.on("docker", result=HalResult(exit_code=0))
    broken.on(
        "pgbackrest", result=HalResult(exit_code=40, stderr="ERROR: [040]: unable to restore\n")
    )
    failed = RestoreDrill(BackupRunner(broken), broken, steps, clock=clock, data_root=tmp_path).run(
        target_time=None
    )
    assert not failed.ok
    names = [(p.name, p.ok) for p in failed.phases]
    assert ("pgbackrest restore", False) in names and ("start writers", True) in names
    assert ("verify the platform's data", True) not in names, (
        "nothing after a failed restore is verified"
    )


def test_slas_backup_commands_run_pgbackrest_through_compose_exec() -> None:
    host = FakeHost.healthy()
    info = json.dumps(
        [
            {
                "name": "slas",
                "backup": [
                    {
                        "label": "L1",
                        "type": "full",
                        "timestamp": {"start": 1789344000, "stop": 1789344600},
                        "info": {"size": 5},
                    }
                ],
            }
        ]
    )
    host.outputs[("docker",)] = CommandResult(0, info)
    common = [
        "backup",
        "--data-root",
        "/AI/Agent",
        "--compose-file",
        "/opt/slas/compose/docker-compose.yml",
        "--compose-file",
        "/opt/slas/compose/prod.override.yml",
    ]

    def run(*args: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli_main([*common, *args], host=host, stdout=out, environ={})
        return code, out.getvalue()

    code, out = run("now", "--type", "full")
    assert code == 0 and out == "The full backup finished and is in the locked repository.\n"
    assert host.calls[-1][:4] == ("docker", "compose", "--project-name", "slas")
    assert host.calls[-1][-5:] == ("pgbackrest", "--stanza=slas", "--type", "full", "backup")
    assert "backup-runner" in host.calls[-1] and "--env-file" in host.calls[-1]
    code, out = run("status")
    assert code == 0 and "1 backup in the locked repository" in out and "full backup L1" in out
    code, out = run("restore", "--to", "2026-09-14T02:30:00+00:00")
    assert code == 1 and "run again with --yes" in out
    code, out = run("restore", "--to", "2026-09-14T02:30:00+00:00", "--yes")
    assert code == 0 and out.startswith("PostgreSQL was restored to 2026-09-14 02:30 UTC")
    code, out = run("drill", "--environment", "rehearsal against fakes")
    assert code == 0 and out.startswith("Restore drill on ")
    assert "ok   pgbackrest restore" in out and "Recorded under /AI/Agent/Backups/drills/." in out
    with pytest.raises(SystemExit):
        run()  # no action: argparse prints the help and exits
