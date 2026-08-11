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
    IF NEW.state NOT IN (
        'open', 'waiting', 'blocked', 'claimed', 'running',
        'needs_review', 'reviewing', 'needs_input', 'completed',
        'failed', 'cancelled'
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
    duration_ms REAL,
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
CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_evidence
    ON evidence_artifacts (evidence_id, created_at, id);
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
CREATE INDEX IF NOT EXISTS idx_observability_events_kind_layer
    ON observability_events (kind, layer, created_at);
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
CREATE INDEX IF NOT EXISTS idx_operator_notifications_subject
    ON operator_notifications (subject_type, subject_id, created_at);

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
CREATE INDEX IF NOT EXISTS idx_action_events_agent_timestamp
    ON action_events (agent_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_action_events_task_timestamp
    ON action_events (task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_action_events_session_timestamp
    ON action_events (session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_action_events_sandbox_timestamp
    ON action_events (sandbox_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_action_events_policy_timestamp
    ON action_events (policy_id, policy_version, timestamp);
CREATE INDEX IF NOT EXISTS idx_action_events_type_outcome
    ON action_events (action_type, outcome, timestamp);

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
    completed_at TEXT
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
CREATE INDEX IF NOT EXISTS idx_secret_audit_secret_created
    ON secret_access_audit (secret_id, created_at);

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
CREATE INDEX IF NOT EXISTS idx_memory_task_created ON memory_records (task_id, created_at);
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

-- Durable, versioned unit of coordinated parallel work. A package owns
-- immutable plan snapshots and base-pinned execution epochs while concrete
-- executable work remains in the canonical tasks table.
CREATE TABLE IF NOT EXISTS work_packages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT,
    repository_id TEXT REFERENCES project_repositories(id) ON DELETE RESTRICT,
    root_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    goal TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'draft', 'admitted', 'active', 'paused', 'replanning',
        'completed', 'failed', 'cancelled'
    )),
    current_plan_version INTEGER NOT NULL DEFAULT 0 CHECK (current_plan_version >= 0),
    current_epoch INTEGER NOT NULL DEFAULT 0 CHECK (current_epoch >= 0),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (id, repository_id),
    CHECK (
        state IN ('draft', 'cancelled') OR
        (current_plan_version >= 1 AND current_epoch >= 1)
    )
);
CREATE INDEX IF NOT EXISTS idx_work_packages_state
    ON work_packages (state, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_work_packages_project
    ON work_packages (project, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_work_packages_repository
    ON work_packages (repository_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_work_packages_root_task
    ON work_packages (root_task_id);

CREATE OR REPLACE FUNCTION trg_work_packages_initial_state()
RETURNS trigger AS $$
BEGIN
    IF NEW.state <> 'draft' THEN
        RAISE EXCEPTION 'work packages must start draft';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_packages_initial_state ON work_packages;
CREATE TRIGGER trg_work_packages_initial_state
    BEFORE INSERT ON work_packages
    FOR EACH ROW EXECUTE FUNCTION trg_work_packages_initial_state();

CREATE OR REPLACE FUNCTION trg_work_packages_state_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'draft' AND NEW.state IN ('admitted', 'cancelled')) OR
        (OLD.state = 'admitted' AND NEW.state IN (
            'active', 'paused', 'failed', 'cancelled'
        )) OR
        (OLD.state = 'active' AND NEW.state IN (
            'paused', 'replanning', 'completed', 'failed', 'cancelled'
        )) OR
        (OLD.state = 'paused' AND NEW.state IN (
            'active', 'replanning', 'failed', 'cancelled'
        )) OR
        (OLD.state = 'replanning' AND NEW.state IN (
            'active', 'paused', 'failed', 'cancelled'
        ))
    ) THEN
        RAISE EXCEPTION 'invalid work package state transition';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_packages_state_transition ON work_packages;
CREATE TRIGGER trg_work_packages_state_transition
    BEFORE UPDATE OF state ON work_packages
    FOR EACH ROW EXECUTE FUNCTION trg_work_packages_state_transition();

CREATE TABLE IF NOT EXISTS work_package_plan_versions (
    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    parent_version INTEGER,
    definition TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (package_id, version),
    UNIQUE (package_id, plan_digest),
    CHECK (parent_version IS NULL OR (
        parent_version >= 1 AND parent_version < version
    )),
    FOREIGN KEY (package_id, parent_version)
        REFERENCES work_package_plan_versions(package_id, version) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_plan_digest
    ON work_package_plan_versions (plan_digest);

CREATE TABLE IF NOT EXISTS work_package_epochs (
    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE CASCADE,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    planning_base_ref TEXT NOT NULL,
    planning_base_sha TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'staged', 'active', 'superseded', 'completed', 'cancelled'
    )),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    PRIMARY KEY (package_id, epoch),
    UNIQUE (package_id, epoch, plan_version),
    FOREIGN KEY (package_id, plan_version)
        REFERENCES work_package_plan_versions(package_id, version) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_active_epoch
    ON work_package_epochs (package_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_work_package_epochs_plan
    ON work_package_epochs (package_id, plan_version, epoch);

CREATE OR REPLACE FUNCTION trg_work_packages_current_epoch_coherent()
RETURNS trigger AS $$
BEGIN
    IF NEW.state NOT IN ('draft', 'cancelled') AND NOT EXISTS (
        SELECT 1 FROM work_package_epochs AS epoch
        WHERE epoch.package_id = NEW.id
          AND epoch.epoch = NEW.current_epoch
          AND epoch.plan_version = NEW.current_plan_version
          AND (
              NEW.state NOT IN ('admitted', 'active', 'paused') OR
              epoch.status = 'active'
          )
    ) THEN
        RAISE EXCEPTION 'work package current epoch/version is incoherent';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_packages_current_epoch_insert ON work_packages;
CREATE TRIGGER trg_work_packages_current_epoch_insert
    BEFORE INSERT ON work_packages
    FOR EACH ROW EXECUTE FUNCTION trg_work_packages_current_epoch_coherent();
DROP TRIGGER IF EXISTS trg_work_packages_current_epoch_update ON work_packages;
CREATE TRIGGER trg_work_packages_current_epoch_update
    BEFORE UPDATE OF state, current_plan_version, current_epoch ON work_packages
    FOR EACH ROW EXECUTE FUNCTION trg_work_packages_current_epoch_coherent();

CREATE OR REPLACE FUNCTION trg_work_package_current_epoch_status()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM work_packages AS package
        WHERE package.id = OLD.package_id
          AND package.current_epoch = OLD.epoch
          AND package.current_plan_version = OLD.plan_version
          AND package.state IN ('admitted', 'active', 'paused')
    ) AND (TG_OP = 'DELETE' OR NEW.status <> 'active') THEN
        RAISE EXCEPTION 'cannot deactivate or delete a runnable package current epoch';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_current_epoch_status ON work_package_epochs;
CREATE TRIGGER trg_work_package_current_epoch_status
    BEFORE UPDATE OF status OR DELETE ON work_package_epochs
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_current_epoch_status();

CREATE OR REPLACE FUNCTION trg_work_package_plan_versions_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'work package plan versions are immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_plan_versions_immutable
    ON work_package_plan_versions;
CREATE TRIGGER trg_work_package_plan_versions_immutable
    BEFORE UPDATE OR DELETE ON work_package_plan_versions
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_plan_versions_immutable();

CREATE OR REPLACE FUNCTION trg_work_package_epochs_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'work package epochs are append-only';
    END IF;
    IF ROW(
        NEW.package_id, NEW.epoch, NEW.plan_version, NEW.planning_base_ref,
        NEW.planning_base_sha, NEW.reason, NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.package_id, OLD.epoch, OLD.plan_version, OLD.planning_base_ref,
        OLD.planning_base_sha, OLD.reason, OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'work package epoch identity is immutable';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'staged' AND NEW.status IN ('active', 'cancelled')) OR
        (OLD.status = 'active' AND NEW.status IN (
            'superseded', 'completed', 'cancelled'
        ))
    ) THEN
        RAISE EXCEPTION 'invalid work package epoch state transition';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_epochs_lifecycle ON work_package_epochs;
CREATE TRIGGER trg_work_package_epochs_lifecycle
    BEFORE UPDATE OR DELETE ON work_package_epochs
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_epochs_lifecycle();

CREATE TABLE IF NOT EXISTS work_package_task_links (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE RESTRICT,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    node_key TEXT NOT NULL,
    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
    declared_effects_digest TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    node_state TEXT NOT NULL CHECK (node_state IN (
        'planned', 'ready', 'executing', 'candidate_submitted',
        'candidate_accepted', 'integrated', 'certified',
        'rejected', 'superseded', 'cancelled'
    )),
    created_at TEXT NOT NULL,
    UNIQUE (package_id, epoch, node_key),
    UNIQUE (package_id, node_key, node_generation),
    UNIQUE (package_id, plan_version, epoch, node_key, task_id),
    UNIQUE (
        package_id, plan_version, epoch, node_key, node_generation, task_id
    ),
    UNIQUE (
        package_id, plan_version, epoch, node_key,
        task_id, declared_effects_digest
    ),
    FOREIGN KEY (package_id, epoch, plan_version)
        REFERENCES work_package_epochs(package_id, epoch, plan_version) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_task_links_package
    ON work_package_task_links (package_id, epoch, node_key);
CREATE INDEX IF NOT EXISTS idx_work_package_task_links_state
    ON work_package_task_links (package_id, epoch, node_state, node_key);

CREATE OR REPLACE FUNCTION trg_work_package_task_links_identity_immutable()
RETURNS trigger AS $$
BEGIN
    IF ROW(
        NEW.task_id, NEW.package_id, NEW.plan_version, NEW.epoch, NEW.node_key,
        NEW.node_generation, NEW.declared_effects_digest,
        NEW.contract_digest, NEW.input_digest, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.task_id, OLD.package_id, OLD.plan_version, OLD.epoch, OLD.node_key,
        OLD.node_generation, OLD.declared_effects_digest,
        OLD.contract_digest, OLD.input_digest, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'work package task-link identity is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_task_links_identity_immutable
    ON work_package_task_links;
CREATE TRIGGER trg_work_package_task_links_identity_immutable
    BEFORE UPDATE ON work_package_task_links
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_task_links_identity_immutable();

CREATE OR REPLACE FUNCTION trg_work_package_task_links_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.node_state <> 'planned' THEN
        RAISE EXCEPTION 'work package task links must start planned';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'work package task links are append-only';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.node_state <> OLD.node_state AND NOT (
        (OLD.node_state = 'planned' AND NEW.node_state IN (
            'ready', 'superseded', 'cancelled'
        )) OR
        (OLD.node_state = 'ready' AND NEW.node_state IN (
            'executing', 'superseded', 'cancelled'
        )) OR
        (OLD.node_state = 'executing' AND NEW.node_state IN (
            'ready', 'candidate_submitted', 'rejected', 'cancelled'
        )) OR
        (OLD.node_state = 'candidate_submitted' AND NEW.node_state IN (
            'candidate_accepted', 'rejected', 'superseded'
        )) OR
        (OLD.node_state = 'candidate_accepted' AND NEW.node_state IN (
            'integrated', 'rejected', 'superseded'
        )) OR
        (OLD.node_state = 'integrated' AND NEW.node_state IN (
            'certified', 'rejected', 'superseded'
        )) OR
        (OLD.node_state = 'rejected' AND NEW.node_state = 'executing')
    ) THEN
        RAISE EXCEPTION 'invalid work package node state transition';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_task_links_lifecycle
    ON work_package_task_links;
CREATE TRIGGER trg_work_package_task_links_lifecycle
    BEFORE INSERT OR UPDATE OF node_state OR DELETE ON work_package_task_links
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_task_links_lifecycle();

-- Close the inverse mixed-version ordering: a legacy writer cannot first
-- create/claim an ordinary task and only then attach it to a package.
CREATE OR REPLACE FUNCTION trg_work_package_task_link_executable_insert()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM tasks AS task
        WHERE task.id = NEW.task_id
          AND task.state IN ('claimed', 'running')
    ) THEN
        RAISE EXCEPTION
            'executable task cannot be linked without package claim authority';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_task_link_executable_insert
    ON work_package_task_links;
CREATE TRIGGER trg_work_package_task_link_executable_insert
    BEFORE INSERT ON work_package_task_links
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_task_link_executable_insert();

CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_task_identity
    ON evidence (id, task_id);
CREATE TABLE IF NOT EXISTS work_package_node_lineage (
    id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    from_plan_version INTEGER NOT NULL,
    from_epoch INTEGER NOT NULL,
    from_node_key TEXT NOT NULL,
    from_task_id TEXT NOT NULL,
    to_plan_version INTEGER NOT NULL,
    to_epoch INTEGER NOT NULL,
    to_node_key TEXT NOT NULL,
    to_task_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN (
        'carried_forward', 'replaced', 'split', 'merged', 'invalidated'
    )),
    contract_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    source_evidence_id TEXT,
    decision TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        package_id, from_plan_version, from_epoch, from_node_key,
        to_plan_version, to_epoch, to_node_key
    ),
    FOREIGN KEY (
        package_id, from_plan_version, from_epoch, from_node_key, from_task_id
    ) REFERENCES work_package_task_links (
        package_id, plan_version, epoch, node_key, task_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        package_id, to_plan_version, to_epoch, to_node_key, to_task_id
    ) REFERENCES work_package_task_links (
        package_id, plan_version, epoch, node_key, task_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (source_evidence_id, from_task_id)
        REFERENCES evidence(id, task_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_node_lineage_from
    ON work_package_node_lineage (
        package_id, from_plan_version, from_epoch, from_node_key
    );
CREATE INDEX IF NOT EXISTS idx_work_package_node_lineage_to
    ON work_package_node_lineage (
        package_id, to_plan_version, to_epoch, to_node_key
    );

CREATE OR REPLACE FUNCTION trg_work_package_node_lineage_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'work package node lineage is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_node_lineage_immutable
    ON work_package_node_lineage;
CREATE TRIGGER trg_work_package_node_lineage_immutable
    BEFORE UPDATE OR DELETE ON work_package_node_lineage
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_node_lineage_immutable();

CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_assignment_identity
    ON leases (id, task_id, agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_task_identity
    ON evidence (id, task_id);
CREATE TABLE IF NOT EXISTS evidence_attempt_links (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    attempt_ref TEXT NOT NULL,
    attempt_base_sha TEXT NOT NULL,
    attempt_head_sha TEXT,
    artifact_digest TEXT,
    declared_effects_digest TEXT,
    observed_effects_digest TEXT,
    protected_ref INTEGER NOT NULL DEFAULT 0 CHECK (protected_ref IN (0, 1)),
    controller_verified INTEGER NOT NULL DEFAULT 0
        CHECK (controller_verified IN (0, 1)),
    controller_verifier TEXT,
    controller_verified_at TEXT,
    created_at TEXT NOT NULL,
    CHECK (protected_ref = 0 OR attempt_ref LIKE 'refs/mac/%'),
    CHECK (
        (controller_verified = 0 AND controller_verifier IS NULL
         AND controller_verified_at IS NULL) OR
        (controller_verified = 1 AND protected_ref = 1
         AND attempt_head_sha IS NOT NULL AND artifact_digest IS NOT NULL
         AND controller_verifier IS NOT NULL
         AND controller_verified_at IS NOT NULL)
    ),
    UNIQUE (evidence_id, task_id, lease_id, attempt_number),
    FOREIGN KEY (evidence_id, task_id)
        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
    FOREIGN KEY (lease_id, task_id, agent_id)
        REFERENCES leases(id, task_id, agent_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_evidence_attempt_links_lease
    ON evidence_attempt_links (lease_id, attempt_number, evidence_id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_attempt_verification_identity
    ON evidence_attempt_links (
        evidence_id, task_id, lease_id, agent_id, attempt_number,
        attempt_ref, attempt_base_sha, declared_effects_digest
    );

CREATE OR REPLACE FUNCTION trg_evidence_attempt_links_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'evidence attempt attribution is immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_evidence_attempt_links_immutable ON evidence_attempt_links;
CREATE TRIGGER trg_evidence_attempt_links_immutable
    BEFORE UPDATE OR DELETE ON evidence_attempt_links
    FOR EACH ROW EXECUTE FUNCTION trg_evidence_attempt_links_immutable();

CREATE TABLE IF NOT EXISTS work_package_assignment_audit (
    lease_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    node_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    attempt_ref TEXT NOT NULL,
    attempt_base_ref TEXT NOT NULL,
    attempt_base_sha TEXT NOT NULL,
    declared_effects_digest TEXT NOT NULL,
    allocator TEXT NOT NULL,
    allocator_version TEXT NOT NULL,
    score DOUBLE PRECISION,
    rationale TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (
        lease_id, package_id, plan_version, epoch, node_key, task_id
    ),
    UNIQUE (
        lease_id, package_id, plan_version, epoch, node_key, task_id, attempt_number
    ),
    FOREIGN KEY (
        package_id, plan_version, epoch, node_key,
        task_id, declared_effects_digest
    )
        REFERENCES work_package_task_links(
            package_id, plan_version, epoch, node_key,
            task_id, declared_effects_digest
        ) ON DELETE RESTRICT,
    FOREIGN KEY (lease_id, task_id, agent_id)
        REFERENCES leases(id, task_id, agent_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_assignment_package
    ON work_package_assignment_audit (package_id, epoch, created_at);
CREATE INDEX IF NOT EXISTS idx_work_package_assignment_agent
    ON work_package_assignment_audit (agent_id, created_at);

CREATE OR REPLACE FUNCTION trg_work_package_assignment_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'work package assignment audit is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_assignment_immutable
    ON work_package_assignment_audit;
CREATE TRIGGER trg_work_package_assignment_immutable
    BEFORE UPDATE OR DELETE ON work_package_assignment_audit
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_assignment_immutable();

-- Mixed-version safety boundary: a hub that only understands the legacy task
-- state machine must not claim a work-package task without the package
-- allocator's exact immutable assignment for this lease generation.
CREATE OR REPLACE FUNCTION trg_work_package_task_claim_authority()
RETURNS trigger AS $$
BEGIN
    IF NEW.state IN ('claimed', 'running')
       AND EXISTS (
            SELECT 1 FROM work_package_task_links AS linked
            WHERE linked.task_id = NEW.id
       )
       AND NOT EXISTS (
            SELECT 1
            FROM work_package_assignment_audit AS assignment
            JOIN work_package_task_links AS linked
              ON linked.package_id = assignment.package_id
             AND linked.plan_version = assignment.plan_version
             AND linked.epoch = assignment.epoch
             AND linked.node_key = assignment.node_key
             AND linked.task_id = assignment.task_id
             AND linked.declared_effects_digest = assignment.declared_effects_digest
            JOIN leases AS lease
              ON lease.id = assignment.lease_id
             AND lease.task_id = assignment.task_id
             AND lease.agent_id = assignment.agent_id
            WHERE assignment.task_id = NEW.id
              AND assignment.lease_id = NEW.lease_id
              AND assignment.agent_id = NEW.owner_agent_id
              AND assignment.attempt_number = NEW.attempt_count
              AND lease.status = 'active'
              AND linked.node_state = 'executing'
       ) THEN
        RAISE EXCEPTION
            'work package task claim lacks exact assignment authority';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_task_claim_authority ON tasks;
CREATE TRIGGER trg_work_package_task_claim_authority
    BEFORE UPDATE OF state, owner_agent_id, lease_id, attempt_count ON tasks
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_task_claim_authority();

CREATE OR REPLACE FUNCTION trg_evidence_attempt_package_identity()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM work_package_assignment_audit AS assignment
        WHERE assignment.lease_id = NEW.lease_id
    ) AND NOT EXISTS (
        SELECT 1 FROM work_package_assignment_audit AS assignment
        WHERE assignment.lease_id = NEW.lease_id
          AND assignment.task_id = NEW.task_id
          AND assignment.agent_id = NEW.agent_id
          AND assignment.attempt_number = NEW.attempt_number
          AND assignment.attempt_ref = NEW.attempt_ref
          AND assignment.attempt_base_sha = NEW.attempt_base_sha
          AND assignment.declared_effects_digest = NEW.declared_effects_digest
    ) THEN
        RAISE EXCEPTION 'evidence attempt does not match package assignment';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_evidence_attempt_package_identity ON evidence_attempt_links;
CREATE TRIGGER trg_evidence_attempt_package_identity
    BEFORE INSERT ON evidence_attempt_links
    FOR EACH ROW EXECUTE FUNCTION trg_evidence_attempt_package_identity();

-- Worker evidence is immutable attribution.  Independent controller facts
-- are appended here instead of mutating or upgrading the worker-authored row.
CREATE TABLE IF NOT EXISTS evidence_attempt_verifications (
    id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    repository_id TEXT NOT NULL
        REFERENCES project_repositories(id) ON DELETE RESTRICT,
    attempt_ref TEXT NOT NULL CHECK (attempt_ref LIKE 'refs/mac/attempts/%'),
    attempt_base_sha TEXT NOT NULL CHECK (
        attempt_base_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
    ),
    attempt_head_sha TEXT NOT NULL CHECK (
        attempt_head_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
    ),
    tree_digest TEXT NOT NULL CHECK (tree_digest ~ '^sha256:[0-9a-f]{64}$'),
    declared_effects_digest TEXT NOT NULL,
    observed_effects_digest TEXT NOT NULL CHECK (
        observed_effects_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    changed_paths JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(changed_paths) = 'array'
    ),
    changes JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(changes) = 'array'
    ),
    verifier TEXT NOT NULL CHECK (verifier <> ''),
    verifier_version TEXT NOT NULL CHECK (verifier_version <> ''),
    verified_at TIMESTAMPTZ NOT NULL,
    receipt_digest TEXT NOT NULL UNIQUE CHECK (
        receipt_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    FOREIGN KEY (
        evidence_id, task_id, lease_id, agent_id, attempt_number,
        attempt_ref, attempt_base_sha, declared_effects_digest
    ) REFERENCES evidence_attempt_links (
        evidence_id, task_id, lease_id, agent_id, attempt_number,
        attempt_ref, attempt_base_sha, declared_effects_digest
    ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_evidence_attempt_verifications_lease
    ON evidence_attempt_verifications (lease_id, attempt_number, evidence_id);

CREATE OR REPLACE FUNCTION trg_evidence_attempt_verification_identity()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM work_package_assignment_audit AS assignment
        JOIN work_package_task_links AS link
          ON link.package_id = assignment.package_id
         AND link.plan_version = assignment.plan_version
         AND link.epoch = assignment.epoch
         AND link.node_key = assignment.node_key
         AND link.task_id = assignment.task_id
        JOIN work_packages AS package ON package.id = assignment.package_id
        WHERE assignment.lease_id = NEW.lease_id
          AND assignment.task_id = NEW.task_id
          AND assignment.agent_id = NEW.agent_id
          AND assignment.attempt_number = NEW.attempt_number
          AND assignment.attempt_ref = NEW.attempt_ref
          AND assignment.attempt_base_sha = NEW.attempt_base_sha
          AND assignment.declared_effects_digest = NEW.declared_effects_digest
          AND package.repository_id = NEW.repository_id
    ) THEN
        RAISE EXCEPTION 'attempt verification does not match package assignment';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_evidence_attempt_verification_identity
    ON evidence_attempt_verifications;
CREATE TRIGGER trg_evidence_attempt_verification_identity
    BEFORE INSERT ON evidence_attempt_verifications
    FOR EACH ROW EXECUTE FUNCTION trg_evidence_attempt_verification_identity();

CREATE OR REPLACE FUNCTION trg_evidence_attempt_verifications_immutable()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'attempt verification receipts are immutable';
    END IF;
    RAISE EXCEPTION 'attempt verification receipts are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_evidence_attempt_verifications_immutable
    ON evidence_attempt_verifications;
CREATE TRIGGER trg_evidence_attempt_verifications_immutable
    BEFORE UPDATE OR DELETE ON evidence_attempt_verifications
    FOR EACH ROW EXECUTE FUNCTION trg_evidence_attempt_verifications_immutable();

CREATE TABLE IF NOT EXISTS work_package_node_candidates (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    node_key TEXT NOT NULL,
    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
    assignment_lease_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    evidence_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'submitted', 'accepted', 'rejected', 'superseded'
    )),
    submitted_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by TEXT,
    rejection_reason TEXT,
    UNIQUE (task_id, assignment_lease_id, attempt_number),
    UNIQUE (evidence_id),
    UNIQUE (
        id, task_id, package_id, plan_version, epoch, node_key,
        node_generation, assignment_lease_id, attempt_number, evidence_id, status
    ),
    FOREIGN KEY (
        package_id, plan_version, epoch, node_key, node_generation, task_id
    )
        REFERENCES work_package_task_links(
            package_id, plan_version, epoch, node_key, node_generation, task_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (
        assignment_lease_id, package_id, plan_version, epoch,
        node_key, task_id, attempt_number
    ) REFERENCES work_package_assignment_audit (
        lease_id, package_id, plan_version, epoch,
        node_key, task_id, attempt_number
    ) ON DELETE RESTRICT,
    FOREIGN KEY (evidence_id, task_id, assignment_lease_id, attempt_number)
        REFERENCES evidence_attempt_links(
            evidence_id, task_id, lease_id, attempt_number
        ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_node_candidates_package
    ON work_package_node_candidates (package_id, epoch, status, node_key);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_accepted_candidate
    ON work_package_node_candidates (
        package_id, epoch, node_key, node_generation
    ) WHERE status = 'accepted';

CREATE OR REPLACE FUNCTION trg_work_package_node_candidate_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'submitted'
           OR NEW.accepted_at IS NOT NULL
           OR NEW.accepted_by IS NOT NULL
           OR NEW.rejection_reason IS NOT NULL THEN
            RAISE EXCEPTION 'work package candidates must start submitted';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'work package candidates are append-only';
    END IF;
    IF ROW(
        NEW.id, NEW.task_id, NEW.package_id, NEW.plan_version, NEW.epoch,
        NEW.node_key, NEW.node_generation, NEW.assignment_lease_id,
        NEW.attempt_number, NEW.evidence_id, NEW.submitted_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.task_id, OLD.package_id, OLD.plan_version, OLD.epoch,
        OLD.node_key, OLD.node_generation, OLD.assignment_lease_id,
        OLD.attempt_number, OLD.evidence_id, OLD.submitted_at
    ) THEN
        RAISE EXCEPTION 'work package candidate identity is immutable';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        OLD.status = 'submitted' AND
        NEW.status IN ('accepted', 'rejected', 'superseded')
    ) THEN
        RAISE EXCEPTION 'invalid work package candidate state transition';
    END IF;
    IF NEW.status = 'accepted' AND (
        NEW.accepted_at IS NULL OR NEW.accepted_by IS NULL
        OR NEW.rejection_reason IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'accepted candidate metadata is incoherent';
    END IF;
    IF NEW.status = 'rejected' AND (
        NEW.accepted_at IS NOT NULL OR NEW.accepted_by IS NOT NULL
        OR NEW.rejection_reason IS NULL OR NEW.rejection_reason = ''
    ) THEN
        RAISE EXCEPTION 'rejected candidate metadata is incoherent';
    END IF;
    IF NEW.status IN ('submitted', 'superseded') AND (
        NEW.accepted_at IS NOT NULL OR NEW.accepted_by IS NOT NULL
        OR NEW.rejection_reason IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'candidate terminal metadata is incoherent';
    END IF;
    IF ROW(NEW.accepted_at, NEW.accepted_by, NEW.rejection_reason)
       IS DISTINCT FROM ROW(OLD.accepted_at, OLD.accepted_by, OLD.rejection_reason)
       AND NOT (OLD.status = 'submitted' AND NEW.status <> 'submitted') THEN
        RAISE EXCEPTION 'candidate terminal metadata is immutable';
    END IF;
    IF NEW.status = 'accepted' AND NOT EXISTS (
        SELECT 1 FROM evidence_attempt_verifications AS verification
        WHERE verification.evidence_id = NEW.evidence_id
          AND verification.task_id = NEW.task_id
          AND verification.lease_id = NEW.assignment_lease_id
          AND verification.attempt_number = NEW.attempt_number
    ) THEN
        RAISE EXCEPTION 'candidate output is not controller-verified';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_node_candidate_lifecycle
    ON work_package_node_candidates;
CREATE TRIGGER trg_work_package_node_candidate_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_node_candidates
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_node_candidate_lifecycle();

CREATE OR REPLACE FUNCTION trg_work_package_task_link_candidate_state()
RETURNS trigger AS $$
BEGIN
    IF NEW.node_state IN (
        'candidate_submitted', 'candidate_accepted', 'integrated',
        'certified', 'rejected'
    ) AND NOT EXISTS (
        SELECT 1 FROM work_package_node_candidates AS candidate
        WHERE candidate.task_id = NEW.task_id
          AND candidate.package_id = NEW.package_id
          AND candidate.plan_version = NEW.plan_version
          AND candidate.epoch = NEW.epoch
          AND candidate.node_key = NEW.node_key
          AND (
              (NEW.node_state = 'candidate_submitted'
               AND candidate.status = 'submitted') OR
              (NEW.node_state IN ('candidate_accepted', 'integrated', 'certified')
               AND candidate.status = 'accepted') OR
              (NEW.node_state = 'rejected' AND candidate.status = 'rejected')
          )
    ) THEN
        RAISE EXCEPTION 'node candidate state lacks exact attempt evidence';
    END IF;
    IF OLD.node_state = 'rejected' AND NEW.node_state = 'executing'
       AND NOT EXISTS (
        SELECT 1
        FROM work_package_assignment_audit AS assignment
        JOIN tasks AS task ON task.id = NEW.task_id
        WHERE assignment.package_id = NEW.package_id
          AND assignment.plan_version = NEW.plan_version
          AND assignment.epoch = NEW.epoch
          AND assignment.node_key = NEW.node_key
          AND assignment.task_id = NEW.task_id
          AND assignment.attempt_number <= task.max_attempts
          AND assignment.attempt_number > COALESCE((
              SELECT MAX(candidate.attempt_number)
              FROM work_package_node_candidates AS candidate
              WHERE candidate.task_id = NEW.task_id
                AND candidate.status = 'rejected'
          ), 0)
    ) THEN
        RAISE EXCEPTION 'rework requires a newer bounded assignment';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_task_link_candidate_state
    ON work_package_task_links;
CREATE TRIGGER trg_work_package_task_link_candidate_state
    BEFORE UPDATE OF node_state ON work_package_task_links
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_task_link_candidate_state();

CREATE OR REPLACE FUNCTION trg_work_package_lineage_carry_forward_evidence()
RETURNS trigger AS $$
BEGIN
    IF NEW.relation = 'carried_forward' AND (
        NEW.source_evidence_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM work_package_node_candidates AS candidate
            WHERE candidate.task_id = NEW.from_task_id
              AND candidate.evidence_id = NEW.source_evidence_id
              AND candidate.package_id = NEW.package_id
              AND candidate.plan_version = NEW.from_plan_version
              AND candidate.epoch = NEW.from_epoch
              AND candidate.node_key = NEW.from_node_key
              AND candidate.status = 'accepted'
        )
    ) THEN
        RAISE EXCEPTION 'carried-forward lineage requires accepted evidence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_lineage_carry_forward_evidence
    ON work_package_node_lineage;
CREATE TRIGGER trg_work_package_lineage_carry_forward_evidence
    BEFORE INSERT ON work_package_node_lineage
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_lineage_carry_forward_evidence();

CREATE TABLE IF NOT EXISTS work_package_wip_tokens (
    id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    node_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    token_kind TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN (
        'mutation', 'candidate_buffer', 'fan_in_reservation', 'integration'
    )),
    state TEXT NOT NULL CHECK (state IN (
        'held', 'released', 'superseded', 'cancelled'
    )),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    capacity_units INTEGER NOT NULL DEFAULT 1 CHECK (capacity_units >= 1),
    reservation_key TEXT,
    predecessor_token_id TEXT REFERENCES work_package_wip_tokens(id) ON DELETE RESTRICT,
    acquired_by_assignment_lease_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    UNIQUE (package_id, epoch, resource_key, generation),
    FOREIGN KEY (
        acquired_by_assignment_lease_id, package_id, plan_version,
        epoch, node_key, task_id
    ) REFERENCES work_package_assignment_audit (
        lease_id, package_id, plan_version,
        epoch, node_key, task_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (package_id, plan_version, epoch, node_key, task_id)
        REFERENCES work_package_task_links(
            package_id, plan_version, epoch, node_key, task_id
        ) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_held_wip_resource
    ON work_package_wip_tokens (package_id, epoch, resource_key)
    WHERE state = 'held';
CREATE INDEX IF NOT EXISTS idx_work_package_wip_stage
    ON work_package_wip_tokens (package_id, epoch, stage, state, acquired_at);
CREATE INDEX IF NOT EXISTS idx_work_package_wip_reservation
    ON work_package_wip_tokens (reservation_key, state, acquired_at);

CREATE OR REPLACE FUNCTION trg_work_package_wip_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'held' OR NEW.released_at IS NOT NULL
           OR NEW.release_reason IS NOT NULL THEN
            RAISE EXCEPTION 'work package WIP tokens must start held';
        END IF;
        IF NEW.predecessor_token_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM work_package_wip_tokens AS predecessor
            WHERE predecessor.id = NEW.predecessor_token_id
              AND predecessor.package_id = NEW.package_id
              AND predecessor.epoch = NEW.epoch
              AND predecessor.resource_key = NEW.resource_key
              AND predecessor.state IN ('released', 'superseded')
        ) THEN
            RAISE EXCEPTION 'WIP transfer predecessor is not resolved';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'work package WIP tokens are append-only';
    END IF;
    IF ROW(
        NEW.id, NEW.package_id, NEW.plan_version, NEW.epoch, NEW.node_key,
        NEW.task_id, NEW.resource_key, NEW.token_kind, NEW.stage, NEW.generation,
        NEW.capacity_units, NEW.reservation_key, NEW.predecessor_token_id,
        NEW.acquired_by_assignment_lease_id, NEW.acquired_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.package_id, OLD.plan_version, OLD.epoch, OLD.node_key,
        OLD.task_id, OLD.resource_key, OLD.token_kind, OLD.stage, OLD.generation,
        OLD.capacity_units, OLD.reservation_key, OLD.predecessor_token_id,
        OLD.acquired_by_assignment_lease_id, OLD.acquired_at
    ) THEN
        RAISE EXCEPTION 'work package WIP token identity is immutable';
    END IF;
    IF NEW.state <> OLD.state AND NOT (
        OLD.state = 'held' AND
        NEW.state IN ('released', 'superseded', 'cancelled')
    ) THEN
        RAISE EXCEPTION 'invalid work package WIP state transition';
    END IF;
    IF NEW.state IN ('released', 'superseded', 'cancelled')
       AND (NEW.released_at IS NULL OR NEW.release_reason IS NULL
            OR NEW.release_reason = '') THEN
        RAISE EXCEPTION 'resolved WIP token requires release metadata';
    END IF;
    IF NEW.state = 'held' AND (
        NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'held WIP token cannot have release metadata';
    END IF;
    IF ROW(NEW.released_at, NEW.release_reason)
       IS DISTINCT FROM ROW(OLD.released_at, OLD.release_reason)
       AND NOT (OLD.state = 'held' AND NEW.state <> 'held') THEN
        RAISE EXCEPTION 'WIP release metadata is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_wip_lifecycle ON work_package_wip_tokens;
CREATE TRIGGER trg_work_package_wip_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_wip_tokens
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_wip_lifecycle();

-- A package task cannot be detached by lease expiry while leaving its DAG
-- node permanently executing.  The finalizer first appends this fenced repair
-- authority, then detaches the task, applies the recorded WIP disposition,
-- transitions the node, and finally stamps expiry_finalized_at atomically.
CREATE TABLE IF NOT EXISTS work_package_lease_expiry_repairs (
    id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    node_key TEXT NOT NULL,
    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    source_task_state TEXT NOT NULL,
    target_task_state TEXT NOT NULL CHECK (
        target_task_state IN ('open', 'waiting', 'failed', 'cancelled')
    ),
    source_node_state TEXT NOT NULL DEFAULT 'executing' CHECK (
        source_node_state = 'executing'
    ),
    target_node_state TEXT NOT NULL CHECK (
        target_node_state IN ('ready', 'cancelled')
    ),
    wip_disposition TEXT NOT NULL CHECK (
        wip_disposition IN ('retain', 'cancel')
    ),
    held_wip_count INTEGER NOT NULL CHECK (held_wip_count >= 0),
    held_wip_ids JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(held_wip_ids) = 'array' AND
        jsonb_array_length(held_wip_ids) = held_wip_count
    ),
    finalizer_token TEXT NOT NULL CHECK (finalizer_token <> ''),
    decision JSONB NOT NULL,
    decision_digest TEXT NOT NULL CHECK (
        decision_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    reason TEXT NOT NULL CHECK (reason <> ''),
    created_by TEXT NOT NULL CHECK (created_by IN ('controller', 'dispatcher')),
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (
            target_task_state IN ('open', 'waiting') AND
            target_node_state = 'ready' AND
            wip_disposition = 'retain'
        ) OR (
            target_task_state IN ('failed', 'cancelled') AND
            target_node_state = 'cancelled' AND
            wip_disposition = 'cancel'
        )
    ),
    FOREIGN KEY (
        lease_id, package_id, plan_version, epoch,
        node_key, task_id, attempt_number
    ) REFERENCES work_package_assignment_audit (
        lease_id, package_id, plan_version, epoch,
        node_key, task_id, attempt_number
    ) ON DELETE RESTRICT,
    FOREIGN KEY (lease_id, task_id, agent_id)
        REFERENCES leases(id, task_id, agent_id) ON DELETE RESTRICT,
    FOREIGN KEY (
        package_id, plan_version, epoch, node_key, node_generation, task_id
    ) REFERENCES work_package_task_links (
        package_id, plan_version, epoch, node_key, node_generation, task_id
    ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_expiry_repairs_node
    ON work_package_lease_expiry_repairs (
        package_id, epoch, node_key, attempt_number
    );

CREATE OR REPLACE FUNCTION trg_work_package_expiry_repair_authority()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM leases AS lease
        JOIN tasks AS task ON task.id = lease.task_id
        JOIN work_package_assignment_audit AS assignment
          ON assignment.lease_id = lease.id
        JOIN work_package_task_links AS link
          ON link.package_id = assignment.package_id
         AND link.plan_version = assignment.plan_version
         AND link.epoch = assignment.epoch
         AND link.node_key = assignment.node_key
         AND link.task_id = assignment.task_id
        WHERE lease.id = NEW.lease_id
          AND lease.task_id = NEW.task_id
          AND lease.agent_id = NEW.agent_id
          AND lease.status = 'expired'
          AND lease.expiry_finalizer_token = NEW.finalizer_token
          AND lease.expiry_finalized_at IS NULL
          AND lease.expiry_finalization_decision = NEW.decision
          AND task.lease_id = NEW.lease_id
          AND task.owner_agent_id = NEW.agent_id
          AND task.state = NEW.source_task_state
          AND assignment.package_id = NEW.package_id
          AND assignment.plan_version = NEW.plan_version
          AND assignment.epoch = NEW.epoch
          AND assignment.node_key = NEW.node_key
          AND assignment.task_id = NEW.task_id
          AND assignment.agent_id = NEW.agent_id
          AND assignment.attempt_number = NEW.attempt_number
          AND link.node_generation = NEW.node_generation
          AND link.node_state = NEW.source_node_state
          AND NEW.attempt_number = (
              SELECT MAX(latest.attempt_number)
              FROM work_package_assignment_audit AS latest
              WHERE latest.package_id = NEW.package_id
                AND latest.plan_version = NEW.plan_version
                AND latest.epoch = NEW.epoch
                AND latest.node_key = NEW.node_key
                AND latest.task_id = NEW.task_id
          )
    ) OR (
        SELECT COUNT(*)
        FROM work_package_wip_tokens AS token
        WHERE token.package_id = NEW.package_id
          AND token.plan_version = NEW.plan_version
          AND token.epoch = NEW.epoch
          AND token.node_key = NEW.node_key
          AND token.task_id = NEW.task_id
          AND token.state = 'held'
    ) <> NEW.held_wip_count OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(NEW.held_wip_ids) AS item(value)
        WHERE NOT EXISTS (
            SELECT 1 FROM work_package_wip_tokens AS token
            WHERE token.id = item.value
              AND token.package_id = NEW.package_id
              AND token.plan_version = NEW.plan_version
              AND token.epoch = NEW.epoch
              AND token.node_key = NEW.node_key
              AND token.task_id = NEW.task_id
              AND token.state = 'held'
        )
    ) OR EXISTS (
        SELECT 1
        FROM work_package_wip_tokens AS token
        WHERE token.package_id = NEW.package_id
          AND token.plan_version = NEW.plan_version
          AND token.epoch = NEW.epoch
          AND token.node_key = NEW.node_key
          AND token.task_id = NEW.task_id
          AND token.state = 'held'
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(NEW.held_wip_ids) AS item(value)
              WHERE item.value = token.id
          )
    ) THEN
        RAISE EXCEPTION 'lease-expiry repair lacks exact finalizer or WIP authority';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_expiry_repair_authority
    ON work_package_lease_expiry_repairs;
CREATE TRIGGER trg_work_package_expiry_repair_authority
    BEFORE INSERT ON work_package_lease_expiry_repairs
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_expiry_repair_authority();

CREATE OR REPLACE FUNCTION trg_work_package_expiry_repairs_immutable()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'lease-expiry repair receipts are immutable';
    END IF;
    RAISE EXCEPTION 'lease-expiry repair receipts are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_expiry_repairs_immutable
    ON work_package_lease_expiry_repairs;
CREATE TRIGGER trg_work_package_expiry_repairs_immutable
    BEFORE UPDATE OR DELETE ON work_package_lease_expiry_repairs
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_expiry_repairs_immutable();

CREATE OR REPLACE FUNCTION trg_work_package_expiry_task_detach_guard()
RETURNS trigger AS $$
BEGIN
    IF OLD.lease_id IS NOT NULL AND NEW.lease_id IS NULL AND EXISTS (
        SELECT 1
        FROM leases AS lease
        JOIN work_package_assignment_audit AS assignment
          ON assignment.lease_id = lease.id
        JOIN work_package_task_links AS link
          ON link.package_id = assignment.package_id
         AND link.plan_version = assignment.plan_version
         AND link.epoch = assignment.epoch
         AND link.node_key = assignment.node_key
         AND link.task_id = assignment.task_id
        WHERE lease.id = OLD.lease_id
          AND lease.task_id = OLD.id
          AND lease.status = 'expired'
          AND link.node_state = 'executing'
    ) AND NOT EXISTS (
        SELECT 1
        FROM work_package_lease_expiry_repairs AS repair
        JOIN leases AS lease ON lease.id = repair.lease_id
        WHERE repair.lease_id = OLD.lease_id
          AND repair.task_id = OLD.id
          AND repair.agent_id = OLD.owner_agent_id
          AND repair.source_task_state = OLD.state
          AND repair.target_task_state = NEW.state
          AND lease.status = 'expired'
          AND lease.expiry_finalizer_token = repair.finalizer_token
          AND lease.expiry_finalized_at IS NULL
          AND NEW.owner_agent_id IS NULL
    ) THEN
        RAISE EXCEPTION 'expired package task detach requires exact repair receipt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_expiry_task_detach_guard ON tasks;
CREATE TRIGGER trg_work_package_expiry_task_detach_guard
    BEFORE UPDATE OF lease_id ON tasks
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_expiry_task_detach_guard();

CREATE OR REPLACE FUNCTION trg_work_package_expiry_node_guard()
RETURNS trigger AS $$
BEGIN
    IF OLD.node_state = 'executing' AND NEW.node_state = 'ready' AND NOT EXISTS (
        SELECT 1
        FROM work_package_lease_expiry_repairs AS repair
        JOIN tasks AS task ON task.id = repair.task_id
        WHERE repair.package_id = NEW.package_id
          AND repair.plan_version = NEW.plan_version
          AND repair.epoch = NEW.epoch
          AND repair.node_key = NEW.node_key
          AND repair.node_generation = NEW.node_generation
          AND repair.task_id = NEW.task_id
          AND repair.source_node_state = OLD.node_state
          AND repair.target_node_state = NEW.node_state
          AND repair.wip_disposition = 'retain'
          AND task.state = repair.target_task_state
          AND task.state IN ('open', 'waiting')
          AND task.lease_id IS NULL
          AND task.owner_agent_id IS NULL
          AND repair.attempt_number = (
              SELECT MAX(latest.attempt_number)
              FROM work_package_assignment_audit AS latest
              WHERE latest.package_id = NEW.package_id
                AND latest.plan_version = NEW.plan_version
                AND latest.epoch = NEW.epoch
                AND latest.node_key = NEW.node_key
                AND latest.task_id = NEW.task_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(repair.held_wip_ids) AS item(value)
              WHERE NOT EXISTS (
                  SELECT 1 FROM work_package_wip_tokens AS token
                  WHERE token.id = item.value AND token.state = 'held'
              )
          )
          AND NOT EXISTS (
              SELECT 1 FROM work_package_wip_tokens AS token
              WHERE token.package_id = NEW.package_id
                AND token.plan_version = NEW.plan_version
                AND token.epoch = NEW.epoch
                AND token.node_key = NEW.node_key
                AND token.task_id = NEW.task_id
                AND token.state = 'held'
                AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(repair.held_wip_ids) AS item(value)
                    WHERE item.value = token.id
                )
          )
    ) THEN
        RAISE EXCEPTION 'executing node requeue requires exact lease-expiry repair';
    END IF;

    IF OLD.node_state = 'executing' AND NEW.node_state = 'cancelled'
       AND EXISTS (
           SELECT 1 FROM work_package_lease_expiry_repairs AS repair
           WHERE repair.package_id = NEW.package_id
             AND repair.plan_version = NEW.plan_version
             AND repair.epoch = NEW.epoch
             AND repair.node_key = NEW.node_key
             AND repair.node_generation = NEW.node_generation
             AND repair.task_id = NEW.task_id
             AND repair.target_node_state = 'cancelled'
       ) AND NOT EXISTS (
        SELECT 1
        FROM work_package_lease_expiry_repairs AS repair
        JOIN tasks AS task ON task.id = repair.task_id
        WHERE repair.package_id = NEW.package_id
          AND repair.plan_version = NEW.plan_version
          AND repair.epoch = NEW.epoch
          AND repair.node_key = NEW.node_key
          AND repair.node_generation = NEW.node_generation
          AND repair.task_id = NEW.task_id
          AND repair.source_node_state = OLD.node_state
          AND repair.target_node_state = NEW.node_state
          AND repair.wip_disposition = 'cancel'
          AND task.state = repair.target_task_state
          AND task.state IN ('failed', 'cancelled')
          AND task.lease_id IS NULL
          AND task.owner_agent_id IS NULL
          AND repair.attempt_number = (
              SELECT MAX(latest.attempt_number)
              FROM work_package_assignment_audit AS latest
              WHERE latest.package_id = NEW.package_id
                AND latest.plan_version = NEW.plan_version
                AND latest.epoch = NEW.epoch
                AND latest.node_key = NEW.node_key
                AND latest.task_id = NEW.task_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(repair.held_wip_ids) AS item(value)
              WHERE NOT EXISTS (
                  SELECT 1 FROM work_package_wip_tokens AS token
                  WHERE token.id = item.value
                    AND token.state = 'cancelled'
                    AND token.released_at IS NOT NULL
                    AND token.release_reason IS NOT NULL
                    AND token.release_reason <> ''
              )
          )
          AND NOT EXISTS (
              SELECT 1 FROM work_package_wip_tokens AS token
              WHERE token.package_id = NEW.package_id
                AND token.plan_version = NEW.plan_version
                AND token.epoch = NEW.epoch
                AND token.node_key = NEW.node_key
                AND token.task_id = NEW.task_id
                AND token.state = 'held'
          )
    ) THEN
        RAISE EXCEPTION 'terminal lease-expiry repair must cancel exact held WIP';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_expiry_node_guard
    ON work_package_task_links;
CREATE TRIGGER trg_work_package_expiry_node_guard
    BEFORE UPDATE OF node_state ON work_package_task_links
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_expiry_node_guard();

CREATE TABLE IF NOT EXISTS work_package_integration_batches (
    id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    repository_id TEXT REFERENCES project_repositories(id) ON DELETE RESTRICT,
    target_ref TEXT NOT NULL,
    assembly_base_sha TEXT NOT NULL CHECK (assembly_base_sha ~ '^[0-9a-f]{40}$'),
    landing_base_sha TEXT NOT NULL CHECK (landing_base_sha ~ '^[0-9a-f]{40}$'),
    input_digest TEXT NOT NULL,
    candidate_sha TEXT,
    candidate_tree_digest TEXT,
    candidate_ref TEXT,
    candidate_fence INTEGER CHECK (candidate_fence IS NULL OR candidate_fence >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'assembling', 'verifying', 'certified',
        'rejected', 'stale', 'published', 'cancelled'
    )),
    integration_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (
        id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ),
    UNIQUE (id, package_id, plan_version, epoch),
    UNIQUE (id, integration_task_id),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL) OR
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
         AND lease_fence >= 1)
    ),
    CHECK (
        (candidate_sha IS NULL AND candidate_tree_digest IS NULL
         AND candidate_ref IS NULL AND candidate_fence IS NULL) OR
        (candidate_sha IS NOT NULL AND candidate_tree_digest IS NOT NULL
         AND candidate_ref IS NOT NULL AND candidate_fence IS NOT NULL
         AND candidate_ref LIKE 'refs/mac/%')
    ),
    FOREIGN KEY (package_id, epoch, plan_version)
        REFERENCES work_package_epochs(package_id, epoch, plan_version) ON DELETE RESTRICT,
    FOREIGN KEY (package_id, repository_id)
        REFERENCES work_packages(id, repository_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_batches_queue
    ON work_package_integration_batches (state, created_at, id);
CREATE INDEX IF NOT EXISTS idx_work_package_batches_target
    ON work_package_integration_batches (repository_id, target_ref, state, created_at);
CREATE INDEX IF NOT EXISTS idx_work_package_batches_package
    ON work_package_integration_batches (package_id, epoch, created_at);

CREATE OR REPLACE FUNCTION trg_work_package_batch_repository_matches()
RETURNS trigger AS $$
DECLARE
    expected_repository TEXT;
BEGIN
    SELECT repository_id INTO expected_repository
      FROM work_packages WHERE id = NEW.package_id;
    IF NEW.repository_id IS DISTINCT FROM expected_repository THEN
        RAISE EXCEPTION 'integration batch repository must match package';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_batch_repository_matches
    ON work_package_integration_batches;
CREATE TRIGGER trg_work_package_batch_repository_matches
    BEFORE INSERT OR UPDATE OF package_id, repository_id
    ON work_package_integration_batches
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_batch_repository_matches();

CREATE OR REPLACE FUNCTION trg_work_package_batch_fence_monotonic()
RETURNS trigger AS $$
BEGIN
    IF NEW.lease_fence < OLD.lease_fence THEN
        RAISE EXCEPTION 'integration batch lease fence cannot decrease';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_batch_fence_monotonic
    ON work_package_integration_batches;
CREATE TRIGGER trg_work_package_batch_fence_monotonic
    BEFORE UPDATE OF lease_fence ON work_package_integration_batches
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_batch_fence_monotonic();

CREATE OR REPLACE FUNCTION trg_work_package_batch_invariants()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'integration batches are append-only';
    END IF;
    IF ROW(
        NEW.id, NEW.package_id, NEW.plan_version, NEW.epoch, NEW.repository_id,
        NEW.target_ref, NEW.assembly_base_sha, NEW.landing_base_sha,
        NEW.input_digest, NEW.integration_task_id, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.package_id, OLD.plan_version, OLD.epoch, OLD.repository_id,
        OLD.target_ref, OLD.assembly_base_sha, OLD.landing_base_sha,
        OLD.input_digest, OLD.integration_task_id, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'integration batch identity is immutable';
    END IF;
    IF NEW.lease_owner IS NOT NULL
       AND NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
       AND NEW.lease_fence <= OLD.lease_fence THEN
        RAISE EXCEPTION 'integration batch owner change requires a new fence';
    END IF;
    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'queued' AND NEW.state IN ('assembling', 'cancelled')) OR
        (OLD.state = 'assembling' AND NEW.state IN (
            'verifying', 'rejected', 'stale', 'cancelled'
        )) OR
        (OLD.state = 'verifying' AND NEW.state IN (
            'certified', 'rejected', 'stale', 'cancelled'
        )) OR
        (OLD.state = 'certified' AND NEW.state IN ('published', 'stale'))
    ) THEN
        RAISE EXCEPTION 'invalid integration batch state transition';
    END IF;
    IF ROW(
        NEW.candidate_sha, NEW.candidate_tree_digest,
        NEW.candidate_ref, NEW.candidate_fence
    ) IS DISTINCT FROM ROW(
        OLD.candidate_sha, OLD.candidate_tree_digest,
        OLD.candidate_ref, OLD.candidate_fence
    ) AND NOT (
        OLD.state = 'assembling' AND NEW.state = 'assembling'
        AND OLD.candidate_sha IS NULL AND OLD.candidate_tree_digest IS NULL
        AND OLD.candidate_ref IS NULL AND OLD.candidate_fence IS NULL
        AND NEW.candidate_sha IS NOT NULL
        AND NEW.candidate_tree_digest IS NOT NULL
        AND NEW.candidate_ref LIKE 'refs/mac/%'
        AND NEW.candidate_fence = OLD.lease_fence
        AND OLD.lease_owner IS NOT NULL
        AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner
        AND NEW.lease_fence = OLD.lease_fence
    ) THEN
        RAISE EXCEPTION 'integration candidate assignment requires current fence';
    END IF;
    IF NEW.state = 'verifying' AND (
        NEW.candidate_sha IS NULL OR NEW.candidate_tree_digest IS NULL
        OR NEW.candidate_ref IS NULL OR NEW.candidate_fence IS NULL
    ) THEN
        RAISE EXCEPTION 'verifying batch requires a fenced candidate';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_batch_invariants
    ON work_package_integration_batches;
CREATE TRIGGER trg_work_package_batch_invariants
    BEFORE UPDATE OR DELETE ON work_package_integration_batches
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_batch_invariants();

CREATE OR REPLACE FUNCTION trg_work_package_batch_initial_state()
RETURNS trigger AS $$
BEGIN
    IF NEW.state <> 'queued' OR NEW.candidate_sha IS NOT NULL
       OR NEW.candidate_tree_digest IS NOT NULL
       OR NEW.candidate_ref IS NOT NULL OR NEW.candidate_fence IS NOT NULL THEN
        RAISE EXCEPTION 'integration batches must start queued';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_batch_initial_state
    ON work_package_integration_batches;
CREATE TRIGGER trg_work_package_batch_initial_state
    BEFORE INSERT ON work_package_integration_batches
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_batch_initial_state();

CREATE TABLE IF NOT EXISTS work_package_batch_inputs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    node_key TEXT NOT NULL,
    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
    task_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (candidate_status = 'accepted'),
    assignment_lease_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    evidence_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, ordinal),
    UNIQUE (batch_id, evidence_id),
    FOREIGN KEY (batch_id, package_id, plan_version, epoch)
        REFERENCES work_package_integration_batches(
            id, package_id, plan_version, epoch
        ) ON DELETE CASCADE,
    FOREIGN KEY (
        assignment_lease_id, package_id, plan_version, epoch,
        node_key, task_id, attempt_number
    ) REFERENCES work_package_assignment_audit (
        lease_id, package_id, plan_version, epoch,
        node_key, task_id, attempt_number
    ) ON DELETE RESTRICT,
    FOREIGN KEY (evidence_id, task_id)
        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
    FOREIGN KEY (evidence_id, task_id, assignment_lease_id, attempt_number)
        REFERENCES evidence_attempt_links(
            evidence_id, task_id, lease_id, attempt_number
        ) ON DELETE RESTRICT,
    FOREIGN KEY (package_id, plan_version, epoch, node_key, task_id)
        REFERENCES work_package_task_links(
            package_id, plan_version, epoch, node_key, task_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (
        candidate_id, task_id, package_id, plan_version, epoch,
        node_key, node_generation, assignment_lease_id,
        attempt_number, evidence_id, candidate_status
    ) REFERENCES work_package_node_candidates (
        id, task_id, package_id, plan_version, epoch,
        node_key, node_generation, assignment_lease_id,
        attempt_number, evidence_id, status
    ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_batch_inputs_task
    ON work_package_batch_inputs (task_id, created_at);

CREATE OR REPLACE FUNCTION trg_work_package_batch_inputs_open()
RETURNS trigger AS $$
DECLARE
    parent_batch_id TEXT;
    parent_state TEXT;
BEGIN
    parent_batch_id := CASE WHEN TG_OP = 'INSERT' THEN NEW.batch_id ELSE OLD.batch_id END;
    -- Serialize membership changes with queued -> assembling. Without the row
    -- lock an INSERT and state transition can both validate an old snapshot.
    SELECT state INTO parent_state
      FROM work_package_integration_batches WHERE id = parent_batch_id
      FOR UPDATE;
    IF TG_OP = 'INSERT' AND (
        parent_state IS DISTINCT FROM 'queued' OR NOT EXISTS (
            SELECT 1 FROM work_package_task_links AS link
            WHERE link.task_id = NEW.task_id
              AND link.package_id = NEW.package_id
              AND link.plan_version = NEW.plan_version
              AND link.epoch = NEW.epoch
              AND link.node_key = NEW.node_key
              AND link.node_generation = NEW.node_generation
              AND link.node_state = 'candidate_accepted'
        ) OR NOT EXISTS (
            SELECT 1 FROM evidence_attempt_verifications AS verification
            WHERE verification.evidence_id = NEW.evidence_id
              AND verification.task_id = NEW.task_id
              AND verification.lease_id = NEW.assignment_lease_id
              AND verification.attempt_number = NEW.attempt_number
        )
    ) THEN
        RAISE EXCEPTION 'batch input is not an accepted verified candidate';
    END IF;
    IF TG_OP IN ('UPDATE', 'DELETE') AND parent_state IS DISTINCT FROM 'queued' THEN
        RAISE EXCEPTION 'batch membership is immutable after assembly starts';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_batch_inputs_open
    ON work_package_batch_inputs;
CREATE TRIGGER trg_work_package_batch_inputs_open
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_batch_inputs
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_batch_inputs_open();

CREATE UNIQUE INDEX IF NOT EXISTS uniq_publications_task_evidence_identity
    ON publications (id, task_id, evidence_id);
CREATE TABLE IF NOT EXISTS work_package_certifications (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    candidate_sha TEXT NOT NULL,
    assembly_base_sha TEXT NOT NULL,
    landing_base_sha TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'passed', 'failed', 'invalidated', 'published'
    )),
    verification_digest TEXT NOT NULL,
    verification TEXT NOT NULL DEFAULT '{}',
    certification_task_id TEXT NOT NULL,
    tests_evidence_id TEXT NOT NULL,
    review_task_id TEXT NOT NULL,
    review_evidence_id TEXT NOT NULL,
    codegraph_evidence_id TEXT,
    certified_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    publication_id TEXT,
    publication_evidence_id TEXT,
    UNIQUE (batch_id, candidate_sha, verification_digest),
    CHECK (
        (publication_id IS NULL AND publication_evidence_id IS NULL) OR
        (publication_id IS NOT NULL AND publication_evidence_id IS NOT NULL)
    ),
    FOREIGN KEY (
        batch_id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ) REFERENCES work_package_integration_batches (
        id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ) ON DELETE RESTRICT,
    FOREIGN KEY (tests_evidence_id, certification_task_id)
        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
    FOREIGN KEY (review_evidence_id, review_task_id)
        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
    FOREIGN KEY (codegraph_evidence_id, certification_task_id)
        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
    FOREIGN KEY (publication_id, certification_task_id, publication_evidence_id)
        REFERENCES publications (id, task_id, evidence_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_certifications_status
    ON work_package_certifications (status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_work_package_certifications_package
    ON work_package_certifications (package_id, epoch, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_cert_landing_identity
    ON work_package_certifications (
        id, batch_id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    );

CREATE OR REPLACE FUNCTION trg_work_package_certification_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status NOT IN ('passed', 'failed')
           OR NEW.invalidated_at IS NOT NULL
           OR NEW.publication_id IS NOT NULL
           OR NEW.publication_evidence_id IS NOT NULL THEN
            RAISE EXCEPTION 'certification must start uncommitted as passed or failed';
        END IF;
        -- Certification and batch-state transitions share the same row lock,
        -- so a cert cannot race a verifying -> terminal transition.
        PERFORM 1 FROM work_package_integration_batches AS batch
            WHERE batch.id = NEW.batch_id
              AND batch.package_id = NEW.package_id
              AND batch.plan_version = NEW.plan_version
              AND batch.epoch = NEW.epoch
              AND batch.state = 'verifying'
              AND batch.candidate_sha = NEW.candidate_sha
              AND batch.assembly_base_sha = NEW.assembly_base_sha
              AND batch.landing_base_sha = NEW.landing_base_sha
              AND batch.target_ref = NEW.target_ref
              AND batch.candidate_tree_digest IS NOT NULL
              AND batch.candidate_ref IS NOT NULL
              AND batch.candidate_fence IS NOT NULL
            FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'certification requires a finalized verifying batch';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'certifications are append-only';
    END IF;
    IF ROW(
        NEW.id, NEW.batch_id, NEW.package_id, NEW.plan_version, NEW.epoch,
        NEW.candidate_sha, NEW.assembly_base_sha, NEW.landing_base_sha,
        NEW.target_ref, NEW.verification_digest, NEW.verification,
        NEW.certification_task_id, NEW.tests_evidence_id, NEW.review_task_id,
        NEW.review_evidence_id, NEW.codegraph_evidence_id, NEW.certified_by,
        NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.batch_id, OLD.package_id, OLD.plan_version, OLD.epoch,
        OLD.candidate_sha, OLD.assembly_base_sha, OLD.landing_base_sha,
        OLD.target_ref, OLD.verification_digest, OLD.verification,
        OLD.certification_task_id, OLD.tests_evidence_id, OLD.review_task_id,
        OLD.review_evidence_id, OLD.codegraph_evidence_id, OLD.certified_by,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION 'certification identity is immutable';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'passed' AND NEW.status IN ('invalidated', 'published')) OR
        (OLD.status = 'failed' AND NEW.status = 'invalidated')
    ) THEN
        RAISE EXCEPTION 'invalid certification state transition';
    END IF;
    IF NEW.status = 'invalidated' AND (
        NEW.invalidated_at IS NULL
        OR NEW.publication_id IS NOT NULL
        OR NEW.publication_evidence_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'invalidated certification metadata is incoherent';
    END IF;
    IF NEW.status = 'published' AND (
        NEW.invalidated_at IS NOT NULL
        OR NEW.publication_id IS NULL
        OR NEW.publication_evidence_id IS NULL
    ) THEN
        RAISE EXCEPTION 'published certification metadata is incoherent';
    END IF;
    IF NEW.status IN ('passed', 'failed') AND (
        NEW.invalidated_at IS NOT NULL
        OR NEW.publication_id IS NOT NULL
        OR NEW.publication_evidence_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'uncommitted certification metadata is incoherent';
    END IF;
    IF NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at
       AND NOT (
           OLD.status IN ('passed', 'failed') AND NEW.status = 'invalidated'
       ) THEN
        RAISE EXCEPTION 'certification invalidation metadata is immutable';
    END IF;
    IF ROW(NEW.publication_id, NEW.publication_evidence_id)
       IS DISTINCT FROM ROW(OLD.publication_id, OLD.publication_evidence_id)
       AND NOT (OLD.status = 'passed' AND NEW.status = 'published') THEN
        RAISE EXCEPTION 'certification publication metadata is incoherent';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_certification_lifecycle
    ON work_package_certifications;
CREATE TRIGGER trg_work_package_certification_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_certifications
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_certification_lifecycle();

CREATE TABLE IF NOT EXISTS work_package_certification_jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    repository_id TEXT NOT NULL REFERENCES project_repositories(id) ON DELETE RESTRICT,
    candidate_sha TEXT NOT NULL CHECK (candidate_sha ~ '^[0-9a-f]{40}$'),
    candidate_tree_digest TEXT NOT NULL CHECK (
        candidate_tree_digest ~ '^git-tree:[0-9a-f]{40}$'
    ),
    candidate_ref TEXT NOT NULL CHECK (candidate_ref LIKE 'refs/mac/%'),
    candidate_fence INTEGER NOT NULL CHECK (candidate_fence >= 1),
    assembly_base_sha TEXT NOT NULL CHECK (assembly_base_sha ~ '^[0-9a-f]{40}$'),
    landing_base_sha TEXT NOT NULL CHECK (landing_base_sha ~ '^[0-9a-f]{40}$'),
    target_ref TEXT NOT NULL CHECK (target_ref LIKE 'refs/heads/%'),
    policy_id TEXT NOT NULL CHECK (policy_id <> ''),
    policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
    policy_checksum TEXT NOT NULL CHECK (
        policy_checksum ~ '^sha256:[0-9a-f]{64}$'
    ),
    image_ref TEXT NOT NULL CHECK (image_ref LIKE '%@' || image_digest),
    image_digest TEXT NOT NULL CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    bundle_digest TEXT NOT NULL CHECK (bundle_digest ~ '^sha256:[0-9a-f]{64}$'),
    commands_digest TEXT NOT NULL CHECK (commands_digest ~ '^sha256:[0-9a-f]{64}$'),
    job_digest TEXT NOT NULL UNIQUE CHECK (job_digest ~ '^sha256:[0-9a-f]{64}$'),
    definition TEXT NOT NULL CHECK (jsonb_typeof(definition::jsonb) = 'object'),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    result_digest TEXT UNIQUE CHECK (
        result_digest IS NULL OR result_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    certification_id TEXT UNIQUE
        REFERENCES work_package_certifications(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (
        id, batch_id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ),
    FOREIGN KEY (
        batch_id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) REFERENCES work_package_integration_batches (
        id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) ON DELETE RESTRICT,
    CHECK (
        (state = 'queued' AND lease_owner IS NULL
         AND lease_expires_at IS NULL AND lease_fence = 0
         AND result_digest IS NULL AND certification_id IS NULL
         AND completed_at IS NULL) OR
        (state = 'running' AND lease_owner IS NOT NULL
         AND lease_expires_at IS NOT NULL AND lease_fence >= 1
         AND result_digest IS NULL AND certification_id IS NULL
         AND completed_at IS NULL) OR
        (state IN ('completed', 'failed') AND result_digest IS NOT NULL
         AND certification_id IS NOT NULL AND completed_at IS NOT NULL
         AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_work_package_certification_jobs_state
    ON work_package_certification_jobs (state, created_at, id);

CREATE OR REPLACE FUNCTION trg_work_package_certification_job_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'queued' OR NEW.lease_owner IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL OR NEW.lease_fence <> 0
           OR NEW.result_digest IS NOT NULL OR NEW.certification_id IS NOT NULL
           OR NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION 'certification jobs must start queued';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'certification jobs are append-only';
    END IF;
    IF ROW(
        NEW.id, NEW.batch_id, NEW.package_id, NEW.plan_version, NEW.epoch,
        NEW.repository_id, NEW.candidate_sha, NEW.candidate_tree_digest,
        NEW.candidate_ref, NEW.candidate_fence, NEW.assembly_base_sha,
        NEW.landing_base_sha, NEW.target_ref, NEW.policy_id, NEW.policy_version,
        NEW.policy_checksum, NEW.image_ref, NEW.image_digest, NEW.bundle_digest,
        NEW.commands_digest, NEW.job_digest, NEW.definition, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.batch_id, OLD.package_id, OLD.plan_version, OLD.epoch,
        OLD.repository_id, OLD.candidate_sha, OLD.candidate_tree_digest,
        OLD.candidate_ref, OLD.candidate_fence, OLD.assembly_base_sha,
        OLD.landing_base_sha, OLD.target_ref, OLD.policy_id, OLD.policy_version,
        OLD.policy_checksum, OLD.image_ref, OLD.image_digest, OLD.bundle_digest,
        OLD.commands_digest, OLD.job_digest, OLD.definition, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'certification job identity is immutable';
    END IF;
    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'queued' AND NEW.state = 'running') OR
        (OLD.state = 'running' AND NEW.state IN ('completed', 'failed'))
    ) THEN
        RAISE EXCEPTION 'invalid certification job state transition';
    END IF;
    IF NEW.lease_fence NOT IN (OLD.lease_fence, OLD.lease_fence + 1) OR (
        NEW.lease_owner IS NOT NULL
        AND NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
        AND NEW.lease_fence <= OLD.lease_fence
    ) THEN
        RAISE EXCEPTION 'certification job owner requires a new fence';
    END IF;
    IF NEW.certification_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM work_package_certifications AS certification
        WHERE certification.id = NEW.certification_id
          AND certification.batch_id = NEW.batch_id
          AND certification.package_id = NEW.package_id
          AND certification.plan_version = NEW.plan_version
          AND certification.epoch = NEW.epoch
          AND certification.candidate_sha = NEW.candidate_sha
          AND certification.assembly_base_sha = NEW.assembly_base_sha
          AND certification.landing_base_sha = NEW.landing_base_sha
          AND certification.target_ref = NEW.target_ref
          AND certification.verification_digest = NEW.result_digest
    ) THEN
        RAISE EXCEPTION 'certification job result identity is invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_certification_job_lifecycle
    ON work_package_certification_jobs;
CREATE TRIGGER trg_work_package_certification_job_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_certification_jobs
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_certification_job_lifecycle();

-- Controller-owned graph nodes terminate from exact controller provenance,
-- never from a synthetic worker candidate.  One immutable receipt binds the
-- task/link projection to the exact integration batch or certification job.
CREATE TABLE IF NOT EXISTS work_package_controller_station_receipts (
    id TEXT PRIMARY KEY,
    station_kind TEXT NOT NULL CHECK (
        station_kind IN ('integration', 'certification')
    ),
    task_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    node_key TEXT NOT NULL CHECK (node_key <> ''),
    batch_id TEXT NOT NULL,
    certification_job_id TEXT UNIQUE
        REFERENCES work_package_certification_jobs(id) ON DELETE RESTRICT,
    certification_id TEXT UNIQUE
        REFERENCES work_package_certifications(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('integrated', 'certified', 'rejected')),
    result_digest TEXT CHECK (
        result_digest IS NULL OR result_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    provenance_digest TEXT NOT NULL UNIQUE CHECK (
        provenance_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    actor TEXT NOT NULL CHECK (actor <> ''),
    detail TEXT NOT NULL CHECK (jsonb_typeof(detail::jsonb) = 'object'),
    created_at TEXT NOT NULL,
    UNIQUE (package_id, plan_version, epoch, node_key),
    FOREIGN KEY (package_id, plan_version, epoch, node_key, task_id)
        REFERENCES work_package_task_links (
            package_id, plan_version, epoch, node_key, task_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (batch_id, package_id, plan_version, epoch)
        REFERENCES work_package_integration_batches (
            id, package_id, plan_version, epoch
        ) ON DELETE RESTRICT,
    CHECK (
        (station_kind = 'integration' AND outcome = 'integrated'
         AND certification_job_id IS NULL
         AND certification_id IS NULL AND result_digest IS NULL) OR
        (station_kind = 'certification' AND outcome IN ('certified', 'rejected')
         AND certification_job_id IS NOT NULL
         AND certification_id IS NOT NULL AND result_digest IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_work_package_controller_station_batch
    ON work_package_controller_station_receipts (
        batch_id, station_kind, outcome, created_at
    );

CREATE OR REPLACE FUNCTION trg_work_package_controller_station_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'controller station receipts are immutable';
    END IF;
    RAISE EXCEPTION 'controller station receipts are append-only';
    END IF;
    IF NEW.station_kind = 'integration' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM work_package_integration_batches AS batch
            JOIN work_package_task_links AS link
              ON link.task_id = NEW.task_id
             AND link.package_id = NEW.package_id
             AND link.plan_version = NEW.plan_version
             AND link.epoch = NEW.epoch
             AND link.node_key = NEW.node_key
            JOIN tasks AS task ON task.id = link.task_id
            WHERE batch.id = NEW.batch_id
              AND batch.package_id = NEW.package_id
              AND batch.plan_version = NEW.plan_version
              AND batch.epoch = NEW.epoch
              AND batch.integration_task_id = NEW.task_id
              AND batch.state = 'verifying'
              AND batch.candidate_sha IS NOT NULL
              AND batch.candidate_tree_digest IS NOT NULL
              AND batch.candidate_ref IS NOT NULL
              AND batch.candidate_fence IS NOT NULL
              AND link.node_state IN ('planned', 'ready')
              AND task.state IN ('open', 'waiting')
              AND task.owner_agent_id IS NULL
              AND task.lease_id IS NULL
              AND task.metadata::jsonb #>> '{no_dispatch}' = 'true'
              AND task.metadata::jsonb #>> '{work_package,node_type}' = 'integration'
        ) THEN
            RAISE EXCEPTION 'controller station receipt lacks exact durable provenance';
        END IF;
    ELSIF NEW.station_kind = 'certification' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM work_package_certification_jobs AS job
            JOIN work_package_certifications AS certification
              ON certification.id = NEW.certification_id
             AND certification.batch_id = job.batch_id
             AND certification.package_id = job.package_id
             AND certification.plan_version = job.plan_version
             AND certification.epoch = job.epoch
             AND certification.candidate_sha = job.candidate_sha
             AND certification.assembly_base_sha = job.assembly_base_sha
             AND certification.landing_base_sha = job.landing_base_sha
             AND certification.target_ref = job.target_ref
             AND certification.verification_digest = job.result_digest
            JOIN work_package_task_links AS link
              ON link.task_id = NEW.task_id
             AND link.package_id = NEW.package_id
             AND link.plan_version = NEW.plan_version
             AND link.epoch = NEW.epoch
             AND link.node_key = NEW.node_key
            JOIN tasks AS task ON task.id = link.task_id
            WHERE job.id = NEW.certification_job_id
              AND job.batch_id = NEW.batch_id
              AND job.package_id = NEW.package_id
              AND job.plan_version = NEW.plan_version
              AND job.epoch = NEW.epoch
              AND job.certification_id = NEW.certification_id
              AND job.result_digest = NEW.result_digest
              AND job.definition::jsonb #>> '{certification_task_id}' = NEW.task_id
              AND job.definition::jsonb #>> '{certification_node_key}' = NEW.node_key
              AND (
                  (NEW.outcome = 'certified' AND job.state = 'completed'
                   AND certification.status = 'passed') OR
                  (NEW.outcome = 'rejected' AND job.state = 'failed'
                   AND certification.status = 'failed')
              )
              AND link.node_state = 'ready'
              AND task.state = 'waiting'
              AND task.owner_agent_id IS NULL
              AND task.lease_id IS NULL
              AND certification.certification_task_id = NEW.task_id
              AND task.metadata::jsonb #>> '{no_dispatch}' = 'true'
              AND task.metadata::jsonb #>> '{work_package,node_type}' = 'certification'
        ) THEN
            RAISE EXCEPTION 'controller station receipt lacks exact durable provenance';
        END IF;
    ELSE
        RAISE EXCEPTION 'controller station receipt kind is invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_controller_station_lifecycle
    ON work_package_controller_station_receipts;
CREATE TRIGGER trg_work_package_controller_station_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_controller_station_receipts
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_controller_station_lifecycle();

-- Install receipt-aware controller-station transitions while retaining the
-- ordinary worker candidate state machine unchanged.
CREATE OR REPLACE FUNCTION trg_work_package_task_links_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.node_state <> 'planned' THEN
        RAISE EXCEPTION 'work package task links must start planned';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'work package task links are append-only';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.node_state <> OLD.node_state AND NOT (
        (OLD.node_state = 'planned' AND NEW.node_state IN (
            'ready', 'superseded', 'cancelled'
        )) OR
        (OLD.node_state = 'ready' AND NEW.node_state IN (
            'executing', 'superseded', 'cancelled'
        )) OR
        (OLD.node_state = 'executing' AND NEW.node_state IN (
            'ready', 'candidate_submitted', 'rejected', 'cancelled'
        )) OR
        (OLD.node_state = 'candidate_submitted' AND NEW.node_state IN (
            'candidate_accepted', 'rejected', 'superseded'
        )) OR
        (OLD.node_state = 'candidate_accepted' AND NEW.node_state IN (
            'integrated', 'rejected', 'superseded'
        )) OR
        (OLD.node_state = 'integrated' AND NEW.node_state IN (
            'certified', 'rejected', 'superseded'
        )) OR
        (OLD.node_state = 'rejected' AND NEW.node_state = 'executing') OR
        (
            OLD.node_state IN ('planned', 'ready')
            AND NEW.node_state = 'integrated'
            AND EXISTS (
                SELECT 1
                FROM work_package_controller_station_receipts AS receipt
                WHERE receipt.task_id = NEW.task_id
                  AND receipt.package_id = NEW.package_id
                  AND receipt.plan_version = NEW.plan_version
                  AND receipt.epoch = NEW.epoch
                  AND receipt.node_key = NEW.node_key
                  AND receipt.station_kind = 'integration'
                  AND receipt.outcome = 'integrated'
            )
        ) OR (
            OLD.node_state = 'ready'
            AND NEW.node_state IN ('certified', 'rejected')
            AND EXISTS (
                SELECT 1
                FROM work_package_controller_station_receipts AS receipt
                WHERE receipt.task_id = NEW.task_id
                  AND receipt.package_id = NEW.package_id
                  AND receipt.plan_version = NEW.plan_version
                  AND receipt.epoch = NEW.epoch
                  AND receipt.node_key = NEW.node_key
                  AND receipt.station_kind = 'certification'
                  AND receipt.outcome = NEW.node_state
            )
        )
    ) THEN
        RAISE EXCEPTION 'invalid work package node state transition';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_work_package_task_link_candidate_state()
RETURNS trigger AS $$
BEGIN
    IF NEW.node_state IN (
        'candidate_submitted', 'candidate_accepted', 'integrated',
        'certified', 'rejected'
    ) AND NOT EXISTS (
        SELECT 1 FROM work_package_node_candidates AS candidate
        WHERE candidate.task_id = NEW.task_id
          AND candidate.package_id = NEW.package_id
          AND candidate.plan_version = NEW.plan_version
          AND candidate.epoch = NEW.epoch
          AND candidate.node_key = NEW.node_key
          AND (
              (NEW.node_state = 'candidate_submitted'
               AND candidate.status = 'submitted') OR
              (NEW.node_state IN ('candidate_accepted', 'integrated', 'certified')
               AND candidate.status = 'accepted') OR
              (NEW.node_state = 'rejected' AND candidate.status = 'rejected')
          )
    ) AND NOT EXISTS (
        SELECT 1
        FROM work_package_controller_station_receipts AS receipt
        WHERE receipt.task_id = NEW.task_id
          AND receipt.package_id = NEW.package_id
          AND receipt.plan_version = NEW.plan_version
          AND receipt.epoch = NEW.epoch
          AND receipt.node_key = NEW.node_key
          AND receipt.outcome = NEW.node_state
    ) THEN
        RAISE EXCEPTION 'node terminal state lacks exact provenance';
    END IF;
    IF OLD.node_state = 'rejected' AND NEW.node_state = 'executing'
       AND NOT EXISTS (
        SELECT 1
        FROM work_package_assignment_audit AS assignment
        JOIN tasks AS task ON task.id = NEW.task_id
        WHERE assignment.package_id = NEW.package_id
          AND assignment.plan_version = NEW.plan_version
          AND assignment.epoch = NEW.epoch
          AND assignment.node_key = NEW.node_key
          AND assignment.task_id = NEW.task_id
          AND assignment.attempt_number <= task.max_attempts
          AND assignment.attempt_number > COALESCE((
              SELECT MAX(candidate.attempt_number)
              FROM work_package_node_candidates AS candidate
              WHERE candidate.task_id = NEW.task_id
                AND candidate.status = 'rejected'
          ), 0)
    ) THEN
        RAISE EXCEPTION 'rework requires a newer bounded assignment';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS work_package_landing_streams (
    repository_id TEXT NOT NULL
        REFERENCES project_repositories(id) ON DELETE RESTRICT,
    target_ref TEXT NOT NULL CHECK (target_ref LIKE 'refs/heads/%'),
    lease_owner TEXT,
    lease_expires_at TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repository_id, target_ref),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL) OR
        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
         AND lease_fence >= 1)
    )
);
CREATE INDEX IF NOT EXISTS idx_work_package_landing_stream_lease
    ON work_package_landing_streams (lease_expires_at, repository_id);

CREATE OR REPLACE FUNCTION trg_work_package_landing_stream_invariants()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'landing streams are append-only';
    END IF;
    IF ROW(NEW.repository_id, NEW.target_ref, NEW.created_at)
       IS DISTINCT FROM ROW(OLD.repository_id, OLD.target_ref, OLD.created_at) THEN
        RAISE EXCEPTION 'landing stream identity is immutable';
    END IF;
    IF NEW.lease_fence < OLD.lease_fence OR (
        NEW.lease_owner IS NOT NULL
        AND NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
        AND NEW.lease_fence <= OLD.lease_fence
    ) THEN
        RAISE EXCEPTION 'landing stream owner requires a monotonic fence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_landing_stream_invariants
    ON work_package_landing_streams;
CREATE TRIGGER trg_work_package_landing_stream_invariants
    BEFORE UPDATE OR DELETE ON work_package_landing_streams
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_landing_stream_invariants();

CREATE TABLE IF NOT EXISTS work_package_landing_intents (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    candidate_sha TEXT NOT NULL CHECK (candidate_sha ~ '^[0-9a-f]{40}$'),
    candidate_ref TEXT NOT NULL CHECK (candidate_ref LIKE 'refs/mac/%'),
    assembly_base_sha TEXT NOT NULL CHECK (assembly_base_sha ~ '^[0-9a-f]{40}$'),
    landing_base_sha TEXT NOT NULL CHECK (landing_base_sha ~ '^[0-9a-f]{40}$'),
    certification_id TEXT NOT NULL UNIQUE,
    stream_fence INTEGER NOT NULL CHECK (stream_fence >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        id, repository_id, target_ref, candidate_sha, landing_base_sha
    ),
    UNIQUE (
        id, batch_id, repository_id, target_ref, candidate_sha
    ),
    FOREIGN KEY (repository_id, target_ref)
        REFERENCES work_package_landing_streams(repository_id, target_ref)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        batch_id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) REFERENCES work_package_integration_batches (
        id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        certification_id, batch_id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ) REFERENCES work_package_certifications (
        id, batch_id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_landing_intents_target
    ON work_package_landing_intents (repository_id, target_ref, created_at, id);

CREATE OR REPLACE FUNCTION trg_work_package_landing_intent_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'landing intents are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'landing intents are append-only';
    END IF;
    PERFORM 1 FROM work_package_integration_batches AS batch
    JOIN work_package_certifications AS certification
      ON certification.id = NEW.certification_id
     AND certification.batch_id = batch.id
    JOIN work_package_landing_streams AS stream
      ON stream.repository_id = NEW.repository_id
     AND stream.target_ref = NEW.target_ref
    WHERE batch.id = NEW.batch_id
      AND batch.package_id = NEW.package_id
      AND batch.plan_version = NEW.plan_version
      AND batch.epoch = NEW.epoch
      AND batch.repository_id = NEW.repository_id
      AND batch.target_ref = NEW.target_ref
      AND batch.candidate_sha = NEW.candidate_sha
      AND batch.candidate_ref = NEW.candidate_ref
      AND batch.assembly_base_sha = NEW.assembly_base_sha
      AND batch.landing_base_sha = NEW.landing_base_sha
      AND batch.state = 'certified'
      AND certification.status = 'passed'
      AND stream.lease_owner = NEW.created_by
      AND stream.lease_fence = NEW.stream_fence
      AND stream.lease_expires_at > NEW.created_at
    FOR UPDATE OF batch, certification, stream;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'landing intent requires an exact certified candidate and current stream fence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_landing_intent_lifecycle
    ON work_package_landing_intents;
CREATE TRIGGER trg_work_package_landing_intent_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_landing_intents
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_landing_intent_lifecycle();

CREATE TABLE IF NOT EXISTS work_package_landing_attempts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    candidate_sha TEXT NOT NULL CHECK (candidate_sha ~ '^[0-9a-f]{40}$'),
    expected_remote_sha TEXT NOT NULL
        CHECK (expected_remote_sha ~ '^[0-9a-f]{40}$'),
    stream_fence INTEGER NOT NULL CHECK (stream_fence >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (intent_id, attempt_number),
    UNIQUE (
        id, intent_id, repository_id, target_ref, candidate_sha, stream_fence
    ),
    FOREIGN KEY (repository_id, target_ref)
        REFERENCES work_package_landing_streams(repository_id, target_ref)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        intent_id, repository_id, target_ref, candidate_sha, expected_remote_sha
    ) REFERENCES work_package_landing_intents (
        id, repository_id, target_ref, candidate_sha, landing_base_sha
    ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_landing_attempts_intent
    ON work_package_landing_attempts (intent_id, attempt_number, created_at);

CREATE OR REPLACE FUNCTION trg_work_package_landing_attempt_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'landing attempts are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'landing attempts are append-only';
    END IF;
    PERFORM 1 FROM work_package_landing_streams AS stream
    WHERE stream.repository_id = NEW.repository_id
      AND stream.target_ref = NEW.target_ref
      AND stream.lease_owner = NEW.created_by
      AND stream.lease_fence = NEW.stream_fence
      AND stream.lease_expires_at > NEW.created_at
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'landing attempt requires the current stream fence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_landing_attempt_lifecycle
    ON work_package_landing_attempts;
CREATE TRIGGER trg_work_package_landing_attempt_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_landing_attempts
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_landing_attempt_lifecycle();

CREATE TABLE IF NOT EXISTS work_package_landing_receipts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL UNIQUE,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    candidate_sha TEXT NOT NULL CHECK (candidate_sha ~ '^[0-9a-f]{40}$'),
    observed_sha TEXT NOT NULL CHECK (observed_sha ~ '^[0-9a-f]{40}$'),
    recovered INTEGER NOT NULL CHECK (recovered IN (0, 1)),
    recovery TEXT NOT NULL DEFAULT '',
    attempt_stream_fence INTEGER NOT NULL CHECK (attempt_stream_fence >= 1),
    recording_stream_fence INTEGER NOT NULL CHECK (recording_stream_fence >= 1),
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    receipt_digest TEXT NOT NULL UNIQUE CHECK (
        receipt_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    CHECK (
        (recovered = 0 AND recovery = '' AND observed_sha = candidate_sha)
        OR (recovered = 1 AND recovery <> '')
    ),
    FOREIGN KEY (repository_id, target_ref)
        REFERENCES work_package_landing_streams(repository_id, target_ref)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        intent_id, batch_id, repository_id, target_ref, candidate_sha
    ) REFERENCES work_package_landing_intents (
        id, batch_id, repository_id, target_ref, candidate_sha
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        attempt_id, intent_id, repository_id, target_ref,
        candidate_sha, attempt_stream_fence
    ) REFERENCES work_package_landing_attempts (
        id, intent_id, repository_id, target_ref, candidate_sha, stream_fence
    ) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_landing_receipts_target
    ON work_package_landing_receipts (repository_id, target_ref, recorded_at, id);

CREATE OR REPLACE FUNCTION trg_work_package_landing_receipt_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'landing receipts are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'landing receipts are append-only';
    END IF;
    PERFORM 1 FROM work_package_integration_batches AS batch
    JOIN work_package_landing_streams AS stream
      ON stream.repository_id = NEW.repository_id
     AND stream.target_ref = NEW.target_ref
    WHERE batch.id = NEW.batch_id
      AND batch.repository_id = NEW.repository_id
      AND batch.target_ref = NEW.target_ref
      AND batch.candidate_sha = NEW.candidate_sha
      AND batch.state = 'certified'
      AND stream.lease_owner = NEW.recorded_by
      AND stream.lease_fence = NEW.recording_stream_fence
      AND stream.lease_expires_at > NEW.recorded_at
    FOR UPDATE OF batch, stream;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'landing receipt requires a certified batch and current stream fence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_landing_receipt_lifecycle
    ON work_package_landing_receipts;
CREATE TRIGGER trg_work_package_landing_receipt_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_landing_receipts
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_landing_receipt_lifecycle();

-- Atomic product-completion receipt.  The landing receipt proves the Git
-- side effect; this separate append-only record proves the controller also
-- released bounded product WIP and closed the exact current graph.
CREATE TABLE IF NOT EXISTS work_package_publication_finalizations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    landing_receipt_id TEXT NOT NULL UNIQUE
        REFERENCES work_package_landing_receipts(id) ON DELETE RESTRICT,
    package_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    repository_id TEXT NOT NULL
        REFERENCES project_repositories(id) ON DELETE RESTRICT,
    integration_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    certification_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    certification_id TEXT NOT NULL
        REFERENCES work_package_certifications(id) ON DELETE RESTRICT,
    candidate_sha TEXT NOT NULL CHECK (candidate_sha ~ '^[0-9a-f]{40}$'),
    candidate_ref TEXT NOT NULL CHECK (candidate_ref LIKE 'refs/mac/%'),
    assembly_base_sha TEXT NOT NULL,
    landing_base_sha TEXT NOT NULL,
    target_ref TEXT NOT NULL CHECK (target_ref LIKE 'refs/heads/%'),
    observed_sha TEXT NOT NULL CHECK (observed_sha ~ '^[0-9a-f]{40}$'),
    landing_receipt_digest TEXT NOT NULL CHECK (
        landing_receipt_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    released_wip_ids TEXT NOT NULL CHECK (
        jsonb_typeof(released_wip_ids::jsonb) = 'array'
    ),
    controller_station_receipt_ids TEXT NOT NULL CHECK (
        jsonb_typeof(controller_station_receipt_ids::jsonb) = 'array'
    ),
    finalization_digest TEXT NOT NULL UNIQUE CHECK (
        finalization_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    finalized_by TEXT NOT NULL,
    finalized_at TEXT NOT NULL,
    FOREIGN KEY (package_id, epoch, plan_version)
        REFERENCES work_package_epochs(package_id, epoch, plan_version)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        batch_id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) REFERENCES work_package_integration_batches (
        id, package_id, plan_version, epoch, candidate_sha,
        assembly_base_sha, landing_base_sha, target_ref
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        certification_id, batch_id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ) REFERENCES work_package_certifications (
        id, batch_id, package_id, plan_version, epoch,
        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
    ) ON DELETE RESTRICT,
    FOREIGN KEY (batch_id, integration_task_id)
        REFERENCES work_package_integration_batches(id, integration_task_id)
        ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_publication_finalizations_package
    ON work_package_publication_finalizations (
        package_id, epoch, finalized_at, id
    );

CREATE OR REPLACE FUNCTION trg_work_package_publication_finalization_lifecycle()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'publication finalizations are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'publication finalizations are append-only';
    END IF;

    PERFORM 1
    FROM work_package_integration_batches AS batch
    JOIN work_package_landing_receipts AS receipt
      ON receipt.id = NEW.landing_receipt_id
     AND receipt.batch_id = batch.id
    JOIN work_package_landing_intents AS intent
      ON intent.id = receipt.intent_id
     AND intent.batch_id = batch.id
    JOIN work_package_certifications AS certification
      ON certification.id = NEW.certification_id
     AND certification.id = intent.certification_id
     AND certification.batch_id = batch.id
    JOIN work_packages AS package ON package.id = batch.package_id
    JOIN work_package_epochs AS epoch
      ON epoch.package_id = batch.package_id
     AND epoch.plan_version = batch.plan_version
     AND epoch.epoch = batch.epoch
    WHERE batch.id = NEW.batch_id
      AND batch.package_id = NEW.package_id
      AND batch.plan_version = NEW.plan_version
      AND batch.epoch = NEW.epoch
      AND batch.repository_id = NEW.repository_id
      AND batch.integration_task_id = NEW.integration_task_id
      AND batch.candidate_sha = NEW.candidate_sha
      AND batch.candidate_ref = NEW.candidate_ref
      AND batch.assembly_base_sha = NEW.assembly_base_sha
      AND batch.landing_base_sha = NEW.landing_base_sha
      AND batch.target_ref = NEW.target_ref
      AND batch.state = 'published'
      AND receipt.repository_id = NEW.repository_id
      AND receipt.target_ref = NEW.target_ref
      AND receipt.candidate_sha = NEW.candidate_sha
      AND receipt.observed_sha = NEW.observed_sha
      AND receipt.receipt_digest = NEW.landing_receipt_digest
      AND certification.package_id = NEW.package_id
      AND certification.plan_version = NEW.plan_version
      AND certification.epoch = NEW.epoch
      AND certification.candidate_sha = NEW.candidate_sha
      AND certification.assembly_base_sha = NEW.assembly_base_sha
      AND certification.landing_base_sha = NEW.landing_base_sha
      AND certification.target_ref = NEW.target_ref
      AND certification.status IN ('passed', 'published')
      AND package.current_plan_version = NEW.plan_version
      AND package.current_epoch = NEW.epoch
      AND package.state = 'completed'
      AND epoch.status = 'completed'
    FOR UPDATE OF batch, package, epoch;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'publication finalization identity is not exact';
    END IF;

    IF jsonb_array_length(NEW.released_wip_ids::jsonb) = 0 OR
       jsonb_array_length(NEW.released_wip_ids::jsonb) <> (
           SELECT COUNT(DISTINCT item.value)
           FROM jsonb_array_elements_text(
               NEW.released_wip_ids::jsonb
           ) AS item(value)
       ) OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(NEW.released_wip_ids::jsonb) AS item(value)
        WHERE NOT EXISTS (
            SELECT 1 FROM work_package_wip_tokens AS token
            WHERE token.id = item.value
              AND token.package_id = NEW.package_id
              AND token.plan_version = NEW.plan_version
              AND token.epoch = NEW.epoch
              AND token.stage = 'integration'
              AND token.state = 'released'
              AND token.reservation_key = NEW.batch_id
              AND token.predecessor_token_id IS NOT NULL
              AND token.release_reason =
                  'publication_finalized:' || NEW.landing_receipt_id
        )
    ) OR EXISTS (
        SELECT 1 FROM work_package_wip_tokens AS token
        WHERE token.package_id = NEW.package_id
          AND token.plan_version = NEW.plan_version
          AND token.epoch = NEW.epoch
          AND token.stage = 'integration'
          AND token.reservation_key = NEW.batch_id
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(NEW.released_wip_ids::jsonb) AS item(value)
              WHERE item.value = token.id
          )
    ) OR EXISTS (
        SELECT 1 FROM work_package_wip_tokens AS token
        WHERE token.package_id = NEW.package_id
          AND token.plan_version = NEW.plan_version
          AND token.epoch = NEW.epoch
          AND token.state = 'held'
    ) THEN
        RAISE EXCEPTION 'publication finalization WIP is incomplete';
    END IF;

    IF jsonb_array_length(NEW.controller_station_receipt_ids::jsonb) = 0 OR
       jsonb_array_length(NEW.controller_station_receipt_ids::jsonb) <> (
           SELECT COUNT(DISTINCT item.value)
           FROM jsonb_array_elements_text(
               NEW.controller_station_receipt_ids::jsonb
           ) AS item(value)
       ) OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(
            NEW.controller_station_receipt_ids::jsonb
        ) AS item(value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM work_package_controller_station_receipts AS receipt
            WHERE receipt.id = item.value
              AND receipt.batch_id = NEW.batch_id
              AND receipt.package_id = NEW.package_id
              AND receipt.plan_version = NEW.plan_version
              AND receipt.epoch = NEW.epoch
              AND (
                  (receipt.station_kind = 'integration'
                   AND receipt.outcome = 'integrated'
                   AND receipt.task_id = NEW.integration_task_id) OR
                  (receipt.station_kind = 'certification'
                   AND receipt.outcome = 'certified'
                   AND receipt.task_id = NEW.certification_task_id
                   AND receipt.certification_id = NEW.certification_id)
              )
        )
    ) OR NOT EXISTS (
        SELECT 1
        FROM work_package_controller_station_receipts AS receipt
        JOIN tasks AS task ON task.id = receipt.task_id
        JOIN work_package_task_links AS link ON link.task_id = receipt.task_id
        WHERE receipt.batch_id = NEW.batch_id
          AND receipt.package_id = NEW.package_id
          AND receipt.plan_version = NEW.plan_version
          AND receipt.epoch = NEW.epoch
          AND receipt.station_kind = 'integration'
          AND receipt.outcome = 'integrated'
          AND receipt.task_id = NEW.integration_task_id
          AND task.state = 'completed'
          AND link.node_state = 'integrated'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  NEW.controller_station_receipt_ids::jsonb
              ) AS item(value)
              WHERE item.value = receipt.id
          )
    ) OR (
        NEW.certification_task_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
            FROM work_package_controller_station_receipts AS receipt
            JOIN tasks AS task ON task.id = receipt.task_id
            JOIN work_package_task_links AS link ON link.task_id = receipt.task_id
            WHERE receipt.batch_id = NEW.batch_id
              AND receipt.package_id = NEW.package_id
              AND receipt.plan_version = NEW.plan_version
              AND receipt.epoch = NEW.epoch
              AND receipt.station_kind = 'certification'
              AND receipt.outcome = 'certified'
              AND receipt.task_id = NEW.certification_task_id
              AND receipt.certification_id = NEW.certification_id
              AND task.state = 'completed'
              AND link.node_state = 'certified'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(
                      NEW.controller_station_receipt_ids::jsonb
                  ) AS item(value)
                  WHERE item.value = receipt.id
              )
        )
    ) OR (
        NEW.certification_task_id IS NULL AND EXISTS (
            SELECT 1
            FROM work_package_controller_station_receipts AS receipt
            WHERE receipt.batch_id = NEW.batch_id
              AND receipt.station_kind = 'certification'
              AND receipt.outcome = 'certified'
        )
    ) OR EXISTS (
        SELECT 1
        FROM work_package_controller_station_receipts AS receipt
        WHERE receipt.batch_id = NEW.batch_id
          AND receipt.outcome IN ('integrated', 'certified')
          AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  NEW.controller_station_receipt_ids::jsonb
              ) AS item(value)
              WHERE item.value = receipt.id
          )
    ) THEN
        RAISE EXCEPTION 'publication finalization station provenance is incomplete';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_publication_finalization_lifecycle
    ON work_package_publication_finalizations;
CREATE TRIGGER trg_work_package_publication_finalization_lifecycle
    BEFORE INSERT OR UPDATE OR DELETE ON work_package_publication_finalizations
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_publication_finalization_lifecycle();

CREATE TABLE IF NOT EXISTS work_package_ref_retirement_intents (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL
        REFERENCES project_repositories(id) ON DELETE RESTRICT,
    ref_kind TEXT NOT NULL CHECK (ref_kind IN ('attempt', 'candidate')),
    ref TEXT NOT NULL,
    expected_sha TEXT NOT NULL CHECK (expected_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    batch_id TEXT REFERENCES work_package_integration_batches(id) ON DELETE RESTRICT,
    terminal_state TEXT NOT NULL CHECK (terminal_state <> ''),
    terminal_at TIMESTAMPTZ NOT NULL,
    eligible_after TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL CHECK (created_by <> ''),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (repository_id, ref, expected_sha),
    CHECK (
        (ref_kind = 'attempt' AND ref LIKE 'refs/mac/attempts/%'
         AND task_id IS NOT NULL AND batch_id IS NULL) OR
        (ref_kind = 'candidate'
         AND (ref LIKE 'refs/mac/integration/%'
              OR ref LIKE 'refs/mac/candidates/%')
         AND task_id IS NULL AND batch_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_work_package_ref_retirement_due
    ON work_package_ref_retirement_intents (repository_id, eligible_after, ref);

CREATE TABLE IF NOT EXISTS work_package_ref_retirement_attempts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES work_package_ref_retirement_intents(id)
        ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('failed', 'deleted', 'missing')),
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    CHECK ((outcome = 'failed' AND error <> '') OR
           (outcome <> 'failed' AND error = ''))
);
CREATE INDEX IF NOT EXISTS idx_work_package_ref_retirement_attempts_intent
    ON work_package_ref_retirement_attempts (intent_id, created_at);

CREATE TABLE IF NOT EXISTS work_package_ref_retirement_receipts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE
        REFERENCES work_package_ref_retirement_intents(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('deleted', 'missing')),
    completed_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION trg_work_package_ref_retirement_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'work-package ref retirement records are immutable';
    END IF;
    RAISE EXCEPTION 'work-package ref retirement records are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_ref_retirement_intent_append_only
    ON work_package_ref_retirement_intents;
CREATE TRIGGER trg_work_package_ref_retirement_intent_append_only
    BEFORE UPDATE OR DELETE ON work_package_ref_retirement_intents
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_ref_retirement_append_only();
DROP TRIGGER IF EXISTS trg_work_package_ref_retirement_attempt_append_only
    ON work_package_ref_retirement_attempts;
CREATE TRIGGER trg_work_package_ref_retirement_attempt_append_only
    BEFORE UPDATE OR DELETE ON work_package_ref_retirement_attempts
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_ref_retirement_append_only();
DROP TRIGGER IF EXISTS trg_work_package_ref_retirement_receipt_append_only
    ON work_package_ref_retirement_receipts;
CREATE TRIGGER trg_work_package_ref_retirement_receipt_append_only
    BEFORE UPDATE OR DELETE ON work_package_ref_retirement_receipts
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_ref_retirement_append_only();

CREATE TABLE IF NOT EXISTS work_package_history (
    id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    plan_version INTEGER,
    epoch INTEGER,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (package_id, seq),
    CHECK (
        (plan_version IS NULL AND epoch IS NULL) OR
        (plan_version IS NOT NULL AND epoch IS NOT NULL)
    ),
    FOREIGN KEY (package_id, epoch, plan_version)
        REFERENCES work_package_epochs(package_id, epoch, plan_version)
        ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_work_package_history_package
    ON work_package_history (package_id, seq);

CREATE OR REPLACE FUNCTION trg_work_package_history_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'work package history is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_history_immutable ON work_package_history;
CREATE TRIGGER trg_work_package_history_immutable
    BEFORE UPDATE OR DELETE ON work_package_history
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_history_immutable();

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

CREATE TABLE IF NOT EXISTS execution_cohort_configurations (
    rollout_revision INTEGER PRIMARY KEY CHECK (rollout_revision >= 1),
    algorithm TEXT NOT NULL,
    treatment_percentage INTEGER NOT NULL
        CHECK (treatment_percentage BETWEEN 0 AND 100),
    assignment_key_fingerprint TEXT NOT NULL CHECK (
        assignment_key_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    created_at TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION trg_execution_cohort_configuration_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'execution cohort configurations are immutable';
    END IF;
    RAISE EXCEPTION 'execution cohort configurations are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_execution_cohort_configuration_append_only
    ON execution_cohort_configurations;
CREATE TRIGGER trg_execution_cohort_configuration_append_only
    BEFORE UPDATE OR DELETE ON execution_cohort_configurations
    FOR EACH ROW EXECUTE FUNCTION trg_execution_cohort_configuration_append_only();

-- Prospective treatment assignment for managed-versus-legacy evaluation.
-- Historical package route is synchronized only when an immutable publication
-- finalization proves the complete controller pipeline; otherwise its managed
-- execution mode is explicitly unknown.
CREATE TABLE IF NOT EXISTS execution_cohort_assignments (
    id TEXT PRIMARY KEY,
    -- Soft identities are intentional: lifecycle cleanup must not erase or
    -- block immutable experiment assignment history.
    task_id TEXT UNIQUE,
    package_id TEXT UNIQUE,
    eligibility TEXT NOT NULL CHECK (
        eligibility IN ('eligible', 'ineligible', 'unknown')
    ),
    treatment_route TEXT NOT NULL,
    rollout_revision INTEGER NOT NULL CHECK (rollout_revision >= 0),
    cohort_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(detail::jsonb) = 'object'),
    assigned_by TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    CHECK (task_id IS NOT NULL OR package_id IS NOT NULL),
    CONSTRAINT execution_cohort_assignments_treatment_route_check CHECK (
        treatment_route IN (
            'legacy_async', 'managed_synchronized', 'unknown_managed_mode'
        )
    )
);

-- Upgrade the preliminary two-route CHECK in place.  Catalog inspection is
-- constant-size and only rewrites the constraint when the v3 route is absent.
DO $execution_cohort_route_contract$
DECLARE
    route_constraint RECORD;
BEGIN
    FOR route_constraint IN
        SELECT constraint_row.oid, constraint_row.conname,
               pg_get_constraintdef(constraint_row.oid) AS definition
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid =
                  'execution_cohort_assignments'::regclass
          AND constraint_row.contype = 'c'
          AND pg_get_constraintdef(constraint_row.oid) LIKE '%treatment_route%'
    LOOP
        IF route_constraint.definition NOT LIKE '%unknown_managed_mode%' THEN
            EXECUTE format(
                'ALTER TABLE execution_cohort_assignments DROP CONSTRAINT %I',
                route_constraint.conname
            );
        END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid =
                  'execution_cohort_assignments'::regclass
          AND constraint_row.contype = 'c'
          AND pg_get_constraintdef(constraint_row.oid)
              LIKE '%unknown_managed_mode%'
    ) THEN
        ALTER TABLE execution_cohort_assignments
            ADD CONSTRAINT execution_cohort_assignments_treatment_route_check
            CHECK (treatment_route IN (
                'legacy_async', 'managed_synchronized',
                'unknown_managed_mode'
            ));
    END IF;
END;
$execution_cohort_route_contract$;
CREATE INDEX IF NOT EXISTS idx_execution_cohort_route
    ON execution_cohort_assignments (
        treatment_route, eligibility, assigned_at, id
    );

CREATE OR REPLACE FUNCTION trg_execution_cohort_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'execution cohort assignments are immutable';
    END IF;
    RAISE EXCEPTION 'execution cohort assignments are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_execution_cohort_append_only
    ON execution_cohort_assignments;

CREATE TABLE IF NOT EXISTS work_package_station_attempts (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL
        REFERENCES execution_cohort_assignments(id) ON DELETE RESTRICT,
    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE RESTRICT,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    station TEXT NOT NULL CHECK (station IN (
        'controller', 'admission', 'integration', 'certification',
        'landing', 'finalization'
    )),
    operation TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    attempted INTEGER NOT NULL CHECK (attempted IN (0, 1)),
    pipeline_run_id TEXT NOT NULL DEFAULT '',
    outcome_index INTEGER NOT NULL DEFAULT 0 CHECK (outcome_index >= 0),
    batch_id TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    queued_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    queue_duration_ms INTEGER NOT NULL CHECK (queue_duration_ms >= 0),
    execution_duration_ms INTEGER NOT NULL CHECK (execution_duration_ms >= 0),
    terminal_status TEXT NOT NULL CHECK (terminal_status IN (
        'succeeded', 'failed', 'busy', 'held', 'stale',
        'rejected', 'skipped'
    )),
    reason_code TEXT NOT NULL DEFAULT '',
    failure_class TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(detail::jsonb) = 'object'),
    recorded_at TEXT NOT NULL,
    UNIQUE (package_id, station, attempt_number),
    UNIQUE (pipeline_run_id, outcome_index),
    FOREIGN KEY (package_id, epoch, plan_version)
        REFERENCES work_package_epochs(package_id, epoch, plan_version)
        ON DELETE RESTRICT
);
DO $work_package_station_contract$
DECLARE
    station_constraint RECORD;
BEGIN
    FOR station_constraint IN
        SELECT constraint_row.conname,
               pg_get_constraintdef(constraint_row.oid) AS definition
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid =
                  'work_package_station_attempts'::regclass
          AND constraint_row.contype = 'c'
          AND pg_get_constraintdef(constraint_row.oid) LIKE '%station%'
    LOOP
        IF station_constraint.definition NOT LIKE '%controller%' THEN
            EXECUTE format(
                'ALTER TABLE work_package_station_attempts DROP CONSTRAINT %I',
                station_constraint.conname
            );
        END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid =
                  'work_package_station_attempts'::regclass
          AND constraint_row.contype = 'c'
          AND pg_get_constraintdef(constraint_row.oid) LIKE '%station%'
          AND pg_get_constraintdef(constraint_row.oid) LIKE '%controller%'
    ) THEN
        ALTER TABLE work_package_station_attempts
            ADD CONSTRAINT work_package_station_attempts_station_check
            CHECK (station IN (
                'controller', 'admission', 'integration', 'certification',
                'landing', 'finalization'
            ));
    END IF;
END;
$work_package_station_contract$;
CREATE INDEX IF NOT EXISTS idx_work_package_station_attempts_package
    ON work_package_station_attempts (package_id, station, completed_at, id);
CREATE INDEX IF NOT EXISTS idx_work_package_station_attempts_status
    ON work_package_station_attempts (
        terminal_status, failure_class, completed_at, id
    );

CREATE OR REPLACE FUNCTION trg_work_package_station_attempt_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'work-package station attempts are immutable';
    END IF;
    RAISE EXCEPTION 'work-package station attempts are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_station_attempt_append_only
    ON work_package_station_attempts;
CREATE TRIGGER trg_work_package_station_attempt_append_only
    BEFORE UPDATE OR DELETE ON work_package_station_attempts
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_station_attempt_append_only();

CREATE TABLE IF NOT EXISTS work_package_controller_outcomes (
    id TEXT PRIMARY KEY,
    pipeline_run_id TEXT NOT NULL,
    outcome_index INTEGER NOT NULL CHECK (outcome_index >= -1),
    package_id TEXT NOT NULL DEFAULT '',
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    epoch INTEGER NOT NULL CHECK (epoch >= 0),
    operation TEXT NOT NULL,
    attempted INTEGER NOT NULL CHECK (attempted IN (0, 1)),
    batch_id TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    execution_duration_ms INTEGER NOT NULL CHECK (execution_duration_ms >= 0),
    status TEXT NOT NULL,
    terminal_status TEXT NOT NULL CHECK (terminal_status IN (
        'succeeded', 'failed', 'busy', 'held', 'stale',
        'rejected', 'skipped'
    )),
    reason_code TEXT NOT NULL DEFAULT '',
    failure_class TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(detail::jsonb) = 'object'),
    recorded_at TEXT NOT NULL,
    UNIQUE (pipeline_run_id, outcome_index)
);
CREATE INDEX IF NOT EXISTS idx_work_package_controller_outcomes_package
    ON work_package_controller_outcomes (package_id, completed_at, id);
CREATE INDEX IF NOT EXISTS idx_work_package_controller_outcomes_status
    ON work_package_controller_outcomes (
        terminal_status, failure_class, completed_at, id
    );

CREATE OR REPLACE FUNCTION trg_work_package_controller_outcome_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'work-package controller outcomes are immutable';
    END IF;
    RAISE EXCEPTION 'work-package controller outcomes are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_controller_outcome_append_only
    ON work_package_controller_outcomes;
CREATE TRIGGER trg_work_package_controller_outcome_append_only
    BEFORE UPDATE OR DELETE ON work_package_controller_outcomes
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_controller_outcome_append_only();

CREATE TABLE IF NOT EXISTS work_package_telemetry_health (
    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'pipeline'),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    last_failure_operation TEXT NOT NULL DEFAULT '',
    last_error_type TEXT NOT NULL DEFAULT '',
    last_error_fingerprint TEXT NOT NULL DEFAULT '',
    last_failed_at TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_package_finalization_outcomes (
    id TEXT PRIMARY KEY,
    finalization_id TEXT NOT NULL
        REFERENCES work_package_publication_finalizations(id) ON DELETE RESTRICT,
    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE RESTRICT,
    outcome_type TEXT NOT NULL CHECK (outcome_type IN ('revert', 'incident')),
    external_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(detail::jsonb) = 'object'),
    created_at TEXT NOT NULL,
    UNIQUE (finalization_id, outcome_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_work_package_finalization_outcomes_package
    ON work_package_finalization_outcomes (
        package_id, outcome_type, observed_at, id
    );

CREATE OR REPLACE FUNCTION trg_work_package_finalization_outcome_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'work-package finalization outcomes are immutable';
    END IF;
    RAISE EXCEPTION 'work-package finalization outcomes are append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_work_package_finalization_outcome_append_only
    ON work_package_finalization_outcomes;
CREATE TRIGGER trg_work_package_finalization_outcome_append_only
    BEFORE UPDATE OR DELETE ON work_package_finalization_outcomes
    FOR EACH ROW EXECUTE FUNCTION trg_work_package_finalization_outcome_append_only();

-- Schema execution is one transaction, and this conditional block additionally
-- makes the intended atomic boundary explicit: the marker is written only
-- after both historical cohorts complete.  Once present, no task/package scan
-- statement is entered on later startups.
DO $execution_cohort_historical_backfill_v2$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM telemetry_data_migrations
        WHERE version = 'execution_cohort_historical_backfill_v2'
    ) THEN
        -- Repair assignments produced by the preliminary backfill before the
        -- append-only trigger is restored below.  A finalization is the strict
        -- proof because its lifecycle trigger binds landing plus exact
        -- integration/certification controller receipts.
        UPDATE execution_cohort_assignments AS assignment
        SET eligibility = 'unknown',
            treatment_route = CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = assignment.package_id
            ) THEN 'managed_synchronized' ELSE 'unknown_managed_mode' END,
            cohort_key = CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = assignment.package_id
            ) THEN 'managed_receipted_pre_instrumentation'
              ELSE 'managed_mode_unknown_pre_instrumentation' END,
            reason = CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = assignment.package_id
            ) THEN 'historical_synchronized_pipeline_receipt'
              ELSE 'historical_package_mode_unproven' END,
            detail = jsonb_build_object(
                'schema', 'mac.execution_cohort.backfill.v2',
                'eligibility_source', 'unavailable',
                'route_source', CASE WHEN EXISTS (
                    SELECT 1
                    FROM work_package_publication_finalizations AS finalization
                    WHERE finalization.package_id = assignment.package_id
                ) THEN 'publication_finalization_receipt' ELSE 'unavailable' END,
                'route_receipt_id', COALESCE((
                    SELECT finalization.id
                    FROM work_package_publication_finalizations AS finalization
                    WHERE finalization.package_id = assignment.package_id
                    ORDER BY finalization.finalized_at, finalization.id
                    LIMIT 1
                ), '')
            )::text
        WHERE assignment.package_id IS NOT NULL
          AND (
              assignment.assigned_by = 'schema-migration'
              OR COALESCE(assignment.detail::jsonb ->> 'schema', '') NOT IN (
                  'mac.execution_cohort.prospective.v2',
                  'mac.execution_cohort.prospective.v3'
              )
          );

        INSERT INTO execution_cohort_assignments (
            id, task_id, package_id, eligibility, treatment_route,
            rollout_revision, cohort_key, reason, detail, assigned_by,
            assigned_at
        )
        SELECT
            'cohort_hist_managed_' || package.id,
            NULL,
            package.id,
            'unknown',
            CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = package.id
            ) THEN 'managed_synchronized' ELSE 'unknown_managed_mode' END,
            COALESCE((
                SELECT revision FROM managed_task_publication_rollout
                WHERE singleton_key = 'fleet'
            ), 0),
            CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = package.id
            ) THEN 'managed_receipted_pre_instrumentation'
              ELSE 'managed_mode_unknown_pre_instrumentation' END,
            CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = package.id
            ) THEN 'historical_synchronized_pipeline_receipt'
              ELSE 'historical_package_mode_unproven' END,
            jsonb_build_object(
                'schema', 'mac.execution_cohort.backfill.v2',
                'eligibility_source', 'unavailable',
                'route_source', CASE WHEN EXISTS (
                    SELECT 1
                    FROM work_package_publication_finalizations AS finalization
                    WHERE finalization.package_id = package.id
                ) THEN 'publication_finalization_receipt' ELSE 'unavailable' END,
                'route_receipt_id', COALESCE((
                    SELECT finalization.id
                    FROM work_package_publication_finalizations AS finalization
                    WHERE finalization.package_id = package.id
                    ORDER BY finalization.finalized_at, finalization.id
                    LIMIT 1
                ), '')
            )::text,
            'schema-migration',
            package.created_at
        FROM work_packages AS package
        ON CONFLICT DO NOTHING;

        INSERT INTO execution_cohort_assignments (
            id, task_id, package_id, eligibility, treatment_route,
            rollout_revision, cohort_key, reason, detail, assigned_by,
            assigned_at
        )
        SELECT
            'cohort_hist_legacy_' || task.id,
            task.id,
            NULL,
            'unknown',
            'legacy_async',
            0,
            CASE WHEN task.metadata::jsonb
                           #>> '{managed_fast_lane,activation}'
                       = 'legacy_compatibility'
                 THEN 'legacy_atomic_shape_pre_instrumentation'
                 ELSE 'legacy_pre_instrumentation_unknown' END,
            CASE WHEN task.metadata::jsonb
                           #>> '{managed_fast_lane,activation}'
                       = 'legacy_compatibility'
                 THEN 'historical_control_plane_route_projection'
                 ELSE 'historical_absence_of_package_linkage' END,
            jsonb_build_object(
                'schema', 'mac.execution_cohort.backfill.v2',
                'eligibility_source', 'unavailable',
                'shape_eligibility_source', CASE
                    WHEN task.metadata::jsonb
                             #>> '{managed_fast_lane,activation}'
                         = 'legacy_compatibility'
                    THEN 'control_plane_managed_fast_lane_projection'
                    ELSE 'unavailable'
                END,
                'route_source', 'absence_of_work_package_link'
            )::text,
            'schema-migration',
            task.created_at
        FROM tasks AS task
        WHERE NOT EXISTS (
            SELECT 1 FROM work_package_task_links AS link
            WHERE link.task_id = task.id
        )
          AND NOT EXISTS (
            SELECT 1 FROM work_packages AS package
            WHERE package.root_task_id = task.id
        )
        ON CONFLICT DO NOTHING;

        INSERT INTO telemetry_data_migrations (
            version, component, detail, applied_at
        ) VALUES (
            'execution_cohort_historical_backfill_v2',
            'execution_cohort_assignments',
            '{"schema":"mac.telemetry_data_migration.v1","historical_backfill":"mac.execution_cohort.backfill.v2"}',
            clock_timestamp()
        );
    END IF;
