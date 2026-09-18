"""Resumable, checksummed fetches of model weights from the hub, and the offline verify.

Standard library only, so `scripts/fetch_models.py` runs on any host with Python 3.12 and no
virtualenv, and the `model-fetcher` service (ADR-0018) uses the very same code. Each model
lands in `<dest>/<path>/` (the `path` the registry names), with SHA256SUMS beside the files
and one manifest.json for the whole set. Downloads resume; every large file is checked
against the sha256 the hub publishes, every small file against its git blob id, and a file
already on disk is kept only when it matches. A token for gated repositories is sent as a
header only and never printed; HF_ENDPOINT points at a mirror inside the perimeter when one
exists; HTTPS_PROXY is honoured by urllib.

`fetch_planned()` reports progress through a callback (bytes done and total, files done and
total) and stops between files when `cancel()` says so, which is what the service's fetch
records and its Cancel button need.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TextIO

DEFAULT_ENDPOINT: Final = "https://huggingface.co"
CHUNK: Final = 1 << 20
#: Install profiles, in order: each one needs everything the ones before it need.
PROFILES: Final[tuple[str, ...]] = ("quickstart", "prod")
#: Formats vLLM never reads (ONNX, TensorFlow, Flax, Rust, Lightning): always skipped.
ALWAYS_REDUNDANT: Final[tuple[str, ...]] = (
    "onnx/*",
    "*.onnx",
    "*.h5",
    "*.msgpack",
    "*.ot",
    "*.ckpt",
)
#: PyTorch pickles are only needed when a model ships no safetensors (BGE-M3 is one).
REDUNDANT_WHEN_SAFETENSORS: Final[tuple[str, ...]] = ("*.bin",)
#: The hub's own hosts (ADR-0018): the site, its LFS content delivery, and the short domain.
DEFAULT_HUB_HOSTS: Final[tuple[str, ...]] = ("huggingface.co", "cdn-lfs.huggingface.co", "*.hf.co")


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


class FetchCancelledError(FetchError):
    """The caller asked to stop between two files; what was fetched stays on disk."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"The fetch of {path} was cancelled.",
            "Someone pressed Cancel.",
            "Start the same fetch again; it resumes where it stopped.",
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
        if re.search(r"\bTODO\b", raw):
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


def strip_comment(line: str) -> str:
    """Drop a `# …` comment: a `#` counts when it starts the line or follows whitespace."""
    return re.sub(r"(^|\s)#.*$", "", line).strip()


def read_sources(path: Path, *, profile: str | None = None) -> list[Source]:
    """The sources file, optionally narrowed to what one install profile needs.

    A `[quickstart]` or `[prod]` line starts a section. Lines above any section belong to
    every profile; a section's lines belong to that profile and the ones after it in
    PROFILES (prod needs everything quickstart needs). `profile=None` keeps every line.
    """
    if profile is not None and profile not in PROFILES:
        raise FetchError(
            f"The profile {profile!r} is not known.",
            f"Profiles are {', '.join(PROFILES)}.",
            "Pass --profile quickstart or --profile prod.",
        )
    sources: list[Source] = []
    section: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = strip_comment(line)
        if not stripped:
            continue
        header = re.fullmatch(r"\[\s*([A-Za-z0-9_-]+)\s*\]", stripped)
        if header:
            section = header.group(1)
            if section not in PROFILES:
                raise FetchError(
                    f"The section [{section}] in {path.name} is not an install profile.",
                    "Sections name the profile that needs the models below them: "
                    f"{', '.join(PROFILES)}.",
                    "Rename the section, or move its lines under [quickstart] or [prod].",
                )
            continue
        if (
            profile is not None
            and section is not None
            and PROFILES.index(section) > PROFILES.index(profile)
        ):
            continue
        sources.append(Source.parse(stripped))
    if not sources:
        scope = f" for the {profile} profile" if profile else ""
        raise FetchError(
            f"{path} lists no models{scope}.",
            "Every line is a comment, blank, or in a section another profile needs.",
            "Add one line per model: `<path> <owner/repo> [revision]`.",
        )
    return sources


