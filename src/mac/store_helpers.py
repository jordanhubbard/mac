"""Store helpers shared by every backend.

These are the higher-level persistence helpers -- human principals, pipeline
resume cursors, task-flow analytics, fleet-release admission episodes -- that
sit above the seven primitives in the `Store` protocol.

They live here because they used to live on SQLiteStore alone. Nothing
required PostgresStore to have them and the protocol did not declare them, so
`isinstance(postgres_store, Store)` passed while sixteen methods were simply
absent from the backend the fleet actually runs. `GET /humans` returned 500 in
production for as long as that was true.

Every method below is written against `execute` / `query_one` / `query_all`,
which both backends implement (Postgres translates SQLite-shaped SQL
internally), so one definition serves both and the two cannot drift again.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StoreHelpersMixin:
    """Backend-neutral persistence helpers layered on the `Store` primitives."""

    @contextmanager
    def foreign_keys_suspended(self) -> "Iterator[Any]":
        """Yield an executor that is not enforcing foreign keys. Fixtures only.

        Some tests need a row whose parents are irrelevant to what is being
        tested -- a finalization outcome used to check export *filtering*, say,
        whose real parent chain runs through batches, landing receipts,
        repositories, and certifications. Building all of that would test the
        fixture rather than the filter.

        Yields the executor to use rather than suspending enforcement globally,
        because on Postgres the setting is per-session and every store.execute()
        borrows a different pooled connection -- a `SET` on one of them does
        nothing for the next. Callers MUST use the yielded object.
        """
        backend = str(self.backend_identity().get("backend") or "").lower()
        if backend == "postgres":
            with self.transaction() as conn:
                conn.execute("SET LOCAL session_replication_role = replica")
                yield conn
        else:
            # SQLite ignores this pragma inside a transaction, so it is set on
            # the store's own connection and the store is the executor.
            self.execute("PRAGMA foreign_keys = OFF")
            try:
                yield self
            finally:
                self.execute("PRAGMA foreign_keys = ON")

    # Durable pipeline resume cursors (task_repair_d771f872). Opaque,
    # bounded JSON documents keyed by a stable (scope, name). Used by the
    # work-package pipeline controller and the repository ref reconciler so a
    # hub restart resumes from its last bookmark instead of rescanning.
    PIPELINE_CURSOR_MAX_BYTES = 65536

    def set_pipeline_cursor(self, scope: str, name: str, value: Any) -> None:
        import json as _json

        scope_value = str(scope or "").strip()
        name_value = str(name or "").strip()
        if not scope_value or not name_value:
            raise ValueError("pipeline cursor scope and name are required")
        encoded = _json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.PIPELINE_CURSOR_MAX_BYTES:
            raise ValueError(
                "pipeline cursor value exceeds %d bytes"
                % self.PIPELINE_CURSOR_MAX_BYTES
            )
        now = _utcnow_iso()
        self.execute(
            """
            INSERT INTO pipeline_cursors (scope, name, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope, name) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (scope_value, name_value, encoded, now),
        )

    def get_pipeline_cursor(self, scope: str, name: str, default: Any = None) -> Any:
        import json as _json

        scope_value = str(scope or "").strip()
        name_value = str(name or "").strip()
        if not scope_value or not name_value:
            return default
        row = self.query_one(
            "SELECT value FROM pipeline_cursors WHERE scope = ? AND name = ?",
            (scope_value, name_value),
        )
        if row is None:
            return default
        try:
            return _json.loads(row["value"])
        except (TypeError, ValueError):
            return default

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
        # then insert any new ones. ON CONFLICT DO NOTHING rather than SQLite's
        # INSERT OR IGNORE, which Postgres rejects outright -- shared helpers
        # must be written in SQL both backends accept.
        current_groups = sorted(set(groups or []))
        self.execute(
            "DELETE FROM human_groups WHERE human_id = ?", (human_id,)
        )
        for group_name in current_groups:
            self.execute(
                """
                INSERT INTO human_groups (id, human_id, group_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT DO NOTHING
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

    # -- Task-flow analytics helpers -------------------------------------
    #
    # Read/write helpers for task_flow_spans and task_completions. Writes
    # accept an optional open ``conn`` so callers can participate in an
    # existing transaction (matching observability_service.insert_observation);
    # when ``conn`` is None the helper runs on the store's own connection.
    # UPSERT semantics make a recompute over historical rows idempotent.

    @staticmethod
    def _executor(conn: Optional[Any], store: Any) -> Any:
        """Return the object to execute SQL on: the passed conn or the store."""
        return conn if conn is not None else store

    def record_fleet_release_admission_episode(
        self,
        *,
        episode_id: str,
        barrier_resource_digest: str,
        owner_kind: str,
        waiter_kind: str,
        wait_started_at: str,
        outcome: str,
        created_at: str,
        updated_at: str,
        project: Optional[str] = None,
        owner_id: Optional[str] = None,
        waiter_id: Optional[str] = None,
        waiting_publishers: int = 0,
        waiting_epoch_openers: int = 0,
        queue_depth: int = 0,
        wait_ended_at: Optional[str] = None,
        wait_seconds: Optional[float] = None,
        metadata_json: str = "{}",
        conn: Optional[Any] = None,
    ) -> None:
        """Persist one fair-admission contention episode for the publication barrier.

        Records queue depth (waiting publishers / epoch openers), the wait
        window, the current barrier owner (publisher vs epoch opener plus its
        identifier), and the episode outcome. Keyed on ``episode_id`` so an
        observability layer that refreshes the same episode as it closes calls
        this with the same id and the row is updated in place rather than
        appended. Side-effect-free beyond the write.
        """
        executor = self._executor(conn, self)
        executor.execute(
            """
            INSERT INTO fleet_release_admission_episodes (
                id, project, barrier_resource_digest, owner_kind, owner_id,
                waiter_kind, waiter_id, waiting_publishers,
                waiting_epoch_openers, queue_depth, wait_started_at,
                wait_ended_at, wait_seconds, outcome, metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project                = excluded.project,
                barrier_resource_digest = excluded.barrier_resource_digest,
                owner_kind             = excluded.owner_kind,
                owner_id               = excluded.owner_id,
                waiter_kind            = excluded.waiter_kind,
                waiter_id              = excluded.waiter_id,
                waiting_publishers     = excluded.waiting_publishers,
                waiting_epoch_openers  = excluded.waiting_epoch_openers,
                queue_depth            = excluded.queue_depth,
                wait_started_at        = excluded.wait_started_at,
                wait_ended_at          = excluded.wait_ended_at,
                wait_seconds           = excluded.wait_seconds,
                outcome                = excluded.outcome,
                metadata               = excluded.metadata,
                updated_at             = excluded.updated_at
            """,
            (
                episode_id, project, barrier_resource_digest, owner_kind,
                owner_id, waiter_kind, waiter_id, waiting_publishers,
                waiting_epoch_openers, queue_depth, wait_started_at,
                wait_ended_at, wait_seconds, outcome, metadata_json,
                created_at, updated_at,
            ),
        )

    def get_fleet_release_admission_episode(
        self, episode_id: str
    ) -> Optional[Any]:
        """Return a single admission episode by id, or None."""
        return self.query_one(
            "SELECT * FROM fleet_release_admission_episodes WHERE id = ?",
            (episode_id,),
        )

    def list_fleet_release_admission_episodes(
        self,
        *,
        project: Optional[str] = None,
        barrier_resource_digest: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Return admission episodes, most recent first, optionally narrowed.

        Filters are additive: ``project`` and/or ``barrier_resource_digest``
        scope the barrier, and ``[since, until)`` bounds the creation time.
        """
        clauses: list = []
        params: list = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if barrier_resource_digest is not None:
            clauses.append("barrier_resource_digest = ?")
            params.append(barrier_resource_digest)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until)
        sql = "SELECT * FROM fleet_release_admission_episodes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.query_all(sql, tuple(params))

    # ------------------------------------------------------------------
    # Deploy-generation retirement facts
    # ------------------------------------------------------------------
    # A worker's node-local barrier file records a generation string. Once
    # the matching fleet-release epoch reaches a terminal state, the hub
    # persists that fact here so a later child can tell the worker the
    # generation is retired instead of draining forever. Abort/commit
    # behaviour is unchanged; these helpers only record and look up.

    GENERATION_RETIRED_STATES = frozenset({"aborted", "committed"})

    def record_generation_retirement(
        self,
        *,
        agent_id: str,
        generation: str,
        epoch_id: str,
        retired_state: str,
        prepared_at: str,
        retired_at: str,
        disposition: Optional[str] = None,
        reason: Optional[str] = None,
        conn: Optional[Any] = None,
    ) -> None:
        """Record a terminal generation retirement, optionally in an open txn.

        Keyed on ``(agent_id, generation, epoch_id)``. A second write for the
        same triple updates the row in place so a retry inside the caller's
        transaction is idempotent. ``conn`` is the open ``StoreConnection``
        when the caller already owns a transaction; omit it to write on the
        store's own connection.
        """
        agent_value = str(agent_id or "").strip()
        generation_value = str(generation or "").strip()
        epoch_value = str(epoch_id or "").strip()
        state_value = str(retired_state or "").strip()
        prepared_value = str(prepared_at or "").strip()
        retired_value = str(retired_at or "").strip()
        if not agent_value or not generation_value or not epoch_value:
            raise ValueError(
                "generation retirement requires agent_id, generation, and epoch_id"
            )
        if not prepared_value or not retired_value:
            raise ValueError(
                "generation retirement requires prepared_at and retired_at"
            )
        if state_value not in self.GENERATION_RETIRED_STATES:
            raise ValueError(
                "generation retirement retired_state must be aborted or committed"
            )
        executor = self._executor(conn, self)
        executor.execute(
            """
            INSERT INTO fleet_release_generation_retirements (
                agent_id, generation, epoch_id, retired_state,
                disposition, reason, prepared_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, generation, epoch_id) DO UPDATE SET
                retired_state = excluded.retired_state,
                disposition   = excluded.disposition,
                reason        = excluded.reason,
                prepared_at   = excluded.prepared_at,
                retired_at    = excluded.retired_at
            """,
            (
                agent_value,
                generation_value,
                epoch_value,
                state_value,
                None if disposition is None else str(disposition),
                None if reason is None else str(reason),
                prepared_value,
                retired_value,
            ),
        )

    def newest_generation_retirement(
        self,
        agent_id: str,
        generation: str,
        *,
        conn: Optional[Any] = None,
    ) -> Optional[Any]:
        """Return the newest retirement fact for ``(agent_id, generation)``.

        Newest-wins is ``retired_at`` descending, then ``epoch_id`` descending
        so two facts with the same timestamp stay deterministic. Works against
        the store or an open transaction (``conn``).
        """
        agent_value = str(agent_id or "").strip()
        generation_value = str(generation or "").strip()
        if not agent_value or not generation_value:
            return None
        executor = self._executor(conn, self)
        return executor.execute(
            """
            SELECT * FROM fleet_release_generation_retirements
            WHERE agent_id = ? AND generation = ?
            ORDER BY retired_at DESC, epoch_id DESC
            LIMIT 1
            """,
            (agent_value, generation_value),
        ).fetchone()
