"""The one abstract class an agent implements (CLAUDE.md §5.1, ADR-0001).

Everything else — tickets, journal, log collection, RCA, SOP rendering, cross-check, skill
expansion, approvals, exports — is kernel code and is forbidden in agent modules.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from slas_schemas.common import AgentName
from slas_schemas.job import Job, MesTicket, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.sop import SopTemplate
from slas_schemas.ticket import Observation, StepVerdict


@runtime_checkable
class Agent(Protocol):
    name: AgentName

    def ingest(self, raw: Upload | MesTicket) -> Job: ...

    def plan(self, job: Job) -> Plan: ...

    def verify(self, step: Step, obs: Observation) -> StepVerdict: ...

    def sop_template(self) -> SopTemplate: ...
