# Shell and Home — copy (CLAUDE.md §9; reference: docs/ui-demo/slas-ui-demo.html)

The demo is the intended UI. The shell is a left rail, a top bar with one health sentence,
and the page. Home is a dashboard: what is running now, and what needs you. Components:
`apps/webui/src/App.tsx` (shell), `apps/webui/src/home/HomePage.tsx`.

## Shell

| Element | Text |
|---|---|
| Brand, first line | SW Local Agent Service |
| Brand, second line | Self-hosted, no cloud |
| Rail, in order | Home · Coding · Validation · Factory · Runs · Models · Skills · Settings · Admin |
| Rail foot | Version *0.0.1* · Air-gapped mode is on |
| Health, nothing running | Everything is healthy. Nothing is running. |
| Health, work in progress | Everything is healthy. *3* jobs running. *(singular: 1 job running)* |
| Health, something needs a person | *1* item needs you. *3* jobs running. |
| Health, GPUs and models known *(when the Models service exists)* | Everything is healthy. *4* GPUs at *16*% memory, *3* models serving, *3* jobs running. |
| Top bar, right | *14:02:11* · Signed in |
| Signed in, pressed | Signed in as *pat*. Roles and permissions are managed under Admin → People. |

## Home

| Element | Text |
|---|---|
| Heading | Home |
| Lede | What is running now, and what needs you. |
| Buttons, in order | New coding task · New factory job · **New validation run** (primary) |
| Loading | Looking at what is running… |

### Needs you (one three-part notice per item; none when nothing needs a person)

| Case | Title | What happened | Likely cause | What to do | Buttons |
|---|---|---|---|---|---|
| Validation run waiting for approval | Validation run *T-validation-0001* is waiting for your approval. | *2* destructive steps need a per-run approval before anything touches *rack3-slot07*. | The suite includes power cycles or another destructive step. | Open the run, read the plan, and approve it or take those steps out. | Open run |
| Coding task stopped | Coding task *Fan controller* stopped. | *T-coding-0001* is stopped: the last 3 iterations made no progress. | The plan asks for something the repository does not contain, or a test cannot pass as written. | Open the task, read the last feed lines, then edit the plan or attach what is missing. | Open task |
| Factory unit did not pass | Unit *SN-GX8-0101-F* on *station-07* did not pass. | *T-factory-0002* holds the station; the line lead decides. | The unit failed the test loop, or the voters did not agree. | Open the job and record PASS or FAIL with a note. | Open job |

### Running now

| Element | Text |
|---|---|
| Heading | Running now |
| Row title | Coding task *T-coding-0001* — *Fan controller* / Validation run *T-validation-0001* — *Power cycle suite*, *rack3-slot07* / Factory ticket *T-factory-0001* — Station *station-07*, unit *SN-GX8-0100* |
| Row second line | the item's own sentence, for example *T-coding-0001 is running: step 2 of 7.* |
| Progress | a bar, steps done over steps total; pill Running · Waiting for approval · Starting |
| Empty | Nothing is running. Start a task, run or job with the buttons above. |

### Recent results

| Element | Text |
|---|---|
| Heading | Recent results |
| Columns | Run · Result · Agent |
| Row | *T-factory-0001 — Final test of SN-GX8-0100* · *10 of 10 steps done. Verdict: PASS (3 of 3 voters).* · Factory |
| Empty | No results yet. Finished tasks, runs and jobs appear here with their outcome. |

Finished times are not shown until the ticket service supplies them; a made-up time would
break §9's "human time" rule worse than no time.

## Pages that arrive later (one panel each, no dead end)

| Page | Sentence |
|---|---|
| Runs | Every ticket from the three agents will be listed here once the ticket service is connected (Phase 3). Until then, each agent's page lists its own work. |
| Models | Models are read from `Models/models.yaml`. This page arrives with the Models service (Phase 3); until then, edit the file on the host and run `slas model fit` before a load. |
| Skills | The skill library and its per-agent switches arrive with Phase 4 (ADR-0013). Skills already imported are offered by the New task, run and job wizards. |
