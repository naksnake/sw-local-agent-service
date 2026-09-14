import { useCallback, useEffect, useState } from "react";

import { PRODUCT_NAME, VERSION } from "./branding";
import type { CodingApi } from "./coding/api";
import { CodingPage } from "./coding/CodingPage";
import type { FactoryApi, StationsAdminApi } from "./factory/api";
import { FactoryPage } from "./factory/FactoryPage";
import { StationsAdmin } from "./factory/StationsAdmin";
import type { GitApi } from "./git/api";
import { GitHostsAdmin } from "./git/GitHostsAdmin";
import { GitRemotesSettings } from "./git/GitRemotesSettings";
import { type AgentPage, HomePage, healthSentence, type Snapshot } from "./home/HomePage";
import type { ValidationApi } from "./validation/api";
import { ValidationPage } from "./validation/ValidationPage";

// The shell from docs/ui-demo/slas-ui-demo.html (CLAUDE.md §9; copy in docs/ui/home.md):
// a left rail with the nine pages, a top bar with one health sentence, and the page.
// Until apps/api exists the pages run on the API fakes main.tsx passes in; a page whose
// API is absent is left out of the rail rather than shown empty.

interface Props {
  codingApi?: CodingApi;
  validationApi?: ValidationApi;
  factoryApi?: FactoryApi;
  stationsApi?: StationsAdminApi;
  gitApi?: GitApi;
  /** Who is signed in; shown in the top bar and recorded on approvals and decisions. */
  user?: string;
}

type AdminTab = "git-hosts" | "stations";

export type Page =
  | "home"
  | "coding"
  | "validation"
  | "factory"
  | "runs"
  | "models"
  | "skills"
  | "settings"
  | "admin";

const RAIL: [Page, string][] = [
  ["home", "Home"],
  ["coding", "Coding"],
  ["validation", "Validation"],
  ["factory", "Factory"],
  ["runs", "Runs"],
  ["models", "Models"],
  ["skills", "Skills"],
  ["settings", "Settings"],
  ["admin", "Admin"],
];

const LATER: Record<"runs" | "models" | "skills", { heading: string; lede: string; sentence: string }> = {
  runs: {
    heading: "Runs",
    lede: "Every ticket, across the three agents.",
    sentence:
      "Every ticket from the three agents will be listed here once the ticket service is connected (Phase 3). Until then, each agent's page lists its own work.",
  },
  models: {
    heading: "Models",
    lede: "Which model serves each role, and the cross-check voters.",
    sentence:
      "Models are read from Models/models.yaml. This page arrives with the Models service (Phase 3); until then, edit the file on the host and run `slas model fit` before a load.",
  },
  skills: {
    heading: "Skills",
    lede: "Reusable step-by-step recipes. Write one once, then turn it on for any agent.",
    sentence:
      "The skill library and its per-agent switches arrive with Phase 4 (ADR-0013). Skills already imported are offered by the New task, run and job wizards.",
  },
};

function clock(now: Date): string {
  return now.toLocaleTimeString(undefined, { hour12: false });
}

export function App({ codingApi, validationApi, factoryApi, stationsApi, gitApi, user = "you" }: Props) {
  const [page, setPage] = useState<Page>("home");
  const [openWizard, setOpenWizard] = useState<AgentPage | null>(null);
  const [adminTab, setAdminTab] = useState<AdminTab>(gitApi !== undefined ? "git-hosts" : "stations");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [whoNote, setWhoNote] = useState(false);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const adminAvailable = gitApi !== undefined || stationsApi !== undefined;
  const available: Record<Page, boolean> = {
    home: true,
    coding: codingApi !== undefined,
    validation: validationApi !== undefined,
    factory: factoryApi !== undefined,
    runs: true,
    models: true,
    skills: true,
    settings: gitApi !== undefined,
    admin: adminAvailable,
  };

  const go = (target: Page) => {
    setOpenWizard(null);
    setPage(target);
  };
  const openFromHome = useCallback((target: AgentPage, wizard: boolean) => {
    setOpenWizard(wizard ? target : null);
    setPage(target);
  }, []);
  const onSnapshot = useCallback((s: Snapshot) => setSnap(s), []);

  const health = healthSentence(snap);
  const dot = snap === null ? "idle" : health.startsWith("Everything") ? "" : "warn";

  return (
    <div className="app">
      <nav className="rail" aria-label="Pages">
        <div className="brand">
          <strong>{PRODUCT_NAME}</strong>
          <span>Self-hosted, no cloud</span>
        </div>
        {RAIL.filter(([key]) => available[key]).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className="rail-link"
            aria-current={page === key ? "page" : undefined}
            onClick={() => go(key)}
          >
            {label}
          </button>
        ))}
        <div className="spacer" />
        <div className="foot">
          <span>Version {VERSION}</span>
          <br />
          <span>Air-gapped mode is on</span>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <div className="health" role="status">
            <span className={"dot " + dot} aria-hidden="true" />
            {health}
          </div>
          <div className="row">
            <span className="pill mono" aria-label="Time">
              {clock(now)}
            </span>
            <button type="button" className="btn small ghost" onClick={() => setWhoNote((v) => !v)}>
              Signed in
            </button>
          </div>
        </header>
        {whoNote && (
          <p className="muted" style={{ padding: "8px 28px 0" }}>
            Signed in as {user}. Roles and permissions are managed under Admin → People.
          </p>
        )}

        <main className="view">
          {page === "home" && (
            <HomePage
              {...(codingApi !== undefined ? { codingApi } : {})}
              {...(validationApi !== undefined ? { validationApi } : {})}
              {...(factoryApi !== undefined ? { factoryApi } : {})}
              onOpen={openFromHome}
              onSnapshot={onSnapshot}
            />
          )}
          {page === "coding" && codingApi !== undefined && (
            <CodingPage
              api={codingApi}
              startWizardOpen={openWizard === "coding"}
              {...(gitApi !== undefined ? { gitApi } : {})}
            />
          )}
          {page === "validation" && validationApi !== undefined && (
            <ValidationPage api={validationApi} user={user} startWizardOpen={openWizard === "validation"} />
          )}
          {page === "factory" && factoryApi !== undefined && (
            <FactoryPage api={factoryApi} user={user} startWizardOpen={openWizard === "factory"} />
          )}
          {(page === "runs" || page === "models" || page === "skills") && (
            <div>
              <div className="page-head">
                <div>
                  <h1>{LATER[page].heading}</h1>
                  <p className="lede">{LATER[page].lede}</p>
                </div>
              </div>
              <section className="panel">
                <p className="sentence">{LATER[page].sentence}</p>
              </section>
            </div>
          )}
          {page === "settings" && gitApi !== undefined && <GitRemotesSettings api={gitApi} />}
          {page === "admin" && adminAvailable && (
            <div className="stack">
              {gitApi !== undefined && stationsApi !== undefined && (
                <nav aria-label="Admin sections" className="tabs">
                  <button
                    type="button"
                    className="tab"
                    aria-current={adminTab === "git-hosts" ? "page" : undefined}
                    onClick={() => setAdminTab("git-hosts")}
                  >
                    Git hosts
                  </button>
                  <button
                    type="button"
                    className="tab"
                    aria-current={adminTab === "stations" ? "page" : undefined}
                    onClick={() => setAdminTab("stations")}
                  >
                    Stations
                  </button>
                </nav>
              )}
              {adminTab === "git-hosts" && gitApi !== undefined && <GitHostsAdmin api={gitApi} />}
              {(adminTab === "stations" || gitApi === undefined) && stationsApi !== undefined && (
                <StationsAdmin api={stationsApi} />
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
