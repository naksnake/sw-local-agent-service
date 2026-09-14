"""`slas status` against a scripted host: services, GPUs, models, work, leases, alerts."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slas_cli.cli import EXIT_OK, EXIT_PROBLEMS, main
from slas_cli.doctor.fakes import FakeHost
from slas_cli.doctor.host import CommandResult
from slas_cli.status import collect_status
from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.kernel import Kernel
from slas_kernel.leases import LeaseTable
from slas_kernel.null_agent import NullAgent
from slas_kernel.store import FileTicketStore
from slas_model_manager.registry import EXAMPLE_REGISTRY, REGISTRY_FILE_HEADER, render_registry_yaml
from slas_observability.alerts import LocalAlertChannel
from slas_schemas.job import Upload
from slas_schemas.ticket import TicketState

DATA_ROOT = "/AI/Agent"
COMPOSE = "/opt/slas/compose/docker-compose.yml"
NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)

PS_LINES = "\n".join(
    json.dumps(item)
    for item in [
        {
            "Service": "api",
            "State": "running",
            "Health": "healthy",
            "Status": "Up 2 hours (healthy)",
        },
        {"Service": "llm-gateway", "State": "running", "Health": "", "Status": "Up 2 hours"},
        {
            "Service": "prometheus",
            "State": "exited",
            "Health": "",
            "Status": "Exited (1) 3 minutes ago",
        },
        {
            "Service": "grafana",
            "State": "running",
            "Health": "unhealthy",
            "Status": "Up 2 hours (unhealthy)",
        },
    ]
)
NVIDIA = (
    "0, NVIDIA H100 80GB HBM3, 62464, 81559, 54, 12\n"
    "1, NVIDIA H100 80GB HBM3, 10240, 81559, 41, 0\n"
)


class Clock:
    def now(self) -> datetime:
        return NOW


def platform_files(tmp_path: Path) -> dict[str, str]:
    """Tickets, leases and alerts written by the real code, read back by the fake host."""
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=FakeExecutor(),
        store=FileTicketStore(tmp_path),
        clock=FakeClock(NOW, step=timedelta(0)),
    )
    done = kernel.run(Upload(filename="a.md", uploaded_by="lee", content="#", size_bytes=1))
    running = kernel.run(Upload(filename="b.md", uploaded_by="lee", content="#", size_bytes=1))
    running.state = TicketState.RUNNING
    FileTicketStore(tmp_path).save(running)
    review = kernel.run(Upload(filename="c.md", uploaded_by="lee", content="#", size_bytes=1))
    review.state = TicketState.NEEDS_REVIEW
    FileTicketStore(tmp_path).save(review)
    LeaseTable(tmp_path / "Validation" / "leases.json").acquire(
        "lab-gx8-01", ticket_id=running.id, user="lee", now=NOW, max_hours=8
    )
    channel = LocalAlertChannel(tmp_path / "Alerts" / "alerts.json", clock=Clock())
    channel.raise_(
        "circuit_breaker_open",
        "warning",
        "vllm-coder is paused until 10:05 after 2 invalid answers.",
        instance="vllm-coder",
    )
    acknowledged = channel.raise_(
        "consensus_disagreement", "warning", "Voters split.", decision="rca"
    )
    channel.acknowledge(acknowledged.id, by="lee")
    files: dict[str, str] = {}
    for path in tmp_path.rglob("*.json"):
        files[f"{DATA_ROOT}/{path.relative_to(tmp_path)}"] = path.read_text(encoding="utf-8")
    files[f"{DATA_ROOT}/Models/models.yaml"] = render_registry_yaml(
        EXAMPLE_REGISTRY, header=REGISTRY_FILE_HEADER
    )
    assert done.state.value == "Done"
    return files


def host_with(tmp_path: Path, *, compose: bool = True, gpus: bool = True) -> FakeHost:
    host = FakeHost.healthy(DATA_ROOT)
    if compose:
        host.existing_paths.add(COMPOSE)
        host.outputs[("docker", "compose", "-f", COMPOSE, "ps", "--all", "--format", "json")] = (
            CommandResult(0, PS_LINES + "\n")
        )
    if gpus:
        host.outputs[
            (
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            )
        ] = CommandResult(0, NVIDIA)
    else:
        del host.commands["nvidia-smi"]
    host.files.update(platform_files(tmp_path))
    return host


def run(host: FakeHost, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(
        ["status", "--data-root", DATA_ROOT, "--compose-file", COMPOSE, *args],
        host=host,
        stdout=out,
        environ={},
    )
    return code, out.getvalue()


def test_status_says_what_runs_what_works_and_what_needs_a_person(tmp_path: Path) -> None:
    code, out = run(host_with(tmp_path))
    assert code == EXIT_PROBLEMS, out
    lines = out.splitlines()
    assert lines[0] == "SW Local Agent Service — status"
    assert lines[1] == "Data root: /AI/Agent"
    assert lines[3] == (
        "Services: 2 of 4 running; grafana unhealthy (Up 2 hours (unhealthy)); "
        "prometheus Exited (1) 3 minutes ago."
    )
    assert lines[4] == (
        "GPUs: gpu 0 NVIDIA H100 80GB HBM3 — 61 of 80 GiB used, 54 °C, 12 % busy · "
        "gpu 1 NVIDIA H100 80GB HBM3 — 10 of 80 GiB used, 41 °C, 0 % busy."
    )
    assert lines[5] == (
        "Models: coder → qwen2.5-coder-32b-awq · planner → deepseek-v3-fp8 · "
        "triage → qwen2.5-7b-awq · embed → bge-m3 · rerank → bge-reranker-v2-m3. "
        "Voters: qwen2.5-coder-32b-awq, "
        "deepseek-v3-fp8, kimi-k2-awq."
    )
    assert lines[6] == (
        "Work: 1 ticket in progress — T-null-0002 (Running, updated 10:00). "
        "1 waits for review — T-null-0003."
    )
    assert lines[7] == "Leases: lab-gx8-01 is leased to T-null-0002 (lee) until 2026-09-14 18:00."
    assert lines[8] == (
        "Alerts: 1 open; the newest: vllm-coder is paused until 10:05 after 2 invalid answers."
    )
    assert lines[10] == (
        "Summary: 3 things need attention: grafana is not running; prometheus is not running; "
        "1 alert is open."
    )


def test_status_json_and_a_quiet_host_exit_zero(tmp_path: Path) -> None:
    host = host_with(tmp_path)
    quiet_ps = "\n".join(
        json.dumps({"Service": s, "State": "running", "Health": "healthy", "Status": "Up"})
        for s in ("api", "prometheus")
    )
    host.outputs[("docker", "compose", "-f", COMPOSE, "ps", "--all", "--format", "json")] = (
        CommandResult(0, f"[{','.join(quiet_ps.splitlines())}]")
    )
    del host.files[f"{DATA_ROOT}/Alerts/alerts.json"]
    code, out = run(host, "--json")
    assert code == EXIT_OK, out
    document = json.loads(out)
    assert document["ok"] is True and document["problems"] == []
    assert [s["name"] for s in document["services"]] == ["api", "prometheus"]
    assert document["sentences"]["alerts"] == "Alerts: none open."
    assert document["sentences"]["summary"] == (
        "Summary: everything is running and nothing needs attention."
    )
    assert document["roles"]["coder"] == "qwen2.5-coder-32b-awq"
    assert document["gpus"][0]["temperature_c"] == 54


def test_status_without_compose_gpus_or_data_root_says_so(tmp_path: Path) -> None:
    host = host_with(tmp_path, compose=False, gpus=False)
    host.directories.discard(DATA_ROOT)
    report = collect_status(host, data_root=DATA_ROOT, compose_file=COMPOSE)
    assert report.services_sentence() == (
        f"Services: unknown — {COMPOSE} is not on this host or docker compose did not answer."
    )
    assert report.gpus_sentence() == "GPUs: nvidia-smi did not answer on this host."
    assert report.problems[:2] == [
        "the data root /AI/Agent is not a directory on this host",
        "the platform's services could not be listed",
    ]
    empty = FakeHost.healthy(DATA_ROOT)
    del empty.commands["docker"]
    bare = collect_status(empty, data_root=DATA_ROOT, compose_file=COMPOSE)
    assert (
        bare.models_sentence()
        == "Models: Models/models.yaml has no roles yet; open the Models page."
    )
    assert bare.work_sentence() == "Work: no ticket is in progress and none waits for review."
    assert bare.leases_sentence() == "Leases: no target or station is held."
    assert bare.gpus_sentence() == "GPUs: none visible." or bare.gpus_known


def test_status_is_read_only_on_the_host(tmp_path: Path) -> None:
    host = host_with(tmp_path)
    run(host)
    programs = {call[0] for call in host.calls}
    assert programs == {"docker", "nvidia-smi"}
    assert all("ps" in call or "--query-gpu" in " ".join(call) for call in host.calls)
