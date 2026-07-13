# MAC Fleet Workbench Desktop

This is an optional Electron shell for the MAC Fleet Workbench IDE. It keeps
the Python project Node-free at the repository root while providing a desktop
build target for users who should not have to understand SSH tunnels or fleet
topology.

## Build

```bash
cd desktop
npm ci
npm run check
npm run package      # unpacked app in desktop/dist/
npm run dist:mac     # macOS dmg + zip
```

Cross-platform targets are also available:

```bash
npm run dist:linux
npm run dist:win
```

From the repository root:

```bash
make desktop-install
make desktop-package
```

Before packaging, build the Fleet Workbench IDE so that `ide/dist` is present:

```bash
make build-gui       # produces ide/dist/
make desktop-package
```

## Run

Fleet targets:

```bash
cd desktop
npm start
```

By default, the app presents secure `~/.mac/clients/*.yaml` profiles first,
with the active profile selected, then legacy entries from
`~/.mac/fleets.yaml`. Profile tokens are read only by Electron main from their
mode-`0600` credential references. Legacy fleet SSH routes are obtained from
`mac fleet ssh-spec`, so Electron does not maintain a second interpretation of
ports, jumps, identities, or host-key policy. Use
`MAC_CLIENT_PROFILES_DIR=/path/to/clients` or
`MAC_DESKTOP_FLEETS_CONFIG=/path/to/fleets.yaml` to override those stores.

Direct API:

```bash
cd desktop
MAC_DESKTOP_API_URL=https://mac.example.test \
MAC_DESKTOP_API_TOKEN=... \
npm start
```

SSH-managed hub tunnel:

```bash
cd desktop
MAC_DESKTOP_SSH_TARGET=horde@example.test \
MAC_DESKTOP_API_TOKEN=... \
npm start
```

Named profile:

```bash
cd desktop
MAC_DESKTOP_PROFILE=profiles.example.json \
MAC_DESKTOP_PROFILE_NAME=sshHub \
MAC_DESKTOP_API_TOKEN=... \
npm start
```

The main process opens a local HTTP proxy and loads the Fleet Workbench IDE
(`ide/dist`) from the packaged app resources. The workbench is a React +
TypeScript Vite build that wraps the live task graph, agent context, A2A
interoperability, and streamed operations. API requests from the workbench are
proxied to the selected fleet. API credentials and SSH commands remain in
Electron main; the renderer only sees the `window.macDashboard` bridge.

The dashboard's URL field is a testing fallback. Normal fleet connections
should be selected from the target dropdown.

The packaged IDE also exposes `Fleet hub` and `Bearer token` controls in the
top bar. Token values from `~/.mac/.env` stay in Electron main; the renderer
only receives token-source labels such as `Hub token (HUB)` or `Manual
bearer token`.

## Profile Shape

Profiles are plain JSON:

```json
{
  "displayName": "hub / horde",
  "tokenEnv": "MAC_DESKTOP_API_TOKEN",
  "ssh": {
    "target": "horde@example.test",
    "localPort": 18789,
      "remoteHost": "127.0.0.1",
      "remotePort": 8789,
      "identityFile": "~/.ssh/mac-production",
      "knownHostsFile": "~/.ssh/mac-production-known-hosts",
      "hostKeyPolicy": "strict"
  },
  "serviceTunnels": {
    "qdrant": {
      "remotePort": 6333,
      "localPort": 16333,
      "path": "/dashboard"
    }
  }
}
```

`serviceTunnels` are opened lazily when the user clicks a dashboard service
link. This keeps the normal app launch fast and hides service-specific port
forwarding from the user.

The desktop app consumes profiles but does not enroll them. Run `mac login`
before opening the app; the CLI owns SSH verification, enrollment, credential
storage, and the managed control-plane tunnel end to end. The lower-level
streaming recovery workflow remains documented in
`docs/client-bootstrap-contract.md`.
