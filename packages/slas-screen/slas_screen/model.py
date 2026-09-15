"""What the screen driver sees and reports (CLAUDE.md §5.2)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage


class Window(SlasModel):
    id: str = Field(min_length=1)
    title: str = ""
    wm_class: str = ""


class Point(SlasModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ScreenResult(SlasModel):
    """Every GUI step: a sentence, and the screenshots taken before and after."""

    ok: bool
    sentence: str = Field(min_length=1)
    before: str | None = None
    after: str | None = None
    matched: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)


class ScreenStopError(RuntimeError):
    """The driver stopped hard: a denied window, or the focus changed unexpectedly."""

    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message
