# docs/ui

UI copy and screen specifications following CLAUDE.md §9. Copy is written here first and
reviewed before a page is built; the page then imports it from `apps/webui/src/copy/en.ts`
(sign-in, shell, Home, Admin → People, Admin → Settings, Models) or, for the agent pages built
earlier, from the constants next to the page.

| Document | Screens | Component | Phase |
|---|---|---|---|
| [`home.md`](home.md) | Shell (rail, health bar), Home dashboard (needs you, running now, recent results), the placeholders for Runs and Skills | `src/App.tsx`, `src/home/HomePage.tsx` | UI follows the demo |
| [`sign-in.md`](sign-in.md) | Sign in, Choose a new password, Shell (signed in as, sign out, checking), Home welcome sentence, Not allowed, Unknown address | `src/session/SignInPage.tsx`, `src/session/ChoosePasswordPage.tsx`, `src/App.tsx` | P1 |
| [`admin-people.md`](admin-people.md) | Admin → People and its dialogs | `src/admin/PeoplePage.tsx` | P1 |
| [`admin-settings.md`](admin-settings.md) | Admin → Settings | `src/admin/SettingsPage.tsx` | P1 |
| [`models.md`](models.md) | Models (registry, cards, roles, voters; read-only) | `src/models/ModelsPage.tsx` | P1 (read) · P3 (swap) |
| [`coding.md`](coding.md), [`new-coding-task.md`](new-coding-task.md) | Coding page, New coding task wizard | `src/coding/` | P6 |
| [`git-remotes.md`](git-remotes.md), [`git-hosts.md`](git-hosts.md), [`git-panel.md`](git-panel.md) | Settings → Git remotes, Admin → Git hosts, the per-project Git panel and Terminal | `src/git/` | P6 |
| [`validation.md`](validation.md), [`new-validation-run.md`](new-validation-run.md) | Validation page (LED cycle map, findings, console), New validation run wizard | `src/validation/` | P7 |
| [`factory.md`](factory.md), [`new-factory-job.md`](new-factory-job.md) | Factory page (test-step map, screenshot strip, watch and take over, line-lead decision), New factory job wizard | `src/factory/` | P9, P10 |
| [`admin-stations.md`](admin-stations.md) | Admin → Stations (records, one-time enrolment codes, tuning, retention, VNC) and the station CLI's sentences | `src/factory/StationsAdmin.tsx` | P10 |

Routes (ADR-0009): `/sign-in`, `/choose-password`, `/` and the rail pages, `/admin/people`,
`/admin/settings`, `/admin/git-hosts`, `/admin/stations`, and a catch-all. Errors everywhere
use the one `ThreePartError` component (`src/components/ThreePartError.tsx`); there is no toast,
and a unit test keeps it that way.
