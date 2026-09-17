// Everything in memory, including the agent pages and their wizards, for review without a
// backend (`pnpm dev`, the Playwright smoke test). Loaded lazily by main.tsx only when
// apis.ts says so; a production build never requests this chunk. The fake world survives a
// page reload through sessionStorage (one tab, gone when the tab closes), so reloading keeps
// a person signed in or on Choose a new password, as the real api would.

import { FakePeopleApi, FakeSettingsApi } from "./admin/fake";
import type { AppProps } from "./App";
import { FakeCodingApi } from "./coding/api";
import { FakeFactoryApi, FakeStationsAdminApi } from "./factory/api";
import { FakeGitApi } from "./git/api";
import { FakeHomeListsApi } from "./home/fake";
import { FakeModelsApi } from "./models/fake";
import { FakeSessionApi, FakeWorld } from "./session/fake";
import { FakeValidationApi } from "./validation/api";

export const FAKE_SIGN_IN_HINT =
  "Development build with in-memory fakes. Sign in as admin@slas.local with the one-time password admin-one-time-pw, or as pat@slas.local with pat-password-12345.";

const STORAGE_KEY = "slas-fake-world";

function restore(world: FakeWorld, session: FakeSessionApi, storage: Storage | null): void {
  if (storage === null) {
    return;
  }
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (raw === null) {
      return;
    }
    const saved = JSON.parse(raw) as { world?: unknown; current?: string | null };
    world.restore(saved.world);
    session.current = typeof saved.current === "string" ? (world.byEmail(saved.current) ?? null) : null;
  } catch {
    // Unreadable or absent: start from the defaults.
  }
}

function persistOnLeave(world: FakeWorld, session: FakeSessionApi, storage: Storage | null): void {
  if (storage === null || typeof window === "undefined") {
    return;
  }
  const save = () => {
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify({ world, current: session.current?.email ?? null }));
    } catch {
      // Storage full or blocked: the next load starts from the defaults.
    }
  };
  window.addEventListener("pagehide", save);
}

function sessionStorageOrNull(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

export function fakeApis(storage: Storage | null = sessionStorageOrNull()): AppProps {
  const world = new FakeWorld();
  const sessionApi = new FakeSessionApi(world);
  restore(world, sessionApi, storage);
  persistOnLeave(world, sessionApi, storage);
  return {
    sessionApi,
    homeListsApi: new FakeHomeListsApi(),
    peopleApi: new FakePeopleApi(world),
    settingsApi: new FakeSettingsApi(world),
    modelsApi: new FakeModelsApi(),
    codingApi: new FakeCodingApi(),
    validationApi: new FakeValidationApi(),
    factoryApi: new FakeFactoryApi(),
    stationsApi: new FakeStationsAdminApi(),
    gitApi: new FakeGitApi(),
    signInHint: FAKE_SIGN_IN_HINT,
  };
}
