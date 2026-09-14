"""The kernel's view of the LLM gateway (CLAUDE.md §4.2): the orchestrator wires the
kernel's `CrossChecker` protocol to `Gateway.cross_check`, turning evidence lines into the
messages every voter sees. The trace id is not passed by hand: the gateway reads it from the
context the api bound, so one id spans WebUI → api → orchestrator → gateway → vLLM.
"""

from __future__ import annotations

from slas_llm_gateway.gateway import Gateway
from slas_llm_gateway.vllm import Message
from slas_schemas.vote import ConsensusVerdict


class GatewayCrossChecker:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def cross_check(self, decision: str, evidence: list[str]) -> ConsensusVerdict:
        content = "Evidence:\n" + "\n".join(f"- {line}" for line in evidence)
        return self.gateway.cross_check(decision, [Message(role="user", content=content)])
