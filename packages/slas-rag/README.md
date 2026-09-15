# slas-rag

Hybrid retrieval over the internal corpus: dense + FTS → RRF → rerank (CLAUDE.md §8.3 Option A).

| Module | What it does |
|---|---|
| `documents.py` | `Document`, `Chunk`, paragraph-aligned chunking, `IngestReport`. A chunk cites itself as `<title> §<n>`. |
| `index.py` | The four boundaries — `Embedder`, `VectorIndex` (Qdrant), `TextIndex` (Postgres FTS), `Reranker` — and their in-memory fakes: hashed bag-of-words embedder, cosine index, BM25 index, token-overlap reranker. |
| `retrieval.py` | `rrf()`, `HybridRetriever` (ingest / remove / search), `KnowledgeBase.ask()` which hands passages to an `Answerer` and appends the citations by code. |

The package imports no database driver and no HTTP client. The Qdrant and Postgres adapters,
and the gateway-backed embedder and reranker, are separate modules that implement these
protocols (P5 follow-up, awaiting the dependency decision).
