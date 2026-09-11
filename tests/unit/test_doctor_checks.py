"""Every preflight check against the scripted FakeHost. Nothing here touches the machine."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from slas_cli.doctor import checks
from slas_cli.doctor.checks import (
    ALL_CHECKS,
    CheckResult,
    DoctorSettings,
    describe_host,
    run_checks,
)
from slas_cli.doctor.fakes import GIB, FakeHost
from slas_cli.doctor.host import CommandResult, Host

DATA_ROOT = "/AI/Agent"
SETTINGS = DoctorSettings(data_root=DATA_ROOT)
NVIDIA_SMI_QUERY = ("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits")


def by_id(results: Sequence[CheckResult], check_id: str) -> CheckResult:
    matches = [result for result in results if result.check_id == check_id]
    assert len(matches) == 1, f"expected exactly one result for {check_id}"
    return matches[0]


def bare_host() -> FakeHost:
    """A Linux box with nothing installed and nowhere to write."""
    return FakeHost(cpus=2, memory_bytes=8 * GIB)


# --- whole report ------------------------------------------------------------------------


def test_healthy_host_passes_every_check() -> None:
    results = run_checks(FakeHost.healthy(), SETTINGS)
    assert len(results) == len(ALL_CHECKS) == 14
    assert [result.status for result in results] == ["ok"] * len(ALL_CHECKS)


def test_check_ids_are_unique_and_stable() -> None:
    ids = [result.check_id for result in run_checks(FakeHost.healthy(), SETTINGS)]
    assert len(set(ids)) == len(ids)
    assert ids[0] == "operating_system"
    assert "gpu" in ids
    assert ids[-1] == "web_port"


def test_every_summary_is_a_sentence() -> None:
    for host in (FakeHost.healthy(), bare_host()):
        for result in run_checks(host, SETTINGS):
            assert result.summary.endswith("."), result
            # Sentences start with a word, a number or a path — "gVisor" is a word too.
            assert result.summary[0].isalnum() or result.summary[0] == "/", result
            assert result.summary == result.summary.strip(), result


def test_problems_carry_three_parts_and_summary_is_what_happened() -> None:
    results = run_checks(bare_host(), SETTINGS)
    problems = [result for result in results if result.status in ("warn", "fail")]
    assert problems, "a bare host must produce problems"
    for result in problems:
        assert result.detail is not None
        assert result.detail.what_happened == result.summary
        assert result.detail.likely_cause.strip()
        assert result.detail.what_to_do.strip()


def test_ok_and_skip_results_have_no_detail() -> None:
    for result in run_checks(FakeHost.healthy(), SETTINGS):
        assert result.detail is None


def test_as_dict_is_flat_and_json_friendly() -> None:
    result = by_id(run_checks(bare_host(), SETTINGS), "gpu")
    data = result.as_dict()
    assert data["id"] == "gpu"
    assert data["status"] == "fail"
    assert data["what_to_do"] and data["likely_cause"]
    assert set(data) == {
        "id",
        "title",
        "status",
        "summary",
        "what_happened",
        "likely_cause",
        "what_to_do",
    }


def test_a_crashing_check_becomes_a_warning_line() -> None:
    def broken(host: Host, settings: DoctorSettings) -> CheckResult:
        raise RuntimeError("boom")

    broken.__name__ = "check_broken_thing"
    (result,) = run_checks(FakeHost.healthy(), SETTINGS, checks=[broken])
    assert result.status == "warn"
    assert result.check_id == "broken_thing"
    assert "could not run" in result.summary
    assert "RuntimeError: boom" in result.summary
    assert result.detail is not None
    assert "bug in the preflight" in result.detail.likely_cause


def test_describe_host_sentence() -> None:
    facts = describe_host(FakeHost.healthy())
    assert facts.sentence() == "Linux 6.8.0-45-generic on x86_64 · 64 cores · 512 GiB memory"
    unknown = describe_host(FakeHost(cpus=None, memory_bytes=None))
    assert unknown.sentence(" | ") == (
        "Linux 6.8.0-45-generic on x86_64 | core count unknown | memory size unknown"
    )


# --- operating system, cpu, memory ---------------------------------------------------------


def test_operating_system() -> None:
    assert checks.check_operating_system(FakeHost.healthy(), SETTINGS).status == "ok"
    result = checks.check_operating_system(FakeHost(system_name="Darwin"), SETTINGS)
    assert result.status == "fail"
    assert result.summary == "This host runs Darwin, not Linux."
    unknown = checks.check_operating_system(FakeHost(system_name=""), SETTINGS)
    assert "could not be identified" in unknown.summary


def test_cpu_thresholds() -> None:
    assert checks.check_cpu(FakeHost(cpus=None), SETTINGS).status == "skip"
    result = checks.check_cpu(FakeHost(cpus=4), SETTINGS)
    assert result.status == "warn"
    assert result.summary == "Only 4 processor cores were found; at least 8 are recommended."
    ok = checks.check_cpu(FakeHost(cpus=8), SETTINGS)
    assert ok.status == "ok"
    assert ok.summary == "8 processor cores (at least 8 recommended)."
    relaxed = checks.check_cpu(FakeHost(cpus=4), DoctorSettings(min_cpu_cores=2))
    assert relaxed.status == "ok"


def test_memory_thresholds() -> None:
    assert checks.check_memory(FakeHost(memory_bytes=None), SETTINGS).status == "skip"
    result = checks.check_memory(FakeHost(memory_bytes=16 * GIB), SETTINGS)
    assert result.status == "warn"
    assert result.summary.startswith("Only 16 GiB of memory was found")
    small = checks.check_memory(FakeHost(memory_bytes=int(7.5 * GIB)), SETTINGS)
    assert small.summary.startswith("Only 7.5 GiB of memory was found")
    ok = checks.check_memory(FakeHost(memory_bytes=64 * GIB), SETTINGS)
    assert ok.summary == "64 GiB of memory (at least 32 GiB recommended)."


# --- container runtime -----------------------------------------------------------------------


def test_container_runtime_ready() -> None:
    host = FakeHost.healthy()
    result = checks.check_container_runtime(host, SETTINGS)
    assert result.status == "ok"
    assert result.summary == "Docker 27.3.1 with Compose 2.29.7 is ready."
    assert ("docker", "info", "--format", "{{.ServerVersion}}") in host.calls


def test_container_runtime_docker_missing() -> None:
    host = FakeHost.healthy()
    del host.commands["docker"]
    result = checks.check_container_runtime(host, SETTINGS)
    assert result.status == "fail"
    assert result.summary == "Docker was not found."
    assert host.calls == []


def test_container_runtime_compose_missing() -> None:
    host = FakeHost.healthy()
    host.outputs[("docker", "compose", "version", "--short")] = CommandResult(1, "", "not found")
    result = checks.check_container_runtime(host, SETTINGS)
    assert result.status == "fail"
    assert "Compose plugin is not" in result.summary


def test_container_runtime_daemon_down() -> None:
    host = FakeHost.healthy()
    host.outputs[("docker", "info", "--format", "{{.ServerVersion}}")] = CommandResult(1, "")
    result = checks.check_container_runtime(host, SETTINGS)
    assert result.status == "fail"
    assert "did not answer" in result.summary
    assert result.detail is not None
    assert "docker group" in result.detail.what_to_do


def test_container_runtime_unknown_docker_version() -> None:
    host = FakeHost.healthy()
    host.outputs[("docker", "--version")] = CommandResult(0, "something odd")
    result = checks.check_container_runtime(host, SETTINGS)
    assert result.status == "ok"
    assert result.summary == "Docker of an unknown version with Compose 2.29.7 is ready."


# --- sandboxes -------------------------------------------------------------------------------


def test_sandbox_runtime() -> None:
    host = FakeHost.healthy()
    ok = checks.check_sandbox_runtime(host, SETTINGS)
    assert ok.summary == "Podman 4.9.3 is installed for the sandboxes."
    del host.commands["podman"]
    result = checks.check_sandbox_runtime(host, SETTINGS)
    assert result.status == "warn"
    assert result.detail is not None
    assert "Phase 6" in result.detail.what_to_do


def test_sandbox_runtime_without_version() -> None:
    host = FakeHost.healthy()
    host.outputs[("podman", "--version")] = CommandResult(1, "")
    assert checks.check_sandbox_runtime(host, SETTINGS).summary == (
        "Podman is installed for the sandboxes."
    )


def test_sandbox_isolation_depends_on_profile() -> None:
    host = FakeHost.healthy()
    assert checks.check_sandbox_isolation(host, SETTINGS).status == "ok"
    del host.commands["runsc"]
    quickstart = checks.check_sandbox_isolation(host, SETTINGS)
    assert quickstart.status == "warn"
    assert "hardened runc" in quickstart.summary
    prod = checks.check_sandbox_isolation(host, DoctorSettings(profile="prod"))
    assert prod.status == "fail"
    assert "prod profile requires it" in prod.summary


def test_user_namespaces() -> None:
    host = FakeHost.healthy()
    assert checks.check_user_namespaces(host, SETTINGS).summary == (
        "Unprivileged user namespaces are enabled (limit 28633)."
    )
    host.files["/proc/sys/user/max_user_namespaces"] = "0\n"
    assert checks.check_user_namespaces(host, SETTINGS).status == "warn"
    host.files["/proc/sys/user/max_user_namespaces"] = "many\n"
    assert checks.check_user_namespaces(host, SETTINGS).status == "skip"
    del host.files["/proc/sys/user/max_user_namespaces"]
    assert checks.check_user_namespaces(host, SETTINGS).status == "skip"


def test_id_mapping_helpers() -> None:
    host = FakeHost.healthy()
    assert checks.check_id_mapping_helpers(host, SETTINGS).status == "ok"
    del host.commands["newgidmap"]
    one = checks.check_id_mapping_helpers(host, SETTINGS)
    assert one.status == "warn"
    assert one.summary == "newgidmap was not found."
    del host.commands["newuidmap"]
    both = checks.check_id_mapping_helpers(host, SETTINGS)
    assert both.summary == "newuidmap and newgidmap were not found."


def test_cgroups_v2() -> None:
    host = FakeHost.healthy()
    assert checks.check_cgroups_v2(host, SETTINGS).summary == "cgroups v2 is in use."
    del host.files["/sys/fs/cgroup/cgroup.controllers"]
    result = checks.check_cgroups_v2(host, SETTINGS)
    assert result.status == "warn"
    assert result.summary == "This host is not using cgroups v2."


# --- GPU -------------------------------------------------------------------------------------


def test_gpu_two_identical_cards() -> None:
    result = checks.check_gpu(FakeHost.healthy(), SETTINGS)
    assert result.status == "ok"
    assert result.summary == (
        "2 GPUs found: NVIDIA H100 80GB HBM3, 159 GiB of GPU memory in total."
    )


def test_gpu_mixed_cards_and_junk_lines() -> None:
    host = FakeHost.healthy()
    host.outputs[NVIDIA_SMI_QUERY] = CommandResult(
        0,
        "NVIDIA H100 80GB HBM3, 81559\n\nNVIDIA A100-SXM4-40GB, 40960\nno comma here\nX, abc\n",
    )
    result = checks.check_gpu(host, SETTINGS)
    assert result.summary == (
        "2 GPUs found: 1 of NVIDIA H100 80GB HBM3, 1 of NVIDIA A100-SXM4-40GB, "
        "120 GiB of GPU memory in total."
    )


def test_gpu_driver_missing() -> None:
    host = FakeHost.healthy()
    del host.commands["nvidia-smi"]
    result = checks.check_gpu(host, SETTINGS)
    assert result.status == "fail"
    assert result.summary == "No NVIDIA GPU driver was found (nvidia-smi is missing)."


def test_gpu_driver_not_answering() -> None:
    host = FakeHost.healthy()
    host.outputs[NVIDIA_SMI_QUERY] = CommandResult(1, "", "NVIDIA-SMI has failed")
    result = checks.check_gpu(host, SETTINGS)
    assert result.status == "fail"
    assert result.summary == "nvidia-smi is installed but could not list any GPU."


def test_gpu_container_toolkit() -> None:
    host = FakeHost.healthy()
    assert checks.check_gpu_container_toolkit(host, SETTINGS).summary == (
        "NVIDIA Container Toolkit 1.16.2 is installed."
    )
    host.outputs[("nvidia-ctk", "--version")] = CommandResult(0, "")
    assert checks.check_gpu_container_toolkit(host, SETTINGS).summary == (
        "The NVIDIA Container Toolkit is installed."
    )
    del host.commands["nvidia-ctk"]
    missing = checks.check_gpu_container_toolkit(host, SETTINGS)
    assert missing.status == "fail"
    assert "nvidia-ctk is missing" in missing.summary
    del host.commands["nvidia-smi"]
    skipped = checks.check_gpu_container_toolkit(host, SETTINGS)
    assert skipped.status == "skip"
    assert skipped.summary == "Skipped because no GPU driver was found."


# --- data root, disk, port -----------------------------------------------------------------


def test_data_root_exists_and_is_writable() -> None:
    result = checks.check_data_root(FakeHost.healthy(), SETTINGS)
    assert result.status == "ok"
    assert result.summary == "/AI/Agent exists and is writable."


def test_data_root_exists_but_not_writable() -> None:
    host = FakeHost.healthy()
    host.writable_paths.discard(DATA_ROOT)
    result = checks.check_data_root(host, SETTINGS)
    assert result.status == "fail"
    assert result.summary == "The data root /AI/Agent exists but is not writable by you."
    assert result.detail is not None
    assert "chown" in result.detail.what_to_do


def test_data_root_is_a_file() -> None:
    host = FakeHost.healthy()
    host.directories.discard(DATA_ROOT)
    result = checks.check_data_root(host, SETTINGS)
    assert result.status == "fail"
    assert result.summary == "The data root /AI/Agent exists but is not a directory."


def test_data_root_will_be_created() -> None:
    host = FakeHost.healthy()
    settings = DoctorSettings(data_root="/AI/Agent/deeper/still")
    result = checks.check_data_root(host, settings)
    assert result.status == "ok"
    assert result.summary == (
        "/AI/Agent/deeper/still does not exist yet; it will be created under /AI/Agent."
    )


def test_data_root_cannot_be_created() -> None:
    host = FakeHost.healthy()
    settings = DoctorSettings(data_root="/srv/slas")
    result = checks.check_data_root(host, settings)
    assert result.status == "fail"
    assert result.summary == "The data root /srv/slas does not exist and cannot be created."
    assert result.detail is not None
    assert result.detail.likely_cause == "/ is not writable by you."


def test_data_root_with_no_existing_ancestor_at_all() -> None:
    host = FakeHost(system_name="Linux")  # no paths exist, not even "/"
    result = checks.check_data_root(host, SETTINGS)
    assert result.status == "fail"
    assert result.detail is not None
    assert result.detail.likely_cause == "its parent directory is not writable by you."


def test_disk_space() -> None:
    host = FakeHost.healthy()
    ok = checks.check_disk_space(host, SETTINGS)
    assert ok.summary == "2000 GiB free at /AI/Agent (at least 200 GiB needed)."
    host.disk_free[DATA_ROOT] = 50 * GIB
    low = checks.check_disk_space(host, SETTINGS)
    assert low.status == "fail"
    assert low.summary == "Only 50 GiB is free where the data root will live (/AI/Agent)."
    del host.disk_free[DATA_ROOT]
    assert checks.check_disk_space(host, SETTINGS).status == "skip"


def test_disk_space_measured_at_nearest_ancestor() -> None:
    host = FakeHost.healthy()
    host.disk_free["/AI"] = 300 * GIB
    result = checks.check_disk_space(host, DoctorSettings(data_root="/AI/elsewhere"))
    assert result.summary == "300 GiB free at /AI (at least 200 GiB needed)."


@pytest.mark.parametrize(
    ("in_use", "status", "summary"),
    [
        (False, "ok", "Port 443 is free for the web interface."),
        (True, "fail", "Something is already listening on port 443."),
        (None, "skip", "Could not check whether port 443 is free."),
    ],
)
def test_web_port(in_use: bool | None, status: str, summary: str) -> None:
    host = FakeHost.healthy()
    host.ports[443] = in_use
    result = checks.check_web_port(host, SETTINGS)
    assert result.status == status
    assert result.summary == summary