@dataclass(frozen=True, slots=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None  # the hub publishes it for LFS files
    blob_oid: str | None = None  # the git blob id the hub publishes for every other file


Opener = Callable[[urllib.request.Request], Any]


# --- the hub host allowlist (ADR-0018) ---------------------------------------------------------


def host_allowed(host: str, patterns: Sequence[str]) -> bool:
    """`huggingface.co` matches itself; `*.hf.co` matches any name under hf.co."""
    name = host.lower().rstrip(".")
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in patterns)


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def not_allowed(host: str, patterns: Sequence[str]) -> FetchError:
    return FetchError(
        f"{host or 'that address'} is not an allowed model hub host.",
        f"The fetcher may reach only {', '.join(patterns)} (ADR-0018).",
        "Paste a link on one of those hosts, or add the mirror to SLAS_HUB_HOSTS and set "
        "HF_ENDPOINT to it.",
    )


class _AllowlistedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to a host on the allowlist; the hub sends LFS files to its CDN."""

    def __init__(self, patterns: Sequence[str]) -> None:
        super().__init__()
        self.patterns = tuple(patterns)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        host = host_of(newurl)
        if not host_allowed(host, self.patterns):
            raise not_allowed(host, self.patterns)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def allowlisted_opener(patterns: Sequence[str]) -> Opener:
    """An opener that refuses any request or redirect outside `patterns`; proxies from the
    environment (HTTPS_PROXY) are honoured as urllib always does."""
    director = urllib.request.build_opener(_AllowlistedRedirects(patterns))

    def open_(request: urllib.request.Request, timeout: float | None = None) -> Any:
        host = host_of(request.full_url)
        if not host_allowed(host, patterns):
            raise not_allowed(host, patterns)
        return director.open(request, timeout=timeout)

    return open_


# --- the hub ---------------------------------------------------------------------------------


@dataclass
class Hub:
    endpoint: str = DEFAULT_ENDPOINT
    token: str | None = None
    opener: Opener = urllib.request.urlopen
    timeout_s: int = 60
    #: A flaky link (a TLS handshake that times out, a reset) is retried this many more times
    #: with a growing pause; an HTTP answer such as 401 or 404 is never retried.
    retries: int = 4
    retry_pause_s: float = 2.0
    sleep: Callable[[float], None] | None = None  # time.sleep unless a test says otherwise

    def _open(self, request: urllib.request.Request, *, log: TextIO | None = None) -> Any:
        attempt = 0
        while True:
            try:
                return self.opener(request, timeout=self.timeout_s)  # type: ignore[call-arg]
            except urllib.error.HTTPError:
                raise
            except OSError as exc:
                if attempt >= self.retries:
                    raise
                attempt += 1
                pause = self.retry_pause_s * 2 ** (attempt - 1)
                reason = getattr(exc, "reason", None) or exc
                if log is not None:
                    log.write(
                        f"  the hub did not answer ({reason}); trying again in {pause:g} s "
                        f"({attempt} of {self.retries})\n"
                    )
                (self.sleep or time.sleep)(pause)

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

    def _json(self, url: str, *, log: TextIO | None = None) -> Any:
        try:
            with self._open(self._request(url), log=log) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._http_error(url, exc) from exc
        except OSError as exc:  # no route, a refused or reset connection, a TLS or read timeout
            raise self._unreachable(exc) from exc

    def _unreachable(self, exc: OSError) -> FetchError:
        reason = getattr(exc, "reason", None) or exc
        return FetchError(
            f"Could not reach {self.endpoint}.",
            f"No route, a blocked host, a proxy in the way, or a TLS problem: {reason} "
            f"(tried {self.retries + 1} times).",
            "If this site reaches the internet through a proxy, export "
            "HTTPS_PROXY=http://<proxy>:<port> (the script honours it) and run again. If the "
            "hub is blocked here, run this on a connected host, or point HF_ENDPOINT at a "
            "mirror inside the perimeter.",
        )

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

    def revision_sha(self, source: Source, *, log: TextIO | None = None) -> str:
        data = self._json(
            f"{self.endpoint}/api/models/{source.repo}/revision/{urllib.parse.quote(source.revision)}",
            log=log,
        )
        sha = data.get("sha")
        return str(sha) if sha else source.revision

    def tree(self, source: Source, *, log: TextIO | None = None) -> list[RemoteFile]:
        data = self._json(
            f"{self.endpoint}/api/models/{source.repo}/tree/"
            f"{urllib.parse.quote(source.revision)}?recursive=true",
            log=log,
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
                    # For an LFS file the entry's oid is the pointer's blob, not the content's.
                    blob_oid=str(entry["oid"]) if entry.get("oid") and not lfs else None,
                )
            )
        return files

    def file_url(self, source: Source, remote: RemoteFile) -> str:
        return (
            f"{self.endpoint}/{source.repo}/resolve/{urllib.parse.quote(source.revision)}/"
            f"{urllib.parse.quote(remote.path)}"
        )

    def download(
        self,
        url: str,
        target: Path,
        *,
        expected_size: int,
        log: TextIO,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Resume into `<target>.part`, then rename. Nothing about the token is ever printed.

        A link that drops mid-file is picked up where it stopped, inside this run, with a
        Range request and the same growing pause as a failed connection; the attempt counter
        resets whenever bytes arrive, so a slow flaky link finishes as long as it makes
        progress, and only a link that stalls `retries` times in a row without progress ends
        the run. `progress(bytes_on_disk)` is called after every chunk.
        """
        part = part_of(target)
        part.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if expected_size and have > expected_size:
            part.unlink()
            have = 0
        if progress is not None:
            progress(have)
        stalls = 0
        while True:
            before = have
            headers = {"Range": f"bytes={have}-"} if have else {}
            reason: object | None = None
            try:
                with self._open(self._request(url, headers=headers), log=log) as response:
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
                            if progress is not None:
                                progress(have)
            except urllib.error.HTTPError as exc:
                raise self._http_error(url, exc) from exc
            except OSError as exc:  # the connection dropped or timed out mid-file
                reason = getattr(exc, "reason", None) or exc
            if reason is None:
                if not expected_size or have >= expected_size:
                    break
                reason = "the connection closed early"  # a short body, no exception
            stalls = 0 if have > before else stalls + 1
            if stalls > self.retries:
                raise FetchError(
                    f"The download of {target.name} stopped after {have} bytes.",
                    f"The connection dropped {self.retries + 1} times in a row without "
                    f"progress: {reason}.",
                    "Run the same command again; it resumes where it stopped. If this site "
                    "uses a proxy, export HTTPS_PROXY=http://<proxy>:<port> first.",
                )
            pause = self.retry_pause_s * 2 ** max(stalls - 1, 0)
            log.write(
                f"  {target.name} stalled at {human(have)} ({reason}); resuming in {pause:g} s\n"
            )
            (self.sleep or time.sleep)(pause)
        if expected_size and have != expected_size:
            raise FetchError(
                f"{target.name} is {have} bytes, but the hub says {expected_size}.",
                "The download was cut short.",
                "Run the same command again; it resumes where it stopped.",
            )
        part.replace(target)
        log.write(f"  fetched {target.name} ({human(have)})\n")


