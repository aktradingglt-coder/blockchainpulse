import { defineConfig } from "vitest/config";

// PROMPT §15.2: ≥80% line and branch coverage outside the strict Python packages. Pages and route handlers are
// covered by end-to-end tests (P7); unit coverage applies to library code.
export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      include: ["src/lib/**/*.ts"],
      exclude: ["src/**/*.test.ts"],
      reporter: ["text", "json-summary"],
      thresholds: { lines: 80, branches: 80, functions: 80, statements: 80 },
    },
  },
});
