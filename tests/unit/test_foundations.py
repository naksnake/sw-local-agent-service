"""Branding constants and the three-part message that every error carries."""

from __future__ import annotations

import pytest

from slas_kernel import branding
from slas_schemas import ThreePartMessage


def test_branding_matches_claude_md_section_0_1() -> None:
    assert branding.PRODUCT_NAME == "SW Local Agent Service"
    assert branding.SLUG == "slas"
    assert branding.SLUG.islower()
    assert branding.DEFAULT_DATA_ROOT == "/AI/Agent"
    assert branding.AGENT_NAMES == ("coding", "validation", "factory")


def test_three_part_message_renders_three_lines() -> None:
    message = ThreePartMessage("The disk is full.", "Logs grew.", "Delete old runs.")
    assert message.render() == (
        "The disk is full.\nLikely cause: Logs grew.\nWhat to do: Delete old runs."
    )
    assert message.render(indent="  ").splitlines()[1] == "  Likely cause: Logs grew."
    assert message.as_dict() == {
        "what_happened": "The disk is full.",
        "likely_cause": "Logs grew.",
        "what_to_do": "Delete old runs.",
    }


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_three_part_message_rejects_blank_parts(blank: str) -> None:
    with pytest.raises(ValueError, match="what_to_do must be a sentence"):
        ThreePartMessage("Something.", "Because.", blank)
    with pytest.raises(ValueError, match="what_happened"):
        ThreePartMessage(blank, "Because.", "Do this.")


def test_three_part_message_is_immutable() -> None:
    message = ThreePartMessage("A.", "B.", "C.")
    with pytest.raises(AttributeError):
        message.what_to_do = "D."  # type: ignore[misc]
