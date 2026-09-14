"""The circuit breaker and schema-enforced generation against the fake vLLM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import Field

from slas_kernel.clock import FakeClock
from slas_llm_gateway.breaker import BreakerState, CircuitBreaker
from slas_llm_gateway.structured import (
    SchemaViolationError,
    VoterUnavailableError,
    generate_structured,
)
from slas_llm_gateway.vllm import CompletionRequest, FakeVllm, InstanceUnavailableError, Message
from slas_schemas.common import SlasModel

START = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


class Answer(SlasModel):
    owner: str = Field(min_length=1)
    severity: str = Field(pattern=r"^S[1-4]$")


def request(instance: str = "vllm-coder") -> CompletionRequest:
    return CompletionRequest(
        instance=instance, messages=[Message(role="user", content="Who owns it?")]
    )


# --- breaker -------------------------------------------------------------------------------


def test_two_failures_open_the_breaker_and_the_cooldown_half_opens_it() -> None:
    clock = FakeClock(START, step=timedelta(seconds=0))
    breaker = CircuitBreaker(clock, failure_threshold=2, cooldown=timedelta(minutes=5))
    assert breaker.allow("v") and breaker.state("v") is BreakerState.CLOSED
    assert breaker.sentence("v") == "v is answering normally."
    breaker.record_failure("v")
    assert breaker.allow("v"), "one failure is not enough"
    breaker.record_failure("v")
    assert breaker.state("v") is BreakerState.OPEN
    assert not breaker.allow("v")
    assert breaker.sentence("v") == "v is paused until 09:05 after 2 invalid answers."
    assert breaker.open_keys() == ["v"]

    clock._now = START + timedelta(minutes=5)  # the test controls time
    assert breaker.state("v") is BreakerState.HALF_OPEN
    assert breaker.allow("v"), "exactly one trial request"
    assert not breaker.allow("v"), "a second trial waits for the first result"
    assert breaker.sentence("v") == "v is being tried again after a pause."
    breaker.record_failure("v")
    assert breaker.state("v") is BreakerState.OPEN, "a failed trial reopens"
    clock._now = START + timedelta(minutes=10)
    assert breaker.allow("v")
    breaker.record_success("v")
    assert breaker.state("v") is BreakerState.CLOSED and breaker.failures("v") == 0


def test_breaker_rejects_a_zero_threshold() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        CircuitBreaker(FakeClock(START), failure_threshold=0)


# --- structured generation -----------------------------------------------------------------


def test_valid_answer_on_the_first_try_carries_the_schema_as_guided_json() -> None:
    vllm = FakeVllm()
    vllm.script_json("vllm-coder", {"owner": "EE", "severity": "S2"})
    breaker = CircuitBreaker(FakeClock(START))
    result = generate_structured(vllm, request(), Answer, breaker=breaker)
    assert result.value == Answer(owner="EE", severity="S2")
    assert result.attempts == 1 and result.tokens > 0 and result.instance == "vllm-coder"
    sent = vllm.requests[0]
    assert sent.guided_json == Answer.model_json_schema()
    assert sent.guided_json is not None and sent.guided_json["required"] == ["owner", "severity"]


def test_one_bad_answer_is_retried_with_the_validation_error_as_a_tool_result() -> None:
    vllm = FakeVllm()
    vllm.violate_schema("vllm-coder", times=1)
    vllm.script_json("vllm-coder", {"owner": "EE", "severity": "S1"})
    breaker = CircuitBreaker(FakeClock(START))
    result = generate_structured(vllm, request(), Answer, breaker=breaker)
    assert result.value.severity == "S1" and result.attempts == 2
    retry = vllm.requests[1]
    assert retry.messages[-2].role == "assistant"
    assert retry.messages[-2].content == FakeVllm.SCHEMA_VIOLATION
    assert retry.messages[-1].role == "tool"
    assert retry.messages[-1].content.startswith("Your answer did not match the required schema (")
    assert "Answer again with only the JSON object." in retry.messages[-1].content
    assert breaker.state("vllm-coder") is BreakerState.CLOSED, "a success resets the count"


def test_two_bad_answers_trip_the_breaker_and_the_third_attempt_is_refused() -> None:
    vllm = FakeVllm()
    vllm.violate_schema("vllm-coder", times=3)
    breaker = CircuitBreaker(FakeClock(START), failure_threshold=2)
    with pytest.raises(VoterUnavailableError) as raised:
        generate_structured(vllm, request(), Answer, breaker=breaker, max_retries=2)
    assert len(vllm.requests) == 2, "the breaker opened after the second invalid answer"
    assert breaker.state("vllm-coder") is BreakerState.OPEN
    assert raised.value.message.likely_cause.startswith("vllm-coder is paused until")


def test_exhausted_retries_raise_a_schema_violation_with_three_parts() -> None:
    vllm = FakeVllm()
    vllm.violate_schema("vllm-coder", times=3)
    breaker = CircuitBreaker(FakeClock(START), failure_threshold=10)
    with pytest.raises(SchemaViolationError) as raised:
        generate_structured(vllm, request(), Answer, breaker=breaker, max_retries=2)
    message = raised.value.message
    assert message.what_happened == "vllm-coder did not produce a valid answer in 3 attempts."
    assert message.likely_cause == (
        "Its last answer did not match the required schema: "
        "unexpected: Extra inputs are not permitted"
    )
    assert breaker.failures("vllm-coder") == 3


def test_a_partially_valid_object_is_never_returned() -> None:
    vllm = FakeVllm()
    vllm.script_json("vllm-coder", {"owner": "EE", "severity": "S9"})  # severity out of range
    vllm.script_json("vllm-coder", {"owner": "EE"})  # severity missing
    vllm.script_json("vllm-coder", {"owner": "EE", "severity": "S3"})
    breaker = CircuitBreaker(FakeClock(START), failure_threshold=10)
    result = generate_structured(vllm, request(), Answer, breaker=breaker, max_retries=2)
    assert result.value.severity == "S3" and result.attempts == 3


def test_an_instance_that_is_down_counts_as_a_failure() -> None:
    vllm = FakeVllm()
    vllm.take_down("vllm-coder")
    breaker = CircuitBreaker(FakeClock(START))
    with pytest.raises(VoterUnavailableError, match="cannot be asked right now"):
        generate_structured(vllm, request(), Answer, breaker=breaker)
    assert breaker.failures("vllm-coder") == 1
    vllm.restore("vllm-coder")
    with pytest.raises(AssertionError, match="no scripted answer"):
        vllm.complete(request())
    with pytest.raises(InstanceUnavailableError):
        vllm.take_down("vllm-coder")
        vllm.complete(request())
