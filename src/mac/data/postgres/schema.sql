-- MAC Postgres schema. Mirrors src/mac/store.py SQLiteStore._initialize.
-- Type strategy: every TEXT column stays TEXT (including JSON storage) so
-- the ~50 service-layer SQL strings work unchanged. Only divergences:
--   * REAL          -> DOUBLE PRECISION
--   * AUTOINCREMENT -> BIGSERIAL
--   * Two CHECK-triggers on tasks become PL/pgSQL trigger functions
--   * The unified `events` view is rewritten using jsonb_* helpers
-- Dialect shim: json_extract(text|jsonb, text) is a PL/pgSQL function
-- below; SQLite-style '$.foo[0]' paths are walked into the JSONB tree
-- using -> / ->>, returning the leaf as text (matching SQLite semantics).
--
-- Idempotent: every CREATE uses IF NOT EXISTS or OR REPLACE so this file
-- runs cleanly against a fresh database and against an already-applied one.

-- ============================================================================
-- Dialect shims
-- ============================================================================

CREATE OR REPLACE FUNCTION json_extract(j jsonb, path text)
RETURNS text AS $$
DECLARE
    rest text;
    cur jsonb := j;
    norm text;
    parts text[];
    p text;
    is_last boolean;
    i int;
BEGIN
    IF j IS NULL OR path IS NULL THEN
        RETURN NULL;
    END IF;
    IF path !~ '^\$' THEN
        RETURN NULL;
    END IF;
    -- Strip leading $ and any leading dot, then rewrite [N] as .N so
    -- everything tokenises on '.': $.foo[0].bar -> foo.0.bar
    rest := substring(path from 2);
    rest := regexp_replace(rest, '^\.', '');
    norm := regexp_replace(rest, '\[([0-9]+)\]', '.\1', 'g');
    IF norm = '' THEN
        RETURN cur::text;
    END IF;
    parts := regexp_split_to_array(norm, '\.');
    FOR i IN 1..coalesce(array_length(parts, 1), 0) LOOP
        p := parts[i];
        is_last := (i = array_length(parts, 1));
        IF p ~ '^[0-9]+$' THEN
            IF is_last THEN
                RETURN cur ->> (p::int);
            END IF;
            cur := cur -> (p::int);
        ELSE
            IF is_last THEN
                RETURN cur ->> p;
            END IF;
            cur := cur -> p;
        END IF;
        IF cur IS NULL THEN
            RETURN NULL;
        END IF;
    END LOOP;
    RETURN cur::text;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Text-input overload: service-layer SQL passes TEXT JSON columns.
CREATE OR REPLACE FUNCTION json_extract(j text, path text)
RETURNS text AS $$
    SELECT json_extract(
        CASE WHEN j IS NULL OR j = '' THEN NULL ELSE j::jsonb END,
        path
    );
$$ LANGUAGE sql IMMUTABLE;

-- ============================================================================
-- Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    display_name TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, handle)
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);

