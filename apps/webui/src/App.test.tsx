import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { PRODUCT_NAME } from "./branding";

describe("App shell", () => {
  it("names the product as a heading", () => {
    render(<App />);
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading.textContent).toBe(PRODUCT_NAME);
  });

  it("tells the visitor what to expect in a sentence, not a code", () => {
    render(<App />);
    expect(screen.getByText(/being set up on this host/)).toBeTruthy();
  });
});
