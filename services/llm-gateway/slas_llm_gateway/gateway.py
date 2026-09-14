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
from slas_observability import metrics
from slas_observability.alerts import AlertChannel, Severity
from slas_observability.tracing import current_trace_id
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
        channel: AlertChannel | None = None,
    ) -> None:
        self.client = client
        self.router = router
        self.redactor = redactor
        self.breaker = breaker
        self.rules = rules
        self.budget = budget
        self._alert = alert or (lambda _sentence: None)
        #: The local alert channel (P11): breaker trips, disagreements, degraded checks.
        self.channel = channel
        if breaker.on_trip is None:
            breaker.on_trip = self._breaker_tripped
        self.redactions: list[Redaction] = []
        metrics.set_gauge("slas_consensus_budget_remaining_tokens", budget.remaining())

    def _raise(self, name: str, severity: Severity, sentence: str, **labels: str) -> None:
        if self.channel is not None:
            self.channel.raise_(name, severity, sentence, **labels)

    def _breaker_tripped(self, instance: str, sentence: str) -> None:
        self._raise("circuit_breaker_open", "warning", sentence, instance=instance)

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
            metrics.inc("slas_gateway_requests_total", role=role, outcome="breaker_open")
            raise VoterUnavailableError(instance, self.breaker.sentence(instance))
        request = CompletionRequest(
            instance=instance,
            messages=self._redact(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            trace_id=current_trace_id(),
        )
        try:
            response = self.client.complete(request)
        except InstanceUnavailableError as exc:
            self.breaker.record_failure(instance, "did not answer")
            metrics.inc("slas_gateway_requests_total", role=role, outcome="unavailable")
            raise VoterUnavailableError(instance, str(exc)) from exc
        self.breaker.record_success(instance)
        self._count_tokens(role, response)
        metrics.inc("slas_gateway_requests_total", role=role, outcome="ok")
        return response

    @staticmethod
    def _count_tokens(role: str, response: CompletionResponse) -> None:
        metrics.inc("slas_gateway_tokens_total", response.prompt_tokens, role=role, kind="prompt")
        metrics.inc(
            "slas_gateway_tokens_total", response.completion_tokens, role=role, kind="completion"
        )

    def generate(
        self,
        role: str,
        messages: list[Message],
        model_type: type[T],
        *,
        max_retries: int = 2,
    ) -> StructuredResult[T]:
        instance = self.router.instance_for(role)
        request = CompletionRequest(
            instance=instance, messages=self._redact(messages), trace_id=current_trace_id()
        )
        try:
            result = generate_structured(
                self.client, request, model_type, breaker=self.breaker, max_retries=max_retries
            )
        except SchemaViolationError:
            metrics.inc("slas_gateway_requests_total", role=role, outcome="schema_violation")
            raise
        except VoterUnavailableError:
            metrics.inc("slas_gateway_requests_total", role=role, outcome="unavailable")
            raise
        metrics.inc("slas_gateway_requests_total", role=role, outcome="ok")
        metrics.inc("slas_gateway_tokens_total", result.tokens, role=role, kind="completion")
        return result

    # --- Consensus Router -------------------------------------------------------------

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict:
        rule = self.rules.rule(decision)
        cleaned = [Message(role="system", content=VOTER_INSTRUCTION), *self._redact(evidence)]
        panel = self.router.voters()[: rule.voters]
        if not panel:
            return tally(rule, [], unavailable=[], degraded=True)

        over_budget = not self.budget.can_afford(VOTE_TOKEN_ESTIMATE * len(panel))
        if over_budget:
            sentence = (
                f"Cross-check budget for today is used up; {decision} was checked by one "
                f"model only. {self.budget.sentence()}"
            )
            self._alert(sentence)
            self._raise("consensus_budget_exhausted", "warning", sentence, decision=decision)
            panel = panel[:1]

        votes: list[Vote] = []
        unavailable: list[str] = []
        for voter in panel:
            request = CompletionRequest(
                instance=voter, messages=cleaned, trace_id=current_trace_id()
            )
            try:
                result = generate_structured(self.client, request, Vote, breaker=self.breaker)
            except (SchemaViolationError, VoterUnavailableError):
                unavailable.append(voter)
                metrics.inc("slas_gateway_requests_total", role="voter", outcome="unavailable")
                continue
            self.budget.charge(result.tokens)
            metrics.inc("slas_gateway_requests_total", role="voter", outcome="ok")
            metrics.inc("slas_gateway_tokens_total", result.tokens, role="voter", kind="completion")
            vote = result.value.model_copy(update={"voter": voter})
            votes.append(vote)
            metrics.inc("slas_consensus_votes_total", decision=decision, verdict=vote.verdict)
        metrics.set_gauge("slas_consensus_budget_remaining_tokens", self.budget.remaining())

        verdict = tally(rule, votes, unavailable=unavailable, degraded=over_budget)
        if not verdict.agreed:
            metrics.inc("slas_consensus_disagreements_total", decision=decision)
            self._raise(
                "consensus_disagreement",
                "warning",
                f"The voters did not agree on {decision}; a person decides. {verdict.sentence}",
                decision=decision,
            )
        if verdict.degraded or unavailable:
            metrics.inc("slas_consensus_degraded_total", decision=decision)
            if unavailable:
                self._raise(
                    "consensus_degraded",
                    "warning",
                    f"{decision} was checked by {len(votes)} of {len(panel)} voters; "
                    f"{', '.join(unavailable)} did not answer.",
                    decision=decision,
                )
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
