# Admin → Stations — copy (CLAUDE.md §5.2, §9, §10.3)

The stations the Factory Agent may drive, their one-time enrolment codes, per-station tuning
and retention. Component: `apps/webui/src/factory/StationsAdmin.tsx`. Nothing on this page
shows a key or a certificate beyond its fingerprint.

| Element | Text |
|---|---|
| Heading | Stations |
| Lede | The test stations the Factory Agent may drive. A station joins by entering a one-time code you issue here; it then holds its own certificate and signing key, and the platform never sees its display — only screenshots and results. Changes apply as soon as you save; nothing is restarted here. |
| Empty | No station yet. Add one below, then issue its code. |
| Station row, enrolled | *station-07* · *Final test, line 2* — *station-07*: enrolled *2026-09-14 10:00*, runner at *https://…:8443*, certificate *SHA256:…* |
| Station row, not enrolled | *station-08*: not enrolled yet. |
| Tuning line | Windows matched by *contains*; *0* s settle after each action; wait timeouts ×*1*; at most *10* actions per second. Screenshots are kept *30* days (*180* days for failed or held jobs), at most *400* per job. The operator can watch and take over through VNC (port *5900*). · VNC is off: the operator cannot watch or take over this station. |
| Buttons | Issue code · Issue a new code (when enrolled) · Tune · Revoke (when enrolled) · Remove (when not enrolled) |
| Issued code | Enter this code on *station-07* within *15* minutes: *K7PM-4RQD-XN2H*. It works once; issuing a new code cancels it. |
| Revoked | *station-07* is no longer enrolled: its certificate and batch key are refused from now on. Issue a new code to enrol it again. |
| Added | *station-09* is added. Issue its code, then enter the code on the station. |
| Tuning box | Window matching and timing for this station's GUI. Tune against the real station with `slas-station-runner windows`; the skill itself does not change. — Window titles match by: contains (the recipe's title appears anywhere) / prefix (the real title starts with it) / exact (the whole title, case-insensitive) / regular expression · Settle after each action (s) · Wait timeouts × · Actions per second, at most — Screenshot retention on the platform and on this station. — Keep for (days) · Failed or held jobs (days) · At most per job — ☐ The operator can watch and take over through VNC → **Save** · Cancel |
| Saved | *station-08* saved. *tuning sentence* *retention sentence* The station picks the change up on its next enrolment or restart. |
| Add form | Adding a station creates its record; nothing reaches the station until you issue a code and someone enters it there. — Name · Description (optional) → **Add station** |

| Case | Text |
|---|---|
| Bad name | "*Station 09*" is not a station name. Names are lowercase letters, digits and dashes, starting with a letter, like station-09. Change the name and add the station again. |
| Duplicate | There is already a station called *station-07*. Every station has one record; pick another name or issue a code for the existing one. |

What the station itself says (the `slas-station-runner` CLI), in three parts:

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Wrong code | The code for *station-07* is not right. | *4 attempts* left before enrolment locks. | Check the code with the administrator who issued it and try again. |
| Locked | Enrolment of *station-07* is locked after *5* wrong codes. | Somebody entered wrong codes too often. | Issue a new code under Admin → Stations; that unlocks the station. |
| Expired | The enrolment code for *station-07* expired at *10:15*. | Codes are valid for a few minutes so a code written down cannot be used later. | Issue a new code under Admin → Stations. |
| Used | No enrolment code is open for *station-07*. | None was issued, or the last one was already used. | Issue a new code under Admin → Stations and enter it on the station. |
| Unknown station | There is no station called *station-99*. | It has not been added under Admin → Stations. | Add the station first, then issue its enrolment code. |

Issuing a code, revoking and tuning need `factory:stations_manage`. Enrolment is a
one-time, human-initiated act (a code read aloud from this page to a person at the station);
the platform never pushes credentials to a machine that did not ask with a valid code.
