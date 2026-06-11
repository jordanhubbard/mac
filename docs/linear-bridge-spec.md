# Linear Bridge — Design Spec

> Status: **deferred 2026-05-28** — implementation paused pending
> end-to-end validation of the existing beads-backed mac pipeline
> (Hermes → mac-api → mac-k8s-runner → Job → evidence → review). No
> code has been written against this spec. When e2e is confirmed
> working, Phase L0 (outbox extension) is the first concrete PR.
>
> Spec was reviewed by both an architect subagent and `codex exec`
> (codex-cli 0.134.0) before being deferred. v2 post-review revisions
> are below; see [`linear-bridge-spec-review.md`](linear-bridge-spec-review.md)
> for the codex review notes.
>
> See also: [`docs/k8s-native-rewrite-plan.md`](k8s-native-rewrite-plan.md),
> [`docs/production-deployment.md`](production-deployment.md) §Beads Bridge.

## Revision history

**v2 (post-review)** — Addressed findings from architect + codex CLI
reviews. Substantive changes:

- **Removed `TrackerBackend` Protocol from v1.** The "behavior-preserving
  retrofit of `BeadsBridgeService`" claim was wrong; real beads logic
  lives in `ControlPlane` methods in `services.py`, not in the 88-line
  `beads_bridge_service.py` subprocess wrapper. Dispatch in v1 uses
  simple branching on `tenants.tracker_kind`; protocol extraction is
  deferred to a follow-up refactor after both bridges are working.
- **Added Phase L0** for outbox extension. The existing
  `task_transition_outbox` has no `next_attempt_at`, no `max_attempts`,
  no `dead_letter` status, and `mark_outbox_failed` removes rows from
  the pending query (no auto-retry). Spec's previous claim that
  "the outbox already handles retries with exponential backoff" was
  false. L0 fixes this; every other phase depends on it.
- **Rewrote auto-register secret handling (§10).** `SecretsService` has
  no `upsert` method. The spec now uses an explicit get-or-rotate
  pattern: `find_by_name → if absent create_secret, else compare hash
  → rotate_secret if changed`.
- **Per-team state_map (§9.1).** Linear workflow state UUIDs are
  per-team; flat `{name: uuid}` collides across teams. Restructured to
  `{team_id: {state_name: state_uuid}}` and refreshed every poll (one
  cheap GraphQL call) rather than lazy-on-unknown-name.
- **Single-transaction upsert (§9.1).** Spec previously claimed
  `UNIQUE(source, external_id)` gave idempotency for free; in reality
  the existing `import_project_item` is SELECT→create_task→INSERT
  which is race-prone across replicas. v2 specifies a single
  transaction with task + project_item upserts together.
- **Lifecycle comment format (§9.2).** Spec previously showed
  `mac-ledger v1: ...` markdown; actual beads comments are
  pipe-delimited (`task=..., event=..., actor=...`). v2 matches the
  existing format.
- **Auth scope mapping (§11).** mac-api's `_required_scope` has no
  `/bridges/` case today; v2 explicitly adds the mapping rules.
- **Plugin file fixes (§13).** `provides_tools` lives in `plugin.yaml`,
  not `plugin/manifest.py`. v2 lists every file each new tool touches.
- **Three more plugin tools added (§13).** Architect flagged that
  comment/state/link_pr alone is insufficient. v2 adds
  `mac_set_task_priority`, `mac_add_task_label`, `mac_assign_task`.
- **Added Observability (§21), Tenant deletion (§22), Auth (§23)
  sections** that were missing.
- **Fixed factual errors:** `integration_findings` is the correct
  table name (architect was wrong; codex was right per
  `schema.sql:724`). Drift code is `beads.export_drift.ready_mismatch`,
  not `jsonl_only_ready`. Beads writeback failures are silently
  swallowed in `_run_bd_for_task` — flagged as a pre-existing bug,
  out of scope for this spec but noted.

## 1. Goal

Let mac integrate Linear as an external issue-tracker source on equal
footing with the existing beads bridge. A workspace operator chooses
which tracker (beads or Linear) backs each tenant; the rest of mac
(task ledger, claim semantics, runners, evidence) is unchanged.

Concretely:

- A new issue created in Linear by a human appears in mac's
  `tasks` table within one poll tick, tagged with
  `metadata.origin.tracker = "linear"`.
- Mac-managed lifecycle transitions (`claimed`, `running`,
  `needs_review`, `completed`, `failed`) mirror back to Linear as
  comments on the originating Issue.
- Hermes' `mac_create_task` plugin tool routes to Linear (via mac-api)
  when the calling Hermes instance's tenant is Linear-backed; mac
  creates the Issue and the local task row in one round trip.
- Operators wire it declaratively via env vars (Kubernetes-friendly);
  the CLI remains available as an escape hatch.

## 2. Non-goals

- Replacing beads. Beads stays in tree and operational; per-tenant
  config selects which backend to use.
- Replacing the mac task ledger. Linear is the source of truth for
  issue identity and human-facing state. Mac's `tasks` /
  `task_history` / `evidence` / `leases` tables remain the source of
  truth for execution state.
- Webhook support in v1. Polling at ~30s tick is sufficient and avoids
  the public-endpoint complexity. Webhooks may come in a Phase L4.
- Multi-workspace via env vars. v1 auto-register supports one
  workspace per mac instance; additional workspaces require the
  imperative CLI. Multi-workspace declarative support is deferred.
- Migration of existing beads-origin mac tasks into Linear. New tasks
  only. Beads-origin tasks keep their origin metadata forever.

## 3. Pain this addresses

The beads workflow has a three-source-of-truth problem:

1. Embedded Dolt DB (`.beads/embeddeddolt/`) — canonical operational state
2. `.beads/issues.jsonl` — git-tracked export
3. Dolt remote (`bd dolt push/pull`) — cross-machine sync

Plus mac's `.tickets/<id>.md` mirror layer added recently. When any
two drift, mac reports `beads.export_drift.jsonl_only_ready` as an
`integration_findings` row and stops importing the drifted issues
until the operator reconciles. Operationally this means manual
`bd dolt pull` → `bd export` → commit cycles whenever state changes,
and `services.py:_sync_beads_database` has had to disable Dolt sync
entirely because the failure modes are too sharp for automated use.

Linear sidesteps all of this. Its hosted DB is the single source of
truth; mac's mirror is one-way (read-only from mac's perspective for
identity/description fields). Lifecycle comments are written by mac
or by agents directly, with retries handled by
`task_transition_outbox`. No git involvement for issue state.

## 4. Design overview

