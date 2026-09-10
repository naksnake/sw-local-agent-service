import { describe, expect, it } from "vitest";

import { cn } from "./utils";

describe("cn", () => {
  it("joins classes and drops falsy values", () => {
    expect(cn("p-2", undefined, false, "text-sm")).toBe("p-2 text-sm");
  });

  it("lets the later Tailwind class win a conflict", () => {
    expect(cn("p-2", "p-4")).toBe("p-4");
    expect(cn("text-red-500", { "text-blue-500": true })).toBe("text-blue-500");
  });
});
