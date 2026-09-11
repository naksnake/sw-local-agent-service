"""authorize(): allowed when the role holds the capability, otherwise a three-part denial."""

from __future__ import annotations

from slas_authz import Allowed, Denied, Principal, authorize, principal_for
from slas_authz.fakes import minimal_roles, roles_from


def test_principal_capabilities_come_from_the_roles_file() -> None:
    roles = minimal_roles()
    admin = principal_for(roles, user_id="u1", email="admin@slas.local", role="administrator")
    viewer = principal_for(roles, user_id="u2", email="v@slas.local", role="viewer")
    assert admin.has("users:manage") and not viewer.has("users:manage")
    assert viewer.capabilities == {"settings:read"}


def test_unknown_role_yields_an_empty_principal() -> None:
    ghost = principal_for(minimal_roles(), user_id="u3", email="g@slas.local", role="ghost")
    assert ghost.capabilities == frozenset()


def test_allowed_decision() -> None:
    roles = minimal_roles()
    admin = principal_for(roles, user_id="u1", email="admin@slas.local", role="administrator")
    decision = authorize(roles, admin, "settings:manage")
    assert isinstance(decision, Allowed) and decision.allowed
    assert decision.capability == "settings:manage"


def test_denied_decision_names_the_role_the_action_and_who_can_grant_it() -> None:
    roles = minimal_roles()
    viewer = principal_for(roles, user_id="u2", email="v@slas.local", role="viewer")
    decision = authorize(roles, viewer, "users:manage")
    assert isinstance(decision, Denied) and not decision.allowed
    err = decision.error
    assert err.what_happened.startswith("Your role (Viewer) does not allow this")
    assert "users:manage" in err.what_happened
    assert "Administrator" in err.what_to_do and "Admin → People" in err.what_to_do


def test_denied_when_no_role_holds_the_capability() -> None:
    roles = roles_from(
        {"administrator": ["users:manage", "settings:manage"]}, extra_capabilities=("fly",)
    )
    admin = principal_for(roles, user_id="u1", email="a@slas.local", role="administrator")
    decision = authorize(roles, admin, "fly")
    assert isinstance(decision, Denied)
    assert "No role currently allows this" in decision.error.what_to_do


def test_principal_is_immutable_and_strict() -> None:
    p = Principal(user_id="u", email="a@b.c", role="viewer", capabilities=frozenset({"x"}))
    assert p.model_dump()["capabilities"] == frozenset({"x"})
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Principal.model_validate({"user_id": "u", "email": "a@b.c", "role": "viewer", "extra": 1})
