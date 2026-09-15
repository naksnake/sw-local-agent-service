# New validation run — wizard copy (CLAUDE.md §9, §10.2)

Three steps, always. Ends in a sentence that says what will happen and one verb button.
Component: `apps/webui/src/validation/NewValidationRunWizard.tsx`.

| Element | Text |
|---|---|
| Step indicator | Step *1* of 3 — Suite · Target · Review & approve |
| Heading | New validation run |

## Step 1 — Suite

| Element | Text |
|---|---|
| Suite field placeholder | Paste suite.md here: a title line and one bullet or table row per item, for example `- DC cycle x25, settle 60 s`. |
| File field label | Or choose a file (`.md`, `.xlsx`) |
| Before a suite | The items are listed here once you add the suite. Destructive items are flagged. |
| Parsed | *GX8 DC cycling*: *2 items*, *26 cycles* in total. then one line per item: *1. DC cycle ×25* |
| Destructive, flagged | … — destructive; the run will ask for your approval before this step. |
| Destructive, not flagged | … — destructive and not flagged as approved in the suite; add `approved` to the item or the plan is refused. |
| Buttons | Cancel · Next: Target (disabled until every item parses and every destructive item is flagged) |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Empty | *suite.md* is empty. | A suite needs a title and at least one item. | Add the items and upload again. |
| No items | *suite.md* has no items. | Items are table rows under a Step/Action header, or bullets such as `- DC cycle x25, settle 60 s`. | Add the items and upload again. |
| No header | *suite.md* has a table without a Step or Action column. | The suite table needs at least a Step column. | Add the header row and try again. |
| Unknown action | Step *1* (*Dance the macarena*) names no known action. | A plan may only use the Validation primitives in plans/primitives/validation.yaml. | Reword the step (for example `DC cycle x25, settle 60 s`), or remove it. |
| Too many cycles | The suite asks for *101* power cycles; the limit is *100* per run. | Guardrail max_cycles_per_run (config/guardrails.yaml). | Split the suite into several runs, or lower the cycle count. |

## Step 2 — Target

| Element | Text |
|---|---|
| Legend | Pick a free server |
| Help | One run per target at a time. Credentials come from the vault; nothing here shows or asks for a password. |
| Server | *lab-gx8-01* · *SLAS-GX8* |
| Busy server (disabled) | *lab-gx8-02* · *SLAS-GX8* — Busy: *lab-gx8-02 is leased to T-validation-0007 (lee) until 2026-09-17 08:00.* |
| No servers | No server is registered yet. Add one under Admin → Targets before starting a run. |
| Buttons | Back · Next: Review (disabled until a server is picked) |

## Step 3 — Review & approve

| Element | Text |
|---|---|
| While compiling | Compiling the plan… |
| Closing sentence | *GX8 DC cycling* on *lab-gx8-01*: *25* power cycles and *2* suite items, *31* steps. Nothing destructive. · … *2* destructive steps need your approval before the run starts. |
| Cross-check | *3 of 3* voters agree the plan stays within the guardrails. Your approval starts it. |
| Approval list heading | Steps that will ask for your approval — then one line per step; the run stops before each of them until you approve it on the Validation page. |
| Guardrails | The sentences from `config/guardrails.yaml`: At most 100 power cycles per run. · At least 10 s settle after a warm or DC cycle, 30 s after AC. · A boot that takes longer than 15 minutes counts as failed. · 3 boot failures in a row abort the run. · One run per target at a time. · A run stops after 72 hours. · Needs your approval every run: ac_cycle, firmware_flash, secure_erase, bios_reset, raid_reconfigure. |
| Primary button | Approve and start · while waiting: Starting… |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Not started | The run didn't start. | The api service didn't answer. | Try again; if it repeats, run `slas logs api` on the host. |

After Approve and start the wizard closes and the run card appears on the Validation page.
A plan with destructive steps appears as *Planned* with the approval box; nothing touches
the hardware until every step is approved (INV-7). The voters' agreement is input to that
decision, never the decision (INV-11).