END;
$execution_cohort_historical_backfill_v2$;

-- A preliminary deployment could already have the v2 marker while retaining
-- package assignments created before the prospective-v2 identity contract.
-- The v2 guard correctly prevents another catalog backfill, so repair only
-- those package rows under a separately versioned, one-time receipt while the
-- append-only trigger is still suspended.
DO $execution_cohort_preliminary_package_repair_v3$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM telemetry_data_migrations
        WHERE version = 'execution_cohort_preliminary_package_repair_v3'
    ) THEN
        UPDATE execution_cohort_assignments AS assignment
        SET eligibility = 'unknown',
            treatment_route = CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = assignment.package_id
            ) THEN 'managed_synchronized' ELSE 'unknown_managed_mode' END,
            cohort_key = CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = assignment.package_id
            ) THEN 'managed_receipted_pre_instrumentation'
              ELSE 'managed_mode_unknown_pre_instrumentation' END,
            reason = CASE WHEN EXISTS (
                SELECT 1
                FROM work_package_publication_finalizations AS finalization
                WHERE finalization.package_id = assignment.package_id
            ) THEN 'historical_synchronized_pipeline_receipt'
              ELSE 'historical_package_mode_unproven' END,
            detail = jsonb_build_object(
                'schema', 'mac.execution_cohort.backfill.v2',
                'eligibility_source', 'unavailable',
                'route_source', CASE WHEN EXISTS (
                    SELECT 1
                    FROM work_package_publication_finalizations AS finalization
                    WHERE finalization.package_id = assignment.package_id
                ) THEN 'publication_finalization_receipt' ELSE 'unavailable' END,
                'route_receipt_id', COALESCE((
                    SELECT finalization.id
                    FROM work_package_publication_finalizations AS finalization
                    WHERE finalization.package_id = assignment.package_id
                    ORDER BY finalization.finalized_at, finalization.id
                    LIMIT 1
                ), '')
            )::text
        WHERE assignment.package_id IS NOT NULL
          AND COALESCE(assignment.detail::jsonb ->> 'schema', '') NOT IN (
              'mac.execution_cohort.prospective.v2',
              'mac.execution_cohort.prospective.v3'
          );

        INSERT INTO telemetry_data_migrations (
            version, component, detail, applied_at
        ) VALUES (
            'execution_cohort_preliminary_package_repair_v3',
            'execution_cohort_assignments',
            '{"schema":"mac.telemetry_data_migration.v1","repair":"mac.execution_cohort.preliminary-package.v3"}',
            clock_timestamp()
        );
    END IF;
