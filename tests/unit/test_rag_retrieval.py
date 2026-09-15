"""slas_rag: chunking, the two indexes, RRF, reranking and cited answers — all against fakes."""

from __future__ import annotations

import pytest

from slas_rag.documents import Document, chunk_document, chunk_text, tokens
from slas_rag.index import (
    FakeEmbedder,
    FakeReranker,
    MemoryTextIndex,
    MemoryVectorIndex,
    cosine,
)
from slas_rag.retrieval import (
    RRF_K,
    FakeAnswerer,
    HybridRetriever,
    KnowledgeBase,
    rrf,
)

DATASHEET = Document(
    id="h100-datasheet",
    title="H100 SXM datasheet",
    collection="datasheets",
    source="Knowledge/datasheets/h100.pdf",
    text=(
        "PCIe interface\n\nThe H100 SXM connects over PCIe Gen5 x16. The link trains to "
        "32 GT/s per lane; LnkSta reports width x16 and speed 32 GT/s when healthy.\n\n"
        "Thermal\n\nThe maximum operating temperature is 85 °C. Above that the GPU throttles "
        "and reports a thermal slowdown in nvidia-smi."
    ),
)
RUNBOOK = Document(
    id="pcie-runbook",
    title="PCIe link troubleshooting",
    collection="runbooks",
    text=(
        "When a PCIe link comes up narrower than the baseline (for example x8 instead of x16), "
        "reseat the card, then check the riser and the AER counters in the kernel log.\n\n"
        "A link that drops during a DC cycle usually points at the riser or the retimer "
        "firmware, not the GPU."
    ),
)
PAST_REPORT = Document(
    id="t-validation-0042",
    title="T-validation-0042 report",
    collection="past-reports",
    text=(
        "Finding: PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle 17. Owner EE. "
        "Root cause: retimer firmware 1.2.3 on riser slot 3; fixed by updating to 1.2.5."
    ),
)


def build() -> tuple[HybridRetriever, FakeReranker]:
    reranker = FakeReranker()
    retriever = HybridRetriever(
        embedder=FakeEmbedder(),
        vectors=MemoryVectorIndex(),
        text=MemoryTextIndex(),
        reranker=reranker,
        chunk_chars=200,  # small enough that the datasheet and the runbook split in two
    )
    for document in (DATASHEET, RUNBOOK, PAST_REPORT):
        retriever.ingest(document)
    return retriever, reranker


# --- documents ---------------------------------------------------------------------------


def test_chunking_follows_paragraphs_and_keeps_offsets() -> None:
    text = "Alpha one.\n\nBeta two.\n\n\nGamma three."
    chunks = chunk_text(text, max_chars=15, overlap=3)
    assert chunks == [(0, "Alpha one."), (12, "Beta two."), (24, "Gamma three.")]
    for start, piece in chunks:
        assert text[start : start + len(piece)] == piece
    packed = chunk_text(text, max_chars=80)
    assert packed == [(0, "Alpha one.\n\nBeta two.\n\nGamma three.")]


def test_long_paragraphs_are_cut_with_overlap() -> None:
    text = "abcdefghij" * 5  # 50 chars, no paragraph breaks
    chunks = chunk_text(text, max_chars=20, overlap=5)
    assert [start for start, _ in chunks] == [0, 15, 30]
    assert [len(piece) for _, piece in chunks] == [20, 20, 20]
    assert chunks[0][1][-5:] == chunks[1][1][:5], "the overlap repeats the tail"
    assert "".join(piece[5:] if i else piece for i, (_, piece) in enumerate(chunks)) == text
    with pytest.raises(ValueError, match="overlap"):
        chunk_text(text, max_chars=10, overlap=10)
    with pytest.raises(ValueError, match="max_chars"):
        chunk_text(text, max_chars=0)


