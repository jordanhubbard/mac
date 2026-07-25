"""Durable, per-agent worker credentials and mixed-version rollout policy.

The shared hub database stores only bearer-token hashes. Raw worker tokens
exist only in a one-time installation manifest and at the worker secret
destination. Rotation deliberately overlaps the old and new hashes until the
operator confirms installation, so a failed deploy cannot strand a worker.

This module is independent of the task ledger.  It provides the identity and
readiness facts that the API/dispatcher consume:

* compatibility mode permits an unbound legacy worker on the ordinary fast
  lane, but never on package-linked work;
* enforced mode requires an exact ``principal.agent_id == actor`` binding;
* the flip to enforced mode refuses unless every active worker has proved the
  installed principal plus compatible source, runtime, and capability state.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mac import mac_paths
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mac.client_principals import (
    ClientPrincipalError,
    _atomic_json,
    _parse_timestamp,
    _timestamp,
    _validate_agent_id,
)
from mac.models import TransitionError, json_dumps, json_loads, new_id
from mac.store import SQLiteStore, Store, StoreError, make_store_from_env


WORKER_METADATA_SCHEMA = "mac.worker_credential_metadata.v1"
INSTALL_MANIFEST_SCHEMA = "mac.worker_credential_install.v1"
INSTALL_RECEIPT_SCHEMA = "mac.worker_credential_install_receipt.v1"
RESOURCE_PROOF_SCHEMA = "mac.worker_credential_proof.v1"
AUTHENTICATED_PROOF_SCHEMA = "mac.worker_credential_authenticated.v1"
DESTINATION_VERIFICATION_SCHEMA = "mac.worker_credential_destination_verification.v1"
INVENTORY_SCHEMA = "mac.worker_credential_readiness.v1"
POLICY_SCHEMA = "mac.worker_credential_policy.v1"
FLEET_SOURCE_RUNTIME_SCHEMA = "mac.fleet_source_runtime.v1"

MODE_COMPATIBILITY = "compatibility"
MODE_ENFORCED = "enforced"
POLICY_MODES = frozenset({MODE_COMPATIBILITY, MODE_ENFORCED})

PACKAGE_CAPABILITY = "work_package_v1"
WORKER_SCOPES = ("agent", "dispatch", "read", "write")
ACTIVE_AGENT_STATUSES = frozenset({"idle", "busy", "draining"})

_K8S_NAME = re.compile(r"[^a-z0-9-]+")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")

# In the compatibility bootstrap these gateway aliases may carry the same
# shared hub bearer as MAC_WORKER_TOKEN.  A successful bound-token install must
# rotate every exact copy together; otherwise the worker would stop using the
# shared token for task calls while still retaining reusable hub authority in
# its environment.  Values that differ are real upstream-provider credentials
# and are deliberately left untouched.
_HUB_FACING_WORKER_TOKEN_ALIASES = (
    "OPENAI_API_KEY",
    "MAC_HERMES_GATEWAY_API_KEY",
    "ACC_HERMES_GATEWAY_API_KEY",
    "NVIDIA_API_KEY",
)


class WorkerCredentialError(ValueError):
    """A worker credential or rollout invariant was violated."""


def ensure_fleet_source_runtime(
    store: Store,
    source_commit: str,
    *,
    created_by: str = "fleet-deploy",
) -> Dict[str, Any]:
    """Register the exact source checkout a fleet heartbeat will declare.

    Heartbeats deliberately reject unknown runtime digests. Fleet deploy must
    therefore create this durable runtime authority before issuing a bound
    credential that requires it. The deterministic name and manifest make the
    operation safe to replay and safe for concurrent deploys; an existing name
    with different content fails closed instead of silently changing identity.
    """

    exact_commit = str(source_commit or "").strip()
    if _SOURCE_COMMIT.fullmatch(exact_commit) is None:
        raise WorkerCredentialError(
            "fleet source runtime requires a lowercase 40-character commit SHA"
        )
    actor = str(created_by or "").strip()
    if not actor:
        raise WorkerCredentialError("fleet source runtime created_by is required")

    runtime_name = "mac-fleet-source-%s" % exact_commit
    manifest = {
        "schema": FLEET_SOURCE_RUNTIME_SCHEMA,
        "source_commit": exact_commit,
        "kind": "mac-fleet-source",
        "provisioner_contract": "mac-fleet-deploy-v1",
    }
    manifest_json = json_dumps(manifest)
    runtime_digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    runtime_id = new_id("runtime")
    created_at = _timestamp()

    with store.transaction() as conn:
        insert = conn.execute(
            "INSERT INTO runtime_environments "
            "(id, name, manifest, digest, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
            (
                runtime_id,
                runtime_name,
                manifest_json,
                runtime_digest,
                actor,
                created_at,
            ),
        )
        created = bool(int(getattr(insert, "rowcount", 0) or 0))
        row = conn.execute(
            "SELECT id, name, manifest, digest, created_by, created_at "
            "FROM runtime_environments WHERE name = ?",
            (runtime_name,),
        ).fetchone()
        if row is None:
            raise WorkerCredentialError(
                "fleet source runtime registration did not become visible"
            )
        stored_manifest = json_loads(_row_value(row, "manifest", "{}"), {})
        stored_digest = str(_row_value(row, "digest", ""))
        if stored_manifest != manifest or stored_digest != runtime_digest:
            raise WorkerCredentialError(
                "fleet source runtime name already exists with different identity"
            )

    return {
        "schema": FLEET_SOURCE_RUNTIME_SCHEMA,
        "status": "ready",
        "source_commit": exact_commit,
        "runtime_id": str(_row_value(row, "id", "")),
        "runtime_name": runtime_name,
        "runtime_digest": runtime_digest,
        "created": created,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _worker_key(agent_id: str) -> str:
    exact = _validate_agent_id(agent_id)
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()[:24]


def _worker_principal_id(agent_id: str, version: int) -> str:
    return "worker-%s-v%04d" % (_worker_key(agent_id), int(version))


def default_policy_path() -> Path:
    configured = os.environ.get("MAC_WORKER_CREDENTIAL_POLICY_FILE")
    if configured:
        return Path(configured).expanduser()
    mac_home = mac_paths.mac_home()
    return mac_home / "worker-credential-policy.json"


def _metadata(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Project the normalized DB record through the legacy metadata shape."""

    value = record.get("credential_metadata")
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "schema": WORKER_METADATA_SCHEMA,
        "worker_credential_version": int(record.get("credential_version") or 0),
        "state": str(record.get("state") or ""),
        "environment": str(record.get("environment") or ""),
        "expected_source_commit": str(record.get("expected_source_commit") or ""),
        "expected_runtime_digest": str(record.get("expected_runtime_digest") or ""),
        "required_capabilities": list(record.get("required_capabilities") or []),
        "package_capable": bool(record.get("package_capable")),
    }


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _record_from_row(row: Any, *, include_hash: bool = False) -> Dict[str, Any]:
    record = {
        "id": str(_row_value(row, "id", "")),
        "agent_id": str(_row_value(row, "agent_id", "")),
        "fleet": str(_row_value(row, "fleet", "")),
        "credential_version": int(_row_value(row, "credential_version", 0)),
        "token_fingerprint": str(_row_value(row, "token_fingerprint", "")),
        "scopes": list(json_loads(_row_value(row, "scopes", "[]"), [])),
        "environment": str(_row_value(row, "environment", "")),
        "expected_source_commit": str(_row_value(row, "expected_source_commit", "")),
        "expected_runtime_digest": str(_row_value(row, "expected_runtime_digest", "")),
        "required_capabilities": list(
            json_loads(_row_value(row, "required_capabilities", "[]"), [])
        ),
        "package_capable": bool(_row_value(row, "package_capable", False)),
        "state": str(_row_value(row, "state", "")),
        "destination": str(_row_value(row, "destination", "")),
        "issued_at": str(_row_value(row, "issued_at", "")),
        "expires_at": str(_row_value(row, "expires_at", "")),
        "activated_at": _row_value(row, "activated_at"),
        "revoked_at": _row_value(row, "revoked_at"),
        "superseded_by": _row_value(row, "superseded_by"),
        "created_by": str(_row_value(row, "created_by", "")),
        "updated_at": str(_row_value(row, "updated_at", "")),
        "principal_kind": "worker",
    }
    if include_hash:
        record["token_hash"] = str(_row_value(row, "token_hash", ""))
    record["credential_metadata"] = _metadata(record)
    return record


def _worker_records(
    records: Iterable[Mapping[str, Any]], agent_id: str
) -> List[Dict[str, Any]]:
    result = [
        dict(value)
        for value in records
        if isinstance(value, Mapping) and value.get("agent_id") == agent_id
    ]
    result.sort(key=lambda item: int(item.get("credential_version") or 0))
    return result


