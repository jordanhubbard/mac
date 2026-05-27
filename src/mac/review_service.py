"""Review + Publication domain service.

A task transitions ``RUNNING → NEEDS_REVIEW → REVIEWING → COMPLETED`` via
this service. A review must be filed by an agent that has never owned the
task (no self-approval); approving requires evidence that belongs to the
reviewed task; completion requires an approved review pointing at task
evidence.

``publish_task`` is the only path that legitimately moves a task to
COMPLETED — it runs as a single transaction that flips the task row,
records the publication, writes two history rows (publish + transition),
emits the matching observability events, and idles the owning agent.
"""

from __future__ import annotations

import json
from typing import Any, Callable, List, Optional

from mac.models import (
    Agent,
    AgentStatus,
    AuthorizationError,
    Evidence,
    NotFoundError,
    Publication,
    PublicationStatus,
    Review,
    ReviewStatus,
    Task,
    TaskState,
    TransitionError,
    ValidationError,
    new_id,
    utcnow,
)
from mac.messaging_service import MessagingService
from mac.observability_service import ObservabilityService


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


class ReviewService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        messaging: MessagingService,
        *,
        get_task: Callable[[str], Task],
        get_agent: Callable[[str], Agent],
        get_evidence: Callable[[str], Evidence],
        transition_task: Callable[..., Task],
        record_history: Callable[..., None],
        find_verdict_evidence: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store
        self.observability = observability
        self.messaging = messaging
        self._get_task = get_task
        self._get_agent = get_agent
        self._get_evidence = get_evidence
        self._transition_task = transition_task
        self._record_history = record_history
        # mac-5u1f: optional verdict-evidence finder. When provided,
        # submit_review uses it to enforce that an APPROVED status
        # comes with a properly signed review_verdict authored by
        # the reviewer themselves — not just any evidence row.
        self._find_verdict_evidence = find_verdict_evidence

    # Reviews -----------------------------------------------------------

    def request_review(
        self, task_id: str, reviewer_agent_id: str, actor: str = "dispatcher"
    ) -> Review:
        task = self._get_task(task_id)
        self._get_agent(reviewer_agent_id)
        if self.agent_is_current_owner_or_latest_evidence_author(task_id, reviewer_agent_id):
            raise AuthorizationError(
                "reviewer cannot review its own active task or latest evidence"
            )
        if task.state == TaskState.NEEDS_REVIEW.value:
            self._transition_task(
                task_id,
                TaskState.REVIEWING.value,
                actor,
                {"reviewer_agent_id": reviewer_agent_id},
            )
        elif task.state != TaskState.REVIEWING.value:
            raise TransitionError("task must need review before requesting review")
        now = utcnow()
        with self.store.transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM reviews
                WHERE task_id = ? AND reviewer_agent_id = ? AND status = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (task_id, reviewer_agent_id, ReviewStatus.PENDING.value),
            ).fetchone()
            if existing is not None:
                return self._review_from_row(existing)
            review_id = new_id("review")
            conn.execute(
                """
                INSERT INTO reviews (id, task_id, reviewer_agent_id, status, reason, evidence_id, created_at, completed_at)
                VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (review_id, task_id, reviewer_agent_id, ReviewStatus.PENDING.value, now),
            )
            self._record_history(
                task_id,
                "task.review_requested",
                actor,
                None,
                None,
                {"review_id": review_id},
                conn=conn,
            )
        # Notify the reviewer via the control-channel. Imported here to
        # avoid a tight bidirectional dep; messaging is composed in.
        from mac.models import MessageType

        self.messaging.send_message(
            "dispatcher",
            reviewer_agent_id,
            MessageType.REVIEW_REQUEST.value,
            {"task_id": task_id, "review_id": review_id},
            task_id=task_id,
        )
        return self.get_review(review_id)

    def submit_review(
        self,
        review_id: str,
        status: str,
        reviewer_agent_id: str,
        reason: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> Review:
        review = self.get_review(review_id)
        if review.reviewer_agent_id != reviewer_agent_id:
            raise AuthorizationError("reviewer does not own review")
        if review.status != ReviewStatus.PENDING.value:
            raise ValidationError("review is already completed")
        status_value = _state_value(status)
        if status_value not in {
            ReviewStatus.APPROVED.value,
            ReviewStatus.CHANGES_REQUESTED.value,
            ReviewStatus.REJECTED.value,
        }:
            raise ValidationError("unsupported review decision: %s" % status_value)
        if status_value == ReviewStatus.APPROVED.value and evidence_id is None:
            raise ValidationError("approving a review requires an evidence_id")
        if evidence_id is not None:
            evidence = self._get_evidence(evidence_id)
            if evidence.task_id != review.task_id:
                raise ValidationError("review evidence must belong to reviewed task")
            # mac-5u1f: an APPROVED review must point at a real signed
            # review_verdict authored by the reviewer, not at any
            # task-attached evidence (which would let the executor's
            # own evidence be passed off as the verdict). Read the
            # executor_evidence_id off the verdict's own manifest and
            # ask the full verdict finder to verify shape + signature.
            if (
                status_value == ReviewStatus.APPROVED.value
                and self._find_verdict_evidence is not None
            ):
                manifest = (
                    evidence.metadata.get("verification")
                    if isinstance(evidence.metadata, dict)
                    else None
                )
                executor_evidence_id_from_manifest = None
                if isinstance(manifest, dict):
                    executor_evidence_id_from_manifest = str(
                        manifest.get("reviewed_evidence_id") or ""
                    ).strip() or None
                if not executor_evidence_id_from_manifest:
                    raise ValidationError(
                        "review approval evidence %s is not a review_verdict "
                        "(missing verification.reviewed_evidence_id)" % evidence_id
                    )
                verdict, problems = self._find_verdict_evidence(
                    review.task_id,
                    reviewer_agent_id,
                    executor_evidence_id=executor_evidence_id_from_manifest,
                    verdict_evidence_id=evidence_id,
                    not_before=review.created_at,
                )
                if verdict is None:
                    raise ValidationError(
                        "review approval requires signed review_verdict evidence: %s"
                        % ("; ".join(problems) if problems else "no verdict found")
                    )
        now = utcnow()
        # mac-p5a4: the review status UPDATE and the task.review_completed
        # history row were two bare store.execute calls — a crash between
        # them left the review APPROVED with no audit row. Wrap them in
        # one transaction so either both land or neither does.
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE reviews
                SET status = ?, reason = ?, evidence_id = ?, completed_at = ?
                WHERE id = ?
                """,
                (status_value, reason, evidence_id, now, review_id),
            )
            self._record_history(
                review.task_id,
                "task.review_completed",
                reviewer_agent_id,
                None,
                None,
                {"review_id": review_id, "status": status_value, "reason": reason},
                conn=conn,
            )
        if status_value in {
            ReviewStatus.CHANGES_REQUESTED.value,
            ReviewStatus.REJECTED.value,
        }:
            task = self._get_task(review.task_id)
            target = (
                TaskState.FAILED.value
                if task.attempt_count >= task.max_attempts
                else TaskState.OPEN.value
            )
            self._transition_task(
                review.task_id,
                target,
                reviewer_agent_id,
                {
                    "review_id": review_id,
                    "review_status": status_value,
                    "reason": "review rejected after max attempts"
                    if target == TaskState.FAILED.value
                    else "review rejected",
                },
            )
        return self.get_review(review_id)

    def get_review(self, review_id: str) -> Review:
        row = self.store.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if row is None:
            raise NotFoundError("review not found: %s" % review_id)
        return self._review_from_row(row)

    def list_reviews(self, task_id: str) -> List[Review]:
        rows = self.store.query_all(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        )
        return [self._review_from_row(row) for row in rows]

    # Publication -------------------------------------------------------

    def publish_task(
        self,
        task_id: str,
        target: str,
        created_by: str,
        evidence_id: Optional[str] = None,
    ) -> Publication:
        task = self._get_task(task_id)
        if task.state != TaskState.REVIEWING.value:
            raise TransitionError("task must be in review before publication")
        if not self.completion_authorized(task_id):
            raise ValidationError("publication requires approved review and evidence")
        content_hash = None
        requires_pub_evidence = self.task_requires_publication_evidence(task)
        if requires_pub_evidence and evidence_id is None:
            raise ValidationError("publication policy requires publication evidence")
        if evidence_id is not None:
            evidence = self._get_evidence(evidence_id)
            if evidence.task_id != task_id:
                raise ValidationError("publication evidence must belong to task")
            if requires_pub_evidence:
                if evidence.kind != "publication":
                    raise ValidationError("publication policy requires publication evidence")
                if not evidence.checksum:
                    raise ValidationError("publication evidence requires a checksum")
            content_hash = evidence.checksum
        owner_agent_id = task.owner_agent_id
        now = utcnow()
        publication_id = new_id("pub")
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET state = ?, owner_agent_id = NULL, lease_id = NULL, leased_until = NULL,
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (TaskState.COMPLETED.value, now, now, task_id, TaskState.REVIEWING.value),
            )
            if cursor.rowcount != 1:
                raise TransitionError("task state changed during publish; retry")
            conn.execute(
                """
                INSERT INTO publications (id, task_id, target, status, evidence_id, content_hash, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    task_id,
                    target,
                    PublicationStatus.PUBLISHED.value,
                    evidence_id,
                    content_hash,
                    created_by,
                    now,
                ),
            )
            self._record_history(
                task_id,
                "task.published",
                created_by,
                None,
                None,
                {"publication_id": publication_id, "target": target},
                conn=conn,
            )
            self._record_history(
                task_id,
                "task.transitioned",
                created_by,
                TaskState.REVIEWING.value,
                TaskState.COMPLETED.value,
                {"publication_id": publication_id},
                conn=conn,
            )
            if owner_agent_id:
                conn.execute(
                    "UPDATE agents SET status = ?, current_task_id = NULL, updated_at = ? WHERE id = ?",
                    (AgentStatus.IDLE.value, now, owner_agent_id),
                )
        return self.get_publication(publication_id)

    def get_publication(self, publication_id: str) -> Publication:
        row = self.store.query_one(
            "SELECT * FROM publications WHERE id = ?", (publication_id,)
        )
        if row is None:
            raise NotFoundError("publication not found: %s" % publication_id)
        return self._publication_from_row(row)

    def list_publications(self, task_id: Optional[str] = None) -> List[Publication]:
        if task_id is not None:
            self._get_task(task_id)
            rows = self.store.query_all(
                "SELECT * FROM publications WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM publications ORDER BY created_at, id"
            )
        return [self._publication_from_row(row) for row in rows]

    # Authorization helpers --------------------------------------------

    def completion_authorized(self, task_id: str) -> bool:
        """Approved review must reference evidence that belongs to the
        same task. Completion needs not just *some* approval and *some*
        evidence, but a documented link between them."""
        approved = self.store.query_one(
            """
            SELECT r.id FROM reviews r
            JOIN evidence e ON e.id = r.evidence_id AND e.task_id = r.task_id
            WHERE r.task_id = ? AND r.status = ?
            LIMIT 1
            """,
            (task_id, ReviewStatus.APPROVED.value),
        )
        return approved is not None

    def agent_has_owned_task(self, task_id: str, agent_id: str) -> bool:
        task = self._get_task(task_id)
        if task.owner_agent_id == agent_id:
            return True
        prior = self.store.query_one(
            "SELECT 1 FROM leases WHERE task_id = ? AND agent_id = ? LIMIT 1",
            (task_id, agent_id),
        )
        return prior is not None

    def agent_is_current_owner_or_latest_evidence_author(
        self, task_id: str, agent_id: str
    ) -> bool:
        task = self._get_task(task_id)
        if task.owner_agent_id == agent_id:
            return True
        return self.latest_executor_evidence_author(task_id) == agent_id

    def latest_executor_evidence_author(self, task_id: str) -> Optional[str]:
        rows = self.store.query_all(
            """
            SELECT created_by, metadata FROM evidence
            WHERE task_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (task_id,),
        )
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (TypeError, ValueError):
                metadata = {}
            verification = metadata.get("verification") if isinstance(metadata, dict) else {}
            if isinstance(verification, dict):
                evidence_type = str(verification.get("evidence_type") or "").strip().lower()
                if evidence_type == "review_verdict":
                    continue
            return str(row["created_by"])
        return None

    def task_requires_publication_evidence(self, task: Task) -> bool:
        policy = task.metadata.get("policy") or {}
        if not isinstance(policy, dict):
            return False
        return bool(
            policy.get("require_publication_evidence")
            or policy.get("publication_evidence_required")
        )

    # Row hydration ----------------------------------------------------

    def _review_from_row(self, row: Any) -> Review:
        return Review(
            row["id"],
            row["task_id"],
            row["reviewer_agent_id"],
            row["status"],
            row["reason"],
            row["evidence_id"],
            row["created_at"],
            row["completed_at"],
        )

    def _publication_from_row(self, row: Any) -> Publication:
        return Publication(
            row["id"],
            row["task_id"],
            row["target"],
            row["status"],
            row["evidence_id"],
            row["content_hash"],
            row["created_by"],
            row["created_at"],
        )
