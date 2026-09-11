import { PRODUCT_NAME } from "./branding";

// Phase 0 shell. Sign-in, Home and the agent pages arrive with Phase 1 (CLAUDE.md §9).
export function App() {
  return (
    <main className="min-h-screen bg-slate-50 px-6 py-12 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{PRODUCT_NAME}</h1>
        <p className="mt-3 text-base leading-7 text-slate-700 dark:text-slate-300">
          The platform is being set up on this host. Sign-in and the agent pages arrive
          in the next phase; until then, nothing here needs your attention.
        </p>
      </div>
    </main>
  );
}
