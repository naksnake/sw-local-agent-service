import { describe, expect, it } from "vitest";

import { humanTime } from "../time";
import { homeWelcome, lifetimeWords, roleInfo } from "./en";

describe("copy helpers", () => {
  it("says the session lifetime as people do", () => {
    expect(lifetimeWords(8)).toBe("8 hours");
    expect(lifetimeWords(1)).toBe("1 hour");
    expect(lifetimeWords(24)).toBe("1 day");
    expect(lifetimeWords(168)).toBe("7 days");
    expect(lifetimeWords(36)).toBe("36 hours");
  });

  it("builds the Home sentence from capabilities, never from capability names", () => {
    expect(homeWelcome("Administrator", ["admin:people", "admin:settings", "screen"], { agentsPresent: false })).toBe(
      "Signed in as Administrator. Coding, Validation and Factory arrive in later phases; People and Settings are under Admin.",
    );
    expect(homeWelcome("Administrator", ["admin:people"], { agentsPresent: true })).toBe(
      "Signed in as Administrator. People and Settings are under Admin.",
    );
    expect(homeWelcome("Pat Lin", ["screen", "ssh", "git:remote_manage", "approve:destructive"], { agentsPresent: false })).toBe(
      "Signed in as Pat Lin. You can operate screens, run commands on targets, work with Git remotes and approve destructive steps. Coding, Validation and Factory arrive in later phases.",
    );
    expect(homeWelcome("Vi", [], { agentsPresent: true })).toBe("Signed in as Vi. You can read tickets, runs and reports.");
    expect(homeWelcome("Vi", ["screen"], { agentsPresent: true })).toBe("Signed in as Vi. You can operate screens.");
  });

  it("knows the shipped roles and falls back to the id", () => {
    expect(roleInfo("line_lead").label).toBe("Line lead");
    expect(roleInfo("unknown")).toEqual({ id: "unknown", label: "unknown", sentence: "" });
  });

  it("says time as people do", () => {
    const now = new Date("2026-09-17T12:00:00Z");
    const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString();
    expect(humanTime(null, now)).toBe("Never");
    expect(humanTime("not a date", now)).toBe("Never");
    expect(humanTime(ago(20), now)).toBe("just now");
    expect(humanTime(ago(60), now)).toBe("1 minute ago");
    expect(humanTime(ago(3 * 60), now)).toBe("3 minutes ago");
    expect(humanTime(ago(2 * 3600), now)).toBe("2 hours ago");
    expect(humanTime(ago(26 * 3600), now)).toBe("yesterday");
    expect(humanTime(ago(3 * 86400), now)).toBe("3 days ago");
    expect(humanTime(ago(30 * 86400), now)).toMatch(/2026/);
  });
});
