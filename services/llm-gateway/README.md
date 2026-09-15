# services/llm-gateway

The only component that talks to vLLM (CLAUDE.md §11). Hosts the Consensus Router (§5.3).

| Module | Owns |
|---|---|
| `vllm.py` | request/response models, the `VllmClient` protocol and `FakeVllm` (scripted answers, schema violations, outages) |
| `redaction.py` | rules from `config/redaction.yaml`; every prompt is redacted before a model sees it (INV-5) |
| `breaker.py` | per-instance circuit breaker: two invalid answers pause a voter |
| `routing.py` | role → instance routing; `switch()` is what a blue/green swap flips |
| `structured.py` | `guided_json` enforcement and the §11 fallback tiers 0–1; never returns a partially valid object |
| `consensus.py` | decision rules from `config/consensus.yaml`, the tally and its sentences, the token budget |
| `gateway.py` | `Gateway.complete / generate / cross_check` tying the above together |

The HTTP surface arrives with the api dependencies (ADR-0005). INV-11: a verdict from
`cross_check()` is input to a human or a deterministic gate; nothing here can act.
