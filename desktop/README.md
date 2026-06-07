# MAC Control Plane Desktop

This is an optional Electron shell for the MAC dashboard. It keeps the Python
project Node-free at the repository root while providing a desktop build target
for users who should not have to understand SSH tunnels or fleet topology.

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

## Run

Fleet targets:

```bash
cd desktop
npm start
```

By default, the app reads `~/.mac/fleets.yaml` and `~/.mac/.env`, presents the
configured fleets in the target dropdown, and keeps API tokens and SSH routing
inside Electron main. Use `MAC_DESKTOP_FLEETS_CONFIG=/path/to/fleets.yaml` to
override the fleet file.

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

The main process opens a local HTTP proxy and loads the existing `/ui`
dashboard shell from the packaged app resources. API requests from that shell
are proxied to the selected fleet. API credentials and SSH commands remain in
Electron main; the renderer only sees the `window.macDashboard` bridge.

The dashboard's URL field is a testing fallback. Normal fleet connections
should be selected from the target dropdown.

The packaged UI also exposes `Fleet hub` and `Bearer token` controls in the
top bar. Token values from `~/.mac/.env` stay in Electron main; the renderer
only receives token-source labels such as `Hub token (ROCKY)` or `Manual
bearer token`.

## Profile Shape

Profiles are plain JSON:

```json
{
  "displayName": "Rocky / horde",
  "tokenEnv": "MAC_DESKTOP_API_TOKEN",
  "ssh": {
    "target": "horde@example.test",
    "localPort": 18789,
    "remoteHost": "127.0.0.1",
    "remotePort": 8789
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
