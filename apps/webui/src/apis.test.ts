import { describe, expect, it } from "vitest";

import { fakeApis } from "./apis.fake";
import { realApis, wantsFakes } from "./apis";
import { HttpSessionApi } from "./session/api";
import type { FakeSessionApi } from "./session/fake";

describe("which APIs the bundle runs on", () => {
  it("defaults to the real APIs in a production build and to the fakes under vite dev", () => {
    expect(wantsFakes({ DEV: false })).toBe(false);
    expect(wantsFakes({ DEV: true })).toBe(true);
    expect(wantsFakes({ DEV: false, VITE_SLAS_FAKE_API: "1" })).toBe(true);
    expect(wantsFakes({ DEV: true, VITE_SLAS_FAKE_API: "0" })).toBe(false);
    expect(wantsFakes({ DEV: false, VITE_SLAS_FAKE_API: "" })).toBe(false);
  });

  it("gives production only the round-1 HTTP APIs, keeping the agent pages off the rail", () => {
    const apis = realApis();
    expect(apis.sessionApi).toBeInstanceOf(HttpSessionApi);
    expect(Object.keys(apis).sort()).toEqual(["homeListsApi", "modelsApi", "peopleApi", "sessionApi", "settingsApi"]);
    expect(apis.signInHint).toBeUndefined();
  });

  it("gives development the fakes for every page, with the sign-in hint", () => {
    const apis = fakeApis(null);
    expect(apis.codingApi).toBeDefined();
    expect(apis.validationApi).toBeDefined();
    expect(apis.factoryApi).toBeDefined();
    expect(apis.signInHint).toContain("admin@slas.local");
  });

  it("keeps the fake world and the signed-in person across a reload through storage", async () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    } as unknown as Storage;
    const first = fakeApis(storage);
    const session = first.sessionApi as FakeSessionApi;
    await session.signIn("admin@slas.local", "admin-one-time-pw");
    await session.changePassword("admin-one-time-pw", "correct-horse-battery");
    window.dispatchEvent(new Event("pagehide"));
    expect(store.size).toBe(1);

    const second = fakeApis(storage);
    const me = await second.sessionApi?.me();
    expect(me?.email).toBe("admin@slas.local");
    expect(me?.must_change_password).toBe(false);
    await expect((second.sessionApi as FakeSessionApi).world.byEmail("admin@slas.local")?.password).toBe("correct-horse-battery");
  });
});
