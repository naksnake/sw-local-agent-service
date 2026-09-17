// The one HTTP client the WebUI uses (docs/api-contract.md). Base `/api/v1`, same-origin
// cookies, JSON in and out, a fresh trace header pair on every request (src/trace.ts) and
// `X-Requested-With: slas-webui` on every state-changing request. Every non-2xx body is
// `{what_happened, likely_cause, what_to_do, trace_id, reason?}` and becomes an ApiError with
// exactly those three parts; a network failure or a non-JSON answer becomes an ApiError with
// the "server not answering" sentences. A 401 anywhere is signalled to the session store.

import { serverNotAnswering, type ThreePart } from "../copy/en";
import { newTraceId, traceHeaders } from "../trace";

export type { ThreePart } from "../copy/en";

export type UnauthorizedReason = "expired" | "none";

export const API_BASE = "/api/v1";
export const REQUESTED_WITH = "slas-webui";

interface ApiErrorInit {
  status: number;
  parts: ThreePart;
  reason?: UnauthorizedReason | null;
  traceId?: string | null;
  unreachable?: boolean;
}

export class ApiError extends Error {
  /** HTTP status; 0 when the server did not answer at all. */
  readonly status: number;
  readonly parts: ThreePart;
  /** Only on a 401: why the session is not valid. */
  readonly reason: UnauthorizedReason | null;
  readonly traceId: string | null;
  /** True when no three-part answer arrived (network failure, non-JSON body). */
  readonly unreachable: boolean;

  constructor(init: ApiErrorInit) {
    super(init.parts.whatHappened);
    this.name = "ApiError";
    this.status = init.status;
    this.parts = init.parts;
    this.reason = init.reason ?? null;
    this.traceId = init.traceId ?? null;
    this.unreachable = init.unreachable ?? false;
  }

  /** The three parts to show: the page's own sentences when the server did not answer. */
  describe(whenUnreachable: ThreePart): ThreePart {
    return this.unreachable ? whenUnreachable : this.parts;
  }
}

/** Any thrown value as an ApiError, so pages have one shape to render. */
export function asApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  return new ApiError({ status: 0, parts: serverNotAnswering, unreachable: true });
}

// --- 401 signalling ----------------------------------------------------------------------------

type UnauthorizedListener = (reason: UnauthorizedReason) => void;
const listeners = new Set<UnauthorizedListener>();

/** Called for every 401 the client sees; the session store subscribes. Returns unsubscribe. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Tell the session store a request came back 401. The fakes call this too. */
export function signalUnauthorized(reason: UnauthorizedReason): void {
  for (const listener of [...listeners]) {
    listener(reason);
  }
}

// --- client ------------------------------------------------------------------------------------

export interface HttpClient {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
  patch<T>(path: string, body: unknown): Promise<T>;
  del(path: string): Promise<void>;
}

export interface HttpOptions {
  base?: string;
  fetch?: typeof fetch;
}

type Method = "GET" | "POST" | "PATCH" | "DELETE";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function threePartOf(body: unknown): ThreePart | null {
  if (!isRecord(body)) {
    return null;
  }
  const { what_happened, likely_cause, what_to_do } = body;
  if (typeof what_happened !== "string" || typeof what_to_do !== "string") {
    return null;
  }
  return {
    whatHappened: what_happened,
    likelyCause: typeof likely_cause === "string" ? likely_cause : "",
    whatToDo: what_to_do,
  };
}

function reasonOf(body: unknown): UnauthorizedReason {
  return isRecord(body) && body["reason"] === "expired" ? "expired" : "none";
}

export function createHttp(options: HttpOptions = {}): HttpClient {
  const base = options.base ?? API_BASE;
  const fetchImpl = options.fetch ?? ((input, init) => globalThis.fetch(input, init));

  async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
    const traceId = newTraceId();
    const headers: Record<string, string> = { Accept: "application/json", ...traceHeaders(traceId) };
    if (method !== "GET") {
      headers["X-Requested-With"] = REQUESTED_WITH;
    }
    const init: RequestInit = { method, headers, credentials: "same-origin" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    let response: Response;
    try {
      response = await fetchImpl(`${base}${path}`, init);
    } catch {
      throw new ApiError({ status: 0, parts: serverNotAnswering, traceId, unreachable: true });
    }

    const echoedTrace = response.headers.get("X-Slas-Trace-Id") ?? traceId;
    if (response.status === 204) {
      return undefined as T;
    }

    let parsed: unknown;
    let isJson = true;
    const text = await response.text();
    try {
      parsed = text === "" ? undefined : JSON.parse(text);
    } catch {
      isJson = false;
    }

    if (response.ok) {
      if (!isJson) {
        throw new ApiError({ status: response.status, parts: serverNotAnswering, traceId: echoedTrace, unreachable: true });
      }
      return parsed as T;
    }

    const parts = isJson ? threePartOf(parsed) : null;
    const reason = response.status === 401 ? reasonOf(parsed) : null;
    const error = new ApiError({
      status: response.status,
      parts: parts ?? serverNotAnswering,
      reason,
      traceId: (isRecord(parsed) && typeof parsed["trace_id"] === "string" ? parsed["trace_id"] : null) ?? echoedTrace,
      unreachable: parts === null,
    });
    if (response.status === 401) {
      signalUnauthorized(reason ?? "none");
    }
    throw error;
  }

  return {
    get: <T>(path: string) => request<T>("GET", path),
    post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
    patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
    del: (path: string) => request<void>("DELETE", path),
  };
}

/** The client production code uses; tests build their own with a fake `fetch`. */
export const http: HttpClient = createHttp();
