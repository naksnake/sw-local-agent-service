"""The Consensus Router: rules, tally sentences, budget, and the gateway fan-out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from slas_kernel.clock import FakeClock
from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.consensus import (
    DEFAULT_CONSENSUS,
    ConsensusConfigError,
    TokenBudget,
    UnknownDecisionError,
    default_rules,
    rules_from_mapping,
    tally,
)
from slas_llm_gateway.gateway import VOTER_INSTRUCTION, Gateway
from slas_llm_gateway.redaction import default_redactor
from slas_llm_gateway.routing import NoInstanceForRoleError, RoleRouter, Routes
from slas_llm_gateway.vllm import FakeVllm, Message
from slas_schemas.vote import Vote

RULES = default_rules()
START = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
VOTERS = ["vllm-voter-qwen", "vllm-voter-deepseek", "vllm-voter-kimi"]


def vote(voter: str, verdict: str = "approve", reason: str = "looks right", **fields: str) -> Vote:
    return Vote(voter=voter, verdict=verdict, fields=fields, reason=reason, confidence=0.8)


# --- rules -----------------------------------------------------------------------------------


def test_default_rules_match_the_table_in_section_5_3() -> None:
    assert list(RULES.decisions) == [
        "plan_approval",
        "code_change",
        "ticket_diagnosis",
        "factory_pass",
        "rca_conclusion",
    ]
    assert all(rule.voters == 3 for rule in RULES.decisions.values())
    assert RULES.rule("plan_approval").rule == "unanimous"
    assert RULES.rule("code_change").rule == "majority"
    assert RULES.rule("ticket_diagnosis").fields == ["owner", "severity", "root_cause"]
    assert RULES.rule("factory_pass").if_not_met == "The line lead decides."
    assert RULES.token_budget_pct == 5.0
    with pytest.raises(UnknownDecisionError) as raised:
        RULES.rule("vibes")
    assert raised.value.message.what_happened == "There is no cross-check rule called vibes."


def test_rules_validate_with_three_parts() -> None:
    broken = {
        "version": 1,
        "decisions": {
            "x": {
                "decision": "x",
                "description": "d",
                "voters": 3,
                "rule": "majority_per_field",
                "approve_phrase": "agree",
                "if_not_met": "Your call.",
            }
        },
    }
    with pytest.raises(ConsensusConfigError) as raised:
        rules_from_mapping(broken, source="c.yaml")
    assert (
        raised.value.message.what_happened == "The cross-check rules in c.yaml could not be used."
    )
    code_change = RULES.rule("code_change").model_dump()
    mismatched = {"version": 1, "decisions": {"a": code_change}}
    with pytest.raises(ConsensusConfigError) as mismatch:
        rules_from_mapping(mismatched)
    assert "does not match" in mismatch.value.message.likely_cause
    assert isinstance(DEFAULT_CONSENSUS["decisions"], dict)


# --- tally -----------------------------------------------------------------------------------


def test_unanimous_plan_approval() -> None:
    rule = RULES.rule("plan_approval")
    verdict = tally(rule, [vote(v) for v in VOTERS])
    assert verdict.agreed and not verdict.degraded
    assert verdict.sentence == "3 of 3 approve the plan."
    dissent = tally(
        rule,
        [
            vote(VOTERS[0]),
            vote(VOTERS[1]),
            vote(VOTERS[2], "concern", "cycle count is above the cap"),
        ],
    )
    assert not dissent.agreed
    assert dissent.sentence == (
        "2 of 3 approve the plan. vllm-voter-kimi has a concern: cycle count is above the cap. "
        "You decide; the concerns are shown above."
    )
    assert dissent.concerns == ["vllm-voter-kimi has a concern: cycle count is above the cap"]


def test_majority_code_change_surfaces_every_concern_even_when_agreed() -> None:
    rule = RULES.rule("code_change")
    verdict = tally(
        rule, [vote(VOTERS[0]), vote(VOTERS[1]), vote(VOTERS[2], "concern", "missing a test")]
    )
    assert verdict.agreed
    assert (
        verdict.sentence
        == "2 of 3 approve the change. vllm-voter-kimi has a concern: missing a test."
    )
    rejected = tally(
        rule,
        [
            vote(VOTERS[0]),
            vote(VOTERS[1], "reject", "breaks the build"),
            vote(VOTERS[2], "reject", "unsafe"),
        ],
    )
    assert not rejected.agreed
    assert rejected.sentence.endswith("Address each concern or dismiss it with a note.")
    assert "vllm-voter-deepseek rejects it: breaks the build" in rejected.sentence


def test_majority_per_field_marks_undecided_fields_your_call() -> None:
    rule = RULES.rule("ticket_diagnosis")
    votes = [
        vote(VOTERS[0], owner="EE", severity="S2", root_cause="PCIe retimer firmware"),
        vote(VOTERS[1], owner="EE", severity="S2", root_cause="PCIe retimer firmware"),
        vote(VOTERS[2], owner="EE", severity="S1", root_cause="riser seating"),
    ]
    verdict = tally(rule, votes)
    assert verdict.agreed, "every field has a majority of the panel"
    assert verdict.undecided_fields == []
    assert verdict.sentence == (
        "3 of 3 agree the owner is EE. 2 of 3 agree the severity is S2. "
        "2 of 3 agree the root cause is PCIe retimer firmware."
    )
    split = tally(
        rule,
        [
            vote(VOTERS[0], owner="EE", severity="S2", root_cause="a"),
            vote(VOTERS[1], owner="EE", severity="S1", root_cause="b"),
            vote(VOTERS[2], owner="EE", severity="S3", root_cause="c"),
        ],
    )
    assert not split.agreed
    assert split.undecided_fields == ["severity", "root_cause"]
    assert split.sentence.startswith(
        "3 of 3 agree the owner is EE. Severity: 1 says S2, 1 says S1, 1 says S3. Your call."
    )


def test_majority_per_field_agrees_when_every_field_has_a_majority() -> None:
    rule = RULES.rule("ticket_diagnosis")
    verdict = tally(
        rule,
        [
            vote(VOTERS[0], owner="EE", severity="S2", root_cause="x"),
            vote(VOTERS[1], owner="EE", severity="S2", root_cause="x"),
            vote(VOTERS[2], owner="ME", severity="S2", root_cause="x"),
        ],
    )
    assert verdict.agreed
    assert verdict.sentence == (
        "2 of 3 agree the owner is EE. 3 of 3 agree the severity is S2. "
        "3 of 3 agree the root cause is x."
    )


def test_factory_pass_requires_all_three_and_otherwise_the_line_lead_decides() -> None:
    rule = RULES.rule("factory_pass")
    assert tally(rule, [vote(v) for v in VOTERS]).sentence == "3 of 3 say PASS."
    verdict = tally(
        rule, [vote(VOTERS[0]), vote(VOTERS[1]), vote(VOTERS[2], "reject", "fan 3 stalled")]
    )
    assert not verdict.agreed
    assert verdict.sentence == (
        "2 of 3 say PASS. vllm-voter-kimi rejects it: fan 3 stalled. The line lead decides."
    )


def test_missing_voters_degrade_the_verdict_and_are_named() -> None:
    rule = RULES.rule("rca_conclusion")
    verdict = tally(rule, [vote(VOTERS[0]), vote(VOTERS[1])], unavailable=[VOTERS[2]])
    assert verdict.agreed, "2 of a panel of 3 is still a majority"
    assert verdict.degraded
    assert verdict.sentence == (
        "2 of 2 agree with the conclusion. vllm-voter-kimi didn't answer and is paused. "
        "Only 2 of 3 voters answered; treat this as a weaker check."
    )
    alone = tally(RULES.rule("plan_approval"), [vote(VOTERS[0])], unavailable=VOTERS[1:])
    assert not alone.agreed, "unanimity needs the whole panel"
    empty = tally(rule, [], unavailable=VOTERS)
    assert not empty.agreed and empty.degraded
    assert empty.sentence.startswith(
        "0 of 0 agree with the conclusion. The conclusion is marked uncertain"
    )


# --- budget -----------------------------------------------------------------------------------


def test_token_budget_is_five_percent_per_day_and_rolls_over() -> None:
    clock = FakeClock(START, step=timedelta(seconds=0))
    budget = TokenBudget(clock, daily_tokens=1_000_000, pct=5.0)
    assert budget.cap == 50_000
    assert budget.can_afford(50_000) and not budget.can_afford(50_001)
    budget.charge(30_000)
    assert budget.remaining() == 20_000
    assert (
        budget.sentence()
        == "Cross-checks have used 30,000 of 50,000 tokens today (5% of the daily allowance)."
    )
    clock._now = START + timedelta(days=1)  # the test controls time
    assert budget.spent_today() == 0 and budget.remaining() == 50_000
    with pytest.raises(ValueError, match="positive"):
        TokenBudget(clock, daily_tokens=0)


# --- gateway fan-out -------------------------------------------------------------------------


def make_gateway(
    vllm: FakeVllm, *, daily_tokens: int = 1_000_000, alerts: list[str] | None = None
) -> Gateway:
    clock = FakeClock(START, step=timedelta(seconds=0))
    return Gateway(
        client=vllm,
        router=RoleRouter(
            Routes(roles={"coder": "vllm-coder", "triage": "vllm-triage"}, voters=VOTERS)
        ),
        redactor=default_redactor(),
        breaker=CircuitBreaker(clock, failure_threshold=2),
        rules=RULES,
        budget=TokenBudget(clock, daily_tokens=daily_tokens),
        alert=alerts.append if alerts is not None else None,
    )


def vote_json(verdict: str = "approve", reason: str = "fine", **fields: str) -> dict[str, object]:
    return {
        "voter": "self-reported",
        "verdict": verdict,
        "fields": fields,
        "reason": reason,
        "confidence": 0.9,
    }


def test_cross_check_asks_every_voter_the_same_redacted_evidence_and_nothing_else() -> None:
    vllm = FakeVllm()
    for voter in VOTERS:
        vllm.script_json(voter, vote_json())
    gateway = make_gateway(vllm)
    evidence = [Message(role="user", content="Plan: 25 DC cycles. BMC password=Sup3rSecret!")]
    verdict = gateway.cross_check("plan_approval", evidence)

    assert verdict.agreed and verdict.sentence == "3 of 3 approve the plan."
    assert [v.voter for v in verdict.votes] == VOTERS, (
        "voter names come from the router, not the model"
    )
    assert [r.instance for r in vllm.requests] == VOTERS
    for sent in vllm.requests:
        assert sent.messages[0].content == VOTER_INSTRUCTION
        assert (
            sent.messages[1].content
            == "Plan: 25 DC cycles. BMC password=[redacted:password_assignment]"
        )
        assert len(sent.messages) == 2, "no voter sees another voter's answer"
        assert sent.guided_json == Vote.model_json_schema()
    assert gateway.budget.spent_today() > 0
    assert len(gateway.redactions) == 1, "the evidence is redacted once, then shared"


def test_a_voter_that_fails_schema_twice_is_paused_and_the_verdict_is_degraded() -> None:
    vllm = FakeVllm()
    vllm.script_json(VOTERS[0], vote_json())
    vllm.violate_schema(VOTERS[1], times=3)
    vllm.script_json(VOTERS[2], vote_json())
    gateway = make_gateway(vllm)
    verdict = gateway.cross_check("code_change", [Message(role="user", content="diff…")])
    assert verdict.agreed and verdict.degraded
    assert verdict.unavailable_voters == [VOTERS[1]]
    assert gateway.breaker.state(VOTERS[1]).value == "open"
    assert "vllm-voter-deepseek didn't answer and is paused." in verdict.sentence
    assert len(vllm.requests_for(VOTERS[1])) == 2


def test_over_budget_degrades_to_one_model_flags_it_and_alerts_instead_of_blocking() -> None:
    vllm = FakeVllm()
    vllm.script_json(VOTERS[0], vote_json())
    alerts: list[str] = []
    gateway = make_gateway(vllm, daily_tokens=1000, alerts=alerts)  # cap = 50 tokens
    verdict = gateway.cross_check("rca_conclusion", [Message(role="user", content="logs")])
    assert verdict.degraded and not verdict.agreed
    assert verdict.sentence.startswith(
        "Checked by one model only: today's cross-check budget is used up. "
        "1 of 1 agree with the conclusion."
    )
    assert len(vllm.requests) == 1
    assert alerts and alerts[0].startswith(
        "Cross-check budget for today is used up; rca_conclusion"
    )


def test_cross_check_with_no_voters_configured_is_degraded_not_an_error() -> None:
    vllm = FakeVllm()
    gateway = make_gateway(vllm)
    gateway.router = RoleRouter(Routes(roles={}, voters=[]))
    verdict = gateway.cross_check("plan_approval", [Message(role="user", content="x")])
    assert verdict.degraded and not verdict.agreed and verdict.votes == []


def test_complete_and_generate_route_by_role_and_redact() -> None:
    vllm = FakeVllm()
    vllm.script("vllm-coder", "print('hi')")
    vllm.script_json("vllm-triage", vote_json("concern", "odd"))
    gateway = make_gateway(vllm)
    response = gateway.complete(
        "coder", [Message(role="user", content="token=abcdef123456 write hi")]
    )
    assert response.text == "print('hi')" and response.instance == "vllm-coder"
    assert vllm.requests[0].messages[0].content == "token=[redacted:password_assignment] write hi"
    assert vllm.requests[0].guided_json is None
    result = gateway.generate("triage", [Message(role="user", content="judge")], Vote)
    assert result.value.verdict == "concern" and result.instance == "vllm-triage"
    with pytest.raises(NoInstanceForRoleError) as raised:
        gateway.complete("embed", [Message(role="user", content="x")])
    assert raised.value.message.what_happened == "No model is serving the embed role."


def test_router_switch_records_history_and_rejects_unknown_roles() -> None:
    router = RoleRouter(Routes(roles={"coder": "vllm-coder"}, voters=[]))
    assert router.switch("coder", "vllm-coder-new") == "vllm-coder"
    assert router.instance_for("coder") == "vllm-coder-new"
    assert router.switch("planner", "vllm-planner") is None
    assert router.history == [
        ("coder", "vllm-coder", "vllm-coder-new"),
        ("planner", None, "vllm-planner"),
    ]
    with pytest.raises(ValueError, match="not a role"):
        router.switch("dj", "x")
    with pytest.raises(ValueError, match="unknown roles"):
        Routes(roles={"dj": "x"})
