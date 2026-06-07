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
dashboard through it. API credentials and SSH commands remain in Electron main;
the renderer only sees the `window.macDashboard` bridge.

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
