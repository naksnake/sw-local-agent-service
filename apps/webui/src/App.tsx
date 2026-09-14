import { useState } from "react";

import { PRODUCT_NAME } from "./branding";
import type { CodingApi } from "./coding/api";
import { CodingPage } from "./coding/CodingPage";
import type { GitApi } from "./git/api";
import { GitHostsAdmin } from "./git/GitHostsAdmin";
import { GitRemotesSettings } from "./git/GitRemotesSettings";
import type { ValidationApi } from "./validation/api";
import { ValidationPage } from "./validation/ValidationPage";

// The shell plus the pages built so far (CLAUDE.md §9): Coding (with the per-project Git
// panel and Terminal), Validation (LED cycle map, console, findings), Settings → Git
// remotes, Admin → Git hosts. Sign-in and the other pages arrive with their phases; until
// apps/api exists the pages run on the API fakes main.tsx passes in, and the Home page says so.

interface Props {
  codingApi?: CodingApi;
  validationApi?: ValidationApi;
  gitApi?: GitApi;
}

type Page = "home" | "coding" | "validation" | "settings" | "admin";

export function App({ codingApi, validationApi, gitApi }: Props) {
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
        <nav aria-label="Pages" className="flex flex-wrap items-center gap-2">
          <span className="mr-4 text-sm font-semibold tracking-tight">{PRODUCT_NAME}</span>
          {navItem("home", "Home")}
          {codingApi !== undefined && navItem("coding", "Coding")}
          {validationApi !== undefined && navItem("validation", "Validation")}
          {gitApi !== undefined && navItem("settings", "Settings")}
          {gitApi !== undefined && navItem("admin", "Admin")}
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
        {page === "coding" && codingApi !== undefined && (
          <CodingPage api={codingApi} {...(gitApi !== undefined ? { gitApi } : {})} />
        )}
        {page === "validation" && validationApi !== undefined && (
          <ValidationPage api={validationApi} />
        )}
        {page === "settings" && gitApi !== undefined && <GitRemotesSettings api={gitApi} />}
        {page === "admin" && gitApi !== undefined && <GitHostsAdmin api={gitApi} />}
      </div>
    </main>
  );
}
