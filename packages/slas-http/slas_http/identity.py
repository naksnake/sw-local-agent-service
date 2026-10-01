"""Who is acting, carried between services as headers (docs/api-contract-round-2.md §identity).

The api resolves the session and forwards the person as `X-Slas-User` (the email),
`X-Slas-Display-Name` and `X-Slas-Capabilities` (comma-separated). Every other service reads
them and runs `require()` where the action executes (CLAUDE.md §11 "authz at executor"). The
headers cross only the internal backend network; no token, password or session id travels.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from fastapi import Request

from slas_http.errors import ServiceError
from slas_schemas.errors import ThreePartMessage

USER_HEADER: Final = "X-Slas-User"
DISPLAY_NAME_HEADER: Final = "X-Slas-Display-Name"
CAPABILITIES_HEADER: Final = "X-Slas-Capabilities"

#: The identity a service uses for its own work (a reconcile loop, a poller).
SYSTEM_USER: Final = "system@slas.local"


@dataclass(frozen=True)
class Identity:
    user: str
    display_name: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def name(self) -> str:
        return self.display_name or self.user

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def headers(self) -> dict[str, str]:
        out = {USER_HEADER: self.user}
        if self.display_name:
            out[DISPLAY_NAME_HEADER] = self.display_name
        if self.capabilities:
            out[CAPABILITIES_HEADER] = ",".join(sorted(self.capabilities))
        return out

    @classmethod
    def system(cls) -> Identity:
        return cls(SYSTEM_USER, "SW Local Agent Service")

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Identity | None:
        lowered = {key.lower(): value for key, value in headers.items()}
        user = lowered.get(USER_HEADER.lower(), "").strip()
        if not user:
            return None
        raw = lowered.get(CAPABILITIES_HEADER.lower(), "")
        capabilities = frozenset(part.strip() for part in raw.split(",") if part.strip())
        return cls(user, lowered.get(DISPLAY_NAME_HEADER.lower(), "").strip(), capabilities)


NO_IDENTITY: Final = ThreePartMessage(
    "The request did not say who is acting.",
    "It reached the service without the identity headers the api adds.",
    "Use the platform's own page; if you are writing a script, go through the api.",
)


def identity_of(request: Request) -> Identity:
    """FastAPI dependency: the acting person, or a three-part 401 when the headers are absent."""
    identity = Identity.from_headers(dict(request.headers))
    if identity is None:
        raise ServiceError(401, NO_IDENTITY)
    return identity


def require(identity: Identity, capability: str, *, verb: str | None = None) -> None:
    """Refuse in three parts when the person lacks the capability."""
    if identity.has(capability):
        return
    doing = verb or f"do this ({capability})"
    raise ServiceError(
        403,
        ThreePartMessage(
            f"{identity.name} may not {doing}.",
            f"The role does not include the capability {capability}.",
            "Ask an administrator to change the role under Admin → People.",
        ),
    )


def require_any(identity: Identity, capabilities: Iterable[str], *, verb: str) -> None:
    wanted = list(capabilities)
    if any(identity.has(capability) for capability in wanted):
        return
    raise ServiceError(
        403,
        ThreePartMessage(
            f"{identity.name} may not {verb}.",
            f"The role includes none of {', '.join(wanted)}.",
            "Ask an administrator to change the role under Admin → People.",
        ),
    )
