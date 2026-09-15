"""Screen driver rules (CLAUDE.md §5.2): rate limit, window deny-list, coordinate policy."""

from __future__ import annotations

import re
from typing import Final, Literal

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


WindowMatch = Literal["contains", "exact", "prefix", "regex"]


class ScreenPolicy(SlasModel):
    max_actions_per_second: int = Field(default=10, ge=1, le=60)
    deny_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY))
    #: Bare coordinates are allowed only because a recipe author wrote them explicitly.
    allow_coordinates: bool = True
    default_timeout_s: float = Field(default=30.0, gt=0)
    poll_interval_s: float = Field(default=0.5, gt=0)
    #: How a recipe's window title is matched against real titles (tuned per station, P10).
    window_match: WindowMatch = "contains"
    #: Seconds to wait after every action, for stations whose GUI lags behind the input.
    action_settle_s: float = Field(default=0.0, ge=0, le=10)
    #: Multiplies every `wait_for` timeout, for slow stations, without editing the skill.
    wait_timeout_scale: float = Field(default=1.0, gt=0, le=10)

    def title_matches(self, wanted: str, title: str) -> bool:
        if self.window_match == "exact":
            return wanted.lower() == title.lower()
        if self.window_match == "prefix":
            return title.lower().startswith(wanted.lower())
        if self.window_match == "regex":
            try:
                return re.search(wanted, title, re.IGNORECASE) is not None
            except re.error:
                return False
        return wanted.lower() in title.lower()

    def denied(self, title: str, wm_class: str = "") -> str | None:
        haystack = f"{title} {wm_class}"
        for pattern in self.deny_patterns:
            if re.search(pattern, haystack, re.IGNORECASE):
                return pattern
        return None
