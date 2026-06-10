---
id: deploy-port-drift-01
status: open
deps: []
links: [ui-token-url-leak-01, ui-modularize-01]
created: 2026-06-08T00:00:00Z
type: bug
priority: 2
audit: dashboard-ui-review
discovered_via: code_review
---
# Example systemd unit advertises port 8000; real deploy serves 8789

## Why this exists

The checked-in systemd unit example tells operators the hub/dashboard listens on
**8000**, but the actual fleet deploy serves the control plane (and the `/ui`
dashboard) on **8789**. The drift sends anyone reading the repo to the wrong
port.

Evidence:

- `deploy/systemd/mac.service:16-17` —
  `ExecStart=.../uvicorn mac.api:create_app --factory --host 127.0.0.1 --port 8000 ...`
- Real deploy default is 8789:
  `deploy/deploy-mac-fleet.sh:1244` — `MAC_PORT="${MAC_DEPLOY_CONTROL_PORT:-${MAC_PORT:-8789}}"`
  and the health check at `deploy/deploy-mac-fleet.sh:976` uses
  `http://127.0.0.1:${MAC_PORT:-8789}/health`.
- The live rocky hub confirms 8789: `~/.mac/fleets.yaml` rocky entry has
  `control_port: 8789` / `hub_url: http://100.125.137.89:8789`, and
  `GET http://100.125.137.89:8789/ui` returns 200 (`<title>MAC Control Plane</title>`).

Separately, the example unit also binds `--host 127.0.0.1`, whereas the rocky
**hub** agent is deployed with `control_bind_host: "0.0.0.0"`
(`~/.mac/fleets.yaml`), so the example doesn't reflect how the hub actually
exposes the dashboard either. (Worker agents do bind 127.0.0.1, per
`deploy/deploy-mac-fleet.sh:731`.)

## Design

1. Make `deploy/systemd/mac.service` consistent with the deploy script: either
   parameterize the port from `MAC_PORT` (sourced from the env file the unit
   already loads) defaulting to **8789**, or hard-code 8789 to match reality —
   not a third value (8000).
2. Drive `--host` from `MAC_BIND_HOST`/`CONTROL_BIND_HOST` rather than a literal
   `127.0.0.1`, so the example matches both worker (loopback) and hub (`0.0.0.0`)
   topologies.
3. Grep the repo/docs for other stale `8000` / `127.0.0.1:8000` references and
   reconcile (the dashboard README/`docs/dashboard-connection.md` examples).

## Acceptance Criteria

- No checked-in deploy artifact or doc advertises 8000 for the hub/dashboard;
  port and bind host derive from the same env vars the deploy script uses.
- A `grep -rn '8000' deploy/ docs/` turns up no control-plane references (or only
  ones clearly scoped to something else).

## Notes

Low-risk doc/config reconciliation surfaced while locating the live dashboard
during a UI review. Related: [[ui-token-url-leak-01]] (the `0.0.0.0` plain-HTTP
exposure has a security angle), [[ui-modularize-01]].