def part_of(target: Path) -> Path:
    return target.with_name(target.name + ".part")


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if unit == "TiB" or round(value, 1) < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def blob_sha1(path: Path) -> str:
    """The git blob id of a file: what the hub publishes for files it does not keep in LFS."""
    digest = hashlib.sha1(b"blob %d\0" % path.stat().st_size)  # noqa: S324 — a git id, not security
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def is_complete(target: Path, remote: RemoteFile) -> bool:
    """Does the file on disk match what the hub describes?

    sha256 for LFS files, the git blob id for the others, the size when neither is known.
    """
    if not target.is_file():
        return False
    if remote.sha256 is not None:
        return sha256_of(target) == remote.sha256
    if remote.blob_oid is not None:
        return blob_sha1(target) == remote.blob_oid
    return not remote.size or target.stat().st_size == remote.size


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and rename, so a reader (or a hard link) never sees a
    half-written file and an existing hard link is not rewritten in place."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def select(
    files: list[RemoteFile], *, include: Sequence[str], exclude: Sequence[str]
) -> tuple[list[RemoteFile], list[str]]:
    """Which files to fetch, and the ones skipped because they are redundant."""
    has_safetensors = any(f.path.endswith(".safetensors") for f in files)
    patterns = [*exclude, *ALWAYS_REDUNDANT]
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
    sizes: dict[str, int] = field(default_factory=dict)  # relative path → bytes on disk
    #: True when the model was found complete on disk and nothing was downloaded.
    kept: bool = False

    def sentence(self) -> str:
        skipped = f"; {len(self.skipped)} redundant files skipped" if self.skipped else ""
        return (
            f"{self.path}: {len(self.files)} files, {human(self.bytes)}, from {self.repo} at "
            f"{self.commit[:12]}{skipped}."
        )


