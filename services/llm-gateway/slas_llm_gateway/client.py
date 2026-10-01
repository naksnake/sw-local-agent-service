"""`HttpGateway`: the gateway over HTTP, with the same methods and return types as `Gateway`.

The orchestrator uses whichever it is given (`GatewayLike`): in tests the in-process
`Gateway` over `FakeVllm`, in the stack this client against `SLAS_GATEWAY_URL`. A three-part
answer from the service comes back as `slas_http.ServiceError` with the same status and
sentences (`.message` is the `ThreePartMessage`, as on the gateway's own errors).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from slas_http.client import DEFAULT_TIMEOUT_S, ServiceClient
from slas_llm_gateway.structured import StructuredResult
from slas_llm_gateway.vllm import CompletionResponse, Message
from slas_schemas.vote import ConsensusVerdict

SERVICE = "llm-gateway"


class GatewayLike(Protocol):
    """What `Gateway` and `HttpGateway` share; the orchestrator depends on this, not on either."""

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        guided_json: dict[str, Any] | None = None,
    ) -> CompletionResponse: ...

    def generate[T: BaseModel](
        self,
        role: str,
        messages: list[Message],
        model_type: type[T],
        *,
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> StructuredResult[T]: ...

    def generate_json(
        self,
        role: str,
        messages: list[Message],
        schema: Mapping[str, Any],
        *,
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> StructuredResult[Any]: ...

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict: ...


class HttpGateway:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client = ServiceClient(SERVICE, base_url, timeout_s=timeout_s, transport=transport)

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _wire(messages: list[Message]) -> list[dict[str, str]]:
        return [message.model_dump() for message in messages]

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        guided_json: dict[str, Any] | None = None,
    ) -> CompletionResponse:
        payload = self.client.post(
            "/v1/complete",
            {
                "role": role,
                "messages": self._wire(messages),
                "max_tokens": max_tokens,
                "temperature": temperature,
                "guided_json": guided_json,
            },
        )
        return CompletionResponse.model_validate(payload)

    def generate_json(
        self,
        role: str,
        messages: list[Message],
        schema: Mapping[str, Any],
        *,
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> StructuredResult[Any]:
        payload = self.client.post(
            "/v1/generate",
            {
                "role": role,
                "messages": self._wire(messages),
                "schema": dict(schema),
                "max_tokens": max_tokens,
                "max_retries": max_retries,
            },
        )
        response = CompletionResponse.model_validate(payload["response"])
        tier = int(payload["tier"])
        return StructuredResult(
            value=payload["object"],
            instance=response.instance,
            # The wire carries the tier, not the count; tier 1 means at least one retry.
            attempts=tier + 1,
            tokens=response.total_tokens,
            response=response,
        )

    def generate[T: BaseModel](
        self,
        role: str,
        messages: list[Message],
        model_type: type[T],
        *,
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> StructuredResult[T]:
        result = self.generate_json(
            role,
            messages,
            model_type.model_json_schema(),
            max_tokens=max_tokens,
            max_retries=max_retries,
        )
        return StructuredResult(
            value=model_type.model_validate(result.value),
            instance=result.instance,
            attempts=result.attempts,
            tokens=result.tokens,
            response=result.response,
        )

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict:
        payload = self.client.post(
            "/v1/cross-check", {"decision": decision, "evidence": self._wire(evidence)}
        )
        return ConsensusVerdict.model_validate(payload)
