# Coding — page copy (CLAUDE.md §9, §10.1)

One primary action (New coding task). Each task shows its plan checklist and its activity
feed as sentences. Component: `apps/webui/src/coding/CodingPage.tsx`. Each task is a panel:
title and ticket id, a state pill (blue Running or Starting, green Done, amber Needs your
review, red Failed) and a progress bar of steps done; the checklist marks each step (✓ done,
◐ running, ○ waiting, ! needs you, – skipped); the feed shows its newest 12 lines with
*Show all N lines*, and lines that report a failure read in red. While an iteration runs,
the agent writes live lines (below) so the feed moves within a step, not only between steps.

| Element | Text |
|---|---|
| Heading | Coding |
| Lede | The Coding Agent works in an isolated sandbox on this host, commits on its own branch, and never holds a Git credential. |
| Primary button | New coding task |
| Loading | Loading tasks… |
| Empty | No coding task yet. Start one with a plan; the agent shows every step here as it works. |

## Task card

| Element | Text |
|---|---|
| Title | *Fan controller* · *T-coding-0001* |
| Sentence | *T-coding-0001* is running: step *2* of *7*. |
| Plan checklist | *n.* step title — waiting · running · done · needs you · skipped |
| Activity, first line | Toolchain: *Python 3.12.6 and Rust 1.80.1*. *(+ fallback sentence if a pin was not available)* |
| Activity, sandbox | The sandbox runs under gVisor. · gVisor is not installed on this host, so the sandbox runs under hardened runc: its own user namespace, a seccomp profile, no network and no capabilities. |
| Activity, iteration (live) | Iteration *2* of *6*: fixing *test*… |
| Activity, approach (live) | Approach: *Parse the CSV with the csv module* · *Report min, max and average* |
| Activity, edits (live) | Changed *templog.py, tests/test_templog.py*: *added the --csv option* Running the checks… |
| Activity, check failed (live) | Iteration *1*: *test failed* — test: *FAILED tests/test_templog.py::test_avg - AssertionError* (*lint ok*). |
| Activity, task | *Task 1*: done after *2* iterations; lint ok, type ok, test ok. |
| Activity, stalled | *Task 1*: stopped after *4* iterations because the last 3 made no progress (*test failed*). A person needs to look at it. |
| Activity, commit | Committed *1a2b3c4d5e* on branch *slas/T-coding-0001* with Slas-Agent and Slas-Ticket trailers. |
| Activity, cross-check | *2 of 3* approve the change. *voter-3 has a concern: …* |
| Activity, no voters | Not cross-checked: no voters are configured, so the change needs your own review before it is used. |
| Footer | Open the Terminal tab to inspect the branch; push happens from the Git panel, which uses your saved remote. |
| Download | Download ZIP *(once Export a ZIP is done)* |
| Follow-up label *(finished tasks)* | Ask the agent for a change to this project |
| Follow-up placeholder | For example: add a --csv option that writes the summary as CSV, with a test |
| Follow-up button | Send to the agent · Starting… |
| Follow-up hint | Ctrl+Enter sends. The agent changes *Fan controller* with its current files in view, runs the checks and commits on a new branch. |
| Follow-up started | *T-coding-0002* started on *Fan controller*: *the prompt's first line* |
| Follow-up refused | The change was not started: *the service's sentence* |

A follow-up prompt is a new coding task on the same project (same title, so the same
`Projects/<slug>/` and its files): the prompt is its plan, its first line its one task, the
earlier task's toolchain is reused, and the checks, commit, cross-check and ZIP run as for
any task.

The Git panel (Status · Commit · History · Push/Pull · Bundle) and the Terminal tab arrive
with the git-broker session; `git push` in the terminal fails with the footer sentence.