_HEX = re.compile(r"^[0-9a-f]{7,64}$")


def already_here(dest: Path, source: Source) -> FetchedModel | None:
    """The model as an earlier run left it, when nothing needs the hub: the manifest names the
    same pinned commit, the checksum file is there, and every file it lists is on disk with the
    size the manifest recorded. A revision that is a branch name never takes this path (it can
    move); a missing or shorter file falls through to the full check, which hashes and resumes.
    """
    if not _HEX.match(source.revision.lower()):
        return None
    entry = next(
        (
            m
            for m in read_manifest(dest / "manifest.json").get("models", [])
            if m.get("path") == source.path
        ),
        None,
    )
    if entry is None:
        return None
    commit = str(entry.get("commit", "")).lower()
    if not commit.startswith(source.revision.lower()):
        return None
    sums_path = dest / source.path / "SHA256SUMS"
    if not sums_path.is_file():
        return None
    sums = read_sums(sums_path)
    if not sums:
        return None
    recorded = entry.get("sizes") if isinstance(entry.get("sizes"), dict) else {}
    sizes: dict[str, int] = {}
    for name in sums:
        target = dest / source.path / name
        if not target.is_file():
            return None
        size = target.stat().st_size
        expected = recorded.get(name)
        if (expected is not None and size != int(expected)) or size == 0:
            return None
        sizes[name] = size
    return FetchedModel(
        path=source.path,
        repo=str(entry.get("repo", source.repo)),
        revision=source.revision,
        commit=commit,
        files=sums,
        bytes=sum(sizes.values()),
        sizes=sizes,
        kept=True,
    )


