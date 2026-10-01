"""The gateway-backed `Coder`: the model proposes edits, schema-constrained (§10.1 ACT).

`GatewayLike` is what the orchestrator asks of the LLM gateway — the signatures of
`slas_llm_gateway.gateway.Gateway`, so the in-process `Gateway` and the gateway service's
`HttpGateway` both fit. The prompt carries the task, the file snapshot and the last check
output; never a credential (INV-5) — the gateway redacts once more at its boundary. The
answer is constrained to `EditSet`; a partially valid answer never reaches the executor
(§11 tier 0/1 live in the gateway).
"""

from __future__ import annotations

from typing import Final, Protocol

from pydantic import BaseModel

from slas_llm_gateway.structured import StructuredResult
from slas_llm_gateway.vllm import CompletionResponse, Message
from slas_orchestrator.coding.executor import EditRequest, EditSet
from slas_schemas.vote import ConsensusVerdict

CODER_ROLE: Final = "coder"
#: Bound on the snapshot the model sees per request, in characters (about 30k tokens).
MAX_PROMPT_CHARS: Final = 120_000
MAX_CHECK_OUTPUT_CHARS: Final = 4_000

SYSTEM_INSTRUCTION: Final = (
    "You are the Coding Agent of SW Local Agent Service, working inside an isolated sandbox "
    "on one task of an approved plan. Answer with a single JSON object: `files` maps a "
    "project-relative path to the complete new content of that file, or to null to delete "
    "it; `note` is one sentence on what you changed. Change only what the task needs, keep "
    "every file complete and runnable, and never write outside the project or into .git. "
    "The checks listed must pass; when a check failed, fix its cause."
)


class GatewayLike(Protocol):
    """The LLM gateway as the orchestrator uses it (contract §2: `Gateway` and `HttpGateway`)."""

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResponse: ...

    def generate[T: BaseModel](
        self,
        role: str,
        messages: list[Message],
        model_type: type[T],
        *,
        max_retries: int = 2,
    ) -> StructuredResult[T]: ...

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict: ...


def _snapshot_section(files: dict[str, str], budget: int) -> str:
    if not files:
        return "The project is empty."
    lines: list[str] = []
    used = 0
    left_out: list[str] = []
    for path in sorted(files):
        block = f"--- {path} ---\n{files[path]}\n"
        if used + len(block) > budget:
            left_out.append(path)
            continue
        lines.append(block)
        used += len(block)
    if left_out:
        lines.append(
            f"({len(left_out)} more file(s) not shown to keep the prompt short: "
            f"{', '.join(left_out[:10])}{'…' if len(left_out) > 10 else ''})"
        )
    return "\n".join(lines)


def build_messages(request: EditRequest) -> list[Message]:
    """The two messages the coder role receives; deterministic, so a run is reproducible."""
    task = request.task
    header = [
        f"Task {task.n}: {task.title}",
        f"Languages: {', '.join(request.languages) or 'not stated'}",
        f"Iteration: {request.iteration}",
    ]
    if task.files:
        header.append(f"Files the plan names: {', '.join(task.files)}")
    if task.acceptance:
        header.append(f"Acceptance: {task.acceptance}")
    if request.failures:
        checks = ["Checks that failed last time:"]
        for failure in request.failures:
            output = failure.output[-MAX_CHECK_OUTPUT_CHARS:]
            checks.append(
                f"[{failure.kind}] {failure.description} (exit {failure.exit_code})\n{output}"
            )
        check_text = "\n".join(checks)
    else:
        check_text = "No check has run yet." if request.iteration == 1 else "Every check passed."
    fixed = "\n".join(header) + "\n\n" + check_text + "\n\nProject files:\n"
    budget = max(MAX_PROMPT_CHARS - len(fixed), 2_000)
    body = fixed + _snapshot_section(request.files, budget)
    return [
        Message(role="system", content=SYSTEM_INSTRUCTION),
        Message(role="user", content=body),
    ]


class GatewayCoder:
    """`Coder.propose_edits` through `gateway.generate(role="coder", …, EditSet)`."""

    def __init__(self, gateway: GatewayLike, *, role: str = CODER_ROLE) -> None:
        self.gateway = gateway
        self.role = role
        self.last_attempts = 0

    def propose_edits(self, request: EditRequest) -> EditSet:
        result = self.gateway.generate(self.role, build_messages(request), EditSet)
        self.last_attempts = result.attempts
        return result.value