def test_chunk_document_numbers_and_cites() -> None:
    chunks = chunk_document(DATASHEET, max_chars=120)
    assert [c.id for c in chunks] == [f"h100-datasheet#{n}" for n in range(1, len(chunks) + 1)]
    assert chunks[0].citation() == "H100 SXM datasheet §1"
    assert all(c.collection == "datasheets" and c.doc_id == "h100-datasheet" for c in chunks)
    assert tokens("LnkSta x16, 32 GT/s — 0000:8a:00.0") == [
        "lnksta",
        "x16",
        "32",
        "gt",
        "s",
        "0000:8a:00.0",
    ]


# --- indexes ------------------------------------------------------------------------------


def test_fake_embedder_is_deterministic_and_puts_similar_text_closer() -> None:
    embedder = FakeEmbedder()
    a, b = embedder.embed(["PCIe link width x16", "PCIe link width x8"])
    (c,) = embedder.embed(["thermal slowdown at 85 degrees"])
    assert embedder.embed(["PCIe link width x16"])[0] == a
    assert cosine(a, b) > cosine(a, c)
    assert abs(cosine(a, a) - 1.0) < 1e-9
    with pytest.raises(ValueError, match="differ in size"):
        cosine(a, a[:-1])
    with pytest.raises(ValueError, match="at least 8"):
        FakeEmbedder(dimensions=4)


def test_text_index_ranks_by_bm25_and_deletes_by_document() -> None:
    index = MemoryTextIndex()
    index.index(chunk_document(RUNBOOK) + chunk_document(PAST_REPORT))
    hits = index.search("retimer firmware", limit=5)
    assert hits and hits[0].chunk_id == "t-validation-0042#1", "both terms, short passage"
    assert index.search("nothing-here-at-all", limit=5) == []
    assert index.delete_document("pcie-runbook") == 1
    assert len(index) == 1 and index.delete_document("pcie-runbook") == 0
    assert MemoryTextIndex().search("anything", limit=3) == []


def test_vector_index_rejects_mismatched_input() -> None:
    index = MemoryVectorIndex()
    with pytest.raises(ValueError, match="chunks but"):
        index.upsert(chunk_document(RUNBOOK), [])


# --- fusion and search --------------------------------------------------------------------