@dataclass
class ModelPlan:
    """What one model needs before a byte is downloaded: files, sizes, what is already here."""

    source: Source
    commit: str
    files: list[RemoteFile]
    skipped: list[str]
    present: list[str] = field(default_factory=list)  # relative paths already complete
    partial: dict[str, int] = field(default_factory=dict)  # relative path → bytes of a .part

    @property
    def bytes_total(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def bytes_resumed(self) -> int:
        return sum(self.partial.values())

    @property
    def bytes_to_fetch(self) -> int:
        have = set(self.present)
        return sum(f.size - self.partial.get(f.path, 0) for f in self.files if f.path not in have)

    def sentence(self) -> str:
        source = self.source
        text = (
            f"{source.path} ← {source.repo}@{source.revision} ({self.commit[:12]}): "
            f"{len(self.files)} files, {human(self.bytes_total)}"
        )
        if self.present or self.partial:
            text += f", {human(self.bytes_to_fetch)} still to fetch"
        if self.partial:
            text += f" ({human(self.bytes_resumed)} already here resumes)"
        if self.skipped:
            text += f"; {len(self.skipped)} redundant files skipped"
        return text


def plan_model(
    hub: Hub,
    source: Source,
    dest: Path,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    log: TextIO | None = None,
) -> ModelPlan:
    commit = hub.revision_sha(source, log=log)
    files = hub.tree(source, log=log)
    chosen, skipped = select(files, include=include, exclude=exclude)
    if not chosen:
        raise FetchError(
            f"{source.repo} has no files to fetch after filtering.",
            "The include or exclude patterns removed everything.",
            "Loosen the patterns, or check the repository on the hub.",
        )
    target_dir = dest / source.path
    plan = ModelPlan(source=source, commit=commit, files=chosen, skipped=skipped)
    for remote in chosen:
        target = target_dir / remote.path
        if is_complete(target, remote):
            plan.present.append(remote.path)
            continue
        part = part_of(target)
        if part.is_file() and (not remote.size or part.stat().st_size <= remote.size):
            plan.partial[remote.path] = part.stat().st_size
    return plan


def free_bytes(dest: Path) -> int:
    """Free space on the volume that will hold dest (its nearest existing parent)."""
    probe = dest.resolve()
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    return shutil.disk_usage(probe).free


def check_disk(plans: Sequence[ModelPlan], dest: Path, *, log: TextIO) -> None:
    needed = sum(plan.bytes_to_fetch for plan in plans)
    total = sum(plan.bytes_total for plan in plans)
    free = free_bytes(dest)
    noun = "model" if len(plans) == 1 else "models"
    log.write(
        f"Total: {len(plans)} {noun}, {human(total)}; {human(needed)} still to fetch; "
        f"{human(free)} free at {dest}.\n"
    )
    if needed > free:
        raise FetchError(
            f"Not enough free disk at {dest}: {human(needed)} is still to fetch and "
            f"{human(free)} is free, {needed - free:,} bytes short.",
            "The destination volume is too small for this set of models.",
            "Free space or point --dest at a larger volume, then run the same command again; "
            "a download that was cut short resumes and is not counted twice.",
        )


@dataclass(frozen=True, slots=True)
class Progress:
    """Where one model's fetch stands, for a progress bar and a sentence."""

    file: str
    bytes_done: int  # across the model: complete files plus the current file's bytes
    bytes_total: int
    files_done: int
    files_total: int


ProgressCallback = Callable[[Progress], None]


def fetch_planned(
    hub: Hub,
    plan: ModelPlan,
    dest: Path,
    *,
    log: TextIO,
    progress: ProgressCallback | None = None,
    cancel: Callable[[], bool] | None = None,
) -> FetchedModel:
    """Download what the plan says is missing, verify every file, write SHA256SUMS.

    `progress` is called with the running totals after every chunk and every file; `cancel`
    is asked between files, and a True answer raises `FetchCancelledError` with every finished
    file kept and the current `.part` left for the next run to resume.
    """
    source = plan.source
    target_dir = dest / source.path
    log.write(
        f"{source.path} ← {source.repo}@{source.revision} ({plan.commit[:12]}): "
        f"{len(plan.files)} files\n"
    )
    fetched = FetchedModel(
        path=source.path,
        repo=source.repo,
        revision=source.revision,
        commit=plan.commit,
        skipped=plan.skipped,
    )
    present = set(plan.present)
    total = plan.bytes_total
    done_before = 0

    def report(file: str, file_bytes: int) -> None:
        if progress is not None:
            progress(
                Progress(
                    file=file,
                    bytes_done=min(done_before + file_bytes, total) if total else 0,
                    bytes_total=total,
                    files_done=len(fetched.files),
                    files_total=len(plan.files),
                )
            )

    def progress_of(name: str) -> Callable[[int], None]:
        def on_bytes(have: int) -> None:
            report(name, have)

        return on_bytes

    for remote in plan.files:
        if cancel is not None and cancel():
            raise FetchCancelledError(source.path)
        target = target_dir / remote.path
        if remote.path in present:
            log.write(f"  kept    {remote.path} (already complete)\n")
            actual = remote.sha256 if remote.sha256 is not None else sha256_of(target)
        else:
            hub.download(
                hub.file_url(source, remote),
                target,
                expected_size=remote.size,
                log=log,
                progress=progress_of(remote.path),
            )
            actual = sha256_of(target)
            if remote.sha256 is not None and actual != remote.sha256:
                target.unlink()
                raise FetchError(
                    f"{source.path}/{remote.path} does not match the checksum the hub publishes.",
                    "The file was corrupted in transit or altered by a proxy.",
                    "Run the same command again; the bad file was removed and will be fetched "
                    "anew.",
                )
            if remote.blob_oid is not None and blob_sha1(target) != remote.blob_oid:
                target.unlink()
                raise FetchError(
                    f"{source.path}/{remote.path} does not match the git blob id the hub "
                    "publishes.",
                    "The file was corrupted in transit or altered by a proxy.",
                    "Run the same command again; the bad file was removed and will be fetched "
                    "anew.",
                )
        fetched.files[remote.path] = actual
        fetched.sizes[remote.path] = target.stat().st_size
        fetched.bytes += fetched.sizes[remote.path]
        done_before += remote.size
        report(remote.path, 0)
    write_sums(target_dir, fetched.files)
    return fetched


def write_sums(target_dir: Path, files: dict[str, str]) -> Path:
    path = target_dir / "SHA256SUMS"
    lines = [f"{digest}  {name}" for name, digest in sorted(files.items())]
    write_atomic(path, "\n".join(lines) + "\n")
    return path


def read_sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        sums[name] = digest
    return sums


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"models": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"models": []}


