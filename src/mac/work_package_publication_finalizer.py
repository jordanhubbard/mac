"""Exact-receipt product completion for landed work-package batches.

Landing and product completion are deliberately separate commits.  The landing
service proves the remote compare-and-swap with an append-only receipt; this
service consumes that exact receipt in one database transaction, releases the
batch's bounded product WIP, verifies controller-station provenance, and closes
the current package epoch.  It never calls the generic task transition API and
never infers completion from a mutable branch or batch state alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.store import Store


WORK_PACKAGE_PUBLICATION_FINALIZER_VERSION = "work-package-publication-finalizer-v1"
PUBLICATION_FINALIZATION_SCHEMA = "mac.work_package.publication_finalization.v1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTIONABLE_BATCH_STATES = frozenset(
    {"queued", "assembling", "verifying", "certified", "published"}
)
_TERMINAL_NODE_STATES = frozenset(
    {"candidate_accepted", "integrated", "certified", "superseded", "cancelled"}
)


class WorkPackagePublicationFinalizationError(RuntimeError):
    """Base fail-closed finalization error."""


class StalePublicationError(WorkPackagePublicationFinalizationError):
    """The published batch no longer belongs to the current package epoch."""


class PublicationReceiptError(WorkPackagePublicationFinalizationError):
    """Landing or controller-station provenance is absent or inconsistent."""


FaultHook = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class PublicationFinalizationResult:
    finalization_id: str
    batch_id: str
    landing_receipt_id: str
    package_id: str
    plan_version: int
    epoch: int
    certification_id: str
    integration_task_id: str
    certification_task_id: Optional[str]
    released_wip_ids: Tuple[str, ...]
    controller_station_receipt_ids: Tuple[str, ...]
    finalization_digest: str
    package_state: str
    epoch_status: str
    created: bool

    def to_dict(self) -> JsonDict:
        return {
            "schema": PUBLICATION_FINALIZATION_SCHEMA,
            "service_version": WORK_PACKAGE_PUBLICATION_FINALIZER_VERSION,
            # These station projections are computed only after the service has
            # re-read their append-only authorities in the same transaction.
            # They make the controller boundary explicit without making this
            # mutable response the source of truth.
            "status": "completed",
            "batch_state": "published",
            "provenance_verified": True,
            "integration_node_state": "integrated",
            "certification_node_state": (
                "certified" if self.certification_task_id is not None else None
            ),
            "integration_task_completed": True,
            "certification_task_completed": self.certification_task_id is not None,
            "held_wip_count": 0,
            "product_finalized": True,
            "finalization_id": self.finalization_id,
            "batch_id": self.batch_id,
            "landing_receipt_id": self.landing_receipt_id,
            "package_id": self.package_id,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "certification_id": self.certification_id,
            "integration_task_id": self.integration_task_id,
            "certification_task_id": self.certification_task_id,
            "released_wip_ids": list(self.released_wip_ids),
            "controller_station_receipt_ids": list(
                self.controller_station_receipt_ids
            ),
            "finalization_digest": self.finalization_digest,
            "package_state": self.package_state,
            "epoch_status": self.epoch_status,
            "created": self.created,
        }


class WorkPackagePublicationFinalizer:
    """Atomically turn one exact published batch into completed product state."""

    def __init__(
        self,
        store: Store,
        *,
        now: Optional[Callable[[], str]] = None,
        fault_hook: Optional[FaultHook] = None,
    ) -> None:
        self.store = store
        self._now = now or utcnow
        self._fault_hook = fault_hook or (lambda _stage, _detail: None)

    def finalize_landed_batch(
        self,
        batch_id: str,
        *,
        actor: str,
        receipt_id: Optional[str] = None,
    ) -> PublicationFinalizationResult:
        """Consume the unique landing receipt under the package serialization lock."""

        batch_value = self._required(batch_id, "publication batch id")
        actor_value = self._required(actor, "publication finalization actor")
        requested_receipt = (
            self._required(receipt_id, "landing receipt id")
            if receipt_id is not None
            else None
        )
        now = self._now()
        if not isinstance(now, str) or not now.strip():
            raise ValueError("publication finalizer clock must return an ISO timestamp")

        with self.store.transaction() as conn:
            batch = self._lock_batch(conn, batch_value)
            batch = self._lock_package_epoch(conn, batch)
            receipt = self._landing_provenance(conn, batch, requested_receipt)
            existing = conn.execute(
                "SELECT * FROM work_package_publication_finalizations "
                "WHERE batch_id = ?",
                (batch_value,),
            ).fetchone()
            if existing is not None:
                return self._idempotent_result(
                    conn,
                    batch=batch,
                    receipt=receipt,
                    row=existing,
                )

            self._require_current_published_context(batch)
            self._reject_competing_batch(conn, batch)
            definition = self._plan_definition(conn, batch)
            station = self._station_provenance(
                conn,
                batch=batch,
                receipt=receipt,
                definition=definition,
            )
            wip_rows = self._exact_held_integration_wip(conn, batch)
            released_ids = tuple(str(row["id"]) for row in wip_rows)
            release_reason = "publication_finalized:%s" % receipt["id"]

            self._fault(
                "before_release",
                batch_id=batch_value,
                landing_receipt_id=str(receipt["id"]),
            )
            for row in wip_rows:
                changed = conn.execute(
                    "UPDATE work_package_wip_tokens SET state = 'released', "
                    "released_at = ?, release_reason = ? "
                    "WHERE id = ? AND state = 'held' AND stage = 'integration' "
                    "AND reservation_key = ?",
                    (now, release_reason, row["id"], batch_value),
                )
                if changed.rowcount != 1:
                    raise TransitionError(
                        "integration WIP changed during publication finalization"
                    )
            self._fault(
                "after_release",
                batch_id=batch_value,
                released_wip_ids=list(released_ids),
            )

            held = conn.execute(
                "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
                "AND plan_version = ? AND epoch = ? AND state = 'held' "
                "ORDER BY id LIMIT 1",
                (
                    batch["package_id"],
                    int(batch["plan_version"]),
                    int(batch["epoch"]),
                ),
            ).fetchone()
            if held is not None:
                raise TransitionError(
                    "current graph still owns product WIP after publication release"
                )
            self._require_graph_finished(conn, batch, definition)

            finalization_payload = {
                "schema": PUBLICATION_FINALIZATION_SCHEMA,
                "service_version": WORK_PACKAGE_PUBLICATION_FINALIZER_VERSION,
                "batch_id": batch_value,
                "landing_receipt_id": str(receipt["id"]),
                "landing_receipt_digest": str(receipt["receipt_digest"]),
                "package_id": str(batch["package_id"]),
                "plan_version": int(batch["plan_version"]),
                "epoch": int(batch["epoch"]),
                "repository_id": str(batch["repository_id"]),
                "candidate_sha": str(batch["candidate_sha"]),
                "candidate_ref": str(batch["candidate_ref"]),
                "target_ref": str(batch["target_ref"]),
                "observed_sha": str(receipt["observed_sha"]),
                "certification_id": str(receipt["certification_id"]),
                "integration_task_id": str(batch["integration_task_id"]),
                "certification_task_id": station["certification_task_id"],
                "released_wip_ids": list(released_ids),
                "controller_station_receipt_ids": list(
                    station["station_receipt_ids"]
                ),
            }
            digest = self._digest(finalization_payload)
            finalization_id = "wppubfin_%s" % digest.removeprefix("sha256:")[:40]

            package_metadata = self._json_object(
                batch["package_metadata"], "work package metadata"
            )
            package_metadata["product_finalization"] = {
                "schema": PUBLICATION_FINALIZATION_SCHEMA,
                "status": "completed",
                "finalization_id": finalization_id,
                "batch_id": batch_value,
                "landing_receipt_id": str(receipt["id"]),
                "finalization_digest": digest,
                "completed_at": now,
            }
            package_update = conn.execute(
                "UPDATE work_packages SET state = 'completed', metadata = ?, "
                "completed_at = COALESCE(completed_at, ?), updated_at = ? "
                "WHERE id = ? AND state = 'active' AND current_plan_version = ? "
                "AND current_epoch = ?",
                (
                    json_dumps(package_metadata),
                    now,
                    now,
                    batch["package_id"],
                    int(batch["plan_version"]),
                    int(batch["epoch"]),
                ),
            )
            if package_update.rowcount != 1:
                raise StalePublicationError(
                    "work package changed before product completion"
                )
            epoch_update = conn.execute(
                "UPDATE work_package_epochs SET status = 'completed' "
                "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
                "AND status = 'active'",
                (
                    batch["package_id"],
                    int(batch["plan_version"]),
                    int(batch["epoch"]),
                ),
            )
            if epoch_update.rowcount != 1:
                raise StalePublicationError(
                    "work-package epoch changed before product completion"
                )

            conn.execute(
                "INSERT INTO work_package_publication_finalizations ("
                "id, batch_id, landing_receipt_id, package_id, plan_version, epoch, "
                "repository_id, integration_task_id, certification_task_id, "
                "certification_id, candidate_sha, candidate_ref, assembly_base_sha, "
                "landing_base_sha, target_ref, observed_sha, landing_receipt_digest, "
                "released_wip_ids, controller_station_receipt_ids, "
                "finalization_digest, finalized_by, finalized_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finalization_id,
                    batch_value,
                    receipt["id"],
                    batch["package_id"],
                    int(batch["plan_version"]),
                    int(batch["epoch"]),
                    batch["repository_id"],
                    batch["integration_task_id"],
                    station["certification_task_id"],
                    receipt["certification_id"],
                    batch["candidate_sha"],
                    batch["candidate_ref"],
                    batch["assembly_base_sha"],
                    batch["landing_base_sha"],
                    batch["target_ref"],
                    receipt["observed_sha"],
                    receipt["receipt_digest"],
                    json_dumps(list(released_ids)),
                    json_dumps(list(station["station_receipt_ids"])),
                    digest,
                    actor_value,
                    now,
                ),
            )

            history_detail = {
                **finalization_payload,
                "finalization_id": finalization_id,
                "finalization_digest": digest,
            }
            for task_id in station["task_ids"]:
                self._append_task_history(
                    conn,
                    task_id=task_id,
                    actor=actor_value,
                    detail=history_detail,
                    now=now,
                )
                self._append_lifecycle_outbox(
                    conn,
                    task_id=task_id,
                    actor=actor_value,
                    detail=history_detail,
                    now=now,
                )
            self._append_package_history(
                conn,
                package_id=str(batch["package_id"]),
                plan_version=int(batch["plan_version"]),
                epoch=int(batch["epoch"]),
                actor=actor_value,
                detail=history_detail,
                now=now,
            )

            batch_metadata = self._json_object(
                batch["batch_metadata"], "integration batch metadata"
            )
            batch_metadata["product_finalization"] = {
                "schema": PUBLICATION_FINALIZATION_SCHEMA,
                "status": "completed",
                "finalization_id": finalization_id,
                "landing_receipt_id": str(receipt["id"]),
                "finalization_digest": digest,
                "completed_at": now,
            }
            projection = conn.execute(
                "UPDATE work_package_integration_batches SET metadata = ?, "
                "updated_at = ? WHERE id = ? AND state = 'published'",
                (json_dumps(batch_metadata), now, batch_value),
            )
            if projection.rowcount != 1:
                raise TransitionError(
                    "published batch changed before finalization projection"
                )
            self._fault(
                "before_commit",
                batch_id=batch_value,
                finalization_id=finalization_id,
            )

        return PublicationFinalizationResult(
            finalization_id=finalization_id,
            batch_id=batch_value,
            landing_receipt_id=str(receipt["id"]),
            package_id=str(batch["package_id"]),
            plan_version=int(batch["plan_version"]),
            epoch=int(batch["epoch"]),
            certification_id=str(receipt["certification_id"]),
            integration_task_id=str(batch["integration_task_id"]),
            certification_task_id=station["certification_task_id"],
            released_wip_ids=released_ids,
            controller_station_receipt_ids=station["station_receipt_ids"],
            finalization_digest=digest,
            package_state="completed",
            epoch_status="completed",
            created=True,
        )

    # -- Exact context ---------------------------------------------------------

    @staticmethod
    def _lock_batch(conn: Any, batch_id: str) -> Mapping[str, Any]:
        locked = conn.execute(
            "UPDATE work_package_integration_batches SET updated_at = updated_at "
            "WHERE id = ?",
            (batch_id,),
        )
        if locked.rowcount != 1:
            raise ValidationError("work-package integration batch was not found")
        row = conn.execute(
            "SELECT batch.*, package.state AS package_state, "
            "package.current_plan_version, package.current_epoch, "
            "package.metadata AS package_metadata, epoch.status AS epoch_status, "
            "plan.definition AS plan_definition, batch.metadata AS batch_metadata "
            "FROM work_package_integration_batches AS batch "
            "JOIN work_packages AS package ON package.id = batch.package_id "
            "JOIN work_package_epochs AS epoch ON epoch.package_id = batch.package_id "
            "AND epoch.plan_version = batch.plan_version AND epoch.epoch = batch.epoch "
            "JOIN work_package_plan_versions AS plan ON plan.package_id = batch.package_id "
            "AND plan.version = batch.plan_version WHERE batch.id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("published batch context is incomplete")
        return dict(row)

    @staticmethod
    def _lock_package_epoch(
        conn: Any, batch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if conn.execute(
            "UPDATE work_packages SET updated_at = updated_at WHERE id = ?",
            (batch["package_id"],),
        ).rowcount != 1:
            raise TransitionError("work package disappeared during finalization")
        if conn.execute(
            "UPDATE work_package_epochs SET status = status WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ?",
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
            ),
        ).rowcount != 1:
            raise TransitionError("work-package epoch disappeared during finalization")
        refreshed = conn.execute(
            "SELECT batch.*, package.state AS package_state, "
            "package.current_plan_version, package.current_epoch, "
            "package.metadata AS package_metadata, epoch.status AS epoch_status, "
            "plan.definition AS plan_definition, batch.metadata AS batch_metadata "
            "FROM work_package_integration_batches AS batch "
            "JOIN work_packages AS package ON package.id = batch.package_id "
            "JOIN work_package_epochs AS epoch ON epoch.package_id = batch.package_id "
            "AND epoch.plan_version = batch.plan_version AND epoch.epoch = batch.epoch "
            "JOIN work_package_plan_versions AS plan ON plan.package_id = batch.package_id "
            "AND plan.version = batch.plan_version WHERE batch.id = ?",
            (batch["id"],),
        ).fetchone()
        if refreshed is None:
            raise TransitionError("published batch context disappeared after locking")
        current = dict(refreshed)
        immutable = (
            "id",
            "package_id",
            "plan_version",
            "epoch",
            "repository_id",
            "integration_task_id",
            "candidate_sha",
            "candidate_ref",
            "assembly_base_sha",
            "landing_base_sha",
            "target_ref",
        )
        if any(current[name] != batch[name] for name in immutable):
            raise TransitionError("published batch identity changed while locking")
        return current

    @staticmethod
    def _require_current_published_context(batch: Mapping[str, Any]) -> None:
        if batch["state"] != "published":
            raise TransitionError("only a published integration batch may finalize")
        if (
            batch["package_state"] != "active"
            or int(batch["current_plan_version"]) != int(batch["plan_version"])
            or int(batch["current_epoch"]) != int(batch["epoch"])
            or batch["epoch_status"] != "active"
        ):
            raise StalePublicationError(
                "published batch is not the active package plan and epoch"
            )
        required = (
            "repository_id",
            "integration_task_id",
            "candidate_sha",
            "candidate_tree_digest",
            "candidate_ref",
            "candidate_fence",
            "assembly_base_sha",
            "landing_base_sha",
            "target_ref",
        )
        if any(batch.get(name) in {None, ""} for name in required):
            raise PublicationReceiptError(
                "published batch lacks exact candidate or controller identity"
            )
        if not _SHA_RE.fullmatch(str(batch["candidate_sha"])):
            raise PublicationReceiptError("published candidate SHA is malformed")

    @staticmethod
    def _reject_competing_batch(conn: Any, batch: Mapping[str, Any]) -> None:
        placeholders = ",".join("?" for _ in _ACTIONABLE_BATCH_STATES)
        rows = conn.execute(
            "SELECT id, state FROM work_package_integration_batches "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
            "AND integration_task_id = ? AND id != ? AND state IN (%s) "
            "ORDER BY id" % placeholders,
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                batch["integration_task_id"],
                batch["id"],
                *sorted(_ACTIONABLE_BATCH_STATES),
            ),
        ).fetchall()
        if rows:
            raise TransitionError(
                "a competing actionable batch exists for the integration task"
            )

    def _landing_provenance(
        self,
        conn: Any,
        batch: Mapping[str, Any],
        requested_receipt: Optional[str],
    ) -> Mapping[str, Any]:
        rows = conn.execute(
            "SELECT receipt.*, intent.certification_id, "
            "intent.candidate_ref AS intent_candidate_ref, "
            "intent.assembly_base_sha AS intent_assembly_base_sha, "
            "intent.landing_base_sha AS intent_landing_base_sha, "
            "attempt.intent_id AS attempt_intent_id, "
            "attempt.candidate_sha AS attempt_candidate_sha, "
            "attempt.target_ref AS attempt_target_ref, "
            "attempt.stream_fence AS attempt_fence, "
            "certification.status AS certification_status, "
            "certification.package_id AS certification_package_id, "
            "certification.plan_version AS certification_plan_version, "
            "certification.epoch AS certification_epoch, "
            "certification.candidate_sha AS certification_candidate_sha, "
            "certification.assembly_base_sha AS certification_assembly_base_sha, "
            "certification.landing_base_sha AS certification_landing_base_sha, "
            "certification.target_ref AS certification_target_ref, "
            "certification.verification_digest AS certification_result_digest, "
            "certification.certification_task_id "
            "FROM work_package_landing_receipts AS receipt "
            "JOIN work_package_landing_intents AS intent ON intent.id = receipt.intent_id "
            "AND intent.batch_id = receipt.batch_id "
            "JOIN work_package_landing_attempts AS attempt ON attempt.id = receipt.attempt_id "
            "AND attempt.intent_id = receipt.intent_id "
            "JOIN work_package_certifications AS certification "
            "ON certification.id = intent.certification_id "
            "AND certification.batch_id = receipt.batch_id "
            "WHERE receipt.batch_id = ? ORDER BY receipt.id",
            (batch["id"],),
        ).fetchall()
        if len(rows) != 1:
            raise PublicationReceiptError(
                "published batch must have one exact landing receipt"
            )
        receipt = dict(rows[0])
        if requested_receipt is not None and receipt["id"] != requested_receipt:
            raise PublicationReceiptError(
                "requested landing receipt does not belong to published batch"
            )
        expected = (
            batch["id"],
            batch["repository_id"],
            batch["target_ref"],
            batch["candidate_sha"],
            batch["candidate_ref"],
            batch["assembly_base_sha"],
            batch["landing_base_sha"],
            batch["candidate_sha"],
            batch["target_ref"],
            int(receipt["attempt_stream_fence"]),
            batch["package_id"],
            int(batch["plan_version"]),
            int(batch["epoch"]),
            batch["candidate_sha"],
            batch["assembly_base_sha"],
            batch["landing_base_sha"],
            batch["target_ref"],
        )
        observed = (
            receipt["batch_id"],
            receipt["repository_id"],
            receipt["target_ref"],
            receipt["candidate_sha"],
            receipt["intent_candidate_ref"],
            receipt["intent_assembly_base_sha"],
            receipt["intent_landing_base_sha"],
            receipt["attempt_candidate_sha"],
            receipt["attempt_target_ref"],
            int(receipt["attempt_fence"]),
            receipt["certification_package_id"],
            int(receipt["certification_plan_version"]),
            int(receipt["certification_epoch"]),
            receipt["certification_candidate_sha"],
            receipt["certification_assembly_base_sha"],
            receipt["certification_landing_base_sha"],
            receipt["certification_target_ref"],
        )
        if observed != expected:
            raise PublicationReceiptError(
                "landing receipt, attempt, intent, and certification identity diverge"
            )
        if receipt["attempt_intent_id"] != receipt["intent_id"]:
            raise PublicationReceiptError("landing attempt does not name receipt intent")
        if receipt["certification_status"] not in {"passed", "published"}:
            raise PublicationReceiptError(
                "landing receipt certification is no longer publishable"
            )
        if not _SHA_RE.fullmatch(str(receipt["observed_sha"])) or not _SHA256_RE.fullmatch(
            str(receipt["receipt_digest"])
        ):
            raise PublicationReceiptError("landing receipt digest or readback is malformed")

        jobs = conn.execute(
            "SELECT * FROM work_package_certification_jobs WHERE batch_id = ? "
            "AND certification_id = ? ORDER BY id",
            (batch["id"], receipt["certification_id"]),
        ).fetchall()
        if len(jobs) != 1:
            raise PublicationReceiptError(
                "landing certification lacks one exact durable certification job"
            )
        job = dict(jobs[0])
        job_expected = (
            batch["id"],
            batch["package_id"],
            int(batch["plan_version"]),
            int(batch["epoch"]),
            batch["repository_id"],
            batch["candidate_sha"],
            batch["candidate_ref"],
            int(batch["candidate_fence"]),
            batch["assembly_base_sha"],
            batch["landing_base_sha"],
            batch["target_ref"],
            "completed",
            receipt["certification_id"],
            receipt["certification_result_digest"],
        )
        job_observed = (
            job["batch_id"],
            job["package_id"],
            int(job["plan_version"]),
            int(job["epoch"]),
            job["repository_id"],
            job["candidate_sha"],
            job["candidate_ref"],
            int(job["candidate_fence"]),
            job["assembly_base_sha"],
            job["landing_base_sha"],
            job["target_ref"],
            job["state"],
            job["certification_id"],
            job["result_digest"],
        )
        if job_observed != job_expected:
            raise PublicationReceiptError(
                "certification job does not prove the landed exact candidate"
            )
        job_definition = self._json_object(
            job.get("definition"), "certification job definition"
        )
        receipt["certification_job_id"] = str(job["id"])
        receipt["job_certification_task_id"] = job_definition.get(
            "certification_task_id"
        )
        return receipt

    # -- Controller stations and graph ----------------------------------------

    @staticmethod
    def _plan_definition(conn: Any, batch: Mapping[str, Any]) -> Mapping[str, Any]:
        definition = json_loads(batch["plan_definition"], None)
        if not isinstance(definition, dict):
            raise ValidationError("published batch plan definition is malformed")
        if (
            definition.get("package_id") != batch["package_id"]
            or definition.get("repository_id") != batch["repository_id"]
        ):
            raise ValidationError("published batch plan identity is incoherent")
        nodes = definition.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValidationError("published batch plan has no materialized graph")
        return definition

    def _station_provenance(
        self,
        conn: Any,
        *,
        batch: Mapping[str, Any],
        receipt: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        batch_metadata = self._json_object(
            batch["batch_metadata"], "integration batch metadata"
        )
        integration_key = str(batch_metadata.get("integration_node_key") or "").strip()
        if not integration_key:
            raise PublicationReceiptError(
                "published batch lacks immutable integration-node provenance"
            )
        nodes = self._nodes(definition)
        integration_node = nodes.get(integration_key)
        if integration_node is None or self._node_kind(integration_node) != "integration":
            raise PublicationReceiptError(
                "published batch does not name one planned integration node"
            )
        integration = self._completed_controller_task(
            conn,
            batch=batch,
            node=integration_node,
            task_id=str(batch["integration_task_id"]),
            expected_link_state="integrated",
            station_kind="integration",
            expected_outcome="integrated",
            certification_job_id=None,
            certification_id=None,
            result_digest=None,
        )

        certification_nodes = [
            node
            for node in nodes.values()
            if self._node_kind(node) == "certification"
            and integration_key in self._string_list(node.get("depends_on"), "depends_on")
        ]
        if len(certification_nodes) > 1:
            raise PublicationReceiptError(
                "integration node has multiple materialized certification successors"
            )
        certification_task_id: Optional[str] = None
        station_receipts = [str(integration["receipt_id"])]
        task_ids = [str(batch["integration_task_id"])]
        if certification_nodes:
            certification_node = certification_nodes[0]
            row = conn.execute(
                "SELECT task_id FROM work_package_task_links WHERE package_id = ? "
                "AND plan_version = ? AND epoch = ? AND node_key = ?",
                (
                    batch["package_id"],
                    int(batch["plan_version"]),
                    int(batch["epoch"]),
                    certification_node["node_key"],
                ),
            ).fetchone()
            if row is None:
                raise PublicationReceiptError(
                    "planned certification node is not materialized"
                )
            certification_task_id = str(row["task_id"])
            if receipt.get("job_certification_task_id") != certification_task_id:
                raise PublicationReceiptError(
                    "certification job does not name materialized certification task"
                )
            if receipt.get("certification_task_id") != certification_task_id:
                raise PublicationReceiptError(
                    "certification record does not name materialized certification task"
                )
            certification = self._completed_controller_task(
                conn,
                batch=batch,
                node=certification_node,
                task_id=certification_task_id,
                expected_link_state="certified",
                station_kind="certification",
                expected_outcome="certified",
                certification_job_id=str(receipt["certification_job_id"]),
                certification_id=str(receipt["certification_id"]),
                result_digest=str(receipt["certification_result_digest"]),
            )
            station_receipts.append(str(certification["receipt_id"]))
            task_ids.append(certification_task_id)
        elif receipt.get("job_certification_task_id") not in {
            None,
            "",
            batch["integration_task_id"],
        }:
            raise PublicationReceiptError(
                "certification job names an unmaterialized controller task"
            )

        return {
            "integration_node_key": integration_key,
            "certification_task_id": certification_task_id,
            "station_receipt_ids": tuple(station_receipts),
            "task_ids": tuple(task_ids),
        }

    def _completed_controller_task(
        self,
        conn: Any,
        *,
        batch: Mapping[str, Any],
        node: Mapping[str, Any],
        task_id: str,
        expected_link_state: str,
        station_kind: str,
        expected_outcome: str,
        certification_job_id: Optional[str],
        certification_id: Optional[str],
        result_digest: Optional[str],
    ) -> Mapping[str, Any]:
        row = conn.execute(
            "SELECT link.*, task.state AS task_state, task.metadata AS task_metadata, "
            "task.owner_agent_id, task.lease_id, task.leased_until, task.completed_at "
            "FROM work_package_task_links AS link JOIN tasks AS task "
            "ON task.id = link.task_id WHERE link.task_id = ? AND link.package_id = ? "
            "AND link.plan_version = ? AND link.epoch = ? AND link.node_key = ?",
            (
                task_id,
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                node["node_key"],
            ),
        ).fetchone()
        if row is None:
            raise PublicationReceiptError(
                "%s controller task is not the exact plan link" % station_kind
            )
        task = dict(row)
        if (
            task["task_state"] != "completed"
            or task["node_state"] != expected_link_state
            or task["owner_agent_id"] is not None
            or task["lease_id"] is not None
            or task["leased_until"] is not None
            or task["completed_at"] is None
        ):
            raise PublicationReceiptError(
                "%s controller task is not durably completed" % station_kind
            )
        metadata = self._json_object(task["task_metadata"], "controller task metadata")
        projection = metadata.get("work_package")
        if not isinstance(projection, Mapping):
            raise PublicationReceiptError("controller task lacks package projection")
        projection_expected = (
            batch["package_id"],
            int(batch["plan_version"]),
            int(batch["epoch"]),
            node["node_key"],
            station_kind,
        )
        projection_observed = (
            projection.get("package_id"),
            int(projection.get("plan_version") or 0),
            int(projection.get("epoch") or 0),
            projection.get("node_key"),
            projection.get("node_type"),
        )
        if projection_observed != projection_expected or metadata.get("no_dispatch") is not True:
            raise PublicationReceiptError(
                "%s controller task lost its immutable route or hold" % station_kind
            )

        receipts = conn.execute(
            "SELECT * FROM work_package_controller_station_receipts "
            "WHERE task_id = ? AND package_id = ? AND plan_version = ? AND epoch = ? "
            "AND node_key = ? AND station_kind = ? AND batch_id = ? AND outcome = ? "
            "ORDER BY id",
            (
                task_id,
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                node["node_key"],
                station_kind,
                batch["id"],
                expected_outcome,
            ),
        ).fetchall()
        if len(receipts) != 1:
            raise PublicationReceiptError(
                "%s controller task lacks one exact station receipt" % station_kind
            )
        station_receipt = dict(receipts[0])
        if not _SHA256_RE.fullmatch(str(station_receipt["provenance_digest"])):
            raise PublicationReceiptError("controller station receipt digest is malformed")
        expected_optional = (
            certification_job_id,
            certification_id,
            result_digest,
        )
        observed_optional = (
            station_receipt.get("certification_job_id"),
            station_receipt.get("certification_id"),
            station_receipt.get("result_digest"),
        )
        if observed_optional != expected_optional:
            raise PublicationReceiptError(
                "%s station receipt certification provenance diverges" % station_kind
            )
        if conn.execute(
            "SELECT 1 FROM task_history WHERE task_id = ? AND to_state = 'completed' "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone() is None:
            raise PublicationReceiptError(
                "%s completion lacks task history" % station_kind
            )
        if conn.execute(
            "SELECT 1 FROM task_transition_outbox WHERE task_id = ? "
            "AND event_type = 'task.lifecycle' AND to_state = 'completed' "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone() is None:
            raise PublicationReceiptError(
                "%s completion lacks transition outbox" % station_kind
            )
        return {"receipt_id": str(station_receipt["id"]), "task_id": task_id}

    def _require_graph_finished(
        self,
        conn: Any,
        batch: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> None:
        nodes = self._nodes(definition)
        rows = conn.execute(
            "SELECT node_key, node_state FROM work_package_task_links "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? ORDER BY node_key",
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
            ),
        ).fetchall()
        by_key = {str(row["node_key"]): str(row["node_state"]) for row in rows}
        if set(by_key) != set(nodes):
            raise TransitionError(
                "current work-package graph materialization is incomplete"
            )
        unfinished = sorted(
            key for key, state in by_key.items() if state not in _TERMINAL_NODE_STATES
        )
        if unfinished:
            raise TransitionError(
                "current work-package graph has unfinished nodes: %s"
                % ", ".join(unfinished)
            )
        for key, node in nodes.items():
            kind = self._node_kind(node)
            expected = {
                "integration": "integrated",
                "certification": "certified",
            }.get(kind)
            if expected is not None and by_key[key] not in {
                expected,
                "superseded",
                "cancelled",
            }:
                raise TransitionError(
                    "%s controller node has incoherent terminal state" % kind
                )

    def _exact_held_integration_wip(
        self, conn: Any, batch: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        rows = conn.execute(
            "SELECT token.* FROM work_package_wip_tokens AS token "
            "WHERE token.package_id = ? AND token.plan_version = ? AND token.epoch = ? "
            "AND token.stage = 'integration' AND token.reservation_key = ? "
            "ORDER BY token.id",
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                batch["id"],
            ),
        ).fetchall()
        if not rows:
            raise TransitionError(
                "published batch has no bounded integration WIP to release"
            )
        input_rows = conn.execute(
            "SELECT * FROM work_package_batch_inputs WHERE batch_id = ? "
            "ORDER BY ordinal, id",
            (batch["id"],),
        ).fetchall()
        inputs_by_task = {str(row["task_id"]): dict(row) for row in input_rows}
        if not inputs_by_task or len(inputs_by_task) != len(input_rows):
            raise TransitionError("published batch has no immutable input membership")
        observed_input_ids = {str(row["task_id"]) for row in rows}
        if observed_input_ids != set(inputs_by_task):
            raise TransitionError(
                "integration WIP does not cover the exact batch input membership"
            )
        for raw in rows:
            row = dict(raw)
            input_row = inputs_by_task[str(row["task_id"])]
            fan_in = conn.execute(
                "SELECT * FROM work_package_wip_tokens WHERE id = ?",
                (str(row["predecessor_token_id"] or ""),),
            ).fetchone()
            if fan_in is None:
                raise TransitionError(
                    "integration WIP lacks its exact durable transfer predecessor"
                )
            fan_in = dict(fan_in)
            candidate_buffer = conn.execute(
                "SELECT * FROM work_package_wip_tokens WHERE id = ?",
                (str(fan_in["predecessor_token_id"] or ""),),
            ).fetchone()
            if candidate_buffer is None:
                raise TransitionError(
                    "integration WIP lacks its accepted fan-in predecessor"
                )
            candidate_buffer = dict(candidate_buffer)
            mutation = conn.execute(
                "SELECT * FROM work_package_wip_tokens WHERE id = ?",
                (str(candidate_buffer["predecessor_token_id"] or ""),),
            ).fetchone()
            if mutation is None:
                raise TransitionError(
                    "integration WIP lacks its mutation ownership predecessor"
                )
            mutation = dict(mutation)

            identity_fields = (
                "package_id",
                "plan_version",
                "epoch",
                "node_key",
                "task_id",
                "resource_key",
                "token_kind",
                "capacity_units",
                "acquired_by_assignment_lease_id",
            )
            if (
                row["state"] != "held"
                or row["reservation_key"] != batch["id"]
                or fan_in["stage"] != "fan_in_reservation"
                or fan_in["state"] != "released"
                or fan_in["release_reason"]
                != "integration_transfer:%s" % batch["id"]
                or int(row["generation"]) != int(fan_in["generation"]) + 1
                or any(row[field] != fan_in[field] for field in identity_fields)
            ):
                raise TransitionError(
                    "integration WIP lacks its exact durable fan-in transfer"
                )

            if (
                candidate_buffer["stage"] != "candidate_buffer"
                or candidate_buffer["state"] != "released"
                or not candidate_buffer["released_at"]
                or int(fan_in["generation"])
                != int(candidate_buffer["generation"]) + 1
                or any(
                    fan_in[field] != candidate_buffer[field]
                    for field in identity_fields
                )
                or fan_in["reservation_key"]
                != candidate_buffer["reservation_key"]
            ):
                raise TransitionError(
                    "integration fan-in lacks its exact accepted candidate buffer"
                )
            try:
                resolution = json_loads(candidate_buffer["release_reason"], None)
            except (TypeError, ValueError) as exc:
                raise TransitionError(
                    "integration candidate acceptance provenance is malformed"
                ) from exc
            required_resolution = {
                "schema": "mac.work_package.wip_resolution.v1",
                "decision": "accepted",
                "candidate_id": str(input_row["candidate_id"]),
                "evidence_id": str(input_row["evidence_id"]),
                "successor_token_id": str(fan_in["id"]),
                "resolved_at": str(candidate_buffer["released_at"]),
            }
            if (
                not isinstance(resolution, Mapping)
                or any(
                    resolution.get(key) != value
                    for key, value in required_resolution.items()
                )
                or not str(resolution.get("actor") or "").strip()
            ):
                raise TransitionError(
                    "integration candidate lacks exact acceptance provenance"
                )

            candidate = conn.execute(
                "SELECT id FROM work_package_node_candidates WHERE id = ? "
                "AND package_id = ? AND plan_version = ? AND epoch = ? "
                "AND node_key = ? AND task_id = ? AND assignment_lease_id = ? "
                "AND evidence_id = ? AND status = 'accepted'",
                (
                    input_row["candidate_id"],
                    row["package_id"],
                    int(row["plan_version"]),
                    int(row["epoch"]),
                    row["node_key"],
                    row["task_id"],
                    row["acquired_by_assignment_lease_id"],
                    input_row["evidence_id"],
                ),
            ).fetchone()
            if (
                candidate is None
                or input_row["candidate_status"] != "accepted"
                or input_row["assignment_lease_id"]
                != row["acquired_by_assignment_lease_id"]
            ):
                raise TransitionError(
                    "integration WIP is not bound to its exact accepted candidate"
                )

            if (
                mutation["stage"] != "mutation"
                or mutation["state"] != "released"
                or mutation["release_reason"]
                != "candidate_transfer:%s" % input_row["candidate_id"]
                or int(candidate_buffer["generation"])
                != int(mutation["generation"]) + 1
                or any(
                    candidate_buffer[field] != mutation[field]
                    for field in identity_fields
                )
                or candidate_buffer["reservation_key"]
                != mutation["reservation_key"]
            ):
                raise TransitionError(
                    "integration candidate lacks its exact mutation WIP lineage"
                )
        return rows

    # -- Idempotency -----------------------------------------------------------

    def _idempotent_result(
        self,
        conn: Any,
        *,
        batch: Mapping[str, Any],
        receipt: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> PublicationFinalizationResult:
        finalization = dict(row)
        expected_identity = (
            batch["id"],
            receipt["id"],
            batch["package_id"],
            int(batch["plan_version"]),
            int(batch["epoch"]),
            batch["repository_id"],
            batch["integration_task_id"],
            receipt["certification_id"],
            batch["candidate_sha"],
            batch["candidate_ref"],
            batch["assembly_base_sha"],
            batch["landing_base_sha"],
            batch["target_ref"],
            receipt["observed_sha"],
            receipt["receipt_digest"],
        )
        observed_identity = (
            finalization["batch_id"],
            finalization["landing_receipt_id"],
            finalization["package_id"],
            int(finalization["plan_version"]),
            int(finalization["epoch"]),
            finalization["repository_id"],
            finalization["integration_task_id"],
            finalization["certification_id"],
            finalization["candidate_sha"],
            finalization["candidate_ref"],
            finalization["assembly_base_sha"],
            finalization["landing_base_sha"],
            finalization["target_ref"],
            finalization["observed_sha"],
            finalization["landing_receipt_digest"],
        )
        if observed_identity != expected_identity:
            raise PublicationReceiptError(
                "existing product finalization conflicts with landing identity"
            )
        definition = self._plan_definition(conn, batch)
        station = self._station_provenance(
            conn,
            batch=batch,
            receipt=receipt,
            definition=definition,
        )
        released_ids = tuple(
            self._json_string_list(
                finalization["released_wip_ids"], "finalization released WIP ids"
            )
        )
        station_ids = tuple(
            self._json_string_list(
                finalization["controller_station_receipt_ids"],
                "finalization station receipt ids",
            )
        )
        if station_ids != station["station_receipt_ids"]:
            raise PublicationReceiptError(
                "existing finalization station provenance conflicts"
            )
        if finalization["certification_task_id"] != station["certification_task_id"]:
            raise PublicationReceiptError(
                "existing finalization certification task conflicts"
            )
        rows = conn.execute(
            "SELECT id, state, release_reason FROM work_package_wip_tokens "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
            "AND stage = 'integration' AND reservation_key = ? ORDER BY id",
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                batch["id"],
            ),
        ).fetchall()
        if tuple(str(item["id"]) for item in rows) != released_ids or any(
            item["state"] != "released"
            or item["release_reason"]
            != "publication_finalized:%s" % receipt["id"]
            for item in rows
        ):
            raise TransitionError("finalized product WIP receipt no longer matches")
        if conn.execute(
            "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND state = 'held' LIMIT 1",
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
            ),
        ).fetchone() is not None:
            raise TransitionError("completed package unexpectedly owns held WIP")
        self._require_graph_finished(conn, batch, definition)
        if (
            batch["state"] != "published"
            or batch["package_state"] != "completed"
            or batch["epoch_status"] != "completed"
            or int(batch["current_plan_version"]) != int(batch["plan_version"])
            or int(batch["current_epoch"]) != int(batch["epoch"])
        ):
            raise StalePublicationError(
                "existing finalization no longer projects completed current state"
            )
        payload = {
            "schema": PUBLICATION_FINALIZATION_SCHEMA,
            "service_version": WORK_PACKAGE_PUBLICATION_FINALIZER_VERSION,
            "batch_id": str(batch["id"]),
            "landing_receipt_id": str(receipt["id"]),
            "landing_receipt_digest": str(receipt["receipt_digest"]),
            "package_id": str(batch["package_id"]),
            "plan_version": int(batch["plan_version"]),
            "epoch": int(batch["epoch"]),
            "repository_id": str(batch["repository_id"]),
            "candidate_sha": str(batch["candidate_sha"]),
            "candidate_ref": str(batch["candidate_ref"]),
            "target_ref": str(batch["target_ref"]),
            "observed_sha": str(receipt["observed_sha"]),
            "certification_id": str(receipt["certification_id"]),
            "integration_task_id": str(batch["integration_task_id"]),
            "certification_task_id": station["certification_task_id"],
            "released_wip_ids": list(released_ids),
            "controller_station_receipt_ids": list(station_ids),
        }
        if self._digest(payload) != finalization["finalization_digest"]:
            raise PublicationReceiptError("existing finalization digest is invalid")
        projection = self._json_object(
            batch["batch_metadata"], "integration batch metadata"
        ).get("product_finalization")
        if not isinstance(projection, Mapping) or (
            projection.get("status"),
            projection.get("finalization_id"),
            projection.get("landing_receipt_id"),
            projection.get("finalization_digest"),
        ) != (
            "completed",
            finalization["id"],
            receipt["id"],
            finalization["finalization_digest"],
        ):
            raise PublicationReceiptError(
                "published batch finalization projection is incoherent"
            )
        self._reject_competing_batch(conn, batch)
        return PublicationFinalizationResult(
            finalization_id=str(finalization["id"]),
            batch_id=str(batch["id"]),
            landing_receipt_id=str(receipt["id"]),
            package_id=str(batch["package_id"]),
            plan_version=int(batch["plan_version"]),
            epoch=int(batch["epoch"]),
            certification_id=str(receipt["certification_id"]),
            integration_task_id=str(batch["integration_task_id"]),
            certification_task_id=station["certification_task_id"],
            released_wip_ids=released_ids,
            controller_station_receipt_ids=station_ids,
            finalization_digest=str(finalization["finalization_digest"]),
            package_state="completed",
            epoch_status="completed",
            created=False,
        )

    # -- Append-only audit -----------------------------------------------------

    @staticmethod
    def _append_task_history(
        conn: Any,
        *,
        task_id: str,
        actor: str,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO task_history (id, task_id, event_type, actor, from_state, "
            "to_state, detail, created_at) VALUES (?, ?, ?, ?, 'completed', "
            "'completed', ?, ?)",
            (
                new_id("history"),
                task_id,
                "task.work_package_product_finalized",
                actor,
                json_dumps(dict(detail)),
                now,
            ),
        )

    @staticmethod
    def _append_lifecycle_outbox(
        conn: Any,
        *,
        task_id: str,
        actor: str,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO task_transition_outbox (id, task_id, event_type, actor, "
            "from_state, to_state, detail, status, attempts, created_at) "
            "VALUES (?, ?, 'task.lifecycle', ?, 'completed', 'completed', ?, "
            "'pending', 0, ?)",
            (
                new_id("outbox"),
                task_id,
                actor,
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
            "INSERT INTO work_package_history (id, package_id, seq, event_type, "
            "actor, plan_version, epoch, detail, created_at) "
            "VALUES (?, ?, ?, 'work_package.publication_finalized', ?, ?, ?, ?, ?)",
            (
                new_id("wph"),
                package_id,
                int(seq["value"]),
                actor,
                plan_version,
                epoch,
                json_dumps(dict(detail)),
                now,
            ),
        )

    # -- Pure helpers ----------------------------------------------------------

    @staticmethod
    def _nodes(definition: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
        raw = definition.get("nodes")
        if not isinstance(raw, list) or not raw:
            raise ValidationError("work-package plan has no nodes")
        result: dict[str, Mapping[str, Any]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationError("work-package plan node is malformed")
            key = str(item.get("node_key") or "").strip()
            if not key or key in result:
                raise ValidationError("work-package plan node identity is malformed")
            result[key] = item
        return result

    @staticmethod
    def _node_kind(node: Mapping[str, Any]) -> str:
        return str(node.get("node_type") or node.get("kind") or "mutation")

    @staticmethod
    def _string_list(value: Any, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValidationError("work-package %s is malformed" % field)
        return list(value)

    @staticmethod
    def _json_string_list(value: Any, field: str) -> list[str]:
        decoded = json_loads(value, None)
        if not isinstance(decoded, list) or not all(
            isinstance(item, str) and item for item in decoded
        ):
            raise ValidationError("%s is malformed" % field)
        return list(decoded)

    @staticmethod
    def _json_object(value: Any, field: str) -> JsonDict:
        decoded = json_loads(value, None) if isinstance(value, str) else value
        if not isinstance(decoded, dict):
            raise ValidationError("%s must be an object" % field)
        return dict(decoded)

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        return "sha256:%s" % hashlib.sha256(
            json_dumps(dict(value)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _required(value: Any, label: str) -> str:
        result = str(value or "").strip()
        if not result:
            raise ValidationError("%s is required" % label)
        return result

    def _fault(self, stage: str, **detail: Any) -> None:
        self._fault_hook(stage, detail)


__all__ = [
    "PUBLICATION_FINALIZATION_SCHEMA",
    "PublicationFinalizationResult",
    "PublicationReceiptError",
    "StalePublicationError",
    "WORK_PACKAGE_PUBLICATION_FINALIZER_VERSION",
    "WorkPackagePublicationFinalizationError",
    "WorkPackagePublicationFinalizer",
]
