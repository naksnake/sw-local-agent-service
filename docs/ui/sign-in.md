# Sign in, Choose a new password, Shell and Home — copy (CLAUDE.md §9)

Written before the code, for review. Every string below is the exact text the page shows.
Rules applied: sentences not codes; one primary action per screen; say what will happen
before it happens; progress for anything over 5 seconds; errors in three parts, inline and
persistent, never a toast; no dead ends.

## Sign in (`/sign-in`)

| Element | Text |
|---|---|
| Heading | *installation name*, e.g. **Lab 3** — falls back to **SW Local Agent Service** |
| Lede | Use the account an administrator created for you. Nothing you type leaves this network. |
| Field | Email |
| Field | Password |
| Primary button | Sign in |
| Button while waiting | Signing in… |
| After 5 s | Still checking… the server is slow to answer. |

**Loading the page** (before the installation name arrives): the heading shows the product
name; nothing is disabled.

**Errors** (inline, under the form, persistent until the next attempt):

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Wrong email or password | That email and password don't match. | A typo, or the password was changed. | Try again, or ask an administrator to reset your password. |
| Account switched off | This account is switched off. | An administrator switched it off. | Ask an administrator to switch it back on. |
| Too many attempts | Too many sign-in attempts in the last 15 minutes. | Several wrong passwords were tried for this account. | Wait and try again. |
| Server not answering | The sign-in server didn't answer. | The api service is starting or stopped. | Wait a moment and press Try again; if it repeats, run `slas logs api` on the host. |
| Rate limiter down | Sign-in is paused for a moment. | The service that counts sign-in attempts didn't answer. | Try again in a minute; if it repeats, run `slas logs redis` on the host. |

**Session ended** (shown on the sign-in page after a 401 with reason `expired`):
Your sign-in ended after *8 hours*. Sign in again to continue.

## Choose a new password (`/choose-password`, forced after a one-time password)

| Element | Text |
|---|---|
| Heading | Choose a new password |
| Lede | Your password was made for one use. Pick one only you know. |
| Field (only after a reload, when the sign-in password is no longer in memory) | Current password — help: The one-time password you signed in with. |
| Help under the first field | At least 12 characters. Longer beats complicated. |
| Field | New password |
| Field | Type it again |
| Primary button | Save and continue |
| Button while waiting | Saving… |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Mismatch | The two passwords don't match. | A typo in one of them. | Type both again. |
| Too short | The password is too short: it has *8* characters and needs at least 12. | | Add a few more words. |
| Same as email | The password can't be your email address. | | Choose something only you know. |
| Server not answering | The password wasn't saved. | The api service didn't answer. | Press Save and continue again; if it repeats, run `slas logs api` on the host. |

Reloading this page keeps the person here until a password is saved.

## Shell (signed in)

| Element | Text |
|---|---|
| Header | Signed in as *Pat Lin* · *Engineer* |
| Navigation | Home · Admin (only with an admin capability) |
| Action | Sign out |
| After sign out | You're signed out. (shown on the sign-in page) |
| Checking the session on load | Checking your sign-in… |

## Home (`/`)

Signed in as *Administrator*. Coding, Validation and Factory arrive in later phases; People
and Settings are under Admin.

For an engineer: Signed in as *Pat Lin*. You can operate screens, run commands on targets,
work with Git remotes and approve destructive steps. Coding, Validation and Factory arrive
in later phases.

The list of things a person can do is built from their capabilities, in sentences, never as
a list of capability names.

## Not allowed (`/admin/*` without the capability)

This part is for administrators. Ask an administrator if you need something changed here.
Go to Home.

## Unknown address

There is nothing at this address. Go to Home.
