// Runs before every vitest file. Without `globals: true`, Testing Library cannot register
// its own cleanup, so we unmount rendered trees between tests here.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