def _not_expired(record: Mapping[str, Any], now: Optional[datetime] = None) -> bool:
    try:
        expires = _parse_timestamp(record.get("expires_at"))
    except ClientPrincipalError:
        return False
    return bool(expires and expires > (now or _utcnow()).astimezone(timezone.utc))


@dataclass(frozen=True)
class WorkerCredentialIssue:
    credential_record: Dict[str, Any]
    raw_token: str
    worker_version: int

    @property
    def record(self) -> Dict[str, Any]:
        return dict(self.credential_record)

    @property
    def token(self) -> str:
        return self.raw_token


class WorkerCredentialLifecycle:
    """Overlap-safe issuance, activation, rotation, and revocation."""

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _event(
        conn: Any,
        record: Mapping[str, Any],
        event_type: str,
        *,
        actor: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        safe_detail = {
            "schema": "mac.worker_credential_event.v1",
            "credential_version": int(record.get("credential_version") or 0),
            "token_fingerprint": str(record.get("token_fingerprint") or ""),
            "state": str(record.get("state") or ""),
            **dict(detail or {}),
        }
        conn.execute(
            "INSERT INTO worker_credential_events ("
            "id, principal_id, agent_id, event_type, actor, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("workercred-event"),
                record["id"],
                record["agent_id"],
                event_type,
                str(actor or "operator"),
                json_dumps(safe_detail),
                _timestamp(),
            ),
        )

    @staticmethod
    def _assert_release_epoch_reservation(
        conn: Any,
        agent_id: str,
        *,
        expected_epoch_id: Optional[str] = None,
    ) -> None:
        """Fence ordinary credential changes while a cutover owns the agent."""

        reservation = conn.execute(
            "SELECT epoch_id FROM fleet_release_epoch_agents "
            "WHERE agent_id = ? AND open_state = 1",
            (agent_id,),
        ).fetchone()
        if expected_epoch_id is None:
            if reservation is not None:
                raise WorkerCredentialError(
                    "worker credential is reserved by an open fleet release epoch"
                )
            return
        if reservation is None or str(reservation["epoch_id"]) != expected_epoch_id:
            raise WorkerCredentialError(
                "worker credential is not reserved by the committing fleet release epoch"
            )

    @staticmethod
    def _validate_install_receipt(
        exact_agent: str,
        principal_id: str,
        receipt: Mapping[str, Any],
    ) -> Tuple[Mapping[str, Any], str]:
        expected_receipt_keys = {
            "schema",
            "agent_id",
            "principal_id",
            "worker_credential_version",
            "token_fingerprint",
            "destination",
            "installed_at",
            "destination_verification",
        }
        if not isinstance(receipt, Mapping) or set(receipt) != expected_receipt_keys:
            raise WorkerCredentialError(
                "activation install receipt has unexpected or missing fields"
            )
        if receipt.get("schema") != INSTALL_RECEIPT_SCHEMA:
            raise WorkerCredentialError("activation requires a worker install receipt")
        if (
            receipt.get("agent_id") != exact_agent
            or receipt.get("principal_id") != principal_id
        ):
            raise WorkerCredentialError(
                "install receipt does not match the requested principal"
            )
        verification = receipt.get("destination_verification")
        expected_verification_keys = {
            "schema",
            "verified",
            "method",
            "destination",
            "agent_id",
            "principal_id",
            "worker_credential_version",
            "token_fingerprint",
            "verified_at",
        }
        if (
            not isinstance(verification, Mapping)
            or set(verification) != expected_verification_keys
            or verification.get("schema") != DESTINATION_VERIFICATION_SCHEMA
            or not verification.get("verified")
        ):
            raise WorkerCredentialError(
                "activation requires verified destination readback"
            )
        destination = str(receipt.get("destination") or "")
        if not destination or verification.get("destination") != destination:
            raise WorkerCredentialError(
                "destination verification does not match install receipt"
            )
        return verification, destination

    def validate_activation_in_transaction(
        self,
        conn: Any,
        agent_id: str,
        principal_id: str,
        *,
        receipt: Mapping[str, Any],
        expected_epoch_id: Optional[str] = None,
        require_pending: bool = False,
    ) -> Dict[str, Any]:
        """Validate an installed, authenticating principal without promoting it.

        The caller owns the surrounding transaction. This is the prepare seam
        for synchronized cutover: pending principals remain accepted by the API
        provider, while the old active principal remains authoritative until
        the cohort commit.
        """

        exact_agent = _validate_agent_id(agent_id)
        verification, destination = self._validate_install_receipt(
            exact_agent, principal_id, receipt
        )
        agent_lock = conn.execute(
            "UPDATE agents SET updated_at = updated_at "
            "WHERE id = ? AND deleted_at IS NULL",
            (exact_agent,),
        )
        if agent_lock.rowcount != 1:
            raise WorkerCredentialError("worker agent does not exist")
        self._assert_release_epoch_reservation(
            conn, exact_agent, expected_epoch_id=expected_epoch_id
        )
        credential_lock = conn.execute(
            "UPDATE worker_credentials SET updated_at = updated_at WHERE id = ?",
            (principal_id,),
        )
        if credential_lock.rowcount != 1:
            raise WorkerCredentialError("worker principal does not exist")
        row = conn.execute(
            "SELECT * FROM worker_credentials WHERE id = ?", (principal_id,)
        ).fetchone()
        record = _record_from_row(row, include_hash=True)
        if (
            record.get("agent_id") != exact_agent
            or record.get("principal_kind") != "worker"
        ):
            raise WorkerCredentialError(
                "principal is not bound to the requested agent"
            )
        allowed_states = {"pending_install"} if require_pending else {
            "pending_install",
            "active",
        }
        if record.get("state") not in allowed_states or not _not_expired(record):
            raise WorkerCredentialError("worker principal is revoked or expired")
        if receipt.get("token_fingerprint") != record.get("token_fingerprint"):
            raise WorkerCredentialError(
                "install receipt fingerprint does not match issuance"
            )
        expected_destination = bool(
            (
                record.get("environment") == "vm"
                and destination == "vm_env"
                and verification.get("method") == "vm_env_readback"
            )
            or (
                record.get("environment") == "k8s"
                and destination.startswith("k8s_secret:")
                and len(destination) > len("k8s_secret:")
                and verification.get("method") == "k8s_secret_readback"
            )
        )
        if not expected_destination:
            raise WorkerCredentialError(
                "install receipt destination does not match credential environment"
            )
        if receipt.get("worker_credential_version") != record.get(
            "credential_version"
        ):
            raise WorkerCredentialError(
                "install receipt credential version does not match issuance"
            )
        for key in (
            "principal_id",
            "agent_id",
            "worker_credential_version",
            "token_fingerprint",
        ):
            if verification.get(key) != receipt.get(key):
                raise WorkerCredentialError(
                    "destination verification does not match install receipt"
                )

        agent_row = conn.execute(
            "SELECT * FROM agents WHERE id = ?", (exact_agent,)
        ).fetchone()
        readiness = _credential_readiness(agent_row, record)
        if not readiness["credential_bound"]:
            raise WorkerCredentialError(
                "activation requires live authenticated heartbeat proof"
            )
        if record.get("package_capable") and not readiness["ready"]:
            raise WorkerCredentialError(
                "activation requires compatible source, runtime, and capability proof"
            )
        return {
            "record": record,
            "destination": destination,
            "readiness": readiness,
        }

    def validate_pending_readiness_in_transaction(
        self,
        conn: Any,
        agent_id: str,
        principal_id: str,
        *,
        expected_epoch_id: str,
    ) -> Dict[str, Any]:
        """Read-only preflight for the exact pending principal prove will use.

        This deliberately shares the activation readiness evaluator and error
        contract while omitting receipt validation and all state mutation.
        ``validate_activation_in_transaction`` remains the authoritative prove
        check and revalidates the same evidence inside the prove transaction.
        """

        exact_agent = _validate_agent_id(agent_id)
        self._assert_release_epoch_reservation(
            conn, exact_agent, expected_epoch_id=expected_epoch_id
        )
        agent_row = conn.execute(
            "SELECT * FROM agents WHERE id = ? AND deleted_at IS NULL",
            (exact_agent,),
        ).fetchone()
        if agent_row is None:
            raise WorkerCredentialError("worker agent does not exist")
        row = conn.execute(
            "SELECT * FROM worker_credentials WHERE id = ?", (principal_id,)
        ).fetchone()
        if row is None:
            raise WorkerCredentialError("worker principal does not exist")
        record = _record_from_row(row)
        if (
            record.get("agent_id") != exact_agent
            or record.get("principal_kind") != "worker"
        ):
            raise WorkerCredentialError(
                "principal is not bound to the requested agent"
            )
        if record.get("state") != "pending_install" or not _not_expired(record):
            raise WorkerCredentialError("worker principal is revoked or expired")

        readiness = _credential_readiness(agent_row, record)
        if not readiness["credential_bound"]:
            raise WorkerCredentialError(
                "activation requires live authenticated heartbeat proof"
            )
        if record.get("package_capable") and not readiness["ready"]:
            raise WorkerCredentialError(
                "activation requires compatible source, runtime, and capability proof"
            )
        return {
            "agent_id": exact_agent,
            "principal_id": record["id"],
            "credential_version": record["credential_version"],
            "readiness": readiness,
        }

    def stage_pending_in_transaction(
        self,
        conn: Any,
        agent_id: str,
        principal_id: str,
        *,
        expected_epoch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate an uninstalled pending principal without node-side proof."""

        exact_agent = _validate_agent_id(agent_id)
        locked = conn.execute(
            "UPDATE agents SET updated_at = updated_at "
            "WHERE id = ? AND deleted_at IS NULL",
            (exact_agent,),
        )
        if locked.rowcount != 1:
            raise WorkerCredentialError("worker agent does not exist")
        self._assert_release_epoch_reservation(
            conn, exact_agent, expected_epoch_id=expected_epoch_id
        )
        credential_lock = conn.execute(
            "UPDATE worker_credentials SET updated_at = updated_at WHERE id = ?",
            (principal_id,),
        )
        if credential_lock.rowcount != 1:
            raise WorkerCredentialError("worker principal does not exist")
        row = conn.execute(
            "SELECT * FROM worker_credentials WHERE id = ?", (principal_id,)
        ).fetchone()
        record = _record_from_row(row, include_hash=True)
        if record.get("agent_id") != exact_agent:
            raise WorkerCredentialError(
                "principal is not bound to the requested agent"
            )
        if record.get("state") != "pending_install" or not _not_expired(record):
            raise WorkerCredentialError(
                "fleet release requires an unexpired pending principal"
            )
        return record

    def discard_pending_in_transaction(
        self,
        conn: Any,
        agent_id: str,
        principal_id: str,
        *,
        expected_epoch_id: str,
        actor: str = "operator",
    ) -> Dict[str, Any]:
        """Revoke exactly the pending principal owned by an open epoch.

        The caller owns the surrounding transaction.  This is deliberately
        narrower than :meth:`revoke`: abort must make the staged successor
        stop authenticating without disturbing any identity that was live
        before the epoch opened.
        """

        exact_agent = _validate_agent_id(agent_id)
        exact_epoch = str(expected_epoch_id or "").strip()
        if not exact_epoch:
            raise WorkerCredentialError("fleet release epoch id is required")
        agent_lock = conn.execute(
            "UPDATE agents SET updated_at = updated_at "
            "WHERE id = ? AND deleted_at IS NULL",
            (exact_agent,),
        )
        if agent_lock.rowcount != 1:
            raise WorkerCredentialError("worker agent does not exist")
        self._assert_release_epoch_reservation(
            conn, exact_agent, expected_epoch_id=exact_epoch
        )
        participant = conn.execute(
            "SELECT principal_id FROM fleet_release_epoch_agents "
            "WHERE epoch_id = ? AND agent_id = ? AND open_state = 1",
            (exact_epoch, exact_agent),
        ).fetchone()
        if participant is None or str(participant["principal_id"]) != principal_id:
            raise WorkerCredentialError(
                "worker principal is not the pending identity owned by the epoch"
            )
        credential_lock = conn.execute(
            "UPDATE worker_credentials SET updated_at = updated_at WHERE id = ?",
            (principal_id,),
        )
        if credential_lock.rowcount != 1:
            raise WorkerCredentialError("worker principal does not exist")
        row = conn.execute(
            "SELECT * FROM worker_credentials WHERE id = ?", (principal_id,)
        ).fetchone()
        record = _record_from_row(row, include_hash=True)
        if (
            record.get("agent_id") != exact_agent
            or record.get("principal_kind") != "worker"
            or record.get("state") != "pending_install"
        ):
            raise WorkerCredentialError(
                "epoch-owned worker principal is no longer pending"
            )
        now = _timestamp()
        discarded = conn.execute(
            "UPDATE worker_credentials SET state = ?, revoked_at = ?, "
            "updated_at = ? WHERE id = ? AND agent_id = ? "
            "AND state = 'pending_install'",
            ("revoked", now, now, principal_id, exact_agent),
        )
        if discarded.rowcount != 1:
            raise WorkerCredentialError(
                "epoch-owned worker principal could not be discarded"
            )
        record["state"] = "revoked"
        record["revoked_at"] = now
        self._event(
            conn,
            record,
            "worker_credential.discarded",
            actor=actor,
            detail={"epoch_id": exact_epoch},
        )
        return _safe_record(record)

    def promote_in_transaction(
        self,
        conn: Any,
        agent_id: str,
        principal_id: str,
        *,
        receipt: Mapping[str, Any],
        actor: str = "operator",
        expected_epoch_id: Optional[str] = None,
        require_pending: bool = False,
    ) -> Dict[str, Any]:
        """Promote one validated principal in the caller's transaction."""

        validated = self.validate_activation_in_transaction(
            conn,
            agent_id,
            principal_id,
            receipt=receipt,
            expected_epoch_id=expected_epoch_id,
            require_pending=require_pending,
        )
        record = dict(validated["record"])
        destination = str(validated["destination"])
        now = _timestamp()
        conn.execute(
            "UPDATE worker_credentials SET state = ?, destination = ?, "
            "activated_at = COALESCE(activated_at, ?), revoked_at = NULL, "
            "superseded_by = NULL, updated_at = ? WHERE id = ?",
            ("active", destination, now, now, principal_id),
        )
        old_rows = conn.execute(
            "SELECT * FROM worker_credentials WHERE agent_id = ? AND id <> ? "
            "AND state IN ('pending_install', 'active')",
            (agent_id, principal_id),
        ).fetchall()
        conn.execute(
            "UPDATE worker_credentials SET state = ?, revoked_at = ?, "
            "superseded_by = ?, updated_at = ? WHERE agent_id = ? AND id <> ? "
            "AND state IN ('pending_install', 'active')",
            ("superseded", now, principal_id, now, agent_id, principal_id),
        )
        record["state"] = "active"
        record["destination"] = destination
        record["activated_at"] = record.get("activated_at") or now
        record["revoked_at"] = None
        record["superseded_by"] = None
        self._event(conn, record, "worker_credential.activated", actor=actor)
        for old_row in old_rows:
            old = _record_from_row(old_row)
            old["state"] = "superseded"
            self._event(
                conn,
                old,
                "worker_credential.superseded",
                actor=actor,
                detail={"superseded_by": principal_id},
            )
        return _safe_record(record)

    def issue(
        self,
        agent_id: str,
        *,
        fleet: str = "",
        environment: str,
        expected_source_commit: str = "",
        expected_runtime_digest: str = "",
        required_capabilities: Iterable[str] = (),
        package_capable: bool = False,
        expires_in: int = 30 * 24 * 60 * 60,
        actor: str = "operator",
    ) -> WorkerCredentialIssue:
        exact_agent = _validate_agent_id(agent_id)
        environment = str(environment or "").strip().lower()
        if environment not in {"vm", "k8s"}:
            raise WorkerCredentialError("worker environment must be vm or k8s")
        ttl_seconds = int(expires_in)
        if ttl_seconds < 60:
            raise WorkerCredentialError(
                "worker credential expires-in must be at least 60 seconds"
            )
        source = str(expected_source_commit or "").strip()
        runtime = str(expected_runtime_digest or "").strip()
        capabilities = sorted(
            {str(value).strip() for value in required_capabilities if str(value).strip()}
        )
        if package_capable:
            missing = []
            if not source:
                missing.append("expected_source_commit")
            if not runtime:
                missing.append("expected_runtime_digest")
            if PACKAGE_CAPABILITY not in capabilities:
                missing.append(PACKAGE_CAPABILITY + " capability")
            if missing:
                raise WorkerCredentialError(
                    "package-capable credential requires " + ", ".join(missing)
                )

        token = "mac_worker_" + secrets.token_urlsafe(32)
        now = _utcnow()
        issued_at = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=ttl_seconds))
        with self.store.transaction() as conn:
            agent_lock = conn.execute(
                "UPDATE agents SET updated_at = updated_at "
                "WHERE id = ? AND deleted_at IS NULL",
                (exact_agent,),
            )
            if agent_lock.rowcount != 1:
                raise WorkerCredentialError(
                    "worker credential requires a registered agent"
                )
            self._assert_release_epoch_reservation(conn, exact_agent)
            version_row = conn.execute(
                "SELECT COALESCE(MAX(credential_version), 0) AS version "
                "FROM worker_credentials WHERE agent_id = ?",
                (exact_agent,),
            ).fetchone()
            version = int(version_row["version"] or 0) + 1
            principal_id = _worker_principal_id(exact_agent, version)
            conn.execute(
                "INSERT INTO worker_credentials ("
                "id, agent_id, fleet, credential_version, token_hash, "
                "token_fingerprint, scopes, environment, expected_source_commit, "
                "expected_runtime_digest, required_capabilities, package_capable, "
                "state, destination, issued_at, expires_at, activated_at, revoked_at, "
                "superseded_by, created_by, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    principal_id,
                    exact_agent,
                    str(fleet or "").strip(),
                    version,
                    _token_hash(token),
                    _fingerprint(token),
                    json_dumps(list(WORKER_SCOPES)),
                    environment,
                    source,
                    runtime,
                    json_dumps(capabilities),
                    bool(package_capable),
                    "pending_install",
                    "",
                    issued_at,
                    expires_at,
                    str(actor or "operator"),
                    issued_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM worker_credentials WHERE id = ?", (principal_id,)
            ).fetchone()
            record = _record_from_row(row, include_hash=True)
            self._event(conn, record, "worker_credential.issued", actor=actor)
        return WorkerCredentialIssue(
            credential_record=record,
            raw_token=token,
            worker_version=version,
        )

    def activate(
        self,
        agent_id: str,
        principal_id: str,
        *,
        receipt: Mapping[str, Any],
        actor: str = "operator",
    ) -> Dict[str, Any]:
        """Activate an installed version, then revoke every superseded version.

        A deployment failure before this call leaves the old credential usable.
        The receipt is secret-free and must identify the exact installed
        principal/fingerprint/agent tuple.
        """

        with self.store.transaction() as conn:
            return self.promote_in_transaction(
                conn,
                agent_id,
                principal_id,
                receipt=receipt,
                actor=actor,
            )

    def revoke(self, agent_id: str, *, actor: str = "operator") -> List[Dict[str, Any]]:
        exact_agent = _validate_agent_id(agent_id)
        revoked: List[Dict[str, Any]] = []
        with self.store.transaction() as conn:
            locked = conn.execute(
                "UPDATE agents SET updated_at = updated_at "
                "WHERE id = ? AND deleted_at IS NULL",
                (exact_agent,),
            )
            if locked.rowcount != 1:
                raise WorkerCredentialError("worker agent does not exist")
            self._assert_release_epoch_reservation(conn, exact_agent)
            rows = conn.execute(
                "SELECT * FROM worker_credentials WHERE agent_id = ? "
                "AND state IN ('pending_install', 'active')",
                (exact_agent,),
            ).fetchall()
            now = _timestamp()
            conn.execute(
                "UPDATE worker_credentials SET state = ?, revoked_at = ?, updated_at = ? "
                "WHERE agent_id = ? AND state IN ('pending_install', 'active')",
                ("revoked", now, now, exact_agent),
            )
            for row in rows:
                record = _record_from_row(row)
                record["state"] = "revoked"
                record["revoked_at"] = now
                revoked.append(record)
                self._event(conn, record, "worker_credential.revoked", actor=actor)
        return [_safe_record(record) for record in revoked]

    def discard_unreserved_pending(
        self,
        agent_id: str,
        *,
        created_by: str,
        actor: str = "fleet-recovery",
    ) -> List[Dict[str, Any]]:
        """Revoke only orphaned pending credentials from one exact issuance owner.

        This closes the crash window between issuing successor credentials and
        opening their fleet-release epoch.  It refuses to operate while any
        epoch owns the agent and never touches an active credential.
        """

        exact_agent = _validate_agent_id(agent_id)
        exact_creator = str(created_by or "").strip()
        if not exact_creator or len(exact_creator.encode("utf-8")) > 512:
            raise WorkerCredentialError("pending credential creator is invalid")
        discarded: List[Dict[str, Any]] = []
        with self.store.transaction() as conn:
            locked = conn.execute(
                "UPDATE agents SET updated_at = updated_at "
                "WHERE id = ? AND deleted_at IS NULL",
                (exact_agent,),
            )
            if locked.rowcount != 1:
                # A typed fleet cohort may include a brand-new worker. If the
                # epoch aborts before registration or credential issuance,
                # there is nothing to discard. Treat that recovery operation
                # as the same idempotent no-op as a repeated successful discard.
                return []
            self._assert_release_epoch_reservation(conn, exact_agent)
            rows = conn.execute(
                "SELECT * FROM worker_credentials WHERE agent_id = ? "
                "AND created_by = ? AND state = 'pending_install'",
                (exact_agent, exact_creator),
            ).fetchall()
            if len(rows) > 1:
                raise WorkerCredentialError(
                    "issuance owner has multiple pending credentials for one agent"
                )
            now = _timestamp()
            for row in rows:
                principal_id = str(row["id"])
                updated = conn.execute(
                    "UPDATE worker_credentials SET state = ?, revoked_at = ?, "
                    "updated_at = ? WHERE id = ? AND agent_id = ? "
                    "AND created_by = ? AND state = 'pending_install'",
                    (
                        "revoked",
                        now,
                        now,
                        principal_id,
                        exact_agent,
                        exact_creator,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkerCredentialError(
                        "orphaned pending credential changed during discard"
                    )
                record = _record_from_row(row)
                record["state"] = "revoked"
                record["revoked_at"] = now
                discarded.append(record)
                self._event(
                    conn,
                    record,
                    "worker_credential.discarded",
                    actor=actor,
                    detail={"created_by": exact_creator, "reason": "epoch_not_opened"},
                )
        return [_safe_record(record) for record in discarded]

    def list(self, *, agent_id: str = "") -> List[Dict[str, Any]]:
        params: Tuple[Any, ...] = ()
        sql = "SELECT * FROM worker_credentials"
        if agent_id:
            sql += " WHERE agent_id = ?"
            params = (_validate_agent_id(agent_id),)
        values = [_safe_record(_record_from_row(row)) for row in self.store.query_all(sql, params)]
        return sorted(values, key=lambda item: (str(item.get("agent_id")), str(item.get("id"))))

    def records(self, *, agent_id: str = "", include_hash: bool = False) -> List[Dict[str, Any]]:
        params: Tuple[Any, ...] = ()
        sql = "SELECT * FROM worker_credentials"
        if agent_id:
            sql += " WHERE agent_id = ?"
            params = (_validate_agent_id(agent_id),)
        return [
            _record_from_row(row, include_hash=include_hash)
            for row in self.store.query_all(sql, params)
        ]


class WorkerCredentialPrincipalProvider:
    """Resolve valid DB-held worker hashes for every API replica."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def tokens(self, *, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
        instant = (now or _utcnow()).astimezone(timezone.utc)
        result: Dict[str, Dict[str, Any]] = {}
        rows = self.store.query_all(
            "SELECT wc.* FROM worker_credentials wc "
            "JOIN agents a ON a.id = wc.agent_id "
            "WHERE wc.state IN ('pending_install', 'active') "
            "AND wc.revoked_at IS NULL AND a.deleted_at IS NULL"
        )
        for row in rows:
            record = _record_from_row(row, include_hash=True)
            try:
                expires_at = _parse_timestamp(record.get("expires_at"))
            except ClientPrincipalError:
                continue
            token_hash = str(record.get("token_hash") or "")
            if (
                expires_at is None
                or expires_at <= instant
                or not token_hash.startswith("sha256:")
            ):
                continue
            result[token_hash] = {
                "scopes": list(record.get("scopes") or []),
                "client_id": record["id"],
                "agent_id": record["agent_id"],
                "principal_kind": "worker",
                "credential_fingerprint": record["token_fingerprint"],
                "worker_credential_version": record["credential_version"],
                "worker_credential_state": record["state"],
            }
        return result


def _safe_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"token_hash"} and value not in (None, "")
    }


def installation_manifest(issue: WorkerCredentialIssue) -> Dict[str, Any]:
    record = issue.record
    metadata = _metadata(record)
    return {
        "schema": INSTALL_MANIFEST_SCHEMA,
        "agent_id": record["agent_id"],
        "principal_id": record["id"],
        "worker_credential_version": issue.worker_version,
        "token_fingerprint": record["token_fingerprint"],
        "environment": metadata.get("environment") or "",
        "credential": {
            "token": issue.token,
            "issued_at": record["issued_at"],
            "expires_at": record["expires_at"],
        },
        "expectations": {
            "source_commit": metadata.get("expected_source_commit") or "",
            "runtime_digest": metadata.get("expected_runtime_digest") or "",
            "capabilities": list(metadata.get("required_capabilities") or []),
            "package_capable": bool(metadata.get("package_capable")),
        },
    }


def _validated_manifest(
    manifest: Mapping[str, Any],
    expected_agent_id: str = "",
    expected_environment: str = "",
) -> Tuple[str, str]:
    if manifest.get("schema") != INSTALL_MANIFEST_SCHEMA:
        raise WorkerCredentialError("not a worker credential install manifest")
    agent_id = _validate_agent_id(str(manifest.get("agent_id") or ""))
    if expected_agent_id and agent_id != expected_agent_id:
        raise WorkerCredentialError("credential manifest is bound to a different agent")
    environment = str(manifest.get("environment") or "")
    if environment not in {"vm", "k8s"}:
        raise WorkerCredentialError("credential manifest has an invalid environment")
    if expected_environment and environment != expected_environment:
        raise WorkerCredentialError(
            "credential manifest is bound to a different environment"
        )
    credential = manifest.get("credential")
    token = str(credential.get("token") or "") if isinstance(credential, Mapping) else ""
    if not token.startswith("mac_worker_") or _fingerprint(token) != manifest.get("token_fingerprint"):
        raise WorkerCredentialError("credential manifest token integrity check failed")
    principal_id = str(manifest.get("principal_id") or "")
    if not principal_id:
        raise WorkerCredentialError("credential manifest has no principal id")
    return agent_id, token


def _install_env_values(manifest: Mapping[str, Any], token: str) -> Dict[str, str]:
    expectations = manifest.get("expectations")
    expected = expectations if isinstance(expectations, Mapping) else {}
    runtime_digest = str(expected.get("runtime_digest") or "")
    return {
        "MAC_WORKER_TOKEN": token,
        "MAC_WORKER_CREDENTIAL_ID": str(manifest["principal_id"]),
        "MAC_WORKER_CREDENTIAL_VERSION": str(manifest["worker_credential_version"]),
        "MAC_WORKER_CREDENTIAL_AGENT_ID": str(manifest["agent_id"]),
        "MAC_WORKER_CREDENTIAL_FINGERPRINT": str(manifest["token_fingerprint"]),
        "MAC_WORKER_CREDENTIAL_SOURCE_COMMIT": str(expected.get("source_commit") or ""),
        "MAC_WORKER_CREDENTIAL_RUNTIME_DIGEST": runtime_digest,
        # The credential expectation and the runtime's advertised digest must
        # move atomically. Otherwise a freshly installed credential can never
        # satisfy activation's live-heartbeat check.
        "MAC_WORKER_RUNNING_DIGEST": runtime_digest,
        "MAC_WORKER_IDENTITY_MODE": "bound",
    }


def install_vm_manifest(
    manifest: Mapping[str, Any],
    env_path: Path,
    *,
    expected_agent_id: str,
) -> Dict[str, Any]:
    """Atomically install one worker token in a mode-0600 VM env file."""

    agent_id, token = _validated_manifest(
        manifest, expected_agent_id, expected_environment="vm"
    )
    from mac.deploy_env import read_env_file, render_env

    path = Path(env_path).expanduser()
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never chmod an arbitrary pre-existing parent supplied by the caller
    # (for example /etc). Tighten only the dedicated leaf directory this
    # install created itself.
    if not parent_existed:
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    expected_values = _install_env_values(manifest, token)
    values = read_env_file(path)
    previous_worker_token = str(values.get("MAC_WORKER_TOKEN") or "")
    values.update(expected_values)
    if previous_worker_token and previous_worker_token != token:
        for key in _HUB_FACING_WORKER_TOKEN_ALIASES:
            if values.get(key) == previous_worker_token:
                values[key] = token
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_env(values))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    # A successful rename is not enough: read the destination back and prove
    # every installed value, including the bearer fingerprint, before issuing
    # a receipt that activation can accept.
    installed = read_env_file(path)
    if any(installed.get(key) != value for key, value in expected_values.items()):
        raise WorkerCredentialError("worker credential VM destination readback failed")
    verification = _destination_verification(
        manifest,
        destination="vm_env",
        method="vm_env_readback",
    )
    return {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "agent_id": agent_id,
        "principal_id": manifest["principal_id"],
        "worker_credential_version": manifest["worker_credential_version"],
        "token_fingerprint": manifest["token_fingerprint"],
        "destination": "vm_env",
        "installed_at": _timestamp(),
        "destination_verification": verification,
    }


def kubernetes_secret_name(agent_id: str) -> str:
    value = _K8S_NAME.sub("-", agent_id.lower()).strip("-")
    digest = _worker_key(agent_id)[:8]
    prefix = (value or "worker")[:42].rstrip("-")
    return "mac-worker-%s-%s" % (prefix, digest)


def build_kubernetes_secret(
    manifest: Mapping[str, Any],
    *,
    namespace: str = "mac",
    name: str = "",
    expected_agent_id: str = "",
) -> Dict[str, Any]:
    agent_id, token = _validated_manifest(
        manifest, expected_agent_id, expected_environment="k8s"
    )
    secret_name = name or kubernetes_secret_name(agent_id)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "mac",
                "mac.agent/id-hash": _worker_key(agent_id),
            },
        },
        "type": "Opaque",
        "stringData": _install_env_values(manifest, token),
    }


def apply_kubernetes_secret(
    manifest: Mapping[str, Any],
    *,
    namespace: str = "mac",
    name: str = "",
    expected_agent_id: str = "",
    runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Apply a per-agent Secret without putting its token in argv or output."""

    secret = build_kubernetes_secret(
        manifest,
        namespace=namespace,
        name=name,
        expected_agent_id=expected_agent_id,
    )
    run = runner or subprocess.run
    proc = run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(secret, separators=(",", ":")),
        capture_output=True,
        text=True,
    )
    if getattr(proc, "returncode", 1) != 0:
        # kubectl diagnostics are intentionally not reflected: admission
        # webhooks can echo object content.  The token was supplied only on
        # stdin, never argv, but error handling remains secret-blind.
        raise WorkerCredentialError("kubectl failed to apply the worker credential Secret")
    secret_name = str(secret["metadata"]["name"])
    readback = run(
        [
            "kubectl",
            "get",
            "secret",
            secret_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if getattr(readback, "returncode", 1) != 0:
        raise WorkerCredentialError(
            "kubectl failed to verify the worker credential Secret"
        )
    try:
        observed = json.loads(str(getattr(readback, "stdout", "") or ""))
        encoded = observed.get("data") if isinstance(observed, Mapping) else None
        if not isinstance(encoded, Mapping):
            raise ValueError("missing data")
        expected_values = secret["stringData"]
        decoded = {
            str(key): base64.b64decode(str(value), validate=True).decode("utf-8")
            for key, value in encoded.items()
        }
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerCredentialError(
            "worker credential Secret readback was invalid"
        ) from exc
    if any(decoded.get(key) != value for key, value in expected_values.items()):
        raise WorkerCredentialError("worker credential Secret readback did not match")
    destination = "k8s_secret:%s/%s" % (namespace, secret_name)
    return {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "agent_id": manifest["agent_id"],
        "principal_id": manifest["principal_id"],
        "worker_credential_version": manifest["worker_credential_version"],
        "token_fingerprint": manifest["token_fingerprint"],
        "destination": destination,
        "installed_at": _timestamp(),
        "destination_verification": _destination_verification(
            manifest,
            destination=destination,
            method="k8s_secret_readback",
        ),
    }


def _destination_verification(
    manifest: Mapping[str, Any], *, destination: str, method: str
) -> Dict[str, Any]:
    return {
        "schema": DESTINATION_VERIFICATION_SCHEMA,
        "verified": True,
        "method": method,
        "destination": destination,
        "agent_id": manifest["agent_id"],
        "principal_id": manifest["principal_id"],
        "worker_credential_version": manifest["worker_credential_version"],
        "token_fingerprint": manifest["token_fingerprint"],
        "verified_at": _timestamp(),
    }


def credential_resource_from_env(
    agent_id: str, env: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """Return the secret-free heartbeat proof for the locally installed token."""

    values = os.environ if env is None else env
    configured_agent = str(values.get("MAC_WORKER_CREDENTIAL_AGENT_ID") or "")
    if not configured_agent:
        return {}
    if configured_agent != agent_id:
        return {
            "schema": RESOURCE_PROOF_SCHEMA,
            "mode": "invalid_binding",
            "agent_id": configured_agent,
        }
    principal_id = str(values.get("MAC_WORKER_CREDENTIAL_ID") or "")
    version = str(values.get("MAC_WORKER_CREDENTIAL_VERSION") or "")
    fingerprint = str(values.get("MAC_WORKER_CREDENTIAL_FINGERPRINT") or "")
    if not principal_id or not version or not fingerprint:
        return {}
    try:
        parsed_version = int(version)
        if parsed_version < 1:
            raise ValueError("version must be positive")
    except (TypeError, ValueError):
        return {
            "schema": RESOURCE_PROOF_SCHEMA,
            "mode": "invalid_credential_version",
            "agent_id": agent_id,
            "principal_id": principal_id,
        }
    return {
        "schema": RESOURCE_PROOF_SCHEMA,
        "mode": "bound",
        "agent_id": agent_id,
        "principal_id": principal_id,
        "worker_credential_version": parsed_version,
        "token_fingerprint": fingerprint,
        "observed_at": _timestamp(),
    }


def _active_worker_record(
    records: Sequence[Mapping[str, Any]], now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    candidates = [
        dict(record)
        for record in records
        if not record.get("revoked_at")
        and _not_expired(record, now)
        and _metadata(record).get("state") == "active"
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: int(item.get("credential_version") or 0),
    )


def authenticated_credential_resource(
    *,
    agent_id: str,
    principal_id: Optional[str],
    token_fingerprint: Optional[str],
    credential_version: Optional[int],
) -> Dict[str, Any]:
    """Build the hub-authenticated half of a live credential proof.

    Clients cannot manufacture this record: the heartbeat endpoint overwrites
    it from the bearer principal resolved by the shared DB credential provider.
    """

    if not principal_id or not token_fingerprint or not credential_version:
        return {}
    return {
        "schema": AUTHENTICATED_PROOF_SCHEMA,
        "agent_id": agent_id,
        "principal_id": principal_id,
        "worker_credential_version": int(credential_version),
        "token_fingerprint": token_fingerprint,
        "authenticated_at": _timestamp(),
    }


def _agent_value(agent: Any, key: str, default: Any = None) -> Any:
    if isinstance(agent, Mapping):
        return agent.get(key, default)
    return _row_value(agent, key, default)


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json_loads(value, {})
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        parsed = json_loads(value, [])
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _credential_readiness(agent: Any, credential: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate one current agent row against one exact credential record."""

    agent_id = str(_agent_value(agent, "id", ""))
    resources = _json_object(_agent_value(agent, "resources", {}))
    proof = _json_object(resources.get("worker_credential"))
    authenticated = _json_object(resources.get("worker_credential_authenticated"))
    version = int(credential.get("credential_version") or 0)
    identity_fields = {
        "agent_id": agent_id,
        "principal_id": credential.get("id"),
        "worker_credential_version": version,
        "token_fingerprint": credential.get("token_fingerprint"),
    }
    proof_matches = bool(
        proof.get("schema") == RESOURCE_PROOF_SCHEMA
        and proof.get("mode") == "bound"
        and all(proof.get(key) == value for key, value in identity_fields.items())
    )
    authentication_matches = bool(
        authenticated.get("schema") == AUTHENTICATED_PROOF_SCHEMA
        and all(
            authenticated.get(key) == value
            for key, value in identity_fields.items()
        )
    )
    credential_bound = proof_matches and authentication_matches

    source_state = _json_object(resources.get("source_state"))
    expected_source = str(credential.get("expected_source_commit") or "")
    source_ready = bool(
        expected_source
        and str(source_state.get("commit_sha") or "") == expected_source
        and not bool(source_state.get("dirty"))
    )
    expected_runtime = str(credential.get("expected_runtime_digest") or "")
    runtime_ready = bool(
        expected_runtime
        and str(_agent_value(agent, "running_digest", "") or "") == expected_runtime
    )
    required_caps = {
        str(value) for value in credential.get("required_capabilities") or []
    }
    actual_caps = {
        str(value) for value in _json_list(_agent_value(agent, "capabilities", []))
    }
    capability_ready = bool(
        credential.get("package_capable")
        and PACKAGE_CAPABILITY in required_caps
        and required_caps.issubset(actual_caps)
    )
    ready = bool(
        credential_bound and source_ready and runtime_ready and capability_ready
    )
    blockers = []
    if not credential_bound:
        blockers.append("authenticated_credential_not_observed")
    if not source_ready:
        blockers.append("source_commit_incompatible")
    if not runtime_ready:
        blockers.append("runtime_digest_incompatible")
    if not capability_ready:
        blockers.append("package_runtime_capability_missing")
    return {
        "credential_bound": credential_bound,
        "source_ready": source_ready,
        "runtime_ready": runtime_ready,
        "capability_ready": capability_ready,
        "ready": ready,
        "blockers": blockers,
    }


def build_readiness_inventory(
    agents: Iterable[Mapping[str, Any]],
    credential_records: Iterable[Mapping[str, Any]],
    *,
    mode: str = MODE_COMPATIBILITY,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the fail-closed evidence inventory used by the enforcement flip."""

    if mode not in POLICY_MODES:
        raise WorkerCredentialError("unknown worker credential policy mode")
    records = [dict(record) for record in credential_records]
    entries: List[Dict[str, Any]] = []
    active = []
    for agent in agents:
        if not isinstance(agent, Mapping) or agent.get("deleted_at"):
            continue
        status = str(agent.get("status") or "").lower()
        if status not in ACTIVE_AGENT_STATUSES:
            continue
        active.append(agent)

    for agent in sorted(active, key=lambda value: str(value.get("id") or "")):
        agent_id = str(agent.get("id") or "")
        agent_records = _worker_records(records, agent_id)
        credential = _active_worker_record(agent_records, now)
        reasons: List[str] = []
        if credential is None:
            reasons.append("agent_bound_credential_missing")
            readiness = {
                "credential_bound": False,
                "source_ready": False,
                "runtime_ready": False,
                "capability_ready": False,
                "ready": False,
                "blockers": [],
            }
        else:
            readiness = _credential_readiness(agent, credential)
            reasons.extend(readiness["blockers"])
        ready = bool(credential and readiness["ready"])
        entries.append(
            {
                "agent_id": agent_id,
                "status": str(agent.get("status") or ""),
                "principal_id": credential.get("id") if credential else None,
                "credential_bound": bool(credential and readiness["credential_bound"]),
                "source_ready": readiness["source_ready"],
                "runtime_ready": readiness["runtime_ready"],
                "capability_ready": readiness["capability_ready"],
                "package_linked_allowed": ready,
                "legacy_fast_lane_allowed": bool(not ready and mode == MODE_COMPATIBILITY),
                # Lane is a task-route property.  Readiness only says whether
                # this worker may execute managed work; a ready worker can
                # still execute an unlinked legacy task.
                "managed_lane_eligible": ready,
                "external_certifier_capable": ready,
                "ready": ready,
                "blockers": reasons,
            }
        )

    ready_count = sum(1 for item in entries if item["ready"])
    active_count = len(entries)
    all_ready = bool(active_count > 0 and ready_count == active_count)
    return {
        "schema": INVENTORY_SCHEMA,
        "generated_at": _timestamp(now),
        "mode": mode,
        "active_worker_count": active_count,
        "ready_worker_count": ready_count,
        "readiness_percent": (100.0 * ready_count / active_count) if active_count else 0.0,
        "all_ready": all_ready,
        "workers": entries,
    }


def inventory_digest(inventory: Mapping[str, Any]) -> str:
    canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _inventory_facts(inventory: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "active_worker_count": int(inventory.get("active_worker_count") or 0),
        "ready_worker_count": int(inventory.get("ready_worker_count") or 0),
        "all_ready": bool(inventory.get("all_ready")),
        "workers": list(inventory.get("workers") or []),
    }


def _live_inventory(store: Store, mode: str) -> Dict[str, Any]:
    rows = store.query_all("SELECT * FROM agents WHERE deleted_at IS NULL")
    agents = [
        {key: row[key] for key in row.keys()}
        if hasattr(row, "keys")
        else dict(row)
        for row in rows
    ]
    records = WorkerCredentialLifecycle(store).records()
    return build_readiness_inventory(agents, records, mode=mode)


def live_inventory_in_transaction(conn: Any, mode: str) -> Dict[str, Any]:
    """Build the rollout inventory from one caller-owned DB snapshot."""

    rows = conn.execute(
        "SELECT * FROM agents WHERE deleted_at IS NULL"
    ).fetchall()
    agents = [
        {key: row[key] for key in row.keys()}
        if hasattr(row, "keys")
        else dict(row)
        for row in rows
    ]
    credential_rows = conn.execute(
        "SELECT * FROM worker_credentials ORDER BY agent_id, credential_version"
    ).fetchall()
    records = [_record_from_row(row) for row in credential_rows]
    return build_readiness_inventory(agents, records, mode=mode)


def write_policy_state_in_transaction(
    conn: Any,
    mode: str,
    *,
    reviewed_inventory: Optional[Mapping[str, Any]] = None,
    actor: str = "operator",
) -> Dict[str, Any]:
    """Validate and write fleet identity policy in an existing transaction."""

    if mode not in POLICY_MODES:
        raise WorkerCredentialError(
            "worker credential mode must be compatibility or enforced"
        )
    inventory = live_inventory_in_transaction(conn, mode)
    if reviewed_inventory is not None and _inventory_facts(
        reviewed_inventory
    ) != _inventory_facts(inventory):
        raise WorkerCredentialError(
            "worker credential readiness inventory is stale or does not match hub authority"
        )
    if mode == MODE_ENFORCED:
        if (
            reviewed_inventory is None
            or reviewed_inventory.get("schema") != INVENTORY_SCHEMA
        ):
            raise WorkerCredentialError("enforcement requires a readiness inventory")
        if (
            not inventory.get("all_ready")
            or float(inventory.get("readiness_percent") or 0) != 100.0
        ):
            raise WorkerCredentialError(
                "refusing worker identity enforcement: active fleet is not 100% ready"
            )
    payload = {
        "schema": POLICY_SCHEMA,
        "mode": mode,
        "updated_at": _timestamp(),
        "updated_by": str(actor or "operator"),
        "inventory_digest": inventory_digest(inventory),
        "ready_agent_ids": [
            str(item["agent_id"])
            for item in inventory.get("workers") or []
            if item.get("ready")
        ],
    }
    row = conn.execute(
        "SELECT revision FROM worker_credential_policy_state "
        "WHERE singleton_key = ?",
        ("fleet",),
    ).fetchone()
    revision = int(row["revision"] or 0) + 1 if row is not None else 1
    payload["revision"] = revision
    conn.execute(
        "INSERT INTO worker_credential_policy_state ("
        "singleton_key, mode, inventory_digest, ready_agent_ids, revision, "
        "updated_by, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton_key) DO UPDATE SET "
        "mode = excluded.mode, inventory_digest = excluded.inventory_digest, "
        "ready_agent_ids = excluded.ready_agent_ids, revision = excluded.revision, "
        "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (
            "fleet",
            payload["mode"],
            payload["inventory_digest"],
            json_dumps(payload["ready_agent_ids"]),
            revision,
            payload["updated_by"],
            payload["updated_at"],
        ),
    )
    return payload


def write_policy_state(
    mode: str,
    *,
    inventory: Optional[Mapping[str, Any]] = None,
    store: Optional[Store] = None,
    path: Optional[Path] = None,
    actor: str = "operator",
) -> Dict[str, Any]:
    if store is not None:
        with store.transaction() as conn:
            return write_policy_state_in_transaction(
                conn,
                mode,
                reviewed_inventory=inventory,
                actor=actor,
            )
    if mode not in POLICY_MODES:
        raise WorkerCredentialError(
            "worker credential mode must be compatibility or enforced"
        )
    reviewed_inventory = inventory
    if mode == MODE_ENFORCED:
        if (
            reviewed_inventory is None
            or reviewed_inventory.get("schema") != INVENTORY_SCHEMA
        ):
            raise WorkerCredentialError("enforcement requires a readiness inventory")
        if (
            not reviewed_inventory.get("all_ready")
            or float(reviewed_inventory.get("readiness_percent") or 0) != 100.0
        ):
            raise WorkerCredentialError(
                "refusing worker identity enforcement: active fleet is not 100% ready"
            )
    payload = {
        "schema": POLICY_SCHEMA,
        "mode": mode,
        "updated_at": _timestamp(),
        "updated_by": str(actor or "operator"),
        "inventory_digest": (
            inventory_digest(reviewed_inventory) if reviewed_inventory else None
        ),
        "ready_agent_ids": (
            [
                str(item["agent_id"])
                for item in reviewed_inventory.get("workers") or []
                if item.get("ready")
            ]
            if reviewed_inventory
            else []
        ),
    }
    if path is not None:
        # Explicit standalone/testing mode only. Production API replicas and
        # the rollout CLI use the shared control-plane store.
        _atomic_json(Path(path).expanduser(), payload)
    else:
        raise WorkerCredentialError(
            "worker credential policy requires shared store authority"
        )
    return payload


def read_policy_state(
    path: Optional[Path] = None, *, store: Optional[Store] = None
) -> Dict[str, Any]:
    if store is not None:
        row = store.query_one(
            "SELECT * FROM worker_credential_policy_state WHERE singleton_key = ?",
            ("fleet",),
        )
        if row is None:
            return {"schema": POLICY_SCHEMA, "mode": MODE_COMPATIBILITY, "revision": 0}
        payload = {
            "schema": POLICY_SCHEMA,
            "mode": str(row["mode"]),
            "inventory_digest": row["inventory_digest"],
            "ready_agent_ids": list(json_loads(row["ready_agent_ids"], [])),
            "revision": int(row["revision"]),
            "updated_by": str(row["updated_by"]),
            "updated_at": str(row["updated_at"]),
        }
        if payload["mode"] not in POLICY_MODES:
            raise WorkerCredentialError("worker credential policy has an invalid mode")
        return payload
    if path is None:
        raise WorkerCredentialError(
            "worker credential policy requires shared store authority"
        )
    policy_path = Path(path).expanduser()
    if not policy_path.exists():
        return {"schema": POLICY_SCHEMA, "mode": MODE_COMPATIBILITY}
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerCredentialError("worker credential policy is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != POLICY_SCHEMA:
        raise WorkerCredentialError("worker credential policy has an invalid schema")
    if payload.get("mode") not in POLICY_MODES:
        raise WorkerCredentialError("worker credential policy has an invalid mode")
    return payload


class WorkerCredentialPolicyProvider:
    """Replica-consistent policy reader backed by the shared hub database."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def state(self) -> Dict[str, Any]:
        # One indexed singleton read per authenticated request is cheap and,
        # unlike per-process mtime caches, observes an enforcement flip on all
        # replicas immediately.
        return read_policy_state(store=self.store)

    @property
    def mode(self) -> str:
        return str(self.state()["mode"])


def _package_worker_readiness_from_rows(
    agent_row: Any,
    credential_rows: Iterable[Any],
    policy_row: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if agent_row is None:
        return {"ready": False, "reason": "package_worker_missing"}
    agent_id = str(_row_value(agent_row, "id", ""))
    if str(_row_value(agent_row, "status", "")).lower() not in ACTIVE_AGENT_STATUSES:
        return {"ready": False, "reason": "package_worker_not_active"}
    if policy_row is None:
        return {"ready": False, "reason": "package_readiness_policy_missing"}
    ready_agent_ids = set(json_loads(_row_value(policy_row, "ready_agent_ids", "[]"), []))
    if agent_id not in ready_agent_ids:
        return {"ready": False, "reason": "package_readiness_membership_missing"}
    credentials = [
        _record_from_row(row)
        for row in credential_rows
        if str(_row_value(row, "state", "")) == "active"
    ]
    credential = _active_worker_record(credentials, now)
    if credential is None:
        return {"ready": False, "reason": "active_worker_credential_missing"}
    readiness = _credential_readiness(agent_row, credential)
    if not readiness["ready"]:
        return {
            "ready": False,
            "reason": str(readiness["blockers"][0]),
            "principal_id": credential["id"],
        }
    return {
        "ready": True,
        "reason": "package_worker_ready",
        "principal_id": credential["id"],
        "credential_version": credential["credential_version"],
        "token_fingerprint": credential["token_fingerprint"],
    }


def package_worker_readiness(
    store: Store, agent_id: str, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    exact_agent = _validate_agent_id(agent_id)
    return _package_worker_readiness_from_rows(
        store.query_one("SELECT * FROM agents WHERE id = ?", (exact_agent,)),
        store.query_all(
            "SELECT * FROM worker_credentials WHERE agent_id = ? "
            "ORDER BY credential_version DESC",
            (exact_agent,),
        ),
        store.query_one(
            "SELECT * FROM worker_credential_policy_state WHERE singleton_key = ?",
            ("fleet",),
        ),
        now=now,
    )


def assert_package_worker_ready(
    conn: Any,
    agent_id: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Authoritative package admission check inside the claim transaction."""

    exact_agent = _validate_agent_id(agent_id)
    agent_lock = conn.execute(
        "UPDATE agents SET updated_at = updated_at WHERE id = ?", (exact_agent,)
    )
    if agent_lock.rowcount != 1:
        raise TransitionError("package worker is missing")
    # Serialize activation/revocation against claim authorization before
    # inspecting the exact active principal and live heartbeat proof.
    conn.execute(
        "UPDATE worker_credentials SET updated_at = updated_at WHERE agent_id = ? "
        "AND state IN ('pending_install', 'active')",
        (exact_agent,),
    )
    policy_row = conn.execute(
        "SELECT * FROM worker_credential_policy_state WHERE singleton_key = ?",
        ("fleet",),
    ).fetchone()
    result = _package_worker_readiness_from_rows(
        conn.execute("SELECT * FROM agents WHERE id = ?", (exact_agent,)).fetchone(),
        conn.execute(
            "SELECT * FROM worker_credentials WHERE agent_id = ? "
            "ORDER BY credential_version DESC",
            (exact_agent,),
        ).fetchall(),
        policy_row,
        now=now,
    )
    if not result["ready"]:
        raise TransitionError(
            "package worker credential readiness failed: %s" % result["reason"]
        )
    return result


@dataclass(frozen=True)
class WorkerActorDecision:
    allowed: bool
    reason: str
    legacy: bool = False


def evaluate_worker_actor(
    *,
    mode: str,
    principal_agent_id: Optional[str],
    claimed_agent_id: str,
    package_linked: bool,
    package_ready: bool = False,
) -> WorkerActorDecision:
    """Pure policy seam used by API actor checks and package admission."""

    if mode not in POLICY_MODES:
        return WorkerActorDecision(False, "worker_identity_policy_invalid")
    if principal_agent_id:
        if principal_agent_id != claimed_agent_id:
            return WorkerActorDecision(False, "agent_principal_mismatch")
        if package_linked and not package_ready:
            return WorkerActorDecision(False, "package_worker_readiness_required")
        return WorkerActorDecision(True, "agent_principal_match")
    if package_linked:
        return WorkerActorDecision(False, "legacy_worker_package_link_forbidden", legacy=True)
    if mode == MODE_ENFORCED:
        return WorkerActorDecision(False, "agent_bound_credential_required")
    return WorkerActorDecision(True, "legacy_worker_compatibility", legacy=True)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_json(Path(path).expanduser(), payload)


def _read_json(path: str) -> Dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkerCredentialError("JSON input must be an object")
    return value


def _safe_print(payload: Mapping[str, Any]) -> None:
    # Only callers that construct explicitly secret-free payloads use this.
    print(json.dumps(payload, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mac.worker_credentials")
    parser.add_argument(
        "--db",
        help="explicit SQLite hub database; otherwise use MAC_DATABASE_URL/MAC_DB",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ensure_runtime = sub.add_parser("ensure-runtime")
    ensure_runtime.add_argument("--source-commit", required=True)
    ensure_runtime.add_argument("--created-by", default="fleet-deploy")

    issue = sub.add_parser("issue")
    issue.add_argument("--agent-id", required=True)
    issue.add_argument("--fleet", default="")
    issue.add_argument("--environment", choices=("vm", "k8s"), required=True)
    issue.add_argument("--expected-source-commit", default="")
    issue.add_argument("--expected-runtime-digest", default="")
    issue.add_argument("--capability", action="append", default=[])
    issue.add_argument("--package-capable", action="store_true")
    issue.add_argument("--expires-in", type=int, default=30 * 24 * 60 * 60)
    issue.add_argument("--created-by", default="operator")
    issue.add_argument("--manifest-out", required=True)

    install = sub.add_parser("install-vm")
    install.add_argument("--manifest", required=True)
    install.add_argument("--agent-id", required=True)
    install.add_argument("--env-file", required=True)
    install.add_argument("--receipt-out", required=True)

    install_k8s = sub.add_parser("install-k8s")
    install_k8s.add_argument("--manifest", required=True)
    install_k8s.add_argument("--agent-id", required=True)
    install_k8s.add_argument("--namespace", default="mac")
    install_k8s.add_argument("--secret-name", default="")
    install_k8s.add_argument("--receipt-out", required=True)

    activate = sub.add_parser("activate")
    activate.add_argument("--agent-id", required=True)
    activate.add_argument("--principal-id", required=True)
    activate.add_argument("--receipt", required=True)
    activate.add_argument(
        "--manifest",
        required=True,
        help="one-time issuance manifest; deleted only after verified activation",
    )

    revoke = sub.add_parser("revoke")
    revoke.add_argument("--agent-id", required=True)

    discard_pending = sub.add_parser("discard-unreserved-pending")
    discard_pending.add_argument("--agent-id", required=True)
    discard_pending.add_argument("--created-by", required=True)

    inventory = sub.add_parser("inventory")
    inventory.add_argument("--agents", required=True, help="hub /agents JSON file or -")
    inventory.add_argument("--mode", choices=tuple(sorted(POLICY_MODES)), default=MODE_COMPATIBILITY)
    inventory.add_argument("--output")

    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=tuple(sorted(POLICY_MODES)))
    mode.add_argument("--inventory")
    mode.add_argument(
        "--review-live",
        action="store_true",
        help="review the current shared-DB inventory in this operation",
    )
    mode.add_argument("--policy-file")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    store: Optional[Store] = None
    lifecycle: Optional[WorkerCredentialLifecycle] = None

    def authority() -> WorkerCredentialLifecycle:
        nonlocal store, lifecycle
        if lifecycle is None:
            store = (
                SQLiteStore(str(Path(args.db).expanduser()))
                if args.db
                else make_store_from_env()
            )
            lifecycle = WorkerCredentialLifecycle(store)
        return lifecycle

    try:
        if args.command == "ensure-runtime":
            result = ensure_fleet_source_runtime(
                authority().store,
                args.source_commit,
                created_by=args.created_by,
            )
            _safe_print(result)
            return 0
        if args.command == "issue":
            issued = authority().issue(
                args.agent_id,
                fleet=args.fleet,
                environment=args.environment,
                expected_source_commit=args.expected_source_commit,
                expected_runtime_digest=args.expected_runtime_digest,
                required_capabilities=args.capability,
                package_capable=args.package_capable,
                expires_in=args.expires_in,
                actor=args.created_by,
            )
            manifest = installation_manifest(issued)
            _write_private_json(Path(args.manifest_out), manifest)
            _safe_print(
                {
                    "status": "issued",
                    "agent_id": args.agent_id,
                    "principal_id": issued.record["id"],
                    "worker_credential_version": issued.worker_version,
                    "token_fingerprint": issued.record["token_fingerprint"],
                    "manifest_written": True,
                }
            )
            return 0
        if args.command == "install-vm":
            manifest = _read_json(args.manifest)
            receipt = install_vm_manifest(
                manifest,
                Path(args.env_file),
                expected_agent_id=args.agent_id,
            )
            _write_private_json(Path(args.receipt_out), receipt)
            _safe_print({**receipt, "receipt_written": True})
            return 0
        if args.command == "install-k8s":
            manifest = _read_json(args.manifest)
            receipt = apply_kubernetes_secret(
                manifest,
                namespace=args.namespace,
                name=args.secret_name,
                expected_agent_id=args.agent_id,
            )
            _write_private_json(Path(args.receipt_out), receipt)
            _safe_print({**receipt, "receipt_written": True})
            return 0
        if args.command == "activate":
            manifest_path = Path(args.manifest).expanduser()
            manifest = _read_json(str(manifest_path))
            manifest_agent, _token = _validated_manifest(
                manifest, expected_agent_id=args.agent_id
            )
            if (
                manifest_agent != args.agent_id
                or manifest.get("principal_id") != args.principal_id
            ):
                raise WorkerCredentialError(
                    "activation manifest does not match requested principal"
                )
            result = authority().activate(
                args.agent_id,
                args.principal_id,
                receipt=_read_json(args.receipt),
            )
            # Consume the only hub-side raw-token copy after DB activation and
            # supersession commit. A failed activation intentionally leaves it
            # for a safe retry.
            manifest_path.unlink()
            _safe_print(
                {
                    "status": "active",
                    "agent_id": result["agent_id"],
                    "principal_id": result["id"],
                    "token_fingerprint": result["token_fingerprint"],
                }
            )
            return 0
        if args.command == "revoke":
            revoked = authority().revoke(args.agent_id)
            _safe_print(
                {
                    "status": "revoked",
                    "agent_id": args.agent_id,
                    "credential_count": len(revoked),
                }
            )
            return 0
        if args.command == "discard-unreserved-pending":
            discarded = authority().discard_unreserved_pending(
                args.agent_id,
                created_by=args.created_by,
            )
            _safe_print(
                {
                    "status": "discarded",
                    "agent_id": args.agent_id,
                    "credential_count": len(discarded),
                }
            )
            return 0
        if args.command == "inventory":
            agents_value = json.load(sys.stdin) if args.agents == "-" else json.loads(
                Path(args.agents).expanduser().read_text(encoding="utf-8")
            )
            if not isinstance(agents_value, list):
                raise WorkerCredentialError("agents input must be a JSON list")
            result = build_readiness_inventory(
                agents_value,
                authority().records(),
                mode=args.mode,
            )
            if args.output:
                _write_private_json(Path(args.output), result)
            _safe_print(result)
            return 0 if result["all_ready"] else 2
        if args.command == "set-mode":
            if args.inventory and args.review_live:
                raise WorkerCredentialError(
                    "set-mode accepts either --inventory or --review-live"
                )
            if args.review_live and args.policy_file:
                raise WorkerCredentialError(
                    "--review-live requires shared database policy authority"
                )
            readiness = _read_json(args.inventory) if args.inventory else None
            if args.review_live:
                readiness = _live_inventory(authority().store, args.mode)
            result = write_policy_state(
                args.mode,
                inventory=readiness,
                store=(authority().store if not args.policy_file else None),
                path=Path(args.policy_file).expanduser() if args.policy_file else None,
            )
            _safe_print(result)
            return 0
    except (
        ClientPrincipalError,
        WorkerCredentialError,
        StoreError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        # Exception messages are constructed from identifiers and invariant
        # names only.  Never include manifests, environment values, or tokens.
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
