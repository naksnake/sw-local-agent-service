// Empty Home lists, as the round-1 api answers.

import type { CodingTask } from "../coding/api";
import type { FactoryJob } from "../factory/api";
import type { RunView } from "../validation/api";
import type { HomeListsApi } from "./api";

export class FakeHomeListsApi implements HomeListsApi {
  async listTasks(): Promise<CodingTask[]> {
    return [];
  }

  async listRuns(): Promise<RunView[]> {
    return [];
  }

  async listJobs(): Promise<FactoryJob[]> {
    return [];
  }
}
