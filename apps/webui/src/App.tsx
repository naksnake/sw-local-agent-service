import { useCallback, useEffect, useState } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import type { PeopleApi, SettingsApi } from "./admin/api";
import { PeoplePage } from "./admin/PeoplePage";
import { SettingsPage } from "./admin/SettingsPage";
import { PRODUCT_NAME, VERSION } from "./branding";
import type { CodingApi } from "./coding/api";
import { CodingPage } from "./coding/CodingPage";
import { homeWelcome, later, notAllowed, shell as copy, unknownAddress } from "./copy/en";
import type { FactoryApi, StationsAdminApi } from "./factory/api";
import { FactoryPage } from "./factory/FactoryPage";
import { StationsAdmin } from "./factory/StationsAdmin";
import type { GitApi } from "./git/api";
import { GitHostsAdmin } from "./git/GitHostsAdmin";
import { GitRemotesSettings } from "./git/GitRemotesSettings";
import type { HomeListsApi } from "./home/api";
import { type AgentPage, HomePage, healthSentence, type Snapshot } from "./home/HomePage";
import type { ModelsApi } from "./models/api";
import { ModelsPage } from "./models/ModelsPage";
import type { Person, SessionApi } from "./session/api";
import { ChoosePasswordPage } from "./session/ChoosePasswordPage";
import { SignInPage } from "./session/SignInPage";
import { type Session, SessionProvider, useSession } from "./session/store";
import type { ValidationApi } from "./validation/api";
import { ValidationPage } from "./validation/ValidationPage";

// The shell from docs/ui-demo/slas-ui-demo.html (CLAUDE.md §9; copy in docs/ui/home.md and
// docs/ui/sign-in.md): a left rail, a top bar with one health sentence, and the page. Routes
// follow ADR-0009: /sign-in and /choose-password stand alone; everything else lives inside
// the shell behind the session gate. A page whose API is absent is left out of the rail and
// answers "There is nothing at this address" rather than showing an empty page. Without a
// SessionApi the shell runs ungated — component tests only; main.tsx always passes one.

export interface AppProps {
  sessionApi?: SessionApi;
  homeListsApi?: HomeListsApi;
  peopleApi?: PeopleApi;
  settingsApi?: SettingsApi;
  modelsApi?: ModelsApi;
  codingApi?: CodingApi;
  validationApi?: ValidationApi;
  factoryApi?: FactoryApi;
  stationsApi?: StationsAdminApi;
  gitApi?: GitApi;
  /** Who is signed in when there is no SessionApi (tests); shown in the top bar. */
  user?: string;
  /** A dev-only line under the sign-in form naming the fake accounts. */
  signInHint?: string;
}

export function App(props: AppProps) {
  if (props.sessionApi !== undefined) {
    return (
      <SessionProvider api={props.sessionApi}>
        <Routed {...props} />
      </SessionProvider>
    );
  }
  return <Routed {...props} />;
}

type AdminTab = "people" | "settings" | "git-hosts" | "stations";

const RAIL: [path: string, label: string][] = [
  ["/", copy.rail.home],
  ["/coding", copy.rail.coding],
  ["/validation", copy.rail.validation],
  ["/factory", copy.rail.factory],
  ["/runs", copy.rail.runs],
  ["/models", copy.rail.models],
  ["/skills", copy.rail.skills],
  ["/settings", copy.rail.settings],
  ["/admin", copy.rail.admin],
];

const TAB_LABEL: Record<AdminTab, string> = {
  people: copy.adminTabs.people,
  settings: copy.adminTabs.settings,
  "git-hosts": copy.adminTabs.gitHosts,
  stations: copy.adminTabs.stations,
};

