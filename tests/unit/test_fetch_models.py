"""scripts/fetch_models.py against a fake hub on loopback: tree listing, the plan and disk
check, resume, checksums (sha256 for LFS files, git blob ids for small ones),
redundant-format skipping, sections and profiles, the offline verify, manifest merging, and
the three-part errors."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

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


def blob_id(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()  # noqa: S324


class FakeHub:
    """Enough of the hub's API for the script: tree, revision, resolve with Range support."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.cut_after: int | None = None  # serve at most this many bytes once, then behave
        self.stall_ranges = False  # answer every Range request with headers and no body
        self.gated = False
        self.corrupt = False
        self.files: dict[str, bytes] = {
            "model.safetensors": WEIGHTS,
            "config.json": CONFIG,
            "README.md": README,
            "pytorch_model.bin": BIN,
            "onnx/model.onnx": b"onnx",
        }
        outer = self

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
                    for name, content in outer.files.items():
                        entry: dict[str, object] = {
                            "type": "file",
                            "path": name,
                            "size": len(content),
                            "oid": blob_id(content),
                        }
                        if len(content) > 100:  # the hub keeps large files in LFS
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
                # Any revision resolves: the pinned commit and the branch serve the same files.
                revision = re.match(r"^/demo/tiny/resolve/[^/]+/", self.path)
                prefix = revision.group(0) if revision else "/demo/tiny/resolve/main/"
                if self.path.startswith(prefix):
                    name = self.path[len(prefix) :]
                    data: bytes | None = outer.files.get(name)
                    if data is None:
                        self._send(404, b"")
                        return
                    if outer.corrupt and name == "model.safetensors":
                        data = b"x" + data[1:]
                    range_header = self.headers.get("Range")
                    if range_header:
                        start = int(range_header.removeprefix("bytes=").rstrip("-"))
                        if outer.stall_ranges:  # the link drops before a single byte arrives
                            self.send_response(206)
                            self.send_header("Content-Length", str(len(data) - start))
                            self.end_headers()
                            self.wfile.flush()
                            self.connection.close()
                            return
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
    assert sums["config.json"] == hashlib.sha256(CONFIG).hexdigest()
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
    assert "Done: 1 model, 1.0 MiB," in out
    assert f"Next: on the platform host run `./install.sh --models {dest}`" in out
    # The plan came first: every model listed with its size, then the disk check.
    assert out.index("Total: 1 model, 1.0 MiB; 1.0 MiB still to fetch;") < out.index("fetched ")
    # Every request carried no token, and the token never appears in the output.
    assert "Bearer" not in out


def test_dry_run_lists_sizes_checks_the_disk_and_downloads_nothing(
    hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "models"
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest), "--dry-run")
    assert code == 0, out
    assert (
        "tiny ← demo/tiny@main (abc123def456): 3 files, 1.0 MiB; 2 redundant files skipped" in out
    )
    assert "Total: 1 model, 1.0 MiB; 1.0 MiB still to fetch;" in out and f"free at {dest}." in out
    assert "Dry run: nothing was downloaded." in out
    assert not dest.exists()
    assert not any("/resolve/" in path for path, _ in hub.requests), "a dry run fetches no file"

    assert run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))[0] == 0
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest), "--dry-run")
    assert code == 0 and "3 files, 1.0 MiB, 0 B still to fetch;" in out

    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1000))
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(tmp_path / "small"))
    assert code == 1
    assert "Not enough free disk at" in out and "1000 B is free, 1,047,611 bytes short." in out
    assert "point --dest at a larger volume" in out
    assert not (tmp_path / "small").exists(), "the disk check runs before any download"


