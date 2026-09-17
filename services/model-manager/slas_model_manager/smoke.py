"""`HttpSmokeTester`: one tiny chat completion against a candidate instance (CLAUDE.md §7).

The swap manager knows the instance by name only, so the tester first asks the instance
which model it serves (`GET /v1/models`, the name vLLM was given with `--served-model-name`)
and then posts a one-line prompt to `/v1/chat/completions`. Both answers must be 200 and the
completion must carry text. The result is a sentence either way.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Final

import httpx

from slas_model_manager.driver import instance_url
from slas_model_manager.swap import SmokeResult
from slas_observability.tracing import outbound_headers

SMOKE_PROMPT: Final = "Reply with the single word: ready"
SMOKE_TIMEOUT_S: Final = 120.0


class HttpSmokeTester:
    def __init__(
        self,
        *,
        url_for: Callable[[str], str] = instance_url,
        timeout_s: float = SMOKE_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url_for = url_for
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._monotonic = monotonic

    def close(self) -> None:
        self._client.close()

    def smoke(self, instance: str) -> SmokeResult:
        base = self.url_for(instance).rstrip("/")
        started = self._monotonic()
        try:
            served = self._served_model(base)
            if served is None:
                return SmokeResult(
                    ok=False,
                    sentence=f"{instance} did not name the model it serves at {base}/v1/models.",
                )
            response = self._client.post(
                f"{base}/v1/chat/completions",
                json={
                    "model": served,
                    "messages": [{"role": "user", "content": SMOKE_PROMPT}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
                headers=outbound_headers(),
            )
        except httpx.HTTPError as exc:
            return SmokeResult(
                ok=False,
                sentence=f"{instance} did not answer the smoke test ({type(exc).__name__}).",
            )
        if response.status_code != 200:
            return SmokeResult(
                ok=False,
                sentence=f"{instance} answered {response.status_code} to a chat completion.",
            )
        text = _completion_text(response)
        if not text:
            return SmokeResult(
                ok=False, sentence=f"{instance} answered a chat completion without any text."
            )
        elapsed_ms = int((self._monotonic() - started) * 1000)
        return SmokeResult(
            ok=True,
            sentence=(
                f"Smoke test passed: {instance} answered a chat completion in {elapsed_ms} ms."
            ),
        )

    def _served_model(self, base: str) -> str | None:
        response = self._client.get(f"{base}/v1/models", headers=outbound_headers())
        if response.status_code != 200:
            return None
        payload: Any = _json(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        return str(first["id"]) if isinstance(first, dict) and first.get("id") else None


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _completion_text(response: httpx.Response) -> str:
    payload = _json(response)
    try:
        return str(payload["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError):
        return ""
