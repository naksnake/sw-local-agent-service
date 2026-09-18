# API contract — round 2 (the agents run: models, sandboxes, Coding, Validation, Factory, Git, Skills)

Round 1 (`docs/api-contract.md`) put sign-in, people, settings and the model registry view on
the wire. Round 2 puts the agents on it: after `./install.sh --build --fetch-models` a person
signs in, opens Coding, Validation or Factory and starts a task from the wizard. This file is
the contract every round-2 builder works against; ADR-0015 records the decision.

Conventions shared with round 1: every response is JSON; every non-2xx body is exactly
`{"what_happened", "likely_cause", "what_to_do", "trace_id"}` (the api adds `reason` on 401);
JSON keys on the wire are `snake_case`, the WebUI maps them to its `camelCase` views;
`traceparent` and `X-Slas-Trace-Id` travel on every hop and are echoed back. State-changing
requests from the browser carry `X-Requested-With: slas-webui`; between services they do not.

## 1. Who talks to whom

```
browser ──/api/v1/*──► api ──► agent-core-orchestrator ──► llm-gateway ──► vllm-* (inference net)
                        │             │      │      │
                        │             │      │      └──► validation-executor / factory-executor
                        │             │      └──► sandbox-manager ──► runtime socket (sandboxes)
                        │             └──► git-broker (remotes for the wizard)
                        ├──► git-broker            (Git panel, remotes, hosts)
                        ├──► sandbox-manager       (terminal lines)
                        ├──► factory-executor      (Admin → Stations)
                        └──► model-manager         (Models page status)
model-manager ──► runtime socket (vllm-*) · ──► llm-gateway PUT /v1/instances
```

Every service is a FastAPI app built with `slas_http.create_service_app()` and served by
uvicorn on `0.0.0.0:8000` (the port compose probes and Prometheus scrapes). Each has
`GET /health` (503 names the failing check) and `GET /metrics`. Only the api is reachable
from the edge.

### Service URLs (environment, set by compose)

| Variable | Default | Read by |
|---|---|---|
| `SLAS_GATEWAY_URL` | `http://llm-gateway:8000` | orchestrator, model-manager |
| `SLAS_MODEL_MANAGER_URL` | `http://model-manager:8000` | api |
| `SLAS_SANDBOX_MANAGER_URL` | `http://sandbox-manager:8000` | orchestrator, api |
| `SLAS_ORCHESTRATOR_URL` | `http://agent-core-orchestrator:8000` | api |
| `SLAS_GIT_BROKER_URL` | `http://git-broker:8000` | api, orchestrator |
| `SLAS_VALIDATION_EXECUTOR_URL` | `http://validation-executor:8000` | orchestrator |
| `SLAS_FACTORY_EXECUTOR_URL` | `http://factory-executor:8000` | orchestrator, api |
| `SLAS_MODEL_FETCHER_URL` | `http://model-fetcher:8000` | api (quickstart only, ADR-0018) |
| `SLAS_BIND` | `0.0.0.0:8000` | every service |

### Identity between services

The api resolves the session and forwards the person on every call as
`X-Slas-User` (email), `X-Slas-Display-Name` and `X-Slas-Capabilities` (comma-separated).
The receiving service reads them with `slas_http.identity.identity_of()` and runs
`require()` where the action executes (CLAUDE.md §11). No token, password or session id
travels. The headers cross only the internal `slas-backend` network. A service acting on its
own (a reconcile loop, an MES poller) uses `Identity.system()`.

### Console scripts and container commands

| Service | Console script (`[project.scripts]`) | Container CMD |
|---|---|---|
| llm-gateway | `slas-gateway` | `slas-gateway serve` |
| model-manager | `slas-model-manager` | `slas-model-manager serve` |
| model-fetcher | `slas-model-fetcher` | `slas-model-fetcher serve` |
| sandbox-manager | `slas-sandbox-manager` | `slas-sandbox-manager serve` |
| agent-core-orchestrator | `slas-orchestrator` | `slas-orchestrator serve` |
| git-broker | `slas-git-broker` | `slas-git-broker serve` |
| validation-executor | `slas-validation-executor` | `slas-validation-executor serve` |
| factory-executor | `slas-factory-executor` | `slas-factory-executor serve` |

Each `serve` reads its settings from the environment (pydantic-settings or plain `os.environ`),
builds its app with `create_app(...)` (every collaborator injectable for tests) and calls
`slas_http.serve.run(app, bind)`. Module layout per service: `slas_<pkg>/service/{settings.py,
app.py, routes*.py}` and `slas_<pkg>/cli.py` for the script.

