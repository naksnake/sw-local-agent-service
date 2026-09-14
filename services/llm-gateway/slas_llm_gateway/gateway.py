"""The gateway: redact → route → (constrain) → complete, and the Consensus Router fan-out.

Every voter sees the same evidence and none of the other votes (§5.3). Cross-checks are
budgeted; past the budget the router degrades to one model, flags it, alerts, and never
blocks. INV-11: what comes back is a verdict object, nothing more.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.consensus import ConsensusRules, TokenBudget, tally
from slas_llm_gateway.redaction import Redaction, Redactor
from slas_llm_gateway.routing import RoleRouter
from slas_llm_gateway.structured import (
    SchemaViolationError,
    StructuredResult,
    VoterUnavailableError,
    generate_structured,
)
from slas_llm_gateway.vllm import (
    CompletionRequest,
    CompletionResponse,
    InstanceUnavailableError,
    Message,
    VllmClient,
)
from slas_schemas.vote import ConsensusVerdict, Vote

T = TypeVar("T", bound=BaseModel)

VOTER_INSTRUCTION = (
    "You are one of several independent reviewers. Judge only the evidence given. "
    "Answer with a single JSON object: verdict (approve, concern or reject), fields "
    "(the values you were asked to decide, if any), reason (one sentence) and confidence "
    "(0 to 1). You do not see the other reviewers' answers."
)

#: Rough per-voter cost used to check the budget before fanning out.
VOTE_TOKEN_ESTIMATE = 800


class Gateway:
    def __init__(
        self,
        *,
        client: VllmClient,
        router: RoleRouter,
        redactor: Redactor,
        breaker: CircuitBreaker,
        rules: ConsensusRules,
        budget: TokenBudget,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.router = router
        self.redactor = redactor
        self.breaker = breaker
        self.rules = rules
        self.budget = budget
        self._alert = alert or (lambda _sentence: None)
        self.redactions: list[Redaction] = []

    # --- plain and structured completion ----------------------------------------------

    def _redact(self, messages: list[Message]) -> list[Message]:
        cleaned: list[Message] = []
        for message in messages:
            redaction = self.redactor.redact(message.content)
            if redaction.redacted:
                self.redactions.append(redaction)
            cleaned.append(message.model_copy(update={"content": redaction.text}))
        return cleaned

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResponse:
        instance = self.router.instance_for(role)
        if not self.breaker.allow(instance):
            raise VoterUnavailableError(instance, self.breaker.sentence(instance))
        request = CompletionRequest(
            instance=instance,
            messages=self._redact(messages),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        try:
            response = self.client.complete(request)
        except InstanceUnavailableError as exc:
            self.breaker.record_failure(instance, "did not answer")
            raise VoterUnavailableError(instance, str(exc)) from exc
        self.breaker.record_success(instance)
        return response

    def generate(
        self,
        role: str,
        messages: list[Message],
        model_type: type[T],
        *,
        max_retries: int = 2,
    ) -> StructuredResult[T]:
        instance = self.router.instance_for(role)
        request = CompletionRequest(instance=instance, messages=self._redact(messages))
        return generate_structured(
            self.client, request, model_type, breaker=self.breaker, max_retries=max_retries
        )

    # --- Consensus Router -------------------------------------------------------------

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict:
        rule = self.rules.rule(decision)
        cleaned = [Message(role="system", content=VOTER_INSTRUCTION), *self._redact(evidence)]
        panel = self.router.voters()[: rule.voters]
        if not panel:
            return tally(rule, [], unavailable=[], degraded=True)

        over_budget = not self.budget.can_afford(VOTE_TOKEN_ESTIMATE * len(panel))
        if over_budget:
            self._alert(
                f"Cross-check budget for today is used up; {decision} was checked by one "
                f"model only. {self.budget.sentence()}"
            )
            panel = panel[:1]

        votes: list[Vote] = []
        unavailable: list[str] = []
        for voter in panel:
            request = CompletionRequest(instance=voter, messages=cleaned)
            try:
                result = generate_structured(self.client, request, Vote, breaker=self.breaker)
            except (SchemaViolationError, VoterUnavailableError):
                unavailable.append(voter)
                continue
            self.budget.charge(result.tokens)
            votes.append(result.value.model_copy(update={"voter": voter}))

        verdict = tally(rule, votes, unavailable=unavailable, degraded=over_budget)
        if over_budget:
            verdict = verdict.model_copy(
                update={
                    "sentence": (
                        "Checked by one model only: today's cross-check budget is used up. "
                        + verdict.sentence
                    )
                }
            )
        return verdict
