// The in-memory session fake and the world it shares with the other fakes (People, Settings).
// It speaks the sentences from docs/ui/sign-in.md so the pages are tested with the words the
// api sends, and it calls `signalUnauthorized` on a 401 exactly as the HTTP client does.
// Loaded only by apis.fake.ts and the tests; never part of a production bundle.

import { ApiError, signalUnauthorized } from "../api/http";
import { VERSION } from "../branding";
import { signIn as signInCopy, type ThreePart } from "../copy/en";
import type { AgentName, Installation, Person, SessionApi } from "./api";

export interface FakeAccount extends Person {
  password: string;
}

const CAPABILITIES: Record<string, string[]> = {
  administrator: [
    "screen",
    "ssh",
    "redfish",
    "files",
    "network",
    "git:remote_manage",
    "git:clone",
    "git:pull",
    "git:push_branch",
    "git:bundle",
    "git:terminal",
    "git:hosts_manage",
    "admin:people",
    "admin:settings",
    "approve:destructive",
    "factory:verdict",
    "factory:control",
    "factory:stations_manage",
    "model:manage",
  ],
  engineer: [
    "screen",
    "ssh",
    "redfish",
    "files",
    "network",
    "git:remote_manage",
    "git:clone",
    "git:pull",
    "git:push_branch",
    "git:bundle",
    "git:terminal",
    "approve:destructive",
    "factory:control",
  ],
  line_lead: [
    "screen",
    "ssh",
    "redfish",
    "files",
    "network",
    "git:remote_manage",
    "git:clone",
    "git:pull",
    "git:push_branch",
    "git:bundle",
    "git:terminal",
    "approve:destructive",
    "factory:control",
    "factory:verdict",
  ],
  viewer: [],
};

export function capabilitiesOf(role: string): string[] {
  return [...(CAPABILITIES[role] ?? [])];
}

/** Throw as the api would: a three-part body with a status, signalling a 401 like the client. */
export function fail(status: number, parts: ThreePart, reason?: "expired" | "none"): never {
  if (status === 401) {
    signalUnauthorized(reason ?? "none");
  }
  throw new ApiError({ status, parts, reason: reason ?? null });
}

/** Throw as the client does when the api did not answer at all. */
export function unreachable(): never {
  throw new ApiError({
    status: 0,
    parts: { whatHappened: "The server didn't answer.", likelyCause: "", whatToDo: "" },
    unreachable: true,
  });
}

/** The people and settings the fakes share, so a person added under Admin can sign in. */
export class FakeWorld {
  accounts: FakeAccount[] = [];
  installationName = "Lab 3";
  /** The agents the fake installation starts (ADR-0017); the dev build shows every page. */
  agents: AgentName[] = ["coding", "validation", "factory"];
  sessionLifetimeHours = 8;
  private counter = 0;

  constructor() {
    this.addAccount("admin@slas.local", "Administrator", "administrator", "admin-one-time-pw", {
      mustChange: true,
    });
    this.addAccount("pat@slas.local", "Pat Lin", "engineer", "pat-password-12345", {
      lastSignIn: new Date(Date.now() - 3 * 60_000).toISOString(),
    });
  }

  addAccount(
    email: string,
    displayName: string,
    role: string,
    password: string,
    options: { mustChange?: boolean; active?: boolean; lastSignIn?: string | null } = {},
  ): FakeAccount {
    this.counter += 1;
    const account: FakeAccount = {
      id: `person-${String(this.counter).padStart(4, "0")}`,
      email,
      display_name: displayName,
      role,
      role_label: roleLabel(role),
      capabilities: capabilitiesOf(role),
      must_change_password: options.mustChange ?? false,
      is_active: options.active ?? true,
      last_sign_in_at: options.lastSignIn ?? null,
      password,
    };
    this.accounts.push(account);
    return account;
  }

  byEmail(email: string): FakeAccount | undefined {
    return this.accounts.find((a) => a.email.toLowerCase() === email.trim().toLowerCase());
  }

  byId(id: string): FakeAccount | undefined {
    return this.accounts.find((a) => a.id === id);
  }

  nextOneTimePassword(): string {
    this.counter += 1;
    const words = ["brisk", "copper", "lantern", "quiet", "harbor", "maple", "signal", "velvet"];
    const pick = (n: number) => words[(this.counter * 7 + n * 3) % words.length] ?? "quiet";
    return `${pick(1)}-${pick(2)}-${pick(3)}-${String((this.counter * 13) % 100).padStart(2, "0")}`;
  }

