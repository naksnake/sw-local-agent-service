# apps/webui

The web interface (CLAUDE.md §9): TypeScript 5 strict, React 18, Vite, Tailwind, shadcn/ui.
Phase 0 ships the build, typecheck and unit-test setup and a placeholder page; the login page
arrives in Phase 1.

```sh
pnpm install --frozen-lockfile
pnpm --filter slas-webui typecheck
pnpm --filter slas-webui test
pnpm --filter slas-webui dev
```

Component tests need a DOM library that CLAUDE.md does not name yet; ask before adding one
(§0.3). Until then, tests cover pure modules only.
