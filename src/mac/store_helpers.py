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

    def upsert_task_flow_span(
        self,
        *,
        span_id: str,
        task_id: str,
        project: str,
        attempt: int,
        stage: str,
        started_at: str,
        ended_at: Optional[str],
        duration_seconds: Optional[float],
        outcome: str,
        metadata_json: str = "{}",
        created_at: str,
        updated_at: str,
        conn: Optional[Any] = None,
    ) -> None:
        """Insert or update a task-flow span, keyed on (task_id, attempt, stage).

        A recompute over historical transitions calls this with the same key and
        the row is updated in place (no duplicate append). ``created_at`` is
        preserved on conflict; only mutable fields are refreshed.
        """
        executor = self._executor(conn, self)
        executor.execute(
            """
            INSERT INTO task_flow_spans (
                id, task_id, project, attempt, stage, started_at, ended_at,
                duration_seconds, outcome, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, attempt, stage) DO UPDATE SET
                project          = excluded.project,
                started_at       = excluded.started_at,
                ended_at         = excluded.ended_at,
                duration_seconds = excluded.duration_seconds,
                outcome          = excluded.outcome,
                metadata         = excluded.metadata,
                updated_at       = excluded.updated_at
            """,
            (
                span_id, task_id, project, attempt, stage, started_at,
                ended_at, duration_seconds, outcome, metadata_json,
                created_at, updated_at,
            ),
        )

    def upsert_task_completion(
        self,
        *,
        completion_id: str,
        task_id: str,
        project: str,
        attempt: int,
        started_at: str,
        ended_at: Optional[str],
        duration_seconds: Optional[float],
        outcome: str,
        publication_sha: Optional[str] = None,
        main_sha: Optional[str] = None,
        route_count: int = 0,
        token_count: int = 0,
        cost_count: float = 0.0,
        review_count: int = 0,
        rebase_count: int = 0,
        test_count: int = 0,
        per_stage_durations_json: str = "{}",
        metadata_json: str = "{}",
        created_at: str,
        updated_at: str,
        conn: Optional[Any] = None,
    ) -> None:
        """Insert or update a task-completion summary, keyed on (task_id, attempt).

        A recompute over historical task_history / reviews / publications calls
        this with the same key so the summary is updated in place rather than
        appended. ``created_at`` is preserved on conflict.
        """
        executor = self._executor(conn, self)
        executor.execute(
            """
            INSERT INTO task_completions (
                id, task_id, project, attempt, started_at, ended_at,
                duration_seconds, outcome, publication_sha, main_sha,
                route_count, token_count, cost_count, review_count,
                rebase_count, test_count, per_stage_durations, metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, attempt) DO UPDATE SET
                project             = excluded.project,
                started_at          = excluded.started_at,
                ended_at            = excluded.ended_at,
                duration_seconds    = excluded.duration_seconds,
                outcome             = excluded.outcome,
                publication_sha     = excluded.publication_sha,
                main_sha            = excluded.main_sha,
                route_count         = excluded.route_count,
                token_count         = excluded.token_count,
                cost_count          = excluded.cost_count,
                review_count        = excluded.review_count,
                rebase_count        = excluded.rebase_count,
                test_count          = excluded.test_count,
                per_stage_durations = excluded.per_stage_durations,
                metadata            = excluded.metadata,
                updated_at          = excluded.updated_at
            """,
            (
                completion_id, task_id, project, attempt, started_at,
                ended_at, duration_seconds, outcome, publication_sha, main_sha,
                route_count, token_count, cost_count, review_count,
                rebase_count, test_count, per_stage_durations_json,
                metadata_json, created_at, updated_at,
            ),
        )

    def list_task_flow_spans_by_task(
        self, task_id: str, *, attempt: Optional[int] = None
    ) -> list:
        """Return spans for a task, optionally filtered to a single attempt.

        Ordered by attempt then started_at so a caller sees stage progression.
        """
        if attempt is not None:
            return self.query_all(
                """
                SELECT * FROM task_flow_spans
                WHERE task_id = ? AND attempt = ?
                ORDER BY started_at, stage
                """,
                (task_id, attempt),
            )
        return self.query_all(
            """
            SELECT * FROM task_flow_spans
            WHERE task_id = ?
            ORDER BY attempt, started_at, stage
            """,
            (task_id,),
        )

    def list_task_flow_spans_by_project(
        self,
        project: str,
        *,
        stage: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list:
        """Return spans for a project, optionally narrowed by stage/time window."""
        clauses = ["project = ?"]
        params: list = [project]
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at < ?")
            params.append(until)
        sql = (
            "SELECT * FROM task_flow_spans WHERE "
            + " AND ".join(clauses)
            + " ORDER BY started_at, task_id, stage"
        )
        return self.query_all(sql, tuple(params))

    def get_task_completion(
        self, task_id: str, attempt: int
    ) -> Optional[Any]:
        """Return the completion summary for a task attempt, or None."""
        return self.query_one(
            "SELECT * FROM task_completions WHERE task_id = ? AND attempt = ?",
            (task_id, attempt),
        )

    def query_task_flow_stage_aggregates(
        self,
        *,
        project: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list:
        """Aggregate stage durations over a window for KPI reporting.

        Returns one row per stage with count, average, and total completed
        duration. Only closed spans (duration_seconds NOT NULL) contribute so a
        still-open stage does not skew the averages. Optionally scoped to a
        project and a [since, until) start-time window.
        """
        clauses = ["duration_seconds IS NOT NULL"]
        params: list = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at < ?")
            params.append(until)
        sql = (
            "SELECT stage, "
            "COUNT(*) AS span_count, "
            "AVG(duration_seconds) AS avg_duration_seconds, "
            "SUM(duration_seconds) AS total_duration_seconds "
            "FROM task_flow_spans WHERE "
            + " AND ".join(clauses)
            + " GROUP BY stage ORDER BY stage"
        )
        return self.query_all(sql, tuple(params))

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
