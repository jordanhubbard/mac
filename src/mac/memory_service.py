"""Memory + conversation-thread + vector-ref service.

These tables are mac-side *audit seams* for cross-process integrations: mac
records the pointer ("this thread is talking to that instance about that
task", "this memory record was indexed at that point in that collection")
without holding the conversation transcript or the embedding itself. Hermes
owns the content; mac owns the operational provenance.

The conversation-thread summary is intentionally length-capped — gateways
that try to push transcript content here get rejected at the boundary.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from mac.models import (
    ConversationThread,
    Evidence,
    MemoryRecord,
    NotFoundError,
    PlatformBinding,
    Task,
    ValidationError,
    VectorRef,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)

CONVERSATION_SUMMARY_MAX_CHARS = 500


class MemoryService:
    CONVERSATION_SUMMARY_MAX_CHARS = CONVERSATION_SUMMARY_MAX_CHARS

    def __init__(
        self,
        store: Any,
        *,
        get_task: Callable[[str], Task],
        get_evidence: Callable[[str], Evidence],
        get_platform_binding: Callable[[str], PlatformBinding],
        record_history: Callable[..., None],
    ) -> None:
        self.store = store
        self._get_task = get_task
        self._get_evidence = get_evidence
        self._get_platform_binding = get_platform_binding
        self._record_history = record_history

    # Memory records ----------------------------------------------------

    def add_memory(
        self,
        task_id: Optional[str],
        subject_type: str,
        subject_id: Optional[str],
        record_type: str,
        content: str,
        evidence_id: Optional[str],
        created_by: str,
    ) -> MemoryRecord:
        if task_id is not None:
            self._get_task(task_id)
        if evidence_id is not None:
            self._get_evidence(evidence_id)
        if not subject_type or not record_type or not content:
            raise ValidationError("memory requires subject_type, record_type, and content")
        now = utcnow()
        memory_id = new_id("mem")
        self.store.execute(
            """
            INSERT INTO memory_records (
                id, task_id, subject_type, subject_id, record_type, content, evidence_id, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, task_id, subject_type, subject_id, record_type, content, evidence_id, created_by, now),
        )
        if task_id:
            self._record_history(
                task_id,
                "task.memory_recorded",
                created_by,
                None,
                None,
                {"memory_id": memory_id, "record_type": record_type},
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> MemoryRecord:
        row = self.store.query_one("SELECT * FROM memory_records WHERE id = ?", (memory_id,))
        if row is None:
            raise NotFoundError("memory record not found: %s" % memory_id)
        return self._memory_from_row(row)

    def search_memory(
        self,
        task_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[str] = None,
        record_type_prefix: Optional[str] = None,
        created_by: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
        order: str = "asc",
    ) -> List[MemoryRecord]:
        """Query memory records with operator-grade filters.

        Parameters
        ----------
        task_id:
            Exact match on the task that produced the record.
        subject_type:
            Exact match on subject_type (e.g. ``"project"``, ``"agent"``).
        subject_id:
            Exact match on subject_id.
        record_type:
            Exact match on record_type (e.g. ``"nap_summary"``).
        record_type_prefix:
            Prefix match on record_type (e.g. ``"dream:"`` matches
            ``"dream:reflection"`` and ``"dream:lesson"``).  Ignored when
            *record_type* is also set (exact match takes priority).
        created_by:
            Exact match on the creator identifier
            (e.g. ``"nap-consolidator"``, ``"agent_rocky"``).
        since:
            ISO-8601 lower bound (inclusive) on ``created_at``.
        until:
            ISO-8601 upper bound (inclusive) on ``created_at``.
        limit:
            Maximum number of records to return.  ``None`` returns all.
        order:
            ``"asc"`` (oldest first, default) or ``"desc"`` (newest first).
        """
        clauses: List[str] = []
        params: List[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if subject_type:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if record_type:
            clauses.append("record_type = ?")
            params.append(record_type)
        elif record_type_prefix:
            clauses.append("record_type LIKE ?")
            params.append(record_type_prefix.rstrip("%") + "%")
        if created_by:
            clauses.append("created_by = ?")
            params.append(created_by)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        sql = "SELECT * FROM memory_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        direction = "DESC" if (order or "asc").lower().startswith("d") else "ASC"
        sql += f" ORDER BY created_at {direction}, id {direction}"
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = self.store.query_all(sql, tuple(params))
        return [self._memory_from_row(row) for row in rows]

    # dream-04: salience-aware decay / forgetting -----------------------

    #: Curated, durable knowledge that decay must never touch. Salience here is
    #: high by construction (these are explicitly-remembered facts/lessons).
    PROTECTED_MEMORY_PREFIXES = ("beads_memory", "deployment_learning", "dream", "user", "project", "feedback")

    def decay_memory(
        self,
        *,
        ttl_days: float = 90.0,
        dry_run: bool = True,
        limit: int = 500,
        protected_prefixes: Optional[Tuple[str, ...]] = None,
    ) -> Dict[str, Any]:
        """Forget stale, low-salience memory records (dream-04).

        Salience here is a recency-and-kind heuristic: records older than
        ``ttl_days`` whose ``record_type`` is NOT curated knowledge
        (PROTECTED_MEMORY_PREFIXES) have decayed and are forgettable. This both
        delivers the dreaming system's "forgetting" + keeps the memory tier from
        growing unbounded (the observability-bloat problem).

        **Dry-run by default** — it reports what *would* be forgotten and
        deletes nothing. Pass ``dry_run=False`` to actually prune (bounded by
        ``limit``). Curated knowledge is always preserved.
        """
        from datetime import datetime, timezone, timedelta

        protected = tuple(protected_prefixes) if protected_prefixes is not None else self.PROTECTED_MEMORY_PREFIXES
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0.0, ttl_days))).isoformat(timespec="microseconds")
        rows = self.store.query_all(
            "SELECT id, record_type, created_at FROM memory_records WHERE created_at < ? ORDER BY created_at LIMIT ?",
            (cutoff, int(limit)),
        )
        forgettable = [
            r for r in rows
            if not any(str(r["record_type"]).startswith(p) for p in protected)
        ]
        by_type: Dict[str, int] = {}
        for r in forgettable:
            key = str(r["record_type"]).split(":", 1)[0]
            by_type[key] = by_type.get(key, 0) + 1
        deleted = 0
        if not dry_run and forgettable:
            for r in forgettable:
                self.store.execute("DELETE FROM memory_records WHERE id = ?", (r["id"],))
            deleted = len(forgettable)
        return {
            "schema": "mac.memory_decay.v1",
            "ttl_days": ttl_days,
            "dry_run": dry_run,
            "cutoff": cutoff,
            "scanned": len(rows),
            "forgettable": len(forgettable),
            "by_type": by_type,
            "protected_prefixes": list(protected),
            "deleted": deleted,
        }

    # Conversation threads ---------------------------------------------

    def track_conversation(
        self,
        platform_binding_id: str,
        external_thread_id: str,
        summary: str = "",
        latest_task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ConversationThread:
        binding = self._get_platform_binding(platform_binding_id)
        external_thread_id = (external_thread_id or "").strip()
        if not external_thread_id:
            raise ValidationError("external_thread_id is required")
        if summary and len(summary) > CONVERSATION_SUMMARY_MAX_CHARS:
            raise ValidationError(
                "conversation summary too long (%d > %d); store transcripts in Hermes, not mac"
                % (len(summary), CONVERSATION_SUMMARY_MAX_CHARS)
            )
        if latest_task_id is not None:
            self._get_task(latest_task_id)
        now = utcnow()
        existing = self.store.query_one(
            """
            SELECT * FROM conversation_threads
            WHERE platform_binding_id = ? AND external_thread_id = ?
            """,
            (binding.id, external_thread_id),
        )
        if existing is not None:
            new_summary = summary if summary else existing["summary"]
            existing_meta = json_loads(existing["metadata"], {})
            if metadata:
                existing_meta.update(metadata)
            new_task = latest_task_id if latest_task_id is not None else existing["latest_task_id"]
            self.store.execute(
                """
                UPDATE conversation_threads
                SET summary = ?, metadata = ?, latest_task_id = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (new_summary, json_dumps(existing_meta), new_task, now, existing["id"]),
            )
            return self.get_conversation_thread(existing["id"])
        thread_id = new_id("thread")
        self.store.execute(
            """
            INSERT INTO conversation_threads (
                id, platform_binding_id, external_thread_id, latest_task_id,
                summary, metadata, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                binding.id,
                external_thread_id,
                latest_task_id,
                summary,
                json_dumps(ensure_json_object(metadata)),
                now,
                now,
            ),
        )
        return self.get_conversation_thread(thread_id)

    def get_conversation_thread(self, thread_id: str) -> ConversationThread:
        row = self.store.query_one(
            "SELECT * FROM conversation_threads WHERE id = ?", (thread_id,)
        )
        if row is None:
            raise NotFoundError("conversation thread not found: %s" % thread_id)
        return self._thread_from_row(row)

    def list_conversation_threads(
        self,
        platform_binding_id: Optional[str] = None,
    ) -> List[ConversationThread]:
        if platform_binding_id:
            rows = self.store.query_all(
                """
                SELECT * FROM conversation_threads
                WHERE platform_binding_id = ?
                ORDER BY last_seen_at DESC, id
                """,
                (platform_binding_id,),
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM conversation_threads ORDER BY last_seen_at DESC, id"
            )
        return [self._thread_from_row(row) for row in rows]

    # Vector refs -------------------------------------------------------

    def record_vector_ref(
        self,
        memory_id: str,
        vector_db: str,
        collection: str,
        point_id: str,
        embedding_model: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by: str = "human",
    ) -> VectorRef:
        self.get_memory(memory_id)
        if not vector_db or not collection or not point_id:
            raise ValidationError("vector_db, collection, and point_id are required")
        now = utcnow()
        ref_id = new_id("vref")
        self.store.execute(
            """
            INSERT INTO vector_refs (
                id, memory_id, vector_db, collection, point_id,
                embedding_model, metadata, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref_id,
                memory_id,
                vector_db,
                collection,
                point_id,
                embedding_model,
                json_dumps(ensure_json_object(metadata)),
                created_by,
                now,
            ),
        )
        return self.get_vector_ref(ref_id)

    def get_vector_ref(self, ref_id: str) -> VectorRef:
        row = self.store.query_one("SELECT * FROM vector_refs WHERE id = ?", (ref_id,))
        if row is None:
            raise NotFoundError("vector ref not found: %s" % ref_id)
        return self._vector_ref_from_row(row)

    def list_vector_refs(
        self,
        memory_id: Optional[str] = None,
        vector_db: Optional[str] = None,
        collection: Optional[str] = None,
    ) -> List[VectorRef]:
        clauses: List[str] = []
        params: List[Any] = []
        if memory_id is not None:
            clauses.append("memory_id = ?")
            params.append(memory_id)
        if vector_db is not None:
            clauses.append("vector_db = ?")
            params.append(vector_db)
        if collection is not None:
            clauses.append("collection = ?")
            params.append(collection)
        sql = "SELECT * FROM vector_refs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [
            self._vector_ref_from_row(row) for row in self.store.query_all(sql, tuple(params))
        ]

    # Row hydration -----------------------------------------------------

    def _memory_from_row(self, row: Any) -> MemoryRecord:
        return MemoryRecord(
            row["id"],
            row["task_id"],
            row["subject_type"],
            row["subject_id"],
            row["record_type"],
            row["content"],
            row["evidence_id"],
            row["created_by"],
            row["created_at"],
        )

    def _thread_from_row(self, row: Any) -> ConversationThread:
        return ConversationThread(
            row["id"],
            row["platform_binding_id"],
            row["external_thread_id"],
            row["latest_task_id"],
            row["summary"],
            json_loads(row["metadata"], {}),
            row["first_seen_at"],
            row["last_seen_at"],
        )

    def _vector_ref_from_row(self, row: Any) -> VectorRef:
        return VectorRef(
            row["id"],
            row["memory_id"],
            row["vector_db"],
            row["collection"],
            row["point_id"],
            row["embedding_model"],
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
        )
