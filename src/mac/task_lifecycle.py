"""Task lifecycle and dispatch services.

Implements the ledger and dispatch services that record task transitions and
emit ordered outbox side effects, using a monotonic sequence so drained events
preserve enqueue order.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

from mac.executor_scope import compute_scope_estimate_from_lessons
from mac.models import (
    JsonDict,
    MessageType,
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

    def dispatch_once(self, *args: Any, **kwargs: Any) -> Optional[JsonDict]:
        return self._dispatch_once_impl(*args, **kwargs)

    def claim_next_for_agent(self, *args: Any, **kwargs: Any) -> Optional[JsonDict]:
        return self._claim_next_for_agent_impl(*args, **kwargs)

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
        skipped = set(skip_tenants or [])
        tasks = [
            task
            for task in self.control_plane._dispatch_ordered_tasks()
            if (self.control_plane._task_tenant_id(task) or "") not in skipped
        ]
        agents = self.control_plane._available_agents()
        assignment_advisor = WorkPackageDispatchAdvisor(self.control_plane.store)
        task_rank_snapshot = assignment_advisor.task_rank_snapshot(tasks)
        score_snapshot = assignment_advisor.score_snapshot(agents)
        unmatched: List[Task] = []
        assignments: List[JsonDict] = []
        skip_logs = 0
        for candidate_rank, task in enumerate(tasks, start=1):
            break_glass = self.control_plane._active_break_glass_authorization(task.id)
            # Autonomous-dispatch gates: a per-task no_dispatch hold or a
            # project-level pause must keep the push dispatcher from auto-
            # claiming, exactly as they keep tasks out of ready_tasks() and the
            # worker-pull claim policy. claim_task() deliberately does NOT
            # enforce these (operators may still claim/start a staged task
            # explicitly), so the gate has to live on every autonomous path.
            if self.control_plane._task_dispatch_held(task) and break_glass is None:
                if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                    self.control_plane._record_routing_skip(
                        name="dispatcher.routing.task_skipped",
                        agent_id=None,
                        task=task,
                        reason="dispatch_held",
                        route="dispatch_once",
                        candidate_rank=candidate_rank,
                        reason_class="policy",
                    )
                    skip_logs += 1
                continue
            if self.control_plane._project_dispatch_paused(task.project) and break_glass is None:
                if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                    self.control_plane._record_routing_skip(
                        name="dispatcher.routing.task_skipped",
                        agent_id=None,
                        task=task,
                        reason="project_dispatch_paused",
                        route="dispatch_once",
                        candidate_rank=candidate_rank,
                        reason_class="policy",
                    )
                    skip_logs += 1
                continue

            def _try_assign(allow_cooperative_reuse: bool) -> bool:
                nonlocal skip_logs, task
                eligible_agents = []
                for agent in agents:
                    available, reason = self.control_plane._agent_availability_for_task(
                        agent, task, allow_cooperative_reuse=allow_cooperative_reuse
                    )
                    if not available:
                        if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                            self.control_plane._record_routing_skip(
                                name="dispatcher.routing.task_skipped",
                                agent_id=agent.id,
                                task=task,
                                reason=reason,
                                route="dispatch_once",
                                candidate_rank=candidate_rank,
                                reason_class="agent_availability",
                            )
                            skip_logs += 1
                        continue
                    eligible_agents.append(agent)
                if not eligible_agents:
                    return False
                task = self._prepare_task_dispatch_admission(task)
                ranked_agents = assignment_advisor.rank_agents(
                    task=task,
                    eligible_agents=eligible_agents,
                    snapshot=score_snapshot,
                    route="dispatch_push",
                    task_rank=task_rank_snapshot.get(task.id),
                    allow_cooperative_reuse=allow_cooperative_reuse,
                )
                for advice in ranked_agents:
                    agent = advice.agent
                    assignment_decision = dict(advice.decision)
                    assignment_decision["task_candidate_rank"] = candidate_rank
                    try:
                        claimed, lease = self.control_plane.claim_task(
                            task.id,
                            agent.id,
                            lease_seconds=lease_seconds,
                            allow_cooperative_reuse=allow_cooperative_reuse,
                            assignment_allocator="deterministic-dispatch",
                            assignment_allocator_version=(
                                WORK_PACKAGE_ASSIGNMENT_ADVISOR_VERSION
                            ),
                            assignment_score=advice.score,
                            assignment_rationale=advice.rationale,
                            assignment_decision=assignment_decision,
                        )
                    except (TransitionError, ValidationError) as exc:
                        # task was already claimed, exhausted attempts, or otherwise
                        # ineligible — try the next (task, agent) pair.
                        if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                            self.control_plane._record_routing_skip(
                                name="dispatcher.routing.task_skipped",
                                agent_id=agent.id,
                                task=task,
                                reason=exc.__class__.__name__,
                                route="dispatch_once",
                                candidate_rank=candidate_rank,
                                reason_class="claim_failed",
                            )
                            skip_logs += 1
                        continue
                    score_snapshot.record_assignment(agent.id)
                    self._record_claimed_allocation_advice(
                        task=claimed,
                        agent_id=agent.id,
                        lease_id=lease.id,
                        score=advice.score,
                        rationale=advice.rationale,
                        decision=assignment_decision,
                        package_linked=task_rank_snapshot.get(task.id) is not None,
                    )
                    nudge_detail = {
                        "task_id": claimed.id,
                        "lease_id": lease.id,
                        "reason": "assigned",
                    }
                    if allow_cooperative_reuse:
                        # The distinct-executor preference was relaxed because the
                        # whole pool had already participated in this family.
                        nudge_detail["cooperative_reuse_fallback"] = True
                        self.control_plane._record_routing_skip(
                            name="dispatcher.routing.cooperative_fallback",
                            agent_id=agent.id,
                            task=task,
                            reason="cooperative_reuse_fallback",
                            route="dispatch_once",
                            candidate_rank=candidate_rank,
                            reason_class="fallback",
                        )
                    self.control_plane.send_message(
                        "dispatcher",
                        agent.id,
                        MessageType.NUDGE.value,
                        nudge_detail,
                        task_id=claimed.id,
                    )
                    assignments.append(
                        {
                            "task": self.control_plane._assignment_task_payload(claimed, lease),
                            "agent": agent.to_dict(),
                            "lease": lease.to_dict(),
                        }
                    )
                    return True
                return False

            matched = _try_assign(False)
            if not matched and self.control_plane._coordination_related_task_ids(task):
                # Every eligible agent already participated in this cooperative
                # family.  A distinct executor is preferred but not required: a
                # relaxed re-assignment beats leaving the task permanently
                # undispatchable.  Mirrors the reviewer-independence fallback.
                matched = _try_assign(True)
            if not matched:
                self._record_unclaimed_allocation_advice(
                    task=task,
                    candidate_rank=candidate_rank,
                    task_rank=task_rank_snapshot.get(task.id),
                    available_agent_count=len(agents),
                )
                unmatched.append(task)
            if len(assignments) >= limit_value:
                break
        # No agent could claim any pending task. Emit a provisioning
        # signal so a future provisioner (k8s operator, nomad job, local
        # spawner) can spin up the kind of agent that's missing. Today
        # the row + observability log are the signal; no auto-spawn.
        for task in unmatched:
            self._emit_dispatch_provisioning_signal(task)
        return assignments

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
                    "assignment_audit_behavior": (
                        "intentionally_absent_without_exact_lease"
                    ),
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
            "decision": (
                "plan_first" if estimate.get("size") == "large" else "execute"
            ),
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
        """Claim the next dispatch-eligible task for one worker.

        This is the worker-side counterpart to dispatch_once(). It preserves
        the same capability, capacity, tenant, trust, and health checks while
        allowing a worker daemon to pull only work assigned to its own durable
        identity. Worker policy filters provide a quarantine lane for canaries:
        dry runs can inspect the next eligible task without leasing it, and
        loop-mode workers can refuse non-canary or out-of-project work before
        touching production tasks.
        """
        self.control_plane._expire_leases_sweep_page(limit=100)
        self.control_plane._unblock_ready_sweep_page(limit=100)
        self.control_plane._auto_retry_blocked_attempts_sweep_page(limit=100)
        agent = self.control_plane.get_agent(agent_id)
        if not dry_run:
            assignment = self.control_plane._active_assignment_for_agent(agent)
            if assignment is not None:
                task = assignment["task"]
                lease = assignment["lease"]
                if not allow_package_linked and self.control_plane._task_is_work_package_linked(
                    str(task["id"])
                ):
                    assignment = None
                else:
                    self.control_plane._record_claim_next_log_best_effort(
                        "worker.routing.resumed",
                        agent_id=agent.id,
                        task_id=task["id"],
                        detail={
                            "agent_id": agent.id,
                            "task_id": task["id"],
                            "lease_id": lease["id"],
                            "task_state": task["state"],
                        },
                    )
                    return assignment
        policy = self.control_plane._worker_claim_policy(
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            claim_only_canary_tasks=claim_only_canary_tasks,
            dry_run=dry_run,
            capabilities=capabilities,
        )
        rejected_policy: Dict[str, int] = {}
        rejected_dispatch = 0
        considered = 0
        skip_logs = 0
        ordered_tasks = self.control_plane._dispatch_ordered_tasks()
        assignment_advisor = WorkPackageDispatchAdvisor(self.control_plane.store)
        task_rank_snapshot = assignment_advisor.task_rank_snapshot(ordered_tasks)
        score_snapshot = assignment_advisor.score_snapshot([agent])
        for task in ordered_tasks:
            considered += 1
            if not allow_package_linked and self.control_plane._task_is_work_package_linked(
                task.id
            ):
                rejected_policy["package_linked"] = (
                    rejected_policy.get("package_linked", 0) + 1
                )
                continue
            allowed, reason = self.control_plane._task_matches_worker_claim_policy(
                task, policy, agent_id=agent.id
            )
            if not allowed:
                rejected_policy[reason] = rejected_policy.get(reason, 0) + 1
                if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                    self.control_plane._record_routing_skip(
                        name="worker.routing.task_skipped",
                        agent_id=agent.id,
                        task=task,
                        reason=reason,
                        route="claim_next",
                        candidate_rank=considered,
                        reason_class="policy",
                        dry_run=dry_run,
                    )
                    skip_logs += 1
                continue
            available, reason = self.control_plane._agent_availability_for_task(agent, task)
            if not available:
                rejected_dispatch += 1
                if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                    self.control_plane._record_routing_skip(
                        name="worker.routing.task_skipped",
                        agent_id=agent.id,
                        task=task,
                        reason=reason,
                        route="claim_next",
                        candidate_rank=considered,
                        reason_class="agent_availability",
                        dry_run=dry_run,
                    )
                    skip_logs += 1
                continue
            if not dry_run:
                task = self._prepare_task_dispatch_admission(task)
            advice = assignment_advisor.rank_agents(
                task=task,
                eligible_agents=[agent],
                snapshot=score_snapshot,
                route="worker_pull",
                task_rank=task_rank_snapshot.get(task.id),
            )[0]
            detail = {
                "agent_id": agent.id,
                "task_id": task.id,
                "dry_run": dry_run,
                "policy": policy,
                "considered": considered,
                "rejected_policy": rejected_policy,
                "rejected_dispatch": rejected_dispatch,
                "assignment_score": advice.score,
                "assignment_decision": advice.decision,
                "assignment_audit_behavior": (
                    "dry_run_intentionally_absent_without_exact_lease"
                    if dry_run
                    else "persist_with_exact_lease_if_claim_succeeds"
                ),
            }
            if dry_run:
                self.control_plane.record_log(
                    "worker.routing.dry_run_candidate",
                    layer="control_plane",
                    source=agent.id,
                    subject_type="task",
                    subject_id=task.id,
                    detail=detail,
                )
                return {
                    "task": task.to_dict(),
                    "agent": agent.to_dict(),
                    "lease": None,
                    "dry_run": True,
                    "policy": policy,
                }
            try:
                claimed, lease = self.control_plane.claim_task(
                    task.id,
                    agent.id,
                    lease_seconds=lease_seconds,
                    sync_beads=sync_beads,
                    assignment_allocator="deterministic-worker-pull",
                    assignment_allocator_version=(
                        WORK_PACKAGE_ASSIGNMENT_ADVISOR_VERSION
                    ),
                    assignment_score=advice.score,
                    assignment_rationale=advice.rationale,
                    assignment_decision={
                        **advice.decision,
                        "task_candidate_rank": considered,
                    },
                )
            except (TransitionError, ValidationError) as exc:
                if skip_logs < self.control_plane._DISPATCH_SKIP_LOG_LIMIT:
                    self.control_plane._record_routing_skip(
                        name="worker.routing.task_skipped",
                        agent_id=agent.id,
                        task=task,
                        reason=exc.__class__.__name__,
                        route="claim_next",
                        candidate_rank=considered,
                        reason_class="claim_failed",
                        dry_run=dry_run,
                    )
                    skip_logs += 1
                continue
            assignment = {
                "task": self.control_plane._assignment_task_payload(claimed, lease),
                "agent": agent.to_dict(),
                "lease": lease.to_dict(),
            }
            self.control_plane._record_claim_next_log_best_effort(
                "worker.routing.claimed",
                agent_id=agent.id,
                task_id=claimed.id,
                detail={
                    **detail,
                    "lease_id": lease.id,
                    "worker_identity_fixed": True,
                    "assignment_audit_behavior": (
                        "persisted_atomically_with_exact_lease"
                        if task_rank_snapshot.get(task.id) is not None
                        else "routing_observation_only_for_ordinary_task"
                    ),
                },
            )
            self.control_plane._send_claim_next_nudge_best_effort(agent.id, claimed.id, lease.id)
            return assignment
        self.control_plane.record_log(
            "worker.routing.no_candidate",
            level="debug",
            layer="control_plane",
            source=agent.id,
            detail={
                "agent_id": agent.id,
                "dry_run": dry_run,
                "policy": policy,
                "considered": considered,
                "rejected_policy": rejected_policy,
                "rejected_dispatch": rejected_dispatch,
                "worker_identity_fixed": True,
                "assignment_audit_behavior": (
                    "intentionally_absent_without_exact_lease"
                ),
            },
        )
        return None
