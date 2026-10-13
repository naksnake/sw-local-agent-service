# ADR-0021: The coder may ask for commands in its own sandbox

Status: accepted
Date: 2026-10-13

Accepted on the project owner's instruction of 2026-10-13: "Ensure the coding agent same as
Claude code".

## Context
The Coding Agent's loop (§10.1) was: the model proposes edits, code applies them and runs
the language's fixed checks (`slas-check lint|type|build|test`) in the sandbox. The model
could not look at anything the snapshot left out, could not try the program it had just
written, and could not narrow a failing test down. Claude Code, which the owner names as
the bar, works by running commands and reading their output. Zone A exists to run
model-authored code (§4.1): the checks already execute the model's code and tests there,
with no network, no credential and no route to a remote (INV-4, INV-14).

## Decision
- `EditSet` gains `commands`: at most 4 per iteration, each an argv list with a `why`. The
  executor runs them in the task's sandbox through the sandbox manager's exec API, after
  the edits and before the checks, with a 120 s timeout each and their output bounded to
  3000 characters. The output reaches the model on the next iteration; each command is a
  live line in the task's activity feed.
- Commands are argv, never a shell line: a shell given `-c` is refused, as is `sudo`, `su`
  and `doas`. Git runs only read-only subcommands (status, log, diff, show, grep, ls-files,
  blame, rev-parse, branch), because the agent commits after the checks pass and a commit
  of the model's own would bypass the trailers and the validation gate. A refused command
  is reported back to the model as "Not run: …" and never reaches the sandbox.
- A task whose checks pass on code the model just changed, while it asked to see command
  output, gets one more iteration to read it (unless it was the last one); a task that
  changed nothing is done when its checks pass, as before.
- The model also sees the project's last 12 commits (subject and task lines), so a
  follow-up prompt knows what earlier tasks did, and the commit step keeps the task's diff
  as `Artifacts/<ticket>/changes.diff` for the Coding page's Changes view.
- `POST /v1/coding/tasks/{id}/stop` asks a running task to stop after the model call in
  flight; that answer is not applied, the step ends as failed with who pressed Stop, and
  the ticket waits for review with the files as they are.

## Consequences
The model can explore and verify inside the sandbox, as a person would in a terminal; a
failing test is narrowed down in one iteration instead of being guessed at. An iteration
can take up to 8 minutes longer (4 × 120 s); the stall rule and `max_iterations` still bound
a task. Nothing the model asks for runs anywhere but its sandbox, and nothing it asks for
reaches a Git remote.

## Invariants touched
INV-3 — unchanged: the sandbox has no physical effect; hardware, stations and power stay
behind the deterministic executors. INV-4 — unchanged: commands run in Zone A only, never on
the platform host. INV-12 — unchanged: skills keep their whitelist; these are the coder's
requests inside its own sandbox, not skill primitives. INV-14 — unchanged: the sandbox holds
no credential and has no route to a remote, and write subcommands of git are refused.
