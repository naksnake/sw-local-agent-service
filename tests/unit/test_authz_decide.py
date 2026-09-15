"""Allow or deny for every role and every capability, always with a sentence."""

from __future__ import annotations

import pytest

from slas_authz import (
    SYSTEM,
    AccessDeniedError,
    Capability,
    Decision,
    Principal,
    decide,
    default_roles,
    require,
)

ROLES = default_roles()


def person(role_id: str, name: str = "Pat Lin") -> Principal:
    role = ROLES.roles[role_id]
    return Principal.from_role(f"{role_id}@slas.local", name, role)


@pytest.mark.parametrize("role_id", ROLES.ids())
@pytest.mark.parametrize("capability", list(Capability))
def test_decision_matches_the_role_definition(role_id: str, capability: Capability) -> None:
    principal = person(role_id)
    decision = decide(principal, capability)
    assert decision.allowed is (capability in ROLES.roles[role_id].capabilities)
    assert decision.capability is capability
    if decision.allowed:
        assert decision.message is None
        assert decision.sentence().startswith("Pat Lin may ")
    else:
        assert decision.message is not None
        assert decision.sentence() == decision.message.what_happened
        assert decision.message.what_happened.startswith("Pat Lin may not ")
        assert capability.value in decision.message.likely_cause


def test_denial_reads_as_three_sentences() -> None:
    decision = decide(person("engineer"), Capability.ADMIN_PEOPLE)
    assert decision.message is not None
    assert decision.message.what_happened == (
        "Pat Lin may not manage who can sign in and what role they have."
    )
    assert decision.message.likely_cause == "The Engineer role does not include admin:people."
    assert decision.message.what_to_do == "Ask an administrator to make the change for you."


def test_push_protected_denial_points_at_a_merge_request() -> None:
    decision = decide(person("administrator", "Ana"), Capability.GIT_PUSH_PROTECTED)
    assert decision.allowed is False
    assert decision.message is not None
    assert decision.message.what_happened == "Ana may not push directly to a protected branch."
    assert decision.message.what_to_do.startswith("Push to a branch and open a merge request")


def test_generic_advice_for_other_capabilities() -> None:
    decision = decide(person("viewer"), Capability.SSH)
    assert decision.message is not None
    assert decision.message.what_to_do == (
        "Ask an administrator to give you a role that includes it."
    )


def test_require_raises_denied_carrying_the_decision() -> None:
    require(person("administrator"), Capability.ADMIN_SETTINGS)  # no exception
    with pytest.raises(AccessDeniedError) as raised:
        require(person("viewer"), Capability.ADMIN_SETTINGS)
    assert raised.value.decision.allowed is False
    assert str(raised.value) == "Pat Lin may not change platform settings."


def test_access_denied_error_refuses_an_allowed_decision() -> None:
    allowed = Decision(True, Capability.SSH, person("engineer"))
    with pytest.raises(ValueError, match="needs a denied decision"):
        AccessDeniedError(allowed)


def test_system_principal_may_do_everything() -> None:
    for capability in Capability:
        assert decide(SYSTEM, capability).allowed
    assert SYSTEM.is_system and SYSTEM.role == "system"


def test_principal_is_a_snapshot_of_exactly_the_roles_capabilities() -> None:
    principal = person("line_lead")
    assert principal.capabilities == ROLES.roles["line_lead"].capabilities
    assert principal.role_label == "Line lead"
    assert principal.is_system is False
    with pytest.raises(AttributeError):
        principal.capabilities = frozenset(Capability)  # type: ignore[misc]


def test_a_principal_never_gains_what_its_role_lacks() -> None:
    engineer = person("engineer")
    assert not engineer.can(Capability.GIT_HOSTS_MANAGE)
    assert not engineer.can(Capability.MODEL_MANAGE)
    assert engineer.can(Capability.GIT_TERMINAL)
