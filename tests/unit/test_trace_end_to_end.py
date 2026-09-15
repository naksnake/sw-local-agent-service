"""One trace id spans WebUI → api → orchestrator → gateway → vLLM → executor (CLAUDE.md §8.2).

The WebUI mints a `traceparent` (the same W3C format `apps/webui/src/trace.ts` produces); the
api edge accepts it and binds it; the kernel runs a validation-like plan whose cross-check
goes through the real Gateway to a fake vLLM HTTP server that records the headers it was
sent; the executor's context and every journal entry and event carry the id. At the end the
set of trace ids seen anywhere has exactly one member.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.journal import Journal
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.store import FileTicketStore
from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.consensus import TokenBudget, default_rules
from slas_llm_gateway.gateway import Gateway
from slas_llm_gateway.redaction import default_redactor
from slas_llm_gateway.routing import RoleRouter, Routes
from slas_llm_gateway.vllm import UrllibVllmClient
from slas_observability import tracing
from slas_observability.alerts import ListAlertChannel
from slas_observability.events import EventLog, ListSink, trace_ids
from slas_orchestrator.gateway import GatewayCrossChecker
from slas_schemas.common import AgentName
from slas_schemas.job import Upload

VOTERS = ["vllm-voter-qwen", "vllm-voter-deepseek", "vllm-voter-kimi"]

#: What apps/webui/src/trace.ts mints: version 00, 16 random bytes, 8 random bytes, sampled.
WEBUI_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class FakeVllmHttp:
    """vLLM's chat endpoint on loopback: answers an approving vote, records every header."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []
        self.bodies: list[dict[str, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                outer.bodies.append(body)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                vote = {
                    "voter": "x",
                    "verdict": "approve",
                    "fields": {},
                    "reason": "the plan is sound",
                    "confidence": 0.9,
                }
                payload = {
                    "choices": [
                        {"message": {"content": json.dumps(vote)}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 20},
                }
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ValidationLikeAgent(NullAgent):
    """The NullAgent's plan under the validation name, so the kernel cross-checks the plan."""

    name: AgentName = "validation"


class TracedExecutor(FakeExecutor):
    def __init__(self, log: EventLog) -> None:
        super().__init__()
        self.log = log

    def execute(self, step, context):  # type: ignore[no-untyped-def]
        self.log.info("step", step=step.id, context_trace=context.trace_id)
        return super().execute(step, context)


def test_one_trace_id_spans_webui_api_orchestrator_gateway_vllm_and_executor(
    tmp_path: Path,
) -> None:
    sink = ListSink()
    logs = {
        name: EventLog(name, sink)
        for name in ("api", "agent-core-orchestrator", "llm-gateway", "validation-executor")
    }
    vllm = FakeVllmHttp()
    vllm.start()
    try:
        clock = FakeClock(datetime(2026, 9, 14, 9, 0, tzinfo=UTC), step=timedelta(seconds=0))
        channel = ListAlertChannel()
        gateway = Gateway(
            client=UrllibVllmClient(dict.fromkeys(VOTERS, vllm.url)),
            router=RoleRouter(Routes(roles={"planner": VOTERS[0]}, voters=VOTERS)),
            redactor=default_redactor(),
            breaker=CircuitBreaker(clock),
            rules=default_rules(),
            budget=TokenBudget(clock, daily_tokens=1_000_000),
            channel=channel,
        )
        executor = TracedExecutor(logs["validation-executor"])
        kernel = Kernel(
            data_root=tmp_path,
            agent=ValidationLikeAgent(),
            executor=executor,
            store=FileTicketStore(tmp_path),
            clock=FakeClock(),
            plan_checker=GatewayCrossChecker(gateway),
        )

        # WebUI → api: the browser's header arrives; the api binds it for the request.
        inbound = {"traceparent": WEBUI_TRACEPARENT, "Content-Type": "application/json"}
        trace_id = tracing.accept(inbound)
        try:
            logs["api"].info("request", method="POST", path="/validation/runs")
            # api → orchestrator: same process here; over HTTP it forwards outbound_headers().
            forwarded = tracing.outbound_headers()
            assert tracing.trace_id_from_headers(forwarded) == trace_id
            logs["agent-core-orchestrator"].info("run", agent="validation")
            ticket = kernel.run(
                Upload(filename="suite.md", uploaded_by="lee", content="# suite\n", size_bytes=8)
            )
            logs["llm-gateway"].info("cross_check", decision="plan_approval")
        finally:
            tracing._current.set(None)

        assert trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert ticket.state.value == "Done" and len(ticket.votes) == 3

        # gateway → vLLM: three voters, each request carried the trace id as headers.
        assert len(vllm.headers) == 3
        for headers in vllm.headers:
            assert tracing.parse_traceparent(headers["traceparent"]) == trace_id
            assert headers["x-slas-trace-id"] == trace_id
        assert all(body["guided_json"] for body in vllm.bodies)

        # orchestrator → executor: every step's context carried it.
        assert {context.trace_id for context in executor.contexts} == {trace_id}

        # The journal: every entry of the run carries it.
        entries = Journal(tmp_path / "Tickets" / ticket.id / "journal.jsonl", FakeClock()).entries()
        assert len(entries) >= 10
        assert {entry.trace_id for entry in entries} == {trace_id}

        # The events from every service: one id.
        records = sink.records()
        assert {record["service"] for record in records} == {
            "api",
            "agent-core-orchestrator",
            "llm-gateway",
            "validation-executor",
        }
        assert trace_ids(records) == {trace_id}
        assert all(r["context_trace"] == trace_id for r in records if r["event"] == "step")

        # One grep finds the whole run.
        journal_text = (tmp_path / "Tickets" / ticket.id / "journal.jsonl").read_text()
        assert journal_text.count(trace_id) == len(entries)
        assert not channel.alerts, "unanimous approval raises nothing"
    finally:
        vllm.stop()

    # Outside a request nothing is bound: a run without an api trace mints its own and
    # does not leak it into the next one.
    kernel2 = Kernel(
        data_root=tmp_path / "second",
        agent=NullAgent(),
        executor=FakeExecutor(),
        store=FileTicketStore(tmp_path / "second"),
        clock=FakeClock(),
    )
    first = kernel2.run(Upload(filename="a.md", uploaded_by="lee", content="#", size_bytes=1))
    second = kernel2.run(Upload(filename="b.md", uploaded_by="lee", content="#", size_bytes=1))
    ids = {
        e.trace_id
        for t in (first, second)
        for e in Journal(
            tmp_path / "second" / "Tickets" / t.id / "journal.jsonl", FakeClock()
        ).entries()
    }
    assert len(ids) == 2 and None not in ids
    assert tracing.current_trace_id() is None
