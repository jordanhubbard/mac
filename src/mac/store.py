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
    def record_fleet_release_admission_episode(
        self, *args: Any, **kwargs: Any
    ) -> None: ...
    def get_fleet_release_admission_episode(
        self, *args: Any, **kwargs: Any
    ) -> Optional[Any]: ...
    def list_fleet_release_admission_episodes(
        self, *args: Any, **kwargs: Any
    ) -> list: ...
    def record_deploy_generation_retirement(
        self, *args: Any, **kwargs: Any
    ) -> str: ...
    def get_deploy_generation_retirement(
        self, agent_id: str, generation: str
    ) -> Optional[Any]: ...
    def is_deploy_generation_retired(
        self, agent_id: str, generation: str
    ) -> bool: ...
    def list_deploy_generation_retirements(
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
