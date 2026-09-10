# tests/e2e

Playwright tests against a running installation (CLAUDE.md §11). In Phase 0 the suite holds
one skipped spec documenting the Phase 1 login check; `pnpm --filter slas-e2e list` proves
the configuration loads without a browser. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to the bundled
Chromium on air-gapped hosts; browsers are never downloaded (INV-1).