function Routed(props: AppProps) {
  const { homeListsApi, peopleApi, settingsApi, modelsApi, codingApi, validationApi, factoryApi, stationsApi, gitApi } = props;
  const session = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const onSnapshot = useCallback((s: Snapshot) => setSnap(s), []);

  if (session !== null && session.state.status === "checking") {
    return (
      <main className="auth">
        <p role="status" className="sentence">
          {copy.checking}
        </p>
      </main>
    );
  }

  const person: Person | null = session !== null && session.state.status === "signed-in" ? session.state.person : null;
  const anonymous = session !== null && session.state.status === "anonymous";
  const mustChange = person?.must_change_password === true;
  const can = (capability: string) => session === null || session.hasCapability(capability);
  // Which agents this installation starts (ADR-0017): the api says; until it has answered,
  // or without a SessionApi (tests), every agent counts as on.
  const agents = session?.installation?.agents;
  const agentOn = (agent: "coding" | "validation" | "factory") => agents === undefined || agents.includes(agent);

  const tabs: AdminTab[] = [];
  if (peopleApi !== undefined && can("admin:people")) tabs.push("people");
  if (settingsApi !== undefined && can("admin:settings")) tabs.push("settings");
  if (gitApi !== undefined && can("git:hosts_manage")) tabs.push("git-hosts");
  if (stationsApi !== undefined && agentOn("factory") && can("factory:stations_manage")) tabs.push("stations");

  const available: Record<string, boolean> = {
    "/": true,
    "/coding": codingApi !== undefined && agentOn("coding"),
    "/validation": validationApi !== undefined && agentOn("validation"),
    "/factory": factoryApi !== undefined && agentOn("factory"),
    "/runs": true,
    "/models": true,
    "/skills": true,
    "/settings": gitApi !== undefined && can("git:remote_manage"),
    "/admin": tabs.length > 0,
  };

  const wizard = (location.state as { wizard?: boolean } | null)?.wizard === true;
  const openFromHome = (page: AgentPage, openWizard: boolean) => navigate(`/${page}`, { state: { wizard: openWizard } });
  const agentsPresent = codingApi !== undefined || validationApi !== undefined || factoryApi !== undefined;
  const welcome = person === null ? undefined : homeWelcome(person.display_name, person.capabilities, { agentsPresent });
  const userName = person?.display_name ?? props.user ?? "you";

  /** A page that needs a capability: the not-allowed sentence instead of a blank page. */
  const guarded = (capability: string, element: React.ReactNode) => (can(capability) ? element : <NotAllowed />);

  return (
    <Routes>
      <Route
        path="/sign-in"
        element={
          session === null ? (
            <Navigate to="/" replace />
          ) : anonymous ? (
            <SignInPage session={session} {...(props.signInHint !== undefined ? { hint: props.signInHint } : {})} />
          ) : (
            <Navigate to={mustChange ? "/choose-password" : "/"} replace />
          )
        }
      />
      <Route
        path="/choose-password"
        element={
          session === null ? (
            <Navigate to="/" replace />
          ) : person === null ? (
            <Navigate to="/sign-in" replace />
          ) : !mustChange ? (
            <Navigate to="/" replace />
          ) : (
            <ChoosePasswordPage session={session} person={person} />
          )
        }
      />
      <Route
        element={
          anonymous ? (
            <Navigate to="/sign-in" replace />
          ) : mustChange ? (
            <Navigate to="/choose-password" replace />
          ) : (
            <Shell rail={RAIL.filter(([path]) => available[path])} snap={snap} person={person} userName={userName} session={session} />
          )
        }
      >
        <Route
          index
          element={
            <HomePage
              {...(homeListsApi !== undefined ? { lists: homeListsApi } : {})}
              {...(codingApi !== undefined && agentOn("coding") ? { codingApi } : {})}
              {...(validationApi !== undefined && agentOn("validation") ? { validationApi } : {})}
              {...(factoryApi !== undefined && agentOn("factory") ? { factoryApi } : {})}
              {...(welcome !== undefined ? { welcome } : {})}
              onOpen={openFromHome}
              onSnapshot={onSnapshot}
            />
          }
        />
        <Route
          path="coding"
          element={
            codingApi !== undefined ? (
              <CodingPage api={codingApi} startWizardOpen={wizard} {...(gitApi !== undefined ? { gitApi } : {})} />
            ) : (
              <UnknownAddress />
            )
          }
        />
        <Route
          path="validation"
          element={
            validationApi !== undefined ? (
              <ValidationPage api={validationApi} user={userName} startWizardOpen={wizard} />
            ) : (
              <UnknownAddress />
            )
          }
        />
        <Route
          path="factory"
          element={
            factoryApi !== undefined ? <FactoryPage api={factoryApi} user={userName} startWizardOpen={wizard} /> : <UnknownAddress />
          }
        />
        <Route path="runs" element={<Later page="runs" />} />
        <Route path="models" element={modelsApi !== undefined ? <ModelsPage api={modelsApi} /> : <Later page="models" />} />
        <Route path="skills" element={<Later page="skills" />} />
        <Route
          path="settings"
          element={gitApi !== undefined ? guarded("git:remote_manage", <GitRemotesSettings api={gitApi} />) : <UnknownAddress />}
        />
        <Route path="admin" element={<AdminSection tabs={tabs} />}>
          <Route index element={tabs[0] !== undefined ? <Navigate to={tabs[0]} replace /> : <NotAllowed />} />
          <Route
            path="people"
            element={peopleApi !== undefined ? guarded("admin:people", <PeoplePage api={peopleApi} me={person} />) : <UnknownAddress />}
          />
          <Route
            path="settings"
            element={settingsApi !== undefined ? guarded("admin:settings", <SettingsPage api={settingsApi} />) : <UnknownAddress />}
          />
          <Route
            path="git-hosts"
            element={gitApi !== undefined ? guarded("git:hosts_manage", <GitHostsAdmin api={gitApi} />) : <UnknownAddress />}
          />
          <Route
            path="stations"
            element={
              stationsApi !== undefined ? guarded("factory:stations_manage", <StationsAdmin api={stationsApi} />) : <UnknownAddress />
            }
          />
          <Route path="*" element={<UnknownAddress />} />
        </Route>
        <Route path="*" element={<UnknownAddress />} />
      </Route>
    </Routes>
  );
}

