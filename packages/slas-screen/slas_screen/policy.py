"""Screen driver rules (CLAUDE.md §5.2): rate limit, window deny-list, coordinate policy."""

from __future__ import annotations

import re
from typing import Final

from pydantic import Field

from slas_schemas.common import SlasModel

#: Windows the driver never focuses, clicks or types into: a terminal on the platform host,
#: a password manager. Matched case-insensitively against the title and the WM class.
DEFAULT_DENY: Final[list[str]] = [
    r"\bterminal\b",
    r"\bxterm\b",
    r"\bkonsole\b",
    r"gnome-terminal",
    r"\balacritty\b",
    r"\bkitty\b",
    r"\btilix\b",
    r"\bkeepass",
    r"1password",
    r"\bbitwarden\b",
    r"\blastpass\b",
]


class ScreenPolicy(SlasModel):
    max_actions_per_second: int = Field(default=10, ge=1, le=60)
    deny_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY))
    #: Bare coordinates are allowed only because a recipe author wrote them explicitly.
    allow_coordinates: bool = True
    default_timeout_s: float = Field(default=30.0, gt=0)
    poll_interval_s: float = Field(default=0.5, gt=0)

    def denied(self, title: str, wm_class: str = "") -> str | None:
        haystack = f"{title} {wm_class}"
        for pattern in self.deny_patterns:
            if re.search(pattern, haystack, re.IGNORECASE):
                return pattern
        return None
