"""The error shape every component returns and the UI renders (CLAUDE.md §9, §11)."""

from pydantic import BaseModel, ConfigDict, Field


class ThreePartError(BaseModel):
    """What happened, its likely cause, and what to do: three sentences, nothing else.

    `slas_cli.report.ThreePartError` is a standard-library mirror of this model for the
    preflight that must run before anything is installed; `tests/unit/test_errors_contract.py`
    keeps the two in step.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    what_happened: str = Field(min_length=1)
    likely_cause: str = Field(min_length=1)
    what_to_do: str = Field(min_length=1)