// --- shell -------------------------------------------------------------------------------------

function clock(now: Date): string {
  return now.toLocaleTimeString(undefined, { hour12: false });
}

interface ShellProps {
  rail: [path: string, label: string][];
  snap: Snapshot | null;
  person: Person | null;
  userName: string;
  session: Session | null;
}

function Shell({ rail, snap, person, userName, session }: ShellProps) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const health = healthSentence(snap);
  const dot = snap === null ? "idle" : health.startsWith("Everything") ? "" : "warn";
  const current = (path: string) => (path === "/" ? pathname === "/" : pathname === path || pathname.startsWith(`${path}/`));

  return (
    <div className="app">
      <nav className="rail" aria-label="Pages">
        <div className="brand">
          <strong>{PRODUCT_NAME}</strong>
          <span>{copy.brandLine}</span>
        </div>
        {rail.map(([path, label]) => (
          <button
            key={path}
            type="button"
            className="rail-link"
            aria-current={current(path) ? "page" : undefined}
            onClick={() => navigate(path)}
          >
            {label}
          </button>
        ))}
        <div className="spacer" />
        <div className="foot">
          <span>{copy.version(VERSION)}</span>
          <br />
          <span>{copy.airGapped}</span>
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
            {person !== null && session !== null ? (
              <>
                <span className="muted" data-testid="signed-in-as">
                  {copy.signedInAs(person.display_name, person.role_label)}
                </span>
                <button type="button" className="btn small ghost" onClick={() => void session.signOut()}>
                  {copy.signOut}
                </button>
              </>
            ) : (
              <span className="muted" data-testid="signed-in-as">
                {copy.signedInAsPlain(userName)}
              </span>
            )}
          </div>
        </header>

        <main className="view">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function AdminSection({ tabs }: { tabs: AdminTab[] }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  return (
    <div className="stack">
      {tabs.length > 1 && (
        <nav aria-label="Admin sections" className="tabs">
          {tabs.map((tab) => (
            <button
              key={tab}
              type="button"
              className="tab"
              aria-current={pathname === `/admin/${tab}` ? "page" : undefined}
              onClick={() => navigate(`/admin/${tab}`)}
            >
              {TAB_LABEL[tab]}
            </button>
          ))}
        </nav>
      )}
      <Outlet />
    </div>
  );
}

function Later({ page }: { page: "runs" | "models" | "skills" }) {
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{later[page].heading}</h1>
          <p className="lede">{later[page].lede}</p>
        </div>
      </div>
      <section className="panel">
        <p className="sentence">{later[page].sentence}</p>
      </section>
    </div>
  );
}

function NotAllowed() {
  return (
    <section className="panel" aria-label="Not allowed">
      <p className="sentence">{notAllowed.sentence}</p>
      <p>
        <Link to="/">{notAllowed.link}</Link>
      </p>
    </section>
  );
}

function UnknownAddress() {
  return (
    <section className="panel" aria-label="Unknown address">
      <p className="sentence">{unknownAddress.sentence}</p>
      <p>
        <Link to="/">{unknownAddress.link}</Link>
      </p>
    </section>
  );
}
