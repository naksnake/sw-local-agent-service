# Prompt Pack — SW Local Agent Service

Copy-paste prompts for Claude Code sessions. Every prompt starts with the same preamble so
the session reads the SSOT before touching code.

## How to run a session well
- **One deliverable per session.** "Implement the skill compiler" is a phase; "implement
  `slas_skills.compile()` for the `focus_window`, `click`, `type`, `key` primitives with
  tests against the screen fake" is a session.
- **Ask for the plan first** on anything touching more than three files. Approve it, then
  say "go".
- **Always end with the audit prompt** for the phase before moving on.
- **Paste failures verbatim.** Test output, tracebacks, screenshots. Don't paraphrase.
- If Claude says a request conflicts with §2 of CLAUDE.md, that is the intended behaviour.
  Decide, write an ADR if you're changing an invariant, and continue.

---

## 0. Preamble (prepend to every prompt)

```
Read CLAUDE.md fully and docs/DEVELOPMENT_PLAN.md. We are in Phase <N>.
Rules for this session: follow the invariants in §2; do not add a dependency without
asking; every new module gets tests against fakes; UI copy follows §9; anything that
touches hardware goes through slas_hal and anything that touches a screen goes through
slas_screen. If the task below conflicts with CLAUDE.md, stop and tell me before writing
code. Output: the files, their tests, and a 3-line summary of what I should verify by hand.
```

## 1. Phase kick-off (use once per phase)

```
<preamble>
Task: propose the session plan for Phase <N> from docs/DEVELOPMENT_PLAN.md.
Break the phase into sessions of one deliverable each, in dependency order. For each
session give: the deliverable, files created or changed, the tests that prove it, and the
"done when" check it contributes to. Do not write code yet.
```

## 2. Phase-by-phase session prompts

### P0 — Skeleton
```
<preamble>
Task: create the repository skeleton from CLAUDE.md §13 with uv, pnpm, ruff, mypy --strict,
pytest, vitest and Playwright configured; a CI workflow with lint, typecheck, unit tests, and
an egress-DROP job that runs the test suite with all outbound network blocked; install.sh
with the preflight step only (slas doctor), printing a plain-language report as in §3.
Also write docs/adr/0001-agent-kernel.md and docs/adr/0002-screen-worker-not-host-x11.md
as accepted ADRs using the template in §15.
```

### P1 — Quickstart core
```
<preamble>
Task: implement compose/docker-compose.yml with postgres, redis, minio, api, webui, edge
(Caddy, self-signed TLS); apps/api with built-in auth (argon2), sessions, and slas_authz
with roles and capabilities from config/rbac-roles.yaml; Admin → People and Admin →
Settings pages; `slas user add`; and tests/deploy that installs on a fresh VM under
egress-DROP and asserts the login page loads (INV-10). Changing a setting must not restart
anything (INV-9).
```

### P2 — Agent Kernel + tickets
```
<preamble>
Task: implement packages/slas-kernel with the lifecycle in §5.1 (ingest → ticket → plan →
act → collect → rca → sop → close), the Agent protocol, write-ahead journal, and
packages/slas-schemas for Job, Plan, Step, Ticket, Vote, SopModel, Finding. Add a NullAgent
with five fake steps. Implement the Ticket Service in apps/api with the state machine from
§5.4 and a Tickets page. Prove crash recovery: a test kills the run after step 3 and a
restart resumes at step 4 from the journal. RCA and SOP may be placeholders that still
produce both languages (INV-13).
```

### P3 — Models, gateway, Consensus Router
```
<preamble>
Task: implement services/model-manager (reads Models/models.yaml, reconciles vLLM
containers, blue/green swap with smoke test and 24 h rollback), services/llm-gateway (role
routing, redaction from config/redaction.yaml, guided_json enforcement, circuit breaker),
and the Consensus Router in the gateway per §5.3 with rules from config/consensus.yaml and
the Vote schema. Build the Models page (cards, roles, fit sentence, swap progress, voter
chips) and `slas model scan|fit|swap|test`. Tests use a fake vLLM that returns scripted
completions and can be told to violate the schema.
```

### P4 — Skills + screen worker
```
<preamble>
Task: generate skills/schema/skill.schema.json from CLAUDE.md §6 and implement
packages/slas-skills: import (schema, primitive whitelist, capability check, risk
classification), compile (input binding, secret handles, bounded loop unrolling → StepPlan),
run (journal every step), export (secrets stripped, content hash). Implement
services/screen-worker (Xvfb, x11vnc, noVNC) and packages/slas-screen (PyAutoGUI + xdotool
behind our driver; screenshot before/after; rate limit; window deny-list) plus a screen
fake for tests. Build the Skills page. The two example skills in §6.3 must validate,
compile and run against fakes; the redfish power_off step must show the approval
requirement at import and at run.
```

