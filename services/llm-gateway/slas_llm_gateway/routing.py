"""Role routing: code asks for a role, the router names the vLLM instance (CLAUDE.md §7).

A blue/green swap flips one route with `switch()`; a rollback flips it back.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

Role = Literal["coder", "planner", "triage", "embed", "rerank"]
ROLES: Final[tuple[str, ...]] = ("coder", "planner", "triage", "embed", "rerank")


class Routes(SlasModel):
    """role → instance name, plus the ordered list of voter instances."""

    roles: dict[str, str] = Field(default_factory=dict)
    voters: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_roles_and_unique_voters(self) -> Routes:
        unknown = sorted(set(self.roles) - set(ROLES))
        if unknown:
            raise ValueError(f"unknown roles: {', '.join(unknown)}")
        if len(set(self.voters)) != len(self.voters):
            raise ValueError("voters must be distinct instances")
        return self


class NoInstanceForRoleError(LookupError):
    def __init__(self, role: str, routes: Routes) -> None:
        configured = ", ".join(sorted(routes.roles)) or "none"
        self.message = ThreePartMessage(
            f"No model is serving the {role} role.",
            f"Models/models.yaml assigns no model to it (roles configured: {configured}).",
            "Assign a model to the role on the Models page, or with `slas model swap`.",
        )
        super().__init__(self.message.what_happened)
        self.role = role


class RoleRouter:
    def __init__(self, routes: Routes) -> None:
        self._routes = routes.model_copy(deep=True)
        self.history: list[tuple[str, str | None, str]] = []

    @property
    def routes(self) -> Routes:
        return self._routes.model_copy(deep=True)

    def instance_for(self, role: str) -> str:
        try:
            return self._routes.roles[role]
        except KeyError:
            raise NoInstanceForRoleError(role, self._routes) from None

    def voters(self) -> list[str]:
        return list(self._routes.voters)

    def switch(self, role: str, instance: str) -> str | None:
        """Point `role` at `instance`; returns the previous instance (None if there was none)."""
        if role not in ROLES:
            raise ValueError(f"{role!r} is not a role; roles are {', '.join(ROLES)}")
        previous = self._routes.roles.get(role)
        self._routes.roles[role] = instance
        self.history.append((role, previous, instance))
        return previous
