import { useState } from "react";

import { PRODUCT_NAME } from "./branding";
import type { CodingApi } from "./coding/api";
import { CodingPage } from "./coding/CodingPage";

// The shell plus the Coding page (CLAUDE.md §9). Sign-in, Home and the other pages arrive
// with their phases; until apps/api exists the Coding page runs on the API fake main.tsx
// passes in, and the shell says so.

interface Props {
  codingApi?: CodingApi;
}

type Page = "home" | "coding";

export function App({ codingApi }: Props) {
  const [page, setPage] = useState<Page>("home");
  const navItem = (target: Page, label: string) => (
    <button
      type="button"
      className={
        "rounded-md px-3 py-1.5 text-sm " +
        (page === target
          ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
          : "text-slate-700 hover:bg-slate-200 dark:text-slate-300 dark:hover:bg-slate-800")
      }
      aria-current={page === target ? "page" : undefined}
      onClick={() => setPage(target)}
    >
      {label}
    </button>
  );

  return (
    <main className="min-h-screen bg-slate-50 px-6 py-8 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="mx-auto max-w-4xl space-y-8">
        <nav aria-label="Pages" className="flex items-center gap-2">
          <span className="mr-4 text-sm font-semibold tracking-tight">{PRODUCT_NAME}</span>
          {navItem("home", "Home")}
          {codingApi !== undefined && navItem("coding", "Coding")}
        </nav>

        {page === "home" && (
          <section>
            <h1 className="text-2xl font-semibold tracking-tight">{PRODUCT_NAME}</h1>
            <p className="mt-3 text-base leading-7 text-slate-700 dark:text-slate-300">
              The platform is being set up on this host. Sign-in and the agent pages arrive in
              their phases; until then, nothing here needs your attention.
            </p>
          </section>
        )}
        {page === "coding" && codingApi !== undefined && <CodingPage api={codingApi} />}
      </div>
    </main>
  );
}
