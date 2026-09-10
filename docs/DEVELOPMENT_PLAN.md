# Development Plan — SW Local Agent Service

Companion to `CLAUDE.md` v3.0. Phases are ordered so that the **shared kernel exists before
any agent**, and every agent is built against **fakes before hardware**. Each phase ends
with something you can click or run. Do not start a phase until the previous one's "Done
when" is green in CI.

Estimated effort is in Claude Code sessions of roughly 1–2 hours with review.

## Dependency map

```
P0 skeleton ──► P1 quickstart core ──► P2 kernel + tickets ──► P3 models + gateway + consensus
                                                     │                      │
                                                     ▼                      ▼
                                              P4 skills + screen ◄─── P5 knowledge + RCA + SOP
                                                     │
                     ┌───────────────────────────────┼─────────────────────────────┐
                     ▼                               ▼                             ▼
              P6 Coding Agent                P7 Validation (fakes)          P9 Factory (fakes)
                                                     │                             │
                                                     ▼                             ▼
                                             P8 Validation hardware       P10 Factory station
                                                     └──────────────┬──────────────┘
                                                                    ▼
                                                     P11 observability ──► P12 prod profile
```

## Phases

### P0 — Skeleton (2 sessions)
**Scope.** Repo layout from CLAUDE.md §13; `uv`, `pnpm`, `ruff`, `mypy --strict`,
`pytest`, `vitest`, Playwright config; CI with an egress-DROP job; `slas doctor` and
`install.sh` preflight only; `.env.example`; ADR-0001 "Agent Kernel" and ADR-0002 "Screen
worker instead of host X11".
**Done when.** `./install.sh` runs preflight and prints a plain-language report; CI is
green on an empty repo; `docs/adr/` has two accepted ADRs.

### P1 — Quickstart core (3 sessions)
**Scope.** Compose with postgres, redis, minio, api, webui shell, edge (self-signed TLS);
built-in auth (argon2) + RBAC/capabilities in `slas-authz`; Admin → People and Settings
(writes `.env`); `slas user add`; deploy test (fresh VM → login page under egress-DROP).
**Done when.** INV-10 test green; you can log in, add a user, change a setting without a
restart.

### P2 — Agent Kernel + Ticket Service (4 sessions)
**Scope.** `slas-kernel` lifecycle (ingest → ticket → plan → act → collect → rca → sop →
close) with pluggable `Agent` protocol; `slas-schemas` for Job, Plan, Step, Ticket, Vote,
SopModel, Finding; Ticket Service in `apps/api` with state machine and exports; write-ahead
journal; a **NullAgent** that exercises the whole lifecycle with fake steps; Tickets page
(list, detail, state, attachments).
**Done when.** Running the NullAgent creates a ticket, journals 5 fake steps, collects
stdout/stderr, produces a placeholder RCA and a placeholder two-language SOP, and closes.
Killing the process mid-run and restarting resumes from the journal.

### P3 — Models, gateway, Consensus Router (4 sessions)
**Scope.** `Models/models.yaml`; model-manager reconciler (start/stop vLLM containers,
blue/green swap, rollback); llm-gateway with role routing, redaction, `guided_json`
enforcement, circuit breaker; **Consensus Router** (fan-out to N voters, vote schema,
per-decision rules from `config/consensus.yaml`, token budget); Models page (cards, roles,
fit sentence, swap progress, voter chips); `slas model scan|fit|swap|test`.
**Done when.** Swap `coder` from the UI with progress and roll back; a cross-check request
with 3 voters returns a verdict object; a voter that fails schema twice trips the breaker;
eval harness runs against local judges only (CI asserts no `api.openai.com`).

### P4 — Skills + screen worker (4 sessions)
**Scope.** `skills/schema/skill.schema.json` from CLAUDE.md §6; `slas-skills` import →
validate → compile → run; primitive whitelist with risk classes; capability check against
`slas-authz`; screen-worker container (Xvfb, x11vnc/noVNC, `slas-screen` driver with
PyAutoGUI + xdotool, screenshot before/after, rate limit, window deny-list); **screen fake**
that replays scripted windows for tests; Skills page (list, enable per agent, import,
export, "Try it").
**Done when.** The two example skills in §6.3 validate, compile and run against fakes; a
skill with a `redfish: power_off` step shows the approval requirement at import and at run;
a GUI skill runs on a real Xvfb display in CI and produces before/after screenshots; a
skill exported from one install imports into another unchanged.

