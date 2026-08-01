"""Task lifecycle and dispatch services.

Implements the ledger and dispatch services that record task transitions and
emit ordered outbox side effects, using a monotonic sequence so drained events
preserve enqueue order.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from mac.allocator import (
    AllocationAgent,
    AllocationRoundResult,
    AllocationTask,
    AuthoritativeAllocator,
    ClaimCommit,
    evaluate_pair,
    evaluate_task,
)
from mac.executor_scope import compute_scope_estimate_from_lessons
from mac.models import (
    AuthorizationError,
    JsonDict,
    NotFoundError,
    Task,
    TaskState,
    TaskTransitionOutbox,
    TransitionError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.work_package_assignment import (
    WORK_PACKAGE_ASSIGNMENT_ADVISOR_VERSION,
    WorkPackageDispatchAdvisor,
    WorkPackageTaskRank,
)

# Outbox rows from a single transition share an identical ``created_at``.
# ``list_outbox`` orders by ``created_at, id``; if ``id`` is a random uuid
# the secondary sort is non-deterministic and side effects (e.g. the beads
# ledger note vs the reopen --status note) can fire out of enqueue order.
# A process-wide monotonic counter baked into the id makes the id sort in
# enqueue order, so the drain order is stable. Store-agnostic, no schema
# change. The counter resets per process, which is fine because it only
# acts as a tiebreaker within the same created_at timestamp.
_outbox_seq = itertools.count()
_outbox_seq_lock = threading.Lock()
_SNAPSHOT_UNSET = object()


def _next_outbox_seq() -> int:
    with _outbox_seq_lock:
        return next(_outbox_seq)


class TaskLedgerService:
    """Small transactional helper for task lifecycle state.

    ControlPlane still exposes the compatibility API, but task lifecycle writes
    call through this helper so state changes, history, and side-effect intents
    are staged together.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def enqueue_outbox(
        self,
        conn: Any,
        *,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> str:
        outbox_id = "tout_%016x_%s" % (_next_outbox_seq(), new_id("").lstrip("_"))
        conn.execute(
            """
            INSERT INTO task_transition_outbox (
                id, task_id, event_type, actor, from_state, to_state, detail,
                status, attempts, created_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL)
            """,
            (
                outbox_id,
                task_id,
                event_type,
                actor,
                from_state,
                to_state,
                json_dumps(detail or {}),
                created_at or utcnow(),
            ),
        )
        return outbox_id

    def list_outbox(
        self,
        *,
        status: str = "pending",
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskTransitionOutbox]:
        clauses = ["status = ?"]
        params: List[Any] = [status]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        sql = (
            "SELECT * FROM task_transition_outbox WHERE "
            + " AND ".join(clauses)
            # ``rowid`` is a SQLite implicit column; Postgres doesn't
            # expose it. Both backends have a ``id TEXT PRIMARY KEY`` —
            # use that as the secondary tiebreaker so ordering is stable
            # across stores.
            + " ORDER BY created_at, id LIMIT ?"
        )
        params.append(min(max(1, int(limit)), 1000))
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def mark_outbox_processed(self, outbox_id: str, *, status: str = "delivered") -> None:
        self.store.execute(
            """
            UPDATE task_transition_outbox
            SET status = ?, attempts = attempts + 1, processed_at = ?
            WHERE id = ?
            """,
            (status, utcnow(), outbox_id),
        )

    def mark_outbox_failed(self, outbox_id: str, error: str) -> None:
        row = self.store.query_one(
            "SELECT detail FROM task_transition_outbox WHERE id = ?",
            (outbox_id,),
        )
        detail: JsonDict = json_loads(row["detail"], {}) if row is not None else {}
        detail["last_error"] = str(error)
        self.store.execute(
            """
            UPDATE task_transition_outbox
            SET status = 'failed', attempts = attempts + 1,
                processed_at = ?, detail = ?
            WHERE id = ?
            """,
            (utcnow(), json_dumps(detail), outbox_id),
        )

    def _from_row(self, row: Any) -> TaskTransitionOutbox:
        return TaskTransitionOutbox(
            id=row["id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            detail=json_loads(row["detail"], {}),
            status=row["status"],
            attempts=int(row["attempts"]),
            created_at=row["created_at"],
            processed_at=row["processed_at"],
        )


class DispatchService:
    """Dispatch engine: push (dispatch_once) and worker-pull (claim_next).

    Holds the deterministic dispatch/claim logic that used to live on
    ControlPlane. ControlPlane keeps thin pass-throughs for backward
    compatibility and still owns the lower-level task/agent/lease helpers this
    engine calls through ``self.control_plane``.
    """

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane
        # Worker pulls are merely wake-ups for one global allocation pass.
        # Coalesce concurrent/idle polls so N workers do not each rebuild the
        # same fleet-wide task/agent snapshot.  A round that made assignments
        # is never cached: released capacity may immediately consume more work.
        self._pull_round_lock = threading.Lock()
        self._last_empty_pull_round_at = 0.0
        # Task/agent changes wake the cache explicitly where available; this
        # bounded fallback prevents a missed event from delaying work while
        # avoiding a full-backlog compatibility matrix once per second.
        self._empty_pull_round_interval_seconds = 5.0
        self._provisioning_signal_lock = threading.Lock()
        self._provisioning_signal_last_at: Dict[str, float] = {}
        self._provisioning_signal_interval_seconds = 300.0

    def dispatch_once(self, *args: Any, **kwargs: Any) -> Optional[JsonDict]:
        return self._dispatch_once_impl(*args, **kwargs)

    def claim_next_for_agent(self, *args: Any, **kwargs: Any) -> Optional[JsonDict]:
        return self._claim_next_for_agent_impl(*args, **kwargs)

    def invalidate_pull_round_cache(self) -> None:
        """Wake the next pull after a material fleet-state change."""

        with self._pull_round_lock:
            self._last_empty_pull_round_at = 0.0

    def _dispatch_once_impl(
        self,
        lease_seconds: int = 900,
        skip_tenants: Optional[Iterable[str]] = None,
        *,
        run_maintenance: bool = True,
    ) -> Optional[JsonDict]:
        assignments = self._dispatch_batch_impl(
            lease_seconds=lease_seconds,
            limit=1,
            skip_tenants=skip_tenants,
            run_maintenance=run_maintenance,
        )
        return assignments[0] if assignments else None

    def _dispatch_batch_impl(
        self,
        *,
        lease_seconds: int = 900,
        limit: int = 100,
        skip_tenants: Optional[Iterable[str]] = None,
        run_maintenance: bool = True,
    ) -> List[JsonDict]:
        limit_value = max(1, min(int(limit), 1000))
        if run_maintenance:
            self.control_plane._expire_leases_sweep_page(limit=limit_value)
            self.control_plane._unblock_ready_sweep_page(limit=limit_value)
            self.control_plane._auto_retry_blocked_attempts_sweep_page(limit=limit_value)
        result, tasks_by_id, _agents_by_id = self._allocate_v2_round(
            lease_seconds=lease_seconds,
            limit=limit_value,
            skip_tenants=skip_tenants,
            dry_run=False,
        )
        self._emit_dispatch_provisioning_signals(result, tasks_by_id)
        return [dict(item.assignment) for item in result.assignments]

    @staticmethod
    def _v2_dispatch_reason(code: str) -> JsonDict:
        return {"code": code, "message": code.replace("_", " ")}

    def _v2_snapshot_task(
        self,
        task: Task,
        *,
        projects: Mapping[str, Any],
        agent_ids_by_name: Mapping[str, List[str]],
        dependencies_satisfied_override: Optional[bool] = None,
        package_ready_override: Optional[bool] = None,
        break_glass_override: Any = _SNAPSHOT_UNSET,
        avoid_agent_ids_override: Optional[Iterable[str]] = None,
        order_signal_override: float = 0.0,
    ) -> AllocationTask:
        project_record = projects.get(task.project) if task.project else None
        project_registered = task.project is None or project_record is not None
        project_metadata = ensure_json_object(
            project_record.metadata if project_record is not None else None
        )
        project_active = bool(
            project_registered
            and (
                project_record is None
                or (
                    project_record.status == "active"
                    and not project_metadata.get("dispatch_paused")
                )
            )
        )
        metadata = ensure_json_object(task.metadata)
        break_glass = (
            self.control_plane._active_break_glass_authorization(task.id)
            if break_glass_override is _SNAPSHOT_UNSET
            else break_glass_override
        )
        if break_glass is not None:
            project_registered = True
            project_active = True
        raw_resources = metadata.get("resources") or metadata.get("required_resources") or {}
        required_resources = {
            str(key): value
            for key, value in (raw_resources.items() if isinstance(raw_resources, Mapping) else ())
            if value is not None
        }
        hardware = metadata.get("hardware")
        required_hardware = (
            {str(key): value for key, value in hardware.items()}
            if isinstance(hardware, Mapping)
            else {}
        )
        required_role = str(metadata.get("required_role") or "").strip() or None
        required_role_known = True
        required_role_capabilities: set[str] = set()
        if required_role is not None:
            try:
                role = self.control_plane.roles.get_role(required_role)
            except NotFoundError:
                required_role_known = False
            else:
                required_role_capabilities.update(role.required_capabilities)
        if dependencies_satisfied_override is None:
            try:
                dependencies_satisfied = self.control_plane._dependencies_satisfied(task)
            except (NotFoundError, TransitionError, ValidationError):
                dependencies_satisfied = False
        else:
            dependencies_satisfied = bool(dependencies_satisfied_override)
        if package_ready_override is None:
            try:
                self.control_plane._assert_work_package_claim_downstream_ready(task.id)
                package_ready = True
            except (TransitionError, ValidationError):
                package_ready = False
        else:
            package_ready = bool(package_ready_override)
        target_agent_id = (
            str(metadata["target_agent_id"]) if metadata.get("target_agent_id") else None
        )
        if target_agent_id is None and metadata.get("target_agent_name"):
            target_name = str(metadata["target_agent_name"])
            matching_ids = agent_ids_by_name.get(target_name, [])
            target_agent_id = (
                matching_ids[0]
                if len(matching_ids) == 1
                else "__unresolved_target_name__:%s" % target_name
            )
        if break_glass is not None:
            target_agent_id = break_glass.agent_id
        excluded_agent_ids: set[str] = set()
        for key in ("excluded_agent_ids", "retry_excluded_agent_ids"):
            values = metadata.get(key)
            if isinstance(values, list):
                excluded_agent_ids.update(str(value) for value in values if str(value))
        return AllocationTask(
            id=task.id,
            priority=task.priority,
            created_at=task.created_at,
            state=task.state,
            released=(
                break_glass is not None
                or not self.control_plane._task_dispatch_held(task)
            ),
            lease_id=task.lease_id,
            dependencies_satisfied=dependencies_satisfied,
            project=task.project,
            project_registered=project_registered,
            project_active=project_active,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            order_signal=float(order_signal_override),
            tenant_id=self.control_plane._task_tenant_id(task),
            target_agent_id=target_agent_id,
            break_glass_agent_id=(
                break_glass.agent_id if break_glass is not None else None
            ),
            avoid_agent_ids=frozenset(
                avoid_agent_ids_override
                if avoid_agent_ids_override is not None
                else self.control_plane._coordination_excluded_agent_ids(task)
            ),
            excluded_agent_ids=frozenset(excluded_agent_ids),
            required_capabilities=frozenset(task.required_capabilities or []),
            required_resources=required_resources,
            required_hardware=required_hardware,
            required_role=required_role,
            required_role_known=required_role_known,
            required_role_capabilities=frozenset(required_role_capabilities),
            package_ready=package_ready,
            metadata=metadata,
        )

    def _v2_snapshot_agent(self, agent: Any) -> AllocationAgent:
        try:
            machine = self.control_plane.get_machine(agent.machine_id)
        except NotFoundError:
            record = agent.to_dict()
            return AllocationAgent.from_hub_record(
                record,
                online=False,
                capacity=1,
                active_leases=0,
                machine_trusted=False,
            )
        resources = dict(machine.resources)
        resources.update(
            {
                str(key): value
                for key, value in ensure_json_object(machine.hardware).items()
                if isinstance(value, (int, float))
            }
        )
        resources.update(ensure_json_object(agent.resources))
        record = agent.to_dict()
        record["resources"] = resources
        tenant_policy = ensure_json_object(ensure_json_object(machine.labels).get("tenant_policy"))
        mode = str(tenant_policy.get("mode") or "shared")
        allowed_tenants = tenant_policy.get("tenant_ids") or tenant_policy.get("allow_tenants")
        denied_tenants = frozenset(
            str(value) for value in (tenant_policy.get("deny_tenants") or [])
        )
        if mode == "denied":
            authorized_tenants: Optional[Iterable[str]] = ()
        elif mode == "private" or allowed_tenants:
            authorized_tenants = allowed_tenants or ()
        else:
            authorized_tenants = None
        snapshot = AllocationAgent.from_hub_record(
            record,
            online=agent.status in {"idle", "busy"},
            # MacWorker owns one synchronous executor.  Advertising a larger
            # resource number must not create unrenewed queued leases.
            capacity=1,
            active_leases=self.control_plane._agent_active_lease_count(agent.id),
            machine_trusted=machine.trusted,
            authorized_tenants=authorized_tenants,
            denied_tenants=denied_tenants,
        )
        bound_role_slug = None
        bound_role_eligible = True
        bound_role_required_capabilities: set[str] = set()
        if agent.role_id is not None:
            try:
                role = self.control_plane.roles.get_role(agent.role_id)
            except NotFoundError:
                bound_role_eligible = False
            else:
                bound_role_slug = role.slug
                bound_role_required_capabilities.update(role.required_capabilities)
                hardware_ok, _reasons = self.control_plane.roles.validate_hardware(
                    role,
                    machine,
                )
                bound_role_eligible = bool(
                    hardware_ok
                    and self.control_plane.roles.soul_accepts_role(agent, role)
                )
        return replace(
            snapshot,
            hardware=ensure_json_object(machine.hardware),
            bound_role_slug=bound_role_slug,
            bound_role_eligible=bound_role_eligible,
            bound_role_required_capabilities=frozenset(
                bound_role_required_capabilities
            ),
        )

    def ready_tasks(
        self,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Task]:
        """Return tasks accepted by the exact allocator-v2 task gates."""

        projects = {record.name: record for record in self.control_plane.list_project_records()}
        agents = self.control_plane.list_agents()
        agent_ids_by_name: Dict[str, List[str]] = {}
        for agent in agents:
            agent_ids_by_name.setdefault(agent.name, []).append(agent.id)
        ready_pairs: List[Tuple[AllocationTask, Task]] = []
        for task in self.control_plane._dispatch_ordered_tasks(project=project):
            if tenant_id is not None and self.control_plane._task_tenant_id(task) != tenant_id:
                continue
            snapshot = self._v2_snapshot_task(
                task,
                projects=projects,
                agent_ids_by_name=agent_ids_by_name,
            )
            if evaluate_task(snapshot).allowed:
                ready_pairs.append((snapshot, task))
        ready_pairs.sort(
            key=lambda item: (
                -int(item[0].priority),
                str(item[0].created_at),
                item[0].id,
            )
        )
        ready = [task for _snapshot, task in ready_pairs]
        return ready[: max(0, int(limit))] if limit is not None else ready

    def explain_task_dispatch(
        self,
        task_id: Any,
        *,
        agents: Optional[Iterable[Any]] = None,
        candidate_limit: int = 60,
        record_observation: bool = False,
    ) -> JsonDict:
        """Explain dispatch using the same v2 task and pair evaluations."""

        task = self.control_plane.get_task(
            task_id.id if isinstance(task_id, Task) else str(task_id)
        )
        candidate_agents = list(agents) if agents is not None else self.control_plane.list_agents()
        agent_ids_by_name: Dict[str, List[str]] = {}
        for agent in candidate_agents:
            agent_ids_by_name.setdefault(agent.name, []).append(agent.id)
        projects = {record.name: record for record in self.control_plane.list_project_records()}
        task_snapshot = self._v2_snapshot_task(
            task,
            projects=projects,
            agent_ids_by_name=agent_ids_by_name,
        )
        task_evaluation = evaluate_task(task_snapshot)
        candidates = []
        for agent in candidate_agents:
            pair = evaluate_pair(task_snapshot, self._v2_snapshot_agent(agent))
            candidates.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "eligible": pair.allowed,
                    "reasons": [self._v2_dispatch_reason(code) for code in pair.agent_rejections],
                    "evaluation": pair.to_dict(),
                }
            )
        candidates.sort(
            key=lambda item: (
                not item["eligible"],
                item["agent_name"],
                item["agent_id"],
            )
        )
        eligible_count = sum(1 for item in candidates if item["eligible"])
        task_reasons = [self._v2_dispatch_reason(code) for code in task_evaluation.task_rejections]
        if task_reasons:
            unclaimed = list(task_reasons)
        elif not candidate_agents:
            unclaimed = [self._v2_dispatch_reason("no_agents_registered")]
        elif not eligible_count:
            unclaimed = [self._v2_dispatch_reason("no_eligible_agent")]
        else:
            unclaimed = [self._v2_dispatch_reason("awaiting_dispatch")]
        limit_value = max(1, int(candidate_limit))
        result: JsonDict = {
            "task": task.to_dict(),
            "task_ready": task_evaluation.allowed,
            "dispatchable": bool(eligible_count),
            "eligible_agent_count": eligible_count,
            "candidate_count": len(candidates),
            "candidate_limit": limit_value,
            "candidate_truncated": len(candidates) > limit_value,
            "dispatch_rank": {
                "ordering": "priority_then_age",
                "priority": int(task_snapshot.priority),
                "created_at": task_snapshot.created_at,
            },
            "task_reasons": task_reasons,
            "unclaimed_reasons": unclaimed,
            "candidates": candidates[:limit_value],
        }
        if record_observation:
            self.control_plane.record_log(
                "task.dispatch.explained",
                layer="control_plane",
                source="operator",
                subject_type="task",
                subject_id=task.id,
                detail={
                    "task_ready": result["task_ready"],
                    "dispatchable": result["dispatchable"],
                    "eligible_agent_count": eligible_count,
                    "unclaimed_reason_codes": [item["code"] for item in unclaimed],
                },
            )
        return result

    def _allocation_v2_inputs(
        self,
        *,
        skip_tenants: Optional[Iterable[str]] = None,
    ) -> Tuple[
        List[AllocationTask],
        List[AllocationAgent],
        Dict[str, Task],
        Dict[str, Any],
    ]:
        """Build the one hub-owned snapshot used by push, pull, and dry-run."""

        skipped = set(skip_tenants or [])
        tasks = [
            task
            for task in self.control_plane._dispatch_ordered_tasks()
            if (self.control_plane._task_tenant_id(task) or "") not in skipped
        ]
        task_records = {task.id: task for task in tasks}
        # Bulk dependency truth once for the whole round.  The former
        # per-task ``get_task`` loop turned an idle poll into hundreds of DB
        # round trips even when almost every task had no dependencies.
        dependency_rows = self.control_plane.store.query_all(
            """
            SELECT edge.task_id AS task_id,
                   dependency.state AS dependency_state,
                   dependency.metadata AS dependency_metadata
            FROM task_edges edge
            JOIN tasks owner ON owner.id = edge.task_id
            JOIN tasks dependency ON dependency.id = edge.dependency_task_id
            WHERE owner.state = ?
            ORDER BY edge.task_id, edge.edge_position
            """,
            (TaskState.OPEN.value,),
        )
        dependency_rows_by_task: Dict[str, List[Any]] = {}
        for row in dependency_rows:
            dependency_rows_by_task.setdefault(str(row["task_id"]), []).append(row)
        dependency_ready: Dict[str, bool] = {}
        for task in tasks:
            # Fail closed for the ONE offending task, never for the round.
            # _dependency_join_policy raises ValidationError on an unrecognised
            # metadata.dependency_policy.join, which is writable through the
            # ordinary update_task surface. The single-task snapshot path
            # already guards this (see dependencies_satisfied above); the bulk
            # path added for round performance did not, so one malformed task
            # aborted _allocation_v2_inputs and halted dispatch fleet-wide,
            # while `mac task ready` -- which takes the guarded path -- still
            # looked healthy.
            try:
                join = self.control_plane._dependency_join_policy(task)
                dependency_ready[task.id] = all(
                    self.control_plane._dependency_state_satisfies_join(
                        str(row["dependency_state"]),
                        json_loads(row["dependency_metadata"], {}),
                        join,
                    )
                    for row in dependency_rows_by_task.get(task.id, ())
                )
            except (NotFoundError, TransitionError, ValidationError):
                dependency_ready[task.id] = False

        now = utcnow()
        break_glass_by_task: Dict[str, Any] = {}
        for row in self.control_plane.store.query_all(
            "SELECT * FROM task_break_glass_authorizations "
            "WHERE status = 'active' AND expires_at > ? "
            "ORDER BY created_at DESC",
            (now,),
        ):
            break_glass_by_task.setdefault(
                str(row["task_id"]),
                self.control_plane._break_glass_authorization_from_row(row),
            )
        package_linked_ids = {
            str(row["task_id"])
            for row in self.control_plane.store.query_all(
                "SELECT task_id FROM work_package_task_links"
            )
        }
        package_ready: Dict[str, bool] = {}
        for task in tasks:
            if task.id not in package_linked_ids:
                package_ready[task.id] = True
                continue
            try:
                self.control_plane._assert_work_package_claim_downstream_ready(task.id)
                package_ready[task.id] = True
            except (TransitionError, ValidationError):
                package_ready[task.id] = False
        # Compiled work-package critical-path rank is placement advice only.
        # A broken/missing advisory must never remove otherwise runnable work
        # from the allocator snapshot.
        try:
            task_ranks = WorkPackageDispatchAdvisor(
                self.control_plane.store
            ).task_rank_snapshot(tasks)
        except Exception:  # noqa: BLE001 - advisory ranking fails open.
            task_ranks = {}
        agents = self.control_plane._available_agents()
        agent_records = {agent.id: agent for agent in agents}
        agent_ids_by_name: Dict[str, List[str]] = {}
        for agent in agents:
            agent_ids_by_name.setdefault(agent.name, []).append(agent.id)
        projects = {record.name: record for record in self.control_plane.list_project_records()}
        task_snapshots = [
            self._v2_snapshot_task(
                task,
                projects=projects,
                agent_ids_by_name=agent_ids_by_name,
                dependencies_satisfied_override=dependency_ready[task.id],
                package_ready_override=package_ready[task.id],
                break_glass_override=break_glass_by_task.get(task.id),
                order_signal_override=(
                    task_ranks[task.id].order_signal
                    if task.id in task_ranks
                    else 0.0
                ),
                # Cooperative separation is a preference, not authorization.
                # Avoid an N-per-task lease-history scan on the claim hot path.
                avoid_agent_ids_override=(),
            )
            for task in tasks
        ]
        agent_snapshots = [self._v2_snapshot_agent(agent) for agent in agents]
        return task_snapshots, agent_snapshots, task_records, agent_records

    def _allocate_v2_round(
        self,
        *,
        lease_seconds: int,
        limit: int,
        skip_tenants: Optional[Iterable[str]] = None,
        dry_run: bool,
    ) -> Tuple[AllocationRoundResult, Dict[str, Task], Dict[str, Any]]:
        task_snapshots, agent_snapshots, tasks_by_id, agents_by_id = self._allocation_v2_inputs(
            skip_tenants=skip_tenants
        )

        def claim_pair(proposal: Any) -> ClaimCommit:
            task = tasks_by_id[proposal.task_id]
            agent = agents_by_id[proposal.agent_id]
            if dry_run:
                return ClaimCommit.success(
                    {
                        "task": task.to_dict(),
                        "agent": agent.to_dict(),
                        "lease": None,
                        "dry_run": True,
                    }
                )
            try:
                claimed = self.control_plane.claim_task_v2(
                    proposal.task_id,
                    proposal.agent_id,
                    lease_seconds=lease_seconds,
                )
            except TransitionError as exc:
                return ClaimCommit.rejected(
                    "%s:%s" % (exc.__class__.__name__, str(exc))
                )
            except (AuthorizationError, ValidationError) as exc:
                return ClaimCommit.rejected(
                    "%s:%s" % (exc.__class__.__name__, str(exc)),
                    retry_with_other_agent=True,
                )
            if isinstance(claimed, Mapping):
                assignment = dict(claimed)
                claimed_task = assignment.get("task")
                lease = assignment.get("lease")
                if not isinstance(claimed_task, Mapping) or not isinstance(lease, Mapping):
                    raise ValidationError("claim_task_v2 mapping must contain task and lease")
                lease_id = str(lease["id"])
                task_id = str(claimed_task["id"])
            else:
                claimed_task, lease = claimed
                assignment = {
                    "task": self.control_plane._assignment_task_payload(claimed_task, lease),
                    "agent": agent.to_dict(),
                    "lease": lease.to_dict(),
                }
                lease_id = lease.id
                task_id = claimed_task.id
            self.control_plane._send_claim_next_nudge_best_effort(agent.id, task_id, lease_id)
            return ClaimCommit.success(assignment)

        hook = None
        if not dry_run:
            hook = getattr(self.control_plane.task_flow, "record_dispatch_round", None)
        result = AuthoritativeAllocator(on_round_complete=hook).allocate_round(
            task_snapshots,
            agent_snapshots,
            claim_pair,
            max_assignments=limit,
        )
        return result, tasks_by_id, agents_by_id

    def _record_claimed_allocation_advice(
        self,
        *,
        task: Task,
        agent_id: str,
        lease_id: str,
        score: float,
        rationale: str,
        decision: Mapping[str, Any],
        package_linked: bool,
    ) -> None:
        """Project successful advice without replacing exact package audit."""

        try:
            self.control_plane.record_log(
                "dispatcher.assignment.claimed",
                level="info",
                layer="control_plane",
                source="deterministic-dispatch",
                subject_type="task",
                subject_id=task.id,
                detail={
                    "agent_id": agent_id,
                    "task_id": task.id,
                    "lease_id": lease_id,
                    "score": score,
                    "rationale": rationale,
                    "decision": dict(decision),
                    "assignment_audit_behavior": (
                        "persisted_atomically_with_exact_lease"
                        if package_linked
                        else "routing_observation_only_for_ordinary_task"
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - telemetry cannot authorize or block work.
            pass

    def _record_unclaimed_allocation_advice(
        self,
        *,
        task: Task,
        candidate_rank: int,
        task_rank: Optional[WorkPackageTaskRank],
        available_agent_count: int,
    ) -> None:
        """Explain a no-claim decision without fabricating assignment authority.

        ``work_package_assignment_audit`` is lease-keyed by design.  When no
        hard-eligible agent reaches a successful transactional claim there is
        no exact lease, so an assignment-audit row would be false evidence.
        The durable routing observation below records that intentional absence.
        """

        task_order = (
            task_rank.to_dict()
            if task_rank is not None
            else {
                "source": "ordinary_task_fallback",
                "critical_path_rank": None,
                "order_signal": 0.0,
            }
        )
        try:
            self.control_plane.record_log(
                "dispatcher.assignment.unclaimed",
                level="info",
                layer="control_plane",
                source="deterministic-dispatch",
                subject_type="task",
                subject_id=task.id,
                detail={
                    "schema": "mac.work_package.assignment_advice.v1",
                    "allocator_version": WORK_PACKAGE_ASSIGNMENT_ADVISOR_VERSION,
                    "advisory_only": True,
                    "route": "dispatch_push",
                    "reason": "no_authoritative_claim_succeeded",
                    "task_candidate_rank": candidate_rank,
                    "task_order": task_order,
                    "available_agent_count": available_agent_count,
                    "assignment_audit_behavior": ("intentionally_absent_without_exact_lease"),
                },
            )
        except Exception:  # noqa: BLE001 - telemetry cannot authorize or block work.
            pass

    def _emit_dispatch_provisioning_signal(self, task: Task) -> None:
        from mac.services import (
            _repository_host_required_commands_from_metadata,
            _repository_required_commands_from_metadata,
        )

        required_role = None
        hardware: JsonDict = {}
        metadata = ensure_json_object(task.metadata)
        required_commands = _repository_required_commands_from_metadata(metadata)
        host_required_commands = _repository_host_required_commands_from_metadata(metadata)
        if isinstance(task.metadata, dict):
            md_role = task.metadata.get("required_role")
            if isinstance(md_role, str) and md_role.strip():
                required_role = md_role.strip()
            md_hw = task.metadata.get("hardware")
            if isinstance(md_hw, dict):
                hardware = md_hw
        self.control_plane.provisioning.request_agent(
            reason="dispatch.no_eligible_agent",
            role_slug=required_role,
            capabilities=list(task.required_capabilities or []),
            hardware=hardware,
            task_id=task.id,
            tenant_id=self.control_plane._task_tenant_id(task),
            detail={
                "task_state": task.state,
                "task_title": task.title,
                "required_commands": required_commands,
                "sandbox_host_required_commands": host_required_commands,
                "sandbox_required_commands": required_commands,
            },
        )

    def _emit_dispatch_provisioning_signals(
        self,
        result: AllocationRoundResult,
        tasks_by_id: Mapping[str, Task],
    ) -> None:
        """Emit bounded capacity demand from either push or pull dispatch.

        Worker pulls can arrive every second.  An unresolved hard-capability
        mismatch is one durable demand condition, not a new provisioning
        request on every poll, so rate-limit by task while still allowing a
        periodic refresh for long-lived demand.
        """

        unmatched_task_ids = [
            decision.task_id
            for decision in result.decisions
            if decision.status == "unmatched" and decision.task_id in tasks_by_id
        ]
        if not unmatched_task_ids:
            return
        now = time.monotonic()
        interval = max(1.0, float(self._provisioning_signal_interval_seconds))
        due: List[str] = []
        with self._provisioning_signal_lock:
            cutoff = now - interval
            self._provisioning_signal_last_at = {
                task_id: observed_at
                for task_id, observed_at in self._provisioning_signal_last_at.items()
                if observed_at >= cutoff
            }
            for task_id in unmatched_task_ids:
                last_at = self._provisioning_signal_last_at.get(task_id, 0.0)
                if last_at and now - last_at < interval:
                    continue
                self._provisioning_signal_last_at[task_id] = now
                due.append(task_id)
        for task_id in due:
            try:
                self._emit_dispatch_provisioning_signal(tasks_by_id[task_id])
            except Exception:
                # A failed request must be eligible for the next pull rather
                # than suppressed for the full throttle window.
                with self._provisioning_signal_lock:
                    if self._provisioning_signal_last_at.get(task_id) == now:
                        self._provisioning_signal_last_at.pop(task_id, None)
                continue

    def _prepare_task_dispatch_admission(self, task: Task) -> Task:
        """Persist deterministic sizing before an implementation lease exists.

        The executor historically estimated scope only after ``claim_task`` had
        incremented the attempt counter.  Large tasks therefore burned their
        first scarce worker slot discovering that they should have planned.
        Prepare ordinary repository tasks while they are still OPEN, then let
        the existing planning-mode executor consume the prepared decision.

        Work-package tasks retain their package coordinator's own admission
        authority.  Reports, child tasks, and explicitly non-decomposable work
        are also left alone.
        """

        if (
            task.state != TaskState.OPEN.value
            or task.attempt_count != 0
            or not self.control_plane._task_is_repo_coupled(task)
            or self.control_plane._task_is_work_package_linked(task.id)
        ):
            return task
        metadata = ensure_json_object(task.metadata)
        if "scope_estimate" in metadata:
            return task
        if metadata.get("no_decompose"):
            return task
        relationships = ensure_json_object(metadata.get("relationships"))
        if relationships.get("parent_task_id") or relationships.get("child_task_ids"):
            return task

        estimate = compute_scope_estimate_from_lessons(task.to_dict(), [])
        prepared = dict(metadata)
        prepared["scope_estimate"] = estimate
        if estimate.get("size") == "large":
            prepared["plan_first"] = True
        prepared["dispatch_admission"] = {
            "schema": "mac.dispatch_admission.v1",
            "prepared_at": utcnow(),
            "decision": ("plan_first" if estimate.get("size") == "large" else "execute"),
            "attempt_count": task.attempt_count,
        }
        return self.control_plane.update_task(
            task.id,
            metadata=prepared,
            actor="dispatcher.admission",
        )

    def _claim_next_for_agent_impl(
        self,
        agent_id: str,
        lease_seconds: int = 900,
        allowed_projects: Optional[Iterable[str]] = None,
        required_metadata: Optional[Dict[str, Any]] = None,
        claim_only_canary_tasks: bool = False,
        dry_run: bool = False,
        capabilities: Optional[Iterable[str]] = None,
        sync_beads: bool = True,
        allow_package_linked: bool = True,
    ) -> Optional[JsonDict]:
        """Fetch one hub-owned assignment, allocating a global round if needed.

        Worker request filters are intentionally ignored.  Project affinity is
        advertised in the agent heartbeat and consumed by allocator v2; task
        eligibility and the atomic lease are hub authority.
        """
        del (
            allowed_projects,
            required_metadata,
            claim_only_canary_tasks,
            capabilities,
            sync_beads,
            allow_package_linked,
        )
        agent = self.control_plane.get_agent(agent_id)
        if not dry_run:
            assignment = self.control_plane._active_assignment_for_agent(agent)
            if assignment is not None:
                return assignment

        if dry_run:
            result, _tasks_by_id, _agents_by_id = self._allocate_v2_round(
                lease_seconds=lease_seconds,
                limit=100,
                dry_run=True,
            )
        else:
            with self._pull_round_lock:
                # Another pull may have allocated this agent while this request
                # waited for the round leader.
                assignment = self.control_plane._active_assignment_for_agent(
                    self.control_plane.get_agent(agent_id)
                )
                if assignment is not None:
                    return assignment
                now = time.monotonic()
                if (
                    self._last_empty_pull_round_at
                    and now - self._last_empty_pull_round_at
                    < self._empty_pull_round_interval_seconds
                ):
                    return None
                # Pull is a latency-sensitive wake-up, not a maintenance
                # scheduler.  Reconciliation claims write durable lease rows
                # even when there is no work, so maintenance remains on the
                # explicit push/tick path.
                result, tasks_by_id, _agents_by_id = self._allocate_v2_round(
                    lease_seconds=lease_seconds,
                    limit=100,
                    dry_run=False,
                )
                self._emit_dispatch_provisioning_signals(result, tasks_by_id)
                self._last_empty_pull_round_at = (
                    time.monotonic() if result.assigned_count == 0 else 0.0
                )
        if dry_run:
            for allocated in result.assignments:
                if allocated.proposal.agent_id == agent_id:
                    return dict(allocated.assignment)
            return None
        return self.control_plane._active_assignment_for_agent(
            self.control_plane.get_agent(agent_id)
        )
