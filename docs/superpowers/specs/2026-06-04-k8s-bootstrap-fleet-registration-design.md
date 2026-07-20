# K8s bootstrap fleet registration — design

> Status: approved (design phase). Next step: implementation plan.

## Problem

The K8s-native deployment (`src/mac/k8s/*`, `deploy/k8s/`, and the GitOps
component at `home-ops/components/ai/mac/`) registers machines, agents, roles,
and projects on bootstrap, but **never registers a fleet record**.

Verified in source:

- `src/mac/k8s/bootstrap.py` posts only to `/machines`, `/agents`, `/roles`,
  `/projects` (lines 106, 126, 211, 243). There is no `/fleets` call.
- `src/mac/k8s/config_loader.py` contains zero references to "fleet"
  (no block to parse).
- A fleet row is created only via `POST /fleets` → `ControlPlane.create_fleet`
  (`src/mac/api.py:2325`, `src/mac/services.py:5458`), which is not called by
  any k8s bootstrap/orchestrator path.

Consequence: `ControlPlane.list_fleets()` returns `[]`, so `/dashboard/state`
returns `"fleets": []` (`src/mac/api.py:1781`). In the UI, the **Map** view's
"Fleets" column is empty and no `fleet → agent` edges are drawn
(`src/mac/ui/app.ts:5009`, `5017`, `5024-5027`). Machines → agents → tasks still
render correctly because those edges are derived from `agent.machine_id`,
`task.owner_agent_id`, and `task.dependencies` (`app.ts:5029-5036`), independent
of fleet records. The "Fleets" nav view and Overview "Fleets" metric show 0.

## Goal

Add an **optional, idempotent** fleet-registration step to the k8s bootstrap so
the UI Map populates the fleet layer, driven entirely by GitOps config. No UI
change is required — once a fleet row exists, the dashboard and Map render it.

## Non-goals

- No changes to the SSH/`mac fleet` deployment path (`fleet_setup.py`,
  `fleet_deploy.py`, `scripts/setup-fleet.py`, `deploy/deploy-mac-fleet.sh`,
  `~/.mac/fleets.yaml`). That path is unrelated and untouched.
- No UI/dashboard code changes.
- No new RBAC or `values.yaml` changes — bootstrap already has the admin token
  and in-cluster network access used for `/machines`, `/agents`, `/roles`,
  `/projects`.

## Design decisions (resolved during brainstorming)

1. **Membership source:** auto-derived from the dispatcher agent + all role
   agents declared in `config.yaml`. No duplicate `agent_ids` list to maintain;
   new roles auto-join the fleet.
2. **Reconcile behavior:** create-or-update. If the fleet is absent, create it
   with the current members; if present, update membership/description/status to
   match config. GitOps config is authoritative across restarts and role
   changes. This is required because `create_fleet` is create-only and raises
   `ValidationError("fleet already exists")` on a duplicate name
   (`services.py:5505`), which would otherwise crash the bootstrap init
   container on every orchestrator restart.
3. **Optionality:** the `fleet:` block is optional. When absent, bootstrap logs
   "no fleet configured; skipping" and does nothing — mirroring the existing
   `attestation_keys` handling (`bootstrap.py:325-327, 487-488`). Existing
   deployments and the sample `deploy/fleet/config.yaml` are unaffected.

## Config schema

New optional top-level block in the k8s `config.yaml`
(`MAC_CONFIG_FILE`, default `/etc/mac/config.yaml`):

```yaml
fleet:
  name: ai-k8s                         # required when the block is present
  description: "K8s-native MAC fleet"  # optional, default ""
  status: active                       # optional, default "active"
```

- Block absent → no fleet registration (skip).
- Block present with missing/blank `name` → fail fast at load
  (`SystemExit`), consistent with other `config_loader` validation.
- `status` must be one of `active`/`inactive`/`retired` (validated server-side
  by `create_fleet`/`update_fleet`); default `active`.

## Component changes

### 1. `src/mac/k8s/config_loader.py`

- Add field `fleet: Optional[JsonDict] = None` to the `MacConfigFile` dataclass
  (alongside `attestation_keys`, line ~43).
- In `load_config_file`, parse `data.get("fleet")`:
  - `None`/absent → leave `fleet = None`.
  - present and a dict with non-empty `name` → store
    `{"name", "description", "status"}` (normalized; description default `""`,
    status default `"active"`).
  - present but not a dict, or `name` blank/missing → `SystemExit` with a clear
    message.
- Add accessor `fleet_block(self) -> Optional[JsonDict]` returning the parsed
  block or `None` (consistent with `attestation_keys_block()`).

### 2. `src/mac/k8s/bootstrap.py`