### P5 — Knowledge, RCA, dual-language SOP (3 sessions)
**Scope.** Qdrant + Postgres FTS ingestion; hybrid retrieval with RRF and rerank;
Knowledge page; kernel RCA pipeline (normalise → fingerprint → retrieve → draft → consensus
→ owner routing); `slas-sop` renderer (SopModel → EN + zh-Hant Markdown/PDF, glossary
pinned, identifiers copied by code); `docs/glossary.yaml`; eval: terminology consistency
and back-translation spot check.
**Done when.** Drop a datasheet, ask a question, get a cited answer; feed a recorded
failure log and get an RCA with votes and an owner; the NullAgent's SOP now renders real
EN and 中文 files side by side with identical commands and numbers.

### P6 — Coding Agent + Hybrid Git Control Engine (6 sessions)
**Scope.** sandbox-manager (rootless Podman + gVisor, hardened runc fallback, TTL, quotas,
no network, `git` installed, per-user identity); sandbox images per language and a **toolchain
resolver** (user picks languages, version optional → newest bundled version, pinned version
honoured if present, else a sentence and fallback; `slas toolchain list|add`); Coding agent on
the kernel: plan ingestion
→ breakdown → iterate loop (lint, type, build, test) → stall detection; consensus on the
final diff; code walkthrough SOP; ZIP export. **Git (CLAUDE.md §5.7):** `services/git-broker`
(encrypted credential store by reference, `GIT_ASKPASS`/tmpfs SSH key handling, hostile-repo
hardening flags, host allowlist from `config/git-hosts.yaml`, validation gate, branch push +
PR/MR, bundle export/import, audit rows); Settings → Git remotes and Admin → Git hosts;
per-project Git panel (Status, Commit, History, Push/Pull, Bundle) and Terminal tab running
inside the sandbox; Coding page and **New coding task wizard** with the export target
listing the user's remotes.
**Done when.** Upload a plan (languages detected) → leave versions empty → the ticket shows
which toolchain the agent chose; pin `Rust 1.99` → a sentence says it isn't available and 1.80
is used → approve the breakdown → agent commits passing code on a
branch in `Projects/<slug>/.git` → 3-voter cross-check shown → EN/中文 walkthrough exported
→ ZIP downloads → "Push to gitlab-firmware" opens a PR on local GitLab. In the terminal,
`git log` shows the agent's commits with `Slas-Agent` trailers and `git push` fails with the
"push happens from the Git panel" message. A PAT pasted in the UI appears in no log,
workspace file, argv or model context (CI grep on every sink). A repo with a malicious
`pre-push` hook pushes without the hook executing. A bundle exported from one install
imports into another. A plan that needs a missing file stops after 3 iterations with a
three-part message.

### P7 — Validation Agent against fakes (5 sessions)
**Scope.** `plans/schema/plan.schema.json` and primitives; plan compiler (`.md`/`.xlsx` →
plan.yaml, reject rules); `slas-hal` with `fakes/` (recorded Redfish, scripted SOL, SEL,
inventory) including ugly fixtures; `slas-diff` (baseline vs snapshot, PCIe width AND
speed, firmware, counters) with Hypothesis tests; validation-executor state machine
(ARM/QUIESCE/ACT/SETTLE/VERIFY/GATE) with fence markers and crash recovery; guardrail
policy engine; `slas-triage` fingerprint/dedup/owner routing; bug-ticket spawning;
Validation page (LED cycle map, console, findings), **New validation run wizard**.
**Done when.** A 25-cycle DC run completes against fakes with a planted failure at cycle
14: LED map shows it, one deduplicated bug ticket is drafted with 3 votes, EN/中文
verification SOP attached; `kill -9` mid-cycle and restart resumes at the right cycle;
approval dialog blocks an AC-cycle plan until approved.

