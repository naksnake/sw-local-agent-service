"""Hybrid retrieval: dense + full-text → Reciprocal Rank Fusion → rerank (CLAUDE.md §8.3 A).

    query ──► embed ──► VectorIndex.search ──┐
          └─────────► TextIndex.search  ──┴─► rrf() ──► Reranker ──► Hit[] with citations

Everything here is deterministic code. The only model calls are the embedder and the
reranker, both behind protocols and both routed through the LLM gateway in production so
that redaction, the breaker and role routing apply (§4.2). A cited answer is produced by
`KnowledgeBase.ask`, which hands the retrieved passages to an `Answerer` and appends the
citations itself — the model never invents a source.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Protocol

from pydantic import Field

from slas_rag.documents import (
    DEFAULT_CHUNK_CHARS,
    Chunk,
    Document,
    IngestReport,
    chunk_document,
)
from slas_rag.index import Embedder, Reranker, Scored, TextIndex, VectorIndex
from slas_schemas.common import SlasModel

#: The RRF constant from Cormack et al.; 60 is the value everyone uses and it is not tuned.
RRF_K: Final = 60


def rrf(rankings: Iterable[Iterable[str]], *, k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = Σ 1 / (k + rank_i(d)) over every ranking it appears in."""
    if k < 1:
        raise ValueError("k must be positive")
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return fused


class Hit(SlasModel):
    chunk: Chunk
    #: Fused score after RRF (and after reranking when a reranker ran).
    score: float
    dense_rank: int | None = None
    text_rank: int | None = None
    rerank_score: float | None = None

    def citation(self) -> str:
        return self.chunk.citation()


class Answerer(Protocol):
    """Question + passages → prose. Production: the gateway's `planner` role."""

    def answer(self, question: str, passages: list[str]) -> str: ...


class CitedAnswer(SlasModel):
    question: str
    text: str
    citations: list[str] = Field(default_factory=list)
    hits: list[Hit] = Field(default_factory=list)

    def rendered(self) -> str:
        """The answer followed by a numbered source list; the numbers are added by code."""
        if not self.citations:
            return self.text
        sources = "\n".join(f"[{n}] {c}" for n, c in enumerate(self.citations, start=1))
        return f"{self.text}\n\nSources:\n{sources}"


class HybridRetriever:
    def __init__(
        self,
        *,
        embedder: Embedder,
        vectors: VectorIndex,
        text: TextIndex,
        reranker: Reranker | None = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
    ) -> None:
        self.embedder = embedder
        self.vectors = vectors
        self.text = text
        self.reranker = reranker
        self.chunk_chars = chunk_chars
        self._chunks: dict[str, Chunk] = {}
        self._documents: dict[str, Document] = {}

    # --- ingest -----------------------------------------------------------------------

    def ingest(self, document: Document) -> IngestReport:
        """Chunk, embed and index one document; re-ingesting replaces its passages."""
        replaced = document.id in self._documents
        if replaced:
            self.remove(document.id)
        chunks = chunk_document(document, max_chars=self.chunk_chars)
        vectors = self.embedder.embed([chunk.text for chunk in chunks])
        self.vectors.upsert(chunks, vectors)
        self.text.index(chunks)
        for chunk in chunks:
            self._chunks[chunk.id] = chunk
        self._documents[document.id] = document
        return IngestReport(
            doc_id=document.id,
            title=document.title,
            collection=document.collection,
            chunks=len(chunks),
            sha256=document.sha256,
            replaced=replaced,
        )

    def remove(self, doc_id: str) -> int:
        removed = self.vectors.delete_document(doc_id)
        self.text.delete_document(doc_id)
        for chunk_id in [c for c, chunk in self._chunks.items() if chunk.doc_id == doc_id]:
            del self._chunks[chunk_id]
        self._documents.pop(doc_id, None)
        return removed

    @property
    def documents(self) -> list[Document]:
        return list(self._documents.values())

    def sentence(self) -> str:
        docs = len(self._documents)
        passages = len(self._chunks)
        return (
            f"The knowledge base holds {docs} {'document' if docs == 1 else 'documents'} "
            f"as {passages} {'passage' if passages == 1 else 'passages'}."
        )

    # --- search -----------------------------------------------------------------------

    def search(self, query: str, *, limit: int = 5, candidates: int = 20) -> list[Hit]:
        """Dense and text candidates → RRF → rerank the fused top `candidates` → top `limit`."""
        if not query.strip() or not self._chunks:
            return []
        if limit < 1 or candidates < limit:
            raise ValueError("limit must be positive and candidates at least limit")
        dense = self.vectors.search(self.embedder.embed([query])[0], limit=candidates)
        lexical = self.text.search(query, limit=candidates)
        dense_rank = _ranks(dense)
        text_rank = _ranks(lexical)
        fused = rrf([list(dense_rank), list(text_rank)])
        ordered = sorted(fused, key=lambda chunk_id: (-fused[chunk_id], chunk_id))[:candidates]
        hits = [
            Hit(
                chunk=self._chunks[chunk_id],
                score=fused[chunk_id],
                dense_rank=dense_rank.get(chunk_id),
                text_rank=text_rank.get(chunk_id),
            )
            for chunk_id in ordered
            if chunk_id in self._chunks
        ]
        if self.reranker is not None and hits:
            scores = self.reranker.rerank(query, [hit.chunk.text for hit in hits])
            if len(scores) != len(hits):
                raise ValueError(f"reranker returned {len(scores)} scores for {len(hits)} passages")
            hits = [
                hit.model_copy(update={"rerank_score": score, "score": score})
                for hit, score in zip(hits, scores, strict=True)
            ]
            hits.sort(key=lambda hit: (-hit.score, hit.chunk.id))
        return hits[:limit]


def _ranks(scored: list[Scored]) -> dict[str, int]:
    return {item.chunk_id: rank for rank, item in enumerate(scored, start=1)}


class KnowledgeBase:
    """Ingest documents, ask questions, get answers that cite what they were drawn from."""

    def __init__(self, retriever: HybridRetriever, answerer: Answerer | None = None) -> None:
        self.retriever = retriever
        self.answerer = answerer

    def ingest(self, document: Document) -> IngestReport:
        return self.retriever.ingest(document)

    def ask(self, question: str, *, limit: int = 5) -> CitedAnswer:
        hits = self.retriever.search(question, limit=limit)
        citations = _unique([hit.citation() for hit in hits])
        if not hits:
            return CitedAnswer(
                question=question,
                text=(
                    "Nothing in the knowledge base matches this question. Add the relevant "
                    "datasheet, spec or report on the Knowledge page and ask again."
                ),
            )
        if self.answerer is None:
            text = "No answering model is configured; the closest passages are listed below."
        else:
            text = self.answerer.answer(question, [hit.chunk.text for hit in hits]).strip()
        return CitedAnswer(question=question, text=text, citations=citations, hits=hits)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class FakeAnswerer:
    """Answers with the first passage, so a test can check that the answer came from the
    retrieved evidence and that the citations were attached by code."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def answer(self, question: str, passages: list[str]) -> str:
        self.calls.append((question, list(passages)))
        first = passages[0].splitlines()[0] if passages else ""
        return f"Based on the retrieved passages: {first}"
