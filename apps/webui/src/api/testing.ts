// A fake `fetch` for the Http client tests: records every call and answers what the test says.
// Lives beside the client so each feature's http.test.ts asserts the same things the same way:
// method, path, body, X-Requested-With on state-changing requests, wire → view mapping.

import { createHttp, type HttpClient, REQUESTED_WITH } from "./http";

export interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

export interface FakeFetch {
  calls: RecordedCall[];
  http: HttpClient;
  /** The one call made, when exactly one was. */
  only(): RecordedCall;
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function threePart(status: number, whatHappened: string, likelyCause = "", whatToDo = "Try again."): Response {
  return json(status, { what_happened: whatHappened, likely_cause: likelyCause, what_to_do: whatToDo, trace_id: "0".repeat(32) });
}

/** `answer` decides the Response per call; a plain value is answered as 200 JSON. */
export function fetchWith(answer: (call: RecordedCall) => Response | unknown): FakeFetch {
  const calls: RecordedCall[] = [];
  const impl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const headers = (init?.headers as Record<string, string> | undefined) ?? {};
    const call: RecordedCall = {
      url: String(input),
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      headers,
    };
    calls.push(call);
    const reply = answer(call);
    return Promise.resolve(reply instanceof Response ? reply : json(200, reply));
  }) as typeof fetch;
  return {
    calls,
    http: createHttp({ fetch: impl }),
    only() {
      if (calls.length !== 1) {
        throw new Error(`expected exactly one call, saw ${calls.length}`);
      }
      return calls[0] as RecordedCall;
    },
  };
}

/** Every state-changing call carries the browser marker; GETs do not. */
export function expectRequestedWith(call: RecordedCall): void {
  const marker = call.headers["X-Requested-With"];
  if (call.method === "GET") {
    if (marker !== undefined) {
      throw new Error(`GET ${call.url} must not carry X-Requested-With`);
    }
    return;
  }
  if (marker !== REQUESTED_WITH) {
    throw new Error(`${call.method} ${call.url} must carry X-Requested-With: ${REQUESTED_WITH}`);
  }
}
