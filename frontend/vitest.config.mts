import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

// Unit tests for the app's logic (state decisions, the API client). They run in Node: nothing
// under test touches the DOM.
export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: {
    include: ["src/**/*.test.ts"],
    env: { NEXT_PUBLIC_API_URL: "https://api.test/" },
  },
});
