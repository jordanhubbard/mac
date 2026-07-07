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
mac login
make run-gui
# open http://127.0.0.1:5273; the active CLI profile connects automatically
```

`make run-gui` reuses the active scoped client profile created by `mac login`,
ensures its SSH tunnel is running, and prompts for the target hub host or IP
before it starts Vite. Press Enter to keep the profile's local SSH tunnel, or
enter a hostname/IP for a direct connection. Direct connections automatically
use the remote hub API port recorded by `mac login` (normally `8789`); the
tunnel's ephemeral loopback port is never reused on the remote host. An
explicit `host:port` or complete `http://`/`https://` URL remains supported.
The credential stays inside the local Vite proxy; it is not printed, placed in
the URL, or exposed to browser storage. Set `IDE_PROFILE=<name>` to select a
non-active profile.

If no client profile exists, the launcher falls back to the existing
deploy handoff file, then the existing fleet-scoped environment token lookup,
and finally to the browser's manual connection form. `deploy-mac-fleet.sh`
writes the handoff as an owner-only JSON file and prints a token-free command:

```bash
IDE_HANDOFF_FILE="$HOME/.mac/fleet-ide-handoff.json" IDE_OPEN=1 make run-gui
```

Use `IDE_AUTH=manual make run-gui` to force the browser form, or
`IDE_API_URL=<url>` to select the endpoint without an interactive prompt. The
existing `make ide-run` target is a compatibility alias. Non-interactive runs
also skip the prompt and retain the resolved profile or default endpoint.

Or from this package:

```bash
cd ide
npm ci
MAC_API_URL=http://100.72.16.110:8789 npm run dev   # manual development fallback
# open http://localhost:5273 and paste a scoped hub token when prompted
```

`/api/*` is proxied to the hub (see `vite.config.ts`). In the normal root-level
launcher flow, Vite adds the profile credential server-side. In the manual
fallback, a pasted token is stored in tab-scoped `sessionStorage["mac.token"]`
(with a migration fallback for an old local value). `?t=` bootstrap values are
removed from the URL immediately.

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

## Verify

```bash
npm run typecheck
npm run test:ui
```

The Playwright regression test runs the workbench against a large projected
ledger, verifies that React StrictMode and repeated stream invalidations do not
create overlapping refreshes, and checks that Kanban lanes render in bounded
batches. The IDE cold-loads `/dashboard/state?view=ide`; full task history and
evidence are fetched only for the selected task with `?view=compact`.

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
