"""NullAgent: five fake steps that exercise the whole kernel lifecycle (DEVELOPMENT_PLAN P2).

It implements exactly the four methods of `Agent` and nothing else — no tickets, no
journal, no logs, no RCA, no SOP rendering (CLAUDE.md §5.1).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from slas_schemas.common import AgentName
from slas_schemas.job import InputRef, Job, MesTicket, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.sop import SopTemplate
from slas_schemas.ticket import Observation, StepVerdict


class NullAgent:
    name: AgentName = "null"

    def ingest(self, raw: Upload | MesTicket) -> Job:
        if isinstance(raw, Upload):
            title = f"Rehearsal for {raw.filename}"
            user = raw.uploaded_by
            digest = hashlib.sha256((raw.content or raw.filename).encode("utf-8")).hexdigest()
            inputs = [
                InputRef(
                    name=raw.filename,
                    kind="upload",
                    path=raw.path,
                    sha256=digest,
                    size_bytes=raw.size_bytes,
                )
            ]
        else:
            title = f"Rehearsal for MES ticket {raw.ticket_no}"
            user = raw.requested_by
            digest = hashlib.sha256(raw.ticket_no.encode("utf-8")).hexdigest()
            inputs = [InputRef(name=raw.ticket_no, kind="mes_ticket", sha256=digest)]
        return Job(
            id=f"job-{digest[:12]}",
            agent=self.name,
            user=user,
            title=title,
            inputs=inputs,
            created_at=datetime.now(UTC),
        )

    def plan(self, job: Job) -> Plan:
        names = ", ".join(ref.name for ref in job.inputs) or "nothing"
        scripted = [
            ("s1", "Say hello", {"stdout": "hello from the null agent\n"}),
            ("s2", "List the inputs", {"stdout": f"inputs: {names}\n"}),
            ("s3", "Count to three", {"stdout": "1\n2\n3\n"}),
            ("s4", "Warn on stderr", {"stderr": "warning: this is only a rehearsal\n"}),
            ("s5", "Finish", {"stdout": "done\n"}),
        ]
        steps = [
            Step(id=step_id, n=index, primitive="fake", title=title, args=args)
            for index, (step_id, title, args) in enumerate(scripted, start=1)
        ]
        return Plan(
            id=f"plan-{job.id}",
            job_id=job.id,
            summary="Five fake steps that produce output and change nothing.",
            steps=steps,
            created_at=datetime.now(UTC),
        )

    def verify(self, step: Step, obs: Observation) -> StepVerdict:
        if obs.exit_code == 0:
            return StepVerdict(outcome="ok", sentence=f"{step.title} finished as expected.")
        return StepVerdict(
            outcome="fail",
            sentence=f"{step.title} exited with code {obs.exit_code}.",
        )

    def sop_template(self) -> SopTemplate:
        return SopTemplate(
            agent=self.name,
            kind="placeholder",
            purpose="Rehearse the kernel lifecycle end to end without touching any machine.",
            checks=["Every step has an observation.", "The journal has one observation per step."],
        )
