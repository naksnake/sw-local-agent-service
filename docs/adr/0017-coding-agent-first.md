# ADR-0017: The Coding Agent is the delivery focus; the other agents start on request

Status: accepted
Date: 2026-09-17

Accepted on the project owner's instruction of 2026-09-17: "Let this project focus on Coding
Agent."

## Context
CLAUDE.md §1 describes one kernel and three agents, and round 2 (ADR-0015) put all three on
the wire. The first installation, on a connected GPU host, is for writing code: the
Validation Agent needs a lab with target servers behind BMCs, the Factory Agent a
production line with enrolled test stations; neither exists at that site yet. Starting their
executor containers there adds two images to build, two services to keep healthy, two pages
whose wizards cannot finish, and two more places a first install can fail, for no work they
can do. §1.2 value 4, "simple to run", says to start what the site uses.

The owner's instruction is a scope decision, not a change of architecture: nothing in the
kernel, the zone model or the invariants changes, and CLAUDE.md §0 says a conflict between
a request and the file is surfaced and recorded, never resolved silently.

## Decision
- **`SLAS_AGENTS` names the agents and optional parts an installation starts; the default is
  `coding`.** `knowledge` (Qdrant and the local search api, §8.3) is an optional part on the
  same list: the Coding Agent does not read the knowledge base until the RCA round, so a
  coding-only site does not start it.
  `./install.sh --agents coding,validation,factory` (or the variable, or the existing `.env`)
  turns the others on. The installer validates the list in three parts, writes it to `.env`,
  builds only the executor images of the chosen agents, and starts their compose profiles
  (`COMPOSE_PROFILES=validation,factory`). `validation-executor` and `factory-executor` carry
  those compose profiles, so an install without them never creates their containers.
- **The api tells the WebUI.** `GET /api/v1/public/installation` gains `agents`; the rail
  shows Coding, Validation, Factory and Admin → Stations only for the agents that are on.
  The orchestrator keeps every router: a call to an agent that is off answers with the
  executor's three-part 503, never a blank page, because the page is not offered.
- **Development focus follows.** Rounds after this one improve the Coding Agent first:
  the model loop on real weights, the Git panel and terminal, the walkthrough, the
  virtual desktop. Validation and Factory stay built, tested against fakes and released,
  and pick up again when a lab or a line is connected. `docs/DEVELOPMENT_PLAN.md` records
  the order.
- **Nothing is removed.** One kernel, three agents (CLAUDE.md §1.1) stands; the invariants
  and the zone model are unchanged; the shipped model registries keep every role.

## Consequences
- A default quickstart install builds 10 first-party images instead of 12 and runs two
  fewer services; the preflight, `slas doctor` and the health wait are unchanged.
- The runbooks describe the default as the Coding Agent install and show the one flag that
  enables the others.
- `config/models.quickstart.yaml` still serves `planner`, `triage`, `embed` and `rerank`;
  the Coding Agent uses `coder`, the voters and `planner` (SOP translation). Trimming the
  registry for a coding-only site is a later decision on the Models page, not this ADR.

## Invariants touched
None relaxed. INV-9 (models and users change without restarts) is untouched: turning an
agent on is an installer choice that adds containers, the same class of change as the
profile; INV-10 (one command to a login page) is easier to meet with fewer services.
