// Admin → People and Admin → Settings calls (docs/api-contract.md). The in-memory fakes live
// in ./fake.ts so a production bundle never carries them.

import type { HttpClient } from "../api/http";
import type { Person } from "../session/api";

// --- People ------------------------------------------------------------------------------------

export interface AddedPerson {
  person: Person;
  one_time_password: string;
}

export interface PeopleApi {
  list(): Promise<Person[]>;
  add(input: { email: string; display_name: string; role: string }): Promise<AddedPerson>;
  changeRole(id: string, role: string): Promise<Person>;
  resetPassword(id: string): Promise<{ one_time_password: string }>;
  setActive(id: string, active: boolean): Promise<Person>;
}

export class HttpPeopleApi implements PeopleApi {
  constructor(private readonly http: HttpClient) {}

  list(): Promise<Person[]> {
    return this.http.get<Person[]>("/admin/people");
  }

  add(input: { email: string; display_name: string; role: string }): Promise<AddedPerson> {
    return this.http.post<AddedPerson>("/admin/people", input);
  }

  changeRole(id: string, role: string): Promise<Person> {
    return this.http.patch<Person>(`/admin/people/${encodeURIComponent(id)}`, { role });
  }

  resetPassword(id: string): Promise<{ one_time_password: string }> {
    return this.http.post<{ one_time_password: string }>(`/admin/people/${encodeURIComponent(id)}/password-reset`);
  }

  setActive(id: string, active: boolean): Promise<Person> {
    return this.http.patch<Person>(`/admin/people/${encodeURIComponent(id)}`, { is_active: active });
  }
}

// --- Settings ----------------------------------------------------------------------------------

export type ChineseVariant = "zh-Hant" | "zh-Hans";

export interface RuntimeSettings {
  installation_name: string;
  chinese_variant: ChineseVariant;
  session_lifetime_hours: number;
}

export interface InstallFacts {
  profile: string;
  data_root: string;
  https_port: number;
  tls_mode: string;
  tls_names: string[];
  version: string;
}

export interface SettingsView {
  runtime: RuntimeSettings;
  install: InstallFacts;
}

export interface SaveResult {
  runtime: RuntimeSettings;
  /** Non-null when the `.env` mirror could not be written; the settings still apply. */
  notice: string | null;
}

export interface SettingsApi {
  get(): Promise<SettingsView>;
  save(changes: Partial<RuntimeSettings>): Promise<SaveResult>;
}

export class HttpSettingsApi implements SettingsApi {
  constructor(private readonly http: HttpClient) {}

  get(): Promise<SettingsView> {
    return this.http.get<SettingsView>("/admin/settings");
  }

  save(changes: Partial<RuntimeSettings>): Promise<SaveResult> {
    return this.http.patch<SaveResult>("/admin/settings", changes);
  }
}
