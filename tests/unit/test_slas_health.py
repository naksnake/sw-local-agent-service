"""`slas-health URL [--insecure-local]`: the probe every compose healthcheck runs. POSIX sh,
exit 0 on a 2xx answer and 1 otherwise, with whichever of curl, wget or python3 the image
has; --insecure-local skips TLS verification for a loopback host only."""

from __future__ import annotations

import http.server
import os
import shutil
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "images" / "slas-health" / "slas-health"
TOOLS = [tool for tool in ("curl", "wget", "python3") if shutil.which(tool)]


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = {"/health": 200, "/created": 201, "/down": 503}.get(self.path, 404)
        body = b"ok\n" if status < 300 else b"not now\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def plain_server() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def self_signed(tmp_path: Path) -> tuple[Path, Path]:
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[str]:
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make a self-signed certificate")
    cert, key = self_signed(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def probe(
    *args: str, tool: str | None = None, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path or os.environ.get("PATH", "/usr/bin:/bin"), "SLAS_HEALTH_TIMEOUT": "5"}
    if tool:
        env["SLAS_HEALTH_TOOL"] = tool
    return subprocess.run(
        ["sh", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60, check=False
    )


def test_the_script_is_posix_sh_and_executable() -> None:
    assert os.access(SCRIPT, os.X_OK), "slas-health must keep its executable bit"
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True, timeout=30)


@pytest.mark.parametrize("tool", TOOLS)
def test_exit_0_on_2xx_and_1_otherwise_with_each_tool(plain_server: str, tool: str) -> None:
    assert probe(f"{plain_server}/health", tool=tool).returncode == 0
    assert probe(f"{plain_server}/created", tool=tool).returncode == 0
    assert probe(f"{plain_server}/moved", tool=tool).returncode == 0, "redirects are followed"
    down = probe(f"{plain_server}/down", tool=tool)
    assert down.returncode == 1 and "answered 503" in down.stderr
    assert probe(f"{plain_server}/missing", tool=tool).returncode == 1
    closed = probe("http://127.0.0.1:9/health", tool=tool)
    assert closed.returncode == 1 and "answered nothing" in closed.stderr


def test_it_picks_the_tool_the_image_has(plain_server: str, tmp_path: Path) -> None:
    only_python = tmp_path / "bin"
    only_python.mkdir()
    (only_python / "python3").symlink_to(sys.executable)
    for name in ("sh", "sed", "tail", "grep", "tr"):
        found = shutil.which(name)
        assert found, name
        (only_python / name).symlink_to(found)
    result = probe(f"{plain_server}/health", path=str(only_python))
    assert result.returncode == 0, result.stderr
    assert probe(f"{plain_server}/down", path=str(only_python)).returncode == 1
    nothing = tmp_path / "empty"
    nothing.mkdir()
    (nothing / "sh").symlink_to(shutil.which("sh") or "/bin/sh")
    result = probe(f"{plain_server}/health", path=str(nothing))
    assert result.returncode == 1 and "none of curl, wget or python3" in result.stderr


@pytest.mark.parametrize("tool", TOOLS)
def test_insecure_local_skips_verification_for_loopback_only(tls_server: str, tool: str) -> None:
    verified = probe(f"{tls_server}/health", tool=tool)
    assert verified.returncode == 1, "a self-signed certificate does not verify"
    trusted = probe(f"{tls_server}/health", "--insecure-local", tool=tool)
    assert trusted.returncode == 0, trusted.stderr
    assert probe(f"{tls_server}/down", "--insecure-local", tool=tool).returncode == 1
    refused = probe("https://10.20.30.40/healthz", "--insecure-local", tool=tool)
    assert refused.returncode == 2
    assert (
        "--insecure-local is only for 127.0.0.1, localhost or [::1], not 10.20.30.40"
        in refused.stderr
    )
    assert probe("https://api:8000/health", "--insecure-local", tool=tool).returncode == 2


def test_wrong_usage_exits_with_two() -> None:
    assert probe().returncode == 2
    assert probe("http://127.0.0.1/a", "http://127.0.0.1/b").returncode == 2
    assert probe("http://127.0.0.1/a", "--bogus").returncode == 2
    bad_tool = probe("http://127.0.0.1:9/a", tool="telnet")
    assert bad_tool.returncode == 2 and "SLAS_HEALTH_TOOL must be" in bad_tool.stderr
