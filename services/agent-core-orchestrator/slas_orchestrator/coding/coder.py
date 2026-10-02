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
#: Room for the answer: whole files inside one JSON object. The gateway's 1024 default cut
#: every real file off mid-JSON, so both tries were invalid and the breaker paused the coder.
CODER_MAX_TOKENS: Final = 16_384
#: Bound on the snapshot the model sees per request, in characters (about 30k tokens).
MAX_PROMPT_CHARS: Final = 120_000
MAX_CHECK_OUTPUT_CHARS: Final = 4_000
#: Of a long check output, the head is kept too: compilers report the first error first,
#: test runners summarise at the end.
CHECK_OUTPUT_HEAD_CHARS: Final = 1_200
#: Bound on the project tree listing (paths only).
MAX_TREE_PATHS: Final = 400
#: Bound on the plan text in the prompt; the rest of the budget is the file snapshot.
MAX_PLAN_PROMPT_CHARS: Final = 24_000

SYSTEM_INSTRUCTION: Final = (
    "You are the Coding Agent of SW Local Agent Service, an expert software engineer working "
    "inside an isolated sandbox on one task of an approved plan. Answer with a single JSON "
    "object, in this order:\n"
    "- `approach`: 2 to 6 short steps saying what you will change and why. When a check "
    "failed, the first step names its root cause from the output.\n"
    "- `replacements`: targeted edits to files that already exist: `path`, `find` (text "
    "copied exactly from the file, with enough surrounding lines to occur once) and "
    "`replace`. Prefer these for small changes to large files.\n"
    "- `files`: a project-relative path mapped to the complete content of a new or "
    "rewritten file, or to null to delete it. Never send a partial file here.\n"
    "- `note`: one sentence on what you changed.\n"
    "Rules: implement what the plan asks for in real, working code: no placeholders, no "
    "TODO stubs, no invented APIs. Read the existing files before changing them and keep "
    "their style. Change only what the task needs, never write outside the project or into "
    ".git. Handle errors and edge cases the plan mentions; give command-line tools a --help "
    "and a non-zero exit on failure. Add or update tests where the language has a test "
    "runner, and make them check behaviour, not just that code runs. The checks listed "
    "must pass; when one failed, fix its cause rather than the test."
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
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> StructuredResult[T]: ...

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict: ...


def _bounded_output(output: str) -> str:
    """A check's output within MAX_CHECK_OUTPUT_CHARS: its head and its tail."""
    if len(output) <= MAX_CHECK_OUTPUT_CHARS:
        return output
    tail = MAX_CHECK_OUTPUT_CHARS - CHECK_OUTPUT_HEAD_CHARS
    return f"{output[:CHECK_OUTPUT_HEAD_CHARS]}\n[… output shortened …]\n{output[-tail:]}"


def _tree_section(paths: list[str]) -> str:
    if not paths:
        return ""
    shown = paths[:MAX_TREE_PATHS]
    more = len(paths) - len(shown)
    return (
        "Project tree:\n"
        + "\n".join(shown)
        + (f"\n({more} more paths)" if more > 0 else "")
        + "\n\n"
    )


def _snapshot_section(files: dict[str, str], budget: int) -> str:
    """The files in the order the executor read them: the ones the task names and the last
    iteration changed come first, so the budget never leaves those out."""
    if not files:
        return "The project is empty."
    lines: list[str] = []
    used = 0
    left_out: list[str] = []
    for path in files:
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
            output = _bounded_output(failure.output)
            checks.append(
                f"[{failure.kind}] {failure.description} (exit {failure.exit_code})\n{output}"
            )
        check_text = "\n".join(checks)
    else:
        check_text = "No check has run yet." if request.iteration == 1 else "Every check passed."
    plan = request.plan.strip()[:MAX_PLAN_PROMPT_CHARS]
    plan_text = f"The plan:\n{plan}\n\n" if plan else ""
    tree = _tree_section(request.paths)
    fixed = "\n".join(header) + "\n\n" + plan_text + check_text + "\n\n" + tree + "Project files:\n"
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
        result = self.gateway.generate(
            self.role, build_messages(request), EditSet, max_tokens=CODER_MAX_TOKENS
        )
        self.last_attempts = result.attempts
        return result.value
