"""The Factory Agent: the four `Agent` methods plus what the wizard and the MES need (§10.3).

TRIGGER  a production ticket from the MES adapter, a label scan, or a manual entry
PLAN     pick a test-loop template → plan (lease · steps · release); station and unit bound
VERIFY   exit code decides; the verdict step's finding is the line lead's draft ticket
SOP      production line SOP, EN + 中文, rendered by the kernel
REPORT   the verdict goes back to the MES with the ticket id and the SOP paths
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from slas_factory_executor.executor import JobState
from slas_factory_executor.mes import MesAdapter, MesVerdict
from slas_factory_executor.templates import (
    TestLoopTemplate,
    compile_template,
    default_templates,
)
from slas_orchestrator.validation.compiler import render_plan_yaml
from slas_schemas.common import AgentName
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import InputRef, Job, MesTicket, TargetRef, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.sop import SopTemplate
from slas_schemas.ticket import Observation, StepVerdict, Ticket

_LABEL = re.compile(r"(?i)\b(?:sn|serial)\s*[:=]?\s*([A-Za-z0-9-]{4,})")
_STATION = re.compile(r"(?i)\bstation\s*[:=]?\s*([a-z][a-z0-9-]*)")


class TriggerError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class FactoryAgent:
    name: AgentName = "factory"

    def __init__(
        self,
        *,
        templates: Mapping[str, TestLoopTemplate] | None = None,
        default_template: str = "final-test-9-steps",
        plans_dir: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.templates = dict(templates or default_templates())
        self.default_template = default_template
        self.plans_dir = plans_dir
        self._now = now or (lambda: datetime.now(UTC))
        self._tickets: dict[str, MesTicket] = {}
        self._chosen: dict[str, str] = {}

    # --- TRIGGER --------------------------------------------------------------------------------

    def ingest(self, raw: Upload | MesTicket) -> Job:
        mes = raw if isinstance(raw, MesTicket) else self._from_label(raw)
        digest = hashlib.sha256(f"{mes.ticket_no}|{mes.unit_sn}|{mes.station}".encode()).hexdigest()
        job_id = f"job-{digest[:12]}"
        self._tickets[job_id] = mes
        return Job(
            id=job_id,
            agent=self.name,
            user=mes.requested_by,
            title=f"Final test of {mes.unit_sn} on {mes.station}",
            inputs=[InputRef(name=f"MES {mes.ticket_no}", kind="mes_ticket", sha256=digest)],
            target=TargetRef(kind="station", ref=mes.station),
            created_at=self._now(),
        )

    def _from_label(self, upload: Upload) -> MesTicket:
        """A label scan or a manual entry: `SN <serial> station <name>` in any order."""
        text = upload.content or ""
        serial = _LABEL.search(text)
        station = _STATION.search(text)
        if serial is None or station is None:
            raise TriggerError(
                ThreePartMessage(
                    f"{upload.filename} names no unit and station.",
                    "A label scan or manual entry must carry the serial number and the station, "
                    "for example `SN SN-GX8-0100 station station-07`.",
                    "Scan the label again or type both values in the wizard.",
                )
            )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return MesTicket(
            ticket_no=f"manual-{digest}",
            station=station.group(1).lower(),
            unit_sn=serial.group(1),
            requested_by=upload.uploaded_by,
            payload={"source": upload.filename},
        )

    def mes_ticket(self, job_id: str) -> MesTicket:
        return self._tickets[job_id]

    def choose_template(self, job: Job, template_id: str) -> TestLoopTemplate:
        try:
            template = self.templates[template_id]
        except KeyError:
            raise TriggerError(
                ThreePartMessage(
                    f"There is no test-loop template called {template_id}.",
                    f"Templates on this installation: {', '.join(sorted(self.templates))}.",
                    "Pick one of those in the wizard, or add the template under Factory/Templates.",
                )
            ) from None
        self._chosen[job.id] = template_id
        return template

    # --- Agent ----------------------------------------------------------------------------

    def plan(self, job: Job) -> Plan:
        mes = self._tickets[job.id]
        template = self.templates[self._chosen.get(job.id, self.default_template)]
        plan = compile_template(
            template,
            job_id=job.id,
            station=mes.station,
            unit_sn=mes.unit_sn,
            mes_ticket_no=mes.ticket_no,
            now=self._now(),
        )
        if self.plans_dir is not None:
            path = self.plans_dir / plan.id / "plan.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, render_plan_yaml(plan), mode=0o644)
        return plan

    def verify(self, step: Step, obs: Observation) -> StepVerdict:
        sentence = obs.summary or f"{step.title} finished."
        return StepVerdict(outcome="ok" if obs.exit_code == 0 else "fail", sentence=sentence[:2000])

    def sop_template(self) -> SopTemplate:
        return SopTemplate(
            agent=self.name,
            kind="production_line",
            purpose=(
                "Take one unit through the final test on its station: power on, log in and start "
                "the vendor test, read the result, the sensors and the event log, and decide PASS "
                "only when three voters agree; anything else is the line lead's call."
            ),
            checks=[
                "The station was leased to this job before anything ran and released only "
                "after a PASS.",
                "Every GUI step has a screenshot before and after, attached to the ticket.",
                "The event log was empty and every sensor was within its limit.",
                "PASS was given by 3 of 3 voters; a FAIL or a split vote held the station for "
                "the line lead.",
                "The station state was backed up under Backups/stations.",
            ],
        )

    # --- REPORT ---------------------------------------------------------------------------

    def report_verdict(self, ticket: Ticket, state: JobState, adapter: MesAdapter) -> MesVerdict:
        mes = self._tickets.get(ticket.job.id)
        verdict = MesVerdict(
            ticket_no=mes.ticket_no if mes else state.mes_ticket_no or ticket.id,
            unit_sn=state.unit_sn or (mes.unit_sn if mes else "unknown"),
            station=state.station,
            verdict=state.verdict or "line_lead",
            ticket_id=ticket.id,
            decided_by=state.decided_by or "nobody yet",
            sentence=state.verdict_sentence or state.sentence(),
            sop_en=ticket.sop.en if ticket.sop else None,
            sop_zh=ticket.sop.zh if ticket.sop else None,
            reported_at=self._now(),
        )
        adapter.report(verdict)
        return verdict
