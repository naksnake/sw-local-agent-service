# services/llm-gateway

The only component that talks to vLLM (CLAUDE.md §11). Hosts the Consensus Router (§5.3).
Since ADR-0015 it is a service: `slas-gateway serve` (the container command) serves the
routes of `docs/api-contract-round-2.md` §2 on `SLAS_BIND` with the shared `slas_http` stack.

| Module | Owns |
|---|---|
| `vllm.py` | request/response models, the `VllmClient` protocol, `FakeVllm` (scripted answers, schema violations, outages), and the two real clients: `UrllibVllmClient` (fixed URL map) and `HttpxVllmClient` (resolves the URL and served model per instance from the table at call time) |
| `instances.py` | the instance table the model manager fills with `PUT /v1/instances`: name → url, model id, healthy, last seen |
| `redaction.py` | rules from `config/redaction.yaml`; every prompt is redacted before a model sees it (INV-5) |
| `breaker.py` | per-instance circuit breaker: two invalid answers pause a voter |
| `routing.py` | role → instance routing; `switch()` is what a blue/green swap flips |
| `structured.py` | `guided_json` enforcement and the §11 fallback tiers 0 and 1; never returns a partially valid object. `generate_structured` validates into a Pydantic model, `generate_validated` into any schema |
| `schema_check.py` | the small JSON Schema checker behind `POST /v1/generate`, covering what Pydantic emits; no third-party validator |
| `consensus.py` | decision rules from `config/consensus.yaml`, the tally and its sentences, the token budget |
| `gateway.py` | `Gateway.complete / generate / generate_json / cross_check` tying the above together |
| `client.py` | `HttpGateway`, the same methods over HTTP, and the `GatewayLike` protocol the orchestrator depends on |
| `service/settings.py` | `Settings.from_env()` and the YAML loaders (`SLAS_CONSENSUS_FILE`, `SLAS_REDACTION_FILE`; missing file → shipped defaults with a warning event) |
| `service/routes.py` | the six routes, each error a three-part `ServiceError` |
| `service/app.py` | `create_app(...)` with every collaborator injectable; `route_table()` for the contract test |
| `cli.py` | `slas-gateway serve` |

## Routes

| Route | Answer |
|---|---|
| `POST /v1/complete` | `CompletionResponse` |
| `POST /v1/generate` | `{"object", "tier", "response"}`; 422 in three parts when no answer validates |
| `POST /v1/cross-check` | `ConsensusVerdict` |
| `GET /v1/routes` | `{"roles", "voters", "instances"}` |
| `PUT /v1/instances` | from the model manager: `{"instances": [{"name", "url", "model_id", "healthy"}], "routes": {"roles", "voters"}}`; replaces the table, answers like `GET /v1/routes` |
| `GET /v1/status` | `{"sentence", "roles": [{"role", "instance", "model_id", "healthy"}], "budget": {"used_pct", "limit_pct"}}` |
| `GET /health` | `{"checks": {"routes": "ok" \| "empty"}}`; an empty table is still healthy |

Until the model manager's first PUT, every role routes to `vllm-<role>`, there are no voters,
and a completion answers 503 "No instance serves the role coder yet." Nothing is read from
`Models/models.yaml` here.

## Environment

`SLAS_BIND` (`0.0.0.0:8000`) · `SLAS_CONSENSUS_FILE` (`/etc/slas/consensus.yaml`) ·
`SLAS_REDACTION_FILE` (`/etc/slas/redaction.yaml`) · `SLAS_VLLM_TIMEOUT_S` (`120`) ·
`SLAS_DAILY_TOKENS` (`20000000`, the daily allowance the cross-check budget is a share of) ·
`CONSENSUS_TOKEN_BUDGET_PCT` (overrides the rules file's `token_budget_pct` when set).

INV-11: a verdict from `cross_check()` is input to a human or a deterministic gate; nothing
here can act.
