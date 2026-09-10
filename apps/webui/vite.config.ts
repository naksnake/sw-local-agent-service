import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: { host: "127.0.0.1" },
  test: {
    // Pure-module tests only in P0. Component tests need a DOM library, which CLAUDE.md
    // §4.3 does not name yet; ask before adding one (§0.3).
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
