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
    NodeType,
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
    TaskState.BLOCKED.value: "failure",
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
        spawn_node: Optional[Dict[str, Any]] = first_node
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    id, workflow_id, workflow_version, definition_snapshot,
                    state, current_node_key, current_task_id, input, context,
                    tenant_id, started_by, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    workflow.id,
                    workflow.version,
                    json_dumps(definition),
                    WorkflowState.RUNNING.value,
                    json_dumps(input_obj),
                    json_dumps(context_obj),
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
            # wf-03: if the start node (or any chain of approval nodes
            # immediately following) is pre-decided, skip them in this
            # same transaction. The walk writes one history row per
            # skipped approval node and returns the first non-skipped
            # node — or None if the chain terminates the run.
            if pre_decisions:
                spawn_node = self._walk_through_pre_decided(
                    run_id,
                    definition,
                    first_node,
                    pre_decisions=pre_decisions,
                    actor=started_by,
                    conn=conn,
                )
                if spawn_node is None:
                    # Pre-decisions resolved the whole run. Mark
                    # COMPLETED and return.
                    conn.execute(
                        """
                        UPDATE workflow_runs
                        SET state = ?, current_node_key = NULL,
                            current_task_id = NULL, updated_at = ?,
                            completed_at = ?
                        WHERE id = ?
                        """,
                        (WorkflowState.COMPLETED.value, now, now, run_id),
                    )
        if spawn_node is None:
            return self.get_run(run_id)
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
        )
        self.store.execute(
            """
            UPDATE workflow_runs
            SET current_node_key = ?, current_task_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (spawn_node["node_key"], task.id, utcnow(), run_id),
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
        """Walk the workflow DAG from the just-completed task to the next.

        mac-t8ih: every state-mutating branch is wrapped in a transaction
        whose first statement is a *guarded* UPDATE that requires the run
        to still be in RUNNING state. If two terminal events race, the
        second one sees rowcount=0 on the guard and returns early — only
        one caller advances the run, no duplicate task gets spawned, no
        orphaned history row gets written.
        """
        definition = run.definition_snapshot
        edge = self._pick_edge(definition, from_key, condition)
        if edge is None and condition != "success":
            # Fall back to a generic success edge when a more-specific
            # condition isn't wired.
            edge = self._pick_edge(definition, from_key, "success")
        now = utcnow()
        if edge is None or not edge.get("to_node_key"):
            # Terminal: success → COMPLETED, anything else → FAILED.
            final_state = (
                WorkflowState.COMPLETED.value
                if condition in {"success", "approved"}
                else WorkflowState.FAILED.value
            )
            with self.store.transaction() as conn:
                cur = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET state = ?, current_node_key = NULL, current_task_id = NULL,
                        updated_at = ?, completed_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (final_state, now, now, run.id, WorkflowState.RUNNING.value),
                )
                if cur.rowcount == 0:
                    return self.get_run(run.id)
                next_seq = self._next_history_seq(run.id)
                conn.execute(
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
            with self.store.transaction() as conn:
                cur = conn.execute(
                    """
                    UPDATE workflow_runs
                    SET state = ?, current_node_key = NULL, current_task_id = NULL,
                        updated_at = ?, completed_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (WorkflowState.FAILED.value, now, now, run.id, WorkflowState.RUNNING.value),
                )
                if cur.rowcount == 0:
                    return self.get_run(run.id)
                next_seq = self._next_history_seq(run.id)
                conn.execute(
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
        pre_decisions = (
            (run.context or {}).get("pre_decisions")
            if isinstance(run.context, dict)
            else None
        )
        # wf-03: if the target is a pre-decided approval node (or the
        # start of a chain of pre-decided ones), skip past them in the
        # same transaction. Returns the first non-skipped node, or None
        # if the chain terminates the run.
        if (
            pre_decisions
            and str(target.get("node_type") or "task").strip().lower() == "approval"
            and pre_decisions.get(target["node_key"]) in {"approved", "rejected"}
        ):
            with self.store.transaction() as conn:
                spawn_target = self._walk_through_pre_decided(
                    run.id,
                    definition,
                    target,
                    pre_decisions=pre_decisions,
                    actor=run.started_by,
                    conn=conn,
                )
                if spawn_target is None:
                    # Chain ran the workflow to a terminal state.
                    conn.execute(
                        """
                        UPDATE workflow_runs
                        SET state = ?, current_node_key = NULL,
                            current_task_id = NULL, updated_at = ?,
                            completed_at = ?
                        WHERE id = ? AND state = ?
                        """,
                        (
                            WorkflowState.COMPLETED.value,
                            now,
                            now,
                            run.id,
                            WorkflowState.RUNNING.value,
                        ),
                    )
                    return self.get_run(run.id)
                target = spawn_target
        # Spawn the next task BEFORE the transaction (because create_task
        # opens its own transaction and SQLite cannot nest). If the
        # subsequent guarded UPDATE finds the run has moved on, we cancel
        # the freshly-spawned task to avoid an orphan.
        plan_payloads = (
            (run.context or {}).get("plan_payloads")
            if isinstance(run.context, dict)
            else None
        )
        new_task = self._spawn_node_task(
            run.id,
            target,
            workflow=None,
            started_by=run.started_by,
            tenant_id=run.tenant_id,
            attempt=prior_attempts + 1,
            role_snapshots=definition.get("role_snapshots") if isinstance(definition, dict) else None,
            pre_decisions=pre_decisions,
            plan_payloads=plan_payloads,
            run_input=run.input if isinstance(run.input, dict) else None,
        )
        with self.store.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE workflow_runs
                SET current_node_key = ?, current_task_id = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (target["node_key"], new_task.id, now, run.id, WorkflowState.RUNNING.value),
            )
            if cur.rowcount == 0:
                # Another _advance won the race. Cancel our spawn outside
                # the transaction (also opens its own tx).
                orphan_task_id = new_task.id
                # We can't call self.cp.transition_task from inside the
                # current tx; mark for cleanup after we exit.
                cleanup_orphan = True
            else:
                cleanup_orphan = False
                next_seq = self._next_history_seq(run.id)
                conn.execute(
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
        if cleanup_orphan:
            try:
                self.cp.transition_task(
                    orphan_task_id,
                    TaskState.CANCELLED.value,
                    "workflow_runtime",
                    {"reason": "advance_race_orphan"},
                )
            except Exception:
                pass
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
        role_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
        pre_decisions: Optional[Dict[str, str]] = None,
        plan_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        run_input: Optional[Dict[str, Any]] = None,
    ) -> Task:
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
        """Skip past any chain of pre-decided approval nodes.

        ``node`` is the current approval node the run has arrived at.
        The caller has already recorded the *entry* into ``node`` in
        history (the start-edge history row, or _advance's edge row).
        This helper writes one history row per *exit* from a skipped
        approval node — ``from = approval_key``, ``to = next_key``,
        ``condition = approved|rejected`` — then keeps walking if the
        next node is itself a pre-decided approval.

        Returns the first non-skipped node (the one a real task should
        spawn on), or ``None`` if the chain terminates the run.
        """
        current = node
        while True:
            if current is None:
                return None
            if str(current.get("node_type") or "task").strip().lower() != "approval":
                return current
            decision = (pre_decisions or {}).get(current["node_key"])
            if decision not in {"approved", "rejected"}:
                return current
            edge = self._pick_edge(definition, current["node_key"], decision)
            if edge is None and decision == "approved":
                edge = self._pick_edge(definition, current["node_key"], "success")
            target = self._node_by_key(definition, edge.get("to_node_key")) if edge else None
            next_seq = self._next_history_seq(run_id)
            conn.execute(
                """
                INSERT INTO workflow_run_history (
                    id, run_id, seq, from_node_key, to_node_key, condition,
                    task_id, actor, attempt_number, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 1, ?, ?)
                """,
                (
                    new_id("wfh"),
                    run_id,
                    next_seq,
                    current["node_key"],
                    target["node_key"] if target else None,
                    decision,
                    actor,
                    json_dumps(
                        {
                            "approval_decision": decision,
                            "pre_decision_origin": "workflow_start",
                            "skipped": True,
                        }
                    ),
                    utcnow(),
                ),
            )
            if target is None:
                return None
            current = target

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
            completed_at=row["completed_at"],
        )
