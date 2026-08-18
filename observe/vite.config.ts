/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The hub this console reads from during `npm run dev`. In production the
// bundle is served by the hub itself at /ui/ and talks to the same origin.
const HUB = process.env.MAC_API_URL || "http://127.0.0.1:8789";
const HUB_TOKEN = (process.env.MAC_UI_PROXY_TOKEN || "").trim();

// Build output lands inside the Python package so the hub's existing
// StaticFiles mount at /ui/assets serves it with no api.py mount changes:
//   src/mac/ui/console/console.js  ->  GET /ui/assets/console/console.js
export default defineConfig({
  plugins: [react()],
  base: "/ui/assets/console/",
  build: {
    outDir: "../src/mac/ui/console",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "console.js",
        chunkFileNames: "console-[name].js",
        assetFileNames: "console[extname]",
      },
    },
  },
  server: {
    port: 5274,
    proxy: {
      "/dashboard": {
        target: HUB,
        changeOrigin: true,
        configure(proxy) {
          if (!HUB_TOKEN) return;
          proxy.on("proxyReq", (req) => req.setHeader("Authorization", `Bearer ${HUB_TOKEN}`));
        },
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
  },
});
