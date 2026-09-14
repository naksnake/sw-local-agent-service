# Validation — page copy (CLAUDE.md §9, §10.2)

One primary action (New validation run). Each run shows its LED cycle map, its findings and
its console as sentences. Component: `apps/webui/src/validation/ValidationPage.tsx`.

| Element | Text |
|---|---|
| Heading | Validation |
| Lede | The Validation Agent drives one lab server at a time through a deterministic state machine. Models compile the plan and analyse the results; code performs every power action, and you approve anything destructive. |
| Primary button | New validation run |
| Loading | Loading runs… |
| Empty | No validation run yet. Start one with a suite; every cycle shows up here as it happens. |

## Run card

| Element | Text |
|---|---|
| Title | *GX8 DC cycling* · *T-validation-0001* · *lab-gx8-01* |
| Sentence, running | *7* of *25* cycles done. |
| Sentence, findings | *25* of *25* cycles done: *12* with findings. |
| Sentence, boot failures | *5* of *25* cycles done: *3* did not boot. |
| Sentence, waiting for approval | *T-validation-0001* waits for your approval of *2 steps* before anything touches *lab-gx8-01*. |
| Approval box | Nothing has touched *lab-gx8-01* yet. *2 steps need* your approval: *AC cycle 1 of 2, AC cycle 2 of 2*. → **Approve these steps and continue** |
| Cycle map | One square per cycle, `aria-label` "Cycle *14*: *finding*"; the tooltip is the cycle's sentence. Legend: green ok · amber finding · red did not boot · grey waiting |
| Cycle sentence, clean | Cycle *7* (DC): booted; no change against the baseline. |
| Cycle sentence, finding | Cycle *14* (DC): booted; 1 change against the baseline during DC cycle 14: PCIe link width changed on *NVIDIA H100 SXM (0000:8a:00.0)*: x16 → x8 during DC cycle 14. |
| Cycle sentence, no boot | Cycle *5* (DC): the target did not come back within *900* s. |
| Cycle sentence, resumed | … Resumed after an interruption without repeating the power action. |
| Abort | … That is *3* boot failures in a row; the run is aborted (guardrail consecutive_failure_abort=3). A person needs to look at the target. |
| Findings, none yet | Findings appear here after the first cycle. |
| Findings, clean | No change against the baseline so far. |
| Finding | *PCIe link width changed on NVIDIA H100 SXM (0000:8a:00.0): x16 → x8 during DC cycle 14*. Owner: *EE*. → **Review ticket** (opens the child bug ticket, titled `[Issue] … \| [Owner] EE`) |
| Console, empty | The serial console streams here once the run starts. |
| Console | The last 40 lines; fence markers read `--- slas fence T-validation-0001 cycle 14 dc ---` |

The run itself ends in **Needs review** whenever a finding exists; the finding's bug ticket
is where a person confirms or dismisses it. A consensus of models never marks anything
PASS or FAIL (INV-11).
