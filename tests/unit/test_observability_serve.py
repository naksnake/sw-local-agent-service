"""`python -m slas_observability.serve <service>`: /health and /metrics in the foreground for
every first-party container until its own entrypoint lands; stops cleanly on SIGTERM;
standard library only, so the screen-worker image runs it without a virtualenv."""

from __future__ import annotations

import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from slas_observability import serve

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_serve_answers_health_and_metrics_until_stopped() -> None:
    stop = threading.Event()
    ports: list[int] = []
    out = io.StringIO()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            serve.serve("git-broker", bind="127.0.0.1:0", stop=stop, ready=ports.append, out=out)
        )
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not ports and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ports, "the server never reported it was listening"
    port = ports[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
        assert json.loads(response.read()) == {"service": "git-broker", "ok": True}
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        assert response.headers["Content-Type"].startswith("text/plain")
    stop.set()
    thread.join(timeout=10)
    assert result == [0]
    text = out.getvalue()
    assert f"git-broker: answering /health and /metrics on 127.0.0.1:{port}" in text
    assert "git-broker: stopped." in text


def test_a_service_name_is_validated() -> None:
    with pytest.raises(ValueError, match="not a service name"):
        serve.serve("Not A Service", bind="127.0.0.1:0", stop=threading.Event())


def test_main_runs_as_a_process_and_stops_on_sigterm() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "slas_observability.serve", "llm-gateway", "--bind", "127.0.0.1:0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        first = process.stdout.readline()
        match = re.search(r"on 127\.0\.0\.1:(\d+);", first)
        assert match, first
        port = int(match.group(1))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            assert json.loads(response.read())["service"] == "llm-gateway"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert "llm-gateway: stopped." in stdout
    finally:
        if process.poll() is None:
            process.kill()


def test_the_runner_imports_with_the_standard_library_alone(tmp_path: Path) -> None:
    """The screen-worker image puts packages/slas-observability on PYTHONPATH and nothing else."""
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys\nimport slas_observability.serve\n"
            "assert 'pydantic' not in sys.modules\nprint('ok')",
        ],
        env={
            "PYTHONPATH": str(REPO_ROOT / "packages" / "slas-observability"),
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
