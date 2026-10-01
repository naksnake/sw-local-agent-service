"""Wire shapes of the Validation and Factory routes (docs/api-contract-round-2.md §5).

One source for the orchestrator, the api's pass-through and the WebUI's `camelCase` views:
every key is `snake_case`, every status and verdict is a plain word, and every row carries a
sentence a person can read (CLAUDE.md §9). Fields the contract lists come first; the fields
the WebUI already draws (`approved`, `params`, `finding_details`, `screenshots`, …) follow
them so nothing the pages show has to be re-derived in the browser.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.job import MesTicket
from slas_schemas.vote import ConsensusVerdict

CycleStatus = Literal["waiting", "running", "ok", "finding", "failed", "skipped"]
CellStatus = Literal["waiting", "running", "ok", "failed", "skipped"]
Verdict = Literal["PASS", "FAIL", "line_lead"]

# --- Validation ------------------------------------------------------------------------------


class ParseSuiteRequest(SlasModel):
    filename: str = Field(min_length=1)
    text: str | None = None
    #: An `.xlsx` workbook, base64; written to a temp file for `parse_suite_xlsx`.
    content_base64: str | None = None


class SuiteItemView(SlasModel):
    n: int = Field(ge=1)
    title: str
    action: str
    cycles: int = Field(ge=1)
    #: Needs a per-run human approval (INV-7): AC cycle, firmware flash, erase, BIOS, RAID.
    destructive: bool
    sentence: str
    #: The suite author flagged the destructive step as approved; without it the plan is refused.
    approved: bool = False
    params: dict[str, str] = Field(default_factory=dict)


class SuiteView(SlasModel):
    source: str
    title: str
    items: list[SuiteItemView] = Field(default_factory=list)
    sentence: str
    #: The three parts in one sentence when the suite could not be read; items is then empty.
    problem: str | None = None


class TargetView(SlasModel):
    ref: str
    model: str = "unknown"
    free: bool
    holder: str | None = None
    armed: bool = False
    sentence: str = ""


class PreviewRequest(SlasModel):
    suite: SuiteView
    target: str = Field(min_length=1)


class PlanStepView(SlasModel):
    id: str
    title: str
    destructive: bool


class PlanPreview(SlasModel):
    sentence: str
    steps: list[PlanStepView] = Field(default_factory=list)
    #: Titles of the steps that will ask for approval before the run starts.
    destructive_steps: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    #: The Consensus Router's verdict on the plan; input to your decision, never the decision.
    cross_check: ConsensusVerdict | None = None
    step_count: int = Field(ge=0)
    cycle_count: int = Field(ge=0)


class StartRunRequest(SlasModel):
    suite: SuiteView
    target: str = Field(min_length=1)


class CycleCellView(SlasModel):
    n: int = Field(ge=1)
    kind: str
    status: CycleStatus = "waiting"
    sentence: str = ""


class FindingRef(SlasModel):
    sentence: str
    owner: str
    ticket_id: str | None = None


class RunView(SlasModel):
    ticket_id: str
    title: str
    target: str
    state: str
    sentence: str
    #: Step titles still waiting for a person; empty once the run may act.
    pending_approvals: list[str] = Field(default_factory=list)
    cells: list[CycleCellView] = Field(default_factory=list)
    console_tail: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    votes: list[str] = Field(default_factory=list)
    finding_details: list[FindingRef] = Field(default_factory=list)
    created_at: str


# --- Factory ---------------------------------------------------------------------------------


class ParseLabelRequest(SlasModel):
    text: str


class LabelParse(SlasModel):
    """`{"trigger": MesTicket}` when the label names a unit and a station, else `{"problem"}`."""

    trigger: MesTicket | None = None
    problem: str | None = None


class StationView(SlasModel):
    name: str
    description: str = ""
    free: bool
    holder: str | None = None
    enrolled: bool = False
    sentence: str = ""


class TemplateView(SlasModel):
    id: str
    name: str
    description: str = ""
    steps: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    sentence: str


class JobRules(SlasModel):
    """The wizard's third step. Voters, on-fail behaviour and SOP export are fixed today
    (CLAUDE.md §5.3, §10.3); only the station backup is a choice."""

    voters: int = 3
    on_fail: str = "hold"
    export_sop: bool = True
    backup_station: bool = True


class StartJobRequest(SlasModel):
    trigger: MesTicket
    template_id: str = Field(min_length=1)
    rules: JobRules = Field(default_factory=JobRules)


class DecideRequest(SlasModel):
    verdict: Literal["PASS", "FAIL"]
    note: str = ""


class ControlRequest(SlasModel):
    verb: Literal["pause", "resume", "abort", "status"]


class StepCellView(SlasModel):
    n: int = Field(ge=1)
    title: str
    status: CellStatus
    sentence: str = ""
    #: The last screenshot of the step (paths under Factory/Jobs/<ticket>/screens).
    screenshot: str | None = None
    screenshots: list[str] = Field(default_factory=list)


class JobView(SlasModel):
    ticket_id: str
    title: str
    station: str
    unit_sn: str
    state: str
    sentence: str
    cells: list[StepCellView] = Field(default_factory=list)
    verdict: Verdict | None = None
    votes: list[str] = Field(default_factory=list)
    held: bool = False
    mes_ticket_no: str = ""
    verdict_sentence: str = ""
    decided_by: str = ""
    #: The child ticket drafted for the line lead, when the unit did not pass.
    draft_ticket_id: str | None = None
    backup_path: str | None = None
    rules_sentence: str = ""
    created_at: str


class ControlView(SlasModel):
    sentence: str
    watch_url: str | None = None
    watch_problem: str | None = None
    station: str = ""
    paused: bool = False
    aborted: bool = False
    by: str = ""


def dump(model: SlasModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