```
              ┌────────────────┐
              │  Linear (API)  │
              └───────┬────────┘
                      │ GraphQL — poll (and agent-driven writes)
                      ▼
            ┌──────────────────────┐
            │  LinearBridgeService │
            │  (new module)        │
            └───────┬──────────────┘
                    │
                    ▼
            ┌────────────────┐         ┌───────────────────────┐
            │   mac.tasks    │◀────────│ Beads bridge methods  │
            └────────────────┘         │  on ControlPlane      │
              ▲    │                   │  (services.py — many) │
              │    │                   └───────────────────────┘
              │    │
              │    └── task_transition_outbox (v2: extended in L0
              │        with next_attempt_at, last_error,
              │        max_attempts, dead_letter status)
              │           │
              │           ▼
              │     dispatch: if task.metadata.origin.tracker == "linear":
              │                   linear_bridge.emit_lifecycle_comment(...)
              │               else: _append_beads_ledger_comment(...)
              │
              └── mac-k8s-runner / leases / evidence / reviews (unchanged)
```

**v2 design call:** there is no `TrackerBackend` Protocol. The two
bridges are independent services (one already-exists-distributed across
ControlPlane methods, one new module). Consumer code (outbox emitter,
mac-api endpoints, plugin tool handlers) dispatches via a simple
helper:

```python
def get_tracker_kind_for_task(task: Task) -> str:
    return (task.metadata.get("origin") or {}).get("tracker", "beads")

def emit_lifecycle_comment(cp, task, event_type, payload):
    kind = get_tracker_kind_for_task(task)
    if kind == "linear":
        cp.linear_bridge.emit_lifecycle_comment(task, event_type, payload)
    else:
        cp._append_beads_ledger_comment(task, event_type, payload)
```

That's ugly but honest. A proper `TrackerBackend` Protocol requires
first extracting the beads logic out of `ControlPlane` into a real
service — a behavior-preserving refactor of ~1500 lines of
`services.py`. That refactor is deferred to a follow-up; v1 ships the
Linear bridge alongside the existing beads layout, with branching
dispatch in maybe 4-6 places total.

Rationale: the v1 priority is "does Linear actually work end-to-end."
Premature abstraction adds risk (the beads retrofit might break
existing behavior) without delivering user-facing value. Extract the
abstraction once both bridges have been operating in production.

## 5. Source of truth boundary

| Concern | Source of truth |
|---|---|
| Issue identity, title, description, priority, labels, assignees | Linear |
| Linear-state name (Todo / In Progress / Done / ...) | Linear |
| mac task id, mac state, claim/lease, evidence, attempts | mac |
| `tasks.metadata.origin.tracker` | mac (set at import time) |
| `linear_workspaces` config | mac |
| Lifecycle comments mirrored to Linear Issue | both — mac is the producer, Linear is the durable store |

When the two diverge — e.g. a human transitions a Linear Issue to
Canceled while mac has it `running` — mac applies its state machine
rules and may cancel the running task with `metadata.cancel_reason =
"linear_closed"`. Mac does NOT override its own terminal states
because Linear changed; once mac says `completed`, that's final.

## 6. Schema changes

One new table, additive, mirrors `beads_repositories`:

```sql
CREATE TABLE IF NOT EXISTS linear_workspaces (
    id                     TEXT    PRIMARY KEY,
    name                   TEXT    NOT NULL UNIQUE,
    workspace_url          TEXT    NOT NULL,
    api_key_secret_ref     TEXT    NOT NULL,
    team_filter            TEXT    NOT NULL DEFAULT '[]',
    active_states          TEXT    NOT NULL DEFAULT '["Todo","In Progress"]',
    terminal_states        TEXT    NOT NULL DEFAULT '["Done","Canceled"]',
    state_map              TEXT    NOT NULL DEFAULT '{}',
    enabled                INTEGER NOT NULL DEFAULT 1,
    poll_interval_seconds  INTEGER NOT NULL DEFAULT 30,
    last_polled_at         TEXT,
    last_imported_at       TEXT,
    last_error             TEXT,
    tenant_id              TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    metadata               TEXT    NOT NULL DEFAULT '{}',
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linear_workspaces_enabled
    ON linear_workspaces (enabled, last_polled_at);
CREATE INDEX IF NOT EXISTS idx_linear_workspaces_tenant
    ON linear_workspaces (tenant_id);
```

`api_key_secret_ref` is a pointer into mac's `secrets` table — the
actual API key value is encrypted at rest by mac's existing Fernet
mechanism. The bridge dereferences via `secrets_service` on each
invocation; ephemeral in memory only.

Existing `project_items` is reused unchanged. New rows for Linear
items use `source = "linear:<workspace_id>"` and
`external_id = <linear_identifier>` (e.g. `ENG-421`). The existing
`UNIQUE(source, external_id)` index gives us idempotent re-import.

`tenants` gets one new column via `_ensure_column`:
```
tracker_kind TEXT NOT NULL DEFAULT 'beads'  -- 'beads' | 'linear'
```

The default preserves existing-tenant behavior (beads). Operators flip
it explicitly when migrating a tenant to Linear.

## 7. Dispatch — branching, not abstraction (v2)

**v2: no `TrackerBackend` Protocol.** Reviewers flagged that
extracting one would require behavior-preserving refactor of ~1500
lines of `ControlPlane` methods in `services.py` (the real home of
beads logic — `register_beads_repository` at services.py:7472,
`poll_beads_repositories` at services.py:7580,
`_sync_beads_database` at services.py:8409,
`_sync_beads_transition_ledger` at services.py:9849, etc.). The
beads code has behaviors (Dolt sync, JSONL drift detection, repo
checkout, required_capabilities filtering) that don't fit cleanly
into the symmetric `Protocol` I originally sketched. Forcing them
into one would either leak through the abstraction or break existing
behavior.

In v1 we accept ugliness in exchange for safety: dispatch via simple
helper that branches on `task.metadata.origin.tracker`. There are
4-6 dispatch sites total — one for outbox lifecycle comments, one
for the `mac_create_task` plugin endpoint, one each for
`mac_comment_on_task` / `mac_update_task_state` / `mac_link_pr_to_task`
/ etc.

