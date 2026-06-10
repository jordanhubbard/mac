---
id: auth-token-sync-01
status: open
deps: []
links: [ui-token-url-leak-01]
created: 2026-06-09T00:00:00Z
type: feature
priority: 1
audit: dashboard-ui-review
discovered_via: incident_debug
---
# Token sync + graceful rotation for fleet bearer credentials

## Why this exists

The hub accepts *only* the bearer tokens loaded from its own `~/.mac/mac.env`
at process startup (`MAC_API_TOKEN` / `MAC_API_TOKENS`; see
`mac.api._load_auth_tokens_from_env`, `src/mac/api.py:1117`), with no expiry —
`_resolve_principal` is a plain dict lookup (`src/mac/api.py:1204`). The client,
meanwhile, sends `MAC_API_TOKEN__<FLEET>` (`src/mac/dispatch.py:1058`). Nothing
kept those two copies in sync, so they drifted and the hub returned
`403 "unknown bearer token"`.

Concretely (the incident that surfaced this): on this workstation
`MAC_API_TOKEN__ROCKY` (fp `a958f750`) had diverged from what the rocky hub was
deployed to accept, `MAC_DEPLOY_HUB_TOKEN__ROCKY` (fp `1f2d8bb5`). The hub's
running `MAC_API_TOKEN` matched the deploy token, confirming the client copy was
simply wrong. There was no in-band recovery — you can't recover an API
credential through the API it gates — so the only fix was the out-of-band,
higher-trust channel: SSH to the hub and read its `MAC_API_TOKEN`.

## Shipped (this ticket)

**#1 — `mac fleet sync-token --fleet <f>`** (`src/mac/fleet_creds.py:sync_token`):
codifies the recovery path. Resolves the hub's SSH target from `fleets.yaml`
(hub_agent → its `target`, honoring `ssh_jump` / strict-host-key from
`defaults`), SSHes in to read the hub's current `MAC_API_TOKEN`, and writes it
into `~/.mac/.env` as `MAC_API_TOKEN__<FLEET>` idempotently (timestamped backup;
`mac.fleet_env.set_env_key`). Prints only a fingerprint, never the secret.

**#3 — `mac fleet rotate-token --fleet <f> [--scope ...] [--prune] [--apply]
[--restart]`** (`src/mac/fleet_creds.py:rotate_token`): graceful rotation using
the hub's existing multi-token `MAC_API_TOKENS` registry, composing with #1 via
the unchanged loader semantics (non-empty `MAC_API_TOKENS` map wins, else single
`MAC_API_TOKEN`):
- default = **dry-run plan** (no mutation; fingerprints only);
- `--apply` mints a new token, sets hub `MAC_API_TOKENS = {old…, new}` (overlap
  window) and advertises `MAC_API_TOKEN = new` so other clients can pick it up
  with `sync-token`, then syncs the operator's own client;
- `--apply --prune` clears `MAC_API_TOKENS` back to the single current token,
  ending the overlap once everyone has rolled over.

Secrets move only over SSH **stdin** (never argv/env/stdout). The SSH runner is
injectable; `tests/test_fleet_creds.py` (34 cases incl. `fleet_env`) covers the
env writer, fleets.yaml resolution, ssh/restart command construction, the
registry primitives, and the sync/rotate/prune flows with a fake runner.

## Remaining follow-ups

1. **Divergence detection (#2):** have the client warn when its
   `MAC_API_TOKEN__<FLEET>` no longer matches the hub (e.g. a `mac fleet doctor`
   check, or auto-suggesting `sync-token` on a 403).
2. **Per-client identity + enrollment (#4):** issue scoped per-client tokens
   (an enrollment flow — device-code / SSH-cert / mTLS) so revoking or
   recovering one client never touches the others. `TokenPrincipal` already
   carries scopes (`src/mac/api.py:58`), so this is an extension, not a rewrite.
3. **`--apply` verification against a live hub:** the remote env-write +
   service-restart paths are unit-tested via an injected runner but have not
   been exercised end-to-end against a real hub; validate on rocky before
   relying on `--apply --restart`.

## Notes

Surfaced while assessing the dashboard UI (the dashboard `/ui` itself needs no
token, so it loaded fine — but every authenticated `mac` call 403'd). Related:
[[ui-token-url-leak-01]] (same "secrets must not leak into logs" principle —
this implementation keeps tokens off argv/stdout).
