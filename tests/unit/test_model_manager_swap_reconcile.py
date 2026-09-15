"""Blue/green swap with smoke test and 24 h rollback; reconciliation against the registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from slas_kernel.clock import FakeClock
from slas_llm_gateway.routing import RoleRouter
from slas_model_manager.reconcile import plan_reconcile, sentence
from slas_model_manager.registry import example_registry
from slas_model_manager.runtime import FakeRuntime, vllm_spec
from slas_model_manager.swap import FakeSmokeTester, SwapError, SwapManager

REGISTRY = example_registry()
IMAGE = "registry.internal/vllm/vllm-openai:v0.6.3"
START = datetime(2026, 9, 14, 9, 14, tzinfo=UTC)
CODER = REGISTRY.model("qwen2.5-coder-32b-awq")
CANDIDATE = REGISTRY.model("deepseek-v3-fp8").model_copy(update={"roles": ["planner", "coder"]})


def manager(
    clock: FakeClock | None = None,
) -> tuple[SwapManager, FakeRuntime, FakeSmokeTester, RoleRouter]:
    runtime = FakeRuntime()
    smoke = FakeSmokeTester()
    router = RoleRouter(REGISTRY.routes())
    swap = SwapManager(
        runtime=runtime,
        smoke=smoke,
        router=router,
        clock=clock or FakeClock(START, step=timedelta(seconds=0)),
        image=IMAGE,
    )
    incumbent = runtime.start(vllm_spec(CODER, name="vllm-coder", gpu_ids=[0], image=IMAGE))
    swap.register_serving("coder", CODER, incumbent)
    return swap, runtime, smoke, router


def test_happy_swap_starts_alongside_smokes_switches_and_drains() -> None:
    swap, runtime, smoke, router = manager()
    assert swap.status("coder") == "Qwen2.5-Coder-32B is serving coder."
    record = swap.swap("coder", CANDIDATE, gpu_ids=[1])
    assert record.phase == "done"
    assert runtime.started == ["vllm-coder", "vllm-coder-deepseek-v3-fp8"]
    assert runtime.stopped == ["vllm-coder"], "the incumbent is drained only after the switch"
    assert smoke.asked == ["vllm-coder-deepseek-v3-fp8"]
    assert router.instance_for("coder") == "vllm-coder-deepseek-v3-fp8"
    assert record.rollback_until == START + timedelta(hours=24)
    assert record.progress == [
        "Starting DeepSeek-V3 alongside the current coder (Qwen2.5-Coder-32B)…",
        "Smoke-testing DeepSeek-V3…",
        "Smoke test passed: 3 of 3 prompts answered.",
        "Qwen2.5-Coder-32B was drained and stopped.",
        "coder is now served by DeepSeek-V3. Qwen2.5-Coder-32B can be restored until "
        "2026-09-15 09:14.",
    ]
    assert swap.status("coder") == record.progress[-1]
    served = swap.serving("coder")
    assert served is not None and served[0].id == CANDIDATE.id


def test_failed_smoke_test_stops_the_candidate_and_keeps_the_route() -> None:
    swap, runtime, smoke, router = manager()
    smoke.script("vllm-coder-deepseek-v3-fp8", False, "1 of 3 prompts timed out.")
    record = swap.swap("coder", CANDIDATE, gpu_ids=[1])
    assert record.phase == "failed"
    assert router.instance_for("coder") == "vllm-coder"
    assert runtime.stopped == ["vllm-coder-deepseek-v3-fp8"]
    assert record.sentence() == (
        "Smoke test failed: 1 of 3 prompts timed out. DeepSeek-V3 was stopped and "
        "Qwen2.5-Coder-32B keeps serving coder."
    )
    with pytest.raises(SwapError, match="no completed swap"):
        swap.rollback("coder")


def test_unhealthy_candidate_is_stopped_before_the_smoke_test() -> None:
    swap, runtime, smoke, router = manager()
    runtime.set_health("vllm-coder-deepseek-v3-fp8", False)
    record = swap.swap("coder", CANDIDATE, gpu_ids=[1])
    assert record.phase == "failed" and smoke.asked == []
    assert "did not become healthy" in record.sentence()
    assert "slas logs vllm-coder-deepseek-v3-fp8" in record.sentence()
    assert router.instance_for("coder") == "vllm-coder"


def test_rollback_within_24_hours_restores_the_incumbent() -> None:
    clock = FakeClock(START, step=timedelta(seconds=0))
    swap, runtime, _, router = manager(clock)
    swap.swap("coder", CANDIDATE, gpu_ids=[1])
    clock._now = START + timedelta(hours=23, minutes=59)  # the test controls time
    record = swap.rollback("coder")
    assert record.phase == "rolled_back"
    assert router.instance_for("coder") == "vllm-coder"
    assert (
        runtime.started[-1] == "vllm-coder" and runtime.stopped[-1] == "vllm-coder-deepseek-v3-fp8"
    )
    assert record.sentence() == (
        "Rolled back: Qwen2.5-Coder-32B is serving coder again and DeepSeek-V3 was stopped."
    )
    served = swap.serving("coder")
    assert served is not None and served[0].id == CODER.id


def test_rollback_after_24_hours_is_refused_with_three_parts() -> None:
    clock = FakeClock(START, step=timedelta(seconds=0))
    swap, _, _, router = manager(clock)
    swap.swap("coder", CANDIDATE, gpu_ids=[1])
    clock._now = START + timedelta(hours=24, minutes=1)
    with pytest.raises(SwapError) as raised:
        swap.rollback("coder")
    assert raised.value.message.what_happened == (
        "The rollback window for coder closed at 2026-09-15 09:14."
    )
    assert raised.value.message.likely_cause == "Rollbacks are kept for 24 hours after a swap."
    assert swap.records["coder"].phase == "expired"
    assert router.instance_for("coder") == "vllm-coder-deepseek-v3-fp8"


def test_swap_refuses_undeclared_roles_and_a_first_assignment_has_no_rollback() -> None:
    swap, _, _, router = manager()
    with pytest.raises(SwapError) as raised:
        swap.swap("coder", REGISTRY.model("bge-m3"), gpu_ids=[0])
    assert raised.value.message.what_happened == "BGE-M3 cannot serve coder."
    assert swap.status("rerank") == "Nothing is serving rerank."
    first = swap.swap("rerank", REGISTRY.model("bge-reranker-v2-m3"), gpu_ids=[0])
    assert first.phase == "done" and first.incumbent is None
    assert first.progress[-1].endswith("There was no previous model to roll back to.")
    assert router.instance_for("rerank") == "vllm-rerank-bge-reranker-v2-m3"
    with pytest.raises(SwapError, match="nothing to roll back to"):
        swap.rollback("rerank")


def test_reconcile_plans_starts_stops_and_keeps() -> None:
    runtime = FakeRuntime()
    runtime.start(vllm_spec(CODER, name="vllm-coder", gpu_ids=[0], image=IMAGE))
    runtime.start(vllm_spec(REGISTRY.model("bge-m3"), name="vllm-embed", gpu_ids=[0], image=IMAGE))
    runtime.start(
        vllm_spec(REGISTRY.model("bge-m3"), name="vllm-triage", gpu_ids=[0], image=IMAGE)
    )  # wrong model
    runtime.start(vllm_spec(REGISTRY.model("bge-m3"), name="vllm-orphan", gpu_ids=[0], image=IMAGE))
    runtime.start(
        vllm_spec(CANDIDATE, name="vllm-coder-deepseek-v3-fp8", gpu_ids=[1], image=IMAGE)
    )  # swap in flight
    actions = plan_reconcile(REGISTRY, runtime.running())
    by_name = {(a.kind, a.name) for a in actions}
    assert ("keep", "vllm-coder") in by_name and ("keep", "vllm-embed") in by_name
    assert ("stop", "vllm-triage") in by_name and ("start", "vllm-triage") in by_name
    assert ("start", "vllm-planner") in by_name and ("start", "vllm-rerank") in by_name
    assert ("start", "vllm-voter-kimi-k2-awq") in by_name
    assert ("stop", "vllm-orphan") in by_name
    assert not any(a.name == "vllm-coder-deepseek-v3-fp8" for a in actions), (
        "swap candidates are left alone"
    )
    text = sentence(actions)
    assert (
        text.startswith("To match the registry: start ") and "stop vllm-triage, vllm-orphan" in text
    )
    assert sentence([a for a in actions if a.kind == "keep"]) == (
        "Every model instance matches the registry; nothing to do."
    )
