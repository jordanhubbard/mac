# Dashboard Connection Contract

The MAC dashboard is topology-neutral. The renderer talks to
`createDashboardApi()` and never shells out, opens SSH, or knows fleet routing.

## Browser / Hosted Mode

When served from the MAC API host, leave `MAC API URL` empty. Requests stay
same-origin:

```text
GET /dashboard/state
GET /observability/stream
```

When a static renderer must call a remote API directly, prefill the endpoint
with either:

```text
?api=https://mac.example.test
?u=https://mac.example.test
```

or set:

```js
window.MAC_DASHBOARD_CONFIG = {
  apiBaseUrl: "https://mac.example.test",
  displayName: "Production fleet"
};
```

## Electron Mode

Electron owns SSH, tunnels, SSO, keychain, and local proxy setup in the main
process. The renderer may receive an optional bridge via preload:

```js
contextBridge.exposeInMainWorld("macDashboard", {
  async connection() {
    return {
      mode: "electron-managed",
      apiBaseUrl: "http://127.0.0.1:18789",
      displayName: "Rocky / horde"
    };
  },
  async request(path, init) {
    return ipcRenderer.invoke("mac-dashboard:request", { path, init });
  },
  async openService(serviceId, fallbackUrl) {
    return ipcRenderer.invoke("mac-dashboard:open-service", { serviceId, fallbackUrl });
  }
});
```

If `request` is omitted, the renderer fetches `apiBaseUrl + path`. That is the
simplest shape when Electron main has already opened an SSH tunnel or local
HTTP proxy. If `request` is supplied, Electron main can bypass browser CORS and
attach SSO/session credentials.

Service navigation also goes through the bridge. That lets Electron open or
reuse tunnels for Qdrant, Firecrawl, TokenHub, or future services without
showing users port-forwarding details.

The optional Electron package lives in `desktop/`:

```bash
make desktop-install
make desktop-package
```

`desktop/main.js` starts a local proxy, opens SSH tunnels when configured, and
loads the existing `/ui` dashboard through that proxy. See
`desktop/README.md` for profile examples and platform packaging commands.