### P5 — Knowledge, RCA, dual-language SOP
```
<preamble>
Task: implement packages/slas-rag (ingest to Qdrant + Postgres FTS, hybrid retrieval with
RRF and rerank through the gateway), the Knowledge page, the kernel RCA pipeline from §5.4
(normalise → fingerprint → retrieve → draft → consensus → owner routing), and
packages/slas-sop (SopModel → sop.en.md and sop.zh-Hant.md with docs/glossary.yaml pinned;
identifiers, commands, numbers and versions copied by code, never by the model). Add eval
checks for terminology consistency and a back-translation spot check using local judges.
```

### P6a — Coding Agent
```
<preamble>
Task: implement services/sandbox-manager (rootless Podman, gVisor runtime with hardened
runc fallback, read-only rootfs, no network, TTL, quotas, git installed with a per-user
identity from GIT_CONFIG_GLOBAL), one sandbox image per language, and the toolchain resolver
from CLAUDE.md §10.1: the user picks languages only (Python, C, C++, Rust, Shell, Go,
TypeScript, YAML/JSON config), version optional; resolve the newest bundled version, honour a
pinned version if present, otherwise explain in one sentence and fall back; record the choice
on the ticket. Then the Coding Agent on the kernel: plan ingestion →
breakdown for user approval → iterate loop (lint, type, build, test) with stall detection
after 3 iterations → local commits on a branch in Projects/<slug>/.git with Slas-Agent and
Slas-Ticket trailers → consensus on the final diff → code walkthrough SOP → ZIP export.
Build the Coding page and the three-step New coding task wizard from §9. The sandbox must
never hold Git credentials or have a route to any remote (INV-14).
```

### P6b — Hybrid Git Control Engine
```
<preamble>
Task: implement services/git-broker and packages/slas-git per CLAUDE.md §5.7: the Remote
model with credentials stored encrypted by reference (AES-GCM keyed from SLAS_SECRET_KEY in
quickstart, Vault KV behind the same interface for prod); per-operation decryption in memory
with GIT_ASKPASS for tokens and a tmpfs 0600 key file for SSH, shredded after; the
hostile-repo hardening flags on every git invocation; the host allowlist from
config/git-hosts.yaml with the plain-language rejection message; the validation gate (path
scope, gitleaks, no new hooks/submodules/escaping symlinks, size, LFS off) and the Consensus
Router call for agent-authored diffs; branch push with PR/MR creation for GitLab, Gitea and
GitHub APIs; git bundle export/import; one audit row per operation. Build Settings → Git
remotes (paste-only fields, fingerprint after save, Test connection), Admin → Git hosts, the
per-project Git panel (Status, Commit, History, Push/Pull, Bundle) and the Terminal tab that
runs inside the sandbox over WebSocket. Tests: a fake Git server, a repo with a malicious
pre-push hook that must not execute, and a CI check that greps every log sink for token and
private-key patterns after a full push flow.
```

### P7 — Validation Agent against fakes
```
<preamble>
Task: implement plans/schema/plan.schema.json and plans/primitives; the plan compiler
(.md/.xlsx → plan.yaml with the reject rules in §10.2); packages/slas-hal with fakes/
(recorded Redfish, scripted SOL, SEL, inventory, including malformed and truncated
fixtures); packages/slas-diff with Hypothesis tests (device counts, PCIe width AND speed,
firmware, AER/EDAC/MCE/Xid, new SEL); services/validation-executor state machine
(ARM/QUIESCE/ACT/SETTLE/VERIFY/GATE) with fence markers, guardrails from
config/guardrails.yaml and crash recovery; packages/slas-triage (fingerprint, dedup, owner
routing); bug-ticket spawning in the [Issue] | [Owner] format; the Validation page with the
LED cycle map and console; and the three-step New validation run wizard. Plant a PCIe
degradation at cycle 14 in the fake and prove one deduplicated ticket results.
```

### P8 — Validation hardware
```
<preamble>
Task: implement the real Redfish, IPMI and SSH drivers in slas_hal behind the same
interface as the fakes, SOL capture, the syslog receiver, and the PDU driver for
<PDU model>. Add a quirk-shim layer keyed by BMC vendor/firmware. Add a CI check that greps
redacted log bundles for any credential pattern. I will provide one target: <alias>. Do not
run any power action until I confirm the target is free.
```

