"""Sandbox spec hardening (INV-4, INV-14), podman argv, sessions with TTL and quotas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_sandbox_manager.manager import (
    PUSH_EXPLANATION,
    SandboxError,
    SandboxManager,
    render_gitconfig,
    slugify,
)
from slas_sandbox_manager.runtime import ExecResult, FakeSandboxRuntime
from slas_sandbox_manager.spec import (
    GITCONFIG_TARGET,
    SCRATCH,
    WORKSPACE,
    HardeningError,
    Mount,
    Resources,
    SandboxSpec,
    default_env,
    exec_argv,
    podman_argv,
)

IMAGE = "registry.internal/slas/sandbox-python:3.12.6"


def mounts(project: str = "/AI/Agent/Coding/pat/Projects/fan-ctl") -> list[Mount]:
    return [
        Mount(source=project, target=WORKSPACE, mode="rw"),
        Mount(source="/AI/Agent/Coding/pat/Container/s1", target=SCRATCH, mode="rw"),
        Mount(source="/AI/Agent/Coding/pat/gitconfig", target=GITCONFIG_TARGET, mode="ro"),
    ]


def spec(**overrides: object) -> SandboxSpec:
    base: dict[str, object] = {
        "name": "slas-sbx-fan-ctl-1",
        "image": IMAGE,
        "runtime": "runsc",
        "user": "pat",
        "slug": "fan-ctl",
        "mounts": mounts(),
        "env": default_env(language="python"),
    }
    return SandboxSpec.model_validate({**base, **overrides})


# --- spec -----------------------------------------------------------------------------------


def test_podman_argv_is_hardened_and_has_no_network() -> None:
    argv = podman_argv(spec())
    joined = " ".join(argv)
    assert argv[:3] == ["podman", "run", "--detach"]
    assert "--network none" in joined and "--read-only" in joined
    assert "--cap-drop ALL" in joined and "--security-opt no-new-privileges" in joined
    assert "--runtime runsc" in joined and "--pids-limit 512" in joined
    assert "--memory 4g" in joined and "--cpus 2" in joined
    assert "--user 10001:10001" in joined and "--workdir /workspace" in joined
    assert f"--volume /AI/Agent/Coding/pat/gitconfig:{GITCONFIG_TARGET}:ro" in joined
    assert "--env GIT_CONFIG_GLOBAL=/etc/slas/gitconfig" in joined
    assert argv[-3:] == [IMAGE, "sleep", "infinity"]
    assert "--userns" not in joined and "seccomp" not in joined, "gVisor needs neither"
    assert not any(part.startswith("--publish") or part == "--privileged" for part in argv)
    assert exec_argv("slas-sbx-fan-ctl-1", ["git", "status"]) == [
        "podman",
        "exec",
        "--workdir",
        "/workspace",
        "--user",
        "10001:10001",
        "slas-sbx-fan-ctl-1",
        "git",
        "status",
    ]
    with pytest.raises(ValueError, match="argv must not be empty"):
        exec_argv("slas-sbx-fan-ctl-1", [])


def test_runc_fallback_adds_user_namespace_and_seccomp() -> None:
    joined = " ".join(podman_argv(spec(runtime="runc")))
    assert "--runtime runc" in joined
    assert (
        "--userns keep-id" in joined
        and "--security-opt seccomp=/etc/slas/seccomp-sandbox.json" in joined
    )
    assert (
        spec(runtime="runc")
        .sentence()
        .startswith("Sandbox slas-sbx-fan-ctl-1 for fan-ctl: hardened runc, no network")
    )
    assert spec().sentence() == (
        "Sandbox slas-sbx-fan-ctl-1 for fan-ctl: gVisor, no network, read-only system, 2 CPUs, "
        "4g memory, closes after 60 minutes idle."
    )


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"image": "registry.internal/slas/sandbox-python:latest"}, "is not pinned"),
        ({"image": "sandbox-python"}, "is not pinned"),
        (
            {"env": {**default_env(language="python"), "GITLAB_TOKEN": "x"}},
            "would carry GITLAB_TOKEN",
        ),
        (
            {"env": {**default_env(language="python"), "SSH_AUTH_SOCK": "/x"}},
            "would carry SSH_AUTH_SOCK",
        ),
        (
            {"env": {**default_env(language="python"), "GIT_ASKPASS": "/x"}},
            "would carry GIT_ASKPASS",
        ),
        ({"mounts": mounts()[:2]}, "not the three the platform allows"),
        (
            {
                "mounts": [
                    *mounts()[:2],
                    Mount(source="/home/pat/.ssh", target=GITCONFIG_TARGET, mode="ro"),
                ]
            },
            "would mount /home/pat/.ssh",
        ),
        (
            {
                "mounts": [
                    Mount(source="/run/podman/podman.sock", target=WORKSPACE, mode="rw"),
                    *mounts()[1:],
                ]
            },
            "would mount /run/podman/podman.sock",
        ),
        (
            {
                "mounts": [
                    Mount(source="/tmp/.X11-unix", target=WORKSPACE, mode="rw"),  # noqa: S108
                    *mounts()[1:],
                ]
            },
            "would mount /tmp/.X11-unix",
        ),
        (
            {"mounts": [Mount(source="/dev/input", target=WORKSPACE, mode="rw"), *mounts()[1:]]},
            "would mount /dev/input",
        ),
        (
            {
                "mounts": [
                    *mounts()[:2],
                    Mount(
                        source="/AI/Agent/Coding/pat/gitconfig", target=GITCONFIG_TARGET, mode="rw"
                    ),
                ]
            },
            "mounted read-write",
        ),
    ],
)
def test_spec_refuses_credentials_sockets_displays_and_extra_mounts(
    overrides: dict[str, object], fragment: str
) -> None:
    with pytest.raises(HardeningError) as raised:
        spec(**overrides)
    assert fragment in raised.value.message.what_happened


def test_network_cannot_be_anything_but_none() -> None:
    with pytest.raises(ValueError):
        spec(network="slas-backend")
    assert SandboxSpec.model_fields["network"].default == "none"
    assert Resources().pids == 512 and Resources(cpus=8, memory="16g").memory == "16g"


# --- manager --------------------------------------------------------------------------------


def manager(
    tmp_path: Path, *, runsc: bool = True, profile: str = "quickstart"
) -> tuple[SandboxManager, FakeSandboxRuntime, FakeClock]:
    runtime = FakeSandboxRuntime()
    clock = FakeClock(datetime(2026, 9, 14, 8, tzinfo=UTC), step=timedelta(0))
    return (
        SandboxManager(
            runtime=runtime,
            data_root=tmp_path,
            clock=clock,
            runsc_available=runsc,
            profile=profile,  # type: ignore[arg-type]
            max_sessions_per_user=2,
            default_ttl_s=600,
        ),
        runtime,
        clock,
    )


def test_open_creates_project_identity_and_scratch_and_execs_argv(tmp_path: Path) -> None:
    mgr, runtime, clock = manager(tmp_path)
    session = mgr.open("pat", "fan-ctl", image=IMAGE, language="python", display_name="Pat Lin")
    project = tmp_path / "Coding" / "pat" / "Projects" / "fan-ctl"
    assert project.is_dir() and Path(session.project_dir) == project
    assert (
        Path(session.scratch_dir).is_dir() and Path(session.scratch_dir).parent.name == "Container"
    )
    gitconfig = (tmp_path / "Coding" / "pat" / "gitconfig").read_text(encoding="utf-8")
    assert "\tname = Pat Lin\n\temail = pat@slas.local\n" in gitconfig
    assert "[credential]\n\thelper =\n" in gitconfig and "hooksPath = /var/empty" in gitconfig
    assert render_gitconfig("Pat Lin", "pat@slas.local") == gitconfig
    assert session.runtime_sentence == "The sandbox runs under gVisor."
    assert (
        runtime.created[0].runtime == "runsc"
        and runtime.created[0].env["SLAS_LANGUAGE"] == "python"
    )
    assert (
        session.sentence(clock.now())
        == "Sandbox for fan-ctl is open; it closes after 10 more idle minutes."
    )

    runtime.script(["slas-check", "test"], ExecResult(exit_code=1, stderr="1 failed"))
    result = mgr.exec(session.id, ["slas-check", "test"])
    assert not result.ok and result.stderr == "1 failed"
    assert runtime.execs == [(session.handle.name, ("slas-check", "test"), "/workspace")]
    with pytest.raises(ValueError, match="non-empty list of strings"):
        mgr.exec(session.id, [])
    assert mgr.sentence() == "1 sandbox is open on this host."
    assert PUSH_EXPLANATION == "Push happens from the Git panel, which uses your saved remote."


def test_quota_ttl_and_reap(tmp_path: Path) -> None:
    mgr, runtime, clock = manager(tmp_path)
    first = mgr.open("pat", "a", image=IMAGE, language="python", display_name="Pat")
    mgr.open("pat", "b", image=IMAGE, language="python", display_name="Pat")
    with pytest.raises(SandboxError) as raised:
        mgr.open("pat", "c", image=IMAGE, language="python", display_name="Pat")
    assert (
        raised.value.message.what_happened
        == "You already have 2 sandboxes open, which is the limit."
    )
    mgr.open("lee", "c", image=IMAGE, language="python", display_name="Lee")

    # Using a sandbox extends its life; idle ones expire and are reaped.
    clock._now = clock.now().replace(minute=5)
    mgr.exec(first.id, ["true"])
    clock._now = clock.now().replace(minute=11)
    idle = sorted(
        s.id for s in mgr.sessions_for("pat") + mgr.sessions_for("lee") if s.id != first.id
    )
    assert sorted(mgr.reap()) == idle and len(idle) == 2
    assert [s.id for s in mgr.sessions_for("pat")] == [first.id]
    assert len(runtime.destroyed) == 2 and Path(first.scratch_dir).exists()

    clock._now = clock.now().replace(minute=20)
    with pytest.raises(SandboxError) as expired:
        mgr.exec(first.id, ["true"])
    assert expired.value.message.what_happened == "The sandbox for a expired after being idle."
    with pytest.raises(SandboxError, match="no longer open"):
        mgr.get(first.id)
    mgr.close("nothing")  # closing an unknown session is a no-op
    assert mgr.sentence() == "0 sandboxes are open on this host."


def test_runc_fallback_in_quickstart_and_refusal_in_prod(tmp_path: Path) -> None:
    mgr, runtime, _ = manager(tmp_path, runsc=False)
    session = mgr.open("pat", "a", image=IMAGE, language="rust", display_name="Pat")
    assert runtime.created[0].runtime == "runc"
    assert session.runtime_sentence.startswith(
        "gVisor is not installed on this host, so the sandbox runs under hardened runc"
    )
    prod, _, _ = manager(tmp_path, runsc=False, profile="prod")
    with pytest.raises(SandboxError) as raised:
        prod.open("pat", "a", image=IMAGE, language="rust", display_name="Pat")
    assert (
        raised.value.message.what_happened
        == "No sandbox can be opened: gVisor is not installed on this host."
    )


def test_slugify() -> None:
    assert slugify("Fan Controller v2!") == "fan-controller-v2"
    with pytest.raises(ValueError):
        slugify("!!!")
