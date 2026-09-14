"""Allow or deny, with a sentence either way (CLAUDE.md §11: authz where the action executes).

`decide()` is pure: it looks only at the principal snapshot and the capability. The api maps
`AccessDeniedError` to a 403 whose body is exactly the three-part message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from slas_authz.capabilities import DESCRIPTIONS, Capability
from slas_authz.principal import Principal
from slas_schemas.errors import ThreePartMessage

_ADVICE: Final[dict[Capability, str]] = {
    Capability.GIT_PUSH_PROTECTED: "Push to a branch and open a merge request instead, or ask "
    "an administrator to grant it.",
    Capability.ADMIN_PEOPLE: "Ask an administrator to make the change for you.",
    Capability.ADMIN_SETTINGS: "Ask an administrator to make the change for you.",
}
_DEFAULT_ADVICE: Final = "Ask an administrator to give you a role that includes it."


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    capability: Capability
    principal: Principal
    message: ThreePartMessage | None = None

    def sentence(self) -> str:
        if self.message is None:
            return f"{self.principal.display_name} may {DESCRIPTIONS[self.capability]}."
        return self.message.what_happened


class AccessDeniedError(PermissionError):
    """Raised by `require()`; carries the decision so the caller can render it."""

    def __init__(self, decision: Decision) -> None:
        if decision.allowed or decision.message is None:
            raise ValueError("AccessDeniedError needs a denied decision")
        super().__init__(decision.message.what_happened)
        self.decision = decision


def decide(principal: Principal, capability: Capability) -> Decision:
    if principal.can(capability):
        return Decision(True, capability, principal)
    message = ThreePartMessage(
        f"{principal.display_name} may not {DESCRIPTIONS[capability]}.",
        f"The {principal.role_label} role does not include {capability.value}.",
        _ADVICE.get(capability, _DEFAULT_ADVICE),
    )
    return Decision(False, capability, principal, message)


def require(principal: Principal, capability: Capability) -> None:
    decision = decide(principal, capability)
    if not decision.allowed:
        raise AccessDeniedError(decision)
