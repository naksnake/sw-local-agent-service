# ADR-0013: Per-agent skill enablement is an installation record, not part of the skill

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md §5.6 says "skills declare which agents may use them; enabling is one toggle per
agent", and §9 gives the Skills page the toggle. `slas_skills` today validates, compiles,
runs and exports a skill, but nothing records "on for Factory": the `Skill` model carries
`agents` (what the author allows) and no installation ever says what it has turned on.
The Skills page cannot be built without that record, and §15 requires an ADR for a
data-model change.

Three forces shape where the record goes. INV-12 says a skill is data that travels
between installations unchanged, so a site's choice to enable it must not ride inside the
file or change its content hash. INV-9 says a person's change never needs a config edit or
a restart, so the record must be read on use. INV-7 says a destructive step needs a per-run
approval, so "enabled" must never be mistaken for "approved".

## Decision
- **One record per library entry, outside the skill file.** `Skills/library/<id>.state.json`
  sits beside `<id>.skill.yaml` and holds `SkillState {skill_id, version, imported_by,
  imported_at, enabled: {agent: {by, at}}}`. Absent record or absent agent means off. The
  record is a Pydantic model in `slas_skills.state`, written atomically with mode 0644 by
  the api only, and read on every use. When the api gains its Postgres stack (ADR-0005), the
  record moves to a `skill_state` table with the file as seed and mirror, in the shape
  ADR-0008 uses for settings; nothing on the page or in the kernel changes.
- **Off by default, on only where the author allows.** A fresh import is off for every
  agent. A toggle is accepted only for an agent in the skill's `agents` list; the UI shows
  no switch for the others. Turning a skill on grants nothing: the capability check at
  import (INV-12) and the runner's per-run approval (INV-7) are unchanged and still run.
- **Two deterministic gates read the record.** The "New …" wizards list only skills enabled
  for their agent. The kernel's skill expansion at PLAN refuses a skill that is not enabled
  for the agent with a three-part error ("Collect and clear the BMC event log is not turned
  on for Validation." / "Someone turned it off, or it was never turned on here." / "Turn it
  on under Skills, or remove it from the plan."). A run already past PLAN keeps its compiled
  plan: turning a skill off never interrupts a running job, and the UI says so.
- **Who may toggle.** The person who imported the skill, and anyone holding
  `admin:settings`. No new capability is added; the roles file is unchanged.
- **Replacing a version resets enablement when the skill grew.** Re-importing an id with a
  newer version keeps the record only if the new version's `agents` and `requires` are
  subsets of the old and its risk class did not rise. Otherwise every agent goes back to
  off and the import result says why. Same version re-imported: content must match the
  stored hash, or the import is treated as a replacement.
- **Shipped skills** have a record like any other, start off, can be turned off again, and
  cannot be deleted; the record marks `imported_by: platform`.
- **Export never carries the record.** `export_skill` and the content hash cover the YAML
  only, as today. Usage figures shown on the page ("used 3 times, last run 2 hours ago")
  come from tickets and `slas_skill_runs_total`, never from the record, so there is one
  truth for what ran.

## Alternatives considered
- An `enabled:` key inside the skill YAML: the file would change per site, the content hash
  would drift between installations, and an exported skill would arrive pre-enabled;
  rejected on INV-12.
- Enablement in `config/*.yaml` or `.env`: needs an edit and a restart; rejected on INV-9
  and §9.
- A dedicated `skills:manage` capability: correct in the long run but a roles-file and
  ADR-0006 change for one toggle; deferred, recorded here so it can be added without
  re-deciding the record.

## Consequences
Easier: the Skills page, the wizard filters and the kernel gate all read one small model
with fakes-only tests; a skill file stays byte-identical across sites. Harder: the library
directory has two file kinds per skill, and the later move to Postgres has to keep the file
as seed and mirror. A skill turned off mid-job finishes that job; operators who need it
stopped now use the job's own stop, not the toggle.

## Invariants touched
- **INV-12** (skills are data): implemented, not relaxed. The record lives outside the
  file, cannot grant a capability, and export strips it by construction.
- **INV-7** (destructive needs per-run approval): unchanged. "Enabled" is a precondition
  for planning, never an approval; the runner still stops on an unapproved destructive step
  and the page says so beside the switch.
- **INV-9** (no config edit or restart): held by reading the record on use; no service
  environment names it, and a toggle changes no compose input.
- **INV-3**, **INV-6**: untouched; the gate is deterministic code at PLAN and the executor
  is unchanged.
