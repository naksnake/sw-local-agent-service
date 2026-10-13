# Settings → Your account — copy (CLAUDE.md §9)

Every person signed in has this tab; Settings opens on it, next to Git remotes for those who
manage remotes. Four boxes, each with its own sentence after it saves. Component:
`apps/webui/src/session/AccountPage.tsx`; routes `PATCH /api/v1/me`, `POST /api/v1/me/password`,
`POST /api/v1/me/sessions/end-others` (docs/api-contract.md).

| Element | Text |
|---|---|
| Heading | Your account |
| Lede | How you appear on commits and tickets, and how you sign in. |
| Name | Your name — help: Shown on your commits, tickets and the top bar. · Save name |
| Saved | Saved. New commits and tickets show *Pat Lin-Chen*. |
| Sign-in | Sign-in name *pat* — It is also your folder, Coding/*pat*/, so it stays the same. Ask an administrator for a new account if you need another name. · Role *Engineer* with its sentence · Last sign-in |
| Password | Current password · New password (At least 12 characters. Longer beats complicated.) · Type it again · Change password |
| Changed | Your password was changed. Your other sign-ins have ended; this one stays. |
| Other sign-ins | Signed in on another computer and left it? End every other sign-in of yours; this one stays. · Sign out everywhere else |
| Ended | Signed out of *2* other sessions. This one stays signed in. · You were signed in only here; nothing else was signed out. |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Empty name | The name is empty. | Everything was deleted from the field. | Type the person's name as it should appear on their commits and tickets. |
| Long name | The name is too long. | A name is at most 120 characters. | Shorten it; it appears on commits and tickets. |
| Mismatch | The two passwords don't match. | A typo in one of them. | Type both again. |
| Wrong current password | the api's sentence | | |

Administrators rename someone else from Admin → People → Edit name (docs/ui/admin-people.md).
