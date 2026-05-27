"""Workflow runtime — drives the workflow state machine.

A run snapshots its workflow's definition at start time so subsequent
edits to the parent workflow don't change in-flight behavior. The
runtime spawns a task per node, sets ``tasks.workflow_run_id`` so the
control-plane's ``transition_task`` hook can call back, and on terminal
states picks the highest-priority matching outbound edge.

The hook in ``ControlPlane.transition_task`` ignores any
``metadata.workflow_run_id`` field a caller might forge — only the
``tasks.workflow_run_id`` column (set here, never by callers) drives
the runtime callback. That's how a misbehaving agent can't smuggle
itself into the workflow state machine.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    AgentRole,
    JsonDict,
    NotFoundError,
    Task,
    TaskState,
    Tenant,
    TransitionError,
    ValidationError,
    Workflow,
    WORKFLOW_TERMINAL_STATES,
    WorkflowRun,
    WorkflowState,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService
from mac.roles_service import RolesService
from mac.workflow_service import WorkflowService

TASK_TERMINAL_TO_CONDITION: Dict[str, str] = {
    TaskState.COMPLETED.value: "success",
    TaskState.FAILED.value: "failure",
    TaskState.CANCELLED.value: "cancelled",
}


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
        get_task: Callable[[str], Task],
        record_history: Callable[..., None],
    ) -> None:
        self.store = store
        self.observability = observability
        self.workflows = workflows
        self.roles = roles
        self._create_task = create_task
        self._transition_task = transition_task
        self._get_task = get_task
        self._record_history = record_history

    # Public API --------------------------------------------------------

    def start_run(
        self,
        workflow_id_or_slug: str,
        *,
        started_by: str,
        input: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> WorkflowRun:
        workflow = self.workflows.get_workflow(workflow_id_or_slug, tenant_id=tenant_id)
        if not workflow.enabled:
            raise ValidationError("workflow %s is disabled" % workflow.slug)
        definition = dict(workflow.definition)
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
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    id, workflow_id, workflow_version, definition_snapshot,
                    state, current_node_key, current_task_id, input, context,
                    tenant_id, started_by, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, '{}', ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    workflow.id,
                    workflow.version,
                    json_dumps(definition),
                    WorkflowState.RUNNING.value,
                    json_dumps(input_obj),
                    tenant_id,
                    started_by,
                    now,
                    now,
                ),
            )
            self._record_run_history(
                conn,
                run_id,
                seq=1,
                from_key="",
                to_key=first_node["node_key"],
                condition="success",
                task_id=None,
                actor=started_by,
                attempt=1,
                detail={"phase": "start"},
            )
        task = self._spawn_node_task(
            run_id,
            first_node,
            workflow=workflow,
            started_by=started_by,
            tenant_id=tenant_id,
            attempt=1,
        )
        self.store.execute(
            """
            UPDATE workflow_runs
            SET current_node_key = ?, current_task_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (first_node["node_key"], task.id, utcnow(), run_id),
        )
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

    def tick(self, *, actor: str = "workflow_runtime.tick") -> List[WorkflowRun]:
        """Sweep runs whose current task has exceeded the node's timeout.

        Cancels the stuck task (which the on_task_completed hook then
        sees as a CANCELLED terminal state) and lets normal edge
        selection take it through whatever ``timeout`` / ``cancelled``
        edge the workflow defined. Idempotent — runs whose current task
        is already terminal are skipped.

        Phase-5 ergonomic surface. Operators drive ticks via
        ``POST /workflows/runs/tick`` (or a future worker hook).
        """
        from datetime import datetime, timezone

        rows = self.store.query_all(
            """
            SELECT id, current_node_key, current_task_id, definition_snapshot, updated_at
            FROM workflow_runs
            WHERE state = ? AND current_task_id IS NOT NULL
            """,
            (WorkflowState.RUNNING.value,),
        )
        advanced: List[WorkflowRun] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            definition = json_loads(row["definition_snapshot"], {})
            node = self._node_by_key(definition, row["current_node_key"])
            if node is None:
                continue
            timeout_min = int(node.get("timeout_minutes") or 0)
            if timeout_min <= 0:
                continue
            try:
                task = self._get_task(row["current_task_id"])
            except NotFoundError:
                continue
            if task.state in {
                TaskState.COMPLETED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
            }:
                continue
            try:
                started = datetime.fromisoformat(task.updated_at)
            except (TypeError, ValueError):
                continue
            elapsed_min = (now - started).total_seconds() / 60.0
            if elapsed_min < timeout_min:
                continue
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
                continue
            advanced.append(self.get_run(row["id"]))
        return advanced

    def cancel_run(self, run_id: str, *, reason: str, actor: str) -> WorkflowRun:
        run = self.get_run(run_id)
        if run.state in WORKFLOW_TERMINAL_STATES:
            return run
        now = utcnow()
        # First cancel the current task so the on_task_completed hook
        # doesn't bounce the run forward after we've set it cancelled.
        if run.current_task_id:
            try:
                task = self._get_task(run.current_task_id)
                if task.state not in {
                    TaskState.COMPLETED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                }:
                    self._transition_task(
                        task.id,
                        TaskState.CANCELLED.value,
                        actor,
                        {"reason": reason, "workflow_run_id": run.id},
                    )
            except (NotFoundError, TransitionError):
                pass
        self.store.execute(
            """
            UPDATE workflow_runs
            SET state = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (WorkflowState.CANCELLED.value, now, now, run.id),
        )
        next_seq = self._next_history_seq(run.id)
        self.store.execute(
            """
            INSERT INTO workflow_run_history (
                id, run_id, seq, from_node_key, to_node_key, condition,
                task_id, actor, attempt_number, detail, created_at
            ) VALUES (?, ?, ?, ?, NULL, 'cancelled', ?, ?, 1, ?, ?)
            """,
            (
                new_id("wfh"),
                run.id,
                next_seq,
                run.current_node_key,
                run.current_task_id,
                actor,
                json_dumps({"reason": reason}),
                now,
            ),
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
        condition = self._terminal_to_condition(
            terminal_state, metadata=json_loads(row["metadata"], {})
        )
        return self._advance(run, row["workflow_node_key"], condition, task_id)

    # Internals --------------------------------------------------------

    def _advance(
        self,
        run: WorkflowRun,
        from_key: Optional[str],
        condition: str,
        task_id: Optional[str],
    ) -> WorkflowRun:
        definition = run.definition_snapshot
        edge = self._pick_edge(definition, from_key, condition)
        if edge is None and condition != "success":
            # Fall back to a generic success edge when a more-specific
            # condition isn't wired.
            edge = self._pick_edge(definition, from_key, "success")
        next_seq = self._next_history_seq(run.id)
        now = utcnow()
        if edge is None or not edge.get("to_node_key"):
            # Terminal: success → COMPLETED, anything else → FAILED.
            final_state = (
                WorkflowState.COMPLETED.value
                if condition in {"success", "approved"}
                else WorkflowState.FAILED.value
            )
            self.store.execute(
                """
                UPDATE workflow_runs
                SET state = ?, current_node_key = NULL, current_task_id = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (final_state, now, now, run.id),
            )
            self.store.execute(
                """
                INSERT INTO workflow_run_history (
                    id, run_id, seq, from_node_key, to_node_key, condition,
                    task_id, actor, attempt_number, detail, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'workflow_runtime', 1, ?, ?)
                """,
                (
                    new_id("wfh"),
                    run.id,
                    next_seq,
                    from_key,
                    condition,
                    task_id,
                    json_dumps({"final_state": final_state}),
                    now,
                ),
            )
            return self.get_run(run.id)
        target = self._node_by_key(definition, edge["to_node_key"])
        if target is None:
            raise ValidationError(
                "edge points at unknown node %r" % edge.get("to_node_key")
            )
        # Enforce per-node max_attempts so a cyclic workflow cannot loop
        # forever. We count prior history rows that landed on this node_key
        # in this run; refuse to spawn if we'd exceed the declared cap.
        max_attempts = int(target.get("max_attempts", 1) or 1)
        prior = self.store.query_one(
            "SELECT COUNT(*) AS n FROM workflow_run_history WHERE run_id = ? AND to_node_key = ?",
            (run.id, target["node_key"]),
        )
        prior_attempts = int(prior["n"]) if prior else 0
        if prior_attempts >= max_attempts:
            self.store.execute(
                """
                UPDATE workflow_runs
                SET state = ?, current_node_key = NULL, current_task_id = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (WorkflowState.FAILED.value, now, now, run.id),
            )
            self.store.execute(
                """
                INSERT INTO workflow_run_history (
                    id, run_id, seq, from_node_key, to_node_key, condition,
                    task_id, actor, attempt_number, detail, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'workflow_runtime', ?, ?, ?)
                """,
                (
                    new_id("wfh"),
                    run.id,
                    next_seq,
                    from_key,
                    "max_attempts_exhausted",
                    task_id,
                    prior_attempts + 1,
                    json_dumps(
                        {
                            "node_key": target["node_key"],
                            "max_attempts": max_attempts,
                            "prior_attempts": prior_attempts,
                        }
                    ),
                    now,
                ),
            )
            return self.get_run(run.id)
        # Spawn the next task and update the run pointer atomically with
        # the history row.
        new_task = self._spawn_node_task(
            run.id,
            target,
            workflow=None,
            started_by=run.started_by,
            tenant_id=run.tenant_id,
            attempt=prior_attempts + 1,
        )
        self.store.execute(
            """
            UPDATE workflow_runs
            SET current_node_key = ?, current_task_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (target["node_key"], new_task.id, now, run.id),
        )
        self.store.execute(
            """
            INSERT INTO workflow_run_history (
                id, run_id, seq, from_node_key, to_node_key, condition,
                task_id, actor, attempt_number, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'workflow_runtime', ?, ?, ?)
            """,
            (
                new_id("wfh"),
                run.id,
                next_seq,
                from_key,
                target["node_key"],
                condition,
                new_task.id,
                prior_attempts + 1,
                json_dumps({"reason": "advance"}),
                now,
            ),
        )
        return self.get_run(run.id)

    def _spawn_node_task(
        self,
        run_id: str,
        node: Dict[str, Any],
        *,
        workflow: Optional[Workflow],
        started_by: str,
        tenant_id: Optional[str],
        attempt: int,
    ) -> Task:
        role: Optional[AgentRole]
        try:
            role = self.roles.get_role(node["role_required"], tenant_id=tenant_id)
        except NotFoundError as exc:
            raise ValidationError(
                "workflow node %s references missing role %s"
                % (node.get("node_key"), node.get("role_required"))
            ) from exc
        required_caps = sorted(
            set(role.required_capabilities)
            | set(role.default_capabilities)
            | set(node.get("extra_capabilities") or [])
        )
        metadata: Dict[str, Any] = {
            "workflow_run_id": run_id,
            "workflow_node_key": node["node_key"],
            "attempt": attempt,
            "persona_hint": node.get("persona_hint"),
            "instructions": node.get("instructions"),
            "required_role": role.slug,
        }
        if role.hardware_requirements:
            metadata["hardware"] = role.hardware_requirements
        if tenant_id is not None:
            metadata.setdefault(
                "origin", {"tenant_id": tenant_id, "type": "workflow_run"}
            )
        if node.get("node_type") == "approval":
            metadata["requires_approval"] = True
        task = self._create_task(
            "%s :: %s" % (workflow.slug if workflow else "workflow", node["node_key"]),
            description=(node.get("instructions") or "").strip(),
            project="workflow",
            required_capabilities=required_caps,
            metadata=metadata,
            actor=started_by,
        )
        # Stamp the workflow link on the row itself. This is the FK that
        # ``transition_task`` consults — caller-supplied metadata is
        # ignored, so a misbehaving agent cannot smuggle a task into the
        # workflow state machine by setting metadata.workflow_run_id.
        self.store.execute(
            """
            UPDATE tasks
            SET workflow_run_id = ?, workflow_node_key = ?, updated_at = ?
            WHERE id = ?
            """,
            (run_id, node["node_key"], utcnow(), task.id),
        )
        return self._get_task(task.id)

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
            completed_at=row["completed_at"],
        )