```python
# src/mac/tracker_dispatch.py — new file, ~50 lines

def tracker_kind_for_task(task: Task) -> str:
    """Return 'beads' or 'linear' based on origin metadata.
    Default 'beads' for backward compatibility with rows imported
    before this column existed."""
    origin = (task.metadata or {}).get("origin") or {}
    return origin.get("tracker", "beads")

def tracker_kind_for_tenant(store: Store, tenant_id: str) -> str:
    row = store.query_one(
        "SELECT tracker_kind FROM tenants WHERE id = ?", (tenant_id,)
    )
    return (row and row["tracker_kind"]) or "beads"

def emit_lifecycle_comment(cp, task, event_type, payload):
    if tracker_kind_for_task(task) == "linear":
        cp.linear_bridge.emit_lifecycle_comment(task, event_type, payload)
    else:
        cp._append_beads_ledger_comment(task, event_type, payload)

def create_issue_for_tenant(cp, tenant_id, title, description, **opts):
    if tracker_kind_for_tenant(cp.store, tenant_id) == "linear":
        return cp.linear_bridge.create_issue_for_tenant(tenant_id, title, description, **opts)
    else:
        return cp.import_project_item(...)  # existing beads path
```

That's the entire dispatch layer. Five lines of branching per
operation, no new abstraction.

