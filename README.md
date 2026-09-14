# SW Local Agent Service

Self-hosted, air-gap-capable AI workers for server hardware engineering: a **Coding Agent**,
a **Validation Agent** and a **Factory Agent** on one shared kernel. Every agent can operate
the OS of the machine it works on, has critical outputs cross-checked by several local
models, tracks its work as a ticket from ingestion to root-cause analysis, writes its report
and SOP in English and Chinese, and can be extended with importable skills.

No cloud. No external APIs. Nothing leaves the perimeter.

## Start here

| If you are… | Read |
|---|---|
| An AI coding session | [`CLAUDE.md`](CLAUDE.md) — the single source of truth. Read it first, every session. |
| Planning the build | [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — 13 phases, done criteria, milestone demos |
| Driving the build with prompts | [`docs/PROMPTS.md`](docs/PROMPTS.md) — copy-paste prompts per phase |
| Reviewing the intended UI | [`docs/ui-demo/slas-ui-demo.html`](docs/ui-demo/slas-ui-demo.html) — open in a browser, no server needed |

## Quick start (target state, Phase 1)

```bash
tar xzf slas-bundle-<version>.tar.gz
cd slas-bundle-<version>
./install.sh            # preflight → config → load images → models → up → login URL
```

One command, one `.env`, one page to manage models. Details in `CLAUDE.md` §3.

## Status

**Phase 0 — Skeleton.** The repository holds the layout from `CLAUDE.md` §13, the Python
workspace (`uv`, `ruff`, `mypy --strict`, `pytest`), the WebUI shell (`pnpm`, Vite, React,
`vitest`, Playwright), CI with an egress-DROP job, `./install.sh` with the preflight step
(`slas doctor`), and ADR-0001/ADR-0002.

**Core through Phase 5, against fakes.** The Agent Kernel (`slas_kernel`: lifecycle,
write-ahead journal, crash recovery, NullAgent), the schemas, authz, the LLM gateway with
the Consensus Router, the model manager, the skills engine and screen driver, and now the
Phase 5 pieces: hybrid retrieval (`slas_rag`: dense + BM25 → RRF → rerank, cited answers),
the RCA pipeline (normalise → fingerprint → retrieve → draft → consensus → deterministic
owner routing from `config/owner-routing.yaml`), the dual-language SOP renderer
(`slas_sop`: `docs/glossary.yaml` pinned, identifiers protected by code), and the eval
checks (`slas_eval`: terminology consistency, back-translation spot check, local judges
only). Everything runs and is tested without a database, a model or a display; the
Qdrant/Postgres adapters, the gateway-backed embedder, reranker, drafter and translator,
and the Knowledge page wait on the dependency decisions listed in the phase reports.

**Phase 6, first session: sandboxes and the Coding Agent.** `services/sandbox-manager`
(hardened sandbox spec — no network, read-only rootfs, no capabilities, three mounts, no
credential; gVisor with a hardened runc fallback; TTL and quotas; the offline toolchain
resolver and `slas toolchain list|add`), one sandbox image per language, workspace-local
Git with `Slas-Agent`/`Slas-Ticket` trailers (`slas_git.workspace`), and the Coding Agent
on the kernel (`services/agent-core-orchestrator`): plan → breakdown → iterate with stall
detection → commit → ZIP → 3-voter cross-check → walkthrough SOP. The Coding page and the
three-step New coding task wizard run on an in-memory API fake until apps/api exists.

**Phase 6, second session: the Hybrid Git Control Engine.** `packages/slas-git` gains the
host allowlist (`config/git-hosts.yaml`), credentials sealed by reference (HKDF from
`SLAS_SECRET_KEY`; AES-GCM binds to `cryptography` once approved, a fake sealer for tests),
remotes, the validation gate (path scope, hooks, submodules, escaping symlinks, size, LFS,
secret scan, protected-branch policy, Consensus Router for agent diffs), merge-request
adapters for GitLab, Gitea and GitHub, bundles, audit rows and redaction.
`services/git-broker` runs every remote operation with the token on an inherited pipe fd
(`GIT_ASKPASS`) or an SSH key on tmpfs shredded after use, the hardening flags on every
`git`, and one audit row each. The Terminal session runs lines inside the sandbox with a
redacted transcript. Settings → Git remotes, Admin → Git hosts and the per-project Git
panel (Status · Commit · History · Push/Pull · Bundle · Terminal) run on API fakes. Tests
push through a fake Git host on loopback with a hostile pre-push hook and grep every sink
for the token afterwards. The Podman driver, the WebSocket/xterm.js terminal and the
service's HTTP surface wait on their dependency decisions.

**Phase 7: the Validation Agent against fakes.** `plans/` (plan schema and primitives
rendered from `slas_hal.primitives`), the plan compiler (`suite.md`/`suite.xlsx` → `plan.yaml`
with the §10.2 reject rules), `slas_hal` (models, Redfish parsers that turn malformed and
truncated answers into three-part errors, and recorded fakes with planted failures),
`slas_diff` (device counts, PCIe width AND speed, firmware, AER/EDAC/MCE/Xid, new SEL),
`services/validation-executor` (ARM/QUIESCE/ACT/SETTLE/VERIFY/GATE with fence markers, the
guardrails from `config/guardrails.yaml`, exclusive leases and per-cycle crash recovery),
`slas_triage` (fingerprint, dedup, owner routing, the durable bug index) and bug-ticket
spawning in the kernel (`[Issue] … | [Owner] EE`, one child ticket per fingerprint across
runs, carrying the diagnosis votes). The Validation page (LED cycle map, findings, console)
and the three-step New validation run wizard run on an API fake. The done-when is a test: a
25-cycle DC run with a PCIe degradation planted at cycle 14 yields one deduplicated bug
ticket with 3 votes and an EN/中文 SOP; a kill mid-cycle resumes at that cycle without a
second power action; an AC plan is blocked until approved. Real drivers wait for P8.

## Developing

```bash
uv sync                      # Python workspace, locked versions
uv run pytest                # unit tests with coverage
uv run ruff check . && uv run ruff format --check . && uv run mypy
pnpm install --frozen-lockfile
pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright install chromium && pnpm e2e
./install.sh                 # preflight only in Phase 0; prints a plain-language report
uv run slas doctor --json    # the same report as data
```

Everything above also runs with the network disabled once the dependencies are installed;
CI proves it in the `egress-drop` job.

## Licence

To be decided (see `CLAUDE.md` §15, open decisions).