  /** Everything worth keeping across a page reload in development. */
  toJSON(): unknown {
    return {
      accounts: this.accounts,
      installationName: this.installationName,
      sessionLifetimeHours: this.sessionLifetimeHours,
      counter: this.counter,
    };
  }

  /** Restore what `toJSON` saved; anything malformed is ignored and the defaults stay. */
  restore(saved: unknown): void {
    if (typeof saved !== "object" || saved === null) {
      return;
    }
    const data = saved as Partial<{ accounts: FakeAccount[]; installationName: string; sessionLifetimeHours: number; counter: number }>;
    if (Array.isArray(data.accounts)) {
      this.accounts = data.accounts;
    }
    if (typeof data.installationName === "string") {
      this.installationName = data.installationName;
    }
    if (typeof data.sessionLifetimeHours === "number") {
      this.sessionLifetimeHours = data.sessionLifetimeHours;
    }
    if (typeof data.counter === "number") {
      this.counter = data.counter;
    }
  }
}

function roleLabel(role: string): string {
  return { administrator: "Administrator", engineer: "Engineer", line_lead: "Line lead", viewer: "Viewer" }[role] ?? role;
}

export function personOf(account: FakeAccount): Person {
  const { password: _password, ...person } = account;
  return { ...person, capabilities: [...person.capabilities] };
}

export type FakeSessionMode = "ok" | "unreachable" | "rate-limiter-down" | "hang";

export class FakeSessionApi implements SessionApi {
  /** The signed-in account, or null. Preset it to start a test signed in. */
  current: FakeAccount | null = null;
  /** Set to simulate the sign-in server's failure modes. */
  mode: FakeSessionMode = "ok";
  /** Whether the next `me()` should say the session expired (after `expireSession()`). */
  private expired = false;
  private failures = 0;

  constructor(readonly world: FakeWorld = new FakeWorld()) {}

  async installation(): Promise<Installation> {
    if (this.mode === "unreachable") {
      unreachable();
    }
    return {
      installation_name: this.world.installationName,
      auth_modes: ["builtin"],
      agents: [...this.world.agents],
      version: VERSION,
      session_lifetime_hours: this.world.sessionLifetimeHours,
    };
  }

  async signIn(email: string, password: string): Promise<Person> {
    if (this.mode === "hang") {
      return new Promise<Person>(() => undefined);
    }
    if (this.mode === "unreachable") {
      unreachable();
    }
    if (this.mode === "rate-limiter-down") {
      fail(503, signInCopy.rateLimiterDown);
    }
    if (this.failures >= 10) {
      fail(429, signInCopy.tooManyAttempts);
    }
    const account = this.world.byEmail(email);
    if (account === undefined || account.password !== password) {
      this.failures += 1;
      fail(401, signInCopy.wrongPassword, "none");
    }
    if (!account.is_active) {
      fail(401, signInCopy.switchedOff, "none");
    }
    this.failures = 0;
    this.expired = false;
    account.last_sign_in_at = new Date().toISOString();
    this.current = account;
    return personOf(account);
  }

  async signOut(): Promise<void> {
    this.current = null;
  }

  async me(): Promise<Person> {
    if (this.mode === "unreachable") {
      unreachable();
    }
    if (this.current === null || !this.current.is_active) {
      const reason = this.expired ? "expired" : "none";
      this.expired = false;
      fail(401, { whatHappened: "You're not signed in.", likelyCause: "", whatToDo: "Sign in to continue." }, reason);
    }
    return personOf(this.current);
  }

  async changePassword(current: string, next: string): Promise<Person> {
    if (this.mode === "unreachable") {
      unreachable();
    }
    if (this.current === null) {
      fail(401, { whatHappened: "You're not signed in.", likelyCause: "", whatToDo: "Sign in to continue." }, "none");
    }
    if (this.current.password !== current) {
      fail(401, signInCopy.wrongPassword, "none");
    }
    if (next.length < 12) {
      fail(400, {
        whatHappened: `The password is too short: it has ${next.length} characters and needs at least 12.`,
        likelyCause: "",
        whatToDo: "Add a few more words.",
      });
    }
    if (next.toLowerCase() === this.current.email.toLowerCase()) {
      fail(400, { whatHappened: "The password can't be your email address.", likelyCause: "", whatToDo: "Choose something only you know." });
    }
    this.current.password = next;
    this.current.must_change_password = false;
    return personOf(this.current);
  }

  /** The session ran out server-side: the next request answers 401 with reason `expired`. */
  expireSession(): void {
    this.current = null;
    this.expired = true;
    signalUnauthorized("expired");
  }

  /** Ten wrong passwords have been tried; the next attempt is throttled. */
  exhaustAttempts(): void {
    this.failures = 10;
  }
}
