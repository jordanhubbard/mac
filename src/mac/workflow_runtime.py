"""Workflow runtime — drives the workflow state machine.

A run snapshots its workflow's definition at start time so subsequent
edits to the parent workflow don't change in-flight behavior. The
runtime spawns a task per node, sets ``tasks.workflow_run_id`` so the
control-plane's ``transition_task`` hook can call back, and on terminal
or manual-repair ``blocked`` states picks the highest-priority matching
outbound edge.

The hook in ``ControlPlane.transition_task`` ignores any
``metadata.workflow_run_id`` field a caller might forge — only the
``tasks.workflow_run_id`` column (set here, never by callers) drives
the runtime callback. That's how a misbehaving agent can't smuggle
itself into the workflow state machine.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    AgentRole,
    NotFoundError,
    Task,
    TaskState,
    TransitionError,
    ValidationError,
    NodeType,
    Workflow,
    WORKFLOW_TERMINAL_STATES,
    WorkflowRun,
    WorkflowState,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)
from mac.observability_service import ObservabilityService
from mac.reconciliation import ReconciliationCoordinator
from mac.roles_service import RolesService
from mac.workflow_service import WorkflowService

TASK_TERMINAL_TO_CONDITION: Dict[str, str] = {
    TaskState.COMPLETED.value: "success",
    TaskState.FAILED.value: "failure",
    TaskState.BLOCKED.value: "failure",
    TaskState.CANCELLED.value: "cancelled",
}

_ADVANCEMENT_PREFIX = "__workflow_advancing__:"
_NO_ACTION_AT = "9999-12-31T23:59:59.999999+00:00"
_TICK_RECONCILER = "workflow-runtime-tick"


class WorkflowRuntime:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        workflows: WorkflowService,
        roles: RolesService,
        *,
        create_task: Callable[..., Task],
        transition_task: Callable[..., Task],
        transition_task_in_transaction: Optional[Callable[..., Task]] = None,
        get_task: Callable[[str], Task],
        record_history: Callable[..., None],
        drain_task_transition_outbox: Optional[Callable[..., Any]] = None,
        reconciliation: Optional[ReconciliationCoordinator] = None,
    ) -> None:
        self.store = store
        self.observability = observability
        self.workflows = workflows
        self.roles = roles
        self._create_task = create_task
        self._transition_task = transition_task
        self._transition_task_in_transaction = transition_task_in_transaction
        self._get_task = get_task
        self._record_history = record_history
        self._drain_task_transition_outbox = drain_task_transition_outbox
        self.reconciliation = reconciliation or ReconciliationCoordinator(store)
        self._backfill_next_action_at()

    # Public API --------------------------------------------------------

    def start_run(
        self,
        workflow_id_or_slug: str,
        *,
        started_by: str,
        input: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
        pre_decisions: Optional[Dict[str, str]] = None,
    ) -> WorkflowRun:
        """Start a workflow run.

        ``pre_decisions`` (wf-03) is an optional ``{node_key: "approved"|
        "rejected"}`` map. For each approval-node task spawned during
        the run, the runtime checks this map first — if present, the
        task is created with ``metadata["approval_decision"]`` already
        set and immediately transitioned to COMPLETED, so the run
        advances along the matching edge without waiting for a human.
        Pre-decisions are validated up front: every key must reference
        a real approval-typed node, every value must be one of
        ``{approved, rejected}``.
        """
        workflow = self.workflows.get_workflow(workflow_id_or_slug, tenant_id=tenant_id)
        if not workflow.enabled:
            raise ValidationError("workflow %s is disabled" % workflow.slug)
        definition = dict(workflow.definition)
        pre_decisions = self._validate_pre_decisions(definition, pre_decisions)
        # mac-hbk7: freeze role definitions at start_run so a mid-run
        # role edit can't change capability requirements or hardware
        # constraints for downstream nodes. Embed a snapshot keyed by
        # role slug into the workflow's definition_snapshot; the
        # runtime reads from this snapshot first and only falls back
        # to a live role lookup when the snapshot is missing (e.g.,
        # legacy runs created before this commit).
        role_snapshots: Dict[str, Dict[str, Any]] = {}
        for node in definition.get("nodes", []):
            slug = (node.get("role_required") or "").strip()
            if not slug or slug in role_snapshots:
                continue
            try:
                role = self.roles.get_role(slug, tenant_id=tenant_id)
            except NotFoundError:
                continue
            role_snapshots[slug] = {
                "slug": role.slug,
                "default_capabilities": list(role.default_capabilities),
                "required_capabilities": list(role.required_capabilities),
                "hardware_requirements": dict(role.hardware_requirements or {}),
            }
        if role_snapshots:
            definition["role_snapshots"] = role_snapshots
        start_edge = self._find_start_edge(definition)
        first_node = self._node_by_key(definition, start_edge["to_node_key"])
        if first_node is None:
            raise ValidationError(
                "workflow start edge points to unknown node %r"
                % start_edge.get("to_node_key")
            )
        run_id = new_id("run")
        now = utcnow()
        input_obj = ensure_json_object(input)
        # pre_decisions ride along on the run's context bag so
        # downstream _advance calls (mid-workflow approval nodes) can
        # read them without an extra schema column.
        context_obj: Dict[str, Any] = {"pre_decisions": pre_decisions} if pre_decisions else {}
        history_events: List[Dict[str, Any]] = [
            {
                "from_node_key": "",
                "to_node_key": first_node["node_key"],
                "condition": "success",
                "task_id": None,
                "actor": started_by,
                "attempt_number": 1,
                "detail": {"phase": "start"},
            }
        ]
        spawn_node, skipped_events = self._plan_pre_decided(
            definition,
            first_node,
            pre_decisions=pre_decisions,
            actor=started_by,
        )
        history_events.extend(skipped_events)
        if spawn_node is None:
            with self.store.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO workflow_runs (
                        id, workflow_id, workflow_version, definition_snapshot,
                        state, current_node_key, current_task_id, input, context,
                        tenant_id, started_by, created_at, updated_at,
                        next_action_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        run_id,
                        workflow.id,
                        workflow.version,
                        json_dumps(definition),
                        WorkflowState.COMPLETED.value,
                        json_dumps(input_obj),
                        json_dumps(context_obj),
                        tenant_id,
                        started_by,
                        now,
                        now,
                        now,
                    ),
                )
                self._write_staged_history(conn, run_id, history_events)
            return self.get_run(run_id)

        reserved_task_id = new_id("task")
        reservation_node = self._reservation_node(reserved_task_id)
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    id, workflow_id, workflow_version, definition_snapshot,
                    state, current_node_key, current_task_id, input, context,
                    tenant_id, started_by, created_at, updated_at,
                    next_action_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    workflow.id,
                    workflow.version,
                    json_dumps(definition),
                    WorkflowState.RUNNING.value,
                    reservation_node,
                    json_dumps(input_obj),
                    json_dumps(context_obj),
                    tenant_id,
                    started_by,
                    now,
                    now,
                    self._reservation_deadline(now),
                ),
            )
        task = self._spawn_node_task(
            run_id,
            spawn_node,
            workflow=workflow,
            started_by=started_by,
            tenant_id=tenant_id,
            attempt=1,
            role_snapshots=role_snapshots or None,
            pre_decisions=pre_decisions,
            run_input=input_obj,
            task_id=reserved_task_id,
        )
        finalized_at = utcnow()
        with self.store.transaction() as conn:
            finalized = conn.execute(
                """
                UPDATE workflow_runs
                SET current_node_key = ?, current_task_id = ?, updated_at = ?,
                    next_action_at = ?
                WHERE id = ? AND state = ? AND current_node_key = ?
                  AND current_task_id IS NULL
                """,
                (
                    spawn_node["node_key"],
                    task.id,
                    finalized_at,
                    self._node_deadline(spawn_node, finalized_at),
                    run_id,
                    WorkflowState.RUNNING.value,
                    reservation_node,
                ),
            )
            if finalized.rowcount == 1:
                self._write_staged_history(conn, run_id, history_events)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> WorkflowRun:
        row = self.store.query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError("workflow run not found: %s" % run_id)
        return self._run_from_row(row)

    def list_runs(
        self,
        *,
        state: Optional[str] = None,
        workflow_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[WorkflowRun]:
        clauses: List[str] = []
        params: List[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if tenant_id is not None:
            clauses.append("(tenant_id = ? OR tenant_id IS NULL)")
            params.append(tenant_id)
        sql = "SELECT * FROM workflow_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [self._run_from_row(r) for r in self.store.query_all(sql, tuple(params))]

    def tick(
        self,
        *,
        actor: str = "workflow_runtime.tick",
        limit: int = 100,
    ) -> List[WorkflowRun]:
        """Sweep a bounded set of stale reservations and timeout candidates.

        Cancels the stuck task (which the on_task_completed hook then
        sees as a CANCELLED terminal state) and lets normal edge
        selection take it through whatever ``timeout`` / ``cancelled``
        edge the workflow defined. A malformed run is logged and skipped
        without preventing later candidates in the same page from advancing.

        Phase-5 ergonomic surface. Operators drive ticks via
        ``POST /workflows/runs/tick`` (or a future worker hook).
        """
        claim = self.reconciliation.claim(_TICK_RECONCILER)
        if claim is None:
            return []
        try:
            advanced, next_cursor = self._tick_page(
                actor=actor,
                limit=limit,
                cursor=claim.cursor,
            )
        except Exception:
            self.reconciliation.abandon(claim)
            raise
        self.reconciliation.complete(claim, cursor=next_cursor)
        return advanced

    def _tick_page(
        self,
        *,
        actor: str,
        limit: int,
        cursor: Optional[str],
    ) -> tuple[List[WorkflowRun], Optional[str]]:
        now = datetime.now(timezone.utc)
        limit_value = max(1, min(int(limit), 1000))
        now_text = now.isoformat(timespec="microseconds")
        params: List[Any] = [WorkflowState.RUNNING.value, now_text]
        cursor_clause = ""
        decoded_cursor = self._decode_tick_cursor(cursor)
        if decoded_cursor is not None:
            cursor_action_at, cursor_id = decoded_cursor
            cursor_clause = (
                "AND (wr.next_action_at > ? OR "
                "(wr.next_action_at = ? AND wr.id > ?)) "
            )
            params.extend([cursor_action_at, cursor_action_at, cursor_id])
        params.append(limit_value + 1)
        rows = self.store.query_all(
            """
            SELECT wr.id, wr.current_node_key, wr.current_task_id,
                   wr.definition_snapshot, wr.updated_at, wr.next_action_at
            FROM workflow_runs AS wr
            WHERE wr.state = ?
              AND wr.next_action_at IS NOT NULL
              AND wr.next_action_at <= ?
            """
            + cursor_clause
            + """
            ORDER BY wr.next_action_at, wr.id
            LIMIT ?
            """,
            tuple(params),
        )
        has_more = len(rows) > limit_value
        rows = rows[:limit_value]
        next_cursor = (
            self._encode_tick_cursor(
                str(rows[-1]["next_action_at"]),
                str(rows[-1]["id"]),
            )
            if has_more and rows
            else None
        )
        advanced: List[WorkflowRun] = []
        for row in rows:
            try:
                recovered = self._tick_candidate(row, now=now, actor=actor)
            except Exception as exc:  # noqa: BLE001 - isolate poison workflow rows.
                try:
                    self.observability.record_log(
                        "workflow.recovery.failed",
                        layer="control_plane",
                        source=actor,
                        level="error",
                        subject_type="workflow_run",
                        subject_id=str(row["id"]),
                        detail={"run_id": str(row["id"]), "error": str(exc)},
                    )
                except Exception:
                    pass
                continue
            if recovered is not None:
                advanced.append(recovered)
        return advanced, next_cursor

    def _tick_candidate(
        self,
        row: Any,
        *,
        now: Any,
        actor: str,
    ) -> Optional[WorkflowRun]:
        reserved_task_id = self._reserved_task_id(row["current_node_key"])
        if reserved_task_id is not None:
            run = self.get_run(row["id"])
            if not self._reservation_is_stale(run):
                return None
            origin = self._reservation_origin(run)
            if origin is None:
                return None
            from_key, condition, source_task_id = origin
            recovered = self._advance(
                run,
                from_key,
                condition,
                source_task_id,
            )
            if recovered.current_node_key != row["current_node_key"]:
                return recovered
            return None
        if row["current_task_id"] is None:
            return None
        definition = json_loads(row["definition_snapshot"], {})
        node = self._node_by_key(definition, row["current_node_key"])
        if node is None:
            return None
        timeout_min = int(node.get("timeout_minutes") or 0)
        if timeout_min <= 0:
            return None
        try:
            task = self._get_task(row["current_task_id"])
        except NotFoundError:
            return None
        if task.state in {
            TaskState.COMPLETED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            return None
        deadline = parse_time(str(row["next_action_at"]))
        elapsed_min = timeout_min + max(
            0.0,
            (now - deadline).total_seconds() / 60.0,
        )
        try:
            self._transition_task(
                task.id,
                TaskState.CANCELLED.value,
                actor,
                {
                    "reason": "workflow_runtime.tick timeout",
                    "elapsed_minutes": elapsed_min,
                    "timeout_minutes": timeout_min,
                    "workflow_run_id": row["id"],
                },
            )
        except (TransitionError, ValidationError):
            return None
        return self.get_run(row["id"])

    def cancel_run(self, run_id: str, *, reason: str, actor: str) -> WorkflowRun:
        run = self.get_run(run_id)
        if run.state in WORKFLOW_TERMINAL_STATES:
            return run
        now = utcnow()
        cancelled_task_id: Optional[str] = None
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("workflow run not found: %s" % run_id)
            current = self._run_from_row(row)
            if current.state in WORKFLOW_TERMINAL_STATES:
                return current
            if current.current_task_id:
                task_row = conn.execute(
                    "SELECT state FROM tasks WHERE id = ?",
                    (current.current_task_id,),
                ).fetchone()
                if task_row is not None and task_row["state"] not in {
                    TaskState.COMPLETED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                }:
                    if self._transition_task_in_transaction is None:
                        raise TransitionError(
                            "transactional task transition is unavailable"
                        )
                    self._transition_task_in_transaction(
                        conn,
                        current.current_task_id,
                        TaskState.CANCELLED.value,
                        actor,
                        {"reason": reason, "workflow_run_id": current.id},
                    )
                    cancelled_task_id = current.current_task_id
            changed = conn.execute(
                """
                UPDATE workflow_runs
                SET state = ?, updated_at = ?, next_action_at = NULL,
                    completed_at = ?
                WHERE id = ? AND state = ?
                  AND COALESCE(current_node_key, '') = ?
                  AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                """,
                (
                    WorkflowState.CANCELLED.value,
                    now,
                    now,
                    current.id,
                    current.state,
                    current.current_node_key or "",
                    current.current_task_id or "",
                    current.updated_at,
                ),
            )
            if changed.rowcount != 1:
                raise TransitionError("workflow run changed during cancellation; retry")
            self._write_staged_history(
                conn,
                current.id,
                [
                    {
                        "from_node_key": current.current_node_key,
                        "to_node_key": None,
                        "condition": "cancelled",
                        "task_id": current.current_task_id,
                        "actor": actor,
                        "attempt_number": 1,
                        "detail": {"reason": reason},
                        "created_at": now,
                    }
                ],
            )
        if cancelled_task_id is not None:
            # The transition and cancelled run committed together. Draining the
            # durable outbox now is safe: the workflow hook observes a terminal
            # run and cannot create downstream work.
            if self._drain_task_transition_outbox is not None:
                self._drain_task_transition_outbox(
                    task_id=cancelled_task_id,
                    limit=20,
                )
        return self.get_run(run.id)

    def on_task_completed(self, task_id: str, terminal_state: str) -> Optional[WorkflowRun]:
        """Called from ``transition_task`` when a workflow-linked task
        terminates. Returns the updated run, or None if the task is not
        part of any workflow."""
        row = self.store.query_one(
            "SELECT workflow_run_id, workflow_node_key, metadata FROM tasks WHERE id = ?",
            (task_id,),
        )
        if row is None:
            return None
        run_id = row["workflow_run_id"]
        if not run_id:
            return None
        run = self.get_run(run_id)
        if run.state in WORKFLOW_TERMINAL_STATES:
            return run
        task_metadata = json_loads(row["metadata"], {})
        node_key = row["workflow_node_key"]
        condition = self._terminal_to_condition(
            terminal_state, metadata=task_metadata
        )
        # wf-04: if this was a plan-type node, harvest its evidence's
        # plan_payloads and store them on the run's context so the
        # subsequent _spawn_node_task calls can use them to parameterize
        # downstream node tasks.
        if (
            terminal_state == TaskState.COMPLETED.value
            and self._is_plan_node(run.definition_snapshot, node_key)
        ):
            self._merge_plan_payloads_from_evidence(run.id, task_id)
            run = self.get_run(run.id)
        return self._advance(run, node_key, condition, task_id)

    def _is_plan_node(self, definition: Dict[str, Any], node_key: Optional[str]) -> bool:
        if not node_key:
            return False
        for node in (definition or {}).get("nodes") or []:
            if node.get("node_key") == node_key:
                return (
                    str(node.get("node_type") or "task").strip().lower()
                    == NodeType.PLAN.value
                )
        return False

    def _merge_plan_payloads_from_evidence(self, run_id: str, task_id: str) -> None:
        """Read the most recent evidence for ``task_id`` and merge its
        ``metadata.plan_payloads`` into ``WorkflowRun.context.plan_payloads``.

        Best-effort: if the plan task's evidence isn't there or is
        malformed, the downstream nodes simply use their static
        definition (just like they did before wf-04).
        """
        row = self.store.query_one(
            "SELECT metadata FROM evidence WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id,),
        )
        if row is None:
            return
        evidence_meta = json_loads(row["metadata"], {}) or {}
        # Accept either metadata.plan_payloads (flat) or
        # metadata.verification.plan_payloads (nested) — agents may use
        # the same verification envelope as other evidence types.
        payloads = (
            evidence_meta.get("plan_payloads")
            or (evidence_meta.get("verification") or {}).get("plan_payloads")
        )
        if not isinstance(payloads, dict) or not payloads:
            return
        # Filter to dict-of-dicts (each value must be node-payload-shaped).
        clean: Dict[str, Dict[str, Any]] = {}
        for key, value in payloads.items():
            if isinstance(value, dict):
                clean[str(key)] = {
                    k: v for k, v in value.items()
                    if k in {"instructions", "metadata", "required_capabilities"}
                }
        if not clean:
            return
        run_row = self.store.query_one(
            "SELECT context FROM workflow_runs WHERE id = ?", (run_id,)
        )
        context = json_loads(run_row["context"] if run_row else None, {}) or {}
        existing = context.get("plan_payloads") or {}
        if not isinstance(existing, dict):
            existing = {}
        existing.update(clean)
        context["plan_payloads"] = existing
        self.store.execute(
            "UPDATE workflow_runs SET context = ?, updated_at = ? WHERE id = ?",
            (json_dumps(context), utcnow(), run_id),
        )

    # Internals --------------------------------------------------------

    def _advance(
        self,
        run: WorkflowRun,
        from_key: Optional[str],
        condition: str,
        task_id: Optional[str],
    ) -> WorkflowRun:
        """Advance exactly once, with a recoverable idempotent spawn reservation."""
        run = self.get_run(run.id)
        if run.state in WORKFLOW_TERMINAL_STATES:
            return run

        reserved_task_id = self._reserved_task_id(run.current_node_key)
        recovering = reserved_task_id is not None
        if task_id is not None and run.current_task_id != task_id:
            return run
        if recovering:
            if not self._reservation_is_stale(run):
                return run
            renewed_at = utcnow()
            with self.store.transaction() as conn:
                renewed = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET updated_at = ?, next_action_at = ?
                    WHERE id = ? AND state = ? AND current_node_key = ?
                      AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                    """,
                    (
                        renewed_at,
                        self._reservation_deadline(renewed_at),
                        run.id,
                        WorkflowState.RUNNING.value,
                        run.current_node_key,
                        run.current_task_id or "",
                        run.updated_at,
                    ),
                )
            if renewed.rowcount == 0:
                return self.get_run(run.id)
            run = self.get_run(run.id)

        definition = run.definition_snapshot
        edge = self._pick_edge(definition, from_key, condition)
        if edge is None and condition != "success":
            edge = self._pick_edge(definition, from_key, "success")
        now = utcnow()
        expected_node = run.current_node_key
        expected_task = run.current_task_id
        expected_updated_at = run.updated_at
        expected_next_action_at = run.next_action_at

        if edge is None or not edge.get("to_node_key"):
            final_state = (
                WorkflowState.COMPLETED.value
                if condition in {"success", "approved"}
                else WorkflowState.FAILED.value
            )
            events = [
                {
                    "from_node_key": from_key,
                    "to_node_key": None,
                    "condition": condition,
                    "task_id": task_id,
                    "actor": "workflow_runtime",
                    "attempt_number": 1,
                    "detail": {"final_state": final_state},
                }
            ]
            with self.store.transaction() as conn:
                finalized = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET state = ?, current_node_key = NULL, current_task_id = NULL,
                        updated_at = ?, next_action_at = NULL, completed_at = ?
                    WHERE id = ? AND state = ?
                      AND COALESCE(current_node_key, '') = ?
                      AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                    """,
                    (
                        final_state,
                        now,
                        now,
                        run.id,
                        WorkflowState.RUNNING.value,
                        expected_node or "",
                        expected_task or "",
                        run.updated_at,
                    ),
                )
                if finalized.rowcount == 1:
                    self._write_staged_history(conn, run.id, events)
            return self.get_run(run.id)

        initial_target = self._node_by_key(definition, edge["to_node_key"])
        if initial_target is None:
            raise ValidationError(
                "edge points at unknown node %r" % edge.get("to_node_key")
            )
        pre_decisions = (
            (run.context or {}).get("pre_decisions")
            if isinstance(run.context, dict)
            else None
        )
        target, skipped_events = self._plan_pre_decided(
            definition,
            initial_target,
            pre_decisions=pre_decisions,
            actor=run.started_by,
        )
        if target is None:
            with self.store.transaction() as conn:
                finalized = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET state = ?, current_node_key = NULL, current_task_id = NULL,
                        updated_at = ?, next_action_at = NULL, completed_at = ?
                    WHERE id = ? AND state = ?
                      AND COALESCE(current_node_key, '') = ?
                      AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                    """,
                    (
                        WorkflowState.COMPLETED.value,
                        now,
                        now,
                        run.id,
                        WorkflowState.RUNNING.value,
                        expected_node or "",
                        expected_task or "",
                        run.updated_at,
                    ),
                )
                if finalized.rowcount == 1:
                    self._write_staged_history(conn, run.id, skipped_events)
            return self.get_run(run.id)

        max_attempts = int(target.get("max_attempts", 1) or 1)
        prior = self.store.query_one(
            "SELECT COUNT(*) AS n FROM workflow_run_history WHERE run_id = ? AND to_node_key = ?",
            (run.id, target["node_key"]),
        )
        prior_attempts = int(prior["n"]) if prior else 0
        if prior_attempts >= max_attempts:
            failure_events = list(skipped_events)
            failure_events.append(
                {
                    "from_node_key": from_key,
                    "to_node_key": None,
                    "condition": "max_attempts_exhausted",
                    "task_id": task_id,
                    "actor": "workflow_runtime",
                    "attempt_number": prior_attempts + 1,
                    "detail": {
                        "node_key": target["node_key"],
                        "max_attempts": max_attempts,
                        "prior_attempts": prior_attempts,
                    },
                }
            )
            with self.store.transaction() as conn:
                finalized = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET state = ?, current_node_key = NULL, current_task_id = NULL,
                        updated_at = ?, next_action_at = NULL, completed_at = ?
                    WHERE id = ? AND state = ?
                      AND COALESCE(current_node_key, '') = ?
                      AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                    """,
                    (
                        WorkflowState.FAILED.value,
                        now,
                        now,
                        run.id,
                        WorkflowState.RUNNING.value,
                        expected_node or "",
                        expected_task or "",
                        run.updated_at,
                    ),
                )
                if finalized.rowcount == 1:
                    self._write_staged_history(conn, run.id, failure_events)
            return self.get_run(run.id)

        if from_key == "":
            history_events = [
                {
                    "from_node_key": "",
                    "to_node_key": initial_target["node_key"],
                    "condition": "success",
                    "task_id": None,
                    "actor": run.started_by,
                    "attempt_number": 1,
                    "detail": {"phase": "start"},
                },
                *skipped_events,
            ]
        else:
            history_events = [
                *skipped_events,
                {
                    "from_node_key": from_key,
                    "to_node_key": target["node_key"],
                    "condition": condition,
                    "task_id": None,
                    "actor": "workflow_runtime",
                    "attempt_number": prior_attempts + 1,
                    "detail": {"reason": "advance"},
                },
            ]

        prior_task_id = run.current_task_id
        if not recovering:
            reserved_task_id = new_id("task")
            reservation_node = self._reservation_node(reserved_task_id)
            reservation_at = utcnow()
            with self.store.transaction() as conn:
                reserved = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET current_node_key = ?, updated_at = ?, next_action_at = ?
                    WHERE id = ? AND state = ?
                      AND COALESCE(current_node_key, '') = ?
                      AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                    """,
                    (
                        reservation_node,
                        reservation_at,
                        self._reservation_deadline(reservation_at),
                        run.id,
                        WorkflowState.RUNNING.value,
                        run.current_node_key or "",
                        prior_task_id or "",
                        run.updated_at,
                    ),
                )
            if reserved.rowcount == 0:
                return self.get_run(run.id)
            run = self.get_run(run.id)
        else:
            reservation_node = run.current_node_key
            reservation_at = run.updated_at

        plan_payloads = (
            (run.context or {}).get("plan_payloads")
            if isinstance(run.context, dict)
            else None
        )
        workflow = None
        if from_key == "":
            workflow = self.workflows.get_workflow(
                run.workflow_id,
                tenant_id=run.tenant_id,
            )
        try:
            new_task = self._spawn_node_task(
                run.id,
                target,
                workflow=workflow,
                started_by=run.started_by,
                tenant_id=run.tenant_id,
                attempt=prior_attempts + 1,
                role_snapshots=definition.get("role_snapshots")
                if isinstance(definition, dict)
                else None,
                pre_decisions=pre_decisions,
                plan_payloads=plan_payloads,
                run_input=run.input if isinstance(run.input, dict) else None,
                task_id=reserved_task_id,
            )
        except Exception:
            created = self.store.query_one(
                "SELECT id FROM tasks WHERE id = ?",
                (reserved_task_id,),
            )
            if created is None:
                self.store.execute(
                    """
                    UPDATE workflow_runs
                    SET current_node_key = ?, updated_at = ?, next_action_at = ?
                    WHERE id = ? AND state = ? AND current_node_key = ?
                      AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                    """,
                    (
                        expected_node,
                        expected_updated_at,
                        expected_next_action_at,
                        run.id,
                        WorkflowState.RUNNING.value,
                        reservation_node,
                        prior_task_id or "",
                        reservation_at,
                    ),
                )
            raise
        if from_key != "" and history_events:
            history_events[-1] = {
                **history_events[-1],
                "task_id": new_task.id,
            }
        finalized_at = utcnow()
        with self.store.transaction() as conn:
            finalized = conn.execute(
                """
                UPDATE workflow_runs
                SET current_node_key = ?, current_task_id = ?, updated_at = ?,
                    next_action_at = ?
                WHERE id = ? AND state = ? AND current_node_key = ?
                  AND COALESCE(current_task_id, '') = ? AND updated_at = ?
                """,
                (
                    target["node_key"],
                    new_task.id,
                    finalized_at,
                    self._node_deadline(target, finalized_at),
                    run.id,
                    WorkflowState.RUNNING.value,
                    reservation_node,
                    prior_task_id or "",
                    reservation_at,
                ),
            )
            if finalized.rowcount == 1:
                self._write_staged_history(conn, run.id, history_events)
        current = self.get_run(run.id)
        if current.current_task_id != new_task.id:
            if new_task.state not in {
                TaskState.COMPLETED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
            }:
                self._transition_task(
                    new_task.id,
                    TaskState.CANCELLED.value,
                    "workflow_runtime",
                    {"reason": "advancement_reservation_lost"},
                )
            raise TransitionError("workflow advancement reservation was lost")
        return current

    def _spawn_node_task(
        self,
        run_id: str,
        node: Dict[str, Any],
        *,
        workflow: Optional[Workflow],
        started_by: str,
        tenant_id: Optional[str],
        attempt: int,
        role_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
        pre_decisions: Optional[Dict[str, str]] = None,
        plan_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        run_input: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        if task_id:
            existing = self.store.query_one(
                "SELECT id, workflow_run_id, workflow_node_key FROM tasks WHERE id = ?",
                (task_id,),
            )
            if existing is not None:
                if (
                    str(existing["workflow_run_id"] or "") != run_id
                    or str(existing["workflow_node_key"] or "")
                    != str(node["node_key"])
                ):
                    raise ValidationError(
                        "reserved workflow task %s belongs to another run or node"
                        % task_id
                    )
                return self._get_task(task_id)
        # wf-04: if a plan node already ran and emitted a payload for
        # this node, apply the override on a *local copy* of the node
        # dict before reading any fields. Static definition wins for
        # fields the plan didn't touch.
        plan_override_metadata: Dict[str, Any] = {}
        if plan_payloads:
            override = plan_payloads.get(node.get("node_key") or "")
            if isinstance(override, dict) and override:
                node = dict(node)
                if "instructions" in override:
                    node["instructions"] = override["instructions"]
                if "required_capabilities" in override:
                    node["required_capabilities"] = override["required_capabilities"]
                if isinstance(override.get("metadata"), dict):
                    plan_override_metadata = dict(override["metadata"])
        # mac-hbk7: prefer the role snapshot embedded in the workflow
        # definition at start_run time so mid-run role edits don't
        # change downstream capabilities or hardware constraints. Fall
        # back to the live role row only when the snapshot is missing.
        role_slug = node["role_required"]
        snapshot = (role_snapshots or {}).get(role_slug)
        if snapshot is not None:
            required_caps_set = (
                set(snapshot.get("required_capabilities") or [])
                | set(snapshot.get("default_capabilities") or [])
                | set(node.get("extra_capabilities") or [])
            )
            role_slug_value = snapshot.get("slug", role_slug)
            hardware_requirements = snapshot.get("hardware_requirements") or {}
        else:
            role: Optional[AgentRole]
            try:
                role = self.roles.get_role(role_slug, tenant_id=tenant_id)
            except NotFoundError as exc:
                raise ValidationError(
                    "workflow node %s references missing role %s"
                    % (node.get("node_key"), node.get("role_required"))
                ) from exc
            required_caps_set = (
                set(role.required_capabilities)
                | set(role.default_capabilities)
                | set(node.get("extra_capabilities") or [])
            )
            role_slug_value = role.slug
            hardware_requirements = role.hardware_requirements or {}
        required_caps = sorted(required_caps_set)
        metadata: Dict[str, Any] = {
            "workflow_run_id": run_id,
            "workflow_node_key": node["node_key"],
            "attempt": attempt,
            "persona_hint": node.get("persona_hint"),
            "instructions": node.get("instructions"),
            "required_role": role_slug_value,
        }
        if hardware_requirements:
            metadata["hardware"] = hardware_requirements
        if tenant_id is not None:
            metadata.setdefault(
                "origin", {"tenant_id": tenant_id, "type": "workflow_run"}
            )
        if node.get("node_type") == "approval":
            metadata["requires_approval"] = True
        # wf-04: a plan-typed node is just a task that produces a
        # plan_payloads evidence; surface the run's input so the
        # executor can see the free-form description it should plan
        # against.
        if str(node.get("node_type") or "").strip().lower() == NodeType.PLAN.value:
            metadata["is_plan_node"] = True
            if run_input is not None:
                metadata["plan_input"] = run_input
        # wf-04 (cont.): merge plan-payload metadata onto the task's
        # metadata so downstream executors see whatever the planner
        # resolved (preferred stack, owner, etc.). Static run metadata
        # keys take precedence so a malformed plan can't overwrite
        # things like workflow_run_id.
        for key, value in plan_override_metadata.items():
            metadata.setdefault(key, value)
        task = self._create_task(
            "%s :: %s" % (workflow.slug if workflow else "workflow", node["node_key"]),
            description=(node.get("instructions") or "").strip(),
            project="workflow",
            required_capabilities=required_caps,
            metadata=metadata,
            actor=started_by,
            _task_id=task_id,
            _workflow_run_id=run_id,
            _workflow_node_key=node["node_key"],
        )
        return task

    def _reservation_node(self, task_id: str) -> str:
        return "%s%s" % (_ADVANCEMENT_PREFIX, task_id)

    def _reserved_task_id(self, node_key: Optional[str]) -> Optional[str]:
        value = str(node_key or "")
        if not value.startswith(_ADVANCEMENT_PREFIX):
            return None
        task_id = value[len(_ADVANCEMENT_PREFIX) :].strip()
        return task_id or None

    def _reservation_is_stale(self, run: WorkflowRun) -> bool:
        stale_after = self._reservation_stale_after_seconds()
        if stale_after is None:
            return False
        try:
            age = (parse_time(utcnow()) - parse_time(run.updated_at)).total_seconds()
        except (TypeError, ValueError):
            return False
        return age >= stale_after

    def _reservation_deadline(self, activated_at: str) -> str:
        stale_after = self._reservation_stale_after_seconds()
        if stale_after is None:
            return _NO_ACTION_AT
        try:
            return (
                parse_time(activated_at) + timedelta(seconds=stale_after)
            ).isoformat(timespec="microseconds")
        except (TypeError, ValueError):
            return _NO_ACTION_AT

    def _node_deadline(self, node: Dict[str, Any], activated_at: str) -> str:
        try:
            timeout_minutes = max(0, int(node.get("timeout_minutes") or 0))
            activated = parse_time(activated_at)
        except (TypeError, ValueError):
            return _NO_ACTION_AT
        if timeout_minutes <= 0:
            return _NO_ACTION_AT
        return (activated + timedelta(minutes=timeout_minutes)).isoformat(
            timespec="microseconds"
        )

    def _backfill_next_action_at(self) -> None:
        """Populate the indexed deadline for runs created before the column.

        Each query is bounded; rows with no timeout receive a far-future
        sentinel so subsequent starts do not repeatedly revisit them.
        """
        while True:
            rows = self.store.query_all(
                """
                SELECT wr.id, wr.current_node_key, wr.definition_snapshot,
                       wr.updated_at, task.updated_at AS task_updated_at
                FROM workflow_runs AS wr
                LEFT JOIN tasks AS task ON task.id = wr.current_task_id
                WHERE wr.state = ? AND wr.next_action_at IS NULL
                ORDER BY wr.updated_at, wr.id
                LIMIT 500
                """,
                (WorkflowState.RUNNING.value,),
            )
            if not rows:
                return
            for row in rows:
                try:
                    node_key = row["current_node_key"]
                    if self._reserved_task_id(node_key) is not None:
                        deadline = self._reservation_deadline(str(row["updated_at"]))
                    else:
                        definition = json_loads(row["definition_snapshot"], {})
                        node = self._node_by_key(definition, node_key)
                        activated_at = row["task_updated_at"] or row["updated_at"]
                        deadline = (
                            self._node_deadline(node, str(activated_at))
                            if node is not None
                            else _NO_ACTION_AT
                        )
                except Exception:  # noqa: BLE001 - quarantine malformed legacy rows.
                    deadline = _NO_ACTION_AT
                self.store.execute(
                    """
                    UPDATE workflow_runs SET next_action_at = ?
                    WHERE id = ? AND next_action_at IS NULL
                    """,
                    (deadline, row["id"]),
                )
            if len(rows) < 500:
                return

    def _encode_tick_cursor(self, action_at: str, run_id: str) -> str:
        return json_dumps({"action_at": action_at, "run_id": run_id})

    def _decode_tick_cursor(
        self, cursor: Optional[str]
    ) -> Optional[tuple[str, str]]:
        if not cursor:
            return None
        try:
            payload = json_loads(cursor, {})
            action_at = str(payload["action_at"])
            run_id = str(payload["run_id"])
        except (KeyError, TypeError, ValueError):
            return None
        if not action_at or not run_id:
            return None
        return action_at, run_id

    def _reservation_stale_after_seconds(self) -> Optional[int]:
        raw = os.environ.get(
            "MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "60"
        ).strip()
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    def _reservation_origin(
        self, run: WorkflowRun
    ) -> Optional[tuple[Optional[str], str, Optional[str]]]:
        if run.current_task_id is None:
            return "", "success", None
        row = self.store.query_one(
            "SELECT state, metadata, workflow_node_key FROM tasks WHERE id = ?",
            (run.current_task_id,),
        )
        if row is None or not row["workflow_node_key"]:
            return None
        metadata = json_loads(row["metadata"], {})
        condition = self._terminal_to_condition(
            str(row["state"]), metadata=metadata
        )
        return str(row["workflow_node_key"]), condition, run.current_task_id

    def _write_staged_history(
        self,
        conn: Any,
        run_id: str,
        events: List[Dict[str, Any]],
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM workflow_run_history WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["next"]) if row is not None else 1
        for event in events:
            conn.execute(
                """
                INSERT INTO workflow_run_history (
                    id, run_id, seq, from_node_key, to_node_key, condition,
                    task_id, actor, attempt_number, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("wfh"),
                    run_id,
                    seq,
                    event.get("from_node_key"),
                    event.get("to_node_key"),
                    event.get("condition") or "success",
                    event.get("task_id"),
                    event.get("actor") or "workflow_runtime",
                    int(event.get("attempt_number") or 1),
                    json_dumps(ensure_json_object(event.get("detail"))),
                    event.get("created_at") or utcnow(),
                ),
            )
            seq += 1

    def _plan_pre_decided(
        self,
        definition: Dict[str, Any],
        node: Dict[str, Any],
        *,
        pre_decisions: Optional[Dict[str, str]],
        actor: str,
    ) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        """Plan approval skips without writing history before the spawn commits."""
        current = node
        events: List[Dict[str, Any]] = []
        while True:
            if current is None:
                return None, events
            if str(current.get("node_type") or "task").strip().lower() != "approval":
                return current, events
            decision = (pre_decisions or {}).get(current["node_key"])
            if decision not in {"approved", "rejected"}:
                return current, events
            edge = self._pick_edge(definition, current["node_key"], decision)
            if edge is None and decision == "approved":
                edge = self._pick_edge(definition, current["node_key"], "success")
            target = self._node_by_key(definition, edge.get("to_node_key")) if edge else None
            events.append(
                {
                    "from_node_key": current["node_key"],
                    "to_node_key": target["node_key"] if target else None,
                    "condition": decision,
                    "task_id": None,
                    "actor": actor,
                    "attempt_number": 1,
                    "detail": {
                        "approval_decision": decision,
                        "pre_decision_origin": "workflow_start",
                        "skipped": True,
                    },
                }
            )
            if target is None:
                return None, events
            current = target

    def _walk_through_pre_decided(
        self,
        run_id: str,
        definition: Dict[str, Any],
        node: Dict[str, Any],
        *,
        pre_decisions: Optional[Dict[str, str]],
        actor: str,
        conn: Any,
    ) -> Optional[Dict[str, Any]]:
        """Deprecated eager-history wrapper retained for API compatibility."""
        target, events = self._plan_pre_decided(
            definition,
            node,
            pre_decisions=pre_decisions,
            actor=actor,
        )
        seq = self._next_history_seq(run_id)
        for event in events:
            conn.execute(
                """
                INSERT INTO workflow_run_history (
                    id, run_id, seq, from_node_key, to_node_key, condition,
                    task_id, actor, attempt_number, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("wfh"),
                    run_id,
                    seq,
                    event.get("from_node_key"),
                    event.get("to_node_key"),
                    event.get("condition") or "success",
                    event.get("task_id"),
                    event.get("actor") or actor,
                    int(event.get("attempt_number") or 1),
                    json_dumps(ensure_json_object(event.get("detail"))),
                    utcnow(),
                ),
            )
            seq += 1
        return target

    def _validate_pre_decisions(
        self,
        definition: Dict[str, Any],
        pre_decisions: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        """Sanity-check pre_decisions against the workflow definition.

        Returns the normalized map; raises ValidationError on bad keys
        (non-existent node_key or non-approval node) or bad values.
        """
        if not pre_decisions:
            return {}
        approval_keys = {
            str(node.get("node_key"))
            for node in definition.get("nodes", []) or []
            if str(node.get("node_type") or "task").strip().lower() == "approval"
        }
        bad_keys: List[str] = []
        bad_values: List[str] = []
        normalized: Dict[str, str] = {}
        for key, value in pre_decisions.items():
            if key not in approval_keys:
                bad_keys.append(str(key))
                continue
            normalized_value = str(value or "").strip().lower()
            if normalized_value not in {"approved", "rejected"}:
                bad_values.append("%s=%s" % (key, value))
                continue
            normalized[str(key)] = normalized_value
        if bad_keys:
            raise ValidationError(
                "pre_decisions reference unknown or non-approval nodes: %s"
                % ", ".join(bad_keys)
            )
        if bad_values:
            raise ValidationError(
                "pre_decisions values must be `approved` or `rejected`: %s"
                % ", ".join(bad_values)
            )
        return normalized

    def _terminal_to_condition(self, terminal_state: str, *, metadata: Dict[str, Any]) -> str:
        if metadata.get("requires_approval"):
            decision = metadata.get("approval_decision")
            if decision == "approved":
                return "approved"
            if decision == "rejected":
                return "rejected"
        return TASK_TERMINAL_TO_CONDITION.get(terminal_state, "failure")

    def _pick_edge(
        self,
        definition: Dict[str, Any],
        from_key: Optional[str],
        condition: str,
    ) -> Optional[Dict[str, Any]]:
        edges = [
            edge
            for edge in definition.get("edges", [])
            if (edge.get("from_node_key") or "") == (from_key or "")
            and (edge.get("condition") or "success") == condition
        ]
        if not edges:
            return None
        # Higher priority wins; ties broken by definition order.
        edges.sort(key=lambda e: -int(e.get("priority") or 0))
        return edges[0]

    def _find_start_edge(self, definition: Dict[str, Any]) -> Dict[str, Any]:
        for edge in definition.get("edges", []):
            if (edge.get("from_node_key") or "") == "" and (
                edge.get("condition") or "success"
            ) == "success":
                return edge
        raise ValidationError("workflow definition missing start edge")

    def _node_by_key(self, definition: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
        for node in definition.get("nodes", []):
            if node.get("node_key") == key:
                return node
        return None

    def _next_history_seq(self, run_id: str) -> int:
        row = self.store.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM workflow_run_history WHERE run_id = ?",
            (run_id,),
        )
        return int(row["next"]) if row is not None else 1

    def _record_run_history(
        self,
        conn: Any,
        run_id: str,
        *,
        seq: int,
        from_key: Optional[str],
        to_key: Optional[str],
        condition: str,
        task_id: Optional[str],
        actor: str,
        attempt: int,
        detail: Dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO workflow_run_history (
                id, run_id, seq, from_node_key, to_node_key, condition,
                task_id, actor, attempt_number, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("wfh"),
                run_id,
                seq,
                from_key,
                to_key,
                condition,
                task_id,
                actor,
                attempt,
                json_dumps(detail),
                utcnow(),
            ),
        )

    # Row hydration -----------------------------------------------------

    def _run_from_row(self, row: Any) -> WorkflowRun:
        return WorkflowRun(
            id=row["id"],
            workflow_id=row["workflow_id"],
            workflow_version=int(row["workflow_version"]),
            definition_snapshot=json_loads(row["definition_snapshot"], {}),
            state=row["state"],
            current_node_key=row["current_node_key"],
            current_task_id=row["current_task_id"],
            input=json_loads(row["input"], {}),
            context=json_loads(row["context"], {}),
            tenant_id=row["tenant_id"],
            started_by=row["started_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            next_action_at=row["next_action_at"],
            completed_at=row["completed_at"],
        )
