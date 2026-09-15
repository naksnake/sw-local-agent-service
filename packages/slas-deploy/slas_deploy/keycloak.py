"""The Keycloak realm the prod profile imports at start (`--import-realm`), rendered to
`config/keycloak/slas-realm.json` (ADR-0012). One realm, one confidential client for the
WebUI with PKCE required, the four platform roles as realm roles, and a mapper that puts a
person's realm roles into the `slas_roles` claim the api reads (slas_authz.oidc). Brute-force
protection on; every URL relative to the edge (`/auth`); no external identity providers.
"""

from __future__ import annotations

from typing import Any, Final

from slas_authz.roles import DEFAULT_ROLES

CLIENT_ID: Final = "slas-webui"
ROLE_CLAIM: Final = "slas_roles"


def realm_export() -> dict[str, Any]:
    roles_data = DEFAULT_ROLES["roles"]
    assert isinstance(roles_data, dict)  # noqa: S101 — the shape is fixed by slas_authz
    realm_roles = [
        {
            "name": role_id,
            "description": str(definition["description"]),
            "composite": False,
            "clientRole": False,
        }
        for role_id, definition in roles_data.items()
    ]
    return {
        "realm": "slas",
        "displayName": "SW Local Agent Service",
        "enabled": True,
        "sslRequired": "external",
        "registrationAllowed": False,
        "resetPasswordAllowed": True,
        "rememberMe": False,
        "bruteForceProtected": True,
        "permanentLockout": False,
        "failureFactor": 10,
        "waitIncrementSeconds": 60,
        "maxFailureWaitSeconds": 900,
        "minimumQuickLoginWaitSeconds": 60,
        "ssoSessionIdleTimeout": 28800,
        "ssoSessionMaxLifespan": 36000,
        "accessTokenLifespan": 300,
        "passwordPolicy": "length(12) and notUsername",
        "defaultRoles": [str(DEFAULT_ROLES["default_role"])],
        "roles": {"realm": realm_roles},
        "clients": [
            {
                "clientId": CLIENT_ID,
                "name": "SW Local Agent Service WebUI",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "rootUrl": "https://${SLAS_PUBLIC_HOST}",
                "redirectUris": ["https://${SLAS_PUBLIC_HOST}/auth/callback"],
                "webOrigins": ["https://${SLAS_PUBLIC_HOST}"],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "post.logout.redirect.uris": "https://${SLAS_PUBLIC_HOST}/",
                },
                "defaultClientScopes": ["openid", "profile", "email", "roles"],
                "protocolMappers": [
                    {
                        "name": ROLE_CLAIM,
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-realm-role-mapper",
                        "consentRequired": False,
                        "config": {
                            "claim.name": ROLE_CLAIM,
                            "jsonType.label": "String",
                            "multivalued": "true",
                            "userinfo.token.claim": "true",
                            "id.token.claim": "true",
                            "access.token.claim": "false",
                        },
                    }
                ],
            }
        ],
        "identityProviders": [],
        "eventsEnabled": True,
        "eventsExpiration": 2592000,
        "adminEventsEnabled": True,
        "adminEventsDetailsEnabled": False,
        "attributes": {"frontendUrl": "https://${SLAS_PUBLIC_HOST}/auth"},
    }
