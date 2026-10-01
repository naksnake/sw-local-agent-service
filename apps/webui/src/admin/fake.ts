// In-memory Admin → People and Admin → Settings, speaking the sentences from
// docs/ui/admin-people.md and docs/ui/admin-settings.md so the pages are tested with the
// words the api sends. Loaded only by apis.fake.ts and the tests.

import { VERSION } from "../branding";
import { people as copy, roleInfo, ROLES } from "../copy/en";
import type { Person } from "../session/api";
import { capabilitiesOf, fail, type FakeWorld, personOf, unreachable } from "../session/fake";
import type { AddedPerson, InstallFacts, PeopleApi, RuntimeSettings, SaveResult, SettingsApi, SettingsView } from "./api";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export class FakePeopleApi implements PeopleApi {
  /** Set to make every call fail as if the api were down. */
  down = false;

  constructor(readonly world: FakeWorld) {}

  private guard(): void {
    if (this.down) {
      unreachable();
    }
  }

  private lastActiveAdministrator(id: string): boolean {
    const admins = this.world.accounts.filter((a) => a.role === "administrator" && a.is_active);
    return admins.length === 1 && admins[0]?.id === id;
  }

  async list(): Promise<Person[]> {
    this.guard();
    return [...this.world.accounts].sort((a, b) => a.display_name.localeCompare(b.display_name)).map(personOf);
  }

  async add(input: { email: string; display_name: string; role: string }): Promise<AddedPerson> {
    this.guard();
    const email = input.email.trim();
    if (!EMAIL.test(email)) {
      fail(400, copy.invalidEmail);
    }
    if (this.world.byEmail(email) !== undefined) {
      fail(409, copy.duplicateEmail(email.toLowerCase()));
    }
    if (ROLES.every((role) => role.id !== input.role)) {
      fail(400, {
        whatHappened: `There is no role called ${input.role}.`,
        likelyCause: `The roles are ${ROLES.map((r) => r.id).join(", ")}.`,
        whatToDo: "Pick one of them.",
      });
    }
    const oneTime = this.world.nextOneTimePassword();
    const account = this.world.addAccount(email.toLowerCase(), input.display_name.trim(), input.role, oneTime, {
      mustChange: true,
    });
    return { person: personOf(account), one_time_password: oneTime };
  }

  async changeRole(id: string, role: string): Promise<Person> {
    this.guard();
    const account = this.world.byId(id);
    if (account === undefined) {
      fail(404, { whatHappened: "That person no longer exists.", likelyCause: "", whatToDo: "Reload the list." });
    }
    if (role !== "administrator" && this.lastActiveAdministrator(id)) {
      fail(400, copy.lastAdministratorRole);
    }
    account.role = role;
    account.role_label = roleInfo(role).label;
    account.capabilities = capabilitiesOf(role);
    return personOf(account);
  }

  async resetPassword(id: string): Promise<{ one_time_password: string }> {
    this.guard();
    const account = this.world.byId(id);
    if (account === undefined) {
      fail(404, { whatHappened: "That person no longer exists.", likelyCause: "", whatToDo: "Reload the list." });
    }
    const oneTime = this.world.nextOneTimePassword();
    account.password = oneTime;
    account.must_change_password = true;
    return { one_time_password: oneTime };
  }

  async setActive(id: string, active: boolean): Promise<Person> {
    this.guard();
    const account = this.world.byId(id);
    if (account === undefined) {
      fail(404, { whatHappened: "That person no longer exists.", likelyCause: "", whatToDo: "Reload the list." });
    }
    if (!active && this.lastActiveAdministrator(id)) {
      fail(400, copy.lastAdministratorOff);
    }
    account.is_active = active;
    return personOf(account);
  }
}

export class FakeSettingsApi implements SettingsApi {
  down = false;
  /** The database refuses the save. */
  databaseDown = false;
  /** The save works but the `.env` mirror cannot be written. */
  mirrorFails = false;
  runtime: RuntimeSettings;
  install: InstallFacts = {
    profile: "quickstart",
    data_root: "/AI/Agent",
    https_port: 443,
    tls_mode: "self-signed",
    tls_names: ["127.0.0.1", "localhost", "lab3.internal", "10.20.0.15"],
    version: VERSION,
  };

  constructor(readonly world: FakeWorld) {
    this.runtime = {
      installation_name: world.installationName,
      chinese_variant: "zh-Hant",
      session_lifetime_hours: world.sessionLifetimeHours,
    };
  }

  async get(): Promise<SettingsView> {
    if (this.down) {
      unreachable();
    }
    return { runtime: { ...this.runtime }, install: { ...this.install, tls_names: [...this.install.tls_names] } };
  }

  async save(changes: Partial<RuntimeSettings>): Promise<SaveResult> {
    if (this.down) {
      unreachable();
    }
    if (this.databaseDown) {
      fail(503, {
        whatHappened: "The settings weren't saved.",
        likelyCause: "The database didn't answer.",
        whatToDo: "Try again in a moment; if it repeats, run `slas logs api` on the host.",
      });
    }
    if (changes.installation_name !== undefined && changes.installation_name.length > 60) {
      fail(400, {
        whatHappened: `The name is too long: it has ${changes.installation_name.length} characters, the limit is 60.`,
        likelyCause: "",
        whatToDo: "Shorten it.",
      });
    }
    if (changes.session_lifetime_hours !== undefined && (changes.session_lifetime_hours < 1 || changes.session_lifetime_hours > 168)) {
      fail(400, {
        whatHappened: `A sign-in can't last ${changes.session_lifetime_hours} hours.`,
        likelyCause: "The lifetime must be between 1 hour and 7 days.",
        whatToDo: "Pick one of the offered lengths.",
      });
    }
    this.runtime = { ...this.runtime, ...changes };
    this.world.installationName = this.runtime.installation_name;
    this.world.sessionLifetimeHours = this.runtime.session_lifetime_hours;
    return {
      runtime: { ...this.runtime },
      notice: this.mirrorFails ? "Saved. The copy in .env could not be updated: permission denied." : null,
    };
  }
}
