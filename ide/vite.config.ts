import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The hub the IDE talks to. Override with MAC_API_URL at dev time, e.g.
//   MAC_API_URL=http://100.72.16.110:8789 npm run dev
// Requests to /api/* are proxied to the hub so the browser avoids CORS and the
// scoped bearer token stays server-side. The MAC launcher provides
// MAC_IDE_PROXY_TOKEN from the active `mac login` profile.
const HUB = process.env.MAC_API_URL || "http://100.72.16.110:8789";
const HUB_TOKEN = (process.env.MAC_IDE_PROXY_TOKEN || "").trim();

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      "/api": {
        target: HUB,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
        configure(proxy) {
          if (!HUB_TOKEN) return;
          proxy.on("proxyReq", (proxyRequest) => {
            proxyRequest.setHeader("Authorization", `Bearer ${HUB_TOKEN}`);
          });
        },
      },
    },
  },
});
