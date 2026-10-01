# tests/e2e

Playwright end-to-end tests. Config lives in the repo root (`playwright.config.ts`), which
starts the WebUI dev server on loopback. Under `vite dev` the WebUI runs on its in-memory
fakes (`apps/webui/src/apis.fake.ts`; see `apis.ts` for the `VITE_SLAS_FAKE_API` switch), so
these journeys need no backend. The fake accounts are `admin@slas.local` with the one-time
password `admin-one-time-pw` and `pat@slas.local` / `pat-password-12345`.

Run with `pnpm e2e` (or `pnpm exec playwright test`).
