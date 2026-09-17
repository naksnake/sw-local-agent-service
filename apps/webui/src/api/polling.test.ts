import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isLive, POLL_INTERVAL_MS, usePolling } from "./polling";

describe("usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("knows which ticket states are still moving", () => {
    for (const state of ["Open", "Planned", "Approved", "Running", "Analysing"]) {
      expect(isLive(state)).toBe(true);
    }
    for (const state of ["Needs review", "Done", "Failed", ""]) {
      expect(isLive(state)).toBe(false);
    }
    expect(POLL_INTERVAL_MS).toBe(5_000);
  });

  it("calls the latest function on every tick while active and stops when paused", async () => {
    const calls: string[] = [];
    let interval: number | null = POLL_INTERVAL_MS;
    let label = "first";
    const { rerender } = renderHook(() =>
      usePolling(async () => {
        calls.push(label);
      }, interval),
    );
    expect(calls).toEqual([]);
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(calls).toEqual(["first"]);

    label = "second";
    rerender();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(calls).toEqual(["first", "second"]);

    interval = null;
    rerender();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(calls).toEqual(["first", "second"]);
  });

  it("swallows a failing poll and keeps ticking, and never overlaps two polls", async () => {
    let calls = 0;
    let release: () => void = () => undefined;
    const { unmount } = renderHook(() =>
      usePolling(async () => {
        calls += 1;
        if (calls === 1) {
          throw new Error("the api did not answer");
        }
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      }, 1_000),
    );
    await vi.advanceTimersByTimeAsync(1_000);
    expect(calls).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(calls).toBe(2);
    // The second poll is still in flight: the next ticks are skipped rather than stacked.
    await vi.advanceTimersByTimeAsync(3_000);
    expect(calls).toBe(2);
    release();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(calls).toBe(3);
    unmount();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(calls).toBe(3);
  });
});
