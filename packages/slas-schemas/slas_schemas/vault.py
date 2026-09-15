"""A Vault KV v2 client from the standard library (CLAUDE.md §3 prod "Vault, injected at
dispatch"; INV-5, INV-14). Used by the executors to resolve `vault:` credential references
and by git-broker to keep remote credentials by reference. AppRole login with the role id
and secret id read from Docker secret files; the token lives in memory only; the CA the
Vault listener uses is pinned. No secret ever appears in a URL, argv or log.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from slas_schemas.errors import ThreePartMessage


class VaultError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class VaultHttp(Protocol):
    """One HTTPS call: method, path, JSON body, headers → (status, JSON body)."""

    def request(
        self, method: str, url: str, body: Mapping[str, Any] | None, headers: Mapping[str, str]
    ) -> tuple[int, dict[str, Any]]: ...


class UrllibVaultHttp:
    def __init__(self, *, cafile: Path | None = None, timeout_s: float = 10.0) -> None:
        self.context = ssl.create_default_context(cafile=str(cafile) if cafile else None)
        self.timeout_s = timeout_s

    def request(
        self, method: str, url: str, body: Mapping[str, Any] | None, headers: Mapping[str, str]
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(  # noqa: S310 — https to the backend network only
            url, data=data, headers={"Content-Type": "application/json", **headers}, method=method
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout_s, context=self.context
            ) as response:
                raw = response.read()
                return int(response.status), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            return exc.code, payload if isinstance(payload, dict) else {}
        except (urllib.error.URLError, OSError) as exc:
            raise VaultError(
                ThreePartMessage(
                    "Vault did not answer.",
                    str(getattr(exc, "reason", exc)),
                    "Check that the vault service is healthy (`slas status`) and unsealed.",
                )
            ) from None


class VaultKv:
    """KV v2 under one mount: read, write, delete, list; AppRole login."""

    def __init__(
        self,
        address: str,
        *,
        mount: str,
        http: VaultHttp,
        token: str | None = None,
    ) -> None:
        self.address = address.rstrip("/")
        self.mount = mount.strip("/")
        self.http = http
        self._token = token

    # --- auth -----------------------------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def login_approle(self, role_id: str, secret_id: str) -> None:
        status, payload = self.http.request(
            "POST",
            f"{self.address}/v1/auth/approle/login",
            {"role_id": role_id, "secret_id": secret_id},
            {},
        )
        token = payload.get("auth", {}).get("client_token") if status == 200 else None
        if not token:
            raise VaultError(
                ThreePartMessage(
                    "Vault refused the AppRole login.",
                    "The role id or secret id in this service's secret files is wrong, "
                    "expired, or the role was deleted.",
                    "Rotate the AppRole secret id (docs/runbooks/prod-profile.md §Vault) and "
                    "restart the service.",
                )
            )
        self._token = str(token)

    def login_from_files(self, role_id_file: Path, secret_id_file: Path) -> None:
        try:
            role_id = role_id_file.read_text(encoding="utf-8").strip()
            secret_id = secret_id_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise VaultError(
                ThreePartMessage(
                    "The AppRole secret files are not readable.",
                    str(exc),
                    "Run ./install.sh --profile prod again; it writes them under "
                    "${SLAS_DATA_ROOT}/secrets.",
                )
            ) from None
        self.login_approle(role_id, secret_id)

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            raise VaultError(
                ThreePartMessage(
                    "This service is not logged in to Vault.",
                    "AppRole login has not happened yet or failed.",
                    "Check the service's start-up log for the login sentence.",
                )
            )
        return {"X-Vault-Token": self._token}

    # --- KV v2 ----------------------------------------------------------------------------

    def read(self, path: str) -> dict[str, str]:
        status, payload = self.http.request(
            "GET", f"{self.address}/v1/{self.mount}/data/{path.strip('/')}", None, self._headers()
        )
        if status == 404:
            raise VaultError(
                ThreePartMessage(
                    f"Vault has no secret at {self.mount}/{path}.",
                    "It was never written, or it was deleted.",
                    "Write it with `vault kv put` under the platform's mount, then try again.",
                )
            )
        if status == 403:
            raise VaultError(
                ThreePartMessage(
                    f"This service may not read {self.mount}/{path}.",
                    "Its Vault policy does not grant the path (config/vault/policies).",
                    "Grant the path in the policy for this service, or move the secret.",
                )
            )
        if status != 200:
            raise VaultError(
                ThreePartMessage(
                    f"Vault answered {status} for {self.mount}/{path}.",
                    "; ".join(str(e) for e in payload.get("errors", [])) or "no detail",
                    "Check the vault service log.",
                )
            )
        data = payload.get("data", {}).get("data", {})
        return {str(k): str(v) for k, v in dict(data).items()}

    def write(self, path: str, data: Mapping[str, str]) -> None:
        status, payload = self.http.request(
            "POST",
            f"{self.address}/v1/{self.mount}/data/{path.strip('/')}",
            {"data": dict(data)},
            self._headers(),
        )
        if status not in (200, 204):
            raise VaultError(
                ThreePartMessage(
                    f"Vault did not accept the write to {self.mount}/{path}.",
                    "; ".join(str(e) for e in payload.get("errors", [])) or f"status {status}",
                    "Check the service's Vault policy grants create and update on the path.",
                )
            )

    def delete(self, path: str) -> None:
        status, _ = self.http.request(
            "DELETE",
            f"{self.address}/v1/{self.mount}/metadata/{path.strip('/')}",
            None,
            self._headers(),
        )
        if status not in (200, 204, 404):
            raise VaultError(
                ThreePartMessage(
                    f"Vault did not delete {self.mount}/{path}.",
                    f"status {status}",
                    "Check the service's Vault policy grants delete on the metadata path.",
                )
            )

    def health(self) -> bool:
        status, _ = self.http.request("GET", f"{self.address}/v1/sys/health", None, {})
        return status == 200
