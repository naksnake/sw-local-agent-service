"""The engine runner: argv only, one compose builder, a fake that journals and scripts answers."""

from __future__ import annotations

import sys

from slas_cli.runner import (
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    Compose,
    FakeEngineRunner,
    RealEngineRunner,
    RunResult,
)


def test_compose_argv_names_project_file_and_env_file() -> None:
    compose = Compose(
        engine="docker", compose_file="/data/compose/docker-compose.yml", env_file="/data/.env"
    )
    argv = compose.argv("ps", "--format", "json")
    assert argv[:2] == ["docker", "compose"]
    assert argv[2:4] == ["--project-name", "slas"]
    assert "-f" in argv and "/data/compose/docker-compose.yml" in argv
    assert "--env-file" in argv and "/data/.env" in argv
    assert argv[-3:] == ["ps", "--format", "json"]


def test_exec_api_never_allocates_a_tty() -> None:
    compose = Compose(engine="podman", compose_file="c.yml", env_file=".env")
    argv = compose.exec_api("python", "-m", "slas_api", "user", "list")
    assert argv[-8:] == ["exec", "-T", "api", "python", "-m", "slas_api", "user", "list"]
    assert argv[0] == "podman"


def test_fake_runner_journals_and_matches_longest_prefix() -> None:
    fake = FakeEngineRunner(
        responses={
            ("docker",): RunResult(1, "", "generic"),
            ("docker", "compose"): RunResult(0, "compose ok", ""),
        }
    )
    assert fake.run(["docker", "compose", "ps"]).stdout == "compose ok"
    assert fake.run(["docker", "info"]).returncode == 1
    assert fake.run(["podman", "info"]) == fake.default
    assert fake.calls == [("docker", "compose", "ps"), ("docker", "info"), ("podman", "info")]
    assert fake.calls_with("docker", "compose") == [("docker", "compose", "ps")]


def test_fake_runner_scripts_a_sequence_and_repeats_the_last_answer() -> None:
    fake = FakeEngineRunner(
        responses={
            ("docker", "compose", "ps"): [RunResult(0, "starting", ""), RunResult(0, "healthy", "")]
        }
    )
    assert fake.run(["docker", "compose", "ps"]).stdout == "starting"
    assert fake.run(["docker", "compose", "ps"]).stdout == "healthy"
    assert fake.run(["docker", "compose", "ps"]).stdout == "healthy"


def test_fake_runner_records_stdin_so_tests_can_prove_secrets_travel_by_stdin() -> None:
    fake = FakeEngineRunner()
    fake.run(["docker", "secret", "create"], input_text="hunter2")
    assert fake.inputs == ["hunter2"]
    assert all("hunter2" not in " ".join(c) for c in fake.calls)


def test_real_runner_runs_argv_without_a_shell() -> None:
    result = RealEngineRunner().run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert result.returncode == 3
    assert result.stdout.strip() == "hi"
    assert not result.ok


def test_real_runner_reports_missing_executable_and_timeout_as_results() -> None:
    missing = RealEngineRunner().run(["slas-no-such-engine-xyz", "--version"])
    assert missing.returncode == EXIT_NOT_FOUND
    assert "not found" in missing.stderr
    slow = RealEngineRunner().run(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.2
    )
    assert slow.returncode == EXIT_TIMEOUT
    assert RealEngineRunner().run([]).returncode == EXIT_NOT_FOUND


def test_real_runner_passes_extra_environment_and_stdin() -> None:
    result = RealEngineRunner().run(
        [
            sys.executable,
            "-c",
            "import os,sys; print(os.environ['SLAS_TEST_X'] + sys.stdin.read())",
        ],
        env={"SLAS_TEST_X": "a"},
        input_text="b",
    )
    assert result.stdout.strip() == "ab"
