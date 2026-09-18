"""A pasted hub link → what the registry needs to know about the model (ADR-0018).

Accepted forms, and nothing else (a refusal names them):

    https://huggingface.co/<owner>/<repo>
    https://huggingface.co/<owner>/<repo>/tree/<revision>
    https://huggingface.co/<owner>/<repo>/commit/<sha>
    hf.co/<owner>/<repo>            (and the same two suffixes; `https://` may be left out)
    <owner>/<repo>[@<revision>]     (a bare repository id)

From the link and the fetched files the fetcher derives the registry entry: the id (a slug
of the repository name, unique in the registry, the person may override it), the `path`
under Models/ (the same as the id), `family` from the owner, `quant` from the repository
name, `context` from `config.json` and a `vram_gib` estimate from the bytes on disk. Every
estimate is said to be one on the Models page.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from slas_fetch import Source, host_allowed
from slas_schemas.errors import ThreePartMessage

ACCEPTED_FORMS: Final[tuple[str, ...]] = (
    "https://huggingface.co/<owner>/<repo>",
    "https://huggingface.co/<owner>/<repo>/tree/<revision>",
    "https://huggingface.co/<owner>/<repo>/commit/<sha>",
    "hf.co/<owner>/<repo>",
    "<owner>/<repo>[@revision]",
)

#: Hub owners → the family name the Consensus Router decorrelates voters by (CLAUDE.md §5.3).
FAMILIES: Final[dict[str, str]] = {
    "deepseek-ai": "DeepSeek",
    "qwen": "Qwen",
    "baai": "BAAI",
    "minimaxai": "MiniMax",
    "meta-llama": "Meta",
    "mistralai": "Mistral",
    "moonshotai": "Kimi",
    "zai-org": "GLM",
    "thudm": "GLM",
    "google": "Google",
    "microsoft": "Microsoft",
    "openai": "OpenAI",
    "nvidia": "NVIDIA",
    "ibm-granite": "IBM",
}

#: What the registry admits (`slas_model_manager.registry.Quant`); anything else needs an ADR.
ADMITTED_QUANTS: Final[tuple[str, ...]] = ("fp8", "awq4", "bf16")
#: Quantisations a repository name may carry that the registry does not admit yet.
UNADMITTED_QUANTS: Final[tuple[str, ...]] = (
    "gptq",
    "nvfp4",
    "mxfp4",
    "fp4",
    "int4",
    "int8",
    "gguf",
)

DEFAULT_CONTEXT: Final = 32768
MIN_CONTEXT: Final = 1024
#: Headroom over the weights for the KV cache, an assumption until real instances run (fit.py).
VRAM_HEADROOM: Final = 1.25
GIB: Final = 1 << 30

_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_BARE: Final = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)(?:@([^\s@]+))?$"
)
_SEGMENT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: Path prefixes on the hub that are not model repositories.
_NOT_MODELS: Final = frozenset({"datasets", "spaces", "collections", "papers", "blog", "docs"})
#: Names a person pastes for the hub itself. A link on one of these names the repository
#: only; the bytes come from HF_ENDPOINT, so `hf.co` (a redirect to the hub) is accepted here
#: without being on the egress allowlist.
LINK_HOSTS: Final = frozenset({"huggingface.co", "hf.co"})


class LinkError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def _refuse(link: str, likely_cause: str) -> LinkError:
    forms = ", ".join(ACCEPTED_FORMS)
    return LinkError(
        ThreePartMessage(
            f"The link {link!r} is not one the model fetcher accepts.",
            likely_cause,
            f"Paste one of: {forms}.",
        )
    )


@dataclass(frozen=True, slots=True)
class HubLink:
    owner: str
    repo: str
    revision: str = "main"

    @property
    def repo_id(self) -> str:
        return f"{self.owner}/{self.repo}"

    def source(self, path: str) -> Source:
        return Source(path=path, repo=self.repo_id, revision=self.revision)

    def canonical(self) -> str:
        suffix = "" if self.revision == "main" else f"/tree/{self.revision}"
        return f"https://huggingface.co/{self.repo_id}{suffix}"


def parse_link(text: str, *, hosts: Sequence[str]) -> HubLink:
    """One of the accepted forms → `HubLink`; anything else is a three-part `LinkError`."""
    link = text.strip()
    if not link:
        raise _refuse(link, "Nothing was pasted.")
    if re.search(r"\s", link):
        raise _refuse(
            link,
            "It has whitespace in it; an owner, repository or revision has no such characters.",
        )
    bare = _BARE.match(link)
    if bare and "." not in bare.group(1):
        owner, repo, revision = bare.group(1), bare.group(2), bare.group(3) or "main"
        return _checked(link, owner, repo, revision)
    if "://" not in link and "." not in link.split("/", 1)[0]:
        raise _refuse(link, "It is neither a hub address nor a bare owner/repo id.")
    candidate = link if "://" in link else f"https://{link}"
    parts = urllib.parse.urlsplit(candidate)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise _refuse(link, "It is neither a hub address nor a bare owner/repo id.")
    if parts.hostname.lower() not in LINK_HOSTS and not host_allowed(parts.hostname, hosts):
        raise LinkError(
            ThreePartMessage(
                f"{parts.hostname} is not an allowed model hub host.",
                f"The fetcher may reach only {', '.join(hosts)} (ADR-0018).",
                "Paste a link on one of those hosts, or add the mirror to SLAS_HUB_HOSTS and "
                "set HF_ENDPOINT to it.",
            )
        )
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise _refuse(link, "The address does not name an owner and a repository.")
    owner, repo, rest = segments[0], segments[1], segments[2:]
    if owner.lower() in _NOT_MODELS:
        raise _refuse(link, f"The address is under /{owner}/, which is not a model repository.")
    revision = "main"
    if rest:
        if len(rest) != 2 or rest[0] not in ("tree", "commit") or not rest[1]:
            raise _refuse(
                link,
                "After the repository only /tree/<revision> or /commit/<sha> is understood, "
                "not a file or a page inside it.",
            )
        revision = urllib.parse.unquote(rest[1])
    return _checked(link, owner, repo, revision)


def _checked(link: str, owner: str, repo: str, revision: str) -> HubLink:
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _SEGMENT.match(owner) or not _SEGMENT.match(repo):
        raise _refuse(link, "The owner or the repository name has characters the hub does not use.")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", revision):
        raise _refuse(link, "The revision has characters a branch, tag or commit does not use.")
    return HubLink(owner=owner, repo=repo, revision=revision)


# --- the derived registry entry ---------------------------------------------------------------


def slug_of(repo: str) -> str:
    """`Qwen3.8-27B-FP8` → `qwen3.8-27b-fp8`; the id pattern of `ModelEntry`."""
    lowered = repo.lower().replace("_", "-")
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned or not _ID.match(cleaned):
        cleaned = "model-" + re.sub(r"[^a-z0-9]", "", lowered)[:40]
    return cleaned


def valid_id(model_id: str) -> bool:
    return bool(_ID.match(model_id))


def unique_id(wanted: str, taken: Sequence[str]) -> str:
    """`wanted`, or `wanted-2`, `wanted-3`… when the registry already has it."""
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


def family_of(owner: str) -> str:
    return FAMILIES.get(owner.lower(), owner)


def quant_of(repo: str) -> str:
    """fp8 → `fp8`; awq → `awq4`; bf16, fp16 or nothing → `bf16`. A quantisation the registry
    does not admit (GPTQ, FP4, INT4…) is refused in three parts rather than mislabelled."""
    name = repo.lower()
    tokens = set(re.split(r"[^a-z0-9]+", name))
    if "fp8" in tokens or "fp8" in name:
        return "fp8"
    if "awq" in tokens or "awq" in name:  # an AWQ build says INT4 too: that is what awq4 is
        return "awq4"
    for word in UNADMITTED_QUANTS:  # quantisation names, not credentials
        if word in tokens or (word == "gptq" and "gptq" in name):
            raise LinkError(
                ThreePartMessage(
                    f"{repo} is a {word.upper()} build, which the registry does not admit.",
                    f"Models/models.yaml takes quant = {', '.join(ADMITTED_QUANTS)}; another "
                    "format needs an ADR (CLAUDE.md §7).",
                    "Pick the FP8, AWQ or BF16 build of this model instead.",
                )
            )
    return "bf16"


def context_of(model_dir: Path) -> int:
    """`max_position_embeddings` from config.json (a vision-language checkpoint keeps it under
    `text_config`); the default when the file or the field is absent."""
    config = model_dir / "config.json"
    if not config.is_file():
        return DEFAULT_CONTEXT
    try:
        data: Any = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_CONTEXT
    for holder in (data, data.get("text_config") if isinstance(data, dict) else None):
        if isinstance(holder, dict):
            value = holder.get("max_position_embeddings")
            if isinstance(value, int) and value >= MIN_CONTEXT:
                return value
    return DEFAULT_CONTEXT


def vram_estimate_gib(total_bytes: int) -> float:
    """The weights plus 25 % headroom for the KV cache, rounded up to whole GiB (never 0)."""
    return float(max(1, math.ceil(total_bytes / GIB * VRAM_HEADROOM)))


def registry_entry(
    link: HubLink, *, model_id: str, total_bytes: int, model_dir: Path
) -> dict[str, object]:
    """The `models:` item the fetcher appends; `roles` stays empty until a person assigns one."""
    return {
        "id": model_id,
        "display_name": link.repo,
        "family": family_of(link.owner),
        "path": model_id,
        "quant": quant_of(link.repo),
        "vram_gib": vram_estimate_gib(total_bytes),
        "context": context_of(model_dir),
        "roles": [],
    }
