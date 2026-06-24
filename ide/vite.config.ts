import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The hub the IDE talks to. Override with MAC_API_URL at dev time, e.g.
//   MAC_API_URL=http://100.72.16.110:8789 npm run dev
// Requests to /api/* are proxied to the hub so the browser avoids CORS and the
// bearer token stays server-side-ish (set VITE_MAC_TOKEN for auth).
const HUB = process.env.MAC_API_URL || "http://100.72.16.110:8789";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      "/api": {
        target: HUB,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
