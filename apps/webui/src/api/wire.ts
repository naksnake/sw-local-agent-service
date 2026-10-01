// Reading snake_case JSON from the api (docs/api-contract-round-2.md) into the pages' camelCase
// views without trusting the wire blindly: every accessor takes `unknown`, checks the type and
// falls back to a harmless default, so a field the api leaves out or sends as null never
// crashes a page — it renders as empty, false or zero and the Http client notes the gap.

export type Wire = Record<string, unknown>;

export function isRecord(value: unknown): value is Wire {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asRecord(value: unknown): Wire {
  return isRecord(value) ? value : {};
}

export function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function nullableStr(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function strList(value: unknown): string[] {
  return list(value).filter((item): item is string => typeof item === "string");
}

export function recordList(value: unknown): Wire[] {
  return list(value).filter(isRecord);
}

/** One of a closed set of words, or the fallback when the api sends something else. */
export function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

/** `/coding/tasks/${seg(id)}` — a path segment the api reads back as the same string. */
export function seg(value: string): string {
  return encodeURIComponent(value);
}
