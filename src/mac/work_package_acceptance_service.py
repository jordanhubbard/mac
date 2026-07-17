"""Controller-owned acceptance and rejection of exact work-package candidates.

Candidate submission only proves attribution: a worker produced evidence for one
fenced assignment and product WIP reached the candidate buffer.  This module is
the next assembly-line station.  It accepts a candidate only after the
controller's append-only repository observation and the current durable review
both name that exact executor evidence.

The service deliberately owns its transaction rather than calling generic task
lifecycle methods.  Candidate status, package-node state, task state, WIP, and
downstream release must change as one decision or not at all.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from mac.models import JsonDict, TransitionError, ValidationError, json_dumps, json_loads, new_id, utcnow
from mac.store import Store


WORK_PACKAGE_ACCEPTANCE_SERVICE_VERSION = "work-package-acceptance-service-v1"
_VERIFICATION_SCHEMA = "mac.worker_evidence.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class CandidateAcceptanceResult:
    candidate_id: str
    task_id: str
    status: str
    created: bool
    verification_receipt_id: str
    review_id: str
    transferred_wip_token_ids: Tuple[str, ...]
    released_downstream_task_ids: Tuple[str, ...]

    def to_dict(self) -> JsonDict:
        return {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "status": self.status,
            "created": self.created,
            "verification_receipt_id": self.verification_receipt_id,
            "review_id": self.review_id,
            "transferred_wip_token_ids": list(self.transferred_wip_token_ids),
            "released_downstream_task_ids": list(
                self.released_downstream_task_ids
            ),
        }


@dataclass(frozen=True)
class CandidateRejectionResult:
    candidate_id: str
    task_id: str
    status: str
    created: bool
    retry_staged: bool
    remaining_rework_cycles: int
    cancelled_wip_token_ids: Tuple[str, ...]
    package_state: str

    def to_dict(self) -> JsonDict:
        return {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "status": self.status,
            "created": self.created,
            "retry_staged": self.retry_staged,
            "remaining_rework_cycles": self.remaining_rework_cycles,
            "cancelled_wip_token_ids": list(self.cancelled_wip_token_ids),
            "package_state": self.package_state,
        }


class WorkPackageAcceptanceService:
    """Resolve candidate-buffer work under the package/epoch fence."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def accept(self, candidate_id: str, *, actor: str) -> CandidateAcceptanceResult:
        candidate_value = self._required(candidate_id, "candidate acceptance id")
        actor_value = self._required(actor, "candidate acceptance actor")
        now = utcnow()

        with self.store.transaction() as conn:
            context = self._lock_context(conn, candidate_value)
            if context["candidate_status"] == "accepted":
                return self._accepted_retry(conn, context)
            if context["candidate_status"] != "submitted":
                raise TransitionError(
                    "only a submitted work-package candidate can be accepted"
                )
            self._require_current_candidate(context, decision="acceptance")
            if context["task_state"] != "reviewing":
                raise TransitionError(
                    "candidate acceptance requires the task to be in reviewing"
                )

            receipt = self._require_exact_receipt(context)
            review = self._require_current_approved_review(conn, context, receipt)
            definition = self._definition(context)
            node = self._plan_node(definition, str(context["node_key"]))
            self._require_materialized_rework_contract(context, node)

            changed = conn.execute(
                "UPDATE work_package_node_candidates SET status = ?, accepted_at = ?, "
                "accepted_by = ? WHERE id = ? AND status = ?",
                ("accepted", now, actor_value, candidate_value, "submitted"),
            )
            if changed.rowcount != 1:
                raise TransitionError("candidate state changed during acceptance")

            transferred = self._transfer_candidate_wip(
                conn,
                context=context,
                candidate_id=candidate_value,
                actor=actor_value,
                now=now,
                require_wip=self._node_kind(node) == "mutation",
            )
            link = conn.execute(
                "UPDATE work_package_task_links SET node_state = ? "
                "WHERE task_id = ? AND package_id = ? AND plan_version = ? "
                "AND epoch = ? AND node_key = ? AND node_generation = ? "
                "AND node_state = ?",
                (
                    "candidate_accepted",
                    context["task_id"],
                    context["package_id"],
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    context["node_key"],
                    int(context["node_generation"]),
                    "candidate_submitted",
                ),
            )
            if link.rowcount != 1:
                raise TransitionError("candidate node changed during acceptance")

            task_detail = {
                "schema": "mac.work_package.candidate_acceptance.v1",
                "package_id": context["package_id"],
                "plan_version": int(context["plan_version"]),
                "epoch": int(context["epoch"]),
                "node_key": context["node_key"],
                "candidate_id": candidate_value,
                "executor_evidence_id": context["evidence_id"],
                "verification_receipt_id": receipt["verification_receipt_id"],
                "review_id": review["review_id"],
                "review_verdict_evidence_id": review["verdict_evidence_id"],
                "service_version": WORK_PACKAGE_ACCEPTANCE_SERVICE_VERSION,
            }
            task = conn.execute(
                "UPDATE tasks SET state = ?, completed_at = COALESCE(completed_at, ?), "
                "owner_agent_id = NULL, lease_id = NULL, leased_until = NULL, "
                "updated_at = ? WHERE id = ? AND state = ? AND attempt_count = ? "
                "AND lease_id IS NULL",
                (
                    "completed",
                    now,
                    now,
                    context["task_id"],
                    "reviewing",
                    int(context["attempt_number"]),
                ),
            )
            if task.rowcount != 1:
                raise TransitionError("candidate task changed during acceptance")
            self._append_task_transition(
                conn,
                task_id=str(context["task_id"]),
                actor=actor_value,
                from_state="reviewing",
                to_state="completed",
                detail=task_detail,
                now=now,
            )

            released = self._release_ready_successors(
                conn,
                context=context,
                definition=definition,
                actor=actor_value,
                candidate_id=candidate_value,
                now=now,
            )
            self._append_package_history(
                conn,
                package_id=str(context["package_id"]),
                plan_version=int(context["plan_version"]),
                epoch=int(context["epoch"]),
                event_type="work_package.candidate_accepted",
                actor=actor_value,
                detail={
                    **task_detail,
                    "transferred_wip_token_ids": list(transferred),
                    "released_downstream_task_ids": list(released),
                },
                now=now,
            )

        return CandidateAcceptanceResult(
            candidate_id=candidate_value,
            task_id=str(context["task_id"]),
            status="accepted",
            created=True,
            verification_receipt_id=str(receipt["verification_receipt_id"]),
            review_id=str(review["review_id"]),
            transferred_wip_token_ids=transferred,
            released_downstream_task_ids=released,
        )

    def reject(
        self,
        candidate_id: str,
        *,
        actor: str,
        reason: str,
    ) -> CandidateRejectionResult:
        candidate_value = self._required(candidate_id, "candidate rejection id")
        actor_value = self._required(actor, "candidate rejection actor")
        reason_value = self._required(reason, "candidate rejection reason")
        now = utcnow()

        with self.store.transaction() as conn:
            context = self._lock_context(conn, candidate_value)
            if context["candidate_status"] == "rejected":
                if str(context["rejection_reason"] or "") != reason_value:
                    raise ValidationError(
                        "candidate rejection retry does not match the recorded reason"
                    )
                return self._rejected_retry(conn, context)
            if context["candidate_status"] != "submitted":
                raise TransitionError(
                    "only a submitted work-package candidate can be rejected"
                )
            self._require_current_candidate(context, decision="rejection")
            if context["task_state"] not in {"reviewing", "open", "blocked"}:
                raise TransitionError(
                    "candidate rejection requires a review or staged-rework task"
                )

            definition = self._definition(context)
            node = self._plan_node(definition, str(context["node_key"]))
            self._require_materialized_rework_contract(context, node)
            remaining = int(context["max_attempts"]) - int(context["attempt_number"])
            if remaining < 0:
                raise ValidationError("candidate attempt exceeds its immutable rework budget")
            retry_staged = remaining > 0
            target_task_state = "open" if retry_staged else "blocked"

            changed = conn.execute(
                "UPDATE work_package_node_candidates SET status = ?, "
                "rejection_reason = ? WHERE id = ? AND status = ?",
                ("rejected", reason_value, candidate_value, "submitted"),
            )
            if changed.rowcount != 1:
                raise TransitionError("candidate state changed during rejection")
            cancelled = self._cancel_candidate_wip(
                conn,
                context=context,
                candidate_id=candidate_value,
                actor=actor_value,
                reason=reason_value,
                now=now,
                require_wip=self._node_kind(node) == "mutation",
            )
            link = conn.execute(
                "UPDATE work_package_task_links SET node_state = ? "
                "WHERE task_id = ? AND package_id = ? AND plan_version = ? "
                "AND epoch = ? AND node_key = ? AND node_generation = ? "
                "AND node_state = ?",
                (
                    "rejected",
                    context["task_id"],
                    context["package_id"],
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    context["node_key"],
                    int(context["node_generation"]),
                    "candidate_submitted",
                ),
            )
            if link.rowcount != 1:
                raise TransitionError("candidate node changed during rejection")

            task_metadata = json_loads(context["task_metadata"], {})
            if not isinstance(task_metadata, dict):
                raise ValidationError("candidate task metadata is malformed")
            task_metadata["no_dispatch"] = True
            task_metadata["work_package_rework"] = {
                "schema": "mac.work_package.rework.v1",
                "package_id": context["package_id"],
                "plan_version": int(context["plan_version"]),
                "epoch": int(context["epoch"]),
                "node_key": context["node_key"],
                "candidate_id": candidate_value,
                "rejected_attempt_number": int(context["attempt_number"]),
                "max_attempts": int(context["max_attempts"]),
                "remaining_cycles": remaining,
                "status": "staged" if retry_staged else "exhausted",
                "reason": reason_value,
                "recorded_at": now,
            }
            from_state = str(context["task_state"])
            task = conn.execute(
                "UPDATE tasks SET state = ?, metadata = ?, owner_agent_id = NULL, "
                "lease_id = NULL, leased_until = NULL, completed_at = NULL, "
                "updated_at = ? WHERE id = ? AND state = ? AND attempt_count = ? "
                "AND lease_id IS NULL",
                (
                    target_task_state,
                    json_dumps(task_metadata),
                    now,
                    context["task_id"],
                    from_state,
                    int(context["attempt_number"]),
                ),
            )
            if task.rowcount != 1:
                raise TransitionError("candidate task changed during rejection")

            package_metadata = json_loads(context["package_metadata"], {})
            if not isinstance(package_metadata, dict):
                raise ValidationError("work-package metadata is malformed")
            package_metadata["andon"] = {
                "schema": "mac.work_package.andon.v1",
                "reason": "candidate_rejected",
                "candidate_id": candidate_value,
                "task_id": context["task_id"],
                "node_key": context["node_key"],
                "attempt_number": int(context["attempt_number"]),
                "remaining_rework_cycles": remaining,
                "retry_staged": retry_staged,
                "detail": reason_value,
                "raised_by": actor_value,
                "raised_at": now,
            }
            package = conn.execute(
                "UPDATE work_packages SET state = ?, metadata = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND current_plan_version = ? "
                "AND current_epoch = ?",
                (
                    "paused",
                    json_dumps(package_metadata),
                    now,
                    context["package_id"],
                    "active",
                    int(context["plan_version"]),
                    int(context["epoch"]),
                ),
            )
            if package.rowcount != 1:
                raise TransitionError("work package changed during candidate rejection")

            detail = {
                "schema": "mac.work_package.candidate_rejection.v1",
                "package_id": context["package_id"],
                "plan_version": int(context["plan_version"]),
                "epoch": int(context["epoch"]),
                "node_key": context["node_key"],
                "candidate_id": candidate_value,
                "executor_evidence_id": context["evidence_id"],
                "reason": reason_value,
                "retry_staged": retry_staged,
                "remaining_rework_cycles": remaining,
                "cancelled_wip_token_ids": list(cancelled),
                "package_paused": True,
                "service_version": WORK_PACKAGE_ACCEPTANCE_SERVICE_VERSION,
            }
            if from_state != target_task_state:
                self._append_task_transition(
                    conn,
                    task_id=str(context["task_id"]),
                    actor=actor_value,
                    from_state=from_state,
                    to_state=target_task_state,
                    detail=detail,
                    now=now,
                )
            else:
                self._append_task_history(
                    conn,
                    task_id=str(context["task_id"]),
                    event_type="task.work_package_candidate_rejected",
                    actor=actor_value,
                    from_state=from_state,
                    to_state=target_task_state,
                    detail=detail,
                    now=now,
                )
            self._append_package_history(
                conn,
                package_id=str(context["package_id"]),
                plan_version=int(context["plan_version"]),
                epoch=int(context["epoch"]),
                event_type="work_package.candidate_rejected",
                actor=actor_value,
                detail=detail,
                now=now,
            )

        return CandidateRejectionResult(
            candidate_id=candidate_value,
            task_id=str(context["task_id"]),
            status="rejected",
            created=True,
            retry_staged=retry_staged,
            remaining_rework_cycles=remaining,
            cancelled_wip_token_ids=cancelled,
            package_state="paused",
        )

    def _lock_context(self, conn: Any, candidate_id: str) -> Mapping[str, Any]:
        discovery = conn.execute(
            "SELECT package_id, task_id FROM work_package_node_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if discovery is None:
            raise ValidationError("work-package candidate was not found")
        package_id = str(discovery["package_id"])
        task_id = str(discovery["task_id"])

        # One package row is the serialization point for every decision that
        # can change its current plan/epoch.  Plan rows themselves are
        # append-only, so locking this pointer and the mutable epoch/link/task
        # rows gives one coherent transaction on SQLite and PostgreSQL.
        if conn.execute(
            "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
            (package_id,),
        ).rowcount != 1:
            raise TransitionError("work package disappeared during candidate decision")
        if conn.execute(
            "UPDATE tasks SET updated_at = updated_at WHERE id = ?", (task_id,)
        ).rowcount != 1:
            raise TransitionError("candidate task disappeared during decision")
        if conn.execute(
            "UPDATE work_package_node_candidates SET status = status WHERE id = ?",
            (candidate_id,),
        ).rowcount != 1:
            raise TransitionError("candidate disappeared during decision")

        identity = conn.execute(
            "SELECT plan_version, epoch FROM work_package_node_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if identity is None:
            raise TransitionError("candidate disappeared during decision")
        if conn.execute(
            "UPDATE work_package_epochs SET status = status WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ?",
            (package_id, int(identity["plan_version"]), int(identity["epoch"])),
        ).rowcount != 1:
            raise TransitionError("candidate epoch disappeared during decision")
        if conn.execute(
            "UPDATE work_package_task_links SET node_state = node_state "
            "WHERE task_id = ? AND package_id = ? AND plan_version = ? AND epoch = ?",
            (
                task_id,
                package_id,
                int(identity["plan_version"]),
                int(identity["epoch"]),
            ),
        ).rowcount != 1:
            raise TransitionError("candidate task link disappeared during decision")

        context = conn.execute(
            """
            SELECT
                candidate.id AS candidate_id,
                candidate.status AS candidate_status,
                candidate.rejection_reason,
                candidate.task_id,
                candidate.package_id,
                candidate.plan_version,
                candidate.epoch,
                candidate.node_key,
                candidate.node_generation,
                candidate.assignment_lease_id,
                candidate.attempt_number,
                candidate.evidence_id,
                candidate.submitted_at,
                package.state AS package_state,
                package.current_plan_version,
                package.current_epoch,
                package.repository_id,
                package.metadata AS package_metadata,
                epoch.status AS epoch_status,
                plan.definition AS plan_definition,
                link.node_state,
                link.declared_effects_digest AS link_effects_digest,
                task.state AS task_state,
                task.metadata AS task_metadata,
                task.dependencies AS task_dependencies,
                task.attempt_count,
                task.max_attempts,
                task.lease_id AS task_lease_id,
                executor.created_by AS executor_created_by,
                executor.metadata AS executor_metadata,
                attempt.lease_id AS attempt_lease_id,
                attempt.agent_id AS attempt_agent_id,
                attempt.attempt_number AS linked_attempt_number,
                attempt.attempt_ref,
                attempt.attempt_base_sha,
                attempt.declared_effects_digest AS attempt_effects_digest,
                verification.id AS verification_receipt_id,
                verification.repository_id AS verification_repository_id,
                verification.attempt_ref AS verified_attempt_ref,
                verification.attempt_base_sha AS verified_attempt_base_sha,
                verification.attempt_head_sha,
                verification.declared_effects_digest AS verified_effects_digest,
                verification.tree_digest,
                verification.receipt_digest
            FROM work_package_node_candidates AS candidate
            JOIN work_packages AS package ON package.id = candidate.package_id
            JOIN work_package_epochs AS epoch
              ON epoch.package_id = candidate.package_id
             AND epoch.plan_version = candidate.plan_version
             AND epoch.epoch = candidate.epoch
            JOIN work_package_plan_versions AS plan
              ON plan.package_id = candidate.package_id
             AND plan.version = candidate.plan_version
            JOIN work_package_task_links AS link
              ON link.task_id = candidate.task_id
             AND link.package_id = candidate.package_id
             AND link.plan_version = candidate.plan_version
             AND link.epoch = candidate.epoch
             AND link.node_key = candidate.node_key
             AND link.node_generation = candidate.node_generation
            JOIN tasks AS task ON task.id = candidate.task_id
            JOIN evidence_attempt_links AS attempt
              ON attempt.evidence_id = candidate.evidence_id
             AND attempt.task_id = candidate.task_id
             AND attempt.lease_id = candidate.assignment_lease_id
             AND attempt.attempt_number = candidate.attempt_number
            JOIN evidence AS executor
              ON executor.id = attempt.evidence_id
             AND executor.task_id = attempt.task_id
            LEFT JOIN evidence_attempt_verifications AS verification
              ON verification.evidence_id = attempt.evidence_id
             AND verification.task_id = attempt.task_id
             AND verification.lease_id = attempt.lease_id
             AND verification.agent_id = attempt.agent_id
             AND verification.attempt_number = attempt.attempt_number
             AND verification.attempt_ref = attempt.attempt_ref
             AND verification.attempt_base_sha = attempt.attempt_base_sha
             AND verification.declared_effects_digest = attempt.declared_effects_digest
            WHERE candidate.id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if context is None:
            raise ValidationError(
                "candidate is not bound to an exact package assignment and evidence row"
            )
        return context

    @staticmethod
    def _require_current_candidate(context: Mapping[str, Any], *, decision: str) -> None:
        if (
            context["package_state"] != "active"
            or int(context["current_plan_version"]) != int(context["plan_version"])
            or int(context["current_epoch"]) != int(context["epoch"])
            or context["epoch_status"] != "active"
            or context["node_state"] != "candidate_submitted"
            or int(context["attempt_count"]) != int(context["attempt_number"])
            or context["task_lease_id"] is not None
            or context["assignment_lease_id"] != context["attempt_lease_id"]
            or int(context["attempt_number"]) != int(context["linked_attempt_number"])
            or context["executor_created_by"] != context["attempt_agent_id"]
            or context["link_effects_digest"] != context["attempt_effects_digest"]
        ):
            raise TransitionError(
                "candidate %s is not current under its package epoch fence" % decision
            )

    def _require_exact_receipt(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        if not context["verification_receipt_id"]:
            raise ValidationError(
                "candidate acceptance requires an append-only output verification receipt"
            )
        exact = (
            context["verification_repository_id"] == context["repository_id"]
            and context["verified_attempt_ref"] == context["attempt_ref"]
            and context["verified_attempt_base_sha"] == context["attempt_base_sha"]
            and context["verified_effects_digest"] == context["attempt_effects_digest"]
            and bool(context["attempt_head_sha"])
            and bool(context["tree_digest"])
            and bool(context["receipt_digest"])
        )
        if not exact:
            raise ValidationError(
                "candidate output verification receipt does not match the exact attempt"
            )

        executor_metadata = json_loads(context["executor_metadata"], {})
        manifest = (
            executor_metadata.get("verification")
            if isinstance(executor_metadata, dict)
            else None
        )
        repo = manifest.get("repo") if isinstance(manifest, dict) else None
        if not isinstance(repo, dict):
            raise ValidationError(
                "candidate executor evidence lacks its reviewed repository identity"
            )
        if str(repo.get("head_sha") or "").strip().lower() != str(
            context["attempt_head_sha"]
        ).lower():
            raise ValidationError(
                "candidate executor evidence head does not match controller observation"
            )
        if str(repo.get("base_sha") or "").strip().lower() != str(
            context["attempt_base_sha"]
        ).lower():
            raise ValidationError(
                "candidate executor evidence base does not match its assignment"
            )
        return context

    def _require_current_approved_review(
        self,
        conn: Any,
        context: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        task_metadata = json_loads(context["task_metadata"], {})
        target = (
            task_metadata.get("review_target")
            if isinstance(task_metadata, dict)
            else None
        )
        if not isinstance(target, dict) or str(
            target.get("executor_evidence_id") or ""
        ).strip() != str(context["evidence_id"]):
            raise ValidationError(
                "candidate is not the task's current executor review target"
            )
        try:
            target_attempt = int(target.get("attempt_count"))
        except (TypeError, ValueError):
            raise ValidationError(
                "candidate review target lacks its exact attempt number"
            ) from None
        if target_attempt != int(context["attempt_number"]):
            raise ValidationError("candidate review target is from a stale attempt")

        # The latest review row is the current durable verdict.  We join its
        # verdict evidence to the task and then inspect the signed manifest
        # that ReviewService validated before it was allowed to write
        # status=approved.  An older approval can never outrank a later review.
        review = conn.execute(
            """
            SELECT
                review.id AS review_id,
                review.status AS review_status,
                review.reviewer_agent_id,
                review.evidence_id AS verdict_evidence_id,
                review.created_at AS review_created_at,
                review.completed_at AS review_completed_at,
                verdict.created_by AS verdict_created_by,
                verdict.created_at AS verdict_created_at,
                verdict.metadata AS verdict_metadata
            FROM reviews AS review
            LEFT JOIN evidence AS verdict
              ON verdict.id = review.evidence_id
             AND verdict.task_id = review.task_id
            WHERE review.task_id = ?
            ORDER BY review.created_at DESC, review.id DESC
            LIMIT 1
            """,
            (context["task_id"],),
        ).fetchone()
        if (
            review is None
            or review["review_status"] != "approved"
            or not review["review_completed_at"]
            or not review["verdict_evidence_id"]
            or review["verdict_created_by"] != review["reviewer_agent_id"]
        ):
            raise ValidationError(
                "candidate acceptance requires the current durable approved review"
            )
        if str(review["review_created_at"]) < str(context["submitted_at"]):
            raise ValidationError("candidate approval predates candidate submission")
        if str(review["verdict_created_at"]) < str(review["review_created_at"]):
            raise ValidationError("candidate verdict evidence predates its review")

        metadata = json_loads(review["verdict_metadata"], {})
        manifest = metadata.get("verification") if isinstance(metadata, dict) else None
        if not isinstance(manifest, dict):
            raise ValidationError("approved review lacks review_verdict evidence")
        required = {
            "schema": _VERIFICATION_SCHEMA,
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "reviewed_evidence_id": str(context["evidence_id"]),
            "signed_by": str(review["reviewer_agent_id"]),
        }
        for field, expected in required.items():
            if str(manifest.get(field) or "").strip() != expected:
                raise ValidationError(
                    "approved review verdict does not exactly match candidate evidence"
                )
        if not str(manifest.get("signature") or "").strip():
            raise ValidationError("approved review verdict lacks its durable signature")
        if not _SHA256_RE.fullmatch(str(manifest.get("worktree_digest") or "")):
            raise ValidationError("approved review verdict lacks a worktree digest")
        repo = manifest.get("repo")
        if not isinstance(repo, dict) or str(repo.get("head_sha") or "").lower() != str(
            receipt["attempt_head_sha"]
        ).lower():
            raise ValidationError(
                "approved review head does not match controller-observed candidate head"
            )
        if repo.get("base_sha") is not None and str(repo.get("base_sha") or "").lower() != str(
            receipt["attempt_base_sha"]
        ).lower():
            raise ValidationError(
                "approved review base does not match the candidate assignment"
            )
        return review

    def _transfer_candidate_wip(
        self,
        conn: Any,
        *,
        context: Mapping[str, Any],
        candidate_id: str,
        actor: str,
        now: str,
        require_wip: bool,
    ) -> Tuple[str, ...]:
        rows = conn.execute(
            "SELECT * FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND task_id = ? "
            "AND stage = ? AND state = ? ORDER BY id",
            (
                context["package_id"],
                int(context["plan_version"]),
                int(context["epoch"]),
                context["task_id"],
                "candidate_buffer",
                "held",
            ),
        ).fetchall()
        if require_wip and not rows:
            raise TransitionError("mutation candidate has no product WIP to accept")
        successors = []
        for row in rows:
            successor_id = self._wip_successor_id(str(row["id"]), candidate_id)
            reason = json_dumps(
                {
                    "schema": "mac.work_package.wip_resolution.v1",
                    "decision": "accepted",
                    "candidate_id": candidate_id,
                    "evidence_id": context["evidence_id"],
                    "actor": actor,
                    "successor_token_id": successor_id,
                    "resolved_at": now,
                }
            )
            released = conn.execute(
                "UPDATE work_package_wip_tokens SET state = ?, released_at = ?, "
                "release_reason = ? WHERE id = ? AND stage = ? AND state = ?",
                ("released", now, reason, row["id"], "candidate_buffer", "held"),
            )
            if released.rowcount != 1:
                raise TransitionError("candidate-buffer WIP changed during acceptance")
            conn.execute(
                "INSERT INTO work_package_wip_tokens ("
                "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
                "token_kind, stage, state, generation, capacity_units, reservation_key, "
                "predecessor_token_id, acquired_by_assignment_lease_id, acquired_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    successor_id,
                    row["package_id"],
                    int(row["plan_version"]),
                    int(row["epoch"]),
                    row["node_key"],
                    row["task_id"],
                    row["resource_key"],
                    row["token_kind"],
                    "fan_in_reservation",
                    "held",
                    int(row["generation"]) + 1,
                    int(row["capacity_units"]),
                    row["reservation_key"],
                    row["id"],
                    row["acquired_by_assignment_lease_id"],
                    now,
                ),
            )
            successors.append(successor_id)
        return tuple(sorted(successors))

    def _cancel_candidate_wip(
        self,
        conn: Any,
        *,
        context: Mapping[str, Any],
        candidate_id: str,
        actor: str,
        reason: str,
        now: str,
        require_wip: bool,
    ) -> Tuple[str, ...]:
        rows = conn.execute(
            "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND task_id = ? "
            "AND stage = ? AND state = ? ORDER BY id",
            (
                context["package_id"],
                int(context["plan_version"]),
                int(context["epoch"]),
                context["task_id"],
                "candidate_buffer",
                "held",
            ),
        ).fetchall()
        if require_wip and not rows:
            raise TransitionError("mutation candidate has no product WIP to reject")
        cancelled = []
        for row in rows:
            release_reason = json_dumps(
                {
                    "schema": "mac.work_package.wip_resolution.v1",
                    "decision": "rejected",
                    "candidate_id": candidate_id,
                    "evidence_id": context["evidence_id"],
                    "actor": actor,
                    "reason": reason,
                    "resolved_at": now,
                }
            )
            changed = conn.execute(
                "UPDATE work_package_wip_tokens SET state = ?, released_at = ?, "
                "release_reason = ? WHERE id = ? AND stage = ? AND state = ?",
                ("cancelled", now, release_reason, row["id"], "candidate_buffer", "held"),
            )
            if changed.rowcount != 1:
                raise TransitionError("candidate-buffer WIP changed during rejection")
            cancelled.append(str(row["id"]))
        return tuple(cancelled)

    def _release_ready_successors(
        self,
        conn: Any,
        *,
        context: Mapping[str, Any],
        definition: Mapping[str, Any],
        actor: str,
        candidate_id: str,
        now: str,
    ) -> Tuple[str, ...]:
        nodes = definition.get("nodes")
        if not isinstance(nodes, list):
            raise ValidationError("work-package plan has no node list")
        node_by_key = {
            str(node.get("node_key")): node
            for node in nodes
            if isinstance(node, Mapping) and node.get("node_key")
        }
        if len(node_by_key) != len(nodes):
            raise ValidationError("work-package plan node identities are malformed")
        current_key = str(context["node_key"])
        released = []
        for successor_key in sorted(node_by_key):
            successor = node_by_key[successor_key]
            depends_on = self._string_list(successor.get("depends_on"), "depends_on")
            if current_key not in depends_on:
                continue
            if not self._predecessors_accepted(
                conn,
                package_id=str(context["package_id"]),
                plan_version=int(context["plan_version"]),
                epoch=int(context["epoch"]),
                predecessor_keys=depends_on,
            ):
                continue
            successor_row = conn.execute(
                "SELECT link.task_id, link.node_state, task.state, task.metadata, "
                "task.dependencies, task.owner_agent_id, task.lease_id "
                "FROM work_package_task_links AS link "
                "JOIN tasks AS task ON task.id = link.task_id "
                "WHERE link.package_id = ? AND link.plan_version = ? "
                "AND link.epoch = ? AND link.node_key = ?",
                (
                    context["package_id"],
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    successor_key,
                ),
            ).fetchone()
            if successor_row is None:
                raise ValidationError("work-package successor task is missing")
            if successor_row["node_state"] != "planned":
                # A prior predecessor acceptance may already have released it.
                if successor_row["node_state"] == "ready":
                    continue
                raise TransitionError("work-package successor is not releasable")
            if (
                successor_row["state"] != "waiting"
                or successor_row["owner_agent_id"] is not None
                or successor_row["lease_id"] is not None
            ):
                raise TransitionError("work-package successor task changed before release")

            internal_task_ids = []
            for predecessor_key in depends_on:
                predecessor = conn.execute(
                    "SELECT task_id FROM work_package_task_links WHERE package_id = ? "
                    "AND plan_version = ? AND epoch = ? AND node_key = ?",
                    (
                        context["package_id"],
                        int(context["plan_version"]),
                        int(context["epoch"]),
                        predecessor_key,
                    ),
                ).fetchone()
                if predecessor is None:
                    raise ValidationError("work-package predecessor task is missing")
                internal_task_ids.append(str(predecessor["task_id"]))
            external = successor.get("external_dependencies") or []
            if not isinstance(external, list):
                raise ValidationError("work-package external dependencies are malformed")
            external_task_ids = []
            for item in external:
                if not isinstance(item, Mapping) or not str(item.get("task_id") or "").strip():
                    raise ValidationError("work-package external dependency is malformed")
                external_task_ids.append(str(item["task_id"]))
            expected_dependencies = sorted(set(internal_task_ids + external_task_ids))
            observed_dependencies = json_loads(successor_row["dependencies"], None)
            if observed_dependencies != expected_dependencies:
                raise ValidationError(
                    "work-package successor dependencies deviate from immutable plan"
                )
            if external_task_ids:
                placeholders = ",".join("?" for _ in external_task_ids)
                rows = conn.execute(
                    "SELECT id, state FROM tasks WHERE id IN (%s)" % placeholders,
                    tuple(external_task_ids),
                ).fetchall()
                by_id = {str(row["id"]): str(row["state"]) for row in rows}
                if any(by_id.get(task_id) != "completed" for task_id in external_task_ids):
                    continue

            metadata = json_loads(successor_row["metadata"], {})
            if not isinstance(metadata, dict) or metadata.get("no_dispatch") is not True:
                raise ValidationError(
                    "planned work-package successor lacks its dispatch hold"
                )
            successor_kind = self._node_kind(successor)
            controller_owned = successor_kind in {"integration", "certification"}
            if controller_owned:
                # Controller stations participate in the durable graph but are
                # never ordinary worker assignments.  Keep the task waiting and
                # held while making its authoritative package link ready for the
                # integration/certification controller.
                task_update = conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE id = ? AND state = ? "
                    "AND dependencies = ? AND owner_agent_id IS NULL AND lease_id IS NULL",
                    (
                        now,
                        successor_row["task_id"],
                        "waiting",
                        successor_row["dependencies"],
                    ),
                )
            else:
                metadata.pop("no_dispatch", None)
                task_update = conn.execute(
                    "UPDATE tasks SET state = ?, metadata = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND dependencies = ? "
                    "AND owner_agent_id IS NULL AND lease_id IS NULL",
                    (
                        "open",
                        json_dumps(metadata),
                        now,
                        successor_row["task_id"],
                        "waiting",
                        successor_row["dependencies"],
                    ),
                )
            if task_update.rowcount != 1:
                raise TransitionError("work-package successor changed during release")
            link_update = conn.execute(
                "UPDATE work_package_task_links SET node_state = ? "
                "WHERE task_id = ? AND package_id = ? AND plan_version = ? "
                "AND epoch = ? AND node_state = ?",
                (
                    "ready",
                    successor_row["task_id"],
                    context["package_id"],
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    "planned",
                ),
            )
            if link_update.rowcount != 1:
                raise TransitionError("work-package successor link changed during release")
            detail = {
                "schema": "mac.work_package.downstream_release.v1",
                "package_id": context["package_id"],
                "plan_version": int(context["plan_version"]),
                "epoch": int(context["epoch"]),
                "node_key": successor_key,
                "accepted_predecessor_candidate_id": candidate_id,
                "predecessor_node_keys": depends_on,
                "dispatch_mode": (
                    "controller_station" if controller_owned else "worker"
                ),
                "service_version": WORK_PACKAGE_ACCEPTANCE_SERVICE_VERSION,
            }
            if controller_owned:
                self._append_task_history(
                    conn,
                    task_id=str(successor_row["task_id"]),
                    event_type="work_package.controller_station_ready",
                    actor=actor,
                    from_state="waiting",
                    to_state="waiting",
                    detail=detail,
                    now=now,
                )
            else:
                self._append_task_transition(
                    conn,
                    task_id=str(successor_row["task_id"]),
                    actor=actor,
                    from_state="waiting",
                    to_state="open",
                    detail=detail,
                    now=now,
                )
            released.append(str(successor_row["task_id"]))
        return tuple(released)

    @staticmethod
    def _predecessors_accepted(
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        predecessor_keys: Sequence[str],
    ) -> bool:
        for node_key in predecessor_keys:
            row = conn.execute(
                "SELECT link.task_id, task.state, candidate.id AS candidate_id "
                "FROM work_package_task_links AS link "
                "JOIN tasks AS task ON task.id = link.task_id "
                "LEFT JOIN work_package_node_candidates AS candidate "
                "ON candidate.task_id = link.task_id "
                "AND candidate.package_id = link.package_id "
                "AND candidate.plan_version = link.plan_version "
                "AND candidate.epoch = link.epoch "
                "AND candidate.node_key = link.node_key "
                "AND candidate.node_generation = link.node_generation "
                "AND candidate.status = ? "
                "WHERE link.package_id = ? AND link.plan_version = ? "
                "AND link.epoch = ? AND link.node_key = ?",
                ("accepted", package_id, plan_version, epoch, node_key),
            ).fetchone()
            if row is None:
                raise ValidationError("work-package predecessor task is missing")
            if row["candidate_id"] is None or row["state"] != "completed":
                return False
        return True

    def _accepted_retry(
        self, conn: Any, context: Mapping[str, Any]
    ) -> CandidateAcceptanceResult:
        if context["node_state"] not in {
            "candidate_accepted",
            "integrated",
            "certified",
        } or context["task_state"] != "completed":
            raise TransitionError("accepted candidate has incoherent task or node state")
        receipt = self._require_exact_receipt(context)
        rows = conn.execute(
            "SELECT id FROM work_package_wip_tokens WHERE predecessor_token_id IN ("
            "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND task_id = ? AND stage = ?"
            ") ORDER BY id",
            (
                context["package_id"],
                int(context["plan_version"]),
                int(context["epoch"]),
                context["task_id"],
                "candidate_buffer",
            ),
        ).fetchall()
        history_rows = conn.execute(
            "SELECT detail FROM work_package_history WHERE package_id = ? "
            "AND event_type = ? ORDER BY seq DESC",
            (context["package_id"], "work_package.candidate_accepted"),
        ).fetchall()
        decision = None
        for row in history_rows:
            detail = json_loads(row["detail"], {})
            if isinstance(detail, dict) and detail.get("candidate_id") == context["candidate_id"]:
                decision = detail
                break
        if decision is None:
            raise TransitionError("accepted candidate lacks its immutable decision history")
        if (
            decision.get("executor_evidence_id") != context["evidence_id"]
            or decision.get("verification_receipt_id")
            != receipt["verification_receipt_id"]
            or not str(decision.get("review_id") or "").strip()
        ):
            raise TransitionError("accepted candidate decision history is incoherent")
        return CandidateAcceptanceResult(
            candidate_id=str(context["candidate_id"]),
            task_id=str(context["task_id"]),
            status="accepted",
            created=False,
            verification_receipt_id=str(receipt["verification_receipt_id"]),
            review_id=str(decision["review_id"]),
            transferred_wip_token_ids=tuple(str(row["id"]) for row in rows),
            released_downstream_task_ids=tuple(
                str(task_id)
                for task_id in decision.get("released_downstream_task_ids", [])
            ),
        )

    def _rejected_retry(
        self, conn: Any, context: Mapping[str, Any]
    ) -> CandidateRejectionResult:
        if context["node_state"] != "rejected" or context["package_state"] != "paused":
            raise TransitionError("rejected candidate has incoherent package state")
        if context["task_state"] not in {"open", "blocked"}:
            raise TransitionError("rejected candidate has incoherent task state")
        rows = conn.execute(
            "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND task_id = ? "
            "AND stage = ? AND state = ? ORDER BY id",
            (
                context["package_id"],
                int(context["plan_version"]),
                int(context["epoch"]),
                context["task_id"],
                "candidate_buffer",
                "cancelled",
            ),
        ).fetchall()
        remaining = max(
            0, int(context["max_attempts"]) - int(context["attempt_number"])
        )
        return CandidateRejectionResult(
            candidate_id=str(context["candidate_id"]),
            task_id=str(context["task_id"]),
            status="rejected",
            created=False,
            retry_staged=context["task_state"] == "open" and remaining > 0,
            remaining_rework_cycles=remaining,
            cancelled_wip_token_ids=tuple(str(row["id"]) for row in rows),
            package_state="paused",
        )

    @staticmethod
    def _definition(context: Mapping[str, Any]) -> Mapping[str, Any]:
        definition = json_loads(context["plan_definition"], {})
        if not isinstance(definition, dict):
            raise ValidationError("work-package plan definition is malformed")
        return definition

    @staticmethod
    def _plan_node(definition: Mapping[str, Any], node_key: str) -> Mapping[str, Any]:
        nodes = definition.get("nodes")
        if not isinstance(nodes, list):
            raise ValidationError("work-package plan has no node list")
        matches = [
            node
            for node in nodes
            if isinstance(node, Mapping) and node.get("node_key") == node_key
        ]
        if len(matches) != 1:
            raise ValidationError("candidate does not name exactly one immutable plan node")
        return matches[0]

    @staticmethod
    def _node_kind(node: Mapping[str, Any]) -> str:
        return str(node.get("node_type") or node.get("kind") or "mutation")

    @staticmethod
    def _require_materialized_rework_contract(
        context: Mapping[str, Any], node: Mapping[str, Any]
    ) -> None:
        rework = node.get("rework") or {}
        if not isinstance(rework, Mapping):
            raise ValidationError("candidate node rework contract is malformed")
        try:
            planned_attempts = int(rework.get("max_cycles")) + 1
        except (TypeError, ValueError):
            raise ValidationError("candidate node rework budget is malformed") from None
        if planned_attempts != int(context["max_attempts"]):
            raise ValidationError(
                "candidate task rework budget deviates from immutable plan"
            )

    @staticmethod
    def _string_list(value: Any, field_name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValidationError("work-package %s is malformed" % field_name)
        return list(value)

    @staticmethod
    def _wip_successor_id(predecessor_id: str, candidate_id: str) -> str:
        identity = "%s\x00%s\x00fan_in_reservation" % (
            predecessor_id,
            candidate_id,
        )
        return "wpwip_%s" % hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _append_task_transition(
        conn: Any,
        *,
        task_id: str,
        actor: str,
        from_state: str,
        to_state: str,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        WorkPackageAcceptanceService._append_task_history(
            conn,
            task_id=task_id,
            event_type="task.transitioned",
            actor=actor,
            from_state=from_state,
            to_state=to_state,
            detail=detail,
            now=now,
        )
        conn.execute(
            "INSERT INTO task_transition_outbox ("
            "id, task_id, event_type, actor, from_state, to_state, detail, "
            "status, attempts, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("outbox"),
                task_id,
                "task.lifecycle",
                actor,
                from_state,
                to_state,
                json_dumps(dict(detail)),
                "pending",
                0,
                now,
            ),
        )

    @staticmethod
    def _append_task_history(
        conn: Any,
        *,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO task_history ("
            "id, task_id, event_type, actor, from_state, to_state, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("history"),
                task_id,
                event_type,
                actor,
                from_state,
                to_state,
                json_dumps(dict(detail)),
                now,
            ),
        )

    @staticmethod
    def _append_package_history(
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        event_type: str,
        actor: str,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS value "
            "FROM work_package_history WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO work_package_history ("
            "id, package_id, seq, event_type, actor, plan_version, epoch, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("wph"),
                package_id,
                int(seq["value"]),
                event_type,
                actor,
                plan_version,
                epoch,
                json_dumps(dict(detail)),
                now,
            ),
        )

    @staticmethod
    def _required(value: Any, label: str) -> str:
        result = str(value or "").strip()
        if not result:
            raise ValidationError("%s is required" % label)
        return result
