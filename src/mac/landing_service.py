"""Fenced, exact-candidate assembly and canonical landing.

The landing service is deliberately a small, disabled-by-default control-plane
component.  It consumes the authoritative work-package integration-batch and
certification records; it does not invent a second queue or accept mutable task
branches as publication inputs.

Durability is split across existing records:

* ``work_package_integration_batches`` is the immutable request (base, target,
  ordered input digest) and the exact assembled candidate identity.
* ``work_package_certifications`` is the exact-candidate certification.
* ``work_package_landing_streams`` provides one fenced lease stream per
  repository and target ref.
* append-only ``work_package_landing_intents``, ``..._attempts``, and
  ``..._receipts`` provide the write-ahead publication protocol.  Batch
  metadata is never publication authority.

The remote compare-and-swap remains the final safety fence.  A process may die
after the push but before recording its receipt; the next owner first reads the
remote and recovers that ambiguous outcome without pushing a second time.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from mac.gitops import validate_git_ref
from mac.models import (
    TransitionError,
    ValidationError,
    WorkPackageCertification,
    WorkPackageIntegrationBatch,
    WorkPackageLandingAttempt,
    WorkPackageLandingIntent,
    WorkPackageLandingReceipt,
    json_dumps,
    json_loads,
)
from mac.repository_hygiene import redact_repository_hygiene_text
from mac.repository_contract import (
    canonical_git_remote_identity,
    resolve_repository_canonical_remote,
    validate_secret_free_git_remote,
)
from mac.store import Store
from mac.work_package_models import validate_supported_work_package_topology


LANDING_RECEIPT_SCHEMA = "mac.landing_receipt.v1"
CERTIFICATION_ISOLATION_SCHEMA = "mac.certification_isolation.v1"
_SHA_LENGTH = 40


class LandingError(RuntimeError):
    """Base error for fail-closed landing operations."""


class LandingDisabledError(LandingError):
    """Raised when a caller tries to run the default-disabled service."""


class LandingBusyError(LandingError):
    """Raised when another owner holds the repository or batch lease."""


class LandingLeaseLostError(LandingError):
    """Raised before a side effect when a monotonic fence is no longer held."""


class GitRunner(Protocol):
    """Injectable, argv-only git runner used by production and real-repo tests."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]: ...


CredentialEnvironment = Callable[[str, "RepositoryEndpoint"], Mapping[str, str]]
FaultHook = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class LandingServiceConfig:
    enabled: bool = False
    lease_seconds: int = 120
    git_timeout_seconds: int = 300
    candidate_namespace: str = "refs/mac/candidates"

    def __post_init__(self) -> None:
        if self.lease_seconds < 5:
            raise ValueError("landing lease_seconds must be at least 5")
        if self.git_timeout_seconds < 1:
            raise ValueError("landing git_timeout_seconds must be positive")
        validate_git_ref(self.candidate_namespace)
        if not self.candidate_namespace.startswith("refs/mac/"):
            raise ValueError("candidate namespace must be protected under refs/mac/")


@dataclass(frozen=True)
class RepositoryEndpoint:
    """Secret-free canonical repository endpoint."""

    repository_id: str
    remote_url: str = field(repr=False)
    display_name: str = "canonical"

    def __post_init__(self) -> None:
        if not self.repository_id.strip():
            raise ValueError("repository_id is required")
        validate_secret_free_git_remote(self.remote_url)


@dataclass(frozen=True)
class AssemblyInput:
    ordinal: int
    task_id: str
    evidence_id: str
    protected_ref: str
    reviewed_sha: str


def compute_landing_input_digest(inputs: Sequence[AssemblyInput]) -> str:
    """Canonical digest used when an integration batch freezes membership."""

    payload = [
        {
            "ordinal": item.ordinal,
            "task_id": item.task_id,
            "evidence_id": item.evidence_id,
            "protected_ref": item.protected_ref,
            "reviewed_sha": item.reviewed_sha,
        }
        for item in inputs
    ]
    return "sha256:%s" % hashlib.sha256(
        json_dumps(payload).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class LandingOutcome:
    status: str
    batch_id: str
    candidate_sha: str = ""
    remote_sha: str = ""
    stream_fence: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "batch_id": self.batch_id,
            "candidate_sha": self.candidate_sha,
            "remote_sha": self.remote_sha,
            "stream_fence": self.stream_fence,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class _StreamLease:
    name: str
    owner: str
    fence: int
    batch_id: str
    repository_id: str
    target_ref: str


@dataclass(frozen=True)
class _BatchLease:
    owner: str
    fence: int
    batch_id: str


class SubprocessGitRunner:
    def __init__(self, timeout_seconds: int = 300) -> None:
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds,
            check=False,
        )