## 2. llm-gateway

Wraps `slas_llm_gateway.gateway.Gateway`. Routes are role names; instances are `vllm-<role>`
and `vllm-voter-<model-id>` (`slas_model_manager.registry.Routes`). Instance URLs arrive from
the model manager; until they do, every completion answers 503 "no instance serves the role
coder yet" in three parts.

| Route | Body → Answer |
|---|---|
| `POST /v1/complete` | `{"role": str, "messages": [{"role": "system\|user\|assistant", "content": str}], "max_tokens": int, "temperature": float, "guided_json": object\|null}` → `CompletionResponse` as JSON (`slas_llm_gateway.vllm.CompletionResponse.model_dump()`). |
| `POST /v1/generate` | `{"role", "messages", "schema": object, "max_tokens"}` → `{"object": <validated JSON>, "tier": int, "response": CompletionResponse}`. Tiers 0–1 from `structured.py`; a failure after the retries is a 422 in three parts. |
| `POST /v1/cross-check` | `{"decision": str, "evidence": [Message]}` → `ConsensusVerdict.model_dump()` (`slas_schemas.vote`). |
| `GET /v1/routes` | `{"roles": {role: instance}, "voters": [instance], "instances": {instance: {"url": str, "model_id": str, "healthy": bool, "last_seen": iso}}}`. |
| `PUT /v1/instances` | From the model manager: `{"instances": [{"name": str, "url": "http://vllm-coder:8000", "model_id": str, "healthy": bool}], "routes": {"roles": {...}, "voters": [...]}}` → the same shape as `GET /v1/routes`. Replaces the table. |
| `GET /v1/status` | `{"sentence": str, "roles": [{"role", "instance", "model_id", "healthy"}], "budget": {"used_pct": float, "limit_pct": float}}` for the Models page. |

