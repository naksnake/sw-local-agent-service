"""Findings: one sentence each, deduplicated by fingerprint, routed to an owner (§5.4, §10.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from slas_schemas.common import SlasModel

Severity = Literal["S1", "S2", "S3", "S4"]


class Finding(SlasModel):
    id: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{16,64}$")
    issue: str = Field(min_length=1, description="One sentence, e.g. 'PCIe link lost on GPU3'.")
    owner: str | None = None
    severity: Severity | None = None
    component: str | None = None
    evidence: list[str] = Field(default_factory=list)
    ticket_id: str | None = None

    def headline(self) -> str:
        """The `[Issue] … | [Owner] EE` line used for bug tickets (CLAUDE.md §10.2)."""
        return f"[Issue] {self.issue} | [Owner] {self.owner or 'unassigned'}"
