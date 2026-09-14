"""scripts/fetch_models.py against a fake hub on loopback: tree listing, resume, checksums,
redundant-format skipping, the offline verify, and the three-part errors."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fetch_models", REPO_ROOT / "scripts" / "fetch_models.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # slotted dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


fm = load_script()

WEIGHTS = bytes(range(256)) * 4096  # 1 MiB, larger than one download chunk
CONFIG = b'{"architectures": ["Demo"]}\n'
README = b"# demo\n"
BIN = b"old-format-weights"


class FakeHub:
    """Enough of the hub's API for the script: tree, revision, resolve with Range support."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.cut_after: int | None = None  # serve at most this many bytes once, then behave
        self.gated = False
        self.corrupt = False
        outer = self
        files = {
            "model.safetensors": WEIGHTS,
            "config.json": CONFIG,
            "README.md": README,
            "pytorch_model.bin": BIN,
            "onnx/model.onnx": b"onnx",
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(
                self, status: int, body: bytes, headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                auth = self.headers.get("Authorization")
                outer.requests.append((self.path, self.headers.get("Range")))
                if outer.gated and auth != "Bearer hf_test-token":
                    self._send(401, b'{"error":"gated"}')
                    return
                if self.path.startswith("/api/models/demo/tiny/revision/"):
                    self._send(200, json.dumps({"sha": "abc123def4567890"}).encode())
                    return
                if self.path.startswith("/api/models/demo/tiny/tree/"):
                    entries: list[dict[str, object]] = [{"type": "directory", "path": "onnx"}]
                    for name, content in files.items():
                        entry: dict[str, object] = {
                            "type": "file",
                            "path": name,
                            "size": len(content),
                        }
                        if len(content) > 100:
                            entry["lfs"] = {
                                "oid": hashlib.sha256(content).hexdigest(),
                                "size": len(content),
                            }
                        entries.append(entry)
                    self._send(200, json.dumps(entries).encode())
                    return
                if self.path.startswith("/api/models/demo/missing/"):
                    self._send(404, b"{}")
                    return
                prefix = "/demo/tiny/resolve/main/"
                if self.path.startswith(prefix):
                    name = self.path[len(prefix) :]
                    data: bytes | None = files.get(name)
                    if data is None:
                        self._send(404, b"")
                        return
                    if outer.corrupt and name == "model.safetensors":
                        data = b"x" + data[1:]
                    range_header = self.headers.get("Range")
                    if range_header:
                        start = int(range_header.removeprefix("bytes=").rstrip("-"))
                        self._send(
                            206,
                            data[start:],
                            {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
                        )
                        return
                    if outer.cut_after is not None and len(data) > outer.cut_after:
                        cut, outer.cut_after = outer.cut_after, None
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data[:cut])
                        self.wfile.flush()
                        self.connection.close()
                        return
                    self._send(200, data)
                    return
                self._send(404, b"")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> FakeHub:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    fake = FakeHub().start()
    monkeypatch.setenv("HF_ENDPOINT", fake.endpoint)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    try:
        yield fake
    finally:
        fake.stop()


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = fm.main(list(argv), stdout=out)
    return code, out.getvalue()


def test_fetch_writes_files_checksums_and_manifest_and_skips_redundant_formats(
    hub: FakeHub, tmp_path: Path
) -> None:
    dest = tmp_path / "models"
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0, out
    assert (dest / "tiny" / "model.safetensors").read_bytes() == WEIGHTS
    assert (dest / "tiny" / "config.json").read_bytes() == CONFIG
    assert not (dest / "tiny" / "pytorch_model.bin").exists(), "safetensors present: .bin skipped"
    assert not (dest / "tiny" / "onnx").exists()
    sums = fm.read_sums(dest / "tiny" / "SHA256SUMS")
    assert sums["model.safetensors"] == hashlib.sha256(WEIGHTS).hexdigest()
    assert set(sums) == {"model.safetensors", "config.json", "README.md"}
    manifest = json.loads((dest / "manifest.json").read_text())
    (entry,) = manifest["models"]
    assert (
        entry["repo"] == "demo/tiny"
        and entry["commit"] == "abc123def4567890"
        and entry["files"] == 3
    )
    assert (
        "tiny: 3 files, 1.0 MiB, from demo/tiny at abc123def456; 2 redundant files skipped." in out
    )
    assert "run `scripts/fetch_models.py verify --dest /AI/Agent/Models` there." in out
    # Every request carried no token, and the token never appears in the output.
    assert "Bearer" not in out


def test_a_cut_download_resumes_with_a_range_request_and_a_rerun_keeps_complete_files(
    hub: FakeHub, tmp_path: Path
) -> None:
    dest = tmp_path / "models"
    hub.cut_after = 300_000
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 1
    assert "model.safetensors is 300000 bytes, but the hub says 1048576." in out
    assert "it resumes where it stopped" in out
    assert (dest / "tiny" / "model.safetensors.part").stat().st_size == 300_000
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0, out
    assert (dest / "tiny" / "model.safetensors").read_bytes() == WEIGHTS
    ranges = [r for path, r in hub.requests if path.endswith("/model.safetensors") and r]
    assert ranges == ["bytes=300000-"]
    hub.requests.clear()
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0 and "kept    model.safetensors (already complete)" in out
    assert not any(path.endswith("/model.safetensors") for path, _ in hub.requests)


def test_a_corrupted_file_is_removed_and_reported(hub: FakeHub, tmp_path: Path) -> None:
    hub.corrupt = True
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(tmp_path))
    assert code == 1
    assert "tiny/model.safetensors does not match the checksum the hub publishes." in out
    assert not (tmp_path / "tiny" / "model.safetensors").exists()


def test_verify_is_offline_and_names_what_is_wrong(hub: FakeHub, tmp_path: Path) -> None:
    dest = tmp_path / "models"
    assert run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))[0] == 0
    hub.requests.clear()
    code, out = run("verify", "--dest", str(dest))
    assert code == 0 and "tiny: 3 files, every file matches." in out
    assert hub.requests == [], "verify never touches the network"
    (dest / "tiny" / "config.json").write_bytes(b"{}")
    (dest / "tiny" / "README.md").unlink()
    code, out = run("verify", "--dest", str(dest))
    assert code == 1
    assert "tiny/config.json does not match its checksum." in out
    assert "tiny/README.md is missing." in out
    assert "copy the listed files again from the build host" in out
    code, out = run("verify", "--dest", str(tmp_path / "empty"))
    assert code == 1 and "has a SHA256SUMS file" in out


