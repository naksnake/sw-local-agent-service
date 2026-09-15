# slas-eval

Eval harness against local judges only (CLAUDE.md §8.1, INV-2).

| Module | What it does |
|---|---|
| `judges.py` | The single construction site for judges: `LocalJudgeEndpoint` refuses cloud AI hosts and anything outside the perimeter; `Judge` protocol; `FakeJudge`. |
| `terminology.py` | `check_terminology()` / `check_sop_terminology()`: every glossary term the English uses is rendered as pinned in the Chinese, nothing on the avoid list appears, and the identifier lists are identical (gate: 100%). |
| `back_translation.py` | `spot_check()`: a deterministic sample of translated fields goes zh → en through a local translator and a local judge scores the round trip. |

Ragas/TruLens integration and the nightly `tests/eval` run wait on the dependency decision;
these checks need no third-party package.