### P8 — Validation hardware (3 sessions, needs one target server)
**Scope.** Real Redfish/IPMI/SSH drivers, SOL capture, syslog receiver, PDU driver (per
open decision 3), BMC quirk shims; macvlan overlay; one real target.
**Done when.** One real DC cycle with SOL capture and baseline diff; approval flow used for
a real AC cycle; fence markers correlate to real console lines; no credential appears in
any log or model context (grep-based CI check on redacted bundles).

### P9 — Factory Agent against fakes (4 sessions)
**Scope.** Factory templates (`templates/factory/*.yaml`); MES adapter interface with a
file-drop implementation; Factory agent on the kernel using skills for GUI steps; station
runner service (mTLS, signed step batches, screenshots back) with a **runner fake**;
factory-executor with station leases; consensus PASS rule; station state backup;
production line SOP; Factory page (test-step map, screenshot strip), **New factory job
wizard**.
**Done when.** A fake MES ticket triggers a 9-step loop on a fake station; PASS requires
3/3 votes; a planted failure holds the station and drafts a line-lead ticket; EN/中文 line
SOP and a station backup land in the ticket.

### P10 — Factory station (2 sessions, needs one station)
**Scope.** Install the station runner on a real station; run the login + BurnIn skill;
tune window matching and timing; screenshot retention policy.
**Done when.** A real unit goes through the loop end to end with the operator watching on
the VNC view and able to take over.

### P11 — Observability (2 sessions)
**Scope.** Prometheus rules, six Grafana dashboards as code (inference, GPU, agents,
sandboxes/screens, validation runs, factory), alerting to a local channel, `slas status`
complete, trace propagation verified.
**Done when.** Every dashboard is populated from a real run; a tripped circuit breaker and
a consensus disagreement each raise an alert.

### P12 — Prod profile (3 sessions)
**Scope.** Vault injection, Keycloak OIDC, Harbor + cosign, Kata/Firecracker tier,
pgBackRest PITR, object-lock, restore drill runbook, macvlan for lab and factory.
**Done when.** `./install.sh --profile prod` on a clean host; restore drill documented with
a measured RTO.

## Milestone demos

| Milestone | After | What you show |
|---|---|---|
| M1 "It installs" | P1 | one command → login page, air-gapped |
| M2 "Models are friendly" | P3 | swap a model from the UI, roll back, watch 3 voters agree |
| M3 "Skills travel" | P4 | export a skill, import on a second install, run it on a virtual display |
| M4 "First agent" | P6 | plan → code → local commits → cross-check → EN/中文 walkthrough → push through the broker → PR |
| M5 "Hardware, safely" | P8 | one real DC cycle with SOL, baseline diff, approval flow |
| M6 "Factory line" | P10 | real unit through the loop, consensus verdict, line SOP |
| M7 "Production" | P12 | prod install, restore drill, dashboards |

## Risks and how the plan de-risks them

| Risk | Mitigation in the plan |
|---|---|
| Open-weights models fail tool calls | constrained decoding from P3; fallback tiers; consensus only on judgement outputs |
| BMC fleet is heterogeneous | HAL fakes from recorded fixtures (P7) before real drivers (P8); quirk shims isolated |
| GUI automation is brittle | window/text/image matching, screenshots before/after, operator takeover via VNC, station runner keeps timing local |
| Three voters don't fit in VRAM | Models page fit sentence; consensus degrades to single-model + flag, never blocks |
| Dual-language drift | one structured source; identifiers copied by code; glossary pinned; eval checks |
| Skill files become an attack surface | schema + whitelist + capabilities + risk classes; no shell primitive; import-time review of destructive steps |
| Pasted Git credentials leak, or a hostile repo runs code in the platform | credentials exist only in `git-broker` memory per operation, stored encrypted by reference; sandboxes have no route and no creds; hooks and repo config neutralised on every broker invocation; CI greps every log sink for token/key patterns |
| "Air-gapped" with a proxy search | Option A default; Option B requires an ADR and cannot use the label |

## Working rules for every phase
- One deliverable per session (see `docs/PROMPTS.md`). Small, reviewable, tested.
- Fakes first. Real hardware and displays only in P8 and P10.
- Every phase updates `CLAUDE.md` if it changed an interface, and adds an ADR if it moved a boundary.
- Run the phase-end audit prompt before declaring a phase done.
