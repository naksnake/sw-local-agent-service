import { useState } from "react";

import { PRODUCT_NAME } from "./branding";
import type { CodingApi } from "./coding/api";
import { CodingPage } from "./coding/CodingPage";
import type { FactoryApi, StationsAdminApi } from "./factory/api";
import { FactoryPage } from "./factory/FactoryPage";
import { StationsAdmin } from "./factory/StationsAdmin";
import type { GitApi } from "./git/api";
import { GitHostsAdmin } from "./git/GitHostsAdmin";
import { GitRemotesSettings } from "./git/GitRemotesSettings";
import type { ValidationApi } from "./validation/api";
import { ValidationPage } from "./validation/ValidationPage";

// The shell plus the pages built so far (CLAUDE.md §9): Coding (with the per-project Git
// panel and Terminal), Validation (LED cycle map, console, findings), Factory (test-step
// map, screenshot strip, watch and take over, line-lead decision), Settings → Git
// remotes, Admin → Git hosts, Admin → Stations. Sign-in and the other pages arrive with their phases; until
// apps/api exists the pages run on the API fakes main.tsx passes in, and the Home page says so.

interface Props {
  codingApi?: CodingApi;
  validationApi?: ValidationApi;
  factoryApi?: FactoryApi;
  stationsApi?: StationsAdminApi;
  gitApi?: GitApi;
}

type AdminTab = "git-hosts" | "stations";

type Page = "home" | "coding" | "validation" | "factory" | "settings" | "admin";

export function App({ codingApi, validationApi, factoryApi, stationsApi, gitApi }: Props) {
  const [page, setPage] = useState<Page>("home");
  const [adminTab, setAdminTab] = useState<AdminTab>(gitApi !== undefined ? "git-hosts" : "stations");
  const adminAvailable = gitApi !== undefined || stationsApi !== undefined;
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
          {factoryApi !== undefined && navItem("factory", "Factory")}
          {gitApi !== undefined && navItem("settings", "Settings")}
          {adminAvailable && navItem("admin", "Admin")}
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
        {page === "factory" && factoryApi !== undefined && <FactoryPage api={factoryApi} />}
        {page === "settings" && gitApi !== undefined && <GitRemotesSettings api={gitApi} />}
        {page === "admin" && adminAvailable && (
          <div className="space-y-6">
            {gitApi !== undefined && stationsApi !== undefined && (
              <nav aria-label="Admin sections" className="flex gap-2 border-b border-slate-200 pb-2 text-sm dark:border-slate-800">
                <button type="button" className={adminTab === "git-hosts" ? "font-semibold underline" : ""} aria-current={adminTab === "git-hosts" ? "page" : undefined} onClick={() => setAdminTab("git-hosts")}>
                  Git hosts
                </button>
                <button type="button" className={adminTab === "stations" ? "font-semibold underline" : ""} aria-current={adminTab === "stations" ? "page" : undefined} onClick={() => setAdminTab("stations")}>
                  Stations
                </button>
              </nav>
            )}
            {adminTab === "git-hosts" && gitApi !== undefined && <GitHostsAdmin api={gitApi} />}
            {(adminTab === "stations" || gitApi === undefined) && stationsApi !== undefined && <StationsAdmin api={stationsApi} />}
          </div>
        )}
      </div>
    </main>
  );
}
