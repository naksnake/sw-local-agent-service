"""Schema-enforced generation: tiers 0 and 1 of the tool-call fallback ladder (CLAUDE.md §11).

Tier 0: the request carries the JSON Schema as `guided_json` so constrained decoding does
the work. Tier 1: an answer that still fails validation is sent back once or twice as a
tool result with the validation error. Every failure counts against the instance's breaker.
A partially valid object is never returned (§11: "Never pass a partially valid call").
Tiers 2 to 5 (simplified schema, planner escalation, deterministic extraction, human queue)
arrive with the agents that need them.

Two entry points, one loop: `generate_structured` validates into a Pydantic model (in-process
callers); `generate_validated` takes any schema and validator, which is how the HTTP surface
serves a schema it did not author (`schema_check.validate_json`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.schema_check import SchemaMismatchError
from slas_llm_gateway.vllm import (
    CompletionRequest,
    CompletionResponse,
    InstanceUnavailableError,
    Message,
    VllmClient,
)
from slas_schemas.common import validation_sentence
from slas_schemas.errors import ThreePartMessage


@dataclass(frozen=True, slots=True)
class StructuredResult[T]:
    value: T
    instance: str
    attempts: int
    #: Prompt and completion tokens over every attempt.
    tokens: int
    #: The answer that validated (the last one sent).
    response: CompletionResponse

    @property
    def tier(self) -> int:
        """0 when constrained decoding was enough, 1 when a retry was needed (§11)."""
        return 0 if self.attempts == 1 else 1


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


def generate_validated[T](
    client: VllmClient,
    request: CompletionRequest,
    schema: Mapping[str, Any],
    validate: Callable[[str], T],
    *,
    breaker: CircuitBreaker,
    max_retries: int = 2,
) -> StructuredResult[T]:
    """Ask with `schema` as `guided_json`; accept only an answer `validate` turns into a value.

    `validate` raises a Pydantic `ValidationError` or a `SchemaMismatchError`; the sentence of
    either becomes the tool result of the retry.
    """
    current = request.model_copy(update={"guided_json": dict(schema)})
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
            value = validate(response.text)
        except ValidationError as error:
            last_error = validation_sentence(error)
        except SchemaMismatchError as error:
            last_error = str(error)
        else:
            breaker.record_success(key)
            return StructuredResult(
                value=value, instance=key, attempts=attempts, tokens=tokens, response=response
            )
        breaker.record_failure(key, f"schema: {last_error}")
        if attempts > max_retries:
            raise SchemaViolationError(key, attempts, last_error)
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


def generate_structured[T: BaseModel](
    client: VllmClient,
    request: CompletionRequest,
    model_type: type[T],
    *,
    breaker: CircuitBreaker,
    max_retries: int = 2,
) -> StructuredResult[T]:
    return generate_validated(
        client,
        request,
        model_type.model_json_schema(),
        model_type.model_validate_json,
        breaker=breaker,
        max_retries=max_retries,
    )
