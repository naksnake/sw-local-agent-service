# plans

Validation plan schema and primitives (CLAUDE.md §10.2), both rendered from
`slas_hal.primitives` — a unit test fails when either file drifts from the code.

| File | What it is |
|---|---|
| `schema/plan.schema.json` | JSON Schema (draft 2020-12) for a compiled plan: `{id, job_id, summary, created_at, steps[]}`, at most 200 steps, each step one of the primitives below with exactly its arguments. |
| `primitives/validation.yaml` | The verbs a Validation plan may use, what each needs and its risk: `lease_target`, `console_on`, `baseline_snapshot`, `power_cycle` (kind `warm` / `dc` / `ac`; `ac` is destructive), `sel_snapshot`, `inventory_snapshot`, `stress`, `run_diag`, `firmware_flash`, `secure_erase`, `bios_reset`, `raid_reconfigure`, `collect_logs`, `release_target`. Destructive ones need a per-run human approval (INV-7). |

Compiled plans land in `${SLAS_DATA_ROOT}/Validation/Plans/<plan-id>/plan.yaml`. Factory plan
primitives arrive with P9.
