# MAC — Fleet Workbench

A clean-slate operator IDE over the MAC control plane. It replaces the legacy
`src/mac/ui` dashboard SPA with a React + TypeScript workbench built around the
live task graph, agent context, A2A interoperability, and streamed operations.

**Layout:**

- **Activity rail + Explorer** — cockpit, work, workflow, agent, runtime,
  observability, and connection views alongside project/task navigation.
- **Workbench editor** — live dependency DAG plus collection and control
  surfaces for every major operator domain.
- **Bottom panel** — event stream, terminal sessions, evidence, and problems.
- **Agent mesh (right)** — capability inspector, task thread, rich dispatch,
  direct assignment, review request, and A2A Agent Card/delegation surface.

## Run (dev)

From the repository root:

```bash
make install-gui
make run-gui IDE_API_URL=http://100.72.16.110:8789
# open http://127.0.0.1:5273, paste a hub bearer token when prompted
```

`make run-gui` reads `~/.mac/.env` and passes a token to Vite when it can. If
`IDE_FLEET=<fleet>` or `MAC_FLEET=<fleet>` is set, the launcher prefers
`MAC_API_TOKEN__<FLEET>`; otherwise it falls back to `MAC_DEPLOY_HUB_TOKEN` and
then `MAC_API_TOKEN`. It prints the selected key name, never the token value.
The existing `make ide-run` target is a compatibility alias.

Or from this package:

```bash
cd ide
npm ci
MAC_API_URL=http://100.72.16.110:8789 npm run dev   # hub to talk to
# open http://localhost:5273, paste a hub bearer token when prompted
```

`/api/*` is proxied to the hub (see `vite.config.ts`). The token is stored in
tab-scoped `sessionStorage["mac.token"]` (with a migration fallback for an old
local value), or set with `VITE_MAC_TOKEN`. `?t=` bootstrap values are removed
from the URL immediately.

## Build and package

From the repository root:

```bash
make build-gui
make ide-preview       # serves the production build at http://127.0.0.1:5273
make package-gui       # writes dist/mac-ide-web.tar.gz
```

Or from this package:

```bash
npm run build
npm run preview
npm run package
```

`npm run package` and `make ide-package` create a static web bundle, not a
native Electron app. The Electron wrapper is still tracked as follow-up work.

## Status

The workbench foundation is implemented: cockpit telemetry, live task DAG,
searchable work ledger, workflow planner, agent mesh, runtime and observability
surfaces, connection inventory, task/evidence context, streamed invalidation,
and A2A delegation.

See [the active Fleet Workbench plan](../docs/fleet-ide-workbench-plan.md) for
the recovered 57-workflow backlog and the OpenVSCode/Theia extension-substrate
decision that follows this increment.

## Next

- Complete review, workflow-run, runtime, rollout, secret, and service-link actions.
- Upgrade Mac's A2A 0.3 surface to A2A 1.0 streaming and remote peer discovery.
- Decide OpenVSCode Server versus Theia before adopting a standard extension
  host, workspace filesystem, source control, and full terminal.
- Point the desktop package at the canonical workbench artifact.
