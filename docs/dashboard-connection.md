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
      displayName: "hub / bastion",
      targetId: "fleet:hub"
    };
  },
  async targets() {
    return [
      { id: "fleet:hub", label: "mac / hub", mode: "fleet-direct" },
      { id: "testing-url", label: "Testing URL", mode: "testing-url" }
    ];
  },
  async selectTarget(targetId, options) {
    return ipcRenderer.invoke("mac-dashboard:select-target", { targetId, ...options });
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
reuse tunnels for Qdrant, Firecrawl, or other explicitly configured services
without showing users port-forwarding details. Standalone TokenHub is not part
of the default fleet topology.

The packaged Electron app first reads secure profiles from
`~/.mac/clients/*.yaml`, keeping the bearer in Electron main via the referenced
mode-`0600` file under `~/.mac/credentials/clients/`. The active secure profile
is the default target. It then reads legacy `~/.mac/fleets.yaml` entries as
additional targets. Fleet SSH routes are resolved by `mac fleet ssh-spec`; the
Electron process no longer independently interprets jump, identity, port, or
host-key defaults. The dashboard's URL field is reserved for the `Testing URL`
target so local or throwaway API endpoints can still be checked without making
URL entry part of normal fleet usage.

The current package consumes profiles provisioned by the CLI rather than
performing enrollment inside Electron. Use `mac login` to create the active
profile, `mac login status` to inspect it, and `mac logout --revoke` to retire
it. Profiles created by the lower-level `mac client profile ...` recovery
workflow remain valid IDE targets. See [SSH Client Bootstrap
Contracts](client-bootstrap-contract.md); do not copy a hub's complete
`~/.mac` directory or admin credential into the desktop client.

Electron mode exposes visible `Fleet hub` and `Bearer token` controls in the
top bar. The renderer sees token-source labels only; token values loaded from
`~/.mac/.env` remain in Electron main and are injected by the local proxy.

The optional Electron package lives in `desktop/`:

```bash
make desktop-install
make desktop-package
```

`desktop/main.js` starts a local proxy, serves the packaged `/ui` dashboard
shell from Electron resources, opens SSH tunnels when configured, and proxies
API requests through the selected target. See
`desktop/README.md` for profile examples and platform packaging commands.
