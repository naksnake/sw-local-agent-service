import { afterEach, describe, expect, it, vi } from "vitest";

import { serverNotAnswering } from "../copy/en";
import { parseTraceparent } from "../trace";
import { ApiError, asApiError, createHttp, onUnauthorized, REQUESTED_WITH, signalUnauthorized } from "./http";

type Call = { url: string; init: RequestInit };

function fetchWith(answer: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const impl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const call = { url: String(input), init: init ?? {} };
    calls.push(call);
    return Promise.resolve(answer(call));
  }) as typeof fetch;
  return { calls, http: createHttp({ fetch: impl }) };
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function headerOf(call: Call, name: string): string | undefined {
  return (call.init.headers as Record<string, string>)[name];
}

const THREE_PART = {
  what_happened: "Someone already signs in as ana@company.local.",
  likely_cause: "The address belongs to an existing person, maybe switched off.",
  what_to_do: "Use another address, or switch the existing account back on.",
  trace_id: "0af7651916cd43dd8448eb211c80319c",
};

describe("http client (docs/api-contract.md)", () => {
  const unsubscribers: (() => void)[] = [];
  afterEach(() => {
    for (const unsubscribe of unsubscribers.splice(0)) {
      unsubscribe();
    }
  });

  it("GETs under /api/v1 with the trace headers, same-origin cookies and no X-Requested-With", async () => {
    const { calls, http } = fetchWith(() => json(200, { ok: true }));
    const answer = await http.get<{ ok: boolean }>("/me");
    expect(answer).toEqual({ ok: true });
    const [call] = calls;
    expect(call?.url).toBe("/api/v1/me");
    expect(call?.init.method).toBe("GET");
    expect(call?.init.credentials).toBe("same-origin");
    const traceparent = headerOf(call as Call, "traceparent") ?? "";
    const traceId = headerOf(call as Call, "X-Slas-Trace-Id");
    expect(parseTraceparent(traceparent)).toBe(traceId);
    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
    expect(headerOf(call as Call, "X-Requested-With")).toBeUndefined();
  });

  it("sends X-Requested-With: slas-webui and a JSON body on POST, PATCH and DELETE", async () => {
    const { calls, http } = fetchWith((call) => (call.init.method === "DELETE" ? new Response(null, { status: 204 }) : json(200, {})));
    await http.post("/session", { email: "a@b.c", password: "x" });
    await http.patch("/admin/settings", { installation_name: "Lab 3" });
    const nothing = await http.del("/session");
    expect(nothing).toBeUndefined();
    for (const call of calls) {
      expect(headerOf(call, "X-Requested-With")).toBe(REQUESTED_WITH);
      expect(headerOf(call, "X-Slas-Trace-Id")).toMatch(/^[0-9a-f]{32}$/);
    }
    expect(calls[0]?.init.body).toBe(JSON.stringify({ email: "a@b.c", password: "x" }));
    expect(headerOf(calls[0] as Call, "Content-Type")).toBe("application/json");
    expect(calls[1]?.init.method).toBe("PATCH");
    expect(calls[2]?.init.method).toBe("DELETE");
    expect(calls[2]?.init.body).toBeUndefined();
  });

  it("maps a non-2xx three-part body to an ApiError with the parts, the status and the trace id", async () => {
    const { http } = fetchWith(() => json(409, THREE_PART));
    const error = await http.post("/admin/people", {}).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(409);
    expect(apiError.unreachable).toBe(false);
    expect(apiError.reason).toBeNull();
    expect(apiError.traceId).toBe(THREE_PART.trace_id);
    expect(apiError.parts).toEqual({
      whatHappened: THREE_PART.what_happened,
      likelyCause: THREE_PART.likely_cause,
      whatToDo: THREE_PART.what_to_do,
    });
    expect(apiError.describe(serverNotAnswering)).toEqual(apiError.parts);
  });

  it("signals every 401 with its reason so the app returns to Sign in", async () => {
    const seen: string[] = [];
    unsubscribers.push(onUnauthorized((reason) => seen.push(reason)));
    const { http } = fetchWith((call) =>
      json(401, { ...THREE_PART, reason: call.url.endsWith("/me") ? "expired" : "none" }),
    );
    const expired = (await http.get("/me").catch((e: unknown) => e)) as ApiError;
    expect(expired.reason).toBe("expired");
    const none = (await http.post("/session", {}).catch((e: unknown) => e)) as ApiError;
    expect(none.reason).toBe("none");
    expect(seen).toEqual(["expired", "none"]);
  });

  it("turns a network failure into the server-not-answering error", async () => {
    const { http } = fetchWith(() => {
      throw new TypeError("Failed to fetch");
    });
    const error = (await http.get("/me").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.unreachable).toBe(true);
    expect(error.parts).toEqual(serverNotAnswering);
    const mine = { whatHappened: "The sign-in server didn't answer.", likelyCause: "x", whatToDo: "y" };
    expect(error.describe(mine)).toEqual(mine);
  });

  it("treats a non-JSON answer as the server not answering, keeping the status", async () => {
    const { http } = fetchWith(() => new Response("<html>502 Bad Gateway</html>", { status: 502 }));
    const error = (await http.get("/models").catch((e: unknown) => e)) as ApiError;
    expect(error.unreachable).toBe(true);
    expect(error.status).toBe(502);
    expect(error.parts).toEqual(serverNotAnswering);

    const { http: okButHtml } = fetchWith(() => new Response("<html>index</html>", { status: 200 }));
    const error2 = (await okButHtml.get("/models").catch((e: unknown) => e)) as ApiError;
    expect(error2.unreachable).toBe(true);
  });

  it("wraps any other thrown value as an unreachable ApiError", () => {
    const wrapped = asApiError(new Error("boom"));
    expect(wrapped.unreachable).toBe(true);
    expect(asApiError(wrapped)).toBe(wrapped);
  });

  it("lets listeners unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = onUnauthorized(listener);
    signalUnauthorized("none");
    unsubscribe();
    signalUnauthorized("expired");
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
