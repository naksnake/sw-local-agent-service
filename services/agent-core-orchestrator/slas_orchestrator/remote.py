"""The executors as the kernel sees them across a container boundary (ADR-0015 §4).

`HttpExecutor` implements `slas_kernel.executor.Executor` by `POST /v1/execute` on the
validation-executor or the factory-executor; the kernel does not know the step left the
process. INV-3 holds on both sides: the model never sees the executor, the executor never
sees the model. `ExecutorReader` reads the executor's own state (the LED cycle map, the
test-step map, targets, stations, templates, pending MES tickets) and carries the two
operator verbs, `control` and `decide`, for the Factory page.

Nothing here retries: a step with a physical effect must not run twice because a socket
blinked (INV-6); `ServiceClient` already turns that into one three-part 503 the kernel
journals as the step's failure.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from slas_factory_executor.executor import JobState
from slas_http.client import LONG_TIMEOUT_S, ServiceClient
from slas_http.errors import ServiceError
from slas_http.identity import Identity
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_schemas.common import SlasModel
from slas_schemas.job import MesTicket
from slas_schemas.plan import Step
from slas_schemas.ticket import Observation
from slas_station_runner.protocol import BatchResult
from slas_validation_executor.executor import RunState

ControlVerb = Literal["pause", "resume", "abort", "status"]


class RunStateAnswer(SlasModel):
    """`GET /v1/runs/{id}` on the validation-executor: the cycle map plus the console tail."""

    state: RunState
    console_tail: list[str] = Field(default_factory=list)


class ControlAnswer(SlasModel):
    """`POST /v1/jobs/{id}/control` on the factory-executor: the runner's answer plus where
    the operator can watch (or why not)."""

    result: BatchResult
    watch_url: str | None = None
    watch_problem: str | None = None


def _is_unknown_primitive(error: ServiceError) -> bool:
    return error.status == 400 and "primitive" in error.message.what_happened.lower()


class HttpExecutor:
    """`Executor` over HTTP: one `POST /v1/execute` per step, with the long timeout because a
    single step may wait for a server to boot and settle."""

    def __init__(self, client: ServiceClient, *, timeout_s: float = LONG_TIMEOUT_S) -> None:
        self.client = client
        self.timeout_s = timeout_s

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        body = {
            "step": step.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
        }
        try:
            answer = self.client.post("/v1/execute", body, timeout_s=self.timeout_s)
        except ServiceError as exc:
            if _is_unknown_primitive(exc):
                raise UnknownPrimitiveError(step) from exc
            raise
        return Observation.model_validate(answer)


class ExecutorReader:
    """Reads and operator verbs against one executor service (contract §6)."""

    def __init__(self, client: ServiceClient) -> None:
        self.client = client

    # --- helpers ---------------------------------------------------------------------------

    def _get_or_none(self, path: str) -> Any:
        try:
            return self.client.get(path)
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def _rows(answer: Any) -> list[dict[str, Any]]:
        if not isinstance(answer, list):
            return []
        return [row for row in answer if isinstance(row, dict)]

    # --- validation-executor ---------------------------------------------------------------

    def run_state(self, ticket_id: str) -> RunStateAnswer | None:
        """The cycle map and console tail of one run, or None when the executor has not
        performed a step of it yet (a run still waiting for approval, for instance)."""
        answer = self._get_or_none(f"/v1/runs/{ticket_id}")
        if not isinstance(answer, dict):
            return None
        payload = dict(answer)
        tail = payload.pop("console_tail", [])
        return RunStateAnswer(
            state=RunState.model_validate(payload),
            console_tail=[str(line) for line in tail] if isinstance(tail, list) else [],
        )

    def list_runs(self) -> dict[str, RunState]:
        """Every run the executor knows, by ticket id."""
        states: dict[str, RunState] = {}
        for row in self._rows(self.client.get("/v1/runs")):
            raw = row.get("state")
            if isinstance(raw, dict):
                state = RunState.model_validate(raw)
                states[str(row.get("ticket_id", state.ticket_id))] = state
        return states

    def targets(self) -> list[dict[str, Any]]:
        return self._rows(self.client.get("/v1/targets"))

    # --- factory-executor ------------------------------------------------------------------

    def job_state(self, ticket_id: str) -> JobState | None:
        answer = self._get_or_none(f"/v1/jobs/{ticket_id}")
        if not isinstance(answer, dict):
            return None
        return JobState.model_validate(answer)

    def list_jobs(self) -> dict[str, JobState]:
        states: dict[str, JobState] = {}
        for row in self._rows(self.client.get("/v1/jobs")):
            raw = row.get("state", row)
            if isinstance(raw, dict) and "ticket_id" in raw:
                state = JobState.model_validate(raw)
                states[state.ticket_id] = state
        return states

    def stations(self) -> list[dict[str, Any]]:
        return self._rows(self.client.get("/v1/stations"))

    def templates(self) -> list[dict[str, Any]]:
        return self._rows(self.client.get("/v1/templates"))

    def mes_pending(self) -> list[MesTicket]:
        """Production tickets the MES dropped and nobody has started yet."""
        return [
            MesTicket.model_validate(row) for row in self._rows(self.client.get("/v1/mes/pending"))
        ]

    def control(
        self, ticket_id: str, verb: ControlVerb, *, by: str, identity: Identity | None = None
    ) -> ControlAnswer:
        answer = self.client.post(
            f"/v1/jobs/{ticket_id}/control", {"verb": verb, "by": by}, identity=identity
        )
        payload = dict(answer) if isinstance(answer, dict) else {}
        watch_url = payload.pop("watch_url", None)
        watch_problem = payload.pop("watch_problem", None)
        return ControlAnswer(
            result=BatchResult.model_validate(payload),
            watch_url=str(watch_url) if watch_url else None,
            watch_problem=str(watch_problem) if watch_problem else None,
        )

    def decide(
        self,
        ticket_id: str,
        *,
        verdict: Literal["PASS", "FAIL"],
        by: str,
        note: str,
        identity: Identity | None = None,
    ) -> JobState:
        answer = self.client.post(
            f"/v1/jobs/{ticket_id}/decide",
            {"verdict": verdict, "by": by, "note": note},
            identity=identity,
        )
        return JobState.model_validate(answer)
