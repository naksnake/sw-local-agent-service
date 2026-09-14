"""The structured SOP: one source, two renderings (CLAUDE.md §5.5, INV-13)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from slas_schemas.common import AgentName, SlasModel


class SopStep(SlasModel):
    n: int = Field(ge=1)
    action: str = Field(min_length=1)
    expected: str = ""
    evidence: list[str] = Field(default_factory=list)


class SopModel(SlasModel):
    title: str = Field(min_length=1)
    purpose: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[SopStep] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    results: dict[str, str] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    glossary_refs: list[str] = Field(default_factory=list)


class SopTemplate(SlasModel):
    """Agent-specific shape; the kernel renders it (CLAUDE.md §5.1 `sop_template`)."""

    agent: AgentName
    kind: Literal["code_walkthrough", "verification", "production_line", "placeholder"]
    purpose: str = Field(min_length=1)
    checks: list[str] = Field(default_factory=list)
