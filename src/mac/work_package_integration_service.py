"""Fenced assembly of exact work-package candidates.

This module is the integration station between accepted component outputs and
external certification.  It intentionally has no canonical-publication path
and never executes repository code.  Its only remote write is creation of one
content-addressed ref below ``refs/mac/integration/``.

The durable protocol is:

1. freeze a deterministic ordered set of accepted, controller-verified inputs;
2. claim the batch with a monotonically increasing owner fence while atomically
   transferring acceptance-owned WIP from ``fan_in_reservation`` to ``integration``;
3. assemble exact protected-ref SHAs in a disposable, credential-free Git
   worktree;
4. create/read back the protected integration ref; and
5. re-lock the exact package, epoch, membership, WIP chain, owner, and fence
   before atomically assigning the candidate and advancing to ``verifying``.

The first implementation supports one level of mutation fan-in.  A nested
integration member is rejected: the current schema does not yet carry an
append-only controller-authored provenance record that binds an integration
node candidate to the exact producing batch.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from mac.gitops import validate_git_ref
from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
)
from mac.repository_hygiene import redact_repository_hygiene_text
from mac.repository_contract import resolve_repository_canonical_remote
from mac.store import Store
from mac.work_package_models import validate_supported_work_package_topology


WORK_PACKAGE_INTEGRATION_SERVICE_VERSION = "work-package-integration-service-v1"
INTEGRATION_BATCH_SCHEMA = "mac.work_package.integration_batch.v1"
INTEGRATION_INPUT_DIGEST_SCHEMA = "mac.work_package.integration_inputs.v1"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_TREE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


class WorkPackageIntegrationError(RuntimeError):
    """Base fail-closed integration error."""


class IntegrationBusyError(WorkPackageIntegrationError):
    """Another live owner currently holds the integration batch."""


class IntegrationLeaseLostError(WorkPackageIntegrationError):
    """The caller no longer owns the exact monotonic batch fence."""


class IntegrationBaseMovedError(WorkPackageIntegrationError):
    """The canonical target no longer names the frozen landing base."""


class IntegrationConflictError(WorkPackageIntegrationError):
    """Exact inputs cannot be assembled without ambiguity or conflict."""


CredentialEnvironment = Callable[[str, Mapping[str, Any]], Mapping[str, str]]
FaultHook = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class WorkPackageIntegrationConfig:
    lease_seconds: int = 120
    git_timeout_seconds: int = 300
    candidate_namespace: str = "refs/mac/integration"
    max_inputs: int = 256

    def __post_init__(self) -> None:
        if not 5 <= int(self.lease_seconds) <= 3600:
            raise ValueError("integration lease_seconds must be between 5 and 3600")
        if not 1 <= int(self.git_timeout_seconds) <= 3600:
            raise ValueError(
                "integration git_timeout_seconds must be between 1 and 3600"
            )
        if not 1 <= int(self.max_inputs) <= 10_000:
            raise ValueError("integration max_inputs must be between 1 and 10000")
        namespace = validate_git_ref(self.candidate_namespace)
        if not namespace.startswith("refs/mac/integration"):
            raise ValueError(
                "integration candidate namespace must be under refs/mac/integration"
            )


@dataclass(frozen=True)
class IntegrationLease:
    batch_id: str
    owner: str
    fence: int
    expires_at: str

    def to_dict(self) -> JsonDict:
        return {
            "batch_id": self.batch_id,
            "owner": self.owner,
            "fence": self.fence,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class IntegrationBatchCreation:
    batch_id: str
    package_id: str
    plan_version: int
    epoch: int
    integration_node_key: str
    landing_base_sha: str
    input_digest: str
    input_ids: Tuple[str, ...]
    created: bool

    def to_dict(self) -> JsonDict:
        return {
            "batch_id": self.batch_id,
            "package_id": self.package_id,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "integration_node_key": self.integration_node_key,
            "landing_base_sha": self.landing_base_sha,
            "input_digest": self.input_digest,
            "input_ids": list(self.input_ids),
            "created": self.created,
        }


@dataclass(frozen=True)
class IntegrationAssemblyOutcome:
    status: str
    batch_id: str
    candidate_sha: str = ""
    candidate_tree_digest: str = ""
    candidate_ref: str = ""
    input_digest: str = ""
    fence: int = 0
    recovered: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "status": self.status,
            "batch_id": self.batch_id,
            "candidate_sha": self.candidate_sha,
            "candidate_tree_digest": self.candidate_tree_digest,
            "candidate_ref": self.candidate_ref,
            "input_digest": self.input_digest,
            "fence": self.fence,
            "recovered": self.recovered,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class _Repository:
    id: str
    source: str = field(repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _WipInput:
    id: str
    package_id: str
    plan_version: int
    epoch: int
    node_key: str
    task_id: str
    resource_key: str
    token_kind: str
    generation: int
    capacity_units: int
    reservation_key: Optional[str]
    acquired_by_assignment_lease_id: str
    acquired_at: str

    def digest_value(self) -> JsonDict:
        return {
            "id": self.id,
            "resource_key": self.resource_key,
            "token_kind": self.token_kind,
            "generation": self.generation,
            "capacity_units": self.capacity_units,
            "acquired_by_assignment_lease_id": (self.acquired_by_assignment_lease_id),
        }


@dataclass(frozen=True)
class _Input:
    ordinal: int
    node_key: str
    node_generation: int
    task_id: str
    candidate_id: str
    assignment_lease_id: str
    attempt_number: int
    evidence_id: str
    artifact_digest: str
    verification_id: str
    verification_receipt_digest: str
    attempt_ref: str
    attempt_base_sha: str
    attempt_head_sha: str
    tree_digest: str
    declared_effects_digest: str
    observed_effects_digest: str
    changed_paths: Tuple[str, ...]
    wip_inputs: Tuple[_WipInput, ...]

    def digest_value(self) -> JsonDict:
        return {
            "ordinal": self.ordinal,
            "node_key": self.node_key,
            "node_generation": self.node_generation,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "assignment_lease_id": self.assignment_lease_id,
            "attempt_number": self.attempt_number,
            "evidence_id": self.evidence_id,
            "artifact_digest": self.artifact_digest,
            "verification_id": self.verification_id,
            "verification_receipt_digest": self.verification_receipt_digest,
            "attempt_ref": self.attempt_ref,
            "attempt_base_sha": self.attempt_base_sha,
            "attempt_head_sha": self.attempt_head_sha,
            "tree_digest": self.tree_digest,
            "declared_effects_digest": self.declared_effects_digest,
            "observed_effects_digest": self.observed_effects_digest,
            "changed_paths": list(self.changed_paths),
            "wip_inputs": [item.digest_value() for item in self.wip_inputs],
        }


@dataclass(frozen=True)
class _Batch:
    id: str
    package_id: str
    plan_version: int
    epoch: int
    repository_id: str
    target_ref: str
    assembly_base_sha: str
    landing_base_sha: str
    input_digest: str
    candidate_sha: Optional[str]
    candidate_tree_digest: Optional[str]
    candidate_ref: Optional[str]
    candidate_fence: Optional[int]
    state: str
    integration_task_id: str
    lease_owner: Optional[str]
    lease_expires_at: Optional[str]
    lease_fence: int
    metadata: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class _AssembledCandidate:
    sha: str
    tree_digest: str
    ref: str
    input_digest: str
    recovered_ref: bool


def compute_integration_input_digest(inputs: Sequence[_Input]) -> str:
    """Return the canonical digest of exact artifact and WIP membership."""

    payload = {
        "schema": INTEGRATION_INPUT_DIGEST_SCHEMA,
        "inputs": [item.digest_value() for item in inputs],
    }
    return "sha256:%s" % hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


class WorkPackageIntegrationService:
    """Controller-owned exact-candidate integration station."""

    def __init__(
        self,
        store: Store,
        *,
        owner: str,
        config: Optional[WorkPackageIntegrationConfig] = None,
        credential_environment: Optional[CredentialEnvironment] = None,
        now: Optional[Callable[[], datetime]] = None,
        fault_hook: Optional[FaultHook] = None,
    ) -> None:
        owner_value = str(owner or "").strip()
        if not owner_value:
            raise ValueError("integration owner is required")
        self.store = store
        self.owner = owner_value
        self.config = config or WorkPackageIntegrationConfig()
        self.credential_environment = credential_environment or (
            lambda _operation, _repository: {}
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._fault_hook = fault_hook or (lambda _stage, _detail: None)

    # -- Read model ------------------------------------------------------------

    def status(self, batch_id: str) -> JsonDict:
        """Return a secret-free, integrity-checked integration-batch snapshot."""

        batch = self._batch(batch_id)
        inputs = self._batch_inputs(self.store, batch)
        observed_digest = compute_integration_input_digest(inputs)
        if observed_digest != batch.input_digest:
            raise TransitionError(
                "integration batch membership no longer matches its digest"
            )
        held_rows = self.store.query_all(
            "SELECT id FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND stage = 'integration' "
            "AND state = 'held' AND reservation_key = ? ORDER BY id",
            (batch.package_id, batch.plan_version, batch.epoch, batch.id),
        )
        return {
            "schema": INTEGRATION_BATCH_SCHEMA,
            "service_version": WORK_PACKAGE_INTEGRATION_SERVICE_VERSION,
            "batch_id": batch.id,
            "package_id": batch.package_id,
            "plan_version": batch.plan_version,
            "epoch": batch.epoch,
            "integration_node_key": self._integration_node_key(batch),
            "integration_task_id": batch.integration_task_id,
            "repository_id": batch.repository_id,
            "target_ref": batch.target_ref,
            "assembly_base_sha": batch.assembly_base_sha,
            "landing_base_sha": batch.landing_base_sha,
            "input_digest": batch.input_digest,
            "state": batch.state,
            "candidate_sha": batch.candidate_sha,
            "candidate_tree_digest": batch.candidate_tree_digest,
            "candidate_ref": batch.candidate_ref,
            "candidate_fence": batch.candidate_fence,
            "lease_owner": batch.lease_owner,
            "lease_expires_at": batch.lease_expires_at,
            "lease_fence": batch.lease_fence,
            "created_at": batch.created_at,
            "inputs": [
                {
                    "ordinal": item.ordinal,
                    "node_key": item.node_key,
                    "node_generation": item.node_generation,
                    "task_id": item.task_id,
                    "candidate_id": item.candidate_id,
                    "assignment_lease_id": item.assignment_lease_id,
                    "attempt_number": item.attempt_number,
                    "evidence_id": item.evidence_id,
                    "artifact_digest": item.artifact_digest,
                    "verification_id": item.verification_id,
                    "verification_receipt_digest": (item.verification_receipt_digest),
                    "attempt_ref": item.attempt_ref,
                    "attempt_base_sha": item.attempt_base_sha,
                    "attempt_head_sha": item.attempt_head_sha,
                    "tree_digest": item.tree_digest,
                    "changed_paths": list(item.changed_paths),
                    "wip_predecessor_ids": [token.id for token in item.wip_inputs],
                }
                for item in inputs
            ],
            "held_integration_wip_token_ids": [str(row["id"]) for row in held_rows],
        }

    # -- Batch creation ---------------------------------------------------------

    def create_batch(
        self,
        package_id: str,
        integration_node_key: str,
        *,
        actor: str,
    ) -> IntegrationBatchCreation:
        package_value = str(package_id or "").strip()
        node_value = str(integration_node_key or "").strip()
        actor_value = str(actor or "").strip()
        if not package_value or not node_value or not actor_value:
            raise ValidationError(
                "package id, integration node key, and actor are required"
            )

        preview = self._package_context(package_value)
        validate_supported_work_package_topology(preview["definition"])
        repository = self._repository_from_context(preview)
        target_ref, members = self._integration_group(preview["definition"], node_value)
        landing_base = self._remote_ref(repository, target_ref, operation="read")
        now = self._iso_now()

        with self.store.transaction() as conn:
            self._lock_current_package(conn, preview)
            current = self._package_context(package_value, conn=conn)
            self._assert_same_package_context(preview, current)
            validate_supported_work_package_topology(current["definition"])
            target_ref_locked, members_locked = self._integration_group(
                current["definition"], node_value
            )
            if target_ref_locked != target_ref or members_locked != members:
                raise TransitionError("integration group changed during batch creation")
            repository_locked = self._repository_from_context(current)
            if repository_locked != repository:
                raise TransitionError(
                    "registered repository changed during batch creation"
                )

            integration_task_id = self._integration_task(conn, current, node_value)
            active_batches = conn.execute(
                "SELECT * FROM work_package_integration_batches "
                "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
                "AND repository_id = ? AND integration_task_id = ? "
                "AND target_ref = ? AND assembly_base_sha = ? AND landing_base_sha = ? "
                "AND state IN ('queued', 'assembling', 'verifying') "
                "ORDER BY created_at, id",
                (
                    package_value,
                    int(current["plan_version"]),
                    int(current["epoch"]),
                    repository.id,
                    integration_task_id,
                    target_ref,
                    landing_base,
                    landing_base,
                ),
            ).fetchall()
            if len(active_batches) > 1:
                raise TransitionError(
                    "integration station has multiple active batches for one exact base"
                )
            if active_batches:
                active = self._batch_from_row(active_batches[0])
                active_inputs = self._batch_inputs(conn, active)
                active_digest = compute_integration_input_digest(active_inputs)
                expected_id = self._batch_id(
                    package_id=package_value,
                    plan_version=int(current["plan_version"]),
                    epoch=int(current["epoch"]),
                    integration_node_key=node_value,
                    target_ref=target_ref,
                    landing_base_sha=landing_base,
                    input_digest=active_digest,
                )
                if (
                    active.id != expected_id
                    or active.input_digest != active_digest
                    or self._integration_node_key(active) != node_value
                    or tuple(item.node_key for item in active_inputs) != members
                ):
                    raise TransitionError(
                        "active integration batch conflicts with deterministic identity"
                    )
                input_ids = tuple(
                    str(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM work_package_batch_inputs "
                        "WHERE batch_id = ? ORDER BY ordinal, id",
                        (active.id,),
                    ).fetchall()
                )
                return self._creation_result(
                    active,
                    node_value=node_value,
                    input_ids=input_ids,
                    created=False,
                )
            inputs = self._accepted_inputs(
                conn,
                package_id=package_value,
                plan_version=int(current["plan_version"]),
                epoch=int(current["epoch"]),
                repository_id=repository.id,
                members=members,
                batch_id=None,
            )
            input_digest = compute_integration_input_digest(inputs)
            batch_id = self._batch_id(
                package_id=package_value,
                plan_version=int(current["plan_version"]),
                epoch=int(current["epoch"]),
                integration_node_key=node_value,
                target_ref=target_ref,
                landing_base_sha=landing_base,
                input_digest=input_digest,
            )
            existing = conn.execute(
                "SELECT * FROM work_package_integration_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                batch = self._batch_from_row(existing)
                self._assert_batch_identity(
                    batch,
                    package_id=package_value,
                    plan_version=int(current["plan_version"]),
                    epoch=int(current["epoch"]),
                    integration_task_id=integration_task_id,
                    target_ref=target_ref,
                    landing_base_sha=landing_base,
                    input_digest=input_digest,
                    integration_node_key=node_value,
                )
                input_ids = tuple(
                    str(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM work_package_batch_inputs "
                        "WHERE batch_id = ? ORDER BY ordinal, id",
                        (batch_id,),
                    ).fetchall()
                )
                expected_ids = tuple(
                    self._batch_input_id(batch_id, item) for item in inputs
                )
                if input_ids != expected_ids:
                    raise TransitionError(
                        "existing deterministic batch has different membership"
                    )
                return self._creation_result(
                    batch,
                    node_value=node_value,
                    input_ids=input_ids,
                    created=False,
                )

            metadata = {
                "schema": INTEGRATION_BATCH_SCHEMA,
                "service_version": WORK_PACKAGE_INTEGRATION_SERVICE_VERSION,
                "integration_node_key": node_value,
                "input_digest_schema": INTEGRATION_INPUT_DIGEST_SCHEMA,
                "member_node_keys": list(members),
                "created_by": actor_value,
            }
            conn.execute(
                "INSERT INTO work_package_integration_batches ("
                "id, package_id, plan_version, epoch, repository_id, target_ref, "
                "assembly_base_sha, landing_base_sha, input_digest, state, "
                "integration_task_id, metadata, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (
                    batch_id,
                    package_value,
                    int(current["plan_version"]),
                    int(current["epoch"]),
                    repository.id,
                    target_ref,
                    landing_base,
                    landing_base,
                    input_digest,
                    integration_task_id,
                    json_dumps(metadata),
                    now,
                    now,
                ),
            )
            input_ids = []
            for item in inputs:
                input_id = self._batch_input_id(batch_id, item)
                conn.execute(
                    "INSERT INTO work_package_batch_inputs ("
                    "id, batch_id, package_id, plan_version, epoch, ordinal, "
                    "node_key, node_generation, task_id, candidate_id, "
                    "candidate_status, assignment_lease_id, attempt_number, "
                    "evidence_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?)",
                    (
                        input_id,
                        batch_id,
                        package_value,
                        int(current["plan_version"]),
                        int(current["epoch"]),
                        item.ordinal,
                        item.node_key,
                        item.node_generation,
                        item.task_id,
                        item.candidate_id,
                        item.assignment_lease_id,
                        item.attempt_number,
                        item.evidence_id,
                        now,
                    ),
                )
                input_ids.append(input_id)
            self._append_history(
                conn,
                package_id=package_value,
                plan_version=int(current["plan_version"]),
                epoch=int(current["epoch"]),
                actor=actor_value,
                event_type="work_package.integration_batch_created",
                detail={
                    "batch_id": batch_id,
                    "integration_node_key": node_value,
                    "input_digest": input_digest,
                    "input_ids": input_ids,
                    "landing_base_sha": landing_base,
                    "target_ref": target_ref,
                    "service_version": WORK_PACKAGE_INTEGRATION_SERVICE_VERSION,
                },
                now=now,
            )

        batch = self._batch(batch_id)
        return self._creation_result(
            batch,
            node_value=node_value,
            input_ids=tuple(input_ids),
            created=True,
        )

    # -- Fenced claim -----------------------------------------------------------

    def claim(self, batch_id: str) -> IntegrationLease:
        batch = self._batch(batch_id)
        repository = self._repository(batch.repository_id)
        observed = self._remote_ref(repository, batch.target_ref, operation="read")
        if observed != batch.landing_base_sha:
            raise IntegrationBaseMovedError(
                "canonical target moved away from the frozen integration base"
            )
        return self._claim(batch_id)

    def _claim(self, batch_id: str) -> IntegrationLease:
        now_dt = self._now_utc()
        now = self._iso(now_dt)
        expires = self._iso(now_dt + timedelta(seconds=int(self.config.lease_seconds)))
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM work_package_integration_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(
                    "work-package integration batch not found: %s" % batch_id
                )
            batch = self._batch_from_row(row)
            self._lock_batch_context(conn, batch)
            if batch.state not in {"queued", "assembling"}:
                raise TransitionError(
                    "integration batch cannot be claimed from %s" % batch.state
                )
            if batch.candidate_sha is not None:
                raise TransitionError(
                    "assembling batch already has an unfinalized candidate identity"
                )
            inputs = self._batch_inputs(conn, batch)
            if compute_integration_input_digest(inputs) != batch.input_digest:
                raise TransitionError(
                    "integration batch membership no longer matches its digest"
                )

            live = batch.lease_owner is not None and not self._expired(
                batch.lease_expires_at, now_dt
            )
            if live and batch.lease_owner != self.owner:
                raise IntegrationBusyError("integration batch is leased")
            if live and batch.lease_owner == self.owner:
                fence = batch.lease_fence
                lease_expires = str(batch.lease_expires_at)
                if batch.state == "queued":
                    raise TransitionError("queued batch has an incoherent live lease")
            else:
                fence = batch.lease_fence + 1
                lease_expires = expires
                update = conn.execute(
                    "UPDATE work_package_integration_batches SET state = 'assembling', "
                    "lease_owner = ?, lease_expires_at = ?, lease_fence = ?, "
                    "updated_at = ? WHERE id = ? AND state = ? AND lease_fence = ? "
                    "AND (lease_owner IS NULL OR lease_expires_at <= ?)",
                    (
                        self.owner,
                        expires,
                        fence,
                        now,
                        batch.id,
                        batch.state,
                        batch.lease_fence,
                        now,
                    ),
                )
                if update.rowcount != 1:
                    raise IntegrationBusyError("integration batch claim CAS failed")

            self._transfer_wip_to_integration(conn, batch, inputs, now=now)
            self._assert_integration_wip(conn, batch, inputs)
            self._append_history_once_per_fence(
                conn,
                batch=batch,
                fence=fence,
                actor=self.owner,
                event_type="work_package.integration_batch_claimed",
                detail={
                    "batch_id": batch.id,
                    "owner": self.owner,
                    "fence": fence,
                    "lease_expires_at": lease_expires,
                },
                now=now,
            )
        return IntegrationLease(batch.id, self.owner, fence, lease_expires)

    # -- Assembly ---------------------------------------------------------------

    def assemble(self, batch_id: str) -> IntegrationAssemblyOutcome:
        initial = self._batch(batch_id)
        repository = self._repository(initial.repository_id)
        if initial.state == "verifying":
            return self._recover_verifying(initial, repository)
        if initial.state in {
            "rejected",
            "stale",
            "cancelled",
            "certified",
            "published",
        }:
            return IntegrationAssemblyOutcome(
                status=initial.state,
                batch_id=initial.id,
                candidate_sha=initial.candidate_sha or "",
                candidate_tree_digest=initial.candidate_tree_digest or "",
                candidate_ref=initial.candidate_ref or "",
                input_digest=initial.input_digest,
                fence=initial.candidate_fence or initial.lease_fence,
                recovered=True,
            )

        observed = self._remote_ref(repository, initial.target_ref, operation="read")
        if observed != initial.landing_base_sha:
            if initial.state == "assembling":
                lease = self._claim(batch_id)
                self._terminalize(
                    lease,
                    state="stale",
                    reason="canonical target moved before integration assembly",
                )
            raise IntegrationBaseMovedError(
                "canonical target moved away from the frozen integration base"
            )

        lease = self._claim(batch_id)
        batch = self._batch(batch_id)
        try:
            candidate = self._assemble_exact(batch, repository, lease)
            self._fault_hook(
                "after_candidate_push_before_finalize",
                {
                    "batch_id": batch.id,
                    "candidate_sha": candidate.sha,
                    "candidate_ref": candidate.ref,
                    "fence": lease.fence,
                },
            )
            self._finalize(batch, lease, candidate)
            return IntegrationAssemblyOutcome(
                status="assembled",
                batch_id=batch.id,
                candidate_sha=candidate.sha,
                candidate_tree_digest=candidate.tree_digest,
                candidate_ref=candidate.ref,
                input_digest=candidate.input_digest,
                fence=lease.fence,
                recovered=candidate.recovered_ref,
            )
        except IntegrationBaseMovedError as exc:
            self._terminalize(lease, state="stale", reason=str(exc))
            raise
        except IntegrationConflictError as exc:
            self._terminalize(lease, state="rejected", reason=str(exc))
            raise

    def _assemble_exact(
        self,
        batch: _Batch,
        repository: _Repository,
        lease: IntegrationLease,
    ) -> _AssembledCandidate:
        context = tempfile.TemporaryDirectory(prefix="mac-integration-assemble-")
        try:
            root = Path(context.name)
            checkout = root / "repo"
            checkout.mkdir()
            self._git(
                repository,
                ["init", "--initial-branch=integration", str(checkout)],
                cwd=None,
                root=root,
                operation="read",
                label="initialize disposable integration worktree",
            )
            hooks = root / "disabled-hooks"
            hooks.mkdir()
            self._git(
                repository,
                [
                    "config",
                    "core.hooksPath",
                    str(hooks),
                ],
                cwd=checkout,
                root=root,
                operation="read",
                label="disable integration hooks",
            )
            self._fetch_exact(
                repository,
                checkout,
                root,
                ref=batch.target_ref,
                expected_sha=batch.assembly_base_sha,
                local_ref="refs/mac/controller-integration/base",
                base_ref=True,
            )
            self._git(
                repository,
                ["checkout", "--detach", batch.assembly_base_sha],
                cwd=checkout,
                root=root,
                operation="read",
                label="checkout exact integration base",
            )

            inputs = self._batch_inputs(self.store, batch)
            input_digest = compute_integration_input_digest(inputs)
            if input_digest != batch.input_digest:
                raise TransitionError(
                    "integration inputs changed before repository assembly"
                )
            touched: set[str] = set()
            for item in inputs:
                overlap = touched.intersection(item.changed_paths)
                if overlap:
                    raise IntegrationConflictError(
                        "accepted inputs overlap controller-observed paths: %s"
                        % ", ".join(sorted(overlap)[:20])
                    )
                touched.update(item.changed_paths)
                local_ref = "refs/mac/controller-integration/input-%04d" % item.ordinal
                self._fetch_exact(
                    repository,
                    checkout,
                    root,
                    ref=item.attempt_ref,
                    expected_sha=item.attempt_head_sha,
                    local_ref=local_ref,
                )
                self._verify_input_observation(repository, checkout, root, item)
                self._reject_ancestry_ambiguity(
                    repository,
                    checkout,
                    root,
                    item.attempt_head_sha,
                    first=item.ordinal == 0,
                )
                commit_env = self._commit_environment(batch)
                result = self._git_raw(
                    repository,
                    [
                        "-c",
                        "user.name=MAC Integration Controller",
                        "-c",
                        "user.email=integration@mac.invalid",
                        "-c",
                        "commit.gpgSign=false",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        "--no-stat",
                        "--no-log",
                        "-m",
                        "MAC integration %s input %04d" % (batch.id, item.ordinal),
                        item.attempt_head_sha,
                    ],
                    cwd=checkout,
                    root=root,
                    operation="read",
                    extra_env=commit_env,
                )
                if result.returncode != 0:
                    self._git_raw(
                        repository,
                        ["merge", "--abort"],
                        cwd=checkout,
                        root=root,
                        operation="read",
                    )
                    raise IntegrationConflictError(
                        "exact accepted inputs do not merge cleanly"
                    )

            candidate_sha = self._rev_parse(
                repository, checkout, root, "HEAD", "assembled candidate"
            )
            tree_sha = self._rev_parse_object(
                repository, checkout, root, "HEAD^{tree}", "candidate tree"
            )
            ancestry = self._git_raw(
                repository,
                [
                    "merge-base",
                    "--is-ancestor",
                    batch.landing_base_sha,
                    candidate_sha,
                ],
                cwd=checkout,
                root=root,
                operation="read",
            )
            if ancestry.returncode != 0:
                raise IntegrationConflictError(
                    "assembled candidate does not preserve the frozen landing base"
                )
            candidate_ref = self._candidate_ref(batch)
            self._assert_lease(lease)
            recovered_ref = self._stage_candidate(
                repository,
                candidate_ref=candidate_ref,
                candidate_sha=candidate_sha,
                checkout=checkout,
                root=root,
            )
            return _AssembledCandidate(
                sha=candidate_sha,
                tree_digest="git-tree:%s" % tree_sha,
                ref=candidate_ref,
                input_digest=input_digest,
                recovered_ref=recovered_ref,
            )
        finally:
            context.cleanup()

    # -- Candidate staging/finalization ----------------------------------------

    def _stage_candidate(
        self,
        repository: _Repository,
        *,
        candidate_ref: str,
        candidate_sha: str,
        checkout: Path,
        root: Path,
    ) -> bool:
        current = self._remote_ref(
            repository,
            candidate_ref,
            operation="write",
            missing_ok=True,
            root=root,
        )
        if current and current != candidate_sha:
            raise IntegrationConflictError(
                "protected integration ref is occupied by another candidate"
            )
        recovered = current == candidate_sha
        if not current:
            self._git(
                repository,
                [
                    "push",
                    "--porcelain",
                    "--force-with-lease=%s:" % candidate_ref,
                    repository.source,
                    "%s:%s" % (candidate_sha, candidate_ref),
                ],
                cwd=checkout,
                root=root,
                operation="write",
                label="create protected integration candidate",
            )
        readback = self._remote_ref(
            repository,
            candidate_ref,
            operation="write",
            root=root,
        )
        if readback != candidate_sha:
            raise WorkPackageIntegrationError(
                "protected integration candidate read-back failed"
            )
        return recovered

    def _finalize(
        self,
        batch: _Batch,
        lease: IntegrationLease,
        candidate: _AssembledCandidate,
    ) -> None:
        now = self._iso_now()
        with self.store.transaction() as conn:
            current_row = conn.execute(
                "SELECT * FROM work_package_integration_batches WHERE id = ?",
                (batch.id,),
            ).fetchone()
            if current_row is None:
                raise IntegrationLeaseLostError(
                    "integration batch disappeared before finalization"
                )
            current = self._batch_from_row(current_row)
            self._lock_batch_context(conn, current)
            if (
                current.state != "assembling"
                or current.lease_owner != lease.owner
                or current.lease_fence != lease.fence
                or self._expired(current.lease_expires_at, self._now_utc())
            ):
                raise IntegrationLeaseLostError(
                    "integration owner fence is stale during finalization"
                )
            inputs = self._batch_inputs(conn, current)
            if compute_integration_input_digest(inputs) != candidate.input_digest:
                raise IntegrationLeaseLostError(
                    "integration membership changed during assembly"
                )
            if candidate.input_digest != current.input_digest:
                raise IntegrationLeaseLostError(
                    "assembled input digest does not match the frozen batch"
                )
            self._assert_integration_wip(conn, current, inputs)
            station, certification_successor = self._controller_station_context(
                conn, current
            )
            assign = conn.execute(
                "UPDATE work_package_integration_batches SET candidate_sha = ?, "
                "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = ?, "
                "updated_at = ? WHERE id = ? AND state = 'assembling' "
                "AND candidate_sha IS NULL AND lease_owner = ? AND lease_fence = ? "
                "AND lease_expires_at > ?",
                (
                    candidate.sha,
                    candidate.tree_digest,
                    candidate.ref,
                    lease.fence,
                    now,
                    current.id,
                    lease.owner,
                    lease.fence,
                    now,
                ),
            )
            if assign.rowcount != 1:
                raise IntegrationLeaseLostError(
                    "integration candidate assignment CAS failed"
                )
            transition = conn.execute(
                "UPDATE work_package_integration_batches SET state = 'verifying', "
                "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND state = 'assembling' AND lease_owner = ? "
                "AND lease_fence = ? AND candidate_sha = ? AND candidate_fence = ?",
                (
                    now,
                    current.id,
                    lease.owner,
                    lease.fence,
                    candidate.sha,
                    lease.fence,
                ),
            )
            if transition.rowcount != 1:
                raise IntegrationLeaseLostError(
                    "integration verifying transition CAS failed"
                )
            receipt = self._record_integration_station_receipt(
                conn,
                batch=current,
                station=station,
                candidate=candidate,
                certification_successor=certification_successor,
                actor=lease.owner,
                now=now,
            )
            self._complete_integration_station(
                conn,
                batch=current,
                station=station,
                receipt=receipt,
                actor=lease.owner,
                now=now,
            )
            if certification_successor is not None:
                self._ready_certification_successor(
                    conn,
                    batch=current,
                    successor=certification_successor,
                    receipt=receipt,
                    actor=lease.owner,
                    now=now,
                )
            self._append_history(
                conn,
                package_id=current.package_id,
                plan_version=current.plan_version,
                epoch=current.epoch,
                actor=lease.owner,
                event_type="work_package.integration_candidate_assembled",
                detail={
                    "batch_id": current.id,
                    "candidate_sha": candidate.sha,
                    "candidate_tree_digest": candidate.tree_digest,
                    "candidate_ref": candidate.ref,
                    "candidate_fence": lease.fence,
                    "input_digest": candidate.input_digest,
                    "controller_station_receipt_id": receipt["id"],
                    "integration_task_id": current.integration_task_id,
                    "certification_task_id": (
                        certification_successor["task_id"]
                        if certification_successor is not None
                        else None
                    ),
                    "service_version": WORK_PACKAGE_INTEGRATION_SERVICE_VERSION,
                },
                now=now,
            )

    def _controller_station_context(
        self,
        conn: Any,
        batch: _Batch,
    ) -> Tuple[JsonDict, Optional[JsonDict]]:
        """Lock the integration station and resolve its one exact cert successor."""

        row = conn.execute(
            "SELECT link.node_key, link.node_state, task.id AS task_id, "
            "task.state AS task_state, task.metadata AS task_metadata, "
            "task.dependencies AS task_dependencies, task.owner_agent_id, task.lease_id "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.task_id = ? AND link.package_id = ? AND link.plan_version = ? "
            "AND link.epoch = ?",
            (
                batch.integration_task_id,
                batch.package_id,
                batch.plan_version,
                batch.epoch,
            ),
        ).fetchone()
        if row is None:
            raise TransitionError("integration controller station disappeared")
        station = dict(row)
        self._require_controller_task(
            station,
            batch=batch,
            node_key=self._integration_node_key(batch),
            node_type="integration",
            allowed_link_states={"planned", "ready"},
            allowed_task_states={"open", "waiting"},
        )

        context = self._package_context(batch.package_id, conn=conn)
        definition = context["definition"]
        raw_nodes = definition.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ValidationError("work-package plan has no exact node list")
        nodes = {
            str(item.get("node_key") or ""): item
            for item in raw_nodes
            if isinstance(item, Mapping) and item.get("node_key")
        }
        if len(nodes) != len(raw_nodes):
            raise ValidationError("work-package plan node identities are malformed")
        integration_key = str(station["node_key"])
        integration_node = nodes.get(integration_key)
        if not isinstance(integration_node, Mapping) or integration_node.get("kind") != "integration":
            raise ValidationError("integration task is not an integration plan node")
        matches = []
        for node_key, node in nodes.items():
            depends_on = node.get("depends_on")
            if (
                node.get("kind") == "certification"
                and isinstance(depends_on, list)
                and integration_key in depends_on
            ):
                matches.append((node_key, node))
        if len(matches) > 1:
            raise ValidationError(
                "integration node has ambiguous certification successors"
            )
        if not matches:
            return station, None

        certification_key, certification_node = matches[0]
        depends_on = certification_node.get("depends_on")
        if depends_on != [integration_key]:
            raise ValidationError(
                "multi-batch certification fan-in lacks an exact candidate contract"
            )
        successor_row = conn.execute(
            "SELECT link.node_key, link.node_state, task.id AS task_id, "
            "task.state AS task_state, task.metadata AS task_metadata, "
            "task.dependencies AS task_dependencies, task.owner_agent_id, task.lease_id "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? AND link.epoch = ? "
            "AND link.node_key = ?",
            (
                batch.package_id,
                batch.plan_version,
                batch.epoch,
                certification_key,
            ),
        ).fetchone()
        if successor_row is None:
            raise ValidationError("certification successor task is missing")
        successor = dict(successor_row)
        self._require_controller_task(
            successor,
            batch=batch,
            node_key=certification_key,
            node_type="certification",
            allowed_link_states={"planned", "ready"},
            allowed_task_states={"waiting"},
        )
        expected_dependencies = [batch.integration_task_id]
        observed_dependencies = json_loads(successor["task_dependencies"], None)
        if observed_dependencies != expected_dependencies:
            raise ValidationError(
                "certification successor dependencies deviate from immutable plan"
            )
        successor["plan_node"] = dict(certification_node)
        return station, successor

    @staticmethod
    def _require_controller_task(
        row: Mapping[str, Any],
        *,
        batch: _Batch,
        node_key: str,
        node_type: str,
        allowed_link_states: set[str],
        allowed_task_states: set[str],
    ) -> None:
        metadata = json_loads(row.get("task_metadata"), None)
        projection = metadata.get("work_package") if isinstance(metadata, dict) else None
        if (
            row.get("node_key") != node_key
            or row.get("node_state") not in allowed_link_states
            or row.get("task_state") not in allowed_task_states
            or row.get("owner_agent_id") is not None
            or row.get("lease_id") is not None
            or not isinstance(metadata, dict)
            or metadata.get("no_dispatch") is not True
            or not isinstance(projection, dict)
            or projection.get("package_id") != batch.package_id
            or int(projection.get("plan_version", 0)) != batch.plan_version
            or int(projection.get("epoch", 0)) != batch.epoch
            or projection.get("node_key") != node_key
            or projection.get("node_type") != node_type
        ):
            raise TransitionError(
                "%s controller task is not exact, held, and unclaimed" % node_type
            )

    def _record_integration_station_receipt(
        self,
        conn: Any,
        *,
        batch: _Batch,
        station: Mapping[str, Any],
        candidate: _AssembledCandidate,
        certification_successor: Optional[Mapping[str, Any]],
        actor: str,
        now: str,
    ) -> JsonDict:
        detail = {
            "schema": "mac.work_package.controller_station_receipt.v1",
            "station_kind": "integration",
            "batch_id": batch.id,
            "integration_task_id": batch.integration_task_id,
            "integration_node_key": station["node_key"],
            "certification_task_id": (
                certification_successor["task_id"]
                if certification_successor is not None
                else None
            ),
            "certification_node_key": (
                certification_successor["node_key"]
                if certification_successor is not None
                else None
            ),
            "candidate_sha": candidate.sha,
            "candidate_tree_digest": candidate.tree_digest,
            "candidate_ref": candidate.ref,
            "candidate_fence": batch.lease_fence,
            "input_digest": candidate.input_digest,
        }
        identity = {
            "station_kind": "integration",
            "task_id": batch.integration_task_id,
            "package_id": batch.package_id,
            "plan_version": batch.plan_version,
            "epoch": batch.epoch,
            "node_key": station["node_key"],
            "batch_id": batch.id,
            "outcome": "integrated",
            "detail": detail,
        }
        provenance_digest = self._json_digest(identity)
        receipt_id = "wpstation_%s" % provenance_digest.split(":", 1)[1][:32]
        conn.execute(
            "INSERT INTO work_package_controller_station_receipts ("
            "id, station_kind, task_id, package_id, plan_version, epoch, node_key, "
            "batch_id, certification_job_id, certification_id, outcome, "
            "result_digest, provenance_digest, actor, detail, created_at"
            ") VALUES (?, 'integration', ?, ?, ?, ?, ?, ?, NULL, NULL, "
            "'integrated', NULL, ?, ?, ?, ?)",
            (
                receipt_id,
                batch.integration_task_id,
                batch.package_id,
                batch.plan_version,
                batch.epoch,
                station["node_key"],
                batch.id,
                provenance_digest,
                actor,
                json_dumps(detail),
                now,
            ),
        )
        return {
            "id": receipt_id,
            "provenance_digest": provenance_digest,
            "detail": detail,
        }

    def _complete_integration_station(
        self,
        conn: Any,
        *,
        batch: _Batch,
        station: Mapping[str, Any],
        receipt: Mapping[str, Any],
        actor: str,
        now: str,
    ) -> None:
        changed = conn.execute(
            "UPDATE tasks SET state = 'completed', completed_at = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND owner_agent_id IS NULL AND lease_id IS NULL",
            (now, now, batch.integration_task_id, station["task_state"]),
        )
        if changed.rowcount != 1:
            raise TransitionError("integration controller task changed during completion")
        link = conn.execute(
            "UPDATE work_package_task_links SET node_state = 'integrated' "
            "WHERE task_id = ? AND package_id = ? AND plan_version = ? AND epoch = ? "
            "AND node_key = ? AND node_state = ?",
            (
                batch.integration_task_id,
                batch.package_id,
                batch.plan_version,
                batch.epoch,
                station["node_key"],
                station["node_state"],
            ),
        )
        if link.rowcount != 1:
            raise TransitionError("integration controller link changed during completion")
        detail = {
            "schema": "mac.work_package.controller_task_transition.v1",
            "station_kind": "integration",
            "batch_id": batch.id,
            "controller_station_receipt_id": receipt["id"],
            "provenance_digest": receipt["provenance_digest"],
            "node_key": station["node_key"],
            "node_state": "integrated",
        }
        self._append_task_transition(
            conn,
            task_id=batch.integration_task_id,
            actor=actor,
            from_state=str(station["task_state"]),
            to_state="completed",
            detail=detail,
            now=now,
        )

    def _ready_certification_successor(
        self,
        conn: Any,
        *,
        batch: _Batch,
        successor: Mapping[str, Any],
        receipt: Mapping[str, Any],
        actor: str,
        now: str,
    ) -> None:
        if successor["node_state"] == "planned":
            changed = conn.execute(
                "UPDATE work_package_task_links SET node_state = 'ready' "
                "WHERE task_id = ? AND package_id = ? AND plan_version = ? AND epoch = ? "
                "AND node_key = ? AND node_state = 'planned'",
                (
                    successor["task_id"],
                    batch.package_id,
                    batch.plan_version,
                    batch.epoch,
                    successor["node_key"],
                ),
            )
            if changed.rowcount != 1:
                raise TransitionError("certification successor changed during release")
        detail = {
            "schema": "mac.work_package.controller_station_ready.v1",
            "station_kind": "certification",
            "batch_id": batch.id,
            "integration_task_id": batch.integration_task_id,
            "controller_station_receipt_id": receipt["id"],
            "provenance_digest": receipt["provenance_digest"],
            "node_key": successor["node_key"],
            "dispatch_mode": "controller_station",
        }
        self._append_task_history(
            conn,
            task_id=str(successor["task_id"]),
            event_type="work_package.controller_station_ready",
            actor=actor,
            from_state="waiting",
            to_state="waiting",
            detail=detail,
            now=now,
        )

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
        WorkPackageIntegrationService._append_task_history(
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
            "status, attempts, created_at, processed_at"
            ") VALUES (?, ?, 'task.lifecycle', ?, ?, ?, ?, 'pending', 0, ?, NULL)",
            (
                new_id("tout"),
                task_id,
                actor,
                from_state,
                to_state,
                json_dumps(dict(detail)),
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
        from_state: str,
        to_state: str,
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
    def _json_digest(value: Mapping[str, Any]) -> str:
        return "sha256:%s" % hashlib.sha256(
            json_dumps(dict(value)).encode("utf-8")
        ).hexdigest()

    # -- WIP -------------------------------------------------------------------

    def _transfer_wip_to_integration(
        self,
        conn: Any,
        batch: _Batch,
        inputs: Sequence[_Input],
        *,
        now: str,
    ) -> None:
        for item in inputs:
            for token in item.wip_inputs:
                successor_id = self._integration_wip_id(batch.id, token.id)
                existing = conn.execute(
                    "SELECT * FROM work_package_wip_tokens WHERE id = ?",
                    (successor_id,),
                ).fetchone()
                source = conn.execute(
                    "SELECT state, stage, release_reason FROM work_package_wip_tokens "
                    "WHERE id = ?",
                    (token.id,),
                ).fetchone()
                if source is None or source["stage"] != "fan_in_reservation":
                    raise TransitionError(
                        "integration WIP predecessor disappeared or changed stage"
                    )
                release_reason = "integration_transfer:%s" % batch.id
                if source["state"] == "held":
                    released = conn.execute(
                        "UPDATE work_package_wip_tokens SET state = 'released', "
                        "released_at = ?, release_reason = ? "
                        "WHERE id = ? AND state = 'held' "
                        "AND stage = 'fan_in_reservation'",
                        (now, release_reason, token.id),
                    )
                    if released.rowcount != 1:
                        raise TransitionError(
                            "fan-in WIP changed during integration claim"
                        )
                elif not (
                    source["state"] == "released"
                    and source["release_reason"] == release_reason
                ):
                    raise TransitionError(
                        "fan-in WIP was consumed by another batch"
                    )

                expected = self._integration_wip_values(
                    batch=batch,
                    source=token,
                    successor_id=successor_id,
                    now=(str(existing["acquired_at"]) if existing is not None else now),
                )
                if existing is None:
                    conn.execute(
                        "INSERT INTO work_package_wip_tokens ("
                        "id, package_id, plan_version, epoch, node_key, task_id, "
                        "resource_key, token_kind, stage, state, generation, "
                        "capacity_units, reservation_key, predecessor_token_id, "
                        "acquired_by_assignment_lease_id, acquired_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'integration', 'held', "
                        "?, ?, ?, ?, ?, ?)",
                        expected,
                    )
                else:
                    self._assert_integration_wip_row(existing, expected)

    def _assert_integration_wip(
        self, conn: Any, batch: _Batch, inputs: Sequence[_Input]
    ) -> None:
        expected_ids = {
            self._integration_wip_id(batch.id, token.id)
            for item in inputs
            for token in item.wip_inputs
        }
        rows = conn.execute(
            "SELECT * FROM work_package_wip_tokens WHERE package_id = ? "
            "AND plan_version = ? AND epoch = ? AND stage = 'integration' "
            "AND state = 'held' AND reservation_key = ? ORDER BY id",
            (batch.package_id, batch.plan_version, batch.epoch, batch.id),
        ).fetchall()
        observed_ids = {str(row["id"]) for row in rows}
        if not expected_ids or observed_ids != expected_ids:
            raise TransitionError(
                "integration batch does not hold its exact bounded product WIP"
            )
        by_id = {str(row["id"]): row for row in rows}
        for item in inputs:
            for token in item.wip_inputs:
                predecessor = conn.execute(
                    "SELECT stage, state, release_reason FROM "
                    "work_package_wip_tokens WHERE id = ?",
                    (token.id,),
                ).fetchone()
                if (
                    predecessor is None
                    or predecessor["stage"] != "fan_in_reservation"
                    or predecessor["state"] != "released"
                    or predecessor["release_reason"]
                    != "integration_transfer:%s" % batch.id
                ):
                    raise TransitionError(
                        "integration WIP predecessor lacks the exact transfer receipt"
                    )
                successor_id = self._integration_wip_id(batch.id, token.id)
                expected = self._integration_wip_values(
                    batch=batch,
                    source=token,
                    successor_id=successor_id,
                    now=str(by_id[successor_id]["acquired_at"]),
                )
                self._assert_integration_wip_row(by_id[successor_id], expected)

    @staticmethod
    def _integration_wip_values(
        *,
        batch: _Batch,
        source: _WipInput,
        successor_id: str,
        now: str,
    ) -> Tuple[Any, ...]:
        return (
            successor_id,
            source.package_id,
            source.plan_version,
            source.epoch,
            source.node_key,
            source.task_id,
            source.resource_key,
            source.token_kind,
            source.generation + 1,
            source.capacity_units,
            batch.id,
            source.id,
            source.acquired_by_assignment_lease_id,
            now,
        )

    @staticmethod
    def _assert_integration_wip_row(
        row: Mapping[str, Any], expected: Sequence[Any]
    ) -> None:
        fields = (
            "id",
            "package_id",
            "plan_version",
            "epoch",
            "node_key",
            "task_id",
            "resource_key",
            "token_kind",
            "generation",
            "capacity_units",
            "reservation_key",
            "predecessor_token_id",
            "acquired_by_assignment_lease_id",
            "acquired_at",
        )
        observed = tuple(row[field] for field in fields)
        if (
            observed != tuple(expected)
            or row["stage"] != "integration"
            or row["state"] != "held"
        ):
            raise TransitionError(
                "integration WIP successor conflicts with exact transfer"
            )

    def _terminalize(
        self,
        lease: IntegrationLease,
        *,
        state: str,
        reason: str,
    ) -> None:
        if state not in {"rejected", "stale"}:
            raise ValueError("unsupported integration terminal state")
        reason_value = str(reason or "integration terminalized").strip()[:1000]
        now = self._iso_now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM work_package_integration_batches WHERE id = ?",
                (lease.batch_id,),
            ).fetchone()
            if row is None:
                raise IntegrationLeaseLostError("integration batch disappeared")
            batch = self._batch_from_row(row)
            if (
                batch.state != "assembling"
                or batch.lease_owner != lease.owner
                or batch.lease_fence != lease.fence
                or self._expired(batch.lease_expires_at, self._now_utc())
            ):
                raise IntegrationLeaseLostError(
                    "stale integration owner cannot terminalize batch"
                )
            rows = conn.execute(
                "SELECT * FROM work_package_wip_tokens WHERE package_id = ? "
                "AND plan_version = ? AND epoch = ? AND stage = 'integration' "
                "AND state = 'held' AND reservation_key = ? ORDER BY id",
                (batch.package_id, batch.plan_version, batch.epoch, batch.id),
            ).fetchall()
            if not rows:
                raise TransitionError(
                    "terminal integration batch has no bounded WIP to return"
                )
            returned_ids = []
            for row_wip in rows:
                release_reason = "integration_%s:%s" % (state, batch.id)
                released = conn.execute(
                    "UPDATE work_package_wip_tokens SET state = 'released', "
                    "released_at = ?, release_reason = ? "
                    "WHERE id = ? AND state = 'held' AND stage = 'integration'",
                    (now, release_reason, row_wip["id"]),
                )
                if released.rowcount != 1:
                    raise TransitionError(
                        "integration WIP changed during terminal return"
                    )
                returned_id = self._returned_wip_id(str(row_wip["id"]), state)
                conn.execute(
                    "INSERT INTO work_package_wip_tokens ("
                    "id, package_id, plan_version, epoch, node_key, task_id, "
                    "resource_key, token_kind, stage, state, generation, "
                    "capacity_units, reservation_key, predecessor_token_id, "
                    "acquired_by_assignment_lease_id, acquired_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fan_in_reservation', 'held', "
                    "?, ?, ?, ?, ?, ?)",
                    (
                        returned_id,
                        row_wip["package_id"],
                        int(row_wip["plan_version"]),
                        int(row_wip["epoch"]),
                        row_wip["node_key"],
                        row_wip["task_id"],
                        row_wip["resource_key"],
                        row_wip["token_kind"],
                        int(row_wip["generation"]) + 1,
                        int(row_wip["capacity_units"]),
                        "returned:%s" % batch.id,
                        row_wip["id"],
                        row_wip["acquired_by_assignment_lease_id"],
                        now,
                    ),
                )
                returned_ids.append(returned_id)
            updated = conn.execute(
                "UPDATE work_package_integration_batches SET state = ?, "
                "lease_owner = NULL, lease_expires_at = NULL, completed_at = ?, "
                "updated_at = ? WHERE id = ? AND state = 'assembling' "
                "AND lease_owner = ? AND lease_fence = ?",
                (state, now, now, batch.id, lease.owner, lease.fence),
            )
            if updated.rowcount != 1:
                raise IntegrationLeaseLostError(
                    "integration terminal transition CAS failed"
                )
            self._append_history(
                conn,
                package_id=batch.package_id,
                plan_version=batch.plan_version,
                epoch=batch.epoch,
                actor=lease.owner,
                event_type="work_package.integration_batch_%s" % state,
                detail={
                    "batch_id": batch.id,
                    "fence": lease.fence,
                    "reason": reason_value,
                    "returned_wip_token_ids": returned_ids,
                },
                now=now,
            )

    # -- Exact input loading ----------------------------------------------------

    def _accepted_inputs(
        self,
        conn: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        repository_id: str,
        members: Sequence[str],
        batch_id: Optional[str],
    ) -> Tuple[_Input, ...]:
        if not members or len(members) > int(self.config.max_inputs):
            raise ValidationError("integration group input count is outside policy")
        inputs = []
        for ordinal, node_key in enumerate(members):
            rows = conn.execute(
                "SELECT candidate.id AS candidate_id, candidate.node_generation, "
                "candidate.task_id, candidate.assignment_lease_id, "
                "candidate.attempt_number, candidate.evidence_id, "
                "verification.id AS verification_id, "
                "verification.receipt_digest, verification.attempt_ref, "
                "verification.attempt_base_sha, verification.attempt_head_sha, "
                "verification.tree_digest, verification.declared_effects_digest, "
                "verification.observed_effects_digest, verification.changed_paths, "
                "attempt.artifact_digest, attempt.attempt_head_sha AS attributed_head_sha, "
                "attempt.protected_ref "
                "FROM work_package_node_candidates AS candidate "
                "JOIN work_package_task_links AS link "
                "ON link.task_id = candidate.task_id "
                "AND link.package_id = candidate.package_id "
                "AND link.plan_version = candidate.plan_version "
                "AND link.epoch = candidate.epoch "
                "AND link.node_key = candidate.node_key "
                "AND link.node_generation = candidate.node_generation "
                "JOIN evidence_attempt_verifications AS verification "
                "ON verification.evidence_id = candidate.evidence_id "
                "AND verification.task_id = candidate.task_id "
                "AND verification.lease_id = candidate.assignment_lease_id "
                "AND verification.attempt_number = candidate.attempt_number "
                "JOIN evidence_attempt_links AS attempt "
                "ON attempt.evidence_id = candidate.evidence_id "
                "AND attempt.task_id = candidate.task_id "
                "AND attempt.lease_id = candidate.assignment_lease_id "
                "AND attempt.attempt_number = candidate.attempt_number "
                "WHERE candidate.package_id = ? AND candidate.plan_version = ? "
                "AND candidate.epoch = ? AND candidate.node_key = ? "
                "AND candidate.status = 'accepted' "
                "AND link.node_state = 'candidate_accepted' "
                "AND verification.repository_id = ?",
                (package_id, plan_version, epoch, node_key, repository_id),
            ).fetchall()
            if len(rows) != 1:
                raise TransitionError(
                    "integration member %s does not have exactly one accepted "
                    "controller-verified candidate" % node_key
                )
            inputs.append(
                self._input_from_row(
                    conn,
                    rows[0],
                    ordinal=ordinal,
                    node_key=node_key,
                    package_id=package_id,
                    plan_version=plan_version,
                    epoch=epoch,
                    batch_id=batch_id,
                )
            )
        return tuple(inputs)

    def _batch_inputs(self, source: Any, batch: _Batch) -> Tuple[_Input, ...]:
        execute = source.execute if hasattr(source, "execute") else None
        if execute is None:
            rows = source.query_all(
                "SELECT input.*, verification.id AS verification_id, "
                "verification.receipt_digest, verification.repository_id, "
                "verification.attempt_ref, verification.attempt_base_sha, "
                "verification.attempt_head_sha, verification.tree_digest, "
                "verification.declared_effects_digest, "
                "verification.observed_effects_digest, verification.changed_paths, "
                "attempt.artifact_digest, attempt.attempt_head_sha AS attributed_head_sha, "
                "attempt.protected_ref "
                "FROM work_package_batch_inputs AS input "
                "JOIN evidence_attempt_verifications AS verification "
                "ON verification.evidence_id = input.evidence_id "
                "AND verification.task_id = input.task_id "
                "AND verification.lease_id = input.assignment_lease_id "
                "AND verification.attempt_number = input.attempt_number "
                "JOIN evidence_attempt_links AS attempt "
                "ON attempt.evidence_id = input.evidence_id "
                "AND attempt.task_id = input.task_id "
                "AND attempt.lease_id = input.assignment_lease_id "
                "AND attempt.attempt_number = input.attempt_number "
                "WHERE input.batch_id = ? ORDER BY input.ordinal, input.id",
                (batch.id,),
            )
        else:
            rows = execute(
                "SELECT input.*, verification.id AS verification_id, "
                "verification.receipt_digest, verification.repository_id, "
                "verification.attempt_ref, verification.attempt_base_sha, "
                "verification.attempt_head_sha, verification.tree_digest, "
                "verification.declared_effects_digest, "
                "verification.observed_effects_digest, verification.changed_paths, "
                "attempt.artifact_digest, attempt.attempt_head_sha AS attributed_head_sha, "
                "attempt.protected_ref "
                "FROM work_package_batch_inputs AS input "
                "JOIN evidence_attempt_verifications AS verification "
                "ON verification.evidence_id = input.evidence_id "
                "AND verification.task_id = input.task_id "
                "AND verification.lease_id = input.assignment_lease_id "
                "AND verification.attempt_number = input.attempt_number "
                "JOIN evidence_attempt_links AS attempt "
                "ON attempt.evidence_id = input.evidence_id "
                "AND attempt.task_id = input.task_id "
                "AND attempt.lease_id = input.assignment_lease_id "
                "AND attempt.attempt_number = input.attempt_number "
                "WHERE input.batch_id = ? ORDER BY input.ordinal, input.id",
                (batch.id,),
            ).fetchall()
        if not rows or len(rows) > int(self.config.max_inputs):
            raise TransitionError("integration batch has no bounded exact membership")
        result = []
        for expected_ordinal, row in enumerate(rows):
            if int(row["ordinal"]) != expected_ordinal:
                raise TransitionError(
                    "integration batch ordinals are not contiguous and deterministic"
                )
            if str(row["repository_id"]) != batch.repository_id:
                raise TransitionError(
                    "integration input verification belongs to another repository"
                )
            result.append(
                self._input_from_row(
                    source,
                    row,
                    ordinal=expected_ordinal,
                    node_key=str(row["node_key"]),
                    package_id=batch.package_id,
                    plan_version=batch.plan_version,
                    epoch=batch.epoch,
                    batch_id=batch.id,
                )
            )
        return tuple(result)

    def _input_from_row(
        self,
        source: Any,
        row: Mapping[str, Any],
        *,
        ordinal: int,
        node_key: str,
        package_id: str,
        plan_version: int,
        epoch: int,
        batch_id: Optional[str],
    ) -> _Input:
        attempt_ref = self._protected_attempt_ref(str(row["attempt_ref"] or ""))
        base = self._sha(str(row["attempt_base_sha"] or ""), "attempt base")
        head = self._sha(str(row["attempt_head_sha"] or ""), "attempt head")
        attributed_head = self._sha(
            str(row["attributed_head_sha"] or ""), "attributed attempt head"
        )
        if attributed_head != head or int(row["protected_ref"] or 0) != 1:
            raise ValidationError(
                "integration verification is not bound to the exact protected output"
            )
        artifact_digest = str(row["artifact_digest"] or "").strip()
        if not _TREE_DIGEST.fullmatch(artifact_digest):
            raise ValidationError(
                "integration input has no immutable sha256 artifact digest"
            )
        tree_digest = str(row["tree_digest"] or "")
        if not _TREE_DIGEST.fullmatch(tree_digest):
            raise ValidationError(
                "integration input has an invalid controller tree digest"
            )
        changed_paths_raw = json_loads(row["changed_paths"], None)
        if not isinstance(changed_paths_raw, list) or not all(
            isinstance(path, str) and path and "\x00" not in path
            for path in changed_paths_raw
        ):
            raise ValidationError(
                "integration input has invalid controller-observed paths"
            )
        changed_paths = tuple(sorted(set(changed_paths_raw)))
        for label, value in (
            ("verification receipt", str(row["receipt_digest"] or "")),
            ("observed effects", str(row["observed_effects_digest"] or "")),
        ):
            if not _TREE_DIGEST.fullmatch(value):
                raise ValidationError(
                    "integration input has an invalid %s digest" % label
                )
        candidate_id = str(row["candidate_id"])
        assignment_lease_id = str(row["assignment_lease_id"])
        wip_inputs = self._input_wip(
            source,
            package_id=package_id,
            plan_version=plan_version,
            epoch=epoch,
            node_key=node_key,
            task_id=str(row["task_id"]),
            assignment_lease_id=assignment_lease_id,
            candidate_id=candidate_id,
            evidence_id=str(row["evidence_id"]),
            batch_id=batch_id,
        )
        return _Input(
            ordinal=ordinal,
            node_key=node_key,
            node_generation=int(row["node_generation"]),
            task_id=str(row["task_id"]),
            candidate_id=candidate_id,
            assignment_lease_id=assignment_lease_id,
            attempt_number=int(row["attempt_number"]),
            evidence_id=str(row["evidence_id"]),
            artifact_digest=artifact_digest,
            verification_id=str(row["verification_id"]),
            verification_receipt_digest=str(row["receipt_digest"]),
            attempt_ref=attempt_ref,
            attempt_base_sha=base,
            attempt_head_sha=head,
            tree_digest=tree_digest,
            declared_effects_digest=str(row["declared_effects_digest"]),
            observed_effects_digest=str(row["observed_effects_digest"]),
            changed_paths=changed_paths,
            wip_inputs=wip_inputs,
        )

    def _input_wip(
        self,
        source: Any,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        node_key: str,
        task_id: str,
        assignment_lease_id: str,
        candidate_id: str,
        evidence_id: str,
        batch_id: Optional[str],
    ) -> Tuple[_WipInput, ...]:
        query_all = self._query_all(source)
        rows = []
        if batch_id:
            rows = query_all(
                "SELECT fan_in.* FROM work_package_wip_tokens AS integration "
                "JOIN work_package_wip_tokens AS fan_in "
                "ON fan_in.id = integration.predecessor_token_id "
                "WHERE integration.package_id = ? AND integration.plan_version = ? "
                "AND integration.epoch = ? AND integration.node_key = ? "
                "AND integration.task_id = ? AND integration.stage = 'integration' "
                "AND integration.state = 'held' AND integration.reservation_key = ? "
                "AND integration.acquired_by_assignment_lease_id = ? "
                "ORDER BY fan_in.id",
                (
                    package_id,
                    plan_version,
                    epoch,
                    node_key,
                    task_id,
                    batch_id,
                    assignment_lease_id,
                ),
            )
        transferred = bool(rows)
        if not rows:
            rows = query_all(
                "SELECT token.* FROM work_package_wip_tokens AS token "
                "WHERE token.package_id = ? AND token.plan_version = ? "
                "AND token.epoch = ? AND token.node_key = ? AND token.task_id = ? "
                "AND token.stage = 'fan_in_reservation' AND token.state = 'held' "
                "AND token.acquired_by_assignment_lease_id = ? ORDER BY token.id",
                (
                    package_id,
                    plan_version,
                    epoch,
                    node_key,
                    task_id,
                    assignment_lease_id,
                ),
            )
        if not rows:
            raise TransitionError(
                "accepted integration candidate has no bounded fan-in reservation"
            )
        for row in rows:
            self._assert_accepted_fan_in_wip(
                source,
                row,
                candidate_id=candidate_id,
                evidence_id=evidence_id,
                integration_batch_id=batch_id if transferred else None,
            )
        values = tuple(self._wip_from_row(row) for row in rows)
        if len({item.resource_key for item in values}) != len(values):
            raise TransitionError("integration candidate has ambiguous product WIP")
        return values

    def _assert_accepted_fan_in_wip(
        self,
        source: Any,
        row: Mapping[str, Any],
        *,
        candidate_id: str,
        evidence_id: str,
        integration_batch_id: Optional[str],
    ) -> None:
        """Verify the immutable mutation -> candidate -> acceptance WIP chain."""

        query_one = self._query_one(source)
        if not candidate_id or not evidence_id:
            raise TransitionError("integration WIP has no exact candidate identity")
        expected_state = "released" if integration_batch_id else "held"
        expected_reason = (
            "integration_transfer:%s" % integration_batch_id
            if integration_batch_id
            else None
        )
        if (
            row["stage"] != "fan_in_reservation"
            or row["state"] != expected_state
            or (
                integration_batch_id and row["release_reason"] != expected_reason
            )
            or (
                not integration_batch_id and row["release_reason"] is not None
            )
        ):
            raise TransitionError("integration fan-in reservation changed lifecycle")
        candidate = query_one(
            "SELECT id FROM work_package_node_candidates WHERE id = ? "
            "AND package_id = ? AND plan_version = ? AND epoch = ? "
            "AND node_key = ? AND task_id = ? AND assignment_lease_id = ? "
            "AND evidence_id = ? AND status = 'accepted'",
            (
                candidate_id,
                row["package_id"],
                int(row["plan_version"]),
                int(row["epoch"]),
                row["node_key"],
                row["task_id"],
                row["acquired_by_assignment_lease_id"],
                evidence_id,
            ),
        )
        if candidate is None:
            raise TransitionError(
                "integration fan-in reservation lacks its accepted candidate"
            )
        buffer = query_one(
            "SELECT * FROM work_package_wip_tokens WHERE id = ?",
            (str(row["predecessor_token_id"] or ""),),
        )
        if buffer is None:
            raise TransitionError("integration fan-in predecessor disappeared")
        identity_fields = (
            "package_id",
            "plan_version",
            "epoch",
            "node_key",
            "task_id",
            "resource_key",
            "token_kind",
            "capacity_units",
            "reservation_key",
            "acquired_by_assignment_lease_id",
        )
        if (
            buffer["stage"] != "candidate_buffer"
            or buffer["state"] != "released"
            or not buffer["released_at"]
            or int(row["generation"]) != int(buffer["generation"]) + 1
            or any(row[field] != buffer[field] for field in identity_fields)
        ):
            raise TransitionError(
                "integration fan-in reservation has an incoherent candidate predecessor"
            )
        try:
            resolution = json_loads(buffer["release_reason"], None)
        except (TypeError, ValueError) as exc:
            raise TransitionError(
                "integration fan-in predecessor has malformed acceptance provenance"
            ) from exc
        required_resolution = {
            "schema": "mac.work_package.wip_resolution.v1",
            "decision": "accepted",
            "candidate_id": candidate_id,
            "evidence_id": evidence_id,
            "successor_token_id": str(row["id"]),
            "resolved_at": str(buffer["released_at"]),
        }
        expected_fan_in_id = "wpwip_%s" % hashlib.sha256(
            (
                str(buffer["id"])
                + "\0"
                + candidate_id
                + "\0fan_in_reservation"
            ).encode("utf-8")
        ).hexdigest()[:32]
        if (
            not isinstance(resolution, Mapping)
            or row["id"] != expected_fan_in_id
            or any(
                resolution.get(key) != value
                for key, value in required_resolution.items()
            )
            or not str(resolution.get("actor") or "").strip()
        ):
            raise TransitionError(
                "integration fan-in predecessor lacks exact acceptance provenance"
            )
        mutation = query_one(
            "SELECT * FROM work_package_wip_tokens WHERE id = ?",
            (str(buffer["predecessor_token_id"] or ""),),
        )
        if (
            mutation is None
            or mutation["stage"] != "mutation"
            or mutation["state"] != "released"
            or mutation["release_reason"] != "candidate_transfer:%s" % candidate_id
            or int(buffer["generation"]) != int(mutation["generation"]) + 1
            or any(buffer[field] != mutation[field] for field in identity_fields)
        ):
            raise TransitionError(
                "integration fan-in reservation lacks its exact mutation lineage"
            )

    # -- Plan/package/repository validation ------------------------------------

    def _package_context(self, package_id: str, *, conn: Any = None) -> JsonDict:
        query_one = self._query_one(conn or self.store)
        row = query_one(
            "SELECT package.id AS package_id, package.state AS package_state, "
            "package.current_plan_version AS plan_version, "
            "package.current_epoch AS epoch, package.repository_id, "
            "epoch.status AS epoch_status, epoch.planning_base_ref, "
            "epoch.planning_base_sha, plan.definition, "
            "repository.source AS repository_source, "
            "repository.metadata AS repository_metadata, "
            "repository.enabled AS repository_enabled "
            "FROM work_packages AS package "
            "JOIN work_package_epochs AS epoch "
            "ON epoch.package_id = package.id "
            "AND epoch.plan_version = package.current_plan_version "
            "AND epoch.epoch = package.current_epoch "
            "JOIN work_package_plan_versions AS plan "
            "ON plan.package_id = package.id "
            "AND plan.version = package.current_plan_version "
            "JOIN project_repositories AS repository "
            "ON repository.id = package.repository_id "
            "WHERE package.id = ?",
            (package_id,),
        )
        if row is None:
            raise ValidationError("active work package not found: %s" % package_id)
        result = dict(row)
        if (
            result["package_state"] != "active"
            or result["epoch_status"] != "active"
            or int(result["repository_enabled"]) != 1
        ):
            raise TransitionError(
                "integration requires an active current package, epoch, and repository"
            )
        definition = json_loads(result["definition"], None)
        if not isinstance(definition, dict):
            raise ValidationError("work-package plan definition is invalid")
        result["definition"] = definition
        return result

    def _integration_group(
        self, definition: Mapping[str, Any], node_key: str
    ) -> Tuple[str, Tuple[str, ...]]:
        nodes = definition.get("nodes")
        derived = definition.get("derived")
        integration = definition.get("integration")
        if not isinstance(nodes, list) or not isinstance(derived, dict):
            raise ValidationError("work-package plan lacks compiled integration data")
        groups = derived.get("integration_groups")
        if not isinstance(groups, list):
            raise ValidationError("work-package plan has no integration groups")
        matches = [
            group
            for group in groups
            if isinstance(group, dict)
            and str(group.get("integration_node_key") or "") == node_key
        ]
        if len(matches) != 1:
            raise ValidationError(
                "plan does not name exactly one integration group for %s" % node_key
            )
        node_by_key = {
            str(node.get("node_key") or ""): node
            for node in nodes
            if isinstance(node, dict)
        }
        integration_node = node_by_key.get(node_key)
        if not integration_node or integration_node.get("kind") != "integration":
            raise ValidationError("integration group does not name an integration node")
        raw_members = matches[0].get("member_node_keys")
        if not isinstance(raw_members, list) or not all(
            isinstance(member, str) and member for member in raw_members
        ):
            raise ValidationError("integration group has invalid member nodes")
        members = tuple(sorted(set(raw_members)))
        if len(members) != len(raw_members) or not members:
            raise ValidationError("integration group membership is ambiguous")
        for member in members:
            member_node = node_by_key.get(member)
            if member_node is None:
                raise ValidationError("integration group names an unknown member")
            if member_node.get("kind") != "mutation":
                raise ValidationError(
                    "nested integration inputs are release-blocked until exact "
                    "controller batch provenance is durable"
                )
        target_ref = ""
        if isinstance(integration, dict):
            target_ref = str(integration.get("target_ref") or "").strip()
        target_ref = target_ref or str(definition.get("planning_base_ref") or "")
        target_ref = validate_git_ref(target_ref)
        if not target_ref.startswith("refs/heads/"):
            raise ValidationError("integration target must be a full refs/heads ref")
        return target_ref, members

    def _integration_task(
        self, conn: Any, context: Mapping[str, Any], node_key: str
    ) -> str:
        rows = conn.execute(
            "SELECT link.task_id, link.node_state FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? "
            "AND link.epoch = ? AND link.node_key = ?",
            (
                context["package_id"],
                int(context["plan_version"]),
                int(context["epoch"]),
                node_key,
            ),
        ).fetchall()
        if len(rows) != 1 or rows[0]["node_state"] not in {"planned", "ready"}:
            raise TransitionError(
                "integration node does not have one unclaimed controller task"
            )
        return str(rows[0]["task_id"])

    def _lock_current_package(self, conn: Any, context: Mapping[str, Any]) -> None:
        locked = conn.execute(
            "UPDATE work_packages SET updated_at = updated_at WHERE id = ? "
            "AND state = 'active' AND current_plan_version = ? AND current_epoch = ?",
            (
                context["package_id"],
                int(context["plan_version"]),
                int(context["epoch"]),
            ),
        )
        if locked.rowcount != 1:
            raise TransitionError("work-package current epoch changed")

    def _lock_batch_context(self, conn: Any, batch: _Batch) -> None:
        context = self._package_context(batch.package_id, conn=conn)
        validate_supported_work_package_topology(context["definition"])
        if (
            int(context["plan_version"]) != batch.plan_version
            or int(context["epoch"]) != batch.epoch
            or str(context["repository_id"]) != batch.repository_id
        ):
            raise TransitionError(
                "integration batch no longer belongs to current epoch"
            )
        self._lock_current_package(conn, context)
        node_rows = conn.execute(
            "SELECT node_key FROM work_package_task_links WHERE task_id = ? "
            "AND package_id = ? AND plan_version = ? AND epoch = ?",
            (
                batch.integration_task_id,
                batch.package_id,
                batch.plan_version,
                batch.epoch,
            ),
        ).fetchall()
        if len(node_rows) != 1:
            raise TransitionError(
                "integration batch task no longer names one exact plan node"
            )
        node_key = str(node_rows[0]["node_key"])
        if node_key != self._integration_node_key(batch):
            raise TransitionError(
                "integration batch metadata conflicts with immutable task identity"
            )
        target_ref, members = self._integration_group(context["definition"], node_key)
        if target_ref != batch.target_ref:
            raise TransitionError("integration target changed after batch creation")
        input_rows = conn.execute(
            "SELECT node_key FROM work_package_batch_inputs WHERE batch_id = ? "
            "ORDER BY ordinal, id",
            (batch.id,),
        ).fetchall()
        if tuple(str(row["node_key"]) for row in input_rows) != members:
            raise TransitionError(
                "integration batch membership differs from plan fan-in"
            )
        if batch.assembly_base_sha != batch.landing_base_sha:
            raise ValidationError(
                "hierarchical assembly bases are release-blocked without exact provenance"
            )

    @staticmethod
    def _assert_same_package_context(
        preview: Mapping[str, Any], current: Mapping[str, Any]
    ) -> None:
        fields = (
            "package_id",
            "plan_version",
            "epoch",
            "repository_id",
            "planning_base_ref",
            "planning_base_sha",
            "repository_source",
        )
        if any(preview[field] != current[field] for field in fields):
            raise TransitionError(
                "work-package context changed during base observation"
            )

    def _repository_from_context(self, context: Mapping[str, Any]) -> _Repository:
        return self._repository_value(
            str(context["repository_id"]),
            str(context["repository_source"] or ""),
            context["repository_metadata"],
        )

    def _repository(self, repository_id: str) -> _Repository:
        row = self.store.query_one(
            "SELECT id, source, metadata, enabled FROM project_repositories "
            "WHERE id = ?",
            (repository_id,),
        )
        if row is None or int(row["enabled"]) != 1:
            raise TransitionError("registered integration repository is unavailable")
        return self._repository_value(
            str(row["id"]), str(row["source"] or ""), row["metadata"]
        )

    @staticmethod
    def _repository_value(
        repository_id: str, source: str, metadata_raw: Any
    ) -> _Repository:
        metadata = json_loads(metadata_raw, {}) or {}
        if not isinstance(metadata, dict):
            raise ValidationError("registered repository metadata is invalid")
        try:
            canonical = resolve_repository_canonical_remote(
                {
                    "id": repository_id,
                    "source": source,
                    "metadata": metadata,
                }
            )
        except ValueError as exc:
            raise ValidationError(
                "registered repository canonical remote is invalid"
            ) from exc
        return _Repository(repository_id, canonical.url, metadata)

    # -- Git -------------------------------------------------------------------

    def _fetch_exact(
        self,
        repository: _Repository,
        checkout: Path,
        root: Path,
        *,
        ref: str,
        expected_sha: str,
        local_ref: str,
        base_ref: bool = False,
    ) -> None:
        ref_value = validate_git_ref(ref)
        local_value = validate_git_ref(local_ref)
        expected = self._sha(expected_sha, "expected fetch")
        advertised = self._remote_ref(
            repository, ref_value, operation="read", root=root
        )
        if advertised != expected:
            if base_ref:
                raise IntegrationBaseMovedError(
                    "canonical target moved during integration assembly"
                )
            raise TransitionError(
                "protected remote ref no longer names exact input SHA"
            )
        self._git(
            repository,
            [
                "fetch",
                "--no-tags",
                "--force",
                repository.source,
                "+%s:%s" % (ref_value, local_value),
            ],
            cwd=checkout,
            root=root,
            operation="read",
            label="fetch exact integration input",
        )
        fetched = self._rev_parse(
            repository, checkout, root, local_value, "fetched integration input"
        )
        if fetched != expected:
            raise TransitionError("fetched integration input differs from exact SHA")

    def _verify_input_observation(
        self,
        repository: _Repository,
        checkout: Path,
        root: Path,
        item: _Input,
    ) -> None:
        exists = self._git_raw(
            repository,
            ["cat-file", "-e", "%s^{commit}" % item.attempt_base_sha],
            cwd=checkout,
            root=root,
            operation="read",
        )
        if exists.returncode != 0:
            raise TransitionError(
                "exact attempt base is unavailable during integration"
            )
        ancestry = self._git_raw(
            repository,
            [
                "merge-base",
                "--is-ancestor",
                item.attempt_base_sha,
                item.attempt_head_sha,
            ],
            cwd=checkout,
            root=root,
            operation="read",
        )
        if ancestry.returncode != 0:
            raise TransitionError(
                "exact integration input does not preserve attempt base"
            )
        listing = self._git(
            repository,
            ["ls-tree", "-r", "-z", "--full-tree", item.attempt_head_sha],
            cwd=checkout,
            root=root,
            operation="read",
            label="observe exact input tree",
        ).stdout
        tree_digest = "sha256:%s" % hashlib.sha256(listing).hexdigest()
        if tree_digest != item.tree_digest:
            raise TransitionError(
                "exact integration input tree differs from controller verification"
            )
        diff = self._git(
            repository,
            [
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                "--find-copies",
                item.attempt_base_sha,
                item.attempt_head_sha,
                "--",
            ],
            cwd=checkout,
            root=root,
            operation="read",
            label="observe exact input paths",
        ).stdout
        observed_paths = self._changed_paths(diff)
        if observed_paths != item.changed_paths:
            raise TransitionError(
                "exact integration input paths differ from controller verification"
            )

    def _reject_ancestry_ambiguity(
        self,
        repository: _Repository,
        checkout: Path,
        root: Path,
        input_sha: str,
        *,
        first: bool,
    ) -> None:
        input_already_present = self._git_raw(
            repository,
            ["merge-base", "--is-ancestor", input_sha, "HEAD"],
            cwd=checkout,
            root=root,
            operation="read",
        )
        aggregate_inside_input = self._git_raw(
            repository,
            ["merge-base", "--is-ancestor", "HEAD", input_sha],
            cwd=checkout,
            root=root,
            operation="read",
        )
        if input_already_present.returncode == 0 or (
            not first and aggregate_inside_input.returncode == 0
        ):
            raise IntegrationConflictError(
                "integration inputs have ambiguous or redundant ancestry"
            )

    def _remote_ref(
        self,
        repository: _Repository,
        ref: str,
        *,
        operation: str,
        missing_ok: bool = False,
        root: Optional[Path] = None,
    ) -> str:
        ref_value = validate_git_ref(ref)
        if root is None:
            with tempfile.TemporaryDirectory(prefix="mac-integration-remote-") as raw:
                return self._remote_ref(
                    repository,
                    ref_value,
                    operation=operation,
                    missing_ok=missing_ok,
                    root=Path(raw),
                )
        result = self._git_raw(
            repository,
            ["ls-remote", "--exit-code", repository.source, ref_value],
            cwd=None,
            root=root,
            operation=operation,
        )
        if result.returncode == 2 and missing_ok:
            return ""
        if result.returncode != 0:
            raise WorkPackageIntegrationError(
                self._git_error("read registered repository ref", result)
            )
        lines = [line for line in result.stdout.splitlines() if line]
        if len(lines) != 1:
            raise WorkPackageIntegrationError(
                "registered repository ref did not resolve to one exact object"
            )
        fields = lines[0].split(b"\t", 1)
        if len(fields) != 2 or fields[1].decode("utf-8", "replace") != ref_value:
            raise WorkPackageIntegrationError(
                "registered repository returned ambiguous ref identity"
            )
        return self._sha(fields[0].decode("ascii", "strict"), "remote ref")

    def _git(
        self,
        repository: _Repository,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        root: Path,
        operation: str,
        label: str,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess[bytes]:
        result = self._git_raw(
            repository,
            args,
            cwd=cwd,
            root=root,
            operation=operation,
            extra_env=extra_env,
        )
        if result.returncode != 0:
            raise WorkPackageIntegrationError(self._git_error(label, result))
        return result

    def _git_raw(
        self,
        repository: _Repository,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        root: Path,
        operation: str,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess[bytes]:
        env = self._git_environment(repository, operation, root)
        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(self.config.git_timeout_seconds),
            check=False,
        )

    def _git_environment(
        self, repository: _Repository, operation: str, root: Path
    ) -> dict[str, str]:
        home = root / "home"
        home.mkdir(exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        credentials = self.credential_environment(
            operation,
            {
                "id": repository.id,
                "source": repository.source,
                "metadata": repository.metadata,
            },
        )
        for key, value in credentials.items():
            key_value = str(key)
            value_value = str(value)
            if not key_value or "\x00" in key_value or "\x00" in value_value:
                raise ValidationError("integration credential environment is invalid")
            if key_value in env or key_value.startswith("GIT_CONFIG_"):
                raise ValidationError(
                    "integration credentials may not override Git safety environment"
                )
            env[key_value] = value_value
        return env

    @staticmethod
    def _git_error(label: str, result: subprocess.CompletedProcess[bytes]) -> str:
        raw = result.stderr or result.stdout or b""
        detail = redact_repository_hygiene_text(raw.decode("utf-8", "replace")).strip()
        return "%s failed%s" % (
            label,
            ": %s" % detail[:500] if detail else "",
        )

    def _rev_parse(
        self,
        repository: _Repository,
        checkout: Path,
        root: Path,
        value: str,
        label: str,
    ) -> str:
        result = self._git(
            repository,
            ["rev-parse", "--verify", "%s^{commit}" % value],
            cwd=checkout,
            root=root,
            operation="read",
            label="resolve %s" % label,
        )
        return self._sha(result.stdout.decode("ascii", "strict").strip(), label)

    def _rev_parse_object(
        self,
        repository: _Repository,
        checkout: Path,
        root: Path,
        value: str,
        label: str,
    ) -> str:
        result = self._git(
            repository,
            ["rev-parse", "--verify", value],
            cwd=checkout,
            root=root,
            operation="read",
            label="resolve %s" % label,
        )
        return self._sha(result.stdout.decode("ascii", "strict").strip(), label)

    # -- Recovery and assertions -----------------------------------------------

    def _recover_verifying(
        self, batch: _Batch, repository: _Repository
    ) -> IntegrationAssemblyOutcome:
        if not (
            batch.candidate_sha
            and batch.candidate_tree_digest
            and batch.candidate_ref
            and batch.candidate_fence
        ):
            raise TransitionError("verifying integration batch lacks exact candidate")
        observed = self._remote_ref(repository, batch.candidate_ref, operation="read")
        if observed != batch.candidate_sha:
            raise WorkPackageIntegrationError(
                "protected integration ref no longer names finalized candidate"
            )
        inputs = self._batch_inputs(self.store, batch)
        if compute_integration_input_digest(inputs) != batch.input_digest:
            raise TransitionError(
                "finalized integration membership no longer matches input digest"
            )
        return IntegrationAssemblyOutcome(
            status="assembled",
            batch_id=batch.id,
            candidate_sha=batch.candidate_sha,
            candidate_tree_digest=batch.candidate_tree_digest,
            candidate_ref=batch.candidate_ref,
            input_digest=batch.input_digest,
            fence=batch.candidate_fence,
            recovered=True,
        )

    def _assert_lease(self, lease: IntegrationLease) -> None:
        row = self.store.query_one(
            "SELECT state, lease_owner, lease_expires_at, lease_fence "
            "FROM work_package_integration_batches WHERE id = ?",
            (lease.batch_id,),
        )
        if (
            row is None
            or row["state"] != "assembling"
            or row["lease_owner"] != lease.owner
            or int(row["lease_fence"]) != lease.fence
            or self._expired(row["lease_expires_at"], self._now_utc())
        ):
            raise IntegrationLeaseLostError("integration owner fence is no longer held")

    # -- Pure helpers -----------------------------------------------------------

    def _batch(self, batch_id: str) -> _Batch:
        row = self.store.query_one(
            "SELECT * FROM work_package_integration_batches WHERE id = ?",
            (str(batch_id or "").strip(),),
        )
        if row is None:
            raise ValidationError(
                "work-package integration batch not found: %s" % batch_id
            )
        return self._batch_from_row(row)

    @staticmethod
    def _batch_from_row(row: Mapping[str, Any]) -> _Batch:
        repository_id = str(row["repository_id"] or "")
        integration_task_id = str(row["integration_task_id"] or "")
        if not repository_id or not integration_task_id:
            raise ValidationError(
                "integration batch lacks repository or integration-task identity"
            )
        return _Batch(
            id=str(row["id"]),
            package_id=str(row["package_id"]),
            plan_version=int(row["plan_version"]),
            epoch=int(row["epoch"]),
            repository_id=repository_id,
            target_ref=str(row["target_ref"]),
            assembly_base_sha=str(row["assembly_base_sha"]),
            landing_base_sha=str(row["landing_base_sha"]),
            input_digest=str(row["input_digest"]),
            candidate_sha=row["candidate_sha"],
            candidate_tree_digest=row["candidate_tree_digest"],
            candidate_ref=row["candidate_ref"],
            candidate_fence=(
                int(row["candidate_fence"])
                if row["candidate_fence"] is not None
                else None
            ),
            state=str(row["state"]),
            integration_task_id=integration_task_id,
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            lease_fence=int(row["lease_fence"]),
            metadata=json_loads(row["metadata"], {}) or {},
            created_at=str(row["created_at"]),
        )

    def _assert_batch_identity(
        self,
        batch: _Batch,
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        integration_task_id: str,
        target_ref: str,
        landing_base_sha: str,
        input_digest: str,
        integration_node_key: str,
    ) -> None:
        expected = (
            package_id,
            plan_version,
            epoch,
            integration_task_id,
            target_ref,
            landing_base_sha,
            landing_base_sha,
            input_digest,
            integration_node_key,
        )
        observed = (
            batch.package_id,
            batch.plan_version,
            batch.epoch,
            batch.integration_task_id,
            batch.target_ref,
            batch.assembly_base_sha,
            batch.landing_base_sha,
            batch.input_digest,
            self._integration_node_key(batch),
        )
        if observed != expected:
            raise TransitionError("deterministic integration batch identity conflicts")

    @staticmethod
    def _integration_node_key(batch: _Batch) -> str:
        value = str(batch.metadata.get("integration_node_key") or "").strip()
        if not value:
            raise ValidationError("integration batch metadata lacks node identity")
        return value

    @staticmethod
    def _batch_id(
        *,
        package_id: str,
        plan_version: int,
        epoch: int,
        integration_node_key: str,
        target_ref: str,
        landing_base_sha: str,
        input_digest: str,
    ) -> str:
        identity = json_dumps(
            {
                "schema": INTEGRATION_BATCH_SCHEMA,
                "package_id": package_id,
                "plan_version": plan_version,
                "epoch": epoch,
                "integration_node_key": integration_node_key,
                "target_ref": target_ref,
                "landing_base_sha": landing_base_sha,
                "input_digest": input_digest,
            }
        )
        return "wpbatch_%s" % hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _batch_input_id(batch_id: str, item: _Input) -> str:
        identity = "%s\x00%d\x00%s\x00%s" % (
            batch_id,
            item.ordinal,
            item.candidate_id,
            item.evidence_id,
        )
        return "wpinput_%s" % hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _integration_wip_id(batch_id: str, predecessor_id: str) -> str:
        return (
            "wpwip_%s"
            % hashlib.sha256(
                (predecessor_id + "\x00integration\x00" + batch_id).encode("utf-8")
            ).hexdigest()[:32]
        )

    @staticmethod
    def _returned_wip_id(integration_token_id: str, state: str) -> str:
        return (
            "wpwip_%s"
            % hashlib.sha256(
                (integration_token_id + "\x00return\x00" + state).encode("utf-8")
            ).hexdigest()[:32]
        )

    def _candidate_ref(self, batch: _Batch) -> str:
        package = _SAFE_COMPONENT.sub("-", batch.package_id).strip("-.") or "package"
        ref = "%s/%s/%s" % (self.config.candidate_namespace, package, batch.id)
        ref = validate_git_ref(ref)
        if not ref.startswith("refs/mac/integration/"):
            raise ValidationError(
                "integration candidate ref escaped protected namespace"
            )
        return ref

    @staticmethod
    def _protected_attempt_ref(value: str) -> str:
        ref = validate_git_ref(value)
        if not ref.startswith("refs/mac/attempts/"):
            raise ValidationError(
                "integration input is not an immutable protected attempt ref"
            )
        return ref

    @staticmethod
    def _sha(value: str, label: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _SHA40.fullmatch(normalized):
            raise ValidationError("%s is not an exact lowercase Git SHA" % label)
        return normalized

    @staticmethod
    def _changed_paths(data: bytes) -> Tuple[str, ...]:
        fields = data.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        index = 0
        paths: set[str] = set()
        while index < len(fields):
            status = fields[index].decode("ascii", "strict")
            index += 1
            if not status:
                raise TransitionError("Git returned an empty input change status")
            count = 2 if status[0] in {"R", "C"} else 1
            if index + count > len(fields):
                raise TransitionError("Git returned a truncated input path record")
            for _ in range(count):
                path = fields[index].decode("utf-8", "surrogateescape")
                index += 1
                if not path or "\x00" in path:
                    raise TransitionError("Git returned an invalid input path")
                paths.add(path)
        return tuple(sorted(paths))

    def _commit_environment(self, batch: _Batch) -> Mapping[str, str]:
        timestamp = self._parse_time(batch.created_at)
        if timestamp is None:
            # The batch id and membership are still deterministic, but a
            # malformed durable timestamp must not produce a non-repeatable
            # commit using the local clock.
            raise ValidationError("integration batch has an invalid creation timestamp")
        value = self._iso(timestamp)
        return {
            "GIT_AUTHOR_NAME": "MAC Integration Controller",
            "GIT_AUTHOR_EMAIL": "integration@mac.invalid",
            "GIT_COMMITTER_NAME": "MAC Integration Controller",
            "GIT_COMMITTER_EMAIL": "integration@mac.invalid",
            "GIT_AUTHOR_DATE": value,
            "GIT_COMMITTER_DATE": value,
        }

    def _creation_result(
        self,
        batch: _Batch,
        *,
        node_value: str,
        input_ids: Tuple[str, ...],
        created: bool,
    ) -> IntegrationBatchCreation:
        return IntegrationBatchCreation(
            batch_id=batch.id,
            package_id=batch.package_id,
            plan_version=batch.plan_version,
            epoch=batch.epoch,
            integration_node_key=node_value,
            landing_base_sha=batch.landing_base_sha,
            input_digest=batch.input_digest,
            input_ids=input_ids,
            created=created,
        )

    @staticmethod
    def _wip_from_row(row: Mapping[str, Any]) -> _WipInput:
        return _WipInput(
            id=str(row["id"]),
            package_id=str(row["package_id"]),
            plan_version=int(row["plan_version"]),
            epoch=int(row["epoch"]),
            node_key=str(row["node_key"]),
            task_id=str(row["task_id"]),
            resource_key=str(row["resource_key"]),
            token_kind=str(row["token_kind"]),
            generation=int(row["generation"]),
            capacity_units=int(row["capacity_units"]),
            reservation_key=row["reservation_key"],
            acquired_by_assignment_lease_id=str(row["acquired_by_assignment_lease_id"]),
            acquired_at=str(row["acquired_at"]),
        )

    @staticmethod
    def _query_one(source: Any) -> Callable[..., Any]:
        if hasattr(source, "query_one"):
            return source.query_one

        def query(sql: str, params: Sequence[Any] = ()) -> Any:
            return source.execute(sql, params).fetchone()

        return query

    @staticmethod
    def _query_all(source: Any) -> Callable[..., list[Any]]:
        if hasattr(source, "query_all"):
            return source.query_all

        def query(sql: str, params: Sequence[Any] = ()) -> list[Any]:
            return source.execute(sql, params).fetchall()

        return query

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
        sequence = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS value "
            "FROM work_package_history WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO work_package_history ("
            "id, package_id, seq, event_type, actor, plan_version, epoch, "
            "detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("wph"),
                package_id,
                int(sequence["value"]),
                event_type,
                actor,
                plan_version,
                epoch,
                json_dumps(dict(detail)),
                now,
            ),
        )

    def _append_history_once_per_fence(
        self,
        conn: Any,
        *,
        batch: _Batch,
        fence: int,
        actor: str,
        event_type: str,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        rows = conn.execute(
            "SELECT detail FROM work_package_history WHERE package_id = ? "
            "AND event_type = ? ORDER BY seq DESC LIMIT 20",
            (batch.package_id, event_type),
        ).fetchall()
        for row in rows:
            value = json_loads(row["detail"], {}) or {}
            if (
                value.get("batch_id") == batch.id
                and int(value.get("fence", -1)) == fence
            ):
                return
        self._append_history(
            conn,
            package_id=batch.package_id,
            plan_version=batch.plan_version,
            epoch=batch.epoch,
            actor=actor,
            event_type=event_type,
            detail=detail,
            now=now,
        )

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _iso_now(self) -> str:
        return self._iso(self._now_utc())

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _expired(cls, value: Any, now: datetime) -> bool:
        parsed = cls._parse_time(value)
        return parsed is None or parsed <= now.astimezone(timezone.utc)


__all__ = [
    "INTEGRATION_BATCH_SCHEMA",
    "INTEGRATION_INPUT_DIGEST_SCHEMA",
    "IntegrationAssemblyOutcome",
    "IntegrationBaseMovedError",
    "IntegrationBatchCreation",
    "IntegrationBusyError",
    "IntegrationConflictError",
    "IntegrationLease",
    "IntegrationLeaseLostError",
    "WORK_PACKAGE_INTEGRATION_SERVICE_VERSION",
    "WorkPackageIntegrationConfig",
    "WorkPackageIntegrationError",
    "WorkPackageIntegrationService",
    "compute_integration_input_digest",
]