- Add `fleet: Optional[JsonDict] = None` to `BootstrapConfig`, populated from
  `cfg_file.fleet_block()` in `from_file`.
- New function `register_fleet(mac: MacApiProtocol, cfg: BootstrapConfig)`:
  1. If `cfg.fleet is None` → log "no fleet configured; skipping" and return.
  2. Derive members: dispatcher `agent_id` (`cfg.dispatcher["agent"]["agent_id"]`
     /`["id"]`) + every role agent id (`cfg.role_agents` entries' `agent_id`).
     Deduplicate while preserving first-seen order.
  3. `GET /fleets/{name}`:
     - NotFound (404) → `POST /fleets` with
       `{name, description, status, agent_ids: members, actor: "mac-k8s-bootstrap"}`.
     - exists → `PUT /fleets/{name}` with
       `{agent_ids: members, description, status, actor: "mac-k8s-bootstrap"}`
       to reconcile membership/description/status (mirrors
       `_reconcile_project_metadata`'s GET-then-PUT, `bootstrap.py:256-291`).
  4. Non-object POST/PUT response → `SystemExit` (matches `register_projects`).
- Call `register_fleet(mac, cfg)` in `main()` **after**
  `seed_role_machines_and_agents` and `register_projects`, **before**
  `rotate_attestation_keys`. Ordering matters: `create_fleet`/`update_fleet`
  validate agent ids via `_validated_fleet_agent_ids`
  (`services.py:5478, 5625`), so the member agents must already be registered.

### 3. GitOps — `home-ops/components/ai/mac/config.yaml`

Add the `fleet:` block (e.g. `name: ai-k8s`). This is the only consumer-side
change needed to enable the feature in the live deployment. No
`values.yaml`/`rbac.yaml` change.

> Note: `home-ops` is a separate repository from `mac`. The `mac` change
> (loader + bootstrap + tests) and the `home-ops` config change are committed
> in their respective repos. The feature is inert in any deployment whose
> `config.yaml` has no `fleet:` block.

## Data flow

```
config.yaml (fleet: block)
  → config_loader.load_config_file → MacConfigFile.fleet_block()
  → BootstrapConfig.fleet
  → bootstrap.register_fleet():
        members = [dispatcher.agent_id, *role.agent_id...]  (deduped)
        GET /fleets/{name}
          404 → POST /fleets {name, desc, status, agent_ids}
          ok  → PUT  /fleets/{name} {agent_ids, desc, status}
  → ControlPlane.create_fleet / update_fleet  (validates agent_ids)
  → fleets table row
  → GET /dashboard/state "fleets": [...]   (api.py:1781)
  → UI Map: Fleets column + fleet→agent edges   (app.ts:5017, 5024-5027)
```

## Error handling

| Condition | Behavior |
|-----------|----------|
| `fleet:` block absent | skip, log "no fleet configured" |
| block present, `name` blank/missing | `SystemExit` at config load |
| block present, not a mapping | `SystemExit` at config load |
| invalid `status` | rejected server-side by create/update (`ValidationError`) |
| `GET /fleets/{name}` NotFound | treated as "create" path |
| `GET /fleets/{name}` other error | surfaced (do not silently create a duplicate) |
| POST/PUT non-object response | `SystemExit` (matches `register_projects`) |

## Testing (TDD)

Unit tests using the existing fake `MacApiProtocol` pattern (`mac.post/get/put`):

**`config_loader`:**
- fleet block present → parsed `{name, description, status}` with defaults.
- block absent → `fleet_block()` returns `None`.
- blank/missing `name` → `SystemExit`.
- non-dict block → `SystemExit`.

**`bootstrap.register_fleet`:**
- `cfg.fleet is None` → no API calls (skip).
- GET returns 404/NotFound → exactly one `POST /fleets` with deduped members =
  dispatcher + role agents, in order.
- GET returns existing fleet → exactly one `PUT /fleets/{name}` with the same
  member set (no POST).
- member derivation dedups when a role agent id equals the dispatcher id.
- non-object POST/PUT response → `SystemExit`.

Existing k8s bootstrap/runner tests must continue to pass
(`tests/test_k8s_runner.py` and any `tests/test_k8s_bootstrap*.py`).

## Verification

After deploy with a `fleet:` block:

```console
TOKEN=$(kubectl -n ai get secret mac-secret -o jsonpath='{.data.MAC_WORKER_TOKEN}' | base64 -d)
curl -s -H "Authorization: Bearer $TOKEN" https://mac.<cluster-domain>/fleets | jq
```

Expect one fleet whose `agent_ids` include `mac-runner` and the four virtual
role agents. The UI Map (`/ui` → Fleet → Map) then shows the Fleets column and
`fleet → agent` edges.