def test_rrf_scores_items_by_their_ranks_in_every_list() -> None:
    fused = rrf([["a", "b", "c"], ["b", "a"]])
    assert fused["a"] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert fused["b"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert fused["c"] == pytest.approx(1 / (RRF_K + 3))
    assert fused["a"] == fused["b"] > fused["c"]
    with pytest.raises(ValueError, match="k must be positive"):
        rrf([["a"]], k=0)


def test_hybrid_search_fuses_dense_and_text_and_reranks() -> None:
    retriever, reranker = build()
    hits = retriever.search("PCIe link lost on GPU3 during DC cycle", limit=3)
    assert hits, "something must match"
    assert hits[0].chunk.doc_id == "t-validation-0042", "the past report is the closest match"
    assert all(h.dense_rank is not None or h.text_rank is not None for h in hits)
    assert all(h.rerank_score is not None for h in hits)
    assert reranker.calls and reranker.calls[0][0] == "PCIe link lost on GPU3 during DC cycle"
    assert hits[0].citation() == "T-validation-0042 report §1"
    assert retriever.sentence() == "The knowledge base holds 3 documents as 5 passages."


def test_reranker_can_change_the_fused_order() -> None:
    reranker = FakeReranker(boosts={"maximum operating temperature": 5.0})
    retriever = HybridRetriever(
        embedder=FakeEmbedder(),
        vectors=MemoryVectorIndex(),
        text=MemoryTextIndex(),
        reranker=reranker,
        chunk_chars=200,
    )
    for document in (DATASHEET, RUNBOOK, PAST_REPORT):
        retriever.ingest(document)
    without = HybridRetriever(
        embedder=FakeEmbedder(),
        vectors=MemoryVectorIndex(),
        text=MemoryTextIndex(),
        chunk_chars=200,
    )
    for document in (DATASHEET, RUNBOOK, PAST_REPORT):
        without.ingest(document)
    query = "PCIe link width and GPU temperature"
    plain = [h.chunk.id for h in without.search(query, limit=5)]
    boosted = [h.chunk.id for h in retriever.search(query, limit=5)]
    assert "h100-datasheet#2" in plain and plain[0] != "h100-datasheet#2"
    assert boosted[0] == "h100-datasheet#2", "the boosted thermal passage jumps to the top"
    assert without.search(query, limit=1)[0].rerank_score is None


def test_search_edge_cases() -> None:
    retriever, _ = build()
    assert retriever.search("   ") == []
    assert (
        HybridRetriever(
            embedder=FakeEmbedder(), vectors=MemoryVectorIndex(), text=MemoryTextIndex()
        ).search("anything")
        == []
    )
    with pytest.raises(ValueError, match="candidates at least limit"):
        retriever.search("PCIe", limit=5, candidates=2)

    class BadReranker:
        def rerank(self, query: str, passages: list[str]) -> list[float]:
            return [1.0]

    retriever.reranker = BadReranker()
    with pytest.raises(ValueError, match="reranker returned 1 scores"):
        retriever.search("PCIe link", limit=3)


def test_reingest_replaces_and_remove_forgets() -> None:
    retriever, _ = build()
    report = retriever.ingest(RUNBOOK.model_copy(update={"text": "Completely new runbook text."}))
    assert report.replaced and report.chunks == 1
    assert report.sentence() == "Re-indexed PCIe link troubleshooting (runbooks) as 1 passage."
    assert not any(
        h.chunk.doc_id == "pcie-runbook" and "reseat" in h.chunk.text
        for h in retriever.search("reseat the card", limit=5)
    )
    assert retriever.remove("pcie-runbook") == 1
    assert [d.id for d in retriever.documents] == ["h100-datasheet", "t-validation-0042"]
    assert retriever.remove("pcie-runbook") == 0


# --- the P5 done-when: drop a datasheet, ask a question, get a cited answer ------------------


def test_ask_returns_an_answer_with_citations_added_by_code() -> None:
    retriever, _ = build()
    answerer = FakeAnswerer()
    knowledge = KnowledgeBase(retriever, answerer)
    answer = knowledge.ask("What speed does the H100 PCIe link train to?", limit=2)
    assert answer.text.startswith("Based on the retrieved passages: ")
    assert answer.citations and answer.citations[0] == "H100 SXM datasheet §1"
    assert len(answer.citations) == len(set(answer.citations))
    assert answer.rendered().endswith(
        "\n".join(f"[{n}] {c}" for n, c in enumerate(answer.citations, 1))
    )
    assert answerer.calls[0][0] == "What speed does the H100 PCIe link train to?"
    assert "32 GT/s" in answerer.calls[0][1][0], "the model saw the passage that is cited"


def test_ask_without_matches_or_without_a_model_says_so() -> None:
    empty = KnowledgeBase(
        HybridRetriever(
            embedder=FakeEmbedder(), vectors=MemoryVectorIndex(), text=MemoryTextIndex()
        ),
        FakeAnswerer(),
    )
    answer = empty.ask("anything")
    assert answer.citations == [] and answer.rendered() == answer.text
    assert answer.text.startswith("Nothing in the knowledge base matches this question.")

    retriever, _ = build()
    no_model = KnowledgeBase(retriever)
    report = no_model.ingest(
        Document(id="extra", title="Extra", collection="runbooks", text="Extra text.")
    )
    assert report.sentence() == "Indexed Extra (runbooks) as 1 passage."
    answer = no_model.ask("retimer firmware 1.2.5")
    assert answer.text == "No answering model is configured; the closest passages are listed below."
    assert answer.citations[0] == "T-validation-0042 report §1"
