"""Candidate-buffer transitions for controller-managed work packages.

Execution evidence is first attributed to an immutable assignment attempt.
This service converts that evidence into an immutable candidate identity and
transfers product WIP from the mutation station to the candidate buffer.  It
does not trust or verify the worker's claimed head; controller observation and
review acceptance are deliberately separate downstream gates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    WorkPackageNodeCandidate,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.store import Store


WORK_PACKAGE_CANDIDATE_SERVICE_VERSION = "work-package-candidate-service-v1"


@dataclass(frozen=True)
class CandidateSubmissionResult:
    candidate: WorkPackageNodeCandidate
    transferred_wip_token_ids: Tuple[str, ...]
    created: bool

    def to_dict(self) -> JsonDict:
        return {
            "candidate": self.candidate.to_dict(),
            "transferred_wip_token_ids": list(self.transferred_wip_token_ids),
            "created": self.created,
        }


class WorkPackageCandidateService:
    """Move exact attempt evidence into a bounded candidate buffer."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def submit(self, evidence_id: str, *, actor: str) -> CandidateSubmissionResult:
        evidence_value = str(evidence_id or "").strip()
        actor_value = str(actor or "").strip()
        if not evidence_value:
            raise ValidationError("candidate submission evidence id is required")
        if not actor_value:
            raise ValidationError("candidate submission actor is required")

        existing = self.store.query_one(
            "SELECT * FROM work_package_node_candidates WHERE evidence_id = ?",
            (evidence_value,),
        )
        if existing is not None:
            return CandidateSubmissionResult(
                candidate=self._candidate_from_row(existing),
                transferred_wip_token_ids=self._candidate_buffer_tokens(existing),
                created=False,
            )

        now = utcnow()
        transferred: list[str] = []
        with self.store.transaction() as conn:
            context = conn.execute(
                """
                SELECT
                    attempt.evidence_id,
                    attempt.task_id,
                    attempt.lease_id,
                    attempt.agent_id,
                    attempt.attempt_number,
                    attempt.attempt_ref,
                    attempt.attempt_base_sha,
                    attempt.declared_effects_digest,
                    assignment.package_id,
                    assignment.plan_version,
                    assignment.epoch,
                    assignment.node_key,
                    link.node_generation,
                    link.node_state,
                    package.state AS package_state,
                    package.current_plan_version,
                    package.current_epoch,
                    epoch.status AS epoch_status,
                    task.state AS task_state,
                    task.attempt_count,
                    task.lease_id AS task_lease_id,
                    lease.status AS lease_status,
                    plan.definition AS plan_definition
                FROM evidence_attempt_links AS attempt
                JOIN evidence AS evidence
                  ON evidence.id = attempt.evidence_id
                 AND evidence.task_id = attempt.task_id
                JOIN work_package_assignment_audit AS assignment
                  ON assignment.lease_id = attempt.lease_id
                 AND assignment.task_id = attempt.task_id
                 AND assignment.agent_id = attempt.agent_id
                 AND assignment.attempt_number = attempt.attempt_number
                 AND assignment.attempt_ref = attempt.attempt_ref
                 AND assignment.attempt_base_sha = attempt.attempt_base_sha
                 AND assignment.declared_effects_digest = attempt.declared_effects_digest
                JOIN work_package_task_links AS link
                  ON link.task_id = assignment.task_id
                 AND link.package_id = assignment.package_id
                 AND link.plan_version = assignment.plan_version
                 AND link.epoch = assignment.epoch
                 AND link.node_key = assignment.node_key
                 AND link.declared_effects_digest = assignment.declared_effects_digest
                JOIN work_packages AS package ON package.id = assignment.package_id
                JOIN work_package_epochs AS epoch
                  ON epoch.package_id = assignment.package_id
                 AND epoch.plan_version = assignment.plan_version
                 AND epoch.epoch = assignment.epoch
                JOIN work_package_plan_versions AS plan
                  ON plan.package_id = assignment.package_id
                 AND plan.version = assignment.plan_version
                JOIN tasks AS task ON task.id = assignment.task_id
                JOIN leases AS lease ON lease.id = assignment.lease_id
                WHERE attempt.evidence_id = ?
                """,
                (evidence_value,),
            ).fetchone()
            if context is None:
                raise ValidationError(
                    "candidate evidence is not bound to an exact work-package assignment"
                )

            package_id = str(context["package_id"])
            package_lock = conn.execute(
                "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
                (package_id,),
            )
            if package_lock.rowcount != 1:
                raise TransitionError("candidate work package disappeared")
            # Re-read after the package lock. The first join is only discovery;
            # these predicates are the authoritative transaction decision.
            current = conn.execute(
                "SELECT package.state AS package_state, "
                "package.current_plan_version, package.current_epoch, "
                "epoch.status AS epoch_status, link.node_state, "
                "task.state AS task_state, task.attempt_count, "
                "task.lease_id AS task_lease_id, lease.status AS lease_status "
                "FROM work_packages AS package "
                "JOIN work_package_epochs AS epoch ON epoch.package_id = package.id "
                "AND epoch.plan_version = ? AND epoch.epoch = ? "
                "JOIN work_package_task_links AS link ON link.package_id = package.id "
                "AND link.plan_version = ? AND link.epoch = ? AND link.task_id = ? "
                "JOIN tasks AS task ON task.id = link.task_id "
                "JOIN leases AS lease ON lease.id = ? "
                "WHERE package.id = ?",
                (
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    context["task_id"],
                    context["lease_id"],
                    package_id,
                ),
            ).fetchone()
            if current is None:
                raise TransitionError("candidate assignment changed during submission")
            if (
                current["package_state"] != "active"
                or int(current["current_plan_version"]) != int(context["plan_version"])
                or int(current["current_epoch"]) != int(context["epoch"])
                or current["epoch_status"] != "active"
                or current["node_state"] != "executing"
                or int(current["attempt_count"]) != int(context["attempt_number"])
                or current["task_state"]
                not in {"claimed", "running", "needs_review", "reviewing"}
                or current["lease_status"] not in {"active", "released"}
                or (
                    current["lease_status"] == "active"
                    and current["task_lease_id"] != context["lease_id"]
                )
                or (
                    current["lease_status"] == "released"
                    and current["task_lease_id"] is not None
                )
            ):
                raise TransitionError(
                    "candidate assignment is not current and reviewable"
                )
            if not str(context["attempt_ref"]).startswith("refs/mac/attempts/"):
                raise ValidationError("candidate attempt ref is not in the protected namespace")

            duplicate = conn.execute(
                "SELECT * FROM work_package_node_candidates WHERE evidence_id = ?",
                (evidence_value,),
            ).fetchone()
            if duplicate is not None:
                return CandidateSubmissionResult(
                    candidate=self._candidate_from_row(duplicate),
                    transferred_wip_token_ids=self._candidate_buffer_tokens(
                        duplicate, conn=conn
                    ),
                    created=False,
                )

            candidate_id = self._candidate_id(context)
            conn.execute(
                "INSERT INTO work_package_node_candidates ("
                "id, task_id, package_id, plan_version, epoch, node_key, "
                "node_generation, assignment_lease_id, attempt_number, evidence_id, "
                "status, submitted_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    context["task_id"],
                    package_id,
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    context["node_key"],
                    int(context["node_generation"]),
                    context["lease_id"],
                    int(context["attempt_number"]),
                    evidence_value,
                    "submitted",
                    now,
                ),
            )

            node = self._plan_node(
                json_loads(context["plan_definition"], {}), str(context["node_key"])
            )
            held = conn.execute(
                "SELECT * FROM work_package_wip_tokens "
                "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
                "AND task_id = ? AND state = ? ORDER BY id",
                (
                    package_id,
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    context["task_id"],
                    "held",
                ),
            ).fetchall()
            if str(node.get("kind") or "mutation") == "mutation" and not held:
                raise TransitionError("mutation candidate has no held product WIP")
            for token in held:
                if token["stage"] != "mutation":
                    raise TransitionError(
                        "candidate submission found product WIP in the wrong stage"
                    )
                released = conn.execute(
                    "UPDATE work_package_wip_tokens SET state = ?, released_at = ?, "
                    "release_reason = ? WHERE id = ? AND state = ? AND stage = ?",
                    (
                        "released",
                        now,
                        "candidate_transfer:%s" % candidate_id,
                        token["id"],
                        "held",
                        "mutation",
                    ),
                )
                if released.rowcount != 1:
                    raise TransitionError("candidate WIP ownership changed during transfer")
                successor_id = self._successor_wip_id(str(token["id"]), candidate_id)
                conn.execute(
                    "INSERT INTO work_package_wip_tokens ("
                    "id, package_id, plan_version, epoch, node_key, task_id, "
                    "resource_key, token_kind, stage, state, generation, capacity_units, "
                    "reservation_key, predecessor_token_id, "
                    "acquired_by_assignment_lease_id, acquired_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        successor_id,
                        token["package_id"],
                        int(token["plan_version"]),
                        int(token["epoch"]),
                        token["node_key"],
                        token["task_id"],
                        token["resource_key"],
                        token["token_kind"],
                        "candidate_buffer",
                        "held",
                        int(token["generation"]) + 1,
                        int(token["capacity_units"]),
                        token["reservation_key"],
                        token["id"],
                        token["acquired_by_assignment_lease_id"],
                        now,
                    ),
                )
                transferred.append(successor_id)

            link_update = conn.execute(
                "UPDATE work_package_task_links SET node_state = ? "
                "WHERE task_id = ? AND package_id = ? AND plan_version = ? "
                "AND epoch = ? AND node_state = ?",
                (
                    "candidate_submitted",
                    context["task_id"],
                    package_id,
                    int(context["plan_version"]),
                    int(context["epoch"]),
                    "executing",
                ),
            )
            if link_update.rowcount != 1:
                raise TransitionError("candidate node state changed during submission")
            self._append_history(
                conn,
                package_id=package_id,
                plan_version=int(context["plan_version"]),
                epoch=int(context["epoch"]),
                actor=actor_value,
                event_type="work_package.candidate_submitted",
                detail={
                    "candidate_id": candidate_id,
                    "task_id": context["task_id"],
                    "evidence_id": evidence_value,
                    "assignment_lease_id": context["lease_id"],
                    "attempt_number": int(context["attempt_number"]),
                    "transferred_wip_token_ids": list(transferred),
                    "service_version": WORK_PACKAGE_CANDIDATE_SERVICE_VERSION,
                },
                now=now,
            )

        candidate = self.store.query_one(
            "SELECT * FROM work_package_node_candidates WHERE evidence_id = ?",
            (evidence_value,),
        )
        if candidate is None:  # pragma: no cover - committed transaction invariant.
            raise TransitionError("candidate submission committed without a candidate")
        return CandidateSubmissionResult(
            candidate=self._candidate_from_row(candidate),
            # Return the same canonical ordering on both the creating call and
            # an idempotent retry.  Successor ids are content-derived, so their
            # lexical order is not necessarily the predecessor insertion order.
            transferred_wip_token_ids=self._candidate_buffer_tokens(candidate),
            created=True,
        )

    def _candidate_buffer_tokens(
        self, candidate: Mapping[str, Any], *, conn: Any = None
    ) -> Tuple[str, ...]:
        query = conn.execute if conn is not None else self.store.query_all
        sql = (
            "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND task_id = ? "
            "AND stage = ? AND state = ? ORDER BY id"
        )
        params = (
            candidate["package_id"],
            int(candidate["plan_version"]),
            int(candidate["epoch"]),
            candidate["task_id"],
            "candidate_buffer",
            "held",
        )
        rows = query(sql, params)
        if conn is not None:
            rows = rows.fetchall()
        return tuple(str(row["id"]) for row in rows)

    @staticmethod
    def _plan_node(definition: Mapping[str, Any], node_key: str) -> Mapping[str, Any]:
        nodes = definition.get("nodes")
        if not isinstance(nodes, list):
            raise ValidationError("candidate plan has no node list")
        matches = [
            node
            for node in nodes
            if isinstance(node, Mapping) and node.get("node_key") == node_key
        ]
        if len(matches) != 1:
            raise ValidationError("candidate assignment does not name one plan node")
        return matches[0]

    @staticmethod
    def _candidate_id(context: Mapping[str, Any]) -> str:
        identity = "\x00".join(
            str(context[field])
            for field in (
                "package_id",
                "plan_version",
                "epoch",
                "node_key",
                "task_id",
                "lease_id",
                "attempt_number",
                "evidence_id",
            )
        )
        return "wpc_%s" % hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _successor_wip_id(predecessor_id: str, candidate_id: str) -> str:
        return "wpwip_%s" % hashlib.sha256(
            (predecessor_id + "\x00" + candidate_id).encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _append_history(
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        actor: str,
        event_type: str,
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
    def _candidate_from_row(row: Mapping[str, Any]) -> WorkPackageNodeCandidate:
        return WorkPackageNodeCandidate(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            package_id=str(row["package_id"]),
            plan_version=int(row["plan_version"]),
            epoch=int(row["epoch"]),
            node_key=str(row["node_key"]),
            node_generation=int(row["node_generation"]),
            assignment_lease_id=str(row["assignment_lease_id"]),
            attempt_number=int(row["attempt_number"]),
            evidence_id=str(row["evidence_id"]),
            status=str(row["status"]),
            submitted_at=str(row["submitted_at"]),
            accepted_at=row["accepted_at"],
            accepted_by=row["accepted_by"],
            rejection_reason=row["rejection_reason"],
        )


__all__ = [
    "CandidateSubmissionResult",
    "WORK_PACKAGE_CANDIDATE_SERVICE_VERSION",
    "WorkPackageCandidateService",
]
