"""Persistence store abstractions for the control plane.

Defines the store and connection protocols, the store error type, and the
factory that opens the PostgreSQL authority.

PostgreSQL is the only backend. The SQLite implementation that used to live
here was removed once the suite ran against the engine the fleet actually
uses: two backends meant the tests could agree with something production did
not run, and they did -- a task state the live Postgres trigger rejected, a
store surface missing sixteen methods, and a read-your-own-writes bug that a
single serialized connection had hidden. The schema now has one home,
``src/mac/data/postgres/schema.sql``.
"""

from __future__ import annotations

import os
from typing import (
    Any,
    ContextManager,
    Iterable,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Task-flow analytics DDL (mac.task_flow_span.v1 / mac.task_completion.v1).
#
# Shared between _initialize (fresh databases) and _migrate (existing databases
# created before these tables were introduced) so the two paths cannot drift.
# All statements use IF NOT EXISTS and the tables are keyed for idempotent
# recompute: a span UPSERTs on (task_id, attempt, stage) and a completion
# UPSERTs on (task_id, attempt), so a backfill over historical task_history /
# reviews / publications populates rows in place rather than appending.
# ---------------------------------------------------------------------------
TASK_FLOW_ANALYTICS_DDL = """
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
    duration_seconds REAL,
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
    duration_seconds REAL,
    -- pending | completed | failed | cancelled
    outcome TEXT NOT NULL DEFAULT 'pending',
    -- Landed commit and canonical-branch head at landing time.
    publication_sha TEXT,
    main_sha TEXT,
    -- Throughput signals.
    route_count INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER NOT NULL DEFAULT 0,
    cost_count REAL NOT NULL DEFAULT 0,
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
    warning_seconds REAL NOT NULL,
    critical_seconds REAL NOT NULL,
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
    age_seconds REAL NOT NULL,
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
    duration_seconds REAL NOT NULL,
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
    age_seconds REAL NOT NULL,
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
    wait_seconds REAL,
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
-- (src/mac/fleet_release_epoch_service.py). One row per contention episode
-- where a publisher and/or an epoch opener waited on the shared publication
-- barrier. Mirrors the task_resource_contentions conventions (TEXT ids,
-- queue depth + wait window, JSON metadata, CHECK constraints, supporting
-- indexes). Persistence substrate only; admission/lock semantics are
-- unchanged by this table.
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
    wait_seconds REAL,
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
"""


class StoreError(Exception):
    """Backend-neutral persistence error.

    PostgresStore wraps psycopg's driver-native errors in this type, so callers
    catch one exception class regardless of what the driver raised.
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
    def backend_identity(self) -> "dict[str, Any]": ...

    # Higher-level helpers, implemented once in StoreHelpersMixin. Declared
    # here so a backend that lacks one fails the protocol check instead of
    # raising AttributeError at the first request that needs it.
    def upsert_human(self, *args: Any, **kwargs: Any) -> None: ...
    def get_human(self, human_id: str) -> Optional[Any]: ...
    def get_human_by_username(self, username: str) -> Optional[Any]: ...
    def list_humans(self, *, group: Optional[str] = None) -> list: ...
    def delete_human(self, human_id: str) -> bool: ...
    def set_pipeline_cursor(self, scope: str, name: str, value: Any) -> None: ...
    def get_pipeline_cursor(
        self, scope: str, name: str, default: Any = None
    ) -> Any: ...
    def upsert_task_flow_span(self, *args: Any, **kwargs: Any) -> None: ...
    def upsert_task_completion(self, *args: Any, **kwargs: Any) -> None: ...
    def get_task_completion(self, *args: Any, **kwargs: Any) -> Optional[Any]: ...
    def list_task_flow_spans_by_task(self, *args: Any, **kwargs: Any) -> list: ...
    def list_task_flow_spans_by_project(self, *args: Any, **kwargs: Any) -> list: ...
    def query_task_flow_stage_aggregates(self, *args: Any, **kwargs: Any) -> list: ...
    def record_fleet_release_admission_episode(
        self, *args: Any, **kwargs: Any
    ) -> None: ...
    def get_fleet_release_admission_episode(
        self, *args: Any, **kwargs: Any
    ) -> Optional[Any]: ...
    def list_fleet_release_admission_episodes(
        self, *args: Any, **kwargs: Any
    ) -> list: ...


def make_store_from_env(
    dsn: Optional[str] = None,
    *,
    initialize_schema: bool = True,
) -> "Store":
    """Open the control-plane database.

    The DSN comes from the explicit argument, then ``MAC_DATABASE_URL``, then
    ``MAC_DB``. All three must be ``postgres://`` or ``postgresql://``:
    PostgreSQL is the only supported backend. SQLite was removed because a
    second engine meant the suite could agree with something the fleet does not
    run -- which it did, shipping a task state the live Postgres trigger
    rejected and a store surface missing sixteen methods.

    There is deliberately no fallback. A process that owns a control plane must
    declare its durable authority; an operator client must never acquire a
    private one merely by importing or starting MAC without a configuration.

    Schema ownership is explicit. Control-plane startup uses the default
    ``initialize_schema=True`` so a fresh authority comes up ready. Ancillary
    processes that attach to an already-running authority must pass ``False``;
    doing so prevents routine data-plane commands from taking PostgreSQL DDL
    locks against live task and lease traffic.
    """
    role = os.environ.get("MAC_CONTROL_PLANE_ROLE", "").strip().lower()
    if role == "client":
        raise StoreError(
            "MAC_CONTROL_PLANE_ROLE=client cannot own a database; connect to the "
            "configured hub instead"
        )
    resolved = (
        (dsn or "").strip()
        or os.environ.get("MAC_DATABASE_URL", "").strip()
        or os.environ.get("MAC_DB", "").strip()
    )
    if not resolved:
        raise StoreError(
            "control-plane database is not configured; set MAC_DATABASE_URL to a "
            "PostgreSQL DSN. MAC does not create a database implicitly."
        )
    return open_postgres_store(resolved, initialize_schema=initialize_schema)


def open_postgres_store(dsn: str, *, initialize_schema: bool = True) -> "Store":
    """Open a `PostgresStore` on ``dsn``, rejecting any other scheme.

    Shared by every entry point that accepts a database from an operator --
    the factory above, ``mac --db``, and the API's injected DSN -- so they all
    reject a stray SQLite path with the same message rather than each
    inventing one.
    """
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise StoreError(
            "unsupported control-plane DSN %r; expected postgres:// or "
            "postgresql://. SQLite is no longer supported." % dsn
        )
    from mac.store_postgres import PostgresStore

    pool_size = int(os.environ.get("MAC_PG_POOL_SIZE", "10") or "10")
    store = PostgresStore(dsn, pool_size=pool_size)
    if initialize_schema:
        store.initialize()
    return store
