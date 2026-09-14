# ADR-0009: WebUI stack additions, the sign-in front door, and how the UI is tested

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md §4.3 names TypeScript 5 strict, React 18, Vite, Tailwind, shadcn/ui, TanStack
Query/Table and react-hook-form + zod, but no router. §9 lists ten pages and rules for copy
and behaviour; sign-in is not a page but every page needs it. Phase 1 must prove "you can
log in" in a real browser on a fresh offline install (INV-10) and must keep the SPA free of
any off-origin request (INV-1).

## Decision
- **Dependencies added to `apps/webui`** (exact versions, ADR-0005 style table):
  react-router (client routing for `/sign-in`, `/choose-password`, `/`, `/admin/people`,
  `/admin/settings` and a catch-all — the one addition to §4.3), @tanstack/react-query,
  react-hook-form, zod and its resolver package, and the shadcn/ui primitives copied into
  `src/components/ui` (radix-ui, class-variance-authority, clsx, tailwind-merge,
  lucide-react). Nothing is fetched at runtime; the built `dist/` is grepped in CI for any
  `https?://` reference and for font or CDN hosts.
- **Sign-in is the front door.** Anonymous visitors land on the sign-in page, which shows
  the installation name from a public endpoint. A one-time password leads to "Choose a new
  password" before anything else. An expired session shows the configured lifetime in its
  sentence. An unknown address shows one sentence and a link Home. Admin → People and
  Admin → Settings are routes inside the Admin page, visible only with `admin:*`; others
  see one sentence, never a blank page.
- **§9 made mechanical.** All copy lives in `src/copy/en.ts` and is written first in
  `docs/ui/<page>.md` for review. There is exactly one error component, which renders what
  happened, likely cause and what to do inline and persistently; no toast component exists
  in the codebase. One dialog per action whose content advances from confirm to result;
  dialogs never stack. Every command name that appears in copy must be registered in the
  `slas` CLI (a unit test enforces it).
- **Testing strategy.** Component tests with vitest against an in-memory API client. A
  fake API (`tests/e2e/fake-api`, node:http, in-memory, same copy file as the real API)
  implements a route contract that a unit test compares against the real FastAPI route
  table, so Playwright journeys run without Docker and cannot drift. The same journey
  helpers run in the `deploy` CI project against the real offline stack over verified TLS
  (no `ignoreHTTPSErrors`), recording every request and failing on any off-origin URL.

## Alternatives considered
- TanStack Router: equally offline-friendly; react-router is the more widely known choice
  and either is one dependency — the choice is recorded here so it is not re-litigated.
- Stubbing routes with Playwright's `page.route`: quick, but the stubs drift from the API;
  a fake server with a contract test does not.
- A toast library for errors: rejected by §9 ("toasts for errors that need action").

## Consequences
Easier: e2e tests run anywhere; the sign-in and admin flows are proven on the real stack
in CI; copy review happens before code. Harder: copied shadcn components must compile under
TypeScript strict with Tailwind 4; the fake API is one more thing to keep in step, which is
why its contract is tested.

## Invariants touched
INV-1 (the SPA fetches nothing off-origin; proven in CI), INV-10 (the login page is what
the deploy job reaches and drives), §9 rules (sentences, one primary action, three-part
errors, no dead ends). CLAUDE.md §4.3 gains react-router once this ADR is accepted.
