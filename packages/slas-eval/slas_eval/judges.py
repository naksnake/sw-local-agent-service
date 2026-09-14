"""Judges for the eval suite: local models only (CLAUDE.md §8.1, INV-2).

This module is the single construction site for judges. A judge endpoint is validated
here: cloud AI provider domains are refused by keyword, and anything that is not loopback,
a private address, a compose service name or an `.internal`/`.local` host is refused too.
A repo-policy test asserts that no source file names a cloud AI endpoint; this module is
the place that guarantees the eval suite cannot reach one even by configuration.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

#: Cloud AI providers named in INV-2, as domain labels: any host whose dotted name contains
#: one of these labels is refused (so every regional or product-specific subdomain is too).
CLOUD_PROVIDER_LABELS: Final[tuple[str, ...]] = (
    "openai",
    "anthropic",
    "azure",
    "googleapis",
    "amazonaws",
    "bedrock",
    "cohere",
    "huggingface",
    "hf",
    "langchain",
    "wandb",
    "mistral",
    "together",
    "groq",
    "replicate",
    "perplexity",
)
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_ALLOWED_SUFFIXES: Final = (".internal", ".local", ".lan", ".localdomain")


class CloudEndpointError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def assert_local_endpoint(url: str) -> str:
    """Return the host if the URL points inside the perimeter; raise otherwise."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise CloudEndpointError(
            ThreePartMessage(
                f"{url!r} is not a usable judge endpoint.",
                "A judge endpoint is an http(s) URL with a host.",
                "Point it at a local vLLM instance, for example http://vllm-planner:8000.",
            )
        )
    labels = host.split(".")
    if len(labels) > 1 and any(label in CLOUD_PROVIDER_LABELS for label in labels):
        raise CloudEndpointError(
            ThreePartMessage(
                f"{host} is a cloud AI service and cannot be a judge.",
                "Evaluation runs against local models only (CLAUDE.md §8.1, INV-2).",
                "Use a local vLLM instance through the gateway instead.",
            )
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_loopback or address.is_private or address.is_link_local:
            return host
    elif host == "localhost" or host.endswith(_ALLOWED_SUFFIXES) or _SERVICE_NAME.match(host):
        return host
    raise CloudEndpointError(
        ThreePartMessage(
            f"{host} is outside the perimeter and cannot be a judge.",
            "Only loopback or private addresses, compose service names and .internal/.local "
            "hosts are allowed (INV-1).",
            "Use a local vLLM instance through the gateway instead.",
        )
    )


class LocalJudgeEndpoint(SlasModel):
    """Where a judge model is served; refuses anything outside the perimeter at construction."""

    url: str = Field(min_length=1)
    role: str = "planner"

    @field_validator("url")
    @classmethod
    def _inside_the_perimeter(cls, url: str) -> str:
        assert_local_endpoint(url)
        return url

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


class JudgeRequest(SlasModel):
    task: str = Field(min_length=1)
    original: str
    candidate: str
    criteria: str = "Do the two texts state the same facts, actions and expectations?"


class JudgeVerdict(SlasModel):
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)


class Judge(Protocol):
    def judge(self, request: JudgeRequest) -> JudgeVerdict: ...


class FakeJudge:
    """Scores by word overlap between original and candidate; `overrides` pin a score for any
    candidate containing the given substring."""

    def __init__(self, overrides: dict[str, float] | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.requests: list[JudgeRequest] = []

    def judge(self, request: JudgeRequest) -> JudgeVerdict:
        self.requests.append(request)
        for needle, score in self.overrides.items():
            if needle in request.candidate:
                return JudgeVerdict(score=score, reason=f"pinned by the test for {needle!r}")
        original = set(re.findall(r"[a-z0-9]+", request.original.lower()))
        candidate = set(re.findall(r"[a-z0-9]+", request.candidate.lower()))
        if not original:
            return JudgeVerdict(score=1.0 if not candidate else 0.0, reason="nothing to compare")
        score = len(original & candidate) / len(original | candidate)
        return JudgeVerdict(score=round(score, 3), reason=f"word overlap {score:.2f}")
