import { describe, expect, it } from "vitest";

import { newTraceId, newTraceparent, parseTraceparent, traceHeaders } from "./trace";

describe("trace ids", () => {
  it("mints W3C traceparent headers the Python side parses", () => {
    const traceId = newTraceId();
    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
    const header = newTraceparent(traceId);
    expect(header).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
    expect(parseTraceparent(header)).toBe(traceId);
    expect(parseTraceparent("00-" + "0".repeat(32) + "-00f067aa0ba902b7-01")).toBeNull();
    expect(parseTraceparent("nonsense")).toBeNull();
    expect(() => newTraceparent("short")).toThrow("is not a trace id.");
  });

  it("sends the same id in both headers", () => {
    const headers = traceHeaders();
    expect(parseTraceparent(headers.traceparent ?? "")).toBe(headers["X-Slas-Trace-Id"]);
    // The header the end-to-end Python test starts from has exactly this shape.
    expect(parseTraceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")).toBe(
      "4bf92f3577b34da6a3ce929d0e0e4736",
    );
  });
});
