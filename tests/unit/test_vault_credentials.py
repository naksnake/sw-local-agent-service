"""Vault at dispatch (prod): a fake Vault KV v2 on loopback, AppRole login from secret files,
`vault:` references resolved by the executors' resolver, git-broker's credential store."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from slas_git.credentials import CredentialError as GitCredentialError
from slas_git.credentials import VaultCredentialStore
from slas_hal.credentials import (
    CredentialError,
    LocalCredentialResolver,
    VaultCredentialResolver,
    resolver_for,
)
from slas_schemas.vault import UrllibVaultHttp, VaultError, VaultKv


class FakeVault:
    """KV v2 and AppRole over plain HTTP on loopback (TLS is the real listener's job)."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.role_id, self.secret_id = "role-123", "secret-456"
        self.token = "hvs.test-token"
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length)) if length else {}

            def _authed(self) -> bool:
                return self.headers.get("X-Vault-Token") == outer.token

            def do_GET(self) -> None:
                outer.requests.append(("GET", self.path))
                if self.path == "/v1/sys/health":
                    self._send(200, {"sealed": False})
                    return
                if not self._authed():
                    self._send(403, {"errors": ["permission denied"]})
                    return
                path = self.path.removeprefix("/v1/slas/data/")
                if path.startswith("git/") and "deny" in path:
                    self._send(403, {"errors": ["permission denied"]})
                    return
                data = outer.secrets.get(path)
                if data is None:
                    self._send(404, {"errors": []})
                    return
                self._send(200, {"data": {"data": data, "metadata": {"version": 1}}})

            def do_POST(self) -> None:
                outer.requests.append(("POST", self.path))
                body = self._body()
                if self.path == "/v1/auth/approle/login":
                    if (
                        body.get("role_id") == outer.role_id
                        and body.get("secret_id") == outer.secret_id
                    ):
                        self._send(200, {"auth": {"client_token": outer.token}})
                    else:
                        self._send(400, {"errors": ["invalid role or secret ID"]})
                    return
                if not self._authed():
                    self._send(403, {"errors": ["permission denied"]})
                    return
                path = self.path.removeprefix("/v1/slas/data/")
                outer.secrets[path] = {str(k): str(v) for k, v in body.get("data", {}).items()}
                self._send(200, {"data": {"version": 1}})

            def do_DELETE(self) -> None:
                outer.requests.append(("DELETE", self.path))
                path = self.path.removeprefix("/v1/slas/metadata/")
                outer.secrets.pop(path, None)
                self._send(204, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def address(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> FakeVault:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def vault(tmp_path: Path):  # type: ignore[no-untyped-def]
    fake = FakeVault().start()
    fake.secrets["lab/gx8-01/bmc"] = {"user": "slas-validation", "password": "Bmc-Passw0rd!"}
    fake.secrets["lab/gx8-01/ssh"] = {"private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"}
    (tmp_path / "role_id").write_text("role-123\n")
    (tmp_path / "secret_id").write_text("secret-456\n")
    try:
        yield fake
    finally:
        fake.stop()


def kv_for(vault: FakeVault, tmp_path: Path) -> VaultKv:
    kv = VaultKv(vault.address, mount="slas", http=UrllibVaultHttp())
    kv.login_from_files(tmp_path / "role_id", tmp_path / "secret_id")
    return kv


def test_approle_login_reads_secret_files_and_wrong_ids_fail_in_three_parts(
    vault: FakeVault, tmp_path: Path
) -> None:
    kv = VaultKv(vault.address, mount="slas", http=UrllibVaultHttp())
    assert bool(kv.authenticated) is False and kv.health()
    with pytest.raises(VaultError) as before:
        kv.read("lab/gx8-01/bmc")
    assert before.value.message.what_happened == "This service is not logged in to Vault."
    kv.login_from_files(tmp_path / "role_id", tmp_path / "secret_id")
    assert bool(kv.authenticated) is True
    assert kv.read("lab/gx8-01/bmc")["password"] == "Bmc-Passw0rd!"
    wrong = VaultKv(vault.address, mount="slas", http=UrllibVaultHttp())
    with pytest.raises(VaultError) as exc:
        wrong.login_approle("role-123", "nope")
    assert exc.value.message.what_happened == "Vault refused the AppRole login."
    assert "Rotate the AppRole secret id" in exc.value.message.what_to_do
    with pytest.raises(VaultError) as missing:
        wrong.login_from_files(tmp_path / "nowhere", tmp_path / "secret_id")
    assert missing.value.message.what_happened == "The AppRole secret files are not readable."
    # No secret in any URL: paths name the secret, never its value.
    assert all("Passw0rd" not in path for _, path in vault.requests)


def test_vault_references_resolve_at_dispatch_and_say_what_is_missing(
    vault: FakeVault, tmp_path: Path
) -> None:
    kv = kv_for(vault, tmp_path)
    (tmp_path / "Factory" / "keys").mkdir(parents=True)
    (tmp_path / "Factory" / "keys" / "station-07.key").write_text("k" * 32)
    resolver = VaultCredentialResolver(
        kv, fallback=LocalCredentialResolver(root=tmp_path, environ={"X": "y"})
    )
    assert resolver.resolve("vault:slas/lab/gx8-01/bmc/password") == "Bmc-Passw0rd!"
    assert resolver.resolve("vault:slas/lab/gx8-01/ssh/private_key").startswith("-----BEGIN")
    assert resolver.resolve("file:Factory/keys/station-07.key") == "k" * 32
    assert resolver.resolve("env:X") == "y"
    assert resolver.asked == [
        "vault:slas/lab/gx8-01/bmc/password",
        "vault:slas/lab/gx8-01/ssh/private_key",
    ]
    with pytest.raises(CredentialError) as short:
        resolver.resolve("vault:slas/only")
    assert short.value.message.what_happened == "The reference vault:slas/only is too short."
    with pytest.raises(CredentialError) as other_mount:
        resolver.resolve("vault:kv/lab/gx8-01/bmc/password")
    assert (
        "names the mount kv, but this service reads slas" in other_mount.value.message.what_happened
    )
    with pytest.raises(CredentialError) as no_key:
        resolver.resolve("vault:slas/lab/gx8-01/bmc/token")
    assert no_key.value.message.what_happened == "The secret at slas/lab/gx8-01/bmc has no token."
    with pytest.raises(CredentialError) as no_path:
        resolver.resolve("vault:slas/lab/gx8-99/bmc/password")
    assert no_path.value.message.what_happened == "Vault has no secret at slas/lab/gx8-99/bmc."
    strict = VaultCredentialResolver(kv)
    with pytest.raises(CredentialError) as env_only:
        strict.resolve("env:X")
    assert "resolves vault: references only" in env_only.value.message.what_happened


def test_resolver_for_picks_vault_in_prod_and_env_in_quickstart(
    vault: FakeVault, tmp_path: Path
) -> None:
    quick = resolver_for("quickstart", environ={"LAB_PW": "x"})
    assert quick.resolve("env:LAB_PW") == "x"
    with pytest.raises(CredentialError) as exc:
        resolver_for("prod", environ={"CREDENTIAL_SOURCE": "vault"})
    assert (
        exc.value.message.what_happened
        == "CREDENTIAL_SOURCE is vault, but this service has no Vault client."
    )
    prod = resolver_for("prod", environ={}, root=tmp_path, kv=kv_for(vault, tmp_path))
    assert isinstance(prod, VaultCredentialResolver)
    assert prod.resolve("vault:slas/lab/gx8-01/bmc/user") == "slas-validation"
    env_in_prod = resolver_for("prod", environ={"CREDENTIAL_SOURCE": "env", "A": "b"})
    assert env_in_prod.resolve("env:A") == "b"


def test_git_broker_keeps_remote_credentials_in_vault_by_reference(
    vault: FakeVault, tmp_path: Path
) -> None:
    store = VaultCredentialStore(kv_for(vault, tmp_path))
    now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    ref = store.put(
        "glpat-secret-token", owner="lee", auth_type="pat", fingerprint="…oken", now=now
    )
    assert ref.startswith("cred-") and store.reveal(ref, owner="lee") == "glpat-secret-token"
    assert store.fingerprint_of(ref) == "…oken"
    assert vault.secrets[f"git/{ref}"]["owner"] == "lee"
    with pytest.raises(GitCredentialError) as someone_else:
        store.reveal(ref, owner="pat")
    assert someone_else.value.message.likely_cause == "It belongs to someone else."
    store.rotate(ref, "glpat-new-token", owner="lee", fingerprint="…oken2", now=now)
    assert store.reveal(ref, owner="lee") == "glpat-new-token"
    assert vault.secrets[f"git/{ref}"]["rotated_at"] == now.isoformat()
    store.delete(ref, owner="lee")
    with pytest.raises(GitCredentialError) as gone:
        store.reveal(ref, owner="lee")
    assert gone.value.message.what_happened == "That credential is not available."
    assert store.fingerprint_of(ref) == "unknown"
    with pytest.raises(GitCredentialError) as denied:
        store.reveal("deny-me", owner="lee")
    assert "does not grant the path" in denied.value.message.likely_cause
    # The token travelled in request bodies over the backend network, never in a URL.
    assert all("glpat" not in path for _, path in vault.requests)