def write_manifest(dest: Path, models: list[FetchedModel], *, now: datetime) -> Path:
    path = dest / "manifest.json"
    entries = {m["path"]: m for m in read_manifest(path).get("models", [])}
    for model in models:
        entries[model.path] = {
            "path": model.path,
            "repo": model.repo,
            "revision": model.revision,
            "commit": model.commit,
            "files": len(model.files),
            "bytes": model.bytes,
            "sizes": dict(sorted(model.sizes.items())),
            "fetched_at": now.isoformat(),
        }
    payload = {"generated_at": now.isoformat(), "models": [entries[k] for k in sorted(entries)]}
    write_atomic(path, json.dumps(payload, indent=2) + "\n")
    return path


def merge_manifest(dest: Path, source: Path, *, now: datetime) -> tuple[int, int]:
    """Fold the entries of `source` into dest/manifest.json by path. Returns (added, updated)."""
    if not source.is_file():
        raise FetchError(
            f"{source} does not exist.",
            "The models directory carries no manifest.json.",
            "Run the fetch again on the connected host; it writes one.",
        )
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "manifest.json"
    entries = {m["path"]: m for m in read_manifest(target).get("models", [])}
    added = updated = 0
    for entry in read_manifest(source).get("models", []):
        if entry["path"] in entries:
            updated += entries[entry["path"]] != entry
        else:
            added += 1
        entries[entry["path"]] = entry
    payload = {"generated_at": now.isoformat(), "models": [entries[k] for k in sorted(entries)]}
    write_atomic(target, json.dumps(payload, indent=2) + "\n")
    return added, updated


def verify(dest: Path, *, log: TextIO, only: Sequence[str] = ()) -> list[str]:
    """Offline: every SHA256SUMS under dest (or only the named models) against the files.

    Returns the problems as sentences.
    """
    problems: list[str] = []
    found = 0
    wanted = set(only)
    for name in sorted(wanted - {p.parent.name for p in dest.glob("*/SHA256SUMS")}):
        problems.append(f"{dest / name} has no SHA256SUMS file.")
    for sums_path in sorted(dest.glob("*/SHA256SUMS")):
        if wanted and sums_path.parent.name not in wanted:
            continue
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
    if found == 0 and not wanted:
        problems.append(f"No model directory under {dest} has a SHA256SUMS file.")
    return problems
