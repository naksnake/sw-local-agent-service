"""Votes from the Consensus Router (CLAUDE.md §5.3). The vote schema is fixed.

INV-11: a verdict is input to a human approval or a deterministic gate; it never *is* the
approval and can never trigger a hardware or GUI action by itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from slas_schemas.common import SlasModel

VoteVerdict = Literal["approve", "concern", "reject"]
ConsensusRule = Literal["unanimous", "majority", "majority_per_field"]


class Vote(SlasModel):
    voter: str = Field(min_length=1)
    verdict: VoteVerdict
    fields: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ConsensusVerdict(SlasModel):
    """What the router returns: the votes, the rule applied, and one sentence for the UI."""

    decision: str = Field(min_length=1)
    rule: ConsensusRule
    votes: list[Vote] = Field(default_factory=list)
    agreed: bool
    #: Fewer voters than the rule asks for answered, or the budget forced a single model.
    degraded: bool = False
    sentence: str = Field(min_length=1)
    concerns: list[str] = Field(default_factory=list)
    undecided_fields: list[str] = Field(default_factory=list)
    unavailable_voters: list[str] = Field(default_factory=list)
