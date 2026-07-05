import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:5274",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 900 },
  },
  webServer: {
    command: "VITE_MAC_AUTH_MODE=managed npm run dev -- --host 127.0.0.1 --port 5274",
    reuseExistingServer: false,
    timeout: 30_000,
    url: "http://127.0.0.1:5274",
  },
});
