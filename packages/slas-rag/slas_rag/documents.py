"""Documents and chunks: what the knowledge base ingests (CLAUDE.md §4.4 Knowledge/, §8.3).

A document is one file from `Knowledge/` (datasheet, test spec, runbook, past report) or one
ticket export. Ingestion splits it into paragraph-aligned chunks; every chunk keeps its
document, title and position so a retrieved passage can always be cited as
"<title> §<n>" and traced back to the file.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, Literal

from pydantic import Field

from slas_schemas.common import SlasModel

Collection = Literal["datasheets", "test-specs", "runbooks", "past-reports", "tickets", "sops"]
COLLECTIONS: Final[tuple[str, ...]] = (
    "datasheets",
    "test-specs",
    "runbooks",
    "past-reports",
    "tickets",
    "sops",
)

#: Chunk size in characters; embeddings degrade on very long passages and citations get vague.
DEFAULT_CHUNK_CHARS: Final = 800
DEFAULT_OVERLAP_CHARS: Final = 100

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_WORD = re.compile(r"[a-z0-9]+(?:[_.:-][a-z0-9]+)*", re.IGNORECASE)


class Document(SlasModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    title: str = Field(min_length=1)
    collection: Collection
    text: str = Field(min_length=1)
    #: Where it came from: a path under Knowledge/, a ticket id, an upload name.
    source: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class Chunk(SlasModel):
    id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    n: int = Field(ge=1)
    title: str = Field(min_length=1)
    collection: Collection
    text: str = Field(min_length=1)
    start: int = Field(ge=0)

    def citation(self) -> str:
        """How a passage is cited in an answer or an RCA: "<title> §<n>"."""
        return f"{self.title} §{self.n}"


def tokens(text: str) -> list[str]:
    """Lower-cased word tokens; shared by the fake embedder, the text index and the reranker."""
    return [match.group(0).lower() for match in _WORD.finditer(text)]


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int | None = None,
) -> list[tuple[int, str]]:
    """Split on paragraph breaks, packing paragraphs up to `max_chars`; long paragraphs are cut.

    Returns (start offset, text) pairs. Cuts inside a long paragraph overlap by `overlap`
    characters (default: an eighth of `max_chars`, at most 100) so a sentence split across
    two chunks is still retrievable from either.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap is None:
        overlap = min(DEFAULT_OVERLAP_CHARS, max_chars // 8)
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be non-negative and smaller than max_chars")
    pieces: list[tuple[int, str]] = []
    offset = 0
    for paragraph in _PARAGRAPH_BREAK.split(text):
        stripped = paragraph.strip()
        if not stripped:
            offset = text.find(paragraph, offset) + len(paragraph)
            continue
        start = text.index(stripped, offset)
        offset = start + len(stripped)
        if len(stripped) <= max_chars:
            pieces.append((start, stripped))
            continue
        cursor = 0
        while cursor < len(stripped):
            end = min(cursor + max_chars, len(stripped))
            pieces.append((start + cursor, stripped[cursor:end]))
            if end == len(stripped):
                break
            cursor = end - overlap

    chunks: list[tuple[int, str]] = []
    current_start = 0
    current: list[str] = []
    current_len = 0
    for start, piece in pieces:
        extra = len(piece) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            chunks.append((current_start, "\n\n".join(current)))
            current, current_len = [], 0
        if not current:
            current_start = start
        current.append(piece)
        current_len += extra
    if current:
        chunks.append((current_start, "\n\n".join(current)))
    return chunks


def chunk_document(
    document: Document,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int | None = None,
) -> list[Chunk]:
    return [
        Chunk(
            id=f"{document.id}#{n}",
            doc_id=document.id,
            n=n,
            title=document.title,
            collection=document.collection,
            text=text,
            start=start,
        )
        for n, (start, text) in enumerate(
            chunk_text(document.text, max_chars=max_chars, overlap=overlap), start=1
        )
    ]


class IngestReport(SlasModel):
    doc_id: str
    title: str
    collection: Collection
    chunks: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replaced: bool = False

    def sentence(self) -> str:
        verb = "Re-indexed" if self.replaced else "Indexed"
        noun = "passage" if self.chunks == 1 else "passages"
        return f"{verb} {self.title} ({self.collection}) as {self.chunks} {noun}."
