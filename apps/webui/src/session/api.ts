// The session calls (docs/api-contract.md: Public, Session). `HttpSessionApi` is what
// production uses; the in-memory fake for `pnpm dev`, the Playwright smoke test and the
// component tests lives in ./fake.ts so a production bundle never carries it.

import type { HttpClient } from "../api/http";

export interface Person {
  id: string;
  email: string;
  display_name: string;
  role: string;
  role_label: string;
  capabilities: string[];
  must_change_password: boolean;
  is_active: boolean;
  last_sign_in_at: string | null;
}

export type AgentName = "coding" | "validation" | "factory";

export interface Installation {
  installation_name: string;
  auth_modes: string[];
  /** The agents this installation starts (ADR-0017); absent means every one. */
  agents?: AgentName[];
  version: string;
  session_lifetime_hours: number;
}

export interface SessionApi {
  installation(): Promise<Installation>;
  signIn(email: string, password: string): Promise<Person>;
  signOut(): Promise<void>;
  me(): Promise<Person>;
  changePassword(current: string, next: string): Promise<Person>;
}

export class HttpSessionApi implements SessionApi {
  constructor(private readonly http: HttpClient) {}

  installation(): Promise<Installation> {
    return this.http.get<Installation>("/public/installation");
  }

  signIn(email: string, password: string): Promise<Person> {
    return this.http.post<Person>("/session", { email, password });
  }

  signOut(): Promise<void> {
    return this.http.del("/session");
  }

  me(): Promise<Person> {
    return this.http.get<Person>("/me");
  }

  changePassword(current: string, next: string): Promise<Person> {
    return this.http.post<Person>("/me/password", { current_password: current, new_password: next });
  }
}
