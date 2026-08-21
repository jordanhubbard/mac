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

    # -- Retired deploy generations --------------------------------------
    #
    # The durable half of deploy-generation admission. The controller stamps an
    # exact generation into an agent's mac.env; the worker refuses to leave
    # `draining` until a local barrier file holds that same string. Admission is
    # therefore settled by a file on the agent's own disk -- and files survive
    # the deploy that wrote them. A rollback restores an older mac.env, a
    # restored-service restart replays the unit it belongs to, and a generation
    # the controller abandoned is back on disk with a matching barrier, asking
    # for work. Until now the hub had nothing to contradict it with: the only
    # record that the generation was abandoned lived in the deploy run.
    #
    # These helpers are that record. Retirement is a fact about the past, so the
    # table is append-only (see the trigger in schema.sql) and the write below
    # is an insert that does nothing on conflict rather than an upsert: a
    # controller retrying mid-rollout re-records the same retirement without
    # needing to know whether it already got there, and no retry can overwrite
    # the reason, successor, or timestamp the first one committed.

    #: Why a generation stopped being admissible. Mirrors the CHECK constraint on
    #: deploy_generation_retirements.reason -- both, or the helper writes rows the
    #: live Postgres trigger rejects only in production.
    DEPLOY_GENERATION_RETIREMENT_REASONS = frozenset(
        {"superseded", "rolled_back", "failed", "quiesced", "decommissioned"}
    )

    @staticmethod
    def _deploy_generation_retirement_id(agent_id: str, generation: str) -> str:
        """Derive the row id from the pair it is unique on.

        Deterministic rather than random so a retried write collides on the
        primary key as well as on UNIQUE(agent_id, generation). A random id
        would leave the PK free to accept a second row if the unique index were
        ever dropped, which is exactly the invariant that must not be able to
        erode quietly.
        """
        import hashlib

        digest = hashlib.sha256(
            ("%s\x00%s" % (agent_id, generation)).encode("utf-8")
        ).hexdigest()
        return "dgr_%s" % digest[:32]

    def record_deploy_generation_retirement(
        self,
        *,
        agent_id: str,
        generation: str,
        reason: str,
        deployment_id: Optional[str] = None,
        successor_generation: Optional[str] = None,
        retired_by: Optional[str] = None,
        retired_at: Optional[str] = None,
        metadata_json: str = "{}",
        conn: Optional[Any] = None,
    ) -> bool:
        """Durably retire ``generation`` for ``agent_id``. Idempotent.

        Returns True when this call wrote the row and False when the pair was
        already retired, so a caller that wants to log or emit an event only on
        the transition can, while a caller that just needs the fact recorded can
        ignore the result and retry freely.

        ``retired_at`` defaults to now. ``conn`` runs the insert inside a
        caller's open transaction, so retiring a generation and whatever else
        the rollout step must record commit or roll back together.

        Raises ValueError on an empty identifier, an unknown reason, or a
        successor equal to the generation being retired -- the last of which
        would otherwise record the live rollout as its own replacement.
        """
        agent = str(agent_id or "").strip()
        gen = str(generation or "").strip()
        if not agent or not gen:
            raise ValueError(
                "deploy generation retirement requires agent_id and generation"
            )
        reason_value = str(reason or "").strip()
        if reason_value not in self.DEPLOY_GENERATION_RETIREMENT_REASONS:
            raise ValueError(
                "unknown deploy generation retirement reason %r; expected one of %s"
                % (reason, sorted(self.DEPLOY_GENERATION_RETIREMENT_REASONS))
            )
        successor = str(successor_generation or "").strip() or None
        if successor is not None and successor == gen:
            raise ValueError(
                "deploy generation %r cannot be its own successor" % gen
            )
        now = str(retired_at or "").strip() or _utcnow_iso()
        executor = self._executor(conn, self)
        cursor = executor.execute(
            """
            INSERT INTO deploy_generation_retirements (
                id, agent_id, generation, reason, deployment_id,
                successor_generation, retired_by, retired_at, metadata,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (agent_id, generation) DO NOTHING
            """,
            (
                self._deploy_generation_retirement_id(agent, gen),
                agent,
                gen,
                reason_value,
                str(deployment_id or "").strip() or None,
                successor,
                str(retired_by or "").strip() or None,
                now,
                metadata_json,
                now,
            ),
        )
        return cursor.rowcount > 0

    def get_deploy_generation_retirement(
        self, agent_id: str, generation: str
    ) -> Optional[Any]:
        """Return the retirement row for the pair, or None if still admissible."""
        agent = str(agent_id or "").strip()
        gen = str(generation or "").strip()
        if not agent or not gen:
            return None
        return self.query_one(
            "SELECT * FROM deploy_generation_retirements "
            "WHERE agent_id = ? AND generation = ?",
            (agent, gen),
        )

    def is_deploy_generation_retired(self, agent_id: str, generation: str) -> bool:
        """Whether ``generation`` is retired for ``agent_id``.

        The admission predicate. A blank agent or generation is False rather
        than an error: an unstamped worker has no generation to retire, and
        failing closed on the empty string would drain every agent that is not
        part of a generation-fenced rollout at all.
        """
        return self.get_deploy_generation_retirement(agent_id, generation) is not None

    def list_deploy_generation_retirements(
        self,
        *,
        agent_id: Optional[str] = None,
        deployment_id: Optional[str] = None,
        reason: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Return retirements, most recent first, optionally narrowed.

        Filters are additive: ``agent_id`` scopes to one agent, ``deployment_id``
        to the rollout that did the retiring, ``reason`` to one cause, and
        ``[since, until)`` bounds ``retired_at``.
        """
        clauses: list = []
        params: list = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if deployment_id is not None:
            clauses.append("deployment_id = ?")
            params.append(deployment_id)
        if reason is not None:
            clauses.append("reason = ?")
            params.append(reason)
        if since is not None:
            clauses.append("retired_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("retired_at < ?")
            params.append(until)
        sql = "SELECT * FROM deploy_generation_retirements"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY retired_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.query_all(sql, tuple(params))