class LandingService:
    """Assemble, accept external certification, and land one exact candidate."""

    def __init__(
        self,
        store: Store,
        *,
        owner: str,
        config: Optional[LandingServiceConfig] = None,
        git_runner: Optional[GitRunner] = None,
        credential_environment: Optional[CredentialEnvironment] = None,
        now: Optional[Callable[[], datetime]] = None,
        fault_hook: Optional[FaultHook] = None,
    ) -> None:
        if not str(owner or "").strip():
            raise ValueError("landing service owner is required")
        self.store = store
        self.owner = owner.strip()
        self.config = config or LandingServiceConfig()
        self.git_runner = git_runner or SubprocessGitRunner(
            self.config.git_timeout_seconds
        )
        self.credential_environment = credential_environment or (
            lambda _operation, _endpoint: {}
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._fault_hook = fault_hook or (lambda _stage, _detail: None)

    def process_once(
        self,
        batch_id: str,
        endpoint: RepositoryEndpoint,
    ) -> LandingOutcome:
        """Advance a batch by one durable station; never loops autonomously."""

        self._require_enabled()
        batch = self._batch(batch_id)
        if batch.state in {"queued", "assembling"}:
            return self.assemble(batch_id, endpoint)
        if batch.state == "verifying":
            return LandingOutcome("awaiting_external_certification", batch_id)
        if batch.state == "certified":
            return self.land(batch_id, endpoint)
        if batch.state == "published":
            return self._published_outcome(batch, endpoint, retire_candidate=True)
        return LandingOutcome(batch.state, batch.id, batch.candidate_sha or "")

    def assemble(
        self, batch_id: str, endpoint: RepositoryEndpoint
    ) -> LandingOutcome:
        """Build a disposable candidate from immutable, reviewed input SHAs."""

        self._require_enabled()
        initial = self._batch(batch_id)
        self._validate_endpoint(initial, endpoint)
        stream = self._acquire_stream(initial)
        if stream is None:
            raise LandingBusyError("repository landing stream is leased")
        batch_lease: Optional[_BatchLease] = None
        completed = False
        try:
            batch_lease = self._acquire_batch(batch_id, start_assembly=True)
            if batch_lease is None:
                raise LandingBusyError("integration batch is leased")
            batch = self._batch(batch_id)
            if batch.state == "verifying":
                completed = True
                return LandingOutcome(
                    "assembled",
                    batch.id,
                    batch.candidate_sha or "",
                    stream_fence=stream.fence,
                )
            if batch.state != "assembling":
                return LandingOutcome(batch.state, batch.id, batch.candidate_sha or "")
            canonical_tip = self._ls_remote(endpoint, batch.target_ref, "read")
            if canonical_tip != batch.landing_base_sha:
                self._mark_stale(
                    batch,
                    observed_sha=canonical_tip,
                    reason="canonical target moved before assembly",
                )
                completed = True
                return LandingOutcome(
                    "stale",
                    batch.id,
                    remote_sha=canonical_tip,
                    stream_fence=stream.fence,
                )

            if batch.candidate_sha:
                staged_sha = self._ls_remote(
                    endpoint,
                    batch.candidate_ref or "",
                    "write",
                    missing_ok=True,
                )
                if staged_sha and staged_sha != batch.candidate_sha:
                    raise LandingError(
                        "protected candidate ref changed during assembly recovery"
                    )
                if staged_sha == batch.candidate_sha:
                    if not self._is_ancestor(
                        endpoint, batch.landing_base_sha, batch.candidate_sha
                    ):
                        raise LandingError(
                            "assigned candidate does not preserve landing base"
                        )
                    self._transition_to_verifying(batch.id, batch_lease)
                    completed = True
                    return LandingOutcome(
                        "assembled",
                        batch.id,
                        batch.candidate_sha,
                        stream_fence=stream.fence,
                        detail={
                            "candidate_ref": batch.candidate_ref,
                            "recovered": True,
                        },
                    )

            inputs = self._assembly_inputs(batch)
            if not inputs:
                raise LandingError("integration batch has no reviewed inputs")
            with tempfile.TemporaryDirectory(prefix="mac-landing-assemble-") as raw:
                checkout = Path(raw) / "repo"
                self._clone(endpoint, checkout)
                self._fetch_exact(endpoint, checkout, batch.target_ref, batch.landing_base_sha)
                self._git_checked(
                    ["checkout", "--detach", batch.assembly_base_sha],
                    cwd=checkout,
                    endpoint=endpoint,
                    operation="read",
                    label="checkout assembly base",
                )
                commit_env = self._deterministic_commit_environment(batch)
                for item in inputs:
                    self._fetch_exact(
                        endpoint, checkout, item.protected_ref, item.reviewed_sha
                    )
                    self._git_checked(
                        [
                            "-c",
                            "user.name=MAC Landing Service",
                            "-c",
                            "user.email=landing@mac.invalid",
                            "merge",
                            "--no-ff",
                            "--no-edit",
                            "-m",
                            "MAC batch %s input %d" % (batch.id, item.ordinal),
                            item.reviewed_sha,
                        ],
                        cwd=checkout,
                        endpoint=endpoint,
                        operation="read",
                        label="merge reviewed input",
                        extra_env=commit_env,
                    )
                candidate_sha = self._rev_parse(checkout, "HEAD", endpoint)
                tree_sha = self._rev_parse(checkout, "HEAD^{tree}", endpoint)
                if not self._is_ancestor_in_checkout(
                    checkout,
                    batch.landing_base_sha,
                    candidate_sha,
                    endpoint,
                ):
                    raise LandingError("assembled candidate does not preserve landing base")
                tree_digest = "git-tree:%s" % tree_sha
                if batch.candidate_sha:
                    if (
                        candidate_sha != batch.candidate_sha
                        or tree_digest != batch.candidate_tree_digest
                    ):
                        raise LandingError(
                            "reassembled candidate differs from immutable assignment"
                        )
                    candidate_ref = batch.candidate_ref or ""
                else:
                    candidate_ref = "%s/%s/%d" % (
                        self.config.candidate_namespace,
                        _safe_component(batch.id),
                        batch_lease.fence,
                    )
                    self._assign_candidate(
                        batch,
                        batch_lease,
                        candidate_sha=candidate_sha,
                        tree_digest=tree_digest,
                        candidate_ref=candidate_ref,
                    )
                    self._fault(
                        "after_candidate_assignment", self._batch(batch.id), stream
                    )
                self._assert_leases(stream, batch_lease)
                self._stage_candidate(
                    endpoint,
                    checkout,
                    candidate_ref=candidate_ref,
                    candidate_sha=candidate_sha,
                )
                self._fault("after_candidate_stage", self._batch(batch.id), stream)
            self._transition_to_verifying(batch.id, batch_lease)
            completed = True
            return LandingOutcome(
                "assembled",
                batch.id,
                candidate_sha,
                stream_fence=stream.fence,
                detail={
                    "candidate_ref": candidate_ref,
                    "recovered": bool(batch.candidate_sha),
                },
            )
        finally:
            if completed:
                if batch_lease is not None:
                    self._release_batch(batch_lease)
                self._release_stream(stream)

    def certify(
        self,
        batch_id: str,
        endpoint: RepositoryEndpoint,
    ) -> LandingOutcome:
        """Refuse the old in-process certification path.

        A Python callback in the landing process can read ambient credentials
        and use the network regardless of the environment mapping passed to it.
        Certification must therefore run in an externally enforced sandbox and
        append its durable WorkPackageCertification record.  The controller
        then calls :meth:`accept_certification`; callers cannot select policy.
        """

        self._require_enabled()
        batch = self._batch(batch_id)
        self._validate_endpoint(batch, endpoint)
        raise LandingError(
            "in-process certification is release-blocked; run the repository policy "
            "in an external network-disabled sandbox and call accept_certification"
        )

    def accept_certification(
        self,
        batch_id: str,
        endpoint: RepositoryEndpoint,
        *,
        certification_id: str,
    ) -> LandingOutcome:
        """Accept a durable result produced by the external sandbox station."""

        self._require_enabled()
        batch = self._batch(batch_id)
        trusted_policy = self._validate_endpoint(batch, endpoint)
        if batch.state != "verifying" or not batch.candidate_sha:
            raise TransitionError("only a verifying exact candidate may be certified")
        certification = self._certification_by_id(certification_id)
        self._validate_certification(
            batch, certification, expected_policy_id=trusted_policy
        )
        stream = self._acquire_stream(batch)
        if stream is None:
            raise LandingBusyError("repository landing stream is leased")
        batch_lease = self._acquire_batch(batch.id)
        if batch_lease is None:
            self._release_stream(stream)
            raise LandingBusyError("integration batch is leased")
        completed = False
        try:
            if self._ls_remote(endpoint, batch.candidate_ref or "", "read") != batch.candidate_sha:
                raise LandingError("certified candidate ref no longer names exact SHA")
            if not self._is_ancestor(
                endpoint, batch.landing_base_sha, batch.candidate_sha
            ):
                raise LandingError("certified candidate does not preserve landing base")
            certification = self._certification_by_id(certification_id)
            self._validate_certification(
                batch, certification, expected_policy_id=trusted_policy
            )
            target_state = "certified" if certification.status == "passed" else "rejected"
            self._transition_verification_result(batch, batch_lease, target_state)
            completed = True
            return LandingOutcome(
                target_state,
                batch.id,
                batch.candidate_sha,
                stream_fence=stream.fence,
                detail={"certification_id": certification.id},
            )
        finally:
            if completed:
                self._release_batch(batch_lease)
                self._release_stream(stream)

    def land(
        self, batch_id: str, endpoint: RepositoryEndpoint
    ) -> LandingOutcome:
        """CAS-push a certified SHA, read it back, and durably receipt it."""

        self._require_enabled()
        initial = self._batch(batch_id)
        trusted_policy = self._validate_endpoint(initial, endpoint)
        if initial.state == "published":
            return self._published_outcome(initial, endpoint, retire_candidate=True)
        if initial.state != "certified":
            raise TransitionError("only a certified integration batch may land")
        certification = self._passed_certification(initial, trusted_policy)
        stream = self._acquire_stream(initial)
        if stream is None:
            raise LandingBusyError("repository landing stream is leased")
        batch_lease = self._acquire_batch(initial.id)
        if batch_lease is None:
            self._release_stream(stream)
            raise LandingBusyError("integration batch is leased")
        completed = False
        try:
            batch = self._batch(initial.id)
            self._validate_certification(
                batch, certification, expected_policy_id=trusted_policy
            )
            candidate_remote = self._ls_remote(
                endpoint, batch.candidate_ref or "", "read"
            )
            if candidate_remote != batch.candidate_sha:
                raise LandingError("protected candidate ref no longer names certified SHA")
            if not self._is_ancestor(
                endpoint, batch.landing_base_sha, batch.candidate_sha or ""
            ):
                raise LandingError("certified candidate does not preserve landing base")
            intent = self._ensure_landing_intent(batch, certification, stream)
            canonical_tip = self._ls_remote(endpoint, batch.target_ref, "read")

            if canonical_tip == batch.candidate_sha:
                attempt = self._latest_landing_attempt(intent)
                if attempt is None:
                    raise LandingError(
                        "canonical target names candidate without a durable push attempt"
                    )
                receipt = self._record_receipt(
                    batch,
                    intent,
                    attempt,
                    stream,
                    observed_sha=canonical_tip,
                    recovered=True,
                    recovery="exact_remote_readback",
                )
            elif canonical_tip != batch.landing_base_sha:
                if self._is_ancestor(
                    endpoint, batch.candidate_sha or "", canonical_tip
                ):
                    attempt = self._latest_landing_attempt(intent)
                    if attempt is None:
                        raise LandingError(
                            "candidate ancestry has no durable push attempt to recover"
                        )
                    receipt = self._record_receipt(
                        batch,
                        intent,
                        attempt,
                        stream,
                        observed_sha=canonical_tip,
                        recovered=True,
                        recovery="candidate_is_ancestor_of_current_target",
                    )
                else:
                    self._mark_stale(
                        batch,
                        observed_sha=canonical_tip,
                        reason="canonical target moved before landing CAS",
                    )
                    completed = True
                    return LandingOutcome(
                        "stale",
                        batch.id,
                        batch.candidate_sha or "",
                        canonical_tip,
                        stream.fence,
                    )
            else:
                attempt = self._create_landing_attempt(batch, intent, stream)
                self._assert_leases(stream, batch_lease)
                self._fault("after_attempt", batch, stream)
                self._fault("before_push", batch, stream)
                self._push_canonical_cas(endpoint, batch)
                self._fault("after_push", batch, stream)
                canonical_tip = self._ls_remote(endpoint, batch.target_ref, "read")
                if canonical_tip != batch.candidate_sha:
                    if not self._is_ancestor(
                        endpoint, batch.candidate_sha or "", canonical_tip
                    ):
                        raise LandingError(
                            "canonical push result is ambiguous; intent retained for recovery"
                        )
                receipt = self._record_receipt(
                    batch,
                    intent,
                    attempt,
                    stream,
                    observed_sha=canonical_tip,
                    recovered=canonical_tip != batch.candidate_sha,
                    recovery=(
                        "concurrent_descendant_after_push"
                        if canonical_tip != batch.candidate_sha
                        else ""
                    ),
                )
            self._fault("after_receipt", batch, stream)
            self._retire_candidate(endpoint, self._batch(batch.id))
            completed = True
            return LandingOutcome(
                "landed" if not receipt.get("recovered") else "recovered",
                batch.id,
                batch.candidate_sha or "",
                str(receipt.get("observed_sha") or ""),
                stream.fence,
                receipt,
            )
        finally:
            if completed:
                self._release_batch(batch_lease)
                self._release_stream(stream)

    # -- Authoritative record loading -------------------------------------------------

    def _batch(self, batch_id: str) -> WorkPackageIntegrationBatch:
        row = self.store.query_one(
            "SELECT * FROM work_package_integration_batches WHERE id = ?", (batch_id,)
        )
        if row is None:
            raise ValidationError("work-package integration batch not found: %s" % batch_id)
        return WorkPackageIntegrationBatch(
            id=row["id"],
            package_id=row["package_id"],
            plan_version=int(row["plan_version"]),
            epoch=int(row["epoch"]),
            repository_id=row["repository_id"],
            target_ref=row["target_ref"],
            assembly_base_sha=row["assembly_base_sha"],
            landing_base_sha=row["landing_base_sha"],
            input_digest=row["input_digest"],
            candidate_sha=row["candidate_sha"],
            candidate_tree_digest=row["candidate_tree_digest"],
            candidate_ref=row["candidate_ref"],
            candidate_fence=(
                int(row["candidate_fence"])
                if row["candidate_fence"] is not None
                else None
            ),
            state=row["state"],
            integration_task_id=row["integration_task_id"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            lease_fence=int(row["lease_fence"]),
            metadata=json_loads(row["metadata"], {}) or {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def _assembly_inputs(self, batch: WorkPackageIntegrationBatch) -> list[AssemblyInput]:
        rows = self.store.query_all(
            "SELECT input.ordinal, input.task_id, input.evidence_id, "
            "verification.repository_id, verification.attempt_ref, "
            "verification.attempt_head_sha "
            "FROM work_package_batch_inputs AS input "
            "JOIN evidence_attempt_verifications AS verification "
            "ON verification.evidence_id = input.evidence_id "
            "AND verification.task_id = input.task_id "
            "AND verification.lease_id = input.assignment_lease_id "
            "AND verification.attempt_number = input.attempt_number "
            "WHERE input.batch_id = ? ORDER BY input.ordinal, input.id",
            (batch.id,),
        )
        inputs: list[AssemblyInput] = []
        for row in rows:
            if str(row["repository_id"] or "") != str(batch.repository_id or ""):
                raise LandingError("batch input verification belongs to another repository")
            ref = validate_git_ref(str(row["attempt_ref"] or ""))
            if not ref.startswith("refs/mac/attempts/"):
                raise LandingError(
                    "batch input is not protected under refs/mac/attempts/"
                )
            sha = _validate_sha(str(row["attempt_head_sha"] or ""), "reviewed input")
            inputs.append(
                AssemblyInput(
                    ordinal=int(row["ordinal"]),
                    task_id=row["task_id"],
                    evidence_id=row["evidence_id"],
                    protected_ref=ref,
                    reviewed_sha=sha,
                )
            )
        computed = compute_landing_input_digest(inputs)
        if batch.input_digest != computed:
            raise LandingError("immutable batch input digest does not match reviewed inputs")
        return inputs

    def _certification_by_id(self, certification_id: str) -> WorkPackageCertification:
        row = self.store.query_one(
            "SELECT * FROM work_package_certifications WHERE id = ?",
            (certification_id,),
        )
        if row is None:
            raise LandingError("certifier did not create its durable certification record")
        return _certification_from_row(row)

    def _passed_certification(
        self,
        batch: WorkPackageIntegrationBatch,
        expected_policy_id: str,
    ) -> WorkPackageCertification:
        rows = self.store.query_all(
            "SELECT * FROM work_package_certifications "
            "WHERE batch_id = ? AND status = 'passed' ORDER BY created_at, id",
            (batch.id,),
        )
        if len(rows) != 1:
            raise LandingError(
                "certified batch must have exactly one uncommitted passed certification"
            )
        certification = _certification_from_row(rows[0])
        self._validate_certification(
            batch, certification, expected_policy_id=expected_policy_id
        )
        return certification

    @staticmethod
    def _validate_certification(
        batch: WorkPackageIntegrationBatch,
        certification: WorkPackageCertification,
        *,
        expected_policy_id: str,
    ) -> None:
        expected = (
            batch.id,
            batch.package_id,
            batch.plan_version,
            batch.epoch,
            batch.candidate_sha,
            batch.assembly_base_sha,
            batch.landing_base_sha,
            batch.target_ref,
        )
        observed = (
            certification.batch_id,
            certification.package_id,
            certification.plan_version,
            certification.epoch,
            certification.candidate_sha,
            certification.assembly_base_sha,
            certification.landing_base_sha,
            certification.target_ref,
        )
        if observed != expected or certification.status not in {"passed", "failed"}:
            raise LandingError("certification is not bound to this exact candidate and base")
        isolation = certification.verification.get("isolation")
        required_isolation = {
            "schema": CERTIFICATION_ISOLATION_SCHEMA,
            "network": "disabled",
            "landing_credentials": "absent",
            "planner_commands": "rejected",
            "policy_source": "trusted_controller",
        }
        if not isinstance(isolation, dict) or any(
            isolation.get(key) != value for key, value in required_isolation.items()
        ):
            raise LandingError("certification lacks the required isolation attestation")
        if not str(isolation.get("policy_id") or "").strip():
            raise LandingError("certification isolation has no trusted policy identity")
        if isolation.get("policy_id") != expected_policy_id:
            raise LandingError("certification policy does not match repository contract")

    # -- Fenced leases ----------------------------------------------------------------

    def _stream_name(self, batch: WorkPackageIntegrationBatch) -> str:
        target_hash = hashlib.sha256(batch.target_ref.encode("utf-8")).hexdigest()[:20]
        return "landing:%s:%s" % (batch.repository_id, target_hash)

    def _acquire_stream(
        self, batch: WorkPackageIntegrationBatch
    ) -> Optional[_StreamLease]:
        if not batch.repository_id:
            raise LandingError("landing batch has no repository identity")
        name = self._stream_name(batch)
        now = self._iso_now()
        expires = self._iso(self._now_utc() + timedelta(seconds=self.config.lease_seconds))
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO work_package_landing_streams "
                "(repository_id, target_ref, lease_owner, lease_expires_at, "
                "lease_fence, created_at, updated_at) "
                "VALUES (?, ?, NULL, NULL, 0, ?, ?) "
                "ON CONFLICT(repository_id, target_ref) DO NOTHING",
                (batch.repository_id, batch.target_ref, now, now),
            )
            row = conn.execute(
                "SELECT lease_owner, lease_expires_at, lease_fence "
                "FROM work_package_landing_streams "
                "WHERE repository_id = ? AND target_ref = ?",
                (batch.repository_id, batch.target_ref),
            ).fetchone()
            fence = int(row["lease_fence"]) + 1
            result = conn.execute(
                "UPDATE work_package_landing_streams SET lease_owner = ?, "
                "lease_expires_at = ?, lease_fence = ?, updated_at = ? "
                "WHERE repository_id = ? AND target_ref = ? AND lease_fence = ? "
                "AND (lease_owner IS NULL OR lease_expires_at <= ? OR lease_owner = ?)",
                (
                    self.owner,
                    expires,
                    fence,
                    now,
                    batch.repository_id,
                    batch.target_ref,
                    fence - 1,
                    now,
                    self.owner,
                ),
            )
            if result.rowcount != 1:
                return None
        return _StreamLease(
            name, self.owner, fence, batch.id, batch.repository_id, batch.target_ref
        )

    def _release_stream(self, lease: _StreamLease) -> None:
        self.store.execute(
            "UPDATE work_package_landing_streams SET lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = ? "
            "WHERE repository_id = ? AND target_ref = ? AND lease_owner = ? "
            "AND lease_fence = ?",
            (
                self._iso_now(),
                lease.repository_id,
                lease.target_ref,
                lease.owner,
                lease.fence,
            ),
        )

    def _acquire_batch(
        self, batch_id: str, *, start_assembly: bool = False
    ) -> Optional[_BatchLease]:
        now = self._iso_now()
        expires = self._iso(self._now_utc() + timedelta(seconds=self.config.lease_seconds))
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state, lease_owner, lease_expires_at, lease_fence "
                "FROM work_package_integration_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("work-package integration batch not found: %s" % batch_id)
            state = row["state"]
            if start_assembly and state == "queued":
                next_state = "assembling"
            else:
                next_state = state
            if row["lease_owner"] not in {None, self.owner} and not _expired(
                row["lease_expires_at"], self._now_utc()
            ):
                return None
            old_fence = int(row["lease_fence"])
            fence = old_fence + 1
            result = conn.execute(
                "UPDATE work_package_integration_batches SET state = ?, "
                "lease_owner = ?, lease_expires_at = ?, lease_fence = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND lease_fence = ? "
                "AND (lease_owner IS NULL OR lease_owner = ? OR lease_expires_at <= ?)",
                (
                    next_state,
                    self.owner,
                    expires,
                    fence,
                    now,
                    batch_id,
                    state,
                    old_fence,
                    self.owner,
                    now,
                ),
            )
            if result.rowcount != 1:
                return None
        return _BatchLease(self.owner, fence, batch_id)

    def _release_batch(self, lease: _BatchLease) -> None:
        self.store.execute(
            "UPDATE work_package_integration_batches SET lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = ? "
            "WHERE id = ? AND lease_owner = ? AND lease_fence = ?",
            (self._iso_now(), lease.batch_id, lease.owner, lease.fence),
        )

    def _assert_leases(self, stream: _StreamLease, batch: _BatchLease) -> None:
        now = self._now_utc()
        row = self.store.query_one(
            "SELECT lease_owner, lease_expires_at, lease_fence "
            "FROM work_package_landing_streams "
            "WHERE repository_id = ? AND target_ref = ?",
            (stream.repository_id, stream.target_ref),
        )
        if (
            row is None
            or row["lease_owner"] != stream.owner
            or _expired(row["lease_expires_at"], now)
            or int(row["lease_fence"]) != stream.fence
        ):
            raise LandingLeaseLostError("repository stream fence is no longer held")
        row = self.store.query_one(
            "SELECT lease_owner, lease_expires_at, lease_fence "
            "FROM work_package_integration_batches WHERE id = ?",
            (batch.batch_id,),
        )
        if (
            row is None
            or row["lease_owner"] != batch.owner
            or int(row["lease_fence"]) != batch.fence
            or _expired(row["lease_expires_at"], now)
        ):
            raise LandingLeaseLostError("integration batch fence is no longer held")

    # -- Batch CAS transitions --------------------------------------------------------

    def _assign_candidate(
        self,
        batch: WorkPackageIntegrationBatch,
        lease: _BatchLease,
        *,
        candidate_sha: str,
        tree_digest: str,
        candidate_ref: str,
    ) -> None:
        candidate_sha = _validate_sha(candidate_sha, "candidate")
        validate_git_ref(candidate_ref)
        result = self.store.execute(
            "UPDATE work_package_integration_batches SET candidate_sha = ?, "
            "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = ?, "
            "updated_at = ? WHERE id = ? AND state = 'assembling' "
            "AND candidate_sha IS NULL AND lease_owner = ? AND lease_fence = ?",
            (
                candidate_sha,
                tree_digest,
                candidate_ref,
                lease.fence,
                self._iso_now(),
                batch.id,
                lease.owner,
                lease.fence,
            ),
        )
        if result.rowcount != 1:
            current = self._batch(batch.id)
            if (
                current.candidate_sha == candidate_sha
                and current.candidate_ref == candidate_ref
                and current.candidate_fence == lease.fence
            ):
                return
            raise LandingLeaseLostError("candidate assignment CAS failed")

    def _transition_to_verifying(self, batch_id: str, lease: _BatchLease) -> None:
        result = self.store.execute(
            "UPDATE work_package_integration_batches SET state = 'verifying', "
            "updated_at = ? WHERE id = ? AND state = 'assembling' "
            "AND lease_owner = ? AND lease_fence = ? AND candidate_sha IS NOT NULL",
            (self._iso_now(), batch_id, lease.owner, lease.fence),
        )
        if result.rowcount != 1 and self._batch(batch_id).state != "verifying":
            raise LandingLeaseLostError("verifying transition CAS failed")

    def _transition_verification_result(
        self,
        batch: WorkPackageIntegrationBatch,
        lease: _BatchLease,
        target: str,
    ) -> None:
        result = self.store.execute(
            "UPDATE work_package_integration_batches SET state = ?, completed_at = ?, "
            "updated_at = ? WHERE id = ? AND state = 'verifying' "
            "AND lease_owner = ? AND lease_fence = ?",
            (
                target,
                None if target == "certified" else self._iso_now(),
                self._iso_now(),
                batch.id,
                lease.owner,
                lease.fence,
            ),
        )
        if result.rowcount != 1:
            raise LandingLeaseLostError("certification transition CAS failed")

    def _ensure_landing_intent(
        self,
        batch: WorkPackageIntegrationBatch,
        certification: WorkPackageCertification,
        stream: _StreamLease,
    ) -> WorkPackageLandingIntent:
        identity = {
            "batch_id": batch.id,
            "certification_id": certification.id,
            "candidate_sha": batch.candidate_sha,
            "candidate_ref": batch.candidate_ref,
            "assembly_base_sha": batch.assembly_base_sha,
            "landing_base_sha": batch.landing_base_sha,
            "target_ref": batch.target_ref,
        }
        intent_id = "land_%s" % hashlib.sha256(
            json_dumps(identity).encode("utf-8")
        ).hexdigest()
        now = self._iso_now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM work_package_integration_batches WHERE id = ?",
                (batch.id,),
            ).fetchone()
            if row is None or row["state"] != "certified":
                raise TransitionError("batch is no longer certified")
            conn.execute(
                "INSERT INTO work_package_landing_intents ("
                "id, batch_id, package_id, plan_version, epoch, repository_id, "
                "target_ref, candidate_sha, candidate_ref, assembly_base_sha, "
                "landing_base_sha, "
                "certification_id, stream_fence, created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_id) DO NOTHING",
                (
                    intent_id,
                    batch.id,
                    batch.package_id,
                    batch.plan_version,
                    batch.epoch,
                    batch.repository_id,
                    batch.target_ref,
                    batch.candidate_sha,
                    batch.candidate_ref,
                    batch.assembly_base_sha,
                    batch.landing_base_sha,
                    certification.id,
                    stream.fence,
                    self.owner,
                    now,
                ),
            )
            intent_row = conn.execute(
                "SELECT * FROM work_package_landing_intents WHERE batch_id = ?",
                (batch.id,),
            ).fetchone()
        intent = _landing_intent_from_row(intent_row)
        if (
            intent.id != intent_id
            or intent.certification_id != certification.id
            or intent.candidate_sha != batch.candidate_sha
            or intent.candidate_ref != batch.candidate_ref
            or intent.assembly_base_sha != batch.assembly_base_sha
            or intent.landing_base_sha != batch.landing_base_sha
            or intent.target_ref != batch.target_ref
        ):
            raise LandingError("append-only landing intent conflicts with batch identity")
        return intent

    def _latest_landing_attempt(
        self, intent: WorkPackageLandingIntent
    ) -> Optional[WorkPackageLandingAttempt]:
        row = self.store.query_one(
            "SELECT * FROM work_package_landing_attempts WHERE intent_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (intent.id,),
        )
        return _landing_attempt_from_row(row) if row is not None else None

    def _create_landing_attempt(
        self,
        batch: WorkPackageIntegrationBatch,
        intent: WorkPackageLandingIntent,
        stream: _StreamLease,
    ) -> WorkPackageLandingAttempt:
        now = self._iso_now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) AS last_attempt "
                "FROM work_package_landing_attempts WHERE intent_id = ?",
                (intent.id,),
            ).fetchone()
            attempt_number = int(row["last_attempt"]) + 1
            attempt_id = "landtry_%s" % hashlib.sha256(
                ("%s\x00%d\x00%d" % (intent.id, attempt_number, stream.fence)).encode(
                    "utf-8"
                )
            ).hexdigest()
            conn.execute(
                "INSERT INTO work_package_landing_attempts ("
                "id, intent_id, attempt_number, repository_id, target_ref, "
                "candidate_sha, expected_remote_sha, stream_fence, created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    intent.id,
                    attempt_number,
                    batch.repository_id,
                    batch.target_ref,
                    batch.candidate_sha,
                    batch.landing_base_sha,
                    stream.fence,
                    self.owner,
                    now,
                ),
            )
            attempt_row = conn.execute(
                "SELECT * FROM work_package_landing_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        return _landing_attempt_from_row(attempt_row)

    def _record_receipt(
        self,
        batch: WorkPackageIntegrationBatch,
        intent: WorkPackageLandingIntent,
        attempt: WorkPackageLandingAttempt,
        stream: _StreamLease,
        *,
        observed_sha: str,
        recovered: bool,
        recovery: str,
    ) -> Mapping[str, Any]:
        observed_sha = _validate_sha(observed_sha, "remote readback")
        now = self._iso_now()
        payload = {
            "schema": LANDING_RECEIPT_SCHEMA,
            "intent_id": intent.id,
            "attempt_id": attempt.id,
            "batch_id": batch.id,
            "candidate_sha": batch.candidate_sha,
            "target_ref": batch.target_ref,
            "observed_sha": observed_sha,
            "recovered": bool(recovered),
            "recovery": recovery,
            "attempt_stream_fence": attempt.stream_fence,
            "recording_stream_fence": stream.fence,
            "recorded_at": now,
        }
        receipt_digest = "sha256:%s" % hashlib.sha256(
            json_dumps(payload).encode("utf-8")
        ).hexdigest()
        receipt_id = "landrcpt_%s" % receipt_digest.removeprefix("sha256:")
        with self.store.transaction() as conn:
            stream_row = conn.execute(
                "SELECT lease_owner, lease_expires_at, lease_fence "
                "FROM work_package_landing_streams "
                "WHERE repository_id = ? AND target_ref = ?",
                (stream.repository_id, stream.target_ref),
            ).fetchone()
            if (
                stream_row is None
                or stream_row["lease_owner"] != stream.owner
                or _expired(stream_row["lease_expires_at"], self._now_utc())
                or int(stream_row["lease_fence"]) != stream.fence
            ):
                raise LandingLeaseLostError("cannot receipt without repository fence")
            row = conn.execute(
                "SELECT state FROM work_package_integration_batches WHERE id = ?",
                (batch.id,),
            ).fetchone()
            if row is None:
                raise TransitionError("landing batch disappeared before receipt")
            existing = conn.execute(
                "SELECT * FROM work_package_landing_receipts WHERE intent_id = ?",
                (intent.id,),
            ).fetchone()
            if row["state"] == "published" and existing is not None:
                return _landing_receipt_from_row(existing)
            if row["state"] != "certified":
                raise TransitionError("only a certified batch may receive publication")
            conn.execute(
                "INSERT INTO work_package_landing_receipts ("
                "id, intent_id, attempt_id, batch_id, repository_id, target_ref, "
                "candidate_sha, observed_sha, recovered, recovery, "
                "attempt_stream_fence, recording_stream_fence, recorded_by, "
                "recorded_at, receipt_digest"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    intent.id,
                    attempt.id,
                    batch.id,
                    batch.repository_id,
                    batch.target_ref,
                    batch.candidate_sha,
                    observed_sha,
                    int(bool(recovered)),
                    recovery,
                    attempt.stream_fence,
                    stream.fence,
                    self.owner,
                    now,
                    receipt_digest,
                ),
            )
            result = conn.execute(
                "UPDATE work_package_integration_batches SET state = 'published', "
                "completed_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'certified'",
                (now, now, batch.id),
            )
            if result.rowcount != 1:
                raise LandingLeaseLostError("landing receipt CAS failed")
            receipt_row = conn.execute(
                "SELECT * FROM work_package_landing_receipts WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            return _landing_receipt_from_row(receipt_row)

    def _mark_stale(
        self,
        batch: WorkPackageIntegrationBatch,
        *,
        observed_sha: str,
        reason: str,
    ) -> None:
        now = self._iso_now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM work_package_integration_batches WHERE id = ?",
                (batch.id,),
            ).fetchone()
            if row is None or row["state"] == "stale":
                return
            if row["state"] not in {"assembling", "verifying", "certified"}:
                raise TransitionError("batch cannot become stale from %s" % row["state"])
            result = conn.execute(
                "UPDATE work_package_integration_batches SET state = 'stale', "
                "completed_at = ?, updated_at = ? WHERE id = ? AND state = ?",
                (now, now, batch.id, row["state"]),
            )
            if result.rowcount != 1:
                raise LandingLeaseLostError("stale batch transition CAS failed")
            conn.execute(
                "UPDATE work_package_certifications SET status = 'invalidated', "
                "invalidated_at = ? WHERE batch_id = ? AND status = 'passed'",
                (now, batch.id),
            )

    # -- Git side effects -------------------------------------------------------------

    def _clone(self, endpoint: RepositoryEndpoint, checkout: Path) -> None:
        self._git_checked(
            ["clone", "--no-checkout", "--origin", "origin", endpoint.remote_url, str(checkout)],
            cwd=None,
            endpoint=endpoint,
            operation="read",
            label="clone canonical repository",
        )

    def _fetch_exact(
        self,
        endpoint: RepositoryEndpoint,
        checkout: Path,
        ref: str,
        expected_sha: str,
    ) -> None:
        validate_git_ref(ref)
        expected_sha = _validate_sha(expected_sha, "expected fetch")
        remote_sha = self._ls_remote(endpoint, ref, "read")
        if remote_sha != expected_sha:
            raise LandingError("remote protected ref does not name reviewed SHA")
        self._git_checked(
            ["fetch", "--no-tags", endpoint.remote_url, ref],
            cwd=checkout,
            endpoint=endpoint,
            operation="read",
            label="fetch exact protected ref",
        )
        fetched = self._rev_parse(checkout, "FETCH_HEAD", endpoint)
        if fetched != expected_sha:
            raise LandingError("fetched commit differs from reviewed SHA")

    def _stage_candidate(
        self,
        endpoint: RepositoryEndpoint,
        checkout: Path,
        *,
        candidate_ref: str,
        candidate_sha: str,
    ) -> None:
        current = self._ls_remote(endpoint, candidate_ref, "write", missing_ok=True)
        if current and current != candidate_sha:
            raise LandingError("protected candidate ref is occupied by another SHA")
        if not current:
            self._git_checked(
                [
                    "push",
                    "--porcelain",
                    "--force-with-lease=%s:" % candidate_ref,
                    endpoint.remote_url,
                    "%s:%s" % (candidate_sha, candidate_ref),
                ],
                cwd=checkout,
                endpoint=endpoint,
                operation="write",
                label="stage protected candidate ref",
            )
        if self._ls_remote(endpoint, candidate_ref, "write") != candidate_sha:
            raise LandingError("protected candidate ref read-back failed")

    def _push_canonical_cas(
        self, endpoint: RepositoryEndpoint, batch: WorkPackageIntegrationBatch
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-landing-push-") as raw:
            checkout = Path(raw) / "repo"
            self._clone(endpoint, checkout)
            self._fetch_exact(
                endpoint,
                checkout,
                batch.candidate_ref or "",
                batch.candidate_sha or "",
            )
            self._git_checked(
                [
                    "push",
                    "--porcelain",
                    "--force-with-lease=%s:%s"
                    % (batch.target_ref, batch.landing_base_sha),
                    endpoint.remote_url,
                    "%s:%s" % (batch.candidate_sha, batch.target_ref),
                ],
                cwd=checkout,
                endpoint=endpoint,
                operation="write",
                label="canonical compare-and-swap push",
            )

    def _retire_candidate(
        self, endpoint: RepositoryEndpoint, batch: WorkPackageIntegrationBatch
    ) -> None:
        if batch.state != "published" or not batch.candidate_ref or not batch.candidate_sha:
            return
        current = self._ls_remote(
            endpoint, batch.candidate_ref, "write", missing_ok=True
        )
        if not current:
            return
        if current != batch.candidate_sha:
            raise LandingError("refusing to retire candidate ref that changed identity")
        with tempfile.TemporaryDirectory(prefix="mac-landing-retire-") as raw:
            checkout = Path(raw) / "repo"
            self._clone(endpoint, checkout)
            self._git_checked(
                [
                    "push",
                    "--porcelain",
                    "--force-with-lease=%s:%s"
                    % (batch.candidate_ref, batch.candidate_sha),
                    endpoint.remote_url,
                    ":%s" % batch.candidate_ref,
                ],
                cwd=checkout,
                endpoint=endpoint,
                operation="write",
                label="retire receipted candidate ref",
            )
        if self._ls_remote(
            endpoint, batch.candidate_ref, "write", missing_ok=True
        ):
            raise LandingError("candidate ref deletion read-back failed")

    def _ls_remote(
        self,
        endpoint: RepositoryEndpoint,
        ref: str,
        operation: str,
        *,
        missing_ok: bool = False,
    ) -> str:
        validate_git_ref(ref)
        result = self._git(
            ["ls-remote", "--exit-code", endpoint.remote_url, ref],
            cwd=None,
            endpoint=endpoint,
            operation=operation,
        )
        if result.returncode == 2 and missing_ok:
            return ""
        if result.returncode != 0:
            raise LandingError(self._git_error("read remote ref", result))
        matches = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == ref:
                matches.append(_validate_sha(fields[0].lower(), "remote ref"))
        if len(matches) != 1:
            if missing_ok and not matches:
                return ""
            raise LandingError("remote ref did not resolve to one exact SHA")
        return matches[0]

    def _is_ancestor(
        self, endpoint: RepositoryEndpoint, ancestor: str, descendant: str
    ) -> bool:
        ancestor = _validate_sha(ancestor, "candidate ancestor")
        descendant = _validate_sha(descendant, "canonical descendant")
        with tempfile.TemporaryDirectory(prefix="mac-landing-ancestry-") as raw:
            checkout = Path(raw) / "repo"
            self._clone(endpoint, checkout)
            self._git_checked(
                ["fetch", "--no-tags", endpoint.remote_url, ancestor, descendant],
                cwd=checkout,
                endpoint=endpoint,
                operation="read",
                label="fetch ancestry commits",
            )
            result = self._git(
                ["merge-base", "--is-ancestor", ancestor, descendant],
                cwd=checkout,
                endpoint=endpoint,
                operation="read",
            )
            if result.returncode not in {0, 1}:
                raise LandingError(self._git_error("verify candidate ancestry", result))
            return result.returncode == 0

    def _is_ancestor_in_checkout(
        self,
        checkout: Path,
        ancestor: str,
        descendant: str,
        endpoint: RepositoryEndpoint,
    ) -> bool:
        ancestor = _validate_sha(ancestor, "landing ancestor")
        descendant = _validate_sha(descendant, "candidate descendant")
        result = self._git(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=checkout,
            endpoint=endpoint,
            operation="read",
        )
        if result.returncode not in {0, 1}:
            raise LandingError(self._git_error("verify assembled ancestry", result))
        return result.returncode == 0

    def _rev_parse(
        self, checkout: Path, value: str, endpoint: RepositoryEndpoint
    ) -> str:
        result = self._git_checked(
            ["rev-parse", "--verify", value],
            cwd=checkout,
            endpoint=endpoint,
            operation="read",
            label="resolve exact commit",
        )
        return _validate_sha(result.stdout.strip().lower(), "resolved commit")

    def _git_checked(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        endpoint: RepositoryEndpoint,
        operation: str,
        label: str,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self._git(
            args,
            cwd=cwd,
            endpoint=endpoint,
            operation=operation,
            extra_env=extra_env,
        )
        if result.returncode != 0:
            raise LandingError(self._git_error(label, result))
        return result

    def _git(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        endpoint: RepositoryEndpoint,
        operation: str,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        # Git gets a deliberately empty home and a narrow environment.  The
        # hub process may carry model/API/database secrets that have no place
        # in a landing subprocess; repository credentials enter only through
        # the explicit controller-owned provider.
        with tempfile.TemporaryDirectory(prefix="mac-landing-git-home-") as raw_home:
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": raw_home,
                "LANG": "C",
                "LC_ALL": "C",
                "GCM_INTERACTIVE": "Never",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
            credentials = self.credential_environment(operation, endpoint)
            for key, value in credentials.items():
                key_value = str(key)
                value_value = str(value)
                if (
                    not key_value
                    or "\x00" in key_value
                    or "\x00" in value_value
                    or key_value in environment
                    or key_value.startswith("GIT_CONFIG_")
                ):
                    raise ValidationError(
                        "landing credential environment is invalid"
                    )
                environment[key_value] = value_value
            if extra_env:
                for key, value in extra_env.items():
                    key_value = str(key)
                    value_value = str(value)
                    if (
                        not key_value
                        or "\x00" in key_value
                        or "\x00" in value_value
                        or key_value in environment
                        or key_value.startswith("GIT_CONFIG_")
                    ):
                        raise ValidationError("landing Git environment is invalid")
                    environment[key_value] = value_value
            return self.git_runner.run(args, cwd=cwd, env=environment)

    @staticmethod
    def _git_error(label: str, result: subprocess.CompletedProcess[str]) -> str:
        detail = redact_repository_hygiene_text(result.stderr or result.stdout).strip()
        return "%s failed%s" % (label, ": %s" % detail[:500] if detail else "")

    # -- Miscellaneous ---------------------------------------------------------------

    def _published_outcome(
        self,
        batch: WorkPackageIntegrationBatch,
        endpoint: RepositoryEndpoint,
        *,
        retire_candidate: bool,
    ) -> LandingOutcome:
        self._validate_endpoint(batch, endpoint)
        row = self.store.query_one(
            "SELECT * FROM work_package_landing_receipts WHERE batch_id = ?",
            (batch.id,),
        )
        if row is None:
            raise LandingError("published batch lacks an append-only landing receipt")
        receipt = _landing_receipt_from_row(row)
        if receipt.get("candidate_sha") != batch.candidate_sha:
            raise LandingError("published receipt conflicts with exact candidate identity")
        if retire_candidate:
            self._retire_candidate(endpoint, batch)
        return LandingOutcome(
            "published",
            batch.id,
            batch.candidate_sha or "",
            str(receipt.get("observed_sha") or ""),
            int(receipt.get("recording_stream_fence") or 0),
            receipt,
        )

    def _validate_endpoint(
        self, batch: WorkPackageIntegrationBatch, endpoint: RepositoryEndpoint
    ) -> str:
        if batch.repository_id != endpoint.repository_id:
            raise ValidationError("repository endpoint does not match integration batch")
        validate_git_ref(batch.target_ref)
        if not batch.target_ref.startswith("refs/heads/"):
            raise ValidationError("landing target must be a full refs/heads/* ref")
        _validate_sha(batch.assembly_base_sha, "assembly base")
        _validate_sha(batch.landing_base_sha, "landing base")
        if batch.candidate_sha:
            _validate_sha(batch.candidate_sha, "candidate")
        if batch.candidate_ref:
            validate_git_ref(batch.candidate_ref)
            if not batch.candidate_ref.startswith("refs/mac/"):
                raise ValidationError("candidate ref must remain protected under refs/mac/")
        with self.store.transaction() as conn:
            plan = conn.execute(
                "SELECT definition FROM work_package_plan_versions "
                "WHERE package_id = ? AND version = ?",
                (batch.package_id, batch.plan_version),
            ).fetchone()
            if plan is not None:
                validate_supported_work_package_topology(
                    json_loads(plan["definition"], {})
                )
            # A same-value write acquires the repository row lock on both
            # SQLite and Postgres without changing registry history.
            locked = conn.execute(
                "UPDATE project_repositories SET updated_at = updated_at "
                "WHERE id = ? AND enabled = 1",
                (batch.repository_id,),
            )
            if locked.rowcount != 1:
                raise LandingError("registered canonical repository is unavailable")
            repository = conn.execute(
                "SELECT source, metadata FROM project_repositories WHERE id = ?",
                (batch.repository_id,),
            ).fetchone()
        try:
            metadata = json_loads(repository["metadata"], {}) or {}
            canonical = resolve_repository_canonical_remote(
                {
                    "id": batch.repository_id,
                    "source": repository["source"],
                    "metadata": metadata,
                }
            )
        except ValueError as exc:
            raise LandingError("registered canonical remote is invalid") from exc
        if canonical.identity != canonical_git_remote_identity(endpoint.remote_url):
            raise LandingError(
                "caller endpoint does not match locked canonical repository source"
            )
        contract = metadata.get("repository_contract")
        if not isinstance(contract, Mapping):
            raise LandingError("repository contract is unavailable for landing")
        policy_id = str(contract.get("landing_certification_policy_id") or "").strip()
        if not policy_id:
            raise LandingError(
                "repository contract has no landing_certification_policy_id"
            )
        return policy_id

    def _deterministic_commit_environment(
        self, batch: WorkPackageIntegrationBatch
    ) -> Mapping[str, str]:
        created = _parse_datetime(batch.created_at)
        timestamp = created.astimezone(timezone.utc).isoformat()
        return {
            "GIT_AUTHOR_NAME": "MAC Landing Service",
            "GIT_AUTHOR_EMAIL": "landing@mac.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_NAME": "MAC Landing Service",
            "GIT_COMMITTER_EMAIL": "landing@mac.invalid",
            "GIT_COMMITTER_DATE": timestamp,
        }

    def _fault(
        self,
        stage: str,
        batch: WorkPackageIntegrationBatch,
        stream: _StreamLease,
    ) -> None:
        self._fault_hook(
            stage,
            {
                "batch_id": batch.id,
                "candidate_sha": batch.candidate_sha,
                "target_ref": batch.target_ref,
                "stream_fence": stream.fence,
            },
        )

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise LandingDisabledError("landing service is disabled")

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


def _landing_intent_from_row(row: Any) -> WorkPackageLandingIntent:
    if row is None:
        raise LandingError("landing intent insert did not produce a durable row")
    return WorkPackageLandingIntent(
        id=row["id"],
        batch_id=row["batch_id"],
        package_id=row["package_id"],
        plan_version=int(row["plan_version"]),
        epoch=int(row["epoch"]),
        repository_id=row["repository_id"],
        certification_id=row["certification_id"],
        candidate_sha=row["candidate_sha"],
        candidate_ref=row["candidate_ref"],
        assembly_base_sha=row["assembly_base_sha"],
        landing_base_sha=row["landing_base_sha"],
        target_ref=row["target_ref"],
        stream_fence=int(row["stream_fence"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _landing_attempt_from_row(row: Any) -> WorkPackageLandingAttempt:
    if row is None:
        raise LandingError("landing attempt insert did not produce a durable row")
    return WorkPackageLandingAttempt(
        id=row["id"],
        intent_id=row["intent_id"],
        attempt_number=int(row["attempt_number"]),
        repository_id=row["repository_id"],
        target_ref=row["target_ref"],
        candidate_sha=row["candidate_sha"],
        expected_remote_sha=row["expected_remote_sha"],
        stream_fence=int(row["stream_fence"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _landing_receipt_from_row(row: Any) -> Mapping[str, Any]:
    receipt = WorkPackageLandingReceipt(
        id=row["id"],
        intent_id=row["intent_id"],
        attempt_id=row["attempt_id"],
        batch_id=row["batch_id"],
        repository_id=row["repository_id"],
        target_ref=row["target_ref"],
        candidate_sha=row["candidate_sha"],
        observed_sha=row["observed_sha"],
        recovered=bool(row["recovered"]),
        recovery=row["recovery"],
        attempt_stream_fence=int(row["attempt_stream_fence"]),
        recording_stream_fence=int(row["recording_stream_fence"]),
        recorded_by=row["recorded_by"],
        recorded_at=row["recorded_at"],
        receipt_digest=row["receipt_digest"],
    )
    return {"schema": LANDING_RECEIPT_SCHEMA, **receipt.to_dict()}


def _certification_from_row(row: Any) -> WorkPackageCertification:
    return WorkPackageCertification(
        id=row["id"],
        batch_id=row["batch_id"],
        package_id=row["package_id"],
        plan_version=int(row["plan_version"]),
        epoch=int(row["epoch"]),
        candidate_sha=row["candidate_sha"],
        assembly_base_sha=row["assembly_base_sha"],
        landing_base_sha=row["landing_base_sha"],
        target_ref=row["target_ref"],
        status=row["status"],
        verification_digest=row["verification_digest"],
        verification=json_loads(row["verification"], {}) or {},
        certification_task_id=row["certification_task_id"],
        tests_evidence_id=row["tests_evidence_id"],
        review_task_id=row["review_task_id"],
        review_evidence_id=row["review_evidence_id"],
        codegraph_evidence_id=row["codegraph_evidence_id"],
        certified_by=row["certified_by"],
        created_at=row["created_at"],
        invalidated_at=row["invalidated_at"],
        publication_id=row["publication_id"],
        publication_evidence_id=row["publication_evidence_id"],
    )


def _validate_sha(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != _SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValidationError("%s must be an exact lowercase 40-hex commit SHA" % label)
    return normalized


def _safe_component(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value
    ).strip(".-")
    if not safe:
        raise ValidationError("batch id cannot form a protected candidate ref")
    return safe[:120]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expired(value: Optional[str], now: datetime) -> bool:
    if not value:
        return True
    try:
        return _parse_datetime(value) <= now
    except (TypeError, ValueError):
        return True


__all__ = [
    "AssemblyInput",
    "CERTIFICATION_ISOLATION_SCHEMA",
    "GitRunner",
    "compute_landing_input_digest",
    "LandingBusyError",
    "LandingDisabledError",
    "LandingError",
    "LandingLeaseLostError",
    "LandingOutcome",
    "LandingService",
    "LandingServiceConfig",
    "RepositoryEndpoint",
    "SubprocessGitRunner",
]