### P9 — Factory Agent against fakes
```
<preamble>
Task: implement templates/factory with the "final-test-9-steps" template, the MES adapter
interface with a file-drop implementation, services/station-runner (mTLS, signed step
batches, screenshots returned) with a runner fake, services/factory-executor with station
leases, and the Factory Agent on the kernel using skills for GUI steps: consensus PASS
requires 3/3, FAIL holds the station and drafts a line-lead ticket, production line SOP in
both languages, station state backup to Backups/stations/<n>. Build the Factory page
(test-step map, screenshot strip) and the three-step New factory job wizard.
```

### P10 — Factory station
```
<preamble>
Task: package services/station-runner for installation on a Windows/Linux test station
(single binary or venv, mTLS enrolment via a one-time code from Admin → Stations). Tune
window matching and timing for the "station-login-burnin" skill against the real station
<alias>. Add a screenshot retention setting. The operator must be able to watch and take
over through the VNC view at any point.
```

### P11 — Observability
```
<preamble>
Task: add Prometheus rules and six Grafana dashboards as provisioned JSON (inference, GPU,
agents, sandboxes and screens, validation runs, factory), the application metrics listed in
CLAUDE.md §8.2, alerting to a local channel for circuit-breaker trips and consensus
disagreements, and complete `slas status`. Verify one trace_id spans WebUI → api →
orchestrator → gateway → vLLM → executor.
```

### P12 — Prod profile
```
<preamble>
Task: implement compose/prod.override.yml with Vault injection at dispatch, Keycloak OIDC
alongside built-in auth, Harbor with cosign verification in install.sh, the Kata/Firecracker
sandbox tier, pgBackRest PITR with object-lock on run artifacts, macvlan overlays for lab and
factory, and docs/runbooks/restore-drill.md. Prove `./install.sh --profile prod` on a clean
host and record the restore RTO.
```

## 3. Utility prompts

### Phase-end audit
```
Read CLAUDE.md and docs/DEVELOPMENT_PLAN.md. Audit Phase <N> against its "Done when"
criterion and the Definition of done in CLAUDE.md §11. Run the test suite and the
egress-DROP job. List what is missing as a checklist with file paths. Do not write code.
```

### Fix a failure
```
<preamble>
Here is a failure, pasted verbatim:
<paste>
Find the root cause before proposing a fix. State the cause in one sentence, then the
smallest change that fixes it, then the test that would have caught it.
```

### Add a skill primitive
```
<preamble>
Task: add the primitive `<name>` to the skill whitelist in CLAUDE.md §6.2 and
skills/schema/skill.schema.json with args <args>, capability <cap>, risk class <class>.
Implement it in slas_skills and the matching driver, add fake support, add tests, and update
the Skills page help text. If the risk class is destructive, it must appear in the import-time
and run-time approval messages.
```

### Add a WebUI page or wizard step
```
<preamble>
Task: <describe the screen>. Before coding, write the copy for every label, status, empty
state and error in plain language following CLAUDE.md §9 and show it to me. Then build it
with the existing components. No raw JSON or enums as primary content.
```

### Write an ADR
```
Read CLAUDE.md §15. Write docs/adr/<NNNN>-<slug>.md for this decision:
<describe>. Include which invariants it touches and how any relaxation is contained. Status:
proposed.
```

### Update CLAUDE.md after a change
```
Read CLAUDE.md. This session changed <interface/boundary/phase>. Propose the minimal edit
to CLAUDE.md that keeps it truthful, bump the version, and reference ADR-<NNNN>. Show me the
diff before applying it.
```

### Weekly health check
```
Read CLAUDE.md. Check the repository for drift: modules that implement tickets, logs, RCA,
SOP or skills outside slas_kernel (§5.1); any subprocess call with shell=True; any container
that mounts a host socket, X11 socket or /dev/input other than model-manager and
sandbox-manager; any test that needs real hardware or a live model; any dependency added
without an ADR. Report as a checklist with file paths.
```

## 4. Prompting rules Claude Code should be reminded of
- Prefer editing an existing module over creating a parallel one.
- When unsure about a Redfish URI, window title, register or model behaviour, write a
  `TODO(SLAS-…)` comment and a failing test instead of guessing.
- Everything user-facing is a sentence. Everything machine-facing is a schema.