def test_a_cut_download_resumes_inside_the_run(
    hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(fm.time, "sleep", pauses.append)
    dest = tmp_path / "models"
    hub.cut_after = 300_000
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0, out
    assert (
        "model.safetensors stalled at 293.0 KiB (the connection closed early); resuming in 2 s"
        in out
    )
    assert pauses == [2.0]
    assert (dest / "tiny" / "model.safetensors").read_bytes() == WEIGHTS
    ranges = [r for path, r in hub.requests if path.endswith("/model.safetensors") and r]
    assert ranges == ["bytes=300000-"], "picked up where it stopped, in the same run"
    assert not (dest / "tiny" / "model.safetensors.part").exists()


def test_a_link_that_stalls_without_progress_gives_up_and_the_disk_check_counts_the_part(
    hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(fm.time, "sleep", pauses.append)
    dest = tmp_path / "models"
    hub.cut_after = 300_000
    hub.stall_ranges = True
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 1
    assert "The download of model.safetensors stopped after 300000 bytes." in out
    assert "dropped 5 times in a row without progress: the connection closed early." in out
    assert "it resumes where it stopped" in out
    assert pauses == [2.0, 2.0, 4.0, 8.0, 16.0], "one pause per stall, growing without progress"
    assert (dest / "tiny" / "model.safetensors.part").stat().st_size == 300_000

    # Room only for the remainder: the plan subtracts the .part and the rerun goes ahead.
    hub.stall_ranges = False
    hub.requests.clear()
    remainder = 1_048_576 - 300_000 + len(CONFIG) + len(README)
    real_disk_usage = fm.shutil.disk_usage
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _path: SimpleNamespace(free=remainder + 10))
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0, out
    assert "3 files, 1.0 MiB, 731.1 KiB still to fetch (293.0 KiB already here resumes)" in out
    assert (dest / "tiny" / "model.safetensors").read_bytes() == WEIGHTS
    ranges = [r for path, r in hub.requests if path.endswith("/model.safetensors") and r]
    assert ranges == ["bytes=300000-"]
    monkeypatch.setattr(fm.shutil, "disk_usage", real_disk_usage)

    hub.requests.clear()
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0 and "kept    model.safetensors (already complete)" in out
    assert not any(path.endswith("/model.safetensors") for path, _ in hub.requests)


def test_a_changed_small_file_is_fetched_again_and_a_corrupted_one_is_removed(
    hub: FakeHub, tmp_path: Path
) -> None:
    dest = tmp_path / "models"
    assert run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))[0] == 0
    # Upstream moved (a new commit): config.json has other bytes of the same length.
    hub.files["config.json"] = b'{"architectures": ["Demo"]} \n'[: len(CONFIG)]
    assert len(hub.files["config.json"]) == len(CONFIG)
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0, out
    assert "kept    config.json" not in out and "fetched config.json" in out
    assert (dest / "tiny" / "config.json").read_bytes() == hub.files["config.json"]
    sums = fm.read_sums(dest / "tiny" / "SHA256SUMS")
    assert sums["config.json"] == hashlib.sha256(hub.files["config.json"]).hexdigest()

    hub.corrupt = True
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(tmp_path / "other"))
    assert code == 1
    assert "tiny/model.safetensors does not match the checksum the hub publishes." in out
    assert not (tmp_path / "other" / "tiny" / "model.safetensors").exists()


def test_a_model_without_safetensors_keeps_its_pytorch_weights(
    hub: FakeHub, tmp_path: Path
) -> None:
    del hub.files["model.safetensors"]
    hub.files["tf_model.h5"] = b"tensorflow"
    hub.files["flax_model.msgpack"] = b"flax"
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(tmp_path))
    assert code == 0, out
    assert (tmp_path / "tiny" / "pytorch_model.bin").read_bytes() == BIN
    for skipped in ("tf_model.h5", "flax_model.msgpack", "onnx"):
        assert not (tmp_path / "tiny" / skipped).exists()
    assert set(fm.read_sums(tmp_path / "tiny" / "SHA256SUMS")) == {
        "pytorch_model.bin",
        "config.json",
        "README.md",
    }
    assert "3 redundant files skipped" in out


def test_verify_is_offline_names_what_is_wrong_and_can_be_limited(
    hub: FakeHub, tmp_path: Path
) -> None:
    dest = tmp_path / "models"
    assert run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))[0] == 0
    hub.requests.clear()
    code, out = run("verify", "--dest", str(dest))
    assert code == 0 and "tiny: 3 files, every file matches." in out
    assert hub.requests == [], "verify never touches the network"
    code, out = run("verify", "--dest", str(dest), "--model", "tiny")
    assert code == 0 and out.endswith("tiny matches its checksums.\n")
    code, out = run("verify", "--dest", str(dest), "--model", "tiny", "--model", "nope")
    assert code == 1 and f"{dest / 'nope'} has no SHA256SUMS file." in out
    (dest / "tiny" / "config.json").write_bytes(b"{}")
    (dest / "tiny" / "README.md").unlink()
    code, out = run("verify", "--dest", str(dest))
    assert code == 1
    assert "tiny/config.json does not match its checksum." in out
    assert "tiny/README.md is missing." in out
    assert "copy the listed files again from the build host" in out
    code, out = run("verify", "--dest", str(tmp_path / "empty"))
    assert code == 1 and "has a SHA256SUMS file" in out


