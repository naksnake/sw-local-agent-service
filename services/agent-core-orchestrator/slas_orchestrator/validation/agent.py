"""The Validation Agent: the four `Agent` methods plus what the wizard needs (§5.1, §10.2).

INGEST   suite.md / suite.xlsx → Suite (items, destructive ones flagged)
TARGET   the wizard picks a free server; credentials come from the vault, never here
PLAN     compile → plan.yaml; destructive steps become kernel approvals (INV-7);
         the kernel sends the plan to the Consensus Router (unanimous, §5.3)
VERIFY   an observation's exit code decides; findings ride on the observation
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from slas_orchestrator.validation.compiler import Compiler, compile_suite, write_plan
from slas_orchestrator.validation.suite import Suite, SuiteError, parse_suite_md, parse_suite_xlsx
from slas_schemas.common import AgentName
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import InputRef, Job, MesTicket, TargetRef, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.sop import SopTemplate
from slas_schemas.ticket import Observation, StepVerdict
from slas_validation_executor.guardrails import Guardrails, default_guardrails


class TargetNotChosenError(RuntimeError):
    def __init__(self, job: Job) -> None:
        self.message = ThreePartMessage(
            f"{job.title} has no target yet.",
            "A validation run works on one server, chosen in the wizard's second step.",
            "Pick a free server in the New validation run wizard, then approve and start.",
        )
        super().__init__(self.message.what_happened)


class ValidationAgent:
    name: AgentName = "validation"

    def __init__(
        self,
        *,
        guardrails: Guardrails | None = None,
        compiler: Compiler | None = None,
        plans_dir: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.guardrails = guardrails or default_guardrails()
        self.compiler = compiler
        self.plans_dir = plans_dir
        self._now = now or (lambda: datetime.now(UTC))
        self._suites: dict[str, Suite] = {}
        self._targets: dict[str, str] = {}

    # --- wizard -----------------------------------------------------------------------

    def ingest(self, raw: Upload | MesTicket) -> Job:
        if not isinstance(raw, Upload):
            raise SuiteError(
                ThreePartMessage(
                    "The Validation Agent does not take MES tickets.",
                    "It starts from a suite file; production tickets go to the Factory Agent.",
                    "Upload a suite.md or suite.xlsx in the New validation run wizard.",
                )
            )
        if raw.filename.lower().endswith(".xlsx"):
            if not raw.path:
                raise SuiteError(
                    ThreePartMessage(
                        f"{raw.filename} arrived without its file.",
                        "The wizard stores the workbook and sends its path.",
                        "Upload the workbook again.",
                    )
                )
            suite = parse_suite_xlsx(Path(raw.path))
        else:
            if raw.content is None:
                raise SuiteError(
                    ThreePartMessage(
                        f"{raw.filename} arrived without its content.",
                        "The wizard sends the suite text with the upload.",
                        "Upload the suite again.",
                    )
                )
            suite = parse_suite_md(raw.content, source=raw.filename)
        digest = (
            __import__("hashlib")
            .sha256(f"{suite.source}|{suite.model_dump_json()}".encode())
            .hexdigest()
        )
        job_id = f"job-{digest[:12]}"
        self._suites[job_id] = suite
        chosen = self._targets.get(job_id)
        return Job(
            id=job_id,
            agent=self.name,
            user=raw.uploaded_by,
            title=suite.title,
            target=TargetRef(kind="server", ref=chosen) if chosen else None,
            inputs=[
                InputRef(
                    name=raw.filename,
                    kind="upload",
                    path=raw.path,
                    sha256=digest,
                    size_bytes=raw.size_bytes,
                )
            ],
            created_at=self._now(),
        )

    def suite(self, job_id: str) -> Suite:
        return self._suites[job_id]

    def choose_target(self, job: Job, target: str) -> Job:
        self._targets[job.id] = target
        return job.model_copy(update={"target": TargetRef(kind="server", ref=target)})

    def destructive_items(self, job_id: str) -> list[str]:
        from slas_hal.primitives import approval_kind
        from slas_orchestrator.validation.compiler import map_action

        flagged: list[str] = []
        for item in self._suites[job_id].items:
            compiled = map_action(item)
            if compiled is not None and approval_kind(compiled.primitive, dict(compiled.args)):
                flagged.append(item.sentence())
        return flagged

    # --- Agent ------------------------------------------------------------------------

    def plan(self, job: Job) -> Plan:
        target = self._targets.get(job.id) or (job.target.ref if job.target else None)
        if not target:
            raise TargetNotChosenError(job)
        plan = compile_suite(
            self._suites[job.id],
            job_id=job.id,
            target=target,
            guardrails=self.guardrails,
            now=self._now(),
            compiler=self.compiler,
        )
        if self.plans_dir is not None:
            write_plan(plan, self.plans_dir)
        return plan

    def verify(self, step: Step, obs: Observation) -> StepVerdict:
        sentence = obs.summary or f"{step.title} finished."
        if obs.exit_code == 0:
            return StepVerdict(outcome="ok", sentence=sentence[:2000])
        return StepVerdict(outcome="fail", sentence=sentence[:2000])

    def sop_template(self) -> SopTemplate:
        return SopTemplate(
            agent=self.name,
            kind="verification",
            purpose=(
                "Verify that the target survives the suite's power cycles and stress without "
                "losing devices, link width, link speed or firmware, and without new errors."
            ),
            checks=[
                "The baseline was recorded before the first power action.",
                "Every cycle has a fence marker in the console and syslog before the power action.",
                "Every VERIFY compared device counts, PCIe width and speed, firmware, "
                "AER/EDAC/MCE/Xid and the SEL against the baseline.",
                "Destructive steps were approved for this run before they ran.",
            ],
        )
