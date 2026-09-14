"""Index boundaries and their in-memory fakes.

Production adapters — Qdrant for `VectorIndex`, Postgres FTS for `TextIndex`, the gateway's
`embed` and `rerank` roles for `Embedder` and `Reranker` — implement these four protocols.
Nothing in this package imports a database driver or an HTTP client; retrieval logic is
tested against the fakes below, which behave like the real thing on small corpora.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Protocol

from pydantic import Field

from slas_rag.documents import Chunk, tokens
from slas_schemas.common import SlasModel


class Scored(SlasModel):
    chunk_id: str = Field(min_length=1)
    score: float


class Embedder(Protocol):
    """Text → dense vector. Production: the gateway's `embed` role (vLLM, no egress)."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorIndex(Protocol):
    """Production: Qdrant, one collection per knowledge collection."""

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...
    def search(self, vector: list[float], *, limit: int) -> list[Scored]: ...
    def delete_document(self, doc_id: str) -> int: ...


class TextIndex(Protocol):
    """Production: Postgres full-text search (`tsvector` + `ts_rank_cd`)."""

    def index(self, chunks: list[Chunk]) -> None: ...
    def search(self, query: str, *, limit: int) -> list[Scored]: ...
    def delete_document(self, doc_id: str) -> int: ...


class Reranker(Protocol):
    """Query + passages → one relevance score per passage. Production: the gateway's `rerank`
    role; the scores come back structured, never as free text."""

    def rerank(self, query: str, passages: list[str]) -> list[float]: ...


# --- fakes ------------------------------------------------------------------------------


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vectors differ in size: {len(a)} and {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=True))


class FakeEmbedder:
    """A hashed bag-of-words embedding: deterministic, dependency-free, and similar texts
    land close together. Stands in for the `embed` role in tests."""

    def __init__(self, dimensions: int = 64) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % self.dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        return index, sign

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token, count in Counter(tokens(text)).items():
                index, sign = self._bucket(token)
                vector[index] += sign * (1.0 + math.log(count))
            vectors.append(_unit(vector))
        return vectors


class MemoryVectorIndex:
    """Cosine search over stored unit vectors. Stands in for Qdrant."""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}
        self._doc_of: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._vectors)

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._vectors[chunk.id] = _unit(vector)
            self._doc_of[chunk.id] = chunk.doc_id

    def search(self, vector: list[float], *, limit: int) -> list[Scored]:
        query = _unit(vector)
        scored = [
            Scored(chunk_id=chunk_id, score=similarity)
            for chunk_id, stored in self._vectors.items()
            if (similarity := cosine(query, stored)) > 0
        ]
        scored.sort(key=lambda s: (-s.score, s.chunk_id))
        return scored[:limit]

    def delete_document(self, doc_id: str) -> int:
        gone = [chunk_id for chunk_id, owner in self._doc_of.items() if owner == doc_id]
        for chunk_id in gone:
            del self._vectors[chunk_id]
            del self._doc_of[chunk_id]
        return len(gone)


class MemoryTextIndex:
    """BM25 over word tokens. Stands in for Postgres FTS."""

    def __init__(self, *, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._terms: dict[str, Counter[str]] = {}
        self._length: dict[str, int] = {}
        self._doc_of: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._terms)

    def index(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            words = tokens(chunk.text) + tokens(chunk.title)
            self._terms[chunk.id] = Counter(words)
            self._length[chunk.id] = len(words)
            self._doc_of[chunk.id] = chunk.doc_id

    def search(self, query: str, *, limit: int) -> list[Scored]:
        if not self._terms:
            return []
        query_terms = set(tokens(query))
        total = len(self._terms)
        average = sum(self._length.values()) / total
        document_frequency = {
            term: sum(1 for counts in self._terms.values() if term in counts)
            for term in query_terms
        }
        scored: list[Scored] = []
        for chunk_id, counts in self._terms.items():
            score = 0.0
            for term in query_terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                df = document_frequency[term]
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                length_norm = 1 - self.b + self.b * self._length[chunk_id] / average
                score += idf * tf * (self.k1 + 1) / (tf + self.k1 * length_norm)
            if score > 0:
                scored.append(Scored(chunk_id=chunk_id, score=score))
        scored.sort(key=lambda s: (-s.score, s.chunk_id))
        return scored[:limit]

    def delete_document(self, doc_id: str) -> int:
        gone = [chunk_id for chunk_id, owner in self._doc_of.items() if owner == doc_id]
        for chunk_id in gone:
            del self._terms[chunk_id]
            del self._length[chunk_id]
            del self._doc_of[chunk_id]
        return len(gone)


class FakeReranker:
    """Scores a passage by the share of query tokens it contains, with an optional boost per
    substring so a test can pin the order. Stands in for the `rerank` role."""

    def __init__(self, boosts: dict[str, float] | None = None) -> None:
        self.boosts = dict(boosts or {})
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        query_terms = set(tokens(query)) or {""}
        scores: list[float] = []
        for passage in passages:
            present = set(tokens(passage))
            overlap = len(query_terms & present) / len(query_terms)
            boost = sum(value for needle, value in self.boosts.items() if needle in passage)
            scores.append(overlap + boost)
        return scores
