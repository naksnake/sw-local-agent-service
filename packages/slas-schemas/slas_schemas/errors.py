"""Three-part messages: what happened, the likely cause, what to do (CLAUDE.md §11).

Every user-facing error and every preflight finding carries exactly these three
sentences. The WebUI and the CLI render them as written — never a code, an enum or a
stack trace as the primary content (CLAUDE.md §9).

Phase 0 keeps this a plain dataclass because there is no API boundary yet. It becomes a
Pydantic model with the same field names when `apps/api` appears in Phase 1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ThreePartMessage:
    """One problem, explained in three sentences."""

    what_happened: str
    likely_cause: str
    what_to_do: str

    def __post_init__(self) -> None:
        for name in ("what_happened", "likely_cause", "what_to_do"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be a sentence, not empty")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    def render(self, indent: str = "") -> str:
        """Three lines, ready for a terminal or a log."""
        return "\n".join(
            (
                f"{indent}{self.what_happened}",
                f"{indent}Likely cause: {self.likely_cause}",
                f"{indent}What to do: {self.what_to_do}",
            )
        )
