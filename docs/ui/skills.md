# Skills — copy (CLAUDE.md §5.6, §6, §9)

The skill library: every imported skill, its risk, and one switch per agent its author
allows. One primary action, New skill. A skill opens in place (no nested dialog) to show its
steps; the editor checks a file with the orchestrator's importer before anything is saved.
Skills are data (INV-12): the page sends YAML and shows what the importer says. Component:
`apps/webui/src/skills/SkillsPage.tsx`; routes in docs/api-contract-round-2.md "Skills".

| Element | Text |
|---|---|
| Heading | Skills |
| Lede | Reusable recipes made only of whitelisted steps. Write one once, then switch it on for any agent it allows. A skill never gives anyone a capability they don't already hold. |
| Buttons | Import a file (loads a .skill.yaml into the editor) · New skill (primary) |
| Empty library | No skill yet. Add one of the examples below, write one with New skill, or import a .skill.yaml file. |
| Table columns | Skill · Risk · Switched on for · Needs |
| Risk | Safe · Caution: runs commands · Destructive: asks for approval |
| Switch | one per allowed agent: *Coding Agent*, *Validation Agent*, *Factory Agent* |
| Switched | *Python lint and tests*: On for the Coding Agent since *13 October 2026*. |
| Row buttons | Export (downloads *id*.skill.yaml) · Delete |
| Opened | the description, the state sentence, Steps (one line each: *run: ruff check .*), "Asks for: …", "Imported by *pat* on *…*.", Edit |
| Examples | Examples that ship with the platform — cards with the description, "For the *Coding Agent* · needs *files*", Add to library (In your library once added) · Start from this |

## Editor

| Element | Text |
|---|---|
| Title | New skill · Edit *name* · Import *file name* · New skill from *example* |
| Hint | Check reads the file the way an import does and saves nothing. |
| Field | Skill file — help: YAML in the format of CLAUDE.md §6: an id, a name, a version, the agents it is for, what it needs and its steps. |
| Check result | *My project checks v1.0.0: 1 step, needs files, risk caution.*, the steps, the approval sentence for a destructive skill, then "Saving adds *name* to the library, switched off for every agent." or "Saving replaces version *1.0.0* in the library; its switches stay on unless the new version allows more agents, needs more or is riskier." |
| Buttons | Check · Save to library (primary) · Cancel |
| Saved | the importer's sentence and the state sentence, e.g. *… risk caution. Off for every agent.* |

## Delete (dialog)

Title: Delete *name*? Body: *name* v*1.0.0* leaves the library: no agent can use it any more.
Tickets that already ran it keep their record. To stop using it for a while, switch it off
instead. Primary button (danger): Delete *name*. After it: *name v1.0.0 was deleted; the
Coding Agent can no longer use it. Tickets that already ran it keep their record.*

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Not valid YAML | The skill file is not valid YAML. | the parser's first line | Fix the file and import it again; the format is CLAUDE.md §6.1. |
| Not a primitive | … uses `shell` …, which is not a skill primitive. | Skills may only use the whitelisted verbs. | Fix the file and import it again; the whitelist is CLAUDE.md §6.2. |
| Lacks a capability | You can't import *name*: it needs *screen*. | Your role does not include it, and a skill never adds capabilities. | Ask an administrator for a role that includes it, or import a skill that needs less. |
| Not allowed to delete | You can't delete *name*. | It was imported by *lee*; only they or an administrator may delete it. | Ask them, or switch it off for your agent instead. |
| Orchestrator down | The skill library didn't load. | The orchestrator didn't answer. | Try again; if it repeats, run `slas logs agent-core-orchestrator` on the host. |

## Skills in the Coding Agent

A skill picked in the New coding task wizard becomes a step after Open a sandbox: *Use the
skill python-lint-and-test: its steps become checks for every task*. Its `run` steps run in
the sandbox after the language's own checks in every iteration, and a task is done only when
they pass too. A skill with any other step (a screen click, Redfish) is refused for coding
with a sentence: only `run` steps work in the sandbox.