END;
$execution_cohort_preliminary_package_repair_v3$;

CREATE TRIGGER trg_execution_cohort_append_only
    BEFORE UPDATE OR DELETE ON execution_cohort_assignments
    FOR EACH ROW EXECUTE FUNCTION trg_execution_cohort_append_only();

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
        'work_package' AS subject_type,
        package_id AS subject_id,
        event_type,
        actor,
        (
            COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb)
            || jsonb_build_object(
                'plan_version', plan_version,
                'epoch', epoch
            )
        )::text AS detail,
        created_at
    FROM work_package_history
    UNION ALL
    SELECT
        id,
        CASE WHEN package_id IS NULL THEN 'task' ELSE 'work_package' END
            AS subject_type,
        COALESCE(package_id, task_id) AS subject_id,
        'execution.cohort_assigned' AS event_type,
        assigned_by AS actor,
        (
            COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb)
            || jsonb_build_object(
                'task_id', task_id,
                'package_id', package_id,
                'eligibility', eligibility,
                'treatment_route', treatment_route,
                'rollout_revision', rollout_revision,
                'cohort_key', cohort_key,
                'reason', reason
            )
        )::text AS detail,
        assigned_at AS created_at
    FROM execution_cohort_assignments
    UNION ALL
    SELECT
        id,
        'work_package' AS subject_type,
        package_id AS subject_id,
        'work_package.station.' || station || '.' || terminal_status AS event_type,
        actor,
        (
            COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb)
            || jsonb_build_object(
                'assignment_id', assignment_id,
                'plan_version', plan_version,
                'epoch', epoch,
                'station', station,
                'operation', operation,
                'attempt_number', attempt_number,
                'attempted', attempted = 1,
                'pipeline_run_id', pipeline_run_id,
                'outcome_index', outcome_index,
                'batch_id', batch_id,
                'job_id', job_id,
                'queued_at', queued_at,
                'started_at', started_at,
                'completed_at', completed_at,
                'queue_duration_ms', queue_duration_ms,
                'execution_duration_ms', execution_duration_ms,
                'terminal_status', terminal_status,
                'reason_code', reason_code,
                'failure_class', failure_class
            )
        )::text AS detail,
        completed_at AS created_at
    FROM work_package_station_attempts
    UNION ALL
    SELECT
        id,
        CASE WHEN package_id = '' THEN 'service' ELSE 'work_package' END
            AS subject_type,
        CASE WHEN package_id = '' THEN 'work-package-pipeline' ELSE package_id END
            AS subject_id,
        'work_package.controller.' || operation || '.' || terminal_status
            AS event_type,
        'work-package-pipeline' AS actor,
        (
            COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb)
            || jsonb_build_object(
                'pipeline_run_id', pipeline_run_id,
                'outcome_index', outcome_index,
                'plan_version', plan_version,
                'epoch', epoch,
                'operation', operation,
                'attempted', attempted = 1,
                'batch_id', batch_id,
                'job_id', job_id,
                'started_at', started_at,
                'completed_at', completed_at,
                'execution_duration_ms', execution_duration_ms,
                'status', status,
                'terminal_status', terminal_status,
                'reason_code', reason_code,
                'failure_class', failure_class
            )
        )::text AS detail,
        completed_at AS created_at
    FROM work_package_controller_outcomes
    UNION ALL
    SELECT
        id,
        'work_package' AS subject_type,
        package_id AS subject_id,
        'work_package.finalization.' || outcome_type AS event_type,
        actor,
        (
            COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb)
            || jsonb_build_object(
                'finalization_id', finalization_id,
                'outcome_type', outcome_type,
                'external_id', external_id,
                'observed_at', observed_at
            )
        )::text AS detail,
        observed_at AS created_at
    FROM work_package_finalization_outcomes
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
CREATE INDEX IF NOT EXISTS idx_task_flow_spans_project
    ON task_flow_spans (project, stage, started_at);
CREATE INDEX IF NOT EXISTS idx_task_flow_spans_stage_time
    ON task_flow_spans (stage, started_at);

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
