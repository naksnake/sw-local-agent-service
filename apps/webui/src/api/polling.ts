// Live progress without stealing anything (CLAUDE.md §9): the agent pages re-read their list
// every few seconds while a task, run or job is still moving, and stop as soon as nothing is.
// A poll only replaces React state, so keyed rows update in place; the browser keeps scroll
// position and focus. Polling pauses while the tab is hidden and resumes on return.

import { useEffect, useRef } from "react";

/** Ticket states in which the kernel is still working and the list is worth re-reading. */
const LIVE_STATES: ReadonlySet<string> = new Set(["Open", "Planned", "Approved", "Running", "Analysing"]);

export function isLive(state: string): boolean {
  return LIVE_STATES.has(state);
}

/** The interval the pages use while anything is live; null pauses the hook. */
export const POLL_INTERVAL_MS = 5_000;

/**
 * Call `fn` every `intervalMs` while it is a number; a null interval pauses. The latest `fn`
 * is always used, so callers pass a plain closure. `fn` failures are swallowed: the last good
 * list stays on screen and the next tick tries again — a poll never shows an error of its own.
 */
export function usePolling(fn: () => Promise<void>, intervalMs: number | null): void {
  const latest = useRef(fn);
  latest.current = fn;

  useEffect(() => {
    if (intervalMs === null) {
      return;
    }
    let cancelled = false;
    let busy = false;
    const tick = () => {
      if (cancelled || busy || (typeof document !== "undefined" && document.hidden)) {
        return;
      }
      busy = true;
      latest
        .current()
        .catch(() => {
          // Kept quiet on purpose; see above.
        })
        .finally(() => {
          busy = false;
        });
    };
    const timer = window.setInterval(tick, intervalMs);
    const onVisible = () => {
      if (typeof document !== "undefined" && !document.hidden) {
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs]);
}