CREATE TABLE IF NOT EXISTS personas (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    soul_ref TEXT NOT NULL,
    memory_scope TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_personas_tenant ON personas (tenant_id);

-- Persona identity data migration (runtime-neutral rename).
-- ``hermes_instances`` -> ``persona_instances`` and the
-- ``platform_bindings.hermes_instance_id`` FK -> ``persona_instance_id``.
-- Idempotent: only renames when the old-schema object is still present, so a
-- fresh database (created directly with the new names below) and an
-- already-migrated database both no-op. Rows and FK relationships are
-- preserved because RENAME operates in place. Temporary read migration for
-- this release only; there is no long-term dual-name support.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'hermes_instances'
          AND table_schema = current_schema()
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'persona_instances'
          AND table_schema = current_schema()
    ) THEN
        ALTER TABLE hermes_instances RENAME TO persona_instances;
        ALTER INDEX IF EXISTS idx_hermes_instances_tenant
            RENAME TO idx_persona_instances_tenant;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'platform_bindings'
          AND column_name = 'hermes_instance_id'
          AND table_schema = current_schema()
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'platform_bindings'
          AND column_name = 'persona_instance_id'
          AND table_schema = current_schema()
    ) THEN
        ALTER TABLE platform_bindings
            RENAME COLUMN hermes_instance_id TO persona_instance_id;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS persona_instances (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
    home_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_persona_instances_tenant ON persona_instances (tenant_id);

CREATE TABLE IF NOT EXISTS platform_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    persona_instance_id TEXT NOT NULL REFERENCES persona_instances(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    scopes TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance ON platform_bindings (persona_instance_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    project TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    required_capabilities TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    metadata TEXT NOT NULL,
    owner_agent_id TEXT,
    lease_id TEXT,
    leased_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workflow_run_id TEXT,
    workflow_node_key TEXT,
    human_assignees TEXT,
    created_by_human TEXT,
    idempotency_key TEXT
);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS human_assignees TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_by_human TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_state_priority ON tasks (state, priority DESC, created_at);

-- Selector support: metadata and required_capabilities are JSON held in TEXT,
-- which cannot be indexed or queried structurally. Selecting a group by
-- `metadata.origin.kind=...` therefore meant loading every task and deciding
-- in Python -- measured at 1.4s per selector over 100k tasks, versus 0.015s
-- through the index below.
--
-- These are GENERATED columns, so the TEXT columns remain the single source
-- of truth and the write path is untouched: no backfill, no dual write, and
-- nothing to keep in sync by hand. Postgres derives them on write and the GIN
-- indexes make containment and path lookups fast.
--
-- Note for query authors: the JSONB `?` existence operator cannot be used
-- through this codebase's parameter binding, because `?` is also the
-- placeholder token that store_postgres translates. Use jsonb_exists(col, ?).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS metadata_json JSONB
    GENERATED ALWAYS AS (metadata::jsonb) STORED;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS capabilities_json JSONB
    GENERATED ALWAYS AS (required_capabilities::jsonb) STORED;
CREATE INDEX IF NOT EXISTS idx_tasks_metadata_gin ON tasks USING GIN (metadata_json);
CREATE INDEX IF NOT EXISTS idx_tasks_capabilities_gin ON tasks USING GIN (capabilities_json);
CREATE INDEX IF NOT EXISTS idx_tasks_review_queue
    ON tasks (priority DESC, created_at, id)
    WHERE state IN ('needs_review', 'reviewing');
-- Project is the operator's natural scope -- "everything parked in mac" -- and
-- every selector, list, and search pushes it into SQL, but nothing indexed it:
-- a project-scoped query fell back to scanning the whole table. Leading with
-- project also serves project-only lookups, which (state, ...) cannot.
CREATE INDEX IF NOT EXISTS idx_tasks_project_state_priority
    ON tasks (project, state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_state_updated ON tasks (state, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (owner_agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency_key
    ON tasks (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_edges (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    dependency_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    edge_position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, dependency_task_id),
    UNIQUE (task_id, edge_position),
    CHECK (task_id <> dependency_task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_edges_dependency
    ON task_edges (dependency_task_id, task_id);

CREATE TABLE IF NOT EXISTS task_dependency_quarantine (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    raw_dependency_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    candidates TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_dependency_quarantine_task
    ON task_dependency_quarantine (task_id, detected_at);

CREATE TABLE IF NOT EXISTS task_dependency_migrations (
    version TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    migrated_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL
);

-- mac-1hnt: enforce the task state machine at the DB layer. SQLite did
-- this with two CHECK triggers using RAISE(ABORT, ...); the same intent
-- in Postgres is a PL/pgSQL trigger function attached to INSERT and to
-- UPDATE OF state.
CREATE OR REPLACE FUNCTION trg_tasks_state_enum() RETURNS trigger AS $$
BEGIN
    -- Kept in step with mac.models.TaskState BY HAND. The duplication is
    -- deliberate (the DB must enforce the enum without importing Python) and
    -- is a known drift hazard: adding a state to the enum without adding it
    -- here fails at runtime with 'invalid task state', which reads as a bug in
    -- the caller rather than a missing migration. 'stopped' is ADR 0020.
    IF NEW.state NOT IN (
        'open', 'waiting', 'blocked', 'claimed', 'running',
        'needs_review', 'reviewing', 'needs_input', 'stopped',
        'completed', 'failed', 'cancelled'
    ) THEN
        RAISE EXCEPTION 'invalid task state';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_state_enum_ins ON tasks;
CREATE TRIGGER trg_tasks_state_enum_ins
    BEFORE INSERT ON tasks
    FOR EACH ROW EXECUTE FUNCTION trg_tasks_state_enum();
DROP TRIGGER IF EXISTS trg_tasks_state_enum_upd ON tasks;
CREATE TRIGGER trg_tasks_state_enum_upd
    BEFORE UPDATE OF state ON tasks
    FOR EACH ROW EXECUTE FUNCTION trg_tasks_state_enum();

CREATE TABLE IF NOT EXISTS task_history (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_history_task_created ON task_history (task_id, created_at);
-- Fleet-wide "what moved in the last N hours", which is the spine of the
-- observability console's flow chart and transition ticker. The index above is
-- task-scoped, so a global time-window scan could not use it and degraded to a
-- seq scan plus sort over every history row ever written. Partial on the
-- transition event because that is the only event_type the time-window query
-- asks for; task_history carries ~50 other event types.
CREATE INDEX IF NOT EXISTS idx_task_history_transitions_created
    ON task_history (created_at DESC)
    WHERE event_type = 'task.transitioned';

-- WHAT THE CODING CLI WAS ASKED, AND WHAT IT SAID BACK.
--
-- The executor invokes claude/codex/cursor headless with the whole prompt as
-- one argument, and until now kept only sha256(stdout), sha256(stderr) and two
-- byte counts. That proves an output existed; it cannot tell you what was
-- asked, what was answered, or why an agent did what it did. Every task in the
-- ledger reads "llm: no attributed model calls recorded" for the same reason.
--
-- A CHILD TABLE, not columns on tasks. One task produces many invocations
-- (retries, multiple attempts, several agents), the payloads are large, and the
-- tasks row is read constantly by dispatch -- putting transcripts inline would
-- put megabytes behind every allocator scan. This is presented as a task
-- PROPERTY at the API and export boundary, where it belongs conceptually,
-- without making the hot path pay for it.
--
-- Retention is deliberate and separate: an unbounded firehose into this table
-- is how action_events reached 16GB and wedged the hub.
CREATE TABLE IF NOT EXISTS task_agent_transcripts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id TEXT,
    command_id TEXT,
    sequence INTEGER NOT NULL DEFAULT 0,
    coding_agent TEXT,
    model TEXT,
    -- The three texts, compressed together as one zlib stream of a small JSON
    -- object. Compressed at rest and transparent at every read: nothing above
    -- the store sees bytes.
    --
    -- MEASURED, not assumed. Postgres already TOAST-compresses large TEXT with
    -- pglz and gets 2.8x for free, so the honest question was what application
    -- compression ADDS on top of that, not what it achieves from raw:
    --
    --   raw             57.7 KB
    --   TEXT via TOAST  20.7 KB   2.8x, free, and greppable from psql
    --   zlib-6 BYTEA    14.4 KB   net 1.4x over TOAST,  2 ms
    --   lzma-6 BYTEA    13.3 KB   net 1.6x over TOAST, 42 ms
    --
    -- zlib because lzma bought 1.6x against 1.4x for twenty-one times the CPU.
    -- One stream over all three fields rather than three streams: they share
    -- vocabulary (the same source file usually appears in both prompt and
    -- response), so compressing together beats compressing separately.
    payload BYTEA,
    -- Named per row, never inferred. Rows written before compression existed
    -- read as 'none', and changing codec later does not require rewriting or
    -- guessing at what is already stored.
    compression TEXT NOT NULL DEFAULT 'none',
    returncode INTEGER,
    started_at TEXT,
    completed_at TEXT,
    duration_ms DOUBLE PRECISION,
    truncated INTEGER NOT NULL DEFAULT 0,
    prompt_sha256 TEXT,
    response_sha256 TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
-- Ordered reads per task: an export walks one task's turns in order.
CREATE INDEX IF NOT EXISTS idx_task_transcripts_task
    ON task_agent_transcripts (task_id, sequence, created_at);
-- Retention sweeps and per-agent audits both scan by age.
CREATE INDEX IF NOT EXISTS idx_task_transcripts_created
    ON task_agent_transcripts (created_at);

CREATE TABLE IF NOT EXISTS task_transition_outbox (
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
CREATE INDEX IF NOT EXISTS idx_task_transition_outbox_status ON task_transition_outbox (status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_transition_outbox_task ON task_transition_outbox (task_id, created_at);

CREATE TABLE IF NOT EXISTS reconciliation_state (
    name TEXT PRIMARY KEY,
    cursor TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_state_lease
    ON reconciliation_state (lease_expires_at, name);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    summary TEXT NOT NULL,
    checksum TEXT,
    metadata TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence (task_id);

CREATE TABLE IF NOT EXISTS evidence_artifacts (
    id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    content_type TEXT NOT NULL,
    encoding TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content_base64 TEXT NOT NULL,
    content_uri TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Live DBs predate content_uri (CREATE TABLE IF NOT EXISTS skips them);
-- idempotent ALTER mirrors the SQLite _ensure_column migration.
ALTER TABLE evidence_artifacts ADD COLUMN IF NOT EXISTS content_uri TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_task
    ON evidence_artifacts (task_id, created_at, id);

CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- PR2c (spec §6.3, Option B): dispatcher (lease owner) may delegate
    -- lifecycle authorship to the role agent spawned in the task Job.
    -- NULL = no delegation; the owner is the sole authoriser.
    delegated_agent_id TEXT,
    expiry_finalizer_token TEXT,
    expiry_finalizer_claimed_at TIMESTAMPTZ,
    expiry_finalized_at TIMESTAMPTZ,
    expiry_finalization_decision JSONB
);
-- Additive migration for existing deployments: schema.sql is run on every
-- mac-api startup, and CREATE TABLE IF NOT EXISTS skips already-present
-- tables, so the column would never appear on a live DB without this
-- explicit ALTER. Postgres 9.6+ supports `ADD COLUMN IF NOT EXISTS`, so
-- this is idempotent and safe to re-run.
ALTER TABLE leases ADD COLUMN IF NOT EXISTS delegated_agent_id TEXT
;
ALTER TABLE leases ADD COLUMN IF NOT EXISTS expiry_finalizer_token TEXT;
ALTER TABLE leases ADD COLUMN IF NOT EXISTS expiry_finalizer_claimed_at TIMESTAMPTZ;
ALTER TABLE leases ADD COLUMN IF NOT EXISTS expiry_finalized_at TIMESTAMPTZ;
ALTER TABLE leases ADD COLUMN IF NOT EXISTS expiry_finalization_decision JSONB;
CREATE INDEX IF NOT EXISTS idx_leases_task_status ON leases (task_id, status);
CREATE INDEX IF NOT EXISTS idx_leases_agent_status ON leases (agent_id, status);
CREATE INDEX IF NOT EXISTS idx_leases_status_expiry ON leases (status, expires_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_active_per_task
    ON leases (task_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    labels TEXT NOT NULL,
    resources TEXT NOT NULL,
    trusted INTEGER NOT NULL,
    hardware TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    instance_kind TEXT NOT NULL DEFAULT 'static'
        CHECK (instance_kind IN ('static', 'fungible')),
    capabilities TEXT NOT NULL,
    resources TEXT NOT NULL,
    status TEXT NOT NULL,
    health_status TEXT NOT NULL,
    current_task_id TEXT,
    running_digest TEXT,
    role_id TEXT,
    hermes_instance_id TEXT,
    attestation_key_ciphertext TEXT,
    attestation_key_prev_ciphertext TEXT,
    attestation_key_rotated_at TEXT,
    installed_packages TEXT NOT NULL DEFAULT '{}',
    dispatch_hold INTEGER NOT NULL DEFAULT 0,
    dispatch_hold_reason TEXT,
    dispatch_hold_at TEXT,
    consecutive_lease_expiries_no_telemetry INTEGER NOT NULL DEFAULT 0,
    last_control_stream_published_at TEXT,
    last_control_stream_consumed_at TEXT,
    -- Tombstone: decommissioned agents keep their row so AgentBus streams,
    -- events, and delivery history survive with real identities.
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
ALTER TABLE agents ADD COLUMN IF NOT EXISTS instance_kind TEXT NOT NULL
    DEFAULT 'static' CHECK (instance_kind IN ('static', 'fungible'));
CREATE INDEX IF NOT EXISTS idx_agents_status_health ON agents (status, health_status);
-- Ephemeral/decommissioned agents are tombstoned (deleted_at set), never
-- purged, so the table grows without bound. The default /agents listing
-- filters deleted_at IS NULL and sorts by name,id; without this partial index
-- that is a full scan plus a sort over every tombstone, which is where the
-- ~1s /agents latency for 8 live agents came from. SQLite has had this index
-- since the deleted_at migration; the Postgres port never got it, so the
-- backend the fleet actually runs kept paying the cost.
CREATE INDEX IF NOT EXISTS idx_agents_live_name
    ON agents (name, id) WHERE deleted_at IS NULL;

-- Hub-authoritative per-worker identities. Only a SHA-256 bearer hash is
-- persisted; raw tokens exist solely in one-time install manifests and at the
-- destination Secret/env file. Co-location with agent state lets every API
-- replica and package claim transaction consult the same authority.
CREATE TABLE IF NOT EXISTS worker_credentials (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    fleet TEXT NOT NULL DEFAULT '',
    credential_version INTEGER NOT NULL CHECK (credential_version >= 1),
    token_hash TEXT NOT NULL UNIQUE,
    token_fingerprint TEXT NOT NULL,
    scopes TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('vm', 'k8s')),
    expected_source_commit TEXT NOT NULL DEFAULT '',
    expected_runtime_digest TEXT NOT NULL DEFAULT '',
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    package_capable BOOLEAN NOT NULL DEFAULT FALSE,
    state TEXT NOT NULL CHECK (
        state IN ('pending_install', 'active', 'superseded', 'revoked')
    ),
    destination TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    activated_at TEXT,
    revoked_at TEXT,
    superseded_by TEXT REFERENCES worker_credentials(id) ON DELETE RESTRICT,
    created_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, credential_version)
);
CREATE INDEX IF NOT EXISTS idx_worker_credentials_agent_state
    ON worker_credentials (agent_id, state, credential_version DESC);
CREATE INDEX IF NOT EXISTS idx_worker_credentials_expiry
    ON worker_credentials (expires_at, state);

CREATE TABLE IF NOT EXISTS worker_credential_events (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES worker_credentials(id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worker_credential_events_agent_created
    ON worker_credential_events (agent_id, created_at, id);

-- Shared singleton rollout policy; local files cannot coordinate API replicas.
CREATE TABLE IF NOT EXISTS worker_credential_policy_state (
    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'fleet'),
    mode TEXT NOT NULL CHECK (mode IN ('compatibility', 'enforced')),
    inventory_digest TEXT,
    ready_agent_ids TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Stable database-owned hub identity. Application startup atomically inserts
-- a random UUID once; every API replica reads the same winning singleton.
CREATE TABLE IF NOT EXISTS hub_authority_identity (
    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'hub'),
    authority_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Durable synchronized-cutover authority. The identity payload is
-- secret-free; encrypted attestation candidates are isolated in their own
-- staging table and removed at either terminal transition.
CREATE TABLE IF NOT EXISTS fleet_release_epochs (
    epoch_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL UNIQUE,
    identity_payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'proved', 'committed', 'aborted')),
    proof_sha256 TEXT,
    successor_hold_reason TEXT,
    desired_policy_mode TEXT CHECK (
        desired_policy_mode IS NULL OR
        desired_policy_mode IN ('compatibility', 'enforced')
    ),
    policy_snapshot TEXT NOT NULL,
    actor TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    proved_at TEXT,
    committed_at TEXT,
    aborted_at TEXT,
    abort_reason TEXT,
    abort_disposition TEXT
);
ALTER TABLE fleet_release_epochs
    ADD COLUMN IF NOT EXISTS abort_disposition TEXT;

CREATE TABLE IF NOT EXISTS fleet_release_epoch_agents (
    epoch_id TEXT NOT NULL REFERENCES fleet_release_epochs(epoch_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    open_state INTEGER NOT NULL DEFAULT 1 CHECK (open_state IN (0, 1)),
    prior_dispatch_hold INTEGER NOT NULL CHECK (prior_dispatch_hold IN (0, 1)),
    prior_hold_reason TEXT,
    prior_hold_at TEXT,
    epoch_hold_reason TEXT NOT NULL,
    epoch_hold_at TEXT NOT NULL,
    prior_active_service_claim_ids TEXT NOT NULL,
    generation TEXT NOT NULL,
    baseline_seen TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES worker_credentials(id) ON DELETE RESTRICT,
    principal_version INTEGER NOT NULL CHECK (principal_version >= 1),
    principal_fingerprint TEXT NOT NULL,
    install_receipt TEXT,
    install_receipt_sha256 TEXT,
    prior_live_principal_ids TEXT NOT NULL,
    prior_attestation_ciphertext_sha256 TEXT NOT NULL,
    attestation_candidate_fingerprint TEXT,
    attestation_proof TEXT,
    attestation_proof_sha256 TEXT,
    report_executor_action TEXT NOT NULL CHECK (
        report_executor_action IN ('preserve', 'approve', 'revoke')
    ),
    prior_report_executor_projection_sha256 TEXT NOT NULL,
    report_executor_attestation TEXT,
    report_executor_startup_timestamp TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (epoch_id, agent_id),
    UNIQUE (epoch_id, ordinal)
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_release_open_agent
    ON fleet_release_epoch_agents (agent_id) WHERE open_state = 1;
CREATE INDEX IF NOT EXISTS idx_fleet_release_epoch_agents_epoch
    ON fleet_release_epoch_agents (epoch_id, ordinal);

CREATE TABLE IF NOT EXISTS fleet_release_attestation_candidates (
    epoch_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    key_ciphertext TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (epoch_id, agent_id),
    FOREIGN KEY (epoch_id, agent_id)
        REFERENCES fleet_release_epoch_agents(epoch_id, agent_id)
        ON DELETE RESTRICT
);

-- Durable "this deploy generation is no longer live" fact.
--
-- `fleet_release_epoch_agents.generation` is the exact string a deploy writes
-- into the node-local barrier file ($MAC_HOME/deploy-start-barrier) and that
-- the worker reads back when it decides whether to keep draining. Nothing
-- recorded that a generation had stopped being live once its epoch reached a
-- terminal state, so a worker holding a barrier from an ABORTED epoch had no
-- authority to consult and drained forever.
--
-- One row per (epoch, participant, generation): the terminal outcome that
-- retired the generation, when the epoch prepared it, when it was retired, and
-- the epoch's disposition/reason carried through verbatim so an operator reading
-- this table alone can say why a worker was released.
CREATE TABLE IF NOT EXISTS fleet_release_generation_retirements (
    epoch_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('aborted', 'committed')),
    disposition TEXT,
    reason TEXT,
    prepared_at TEXT,
    retired_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (epoch_id, agent_id, generation),
    FOREIGN KEY (epoch_id, agent_id)
        REFERENCES fleet_release_epoch_agents(epoch_id, agent_id)
        ON DELETE RESTRICT
);
-- Additive migrations for a database that already carries an earlier, narrower
-- version of the table. CREATE TABLE IF NOT EXISTS is a no-op there, so without
-- these a live hub keeps a table missing the columns the accessors write --
-- exactly the `reviews.findings` failure mode. Mirrored by ensure_column() calls
-- in store_postgres.py::initialize so both paths agree.
--
-- These precede the index below because the index names `retired_at`: on such a
-- database the column does not exist yet, and the whole schema is applied as one
-- statement batch, so an index built first would abort the entire migration.
ALTER TABLE fleet_release_generation_retirements
    ADD COLUMN IF NOT EXISTS disposition TEXT;
ALTER TABLE fleet_release_generation_retirements
    ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE fleet_release_generation_retirements
    ADD COLUMN IF NOT EXISTS prepared_at TEXT;
ALTER TABLE fleet_release_generation_retirements
    ADD COLUMN IF NOT EXISTS retired_at TEXT NOT NULL DEFAULT '';
-- The read this table exists to serve is the worker's: "for MY agent id and the
-- generation in MY barrier file, is there a retirement, and what is the newest
-- one?" That is an (agent_id, generation) lookup, not an epoch lookup, so the
-- primary key above cannot serve it. `retired_at DESC` trails the key columns so
-- the newest-first ordering is satisfied by the same index.
CREATE INDEX IF NOT EXISTS idx_fleet_release_generation_retirements_agent
    ON fleet_release_generation_retirements (agent_id, generation, retired_at DESC);

-- One-way shared authority for ordinary atomic task publication. Absence is
-- compatibility mode; the sole row records the irreversible managed cutover.
CREATE TABLE IF NOT EXISTS managed_task_publication_rollout (
    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'fleet'),
    revision INTEGER NOT NULL CHECK (revision = 1),
    crossed_by TEXT NOT NULL,
    crossed_at TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}'
);

-- Durable request identity for create retries. Raw idempotency keys and
-- principal identifiers are hashed before storage.
CREATE TABLE IF NOT EXISTS task_create_idempotency (
    scope_digest TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope_digest, key_digest)
);

-- `leases` is declared before `agents` for historical schema ordering, so the
-- delegated-agent foreign key must be installed only after the referenced
-- table exists. The catalog guard keeps this additive migration idempotent on
-- both fresh and already-initialized authorities.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'leases'::regclass
          AND conname = 'leases_delegated_agent_id_fkey'
    ) THEN
        ALTER TABLE leases
            ADD CONSTRAINT leases_delegated_agent_id_fkey
            FOREIGN KEY (delegated_agent_id)
            REFERENCES agents(id) ON DELETE SET NULL;
    END IF;
END;
$$;

-- Durable supervisor-observed crash evidence. The report row is the
-- deduplicated revision+stack incident; occurrences retain every raw event.
CREATE TABLE IF NOT EXISTS agent_crash_reports (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    process_name TEXT NOT NULL,
    revision TEXT NOT NULL,
    stack_signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    repair_attempt_count INTEGER NOT NULL DEFAULT 0,
    affected_agent_ids TEXT NOT NULL DEFAULT '[]',
    repair_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_crash_reports_status_last_seen
    ON agent_crash_reports (status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_agent_crash_reports_repair_task
    ON agent_crash_reports (repair_task_id);
ALTER TABLE agent_crash_reports
    ADD COLUMN IF NOT EXISTS repair_attempt_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS agent_crash_occurrences (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    report_id TEXT NOT NULL REFERENCES agent_crash_reports(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    supervisor TEXT NOT NULL,
    process_name TEXT NOT NULL,
    pid INTEGER,
    exit_code INTEGER,
    signal INTEGER,
    reason TEXT NOT NULL,
    revision TEXT NOT NULL,
    tree_sha TEXT NOT NULL,
    task_id TEXT,
    lease_id TEXT,
    stack_trace TEXT NOT NULL,
    stderr_tail TEXT NOT NULL,
    core_reference TEXT NOT NULL,
    core_metadata TEXT NOT NULL DEFAULT '{}',
    resource_snapshot TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_crash_occurrences_report
    ON agent_crash_occurrences (report_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_agent_crash_occurrences_agent
    ON agent_crash_occurrences (agent_id, observed_at);

-- Explicit, admin-authorized escape hatch for recovery work that must repair
-- the sandbox/worker environment itself.  This is separate from task metadata
-- so an ordinary task author cannot self-select direct host execution.
CREATE TABLE IF NOT EXISTS task_break_glass_authorizations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    execution_boundary TEXT NOT NULL CHECK(execution_boundary = 'host'),
    reason TEXT NOT NULL,
    authorized_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'claimed', 'consumed', 'revoked', 'expired')),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    lease_id TEXT REFERENCES leases(id) ON DELETE SET NULL,
    consumed_at TEXT,
    revoked_at TEXT,
    revoked_by TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_break_glass_task_status
    ON task_break_glass_authorizations (task_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_task_break_glass_agent_status
    ON task_break_glass_authorizations (agent_id, status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_task_break_glass_active
    ON task_break_glass_authorizations (task_id) WHERE status = 'active';

-- media-01 service-role election: desired media services + the leased holds
-- capable hosts claim (mirrors tasks+leases). Ported from SQLiteStore._initialize.
-- Placed after `agents` so the service_claims -> agents FK resolves (Postgres
-- requires the referenced table to exist at CREATE TABLE time).
CREATE TABLE IF NOT EXISTS service_roles (
    id TEXT PRIMARY KEY,
    op TEXT NOT NULL,
    slug TEXT NOT NULL,
    model_id TEXT,
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    hardware_requirements TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(slug, tenant_id)
);
CREATE TABLE IF NOT EXISTS service_claims (
    id TEXT PRIMARY KEY,
    service_role_id TEXT NOT NULL REFERENCES service_roles(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_service_claims_role_status
    ON service_claims (service_role_id, status);
CREATE INDEX IF NOT EXISTS idx_service_claims_agent_status
    ON service_claims (agent_id, status);
-- Pool model: a host holds an op at most once; multiple hosts may hold the same
-- op. Split-brain guard at the DB layer.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_service_claims_active_per_role_agent
    ON service_claims (service_role_id, agent_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS fleets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fleets_status_name ON fleets (status, name);
CREATE INDEX IF NOT EXISTS idx_fleets_tenant ON fleets (tenant_id);

CREATE TABLE IF NOT EXISTS fleet_agents (
    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_fleet_agents_agent ON fleet_agents (agent_id);

CREATE TABLE IF NOT EXISTS fleet_agent_observations (
    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (fleet_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_fleet_agent_observations_agent ON fleet_agent_observations (agent_id);
CREATE INDEX IF NOT EXISTS idx_fleet_agent_observations_last_seen ON fleet_agent_observations (last_seen_at);

CREATE TABLE IF NOT EXISTS fleet_events (
    id TEXT PRIMARY KEY,
    fleet_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fleet_events_fleet_created ON fleet_events (fleet_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender_agent_id TEXT NOT NULL,
    recipient_agent_id TEXT,
    task_id TEXT,
    message_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_status ON messages (recipient_agent_id, status);

CREATE TABLE IF NOT EXISTS agentbus_streams (
    id TEXT PRIMARY KEY,
    sender_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    recipient_agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    topic TEXT NOT NULL,
    content_type TEXT NOT NULL,
    headers TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    -- Group streams: JSON member list; NULL = legacy sender/recipient pair.
    participants TEXT
);
CREATE INDEX IF NOT EXISTS idx_agentbus_streams_recipient_status
    ON agentbus_streams (recipient_agent_id, status, updated_at);

-- Hub-durable consumer read positions (opaque client-defined bookmarks).
CREATE TABLE IF NOT EXISTS agentbus_consumer_cursors (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    position TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, topic)
);

-- Durable resume cursors for the bounded work-package pipeline controller and
-- the repository ref reconciler (opaque, bounded client-defined JSON keyed by
-- a stable scope/name), so a hub restart resumes instead of rescanning.
CREATE TABLE IF NOT EXISTS pipeline_cursors (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, name)
);
CREATE INDEX IF NOT EXISTS idx_agentbus_streams_sender_status
    ON agentbus_streams (sender_agent_id, status, updated_at);

CREATE TABLE IF NOT EXISTS agentbus_chunks (
    id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL REFERENCES agentbus_streams(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    sender_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_encoding TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(stream_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_agentbus_chunks_stream_sequence
    ON agentbus_chunks (stream_id, sequence);

CREATE TABLE IF NOT EXISTS observability_events (
    sequence BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    layer TEXT NOT NULL,
    source TEXT NOT NULL,
    level TEXT NOT NULL,
    name TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    value DOUBLE PRECISION,
    unit TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observability_events_created
    ON observability_events (created_at, sequence);
CREATE INDEX IF NOT EXISTS idx_observability_events_name_created
    ON observability_events (name, created_at);
CREATE INDEX IF NOT EXISTS idx_observability_events_subject_sequence
    ON observability_events (
        kind, name, subject_type, subject_id, sequence DESC
    );

CREATE TABLE IF NOT EXISTS operator_notifications (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    channels TEXT NOT NULL,
    metadata TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_operator_notifications_status_created
    ON operator_notifications (status, created_at);

CREATE TABLE IF NOT EXISTS notifier_channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    channel_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    event_types TEXT NOT NULL DEFAULT '[]',
    target TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifier_channels_type_enabled
    ON notifier_channels (channel_type, enabled);

-- Runtime-neutral public identity and OpenClaw delivery plane.  Public
-- identities are logical fleet resources rather than one bot per worker.
CREATE TABLE IF NOT EXISTS communication_identities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_communication_identity_default
    ON communication_identities (is_default) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS communication_accounts (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES communication_identities(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    credential_refs TEXT NOT NULL DEFAULT '{}',
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(identity_id, channel, account_id)
);
CREATE INDEX IF NOT EXISTS idx_communication_accounts_channel
    ON communication_accounts (channel, enabled);

CREATE TABLE IF NOT EXISTS representation_bindings (
    id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    identity_id TEXT REFERENCES communication_identities(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_kind, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_representation_bindings_identity
    ON representation_bindings (identity_id, enabled);

CREATE TABLE IF NOT EXISTS gateway_identity_leases (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE REFERENCES communication_accounts(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    fencing_token TEXT NOT NULL UNIQUE,
    leased_until TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gateway_identity_leases_agent
    ON gateway_identity_leases (agent_id, leased_until);

CREATE TABLE IF NOT EXISTS human_message_deliveries (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES communication_identities(id) ON DELETE RESTRICT,
    account_id TEXT NOT NULL REFERENCES communication_accounts(id) ON DELETE RESTRICT,
    channel TEXT,
    target TEXT NOT NULL,
    body TEXT NOT NULL,
    origin_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    delivery_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    delivery_lease_id TEXT,
    leased_until TEXT,
    provider_message_id TEXT,
    last_error TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_human_message_deliveries_status
    ON human_message_deliveries (status, created_at);
CREATE INDEX IF NOT EXISTS idx_human_message_deliveries_identity
    ON human_message_deliveries (identity_id, created_at);

CREATE TABLE IF NOT EXISTS command_audit (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    argv TEXT NOT NULL,
    cwd TEXT NOT NULL,
    task_id TEXT,
    lease_id TEXT,
    started_at TEXT,
    completed_at TEXT,
    duration_ms DOUBLE PRECISION,
    returncode INTEGER,
    stdout_sha256 TEXT,
    stderr_sha256 TEXT,
    stdout_bytes INTEGER,
    stderr_bytes INTEGER,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_command_audit_created ON command_audit (created_at, id);
CREATE INDEX IF NOT EXISTS idx_command_audit_agent_created ON command_audit (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_command_audit_task_created ON command_audit (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_command_audit_command ON command_audit (command_id, created_at);

CREATE TABLE IF NOT EXISTS action_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    agent_id TEXT,
    hermes_instance_id TEXT,
    task_id TEXT,
    session_id TEXT,
    sandbox_id TEXT,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_name TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    outcome TEXT NOT NULL,
    severity TEXT NOT NULL,
    policy_id TEXT,
    policy_version INTEGER,
    command_id TEXT,
    parent_event_id TEXT,
    attributes TEXT NOT NULL,
    redaction_state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_events_timestamp
    ON action_events (timestamp, event_id);
CREATE INDEX IF NOT EXISTS idx_action_events_task_timestamp
    ON action_events (task_id, timestamp);

CREATE TABLE IF NOT EXISTS openshell_policies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    policy_text TEXT NOT NULL,
    parsed_metadata TEXT NOT NULL,
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_openshell_policies_active_name
    ON openshell_policies (active, name);

CREATE TABLE IF NOT EXISTS openshell_policy_versions (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES openshell_policies(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    policy_text TEXT NOT NULL,
    parsed_metadata TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, version)
);
CREATE INDEX IF NOT EXISTS idx_openshell_policy_versions_policy
    ON openshell_policy_versions (policy_id, version);

CREATE TABLE IF NOT EXISTS openshell_policy_assignments (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES openshell_policies(id) ON DELETE CASCADE,
    policy_version INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_openshell_policy_assignments_target
    ON openshell_policy_assignments (target_type, target_id, active);
CREATE INDEX IF NOT EXISTS idx_openshell_policy_assignments_policy
    ON openshell_policy_assignments (policy_id, active);

CREATE TABLE IF NOT EXISTS openshell_agent_status (
    agent_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    required INTEGER NOT NULL,
    active INTEGER NOT NULL,
    sandbox_id TEXT,
    policy_id TEXT,
    policy_version INTEGER,
    checksum TEXT,
    supervisor_pid INTEGER,
    detail TEXT NOT NULL,
    reported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_openshell_agent_status_status
    ON openshell_agent_status (status, reported_at);

CREATE TABLE IF NOT EXISTS fleet_directives (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    scope TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fleet_directives_state_name
    ON fleet_directives (state, name);

CREATE TABLE IF NOT EXISTS fleet_directive_versions (
    id TEXT PRIMARY KEY,
    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    document TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(directive_id, version)
);
CREATE INDEX IF NOT EXISTS idx_fleet_directive_versions_directive
    ON fleet_directive_versions (directive_id, version);
CREATE OR REPLACE FUNCTION trg_fleet_directive_versions_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'fleet directive versions are immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_fleet_directive_versions_immutable ON fleet_directive_versions;
CREATE TRIGGER trg_fleet_directive_versions_immutable
BEFORE UPDATE OR DELETE ON fleet_directive_versions
FOR EACH ROW EXECUTE FUNCTION trg_fleet_directive_versions_immutable();

CREATE TABLE IF NOT EXISTS fleet_directive_bindings (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    binding_key TEXT NOT NULL,
    binding_value TEXT NOT NULL,
    version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(target_type, target_id, binding_key, version)
);
CREATE INDEX IF NOT EXISTS idx_fleet_directive_bindings_target
    ON fleet_directive_bindings (target_type, target_id, active, binding_key);

CREATE TABLE IF NOT EXISTS fleet_directive_checks (
    id TEXT PRIMARY KEY,
    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
    directive_version INTEGER NOT NULL,
    directive_digest TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    report TEXT NOT NULL,
    checked_by TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fleet_directive_checks_directive
    ON fleet_directive_checks (directive_id, directive_version, checked_at);

CREATE TABLE IF NOT EXISTS fleet_directive_approvals (
    id TEXT PRIMARY KEY,
    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
    directive_version INTEGER NOT NULL,
    directive_digest TEXT NOT NULL,
    check_id TEXT NOT NULL REFERENCES fleet_directive_checks(id),
    context_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    UNIQUE(directive_id, directive_version, directive_digest)
);

CREATE TABLE IF NOT EXISTS fleet_directive_activations (
    id TEXT PRIMARY KEY,
    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
    directive_version INTEGER NOT NULL,
    directive_digest TEXT NOT NULL,
    check_id TEXT NOT NULL REFERENCES fleet_directive_checks(id),
    approval_id TEXT NOT NULL REFERENCES fleet_directive_approvals(id),
    epoch INTEGER NOT NULL UNIQUE,
    state TEXT NOT NULL,
    cohort TEXT NOT NULL,
    expected_acks INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    deactivated_at TEXT,
    deactivated_by TEXT,
    deactivation_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_fleet_directive_activations_state_epoch
    ON fleet_directive_activations (state, epoch);
ALTER TABLE fleet_directive_activations
    ADD COLUMN IF NOT EXISTS deactivated_by TEXT;
ALTER TABLE fleet_directive_activations
    ADD COLUMN IF NOT EXISTS deactivation_reason TEXT;

CREATE TABLE IF NOT EXISTS fleet_directive_acks (
    id TEXT PRIMARY KEY,
    activation_id TEXT NOT NULL REFERENCES fleet_directive_activations(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    directive_digest TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    UNIQUE(activation_id, agent_id)
);

CREATE TABLE IF NOT EXISTS fleet_directive_waivers (
    id TEXT PRIMARY KEY,
    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
    directive_version INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_by TEXT,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_fleet_directive_waivers_lookup
    ON fleet_directive_waivers (directive_id, directive_version, target_type, target_id);

CREATE TABLE IF NOT EXISTS fleet_directive_macro_instances (
    id TEXT PRIMARY KEY,
    activation_id TEXT NOT NULL REFERENCES fleet_directive_activations(id) ON DELETE CASCADE,
    repository_id TEXT NOT NULL,
    work_package_id TEXT,
    state TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(activation_id, repository_id)
);
CREATE INDEX IF NOT EXISTS idx_fleet_directive_macro_instances_state
    ON fleet_directive_macro_instances (state, updated_at);

CREATE TABLE IF NOT EXISTS agent_lifecycle_events (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_lifecycle_events_agent_created
    ON agent_lifecycle_events (agent_id, created_at);

CREATE TABLE IF NOT EXISTS agent_events (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent_created ON agent_events (agent_id, created_at);

CREATE TABLE IF NOT EXISTS mood_overlays (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    reason TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    expires_at TEXT,
    cleared_at TEXT,
    cleared_by TEXT,
    cleared_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_mood_overlays_agent_set_at ON mood_overlays (agent_id, set_at);

-- Allowlisted runtime-settable agent configuration flags. An empty channel is
-- agent-global; otherwise the value is scoped to a gateway channel key.
CREATE TABLE IF NOT EXISTS agent_config_flags (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    flag TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL,
    set_by TEXT,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, flag, channel)
);

-- One consolidated deploy-config document per agent: the non-secret "geek
-- knobs" its gateway actually launched with, self-reported at gateway startup.
CREATE TABLE IF NOT EXISTS agent_deploy_configs (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    document TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nap_schedules (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    offset_minutes INTEGER NOT NULL,
    window_minutes INTEGER NOT NULL DEFAULT 15,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nap_runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_evidence_id TEXT,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nap_runs_agent_started ON nap_runs (agent_id, started_at);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    reviewer_agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    evidence_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    -- WHAT the reviewer said, not merely which way it voted. `reason` is a
    -- caller-chosen template, so without this the row carried a boolean and
    -- nothing auditable: a sample of 52 reviews on 2026-08-17 held exactly
    -- four distinct reason strings and zero findings.
    findings TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reviews_task_status ON reviews (task_id, status);

CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_id TEXT,
    content_hash TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    rotated_at TEXT,
    enabled INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS secret_access_audit (
    id TEXT PRIMARY KEY,
    secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE CASCADE,
    accessor_agent_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    result TEXT NOT NULL,
    expires_at TEXT,
    revealed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_threads (
    id TEXT PRIMARY KEY,
    platform_binding_id TEXT NOT NULL REFERENCES platform_bindings(id) ON DELETE CASCADE,
    external_thread_id TEXT NOT NULL,
    latest_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    metadata TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(platform_binding_id, external_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_threads_binding
    ON conversation_threads (platform_binding_id, last_seen_at);

CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    subject_type TEXT NOT NULL,
    subject_id TEXT,
    record_type TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_subject ON memory_records (subject_type, subject_id);

CREATE TABLE IF NOT EXISTS vector_refs (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
    vector_db TEXT NOT NULL,
    collection TEXT NOT NULL,
    point_id TEXT NOT NULL,
    embedding_model TEXT,
    metadata TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(vector_db, collection, point_id)
);
CREATE INDEX IF NOT EXISTS idx_vector_refs_memory ON vector_refs (memory_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    uri TEXT NOT NULL,
    sbom_uri TEXT,
    signers TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts (kind);

CREATE TABLE IF NOT EXISTS environments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
    channel TEXT NOT NULL DEFAULT 'fleet',
    promotes_from TEXT REFERENCES environments(id) ON DELETE SET NULL,
    metadata TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, name)
);

CREATE TABLE IF NOT EXISTS environment_events (
    id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_environment_events_env
    ON environment_events (environment_id, created_at);

CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    status TEXT NOT NULL,
    deployed_by TEXT NOT NULL,
    deployed_at TEXT NOT NULL,
    retired_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_deployments_env_active
    ON deployments (environment_id, retired_at);

CREATE TABLE IF NOT EXISTS runtime_environments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    manifest TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_environment_deltas (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    project TEXT,
    base_runtime_id TEXT REFERENCES runtime_environments(id) ON DELETE SET NULL,
    base_runtime_digest TEXT,
    package_manager TEXT NOT NULL,
    commands TEXT NOT NULL,
    added_dependencies TEXT NOT NULL,
    lockfile_path TEXT,
    lockfile_digest TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    validation TEXT NOT NULL DEFAULT '{}',
    evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
    promoted_runtime_environment_id TEXT REFERENCES runtime_environments(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    validated_at TEXT,
    promoted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runtime_deltas_status
    ON runtime_environment_deltas (status, created_at);
CREATE INDEX IF NOT EXISTS idx_runtime_deltas_task
    ON runtime_environment_deltas (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runtime_deltas_project
    ON runtime_environment_deltas (project, created_at);

CREATE TABLE IF NOT EXISTS runtime_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    environment_id TEXT NOT NULL REFERENCES runtime_environments(id),
    status TEXT NOT NULL,
    evidence_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_status_name ON projects (status, name);

-- A named task group: an expression, not a frozen list of ids.
-- Storing the selector rather than its members means the group re-evaluates
-- against the ledger every time it is used, so "everything parked in mac"
-- stays true as tasks enter and leave it. A materialised membership list
-- would start rotting the moment it was written.
CREATE TABLE IF NOT EXISTS task_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    expression TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'human',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_groups_name ON task_groups (name);

CREATE TABLE IF NOT EXISTS project_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_events_project_created
    ON project_events (project_id, created_at);

CREATE TABLE IF NOT EXISTS project_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS project_repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    source TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
    last_polled_at TEXT,
    last_imported_at TEXT,
    last_error TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_repositories_enabled
    ON project_repositories (enabled, last_polled_at);

CREATE TABLE IF NOT EXISTS integration_observations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    authority TEXT NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT,
    cursor TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_observations_source
    ON integration_observations (source_kind, source_id, observed_at);

CREATE TABLE IF NOT EXISTS integration_findings (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    UNIQUE(source_kind, source_id, finding_type, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_integration_findings_status
    ON integration_findings (status, severity, last_seen_at);

CREATE TABLE IF NOT EXISTS rollouts (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    target_percent INTEGER NOT NULL,
    tenant_id TEXT,
    channel TEXT NOT NULL DEFAULT 'fleet',
    runtime_environment_id TEXT,
    artifact_uri TEXT,
    artifact_hash TEXT,
    health_policy TEXT NOT NULL DEFAULT '{}',
    required_eval_set_id TEXT,
    deploy_environment_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollout_events (
    id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_sets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    scoring TEXT NOT NULL,
    baseline_score DOUBLE PRECISION,
    regression_threshold DOUBLE PRECISION NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    eval_set_id TEXT NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    baseline_score DOUBLE PRECISION,
    delta DOUBLE PRECISION,
    threshold DOUBLE PRECISION NOT NULL,
    passed INTEGER NOT NULL,
    detail TEXT NOT NULL,
    evidence_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_set_target
    ON eval_runs (eval_set_id, target_kind, target_id, created_at);

CREATE TABLE IF NOT EXISTS eval_set_events (
    id TEXT PRIMARY KEY,
    eval_set_id TEXT NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_set_events_set ON eval_set_events (eval_set_id, created_at);

CREATE TABLE IF NOT EXISTS scientific_policies (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    parameters TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project, name, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scientific_policies_one_active
    ON scientific_policies(project) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_scientific_policies_project_status
    ON scientific_policies(project, status, updated_at);

CREATE TABLE IF NOT EXISTS scientific_experiments (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    state TEXT NOT NULL,
    running_slot TEXT UNIQUE,
    control_policy_id TEXT NOT NULL REFERENCES scientific_policies(id),
    treatment_policy_id TEXT NOT NULL REFERENCES scientific_policies(id),
    primary_metric TEXT NOT NULL,
    direction TEXT NOT NULL,
    min_effect DOUBLE PRECISION NOT NULL DEFAULT 0,
    quality_margin DOUBLE PRECISION NOT NULL DEFAULT 0.05,
    min_samples_per_arm INTEGER NOT NULL,
    max_samples_per_arm INTEGER NOT NULL,
    exploration_fraction DOUBLE PRECISION NOT NULL,
    outcome_horizon_seconds DOUBLE PRECISION NOT NULL,
    guardrails TEXT NOT NULL DEFAULT '{}',
    auto_promote INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scientific_experiments_project_state
    ON scientific_experiments(project, state, created_at);

CREATE TABLE IF NOT EXISTS scientific_assignments (
    experiment_id TEXT NOT NULL REFERENCES scientific_experiments(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    arm TEXT NOT NULL,
    policy_id TEXT NOT NULL REFERENCES scientific_policies(id),
    phase TEXT NOT NULL,
    propensity DOUBLE PRECISION NOT NULL,
    stratum TEXT NOT NULL DEFAULT '',
    assignment TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(experiment_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_scientific_assignments_experiment_arm
    ON scientific_assignments(experiment_id, phase, arm, assigned_at);

CREATE TABLE IF NOT EXISTS scientific_observations (
    experiment_id TEXT NOT NULL REFERENCES scientific_experiments(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    arm TEXT NOT NULL,
    phase TEXT NOT NULL,
    terminal INTEGER NOT NULL DEFAULT 0,
    quality_validated INTEGER NOT NULL DEFAULT 0,
    metrics TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(experiment_id, task_id)
);

CREATE TABLE IF NOT EXISTS scientific_decisions (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES scientific_experiments(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scientific_decisions_experiment
    ON scientific_decisions(experiment_id, created_at);

CREATE TABLE IF NOT EXISTS scientific_optimizer_events (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scientific_optimizer_events_subject
    ON scientific_optimizer_events(subject_type, subject_id, created_at);

CREATE TABLE IF NOT EXISTS scientific_optimizer_locks (
    name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_roles (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT,
    description TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    level TEXT NOT NULL,
    reports_to TEXT REFERENCES agent_roles(id) ON DELETE SET NULL,
    specialties TEXT NOT NULL DEFAULT '[]',
    default_capabilities TEXT NOT NULL DEFAULT '[]',
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    hardware_requirements TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    is_default INTEGER NOT NULL DEFAULT 0,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(slug, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_roles_slug_tenant ON agent_roles (slug, tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_roles_reports_to ON agent_roles (reports_to);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    workflow_type TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    definition TEXT NOT NULL,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(slug, tenant_id, version)
);
CREATE INDEX IF NOT EXISTS idx_workflows_type_enabled ON workflows (workflow_type, enabled);

CREATE TABLE IF NOT EXISTS workflow_drafts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    proposed_steps TEXT NOT NULL DEFAULT '[]',
    questions TEXT NOT NULL DEFAULT '[]',
    answers TEXT NOT NULL DEFAULT '{}',
    edit_history TEXT NOT NULL DEFAULT '[]',
    compiled_workflow_id TEXT REFERENCES workflows(id) ON DELETE SET NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_status ON workflow_drafts (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_drafts_tenant ON workflow_drafts (tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    workflow_version INTEGER NOT NULL,
    definition_snapshot TEXT NOT NULL,
    state TEXT NOT NULL,
    current_node_key TEXT,
    current_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    input TEXT NOT NULL DEFAULT '{}',
    context TEXT NOT NULL DEFAULT '{}',
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    started_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_action_at TEXT,
    completed_at TEXT
);
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS next_action_at TEXT;
CREATE INDEX IF NOT EXISTS idx_workflow_runs_state ON workflow_runs (state, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_next_action
    ON workflow_runs (state, next_action_at, id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_current_task ON workflow_runs (current_task_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs (workflow_id, created_at);

CREATE TABLE IF NOT EXISTS workflow_run_history (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    from_node_key TEXT,
    to_node_key TEXT,
    condition TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    actor TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_workflow_run_history_run ON workflow_run_history (run_id, seq);


CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_task_identity
    ON evidence (id, task_id);


CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_assignment_identity
    ON leases (id, task_id, agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_task_identity
    ON evidence (id, task_id);


CREATE UNIQUE INDEX IF NOT EXISTS uniq_publications_task_evidence_identity
    ON publications (id, task_id, evidence_id);


-- One-time, append-only data-migration receipts keep schema startup from
-- rescanning the historical task/package catalogs after a backfill succeeds.
CREATE TABLE IF NOT EXISTS telemetry_data_migrations (
    version TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(detail::jsonb) = 'object'),
    applied_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION trg_telemetry_data_migration_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'telemetry data migrations are immutable';
    END IF;
    RAISE EXCEPTION 'telemetry data migrations are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_telemetry_data_migration_append_only
    ON telemetry_data_migrations;
CREATE TRIGGER trg_telemetry_data_migration_append_only
    BEFORE UPDATE OR DELETE ON telemetry_data_migrations
    FOR EACH ROW EXECUTE FUNCTION trg_telemetry_data_migration_append_only();

-- One-time, append-only schema-rename receipts. Startup records that a
-- structural migration (e.g. hermes_instances -> persona_instances) has run so
-- it never re-inspects the catalog. Mirrors SQLiteStore.schema_migration_receipts.
CREATE TABLE IF NOT EXISTS schema_migration_receipts (
    version TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    applied_at TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION trg_schema_migration_receipt_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'schema migration receipts are immutable';
    END IF;
    RAISE EXCEPTION 'schema migration receipts are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_schema_migration_receipt_append_only
    ON schema_migration_receipts;
CREATE TRIGGER trg_schema_migration_receipt_append_only
    BEFORE UPDATE OR DELETE ON schema_migration_receipts
    FOR EACH ROW EXECUTE FUNCTION trg_schema_migration_receipt_append_only();


CREATE TABLE IF NOT EXISTS agent_provisioning_requests (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    role_slug TEXT,
    capabilities TEXT NOT NULL DEFAULT '[]',
    hardware TEXT NOT NULL DEFAULT '{}',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    detail TEXT NOT NULL DEFAULT '{}',
    fulfilled_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    requested_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_provisioning_status
    ON agent_provisioning_requests (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_provisioning_role
    ON agent_provisioning_requests (role_slug, status);

-- ============================================================================
-- Unified audit stream. Operators query one surface (`events`) instead of
-- joining ~10 per-resource tables. Rewritten from the SQLite version using
-- jsonb_build_object / jsonb_set, then cast back to text so the column type
-- matches what the rest of the codebase expects from `detail`.
-- ============================================================================
CREATE OR REPLACE VIEW events AS
    SELECT
        id,
        'task' AS subject_type,
        task_id AS subject_id,
        event_type,
        actor,
        (
            -- jsonb_set is STRICT — a NULL replacement collapses the
            -- whole expression to NULL. SQLite json_set encodes NULL as
            -- JSON null instead, which is the behavior the rest of the
            -- code expects. Wrap to_jsonb() in COALESCE so a SQL NULL
            -- from_state/to_state lands as `null` inside the object.
            jsonb_set(
                jsonb_set(
                    COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb),
                    '{from_state}',
                    COALESCE(to_jsonb(from_state), 'null'::jsonb),
                    true
                ),
                '{to_state}',
                COALESCE(to_jsonb(to_state), 'null'::jsonb),
                true
            )
        )::text AS detail,
        created_at
    FROM task_history
    UNION ALL
    SELECT id, 'rollout' AS subject_type, rollout_id AS subject_id,
           event_type, actor, detail, created_at
    FROM rollout_events
    UNION ALL
    SELECT id, 'eval_set' AS subject_type, eval_set_id AS subject_id,
           event_type, actor, detail, created_at
    FROM eval_set_events
    UNION ALL
    SELECT
        id,
        'secret' AS subject_type,
        secret_id AS subject_id,
        'secret.' || result AS event_type,
        accessor_agent_id AS actor,
        jsonb_build_object(
            'purpose', purpose,
            'expires_at', expires_at,
            'revealed_at', revealed_at
        )::text AS detail,
        created_at
    FROM secret_access_audit
    UNION ALL
    SELECT id, 'environment' AS subject_type, environment_id AS subject_id,
           event_type, actor, detail, created_at
    FROM environment_events
    UNION ALL
    SELECT id, 'project' AS subject_type, project_id AS subject_id,
           event_type, actor, detail, created_at
    FROM project_events
    UNION ALL
    SELECT id, 'fleet' AS subject_type, fleet_id AS subject_id,
           event_type, actor, detail, created_at
    FROM fleet_events
    UNION ALL
    SELECT id, 'agent' AS subject_type, agent_id AS subject_id,
           event_type, actor, detail, created_at
    FROM agent_lifecycle_events
    UNION ALL
    SELECT id, 'agent' AS subject_type, agent_id AS subject_id,
           event_type, actor, detail, created_at
    FROM agent_events
    UNION ALL
    SELECT
        id,
        CASE WHEN task_id IS NOT NULL THEN 'task' ELSE 'agent' END AS subject_type,
        COALESCE(task_id, agent_id) AS subject_id,
        'command.' || phase AS event_type,
        agent_id AS actor,
        jsonb_build_object(
            'command_id', command_id,
            'agent_id', agent_id,
            'argv0', json_extract(argv, '$[0]'),
            'argv_redacted', true,
            'cwd', cwd,
            'task_id', task_id,
            'lease_id', lease_id,
            'started_at', started_at,
            'completed_at', completed_at,
            'duration_ms', duration_ms,
            'returncode', returncode,
            'stdout_sha256', stdout_sha256,
            'stderr_sha256', stderr_sha256,
            'stdout_bytes', stdout_bytes,
            'stderr_bytes', stderr_bytes,
            'metadata',
                CASE WHEN metadata IS NULL OR metadata = ''
                     THEN NULL
                     ELSE metadata::jsonb END
        )::text AS detail,
        created_at
    FROM command_audit
    UNION ALL
    SELECT
        event_id AS id,
        COALESCE(NULLIF(subject_type, ''), 'action_event') AS subject_type,
        COALESCE(subject_id, event_id) AS subject_id,
        'action.' || action_type || '.' || action_name AS event_type,
        actor,
        jsonb_build_object(
            'schema', 'mac.action_event.v1',
            'agent_id', agent_id,
            'hermes_instance_id', hermes_instance_id,
            'task_id', task_id,
            'session_id', session_id,
            'sandbox_id', sandbox_id,
            'action_type', action_type,
            'action_name', action_name,
            'outcome', outcome,
            'severity', severity,
            'policy_id', policy_id,
            'policy_version', policy_version,
            'command_id', command_id,
            'parent_event_id', parent_event_id,
            'redaction_state', redaction_state,
            'attributes',
                CASE WHEN attributes IS NULL OR attributes = ''
                     THEN '{}'::jsonb
                     ELSE attributes::jsonb END
        )::text AS detail,
        timestamp AS created_at
    FROM action_events
    UNION ALL
    SELECT
        id,
        'conversation_thread' AS subject_type,
        id AS subject_id,
        'gateway.thread_tracked' AS event_type,
        'gateway' AS actor,
        jsonb_build_object(
            'platform_binding_id', platform_binding_id,
            'external_thread_id', external_thread_id,
            'latest_task_id', latest_task_id,
            'summary', summary
        )::text AS detail,
        last_seen_at AS created_at
    FROM conversation_threads
    UNION ALL
    SELECT
        id,
        'vector_ref' AS subject_type,
        memory_id AS subject_id,
        'vector.indexed' AS event_type,
        created_by AS actor,
        jsonb_build_object(
            'vector_db', vector_db,
            'collection', collection,
            'point_id', point_id,
            'embedding_model', embedding_model
        )::text AS detail,
        created_at
    FROM vector_refs;


-- ============================================================================
-- Source release registry (mac.source_release.v1)
-- Mirrors SQLiteStore source_releases table for PostgreSQL.
-- Immutable record of a reviewed/published commit.
-- ============================================================================
CREATE TABLE IF NOT EXISTS source_releases (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    canonical_remote_url TEXT NOT NULL,
    -- Immutable 40-char hex SHA
    commit_sha TEXT NOT NULL,
    canonical_ref TEXT NOT NULL,
    tree_digest TEXT NOT NULL,
    artifact_digest TEXT,
    image_digest TEXT,
    -- Creation provenance
    created_by TEXT NOT NULL,
    created_by_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    -- Evidence cross-references
    review_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
    publication_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
    -- Status lifecycle: draft | reviewed | published | retracted
    status TEXT NOT NULL DEFAULT 'draft',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- One canonical release per (repository, sha)
    UNIQUE(repository_id, commit_sha),
    -- Enforce 40-char hex SHA format
    CHECK(length(commit_sha) = 40 AND commit_sha ~ '^[0-9a-f]+$'),
    -- Reject branch refs
    CHECK(canonical_ref NOT LIKE 'refs/heads/%')
);
CREATE INDEX IF NOT EXISTS idx_source_releases_repo_status
    ON source_releases (repository_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_source_releases_status_created
    ON source_releases (status, created_at);

-- Immutability trigger: commit_sha may never change after creation.
CREATE OR REPLACE FUNCTION _trg_source_releases_sha_immutable()
RETURNS trigger AS $$
BEGIN
    IF NEW.commit_sha <> OLD.commit_sha THEN
        RAISE EXCEPTION 'source_releases.commit_sha is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_source_releases_sha_immutable ON source_releases;
CREATE TRIGGER trg_source_releases_sha_immutable
BEFORE UPDATE OF commit_sha ON source_releases
FOR EACH ROW EXECUTE FUNCTION _trg_source_releases_sha_immutable();

-- ============================================================================
-- Fleet desired-source state (mac.fleet_desired_source.v1)
-- Mirrors SQLiteStore fleet_desired_source_states table for PostgreSQL.
-- ============================================================================
CREATE TABLE IF NOT EXISTS fleet_desired_source_states (
    id TEXT PRIMARY KEY,
    fleet_id TEXT REFERENCES fleets(id) ON DELETE CASCADE,
    environment_id TEXT REFERENCES environments(id) ON DELETE CASCADE,
    -- Monotonic generation counter (>= 1)
    generation INTEGER NOT NULL,
    release_id TEXT NOT NULL REFERENCES source_releases(id),
    rollout_policy TEXT NOT NULL DEFAULT 'immediate',
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    prior_generation INTEGER,
    paused INTEGER NOT NULL DEFAULT 0,
    request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Generation must be positive
    CHECK(generation >= 1),
    -- Scope exclusivity: at least one scope column must be set
    CHECK(fleet_id IS NOT NULL OR environment_id IS NOT NULL)
);
-- Partial unique indexes mirror SQLite's partial-index UNIQUE constraints.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_fleet
    ON fleet_desired_source_states (fleet_id) WHERE fleet_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_env
    ON fleet_desired_source_states (environment_id) WHERE environment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_fleet
    ON fleet_desired_source_states (fleet_id, generation);
CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_env
    ON fleet_desired_source_states (environment_id, generation);

-- Monotonicity trigger: generation may only increase.
CREATE OR REPLACE FUNCTION _trg_fleet_desired_source_gen_monotonic()
RETURNS trigger AS $$
BEGIN
    IF NEW.generation <= OLD.generation THEN
        RAISE EXCEPTION 'fleet_desired_source_states.generation must increase monotonically';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fleet_desired_source_gen_monotonic ON fleet_desired_source_states;
CREATE TRIGGER trg_fleet_desired_source_gen_monotonic
BEFORE UPDATE OF generation ON fleet_desired_source_states
FOR EACH ROW EXECUTE FUNCTION _trg_fleet_desired_source_gen_monotonic();

-- ============================================================================
-- Desired-source transition history (append-only audit log)
-- ============================================================================
CREATE TABLE IF NOT EXISTS fleet_desired_source_transitions (
    id TEXT PRIMARY KEY,
    desired_source_state_id TEXT NOT NULL
        REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
    from_generation INTEGER,
    to_generation INTEGER NOT NULL,
    release_id TEXT NOT NULL REFERENCES source_releases(id),
    rollout_policy TEXT NOT NULL DEFAULT 'immediate',
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    request_id TEXT,
    created_at TEXT NOT NULL,
    CHECK(to_generation >= 1)
);
CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_transitions_state
    ON fleet_desired_source_transitions (desired_source_state_id, to_generation);
CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_transitions_release
    ON fleet_desired_source_transitions (release_id, created_at);

-- ============================================================================
-- Desired-source idempotency records
-- ============================================================================
CREATE TABLE IF NOT EXISTS fleet_desired_source_idempotency (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    desired_source_state_id TEXT NOT NULL
        REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_key, request_id)
);
CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_idempotency_scope
    ON fleet_desired_source_idempotency (scope_key, request_id);

-- ============================================================================
-- Evidence-reuse decision audit records
-- Durable trail of prior-executor-evidence reuse decisions (see
-- src/mac/evidence_reuse_verifier.py). SQLite stores ``reused`` as INTEGER;
-- Postgres uses the same INTEGER column so the shared DDL and 0/1 params
-- round-trip identically across backends.
-- ============================================================================
CREATE TABLE IF NOT EXISTS evidence_reuse_records (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    source_evidence_id TEXT NOT NULL,
    remote_url TEXT,
    expected_head_sha TEXT,
    reused INTEGER NOT NULL,
    verification TEXT NOT NULL,
    problems TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reused_by_agent_id TEXT NOT NULL DEFAULT '',
    reuse_context TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_evidence_reuse_records_task
    ON evidence_reuse_records (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_reuse_records_source
    ON evidence_reuse_records (source_evidence_id, created_at);

-- Durable per-node source convergence state and multi-replica controller lease.
CREATE TABLE IF NOT EXISTS source_convergence_nodes (
    id TEXT PRIMARY KEY,
    desired_source_state_id TEXT NOT NULL
        REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    desired_generation INTEGER NOT NULL,
    release_id TEXT NOT NULL REFERENCES source_releases(id),
    desired_sha TEXT NOT NULL,
    actual_sha TEXT,
    action TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    phase TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    request_id TEXT,
    stream_id TEXT REFERENCES agentbus_streams(id) ON DELETE SET NULL,
    next_retry_at TEXT,
    blocker_code TEXT,
    blocker_detail TEXT,
    result TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(fleet_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_source_convergence_nodes_phase
    ON source_convergence_nodes (fleet_id, desired_generation, phase);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_convergence_nodes_request
    ON source_convergence_nodes (request_id) WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS source_convergence_controller_leases (
    scope_key TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ============================================================================
-- Human principals registry
-- First-class assignable human identities (username / email / GitHub login)
-- and explicit group membership rows.
-- ============================================================================
CREATE TABLE IF NOT EXISTS humans (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT,
    github_login TEXT UNIQUE,
    display_name TEXT,
    groups TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- WHO owns this agent, and who may use it.
--
-- A static worker on its owner's own network is not fleet capacity: advertising
-- it fleet-wide is not a scheduling inefficiency, it is a FALSE CLAIM, and the
-- allocator will place work on a machine the rest of the fleet cannot reach.
-- An internet-reachable worker may equally hold data its owner will register
-- against their own virtual fleet and no further.
--
-- visibility defaults to 'shared' HERE, on purpose: every agent that exists
-- when this column is added is already being used as shared capacity, and
-- defaulting to private would strand the entire running fleet at once. New
-- registrations default to private in the service layer, where the safe
-- default belongs.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_human_id TEXT;
-- The foreign key is added separately, and only when absent, because
-- ADD COLUMN IF NOT EXISTS skips the WHOLE statement once the column exists.
-- Attaching the reference to the column definition therefore means that any
-- database where the column outlived the constraint -- a humans table dropped
-- and recreated, a restore that reordered objects -- never gets it back, and
-- an agent can then be owned by a principal that does not exist.
DO $$
BEGIN
    -- Scoped to the CURRENT schema. pg_constraint is cluster-wide, and this
    -- schema is applied per-schema (every test gets its own, and a hub can
    -- host more than one). An unscoped check finds someone else's constraint
    -- and skips, leaving this schema without the foreign key for good.
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE c.conname = 'agents_owner_human_id_fkey'
          AND n.nspname = current_schema()
    ) THEN
        ALTER TABLE agents ADD CONSTRAINT agents_owner_human_id_fkey
            FOREIGN KEY (owner_human_id) REFERENCES humans(id) ON DELETE SET NULL;
    END IF;
END
$$;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'shared'
    CHECK (visibility IN ('private', 'shared'));
CREATE INDEX IF NOT EXISTS idx_agents_owner ON agents (owner_human_id);
CREATE INDEX IF NOT EXISTS idx_humans_username
    ON humans (username);
CREATE INDEX IF NOT EXISTS idx_humans_github_login
    ON humans (github_login)
    WHERE github_login IS NOT NULL;

CREATE TABLE IF NOT EXISTS human_groups (
    id TEXT PRIMARY KEY,
    human_id TEXT NOT NULL REFERENCES humans(id) ON DELETE CASCADE,
    group_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(human_id, group_name)
);
CREATE INDEX IF NOT EXISTS idx_human_groups_human
    ON human_groups (human_id);
CREATE INDEX IF NOT EXISTS idx_human_groups_group
    ON human_groups (group_name);

CREATE TABLE IF NOT EXISTS openclaw_conversation_executions (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    persona_instance_id TEXT NOT NULL,
    persona_id TEXT,
    agent_id TEXT,
    human_id TEXT NOT NULL,
    tenant_id TEXT,
    slack TEXT NOT NULL,
    repository TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    granted_capabilities TEXT NOT NULL DEFAULT '[]',
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    worktree TEXT,
    candidate_ref TEXT,
    candidate_sha TEXT,
    candidate_tree_digest TEXT,
    review_target_sha TEXT,
    gate_results TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_openclaw_conv_exec_persona
    ON openclaw_conversation_executions (persona_instance_id);
CREATE INDEX IF NOT EXISTS idx_openclaw_conv_exec_task
    ON openclaw_conversation_executions (task_id);

-- ============================================================================
-- Task-flow analytics (mac.task_flow_span.v1 / mac.task_completion.v1)
-- Mirrors the SQLiteStore task_flow_spans and task_completions tables for
-- PostgreSQL. Records are keyed for idempotent recompute: a span UPSERTs on
-- (task_id, attempt, stage) and a completion UPSERTs on (task_id, attempt),
-- so a backfill over historical task_history / reviews / publications
-- populates rows in place rather than appending duplicates.
-- ============================================================================
CREATE TABLE IF NOT EXISTS task_flow_spans (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    -- Canonical stage boundary name (mac.models.TASK_FLOW_STAGE_NAMES).
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    -- Derived mirror of (ended_at - started_at) in seconds; NULL while open.
    duration_seconds DOUBLE PRECISION,
    -- pending | completed | failed | cancelled
    outcome TEXT NOT NULL DEFAULT 'pending',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Idempotency key: one span per task attempt + stage. A recompute UPSERTs.
    UNIQUE(task_id, attempt, stage),
    CHECK(attempt >= 1),
    CHECK(duration_seconds IS NULL OR duration_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_task_flow_spans_task
    ON task_flow_spans (task_id, attempt);

CREATE TABLE IF NOT EXISTS task_completions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds DOUBLE PRECISION,
    -- pending | completed | failed | cancelled
    outcome TEXT NOT NULL DEFAULT 'pending',
    -- Landed commit and canonical-branch head at landing time.
    publication_sha TEXT,
    main_sha TEXT,
    -- Throughput signals.
    route_count INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER NOT NULL DEFAULT 0,
    cost_count DOUBLE PRECISION NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    rebase_count INTEGER NOT NULL DEFAULT 0,
    test_count INTEGER NOT NULL DEFAULT 0,
    -- JSON object mapping canonical stage name -> duration seconds.
    per_stage_durations TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Idempotency key: one summary per task attempt. A recompute UPSERTs.
    UNIQUE(task_id, attempt),
    CHECK(attempt >= 1),
    CHECK(duration_seconds IS NULL OR duration_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_task_completions_task
    ON task_completions (task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_task_completions_project
    ON task_completions (project, ended_at);
CREATE INDEX IF NOT EXISTS idx_task_completions_outcome_time
    ON task_completions (outcome, ended_at);

CREATE TABLE IF NOT EXISTS task_flow_snapshots (
    id TEXT PRIMARY KEY,
    project TEXT,
    observed_at TEXT NOT NULL,
    since_at TEXT NOT NULL,
    warning_seconds DOUBLE PRECISION NOT NULL,
    critical_seconds DOUBLE PRECISION NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_flow_snapshots_project_time
    ON task_flow_snapshots (project, observed_at);

CREATE TABLE IF NOT EXISTS task_stranding_episodes (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    project TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    stage TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    resolved_at TEXT,
    age_seconds DOUBLE PRECISION NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(attempt >= 1),
    CHECK(age_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_task_stranding_open
    ON task_stranding_episodes (resolved_at, severity, opened_at);
CREATE INDEX IF NOT EXISTS idx_task_stranding_project
    ON task_stranding_episodes (project, resolved_at, opened_at);

CREATE TABLE IF NOT EXISTS dispatch_rounds (
    id TEXT PRIMARY KEY,
    allocator_version TEXT NOT NULL,
    source TEXT NOT NULL,
    project TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_seconds DOUBLE PRECISION NOT NULL,
    ready_count INTEGER NOT NULL,
    free_capacity INTEGER NOT NULL,
    assignment_count INTEGER NOT NULL,
    unmatched_count INTEGER NOT NULL,
    claim_failure_count INTEGER NOT NULL,
    false_ready_count INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    CHECK(duration_seconds >= 0),
    CHECK(ready_count >= 0),
    CHECK(free_capacity >= 0),
    CHECK(assignment_count >= 0),
    CHECK(unmatched_count >= 0),
    CHECK(claim_failure_count >= 0),
    CHECK(false_ready_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_rounds_completed
    ON dispatch_rounds (completed_at);
CREATE INDEX IF NOT EXISTS idx_dispatch_rounds_project_completed
    ON dispatch_rounds (project, completed_at);

-- Current fleet-level ready-work/free-capacity mismatch.  Round rows and
-- observability events retain the history; this row gives operators a
-- contention-safe, O(1) answer for whether the mismatch is active now.
CREATE TABLE IF NOT EXISTS dispatch_mismatch_state (
    scope TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    resolved_at TEXT,
    age_seconds DOUBLE PRECISION NOT NULL,
    severity TEXT NOT NULL,
    ready_count INTEGER NOT NULL,
    free_capacity INTEGER NOT NULL,
    assignment_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(age_seconds >= 0),
    CHECK(ready_count >= 0),
    CHECK(free_capacity >= 0),
    CHECK(assignment_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_mismatch_active
    ON dispatch_mismatch_state (resolved_at, severity, opened_at);

CREATE TABLE IF NOT EXISTS task_resource_contentions (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    project TEXT,
    attempt INTEGER,
    stage TEXT NOT NULL,
    resource_class TEXT NOT NULL,
    resource_digest TEXT NOT NULL,
    reason TEXT NOT NULL,
    peer_task_ids TEXT NOT NULL DEFAULT '[]',
    wait_started_at TEXT NOT NULL,
    wait_ended_at TEXT,
    wait_seconds DOUBLE PRECISION,
    outcome TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(attempt IS NULL OR attempt >= 1),
    CHECK(wait_seconds IS NULL OR wait_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_task_resource_contention_project_time
    ON task_resource_contentions (project, created_at);
CREATE INDEX IF NOT EXISTS idx_task_resource_contention_resource
    ON task_resource_contentions (resource_class, resource_digest, created_at);
CREATE INDEX IF NOT EXISTS idx_task_resource_contention_task
    ON task_resource_contentions (task_id, created_at);

-- Fair-admission telemetry for the runtime-source publication barrier
-- (mirror of fleet_release_admission_episodes in src/mac/store.py). SQLite REAL
-- becomes DOUBLE PRECISION here; every other column, CHECK, and index is
-- identical to preserve SQLite<->Postgres parity. Persistence substrate only.
CREATE TABLE IF NOT EXISTS fleet_release_admission_episodes (
    id TEXT PRIMARY KEY,
    project TEXT,
    barrier_resource_digest TEXT NOT NULL,
    owner_kind TEXT NOT NULL,
    owner_id TEXT,
    waiter_kind TEXT NOT NULL,
    waiter_id TEXT,
    waiting_publishers INTEGER NOT NULL DEFAULT 0,
    waiting_epoch_openers INTEGER NOT NULL DEFAULT 0,
    queue_depth INTEGER NOT NULL DEFAULT 0,
    wait_started_at TEXT NOT NULL,
    wait_ended_at TEXT,
    wait_seconds DOUBLE PRECISION,
    outcome TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(owner_kind IN ('publisher', 'epoch_opener')),
    CHECK(waiter_kind IN ('publisher', 'epoch_opener')),
    CHECK(waiting_publishers >= 0),
    CHECK(waiting_epoch_openers >= 0),
    CHECK(queue_depth >= 0),
    CHECK(wait_seconds IS NULL OR wait_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_fleet_release_admission_project_time
    ON fleet_release_admission_episodes (project, created_at);
CREATE INDEX IF NOT EXISTS idx_fleet_release_admission_barrier
    ON fleet_release_admission_episodes (barrier_resource_digest, created_at);
CREATE INDEX IF NOT EXISTS idx_fleet_release_admission_owner
    ON fleet_release_admission_episodes (owner_kind, owner_id, created_at);

-- ---------------------------------------------------------------------------
-- mac's own merge queue (mac.native_merge_queue).
--
-- GitHub merge queues are an organization-only feature, so every User-owned
-- repository mac manages has no forge-provided serialization at all.  These two
-- tables are the durable state that lets mac provide it itself: an ORDERED set
-- of approved changes per (repository, canonical branch), and the AIMD
-- speculation window for that same key.
--
-- Crash safety lives in `lease_owner` / `lease_expires_at`: a hub that dies
-- mid-flight leaves a leased row that the next hub reclaims once the lease
-- expires, WITHOUT losing the entry's position.  Double-landing is prevented by
-- the terminal states plus `landing_is_safe`, which refuses to merge unless the
-- canonical tip's TREE is still the tree the entry was tested on top of.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS merge_queue_entries (
    id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    branch TEXT NOT NULL,
    task_id TEXT NOT NULL,
    pull_request_number INTEGER NOT NULL DEFAULT 0,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL,
    position INTEGER NOT NULL,
    speculation_epoch INTEGER NOT NULL DEFAULT 0,
    tested_base_sha TEXT NOT NULL DEFAULT '',
    tested_base_tree TEXT NOT NULL DEFAULT '',
    tested_merge_tree TEXT NOT NULL DEFAULT '',
    predecessors TEXT NOT NULL DEFAULT '[]',
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    eviction_reason TEXT NOT NULL DEFAULT '',
    landed_sha TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (state IN (
        'queued', 'testing', 'tested', 'landed', 'evicted', 'superseded'
    )),
    CHECK (position >= 0),
    CHECK (speculation_epoch >= 0),
    CHECK (attempts >= 0)
);
-- One LIVE entry per task per queue.  Terminal rows are excluded so a task
-- evicted from the queue can be re-admitted after it is fixed.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_merge_queue_live_task
    ON merge_queue_entries (repository, branch, task_id)
    WHERE state IN ('queued', 'testing', 'tested');
CREATE INDEX IF NOT EXISTS idx_merge_queue_entries_order
    ON merge_queue_entries (repository, branch, state, position, id);
CREATE INDEX IF NOT EXISTS idx_merge_queue_entries_lease
    ON merge_queue_entries (lease_expires_at, state);

CREATE TABLE IF NOT EXISTS merge_queue_windows (
    repository TEXT NOT NULL,
    branch TEXT NOT NULL,
    window_size INTEGER NOT NULL DEFAULT 1,
    landed_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    speculation_discarded INTEGER NOT NULL DEFAULT 0,
    last_event TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repository, branch),
    CHECK (window_size >= 1),
    CHECK (landed_count >= 0),
    CHECK (failure_count >= 0),
    CHECK (speculation_discarded >= 0)
);
