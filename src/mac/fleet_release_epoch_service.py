"""Durable hub participant for synchronized fleet release transactions.

The hub protocol has an explicit pre-mutation boundary:

* ``open_epoch`` atomically reserves the exact cohort and adopts every exact
  prior hold into one epoch-owned hold. It stages identity intent, but requires
  no node installation or heartbeat proof.
* ``prove`` runs only after node rollback intent is durable and node apply has
  completed. It verifies the whole cohort's pending credentials, candidate
  attestation keys, generation, and report-executor proof without promotion.
* ``commit`` promotes all authority surfaces, transitions all epoch holds, and
  writes the exact marker in one database transaction.
* ``abort`` restores every exact prior hold and service-claim snapshot while
  deleting only epoch-owned encrypted staging.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from mac.models import (
    AgentStatus,
    HealthStatus,
    NotFoundError,
    REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY,
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY,
    ServiceClaimStatus,
    TaskState,
    TransitionError,
    ValidationError,
    agent_has_read_only_report_repository_executor,
    ensure_json_object,
    json_dumps,
    json_loads,
    parse_time,
    read_only_report_repository_executor_approval,
    read_only_report_repository_executor_resource,
    utcnow,
    valid_read_only_report_repository_executor_attestation,
    valid_read_only_report_repository_executor_approval,
)
from mac.worker_credentials import (
    MODE_COMPATIBILITY,
    POLICY_MODES,
    WorkerCredentialError,
    WorkerCredentialLifecycle,
    live_inventory_in_transaction,
    write_policy_state_in_transaction,
)


EPOCH_IDENTITY_SCHEMA = "mac.fleet_release_epoch_identity.v1"
EPOCH_PROOF_SCHEMA = "mac.fleet_release_epoch_proof.v1"
EPOCH_RECEIPT_SCHEMA = "mac.fleet_release_epoch_receipt.v1"
EPOCH_READINESS_SCHEMA = "mac.fleet_release_pre_prove_readiness.v1"
EPOCH_MARKER_SCHEMA = "mac.fleet_release_epoch_marker.v1"
ATTESTATION_PROOF_SCHEMA = "mac.fleet_release_attestation_candidate_proof.v1"
ATTESTATION_PROOF_PURPOSE = "synchronized-fleet-release-candidate"
REPORT_ACTIONS = frozenset({"preserve", "approve", "revoke"})

# Abort dispositions govern what happens to an epoch-owned pending
# credential that a node has already installed and is heartbeating with.
# ``auto`` refuses a destructive abort once any pending credential is
# installed, forcing the operator to choose an explicit recovery action.
# ``retain_installed`` aborts epoch bookkeeping while leaving every
# installed pending credential authenticating, so the proven predecessor
# projection each node depends on survives.  ``discard_installed`` is the
# explicit destructive path that revokes even installed pending
# credentials (the historical, unconditional abort behaviour).
ABORT_DISPOSITION_AUTO = "auto"
ABORT_DISPOSITION_RETAIN_INSTALLED = "retain_installed"
ABORT_DISPOSITION_DISCARD_INSTALLED = "discard_installed"
ABORT_DISPOSITIONS = frozenset(
    {
        ABORT_DISPOSITION_AUTO,
        ABORT_DISPOSITION_RETAIN_INSTALLED,
        ABORT_DISPOSITION_DISCARD_INSTALLED,
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return (
        "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    )


def _sha256_text(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _row(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _text(value: Any, field: str, maximum_bytes: int = 512) -> str:
    result = str(value or "").strip()
    if (
        not result
        or len(result.encode("utf-8")) > maximum_bytes
        or any(not character.isprintable() for character in result)
    ):
        raise ValidationError("%s is invalid" % field)
    return result


def _digest(value: Any) -> str:
    result = str(value or "").strip()
    if (
        len(result) != 71
        or not result.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in result[7:])
    ):
        raise ValidationError("fleet release identity digest is invalid")
    return result


class FleetReleaseEpochService:
    """Typed hub authority for exact open/prove/commit/abort epochs."""

    def __init__(
        self,
        control_plane: Any,
        *,
        verify_signature: Callable[[str, Dict[str, Any], str], bool],
    ) -> None:
        self.control_plane = control_plane
        self.store = control_plane.store
        self.credentials = WorkerCredentialLifecycle(self.store)
        self._verify_signature = verify_signature
        self.hub_authority_id = self._ensure_hub_authority_identity()

    def _ensure_hub_authority_identity(self) -> str:
        candidate = str(uuid.uuid4())
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO hub_authority_identity "
                "(singleton_key, authority_id, created_at) "
                "VALUES ('hub', ?, ?) ON CONFLICT(singleton_key) DO NOTHING",
                (candidate, utcnow()),
            )
            row = conn.execute(
                "SELECT authority_id FROM hub_authority_identity "
                "WHERE singleton_key = 'hub'"
            ).fetchone()
            if row is None:
                raise TransitionError("hub authority identity was not persisted")
            try:
                return str(uuid.UUID(str(row["authority_id"])))
            except (TypeError, ValueError) as exc:
                raise TransitionError("hub authority identity is invalid") from exc

    @staticmethod
    def _marker_id(epoch_id: str) -> str:
        # Deliberately shares the legacy marker namespace. The same textual
        # epoch can never commit once through each protocol.
        return (
            "alce_epoch_%s" % hashlib.sha256(epoch_id.encode("utf-8")).hexdigest()[:32]
        )

    @staticmethod
    def _epoch_hold_reason(epoch_id: str) -> str:
        return (
            "mac:fleet-release:%s"
            % hashlib.sha256(epoch_id.encode("utf-8")).hexdigest()[:32]
        )

    @staticmethod
    def _policy_snapshot(conn: Any) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM worker_credential_policy_state WHERE singleton_key = 'fleet'"
        ).fetchone()
        if row is None:
            return {
                "present": False,
                "mode": MODE_COMPATIBILITY,
                "revision": 0,
            }
        value = _row(row)
        return {
            "present": True,
            "mode": str(value.get("mode") or ""),
            "inventory_digest": value.get("inventory_digest"),
            "ready_agent_ids": list(json_loads(value.get("ready_agent_ids"), [])),
            "revision": int(value.get("revision") or 0),
            "updated_by": str(value.get("updated_by") or ""),
            "updated_at": str(value.get("updated_at") or ""),
        }

    @staticmethod
    def _live_principals(conn: Any, agent_id: str) -> List[str]:
        rows = conn.execute(
            "SELECT id FROM worker_credentials WHERE agent_id = ? "
            "AND state IN ('pending_install', 'active') ORDER BY id",
            (agent_id,),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    @staticmethod
    def _active_claims(conn: Any, agent_id: str) -> List[str]:
        rows = conn.execute(
            "SELECT id FROM service_claims WHERE agent_id = ? AND status = ? "
            "ORDER BY id",
            (agent_id, ServiceClaimStatus.ACTIVE.value),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    @staticmethod
    def _report_resource_projection(resources: Mapping[str, Any]) -> Dict[str, Any]:
        """Return every resource field a staged report action can mutate."""

        return {
            REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: resources.get(
                REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY
            ),
            REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: resources.get(
                REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY
            ),
        }

    def _validate_report_authority_projection(
        self, participant: Any, resources: Mapping[str, Any]
    ) -> None:
        """Require the open-time report CAS or one safe derived-marker loss.

        Worker registration always re-derives the controller-owned marker. A
        deployment that changes the worker attestation can therefore remove an
        old marker after epoch open while preserving its exact approval. That
        is a monotonic loss of authority, not a competing authority mutation,
        and staged approve/revoke actions may safely finish it at commit.
        """

        projection = self._report_resource_projection(resources)
        prior_sha256 = str(participant["prior_report_executor_projection_sha256"])
        if hmac.compare_digest(_sha256_json(projection), prior_sha256):
            return
        if str(participant["report_executor_action"]) == "preserve":
            raise ValidationError(
                "report executor authority changed after fleet release open"
            )
        approval = projection.get(REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY)
        if (
            projection.get(REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY) is not None
            or not valid_read_only_report_repository_executor_approval(approval)
        ):
            raise ValidationError(
                "report executor authority changed after fleet release open"
            )
        marker_arguments = {
            key: str(approval[key])
            for key in (
                "runtime_image_ref",
                "policy_sha256",
                "openshell_bin_path",
                "openshell_bin_sha256",
                "executor_path",
                "executor_sha256",
                "platform",
                "isolation_posture",
                "python_path",
                "python_sha256",
                "executor_script_path",
                "executor_script_sha256",
                "source_root",
                "source_bundle_sha256",
            )
        }
        reconstructed = dict(projection)
        reconstructed[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] = (
            read_only_report_repository_executor_resource(**marker_arguments)
        )
        if not hmac.compare_digest(_sha256_json(reconstructed), prior_sha256):
            raise ValidationError(
                "report executor authority changed after fleet release open"
            )

    @staticmethod
    def _expected_live_principals(participant: Any) -> List[str]:
        prior = list(json_loads(participant["prior_live_principal_ids"], []))
        return sorted(
            {str(value) for value in prior} | {str(participant["principal_id"])}
        )

    @staticmethod
    def _has_active_work(conn: Any, agent_id: str, agent_row: Any) -> bool:
        task = conn.execute(
            "SELECT id FROM tasks WHERE owner_agent_id = ? AND state IN (?, ?) LIMIT 1",
            (agent_id, TaskState.CLAIMED.value, TaskState.RUNNING.value),
        ).fetchone()
        return bool(task is not None or agent_row["current_task_id"] is not None)

    def assert_agent_unreserved_in_transaction(self, conn: Any, agent_id: str) -> None:
        reservation = conn.execute(
            "SELECT epoch_id FROM fleet_release_epoch_agents "
            "WHERE agent_id = ? AND open_state = 1",
            (agent_id,),
        ).fetchone()
        if reservation is not None:
            raise ValidationError(
                "agent identity is reserved by an open fleet release epoch"
            )

    def _normalize_open(
        self,
        epoch_id: str,
        participants: Iterable[Mapping[str, Any]],
        *,
        successor_hold_reason: Optional[str],
        desired_policy_mode: Optional[str],
    ) -> tuple[str, List[Dict[str, Any]], Dict[str, Any], str]:
        epoch = _text(epoch_id, "fleet release epoch id")
        epoch_hold_reason = self._epoch_hold_reason(epoch)
        successor: Optional[str] = None
        if successor_hold_reason is not None:
            successor = _text(successor_hold_reason, "successor dispatch hold reason")
            if successor == epoch_hold_reason:
                raise ValidationError(
                    "successor hold must differ from epoch-owned hold"
                )
        desired_mode: Optional[str] = None
        if desired_policy_mode is not None:
            desired_mode = str(desired_policy_mode or "").strip()
            if desired_mode not in POLICY_MODES:
                raise ValidationError(
                    "desired worker credential policy mode is invalid"
                )

        result: List[Dict[str, Any]] = []
        seen: set[str] = set()
        try:
            iterator = iter(participants)
        except TypeError as exc:
            raise ValidationError(
                "fleet release participants must be iterable"
            ) from exc
        for raw in iterator:
            if not isinstance(raw, Mapping):
                raise ValidationError("fleet release participant is malformed")
            agent_id = _text(raw.get("agent_id"), "agent id")
            if agent_id in seen:
                raise ValidationError(
                    "duplicate agent in fleet release epoch: %s" % agent_id
                )
            seen.add(agent_id)
            expected_dispatch_hold = raw.get("expected_dispatch_hold")
            if not isinstance(expected_dispatch_hold, bool):
                raise ValidationError(
                    "expected dispatch hold must be an explicit boolean"
                )
            prior_reason_value = raw.get("expected_hold_reason")
            prior_hold_at_value = raw.get("expected_hold_at")
            if expected_dispatch_hold:
                expected_hold_reason: Optional[str] = _text(
                    prior_reason_value, "expected hold reason"
                )
                expected_hold_at: Optional[str] = _text(
                    prior_hold_at_value, "expected hold timestamp"
                )
                try:
                    parse_time(expected_hold_at)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("expected hold timestamp is invalid") from exc
            else:
                if prior_reason_value not in (None, "") or prior_hold_at_value not in (
                    None,
                    "",
                ):
                    raise ValidationError(
                        "unheld participant cannot specify expected hold ownership"
                    )
                expected_hold_reason = None
                expected_hold_at = None
            generation = _text(raw.get("generation"), "generation")
            baseline_seen = _text(raw.get("baseline_seen"), "baseline seen")
            try:
                parse_time(baseline_seen)
            except (TypeError, ValueError) as exc:
                raise ValidationError("baseline seen is invalid") from exc
            principal_id = _text(raw.get("principal_id"), "principal id")

            candidate_key: Optional[str] = None
            candidate_fingerprint: Optional[str] = None
            candidate = raw.get("attestation_candidate")
            if candidate is not None:
                if not isinstance(candidate, Mapping) or set(candidate) != {"key"}:
                    raise ValidationError(
                        "attestation candidate has unexpected or missing fields"
                    )
                candidate_key = str(candidate.get("key") or "")
                try:
                    candidate_key.encode("ascii")
                except UnicodeEncodeError as exc:
                    raise ValidationError(
                        "attestation candidate key must be ASCII"
                    ) from exc
                if (
                    len(candidate_key) < 32
                    or len(candidate_key) > 512
                    or any(not character.isprintable() for character in candidate_key)
                ):
                    raise ValidationError("attestation candidate key is invalid")
                candidate_fingerprint = _sha256_text(candidate_key)

            report_action = str(raw.get("report_executor_action") or "preserve").strip()
            if report_action not in REPORT_ACTIONS:
                raise ValidationError("report executor action is invalid")
            report_attestation = raw.get("report_executor_attestation")
            if report_action == "approve":
                if not valid_read_only_report_repository_executor_attestation(
                    report_attestation
                ):
                    raise ValidationError("report executor attestation is malformed")
                report_attestation = json.loads(_canonical_json(report_attestation))
            elif report_attestation is not None:
                raise ValidationError(
                    "report executor attestation is accepted only for approval"
                )
            result.append(
                {
                    "agent_id": agent_id,
                    "expected_dispatch_hold": expected_dispatch_hold,
                    "expected_hold_reason": expected_hold_reason,
                    "expected_hold_at": expected_hold_at,
                    "generation": generation,
                    "baseline_seen": baseline_seen,
                    "principal_id": principal_id,
                    "attestation_candidate_key": candidate_key,
                    "attestation_candidate_fingerprint": candidate_fingerprint,
                    "report_executor_action": report_action,
                    "report_executor_attestation": report_attestation,
                }
            )
        if not result:
            raise ValidationError(
                "fleet release epoch requires at least one participant"
            )
        result.sort(key=lambda item: item["agent_id"])
        request = {
            "schema": EPOCH_IDENTITY_SCHEMA,
            "epoch_id": epoch,
            "hub_authority_id": self.hub_authority_id,
            "epoch_hold_reason": epoch_hold_reason,
            "successor_hold_reason": successor,
            "desired_worker_credential_mode": desired_mode,
            "agents": [
                {
                    key: item[key]
                    for key in (
                        "agent_id",
                        "expected_dispatch_hold",
                        "expected_hold_reason",
                        "expected_hold_at",
                        "generation",
                        "baseline_seen",
                        "principal_id",
                        "attestation_candidate_fingerprint",
                        "report_executor_action",
                        "report_executor_attestation",
                    )
                }
                for item in result
            ],
        }
        return epoch_hold_reason, result, request, _sha256_json(request)

    def _lock_agent(self, conn: Any, agent_id: str) -> Any:
        locked = conn.execute(
            "UPDATE agents SET updated_at = updated_at "
            "WHERE id = ? AND deleted_at IS NULL",
            (agent_id,),
        )
        if locked.rowcount != 1:
            raise NotFoundError("agent not found: %s" % agent_id)
        return conn.execute(
            "SELECT * FROM agents WHERE id = ? AND deleted_at IS NULL",
            (agent_id,),
        ).fetchone()

    @staticmethod
    def _prior_hold_matches(agent_row: Any, item: Mapping[str, Any]) -> bool:
        if item["expected_dispatch_hold"]:
            return bool(
                agent_row["dispatch_hold"]
                and str(agent_row["dispatch_hold_reason"] or "")
                == item["expected_hold_reason"]
                and agent_row["dispatch_hold_at"] == item["expected_hold_at"]
            )
        return bool(
            not agent_row["dispatch_hold"]
            and agent_row["dispatch_hold_reason"] is None
            and agent_row["dispatch_hold_at"] is None
        )

    def _receipt(self, conn: Any, epoch_row: Any) -> Dict[str, Any]:
        epoch = _row(epoch_row)
        participants = conn.execute(
            "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
            "ORDER BY ordinal",
            (epoch["epoch_id"],),
        ).fetchall()
        receipt: Dict[str, Any] = {
            "schema": EPOCH_RECEIPT_SCHEMA,
            "status": str(epoch["state"]),
            "epoch_id": str(epoch["epoch_id"]),
            "hub_authority_id": self.hub_authority_id,
            "identity_sha256": str(epoch["identity_sha256"]),
            "cohort_size": len(participants),
            "successor_hold_reason": epoch.get("successor_hold_reason"),
            "desired_worker_credential_mode": epoch.get("desired_policy_mode"),
            "prepared_at": str(epoch["prepared_at"]),
            "agents": [
                {
                    "agent_id": str(item["agent_id"]),
                    "prior_dispatch_hold": bool(item["prior_dispatch_hold"]),
                    "prior_hold_reason": item["prior_hold_reason"],
                    "prior_hold_at": item["prior_hold_at"],
                    "epoch_hold_reason": str(item["epoch_hold_reason"]),
                    "epoch_hold_at": str(item["epoch_hold_at"]),
                    "generation": str(item["generation"]),
                    "principal_id": str(item["principal_id"]),
                    "principal_version": int(item["principal_version"]),
                    "principal_fingerprint": str(item["principal_fingerprint"]),
                    "attestation_candidate_fingerprint": item[
                        "attestation_candidate_fingerprint"
                    ],
                    "report_executor_action": str(item["report_executor_action"]),
                }
                for item in participants
            ],
        }
        if epoch.get("proof_sha256"):
            receipt["proof_sha256"] = str(epoch["proof_sha256"])
        for key in ("proved_at", "committed_at", "aborted_at"):
            if epoch.get(key):
                receipt[key] = str(epoch[key])
        if epoch.get("abort_reason"):
            receipt["abort_reason"] = str(epoch["abort_reason"])
        if epoch.get("abort_disposition"):
            receipt["abort_disposition"] = str(epoch["abort_disposition"])
        return receipt

    def _assert_epoch_identity_integrity(self, conn: Any, epoch_row: Any) -> None:
        """Re-derive canonical identity from the durable participant rows."""

        epoch = _row(epoch_row)
        identity = ensure_json_object(json_loads(epoch.get("identity_payload"), {}))
        policy_snapshot = ensure_json_object(
            json_loads(epoch.get("policy_snapshot"), {})
        )
        if (
            identity.get("schema") != EPOCH_IDENTITY_SCHEMA
            or identity.get("epoch_id") != epoch.get("epoch_id")
            or identity.get("hub_authority_id") != self.hub_authority_id
            or identity.get("request_sha256") != epoch.get("request_sha256")
            or identity.get("epoch_hold_reason")
            != self._epoch_hold_reason(str(epoch.get("epoch_id") or ""))
            or identity.get("successor_hold_reason")
            != epoch.get("successor_hold_reason")
            or identity.get("desired_worker_credential_mode")
            != epoch.get("desired_policy_mode")
            or identity.get("policy_snapshot") != policy_snapshot
            or _sha256_json(identity) != epoch.get("identity_sha256")
        ):
            raise TransitionError("fleet release epoch identity storage is corrupt")
        participants = conn.execute(
            "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
            "ORDER BY ordinal",
            (epoch["epoch_id"],),
        ).fetchall()
        projected_agents: List[Dict[str, Any]] = []
        for participant in participants:
            projected_agents.append(
                {
                    "agent_id": str(participant["agent_id"]),
                    "expected_dispatch_hold": bool(participant["prior_dispatch_hold"]),
                    "expected_hold_reason": participant["prior_hold_reason"],
                    "expected_hold_at": participant["prior_hold_at"],
                    "generation": str(participant["generation"]),
                    "baseline_seen": str(participant["baseline_seen"]),
                    "principal_id": str(participant["principal_id"]),
                    "attestation_candidate_fingerprint": participant[
                        "attestation_candidate_fingerprint"
                    ],
                    "report_executor_action": str(
                        participant["report_executor_action"]
                    ),
                    "report_executor_attestation": (
                        ensure_json_object(
                            json_loads(participant["report_executor_attestation"], {})
                        )
                        if participant["report_executor_attestation"] is not None
                        else None
                    ),
                    "prior_hold_at": participant["prior_hold_at"],
                    "epoch_hold_at": str(participant["epoch_hold_at"]),
                    "prior_active_service_claim_ids": list(
                        json_loads(participant["prior_active_service_claim_ids"], [])
                    ),
                    "principal_version": int(participant["principal_version"]),
                    "principal_fingerprint": str(participant["principal_fingerprint"]),
                    "prior_live_principal_ids": list(
                        json_loads(participant["prior_live_principal_ids"], [])
                    ),
                    "prior_attestation_ciphertext_sha256": str(
                        participant["prior_attestation_ciphertext_sha256"]
                    ),
                    "prior_report_executor_projection_sha256": str(
                        participant["prior_report_executor_projection_sha256"]
                    ),
                }
            )
        if identity.get("agents") != projected_agents:
            raise TransitionError(
                "fleet release epoch participant identity storage is corrupt"
            )

    def _stored_proof_matches(
        self, epoch_row: Any, participants: Iterable[Any]
    ) -> bool:
        epoch = _row(epoch_row)
        if epoch.get("state") == "open":
            return epoch.get("proof_sha256") is None
        if epoch.get("state") == "aborted" and epoch.get("proof_sha256") is None:
            return True
        if epoch.get("state") not in {"proved", "committed", "aborted"}:
            return False
        payload = {
            "schema": EPOCH_PROOF_SCHEMA,
            "epoch_id": str(epoch["epoch_id"]),
            "hub_authority_id": self.hub_authority_id,
            "identity_sha256": str(epoch["identity_sha256"]),
            "agents": [
                {
                    "agent_id": str(participant["agent_id"]),
                    "install_receipt_sha256": participant["install_receipt_sha256"],
                    "attestation_proof_sha256": participant["attestation_proof_sha256"],
                    "report_executor_startup_timestamp": participant[
                        "report_executor_startup_timestamp"
                    ],
                }
                for participant in participants
            ],
        }
        return hmac.compare_digest(
            str(epoch.get("proof_sha256") or ""), _sha256_json(payload)
        )

    def _marker_matches(self, conn: Any, epoch_row: Any) -> bool:
        epoch = _row(epoch_row)
        marker = conn.execute(
            "SELECT event_type, detail FROM agent_lifecycle_events WHERE id = ?",
            (self._marker_id(str(epoch["epoch_id"])),),
        ).fetchone()
        if (
            marker is None
            or marker["event_type"] != "agent.dispatch_hold_epoch_committed"
        ):
            return False
        detail = ensure_json_object(json_loads(marker["detail"], {}))
        return bool(
            detail.get("schema") == EPOCH_MARKER_SCHEMA
            and detail.get("epoch_id") == epoch["epoch_id"]
            and detail.get("identity_sha256") == epoch["identity_sha256"]
            and detail.get("proof_sha256") == epoch["proof_sha256"]
        )

    def _replay_open_receipt(
        self, conn: Any, existing: Any, request_sha256: str
    ) -> Dict[str, Any]:
        if str(existing["request_sha256"]) != request_sha256:
            raise ValidationError(
                "fleet release epoch id was already used with a different request"
            )
        self._assert_epoch_identity_integrity(conn, existing)
        if existing["state"] == "committed" and not self._marker_matches(
            conn, existing
        ):
            raise TransitionError("committed fleet release marker is incomplete")
        return self._receipt(conn, existing)

    def open_epoch(
        self,
        epoch_id: str,
        participants: Iterable[Mapping[str, Any]],
        *,
        successor_hold_reason: Optional[str] = None,
        desired_policy_mode: Optional[str] = None,
        actor: str = "fleet-deploy",
    ) -> Dict[str, Any]:
        (
            epoch_hold_reason,
            normalized,
            request,
            request_sha256,
        ) = self._normalize_open(
            epoch_id,
            participants,
            successor_hold_reason=successor_hold_reason,
            desired_policy_mode=desired_policy_mode,
        )
        epoch = str(request["epoch_id"])
        actor_value = str(actor or "fleet-deploy")
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?",
                (epoch,),
            ).fetchone()
            if existing is not None:
                return self._replay_open_receipt(conn, existing, request_sha256)
            if (
                conn.execute(
                    "SELECT id FROM agent_lifecycle_events WHERE id = ?",
                    (self._marker_id(epoch),),
                ).fetchone()
                is not None
            ):
                raise ValidationError(
                    "fleet release epoch id was already used by a legacy release"
                )

            locked_rows: Dict[str, Any] = {}
            for item in normalized:
                agent_id = str(item["agent_id"])
                locked_rows[agent_id] = self._lock_agent(conn, agent_id)
            # A concurrent same-epoch opener may have committed while this
            # transaction waited for the first agent row.  Re-read under the
            # acquired cohort locks before classifying its reservation as a
            # competing epoch.
            existing = conn.execute(
                "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?",
                (epoch,),
            ).fetchone()
            if existing is not None:
                return self._replay_open_receipt(conn, existing, request_sha256)
            if (
                conn.execute(
                    "SELECT id FROM agent_lifecycle_events WHERE id = ?",
                    (self._marker_id(epoch),),
                ).fetchone()
                is not None
            ):
                raise ValidationError(
                    "fleet release epoch id was already used by a legacy release"
                )
            for item in normalized:
                agent_id = str(item["agent_id"])
                self.assert_agent_unreserved_in_transaction(conn, agent_id)
            policy_snapshot = self._policy_snapshot(conn)
            prepared_at = utcnow()
            identity_agents: List[Dict[str, Any]] = []
            staged: List[Dict[str, Any]] = []
            for ordinal, item in enumerate(normalized):
                agent_id = str(item["agent_id"])
                agent_row = locked_rows[agent_id]
                if not self._prior_hold_matches(agent_row, item):
                    raise ValidationError(
                        "fleet release open lost expected prior hold for %s" % agent_id
                    )
                try:
                    principal = self.credentials.stage_pending_in_transaction(
                        conn, agent_id, item["principal_id"]
                    )
                except WorkerCredentialError as exc:
                    raise ValidationError(str(exc)) from exc
                prior_claims = self._active_claims(conn, agent_id)
                if item["expected_dispatch_hold"] and prior_claims:
                    raise ValidationError(
                        "held agent unexpectedly owns active service claims: %s"
                        % agent_id
                    )
                resources = ensure_json_object(json_loads(agent_row["resources"], {}))
                live_principals = self._live_principals(conn, agent_id)
                if str(item["principal_id"]) not in live_principals:
                    raise TransitionError(
                        "staged worker principal is absent from live identity inventory"
                    )
                staged_item = {
                    **item,
                    "ordinal": ordinal,
                    "prior_hold_at": agent_row["dispatch_hold_at"],
                    "epoch_hold_at": prepared_at,
                    "prior_active_service_claim_ids": prior_claims,
                    "principal_version": int(principal["credential_version"]),
                    "principal_fingerprint": str(principal["token_fingerprint"]),
                    "prior_live_principal_ids": [
                        principal_id
                        for principal_id in live_principals
                        if principal_id != str(item["principal_id"])
                    ],
                    "prior_attestation_ciphertext_sha256": _sha256_text(
                        agent_row["attestation_key_ciphertext"]
                    ),
                    "prior_report_executor_projection_sha256": _sha256_json(
                        self._report_resource_projection(resources)
                    ),
                }
                staged.append(staged_item)
                identity_agents.append(
                    {
                        **request["agents"][ordinal],
                        "prior_hold_at": staged_item["prior_hold_at"],
                        "epoch_hold_at": prepared_at,
                        "prior_active_service_claim_ids": prior_claims,
                        "principal_version": staged_item["principal_version"],
                        "principal_fingerprint": staged_item["principal_fingerprint"],
                        "prior_live_principal_ids": staged_item[
                            "prior_live_principal_ids"
                        ],
                        "prior_attestation_ciphertext_sha256": staged_item[
                            "prior_attestation_ciphertext_sha256"
                        ],
                        "prior_report_executor_projection_sha256": staged_item[
                            "prior_report_executor_projection_sha256"
                        ],
                    }
                )
            identity = {
                **request,
                "request_sha256": request_sha256,
                "policy_snapshot": policy_snapshot,
                "agents": identity_agents,
            }
            identity_sha256 = _sha256_json(identity)
            conn.execute(
                "INSERT INTO fleet_release_epochs ("
                "epoch_id, request_sha256, identity_sha256, identity_payload, "
                "state, successor_hold_reason, desired_policy_mode, "
                "policy_snapshot, actor, prepared_at"
                ") VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (
                    epoch,
                    request_sha256,
                    identity_sha256,
                    _canonical_json(identity),
                    request["successor_hold_reason"],
                    request["desired_worker_credential_mode"],
                    _canonical_json(policy_snapshot),
                    actor_value,
                    prepared_at,
                ),
            )
            for item in staged:
                agent_id = str(item["agent_id"])
                conn.execute(
                    "INSERT INTO fleet_release_epoch_agents ("
                    "epoch_id, agent_id, ordinal, open_state, "
                    "prior_dispatch_hold, prior_hold_reason, prior_hold_at, "
                    "epoch_hold_reason, epoch_hold_at, "
                    "prior_active_service_claim_ids, generation, baseline_seen, "
                    "principal_id, principal_version, principal_fingerprint, "
                    "prior_live_principal_ids, "
                    "prior_attestation_ciphertext_sha256, "
                    "attestation_candidate_fingerprint, "
                    "report_executor_action, "
                    "prior_report_executor_projection_sha256, "
                    "report_executor_attestation, created_at"
                    ") VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        epoch,
                        agent_id,
                        item["ordinal"],
                        int(item["expected_dispatch_hold"]),
                        item["expected_hold_reason"],
                        item["prior_hold_at"],
                        epoch_hold_reason,
                        item["epoch_hold_at"],
                        _canonical_json(item["prior_active_service_claim_ids"]),
                        item["generation"],
                        item["baseline_seen"],
                        item["principal_id"],
                        item["principal_version"],
                        item["principal_fingerprint"],
                        _canonical_json(item["prior_live_principal_ids"]),
                        item["prior_attestation_ciphertext_sha256"],
                        item["attestation_candidate_fingerprint"],
                        item["report_executor_action"],
                        item["prior_report_executor_projection_sha256"],
                        (
                            _canonical_json(item["report_executor_attestation"])
                            if item["report_executor_attestation"] is not None
                            else None
                        ),
                        prepared_at,
                    ),
                )
                candidate_key = item["attestation_candidate_key"]
                if candidate_key is not None:
                    conn.execute(
                        "INSERT INTO fleet_release_attestation_candidates "
                        "(epoch_id, agent_id, key_ciphertext, key_fingerprint, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            epoch,
                            agent_id,
                            self.control_plane.secrets._encrypt(candidate_key),
                            item["attestation_candidate_fingerprint"],
                            prepared_at,
                        ),
                    )
                conn.execute(
                    "UPDATE agents SET dispatch_hold = 1, "
                    "dispatch_hold_reason = ?, dispatch_hold_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        epoch_hold_reason,
                        prepared_at,
                        prepared_at,
                        agent_id,
                    ),
                )
                conn.execute(
                    "UPDATE service_claims SET status = ?, updated_at = ? "
                    "WHERE agent_id = ? AND status = ?",
                    (
                        ServiceClaimStatus.RELEASED.value,
                        prepared_at,
                        agent_id,
                        ServiceClaimStatus.ACTIVE.value,
                    ),
                )
                self.control_plane._record_agent_lifecycle_event(
                    conn,
                    agent_id,
                    "agent.fleet_release_epoch.opened",
                    actor_value,
                    {
                        "epoch_id": epoch,
                        "identity_sha256": identity_sha256,
                        "epoch_hold_reason": epoch_hold_reason,
                        "principal_id": item["principal_id"],
                        "principal_fingerprint": item["principal_fingerprint"],
                        "attestation_candidate_fingerprint": item[
                            "attestation_candidate_fingerprint"
                        ],
                        "report_executor_action": item["report_executor_action"],
                    },
                    prepared_at,
                )
            row = conn.execute(
                "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?",
                (epoch,),
            ).fetchone()
            return self._receipt(conn, row)

    def _load_exact(self, conn: Any, epoch_id: str, identity_sha256: str) -> Any:
        epoch = _text(epoch_id, "fleet release epoch id")
        digest = _digest(identity_sha256)
        row = conn.execute(
            "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?", (epoch,)
        ).fetchone()
        if row is None:
            raise NotFoundError("fleet release epoch not found: %s" % epoch)
        if not hmac.compare_digest(str(row["identity_sha256"]), digest):
            raise ValidationError("fleet release epoch identity digest does not match")
        self._assert_epoch_identity_integrity(conn, row)
        return row

    def _epoch_hold_matches(self, agent_row: Any, participant: Any) -> bool:
        return bool(
            agent_row["dispatch_hold"]
            and agent_row["dispatch_hold_reason"] == participant["epoch_hold_reason"]
            and agent_row["dispatch_hold_at"] == participant["epoch_hold_at"]
        )

    @staticmethod
    def _restored_prior_hold_matches(agent_row: Any, participant: Any) -> bool:
        """Recognize the exact pre-epoch hold snapshot during abort recovery."""

        if participant["prior_dispatch_hold"]:
            return bool(
                agent_row["dispatch_hold"]
                and agent_row["dispatch_hold_reason"]
                == participant["prior_hold_reason"]
                and agent_row["dispatch_hold_at"] == participant["prior_hold_at"]
            )
        return bool(
            not agent_row["dispatch_hold"]
            and agent_row["dispatch_hold_reason"] is None
            and agent_row["dispatch_hold_at"] is None
        )

    @staticmethod
    def _superseding_hold_matches(agent_row: Any, participant: Any) -> bool:
        """Recognize a later safety hold that abort must preserve, never clear."""

        return bool(
            agent_row["dispatch_hold"]
            and str(agent_row["dispatch_hold_reason"] or "").strip()
            and agent_row["dispatch_hold_at"]
            and agent_row["dispatch_hold_reason"]
            != participant["epoch_hold_reason"]
        )

    def _validate_node_readiness(
        self, conn: Any, agent_row: Any, participant: Any
    ) -> Dict[str, Any]:
        agent_id = str(participant["agent_id"])
        if self._has_active_work(conn, agent_id, agent_row):
            raise ValidationError(
                "fleet release epoch found active work on %s" % agent_id
            )
        resources = ensure_json_object(json_loads(agent_row["resources"], {}))
        last_seen = str(agent_row["last_seen_at"] or "").strip()
        if (
            agent_row["status"] != AgentStatus.IDLE.value
            or agent_row["health_status"] != HealthStatus.HEALTHY.value
            or not last_seen
            or parse_time(last_seen) <= parse_time(participant["baseline_seen"])
            or resources.get("deployment_generation") != participant["generation"]
        ):
            raise ValidationError(
                "fleet release epoch lost node readiness for %s" % agent_id
            )
        return resources

    def _candidate_key(
        self, conn: Any, epoch_id: str, participant: Any
    ) -> Optional[str]:
        fingerprint = participant["attestation_candidate_fingerprint"]
        if fingerprint is None:
            return None
        row = conn.execute(
            "SELECT key_ciphertext, key_fingerprint "
            "FROM fleet_release_attestation_candidates "
            "WHERE epoch_id = ? AND agent_id = ?",
            (epoch_id, participant["agent_id"]),
        ).fetchone()
        if row is None or row["key_fingerprint"] != fingerprint:
            raise TransitionError("staged attestation candidate is missing")
        try:
            key = self.control_plane.secrets._decrypt(row["key_ciphertext"])
        except Exception as exc:  # noqa: BLE001 - corrupt secret must fail closed.
            raise TransitionError(
                "staged attestation candidate cannot be decrypted"
            ) from exc
        if _sha256_text(key) != fingerprint:
            raise TransitionError("staged attestation candidate digest differs")
        return key

    def _validate_candidate_proof(
        self,
        epoch_id: str,
        participant: Any,
        key: Optional[str],
        proof: Any,
    ) -> Optional[Dict[str, Any]]:
        if key is None:
            if proof is not None:
                raise ValidationError(
                    "attestation proof supplied without a staged candidate"
                )
            return None
        if not isinstance(proof, Mapping) or set(proof) != {
            "challenge",
            "signature",
        }:
            raise ValidationError(
                "attestation candidate proof has unexpected or missing fields"
            )
        challenge = proof.get("challenge")
        expected_keys = {
            "schema",
            "purpose",
            "epoch_id",
            "agent_id",
            "generation",
            "principal_id",
            "candidate_fingerprint",
            "nonce",
        }
        if (
            not isinstance(challenge, Mapping)
            or set(challenge) != expected_keys
            or challenge.get("schema") != ATTESTATION_PROOF_SCHEMA
            or challenge.get("purpose") != ATTESTATION_PROOF_PURPOSE
            or challenge.get("epoch_id") != epoch_id
            or challenge.get("agent_id") != participant["agent_id"]
            or challenge.get("generation") != participant["generation"]
            or challenge.get("principal_id") != participant["principal_id"]
            or challenge.get("candidate_fingerprint")
            != participant["attestation_candidate_fingerprint"]
            or len(str(challenge.get("nonce") or "")) < 32
        ):
            raise ValidationError("attestation candidate proof challenge is invalid")
        normalized = json.loads(_canonical_json(proof))
        if not self._verify_signature(
            key,
            normalized["challenge"],
            str(normalized["signature"] or ""),
        ):
            raise ValidationError("attestation candidate proof signature is invalid")
        return normalized

    def _validate_report_approval(
        self,
        participant: Any,
        resources: Mapping[str, Any],
        startup_timestamp: Optional[str],
    ) -> None:
        if participant["report_executor_action"] != "approve":
            if startup_timestamp is not None:
                raise ValidationError(
                    "report startup proof supplied without staged approval"
                )
            return
        timestamp = _text(startup_timestamp, "report executor startup timestamp")
        attestation = ensure_json_object(
            json_loads(participant["report_executor_attestation"], {})
        )
        if resources.get(REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY) != attestation:
            raise ValidationError(
                "report executor attestation differs from staged approval"
            )
        if resources.get("openshell_required") is not True:
            raise ValidationError("report executor requires OpenShell policy")
        if not self.control_plane._report_executor_startup_proof_matches(
            str(participant["agent_id"]),
            resources,
            attestation,
            timestamp,
        ):
            raise ValidationError(
                "report executor startup proof does not match staged approval"
            )

    def _proof_request_sha256(
        self,
        epoch_id: str,
        identity_sha256: str,
        participants: Iterable[Any],
        proof_by_agent: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """Digest a proof replay without consulting mutable live authority.

        Once an epoch is terminal its candidate secret has been erased and its
        hold has moved.  Exact retries must therefore compare the caller's
        secret-free evidence envelope with the persisted proof digest rather
        than attempting node proof again.
        """

        normalized: List[Dict[str, Any]] = []
        for participant in participants:
            agent_id = str(participant["agent_id"])
            proof = proof_by_agent[agent_id]
            receipt = proof.get("install_receipt")
            if not isinstance(receipt, Mapping):
                raise ValidationError("worker install receipt is required")
            candidate_proof = proof.get("attestation_proof")
            if candidate_proof is not None and not isinstance(candidate_proof, Mapping):
                raise ValidationError("attestation candidate proof is malformed")
            startup = proof.get("report_executor_startup_timestamp")
            if startup is not None:
                startup = str(startup).strip()
            normalized.append(
                {
                    "agent_id": agent_id,
                    "install_receipt_sha256": _sha256_json(receipt),
                    "attestation_proof_sha256": (
                        _sha256_json(candidate_proof)
                        if candidate_proof is not None
                        else None
                    ),
                    "report_executor_startup_timestamp": startup,
                }
            )
        return _sha256_json(
            {
                "schema": EPOCH_PROOF_SCHEMA,
                "epoch_id": epoch_id,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": identity_sha256,
                "agents": normalized,
            }
        )

    def prove(
        self,
        epoch_id: str,
        identity_sha256: str,
        proofs: Iterable[Mapping[str, Any]],
        *,
        actor: str = "fleet-deploy",
    ) -> Dict[str, Any]:
        proof_by_agent: Dict[str, Mapping[str, Any]] = {}
        try:
            iterator = iter(proofs)
        except TypeError as exc:
            raise ValidationError("fleet release proofs must be iterable") from exc
        for proof in iterator:
            if not isinstance(proof, Mapping):
                raise ValidationError("fleet release proof is malformed")
            agent_id = _text(proof.get("agent_id"), "proof agent id")
            if agent_id in proof_by_agent:
                raise ValidationError("duplicate fleet release proof agent")
            if set(proof) != {
                "agent_id",
                "install_receipt",
                "attestation_proof",
                "report_executor_startup_timestamp",
            }:
                raise ValidationError(
                    "fleet release proof has unexpected or missing fields"
                )
            proof_by_agent[agent_id] = proof
        actor_value = str(actor or "fleet-deploy")
        with self.store.transaction() as conn:
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            conn.execute(
                "UPDATE fleet_release_epochs SET state = state WHERE epoch_id = ?",
                (epoch_id,),
            )
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            if epoch["state"] == "aborted":
                raise TransitionError("aborted fleet release epoch cannot be proved")
            participants = conn.execute(
                "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
                "ORDER BY ordinal",
                (epoch_id,),
            ).fetchall()
            expected_ids = [str(item["agent_id"]) for item in participants]
            if sorted(proof_by_agent) != sorted(expected_ids):
                raise ValidationError(
                    "fleet release proof cohort does not match open cohort"
                )
            if epoch["state"] in {"proved", "committed"}:
                if not self._stored_proof_matches(epoch, participants):
                    raise TransitionError(
                        "fleet release epoch proof storage is corrupt"
                    )
                proof_sha256 = self._proof_request_sha256(
                    str(epoch["epoch_id"]),
                    str(epoch["identity_sha256"]),
                    participants,
                    proof_by_agent,
                )
                if epoch["proof_sha256"] != proof_sha256:
                    raise ValidationError(
                        "fleet release epoch was proved with different evidence"
                    )
                if epoch["state"] == "committed" and not self._marker_matches(
                    conn, epoch
                ):
                    raise TransitionError(
                        "committed fleet release marker is incomplete"
                    )
                return self._receipt(conn, epoch)

            normalized_proofs: List[Dict[str, Any]] = []
            validated_values: Dict[str, Dict[str, Any]] = {}
            for participant in participants:
                agent_id = str(participant["agent_id"])
                agent_row = self._lock_agent(conn, agent_id)
                if not self._epoch_hold_matches(agent_row, participant):
                    raise ValidationError(
                        "fleet release lost epoch-owned hold for %s" % agent_id
                    )
                resources = self._validate_node_readiness(conn, agent_row, participant)
                self._validate_report_authority_projection(participant, resources)
                proof = proof_by_agent[agent_id]
                receipt = proof.get("install_receipt")
                if not isinstance(receipt, Mapping):
                    raise ValidationError("worker install receipt is required")
                try:
                    self.credentials.validate_activation_in_transaction(
                        conn,
                        agent_id,
                        str(participant["principal_id"]),
                        receipt=receipt,
                        expected_epoch_id=epoch_id,
                        require_pending=True,
                    )
                except WorkerCredentialError as exc:
                    raise ValidationError(str(exc)) from exc
                receipt_value = json.loads(_canonical_json(receipt))
                candidate_key = self._candidate_key(conn, epoch_id, participant)
                candidate_proof = self._validate_candidate_proof(
                    epoch_id,
                    participant,
                    candidate_key,
                    proof.get("attestation_proof"),
                )
                startup_value = proof.get("report_executor_startup_timestamp")
                if startup_value is not None:
                    startup_value = str(startup_value).strip()
                self._validate_report_approval(participant, resources, startup_value)
                normalized = {
                    "agent_id": agent_id,
                    "install_receipt_sha256": _sha256_json(receipt_value),
                    "attestation_proof_sha256": (
                        _sha256_json(candidate_proof)
                        if candidate_proof is not None
                        else None
                    ),
                    "report_executor_startup_timestamp": startup_value,
                }
                normalized_proofs.append(normalized)
                validated_values[agent_id] = {
                    "receipt": receipt_value,
                    "candidate_proof": candidate_proof,
                    "startup_timestamp": startup_value,
                }
            proof_payload = {
                "schema": EPOCH_PROOF_SCHEMA,
                "epoch_id": epoch_id,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": identity_sha256,
                "agents": normalized_proofs,
            }
            proof_sha256 = _sha256_json(proof_payload)
            if epoch["state"] != "open":
                raise TransitionError("fleet release epoch cannot accept proof")
            proved_at = utcnow()
            for participant in participants:
                agent_id = str(participant["agent_id"])
                value = validated_values[agent_id]
                conn.execute(
                    "UPDATE fleet_release_epoch_agents SET "
                    "install_receipt = ?, install_receipt_sha256 = ?, "
                    "attestation_proof = ?, attestation_proof_sha256 = ?, "
                    "report_executor_startup_timestamp = ? "
                    "WHERE epoch_id = ? AND agent_id = ? AND open_state = 1",
                    (
                        _canonical_json(value["receipt"]),
                        _sha256_json(value["receipt"]),
                        (
                            _canonical_json(value["candidate_proof"])
                            if value["candidate_proof"] is not None
                            else None
                        ),
                        (
                            _sha256_json(value["candidate_proof"])
                            if value["candidate_proof"] is not None
                            else None
                        ),
                        value["startup_timestamp"],
                        epoch_id,
                        agent_id,
                    ),
                )
                self.control_plane._record_agent_lifecycle_event(
                    conn,
                    agent_id,
                    "agent.fleet_release_epoch.proved",
                    actor_value,
                    {
                        "epoch_id": epoch_id,
                        "identity_sha256": identity_sha256,
                        "proof_sha256": proof_sha256,
                    },
                    proved_at,
                )
            conn.execute(
                "UPDATE fleet_release_epochs SET state = 'proved', "
                "proof_sha256 = ?, proved_at = ? "
                "WHERE epoch_id = ? AND state = 'open'",
                (proof_sha256, proved_at, epoch_id),
            )
            proved = conn.execute(
                "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
            return self._receipt(conn, proved)

    def pre_prove_readiness(
        self, epoch_id: str, identity_sha256: str
    ) -> Dict[str, Any]:
        """Fail closed unless every pending cohort principal is prove-ready.

        This is an early diagnostic and timing gate only. It intentionally
        performs no mutation; ``prove`` repeats all checks transactionally.
        """

        with self.store.transaction() as conn:
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            if epoch["state"] != "open":
                raise TransitionError(
                    "fleet release epoch is not open for pre-prove readiness"
                )
            participants = conn.execute(
                "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
                "ORDER BY ordinal",
                (epoch_id,),
            ).fetchall()
            agents: List[Dict[str, Any]] = []
            for participant in participants:
                agent_id = str(participant["agent_id"])
                agent_row = conn.execute(
                    "SELECT * FROM agents WHERE id = ? AND deleted_at IS NULL",
                    (agent_id,),
                ).fetchone()
                if agent_row is None:
                    raise NotFoundError("agent not found: %s" % agent_id)
                if not self._epoch_hold_matches(agent_row, participant):
                    raise ValidationError(
                        "fleet release lost epoch-owned hold for %s" % agent_id
                    )
                resources = self._validate_node_readiness(
                    conn, agent_row, participant
                )
                self._validate_report_authority_projection(participant, resources)
                try:
                    readiness = (
                        self.credentials.validate_pending_readiness_in_transaction(
                            conn,
                            agent_id,
                            str(participant["principal_id"]),
                            expected_epoch_id=epoch_id,
                        )
                    )
                except WorkerCredentialError as exc:
                    raise ValidationError(str(exc)) from exc
                agents.append(
                    {
                        "agent_id": agent_id,
                        "credential_version": readiness["credential_version"],
                    }
                )
            return {
                "schema": EPOCH_READINESS_SCHEMA,
                "status": "ready",
                "epoch_id": str(epoch["epoch_id"]),
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": str(epoch["identity_sha256"]),
                "cohort_size": len(agents),
                "agents": agents,
            }

    def _revalidate_proved_participant(
        self, conn: Any, epoch_id: str, participant: Any
    ) -> tuple[Dict[str, Any], Optional[str]]:
        agent_id = str(participant["agent_id"])
        agent_row = self._lock_agent(conn, agent_id)
        if not self._epoch_hold_matches(agent_row, participant):
            raise ValidationError(
                "fleet release lost epoch-owned hold for %s" % agent_id
            )
        resources = self._validate_node_readiness(conn, agent_row, participant)
        if self._live_principals(conn, agent_id) != self._expected_live_principals(
            participant
        ):
            raise ValidationError(
                "worker principal set changed after fleet release open"
            )
        if (
            _sha256_text(agent_row["attestation_key_ciphertext"])
            != participant["prior_attestation_ciphertext_sha256"]
        ):
            raise ValidationError(
                "attestation authority changed after fleet release open"
            )
        self._validate_report_authority_projection(participant, resources)
        receipt = ensure_json_object(json_loads(participant["install_receipt"], {}))
        if _sha256_json(receipt) != participant["install_receipt_sha256"]:
            raise TransitionError("staged worker install receipt is corrupt")
        try:
            self.credentials.validate_activation_in_transaction(
                conn,
                agent_id,
                str(participant["principal_id"]),
                receipt=receipt,
                expected_epoch_id=epoch_id,
                require_pending=True,
            )
        except WorkerCredentialError as exc:
            raise ValidationError(str(exc)) from exc
        candidate_key = self._candidate_key(conn, epoch_id, participant)
        candidate_proof = (
            ensure_json_object(json_loads(participant["attestation_proof"], {}))
            if participant["attestation_proof"] is not None
            else None
        )
        if (
            candidate_proof is not None
            and _sha256_json(candidate_proof) != participant["attestation_proof_sha256"]
        ):
            raise TransitionError("staged attestation proof is corrupt")
        self._validate_candidate_proof(
            epoch_id, participant, candidate_key, candidate_proof
        )
        self._validate_report_approval(
            participant,
            resources,
            participant["report_executor_startup_timestamp"],
        )
        return resources, candidate_key

    def _apply_report_action(
        self,
        conn: Any,
        participant: Any,
        resources: Dict[str, Any],
        now: str,
        actor: str,
    ) -> None:
        action = str(participant["report_executor_action"])
        if action == "preserve":
            return
        agent_id = str(participant["agent_id"])
        if action == "revoke":
            resources.pop(REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY, None)
            resources.pop(REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY, None)
            event = "agent.report_repository_executor.revoked"
            detail = {
                "agent_id": agent_id,
                "reason": "synchronized fleet release desired absence",
            }
        else:
            attestation = ensure_json_object(
                json_loads(participant["report_executor_attestation"], {})
            )
            approval = read_only_report_repository_executor_approval(
                runtime_image_ref=str(attestation["runtime_image_ref"]),
                policy_sha256=str(attestation["policy_sha256"]),
                openshell_bin_path=str(attestation["openshell_bin_path"]),
                openshell_bin_sha256=str(attestation["openshell_bin_sha256"]),
                executor_path=str(attestation["executor_path"]),
                executor_sha256=str(attestation["executor_sha256"]),
                platform=str(attestation["platform"]),
                isolation_posture=str(attestation["isolation_posture"]),
                python_path=str(attestation["python_path"]),
                python_sha256=str(attestation["python_sha256"]),
                executor_script_path=str(attestation["executor_script_path"]),
                executor_script_sha256=str(attestation["executor_script_sha256"]),
                source_root=str(attestation["source_root"]),
                source_bundle_sha256=str(attestation["source_bundle_sha256"]),
            )
            resources[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] = approval
            resources = self.control_plane._project_report_repository_executor_marker(
                resources
            )
            if not agent_has_read_only_report_repository_executor(resources):
                raise ValidationError(
                    "report executor controller marker was not derived"
                )
            event = "agent.report_repository_executor.approved"
            detail = {
                "agent_id": agent_id,
                "startup_timestamp": participant["report_executor_startup_timestamp"],
                "runtime_image_ref": attestation["runtime_image_ref"],
                "policy_sha256": attestation["policy_sha256"],
                "source_bundle_sha256": attestation["source_bundle_sha256"],
            }
        conn.execute(
            "UPDATE agents SET resources = ?, updated_at = ? WHERE id = ?",
            (json_dumps(resources), now, agent_id),
        )
        self.control_plane._record_agent_lifecycle_event(
            conn, agent_id, event, actor, detail, now
        )

    def commit(
        self,
        epoch_id: str,
        identity_sha256: str,
        *,
        actor: str = "fleet-deploy",
    ) -> Dict[str, Any]:
        actor_value = str(actor or "fleet-deploy")
        with self.store.transaction() as conn:
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            conn.execute(
                "UPDATE fleet_release_epochs SET state = state WHERE epoch_id = ?",
                (epoch_id,),
            )
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            if epoch["state"] == "aborted":
                raise TransitionError("aborted fleet release epoch cannot commit")
            if epoch["state"] == "committed":
                if not self._marker_matches(conn, epoch):
                    raise TransitionError(
                        "committed fleet release marker is incomplete"
                    )
                return self._receipt(conn, epoch)
            if epoch["state"] != "proved" or not epoch["proof_sha256"]:
                raise TransitionError(
                    "fleet release epoch requires exact full-cohort proof"
                )
            participants = conn.execute(
                "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
                "ORDER BY ordinal",
                (epoch_id,),
            ).fetchall()
            if not self._stored_proof_matches(epoch, participants):
                raise TransitionError("fleet release epoch proof storage is corrupt")
            for participant in participants:
                self._lock_agent(conn, str(participant["agent_id"]))
            if epoch["desired_policy_mode"] is not None and self._policy_snapshot(
                conn
            ) != ensure_json_object(json_loads(epoch["policy_snapshot"], {})):
                raise ValidationError(
                    "worker credential policy changed after fleet release open"
                )
            validated: Dict[str, tuple[Dict[str, Any], Optional[str]]] = {}
            for participant in participants:
                agent_id = str(participant["agent_id"])
                if self._active_claims(conn, agent_id):
                    raise ValidationError(
                        "fleet release commit found new active service claims for %s"
                        % agent_id
                    )
                validated[str(participant["agent_id"])] = (
                    self._revalidate_proved_participant(conn, epoch_id, participant)
                )
            now = utcnow()
            for participant in participants:
                agent_id = str(participant["agent_id"])
                receipt = ensure_json_object(
                    json_loads(participant["install_receipt"], {})
                )
                try:
                    self.credentials.promote_in_transaction(
                        conn,
                        agent_id,
                        str(participant["principal_id"]),
                        receipt=receipt,
                        actor=actor_value,
                        expected_epoch_id=epoch_id,
                        require_pending=True,
                    )
                except WorkerCredentialError as exc:
                    raise ValidationError(str(exc)) from exc
                resources, candidate_key = validated[agent_id]
                if candidate_key is not None:
                    conn.execute(
                        "UPDATE agents SET "
                        "attestation_key_prev_ciphertext = attestation_key_ciphertext, "
                        "attestation_key_ciphertext = ?, "
                        "attestation_key_rotated_at = ?, updated_at = ? WHERE id = ?",
                        (
                            self.control_plane.secrets._encrypt(candidate_key),
                            now,
                            now,
                            agent_id,
                        ),
                    )
                    self.control_plane._record_agent_lifecycle_event(
                        conn,
                        agent_id,
                        "agent.attestation_key.promoted",
                        actor_value,
                        {
                            "agent_id": agent_id,
                            "epoch_id": epoch_id,
                            "candidate_fingerprint": participant[
                                "attestation_candidate_fingerprint"
                            ],
                        },
                        now,
                    )
                self._apply_report_action(
                    conn,
                    participant,
                    dict(resources),
                    now,
                    actor_value,
                )
            desired_policy = epoch["desired_policy_mode"]
            if desired_policy is not None:
                reviewed = live_inventory_in_transaction(conn, desired_policy)
                try:
                    write_policy_state_in_transaction(
                        conn,
                        desired_policy,
                        reviewed_inventory=reviewed,
                        actor=actor_value,
                    )
                except WorkerCredentialError as exc:
                    raise ValidationError(str(exc)) from exc
            successor = epoch["successor_hold_reason"]
            event_type = (
                "agent.dispatch_hold_epoch_transitioned"
                if successor is not None
                else "agent.dispatch_hold_epoch_released"
            )
            for participant in participants:
                agent_id = str(participant["agent_id"])
                current = conn.execute(
                    "SELECT * FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if not self._epoch_hold_matches(current, participant):
                    raise ValidationError("fleet release lost epoch hold before commit")
                if successor is None:
                    conn.execute(
                        "UPDATE agents SET dispatch_hold = 0, "
                        "dispatch_hold_reason = NULL, dispatch_hold_at = NULL, "
                        "updated_at = ? WHERE id = ?",
                        (now, agent_id),
                    )
                else:
                    conn.execute(
                        "UPDATE agents SET dispatch_hold = 1, "
                        "dispatch_hold_reason = ?, dispatch_hold_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        (successor, now, now, agent_id),
                    )
                self.control_plane._record_agent_lifecycle_event(
                    conn,
                    agent_id,
                    event_type,
                    actor_value,
                    {
                        "agent_id": agent_id,
                        "epoch_id": epoch_id,
                        "identity_sha256": identity_sha256,
                        "proof_sha256": epoch["proof_sha256"],
                        "epoch_hold_reason": participant["epoch_hold_reason"],
                        "successor_hold_reason": successor,
                    },
                    now,
                )
            marker = {
                "schema": EPOCH_MARKER_SCHEMA,
                "epoch_id": epoch_id,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": identity_sha256,
                "proof_sha256": epoch["proof_sha256"],
                "agent_ids": [
                    str(participant["agent_id"]) for participant in participants
                ],
                "successor_hold_reason": successor,
                "desired_worker_credential_mode": desired_policy,
            }
            inserted = conn.execute(
                "INSERT INTO agent_lifecycle_events "
                "(id, agent_id, event_type, actor, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (
                    self._marker_id(epoch_id),
                    participants[0]["agent_id"],
                    "agent.dispatch_hold_epoch_committed",
                    actor_value,
                    json_dumps(marker),
                    now,
                ),
            )
            if inserted.rowcount != 1:
                raise ValidationError("fleet release epoch marker was already claimed")
            conn.execute(
                "DELETE FROM fleet_release_attestation_candidates WHERE epoch_id = ?",
                (epoch_id,),
            )
            conn.execute(
                "UPDATE fleet_release_epoch_agents SET open_state = 0 "
                "WHERE epoch_id = ?",
                (epoch_id,),
            )
            conn.execute(
                "UPDATE fleet_release_epochs SET state = 'committed', "
                "committed_at = ? WHERE epoch_id = ? AND state = 'proved'",
                (now, epoch_id),
            )
            committed = conn.execute(
                "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
            return self._receipt(conn, committed)

    def abort(
        self,
        epoch_id: str,
        identity_sha256: str,
        *,
        reason: str,
        disposition: str = ABORT_DISPOSITION_AUTO,
        actor: str = "fleet-deploy",
    ) -> Dict[str, Any]:
        reason_value = _text(reason, "fleet release abort reason", 1024)
        actor_value = str(actor or "fleet-deploy")
        disposition_value = str(disposition or ABORT_DISPOSITION_AUTO).strip()
        if disposition_value not in ABORT_DISPOSITIONS:
            raise ValidationError("fleet release abort disposition is invalid")
        with self.store.transaction() as conn:
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            conn.execute(
                "UPDATE fleet_release_epochs SET state = state WHERE epoch_id = ?",
                (epoch_id,),
            )
            epoch = self._load_exact(conn, epoch_id, identity_sha256)
            if epoch["state"] == "committed":
                raise TransitionError("committed fleet release epoch cannot abort")
            if epoch["state"] == "aborted":
                if str(epoch["abort_reason"] or "") != reason_value:
                    raise ValidationError(
                        "fleet release epoch was aborted with a different reason"
                    )
                prior_disposition = str(
                    epoch["abort_disposition"] or ABORT_DISPOSITION_AUTO
                )
                if (
                    disposition_value != ABORT_DISPOSITION_AUTO
                    and disposition_value != prior_disposition
                ):
                    raise ValidationError(
                        "fleet release epoch was aborted with a different disposition"
                    )
                return self._receipt(conn, epoch)
            participants = conn.execute(
                "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
                "ORDER BY ordinal",
                (epoch_id,),
            ).fetchall()
            # A pending credential whose install receipt is recorded has already
            # been written into node ``mac.env`` and is the identity the node is
            # currently heartbeating with. Discarding it after install is what
            # left healthy static nodes in a 403 outage: the epoch never
            # committed, so the hub still treats the predecessor as authoritative
            # while the node presents the now-revoked successor. Abort must not
            # perform that destructive revoke implicitly.
            installed_agents = {
                str(participant["agent_id"])
                for participant in participants
                if participant["install_receipt_sha256"] is not None
            }
            if installed_agents and disposition_value == ABORT_DISPOSITION_AUTO:
                raise TransitionError(
                    "fleet release abort refuses to revoke installed pending "
                    "credentials for %s; choose an explicit recovery disposition "
                    "(retain_installed to keep the proven predecessor projection "
                    "authenticating, or discard_installed to force the "
                    "destructive revoke)"
                    % ", ".join(sorted(installed_agents))
                )
            locked: Dict[str, Any] = {}
            preserve_superseding_hold: set[str] = set()
            for participant in participants:
                agent_id = str(participant["agent_id"])
                locked[agent_id] = self._lock_agent(conn, agent_id)
                epoch_owned = self._epoch_hold_matches(
                    locked[agent_id], participant
                )
                prior_restored = self._restored_prior_hold_matches(
                    locked[agent_id], participant
                )
                superseding_hold = (
                    not epoch_owned
                    and not prior_restored
                    and self._superseding_hold_matches(
                        locked[agent_id], participant
                    )
                )
                if not (epoch_owned or prior_restored or superseding_hold):
                    raise ValidationError(
                        "fleet release abort lost epoch-owned hold and did not find "
                        "the exact prior snapshot or a superseding safety hold for %s"
                        % agent_id
                    )
                if superseding_hold:
                    preserve_superseding_hold.add(agent_id)
                if self._active_claims(conn, agent_id):
                    raise ValidationError(
                        "fleet release abort found new active service claims for %s"
                        % agent_id
                    )
            now = utcnow()
            for participant in participants:
                agent_id = str(participant["agent_id"])
                if self._live_principals(
                    conn, agent_id
                ) != self._expected_live_principals(participant):
                    raise ValidationError(
                        "worker principal set changed after fleet release open"
                    )
                prior_claims = list(
                    json_loads(participant["prior_active_service_claim_ids"], [])
                )
                for claim_id in prior_claims:
                    restored = conn.execute(
                        "UPDATE service_claims SET status = ?, updated_at = ? "
                        "WHERE id = ? AND agent_id = ? AND status = ?",
                        (
                            ServiceClaimStatus.ACTIVE.value,
                            now,
                            claim_id,
                            agent_id,
                            ServiceClaimStatus.RELEASED.value,
                        ),
                    )
                    if restored.rowcount != 1:
                        raise ValidationError(
                            "fleet release abort could not restore exact service claim"
                        )
                retained_installed = (
                    agent_id in installed_agents
                    and disposition_value == ABORT_DISPOSITION_RETAIN_INSTALLED
                )
                if not retained_installed:
                    try:
                        self.credentials.discard_pending_in_transaction(
                            conn,
                            agent_id,
                            str(participant["principal_id"]),
                            expected_epoch_id=epoch_id,
                            actor=actor_value,
                        )
                    except WorkerCredentialError as exc:
                        raise ValidationError(str(exc)) from exc
                if agent_id in preserve_superseding_hold:
                    # A later operator hold is safer than the stale pre-epoch
                    # snapshot. Abort releases epoch ownership but must not
                    # overwrite that newer dispatch barrier.
                    pass
                elif participant["prior_dispatch_hold"]:
                    conn.execute(
                        "UPDATE agents SET dispatch_hold = 1, "
                        "dispatch_hold_reason = ?, dispatch_hold_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            participant["prior_hold_reason"],
                            participant["prior_hold_at"],
                            now,
                            agent_id,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE agents SET dispatch_hold = 0, "
                        "dispatch_hold_reason = NULL, dispatch_hold_at = NULL, "
                        "updated_at = ? WHERE id = ?",
                        (now, agent_id),
                    )
                self.control_plane._record_agent_lifecycle_event(
                    conn,
                    agent_id,
                    "agent.fleet_release_epoch.aborted",
                    actor_value,
                    {
                        "epoch_id": epoch_id,
                        "identity_sha256": identity_sha256,
                        "reason": reason_value,
                        "prior_dispatch_hold": bool(participant["prior_dispatch_hold"]),
                        "prior_hold_reason": participant["prior_hold_reason"],
                        "preserved_superseding_hold": (
                            agent_id in preserve_superseding_hold
                        ),
                        "preserved_hold_reason": (
                            locked[agent_id]["dispatch_hold_reason"]
                            if agent_id in preserve_superseding_hold
                            else None
                        ),
                        "disposition": disposition_value,
                        "retained_installed_pending": retained_installed,
                    },
                    now,
                )
            conn.execute(
                "DELETE FROM fleet_release_attestation_candidates WHERE epoch_id = ?",
                (epoch_id,),
            )
            conn.execute(
                "UPDATE fleet_release_epoch_agents SET open_state = 0 "
                "WHERE epoch_id = ?",
                (epoch_id,),
            )
            conn.execute(
                "UPDATE fleet_release_epochs SET state = 'aborted', "
                "aborted_at = ?, abort_reason = ?, abort_disposition = ? "
                "WHERE epoch_id = ? AND state IN ('open', 'proved')",
                (now, reason_value, disposition_value, epoch_id),
            )
            aborted = conn.execute(
                "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
            return self._receipt(conn, aborted)

    def status(self, epoch_id: str, identity_sha256: str) -> Dict[str, Any]:
        epoch = _text(epoch_id, "fleet release epoch id")
        digest = _digest(identity_sha256)
        row = self.store.query_one(
            "SELECT * FROM fleet_release_epochs WHERE epoch_id = ?", (epoch,)
        )
        if row is None:
            return {
                "status": "absent",
                "epoch_id": epoch,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": digest,
            }
        if not hmac.compare_digest(str(row["identity_sha256"]), digest):
            return {
                "status": "mismatch",
                "epoch_id": epoch,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": digest,
            }
        try:
            self._assert_epoch_identity_integrity(self.store, row)
        except TransitionError:
            return {
                "status": "mismatch",
                "epoch_id": epoch,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": digest,
            }
        participants = self.store.query_all(
            "SELECT * FROM fleet_release_epoch_agents WHERE epoch_id = ? "
            "ORDER BY ordinal",
            (epoch,),
        )
        if not self._stored_proof_matches(row, participants):
            return {
                "status": "mismatch",
                "epoch_id": epoch,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": digest,
            }
        if row["state"] == "committed" and not self._marker_matches(self.store, row):
            return {
                "status": "mismatch",
                "epoch_id": epoch,
                "hub_authority_id": self.hub_authority_id,
                "identity_sha256": digest,
            }
        return self._receipt(self.store, row)
