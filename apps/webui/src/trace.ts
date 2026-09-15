// One trace id from the browser to the executor (CLAUDE.md §8.2). The WebUI mints a W3C
// `traceparent` for every request it sends the api; the api accepts it (or mints one) and
// every hop after that forwards the same 32-hex trace id. `slas_observability.tracing` parses
// exactly this format on the Python side; tests/unit/test_trace_end_to_end.py starts from a
// header this module produced.

export const TRACEPARENT_HEADER = "traceparent";
export const SLAS_TRACE_HEADER = "X-Slas-Trace-Id";

const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

function randomHex(bytes: number): string {
  const buffer = new Uint8Array(bytes);
  if (typeof globalThis.crypto !== "undefined" && typeof globalThis.crypto.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(buffer);
  } else {
    for (let i = 0; i < bytes; i += 1) {
      buffer[i] = Math.floor(Math.random() * 256);
    }
  }
  let hex = "";
  for (const value of buffer) {
    hex += value.toString(16).padStart(2, "0");
  }
  return hex;
}

function nonZeroHex(bytes: number): string {
  let hex = randomHex(bytes);
  while (/^0+$/.test(hex)) {
    hex = randomHex(bytes);
  }
  return hex;
}

/** A new trace id: 32 lowercase hex digits, never all zeros. */
export function newTraceId(): string {
  return nonZeroHex(16);
}

/** `00-<trace id>-<span id>-01`, a fresh span under `traceId` (or a new trace). */
export function newTraceparent(traceId: string = newTraceId()): string {
  if (!/^[0-9a-f]{32}$/.test(traceId) || /^0+$/.test(traceId)) {
    throw new Error(`${traceId} is not a trace id.`);
  }
  return `00-${traceId}-${nonZeroHex(8)}-01`;
}

/** The trace id inside a `traceparent`, or null when the header is not one. */
export function parseTraceparent(header: string): string | null {
  const match = TRACEPARENT.exec(header.trim());
  if (!match) {
    return null;
  }
  const [, traceId, spanId] = match;
  if (!traceId || !spanId || /^0+$/.test(traceId) || /^0+$/.test(spanId)) {
    return null;
  }
  return traceId;
}

/** Headers for one request to the api. Every fetch the WebUI makes goes out with these. */
export function traceHeaders(traceId: string = newTraceId()): Record<string, string> {
  return { [TRACEPARENT_HEADER]: newTraceparent(traceId), [SLAS_TRACE_HEADER]: traceId };
}
