"""OIDC sign-in beside the built-in accounts (CLAUDE.md §3 prod "OIDC via local Keycloak";
ADR-0012). Authorization-code flow with PKCE as a confidential client: the api redirects the
browser to Keycloak, exchanges the code at the token endpoint over TLS on the backend network
with the client secret from a Docker secret file, then asks the userinfo endpoint who signed
in. Identity comes from that authenticated call, never from parsing a token locally — so no
JWT library is needed and no key material lives in the api.

Roles: Keycloak puts the person's realm roles in the `slas_roles` claim (the realm import
in config/keycloak/slas-realm.json adds the mapper). The first claim value that names a role
in `config/rbac-roles.yaml` wins; no match means the default role. Built-in accounts keep
working: `SLAS_AUTH_MODES=builtin,oidc`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import Field

from slas_authz.roles import RoleSet
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage


class OidcError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class OidcSettings(SlasModel):
    issuer: str = Field(pattern=r"^https://")
    client_id: str = Field(min_length=1)
    redirect_uri: str = Field(pattern=r"^https://")
    role_claim: str = "slas_roles"
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])


class Discovery(SlasModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    end_session_endpoint: str | None = None


class ExternalIdentity(SlasModel):
    subject: str = Field(min_length=1)
    email: str = Field(min_length=3)
    display_name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    claimed_roles: list[str] = Field(default_factory=list)

    def sentence(self) -> str:
        return f"{self.display_name} ({self.email}) signed in through Keycloak as {self.role}."


class HttpClient(Protocol):
    def get_json(self, url: str, headers: Mapping[str, str]) -> tuple[int, dict[str, Any]]: ...

    def post_form(
        self, url: str, form: Mapping[str, str], headers: Mapping[str, str]
    ) -> tuple[int, dict[str, Any]]: ...


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


def pkce_pair() -> Pkce:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return Pkce(verifier, base64.urlsafe_b64encode(digest).decode().rstrip("="))


@dataclass(frozen=True)
class LoginStart:
    url: str
    state: str
    nonce: str
    pkce: Pkce


class OidcClient:
    def __init__(
        self, settings: OidcSettings, *, http: HttpClient, client_secret: str, roles: RoleSet
    ) -> None:
        self.settings = settings
        self.http = http
        self._client_secret = client_secret
        self.roles = roles
        self._discovery: Discovery | None = None

    # --- discovery ----------------------------------------------------------------------

    def discover(self) -> Discovery:
        if self._discovery is not None:
            return self._discovery
        url = f"{self.settings.issuer.rstrip('/')}/.well-known/openid-configuration"
        status, payload = self.http.get_json(url, {})
        if status != 200:
            raise OidcError(
                ThreePartMessage(
                    "Keycloak's OpenID configuration could not be read.",
                    f"{url} answered {status}.",
                    "Check that the keycloak service is healthy and that SLAS_OIDC_ISSUER "
                    "names the slas realm.",
                )
            )
        discovery = Discovery.model_validate(payload)
        if discovery.issuer.rstrip("/") != self.settings.issuer.rstrip("/"):
            raise OidcError(
                ThreePartMessage(
                    "Keycloak names a different issuer than the platform expects.",
                    f"Configured {self.settings.issuer}, Keycloak says {discovery.issuer}.",
                    "Set SLAS_OIDC_ISSUER to the realm URL Keycloak advertises.",
                )
            )
        self._discovery = discovery
        return discovery

    # --- the flow -----------------------------------------------------------------------

    def start_login(self) -> LoginStart:
        discovery = self.discover()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        pkce = pkce_pair()
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
            }
        )
        return LoginStart(f"{discovery.authorization_endpoint}?{query}", state, nonce, pkce)

    def finish_login(
        self, *, code: str, returned_state: str, expected: LoginStart
    ) -> ExternalIdentity:
        if not secrets.compare_digest(returned_state, expected.state):
            raise OidcError(
                ThreePartMessage(
                    "The sign-in did not come back the way it left.",
                    "The state Keycloak returned is not the one this browser started with.",
                    "Start the sign-in again from the sign-in page.",
                )
            )
        discovery = self.discover()
        status, token = self.http.post_form(
            discovery.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
                "client_id": self.settings.client_id,
                "client_secret": self._client_secret,
                "code_verifier": expected.pkce.verifier,
            },
            {},
        )
        access_token = token.get("access_token") if status == 200 else None
        if not access_token:
            raise OidcError(
                ThreePartMessage(
                    "Keycloak did not exchange the sign-in code.",
                    str(token.get("error_description") or token.get("error") or f"status {status}"),
                    "Start the sign-in again; if it repeats, check the client secret under "
                    "${SLAS_DATA_ROOT}/secrets/oidc_client_secret matches Keycloak's.",
                )
            )
        status, claims = self.http.get_json(
            discovery.userinfo_endpoint, {"Authorization": f"Bearer {access_token}"}
        )
        if status != 200:
            raise OidcError(
                ThreePartMessage(
                    "Keycloak did not say who signed in.",
                    f"The userinfo endpoint answered {status}.",
                    "Start the sign-in again; if it repeats, check the keycloak service log.",
                )
            )
        return self.identity_from_claims(claims)

    # --- claims → identity ------------------------------------------------------------

    def identity_from_claims(self, claims: Mapping[str, Any]) -> ExternalIdentity:
        subject = str(claims.get("sub") or "")
        email = str(claims.get("email") or "").strip().lower()
        if not subject or not email:
            raise OidcError(
                ThreePartMessage(
                    "Keycloak's answer names no account.",
                    "The userinfo has no `sub` or no `email`; the client scope lacks `email`.",
                    "Give the slas-webui client the email scope in Keycloak (the realm import "
                    "does this).",
                )
            )
        claimed = _claimed_roles(claims, self.settings.role_claim)
        role = next((r for r in claimed if self.roles.get(r) is not None), self.roles.default_role)
        display = str(claims.get("name") or claims.get("preferred_username") or email)
        return ExternalIdentity(
            subject=subject, email=email, display_name=display, role=role, claimed_roles=claimed
        )


def _claimed_roles(claims: Mapping[str, Any], role_claim: str) -> list[str]:
    raw = claims.get(role_claim)
    if raw is None:
        raw = dict(claims.get("realm_access", {}) or {}).get("roles", [])
    if isinstance(raw, str):
        raw = raw.split()
    return [str(r) for r in raw] if isinstance(raw, list) else []


def auth_modes(value: str) -> list[str]:
    """`SLAS_AUTH_MODES=builtin,oidc` → the modes the sign-in page offers, built-in first."""
    modes = [m.strip() for m in value.split(",") if m.strip()]
    unknown = [m for m in modes if m not in ("builtin", "oidc")]
    if unknown or not modes:
        raise OidcError(
            ThreePartMessage(
                f"SLAS_AUTH_MODES is not usable: {value!r}.",
                "It lists sign-in modes; the platform knows builtin and oidc.",
                "Set SLAS_AUTH_MODES=builtin (quickstart) or builtin,oidc (prod).",
            )
        )
    return sorted(set(modes), key=lambda m: 0 if m == "builtin" else 1)
