"""The Coding Agent: a thin specialisation of the kernel (CLAUDE.md §5.1, §10.1).

Exactly the four `Agent` methods plus what the wizard needs before the kernel runs
(`propose`, `approve`). Tickets, journal, logs, RCA, SOP, cross-check and exports stay in
the kernel and in the kernel-side `CodingExecutor`.

    INGEST  plan.md → PlanDocument → Job (target: a sandbox reference)
    PLAN    approved Breakdown + toolchain resolution → steps for the deterministic executor
            step 1 records the toolchain choice on the ticket ("first feed line")
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from slas_orchestrator.coding.breakdown import Breakdown, Breakdowner, propose
from slas_orchestrator.coding.plan_doc import PlanDocument, PlanError, parse_plan
from slas_sandbox_manager.manager import slugify
from slas_sandbox_manager.toolchains import Manifest, default_manifest
from slas_schemas.common import AgentName
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import InputRef, Job, MesTicket, TargetRef, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.sop import SopTemplate
from slas_schemas.ticket import Observation, StepVerdict

PRIMITIVES = ("toolchain", "sandbox_open", "iterate", "commit", "export_zip", "cross_check")


class BreakdownNotApprovedError(RuntimeError):
    def __init__(self, job: Job) -> None:
        self.message = ThreePartMessage(
            f"{job.title} has no approved breakdown.",
            "The Coding Agent only plans from a breakdown a person reviewed in the wizard.",
            "Open the task in the Coding page, review the proposed steps and press Start task.",
        )
        super().__init__(self.message.what_happened)


class CodingAgent:
    name: AgentName = "coding"

    def __init__(
        self,
        *,
        manifest: Manifest | None = None,
        breakdowner: Breakdowner | None = None,
        now: Any = None,
    ) -> None:
        self.manifest = manifest or default_manifest()
        self.breakdowner = breakdowner
        self._now = now or (lambda: datetime.now(UTC))
        self._plans: dict[str, PlanDocument] = {}
        self._approved: dict[str, Breakdown] = {}

    # --- wizard -----------------------------------------------------------------------

    def ingest(self, raw: Upload | MesTicket) -> Job:
        if not isinstance(raw, Upload):
            raise PlanError(
                ThreePartMessage(
                    "The Coding Agent does not take MES tickets.",
                    "It starts from a plan file; production tickets go to the Factory Agent.",
                    "Upload a plan.md in the New coding task wizard.",
                )
            )
        if raw.content is None:
            raise PlanError(
                ThreePartMessage(
                    f"{raw.filename} arrived without its content.",
                    "The wizard sends the plan text with the upload.",
                    "Drop or paste the plan again.",
                )
            )
        plan = parse_plan(raw.content, filename=raw.filename)
        self._plans[plan.job_id] = plan
        return Job(
            id=plan.job_id,
            agent=self.name,
            user=raw.uploaded_by,
            title=plan.title,
            inputs=[
                InputRef(
                    name=raw.filename,
                    kind="upload",
                    path=raw.path,
                    sha256=plan.sha256,
                    size_bytes=len(raw.content.encode("utf-8")),
                )
            ],
            target=TargetRef(kind="sandbox", ref=f"sandbox:{slugify(plan.title)}"),
            created_at=self._now(),
        )

    def plan_document(self, job_id: str) -> PlanDocument:
        return self._plans[job_id]

    def propose(self, job: Job) -> Breakdown:
        """The breakdown the wizard shows for editing; nothing runs yet."""
        return propose(self._plans[job.id], breakdowner=self.breakdowner)

    def approve(self, job_id: str, breakdown: Breakdown) -> Breakdown:
        """Record the person's edited breakdown; `plan()` uses exactly this."""
        for choice in breakdown.languages:
            self.manifest.versions(choice.language)  # unknown languages fail here, early
        self._approved[job_id] = breakdown
        return breakdown

    # --- Agent ------------------------------------------------------------------------

    def plan(self, job: Job) -> Plan:
        breakdown = self._approved.get(job.id)
        if breakdown is None:
            raise BreakdownNotApprovedError(job)
        resolutions = breakdown.resolutions(self.manifest)
        primary = resolutions[0]
        slug = slugify(breakdown.title)
        steps: list[Step] = [
            Step(
                id="toolchain",
                n=1,
                primitive="toolchain",
                title=breakdown.toolchain_sentence(self.manifest)[:200],
                args={
                    "resolutions": [r.to_record() for r in resolutions],
                    "sentence": breakdown.toolchain_sentence(self.manifest),
                },
            ),
            Step(
                id="sandbox",
                n=2,
                primitive="sandbox_open",
                title=f"Open an isolated sandbox for {slug}",
                args={
                    "slug": slug,
                    "image": primary.image,
                    "language": primary.language,
                    "isolation": breakdown.isolation,
                },
            ),
        ]
        # One check per kind, in the order a person expects them: `slas-check` in the
        # image runs the right tool for the sandbox's language, so kinds never repeat.
        order = ("lint", "type", "build", "test", "validate")
        by_kind = {c.kind: c for r in resolutions for c in r.checks}
        checks = [
            {"kind": c.kind, "argv": list(c.argv), "description": c.description}
            for c in sorted(by_kind.values(), key=lambda c: order.index(c.kind))
        ]
        for task in breakdown.tasks:
            steps.append(
                Step(
                    id=f"task-{task.n}",
                    n=len(steps) + 1,
                    primitive="iterate",
                    title=f"Task {task.n}: {task.title}"[:200],
                    args={
                        "task": task.model_dump(),
                        "checks": checks,
                        "max_iterations": breakdown.max_iterations,
                        "languages": [r.language for r in resolutions],
                    },
                )
            )
        steps.append(
            Step(
                id="commit",
                n=len(steps) + 1,
                primitive="commit",
                title="Commit the changes on the agent's branch",
                args={
                    "subject": breakdown.title[:72],
                    "body": "\n".join(f"- {t.title}" for t in breakdown.tasks),
                },
            )
        )
        steps.append(
            Step(
                id="export",
                n=len(steps) + 1,
                primitive="export_zip",
                title="Export a ZIP of the project",
                args={"slug": slug},
            )
        )
        if breakdown.cross_check:
            steps.append(
                Step(
                    id="cross-check",
                    n=len(steps) + 1,
                    primitive="cross_check",
                    title="Cross-check the final diff with 3 voters",
                    args={"decision": "code_change"},
                )
            )
        return Plan(
            id=f"plan-{job.id}",
            job_id=job.id,
            summary=f"{breakdown.sentence(self.manifest)}"[:2000],
            steps=steps,
            created_at=self._now(),
        )

    def verify(self, step: Step, obs: Observation) -> StepVerdict:
        sentence = obs.summary or f"{step.title} finished."
        if obs.exit_code == 0:
            return StepVerdict(outcome="ok", sentence=sentence[:2000] or "Done.")
        return StepVerdict(outcome="fail", sentence=sentence[:2000] or f"{step.title} failed.")

    def sop_template(self) -> SopTemplate:
        return SopTemplate(
            agent=self.name,
            kind="code_walkthrough",
            purpose=(
                "Walk through what the Coding Agent changed, file by file, and how to verify "
                "it in the sandbox."
            ),
            checks=[
                "Every task's checks passed in the sandbox.",
                "The commits carry Slas-Agent and Slas-Ticket trailers.",
                "The final diff was cross-checked by 3 voters.",
                "The ZIP export matches the branch.",
            ],
        )
