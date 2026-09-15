"""slas_observability: the metrics registry and its text format, the /metrics endpoint, trace
ids and headers, structured events, and the YAML emitter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from slas_observability import metrics, tracing, yamlish
from slas_observability.events import EventLog, FileSink, ListSink, StreamSink, trace_ids
from slas_observability.exposition import CONTENT_TYPE, MetricsServer
from slas_observability.metrics import CATALOGUE, MetricError, Registry

# --- metrics -------------------------------------------------------------------------------------


def test_the_catalogue_carries_every_name_from_claude_md_section_8_2() -> None:
    required = {
        "slas_agent_turns_total": ("agent", "outcome"),
        "slas_consensus_votes_total": ("decision", "verdict"),
        "slas_consensus_disagreements_total": ("decision",),
        "slas_skill_runs_total": ("skill", "outcome"),
        "slas_screen_steps_total": ("primitive", "outcome"),
        "slas_ticket_state_changes_total": ("agent", "to"),
        "slas_sop_exports_total": ("lang",),
    }
    for name, labels in required.items():
        assert CATALOGUE[name].labels == labels, name
        assert CATALOGUE[name].kind == "counter"
    assert all(name.startswith("slas_") for name in CATALOGUE)
    assert all(spec.help.endswith(".") for spec in CATALOGUE.values())


def test_counters_gauges_and_histograms_render_in_the_prometheus_text_format() -> None:
    registry = Registry()
    registry.inc("slas_screen_steps_total", primitive="click", outcome="ok")
    registry.inc("slas_screen_steps_total", 2, primitive="click", outcome="ok")
    registry.inc("slas_screen_steps_total", primitive='ty"pe', outcome="failed")
    registry.set("slas_breaker_open", 1, instance="vllm-coder")
    registry.add("slas_tickets_in_state", 1, agent="coding", state="Running")
    registry.add("slas_tickets_in_state", -5, agent="coding", state="Running")
    registry.observe("slas_step_seconds", 3.0, agent="coding")
    registry.observe("slas_step_seconds", 90.0, agent="coding")
    registry.set("slas_sandbox_sessions_open", 2)

    text = registry.render()
    assert "# HELP slas_screen_steps_total GUI primitives performed" in text
    assert "# TYPE slas_screen_steps_total counter" in text
    assert 'slas_screen_steps_total{primitive="click",outcome="ok"} 3\n' in text
    assert 'slas_screen_steps_total{primitive="ty\\"pe",outcome="failed"} 1\n' in text
    assert 'slas_breaker_open{instance="vllm-coder"} 1\n' in text
    assert 'slas_tickets_in_state{agent="coding",state="Running"} 0\n' in text, "never below 0"
    assert "slas_sandbox_sessions_open 2\n" in text
    assert 'slas_step_seconds_bucket{agent="coding",le="5"} 1\n' in text
    assert 'slas_step_seconds_bucket{agent="coding",le="300"} 2\n' in text
    assert 'slas_step_seconds_bucket{agent="coding",le="+Inf"} 2\n' in text
    assert 'slas_step_seconds_sum{agent="coding"} 93\n' in text
    assert 'slas_step_seconds_count{agent="coding"} 2\n' in text
    assert registry.value("slas_screen_steps_total", primitive="click", outcome="ok") == 3
    assert registry.count("slas_step_seconds", agent="coding") == 2
    assert registry.samples("slas_breaker_open") == {("vllm-coder",): 1.0}
    registry.reset()
    assert "slas_sandbox_sessions_open 2" not in registry.render()


def test_unknown_names_wrong_labels_and_wrong_kinds_raise_at_once() -> None:
    registry = Registry()
    with pytest.raises(MetricError, match="not a metric this platform emits"):
        registry.inc("slas_made_up_total")
    with pytest.raises(MetricError, match="takes the labels"):
        registry.inc("slas_screen_steps_total", primitive="click")
    with pytest.raises(MetricError, match="is a gauge, not a counter"):
        registry.inc("slas_breaker_open", instance="x")
    with pytest.raises(MetricError, match="only goes up"):
        registry.inc("slas_sop_exports_total", -1, lang="en")
    with pytest.raises(MetricError, match="is a counter, not a histogram"):
        registry.observe("slas_sop_exports_total", 1.0, lang="en")


def test_the_metrics_endpoint_serves_the_registry_and_health() -> None:
    registry = Registry()
    registry.inc("slas_sop_exports_total", lang="en")
    server = MetricsServer("llm-gateway", bind="127.0.0.1:0", registry=registry)
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=5) as r:
            assert r.headers["Content-Type"] == CONTENT_TYPE
            body = r.read().decode()
        assert 'slas_sop_exports_total{lang="en"} 1\n' in body
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/health", timeout=5) as r:
            assert json.loads(r.read()) == {"service": "llm-gateway", "ok": True}
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/other", timeout=5)
        assert exc.value.code == 404
    finally:
        server.stop()


# --- tracing -------------------------------------------------------------------------------------


def test_trace_ids_and_traceparent_round_trip() -> None:
    trace_id = tracing.new_trace_id()
    assert tracing.is_trace_id(trace_id) and len(trace_id) == 32
    header = tracing.format_traceparent(trace_id, "0123456789abcdef")
    assert header == f"00-{trace_id}-0123456789abcdef-01"
    assert tracing.parse_traceparent(header) == trace_id
    assert tracing.parse_traceparent("00-" + "0" * 32 + "-0123456789abcdef-01") is None
    assert tracing.parse_traceparent("garbage") is None
    with pytest.raises(ValueError, match="not a trace id"):
        tracing.format_traceparent("nope")
    headers = {"Traceparent": header, "x-slas-trace-id": "ignored"}
    assert tracing.trace_id_from_headers(headers) == trace_id
    assert tracing.trace_id_from_headers({"X-Slas-Trace-Id": trace_id.upper()}) == trace_id
    assert tracing.trace_id_from_headers({"X-Slas-Trace-Id": "short"}) is None
    assert tracing.trace_id_from_headers({}) is None


def test_the_context_binding_is_scoped_and_outbound_headers_carry_it() -> None:
    assert tracing.current_trace_id() is None
    with tracing.trace() as outer:
        assert tracing.current_trace_id() == outer
        assert tracing.ensure_trace_id() == outer
        out = tracing.outbound_headers()
        assert out["X-Slas-Trace-Id"] == outer
        assert tracing.parse_traceparent(out["traceparent"]) == outer
        with tracing.trace(outer) as same:
            assert same == outer
        with tracing.trace("a" * 32) as inner:
            assert inner == "a" * 32 and tracing.current_trace_id() == inner
        assert tracing.current_trace_id() == outer
    assert tracing.current_trace_id() is None
    accepted = tracing.accept({"traceparent": tracing.format_traceparent("b" * 32)})
    try:
        assert accepted == "b" * 32 and tracing.current_trace_id() == "b" * 32
        minted = tracing.accept({})
        assert minted != "b" * 32 and tracing.is_trace_id(minted)
    finally:
        tracing.unbind(tracing.bind(tracing.new_trace_id()))
        tracing._current.set(None)


# --- events --------------------------------------------------------------------------------------


def test_events_are_json_lines_with_the_trace_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sink = ListSink()
    log = EventLog(
        "api",
        sink,
        clock=lambda: datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
        redact=lambda text: text.replace("hunter2", "[redacted]"),
    )
    with tracing.trace("c" * 32):
        record = log.info("request", path="/tickets", note="password hunter2")
    assert record["trace_id"] == "c" * 32
    assert sink.records() == [
        {
            "ts": "2026-09-14T10:00:00+00:00",
            "level": "info",
            "service": "api",
            "event": "request",
            "trace_id": "c" * 32,
            "path": "/tickets",
            "note": "password [redacted]",
        }
    ]
    log.warning("slow", ms=1200)
    log.error("failed", code=500)
    log.debug("noise")
    assert [r["level"] for r in sink.records()] == ["info", "warning", "error", "debug"]
    assert trace_ids(sink.records()) == {"c" * 32, None}
    with pytest.raises(ValueError, match="reserved"):
        log.info("x", service="other")

    file_sink = FileSink(tmp_path / "events" / "api.jsonl")
    EventLog("api", file_sink).info("started")
    assert json.loads((tmp_path / "events" / "api.jsonl").read_text())["event"] == "started"

    class Stream:
        def __init__(self) -> None:
            self.text = ""

        def write(self, text: str) -> None:
            self.text += text

        def flush(self) -> None:
            pass

    stream = Stream()
    EventLog("api", StreamSink(stream)).info("hello")  # type: ignore[arg-type]
    assert stream.text.endswith("\n") and '"event": "hello"' in stream.text


# --- yamlish -------------------------------------------------------------------------------------


def test_the_yaml_emitter_quotes_what_yaml_would_misread() -> None:
    assert yamlish.scalar("plain-token_1") == "plain-token_1"
    assert yamlish.scalar("yes") == '"yes"'
    assert yamlish.scalar("12") == '"12"'
    assert yamlish.scalar("has: colon") == '"has: colon"'
    assert yamlish.scalar('up{job="vllm"} == 0') == '"up{job=\\"vllm\\"} == 0"'
    assert yamlish.scalar(True) == "true" and yamlish.scalar(None) == "null"
    assert yamlish.scalar(1.5) == "1.5"
    text = yamlish.dump(
        {
            "global": {"scrape_interval": "15s"},
            "empty_map": {},
            "empty_list": [],
            "jobs": [{"job_name": "a", "targets": ["x:1", "y:2"]}, "loose"],
            "nested": [[1, 2], {"k": {"deep": "v"}}],
        }
    )
    assert text == (
        "global:\n"
        "  scrape_interval: 15s\n"
        "empty_map: {}\n"
        "empty_list: []\n"
        "jobs:\n"
        "  - job_name: a\n"
        "    targets:\n"
        "      - x:1\n"
        "      - y:2\n"
        "  - loose\n"
        "nested:\n"
        "  - - 1\n"
        "    - 2\n"
        "  - k:\n"
        "      deep: v\n"
    )


def test_module_level_helpers_write_to_the_process_registry() -> None:
    metrics.REGISTRY.reset()
    metrics.inc("slas_sop_exports_total", lang="en")
    metrics.set_gauge("slas_sandbox_sessions_open", 3)
    metrics.add_gauge("slas_sandbox_sessions_open", -1)
    metrics.observe("slas_step_seconds", 2.0, agent="coding")
    text = metrics.render()
    assert "slas_sandbox_sessions_open 2\n" in text
    assert 'slas_sop_exports_total{lang="en"} 1\n' in text
    assert 'slas_step_seconds_count{agent="coding"} 1\n' in text
