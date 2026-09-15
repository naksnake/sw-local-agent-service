# Factory — page copy (CLAUDE.md §9, §10.3)

One primary action (New factory job). Each job shows its test-step map, the station's
screenshot strip, the verdict as a sentence and, when a unit is held, the line lead's
decision. Component: `apps/webui/src/factory/FactoryPage.tsx`.

| Element | Text |
|---|---|
| Heading | Factory |
| Lede | The Factory Agent takes one unit at a time through the test loop on its station. Code performs every step and screenshots every GUI action; three voters review the result, and a unit passes only when all three agree. |
| Primary button | New factory job |
| Loading | Loading jobs… |
| Empty | No factory job yet. Start one from a production ticket or a label; every step shows up here with its screenshots. |

## Job card

| Element | Text |
|---|---|
| Title | *Final test of SN-GX8-0100 on station-07* · *T-factory-0001* · *MES-88131* |
| Sentence, running | *4* of *10* steps done. |
| Sentence, PASS | *10* of *10* steps done. Verdict: PASS (*3 of 3 voters*). |
| Sentence, FAIL | *9* of *10* steps done. Verdict: FAIL; the station is held for the line lead. |
| Sentence, split vote | *9* of *10* steps done. The voters did not agree; the line lead decides. The station is held. |
| Test-step map | One cell per step, `aria-label` "Step *9*: *done*"; the tooltip is the step's sentence. Below it the numbered list: *n.* title — waiting · running · done · needs you · skipped |
| Screenshot strip | Every screenshot the station sent back, in step order, `alt` "Step *3*, screenshot *1*"; before any GUI step: Screenshots appear here as soon as the first GUI step runs. |
| Verdict, PASS | PASS: *3 of 3* voters say PASS. *3 of 3 agree with the conclusion.* |
| Verdict, FAIL | FAIL: Unit *SN-GX8-0100* failed the final test on *station-07*: *BurnIn reported FAIL (gpu-memory)*. The unit stays on and *station-07* is held; a ticket is drafted for the line lead. |
| Verdict, split | The line lead decides: Unit *SN-GX8-0100* passed every check on *station-07*, but only *2 of 3* voters say PASS (*…*). The unit stays on and *station-07* is held; a ticket is drafted for the line lead. |
| Verdict, no voters | The line lead decides: … but no voters are configured. … Not cross-checked: no voters are configured, so the line lead decides. |
| Draft ticket | A ticket is drafted for the line lead. → **Review ticket** (the child ticket, `[Issue] … \| [Owner] TE`) |
| Backup | Station backup: *Backups/stations/station-07/T-factory-0001* |
| Held box | *station-07* is held and the unit stays on until you decide. Your decision is recorded with your name; the voters' view is input, not the verdict. Note field: What you checked, in a sentence → **Decide PASS** · **Decide FAIL** |
| Verdict, line lead | PASS: decided by *lee*. *Reseated the GPU riser; retest passed by hand.* |

A PASS/FAIL decision by the line lead needs the `factory:verdict` capability (config/rbac-roles.yaml)
and is a destructive act in the INV-7 sense: it is a person's, recorded with their name, never
the voters' (INV-11).

## Watch and take over (P10)

Shown on a running job when the station record has VNC on. Every control action is recorded
on the job with the operator's name; the runner pauses only at a step boundary, never
mid-action.

| Element | Text |
|---|---|
| Heading | Watch and take over |
| Before watching | You can watch *station-07* live and take it over at any point. The runner stops at the next step boundary and sends no input until you hand it back. → **Watch station** · **Take over** · **Abort** |
| Watching | Watch the station at *vnc://127.0.0.1:5901 (relayed over mTLS to https://station-07:8443)*. Read-only until you take over. — The runner drives *station-07*. |
| Taken over | *lee* has taken over *station-07*; the runner sends no input until it is resumed. → **Resume** · **Abort** |
| Aborted | *lee* aborted the run on *station-07*. The running step reads: Stopped: *lee* took over *station-07*. The job ends as Failed; the unit stays on and the station is held. |
| VNC off | VNC is not enabled on *station-07*. The station record has VNC off. Enable it under Admin → Stations and re-enrol. |
