"""`slas status` (CLAUDE.md §3, P11): one page of sentences about the platform on this host.

    Services   docker compose ps on the platform's compose file
    GPUs       nvidia-smi: name, memory, temperature, utilisation
    Models     the roles and voters from Models/models.yaml
    Work       tickets in progress and waiting for review, from Tickets/
    Leases     which lab targets and stations are held, from the executors' lease files
    Alerts     the local alert channel (Alerts/alerts.json)

Read-only, through the same `Host` abstraction as `slas doctor`, so it is tested against a
scripted host and never touches the machine in tests. Exit code 1 when something needs a
person: a service not running, an open alert, or a data root that cannot be read.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import Field

from slas_cli.doctor.host import Host
from slas_kernel.branding import PRODUCT_NAME
from slas_kernel.leases import Lease
from slas_observability.alerts import Alert
from slas_schemas.common import SlasModel
from slas_schemas.ticket import Ticket, TicketState

IN_PROGRESS: Final = frozenset(
    {
        TicketState.OPEN,
        TicketState.PLANNED,
        TicketState.APPROVED,
        TicketState.RUNNING,
        TicketState.ANALYSING,
    }
)

GPU_QUERY: Final = "--query-gpu=index,name,memory.used,memory.total,temperature.gpu,utilization.gpu"


class ServiceState(SlasModel):
    name: str
    state: str
    health: str = ""
    status: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running" and self.health in ("", "healthy")

    def sentence(self) -> str:
        detail = self.status or self.state
        if self.health and self.health != "healthy":
            detail = f"{self.health} ({detail})"
        return f"{self.name} {detail}"


class GpuState(SlasModel):
    index: int
    name: str
    memory_used_mib: int
    memory_total_mib: int
    temperature_c: int
    utilisation_pct: int

    def sentence(self) -> str:
        used = self.memory_used_mib / 1024
        total = self.memory_total_mib / 1024
        return (
            f"gpu {self.index} {self.name} — {used:.0f} of {total:.0f} GiB used, "
            f"{self.temperature_c} °C, {self.utilisation_pct} % busy"
        )


class TicketLine(SlasModel):
    id: str
    agent: str
    state: str
    title: str
    updated_at: datetime

    def sentence(self) -> str:
        return f"{self.id} ({self.state}, updated {self.updated_at:%H:%M})"


class StatusReport(SlasModel):
    data_root: str
    compose_file: str
    services: list[ServiceState] = Field(default_factory=list)
    services_known: bool = False
    gpus: list[GpuState] = Field(default_factory=list)
    gpus_known: bool = False
    roles: dict[str, str] = Field(default_factory=dict)
    voters: list[str] = Field(default_factory=list)
    in_progress: list[TicketLine] = Field(default_factory=list)
    needs_review: list[TicketLine] = Field(default_factory=list)
    leases: list[Lease] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    # --- sentences ------------------------------------------------------------------------

    def services_sentence(self) -> str:
        if not self.services_known:
            return (
                f"Services: unknown — {self.compose_file} is not on this host or docker compose "
                "did not answer."
            )
        running = [s for s in self.services if s.running]
        down = [s for s in self.services if not s.running]
        head = f"Services: {len(running)} of {len(self.services)} running"
        if down:
            return head + "; " + "; ".join(s.sentence() for s in down) + "."
        return head + "."

    def gpus_sentence(self) -> str:
        if not self.gpus_known:
            return "GPUs: nvidia-smi did not answer on this host."
        if not self.gpus:
            return "GPUs: none visible."
        return "GPUs: " + " · ".join(g.sentence() for g in self.gpus) + "."

    def models_sentence(self) -> str:
        if not self.roles:
            return "Models: Models/models.yaml has no roles yet; open the Models page."
        roles = " · ".join(f"{role} → {model}" for role, model in self.roles.items())
        voters = ", ".join(self.voters) if self.voters else "none"
        return f"Models: {roles}. Voters: {voters}."

    def work_sentence(self) -> str:
        if not self.in_progress and not self.needs_review:
            return "Work: no ticket is in progress and none waits for review."
        parts: list[str] = []
        if self.in_progress:
            n = len(self.in_progress)
            parts.append(
                f"{n} {'ticket' if n == 1 else 'tickets'} in progress — "
                + "; ".join(t.sentence() for t in self.in_progress[:5])
                + (f"; and {n - 5} more" if n > 5 else "")
            )
        if self.needs_review:
            n = len(self.needs_review)
            parts.append(
                f"{n} {'waits' if n == 1 else 'wait'} for review — "
                + "; ".join(t.id for t in self.needs_review[:5])
                + (f"; and {n - 5} more" if n > 5 else "")
            )
        return "Work: " + ". ".join(parts) + "."

    def leases_sentence(self) -> str:
        if not self.leases:
            return "Leases: no target or station is held."
        return "Leases: " + " ".join(lease.sentence() for lease in self.leases)

    def alerts_sentence(self) -> str:
        open_alerts = [a for a in self.alerts if a.open]
        if not open_alerts:
            return "Alerts: none open."
        newest = max(open_alerts, key=lambda a: a.last_seen_at)
        n = len(open_alerts)
        critical = sum(1 for a in open_alerts if a.severity == "critical")
        head = f"Alerts: {n} open" + (f" ({critical} critical)" if critical else "")
        return f"{head}; the newest: {newest.sentence}"

    def summary_sentence(self) -> str:
        if not self.problems:
            return "Summary: everything is running and nothing needs attention."
        n = len(self.problems)
        return (
            f"Summary: {n} {'thing needs' if n == 1 else 'things need'} attention: "
            + "; ".join(self.problems)
            + "."
        )

    def render_text(self) -> str:
        lines = [
            f"{PRODUCT_NAME} — status",
            f"Data root: {self.data_root}",
            "",
            self.services_sentence(),
            self.gpus_sentence(),
            self.models_sentence(),
            self.work_sentence(),
            self.leases_sentence(),
            self.alerts_sentence(),
            "",
            self.summary_sentence(),
        ]
        return "\n".join(lines) + "\n"

    def render_json(self) -> str:
        document = self.model_dump(mode="json")
        document["ok"] = self.ok
        document["sentences"] = {
            "services": self.services_sentence(),
            "gpus": self.gpus_sentence(),
            "models": self.models_sentence(),
            "work": self.work_sentence(),
            "leases": self.leases_sentence(),
            "alerts": self.alerts_sentence(),
            "summary": self.summary_sentence(),
        }
        return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --- probes -----------------------------------------------------------------------------------


def _compose_services(host: Host, compose_file: str) -> tuple[list[ServiceState], bool]:
    if not host.path_exists(compose_file) or host.which("docker") is None:
        return [], False
    result = host.run(
        ["docker", "compose", "-f", compose_file, "ps", "--all", "--format", "json"],
        timeout_s=20.0,
    )
    if not result.ok:
        return [], False
    services: list[ServiceState] = []
    for item in _json_items(result.stdout):
        services.append(
            ServiceState(
                name=str(item.get("Service") or item.get("Name") or "?"),
                state=str(item.get("State") or "").lower(),
                health=str(item.get("Health") or "").lower(),
                status=str(item.get("Status") or ""),
            )
        )
    return sorted(services, key=lambda s: s.name), True


def _json_items(text: str) -> list[dict[str, Any]]:
    """`docker compose ps --format json` prints one object per line (or one array)."""
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        loaded = json.loads(stripped)
        return [item for item in loaded if isinstance(item, dict)]
    items: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        if line.strip():
            loaded = json.loads(line)
            if isinstance(loaded, dict):
                items.append(loaded)
    return items


def _gpus(host: Host) -> tuple[list[GpuState], bool]:
    if host.which("nvidia-smi") is None:
        return [], False
    result = host.run(["nvidia-smi", GPU_QUERY, "--format=csv,noheader,nounits"])
    if not result.ok:
        return [], False
    gpus: list[GpuState] = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 6:
            continue
        try:
            gpus.append(
                GpuState(
                    index=int(parts[0]),
                    name=parts[1],
                    memory_used_mib=int(float(parts[2])),
                    memory_total_mib=int(float(parts[3])),
                    temperature_c=int(float(parts[4])),
                    utilisation_pct=int(float(parts[5])),
                )
            )
        except ValueError:
            continue
    return gpus, True


def _roles_and_voters(host: Host, data_root: str) -> tuple[dict[str, str], list[str]]:
    """The `roles:` and `voters:` blocks of the rendered Models/models.yaml (its shape is fixed
    by `slas_model_manager.registry.render_registry_yaml` and kept in step by a test)."""
    text = host.read_text(f"{data_root}/Models/models.yaml")
    if text is None:
        return {}, []
    roles: dict[str, str] = {}
    voters: list[str] = []
    block = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            block = line.split(":", 1)[0].strip()
            continue
        if block == "roles" and ":" in line:
            role, _, model = line.strip().partition(":")
            roles[role.strip()] = model.strip()
        elif block == "voters" and line.strip().startswith("- "):
            voters.append(line.strip()[2:].strip())
    return roles, voters


def _tickets(host: Host, data_root: str) -> tuple[list[TicketLine], list[TicketLine]]:
    in_progress: list[TicketLine] = []
    needs_review: list[TicketLine] = []
    tickets_dir = f"{data_root}/Tickets"
    for name in host.list_dir(tickets_dir):
        if name.startswith("."):
            continue
        text = host.read_text(f"{tickets_dir}/{name}/ticket.json")
        if text is None:
            continue
        try:
            ticket = Ticket.model_validate_json(text)
        except ValueError:
            continue
        line = TicketLine(
            id=ticket.id,
            agent=ticket.agent,
            state=ticket.state.value,
            title=ticket.title,
            updated_at=ticket.updated_at,
        )
        if ticket.state in IN_PROGRESS:
            in_progress.append(line)
        elif ticket.state is TicketState.NEEDS_REVIEW:
            needs_review.append(line)
    key = lambda t: t.updated_at  # noqa: E731
    return sorted(in_progress, key=key, reverse=True), sorted(needs_review, key=key, reverse=True)


def _leases(host: Host, data_root: str) -> list[Lease]:
    leases: list[Lease] = []
    for relative in ("Validation/leases.json", "Factory/leases.json"):
        text = host.read_text(f"{data_root}/{relative}")
        if not text:
            continue
        try:
            raw = json.loads(text)
        except ValueError:
            continue
        items = raw.values() if isinstance(raw, dict) else raw
        for item in items:
            try:
                leases.append(Lease.model_validate(item))
            except ValueError:
                continue
    return sorted(leases, key=lambda lease: lease.target)


def _alerts(host: Host, data_root: str) -> list[Alert]:
    text = host.read_text(f"{data_root}/Alerts/alerts.json")
    if not text:
        return []
    try:
        raw = json.loads(text)
    except ValueError:
        return []
    alerts: list[Alert] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            alerts.append(Alert.model_validate(item))
        except ValueError:
            continue
    return alerts


def collect_status(host: Host, *, data_root: str, compose_file: str) -> StatusReport:
    report = StatusReport(data_root=data_root, compose_file=compose_file)
    if not host.is_dir(data_root):
        report.problems.append(f"the data root {data_root} is not a directory on this host")
    report.services, report.services_known = _compose_services(host, compose_file)
    report.gpus, report.gpus_known = _gpus(host)
    report.roles, report.voters = _roles_and_voters(host, data_root)
    report.in_progress, report.needs_review = _tickets(host, data_root)
    report.leases = _leases(host, data_root)
    report.alerts = _alerts(host, data_root)

    if not report.services_known:
        report.problems.append("the platform's services could not be listed")
    for service in report.services:
        if not service.running:
            report.problems.append(f"{service.name} is not running")
    open_alerts = [a for a in report.alerts if a.open]
    if open_alerts:
        n = len(open_alerts)
        report.problems.append(f"{n} {'alert is' if n == 1 else 'alerts are'} open")
    return report


def render(report: StatusReport, *, as_json: bool) -> str:
    return report.render_json() if as_json else report.render_text()


__all__: Sequence[str] = (
    "GpuState",
    "ServiceState",
    "StatusReport",
    "TicketLine",
    "collect_status",
    "render",
)
