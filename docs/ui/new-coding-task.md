# New coding task — wizard copy (CLAUDE.md §9, §10.1)

Three steps, always. Ends in a sentence that says what will happen and one verb button.
Component: `apps/webui/src/coding/NewCodingTaskWizard.tsx`.

| Element | Text |
|---|---|
| Step indicator | Step *1* of 3 — Plan · Setup · Review |
| Heading | New coding task |

## Step 1 — Plan

| Element | Text |
|---|---|
| Plan field placeholder | Drop or paste plan.md here. A title line and a list of tasks is enough. |
| File field label | Or choose a file |
| Before a plan | Languages are detected from the plan once you add one. |
| Nothing recognised | No language we recognise is mentioned yet; you can pick them in the next step. |
| Detected | Languages detected: *Python, Rust*. |
| Buttons | Cancel · Next: Setup (disabled until the plan has text) |

## Step 2 — Setup

Languages only; a version is optional.

| Element | Text |
|---|---|
| Languages help | Pick the languages the task uses. Leave a version empty and the agent uses the newest bundled toolchain and says which one. |
| Version placeholder | newest bundled |
| Isolation | gVisor if this host has it, otherwise hardened runc · gVisor only (the task waits if gVisor is missing) |
| Skills, none enabled | No skill is enabled for the Coding Agent. Enable one on the Skills page. |
| Cross-check | Three voters review the final diff. Their concerns are shown to you; you still decide. |
| Export | ZIP file (always available) · Push a branch to a Git remote you have added · Git bundle for another site |
| Remote, none saved | You have no saved remote. Add one under Settings → Git remotes; the sandbox itself never holds a credential. |
| Remote select | Choose a remote — names only, never an address or a token |
| Buttons | Back · Next: Review (disabled with no language, or remote export without a remote) |

## Step 3 — Review

| Element | Text |
|---|---|
| Task name | editable |
| Proposed steps help | Edit, add or remove tasks. The agent does them in this order. |
| Toolchain, no pin | *Python*: no version pinned, so the newest bundled *python 3.12.6* is used. |
| Toolchain, pin present | *Python 3.11* pinned; the bundle has it as the newest 3.11.x, using *3.11.10*. |
| Toolchain, pin missing | *Rust 1.99* isn't in the offline toolchain bundle, so the newest bundled *1.80.1* is used instead. |
| Closing sentence | The agent will work in an isolated sandbox with *Python 3.12.6 and Rust 1.80.1*, do *3 tasks*, commit on its own branch, cross-check the result with 3 voters, and export a ZIP. *(+ any toolchain fallback sentence)* |
| Primary button | Start task · while waiting: Starting… |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Not started | The task didn't start. | The api service didn't answer. | Try again; if it repeats, run `slas logs api` on the host. |

After Start task the wizard closes and the ticket card appears on the Coding page; the first
activity line is the toolchain choice.
