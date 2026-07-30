import { playwright } from "@vitest/browser-playwright";
import { defineConfig } from "vitest/config";

// Browser mode, not "node": the card's only shipped artifact is a Lit custom
// element, which cannot render without a DOM. A node environment would let
// `npm test` pass while proving nothing about what users actually load.
export default defineConfig({
  test: {
    include: ["src/**/*.test.ts"],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: "chromium" }],
    },
  },
});