The proper `TrackerBackend` Protocol is **deferred to a follow-up
refactor** (let's call it Phase L9) that happens AFTER both bridges
have run in production for a few weeks and we know what the right
shape actually is. By then it's a defensible extraction, not a guess.

### What this means for the spec

- Phase L4 (originally "TrackerBackend abstraction + tenant binding")
  becomes "tenant binding only" — `tenants.tracker_kind` column +
  dispatch helpers. No refactor of beads.
- Mac-api endpoints look up `task.metadata.origin.tracker` directly.
- Plugin tool handlers route through `tracker_dispatch.*` helpers.
- BeadsBridgeService stays untouched. The work in this file becomes
  zero.

## 8. LinearClient

`src/mac/linear_client.py` — thin GraphQL wrapper, no state.

```python
class LinearClient:
    def __init__(self, api_key: str, *, endpoint: str = "https://api.linear.app/graphql"):
        ...

    def list_teams(self) -> list[JsonDict]: ...

    def list_workflow_states(self, team_id: str) -> list[JsonDict]: ...

    def list_issues_updated_since(
        self,
        team_id: str,
        since_iso: str,
        *,
        active_state_names: Sequence[str] = (),
        page_size: int = 100,
    ) -> Iterator[JsonDict]: ...

    def get_issue(self, identifier: str) -> Optional[JsonDict]: ...

    def create_issue(
        self,
        team_id: str,
        title: str,
        description: str = "",
        *,
        state_id: Optional[str] = None,
        priority: Optional[int] = None,
        label_ids: Sequence[str] = (),
    ) -> JsonDict: ...

    def create_comment(self, issue_id: str, body: str) -> JsonDict: ...

    def update_issue_state(self, issue_id: str, state_id: str) -> JsonDict: ...

    def attach_url(
        self,
        issue_id: str,
        url: str,
        title: Optional[str] = None,
    ) -> JsonDict: ...
```

Implementation:

- Uses Python stdlib `urllib` to avoid adding an HTTP dependency.
  (`httpx` is already in `[postgres]` extra; reusing it is fine but
  not required.)
- `Authorization: <api_key>` header (Linear doesn't use `Bearer`).
- Retries on 5xx with exponential backoff, max 3 attempts.
- Respects `Retry-After` on 429.
- Returns Linear's GraphQL response wrapped (errors surface as a
  `LinearApiError` exception with the response body attached).
- All operations are single-statement GraphQL; pagination uses
  cursor-based GraphQL connections.

No persistent state. One instance per workspace, held by the bridge.

## 9. LinearBridgeService

`src/mac/linear_bridge_service.py` — implements `TrackerBackend`.

Field-for-field analog of `BeadsBridgeService` minus the Dolt /
checkout / JSONL-drift complexity:

```python
class LinearBridgeService:
    def __init__(self, store: Store, secrets: SecretsService): ...

    # ── workspace management ─────────────────────────────────

    def register_workspace(
        self,
        name: str,
        workspace_url: str,
        api_key_secret_ref: str,
        *,
        team_filter: Sequence[str] = (),
        active_states: Sequence[str] = ("Todo", "In Progress"),
        terminal_states: Sequence[str] = ("Done", "Canceled"),
        poll_interval_seconds: int = 30,
        tenant_id: Optional[str] = None,
    ) -> LinearWorkspace: ...

    def list_workspaces(
        self, *, tenant_id: Optional[str] = None,
    ) -> list[LinearWorkspace]: ...

    def disable_workspace(self, name: str) -> None: ...

    def resolve_state_map(self, name: str) -> Dict[str, str]: ...
    # Calls list_workflow_states for each configured team; caches in
    # linear_workspaces.state_map so future state transitions don't
    # need the lookup.

    # ── poll + import ────────────────────────────────────────

    def poll_workspace(
        self, name: str, *, force: bool = False,
    ) -> PollResult: ...

    # ── TrackerBackend impl ──────────────────────────────────

    def poll(self, workspace_id: str) -> PollResult: ...

    def emit_lifecycle_comment(
        self, task: Task, event_type: str, payload: JsonDict,
    ) -> None: ...

    def create_issue(self, workspace_id, title, description, ...) -> CreatedIssue: ...

    def attach_pr(self, task: Task, pr_url: str, title=None) -> None: ...

    def update_state(self, task: Task, target_state: str, actor: str) -> None: ...
```

### 9.1 Poll cycle

For each `enabled=1` workspace where `last_polled_at` is older than
`poll_interval_seconds`:

1. Resolve the API key via `secrets_service`.
2. For each `team_id` in `team_filter`:
   - GraphQL query: `Issue` where `team.id == <team_id>
     AND updatedAt > <last_polled_at>`, cursor-paginated.
3. For each issue returned:
   - Compute deterministic mac task id:
     `task_linear_<identifier_lowercased>`
     (e.g. `ENG-421` → `task_linear_eng_421`).
   - Upsert `tasks` row via `INSERT … ON CONFLICT(id) DO UPDATE`,
     setting `metadata.origin = {tracker:"linear", workspace_id,
     external_id:<identifier>, url}`.
   - Upsert `project_items` with
     `(source = "linear:<workspace_id>", external_id = identifier,
     task_id = task_id)`.
   - If the Linear state is in `terminal_states` and the mac task is
     not in a terminal mac state, schedule a reconcile step (§9.3).
4. Persist `last_polled_at = utcnow()`.
5. On any GraphQL error, persist `last_error` and surface an
   `integration_findings` row (severity `warning`).

The first poll for a fresh workspace pulls all issues in
`active_states` (no `since_iso`). Subsequent polls are incremental.

### 9.2 Lifecycle comment emission

`emit_lifecycle_comment` is called by the outbox consumer when a
Linear-origin task transitions state. It posts a Linear comment in
the same pipe-delimited format mac's beads bridge already uses
(verified against `services.py:9625`):

```
mac-ledger v1 | task=task_abc | event=state_running | actor=agent_xyz | lease=lease_xyz
mac-ledger v1 | task=task_abc | event=state_needs_review | actor=agent_xyz | evidence=evidence_xxx
mac-ledger v1 | task=task_abc | event=published | actor=agent_xyz | target=git://main | pr=<url>
mac-ledger v1 | task=task_abc | event=state_failed | actor=agent_xyz | reason=verification_failed
```

Format rationale: operator continuity with beads (same parser works
for both), grep-friendly, machine-parseable. Linear renders pipe
characters fine in markdown comments.

Mirrored events (same set beads uses; see
`production-deployment.md` §Beads Human Ledger): `imported`,
`claimed`, `state_running`, `state_needs_review`, `state_reviewing`,
`state_failed`, `state_cancelled`, `state_open`, `evidence_added`,
`review_requested`, `review_completed`, `published`, `retry_reopened`,
`retry_exhausted`.

Lease renewals are NOT mirrored (would flood the issue thread).

### 9.3 Terminal-state reconciliation

When a human moves a Linear Issue to a terminal state while mac has
the corresponding task running:

1. mac runs the existing `cancel_task` path with
   `reason="linear_closed"` and `actor="linear:<external_id>"`.
2. The active lease is invalidated, the running Job receives SIGTERM
   (mac-k8s-runner sees the cancelled state on the next poll).
3. A `mac-ledger v1: state_cancelled — closed in Linear` comment goes
   back to the same Linear Issue for audit clarity.

If mac is already in a terminal state when Linear closes the Issue,
no action — mac's terminal state wins.

## 10. Auto-register on mac-api startup

Following the same declarative pattern used for the K8s `register-mac-identity`
init container.

`mac.api.create_app` startup hook calls
`_maybe_register_linear_workspace_from_env`. Env contract:

| Variable | Purpose | Required when |
|---|---|---|
| `MAC_LINEAR_BRIDGE_ENABLED` | On-switch; `"1"` to enable | always |
| `LINEAR_API_KEY` | Linear API token (from mac-secret) | bridge enabled |
| `MAC_LINEAR_WORKSPACE_NAME` | Workspace name in mac's table | optional (default `default`) |
| `MAC_LINEAR_WORKSPACE_URL` | `linear.app/<slug>` for context | bridge enabled |
| `MAC_LINEAR_TEAMS` | CSV team slugs to poll, e.g. `eng,ops` | bridge enabled |
| `MAC_LINEAR_TENANT_ID` | Bind this workspace to a tenant | optional |
| `MAC_LINEAR_ACTIVE_STATES` | JSON array | optional (default `["Todo","In Progress"]`) |
| `MAC_LINEAR_TERMINAL_STATES` | JSON array | optional (default `["Done","Canceled"]`) |
| `MAC_LINEAR_POLL_INTERVAL_SECONDS` | Default `30` | optional |
| `MAC_LINEAR_BRIDGE_ON_HEARTBEAT` | `"1"` to drive polling from hub heartbeat | optional |

Implementation (v2 — rewritten to use real `SecretsService` API):

```python
def _maybe_register_linear_workspace_from_env(cp: ControlPlane) -> None:
    if not _env_bool("MAC_LINEAR_BRIDGE_ENABLED"):
        # v2: env-driven disable — reconcile DOWN. If a workspace was
        # auto-registered previously, mark it disabled so polling stops.
        cp.linear_bridge.disable_env_managed_workspace_if_present()
        return
    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        log.warning("MAC_LINEAR_BRIDGE_ENABLED=1 but LINEAR_API_KEY unset; skipping")
        return

    name = os.environ.get("MAC_LINEAR_WORKSPACE_NAME", "default")
    workspace_url = os.environ.get("MAC_LINEAR_WORKSPACE_URL", "").strip()
    teams = _csv(os.environ.get("MAC_LINEAR_TEAMS", ""))
    if not workspace_url or not teams:
        log.error("MAC_LINEAR_BRIDGE_ENABLED=1 but workspace_url/teams not configured")
        return

    # v2: secret upsert via get-or-create-or-rotate. SecretsService has
    # create_secret (rejects if name exists) and rotate_secret (rejects
    # if name doesn't exist), so we branch on existence.
    secret_name = f"linear-{name}-api-key"
    existing = cp.secrets_service.find_by_name(secret_name)  # NEW helper
    api_key_hash = sha256(api_key.encode()).hexdigest()
    if existing is None:
        secret = cp.secrets_service.create_secret(
            name=secret_name,
            value=api_key,
            scopes={"system": "linear", "workspace": name},
            created_by="mac-startup",
        )
        secret_id = secret.id
    else:
        secret_id = existing.id
        # Only rotate when the env-provided key materially changed.
        # Hash comparison avoids gratuitous rotation audit churn on
        # every restart. Note: requires a stored hash; either add
        # secrets.value_hash column (preferred) or reveal+compare
        # (uses an audit slot, less elegant).
        stored_hash = (existing.metadata or {}).get("value_sha256")
        if stored_hash != api_key_hash:
            cp.secrets_service.rotate_secret(
                secret_id, api_key, actor="mac-startup",
                metadata_patch={"value_sha256": api_key_hash},
            )

    # v2: explicit upsert with config-hash. If env config differs from
    # DB row, env wins; emit a finding so operators see drift.
    desired = {
        "workspace_url": workspace_url,
        "api_key_secret_ref": secret_id,
        "team_filter": teams,
        "active_states": _json_arg("MAC_LINEAR_ACTIVE_STATES", ["Todo", "In Progress"]),
        "terminal_states": _json_arg("MAC_LINEAR_TERMINAL_STATES", ["Done", "Canceled"]),
        "poll_interval_seconds": int(os.environ.get("MAC_LINEAR_POLL_INTERVAL_SECONDS", "30")),
        "tenant_id": os.environ.get("MAC_LINEAR_TENANT_ID") or None,
        "enabled": 1,
        "env_managed": 1,  # marker: this row is owned by env, not CLI
    }
    existing_ws = cp.linear_bridge.find_workspace_by_name(name)
    if existing_ws is None:
        cp.linear_bridge.register_workspace(name=name, **desired)
    else:
        diff = _diff_workspace_config(existing_ws, desired)
        if diff:
            cp.linear_bridge.update_workspace(name=name, **desired)
            cp.record_integration_finding(
                source_id=existing_ws.id, source_kind="linear_workspace",
                finding_type="config.env_overrode_db",
                severity="info", title="Linear workspace config updated from env",
                detail={"changed_fields": list(diff.keys())},
            )

    # Refresh state_map every startup. Cheap (one GraphQL call per team).
    try:
        cp.linear_bridge.refresh_state_map(name)
    except Exception as exc:
        log.warning("Linear state map resolution failed: %s", exc)
        cp.record_integration_finding(
            source_id=name, source_kind="linear_workspace",
            finding_type="state_map.refresh_failed", severity="warning",
            title="Linear state_map refresh failed at startup",
            detail={"error": str(exc)},
        )

    log.info("auto-registered Linear workspace %s (teams=%s)", name, teams)
```

**New helpers required** (small, low-risk additions):

- `SecretsService.find_by_name(name) -> Optional[Secret]` — read-only,
  no audit side effect. Returns `None` when absent.
- `SecretsService.rotate_secret` already exists; v2 just uses it
  through the documented path.
- `linear_bridge.find_workspace_by_name`, `update_workspace`,
  `disable_env_managed_workspace_if_present` — bridge-level helpers.

Multi-replica safety: `linear_workspaces.name` is `UNIQUE`. Concurrent
replicas race for the INSERT or UPDATE; SQL atomicity ensures exactly
one final row. The `desired` map is identical across replicas at any
moment, so racing replicas converge regardless of order.

The `env_managed=1` marker keeps env-managed workspaces separate from
CLI-managed ones. Operators using the imperative CLI for additional
workspaces get rows with `env_managed=0` that the auto-register
function leaves alone.

## 11. mac-api endpoints

New routes added to `api.py`. All require existing auth (`admin` for
workspace mgmt; `agent` for runtime calls).

```
POST   /bridges/linear/workspaces                  register
GET    /bridges/linear/workspaces                  list
GET    /bridges/linear/workspaces/{name}           detail
POST   /bridges/linear/workspaces/{name}/poll      manual trigger
DELETE /bridges/linear/workspaces/{name}           disable
GET    /bridges/linear/workspaces/{name}/teams     list Linear teams
GET    /bridges/linear/workspaces/{name}/states    list workflow states

POST   /tasks/{id}/linear/comment                  body: {body}
POST   /tasks/{id}/linear/state                    body: {target_state}
POST   /tasks/{id}/linear/attach                   body: {url, title?}
```

The `/tasks/{id}/linear/*` routes are tracker-agnostic by design — they
look up `task.metadata.origin.tracker` and route through
`get_tracker_for_origin`. Calling them on a beads-origin task returns
422 (`tracker mismatch — task is beads-origin`).

The existing `POST /hermes-instances/{id}/tasks` endpoint gains a
Linear-routing branch: when the calling Hermes instance's tenant is
Linear-backed, mac-api creates the Linear Issue first, then the mac
task row, in one transaction. The response includes both ids.

## 12. CLI surface

Mirroring beads:

```
mac bridge linear register <name> --workspace-url ... --api-key-secret <id>
                                  --team <team> [--team <team>...]
                                  [--tenant <tenant_id>]
                                  [--active-states JSON] [--terminal-states JSON]
                                  [--poll-interval-seconds 30]
mac bridge linear list [--tenant <tenant_id>]
mac bridge linear show <name>
mac bridge linear poll [--workspace <name>] [--force]
mac bridge linear disable <name>
mac bridge linear list-teams <name>
mac bridge linear list-states <name>
mac bridge linear resolve-state-map <name>           # refresh state_map cache

mac tenant set-tracker <tenant_id> <beads|linear> [--workspace <name>]
```

And `mac-hermes`:

```
mac-hermes linear-workspaces
mac-hermes register-linear-workspace ...
mac-hermes poll-linear-workspaces ...
```

The imperative paths exist as escape hatches for multi-workspace
setups (one per tenant) that env-based auto-register can't express,
and for ad-hoc operations (manual re-poll, list states after a Linear
workflow change).

## 13. Hermes plugin updates (v2)

The mac-hermes-plugin gains **six** new tools (was three; architect
review flagged that comment/state/link_pr alone leaves the LLM
without normal triage operations).

| Tool | mac-api route | Purpose |
|---|---|---|
| `mac_comment_on_task` | POST `/tasks/{id}/linear/comment` | Free-form comment the human will read |
| `mac_update_task_state` | POST `/tasks/{id}/linear/state` | Move issue to a named workflow state |
| `mac_link_pr_to_task` | POST `/tasks/{id}/linear/attach` | Attach a PR URL with optional title |
| `mac_set_task_priority` | POST `/tasks/{id}/linear/priority` | Adjust priority when work is more/less urgent than initially scoped |
| `mac_add_task_label` | POST `/tasks/{id}/linear/label` | Add a label (e.g. `needs-human-review`, `security`, `blocked`) — primary triage signal in Linear |
| `mac_assign_task` | POST `/tasks/{id}/linear/assignee` | Assign to a Linear user when escalating to a human |

Each route enforces tenant scope via `principal.assert_tenant`. Each
route returns 422 with `tracker mismatch — task is beads-origin` when
called on a non-Linear task (the LLM tool description tells the LLM
to handle 422 gracefully rather than retry).

Server-side dispatch via `tracker_dispatch.*` helpers (§7). The LLM
never picks the tracker; the task's origin metadata does.

### 13.1 Files touched per tool

**v2 correction** — `provides_tools` lives in `plugin/plugin.yaml`,
NOT `plugin/manifest.py`. Adding each tool requires edits to:

1. `plugin/plugin.yaml` — append the tool name to `provides_tools`
2. `plugin/manifest.py` — append a `ToolSpec` to `TOOLS`
3. `plugin/schemas.py` — add a JSON Schema for the args
4. `plugin/__init__.py` — potentially add body-shaping logic if the
   tool needs to translate LLM-friendly args into the API shape (most
   tools route through `_handle_tool` and don't need this)

After source edits: rebuild `mac-hermes-plugin` image, push, bump
tag in `home-ops/components/ai/hermes-agent/values.yaml`, ArgoCD
syncs, hermes-agent re-rolls and the new tools become available to
the LLM at next session start.

### 13.2 mac_create_task routing

Existing `mac_create_task` LLM-facing schema is unchanged. The
mac-api endpoint behind it (`/hermes-instances/{id}/tasks`) gains a
Linear-routing branch based on the calling Hermes instance's tenant
`tracker_kind`:

- `tracker_kind = 'beads'` → existing behavior (direct mac task
  creation, optionally bridged to beads via the bridge poller)
- `tracker_kind = 'linear'` → mac-api creates the Linear Issue via
  `LinearClient.create_issue`, then creates the mirroring mac task
  row in the same transaction with `metadata.origin = {tracker:
  'linear', workspace_id, external_id, url, identifier}`

The response payload gains `linear_identifier` and `linear_url`
fields when routed through Linear, both `None` for beads. The
plugin's `mac_create_task` handler forwards them to the LLM so it
can speak the friendly `ENG-421` identifier and the URL.

## 14. Per-tenant tracker selection

`tenants.tracker_kind` (column added via `_ensure_column`):

- `'beads'` (default) → tasks created from this tenant flow to beads
- `'linear'` → tasks flow to Linear

A tenant bound to `'linear'` must also have at least one
`linear_workspaces.tenant_id = <tenant_id>` row (or
`tenant_id = NULL` shared workspaces — TBD, see §17).

CLI: `mac tenant set-tracker <tenant_id> linear --workspace acme-eng`.

The validation:

- Setting `tracker_kind='linear'` with no matching workspace row is
  rejected at the mac-api layer with 422.
- Setting `tracker_kind='beads'` with no `beads_repositories` is
  permitted (a tenant can have a tracker mode without a configured
  source, useful during initial setup).

## 15. Operational concerns

### 15.1 Rate limits

Linear's GraphQL API has soft limits documented at ~1500 req/min for
authenticated users. At 30s poll cadence with one workspace and a
handful of teams, normal polling load is well under 10 req/min. The
risk is bursty agent-driven writes (many concurrent
`mac_comment_on_task` calls during a fleet operation). Mitigation:

- `LinearClient` honors `Retry-After` on 429
- The outbox consumer is single-threaded per backend so comments are
  serialized
- Bridge logs rate-limit responses to observability stream for visibility

If we ever hit a real rate-limit problem, the answer is webhooks
(Phase L4) which eliminate the poll traffic entirely.

### 15.2 Retry strategy (v2)

**v2: the existing outbox does NOT have backoff or auto-retry — this
must be fixed in Phase L0 before any Linear work ships.**

Today's `task_transition_outbox` schema
(`src/mac/data/postgres/schema.sql:206`):

```sql
CREATE TABLE task_transition_outbox (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
```

No `next_attempt_at`, no `max_attempts`, no `last_error`, no
`dead_letter` status. The drain loop
(`task_lifecycle.py:list_outbox(status="pending")`) picks all pending
rows; `mark_outbox_failed` flips status to `"failed"` which removes
the row from future drains. There is no retry — a single Linear API
hiccup permanently strands a lifecycle comment.

Beads escapes this today only because `_run_bd_for_task` (services.py:
9549) silently swallows exceptions and returns `False` — the outbox
drain sees no exception, marks the row processed, and the failed
ledger comment is silently lost. (Pre-existing bug, separate fix.)

Phase L0 extends the outbox to support proper retry:

```sql
ALTER TABLE task_transition_outbox
    ADD COLUMN next_attempt_at TEXT,                     -- NULL = ready now
    ADD COLUMN last_error TEXT,
    ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5;
-- 'pending' | 'processing' | 'processed' | 'dead_letter'
-- ('failed' is renamed to 'dead_letter' via migration data step)
```

Drain query becomes:
```sql
SELECT * FROM task_transition_outbox
 WHERE status = 'pending'
   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
 ORDER BY created_at
 LIMIT ?
```

`mark_outbox_failed(id, error, retriable: bool)` semantics:
- If retriable AND `attempts < max_attempts`: increment `attempts`,
  set `next_attempt_at = now + backoff(attempts)`, set `last_error`,
  status stays `pending`.
- If retriable AND `attempts >= max_attempts`: status → `dead_letter`,
  set `last_error`, emit `integration_findings` row.
- If NOT retriable (permanent error, e.g. 4xx other than 429):
  status → `dead_letter` immediately, emit finding.

`backoff(attempts)` is exponential with jitter:
`min(60 * 2^attempts, 3600) * (0.5 + random())`. Max 1h between
retries.

For Linear specifically:
- 429 with `Retry-After: N` → retriable, override
  `next_attempt_at = now + N`.
- 5xx → retriable, normal backoff.
- 401/403 → not retriable, dead-letter immediately + alert.
- 404 (issue deleted from Linear) → not retriable, mark task
  origin-orphaned, emit finding.
- 422 (validation error from Linear) → not retriable, dead-letter +
  surface to operator.
- Network timeouts → retriable.

This change benefits beads too: once the swallowed-exceptions bug is
fixed in `_run_bd_for_task`, beads ledger comments get the same
retry behavior for free.

### 15.2.1 Migration plan for L0

The schema change is additive (new columns) and the status enum gains
`dead_letter` but keeps `failed` as a deprecated alias for one
release cycle. Concrete steps:

1. Add `next_attempt_at`, `last_error`, `max_attempts` columns
   (additive, both backends via `_ensure_column` + schema.sql edit).
2. Backfill: `UPDATE task_transition_outbox SET max_attempts = 5
   WHERE max_attempts IS NULL` (no-op given DEFAULT, but explicit).
3. Update `TaskLedgerService.mark_outbox_failed` to take a
   `retriable` bool and apply the new state-transition logic.
4. Update the drain query everywhere it appears.
5. Backfill existing `status='failed'` rows: `UPDATE … SET
   status='dead_letter' WHERE status='failed'`.

No data loss; no downtime; old code reading the table without the
new columns is fine because they're nullable.

### 15.3 Drift detection

The 30s poll catches Linear-side changes. To catch mac-vs-Linear drift
(e.g. mac thinks task is `running` but Linear shows `Done`), the poll
loop performs a periodic reconcile pass: every Nth tick (default 10,
so every ~5 min), it pulls the FULL current state of all open
issues in `team_filter` and compares against mac's view. Discrepancies
become `integration_findings` rows for operator review.

### 15.4 Secret lifecycle

`LINEAR_API_KEY` is stored encrypted in mac's `secrets` table (Fernet,
same as other mac secrets). The on-disk Secret in K8s is the source;
mac re-imports on every auto-register. Rotation = update 1Password →
ExternalSecret resyncs → mac-api restart re-imports.

The API key is bound to one Linear bot user. Operators should create
a dedicated bot user (not a person's personal token) so leaves /
permission changes don't break the bridge.

## 16. Testing strategy

### 16.1 Unit tests (no live Linear)

- `LinearClient`: HTTP layer mocked with `httpx.MockTransport`,
  asserts the GraphQL queries we send and the error/retry handling.
  ~15 tests.
- `LinearBridgeService.poll_workspace`: fake `LinearClient` returning
  scripted issue lists, asserts `tasks` and `project_items` upserts
  happen idempotently across re-runs. ~10 tests.
- `LinearBridgeService.emit_lifecycle_comment`: fake client,
  asserts the right markdown body shape per event type. ~10 tests.
- `TrackerBackend` protocol conformance: both `BeadsBridgeService` and
  `LinearBridgeService` must pass the same shared contract test
  suite. ~15 tests.

### 16.2 Live-Linear tests (gated)

Following the `pytest.mark.postgres` pattern:

- `pytest.mark.linear` marker
- `MAC_TEST_LINEAR_API_KEY` + `MAC_TEST_LINEAR_TEAM` env vars
- Tests create/list/comment/delete issues in a dedicated test team
- Skipped by default; CI enables them only on protected branches with
  a sandbox Linear workspace
- ~10 tests covering the full create → poll → import → comment back
  → state transition loop

### 16.3 End-to-end test in dev cluster

Manual: register a sandbox Linear workspace, create an Issue, watch
it appear in mac within 30s, claim it from the runner, see lifecycle
comments land on the Linear Issue, close the Issue from Linear, watch
mac cancel the running task.

## 17. Open questions

1. **Workspace-tenant cardinality.** Can a single
   `linear_workspaces` row serve multiple tenants (set
   `tenant_id = NULL`, partition by Linear team labels)? Or strictly
   1:1? Spec assumes 1:N (one workspace, many tenants via team
   filter) since it's strictly more expressive. Worth confirming
   before code.

2. **State map refresh cadence.** State_map is cached on first
   register + refreshed manually via CLI. Should it auto-refresh on
   every poll (cheap, one extra GraphQL call) or only when the bridge
   sees an unknown state name? Spec assumes the latter (lazy
   refresh).

3. **Backfill of beads-origin tasks.** Spec says "no backfill, new
   tasks only". Should we offer an explicit CLI `mac bridge linear
   import-from-beads` for operators who want it? Probably yes as a
   separate utility; not part of v1.

4. **Plugin tool granularity.** Spec exposes three new tools
   (`comment`, `update_state`, `link_pr`). Symphony exposes one
   tool (`linear_graphql` — raw GraphQL) and lets the LLM compose.
   Trade-off: three tools is safer + more obvious to the LLM; one
   tool is more powerful + more error-prone. Spec picks three; flag
   for review.

5. **Workflow runs.** Mac's `workflows` table models DAG-of-tasks.
   Linear has parent-child Issues. Should DAG workflows mirror as
   Linear parent + sub-issues? Defer to a Phase L5 — first version
   handles flat issues only.

## 18. Risks

| Risk | Mitigation |
|---|---|
| Linear API key leakage | Key encrypted in mac.secrets; never logged; rotated via 1Password |
| Linear API outage breaks mac entirely | Bridge failures are caught; mac continues with cached state; outbox retries writes |
| Drift between mac state and Linear state | Periodic reconcile pass (§15.3); `integration_findings` surface drift |
| TrackerBackend retrofit of beads regresses existing flows | Shared contract test suite; both implementations must pass identical tests |
| LLM picks wrong tool (e.g. comment instead of update_state) | Clear tool descriptions; mac-api validates intent server-side |
| Multi-replica race on auto-register | `linear_workspaces.name UNIQUE` enforces single winner; ON CONFLICT no-op |
| State_map staleness if operator renames Linear states | CLI `resolve-state-map` to refresh; bridge logs warning on unknown state |
| Beads users feel pressured to migrate | Beads stays fully supported; no deprecation message |

## 19. Implementation phasing (v2)

Each phase is independently mergeable; later phases assume earlier
phases shipped.

### Phase L0 — Extend the outbox for proper retry

**Prerequisite for all Linear work.** Without this, the first Linear
API blip silently strands lifecycle comments.

- Add `next_attempt_at`, `last_error`, `max_attempts` columns to
  `task_transition_outbox`
- Add `dead_letter` status; data-migrate existing `'failed'` rows
- Rewrite `TaskLedgerService.mark_outbox_failed` to take a
  `retriable: bool` and apply backoff
- Update drain query to honor `next_attempt_at`
- Add `SecretsService.find_by_name(name) -> Optional[Secret]` helper
  (needed by auto-register; clean to add here)
- Tests: outbox round-trips with simulated 429/5xx/4xx; backoff
  monotonicity; dead_letter triggers `integration_findings`

After this phase: outbox is production-grade for any backend. Beads
also benefits (once the silent-exception-swallow bug in
`_run_bd_for_task` is fixed separately).

### Phase L1 — Core LinearClient + workspace table

- `src/mac/linear_client.py` with HTTP/GraphQL ops
- `linear_workspaces` table migration (idempotent CREATE TABLE IF NOT EXISTS)
- Unit tests for client + mocked GraphQL transport
- CLI: `mac bridge linear list-teams`, `list-states`
  (read-only operations, no behavior change in mac)

No production effect — operators can probe their Linear workspace
without mac doing anything new.

### Phase L2 — LinearBridgeService (poll + import)

- `LinearBridgeService` with `register_workspace`, `list_workspaces`,
  `poll_workspace`
- `tasks.metadata.origin.tracker = "linear"` import path
- CLI: `mac bridge linear register|list|show|poll|disable`
- Hub-heartbeat poll trigger (`MAC_LINEAR_BRIDGE_ON_HEARTBEAT=1`)
- Unit tests for poll cycle

After this phase: an operator can register a Linear workspace and see
issues import into mac as tasks. No outbound writes yet.

### Phase L3 — Lifecycle comments out (outbox)

- `LinearBridgeService.emit_lifecycle_comment` wired into
  `task_transition_outbox` consumer
- Linear-origin tasks emit `mac-ledger v1:` comments on state
  transitions

After this phase: round-trip works — issues flow Linear → mac → Linear
(as comments).

### Phase L4 — Tenant binding (v2: no Protocol)

- `tenants.tracker_kind` column added via `_ensure_column`
- `src/mac/tracker_dispatch.py` — thin helpers (~50 lines):
  `tracker_kind_for_task`, `tracker_kind_for_tenant`,
  `emit_lifecycle_comment`, `create_issue_for_tenant`
- CLI: `mac tenant set-tracker <tenant> <beads|linear> [--workspace <name>]`
- `mac_create_task` plugin endpoint dispatches via the helpers
- Validation: setting `tracker_kind='linear'` requires either
  matching `linear_workspaces.tenant_id` row OR a shared workspace
  (`tenant_id IS NULL`)

After this phase: per-tenant tracker selection works. No retrofit of
beads code; no `TrackerBackend` Protocol in this phase.

A proper `TrackerBackend` Protocol extraction is **deferred to Phase
L9** (not in v1 scope) — done after both bridges have run in
production for several weeks and the right abstraction shape is
evidence-driven rather than guessed.

### Phase L5 — Auto-register from env

- `_maybe_register_linear_workspace_from_env` startup hook
- Env-var contract documented in production-deployment.md
- home-ops side: env config + ExternalSecret update

After this phase: declarative deploy; no imperative bootstrap needed
for the single-workspace case.

### Phase L6 — Hermes plugin extensions

- `mac_comment_on_task`, `mac_update_task_state`, `mac_link_pr_to_task`
  added to `plugin/manifest.py`
- New mac-api endpoints
- Plugin image rebuild + home-ops tag bump

After this phase: agents can write back to Linear during conversations.

### Phase L7 — Terminal-state reconciliation

- Periodic reconcile pass per §15.3
- `integration_findings` for drift detection
- Cancellation flow when Linear closes a running Issue

After this phase: bidirectional state sync is robust.

### Phase L8 — Docs + operator readiness

- `production-deployment.md` §Linear Bridge (mirrors §Beads Bridge)
- `linear-bridge-runbook.md`: rotation, troubleshooting, common
  failure modes
- README updates

Phases L1-L3 are minimum viable. Phases L4-L8 polish + production.

## 21. Observability (added in v2)

Each new module surfaces metrics, logs, and findings through mac's
existing `ObservabilityService` so operators see Linear health in the
same dashboard surface as the rest of mac.

### 21.1 Metrics

| Metric | Unit | Source |
|---|---|---|
| `linear.poll.duration_ms` | ms | per `poll_workspace` invocation |
| `linear.poll.issues_imported` | count | per poll (new + updated) |
| `linear.poll.success` | bool | per poll |
| `linear.graphql.duration_ms` | ms | per `LinearClient` call, with operation_name in detail |
| `linear.graphql.rate_limit_remaining` | count | from `X-RateLimit-Remaining` header |
| `linear.outbox.queue_depth` | count | gauge — pending Linear-targeted rows |
| `linear.outbox.dead_letter_total` | counter | running count of dead-lettered rows |

### 21.2 Logs

- `linear.workspace.registered` — info, on register/update
- `linear.workspace.disabled` — info, when env reconciles down
- `linear.poll.failed` — error, with truncated error message
- `linear.write.failed` — warn, with retry-eligibility
- `linear.write.dead_lettered` — error, with `task_id`,
  `event_type`, `last_error`

### 21.3 Findings (via `record_integration_finding`)

`source_kind = "linear_workspace"`, with the following
`finding_type`s:

- `config.env_overrode_db` (info) — startup detected env drift
- `state_map.refresh_failed` (warning) — bridge can't reach Linear
- `poll.stalled` (critical) — `last_polled_at` older than
  `5 × poll_interval_seconds`
- `auth.invalid` (critical) — 401/403 from Linear
- `state.unknown_in_linear` (warning) — Linear has a state not in
  `active_states`/`terminal_states` config
- `task.linear_terminal_while_running` (info) — reconcile detected
  Linear close mid-run; mac is cancelling

### 21.4 Alerts (operator-defined; spec lists the conditions)

- `poll.stalled` for >10 min → page
- `auth.invalid` → page
- `outbox.dead_letter_total` increases >0 in 5min → notify

## 22. Tenant deletion semantics (added in v2)

Today, `linear_workspaces.tenant_id REFERENCES tenants(id) ON DELETE
CASCADE` would cascade the workspace row when a tenant is deleted.
That's the right default but leaves:

- The encrypted API key in `secrets` — orphaned. Cleanup required.
- Already-imported mac tasks with `metadata.origin.tracker = "linear"`
  pointing at the deleted workspace_id — orphaned. Lifecycle comments
  would fail.
- The `project_items` rows under `source = "linear:<workspace_id>"` —
  cascade-deleted via `task_id` FK when the parent task is deleted,
  but the project_items survive if the task survives.

v2 specifies the cleanup contract:

1. `cp.delete_tenant(tenant_id)` raises if any
   `linear_workspaces.tenant_id = tenant_id` rows exist with
   non-completed work. Operator must `mac bridge linear disable
   <name>` first and drain in-flight tasks.
2. After disable, deletion cascades:
   - `linear_workspaces` row deleted
   - Secret marked retired (`secret.enabled = 0`); fully purged via
     existing secret-cleanup retention path
   - Existing tasks with `origin.tracker = "linear"` get a new
     metadata key `origin.deleted_workspace = true` so the dispatch
     helper can skip Linear writes
3. Forensic findings recorded so the audit trail survives the delete.

`ON DELETE CASCADE` on the schema is correct as the default for
operator force-deletes; the validation gate in
`delete_tenant` is the safe-by-default path.

## 23. Auth and scope (added in v2)

The new endpoints must explicitly extend mac-api's
`_required_scope(method, path)` in `api.py:943`. Today that function
has no case for `/bridges/` — non-GET requests fall through to the
catch-all `"write"`. v2 adds:

```python
def _required_scope(method, path):
    ...
    if path.startswith("/bridges/"):
        return "admin"  # all bridge mgmt requires admin
    if "/linear/comment" in path or "/linear/state" in path \
            or "/linear/attach" in path \
            or "/linear/label" in path or "/linear/priority" in path \
            or "/linear/assignee" in path:
        return "agent"  # runtime tools called by mac-task-runner
    ...
```

The `assert_tenant` check in each handler ensures the bearer can act
on the resource's tenant. For agent-scoped calls on Linear-origin
tasks, the agent's bound tenant must match the task's tenant.

For Linear-side auth: v2 specifies **personal API key tied to a
dedicated bot user** in the Linear workspace. Rationale:

- Linear OAuth apps require an app-publishing flow that's heavy for a
  self-hosted bridge
- Personal API keys are workspace-scoped — sufficient for our case
- The bot user provides identity for audit ("created by mac-bridge")
- Token rotation = update 1Password → ExternalSecret resyncs → mac
  re-registers via the auto-register code path

Future work: support Linear App API keys (scoped tokens) when Linear
exposes them widely; OAuth flow for multi-org SaaS deployments.

## 24. Acceptance criteria for v1 (phases L0–L6)

A new Linear-backed tenant can:

1. Be created with `mac tenant set-tracker acme linear --workspace acme-eng`
2. Have a Linear Issue created from the Linear UI appear as a mac
   task within 60s
3. Receive lifecycle comments on the Linear Issue as the mac task
   moves through claim/run/review/complete states
4. Have Hermes successfully call `mac_create_task` and produce a new
   Linear Issue + mac task
5. Have an agent successfully call `mac_comment_on_task` mid-execution
   and see the comment appear in Linear

All five must work end-to-end against a real Linear workspace before
v1 is considered shippable.