def test_sources_file_gated_repo_and_errors_speak_in_three_parts(
    hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text("# comment\n\ntiny demo/tiny main\n")
    hub.gated = True
    code, out = run("fetch", "--sources", str(sources), "--dest", str(tmp_path / "m"))
    assert code == 1 and "refused" in out and "export HF_TOKEN" in out
    monkeypatch.setenv("HF_TOKEN", "hf_test-token")
    code, out = run("fetch", "--sources", str(sources), "--dest", str(tmp_path / "m"))
    assert code == 0, out
    assert "hf_test-token" not in out

    code, out = run("fetch", "--model", "gone=demo/missing", "--dest", str(tmp_path / "m"))
    assert code == 1 and "The hub has nothing at" in out and "fix config/model-sources.txt" in out

    sources.write_text("deepseek-v4-pro-fp4 TODO owner/repo\n")
    code, out = run("fetch", "--sources", str(sources), "--dest", str(tmp_path / "m"))
    assert code == 1 and "still says TODO" in out

    code, out = run("fetch", "--model", "bad=nope", "--dest", str(tmp_path / "m"))
    assert code == 1 and "does not name a repository as owner/name" in out

    code, out = run("fetch", "--dest", str(tmp_path / "m"))
    assert code == 1 and "No model was named." in out

    shipped = fm.read_sources(REPO_ROOT / "config" / "model-sources.txt")
    assert [s.repo for s in shipped] == ["BAAI/bge-m3", "BAAI/bge-reranker-v2-m3"], (
        "the shipped file lists only repositories that exist; the rest are TODO comments"
    )
