"""Schemas: ticket ids, the state machine, plan validation, findings, votes, JSON Schema."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from slas_schemas.finding import Finding
from slas_schemas.ids import make_ticket_id, parse_ticket_id
from slas_schemas.job import Job
from slas_schemas.plan import MAX_STEPS, Plan, Step
from slas_schemas.ticket import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransitionError,
    Ticket,
    TicketState,
    can_transition,
)
from slas_schemas.vote import ConsensusVerdict, Vote

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


def job() -> Job:
    return Job(id="job-1", agent="null", user="pat", title="A job", created_at=NOW)


def steps(count: int, *, risk: str = "safe") -> list[Step]:
    return [
        Step(id=f"s{i}", n=i, primitive="fake", title=f"Step {i}", risk=risk)
        for i in range(1, count + 1)
    ]


def test_ticket_ids_round_trip_and_reject_junk() -> None:
    assert make_ticket_id("coding", 7) == "T-coding-0007"
    assert make_ticket_id("null", 12345) == "T-null-12345"
    assert parse_ticket_id("T-validation-0042") == ("validation", 42)
    with pytest.raises(ValueError, match="start at 1"):
        make_ticket_id("factory", 0)
    for bad in ("T-robot-0001", "coding-0001", "T-coding-1", ""):
        with pytest.raises(ValueError, match="not a ticket id"):
            parse_ticket_id(bad)
    with pytest.raises(ValidationError):
        Ticket(
            id="T-nope-0001",
            agent="null",
            user="p",
            title="t",
            job=job(),
            created_at=NOW,
            updated_at=NOW,
        )


def test_state_machine_matches_section_5_4() -> None:
    order = [
        TicketState.OPEN,
        TicketState.PLANNED,
        TicketState.APPROVED,
        TicketState.RUNNING,
        TicketState.ANALYSING,
        TicketState.NEEDS_REVIEW,
        TicketState.DONE,
    ]
    for current, following in itertools.pairwise(order):
        assert can_transition(current, following), (current, following)
    for state in TicketState:
        if state not in TERMINAL_STATES:
            assert can_transition(state, TicketState.FAILED), state
    assert can_transition(TicketState.ANALYSING, TicketState.DONE)
    assert not can_transition(TicketState.OPEN, TicketState.RUNNING)
    assert not can_transition(TicketState.DONE, TicketState.OPEN)
    assert TRANSITIONS[TicketState.DONE] == frozenset() == TRANSITIONS[TicketState.FAILED]


def test_ticket_transition_records_history_and_refuses_illegal_moves() -> None:
    ticket = Ticket(
        id="T-null-0001",
        agent="null",
        user="pat",
        title="t",
        job=job(),
        created_at=NOW,
        updated_at=NOW,
    )
    change = ticket.transition(TicketState.PLANNED, NOW, "planned")
    assert change.from_state is TicketState.OPEN and change.to_state is TicketState.PLANNED
    assert ticket.history == [change]
    assert ticket.state is TicketState.PLANNED
    with pytest.raises(IllegalTransitionError) as raised:
        ticket.transition(TicketState.DONE, NOW, "skip ahead")
    assert str(raised.value) == (
        "T-null-0001 is Planned and cannot become Done; from Planned it can only become "
        "Approved, Failed."
    )
    assert ticket.state is TicketState.PLANNED, "a refused transition changes nothing"
    assert ticket.sentence() == "T-null-0001 is planned."


def test_plan_validation() -> None:
    plan = Plan(id="p", job_id="job-1", summary="s", steps=steps(3), created_at=NOW)
    assert plan.sentence() == "3 steps, none destructive."
    assert plan.step("s2").n == 2
    with pytest.raises(KeyError):
        plan.step("nope")
    with pytest.raises(ValidationError, match="step ids must be unique; repeated: s1"):
        Plan(id="p", job_id="j", summary="s", steps=[steps(1)[0], steps(1)[0]], created_at=NOW)
    misnumbered = steps(2)
    misnumbered[1] = misnumbered[1].model_copy(update={"n": 5})
    with pytest.raises(ValidationError, match="numbered 5 but is the 2th step"):
        Plan(id="p", job_id="j", summary="s", steps=misnumbered, created_at=NOW)
    with pytest.raises(ValidationError):
        Plan(id="p", job_id="j", summary="s", steps=[], created_at=NOW)
    with pytest.raises(ValidationError):
        Plan(id="p", job_id="j", summary="s", steps=steps(MAX_STEPS + 1), created_at=NOW)


def test_destructive_steps_are_named_in_the_plan_sentence() -> None:
    mixed = steps(3)
    mixed[1] = mixed[1].model_copy(update={"risk": "destructive", "title": "AC cycle"})
    plan = Plan(id="p", job_id="j", summary="s", steps=mixed, created_at=NOW)
    assert plan.destructive
    assert plan.sentence() == "3 steps; 1 needs your approval: AC cycle."
    assert [s.id for s in plan.destructive_steps()] == ["s2"]
    assert mixed[1].needs_approval and not mixed[0].needs_approval


def test_unknown_fields_are_rejected_everywhere() -> None:
    with pytest.raises(ValidationError):
        Step(id="s1", n=1, primitive="fake", title="t", bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Vote(voter="a", verdict="approve", reason="r", confidence=0.5, extra="x")  # type: ignore[call-arg]


def test_finding_headline_follows_section_10_2() -> None:
    finding = Finding(
        id="f1",
        fingerprint="0123456789abcdef",
        issue="PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle",
        owner="EE",
    )
    assert finding.headline() == (
        "[Issue] PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle | [Owner] EE"
    )
    assert (
        Finding(id="f2", fingerprint="0123456789abcdef", issue="x")
        .headline()
        .endswith("| [Owner] unassigned")
    )


def test_vote_schema_is_fixed() -> None:
    vote = Vote(voter="qwen", verdict="concern", fields={"owner": "EE"}, reason="r", confidence=0.7)
    verdict = ConsensusVerdict(
        decision="rca",
        rule="majority",
        votes=[vote],
        agreed=False,
        sentence="1 of 1 has a concern.",
    )
    assert verdict.model_dump()["votes"][0]["verdict"] == "concern"
    with pytest.raises(ValidationError):
        Vote(voter="qwen", verdict="maybe", reason="r", confidence=0.7)
    with pytest.raises(ValidationError):
        Vote(voter="qwen", verdict="approve", reason="r", confidence=1.5)


def test_models_export_json_schema() -> None:
    schema = Ticket.model_json_schema()
    assert schema["title"] == "Ticket"
    assert "Plan" in schema["$defs"] and "Rca" in schema["$defs"]
    assert set(Plan.model_json_schema()["required"]) >= {"id", "job_id", "steps"}
