# Admin → Settings — copy (CLAUDE.md §9, ADR-0008)

Two blocks: what you can change now, and what was set at install. One primary action
(Save changes). Nothing on this page needs a restart, and the page says so.

## Page

| Element | Text |
|---|---|
| Heading | Settings |
| Lede | Changes apply as soon as you save. Nothing is restarted. |

**States**

| State | Text |
|---|---|
| Loading | Loading settings… |
| Failed to load | The settings didn't load. / The api service didn't answer. / Press Try again; if it repeats, run `slas logs api` on the host. |
| Not allowed | This part is for administrators. Ask an administrator if you need something changed here. Go to Home. |

## You can change these now

| Setting | Label | Help | Choices |
|---|---|---|---|
| installation_name | Name shown on the sign-in page | Helps people tell installations apart, for example Lab 3. | free text, up to 60 characters |
| chinese_variant | Chinese used in reports and SOPs | English is always produced alongside. | Traditional (繁體) — default · Simplified (简体) |
| session_lifetime_hours | How long a sign-in lasts | Applies to new sign-ins; nobody is signed out. | 8 hours · 1 day · 7 days |

| Element | Text |
|---|---|
| Primary button (disabled until something changed) | Save changes |
| Button while waiting | Saving… |
| Saved | Saved at *14:02*. It applies now; nothing restarted. |
| Saved, mirror failed | Saved, but the copy in `.env` couldn't be written. / The data root isn't writable by the api service. / The settings apply now; fix permissions on */AI/Agent/.env* so they survive a reinstall. |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Name too long | The name is too long: it has *90* characters, the limit is 60. | | Shorten it. |
| Not saved | The settings weren't saved. | The database didn't answer. | Try again in a moment; if it repeats, run `slas logs api` on the host. |

## Set at install

Shown as plain text, never as inputs, even if the API describes them.

| Fact | Example |
|---|---|
| Where data is stored | /AI/Agent |
| Web port | 443 |
| Addresses on the certificate | 127.0.0.1, localhost, lab3.internal, 10.20.0.15 |
| TLS certificate | self-signed by this installation |
| Profile | quickstart |
| Version | 1.0.0 |

Footer sentence: To change these, edit `.env` on the host and run `./install.sh` again;
only the affected service is restarted.
