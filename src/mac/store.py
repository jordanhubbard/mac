from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    ContextManager,
    Iterable,
    Iterator,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)


class StoreError(Exception):
    """Backend-neutral persistence error.

    SQLiteStore continues to surface ``sqlite3.Error`` subclasses directly
    so existing callers that catch ``sqlite3.IntegrityError`` keep working.
    Non-SQLite backends (e.g. PostgresStore) wrap their driver-native
    errors in ``StoreError``. Code that must handle either backend should
    catch ``(StoreError, sqlite3.Error)``.
    """


@runtime_checkable
class StoreConnection(Protocol):
    """Connection-like object yielded by ``Store.transaction()``."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...


@runtime_checkable
class Store(Protocol):
    """Backend-agnostic persistence interface used by the control plane.

    Implementations accept SQL written in SQLite dialect. Non-SQLite
    backends translate placeholders and dialect-specific functions
    internally so service-layer SQL stays SQLite-shaped across the ~50
    service modules.
    """

    path: str

    def close(self) -> None: ...
    def transaction(self) -> ContextManager[StoreConnection]: ...
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...
    def executemany(
        self, sql: str, params: Iterable[Sequence[Any]]
    ) -> Any: ...
    def query_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> Optional[Any]: ...
    def query_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list: ...


def make_store_from_env(
    sqlite_path: Optional[str] = None,
) -> "Store":
    """Backend-selecting store factory.

    Returns a `PostgresStore` when ``MAC_DATABASE_URL`` is set to a
    ``postgres://`` or ``postgresql://`` DSN; otherwise a `SQLiteStore`
    at the explicitly passed ``sqlite_path`` or ``MAC_DB``.

    There is deliberately no home-directory fallback. A process that owns a
    control plane must declare its durable authority; an operator client must
    never acquire a private ``~/.mac/mac.db`` merely by importing or starting
    MAC without a hub configuration.

    The Postgres backend auto-applies the bundled schema on first
    construction so a fresh CNPG cluster comes up ready; this server factory
    likewise constructs SQLite with schema initialization enabled.
    """
    role = os.environ.get("MAC_CONTROL_PLANE_ROLE", "").strip().lower()
    if role == "client":
        raise StoreError(
            "MAC_CONTROL_PLANE_ROLE=client cannot own a database; connect to the "
            "configured hub instead"
        )
    dsn = os.environ.get("MAC_DATABASE_URL", "").strip()
    if dsn:
        if not dsn.startswith(("postgres://", "postgresql://")):
            raise StoreError(
                "unsupported MAC_DATABASE_URL scheme; expected postgres:// or "
                "postgresql://"
            )
        from mac.store_postgres import PostgresStore

        pool_size = int(os.environ.get("MAC_PG_POOL_SIZE", "10") or "10")
        store = PostgresStore(dsn, pool_size=pool_size)
        store.initialize()
        return store
    path = sqlite_path or os.environ.get("MAC_DB", "").strip()
    if not path:
        raise StoreError(
            "control-plane database is not configured; set MAC_DATABASE_URL for "
            "PostgreSQL or MAC_DB to an explicit SQLite path. MAC no longer "
            "creates ~/.mac/mac.db implicitly."
        )
    return SQLiteStore(path)


class SQLiteStore:
    """Durable SQLite backing store for the control plane.

    Server startup uses the default ``initialize_schema=True`` to create and
    migrate the authority. Routine direct CLI access to an existing database
    passes ``False`` so a read does not acquire schema or journal-mode locks.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        initialize_schema: bool = True,
    ) -> None:
        if not path:
            raise StoreError(
                "SQLiteStore requires an explicit database path; pass a path "
                "directly or configure MAC_DB through make_store_from_env()"
            )
        self.path = path
        if path != ":memory:":
            database = Path(path).expanduser()
            if initialize_schema:
                database.parent.mkdir(parents=True, exist_ok=True)
            elif not database.is_file():
                raise StoreError(
                    "SQLite authority database does not exist: %s; run `mac --db %s init` "
                    "to initialize a standalone authority" % (database, database)
                )
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if initialize_schema and path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        if initialize_schema:
            self._initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                # Cancellation and interpreter-level exits inherit directly
                # from BaseException. If one crosses the context boundary,
                # leaving this shared connection in_transaction poisons every
                # later API write with "cannot start a transaction within a
                # transaction". Always unwind before releasing the RLock.
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        # Autocommit semantics: SQLite commits a single statement on its own.
        # Inside an explicit transaction() block, statements run as part of that
        # transaction instead.
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(
                """
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
                CREATE INDEX IF NOT EXISTS idx_users_tenant
                    ON users (tenant_id);

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
                CREATE INDEX IF NOT EXISTS idx_personas_tenant
                    ON personas (tenant_id);

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
                CREATE INDEX IF NOT EXISTS idx_hermes_instances_tenant
                    ON hermes_instances (tenant_id);

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
                CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance
                    ON platform_bindings (hermes_instance_id);

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
                    human_assignees TEXT,
                    created_by_human TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_state_priority
                    ON tasks (state, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_review_queue
                    ON tasks (priority DESC, created_at, id)
                    WHERE state IN ('needs_review', 'reviewing');
                CREATE INDEX IF NOT EXISTS idx_tasks_state_updated
                    ON tasks (state, updated_at, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_owner
                    ON tasks (owner_agent_id);
                -- mac-1hnt: enforce the task state machine at the DB
                -- layer. A SQLite CHECK constraint can't be added to
                -- an existing table, so use a trigger that rejects
                -- INSERTs/UPDATEs with a state outside the enum. The
                -- Python ``validate_transition`` still handles the
                -- richer "from → to" rule; this trigger is the
                -- belt-and-braces against bare UPDATEs and bugs.
                CREATE TRIGGER IF NOT EXISTS trg_tasks_state_enum_ins
                BEFORE INSERT ON tasks
                FOR EACH ROW
                WHEN NEW.state NOT IN (
                    'open', 'blocked', 'claimed', 'running',
                    'needs_review', 'reviewing', 'completed',
                    'failed', 'cancelled'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task state');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_tasks_state_enum_upd
                BEFORE UPDATE OF state ON tasks
                FOR EACH ROW
                WHEN NEW.state NOT IN (
                    'open', 'blocked', 'claimed', 'running',
                    'needs_review', 'reviewing', 'completed',
                    'failed', 'cancelled'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task state');
                END;

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
                CREATE INDEX IF NOT EXISTS idx_task_history_task_created
                    ON task_history (task_id, created_at);
                -- mac-ykkc: index event_type so debug/audit queries by action
                -- (e.g. counting task.review_claimed) are not table scans.
                CREATE INDEX IF NOT EXISTS idx_task_history_event_type
                    ON task_history (event_type, task_id);

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
                CREATE INDEX IF NOT EXISTS idx_task_transition_outbox_status
                    ON task_transition_outbox (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_transition_outbox_task
                    ON task_transition_outbox (task_id, created_at);

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
                CREATE INDEX IF NOT EXISTS idx_evidence_task
                    ON evidence (task_id);

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
                    -- PR2c (spec §6.3, Option B): dispatcher (lease owner)
                    -- may delegate lifecycle authorship to the role agent
                    -- spawned in the task Job. NULL = no delegation; the
                    -- owner is the sole authoriser.
                    delegated_agent_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_leases_task_status
                    ON leases (task_id, status);
                CREATE INDEX IF NOT EXISTS idx_leases_agent_status
                    ON leases (agent_id, status);
                CREATE INDEX IF NOT EXISTS idx_leases_status_expiry
                    ON leases (status, expires_at, id);
                -- mac-x5el: enforce "at most one ACTIVE lease per task"
                -- at the DB layer so a Python bug or a manual UPDATE
                -- cannot produce duplicate active leases that confuse
                -- claim/release/expire.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_active_per_task
                    ON leases (task_id) WHERE status = 'active';

                -- media-01 service-role election: desired media services + the
                -- leased holds capable hosts claim (mirrors tasks+leases).
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
                -- Pool model: a host holds an op at most once; multiple hosts may
                -- hold the same op. Split-brain guard at the DB layer.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_service_claims_active_per_role_agent
                    ON service_claims (service_role_id, agent_id) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS machines (
                    id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    resources TEXT NOT NULL,
                    trusted INTEGER NOT NULL,
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agents_status_health
                    ON agents (status, health_status);

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
                CREATE INDEX IF NOT EXISTS idx_fleets_status_name
                    ON fleets (status, name);
                CREATE INDEX IF NOT EXISTS idx_fleets_tenant
                    ON fleets (tenant_id);

                CREATE TABLE IF NOT EXISTS fleet_agents (
                    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (fleet_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_agents_agent
                    ON fleet_agents (agent_id);

                CREATE TABLE IF NOT EXISTS fleet_agent_observations (
                    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (fleet_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_agent_observations_agent
                    ON fleet_agent_observations (agent_id);
                CREATE INDEX IF NOT EXISTS idx_fleet_agent_observations_last_seen
                    ON fleet_agent_observations (last_seen_at);

                CREATE TABLE IF NOT EXISTS fleet_events (
                    id TEXT PRIMARY KEY,
                    fleet_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_events_fleet_created
                    ON fleet_events (fleet_id, created_at);

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
                CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
                    ON messages (recipient_agent_id, status);

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
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    source TEXT NOT NULL,
                    level TEXT NOT NULL,
                    name TEXT NOT NULL,
                    subject_type TEXT,
                    subject_id TEXT,
                    value REAL,
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
                    duration_ms REAL,
                    returncode INTEGER,
                    stdout_sha256 TEXT,
                    stderr_sha256 TEXT,
                    stdout_bytes INTEGER,
                    stderr_bytes INTEGER,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_command_audit_created
                    ON command_audit (created_at, id);
                CREATE INDEX IF NOT EXISTS idx_command_audit_agent_created
                    ON command_audit (agent_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_command_audit_task_created
                    ON command_audit (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_command_audit_command
                    ON command_audit (command_id, created_at);

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

                -- Per-agent operational events (mood transitions, nap
                -- lifecycle, future agent-level audit). Flows through the
                -- unified events view.
                CREATE TABLE IF NOT EXISTS agent_events (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_agent_created
                    ON agent_events (agent_id, created_at);

                -- Append-only mood transitions. The current mood is the most
                -- recent row per agent where cleared_at IS NULL and
                -- (expires_at IS NULL OR expires_at > now). Agents pick their
                -- own mood; mac records.
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
                CREATE INDEX IF NOT EXISTS idx_mood_overlays_agent_set_at
                    ON mood_overlays (agent_id, set_at);

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
                CREATE INDEX IF NOT EXISTS idx_nap_runs_agent_started
                    ON nap_runs (agent_id, started_at);

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
                CREATE INDEX IF NOT EXISTS idx_reviews_task_status
                    ON reviews (task_id, status);

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

                -- Gateway-side provenance: who is talking to which Hermes
                -- instance, in which platform thread, about which task.
                -- Content stays in Hermes; mac only records the pointer.
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

                -- Vector-memory-side provenance: a Hermes memory record may be
                -- mirrored into a vector store (Qdrant, pgvector, etc.). mac
                -- never stores embeddings; it only audits "this memory was
                -- indexed at this point id in this collection."
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
                CREATE INDEX IF NOT EXISTS idx_vector_refs_memory
                    ON vector_refs (memory_id);

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
                CREATE INDEX IF NOT EXISTS idx_artifacts_kind
                    ON artifacts (kind);

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
                CREATE INDEX IF NOT EXISTS idx_projects_status_name
                    ON projects (status, name);

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
                CREATE INDEX IF NOT EXISTS idx_memory_task_created
                    ON memory_records (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_memory_subject
                    ON memory_records (subject_type, subject_id);

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
                    baseline_score REAL,
                    regression_threshold REAL NOT NULL DEFAULT 0,
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
                    score REAL NOT NULL,
                    baseline_score REAL,
                    delta REAL,
                    threshold REAL NOT NULL,
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
                CREATE INDEX IF NOT EXISTS idx_eval_set_events_set
                    ON eval_set_events (eval_set_id, created_at);

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
                    control_policy_id TEXT NOT NULL,
                    treatment_policy_id TEXT NOT NULL,
                    primary_metric TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    min_effect REAL NOT NULL DEFAULT 0,
                    quality_margin REAL NOT NULL DEFAULT 0.05,
                    min_samples_per_arm INTEGER NOT NULL,
                    max_samples_per_arm INTEGER NOT NULL,
                    exploration_fraction REAL NOT NULL,
                    outcome_horizon_seconds REAL NOT NULL,
                    guardrails TEXT NOT NULL DEFAULT '{}',
                    auto_promote INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(control_policy_id) REFERENCES scientific_policies(id),
                    FOREIGN KEY(treatment_policy_id) REFERENCES scientific_policies(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scientific_experiments_project_state
                    ON scientific_experiments(project, state, created_at);

                CREATE TABLE IF NOT EXISTS scientific_assignments (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    arm TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    propensity REAL NOT NULL,
                    stratum TEXT NOT NULL DEFAULT '',
                    assignment TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, task_id),
                    FOREIGN KEY(experiment_id) REFERENCES scientific_experiments(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(policy_id) REFERENCES scientific_policies(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scientific_assignments_experiment_arm
                    ON scientific_assignments(experiment_id, phase, arm, assigned_at);

                CREATE TABLE IF NOT EXISTS scientific_observations (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    quality_validated INTEGER NOT NULL DEFAULT 0,
                    metrics TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, task_id),
                    FOREIGN KEY(experiment_id) REFERENCES scientific_experiments(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS scientific_decisions (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES scientific_experiments(id) ON DELETE CASCADE
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
                CREATE INDEX IF NOT EXISTS idx_agent_roles_slug_tenant
                    ON agent_roles (slug, tenant_id);
                CREATE INDEX IF NOT EXISTS idx_agent_roles_reports_to
                    ON agent_roles (reports_to);

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
                CREATE INDEX IF NOT EXISTS idx_workflows_type_enabled
                    ON workflows (workflow_type, enabled);

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
                CREATE INDEX IF NOT EXISTS idx_workflow_drafts_status
                    ON workflow_drafts (status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_workflow_drafts_tenant
                    ON workflow_drafts (tenant_id, updated_at);

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
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_state
                    ON workflow_runs (state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_current_task
                    ON workflow_runs (current_task_id);
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow
                    ON workflow_runs (workflow_id, created_at);

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
                CREATE INDEX IF NOT EXISTS idx_workflow_run_history_run
                    ON workflow_run_history (run_id, seq);

                -- Provisioning requests: durable record of "the swarm needs
                -- an agent it does not have." Surfaced by the dispatcher
                -- and the default-review workflow when no eligible agent
                -- can be selected. A future provisioner (k8s operator,
                -- nomad job, local spawner) polls this table.
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

                -- Unified audit stream. Operators query one surface instead of
                -- joining four per-resource tables. The view is read-only; each
                -- write still goes to its owning table inside the originating
                -- transaction, so audit trail and durable state commit together.
                DROP VIEW IF EXISTS events;
                CREATE VIEW events AS
                    SELECT
                        id,
                        'task' AS subject_type,
                        task_id AS subject_id,
                        event_type,
                        actor,
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.from_state', from_state,
                            '$.to_state', to_state
                        ) AS detail,
                        created_at
                    FROM task_history
                    UNION ALL
                    SELECT id, 'rollout', rollout_id, event_type, actor, detail, created_at
                    FROM rollout_events
                    UNION ALL
                    SELECT id, 'eval_set', eval_set_id, event_type, actor, detail, created_at
                    FROM eval_set_events
                    UNION ALL
                    SELECT
                        id,
                        'secret',
                        secret_id,
                        'secret.' || result,
                        accessor_agent_id,
                        json_object(
                            'purpose', purpose,
                            'expires_at', expires_at,
                            'revealed_at', revealed_at
                        ),
                        created_at
                    FROM secret_access_audit
                    UNION ALL
                    SELECT id, 'environment', environment_id, event_type, actor, detail, created_at
                    FROM environment_events
                    UNION ALL
                    SELECT id, 'project', project_id, event_type, actor, detail, created_at
                    FROM project_events
                    UNION ALL
                    SELECT id, 'fleet', fleet_id, event_type, actor, detail, created_at
                    FROM fleet_events
                    UNION ALL
                    SELECT id, 'agent', agent_id, event_type, actor, detail, created_at
                    FROM agent_lifecycle_events
                    UNION ALL
                    SELECT id, 'agent', agent_id, event_type, actor, detail, created_at
                    FROM agent_events
                    UNION ALL
                    SELECT
                        id,
                        CASE WHEN task_id IS NOT NULL THEN 'task' ELSE 'agent' END,
                        COALESCE(task_id, agent_id),
                        'command.' || phase,
                        agent_id,
                        json_object(
                            'command_id', command_id,
                            'agent_id', agent_id,
                            'argv0', json_extract(argv, '$[0]'),
                            'argv_redacted', json('true'),
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
                            'metadata', json(metadata)
                        ),
                        created_at
                    FROM command_audit
                    UNION ALL
                    SELECT
                        event_id,
                        COALESCE(NULLIF(subject_type, ''), 'action_event'),
                        COALESCE(subject_id, event_id),
                        'action.' || action_type || '.' || action_name,
                        actor,
                        json_object(
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
                            'attributes', json(attributes)
                        ),
                        timestamp
                    FROM action_events
                    UNION ALL
                    -- Conversation threads project as one event per row: the
                    -- "thread_tracked" observation at last_seen_at. This
                    -- surfaces gateway activity in the unified audit stream
                    -- without needing a sibling events table.
                    SELECT
                        id,
                        'conversation_thread',
                        id,
                        'gateway.thread_tracked',
                        'gateway',
                        json_object(
                            'platform_binding_id', platform_binding_id,
                            'external_thread_id', external_thread_id,
                            'latest_task_id', latest_task_id,
                            'summary', summary
                        ),
                        last_seen_at
                    FROM conversation_threads
                    UNION ALL
                    -- Vector refs project as one event per row: the
                    -- "indexed" observation at creation time.
                    SELECT
                        id,
                        'vector_ref',
                        memory_id,
                        'vector.indexed',
                        created_by,
                        json_object(
                            'vector_db', vector_db,
                            'collection', collection,
                            'point_id', point_id,
                            'embedding_model', embedding_model
                        ),
                        created_at
                    FROM vector_refs;

                -- ----------------------------------------------------------------
                -- Source release registry (mac.source_release.v1)
                -- Immutable record of a reviewed/published commit. Uniqueness on
                -- (repository_id, commit_sha) is enforced by a UNIQUE index.
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS source_releases (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    canonical_remote_url TEXT NOT NULL,
                    -- Immutable 40-char hex SHA. A CHECK + trigger pair ensures the
                    -- column is never updated after creation.
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
                    CHECK(length(commit_sha) = 40 AND commit_sha GLOB '[0-9a-f]*'),
                    -- Reject branch refs
                    CHECK(canonical_ref NOT LIKE 'refs/heads/%')
                );
                CREATE INDEX IF NOT EXISTS idx_source_releases_repo_status
                    ON source_releases (repository_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_source_releases_status_created
                    ON source_releases (status, created_at);
                -- Immutability trigger: once a row exists, commit_sha may never change.
                CREATE TRIGGER IF NOT EXISTS trg_source_releases_sha_immutable
                BEFORE UPDATE OF commit_sha ON source_releases
                FOR EACH ROW
                WHEN NEW.commit_sha != OLD.commit_sha
                BEGIN
                    SELECT RAISE(ABORT, 'source_releases.commit_sha is immutable');
                END;

                -- ----------------------------------------------------------------
                -- Fleet desired-source state (mac.fleet_desired_source.v1)
                -- Current desired release for a fleet/environment scope.
                -- One active row per scope; generation is monotonically increasing.
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS fleet_desired_source_states (
                    id TEXT PRIMARY KEY,
                    -- Scope: exactly one of fleet_id / environment_id must be non-NULL
                    fleet_id TEXT REFERENCES fleets(id) ON DELETE CASCADE,
                    environment_id TEXT REFERENCES environments(id) ON DELETE CASCADE,
                    -- Monotonic generation (>= 1)
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
                -- Partial unique indexes: one desired-source row per fleet/env scope.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_fleet
                    ON fleet_desired_source_states (fleet_id) WHERE fleet_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_env
                    ON fleet_desired_source_states (environment_id) WHERE environment_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_fleet
                    ON fleet_desired_source_states (fleet_id, generation);
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_env
                    ON fleet_desired_source_states (environment_id, generation);
                -- Monotonicity trigger: generation may only increase.
                CREATE TRIGGER IF NOT EXISTS trg_fleet_desired_source_gen_monotonic
                BEFORE UPDATE OF generation ON fleet_desired_source_states
                FOR EACH ROW
                WHEN NEW.generation <= OLD.generation
                BEGIN
                    SELECT RAISE(ABORT, 'fleet_desired_source_states.generation must increase monotonically');
                END;

                -- ----------------------------------------------------------------
                -- Desired-source transition history (append-only audit log)
                -- ----------------------------------------------------------------
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

                -- ----------------------------------------------------------------
                -- Desired-source idempotency records
                -- Prevents double-application of the same request_id per scope.
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS fleet_desired_source_idempotency (
                    id TEXT PRIMARY KEY,
                    -- Denormalised scope key for fast lookup (fleet:<id> | env:<id>)
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

                -- Human principals registry: first-class assignable human identities
                -- (username / email / GitHub login) and explicit group membership.
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
                """
            )
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        # beads→mac: the project repository registry was historically the
        # `beads_repositories` table. `project_repositories` is created empty
        # during table setup, so copy any legacy rows over and drop the old
        # table. Idempotent (skipped once the legacy table is gone); columns
        # are identical, so a positional copy is safe.
        legacy_repo_table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = 'beads_repositories'"
        ).fetchone()
        if legacy_repo_table is not None:
            self._conn.execute(
                "INSERT OR IGNORE INTO project_repositories SELECT * FROM beads_repositories"
            )
            self._conn.execute("DROP TABLE beads_repositories")
        # mac-s2vz: record when an agent's attestation key was last
        # rotated so the verifier can produce a clear error message
        # (key-rotation-after-signature, not "signature does not verify")
        # for evidence signed under a now-retired key.
        self._ensure_column(
            "agents", "attestation_key_rotated_at", "attestation_key_rotated_at TEXT"
        )
        # mac-1oi4: capture who asked for an agent so fulfill can refuse
        # a self-fulfill (the same actor approving its own request).
        self._ensure_column(
            "agent_provisioning_requests", "requested_by", "requested_by TEXT"
        )
        self._ensure_column("secret_access_audit", "expires_at", "expires_at TEXT")
        self._ensure_column("secret_access_audit", "revealed_at", "revealed_at TEXT")
        self._ensure_column("publications", "content_hash", "content_hash TEXT")
        self._ensure_column("rollouts", "tenant_id", "tenant_id TEXT")
        self._ensure_column("rollouts", "channel", "channel TEXT NOT NULL DEFAULT 'fleet'")
        self._ensure_column("rollouts", "runtime_environment_id", "runtime_environment_id TEXT")
        self._ensure_column("rollouts", "artifact_uri", "artifact_uri TEXT")
        self._ensure_column("rollouts", "artifact_hash", "artifact_hash TEXT")
        self._ensure_column("rollouts", "health_policy", "health_policy TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("rollouts", "required_eval_set_id", "required_eval_set_id TEXT")
        self._ensure_column("rollouts", "deploy_environment_id", "deploy_environment_id TEXT")
        self._ensure_column("agents", "running_digest", "running_digest TEXT")
        self._ensure_column("agents", "role_id", "role_id TEXT")
        self._ensure_column("agents", "hermes_instance_id", "hermes_instance_id TEXT")
        self._ensure_column(
            "agents", "attestation_key_ciphertext", "attestation_key_ciphertext TEXT"
        )
        self._ensure_column(
            "agents", "installed_packages", "installed_packages TEXT NOT NULL DEFAULT '{}'"
        )
        # schema_dispatch_hold: per-agent dispatch hold + zombie-detection counters.
        self._ensure_column(
            "agents", "dispatch_hold", "dispatch_hold INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(
            "agents", "dispatch_hold_reason", "dispatch_hold_reason TEXT"
        )
        self._ensure_column(
            "agents", "dispatch_hold_at", "dispatch_hold_at TEXT"
        )
        self._ensure_column(
            "agents",
            "consecutive_lease_expiries_no_telemetry",
            "consecutive_lease_expiries_no_telemetry INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            "agents", "last_control_stream_published_at", "last_control_stream_published_at TEXT"
        )
        self._ensure_column(
            "agents", "last_control_stream_consumed_at", "last_control_stream_consumed_at TEXT"
        )
        self._ensure_column("machines", "hardware", "hardware TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("tasks", "started_at", "started_at TEXT")
        self._ensure_column("tasks", "completed_at", "completed_at TEXT")
        self._ensure_column("tasks", "workflow_run_id", "workflow_run_id TEXT")
        self._ensure_column("tasks", "workflow_node_key", "workflow_node_key TEXT")
        self._ensure_column(
            "workflow_runs", "next_action_at", "next_action_at TEXT"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_runs_next_action "
            "ON workflow_runs (state, next_action_at, id)"
        )
        # PR2c (spec §6.3, Option B): dispatcher (lease owner) may delegate
        # lifecycle authorship to the role agent spawned in the task Job.
        self._ensure_column("leases", "delegated_agent_id", "delegated_agent_id TEXT")
        # Evidence artifact bytes may be externalized to the hub blob store
        # (mac.evidence_blobs); the row keeps digest + URI, content_base64 "".
        self._ensure_column(
            "evidence_artifacts", "content_uri", "content_uri TEXT NOT NULL DEFAULT ''"
        )
        # mac-src1: source release and desired-source tables may be absent on
        # databases created before this migration. CREATE TABLE IF NOT EXISTS is
        # safe on both fresh and pre-existing databases; the triggers/indexes
        # use IF NOT EXISTS too.  We run the same DDL blocks used in
        # _initialize so older DBs get the full schema on first open.
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_releases (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                canonical_remote_url TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                canonical_ref TEXT NOT NULL,
                tree_digest TEXT NOT NULL,
                artifact_digest TEXT,
                image_digest TEXT,
                created_by TEXT NOT NULL,
                created_by_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                review_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                publication_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(repository_id, commit_sha),
                CHECK(length(commit_sha) = 40 AND commit_sha GLOB '[0-9a-f]*'),
                CHECK(canonical_ref NOT LIKE 'refs/heads/%')
            );
            CREATE INDEX IF NOT EXISTS idx_source_releases_repo_status
                ON source_releases (repository_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_source_releases_status_created
                ON source_releases (status, created_at);
            CREATE TRIGGER IF NOT EXISTS trg_source_releases_sha_immutable
            BEFORE UPDATE OF commit_sha ON source_releases
            FOR EACH ROW
            WHEN NEW.commit_sha != OLD.commit_sha
            BEGIN
                SELECT RAISE(ABORT, 'source_releases.commit_sha is immutable');
            END;

            CREATE TABLE IF NOT EXISTS fleet_desired_source_states (
                id TEXT PRIMARY KEY,
                fleet_id TEXT REFERENCES fleets(id) ON DELETE CASCADE,
                environment_id TEXT REFERENCES environments(id) ON DELETE CASCADE,
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
                CHECK(generation >= 1),
                CHECK(fleet_id IS NOT NULL OR environment_id IS NOT NULL)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_fleet
                ON fleet_desired_source_states (fleet_id) WHERE fleet_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_env
                ON fleet_desired_source_states (environment_id) WHERE environment_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_fleet
                ON fleet_desired_source_states (fleet_id, generation);
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_env
                ON fleet_desired_source_states (environment_id, generation);
            CREATE TRIGGER IF NOT EXISTS trg_fleet_desired_source_gen_monotonic
            BEFORE UPDATE OF generation ON fleet_desired_source_states
            FOR EACH ROW
            WHEN NEW.generation <= OLD.generation
            BEGIN
                SELECT RAISE(ABORT, 'fleet_desired_source_states.generation must increase monotonically');
            END;

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

            -- Human principals registry: first-class assignable human identities
            -- (username / email / GitHub login) and explicit group membership.
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
            """
        )
        # Human assignees and identity stamp on tasks: human_assignees is a
        # JSON list of human ids/logins; created_by_human stamps the task's
        # creating human identity. Both are nullable so existing rows keep
        # their NULL values after the migration.
        self._ensure_column(
            "tasks", "human_assignees", "human_assignees TEXT"
        )
        self._ensure_column(
            "tasks", "created_by_human", "created_by_human TEXT"
        )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in columns:
            self._conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, definition))

    # ------------------------------------------------------------------
    # Human principals CRUD helpers
    # ------------------------------------------------------------------
    # These helpers mirror the style of the rest of SQLiteStore: callers
    # are responsible for JSON-serialising / deserialising list fields.
    # ``groups`` is stored as a JSON array text column.  The upsert also
    # reconciles the ``human_groups`` membership table so both the denorm
    # JSON column and the normalised table stay in sync.
    # ------------------------------------------------------------------

    def upsert_human(
        self,
        human_id: str,
        username: str,
        *,
        email: Optional[str] = None,
        github_login: Optional[str] = None,
        display_name: Optional[str] = None,
        groups: Optional[list] = None,
        created_at: str,
        updated_at: str,
    ) -> None:
        """Insert or replace a human row and reconcile group membership."""
        import json as _json

        groups_json = _json.dumps(sorted(set(groups or [])))
        self.execute(
            """
            INSERT INTO humans (id, username, email, github_login, display_name,
                                groups, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username     = excluded.username,
                email        = excluded.email,
                github_login = excluded.github_login,
                display_name = excluded.display_name,
                groups       = excluded.groups,
                updated_at   = excluded.updated_at
            """,
            (
                human_id,
                username,
                email,
                github_login,
                display_name,
                groups_json,
                created_at,
                updated_at,
            ),
        )
        # Reconcile human_groups: remove rows no longer in the groups list,
        # then insert any new ones (idempotent via INSERT OR IGNORE).
        current_groups = sorted(set(groups or []))
        self.execute(
            "DELETE FROM human_groups WHERE human_id = ?", (human_id,)
        )
        for group_name in current_groups:
            self.execute(
                """
                INSERT OR IGNORE INTO human_groups (id, human_id, group_name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "hg_%s_%s" % (human_id, group_name),
                    human_id,
                    group_name,
                    created_at,
                ),
            )

    def get_human(self, human_id: str) -> Optional[Any]:
        """Return the human row for ``human_id``, or None if not found."""
        return self.query_one(
            "SELECT * FROM humans WHERE id = ?", (human_id,)
        )

    def get_human_by_username(self, username: str) -> Optional[Any]:
        """Return the human row for ``username``, or None if not found."""
        return self.query_one(
            "SELECT * FROM humans WHERE username = ?", (username,)
        )

    def list_humans(self, *, group: Optional[str] = None) -> list:
        """Return all humans, optionally filtered by group membership."""
        if group is not None:
            return self.query_all(
                """
                SELECT h.* FROM humans h
                INNER JOIN human_groups hg ON hg.human_id = h.id
                WHERE hg.group_name = ?
                ORDER BY h.username
                """,
                (group,),
            )
        return self.query_all("SELECT * FROM humans ORDER BY username")

    def delete_human(self, human_id: str) -> bool:
        """Delete a human by id; returns True if a row was deleted."""
        cursor = self.execute(
            "DELETE FROM humans WHERE id = ?", (human_id,)
        )
        return cursor.rowcount > 0
