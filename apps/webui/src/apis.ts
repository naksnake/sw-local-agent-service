// Which APIs the app runs on. Production: the HTTP clients against /api/v1 (docs/api-contract.md).
// Development and the Playwright smoke test: in-memory fakes (apis.fake.ts), so `pnpm dev`
// works without a backend. The choice is made once, at build time, from VITE_SLAS_FAKE_API:
//   - "1"   → fakes, in any build
//   - "0"   → real APIs, in any build
//   - unset → real APIs in a production build; fakes under `vite dev`
// A production bundle therefore defaults to the real APIs, and a fake bundle is only ever
// produced on purpose. main.tsx loads apis.fake.ts lazily, so a real build never fetches it.

import { HttpPeopleApi, HttpSettingsApi } from "./admin/api";
import { http } from "./api/http";
import type { AppProps } from "./App";
import { HttpCodingApi } from "./coding/http";
import { HttpFactoryApi, HttpStationsAdminApi } from "./factory/http";
import { HttpGitApi } from "./git/http";
import { HttpHomeListsApi } from "./home/api";
import { HttpModelsApi } from "./models/api";
import { HttpSessionApi } from "./session/api";
import { HttpValidationApi } from "./validation/http";

export interface BuildEnv {
  DEV: boolean;
  VITE_SLAS_FAKE_API?: string | undefined;
}

export function wantsFakes(env: BuildEnv): boolean {
  if (env.VITE_SLAS_FAKE_API === "1") {
    return true;
  }
  if (env.VITE_SLAS_FAKE_API === "0") {
    return false;
  }
  return env.DEV;
}

/**
 * Everything over HTTP (docs/api-contract.md, docs/api-contract-round-2.md): round 1's sign-in,
 * Home lists, People, Settings and Models, and round 2's Coding, Validation, Factory, Git
 * (Settings → Git remotes, Admin → Git hosts, the Git panel) and Admin → Stations. App.tsx puts
 * a page on the rail when its api is present and the person holds the capability.
 */
export function realApis(): AppProps {
  return {
    sessionApi: new HttpSessionApi(http),
    homeListsApi: new HttpHomeListsApi(http),
    peopleApi: new HttpPeopleApi(http),
    settingsApi: new HttpSettingsApi(http),
    modelsApi: new HttpModelsApi(http),
    codingApi: new HttpCodingApi(http),
    validationApi: new HttpValidationApi(http),
    factoryApi: new HttpFactoryApi(http),
    stationsApi: new HttpStationsAdminApi(http),
    gitApi: new HttpGitApi(http),
  };
}
