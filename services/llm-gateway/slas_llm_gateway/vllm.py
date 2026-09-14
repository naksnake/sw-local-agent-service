"""What the gateway sends to and receives from a vLLM instance, plus the fake for tests.

vLLM speaks an OpenAI-compatible chat API; these models are the subset the platform uses.
`guided_json` is the schema constrained decoding enforces (CLAUDE.md §7, §11 tier 0).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import Field

from slas_observability.tracing import outbound_headers
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
    #: The request's trace id; the HTTP client forwards it as `traceparent` (CLAUDE.md §8.2).
    trace_id: str | None = None


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


class UrllibVllmClient:
    """vLLM's OpenAI-compatible chat API over the standard library, one base URL per
    instance (`http://vllm-coder:8000`). Forwards the trace id as `traceparent` and
    `X-Slas-Trace-Id`; guided decoding travels as vLLM's `guided_json` field."""

    def __init__(self, base_urls: Mapping[str, str], *, timeout_s: float = 120.0) -> None:
        self.base_urls = {k: v.rstrip("/") for k, v in base_urls.items()}
        self.timeout_s = timeout_s

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        base = self.base_urls.get(request.instance)
        if base is None:
            raise InstanceUnavailableError(request.instance)
        body: dict[str, Any] = {
            "model": request.instance,
            "messages": [m.model_dump() for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.guided_json is not None:
            body["guided_json"] = request.guided_json
        headers = {"Content-Type": "application/json", **outbound_headers(request.trace_id)}
        http_request = urllib.request.Request(  # noqa: S310 — backend network, no egress
            f"{base}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise InstanceUnavailableError(request.instance) from exc
        try:
            choice = payload["choices"][0]
            text = str(choice["message"]["content"])
            usage = payload.get("usage", {})
            finish = str(choice.get("finish_reason") or "stop")
        except (KeyError, IndexError, TypeError) as exc:
            raise InstanceUnavailableError(request.instance) from exc
        return CompletionResponse(
            instance=request.instance,
            text=text,
            prompt_tokens=int(
                usage.get(
                    "prompt_tokens", _estimate_tokens(" ".join(m.content for m in request.messages))
                )
            ),
            completion_tokens=int(usage.get("completion_tokens", _estimate_tokens(text))),
            finish_reason="length" if finish == "length" else "stop",
        )
