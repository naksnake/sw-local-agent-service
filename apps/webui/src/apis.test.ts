import { describe, expect, it } from "vitest";

import { fakeApis } from "./apis.fake";
import { realApis, wantsFakes } from "./apis";
import { HttpCodingApi } from "./coding/http";
import { HttpFactoryApi, HttpStationsAdminApi } from "./factory/http";
import { HttpGitApi } from "./git/http";
import { HttpSessionApi } from "./session/api";
import type { FakeSessionApi } from "./session/fake";
import { HttpValidationApi } from "./validation/http";

describe("which APIs the bundle runs on", () => {
  it("defaults to the real APIs in a production build and to the fakes under vite dev", () => {
    expect(wantsFakes({ DEV: false })).toBe(false);
    expect(wantsFakes({ DEV: true })).toBe(true);
    expect(wantsFakes({ DEV: false, VITE_SLAS_FAKE_API: "1" })).toBe(true);
    expect(wantsFakes({ DEV: true, VITE_SLAS_FAKE_API: "0" })).toBe(false);
    expect(wantsFakes({ DEV: false, VITE_SLAS_FAKE_API: "" })).toBe(false);
  });

  it("gives production every HTTP API, so the agent pages, Git and Stations are on the rail", () => {
    const apis = realApis();
    expect(apis.sessionApi).toBeInstanceOf(HttpSessionApi);
    expect(apis.codingApi).toBeInstanceOf(HttpCodingApi);
    expect(apis.validationApi).toBeInstanceOf(HttpValidationApi);
    expect(apis.factoryApi).toBeInstanceOf(HttpFactoryApi);
    expect(apis.stationsApi).toBeInstanceOf(HttpStationsAdminApi);
    expect(apis.gitApi).toBeInstanceOf(HttpGitApi);
    expect(Object.keys(apis).sort()).toEqual([
      "codingApi",
      "factoryApi",
      "gitApi",
      "homeListsApi",
      "modelsApi",
      "peopleApi",
      "sessionApi",
      "settingsApi",
      "stationsApi",
      "validationApi",
    ]);
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
