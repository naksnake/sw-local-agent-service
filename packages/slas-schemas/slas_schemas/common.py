"""Base model and shared literals for every schema (CLAUDE.md §11: Pydantic v2 at boundaries)."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

#: The three agents plus `null`, which exists only for the kernel's own tests and demos
#: (docs/DEVELOPMENT_PLAN.md P2). The API refuses `null` from Phase 1 onward.
AgentName = Literal["coding", "validation", "factory", "null"]
REAL_AGENT_NAMES: Final[tuple[str, ...]] = ("coding", "validation", "factory")

#: Languages a SOP or report is rendered in (CLAUDE.md §5.5): English always, and one Chinese.
Lang = Literal["en", "zh-Hant", "zh-Hans"]


class SlasModel(BaseModel):
    """Strict by default: unknown fields are errors and assignment validates.

    Strings are not stripped: observations carry stdout and stderr verbatim, newlines included.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
