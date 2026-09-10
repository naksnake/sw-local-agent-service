import { describe, expect, it } from "vitest";

import { AGENT_NAMES, PAGES, PRODUCT_NAME, SLUG } from "./branding";

describe("branding (CLAUDE.md §0.1, §9)", () => {
  it("uses the product name and slug exactly", () => {
    expect(PRODUCT_NAME).toBe("SW Local Agent Service");
    expect(SLUG).toBe("slas");
    expect(SLUG).toBe(SLUG.toLowerCase());
  });

  it("names the three agents", () => {
    expect(AGENT_NAMES).toEqual(["Coding Agent", "Validation Agent", "Factory Agent"]);
  });

  it("lists the ten §9 pages in order; adding one needs an ADR", () => {
    expect(PAGES).toHaveLength(10);
    expect(PAGES[0]).toBe("Home");
    expect(PAGES[PAGES.length - 1]).toBe("Admin");
  });
});
