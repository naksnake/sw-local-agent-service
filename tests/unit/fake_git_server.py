"""A fake Git host on loopback for tests: smart HTTP through `git http-backend`, Basic auth
with one token, and a fake GitLab/Gitea/GitHub API that records merge-request calls.

Standard library only. Nothing leaves the machine; the only "network" is 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class FakeGitHostState:
    def __init__(
        self, root: Path, *, token: str, username: str = "oauth2", kind: str = "gitlab"
    ) -> None:
        self.root = root
        self.token = token
        self.username = username
        self.kind = kind
        self.access_log: list[str] = []  # "METHOD path status" — what a real server would log
        self.api_calls: list[dict[str, Any]] = []
        self.auth_failures = 0
        self.base_url = ""

    def authorised(self, header: str | None) -> bool:
        if not header:
            return False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except ValueError:
                return False
            user, _, password = decoded.partition(":")
            return password == self.token and (user == self.username or not user)
        if header.startswith(("token ", "Bearer ")):
            return header.split(" ", 1)[1] == self.token
        return header == self.token  # PRIVATE-TOKEN


def _handler(state: FakeGitHostState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return  # the access log is written by _finish with the status only

        def _finish(self, status: int) -> None:
            state.access_log.append(f"{self.command} {urlsplit(self.path).path} {status}")

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _deny(self) -> None:
            state.auth_failures += 1
            body = b"Authentication required\n"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="fake-git"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._finish(401)

        def _api(self) -> None:
            body = self._read_body()
            token_header = self.headers.get("PRIVATE-TOKEN") or self.headers.get("Authorization")
            if not state.authorised(token_header):
                self._deny()
                return
            payload = json.loads(body or b"{}")
            state.api_calls.append(
                {"path": urlsplit(self.path).path, "method": self.command, "body": payload}
            )
            answer: dict[str, Any]
            if state.kind == "gitlab":
                answer = {
                    "iid": len(state.api_calls),
                    "web_url": f"{state.base_url}/-/merge_requests/{len(state.api_calls)}",
                }
            else:
                answer = {
                    "number": len(state.api_calls),
                    "html_url": f"{state.base_url}/pulls/{len(state.api_calls)}",
                }
            data = json.dumps(answer).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self._finish(201)

        def _git(self) -> None:
            body = self._read_body()
            if not state.authorised(self.headers.get("Authorization")):
                self._deny()
                return
            parts = urlsplit(self.path)
            env = {
                "GIT_PROJECT_ROOT": str(state.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": parts.path,
                "QUERY_STRING": parts.query,
                "REQUEST_METHOD": self.command,
                "REMOTE_USER": state.username,
                "REMOTE_ADDR": "127.0.0.1",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            for name, value in self.headers.items():
                env["HTTP_" + name.upper().replace("-", "_")] = value
            completed = subprocess.run(  # argv, the CGI git ships
                ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
            )
            head, _, payload = completed.stdout.partition(b"\r\n\r\n")
            if not _ and b"\n\n" in completed.stdout:
                head, _, payload = completed.stdout.partition(b"\n\n")
            status = 200
            headers: list[tuple[str, str]] = []
            for line in head.decode("latin-1").splitlines():
                key, _, value = line.partition(":")
                if key.lower() == "status":
                    status = int(value.strip().split()[0])
                elif key:
                    headers.append((key, value.strip()))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self._finish(status)

        def do_GET(self) -> None:
            self._git()

        def do_POST(self) -> None:
            if urlsplit(self.path).path.startswith("/api/"):
                self._api()
            else:
                self._git()

    return Handler


class FakeGitHost:
    """Start with `with FakeGitHost(...) as host:`; `host.state.base_url` is http://127.0.0.1:port."""

    def __init__(
        self, root: Path, *, token: str, username: str = "oauth2", kind: str = "gitlab"
    ) -> None:
        self.state = FakeGitHostState(root, token=token, username=username, kind=kind)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeGitHost:
        self._thread.start()
        self.state.base_url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def create_repo(self, path: str, *, seed_branch: str = "main") -> Path:
        """A bare repository with one commit on `seed_branch`, accepting pushes."""
        bare = self.state.root / f"{path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.state.root),
        }
        subprocess.run(
            ["git", "init", "--quiet", "--bare", f"--initial-branch={seed_branch}", str(bare)],
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True, env=env
        )
        work = self.state.root / f"seed-{path.replace('/', '-')}"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True, env=env)
        (work / "README.md").write_text(f"# {path}\n", encoding="utf-8")
        for argv in (
            ["git", "-C", str(work), "checkout", "--quiet", "-b", seed_branch],
            ["git", "-C", str(work), "add", "README.md"],
            [
                "git",
                "-C",
                str(work),
                "-c",
                "user.name=Seed",
                "-c",
                "user.email=seed@slas.local",
                "commit",
                "--quiet",
                "-m",
                "Seed",
            ],
            ["git", "-C", str(work), "push", "--quiet", "origin", seed_branch],
        ):
            subprocess.run(argv, check=True, env=env)
        return bare
