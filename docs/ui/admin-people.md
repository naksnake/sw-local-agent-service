# Admin → People — copy (CLAUDE.md §9)

Who can sign in and what each person may do. One primary action (Add person); every other
action lives in the row menu. One dialog per action, whose content advances from
confirmation to result; dialogs never stack. Errors render inline in the dialog or above
the table, in three parts, never as a toast.

## Page

| Element | Text |
|---|---|
| Heading | People |
| Lede | Who can sign in, and what each person may do. |
| Primary button | Add person |
| Table columns | Person · Role · Can do · Last sign-in · Status |
| Can do | one sentence from the role, e.g. *Runs coding tasks, validation runs and factory jobs, and approves destructive steps.* |
| Last sign-in | human time: *3 minutes ago*, *yesterday*, *Never* |
| Status | Can sign in · Switched off · Must choose a password |
| Row menu | Change role · Reset password · Switch off (or Switch on) |

**States**

| State | Text |
|---|---|
| Loading | Loading people… |
| Only you | Only you so far. Add the engineers who will use the platform; each gets a one-time password. |
| Failed to load | The list of people didn't load. / The api service didn't answer. / Press Try again; if it repeats, run `slas logs api` on the host. |
| Not allowed | This part is for administrators. Ask an administrator if you need something changed here. Go to Home. |

## Add person (dialog)

| Element | Text |
|---|---|
| Title | Add a person |
| Field | Name — help: Shown on their commits and tickets. |
| Field | Email — help: They sign in with it. It doesn't have to reach the internet. |
| Field | Role — a choice list of role labels with the role's sentence under each |
| Closing sentence | *Ana* will get a one-time password and choose her own at first sign-in. |
| Primary button | Add *Ana* |
| Result panel title | *Ana* can sign in now |
| Result panel body | Give *Ana* this one-time password. It works once; she chooses her own at first sign-in. You won't see it again. |
| Result panel action | Copy · Done |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Duplicate email | Someone already signs in as *ana@company.local*. | The address belongs to an existing person, maybe switched off. | Use another address, or switch the existing account back on. |
| Invalid email | That doesn't look like an email address. | A missing @ or domain. | Check it and try again. |
| Server not answering | *Ana* wasn't added. | The api service didn't answer. | Press Add again; if it repeats, run `slas logs api` on the host. |

## Change role (dialog)

Title: Change *Ana*'s role. Body: a choice list of role labels with each role's sentence.
Closing sentence: *Ana* becomes a *Line lead* on her next request; nothing else changes.
Primary button: Change role.

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Last administrator | You can't change the last administrator's role. | Without an administrator nobody could manage people or settings. | Make someone else an administrator first. |

## Reset password (dialog)

Title: Reset *Ana*'s password. Body: *Ana*'s current password stops working, she is signed
out everywhere and gets a new one-time password. Primary button: Reset password. The same
dialog then advances to the result panel from Add person.

## Switch off / Switch on (dialog)

Switch off — Title: Switch off *Ana*'s account. Body: *Ana* can't sign in until switched
back on, and is signed out everywhere now. Her history stays. Primary button: Switch off.

Switch on — Title: Switch on *Ana*'s account. Body: *Ana* can sign in again with her current
password. Primary button: Switch on.

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Yourself | You can't switch off your own account. | You're signed in with it. | Ask another administrator. |
| Last administrator | You can't switch off the last administrator. | Without an administrator nobody could manage people or settings. | Make someone else an administrator first. |

## Never shown

A one-time password appears once, in the result panel, and never again: not in the table,
not in any list response, not in a log, not in the audit trail.
