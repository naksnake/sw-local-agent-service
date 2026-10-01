// The three lists Home is built from (docs/api-contract.md, "The Home lists"). Round 1
// answers empty lists; the shapes are the agents' own types. These are list-only views of
// the CodingApi, ValidationApi and FactoryApi — the wizards and the agent pages stay off the
// rail until their calls exist, so App never receives these as the full agent APIs.

import type { HttpClient } from "../api/http";
import type { CodingApi, CodingTask } from "../coding/api";
import type { FactoryApi, FactoryJob } from "../factory/api";
import type { RunView, ValidationApi } from "../validation/api";

export type CodingListApi = Pick<CodingApi, "listTasks">;
export type ValidationListApi = Pick<ValidationApi, "listRuns">;
export type FactoryListApi = Pick<FactoryApi, "listJobs">;

export interface HomeListsApi {
  listTasks(): Promise<CodingTask[]>;
  listRuns(): Promise<RunView[]>;
  listJobs(): Promise<FactoryJob[]>;
}

export function httpCodingListApi(http: HttpClient): CodingListApi {
  return { listTasks: () => http.get<CodingTask[]>("/coding/tasks") };
}

export function httpValidationListApi(http: HttpClient): ValidationListApi {
  return { listRuns: () => http.get<RunView[]>("/validation/runs") };
}

export function httpFactoryListApi(http: HttpClient): FactoryListApi {
  return { listJobs: () => http.get<FactoryJob[]>("/factory/jobs") };
}

export class HttpHomeListsApi implements HomeListsApi {
  private readonly coding: CodingListApi;
  private readonly validation: ValidationListApi;
  private readonly factory: FactoryListApi;

  constructor(http: HttpClient) {
    this.coding = httpCodingListApi(http);
    this.validation = httpValidationListApi(http);
    this.factory = httpFactoryListApi(http);
  }

  listTasks(): Promise<CodingTask[]> {
    return this.coding.listTasks();
  }

  listRuns(): Promise<RunView[]> {
    return this.validation.listRuns();
  }

  listJobs(): Promise<FactoryJob[]> {
    return this.factory.listJobs();
  }
}