Health checks: `{"routes": "ok" | "empty"}` — an empty table is still healthy (the gateway is
up; the models are the model manager's business).

YAML: `SLAS_CONSENSUS_FILE` → `ConsensusRules`, `SLAS_REDACTION_FILE` → `Redactor`, loaded once
at start with `yaml.safe_load`; the shipped files under `config/` are the defaults.

Python client for the other services: `slas_llm_gateway.client.HttpGateway(base_url)` with
`complete()`, `generate()`, `cross_check()` — the same signatures as `Gateway`, so the
orchestrator's `GatewayCrossChecker` and the new gateway-backed `Coder` take either.

## 3. model-manager

Owns every `vllm-*` container. Reads `${SLAS_DATA_ROOT}/Models/models.yaml`, computes the
desired instances (`reconcile.desired_instances`), places them on GPUs, starts them through
the runtime socket (`slas_container.ContainerApi`), waits for `GET /health` on each, then
publishes the table to the gateway (`PUT /v1/instances`). The loop runs in a thread every
`SLAS_RECONCILE_INTERVAL_S` (default 30) and on `POST /v1/reconcile`. With
`SLAS_START_CODER_FIRST=1` (the default) the coder's instance is started alone and every other
start waits, reported `starting` with "waits its turn", until the coder answers its health
check or fails; a `starting` instance's sentence carries how long it has loaded and vLLM's
last log line.

| Route | Body → Answer |
|---|---|
| `GET /v1/status` | `{"sentence": str, "engine": str, "gpus": [{"id": int, "instances": [str]}], "instances": [{"name", "model_id", "display_name", "role\|voter", "gpu_ids", "state": "starting\|healthy\|unhealthy\|stopped\|failed", "sentence", "url"}]}`. |
| `POST /v1/reconcile` | `{}` → `{"actions": [{"kind", "name", "model_id", "reason"}], "sentence"}`. |
| `POST /v1/swap` | `{"role": str, "candidate": str}` → `SwapRecord.model_dump()`; 409 when a swap is in progress; capability `model:manage`. |
| `POST /v1/rollback` | `{"role"}` → `SwapRecord`; `model:manage`. |
| `GET /v1/fit` | `?model=<id>` → `{"fits": bool, "sentence": str}` (`fit.py`). |
| `PUT /v1/roles` | `{"roles": {role: model_id}, "voters": [model_id]}`, both optional: only the keys given change (a role given as `null` is unassigned) → `{"sentence": str, "roles": {...}, "voters": [...], "models": [{"id", "display_name", "family", "quant", "roles", "present"}]}`, the registry as written. Validates that every id is in the registry and its weights are present (`Models/<path>/SHA256SUMS`), rewrites `Models/models.yaml` atomically keeping every other field and the header, and reconciles at once. 400 in three parts for an unknown role or id or a model without the weights; 409 while a swap is in flight; `model:manage`. Roles the model was not declared for are added to its `roles` list (the person's assignment is the declaration, INV-9). |

Environment: `SLAS_RUNTIME_SOCKET` (in-container path `/run/podman/podman.sock`),
`SLAS_GPU_IDS` (`0,1,2,3`), `SLAS_HOST_MODELS_DIR` (the host path bind-mounted into every
vLLM container, `${SLAS_DATA_ROOT}/Models`), `SLAS_INFERENCE_NETWORK` (`slas_slas-inference`),
`SLAS_VLLM_IMAGE` (from the image lock, see §9), `SLAS_VLLM_SHM` (`16g`).

Container spec (`slas_container.CreateSpec`): name = instance name, network = the inference
network with the instance name as alias (so `http://vllm-coder:8000` resolves for the gateway
and Prometheus), `ipc_host=True`, shm 16 GiB, `mounts=[host models dir → /data/Models ro]`,
`gpu_ids` from the placement, env = `AIRGAP_ENV` + `CUDA_VISIBLE_DEVICES` (renumbered
`0..n-1`, since the request already selects the devices), `restart="unless-stopped"`, labels
`slas.kind=vllm`, `slas.instance=<name>`, `slas.model=<id>`. Embedding and rerank entries
start vLLM with `--runner pooling` (`--convert embed` for an embedding model; a reranker scores
as it is) and no generate flags; a generate instance carries `--enable-prefix-caching` and
`--structured-outputs-config {"backend": "xgrammar"}` (vLLM removed `--task` and
`--guided-decoding-backend`; an instance given either exits at start). Placement: greedy by
`vram_gib` against `SLAS_GPU_VRAM_GIB` per GPU (default 270, an HGX B300 GPU); an instance that does not fit
is reported `failed` with the fit sentence, never started. A crashed container's last 40 log
lines go into the `sentence`.

Health check: `{"runtime": "ok"|"down"}` (ping of the socket).

## 3b. model-fetcher (quickstart only, ADR-0018)

The one component with a route out: sole member of `slas-egress`, to the hub allowlist
(`SLAS_HUB_HOSTS`, default `huggingface.co,cdn-lfs.huggingface.co,*.hf.co`, plus the
`HF_ENDPOINT` host), through `HTTPS_PROXY` when set. Behind "Add a model" on the Models
page: it downloads the weights of a pasted link into `${SLAS_DATA_ROOT}/Models/<id>/` with
`slas_fetch` (resume, sha256 or git blob id per file), writes `SHA256SUMS` and
`manifest.json` as `scripts/fetch_models.py` does, then appends the entry to
`Models/models.yaml` (validated with the model manager's loader; never an existing id;
`roles` and `voters` untouched). The model manager picks the file up on its next tick. It
starts nothing and mounts no socket. Behind the compose profile `fetch`, which the installer
activates on quickstart and never on prod.

Accepted links: `https://huggingface.co/<owner>/<repo>`, with `/tree/<revision>` or
`/commit/<sha>`, `hf.co/<owner>/<repo>`, or a bare `<owner>/<repo>[@revision]`. Anything
else is a 400 that names those forms. The registry id is the repository name as a slug
(lowercase, `_` → `-`), made unique against the registry (`-2`, `-3`…) unless the body
gives one; `path` = id; `family` from the owner (deepseek-ai → DeepSeek, Qwen, BAAI,
MiniMaxAI → MiniMax, meta-llama → Meta, mistralai → Mistral…, else the owner); `quant` from
the name (`fp8`, `awq` → `awq4`, else `bf16`; a GPTQ, FP4 or GGUF build is refused because
the registry does not admit it); `context` from `config.json` `max_position_embeddings`
(else 32768); `vram_gib` = ⌈bytes / GiB × 1.25⌉, an estimate the done sentence says so.

| Route | Body → Answer |
|---|---|
| `POST /v1/fetches` | `{"link": str, "id": str\|null}` → 201 `FetchRecord`; the fetch runs in a background thread. 400 when the link is not accepted, its host is not on the allowlist, the id is not a registry id, or the build's quantisation is not admitted; 409 when that model id is registered already or being fetched; 503 when `models.yaml` is unusable. `model:manage`. |
| `GET /v1/fetches` | `[FetchRecord]`, newest first (the fetcher's memory; a restart forgets finished records, the files stay). |
| `GET /v1/fetches/{fetch_id}` | `FetchRecord`; 404 in three parts when unknown. |
| `DELETE /v1/fetches/{fetch_id}` | A running fetch: sets the cancel flag, checked between files → `{"sentence"}`; the record ends `cancelled` and every finished file stays, so the same link fetched again resumes. A finished record: removed → `{"sentence"}`. `model:manage`. |

`FetchRecord`: `{"id", "link", "model_id", "repo", "revision", "display_name", "state":
"planning|downloading|importing|done|failed|cancelled", "bytes_done", "bytes_total",
"files_done", "files_total", "sentence", "started_at", "finished_at": iso|null, "by",
"problem": {what_happened, likely_cause, what_to_do}|null, "entry": the registry entry as
written|null}`. Sentences: *"Downloading Qwen3.8-27B-FP8: 12.4 GiB of 29.0 GiB, 3 of 9
files."* / *"Qwen3.8-27B-FP8 is here (29.0 GiB, 9 files) and registered as qwen3.8-27b-fp8;
give it a role on this page to start it. Its GPU memory is estimated at 37 GiB from the file
sizes; correct it in the registry if you know better."*

Environment: `SLAS_MODELS_DIR` (`/data/Models`), `SLAS_HUB_HOSTS`, `HF_ENDPOINT`,
`HTTPS_PROXY`, `HF_TOKEN_FILE` (`/run/secrets/hf_token`, created empty by install.sh; a
token for gated repositories is sent as a header only, never logged, never in a URL,
never returned), `SLAS_BIND`.

Health check: `{"models_dir": "ok"|"unwritable", "hub": "not checked"}` — the hub is never
called from a health probe.

## 4. sandbox-manager

Wraps `slas_sandbox_manager.manager.SandboxManager` with a runtime backed by
`slas_container.ContainerApi` (`slas_sandbox_manager.runtime.ContainerApiRuntime`). The
manager's `Session` is the wire shape.

| Route | Body → Answer |
|---|---|
| `POST /v1/sessions` | `{"user", "slug", "display_name", "languages": [{"language", "version"}], "ticket_id", "ttl_s"?}` → `Session.model_dump()` plus `"sentence"`. Prepares `Projects/<slug>` (git init with the person's identity) and opens the sandbox on the resolved image. 409 when the person already holds `max_sessions_per_user`. |
| `GET /v1/sessions/{id}` | `Session` + `"alive": bool`. 404 in three parts when unknown. |
| `POST /v1/sessions/{id}/exec` | `{"argv": [str], "cwd": str?, "timeout_s": int?}` → `ExecResult.model_dump()`. argv only; a string body is a 400. |
| `DELETE /v1/sessions/{id}` | → 204. |
| `POST /v1/sessions/{id}/terminal` | `{"line": str}` → `TerminalLine.model_dump()` (`terminal.py`; `git push` is answered with `PUSH_EXPLANATION`). Capability `git:terminal`. |
| `GET /v1/toolchains` | `{"manifest": {language: [versions]}, "source": str}`. |
| `POST /v1/toolchains/resolve` | `{"choices": [{"language", "version"}]}` → `[{"language", "label", "requested", "version", "honoured", "sentence", "image"}]` (`Resolution.to_record()` **gains `label`**). |
| `POST /v1/languages/detect` | `{"plan": str}` → `{"languages": [str]}` (`toolchains.detect_languages`). |
| `POST /v1/reap` | `{}` → `{"closed": [ids]}`. Also runs every 60 s in a thread. |

Environment: `SLAS_RUNTIME_SOCKET`, `SLAS_DATA_ROOT` (`/data` with `Coding/` under it),
`SLAS_HOST_DATA_ROOT` (host path, for bind mounts), `DEFAULT_RUNTIME` (`runsc`),
`SANDBOX_TIER`, `SLAS_SANDBOX_REGISTRY` (`local`), `SLAS_TOOLCHAIN_MANIFEST`
(`/data/Toolchains/manifest.json`). At start the manager pings the socket, checks whether
`runsc` is a known runtime (`GET /info` → `Runtimes`) and falls back to hardened `runc` with a
warning sentence in the log and in `GET /health` (`{"runtime": "ok", "isolation": "gvisor"|"runc"}`).

**Sandbox images on a connected host.** `images/sandbox-<language>/Dockerfile` must build with
`./install.sh --build` without the offline toolchain bundle: each language image installs its
toolchain from a pinned upstream image or a pinned distribution package (the newest version the
manifest lists), plus `git`, and copies `images/sandbox-common/slas-check.sh`. `python -m
slas_sandbox_manager.images list` prints `name<TAB>tag<TAB>dockerfile` for every image;
`python -m slas_sandbox_manager.images manifest --out <path>` writes the toolchain manifest
those images satisfy. `install.sh --build` calls both (§9).

## 5. agent-core-orchestrator

Hosts the kernel and the three agents. Every `Kernel.run()` happens in a worker thread; the
routes return at once with the ticket and the pages poll the list routes (live progress,
CLAUDE.md §9). Tickets are read from `FileTicketStore(data_root)`; the run views join the
ticket with the executor's state.

### Coding (`/v1/coding`)

| Route | Body → Answer |
|---|---|
| `POST /v1/coding/languages/detect` | `{"plan"}` → `{"languages": [str]}` (delegates to the sandbox manager). |
| `POST /v1/coding/propose` | `{"plan", "filename"}` → `Breakdown` as JSON: `{"title", "tasks": [{"n", "title"}], "languages": [{"language", "version"}], "isolation", "skills", "cross_check", "export_target", "remote_ref", "max_iterations"}`. |
| `POST /v1/coding/toolchains/resolve` | `{"choices"}` → the sandbox manager's answer, passed through. |
| `GET /v1/coding/remotes` | `{"remotes": [str]}` — the names of the person's remotes from the git broker (`GET /v1/remotes`). |
| `GET /v1/coding/skills` | `{"skills": [{"id", "name"}]}` — skills enabled for `coding` (`SkillStateStore`). |
| `GET /v1/coding/readiness` | `{"ready": bool, "sentence": str}` — whether the gateway reports a healthy instance for the `coder` role (`GET /v1/status` on the gateway). The wizard shows the sentence above Start task. |
| `POST /v1/coding/tasks` | `{"breakdown": Breakdown, "plan": str, "filename": str}` → `CodingTask` (below); starts the kernel run. 503 in three parts while the coder role has no healthy instance or the gateway does not answer: a task started then would fail at its first edit minutes later. |
| `GET /v1/coding/tasks` | `[CodingTask]`, newest first, the person's own unless `admin:people`. |
| `GET /v1/coding/tasks/{ticket_id}` | `CodingTask`. |
| `DELETE /v1/coding/tasks/{ticket_id}` | Remove a finished task (Done, Failed or Needs review): the ticket and its journal, its SOP, its artifacts and any sandbox session still open for it → `{"sentence"}`. 409 while the task is running; 404 for a task the person may not see. The project directory under `Coding/<user>/Projects/<slug>` stays: it is the person's repository. |

`CodingTask`: `{"ticket_id", "title", "state", "sentence", "steps": [{"n", "title", "status":
"pending|running|done|failed|skipped"}], "feed": [str]}`. `steps` come from the plan and the
ticket's step verdicts; `feed` is derived from the journal (intent → "Doing X…", observation →
its sentence), first line the toolchain choice.

The coder: `slas_orchestrator.coding.coder.GatewayCoder(gateway)` implements `Coder` with
`gateway.generate(role="coder", schema=EditSet.model_json_schema(), …)`; the prompt carries the
task, the file snapshot and the last check output, never a credential (INV-5).

### Validation (`/v1/validation`)

| Route | Body → Answer |
|---|---|
| `POST /v1/validation/suites/parse` | `{"filename", "text"?: str, "content_base64"?: str}` → `SuiteView`: `{"source", "title", "items": [{"n", "title", "action", "cycles", "destructive", "sentence"}], "sentence", "problem": str\|null}`. `.xlsx` arrives base64 and is written to a temp file for `parse_suite_xlsx`. |
| `GET /v1/validation/targets` | `[{"ref", "model", "free", "holder", "armed", "sentence"}]` — the executor's `GET /v1/targets`, passed through. |
| `POST /v1/validation/preview` | `{"suite": SuiteView, "target"}` → `{"sentence", "steps": [{"id", "title", "destructive"}], "destructive_steps": [str], "guardrails": [str], "cross_check": ConsensusVerdict\|null}`. Compiles the plan and runs the plan-approval cross-check without starting anything. |
| `POST /v1/validation/runs` | `{"suite", "target"}` → `RunView`. Ingests, chooses the target, starts the kernel in a thread. A plan with destructive steps stops at `Planned` with `pending_approvals`. |
| `POST /v1/validation/runs/{id}/approve` | `{}` → `RunView`; approves every pending step as the acting person (`Kernel.approve` per step, then `resume` in a thread). Capability `approve:destructive`. |
| `GET /v1/validation/runs` | `[RunView]`, newest first. |
| `GET /v1/validation/runs/{id}` | `RunView`. |

`RunView`: `{"ticket_id", "title", "target", "state", "sentence", "pending_approvals": [str],
"cells": [{"n", "kind", "status": "waiting|running|ok|finding|failed|skipped", "sentence"}],
"console_tail": [str], "findings": [str], "votes": [str]}` — `RunState` from the executor
(`GET /v1/runs/{id}`) plus the ticket.

The executor is remote: `slas_orchestrator.remote.HttpExecutor(client)` implements
`slas_kernel.executor.Executor` by `POST /v1/execute` with `LONG_TIMEOUT_S`.

### Factory (`/v1/factory`)

| Route | Body → Answer |
|---|---|
| `GET /v1/factory/mes-tickets` | `[MesTicket.model_dump()]` from the executor's `GET /v1/mes/pending`. |
| `POST /v1/factory/labels/parse` | `{"text"}` → `{"trigger": MesTicket}` or `{"problem": str}` (`FactoryAgent` label parser; the sentence is the fake's). |
| `GET /v1/factory/stations` | `[{"name", "description", "free", "holder", "enrolled", "sentence"}]` from the executor. |
| `GET /v1/factory/templates` | `[{"id", "name", "steps": [str], "skills": [str], "sentence"}]`. |
| `POST /v1/factory/jobs` | `{"trigger": MesTicket, "template_id", "rules": {"voters": 3, "on_fail": "hold", "export_sop": true, "backup_station": bool}}` → `JobView`. |
| `POST /v1/factory/jobs/{id}/decide` | `{"verdict": "PASS"\|"FAIL", "note"}` → `JobView`; capability `factory:verdict`. |
| `POST /v1/factory/jobs/{id}/control` | `{"verb": "pause"\|"resume"\|"abort"}` → `{"sentence", "watch_url": str\|null, "watch_problem": str\|null}`; capability `factory:control`. |
| `GET /v1/factory/jobs`, `GET /v1/factory/jobs/{id}` | `[JobView]` / `JobView`. |

`JobView`: `{"ticket_id", "title", "station", "unit_sn", "state", "sentence", "cells": [{"n",
"title", "status", "sentence", "screenshot": str\|null}], "verdict": str\|null, "votes": [str],
"held": bool}`.

### Skills (`/v1/skills`)

| Route | Answer |
|---|---|
| `GET /v1/skills` | `[{"id", "name", "version", "risk", "agents", "requires", "enabled": {agent: bool}, "sentence"}]` from `Skills/library`. |
| `POST /v1/skills/import` | `{"yaml": str}` → `ImportReport` as JSON (`slas_skills.importer`). |
| `POST /v1/skills/{id}/enable` · `/disable` | `{"agent"}` → the skill row. |
| `GET /v1/skills/{id}/export` | `{"yaml": str, "content_hash": str}` (state never travels). |

### Tickets (`/v1/tickets`)

`GET /v1/tickets` → `[{"id", "agent", "title", "state", "sentence", "created_at"}]` for the
Home lists; `GET /v1/tickets/{id}` → `Ticket.model_dump()`.

Health check: `{"gateway": "ok"|"down", "sandbox_manager": ..., "validation_executor": ...,
"factory_executor": ..., "git_broker": ...}` — probed with a 2 s timeout; only `gateway`
and `sandbox_manager` failing make the orchestrator unhealthy (the others are optional
zones on a host without a lab or a factory line).

## 6. validation-executor and factory-executor

Libraries today; each becomes a long-running service that performs steps for the kernel.

### validation-executor

| Route | Body → Answer |
|---|---|
| `POST /v1/execute` | `{"step": Step, "context": ExecutionContext}` → `Observation.model_dump()`. The one entry point for the kernel; runs `ValidationExecutor.execute` (blocking; the caller uses the long timeout). `UnknownPrimitiveError` → 400, a guardrail refusal → 409, all in three parts. |
| `GET /v1/runs` | `[{"ticket_id", "state": RunState}]` from `Validation/Runs/*/cycles.json`. |
| `GET /v1/runs/{id}` | `RunState.model_dump()` + `"console_tail": [str]` (last 40 lines of `console.log`). |
| `GET /v1/targets` | `TargetRegistry.list()` joined with `LeaseTable` → `[{"ref", "model", "free", "holder", "armed", "sentence"}]`. |
| `POST /v1/targets/{alias}/arm` · `/disarm` | `{"note"}` → the target row; capability `approve:destructive`. |

At start: the syslog receiver on `SYSLOG_LISTEN`, `RealHal` from `CREDENTIAL_SOURCE`,
guardrails from `GUARDRAIL_POLICY`, quirks from `BMC_QUIRKS`. Without any target in the
registry the service is healthy and idle. Health: `{"hal": "ok", "syslog": "ok"|"down"}`.

### factory-executor

| Route | Body → Answer |
|---|---|
| `POST /v1/execute` | as above, `FactoryExecutor.execute`. |
| `GET /v1/jobs`, `GET /v1/jobs/{id}` | `JobState` rows. |
| `POST /v1/jobs/{id}/control` | `{"verb", "by"}` → `BatchResult` + `{"watch_url", "watch_problem"}`. |
| `POST /v1/jobs/{id}/decide` | `{"verdict", "by", "note"}` → `JobState`. |
| `GET /v1/stations` | `StationRegistry.list()` joined with the station lease table (the wizard's rows). |
| `GET /v1/station-records` | `[StationRecord.model_dump() + "sentence"]` (Admin → Stations). |
| `POST /v1/station-records` | `{"name", "description"}` → record; `PUT /v1/station-records/{name}/tuning` `{"screen", "retention", "vnc_enabled"}`; `POST /v1/station-records/{name}/code` → `IssuedCode`; `POST /v1/station-records/{name}/revoke`; `DELETE /v1/station-records/{name}`. Capability `factory:stations_manage`. |
| `GET /v1/templates` | `[TestLoopTemplate + "sentence"]` from `/data/Factory/Templates` (the shipped file is copied there when the directory is empty). |
| `GET /v1/mes/pending` | `FileDropMesAdapter.pending()` under `/data/Factory/mes/`. |

At start: `EnrolmentServer` on `ENROLMENT_LISTEN`, the MES poller thread, the CA under
`Factory/ca`. Health: `{"enrolment": "ok"|"down", "mes": "ok"}`.

## 7. git-broker

`GitBroker` behind HTTP; the acting person comes from the identity headers and is the
`owner` of every remote. Slugs are project slugs under `Coding/<user>/Projects`.

| Route | Body → Answer | Capability |
|---|---|---|
| `GET /v1/remotes` | `[RemoteView]` = `Remote` without secrets + `"fingerprint"`, `"last_used"`, `"sentence"` | signed in |
| `POST /v1/remotes` | `{"name", "uri", "auth_type": "pat"\|"ssh_key", "secret"}` → `RemoteView`; the secret is never echoed | `git:remote_manage` |
| `POST /v1/remotes/{id}/rotate` | `{"secret"}` → `RemoteView` | `git:remote_manage` |
| `DELETE /v1/remotes/{id}` | → `{"sentence"}` | `git:remote_manage` |
| `POST /v1/remotes/{id}/test` | `{}` → `{"ok", "sentence"}` (`ls-remote`) | `git:clone` or `git:pull` |
| `GET /v1/hosts` | `[GitHost + "sentence"]` | signed in |
| `POST /v1/hosts` | `{"name", "hostname", "kind", "ssh_host_key"}` → host; writes `config/git-hosts.yaml` through `render_git_hosts_yaml` (the file is mounted rw at `/etc/slas/git-hosts.yaml` for this) | `git:hosts_manage` |
| `GET /v1/projects/{slug}/status` | `{"branch", "entries": [{"path", "state"}]}` | signed in |
| `POST /v1/projects/{slug}/commit` | `{"subject"}` → `CommitView` `{"sha", "subject", "author", "when", "by_agent", "ticket_id"}` | signed in |
| `GET /v1/projects/{slug}/history` | `[CommitView]` | signed in |
| `POST /v1/projects/{slug}/push` | `{"remote_id", "branch"}` → `PushResult` + `"gate": [{"name", "ok", "sentence"}]`, `"review_url"` | `git:push_branch` (`git:push_protected` for a protected branch) |
| `POST /v1/projects/{slug}/pull` | `{"remote_id"}` → `{"sentence"}` | `git:pull` |
| `POST /v1/projects/{slug}/bundle/export` | `{}` → `BundleInfo` + `"path"` | `git:bundle` |
| `POST /v1/projects/{slug}/bundle/import` | `{"file_name"}` (under `Bundles/`) → `{"sentences": [str]}` | `git:bundle` |

The credential store is `EncryptedFileStore` at `${SLAS_DATA_ROOT}/.git-broker/credentials.json`
sealed with `SLAS_SECRET_KEY` from `/run/secrets/secret_key` (the `postgres+aesgcm` store of the
compose profile is the same interface; switching is a settings change, not a contract change).
The audit log is `${SLAS_DATA_ROOT}/.git-broker/audit.jsonl`.

## 8. api — the browser-facing routes

The api proxies with `slas_http.ServiceClient`, adding the identity headers and requiring
the capability listed above **before** forwarding (the downstream checks again). Bodies and
answers pass through unchanged; a downstream three-part error is returned with its status.

| Browser route | Forwards to |
|---|---|
| `/api/v1/coding/*` | orchestrator `/v1/coding/*` |
| `/api/v1/validation/*` | orchestrator `/v1/validation/*` |
| `/api/v1/factory/*` | orchestrator `/v1/factory/*` |
| `/api/v1/skills/*` | orchestrator `/v1/skills/*` |
| `/api/v1/tickets/*` | orchestrator `/v1/tickets/*` |
| `/api/v1/git/remotes*`, `/api/v1/git/hosts*`, `/api/v1/git/projects/*` | git-broker `/v1/remotes*`, `/v1/hosts*`, `/v1/projects/*` |
| `/api/v1/git/projects/{slug}/terminal` | sandbox-manager `/v1/sessions/{session}/terminal` (the api looks the person's session up by slug via `GET /v1/sessions?user=&slug=`) |
| `/api/v1/stations/*` | factory-executor `/v1/station-records/*` |
| `/api/v1/models/status` | model-manager `/v1/status`; `POST /api/v1/models/swap`, `/rollback`; `PUT /api/v1/models/roles` → `/v1/roles` (`model:manage`) |
| `/api/v1/models/fetches*` | model-fetcher `/v1/fetches*` (§3b): `GET` list and one record for anyone signed in; `POST` and `DELETE` behind `model:manage`. On prod the fetcher is not running, so these answer the 503 that names it. |

The round-1 stubs `GET /api/v1/coding/tasks`, `/validation/runs`, `/factory/jobs` now return
the real lists. Home lists read them.

## 9. Deployment

- Image lock: `vllm` third-party entry `vllm/vllm-openai:v0.29.0-x86_64-cu129`, upstream
  `docker.io/vllm/vllm-openai:v0.29.0-x86_64-cu129`, digest
  `sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a`, profiles
  quickstart and prod. Not a compose service: the model manager starts it. `install.sh --build`
  pulls it with the other third-party images and compose passes its reference as
  `SLAS_VLLM_IMAGE`.
- Compose: the URL variables of §1 on the services that read them; `SLAS_HOST_MODELS_DIR`,
  `SLAS_INFERENCE_NETWORK`, `SLAS_VLLM_IMAGE`, `SLAS_GPU_VRAM_GIB` on model-manager;
  `SLAS_HOST_DATA_ROOT`, `SLAS_TOOLCHAIN_MANIFEST`, `SLAS_SANDBOX_REGISTRY` on sandbox-manager;
  `Tickets/`, `Skills/`, `Validation/`, `Factory/`, `SOP/` volumes where the routes above read
  them; the git broker's data directory; `config/git-hosts.yaml` mounted `rw`.
- Dockerfiles: CMD per §1; the sandbox images build on the connected path (§4).
- model-fetcher (ADR-0018): first-party image for the quickstart profile only; compose
  service behind the `fetch` profile on `slas-backend` and the new non-internal `slas-egress`
  (its sole member), `${SLAS_DATA_ROOT}/Models` mounted rw, the `hf_token` secret (created
  empty), `SLAS_HUB_HOSTS`, `HF_ENDPOINT` and `HTTPS_PROXY` from `.env`; `install.sh` adds
  `fetch` to `COMPOSE_PROFILES` on quickstart, never on prod, and removes a leftover
  container on prod; Prometheus scrapes it on quickstart only.
- `install.sh --build`: builds the sandbox images (`python -m slas_sandbox_manager.images list`),
  writes the toolchain manifest, pulls the vLLM image, and the preflight says in a sentence
  which runtime serves `SLAS_RUNTIME_SOCKET`, whether `runsc` is registered and whether the
  NVIDIA runtime answers (`docker info` → Runtimes / `nvidia-ctk --version`).
- `slas doctor` gains the same three checks.

## 10. What stays out of round 2

Screen worker sessions for the Coding Agent's virtual desktop; Kata; Vault; OIDC; the
knowledge base ingest; a WebUI Skills page beyond the wizard's list (adding a page needs an
ADR). Each is a later round on the same surfaces.
