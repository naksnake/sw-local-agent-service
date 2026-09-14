"""What the gateway sends to and receives from a vLLM instance, plus the fake for tests.

vLLM speaks an OpenAI-compatible chat API; these models are the subset the platform uses.
`guided_json` is the schema constrained decoding enforces (CLAUDE.md §7, §11 tier 0).
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Literal, Protocol

from pydantic import Field

from slas_schemas.common import SlasModel


class Message(SlasModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class CompletionRequest(SlasModel):
    instance: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    guided_json: dict[str, Any] | None = None
    max_tokens: int = Field(default=1024, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class CompletionResponse(SlasModel):
    instance: str
    text: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    finish_reason: Literal["stop", "length", "error"] = "stop"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class InstanceUnavailableError(RuntimeError):
    def __init__(self, instance: str) -> None:
        super().__init__(f"The model instance {instance} did not answer.")
        self.instance = instance


class VllmClient(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResponse: ...


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


class FakeVllm:
    """Scripted vLLM: answers come from a per-instance queue; can misbehave on command."""

    SCHEMA_VIOLATION = json.dumps({"unexpected": "this is not the object that was asked for"})

    def __init__(self) -> None:
        self.scripts: dict[str, deque[str]] = {}
        self.requests: list[CompletionRequest] = []
        self._violations: dict[str, int] = {}
        self.down: set[str] = set()

    def script(self, instance: str, *texts: str) -> None:
        self.scripts.setdefault(instance, deque()).extend(texts)

    def script_json(self, instance: str, *objects: object) -> None:
        self.script(instance, *(json.dumps(obj) for obj in objects))

    def violate_schema(self, instance: str, times: int = 1) -> None:
        """The next `times` answers from `instance` ignore the requested schema."""
        self._violations[instance] = self._violations.get(instance, 0) + times

    def take_down(self, instance: str) -> None:
        self.down.add(instance)

    def restore(self, instance: str) -> None:
        self.down.discard(instance)

    def requests_for(self, instance: str) -> list[CompletionRequest]:
        return [request for request in self.requests if request.instance == instance]

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if request.instance in self.down:
            raise InstanceUnavailableError(request.instance)
        if self._violations.get(request.instance, 0) > 0:
            self._violations[request.instance] -= 1
            text = self.SCHEMA_VIOLATION
        else:
            queue = self.scripts.get(request.instance)
            if not queue:
                raise AssertionError(f"FakeVllm has no scripted answer left for {request.instance}")
            text = queue.popleft()
        prompt_tokens = sum(_estimate_tokens(m.content) for m in request.messages)
        return CompletionResponse(
            instance=request.instance,
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=_estimate_tokens(text),
        )
