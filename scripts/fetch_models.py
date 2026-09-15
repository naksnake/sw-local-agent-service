#!/usr/bin/env python3
"""Fetch model weights on a CONNECTED build host and write checksums for the air-gapped box.

Standard library only, so it runs on any host with Python 3.12 and no virtualenv. The
platform host never runs this (INV-1): the weights travel by sneakernet and are verified on
arrival with `--verify`, which needs no network.

    scripts/fetch_models.py fetch  --sources config/model-sources.txt --dest ./models
    scripts/fetch_models.py fetch  --model qwen3.8-27b-fp8=Qwen/Qwen3.8-27B-FP8 --dest ./models
    scripts/fetch_models.py verify --dest /AI/Agent/Models            # on the box, offline

Each model lands in <dest>/<path>/ (the `path` the registry names), with SHA256SUMS beside
the files and one manifest.json for the whole set. Downloads resume; every large file is
checked against the sha256 the hub publishes, and small files are hashed after download.
A token for gated repositories comes from HF_TOKEN in the environment, never from argv;
HF_ENDPOINT points at a mirror inside the perimeter when one exists.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

DEFAULT_ENDPOINT: Final = "https://huggingface.co"
CHUNK: Final = 1 << 20
#: Formats vLLM does not need when safetensors are present; skipping them halves most downloads.
REDUNDANT_WHEN_SAFETENSORS: Final[tuple[str, ...]] = (
    "*.bin",
    "*.h5",
    "*.msgpack",
    "*.ot",
    "*.ckpt",
    "onnx/*",
    "*.onnx",
)


class FetchError(Exception):
    """One problem, in three parts (CLAUDE.md §11)."""

    def __init__(self, what_happened: str, likely_cause: str, what_to_do: str) -> None:
        super().__init__(what_happened)
        self.what_happened = what_happened
        self.likely_cause = likely_cause
        self.what_to_do = what_to_do

    def render(self) -> str:
        return "\n".join(
            (
                self.what_happened,
                f"Likely cause: {self.likely_cause}",
                f"What to do: {self.what_to_do}",
            )
        )


@dataclass(frozen=True, slots=True)
class Source:
    path: str
    repo: str
    revision: str = "main"

    @classmethod
    def parse(cls, text: str) -> Source:
        """`<path>=<repo>[@<revision>]` or `<path> <repo> [<revision>]`."""
        raw = text.strip()
        if "=" in raw and " " not in raw:
            path, rest = raw.split("=", 1)
        else:
            parts = raw.split()
            if len(parts) < 2:
                raise FetchError(
                    f"The source line {raw!r} is incomplete.",
                    "A line names the local directory and the hub repository.",
                    "Write `<path> <owner/repo> [revision]`, for example `bge-m3 BAAI/bge-m3`.",
                )
            path, rest = parts[0], "@".join(parts[1:])
        repo, _, revision = rest.partition("@")
        if "TODO" in raw.upper():
            raise FetchError(
                f"The source line for {path} still says TODO.",
                "The repository id for this model has not been filled in.",
                "Put the hub repository id (owner/name) on that line, or remove the line.",
            )
        if repo.count("/") != 1 or not path:
            raise FetchError(
                f"The source line {raw!r} does not name a repository as owner/name.",
                "Hub repositories are written owner/name, for example BAAI/bge-m3.",
                "Fix the line and run again.",
            )
        return cls(path=path, repo=repo, revision=revision or "main")


def read_sources(path: Path) -> list[Source]:
    sources: list[Source] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        sources.append(Source.parse(stripped))
    if not sources:
        raise FetchError(
            f"{path} lists no models.",
            "Every line is a comment or blank.",
            "Add one line per model: `<path> <owner/repo> [revision]`.",
        )
    return sources


@dataclass(frozen=True, slots=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None  # the hub publishes it for LFS files; small files are hashed after download


Opener = Callable[[urllib.request.Request], Any]


@dataclass
class Hub:
    endpoint: str = DEFAULT_ENDPOINT
    token: str | None = None
    opener: Opener = urllib.request.urlopen
    timeout_s: int = 60

    def _request(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> urllib.request.Request:
        merged = {"User-Agent": "slas-fetch-models/1"}
        if self.token:
            merged["Authorization"] = f"Bearer {self.token}"
        merged.update(headers or {})
        if not url.startswith(("https://", "http://")):
            raise FetchError(
                f"The endpoint {url} is not an http(s) URL.",
                "HF_ENDPOINT must name a web server, not a file or another scheme.",
                "Set HF_ENDPOINT to https://<mirror> or unset it.",
            )
        return urllib.request.Request(url, headers=merged)  # noqa: S310 — scheme checked above

    def _json(self, url: str) -> Any:
        try:
            with self.opener(self._request(url), timeout=self.timeout_s) as response:  # type: ignore[call-arg]
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._http_error(url, exc) from exc
        except urllib.error.URLError as exc:
            raise FetchError(
                f"Could not reach {self.endpoint}.",
                f"No route, a proxy that refuses it, or a TLS problem: {exc.reason}.",
                "Run this on a connected host, or point HF_ENDPOINT at a mirror inside the "
                "perimeter.",
            ) from exc

    def _http_error(self, url: str, exc: urllib.error.HTTPError) -> FetchError:
        if exc.code in (401, 403):
            return FetchError(
                f"The hub refused {url} ({exc.code}).",
                "The repository is gated or private and no valid token was given.",
                "Accept the licence on the hub with your account, then export HF_TOKEN and "
                "run again. The token is read from the environment only.",
            )
        if exc.code == 404:
            return FetchError(
                f"The hub has nothing at {url}.",
                "The repository id or the revision is misspelt, or the model was removed.",
                "Check the id on the hub and fix config/model-sources.txt.",
            )
        return FetchError(
            f"The hub answered {exc.code} for {url}.",
            "A hub or proxy problem.",
            "Try again in a minute; if it repeats, check the proxy log.",
        )

    def revision_sha(self, source: Source) -> str:
        data = self._json(
            f"{self.endpoint}/api/models/{source.repo}/revision/{urllib.parse.quote(source.revision)}"
        )
        sha = data.get("sha")
        return str(sha) if sha else source.revision

    def tree(self, source: Source) -> list[RemoteFile]:
        data = self._json(
            f"{self.endpoint}/api/models/{source.repo}/tree/"
            f"{urllib.parse.quote(source.revision)}?recursive=true"
        )
        files: list[RemoteFile] = []
        for entry in data:
            if entry.get("type") != "file":
                continue
            lfs = entry.get("lfs") or {}
            files.append(
                RemoteFile(
                    path=str(entry["path"]),
                    size=int(entry.get("size") or lfs.get("size") or 0),
                    sha256=str(lfs["oid"]) if lfs.get("oid") else None,
                )
            )
        return files

    def file_url(self, source: Source, remote: RemoteFile) -> str:
        return (
            f"{self.endpoint}/{source.repo}/resolve/{urllib.parse.quote(source.revision)}/"
            f"{urllib.parse.quote(remote.path)}"
        )

    def download(self, url: str, target: Path, *, expected_size: int, log: TextIO) -> None:
        """Resume into `<target>.part`, then rename. Nothing about the token is ever printed."""
        part = target.with_name(target.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if expected_size and have > expected_size:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with self.opener(
                self._request(url, headers=headers), timeout=self.timeout_s
            ) as response:  # type: ignore[call-arg]
                status = getattr(response, "status", 200)
                mode = "ab" if have and status == 206 else "wb"
                if mode == "wb":
                    have = 0
                with part.open(mode) as handle:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        have += len(chunk)
        except urllib.error.HTTPError as exc:
            raise self._http_error(url, exc) from exc
        except urllib.error.URLError as exc:
            raise FetchError(
                f"The download of {target.name} stopped after {have} bytes.",
                f"The connection dropped: {exc.reason}.",
                "Run the same command again; it resumes where it stopped.",
            ) from exc
        if expected_size and have != expected_size:
            raise FetchError(
                f"{target.name} is {have} bytes, but the hub says {expected_size}.",
                "The download was cut short.",
                "Run the same command again; it resumes where it stopped.",
            )
        part.replace(target)
        log.write(f"  fetched {target.name} ({human(have)})\n")


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def select(
    files: list[RemoteFile], *, include: Sequence[str], exclude: Sequence[str]
) -> tuple[list[RemoteFile], list[str]]:
    """Which files to fetch, and the ones skipped because safetensors make them redundant."""
    has_safetensors = any(f.path.endswith(".safetensors") for f in files)
    patterns = list(exclude)
    if has_safetensors:
        patterns += REDUNDANT_WHEN_SAFETENSORS
    chosen: list[RemoteFile] = []
    skipped: list[str] = []
    for remote in files:
        if include and not any(fnmatch.fnmatch(remote.path, p) for p in include):
            skipped.append(remote.path)
            continue
        if any(fnmatch.fnmatch(remote.path, p) for p in patterns):
            skipped.append(remote.path)
            continue
        chosen.append(remote)
    return chosen, skipped


@dataclass
class FetchedModel:
    path: str
    repo: str
    revision: str
    commit: str
    files: dict[str, str] = field(default_factory=dict)  # relative path → sha256
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)

    def sentence(self) -> str:
        skipped = f"; {len(self.skipped)} redundant files skipped" if self.skipped else ""
        return (
            f"{self.path}: {len(self.files)} files, {human(self.bytes)}, from {self.repo} at "
            f"{self.commit[:12]}{skipped}."
        )


def fetch_model(
    hub: Hub,
    source: Source,
    dest: Path,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    log: TextIO,
) -> FetchedModel:
    target_dir = dest / source.path
    commit = hub.revision_sha(source)
    files = hub.tree(source)
    chosen, skipped = select(files, include=include, exclude=exclude)
    if not chosen:
        raise FetchError(
            f"{source.repo} has no files to fetch after filtering.",
            "The include or exclude patterns removed everything.",
            "Loosen the patterns, or check the repository on the hub.",
        )
    log.write(
        f"{source.path} ← {source.repo}@{source.revision} ({commit[:12]}): {len(chosen)} files\n"
    )
    fetched = FetchedModel(
        path=source.path, repo=source.repo, revision=source.revision, commit=commit, skipped=skipped
    )
    for remote in chosen:
        target = target_dir / remote.path
        if target.exists() and (remote.sha256 is None or sha256_of(target) == remote.sha256):
            if remote.sha256 is not None:
                log.write(f"  kept    {remote.path} (already complete)\n")
        else:
            hub.download(hub.file_url(source, remote), target, expected_size=remote.size, log=log)
        actual = sha256_of(target)
        if remote.sha256 is not None and actual != remote.sha256:
            target.unlink()
            raise FetchError(
                f"{source.path}/{remote.path} does not match the checksum the hub publishes.",
                "The file was corrupted in transit or altered by a proxy.",
                "Run the same command again; the bad file was removed and will be fetched anew.",
            )
        fetched.files[remote.path] = actual
        fetched.bytes += target.stat().st_size
    write_sums(target_dir, fetched.files)
    return fetched


def write_sums(target_dir: Path, files: dict[str, str]) -> Path:
    path = target_dir / "SHA256SUMS"
    lines = [f"{digest}  {name}" for name, digest in sorted(files.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        sums[name] = digest
    return sums


def write_manifest(dest: Path, models: list[FetchedModel], *, now: datetime) -> Path:
    path = dest / "manifest.json"
    existing: dict[str, Any] = {}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    entries = {m["path"]: m for m in existing.get("models", [])}
    for model in models:
        entries[model.path] = {
            "path": model.path,
            "repo": model.repo,
            "revision": model.revision,
            "commit": model.commit,
            "files": len(model.files),
            "bytes": model.bytes,
            "fetched_at": now.isoformat(),
        }
    payload = {"generated_at": now.isoformat(), "models": [entries[k] for k in sorted(entries)]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def verify(dest: Path, *, log: TextIO) -> list[str]:
    """Offline: every SHA256SUMS under dest against the files. Returns the problems."""
    problems: list[str] = []
    found = 0
    for sums_path in sorted(dest.glob("*/SHA256SUMS")):
        found += 1
        model_dir = sums_path.parent
        sums = read_sums(sums_path)
        bad = 0
        for name, digest in sums.items():
            file = model_dir / name
            if not file.is_file():
                problems.append(f"{model_dir.name}/{name} is missing.")
                bad += 1
            elif sha256_of(file) != digest:
                problems.append(f"{model_dir.name}/{name} does not match its checksum.")
                bad += 1
        state = "every file matches" if bad == 0 else f"{bad} of {len(sums)} files wrong"
        log.write(f"{model_dir.name}: {len(sums)} files, {state}.\n")
    if found == 0:
        problems.append(f"No model directory under {dest} has a SHA256SUMS file.")
    return problems


def iter_sources(args: argparse.Namespace) -> Iterator[Source]:
    if args.sources:
        yield from read_sources(Path(args.sources))
    for spec in args.model or []:
        yield Source.parse(spec)


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, opener: Opener | None = None
) -> int:
    out = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="fetch_models.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser(
        "fetch", help="Download models and write checksums (connected host)."
    )
    fetch.add_argument(
        "--sources", help="config/model-sources.txt: `<path> <owner/repo> [revision]` per line."
    )
    fetch.add_argument(
        "--model", action="append", help="One model as <path>=<owner/repo>[@revision]; repeatable."
    )
    fetch.add_argument("--dest", required=True, help="Directory that becomes Models/ on the box.")
    fetch.add_argument(
        "--include", action="append", default=[], help="Only files matching this glob (repeatable)."
    )
    fetch.add_argument(
        "--exclude", action="append", default=[], help="Skip files matching this glob (repeatable)."
    )
    check = commands.add_parser(
        "verify", help="Check every SHA256SUMS under --dest; no network (the box)."
    )
    check.add_argument("--dest", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    dest = Path(args.dest)
    try:
        if args.command == "verify":
            problems = verify(dest, log=out)
            if problems:
                out.write("\n".join(problems) + "\n")
                out.write(
                    "What to do: copy the listed files again from the build host and run "
                    "verify again.\n"
                )
                return 1
            out.write(f"Every model under {dest} matches its checksums.\n")
            return 0
        sources = list(iter_sources(args))
        if not sources:
            raise FetchError(
                "No model was named.",
                "Neither --sources nor --model was given.",
                "Pass --sources config/model-sources.txt, or --model <path>=<owner/repo>.",
            )
        hub = Hub(
            endpoint=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
            token=os.environ.get("HF_TOKEN") or None,
        )
        if opener is not None:
            hub.opener = opener
        fetched = [
            fetch_model(hub, source, dest, include=args.include, exclude=args.exclude, log=out)
            for source in sources
        ]
        manifest = write_manifest(dest, fetched, now=datetime.now(UTC))
        for model in fetched:
            out.write(model.sentence() + "\n")
        total = sum(m.bytes for m in fetched)
        out.write(
            f"Done: {len(fetched)} models, {human(total)}, checksums in each SHA256SUMS and "
            f"{manifest}. Copy {dest}/ to /AI/Agent/Models/ on the box and run "
            f"`scripts/fetch_models.py verify --dest /AI/Agent/Models` there.\n"
        )
        return 0
    except FetchError as exc:
        out.write(exc.render() + "\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
