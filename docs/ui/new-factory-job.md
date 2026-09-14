# New factory job — wizard copy (CLAUDE.md §9, §10.3)

Three steps, always. Ends in a sentence that says what will happen and one verb button.
Component: `apps/webui/src/factory/NewFactoryJobWizard.tsx`.

| Element | Text |
|---|---|
| Step indicator | Step *1* of 3 — Trigger · Test loop · Rules |
| Heading | New factory job |

## Step 1 — Trigger

| Element | Text |
|---|---|
| MES list legend | Production tickets waiting in the MES |
| MES row | *MES-88131*: unit *SN-GX8-0100* on *station-07* · from *mes* |
| MES empty | No production ticket is waiting. Scan a label or enter the unit by hand below. |
| Label legend | Or scan a label — placeholder: Scan or paste the label, for example SN SN-GX8-0100 station station-07 → **Read label** |
| Manual legend | Or enter the unit by hand — Serial number · Pick a station (busy stations marked) → **Use these** |
| Before a choice | Pick a production ticket, scan a label, or enter the unit by hand. |
| Chosen | Unit *SN-GX8-0100* on *station-07*, from MES ticket *MES-88131* · from a label scan · from a manual entry. |
| Busy station | Unit *SN-GX8-0300* on *station-08*: the station is busy. *station-08 is leased to T-factory-0007 (mes) until …* |
| Buttons | Cancel · Next: Test loop (disabled until a unit and a free station are chosen) |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Bad label | The label names no unit and station. | A label scan or manual entry must carry the serial number and the station, for example `SN SN-GX8-0100 station station-07`. | Scan the label again or type both values. |

## Step 2 — Test loop

| Element | Text |
|---|---|
| Legend | Test-loop template |
| Template | *Final test, 9 steps* — *Power the unit on, log in to the station and start BurnIn, wait for the test, read the result, the sensors and the event log, back up the station, decide with 3 voters.* |
| Steps | The template's steps, numbered, then: Release the station |
| Skills | Skills used for the GUI steps: *station-login-burnin*. Every GUI step is screenshot before and after. · No skill is used; every step is a station command or a reading. |
| Buttons | Back · Next: Rules |

## Step 3 — Rules

| Element | Text |
|---|---|
| Voters | Three voters see the result, the sensors and the event log. PASS needs all three; a split vote goes to the line lead. The voters never mark a unit PASS or FAIL on their own. |
| On failure | The unit stays on, the station stays leased, and a ticket is drafted for the line lead. |
| Exports | Production line SOP in English and Chinese (always) · Back up the station state (config, recent logs, application versions) |
| Closing sentence | *Final test, 9 steps* for unit *SN-GX8-0100* on *station-07*: *10* steps, using the *station-login-burnin* skill. PASS needs 3 of 3 voters; anything else holds the station for the line lead. The station state is backed up. |
| Primary button | Start job · while waiting: Starting… |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Not started | The job didn't start. | The api service didn't answer. | Try again; if it repeats, run `slas logs api` on the host. |

After Start job the wizard closes and the job card appears on the Factory page with its
test-step map and screenshot strip.