def test_merge_manifest_folds_entries_by_path(tmp_path: Path) -> None:
    target = tmp_path / "Models"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps({"models": [{"path": "a", "commit": "1"}, {"path": "b", "commit": "1"}]})
    )
    source = tmp_path / "carried.json"
    source.write_text(
        json.dumps({"models": [{"path": "b", "commit": "2"}, {"path": "c", "commit": "1"}]})
    )
    code, out = run("merge-manifest", "--dest", str(target), "--from", str(source))
    assert code == 0 and "1 models added, 1 updated" in out
    merged = json.loads((target / "manifest.json").read_text())
    assert [(m["path"], m["commit"]) for m in merged["models"]] == [
        ("a", "1"),
        ("b", "2"),
        ("c", "1"),
    ]
    assert "generated_at" in merged
    code, out = run("merge-manifest", "--dest", str(tmp_path / "fresh"), "--from", str(source))
    assert code == 0 and json.loads((tmp_path / "fresh" / "manifest.json").read_text())["models"]
    code, out = run("merge-manifest", "--dest", str(target), "--from", str(tmp_path / "none.json"))
    assert code == 1 and "does not exist." in out and "Likely cause:" in out


def test_sources_file_gated_repo_and_errors_speak_in_three_parts(
    hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text("# comment\n\ntiny demo/tiny main   # the demo model\n")
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

    code, out = run(
        "fetch", "--profile", "prod", "--model", "tiny=demo/tiny", "--dest", str(tmp_path / "m")
    )
    assert code == 1 and "--profile was given without --sources." in out


def test_sources_sections_select_the_models_a_profile_needs(hub: FakeHub, tmp_path: Path) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text(
        "# shared by every profile\ncommon demo/common\n"
        "[quickstart]   # the small set\nsmall demo/small\n"
        "[ prod ]\nlarge demo/large abc\n"
    )
    assert [s.path for s in fm.read_sources(sources)] == ["common", "small", "large"]
    assert [s.path for s in fm.read_sources(sources, profile="quickstart")] == ["common", "small"]
    prod = fm.read_sources(sources, profile="prod")
    assert [s.path for s in prod] == ["common", "small", "large"] and prod[-1].revision == "abc"
    # A repository whose name merely contains "todo" is not a placeholder.
    assert fm.Source.parse("autodoc someone/AutoDoc-7B").repo == "someone/AutoDoc-7B"

    with pytest.raises(fm.FetchError) as bad_profile:
        fm.read_sources(sources, profile="staging")
    assert "The profile 'staging' is not known." in bad_profile.value.what_happened

    sources.write_text("[staging]\nx demo/x\n")
    with pytest.raises(fm.FetchError) as bad_section:
        fm.read_sources(sources)
    assert "The section [staging] in sources.txt is not an install profile." in str(
        bad_section.value
    )

    sources.write_text("[prod]\nlarge demo/large\n")
    with pytest.raises(fm.FetchError) as empty:
        fm.read_sources(sources, profile="quickstart")
    assert "lists no models for the quickstart profile." in empty.value.what_happened

    # Through the command line: the prod section is left out for quickstart.
    sources.write_text("tiny demo/tiny\n[prod]\nbig demo/missing\n")
    code, out = run(
        "fetch", "--sources", str(sources), "--profile", "quickstart", "--dest", str(tmp_path / "q")
    )
    assert code == 0, out
    assert (tmp_path / "q" / "tiny" / "SHA256SUMS").exists() and "big" not in out


def test_the_shipped_sources_are_pinned_and_match_the_profile_registries() -> None:
    from slas_model_manager.registry import PROFILE_REGISTRIES

    shipped = REPO_ROOT / "config" / "model-sources.txt"
    every = fm.read_sources(shipped)
    assert {s.repo for s in every} == {
        "deepseek-ai/DeepSeek-V4-Flash",
        "Qwen/Qwen3.8-27B-FP8",
        "BAAI/bge-m3",
        "BAAI/bge-reranker-v2-m3",
        "deepseek-ai/DeepSeek-V4-Pro",
        "MiniMaxAI/MiniMax-M2.7",
        "Qwen/Qwen3.8-27B",
    }
    for source in every:
        assert re.fullmatch(r"[0-9a-f]{40}", source.revision), f"{source.path} is not pinned"
    for profile, registry in PROFILE_REGISTRIES.items():
        models = cast(list[dict[str, object]], registry["models"])
        wanted = {str(model["path"]) for model in models}
        fetched = {s.path for s in fm.read_sources(shipped, profile=profile)}
        assert fetched == wanted, (
            f"{profile}: sources and models.{profile}.yaml name different paths"
        )
    assert len(fm.read_sources(shipped, profile="quickstart")) < len(every)
    # The commented example lines are ready to uncomment: full commits, no TODO.
    for line in shipped.read_text().splitlines():
        if re.match(r"# [a-z0-9.-]+\s+\S+/\S+\s+[0-9a-f]+$", line):
            assert re.search(r"\s[0-9a-f]{40}$", line), line


def test_human_sizes_round_to_the_next_unit() -> None:
    assert fm.human(1048552) == "1.0 MiB"
    assert fm.human(1023) == "1023 B"
    assert fm.human(100 * 2**30 + 40 * 2**20) == "100.0 GiB"


def test_a_timeout_or_blocked_host_is_reported_in_three_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(fm.time, "sleep", pauses.append)

    def hanging_opener(_request: object, timeout: int = 0) -> object:
        raise TimeoutError("_ssl.c:983: The handshake operation timed out")

    out = io.StringIO()
    code = fm.main(
        ["fetch", "--model", "tiny=demo/tiny", "--dest", str(tmp_path)],
        stdout=out,
        opener=hanging_opener,
    )
    text = out.getvalue()
    assert code == 1
    assert pauses == [2.0, 4.0, 8.0, 16.0], "four retries with a growing pause"
    assert text.count("the hub did not answer") == 4
    assert "Could not reach https://huggingface.co." in text
    assert (
        "Likely cause: No route, a blocked host, a proxy in the way, or a TLS problem: _ssl.c:983"
        in text
    )
    assert "(tried 5 times)." in text
    assert "export HTTPS_PROXY=http://<proxy>:<port>" in text and "HF_ENDPOINT" in text
    assert "Traceback" not in text


def test_a_flaky_link_is_retried_and_an_http_answer_is_not(hub: FakeHub, tmp_path: Path) -> None:
    real_open = fm.urllib.request.urlopen
    failures = {"left": 2}
    pauses: list[float] = []

    def flaky(request: object, timeout: int = 0) -> object:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise TimeoutError("The handshake operation timed out")
        return real_open(request, timeout=timeout)

    out = io.StringIO()
    made = fm.Hub(endpoint=hub.endpoint, opener=flaky, sleep=pauses.append)
    plan = fm.plan_model(made, fm.Source.parse("tiny=demo/tiny"), tmp_path, log=out)
    assert plan.commit == "abc123def4567890"
    assert pauses == [2.0, 4.0]
    assert "trying again in 2 s (1 of 4)" in out.getvalue()
    assert out.getvalue().count("the hub did not answer") == 2

    pauses.clear()
    with pytest.raises(fm.FetchError) as missing:
        fm.plan_model(made, fm.Source.parse("gone=demo/missing"), tmp_path, log=out)
    assert "The hub has nothing at" in missing.value.what_happened
    assert pauses == [], "a 404 is an answer, not a flaky link"


def test_a_model_fetched_earlier_is_kept_without_asking_the_hub_or_hashing(
    hub: FakeHub, tmp_path: Path
) -> None:
    """A second run finds the manifest, the checksum file and every file at its recorded size,
    says so, and never opens a connection (the weights are far too large to fetch twice)."""
    dest = tmp_path / "models"
    code, out = run("fetch", "--model", "tiny=demo/tiny@abc123def4567890", "--dest", str(dest))
    assert code == 0, out
    manifest = json.loads((dest / "manifest.json").read_text())
    (entry,) = manifest["models"]
    assert entry["sizes"]["model.safetensors"] == len(WEIGHTS)

    hub.requests.clear()
    code, out = run("fetch", "--model", "tiny=demo/tiny@abc123def4567890", "--dest", str(dest))
    assert code == 0, out
    assert (
        f"tiny: already complete at {dest / 'tiny'} (3 files, 1.0 MiB); nothing to download." in out
    )
    assert "The model is already here; nothing was downloaded." in out
    assert hub.requests == [], "no listing, no download"
    assert "Total:" not in out and "fetched " not in out

    # A branch name can move: the fast path is only for a pinned commit.
    code, out = run("fetch", "--model", "tiny=demo/tiny", "--dest", str(dest))
    assert code == 0 and hub.requests, "main is listed on the hub again"
    assert "kept    model.safetensors (already complete)" in out

    # A file that shrank breaks the fast path and the full check fetches it again.
    (dest / "tiny" / "config.json").write_bytes(CONFIG[:3])
    hub.requests.clear()
    code, out = run("fetch", "--model", "tiny=demo/tiny@abc123def4567890", "--dest", str(dest))
    assert code == 0 and "already complete at" not in out
    assert any(path.endswith("/config.json") for path, _ in hub.requests)
    assert (dest / "tiny" / "config.json").read_bytes() == CONFIG

    # Two models, one already here: the done line counts what moved and what stayed.
    hub.requests.clear()
    code, out = run(
        "fetch",
        "--model",
        "tiny=demo/tiny@abc123def4567890",
        "--model",
        "twin=demo/tiny@abc123def4567890",
        "--dest",
        str(dest),
    )
    assert code == 0, out
    assert "tiny: already complete at" in out
    assert "Done: 1 model, 1.0 MiB," in out and "1 already here, untouched." in out
