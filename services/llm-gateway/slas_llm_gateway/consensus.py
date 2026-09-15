"""The Consensus Router's rules, tally and token budget (CLAUDE.md §5.3).

| Decision | Voters | Rule | If not met |
|---|---|---|---|
| plan_approval | 3 | unanimous | human decides; concerns shown |
| code_change | 3 | majority; every concern surfaced | user addresses or dismisses with a note |
| ticket_diagnosis | 3 | majority per field | field marked "your call" |
| factory_pass | 3 | unanimous for PASS | line lead decides |
| rca_conclusion | 3 | majority | flagged "uncertain" in the report |

Votes are shown as sentences ("3 of 3 agree the owner is EE. Severity: 2 say S2, 1 says
S1. Your call."). INV-11: a verdict is input to a human approval or a deterministic gate; it
never is the approval. Budget: cross-checks use at most `token_budget_pct` of the daily
tokens; past that the router degrades to one model and flags it, never blocking work.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from typing import Final, Protocol

from pydantic import Field, ValidationError, model_validator

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_schemas.vote import ConsensusRule, ConsensusVerdict, Vote


class Clock(Protocol):
    def now(self) -> datetime: ...


class DecisionRule(SlasModel):
    decision: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    voters: int = Field(ge=1, le=9)
    rule: ConsensusRule
    #: What an "approve" vote means, completing "<k> of <n> …".
    approve_phrase: str = Field(min_length=1)
    #: Fields tallied for majority_per_field, e.g. owner, severity, root_cause.
    fields: list[str] = Field(default_factory=list)
    if_not_met: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fields_match_rule(self) -> DecisionRule:
        if self.rule == "majority_per_field" and not self.fields:
            raise ValueError(f"{self.decision}: majority_per_field needs at least one field")
        if self.rule != "majority_per_field" and self.fields:
            raise ValueError(f"{self.decision}: fields are only used by majority_per_field")
        return self


class ConsensusRules(SlasModel):
    version: int = 1
    default_voters: int = Field(default=3, ge=1)
    token_budget_pct: float = Field(default=5.0, gt=0, le=100)
    decisions: dict[str, DecisionRule]

    @model_validator(mode="after")
    def _keys_match(self) -> ConsensusRules:
        for key, rule in self.decisions.items():
            if key != rule.decision:
                raise ValueError(
                    f"decision key {key!r} does not match rule.decision {rule.decision!r}"
                )
        return self

    def rule(self, decision: str) -> DecisionRule:
        try:
            return self.decisions[decision]
        except KeyError:
            raise UnknownDecisionError(decision, sorted(self.decisions)) from None


class UnknownDecisionError(LookupError):
    def __init__(self, decision: str, known: list[str]) -> None:
        self.message = ThreePartMessage(
            f"There is no cross-check rule called {decision}.",
            f"config/consensus.yaml defines: {', '.join(known)}.",
            "Use one of those names, or add a rule and an ADR.",
        )
        super().__init__(self.message.what_happened)


class ConsensusConfigError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def rules_from_mapping(data: object, *, source: str = "<memory>") -> ConsensusRules:
    try:
        return ConsensusRules.model_validate(data)
    except ValidationError as exc:
        raise ConsensusConfigError(
            ThreePartMessage(
                f"The cross-check rules in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; the format is documented in config/README.md.",
            )
        ) from exc


DEFAULT_CONSENSUS: Final[dict[str, object]] = {
    "version": 1,
    "default_voters": 3,
    "token_budget_pct": 5.0,
    "decisions": {
        "plan_approval": {
            "decision": "plan_approval",
            "description": "A Validation or Factory plan before anything runs.",
            "voters": 3,
            "rule": "unanimous",
            "approve_phrase": "approve the plan",
            "if_not_met": "You decide; the concerns are shown above.",
        },
        "code_change": {
            "decision": "code_change",
            "description": "The Coding Agent's final diff.",
            "voters": 3,
            "rule": "majority",
            "approve_phrase": "approve the change",
            "if_not_met": "Address each concern or dismiss it with a note.",
        },
        "ticket_diagnosis": {
            "decision": "ticket_diagnosis",
            "description": "Owner, severity and root cause of a finding.",
            "voters": 3,
            "rule": "majority_per_field",
            "approve_phrase": "agree",
            "fields": ["owner", "severity", "root_cause"],
            "if_not_met": "Your call.",
        },
        "factory_pass": {
            "decision": "factory_pass",
            "description": "A unit's PASS verdict on the line.",
            "voters": 3,
            "rule": "unanimous",
            "approve_phrase": "say PASS",
            "if_not_met": "The line lead decides.",
        },
        "rca_conclusion": {
            "decision": "rca_conclusion",
            "description": "The root-cause conclusion in a report.",
            "voters": 3,
            "rule": "majority",
            "approve_phrase": "agree with the conclusion",
            "if_not_met": "The conclusion is marked uncertain in the report.",
        },
    },
}

CONSENSUS_FILE_HEADER: Final = (
    "Consensus Router rules for SW Local Agent Service (CLAUDE.md §5.3).\n"
    "Rendered from slas_llm_gateway.consensus.DEFAULT_CONSENSUS; a unit test keeps file and\n"
    "code in step. INV-11: a verdict never authorises anything by itself. Cross-checks may use\n"
    "at most token_budget_pct of the daily tokens; beyond that the router degrades to one\n"
    "model and flags it."
)


def default_rules() -> ConsensusRules:
    return rules_from_mapping(DEFAULT_CONSENSUS, source="config/consensus.yaml")


def render_consensus_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    rules = rules_from_mapping(data)
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {rules.version}")
    lines.append(f"default_voters: {rules.default_voters}")
    lines.append(f"token_budget_pct: {rules.token_budget_pct}")
    lines.append("decisions:")
    for rule in rules.decisions.values():
        lines.append(f"  {rule.decision}:")
        lines.append(f"    decision: {rule.decision}")
        lines.append(f"    description: {json.dumps(rule.description, ensure_ascii=False)}")
        lines.append(f"    voters: {rule.voters}")
        lines.append(f"    rule: {rule.rule}")
        lines.append(f"    approve_phrase: {json.dumps(rule.approve_phrase, ensure_ascii=False)}")
        if rule.fields:
            lines.append("    fields:")
            lines.extend(f"      - {name}" for name in rule.fields)
        lines.append(f"    if_not_met: {json.dumps(rule.if_not_met, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


# --- tally ------------------------------------------------------------------------------


def _label(field_name: str) -> str:
    return field_name.replace("_", " ")


def _say(count: int) -> str:
    return "says" if count == 1 else "say"


def _dissent(vote: Vote) -> str:
    verb = "has a concern" if vote.verdict == "concern" else "rejects it"
    return f"{vote.voter} {verb}: {vote.reason}"


def tally(
    rule: DecisionRule,
    votes: list[Vote],
    *,
    unavailable: list[str] | None = None,
    degraded: bool = False,
) -> ConsensusVerdict:
    """Apply the rule to the votes and say the result in one or two sentences."""
    unavailable = list(unavailable or [])
    n = len(votes)
    panel = rule.voters
    degraded = degraded or n < panel
    parts: list[str] = []
    concerns = [_dissent(vote) for vote in votes if vote.verdict != "approve"]
    undecided: list[str] = []

    if rule.rule == "majority_per_field":
        agreed = n > 0
        for field_name in rule.fields:
            values = Counter(vote.fields[field_name] for vote in votes if field_name in vote.fields)
            top_value, top_count = values.most_common(1)[0] if values else ("", 0)
            if top_count * 2 > panel:
                parts.append(f"{top_count} of {n} agree the {_label(field_name)} is {top_value}.")
            else:
                agreed = False
                undecided.append(field_name)
                spread = ", ".join(
                    f"{count} {_say(count)} {value}" for value, count in values.most_common()
                )
                spread = spread or "nobody answered"
                parts.append(f"{_label(field_name).capitalize()}: {spread}. {rule.if_not_met}")
    else:
        approvals = sum(1 for vote in votes if vote.verdict == "approve")
        agreed = approvals == panel if rule.rule == "unanimous" else approvals * 2 > panel
        parts.append(f"{approvals} of {n} {rule.approve_phrase}.")
        if concerns:
            parts.append("; ".join(concerns) + ".")
        if not agreed:
            parts.append(rule.if_not_met)

    if n == 0:
        agreed = False
    for voter in unavailable:
        parts.append(f"{voter} didn't answer and is paused.")
    if degraded and n and n < panel:
        parts.append(f"Only {n} of {panel} voters answered; treat this as a weaker check.")

    return ConsensusVerdict(
        decision=rule.decision,
        rule=rule.rule,
        votes=votes,
        agreed=agreed,
        degraded=degraded,
        sentence=" ".join(parts),
        concerns=concerns,
        undecided_fields=undecided,
        unavailable_voters=unavailable,
    )


# --- budget -----------------------------------------------------------------------------


class TokenBudget:
    """Cross-check spend per UTC day, capped at a percentage of the daily token allowance."""

    def __init__(self, clock: Clock, *, daily_tokens: int, pct: float = 5.0) -> None:
        if daily_tokens < 1:
            raise ValueError("daily_tokens must be positive")
        self._clock = clock
        self.daily_tokens = daily_tokens
        self.pct = pct
        self._day: date | None = None
        self._spent = 0

    @property
    def cap(self) -> int:
        return int(self.daily_tokens * self.pct / 100)

    def _roll(self) -> None:
        today = self._clock.now().date()
        if today != self._day:
            self._day = today
            self._spent = 0

    def spent_today(self) -> int:
        self._roll()
        return self._spent

    def remaining(self) -> int:
        return max(0, self.cap - self.spent_today())

    def can_afford(self, estimate: int) -> bool:
        return self.remaining() >= estimate

    def charge(self, tokens: int) -> None:
        self._roll()
        self._spent += max(0, tokens)

    def sentence(self) -> str:
        return (
            f"Cross-checks have used {self.spent_today():,} of {self.cap:,} tokens today "
            f"({self.pct:g}% of the daily allowance)."
        )
