"""Task-transition engine extracted from ControlPlane.

Block 2 of the ControlPlane decomposition (see docs/audit.md sections 5.1 and
8-P3).  This service owns the deterministic task state-machine writes and the
task-transition outbox drain that used to live directly on ``ControlPlane``:

* the core ``_transition_task_impl`` engine that validates a transition,
  applies the guarded UPDATE, records history, and stages ordered outbox side
  effects in a single transaction;
* the terminal-dependency reconciliation helpers
  (``_terminal_dependency_replacement`` /
  ``_resolve_waiting_dependents_of``); and
* the transition-outbox drain surface
  (``list_task_transition_outbox`` / ``drain_task_transition_outbox`` /
  ``drain_task_transition_outbox_best_effort`` /
  ``_process_task_transition_outbox_item``).

``ControlPlane`` keeps thin pass-throughs for backward compatibility and still
owns the lower-level task/agent/lease helpers, the outbox drain lock, and the
in-memory failure counter this engine reads through ``self.control_plane``.
This is a behaviour-preserving extraction: the moved code calls back into
ControlPlane via the same public method names, so intra-engine calls continue
to flow through the compatibility pass-throughs exactly as before.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mac.models import (
    AuthorizationError,
    Evidence,
    JsonDict,
    LeaseStatus,
    NotFoundError,
    Task,
    TaskState,
    TaskTransitionOutbox,
    TERMINAL_TASK_STATES,
    TransitionError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    normalize_needs_input_detail,
    utcnow,
    validate_transition,
)
from mac.repository_hygiene import (
    normalize_cancellation_detail,
    repository_ref_lifecycle_for_transition,
    validate_replacement_target,
)


def _state_value(state: Any) -> str:
    from mac.services import _state_value as _impl

    return _impl(state)


def _normalize_blocked_detail(detail: Optional[Dict[str, Any]]) -> JsonDict:
    from mac.services import _normalize_blocked_detail as _impl

    return _impl(detail)


def _failure_diagnosis(target_state: str, detail: Optional[Dict[str, Any]]) -> Optional[str]:
    from mac.services import _failure_diagnosis as _impl

    return _impl(target_state, detail)


def _structured_failure_diagnosis(
    target_state: str,
    detail: Optional[Dict[str, Any]],
    *,
    actor: str,
    attempt_count: int,
    resolve_output_tail: Optional[Any] = None,
) -> Optional[JsonDict]:
    from mac.services import _structured_failure_diagnosis as _impl

    return _impl(
        target_state,
        detail,
        actor=actor,
        attempt_count=attempt_count,
        resolve_output_tail=resolve_output_tail,
    )


def _retry_generation(metadata: JsonDict) -> int:
    try:
        return max(0, int(metadata.get("retry_generation") or 0))
    except (TypeError, ValueError):
        return 0


class TaskTransitionService:
    """Task-transition engine: state writes and transition-outbox drain.

    Holds the deterministic transition/reconciliation/outbox logic that used to
    live on ``ControlPlane``.  ControlPlane keeps thin pass-throughs for
    backward compatibility and still owns the lower-level task/agent/lease
    helpers, the outbox drain lock, and the failure counter this engine calls
    through ``self.control_plane``.
    """

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane


    def _transition_task_impl(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        lease_id: Optional[str],
        trusted_internal: bool,
        drain_outbox: bool,
        conn: Optional[Any],
    ) -> Task:
        target = _state_value(target_state)
        if conn is None:
            task = self.control_plane.get_task(task_id)
        else:
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                raise NotFoundError("task not found: %s" % task_id)
            task = self.control_plane._task_from_row(task_row)
        # ``get_task`` accepts unambiguous display prefixes.  Every write and
        # related-record lookup below must use the canonical id it resolved;
        # otherwise the initial read succeeds but the UPDATE/history/outbox
        # writes target the non-existent prefix.
        task_id = task.id
        package_link = (
            conn.execute(
                "SELECT package_id FROM work_package_task_links WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if conn is not None
            else self.control_plane.store.query_one(
                "SELECT package_id FROM work_package_task_links WHERE task_id = ?",
                (task_id,),
            )
        )
        if package_link is not None and target not in {
            TaskState.RUNNING.value,
            TaskState.NEEDS_REVIEW.value,
        }:
            self.control_plane._require_non_package_task_mutation(
                task_id,
                operation="generic transition to %s" % target,
                conn=conn,
            )
        # Worker-authored lifecycle writes are fenced to the exact active lease
        # attempt. Without this check, an old process for lease A can mutate a
        # task after the same agent has reacquired it as lease B (the classic
        # ABA failure). It also prevents any bound agent from using the generic
        # endpoint to mutate an unowned OPEN/review/terminal task. Operators,
        # dispatchers, and reviewers use explicit trusted service paths.
        fenced_lease_id: Optional[str] = None
        if not trusted_internal:
            if task.state not in {
                TaskState.CLAIMED.value,
                TaskState.RUNNING.value,
            }:
                raise AuthorizationError(
                    "worker task transitions require ownership of an active lease"
                )
            self.control_plane._require_exact_lease_actor(task, actor, lease_id)
            fenced_lease_id = str(lease_id or "").strip()
        transition_detail = dict(detail or {})
        current_retry_generation = _retry_generation(
            ensure_json_object(task.metadata)
        )
        if target == TaskState.BLOCKED.value:
            # The hub owns the retry epoch. A worker cannot evade repeated
            # failure accounting by supplying an arbitrary generation.
            transition_detail["retry_generation"] = current_retry_generation
            transition_detail = _normalize_blocked_detail(transition_detail)
        if target == TaskState.NEEDS_INPUT.value:
            # Parking work on a human question REQUIRES stating the question.
            # Enforced at the single chokepoint every transition passes
            # through, so no caller can park a task on an unstated blocker.
            transition_detail = normalize_needs_input_detail(transition_detail)
        diagnosis_record = _structured_failure_diagnosis(
            target,
            transition_detail,
            actor=actor,
            attempt_count=task.attempt_count,
            # A failure whose detail omitted stdout/stderr is still
            # diagnosable: the worker uploaded both as evidence artifacts.
            resolve_output_tail=lambda: self.control_plane._evidence_output_tail(
                task.id,
                evidence_id=str(transition_detail.get("evidence_id") or "") or None,
            ),
        )
        if diagnosis_record is not None:
            existing_diagnosis = transition_detail.get("diagnosis")
            if existing_diagnosis not in (None, "", {}, diagnosis_record):
                diagnosis_record["reported_diagnosis"] = str(existing_diagnosis)[:1000]
            transition_detail["diagnosis"] = diagnosis_record
        if target == TaskState.CANCELLED.value:
            # Resolve replacement_task_id prefix before normalization so that
            # normalize_cancellation_detail receives a canonical full ID.
            # AmbiguousIdError and NotFoundError propagate without state change.
            _raw_replacement = str(
                (dict(transition_detail) if transition_detail else {}).get(
                    "replacement_task_id"
                ) or ""
            ).strip()
            if _raw_replacement:
                _resolved_replacement = self.control_plane._resolve_task_id(_raw_replacement)
                if _resolved_replacement != _raw_replacement:
                    transition_detail = dict(transition_detail)
                    transition_detail["replacement_task_id"] = _resolved_replacement
            transition_detail = normalize_cancellation_detail(transition_detail)
            # Write guard: reject cancellations that point at a terminal or held
            # replacement task unless the caller has explicitly set archival_override.
            disposition = str(transition_detail.get("disposition") or "").strip().lower()
            if disposition in {"superseded", "duplicate"}:
                replacement_id = str(
                    transition_detail.get("replacement_task_id") or ""
                ).strip()
                archival_override = bool(transition_detail.get("archival_override", False))
                validate_replacement_target(
                    replacement_id,
                    self.control_plane.get_task,
                    archival_override=archival_override,
                )
                if archival_override:
                    # Record archival_override in lifecycle metadata for audit.
                    transition_detail["archival_override_recorded"] = True
        detail = transition_detail
        if task.state == target:
            # A terminal cancellation may be re-submitted solely to backfill or
            # correct its repository-ref disposition. Keep the original
            # terminal timestamp so this cannot reset the grace period.
            if target != TaskState.CANCELLED.value:
                return task
            lifecycle = repository_ref_lifecycle_for_transition(
                target,
                detail,
                now=task.completed_at or utcnow(),
            )
            metadata = ensure_json_object(task.metadata)
            if metadata.get("repository_ref_lifecycle") == lifecycle:
                if drain_outbox and conn is None:
                    self.control_plane.drain_task_transition_outbox(task_id=task_id, limit=20)
                return task
            metadata["repository_ref_lifecycle"] = lifecycle
            now = utcnow()

            def apply_lifecycle(transaction: Any) -> None:
                transaction.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json_dumps(metadata), now, task_id),
                )
                self.control_plane._record_history(
                    task_id,
                    "repository_ref.lifecycle_updated",
                    actor,
                    target,
                    target,
                    detail,
                    conn=transaction,
                )
                self.control_plane.task_ledger.enqueue_outbox(
                    transaction,
                    task_id=task_id,
                    event_type="task.lifecycle",
                    actor=actor,
                    from_state=target,
                    to_state=target,
                    detail=detail,
                    created_at=now,
                )
            if conn is None:
                with self.control_plane.store.transaction() as transaction:
                    apply_lifecycle(transaction)
                if drain_outbox:
                    self.control_plane.drain_task_transition_outbox(task_id=task_id, limit=20)
                return self.control_plane.get_task(task_id)
            apply_lifecycle(conn)
            transitioned_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if transitioned_row is None:
                raise NotFoundError("task not found: %s" % task_id)
            return self.control_plane._task_from_row(transitioned_row)
        validate_transition(task.state, target)
        review_ready_evidence: Optional[Evidence] = None
        if target == TaskState.NEEDS_REVIEW.value:
            review_ready_evidence = self.control_plane._require_review_ready(task)
        if target == TaskState.COMPLETED.value and not self.control_plane.reviews.completion_authorized(task_id):
            raise ValidationError("task completion requires approved review and evidence")
        if target == TaskState.COMPLETED.value:
            self.control_plane._require_canonical_integration_proof(task)
        now = utcnow()
        updated_metadata: Optional[JsonDict] = None
        candidate_metadata = ensure_json_object(task.metadata)
        metadata_changed = False
        is_operator_reopen = (
            target == TaskState.OPEN.value
            and str(detail.get("via") or "").strip() == "operator_reopen"
        )
        if is_operator_reopen:
            next_retry_generation = current_retry_generation + 1
            detail["retry_generation"] = next_retry_generation
            candidate_metadata["retry_generation"] = next_retry_generation
            for key in (
                "retry_excluded_agent_ids",
                "retry_failure_fingerprint",
                "retry_failure_kind",
                # A dependency_resolution record describes ONE unsatisfied
                # prerequisite episode. Left behind across a reopen it makes
                # _dependency_state_satisfies_join count this task as
                # "settled" the next time it blocks -- for any reason at all,
                # including a plain executor failure the spec says must hold
                # the integration parent. The episode ended when the task was
                # reopened; the record must end with it.
                "dependency_resolution",
            ):
                candidate_metadata.pop(key, None)
            metadata_changed = True
        if diagnosis_record is not None:
            diagnosis_summary = _failure_diagnosis(target, detail) or diagnosis_record["problem"]
            activity = candidate_metadata.get("activity")
            if not isinstance(activity, list):
                activity = []
            activity.append(
                {
                    "phase": "diagnosis",
                    "actor": str(actor or "")[:120],
                    "summary": str(diagnosis_summary)[:1200],
                    "detail": diagnosis_record,
                    "at": utcnow(),
                }
            )
            candidate_metadata["activity"] = activity[-24:]
            metadata_changed = True
        if target == TaskState.NEEDS_INPUT.value:
            candidate_metadata["needs_input"] = {
                "schema": transition_detail["schema"],
                "questions": transition_detail["questions"],
                "asked_by": str(actor or "")[:120],
                "asked_at": now,
                "from_state": task.state,
            }
            metadata_changed = True
        elif task.state == TaskState.NEEDS_INPUT.value:
            # Leaving the state: fold the outstanding questions into history so
            # the answer stays auditable next to what was asked, then clear it.
            outstanding = ensure_json_object(candidate_metadata.get("needs_input"))
            if outstanding:
                answered = dict(outstanding)
                answered["answered_by"] = str(actor or "")[:120]
                answered["answered_at"] = now
                answered["answer"] = str(
                    (transition_detail or {}).get("answer")
                    or (transition_detail or {}).get("reason")
                    or ""
                )[:2000]
                answered["resolved_to"] = target
                history = candidate_metadata.get("needs_input_history")
                if not isinstance(history, list):
                    history = []
                candidate_metadata["needs_input_history"] = (history + [answered])[-20:]
                candidate_metadata.pop("needs_input", None)
                metadata_changed = True
        if review_ready_evidence is not None:
            candidate_metadata["review_target"] = {
                "executor_evidence_id": review_ready_evidence.id,
                "attempt_count": task.attempt_count,
                "recorded_at": now,
            }
            metadata_changed = True
        elif target in {
            TaskState.OPEN.value,
            TaskState.WAITING.value,
            TaskState.BLOCKED.value,
            TaskState.RUNNING.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            if candidate_metadata.pop("review_target", None) is not None:
                metadata_changed = True
        repository_ref_lifecycle = repository_ref_lifecycle_for_transition(
            target,
            detail,
            now=now,
        )
        if repository_ref_lifecycle is not None:
            if candidate_metadata.get("repository_ref_lifecycle") != repository_ref_lifecycle:
                candidate_metadata["repository_ref_lifecycle"] = repository_ref_lifecycle
                metadata_changed = True
        if metadata_changed:
            updated_metadata = candidate_metadata
        owner_agent_id = task.owner_agent_id
        lease_id = task.lease_id
        leased_until = task.leased_until
        release_lease_id = None
        if target in {
            TaskState.WAITING.value,
            TaskState.BLOCKED.value,
            TaskState.OPEN.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            release_lease_id = lease_id
            owner_agent_id = None
            lease_id = None
            leased_until = None
        # mac-d2xh: a dead-letter requeue (FAILED→OPEN or CANCELLED→OPEN)
        # must reset attempt_count and clear completed_at; otherwise the
        # next claim immediately fails the cap check (attempt_count >=
        # max_attempts) and the requeue is a no-op.
        is_requeue_from_terminal = (
            task.state in {TaskState.FAILED.value, TaskState.CANCELLED.value}
            and target == TaskState.OPEN.value
        )

        def apply_transition(conn: Any) -> None:
            if fenced_lease_id:
                self.control_plane._require_exact_lease_actor_in_transaction(
                    conn,
                    task_id=task_id,
                    agent_id=actor,
                    lease_id=fenced_lease_id,
                    allowed_states=(task.state,),
                )
            if release_lease_id:
                conn.execute(
                    "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (LeaseStatus.RELEASED.value, now, release_lease_id, LeaseStatus.ACTIVE.value),
                )
                self.control_plane._consume_break_glass_authorizations(
                    conn,
                    task_id=task_id,
                    lease_id=release_lease_id,
                    now=now,
                )
            if is_requeue_from_terminal:
                transition_where = "WHERE id = ? AND state = ?"
                transition_guards: List[Any] = [task_id, task.state]
                if fenced_lease_id:
                    transition_where += " AND lease_id = ?"
                    transition_guards.append(fenced_lease_id)
                changed = conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                        started_at = NULL, completed_at = NULL,
                        attempt_count = 0, updated_at = ?
                    """ + transition_where,
                    tuple(
                        [
                        target,
                        owner_agent_id,
                        lease_id,
                        leased_until,
                        now,
                        ]
                        + transition_guards
                    ),
                )
            else:
                transition_where = "WHERE id = ? AND state = ?"
                transition_guards = [task_id, task.state]
                if fenced_lease_id:
                    transition_where += " AND lease_id = ?"
                    transition_guards.append(fenced_lease_id)
                changed = conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?, owner_agent_id = ?, lease_id = ?, leased_until = ?,
                        started_at = ?, completed_at = ?, updated_at = ?
                    """ + transition_where,
                    tuple(
                        [
                        target,
                        owner_agent_id,
                        lease_id,
                        leased_until,
                        now if target == TaskState.RUNNING.value and not task.started_at else task.started_at,
                        now if target in TERMINAL_TASK_STATES and not task.completed_at else task.completed_at,
                        now,
                        ]
                        + transition_guards
                    ),
                )
            if changed.rowcount != 1:
                raise TransitionError("task state changed during transition; retry")
            if updated_metadata is not None:
                conn.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json_dumps(updated_metadata), now, task_id),
                )
            if task.owner_agent_id and target in TERMINAL_TASK_STATES.union(
                {
                    TaskState.WAITING.value,
                    TaskState.BLOCKED.value,
                    TaskState.OPEN.value,
                    TaskState.NEEDS_REVIEW.value,
                }
            ):
                self.control_plane._set_agent_idle(task.owner_agent_id, conn=conn)
            self.control_plane._record_history(
                task_id, "task.transitioned", actor, task.state, target, detail or {}, conn=conn
            )
            self.control_plane.task_ledger.enqueue_outbox(
                conn,
                task_id=task_id,
                event_type="task.lifecycle",
                actor=actor,
                from_state=task.state,
                to_state=target,
                detail=detail or {},
                created_at=now,
            )
            if target in TERMINAL_TASK_STATES.union({TaskState.BLOCKED.value}):
                row = conn.execute(
                    "SELECT workflow_run_id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is not None and row["workflow_run_id"]:
                    self.control_plane.task_ledger.enqueue_outbox(
                        conn,
                        task_id=task_id,
                        event_type="workflow.advance",
                        actor=actor,
                        from_state=task.state,
                        to_state=target,
                        detail=detail or {},
                        created_at=now,
                    )
        if conn is None:
            with self.control_plane.store.transaction() as transaction:
                apply_transition(transaction)
        else:
            apply_transition(conn)
            if target in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
                # The caller owns this transaction, so dependency
                # reconciliation cannot run here: it opens transactions of its
                # own and must not observe a terminal state that has not
                # committed yet. The conn-is-None path calls
                # _resolve_waiting_dependents_of directly after commit; this
                # path had no equivalent and simply skipped it, so every
                # in-transaction cancel left its waiting dependents with no
                # dependency_resolution record, no BLOCKED transition and no
                # observation -- silently unrunnable forever. workflow_runtime
                # cancels exactly this way. Hand the work to the outbox, which
                # already exists to run post-commit side effects in order.
                self.control_plane.task_ledger.enqueue_outbox(
                    conn,
                    task_id=task_id,
                    event_type="dependency.reconcile",
                    actor=actor,
                    from_state=task.state,
                    to_state=target,
                    detail=detail or {},
                    created_at=now,
                )
            transitioned_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if transitioned_row is None:
                raise NotFoundError("task not found: %s" % task_id)
            return self.control_plane._task_from_row(transitioned_row)
        if drain_outbox:
            self.control_plane.drain_task_transition_outbox(task_id=task_id, limit=20)
        transitioned = self.control_plane.get_task(task_id)
        if target in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
            self.control_plane._resolve_waiting_dependents_of(task_id, target, actor)
        return transitioned

    def _terminal_dependency_replacement(self, dep_id: str) -> Optional[str]:
        """Return a live replacement recorded on a terminal prerequisite.

        Cancellation normalisation already validates a superseding replacement
        at write time. Re-check its current liveness here because a replacement
        may have become terminal between the prerequisite transition and this
        dependent reconciliation.
        """
        try:
            dependency = self.control_plane.get_task(dep_id)
        except NotFoundError:
            return None
        lifecycle = ensure_json_object(dependency.metadata).get(
            "repository_ref_lifecycle"
        )
        replacement_id = str(
            ensure_json_object(lifecycle).get("replacement_task_id") or ""
        ).strip()
        if not replacement_id:
            return None
        try:
            replacement = self.control_plane.get_task(replacement_id)
        except NotFoundError:
            return None
        if replacement.state in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
            return None
        if bool(ensure_json_object(replacement.metadata).get("no_dispatch")):
            return None
        return replacement.id

    def _resolve_waiting_dependents_of(self, dep_id: str, dep_state: str, actor: str) -> None:
        """Reconcile waiting dependents of a terminal prerequisite.

        A dependency edge orders work; it does not implicitly grant permission
        to cancel an entire task family.  A durable replacement is substituted
        when one exists.  Otherwise the dependent is supervised: cooperative
        integration parents stay waiting so their ``all_settled`` join can
        inspect the failed outcome, while ordinary/downstream tasks become a
        non-terminal dependency block.  Only an explicit ``cancel_scope``
        policy retains the old all-for-one cascade.

        This remains best-effort: a reconciliation error must never invalidate
        the triggering terminal transition.
        """
        replacement_id = self.control_plane._terminal_dependency_replacement(dep_id)
        try:
            rows = self.control_plane.store.query_all(
                "SELECT id FROM tasks WHERE state = ? AND dependencies LIKE ?",
                (TaskState.WAITING.value, "%" + dep_id + "%"),
            )
        except Exception:  # noqa: BLE001 - propagation must not break the transition
            return
        for row in rows or []:
            dependent_id = row["id"]
            try:
                dependent = self.control_plane.get_task(dependent_id)
            except NotFoundError:
                continue
            # LIKE is a substring match — confirm a real dependency edge, and
            # that the dependent is still WAITING (a concurrent transition may
            # have moved it).
            if dep_id not in dependent.dependencies:
                continue
            if dependent.state != TaskState.WAITING.value:
                continue
            if self.control_plane.store.query_one(
                "SELECT package_id FROM work_package_task_links WHERE task_id = ?",
                (dependent_id,),
            ) is not None:
                # A package node's dependency and cancellation decisions are
                # part of the immutable graph/epoch transaction.  The legacy
                # best-effort reconciler must not rewrite just the task row.
                try:
                    self.control_plane.record_log(
                        "work_package.dependency_reconciliation_deferred",
                        level="warning",
                        detail={
                            "dependent": dependent_id,
                            "dependency": dep_id,
                            "dependency_state": dep_state,
                        },
                    )
                except Exception:  # noqa: BLE001 - diagnostic only
                    pass
                continue
            try:
                if replacement_id:
                    metadata = ensure_json_object(dependent.metadata)
                    resolution = ensure_json_object(
                        metadata.get("dependency_resolution")
                    )
                    unsatisfied = ensure_json_object(
                        resolution.get("unsatisfied")
                    )
                    if unsatisfied.pop(dep_id, None) is not None:
                        resolution["unsatisfied"] = unsatisfied
                        resolution["status"] = (
                            "repairing" if unsatisfied else "resolved"
                        )
                        resolution["updated_at"] = utcnow()
                        metadata["dependency_resolution"] = resolution
                    self.control_plane.update_task(
                        dependent_id,
                        dependencies=[
                            replacement_id if item == dep_id else item
                            for item in dependent.dependencies
                        ],
                        metadata=metadata,
                        actor="dependency-reconciliation",
                    )
                else:
                    metadata = ensure_json_object(dependent.metadata)
                    policy = ensure_json_object(
                        metadata.get("dependency_policy")
                    )
                    coordination = ensure_json_object(
                        metadata.get("coordination")
                    )
                    on_unsatisfied = str(
                        policy.get("on_unsatisfied")
                        or coordination.get("failure_policy")
                        or "supervise"
                    ).strip().lower()
                    if on_unsatisfied == "cancel_scope":
                        self.control_plane._transition_task_internal(
                            dependent_id,
                            TaskState.CANCELLED.value,
                            "dependency-reconciliation",
                            {
                                "reason": "dependency_terminated",
                                "disposition": "preserve",
                                "failed_dependency": dep_id,
                                "dependency_state": dep_state,
                                "dependency_policy": "cancel_scope",
                                "manual_repair_required": True,
                            },
                        )
                        continue
                    if on_unsatisfied not in {"supervise", "block"}:
                        on_unsatisfied = "supervise"

                    now = utcnow()
                    resolution = ensure_json_object(
                        metadata.get("dependency_resolution")
                    )
                    unsatisfied = ensure_json_object(
                        resolution.get("unsatisfied")
                    )
                    unsatisfied[dep_id] = {
                        "state": dep_state,
                        "observed_at": now,
                        "source_actor": actor,
                    }
                    resolution.update(
                        {
                            "schema": "mac.dependency_resolution.v1",
                            "status": "unsatisfied",
                            "policy": on_unsatisfied,
                            "unsatisfied": unsatisfied,
                            "updated_at": now,
                        }
                    )
                    metadata["dependency_resolution"] = resolution
                    self.control_plane.update_task(
                        dependent_id,
                        metadata=metadata,
                        actor="dependency-reconciliation",
                    )
                    detail = {
                        "reason": "dependencies_incomplete",
                        "failed_dependency": dep_id,
                        "dependency_state": dep_state,
                        "dependency_policy": on_unsatisfied,
                        "manual_repair_required": False,
                        "derived_cancellations": 0,
                    }
                    if coordination.get("mode") == "cooperative_integration":
                        self.control_plane._record_history(
                            dependent_id,
                            "task.dependency_unsatisfied",
                            "dependency-reconciliation",
                            TaskState.WAITING.value,
                            TaskState.WAITING.value,
                            detail,
                        )
                    else:
                        self.control_plane._transition_task_internal(
                            dependent_id,
                            TaskState.BLOCKED.value,
                            "dependency-reconciliation",
                            detail,
                        )
                    try:
                        self.control_plane.record_log(
                            "task.dependency_supervised",
                            level="warning",
                            subject_type="task",
                            subject_id=dependent_id,
                            detail={
                                "dependent": dependent_id,
                                "dependency": dep_id,
                                "dependency_state": dep_state,
                                "policy": on_unsatisfied,
                                "derived_cancellations": 0,
                            },
                        )
                    except Exception:  # noqa: BLE001 - diagnostic only
                        pass
            except Exception as exc:  # noqa: BLE001 - one dependent must not stop the rest
                try:
                    self.control_plane.record_log(
                        "task.dependency_propagation_failed",
                        level="warning",
                        detail={"dependent": dependent_id, "dependency": dep_id, "error": str(exc)},
                    )
                except Exception:  # noqa: BLE001
                    pass

    def list_task_transition_outbox(
        self,
        *,
        status: str = "pending",
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskTransitionOutbox]:
        return self.control_plane.task_ledger.list_outbox(status=status, task_id=task_id, limit=limit)

    def drain_task_transition_outbox(
        self,
        *,
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> JsonDict:
        processed = []
        for item in self.control_plane.task_ledger.list_outbox(task_id=task_id, limit=limit):
            try:
                self.control_plane._process_task_transition_outbox_item(item)
            except Exception as exc:  # noqa: BLE001 - one failed side effect must not block later rows.
                self.control_plane.task_ledger.mark_outbox_failed(item.id, str(exc))
                self.control_plane.record_log(
                    "task.transition_outbox.failed",
                    layer="control_plane",
                    source="task-ledger",
                    level="warning",
                    subject_type="task",
                    subject_id=item.task_id,
                    detail={"outbox_id": item.id, "event_type": item.event_type, "error": str(exc)},
                )
                processed.append({"id": item.id, "event_type": item.event_type, "status": "failed"})
                continue
            self.control_plane.task_ledger.mark_outbox_processed(item.id)
            processed.append({"id": item.id, "event_type": item.event_type, "status": "delivered"})
        return {"processed": processed, "count": len(processed)}

    def drain_task_transition_outbox_best_effort(
        self,
        *,
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> JsonDict:
        if not self.control_plane._task_outbox_drain_lock.acquire(blocking=False):
            return {"processed": [], "count": 0, "status": "busy"}
        try:
            result = self.control_plane.drain_task_transition_outbox(task_id=task_id, limit=limit)
            # Success resets the failure streak so the health signal reflects
            # only *ongoing* trouble.
            self.control_plane._task_outbox_drain_failures = 0
            return result
        except Exception as exc:  # noqa: BLE001 - side effects must not break API responses.
            # Track failures in an in-memory counter that CANNOT itself fail:
            # the previous code logged-and-swallowed, then wrapped the log in a
            # bare `except: pass`, so a persistently failing outbox (stranded
            # task transitions) could be entirely invisible if logging also
            # failed and the caller ignored the return. The counter guarantees
            # the failure is observable via status(), and severity escalates
            # once failures persist.
            self.control_plane._task_outbox_drain_failures = (
                getattr(self, "_task_outbox_drain_failures", 0) + 1
            )
            failures = self.control_plane._task_outbox_drain_failures
            try:
                self.control_plane.record_log(
                    "task.transition_outbox.drain_failed",
                    layer="control_plane",
                    source="task-ledger",
                    # A one-off drain miss is a warning; a sustained failure
                    # streak means transitions are stranding — escalate.
                    level="error" if failures >= 3 else "warning",
                    subject_type="task" if task_id else None,
                    subject_id=task_id,
                    detail={
                        "error": str(exc),
                        "limit": limit,
                        "consecutive_failures": failures,
                    },
                )
            except Exception:  # noqa: BLE001 - telemetry may be down; counter still holds it.
                pass
            return {
                "processed": [],
                "count": 0,
                "status": "failed",
                "error": str(exc),
                "consecutive_failures": failures,
            }
        finally:
            self.control_plane._task_outbox_drain_lock.release()

    def _process_task_transition_outbox_item(self, item: TaskTransitionOutbox) -> None:
        if item.event_type == "dependency.reconcile":
            # Post-commit half of an in-transaction terminal transition. Safe to
            # repeat: _resolve_waiting_dependents_of skips any dependent that is
            # no longer WAITING, so a redelivered row is a no-op.
            if item.to_state in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
                self.control_plane._resolve_waiting_dependents_of(
                    item.task_id, item.to_state or "", item.actor or "outbox"
                )
            return
        if item.event_type == "task.lifecycle":
            task = self.control_plane.get_task(item.task_id)
            metadata = ensure_json_object(task.metadata)
            lifecycle = ensure_json_object(metadata.get("repository_ref_lifecycle"))
            if lifecycle:
                self.control_plane.record_log(
                    "repository.ref.lifecycle",
                    layer="control_plane",
                    source="task-ledger",
                    level="info",
                    subject_type="task",
                    subject_id=item.task_id,
                    detail={
                        "task_state": task.state,
                        "disposition": lifecycle.get("disposition"),
                        "status": lifecycle.get("status"),
                        "eligible_after": lifecycle.get("eligible_after"),
                        "replacement_task_id": lifecycle.get("replacement_task_id"),
                    },
                )
            return
        task = self.control_plane.get_task(item.task_id)
        if item.event_type == "workflow.advance":
            # Workflow-runtime hook. The link is the `tasks.workflow_run_id`
            # column (never caller metadata), so forged task metadata cannot
            # push a free-floating task into the workflow state machine.
            if item.to_state in TERMINAL_TASK_STATES.union({TaskState.BLOCKED.value}):
                self.control_plane.workflow_runtime.on_task_completed(item.task_id, item.to_state or "")
            return
        raise ValidationError("unsupported task transition outbox event: %s" % item.event_type)
