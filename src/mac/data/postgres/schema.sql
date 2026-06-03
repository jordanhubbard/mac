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

CREATE TABLE IF NOT EXISTS hermes_instances (
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
CREATE INDEX IF NOT EXISTS idx_hermes_instances_tenant ON hermes_instances (tenant_id);

CREATE TABLE IF NOT EXISTS platform_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    hermes_instance_id TEXT NOT NULL REFERENCES hermes_instances(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    scopes TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance ON platform_bindings (hermes_instance_id);

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
    workflow_node_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state_priority ON tasks (state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (owner_agent_id);

-- mac-1hnt: enforce the task state machine at the DB layer. SQLite did
-- this with two CHECK triggers using RAISE(ABORT, ...); the same intent
-- in Postgres is a PL/pgSQL trigger function attached to INSERT and to
-- UPDATE OF state.
CREATE OR REPLACE FUNCTION trg_tasks_state_enum() RETURNS trigger AS $$
BEGIN
    IF NEW.state NOT IN (
        'open', 'blocked', 'claimed', 'running',
        'needs_review', 'reviewing', 'completed',
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
    delegated_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL
);
-- Additive migration for existing deployments: schema.sql is run on every
-- mac-api startup, and CREATE TABLE IF NOT EXISTS skips already-present
-- tables, so the column would never appear on a live DB without this
-- explicit ALTER. Postgres 9.6+ supports `ADD COLUMN IF NOT EXISTS`, so
-- this is idempotent and safe to re-run.
ALTER TABLE leases ADD COLUMN IF NOT EXISTS delegated_agent_id TEXT
    REFERENCES agents(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_leases_task_status ON leases (task_id, status);
CREATE INDEX IF NOT EXISTS idx_leases_agent_status ON leases (agent_id, status);
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
    capabilities TEXT NOT NULL,
    resources TEXT NOT NULL,
    status TEXT NOT NULL,
    health_status TEXT NOT NULL,
    current_task_id TEXT,
    running_digest TEXT,
    role_id TEXT,
    hermes_instance_id TEXT,
    attestation_key_ciphertext TEXT,
    attestation_key_rotated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_status_health ON agents (status, health_status);

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
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agentbus_streams_recipient_status
    ON agentbus_streams (recipient_agent_id, status, updated_at);
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

CREATE TABLE IF NOT EXISTS beads_repositories (
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
CREATE INDEX IF NOT EXISTS idx_beads_repositories_enabled
    ON beads_repositories (enabled, last_polled_at);

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
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_state ON workflow_runs (state, updated_at);
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
