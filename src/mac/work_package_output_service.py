"""Durably publish controller-observed work-package attempt outputs.

Worker-authored evidence identifies an attempt, but it is not repository
authority.  This service takes one immutable attribution row, resolves the
repository and plan from controller-owned tables, observes the exact protected
ref outside a database transaction, then re-locks the package and appends an
immutable verification receipt only if every decision input is unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from mac.models import (
    EvidenceAttemptVerification,
    JsonDict,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
)
from mac.repository_contract import resolve_repository_canonical_remote
from mac.store import Store
from mac.work_package_output import (
    AttemptOutputObservation,
    GitAttemptOutputVerifier,
)


WORK_PACKAGE_OUTPUT_SERVICE_VERSION = "work-package-output-service-v1"
_RECEIPT_SCHEMA = "mac.work_package.output_verification.v1"
_FULL_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class AttemptVerificationResult:
    verification: EvidenceAttemptVerification
    candidate_id: str
    created: bool

    def to_dict(self) -> JsonDict:
        return {
            "verification": self.verification.to_dict(),
            "candidate_id": self.candidate_id,
            "created": self.created,
        }


@dataclass(frozen=True)
class _VerificationContext:
    evidence_id: str
    task_id: str
    lease_id: str
    agent_id: str
    attempt_number: int
    attempt_ref: str
    attempt_base_ref: str
    attempt_base_sha: str
    attempt_head_sha: str
    protected_ref: bool
    declared_effects_digest: str
    package_id: str
    plan_version: int
    epoch: int
    node_key: str
    node_generation: int
    candidate_id: str
    candidate_status: str
    node_state: str
    package_state: str
    current_plan_version: int
    current_epoch: int
    epoch_status: str
    task_state: str
    task_attempt_count: int
    task_lease_id: Optional[str]
    lease_status: str
    repository_id: str
    repository_source: str
    repository_enabled: bool
    resource_namespace: JsonDict
    declared_effects: JsonDict

    def decision_identity(self) -> Tuple[Any, ...]:
        """All mutable or externally meaningful inputs to observation."""

        return (
            self.evidence_id,
            self.task_id,
            self.lease_id,
            self.agent_id,
            self.attempt_number,
            self.attempt_ref,
            self.attempt_base_ref,
            self.attempt_base_sha,
            self.attempt_head_sha,
            self.protected_ref,
            self.declared_effects_digest,
            self.package_id,
            self.plan_version,
            self.epoch,
            self.node_key,
            self.node_generation,
            self.candidate_id,
            self.candidate_status,
            self.node_state,
            self.package_state,
            self.current_plan_version,
            self.current_epoch,
            self.epoch_status,
            self.task_state,
            self.task_attempt_count,
            self.task_lease_id,
            self.lease_status,
            self.repository_id,
            self.repository_source,
            self.repository_enabled,
            json_dumps(self.resource_namespace),
            json_dumps(self.declared_effects),
        )


class WorkPackageOutputService:
    """Verify and receipt one exact, currently submitted package candidate."""

    def __init__(
        self,
        store: Store,
        *,
        verifier: Optional[GitAttemptOutputVerifier] = None,
    ) -> None:
        self.store = store
        self.verifier = verifier or GitAttemptOutputVerifier()

    def verify(self, evidence_id: str) -> AttemptVerificationResult:
        evidence_value = str(evidence_id or "").strip()
        if not evidence_value:
            raise ValidationError("attempt output verification evidence id is required")

        # One SELECT gives the observer an internally coherent controller
        # snapshot without holding a database transaction across Git/network IO.
        before = self._load_context(evidence_value)
        existing = self.store.query_one(
            "SELECT * FROM evidence_attempt_verifications WHERE evidence_id = ?",
            (evidence_value,),
        )
        if existing is not None:
            self._require_receipt_matches_context(existing, before)
            return AttemptVerificationResult(
                verification=self._verification_from_row(existing),
                candidate_id=before.candidate_id,
                created=False,
            )

        try:
            observation = self.verifier.observe(
                {
                    # This source is intentionally read only from the durable
                    # repository registry, never evidence/task metadata.
                    "id": before.repository_id,
                    "source": before.repository_source,
                },
                attempt_ref=before.attempt_ref,
                base_sha=before.attempt_base_sha,
                attempt_base_ref=before.attempt_base_ref,
                declared_effects=before.declared_effects,
                resource_namespace=before.resource_namespace,
            )
        except Exception as exc:
            # Git diagnostics can contain credential-helper or authenticated
            # transport details.  The concrete verifier already suppresses
            # stdout/stderr; this boundary also prevents alternative verifier
            # implementations from leaking their exception text.
            raise ValidationError(
                "controller attempt output observation failed"
            ) from exc
        self._require_observation_matches_context(observation, before)

        receipt_payload = self._receipt_payload(before, observation)
        receipt_digest = (
            "sha256:%s"
            % hashlib.sha256(json_dumps(receipt_payload).encode("utf-8")).hexdigest()
        )
        receipt_id = "wpverify_%s" % receipt_digest.removeprefix("sha256:")

        with self.store.transaction() as conn:
            # This no-op update is the backend-neutral package row lock used by
            # the other package coordinators. It serializes receipt publication
            # with epoch swaps and candidate transitions.
            locked = conn.execute(
                "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
                (before.package_id,),
            )
            if locked.rowcount != 1:
                raise TransitionError("attempt output work package disappeared")
            repository_lock = conn.execute(
                "UPDATE project_repositories SET updated_at = updated_at WHERE id = ?",
                (before.repository_id,),
            )
            if repository_lock.rowcount != 1:
                raise TransitionError(
                    "attempt output repository registration disappeared"
                )
            after = self._load_context(evidence_value, conn=conn)
            if after.decision_identity() != before.decision_identity():
                raise TransitionError(
                    "attempt output verification context changed during observation"
                )
            self._require_observation_matches_context(observation, after)

            duplicate = conn.execute(
                "SELECT * FROM evidence_attempt_verifications WHERE evidence_id = ?",
                (evidence_value,),
            ).fetchone()
            if duplicate is not None:
                self._require_receipt_matches_context(duplicate, after)
                self._require_receipt_matches_observation(duplicate, observation)
                return AttemptVerificationResult(
                    verification=self._verification_from_row(duplicate),
                    candidate_id=after.candidate_id,
                    created=False,
                )

            conn.execute(
                "INSERT INTO evidence_attempt_verifications ("
                "id, evidence_id, task_id, lease_id, agent_id, attempt_number, "
                "repository_id, attempt_ref, attempt_base_sha, attempt_head_sha, "
                "tree_digest, declared_effects_digest, observed_effects_digest, "
                "changed_paths, changes, verifier, verifier_version, verified_at, "
                "receipt_digest"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    after.evidence_id,
                    after.task_id,
                    after.lease_id,
                    after.agent_id,
                    after.attempt_number,
                    after.repository_id,
                    after.attempt_ref,
                    after.attempt_base_sha,
                    after.attempt_head_sha,
                    observation.tree_digest,
                    after.declared_effects_digest,
                    observation.observed_effects_digest,
                    json_dumps(list(observation.changed_paths)),
                    json_dumps([change.to_dict() for change in observation.changes]),
                    "git-attempt-output",
                    observation.verifier,
                    observation.verified_at,
                    receipt_digest,
                ),
            )
            row = conn.execute(
                "SELECT * FROM evidence_attempt_verifications WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - committed INSERT invariant.
                raise TransitionError("attempt output receipt insert disappeared")
            return AttemptVerificationResult(
                verification=self._verification_from_row(row),
                candidate_id=after.candidate_id,
                created=True,
            )

    def _load_context(
        self, evidence_id: str, *, conn: Any = None
    ) -> _VerificationContext:
        execute = conn.execute if conn is not None else self.store.query_one
        sql = """
            SELECT
                attempt.evidence_id,
                attempt.task_id,
                attempt.lease_id,
                attempt.agent_id,
                attempt.attempt_number,
                attempt.attempt_ref,
                attempt.attempt_base_sha,
                attempt.attempt_head_sha,
                attempt.protected_ref,
                attempt.declared_effects_digest,
                assignment.attempt_base_ref,
                assignment.package_id,
                assignment.plan_version,
                assignment.epoch,
                assignment.node_key,
                link.node_generation,
                link.node_state,
                candidate.id AS candidate_id,
                candidate.status AS candidate_status,
                package.state AS package_state,
                package.current_plan_version,
                package.current_epoch,
                epoch.status AS epoch_status,
                epoch.planning_base_ref AS epoch_base_ref,
                epoch.planning_base_sha AS epoch_base_sha,
                plan.definition AS plan_definition,
                task.state AS task_state,
                task.attempt_count AS task_attempt_count,
                task.lease_id AS task_lease_id,
                lease.status AS lease_status,
                repository.id AS repository_id,
                repository.source AS repository_source,
                repository.metadata AS repository_metadata,
                repository.enabled AS repository_enabled
            FROM evidence_attempt_links AS attempt
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
            JOIN work_package_node_candidates AS candidate
              ON candidate.evidence_id = attempt.evidence_id
             AND candidate.task_id = assignment.task_id
             AND candidate.package_id = assignment.package_id
             AND candidate.plan_version = assignment.plan_version
             AND candidate.epoch = assignment.epoch
             AND candidate.node_key = assignment.node_key
             AND candidate.node_generation = link.node_generation
             AND candidate.assignment_lease_id = assignment.lease_id
             AND candidate.attempt_number = assignment.attempt_number
            JOIN work_packages AS package ON package.id = assignment.package_id
            JOIN work_package_epochs AS epoch
              ON epoch.package_id = assignment.package_id
             AND epoch.plan_version = assignment.plan_version
             AND epoch.epoch = assignment.epoch
            JOIN work_package_plan_versions AS plan
              ON plan.package_id = assignment.package_id
             AND plan.version = assignment.plan_version
            JOIN tasks AS task ON task.id = assignment.task_id
            JOIN leases AS lease
              ON lease.id = assignment.lease_id
             AND lease.task_id = assignment.task_id
             AND lease.agent_id = assignment.agent_id
            JOIN project_repositories AS repository
              ON repository.id = package.repository_id
            WHERE attempt.evidence_id = ?
        """
        result = execute(sql, (evidence_id,))
        row = result.fetchone() if conn is not None else result
        if row is None:
            raise ValidationError(
                "attempt evidence is not bound to one exact submitted package candidate"
            )
        return self._context_from_row(row)

    @staticmethod
    def _context_from_row(row: Mapping[str, Any]) -> _VerificationContext:
        head = str(row["attempt_head_sha"] or "").strip().lower()
        if not head:
            raise ValidationError(
                "attempt evidence does not declare the exact protected-ref head"
            )
        definition = _json_object(row["plan_definition"], "work-package plan")
        nodes = definition.get("nodes")
        if not isinstance(nodes, list):
            raise ValidationError("work-package plan has no node list")
        matches = [
            item
            for item in nodes
            if isinstance(item, Mapping) and item.get("node_key") == row["node_key"]
        ]
        if len(matches) != 1:
            raise ValidationError("work-package assignment does not name one plan node")
        node = matches[0]
        effects = node.get("effects")
        namespace = definition.get("resource_namespace")
        if not isinstance(effects, Mapping) or not isinstance(namespace, Mapping):
            raise ValidationError("work-package output scope is malformed")

        try:
            canonical = resolve_repository_canonical_remote(
                {
                    "id": row["repository_id"],
                    "source": row["repository_source"],
                    "metadata": row["repository_metadata"],
                }
            )
        except ValueError as exc:
            raise ValidationError(
                "work-package repository canonical remote is invalid"
            ) from exc
        context = _VerificationContext(
            evidence_id=str(row["evidence_id"]),
            task_id=str(row["task_id"]),
            lease_id=str(row["lease_id"]),
            agent_id=str(row["agent_id"]),
            attempt_number=int(row["attempt_number"]),
            attempt_ref=str(row["attempt_ref"]),
            attempt_base_ref=str(row["attempt_base_ref"]),
            attempt_base_sha=str(row["attempt_base_sha"]).lower(),
            attempt_head_sha=head,
            protected_ref=bool(row["protected_ref"]),
            declared_effects_digest=str(row["declared_effects_digest"] or ""),
            package_id=str(row["package_id"]),
            plan_version=int(row["plan_version"]),
            epoch=int(row["epoch"]),
            node_key=str(row["node_key"]),
            node_generation=int(row["node_generation"]),
            candidate_id=str(row["candidate_id"]),
            candidate_status=str(row["candidate_status"]),
            node_state=str(row["node_state"]),
            package_state=str(row["package_state"]),
            current_plan_version=int(row["current_plan_version"]),
            current_epoch=int(row["current_epoch"]),
            epoch_status=str(row["epoch_status"]),
            task_state=str(row["task_state"]),
            task_attempt_count=int(row["task_attempt_count"]),
            task_lease_id=(
                str(row["task_lease_id"]) if row["task_lease_id"] is not None else None
            ),
            lease_status=str(row["lease_status"]),
            repository_id=str(row["repository_id"]),
            repository_source=canonical.url,
            repository_enabled=bool(row["repository_enabled"]),
            resource_namespace=dict(namespace),
            declared_effects=dict(effects),
        )
        WorkPackageOutputService._require_current_context(
            context, row, definition, node
        )
        return context

    @staticmethod
    def _require_current_context(
        context: _VerificationContext,
        row: Mapping[str, Any],
        definition: Mapping[str, Any],
        node: Mapping[str, Any],
    ) -> None:
        if (
            context.package_state != "active"
            or context.current_plan_version != context.plan_version
            or context.current_epoch != context.epoch
            or context.epoch_status != "active"
            or context.candidate_status != "submitted"
            or context.node_state != "candidate_submitted"
        ):
            raise TransitionError(
                "attempt output candidate is not submitted in the current active epoch"
            )
        if not context.repository_enabled or not context.repository_source:
            raise ValidationError(
                "work-package repository is not enabled and registered"
            )
        if not context.protected_ref or not context.attempt_ref.startswith(
            "refs/mac/attempts/"
        ):
            raise ValidationError("attempt output is not an attributed protected ref")
        if not _FULL_OBJECT_ID.fullmatch(context.attempt_base_sha) or not (
            _FULL_OBJECT_ID.fullmatch(context.attempt_head_sha)
        ):
            raise ValidationError(
                "attempt output attribution has an invalid Git object id"
            )
        if context.task_attempt_count != context.attempt_number:
            raise TransitionError("attempt output is not the task's current attempt")
        if context.task_state not in {
            "claimed",
            "running",
            "needs_review",
            "reviewing",
        }:
            raise TransitionError("attempt output task is not reviewable")
        if context.lease_status not in {"active", "released"}:
            raise TransitionError("attempt output lease is no longer reviewable")
        if (
            context.lease_status == "active"
            and context.task_lease_id != context.lease_id
        ) or (context.lease_status == "released" and context.task_lease_id is not None):
            raise TransitionError("attempt output lease attachment is incoherent")
        if (
            definition.get("package_id") != context.package_id
            or definition.get("repository_id") != context.repository_id
            or definition.get("planning_base_ref") != row["epoch_base_ref"]
            or str(definition.get("planning_base_sha") or "").lower()
            != str(row["epoch_base_sha"] or "").lower()
            or context.attempt_base_ref != row["epoch_base_ref"]
            or context.attempt_base_sha != str(row["epoch_base_sha"] or "").lower()
            or node.get("effects_digest") != context.declared_effects_digest
        ):
            raise ValidationError(
                "attempt output assignment does not match its immutable plan epoch"
            )

    @staticmethod
    def _require_observation_matches_context(
        observation: AttemptOutputObservation, context: _VerificationContext
    ) -> None:
        if not isinstance(observation, AttemptOutputObservation):
            raise ValidationError(
                "controller output verifier returned a malformed result"
            )
        if (
            observation.repository_id != context.repository_id
            or observation.attempt_ref != context.attempt_ref
            or observation.base_sha.lower() != context.attempt_base_sha
            or observation.head_sha.lower() != context.attempt_head_sha
        ):
            raise ValidationError(
                "controller observation does not match the exact attributed attempt"
            )
        if not observation.verifier or not observation.verified_at:
            raise ValidationError(
                "controller output observation lacks verifier identity"
            )

    @staticmethod
    def _receipt_payload(
        context: _VerificationContext, observation: AttemptOutputObservation
    ) -> JsonDict:
        return {
            "schema": _RECEIPT_SCHEMA,
            "service_version": WORK_PACKAGE_OUTPUT_SERVICE_VERSION,
            "evidence_id": context.evidence_id,
            "task_id": context.task_id,
            "lease_id": context.lease_id,
            "agent_id": context.agent_id,
            "attempt_number": context.attempt_number,
            "package_id": context.package_id,
            "plan_version": context.plan_version,
            "epoch": context.epoch,
            "node_key": context.node_key,
            "node_generation": context.node_generation,
            "candidate_id": context.candidate_id,
            "repository_id": context.repository_id,
            "attempt_ref": context.attempt_ref,
            "attempt_base_sha": context.attempt_base_sha,
            "attempt_head_sha": context.attempt_head_sha,
            "tree_digest": observation.tree_digest,
            "declared_effects_digest": context.declared_effects_digest,
            "observed_effects_digest": observation.observed_effects_digest,
            "changed_paths": list(observation.changed_paths),
            "changes": [change.to_dict() for change in observation.changes],
            "verifier": "git-attempt-output",
            "verifier_version": observation.verifier,
            "verified_at": observation.verified_at,
        }

    @staticmethod
    def _require_receipt_matches_context(
        row: Mapping[str, Any], context: _VerificationContext
    ) -> None:
        actual = (
            str(row["evidence_id"]),
            str(row["task_id"]),
            str(row["lease_id"]),
            str(row["agent_id"]),
            int(row["attempt_number"]),
            str(row["repository_id"]),
            str(row["attempt_ref"]),
            str(row["attempt_base_sha"]).lower(),
            str(row["attempt_head_sha"]).lower(),
            str(row["declared_effects_digest"]),
        )
        expected = (
            context.evidence_id,
            context.task_id,
            context.lease_id,
            context.agent_id,
            context.attempt_number,
            context.repository_id,
            context.attempt_ref,
            context.attempt_base_sha,
            context.attempt_head_sha,
            context.declared_effects_digest,
        )
        if actual != expected:
            raise TransitionError(
                "existing output receipt does not match current attempt"
            )

    @staticmethod
    def _require_receipt_matches_observation(
        row: Mapping[str, Any], observation: AttemptOutputObservation
    ) -> None:
        if (
            str(row["tree_digest"]) != observation.tree_digest
            or str(row["observed_effects_digest"])
            != observation.observed_effects_digest
            or _json_list(row["changed_paths"], "receipt changed paths")
            != list(observation.changed_paths)
            or _json_list(row["changes"], "receipt changes")
            != [change.to_dict() for change in observation.changes]
            or str(row["verifier_version"]) != observation.verifier
        ):
            raise TransitionError(
                "concurrent output receipt conflicts with controller observation"
            )

    @staticmethod
    def _verification_from_row(row: Mapping[str, Any]) -> EvidenceAttemptVerification:
        return EvidenceAttemptVerification(
            id=str(row["id"]),
            evidence_id=str(row["evidence_id"]),
            task_id=str(row["task_id"]),
            lease_id=str(row["lease_id"]),
            agent_id=str(row["agent_id"]),
            attempt_number=int(row["attempt_number"]),
            repository_id=str(row["repository_id"]),
            attempt_ref=str(row["attempt_ref"]),
            attempt_base_sha=str(row["attempt_base_sha"]),
            attempt_head_sha=str(row["attempt_head_sha"]),
            tree_digest=str(row["tree_digest"]),
            declared_effects_digest=str(row["declared_effects_digest"]),
            observed_effects_digest=str(row["observed_effects_digest"]),
            changed_paths=[
                str(item)
                for item in _json_list(row["changed_paths"], "receipt changed paths")
            ],
            changes=[
                dict(item)
                for item in _json_list(row["changes"], "receipt changes")
                if isinstance(item, Mapping)
            ],
            verifier=str(row["verifier"]),
            verifier_version=str(row["verifier_version"]),
            verified_at=str(row["verified_at"]),
            receipt_digest=str(row["receipt_digest"]),
        )


def _json_object(value: Any, label: str) -> JsonDict:
    decoded = json_loads(value, {}) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValidationError("%s is malformed" % label)
    return dict(decoded)


def _json_list(value: Any, label: str) -> list[Any]:
    decoded = json_loads(value, []) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise ValidationError("%s is malformed" % label)
    return decoded


__all__ = [
    "AttemptVerificationResult",
    "WORK_PACKAGE_OUTPUT_SERVICE_VERSION",
    "WorkPackageOutputService",
]
