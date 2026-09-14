"""Schema-enforced generation: tiers 0 and 1 of the tool-call fallback ladder (CLAUDE.md §11).

Tier 0: the request carries the JSON Schema as `guided_json` so constrained decoding does
the work. Tier 1: an answer that still fails validation is sent back once or twice as a
tool result with the validation error. Every failure counts against the instance's breaker.
A partially valid object is never returned (§11: "Never pass a partially valid call").
Tiers 2 to 5 (simplified schema, planner escalation, deterministic extraction, human queue)
arrive with the agents that need them.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.vllm import (
    CompletionRequest,
    InstanceUnavailableError,
    Message,
    VllmClient,
)
from slas_schemas.common import validation_sentence
from slas_schemas.errors import ThreePartMessage


@dataclass(frozen=True, slots=True)
class StructuredResult[T: BaseModel]:
    value: T
    instance: str
    attempts: int
    tokens: int


class SchemaViolationError(RuntimeError):
    def __init__(self, instance: str, attempts: int, last_error: str) -> None:
        self.message = ThreePartMessage(
            f"{instance} did not produce a valid answer in {attempts} attempts.",
            f"Its last answer did not match the required schema: {last_error}",
            "The instance is paused by the circuit breaker; the request falls back to the "
            "next tier or waits for a human.",
        )
        super().__init__(self.message.what_happened)
        self.instance = instance
        self.attempts = attempts


class VoterUnavailableError(RuntimeError):
    def __init__(self, instance: str, why: str) -> None:
        self.message = ThreePartMessage(
            f"{instance} cannot be asked right now.",
            why,
            "Other voters continue; the verdict is marked as degraded if fewer answered.",
        )
        super().__init__(self.message.what_happened)
        self.instance = instance


def generate_structured[T: BaseModel](
    client: VllmClient,
    request: CompletionRequest,
    model_type: type[T],
    *,
    breaker: CircuitBreaker,
    max_retries: int = 2,
) -> StructuredResult[T]:
    schema = model_type.model_json_schema()
    current = request.model_copy(update={"guided_json": schema})
    key = request.instance
    attempts = 0
    tokens = 0
    last_error = ""
    while True:
        if not breaker.allow(key):
            raise VoterUnavailableError(key, breaker.sentence(key))
        attempts += 1
        try:
            response = client.complete(current)
        except InstanceUnavailableError as exc:
            breaker.record_failure(key, "did not answer")
            raise VoterUnavailableError(key, str(exc)) from exc
        tokens += response.total_tokens
        try:
            value = model_type.model_validate_json(response.text)
        except ValidationError as error:
            last_error = validation_sentence(error)
            breaker.record_failure(key, f"schema: {last_error}")
            if attempts > max_retries:
                raise SchemaViolationError(key, attempts, last_error) from error
            current = current.model_copy(
                update={
                    "messages": [
                        *current.messages,
                        Message(role="assistant", content=response.text),
                        Message(
                            role="tool",
                            content=(
                                f"Your answer did not match the required schema ({last_error}). "
                                "Answer again with only the JSON object."
                            ),
                        ),
                    ]
                }
            )
            continue
        breaker.record_success(key)
        return StructuredResult(value=value, instance=key, attempts=attempts, tokens=tokens)
