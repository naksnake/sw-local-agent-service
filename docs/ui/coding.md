# Coding — page copy (CLAUDE.md §9, §10.1)

One primary action (New coding task). Each task shows its plan checklist and its activity
feed as sentences. Component: `apps/webui/src/coding/CodingPage.tsx`.

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
| Activity, task | *Task 1*: done after *2* iterations; lint ok, type ok, test ok. |
| Activity, stalled | *Task 1*: stopped after *4* iterations because the last 3 made no progress (*test failed*). A person needs to look at it. |
| Activity, commit | Committed *1a2b3c4d5e* on branch *slas/T-coding-0001* with Slas-Agent and Slas-Ticket trailers. |
| Activity, cross-check | *2 of 3* approve the change. *voter-3 has a concern: …* |
| Activity, no voters | Not cross-checked: no voters are configured, so the change needs your own review before it is used. |
| Footer | Open the Terminal tab to inspect the branch; push happens from the Git panel, which uses your saved remote. |

The Git panel (Status · Commit · History · Push/Pull · Bundle) and the Terminal tab arrive
with the git-broker session; `git push` in the terminal fails with the footer sentence.
