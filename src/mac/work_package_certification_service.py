"""Durable, fenced bridge from exact integration candidates to OpenShell.

The hub prepares an immutable job from a repository-owned certification
contract, then an outer controller claims that job and invokes the credential-
free :mod:`mac.openshell_certifier` runner.  Result ingestion rechecks the
monotonic job fence and every candidate, policy, image, bundle, and command
identity before appending the certification consumed by landing.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from mac.models import (
    JsonDict,
    TransitionError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
)
from mac.openshell_certifier import (
    CERTIFIER_PRIMARY_COMMAND,
    CERTIFICATION_ISOLATION_SCHEMA,
    CERTIFICATION_RESULT_SCHEMA,
    CertifierPhaseProfile,
    CertificationCleanupError,
    CertificationPolicy,
    CertificationValidationError,
    ControllerCommand,
    OpenShellCertificationJob,
    OpenShellCertificationResult,
    OpenShellCertificationRunner,
    validate_certifier_controller_command,
    validate_certifier_image_ref,
    validate_certifier_phase_manifest,
)
from mac.store import Store


CERTIFICATION_JOB_RECORD_SCHEMA = "mac.work_package.certification_job.v2"
CERTIFICATION_CONTRACT_SCHEMA = "mac.work_package.certification_contract.v2"
CERTIFICATION_INGESTION_SCHEMA = "mac.work_package.certification_ingestion.v1"
_RESULT_KEYS = frozenset(
    {
        "schema",
        "job_id",
        "job_digest",
        "batch_id",
        "package_id",
        "plan_version",
        "epoch",
        "candidate_sha",
        "candidate_tree_digest",
        "assembly_base_sha",
        "landing_base_sha",
        "target_ref",
        "status",
        "policy",
        "image_ref",
        "image_digest",
        "bundle_digest",
        "commands_digest",
        "sandbox_name",
        "checks",
        "phase_manifest",
        "isolation",
        "started_at",
        "completed_at",
        "cleanup_status",
        "failure_class",
        "failure_reason",
        "result_digest",
    }
)
_CHECK_KEYS = frozenset(
    {"command_id", "argv", "returncode", "status", "stdout", "stderr", "timed_out"}
)
_ISOLATION_KEYS = frozenset(
    {
        "schema",
        "network",
        "landing_credentials",
        "planner_commands",
        "policy_source",
        "policy_id",
        "policy_version",
        "policy_checksum",
        "landlock",
        "run_as_user",
        "launcher_environment",
        "input_format",
        "assembly_base_transport",
    }
)
_SAFE_LAUNCH_ENV_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "OPENSHELL_GATEWAY_ENDPOINT",
        "PATH",
        "SYSTEMROOT",
        "TMPDIR",
        "TZ",
    }
)
_CONTRACT_KEYS = frozenset(
    {
        "schema",
        "policy",
        "policy_text",
        "phase_profile",
        "image_ref",
        "controller_commands",
    }
)
_POLICY_KEYS = frozenset({"policy_id", "version", "checksum"})
_COMMAND_KEYS = frozenset({"command_id", "argv", "timeout_seconds"})


def normalize_repository_certification_contract(
    repository_contract: Mapping[str, Any],
) -> JsonDict:
    """Validate every repository field needed to prepare a certifier job.

    Checked-in contract loading and the live preparation path both use this
    function. Invalid policy, command, and immutable-image configuration is
    therefore rejected before integration transfers product WIP downstream.
    """

    if not isinstance(repository_contract, Mapping):
        raise ValidationError("repository has no certification contract")
    policy_id = str(
        repository_contract.get("landing_certification_policy_id") or ""
    ).strip()
    raw = repository_contract.get("work_package_certification")
    if not policy_id or not isinstance(raw, Mapping):
        raise ValidationError("repository certification contract is incomplete")
    if set(raw) != _CONTRACT_KEYS:
        raise ValidationError("repository certification contract fields are invalid")
    if raw.get("schema") != CERTIFICATION_CONTRACT_SCHEMA:
        raise ValidationError("repository certification contract schema is invalid")

    policy = raw.get("policy")
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_KEYS:
        raise ValidationError("certification policy identity is malformed")
    if str(policy.get("policy_id") or "").strip() != policy_id:
        raise ValidationError("certification policy does not match landing policy")
    version = policy.get("version")
    if isinstance(version, bool):
        raise ValidationError("certification policy version must be positive")
    try:
        policy_version = int(version)
    except (TypeError, ValueError) as exc:
        raise ValidationError("certification policy version must be positive") from exc
    if policy_version < 1:
        raise ValidationError("certification policy version must be positive")

    commands = raw.get("controller_commands")
    if not isinstance(commands, list) or not commands:
        raise ValidationError("certification contract has no controller commands")
    normalized_commands = []
    command_ids = set()
    primary_commands = 0
    for item in commands:
        if (
            not isinstance(item, Mapping)
            or not {"command_id", "argv"}.issubset(item)
            or set(item) - _COMMAND_KEYS
        ):
            raise ValidationError("certification command is malformed")
        argv = item.get("argv")
        if not isinstance(argv, list) or not all(
            isinstance(value, str) for value in argv
        ):
            raise ValidationError("certification command argv is malformed")
        if any(
            value == "--base-sha" or value.startswith("--base-sha=") for value in argv
        ):
            raise ValidationError(
                "certification assembly base is reserved for the controller"
            )
        timeout = item.get("timeout_seconds", 900)
        if isinstance(timeout, bool):
            raise ValidationError("certification command timeout must be positive")
        try:
            timeout_seconds = int(timeout)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "certification command timeout must be positive"
            ) from exc
        command = ControllerCommand(
            str(item.get("command_id") or ""),
            tuple(argv),
            timeout_seconds,
        )
        try:
            validate_certifier_controller_command(command)
        except CertificationValidationError as exc:
            raise ValidationError("certification command is invalid") from exc
        if command.command_id in command_ids:
            raise ValidationError("certification command ids must be unique")
        command_ids.add(command.command_id)
        if command.argv[0] == CERTIFIER_PRIMARY_COMMAND:
            primary_commands += 1
            if command.argv != (CERTIFIER_PRIMARY_COMMAND,):
                raise ValidationError(
                    "primary certification command does not accept repository arguments"
                )
        normalized_commands.append(command.to_dict())
    if primary_commands != 1:
        raise ValidationError(
            "certification contract requires exactly one primary frozen test command"
        )

    try:
        phase_profile = CertifierPhaseProfile.from_mapping(raw.get("phase_profile"))
    except CertificationValidationError as exc:
        raise ValidationError("certifier phase profile is invalid") from exc

    value = {
        "policy": {
            "policy_id": policy_id,
            "version": policy_version,
            "checksum": str(policy.get("checksum") or ""),
        },
        "policy_text": str(raw.get("policy_text") or ""),
        "phase_profile": phase_profile.to_dict(),
        "image_ref": str(raw.get("image_ref") or "").strip(),
        "controller_commands": normalized_commands,
    }
    try:
        CertificationPolicy(
            value["policy"]["policy_id"],
            value["policy"]["version"],
            value["policy"]["checksum"],
            value["policy_text"],
        ).validate()
        value["image_ref"] = validate_certifier_image_ref(value["image_ref"])
    except CertificationValidationError as exc:
        raise ValidationError("certification contract is invalid") from exc
    return value


def _bind_controller_assembly_base(
    contract: Mapping[str, Any], assembly_base_sha: str
) -> JsonDict:
    """Materialize the reserved base once so command/job/result digests bind it."""

    if not re.fullmatch(r"[0-9a-f]{40}", str(assembly_base_sha or "")):
        raise ValidationError("certification assembly base is invalid")
    resolved = dict(contract)
    resolved["controller_commands"] = [
        {
            **dict(item),
            "argv": [*item["argv"], "--base-sha", assembly_base_sha],
        }
        for item in contract["controller_commands"]
    ]
    return resolved


@dataclass(frozen=True)
class CertificationJobClaim:
    job_id: str
    owner: str
    fence: int
    expires_at: str

    def to_dict(self) -> JsonDict:
        return {
            "job_id": self.job_id,
            "owner": self.owner,
            "fence": self.fence,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class CertificationIngestionResult:
    job_id: str
    certification_id: str
    status: str
    result_digest: str
    created: bool
    certification_task_id: str
    certification_node_key: str
    controller_station_receipt_id: str
    batch_state: str
    package_state: str

    def to_dict(self) -> JsonDict:
        return {
            "schema": CERTIFICATION_INGESTION_SCHEMA,
            "job_id": self.job_id,
            "certification_id": self.certification_id,
            "status": self.status,
            "result_digest": self.result_digest,
            "created": self.created,
            "certification_task_id": self.certification_task_id,
            "certification_node_key": self.certification_node_key,
            "controller_station_receipt_id": self.controller_station_receipt_id,
            "batch_state": self.batch_state,
            "package_state": self.package_state,
        }


class CertificationJobBusyError(TransitionError):
    """A live controller owns the exact certification job fence."""


class CertificationJobLeaseLostError(TransitionError):
    """A stale controller attempted to ingest an external result."""


class WorkPackageCertificationService:
    """Prepare, fence, execute, and ingest exact-candidate certifications."""

    def __init__(
        self,
        store: Store,
        *,
        owner: str = "work-package-certification-controller",
        runner: Optional[OpenShellCertificationRunner] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        owner_value = str(owner or "").strip()
        if not owner_value:
            raise ValueError("certification controller owner is required")
        self.store = store
        self.owner = owner_value
        # Resolve the production runner lazily.  Control-plane processes keep
        # certification services available while the default-off pipeline is
        # unconfigured; the first executable certification boundary performs a
        # fail-closed binding/preflight before it claims the durable job fence.
        self.runner = runner
        self._runner_from_environment = runner is None
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Whether the caller supplied its own clock. Only tests do: both
        # production constructions (services.py and work_package_pipeline_runtime)
        # leave it None, so the authority clock in production is always the
        # database's. See _authority_now for why the distinction has to exist.
        self._clock_injected = now is not None

    def validate_runtime_binding(self) -> None:
        """Fail closed unless the production OpenShell CLI binding is usable."""

        try:
            self._runtime_runner()
        except CertificationValidationError as exc:
            raise ValidationError("OpenShell certifier runtime is unavailable") from exc

    def validate_repository_contract(
        self, repository_id: str, *, source: Optional[Any] = None
    ) -> None:
        """Fail closed unless repository and production certifier are both ready."""

        self._certification_contract(
            self._required(repository_id, "certification repository id"),
            source=source,
        )
        # This validator is the downstream pull gate used before package
        # activation and again before integration transfers WIP.  Prove the
        # host CLI binding here, not only after a candidate has been assembled.
        self.validate_runtime_binding()

    def prepare(
        self,
        batch_id: str,
        bundle_path: Path,
        *,
        actor: str,
    ) -> JsonDict:
        """Persist one immutable job derived from the locked repository contract."""

        actor_value = self._required(actor, "certification preparation actor")
        batch = self._batch(batch_id)
        if batch["state"] != "verifying":
            raise TransitionError("only a verifying exact candidate can be certified")
        successor = self._certification_successor(self.store, batch)
        contract = self._certification_contract(batch["repository_id"])
        resolved_contract = _bind_controller_assembly_base(
            contract, str(batch["assembly_base_sha"])
        )
        bundle = Path(bundle_path)
        bundle_digest = self._sha256_file(bundle)
        seed = {
            "batch_id": batch["id"],
            "candidate_sha": batch["candidate_sha"],
            "candidate_tree_digest": batch["candidate_tree_digest"],
            "candidate_ref": batch["candidate_ref"],
            "candidate_fence": int(batch["candidate_fence"]),
            "assembly_base_sha": batch["assembly_base_sha"],
            "policy": contract["policy"],
            "phase_profile": contract["phase_profile"],
            "image_ref": contract["image_ref"],
            "controller_commands": contract["controller_commands"],
            "bundle_digest": bundle_digest,
            "certification_task_id": successor["task_id"],
            "certification_node_key": successor["node_key"],
        }
        job_id = "wpcjob_%s" % self._sha256_json(seed).split(":", 1)[1][:32]
        job = self._job_from_values(
            job_id, batch, resolved_contract, bundle, bundle_digest
        )
        self._validate_job(job)
        definition = {
            "schema": CERTIFICATION_JOB_RECORD_SCHEMA,
            "job": job.identity(),
            "policy_text": contract["policy_text"],
            "candidate_ref": batch["candidate_ref"],
            "candidate_fence": int(batch["candidate_fence"]),
            "repository_id": batch["repository_id"],
            "integration_task_id": batch["integration_task_id"],
            "integration_node_key": successor["integration_node_key"],
            "integration_station_receipt_id": successor[
                "integration_station_receipt_id"
            ],
            "certification_task_id": successor["task_id"],
            "certification_node_key": successor["node_key"],
            "prepared_by": actor_value,
        }
        now = self._iso(self._now())
        with self.store.transaction() as conn:
            self._lock_batch(conn, batch)
            locked_contract = self._lock_repository_contract(
                conn, str(batch["repository_id"])
            )
            if json_dumps(locked_contract) != json_dumps(contract):
                raise TransitionError(
                    "repository certification contract changed during preparation"
                )
            locked_successor = self._certification_successor(conn, batch)
            if json_dumps(locked_successor) != json_dumps(successor):
                raise TransitionError(
                    "certification successor changed during preparation"
                )
            existing = conn.execute(
                "SELECT * FROM work_package_certification_jobs WHERE batch_id = ?",
                (batch["id"],),
            ).fetchone()
            if existing is not None:
                self._assert_same_job(existing, job, successor)
                return self._job_public(existing, created=False)
            conn.execute(
                "INSERT INTO work_package_certification_jobs ("
                "id, batch_id, package_id, plan_version, epoch, repository_id, "
                "candidate_sha, candidate_tree_digest, candidate_ref, candidate_fence, "
                "assembly_base_sha, landing_base_sha, target_ref, policy_id, "
                "policy_version, policy_checksum, image_ref, image_digest, "
                "bundle_digest, commands_digest, job_digest, definition, state, "
                "lease_owner, lease_expires_at, lease_fence, result_digest, "
                "certification_id, created_at, updated_at, completed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, 'queued', NULL, NULL, 0, NULL, NULL, ?, ?, NULL)",
                (
                    job.job_id,
                    job.batch_id,
                    job.package_id,
                    job.plan_version,
                    job.epoch,
                    batch["repository_id"],
                    job.candidate_sha,
                    job.candidate_tree_digest,
                    batch["candidate_ref"],
                    int(batch["candidate_fence"]),
                    job.assembly_base_sha,
                    job.landing_base_sha,
                    job.target_ref,
                    job.policy.policy_id,
                    job.policy.version,
                    job.policy.checksum,
                    job.image_ref,
                    job.image_digest,
                    job.bundle_digest,
                    job.commands_digest,
                    job.job_digest,
                    json_dumps(definition),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM work_package_certification_jobs WHERE id = ?",
                (job.job_id,),
            ).fetchone()
        return self._job_public(row, created=True)

    def claim(
        self, job_id: str, *, owner: Optional[str] = None
    ) -> CertificationJobClaim:
        owner_value = self._required(owner or self.owner, "certification job owner")
        job_value = self._required(job_id, "certification job id")
        with self.store.transaction() as conn:
            locked = conn.execute(
                "UPDATE work_package_certification_jobs SET updated_at = updated_at "
                "WHERE id = ?",
                (job_value,),
            )
            if locked.rowcount != 1:
                raise ValidationError("certification job not found: %s" % job_value)
            row = conn.execute(
                "SELECT * FROM work_package_certification_jobs WHERE id = ?",
                (job_value,),
            ).fetchone()
            self._assert_persisted_job(row)
            if row["state"] in {"completed", "failed"}:
                raise TransitionError("certification job is already terminal")
            self._assert_certification_station_ready(conn, row)
            now = self._authority_now(conn)
            current_expiry = self._parse_time(row["lease_expires_at"])
            live = (
                row["state"] == "running"
                and current_expiry is not None
                and current_expiry > now
            )
            if live:
                if row["lease_owner"] != owner_value:
                    raise CertificationJobBusyError(
                        "certification job has a live owner"
                    )
                return CertificationJobClaim(
                    job_value,
                    owner_value,
                    int(row["lease_fence"]),
                    str(row["lease_expires_at"]),
                )
            fence = int(row["lease_fence"]) + 1
            definition = self._definition(row)
            commands = definition["job"]["controller_commands"]
            seconds = min(
                86_400,
                max(
                    300,
                    sum(int(item["timeout_seconds"]) + 60 for item in commands) + 300,
                ),
            )
            expires = self._iso(now + timedelta(seconds=seconds))
            changed = conn.execute(
                "UPDATE work_package_certification_jobs SET state = 'running', "
                "lease_owner = ?, lease_expires_at = ?, lease_fence = ?, updated_at = ? "
                "WHERE id = ? AND lease_fence = ? AND state IN ('queued', 'running')",
                (
                    owner_value,
                    expires,
                    fence,
                    self._iso(now),
                    job_value,
                    int(row["lease_fence"]),
                ),
            )
            if changed.rowcount != 1:
                raise CertificationJobBusyError("certification job claim raced")
        return CertificationJobClaim(job_value, owner_value, fence, expires)

    def run(
        self,
        job_id: str,
        bundle_path: Path,
        *,
        owner: Optional[str] = None,
        result_path: Optional[Path] = None,
    ) -> CertificationIngestionResult:
        """Run OpenShell externally and ingest under the exact claimed fence."""

        row = self._job_row(job_id)
        bundle = Path(bundle_path)
        job = self._job_from_row(row, bundle)
        self._validate_job(job)
        if self._sha256_file(bundle) != str(row["bundle_digest"]):
            raise ValidationError(
                "certification bundle does not match the prepared job"
            )
        try:
            runner = self._runtime_runner()
        except CertificationValidationError as exc:
            raise ValidationError("OpenShell certifier runtime is unavailable") from exc
        temporary = (
            tempfile.TemporaryDirectory(prefix="mac-certification-result-")
            if result_path is None
            else None
        )
        try:
            target = (
                Path(temporary.name) / "result.json"
                if temporary is not None
                else Path(result_path)
            )
            claim = self.claim(job_id, owner=owner)
            row = self._job_row(job_id)
            job = self._job_from_row(row, bundle)
            try:
                result = runner.run(job, result_path=target)
                payload = result.to_dict()
            except CertificationValidationError as exc:
                raise ValidationError("certification job or bundle is invalid") from exc
            except OSError as exc:
                # The absolute CLI may disappear or its gateway transport may
                # fail after the just-completed preflight.  Keep that race at
                # the fail-closed service boundary instead of surfacing a raw
                # process exception through the API. Expire only this exact
                # owner/fence so another controller can retry immediately;
                # a newer claim must remain untouched.
                self._expire_retryable_claim(claim)
                raise ValidationError(
                    "OpenShell certifier runtime became unavailable"
                ) from exc
            except CertificationCleanupError:
                if not target.is_file():
                    raise
                try:
                    loaded = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        "certifier cleanup failure produced no valid result"
                    ) from exc
                if not isinstance(loaded, dict):
                    raise ValidationError(
                        "certifier cleanup failure produced no valid result"
                    )
                payload = loaded
            return self.ingest(
                job_id,
                payload,
                owner=claim.owner,
                fence=claim.fence,
            )
        finally:
            if temporary is not None:
                temporary.cleanup()

    def _expire_retryable_claim(self, claim: CertificationJobClaim) -> bool:
        """Make this exact claim immediately reclaimable without fencing a successor."""

        with self.store.transaction() as conn:
            now = self._iso(self._authority_now(conn))
            changed = conn.execute(
                "UPDATE work_package_certification_jobs "
                "SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'running' AND lease_owner = ? "
                "AND lease_fence = ?",
                (now, now, claim.job_id, claim.owner, claim.fence),
            )
            return changed.rowcount == 1

    def _runtime_runner(self) -> OpenShellCertificationRunner:
        runner = self.runner
        if runner is None:
            runner = OpenShellCertificationRunner.from_environment()
            self.runner = runner
        elif self._runner_from_environment:
            # A production binding is cached so every station uses the same
            # absolute binary and service HOME, but readiness is not cached.
            # Re-probe at each activation/release gate and immediately before
            # each certification claim. Explicitly injected test/embedding
            # runners remain an intentional seam and need not expose preflight.
            runner.preflight()
        return runner

    def ingest(
        self,
        job_id: str,
        result: Mapping[str, Any] | OpenShellCertificationResult,
        *,
        owner: str,
        fence: int,
    ) -> CertificationIngestionResult:
        owner_value = self._required(owner, "certification result owner")
        fence_value = self._positive_int(fence, "certification result fence")
        row = self._job_row(job_id)
        payload = (
            dict(result.to_dict())
            if isinstance(result, OpenShellCertificationResult)
            else dict(result)
        )
        self._validate_result(row, payload)
        result_digest = str(payload["result_digest"])
        certification_id = (
            "wpcert_%s"
            % hashlib.sha256(
                (str(job_id) + "\0" + result_digest).encode("utf-8")
            ).hexdigest()[:32]
        )
        terminal = "completed" if payload["status"] == "passed" else "failed"
        certification_status = "passed" if terminal == "completed" else "failed"

        with self.store.transaction() as conn:
            locked = conn.execute(
                "UPDATE work_package_certification_jobs SET updated_at = updated_at "
                "WHERE id = ?",
                (job_id,),
            )
            if locked.rowcount != 1:
                raise ValidationError("certification job disappeared")
            current = conn.execute(
                "SELECT * FROM work_package_certification_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if current["state"] in {"completed", "failed"}:
                if (
                    current["result_digest"] == result_digest
                    and current["certification_id"] == certification_id
                ):
                    self._assert_terminal_certification(
                        conn,
                        current,
                        certification_id=certification_id,
                        result_digest=result_digest,
                        status=certification_status,
                    )
                    projection = self._terminal_station_projection(
                        conn,
                        current,
                        certification_id=certification_id,
                        expected_outcome=(
                            "certified"
                            if certification_status == "passed"
                            else "rejected"
                        ),
                    )
                    return CertificationIngestionResult(
                        job_id,
                        certification_id,
                        certification_status,
                        result_digest,
                        False,
                        projection["certification_task_id"],
                        projection["certification_node_key"],
                        projection["controller_station_receipt_id"],
                        projection["batch_state"],
                        projection["package_state"],
                    )
                raise TransitionError("terminal certification job result is immutable")
            now_dt = self._authority_now(conn)
            if (
                current["state"] != "running"
                or current["lease_owner"] != owner_value
                or int(current["lease_fence"]) != fence_value
                or self._parse_time(current["lease_expires_at"]) is None
                or self._parse_time(current["lease_expires_at"]) <= now_dt
            ):
                raise CertificationJobLeaseLostError(
                    "certification result does not hold the current job fence"
                )
            self._assert_job_row_identity(row, current)
            batch = conn.execute(
                "SELECT * FROM work_package_integration_batches WHERE id = ?",
                (current["batch_id"],),
            ).fetchone()
            self._assert_batch_matches_job(batch, current)
            station = self._assert_certification_station_ready(conn, current)
            task_id = str(station["task_id"] or "")
            if not task_id:
                raise ValidationError("certification job has no exact controller task")
            at = self._iso(now_dt)
            tests_evidence_id = (
                "ev_%s"
                % hashlib.sha256(
                    (job_id + "\0tests\0" + result_digest).encode("utf-8")
                ).hexdigest()[:32]
            )
            review_evidence_id = (
                "ev_%s"
                % hashlib.sha256(
                    (job_id + "\0review\0" + result_digest).encode("utf-8")
                ).hexdigest()[:32]
            )
            actor = "openshell-certifier:%s@%s" % (
                current["policy_id"],
                current["policy_version"],
            )
            evidence_metadata = json_dumps(
                {
                    "verification": payload,
                    "certification_job_id": job_id,
                    "certification_job_fence": fence_value,
                }
            )
            conn.execute(
                "INSERT INTO evidence (id, task_id, kind, uri, summary, checksum, "
                "metadata, created_by, created_at) VALUES (?, ?, 'test', ?, ?, ?, ?, ?, ?)",
                (
                    tests_evidence_id,
                    task_id,
                    "certification://%s/tests" % job_id,
                    "external OpenShell certification checks %s" % certification_status,
                    result_digest,
                    evidence_metadata,
                    actor,
                    at,
                ),
            )
            conn.execute(
                "INSERT INTO evidence (id, task_id, kind, uri, summary, checksum, "
                "metadata, created_by, created_at) VALUES (?, ?, 'review', ?, ?, ?, ?, ?, ?)",
                (
                    review_evidence_id,
                    task_id,
                    "certification://%s/result" % job_id,
                    "controller ingested exact fenced certifier result",
                    result_digest,
                    evidence_metadata,
                    actor,
                    at,
                ),
            )
            checks = (
                payload.get("checks") if isinstance(payload.get("checks"), list) else []
            )
            codegraph_evidence_id = (
                tests_evidence_id
                if any(
                    "codegraph" in str(item.get("command_id") or "").lower()
                    for item in checks
                    if isinstance(item, dict)
                )
                else None
            )
            conn.execute(
                "INSERT INTO work_package_certifications ("
                "id, batch_id, package_id, plan_version, epoch, candidate_sha, "
                "assembly_base_sha, landing_base_sha, target_ref, status, "
                "verification_digest, verification, certification_task_id, "
                "tests_evidence_id, review_task_id, review_evidence_id, "
                "codegraph_evidence_id, certified_by, created_at, invalidated_at, "
                "publication_id, publication_evidence_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, NULL)",
                (
                    certification_id,
                    current["batch_id"],
                    current["package_id"],
                    int(current["plan_version"]),
                    int(current["epoch"]),
                    current["candidate_sha"],
                    current["assembly_base_sha"],
                    current["landing_base_sha"],
                    current["target_ref"],
                    certification_status,
                    result_digest,
                    json_dumps(payload),
                    task_id,
                    tests_evidence_id,
                    task_id,
                    review_evidence_id,
                    codegraph_evidence_id,
                    actor,
                    at,
                ),
            )
            changed = conn.execute(
                "UPDATE work_package_certification_jobs SET state = ?, "
                "lease_owner = NULL, lease_expires_at = NULL, result_digest = ?, "
                "certification_id = ?, completed_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'running' AND lease_owner = ? "
                "AND lease_fence = ?",
                (
                    terminal,
                    result_digest,
                    certification_id,
                    at,
                    at,
                    job_id,
                    owner_value,
                    fence_value,
                ),
            )
            if changed.rowcount != 1:
                raise CertificationJobLeaseLostError(
                    "certification job fence changed during result commit"
                )
            receipt = self._record_certification_station_receipt(
                conn,
                job=current,
                station=station,
                certification_id=certification_id,
                result_digest=result_digest,
                phase_manifest=payload.get("phase_manifest") or {},
                outcome=(
                    "certified" if certification_status == "passed" else "rejected"
                ),
                actor=actor,
                now=at,
            )
            projection = self._project_certification_station(
                conn,
                job=current,
                batch=batch,
                station=station,
                receipt=receipt,
                certification_id=certification_id,
                certification_status=certification_status,
                actor=actor,
                now=at,
            )
        return CertificationIngestionResult(
            job_id,
            certification_id,
            certification_status,
            result_digest,
            True,
            projection["certification_task_id"],
            projection["certification_node_key"],
            projection["controller_station_receipt_id"],
            projection["batch_state"],
            projection["package_state"],
        )

    def get(self, job_id: str) -> JsonDict:
        return self._job_public(self._job_row(job_id), created=False)

    def list(
        self,
        *,
        state: Optional[str] = None,
        batch_ids: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> Tuple[JsonDict, ...]:
        limit_value = max(1, min(int(limit), 1000))
        batch_values = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in (batch_ids or ())
                    if str(value).strip()
                }
            )
        )
        if batch_ids is not None and not batch_values:
            return ()
        if batch_values:
            placeholders = ", ".join("?" for _value in batch_values)
            clauses = ["batch_id IN (%s)" % placeholders]
            params: list[Any] = list(batch_values)
            if state:
                clauses.append("state = ?")
                params.append(str(state))
            params.append(limit_value)
            rows = self.store.query_all(
                "SELECT * FROM work_package_certification_jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, id LIMIT ?",
                tuple(params),
            )
        elif state:
            rows = self.store.query_all(
                "SELECT * FROM work_package_certification_jobs WHERE state = ? "
                "ORDER BY created_at, id LIMIT ?",
                (str(state), limit_value),
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM work_package_certification_jobs "
                "ORDER BY created_at, id LIMIT ?",
                (limit_value,),
            )
        return tuple(self._job_public(row, created=False) for row in rows)

    def reject_failed_certification(
        self,
        batch_id: str,
        *,
        certification_id: str,
        actor: str,
    ) -> JsonDict:
        """Read back the atomic failed-certification Andon disposition.

        Failed result ingestion performs the transaction.  This deliberately
        idempotent station boundary lets a pipeline recover after response loss
        without replaying or weakening the exact job/result fence.
        """

        batch_value = self._required(batch_id, "failed certification batch id")
        certification_value = self._required(
            certification_id, "failed certification id"
        )
        self._required(actor, "failed certification actor")
        with self.store.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM work_package_certification_jobs "
                "WHERE batch_id = ? AND certification_id = ?",
                (batch_value, certification_value),
            ).fetchone()
            if job is None or job["state"] != "failed":
                raise TransitionError(
                    "batch has no exact terminal failed certification job"
                )
            self._assert_terminal_certification(
                conn,
                job,
                certification_id=certification_value,
                result_digest=str(job["result_digest"]),
                status="failed",
            )
            projection = self._terminal_station_projection(
                conn,
                job,
                certification_id=certification_value,
                expected_outcome="rejected",
            )
            if (
                projection["batch_state"] != "rejected"
                or projection["package_state"] != "paused"
                or projection["held_wip_count"] != 0
                or projection["wip_disposition"] != "quarantined"
                or projection["andon_recorded"] is not True
            ):
                raise TransitionError(
                    "failed certification has no complete Andon disposition"
                )
            return {
                "status": "completed",
                "batch_id": batch_value,
                "batch_state": "rejected",
                "certification_id": certification_value,
                "provenance_verified": True,
                "andon_recorded": True,
                "package_state": "paused",
                "wip_disposition": "quarantined",
                "held_wip_count": 0,
                "integration_task_id": projection["integration_task_id"],
                "certification_task_id": projection["certification_task_id"],
                "integration_node_state": projection["integration_node_state"],
                "certification_node_state": projection["certification_node_state"],
                "controller_station_receipt_id": projection[
                    "controller_station_receipt_id"
                ],
            }

    def _certification_successor(
        self,
        source: Any,
        batch: Mapping[str, Any],
    ) -> JsonDict:
        package = self._query_one(
            source,
            "SELECT package.state AS package_state, package.current_plan_version, "
            "package.current_epoch, epoch.status AS epoch_status, plan.definition "
            "FROM work_packages AS package "
            "JOIN work_package_epochs AS epoch ON epoch.package_id = package.id "
            "AND epoch.plan_version = package.current_plan_version "
            "AND epoch.epoch = package.current_epoch "
            "JOIN work_package_plan_versions AS plan ON plan.package_id = package.id "
            "AND plan.version = package.current_plan_version "
            "WHERE package.id = ?",
            (batch["package_id"],),
        )
        if (
            package is None
            or package["package_state"] != "active"
            or package["epoch_status"] != "active"
            or int(package["current_plan_version"]) != int(batch["plan_version"])
            or int(package["current_epoch"]) != int(batch["epoch"])
        ):
            raise TransitionError(
                "certification batch is not in the current active package epoch"
            )
        try:
            definition = json_loads(package["definition"], None)
        except (TypeError, ValueError) as exc:
            raise ValidationError("work-package plan definition is malformed") from exc
        nodes = definition.get("nodes") if isinstance(definition, Mapping) else None
        if not isinstance(nodes, list):
            raise ValidationError("work-package plan has no exact node list")
        node_by_key = {
            str(item.get("node_key") or ""): item
            for item in nodes
            if isinstance(item, Mapping) and item.get("node_key")
        }
        if len(node_by_key) != len(nodes):
            raise ValidationError("work-package plan node identities are malformed")

        integration_rows = self._query_all(
            source,
            "SELECT link.node_key, link.node_state, task.state AS task_state, "
            "task.metadata AS task_metadata, receipt.id AS receipt_id, "
            "receipt.provenance_digest AS receipt_digest, "
            "receipt.detail AS receipt_detail "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "JOIN work_package_controller_station_receipts AS receipt "
            "ON receipt.task_id = link.task_id "
            "AND receipt.package_id = link.package_id "
            "AND receipt.plan_version = link.plan_version "
            "AND receipt.epoch = link.epoch AND receipt.node_key = link.node_key "
            "WHERE link.task_id = ? AND link.package_id = ? "
            "AND link.plan_version = ? AND link.epoch = ? "
            "AND receipt.batch_id = ? AND receipt.station_kind = 'integration' "
            "AND receipt.outcome = 'integrated'",
            (
                batch["integration_task_id"],
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                batch["id"],
            ),
        )
        if len(integration_rows) != 1:
            raise TransitionError(
                "certification requires one exact integrated controller receipt"
            )
        integration = dict(integration_rows[0])
        integration_node = node_by_key.get(str(integration["node_key"]))
        if (
            integration["node_state"] != "integrated"
            or integration["task_state"] != "completed"
            or not isinstance(integration_node, Mapping)
            or integration_node.get("kind") != "integration"
        ):
            raise TransitionError(
                "certification integration predecessor is not durably complete"
            )
        self._require_task_projection(
            integration["task_metadata"],
            batch=batch,
            task_id=str(batch["integration_task_id"]),
            node_key=str(integration["node_key"]),
            node_type="integration",
        )

        matches = []
        integration_key = str(integration["node_key"])
        for node_key, node in node_by_key.items():
            depends_on = node.get("depends_on")
            if (
                node.get("kind") == "certification"
                and isinstance(depends_on, list)
                and integration_key in depends_on
            ):
                matches.append((node_key, node))
        if len(matches) != 1:
            raise ValidationError(
                "integration batch must name exactly one certification successor"
            )
        node_key, node = matches[0]
        if node.get("depends_on") != [integration_key]:
            raise ValidationError(
                "multi-batch certification fan-in lacks an exact candidate contract"
            )
        rows = self._query_all(
            source,
            "SELECT link.task_id, link.node_state, task.state AS task_state, "
            "task.metadata AS task_metadata, task.dependencies, "
            "task.owner_agent_id, task.lease_id "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.plan_version = ? AND link.epoch = ? "
            "AND link.node_key = ?",
            (
                batch["package_id"],
                int(batch["plan_version"]),
                int(batch["epoch"]),
                node_key,
            ),
        )
        if len(rows) != 1:
            raise ValidationError(
                "certification successor task is missing or ambiguous"
            )
        successor = dict(rows[0])
        if (
            successor["node_state"] != "ready"
            or successor["task_state"] != "waiting"
            or successor["owner_agent_id"] is not None
            or successor["lease_id"] is not None
            or json_loads(successor["dependencies"], None)
            != [str(batch["integration_task_id"])]
        ):
            raise TransitionError(
                "certification successor is not exact, ready, held, and unclaimed"
            )
        self._require_task_projection(
            successor["task_metadata"],
            batch=batch,
            task_id=str(successor["task_id"]),
            node_key=node_key,
            node_type="certification",
        )
        try:
            integration_detail = json_loads(integration["receipt_detail"], None)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "integration controller receipt detail is malformed"
            ) from exc
        expected_detail = {
            "schema": "mac.work_package.controller_station_receipt.v1",
            "station_kind": "integration",
            "batch_id": batch["id"],
            "integration_task_id": batch["integration_task_id"],
            "integration_node_key": integration_key,
            "certification_task_id": successor["task_id"],
            "certification_node_key": node_key,
            "candidate_sha": batch["candidate_sha"],
            "candidate_tree_digest": batch["candidate_tree_digest"],
            "candidate_ref": batch["candidate_ref"],
            "candidate_fence": int(batch["candidate_fence"]),
            "input_digest": batch["input_digest"],
        }
        integration_identity = {
            "station_kind": "integration",
            "task_id": batch["integration_task_id"],
            "package_id": batch["package_id"],
            "plan_version": int(batch["plan_version"]),
            "epoch": int(batch["epoch"]),
            "node_key": integration_key,
            "batch_id": batch["id"],
            "outcome": "integrated",
            "detail": expected_detail,
        }
        expected_receipt_digest = self._sha256_json(integration_identity)
        if (
            integration_detail != expected_detail
            or integration["receipt_digest"] != expected_receipt_digest
            or integration["receipt_id"]
            != "wpstation_%s" % expected_receipt_digest.split(":", 1)[1][:32]
            or not self._has_task_transition(
                source,
                task_id=str(batch["integration_task_id"]),
                receipt_id=str(integration["receipt_id"]),
                to_state="completed",
            )
        ):
            raise TransitionError(
                "integration controller receipt or task transition is incoherent"
            )
        return {
            "task_id": str(successor["task_id"]),
            "node_key": node_key,
            "integration_task_id": str(batch["integration_task_id"]),
            "integration_node_key": integration_key,
            "integration_station_receipt_id": str(integration["receipt_id"]),
            "integration_station_provenance_digest": str(integration["receipt_digest"]),
        }

    def _assert_certification_station_ready(
        self,
        conn: Any,
        job: Mapping[str, Any],
    ) -> JsonDict:
        batch = conn.execute(
            "SELECT * FROM work_package_integration_batches WHERE id = ?",
            (job["batch_id"],),
        ).fetchone()
        self._assert_batch_matches_job(batch, job)
        successor = self._certification_successor(conn, dict(batch))
        definition = self._definition(job)
        expected = (
            definition.get("certification_task_id"),
            definition.get("certification_node_key"),
            definition.get("integration_task_id"),
            definition.get("integration_node_key"),
            definition.get("integration_station_receipt_id"),
        )
        observed = (
            successor["task_id"],
            successor["node_key"],
            successor["integration_task_id"],
            successor["integration_node_key"],
            successor["integration_station_receipt_id"],
        )
        if observed != expected:
            raise TransitionError(
                "certification job no longer names its exact controller station"
            )
        return successor

    def _record_certification_station_receipt(
        self,
        conn: Any,
        *,
        job: Mapping[str, Any],
        station: Mapping[str, Any],
        certification_id: str,
        result_digest: str,
        phase_manifest: Mapping[str, Any],
        outcome: str,
        actor: str,
        now: str,
    ) -> JsonDict:
        detail = {
            "schema": "mac.work_package.controller_station_receipt.v1",
            "station_kind": "certification",
            "batch_id": job["batch_id"],
            "certification_job_id": job["id"],
            "certification_id": certification_id,
            "certification_task_id": station["task_id"],
            "certification_node_key": station["node_key"],
            "integration_task_id": station["integration_task_id"],
            "integration_node_key": station["integration_node_key"],
            "integration_station_receipt_id": station["integration_station_receipt_id"],
            "candidate_sha": job["candidate_sha"],
            "assembly_base_sha": job["assembly_base_sha"],
            "job_digest": job["job_digest"],
            "result_digest": result_digest,
            "phase_manifest_digest": phase_manifest.get("manifest_digest"),
            "selection_mode": phase_manifest.get("selection_mode"),
            "changed_files_digest": phase_manifest.get("changed_files_digest"),
            "full_suite_count": phase_manifest.get("full_suite_count"),
            "outcome": outcome,
        }
        identity = {
            "station_kind": "certification",
            "task_id": station["task_id"],
            "package_id": job["package_id"],
            "plan_version": int(job["plan_version"]),
            "epoch": int(job["epoch"]),
            "node_key": station["node_key"],
            "batch_id": job["batch_id"],
            "certification_job_id": job["id"],
            "certification_id": certification_id,
            "outcome": outcome,
            "result_digest": result_digest,
            "detail": detail,
        }
        provenance_digest = self._sha256_json(identity)
        receipt_id = "wpstation_%s" % provenance_digest.split(":", 1)[1][:32]
        conn.execute(
            "INSERT INTO work_package_controller_station_receipts ("
            "id, station_kind, task_id, package_id, plan_version, epoch, node_key, "
            "batch_id, certification_job_id, certification_id, outcome, "
            "result_digest, provenance_digest, actor, detail, created_at"
            ") VALUES (?, 'certification', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                station["task_id"],
                job["package_id"],
                int(job["plan_version"]),
                int(job["epoch"]),
                station["node_key"],
                job["batch_id"],
                job["id"],
                certification_id,
                outcome,
                result_digest,
                provenance_digest,
                actor,
                json_dumps(detail),
                now,
            ),
        )
        return {
            "id": receipt_id,
            "provenance_digest": provenance_digest,
            "outcome": outcome,
            "detail": detail,
        }

    def _project_certification_station(
        self,
        conn: Any,
        *,
        job: Mapping[str, Any],
        batch: Mapping[str, Any],
        station: Mapping[str, Any],
        receipt: Mapping[str, Any],
        certification_id: str,
        certification_status: str,
        actor: str,
        now: str,
    ) -> JsonDict:
        task_state = "completed" if certification_status == "passed" else "failed"
        node_state = "certified" if certification_status == "passed" else "rejected"
        changed = conn.execute(
            "UPDATE tasks SET state = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND state = 'waiting' AND owner_agent_id IS NULL "
            "AND lease_id IS NULL",
            (task_state, now, now, station["task_id"]),
        )
        if changed.rowcount != 1:
            raise TransitionError(
                "certification controller task changed during result commit"
            )
        changed = conn.execute(
            "UPDATE work_package_task_links SET node_state = ? "
            "WHERE task_id = ? AND package_id = ? AND plan_version = ? AND epoch = ? "
            "AND node_key = ? AND node_state = 'ready'",
            (
                node_state,
                station["task_id"],
                job["package_id"],
                int(job["plan_version"]),
                int(job["epoch"]),
                station["node_key"],
            ),
        )
        if changed.rowcount != 1:
            raise TransitionError(
                "certification controller link changed during result commit"
            )
        transition_detail = {
            "schema": "mac.work_package.controller_task_transition.v1",
            "station_kind": "certification",
            "batch_id": job["batch_id"],
            "certification_job_id": job["id"],
            "certification_id": certification_id,
            "controller_station_receipt_id": receipt["id"],
            "provenance_digest": receipt["provenance_digest"],
            "node_key": station["node_key"],
            "node_state": node_state,
            "assembly_base_sha": receipt["detail"].get("assembly_base_sha"),
            "phase_manifest_digest": receipt["detail"].get("phase_manifest_digest"),
            "selection_mode": receipt["detail"].get("selection_mode"),
            "changed_files_digest": receipt["detail"].get("changed_files_digest"),
            "full_suite_count": receipt["detail"].get("full_suite_count"),
        }
        self._append_task_transition(
            conn,
            task_id=str(station["task_id"]),
            actor=actor,
            from_state="waiting",
            to_state=task_state,
            detail=transition_detail,
            now=now,
        )

        batch_state = str(batch["state"])
        package_state = "active"
        if certification_status == "failed":
            cancelled = conn.execute(
                "UPDATE work_package_wip_tokens SET state = 'cancelled', "
                "released_at = ?, release_reason = ? WHERE package_id = ? "
                "AND plan_version = ? AND epoch = ? AND stage = 'integration' "
                "AND state = 'held' AND reservation_key = ?",
                (
                    now,
                    "certification_quarantine:%s" % certification_id,
                    job["package_id"],
                    int(job["plan_version"]),
                    int(job["epoch"]),
                    job["batch_id"],
                ),
            )
            metadata = json_loads(batch["metadata"], {}) or {}
            if not isinstance(metadata, dict):
                raise ValidationError("integration batch metadata is malformed")
            metadata["product_rejection"] = {
                "schema": "mac.work_package.product_rejection.v1",
                "status": "completed",
                "certification_job_id": job["id"],
                "certification_id": certification_id,
                "certification_task_id": station["task_id"],
                "controller_station_receipt_id": receipt["id"],
                "provenance_digest": receipt["provenance_digest"],
                "wip_disposition": "quarantined",
                "resolved_wip_count": int(cancelled.rowcount),
                "andon_recorded": True,
                "actor": actor,
                "at": now,
            }
            changed = conn.execute(
                "UPDATE work_package_integration_batches SET state = 'rejected', "
                "metadata = ?, completed_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'verifying' AND candidate_sha = ?",
                (
                    json_dumps(metadata),
                    now,
                    now,
                    job["batch_id"],
                    job["candidate_sha"],
                ),
            )
            if changed.rowcount != 1:
                raise TransitionError("failed certification batch rejection CAS failed")
            changed = conn.execute(
                "UPDATE work_packages SET state = 'paused', updated_at = ? "
                "WHERE id = ? AND state = 'active' AND current_plan_version = ? "
                "AND current_epoch = ?",
                (
                    now,
                    job["package_id"],
                    int(job["plan_version"]),
                    int(job["epoch"]),
                ),
            )
            if changed.rowcount != 1:
                raise TransitionError("failed certification package Andon CAS failed")
            self._append_package_history(
                conn,
                package_id=str(job["package_id"]),
                plan_version=int(job["plan_version"]),
                epoch=int(job["epoch"]),
                actor=actor,
                event_type="work_package.certification_rejected",
                detail={
                    **transition_detail,
                    "wip_disposition": "quarantined",
                    "resolved_wip_count": int(cancelled.rowcount),
                    "package_state": "paused",
                    "andon_recorded": True,
                },
                now=now,
            )
            batch_state = "rejected"
            package_state = "paused"
        return {
            "certification_task_id": str(station["task_id"]),
            "certification_node_key": str(station["node_key"]),
            "controller_station_receipt_id": str(receipt["id"]),
            "batch_state": batch_state,
            "package_state": package_state,
        }

    def _terminal_station_projection(
        self,
        conn: Any,
        job: Mapping[str, Any],
        *,
        certification_id: str,
        expected_outcome: str,
    ) -> JsonDict:
        definition = self._definition(job)
        task_id = str(definition.get("certification_task_id") or "")
        node_key = str(definition.get("certification_node_key") or "")
        receipt = conn.execute(
            "SELECT * FROM work_package_controller_station_receipts "
            "WHERE station_kind = 'certification' AND task_id = ? AND package_id = ? "
            "AND plan_version = ? AND epoch = ? AND node_key = ? AND batch_id = ? "
            "AND certification_job_id = ? AND certification_id = ? "
            "AND result_digest = ? AND outcome = ?",
            (
                task_id,
                job["package_id"],
                int(job["plan_version"]),
                int(job["epoch"]),
                node_key,
                job["batch_id"],
                job["id"],
                certification_id,
                job["result_digest"],
                expected_outcome,
            ),
        ).fetchone()
        if receipt is None:
            raise TransitionError(
                "terminal certification lacks its exact controller station receipt"
            )
        try:
            detail = json_loads(receipt["detail"], None)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "controller station receipt detail is malformed"
            ) from exc
        identity = {
            "station_kind": "certification",
            "task_id": task_id,
            "package_id": job["package_id"],
            "plan_version": int(job["plan_version"]),
            "epoch": int(job["epoch"]),
            "node_key": node_key,
            "batch_id": job["batch_id"],
            "certification_job_id": job["id"],
            "certification_id": certification_id,
            "outcome": expected_outcome,
            "result_digest": job["result_digest"],
            "detail": detail,
        }
        if not isinstance(detail, dict) or receipt[
            "provenance_digest"
        ] != self._sha256_json(identity):
            raise TransitionError("controller station receipt digest is incoherent")
        terminal_task_state = (
            "completed" if expected_outcome == "certified" else "failed"
        )
        station = conn.execute(
            "SELECT link.node_state, task.state AS task_state "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.task_id = ? AND link.package_id = ? AND link.plan_version = ? "
            "AND link.epoch = ? AND link.node_key = ?",
            (
                task_id,
                job["package_id"],
                int(job["plan_version"]),
                int(job["epoch"]),
                node_key,
            ),
        ).fetchone()
        integration = conn.execute(
            "SELECT link.node_state, task.state AS task_state, receipt.id "
            "FROM work_package_controller_station_receipts AS receipt "
            "JOIN work_package_task_links AS link ON link.task_id = receipt.task_id "
            "AND link.package_id = receipt.package_id "
            "AND link.plan_version = receipt.plan_version "
            "AND link.epoch = receipt.epoch AND link.node_key = receipt.node_key "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE receipt.id = ? AND receipt.batch_id = ? "
            "AND receipt.station_kind = 'integration' AND receipt.outcome = 'integrated'",
            (
                definition["integration_station_receipt_id"],
                job["batch_id"],
            ),
        ).fetchone()
        expected_node_state = expected_outcome
        if (
            station is None
            or station["node_state"] != expected_node_state
            or station["task_state"] != terminal_task_state
            or integration is None
            or integration["node_state"] != "integrated"
            or integration["task_state"] != "completed"
        ):
            raise TransitionError(
                "terminal certification task/link projection is incomplete"
            )
        if not self._has_task_transition(
            conn,
            task_id=task_id,
            receipt_id=str(receipt["id"]),
            to_state=terminal_task_state,
        ):
            raise TransitionError(
                "terminal certification lacks task history and outbox provenance"
            )
        batch = conn.execute(
            "SELECT state, metadata, integration_task_id FROM "
            "work_package_integration_batches WHERE id = ?",
            (job["batch_id"],),
        ).fetchone()
        package = conn.execute(
            "SELECT state FROM work_packages WHERE id = ?",
            (job["package_id"],),
        ).fetchone()
        if batch is None or package is None:
            raise TransitionError(
                "terminal certification package projection disappeared"
            )
        held_row = conn.execute(
            "SELECT COUNT(*) AS count FROM work_package_wip_tokens "
            "WHERE package_id = ? AND plan_version = ? AND epoch = ? "
            "AND stage = 'integration' AND state = 'held' AND reservation_key = ?",
            (
                job["package_id"],
                int(job["plan_version"]),
                int(job["epoch"]),
                job["batch_id"],
            ),
        ).fetchone()
        metadata = json_loads(batch["metadata"], {}) or {}
        rejection = (
            metadata.get("product_rejection") if isinstance(metadata, dict) else None
        )
        if expected_outcome == "certified":
            if batch["state"] not in {"verifying", "certified", "published"}:
                raise TransitionError(
                    "passed certification batch projection is invalid"
                )
            if package["state"] not in {"active", "completed"}:
                raise TransitionError(
                    "passed certification package projection is invalid"
                )
        else:
            if batch["state"] != "rejected" or package["state"] != "paused":
                raise TransitionError(
                    "failed certification Andon projection is invalid"
                )
            if (
                not isinstance(rejection, Mapping)
                or rejection.get("status") != "completed"
                or rejection.get("certification_id") != certification_id
                or rejection.get("controller_station_receipt_id") != receipt["id"]
                or rejection.get("wip_disposition") != "quarantined"
                or rejection.get("andon_recorded") is not True
            ):
                raise TransitionError(
                    "failed certification rejection receipt is incomplete"
                )
        return {
            "certification_task_id": task_id,
            "certification_node_key": node_key,
            "controller_station_receipt_id": str(receipt["id"]),
            "batch_state": str(batch["state"]),
            "package_state": str(package["state"]),
            "held_wip_count": int(held_row["count"]),
            "wip_disposition": (
                rejection.get("wip_disposition")
                if isinstance(rejection, Mapping)
                else None
            ),
            "andon_recorded": (
                rejection.get("andon_recorded") is True
                if isinstance(rejection, Mapping)
                else False
            ),
            "integration_task_id": str(batch["integration_task_id"]),
            "integration_node_state": str(integration["node_state"]),
            "certification_node_state": str(station["node_state"]),
        }

    @staticmethod
    def _require_task_projection(
        raw_metadata: Any,
        *,
        batch: Mapping[str, Any],
        task_id: str,
        node_key: str,
        node_type: str,
    ) -> None:
        try:
            metadata = json_loads(raw_metadata, None)
        except (TypeError, ValueError) as exc:
            raise ValidationError("controller task metadata is malformed") from exc
        projection = (
            metadata.get("work_package") if isinstance(metadata, dict) else None
        )
        try:
            exact = (
                isinstance(metadata, dict)
                and metadata.get("no_dispatch") is True
                and isinstance(projection, Mapping)
                and projection.get("package_id") == batch["package_id"]
                and int(projection.get("plan_version")) == int(batch["plan_version"])
                and int(projection.get("epoch")) == int(batch["epoch"])
                and projection.get("node_key") == node_key
                and projection.get("node_type") == node_type
                and bool(task_id)
            )
        except (TypeError, ValueError):
            exact = False
        if not exact:
            raise TransitionError(
                "%s controller task projection is not exact and held" % node_type
            )

    @staticmethod
    def _query_one(source: Any, sql: str, params: Tuple[Any, ...]) -> Any:
        if hasattr(source, "query_one"):
            return source.query_one(sql, params)
        return source.execute(sql, params).fetchone()

    @staticmethod
    def _query_all(source: Any, sql: str, params: Tuple[Any, ...]) -> list[Any]:
        if hasattr(source, "query_all"):
            return list(source.query_all(sql, params))
        return list(source.execute(sql, params).fetchall())

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
        encoded = json_dumps(dict(detail))
        conn.execute(
            "INSERT INTO task_history (id, task_id, event_type, actor, "
            "from_state, to_state, detail, created_at) "
            "VALUES (?, ?, 'task.transitioned', ?, ?, ?, ?, ?)",
            (
                new_id("history"),
                task_id,
                actor,
                from_state,
                to_state,
                encoded,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO task_transition_outbox (id, task_id, event_type, actor, "
            "from_state, to_state, detail, status, attempts, created_at, processed_at) "
            "VALUES (?, ?, 'task.lifecycle', ?, ?, ?, ?, 'pending', 0, ?, NULL)",
            (
                new_id("tout"),
                task_id,
                actor,
                from_state,
                to_state,
                encoded,
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
        event_type: str,
        detail: Mapping[str, Any],
        now: str,
    ) -> None:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM work_package_history WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO work_package_history (id, package_id, seq, event_type, "
            "actor, plan_version, epoch, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("wph"),
                package_id,
                int(seq["next_seq"]),
                event_type,
                actor,
                plan_version,
                epoch,
                json_dumps(dict(detail)),
                now,
            ),
        )

    @staticmethod
    def _has_task_transition(
        conn: Any,
        *,
        task_id: str,
        receipt_id: str,
        to_state: str,
    ) -> bool:
        history_rows = conn.execute(
            "SELECT detail FROM task_history WHERE task_id = ? "
            "AND event_type = 'task.transitioned' AND to_state = ?",
            (task_id, to_state),
        ).fetchall()
        outbox_rows = conn.execute(
            "SELECT detail FROM task_transition_outbox WHERE task_id = ? "
            "AND event_type = 'task.lifecycle' AND to_state = ?",
            (task_id, to_state),
        ).fetchall()

        def matches(rows: Any) -> bool:
            for row in rows:
                try:
                    detail = json_loads(row["detail"], {}) or {}
                except (TypeError, ValueError):
                    continue
                if detail.get("controller_station_receipt_id") == receipt_id:
                    return True
            return False

        return matches(history_rows) and matches(outbox_rows)

    def _job_from_values(
        self,
        job_id: str,
        batch: Mapping[str, Any],
        contract: Mapping[str, Any],
        bundle_path: Path,
        bundle_digest: str,
    ) -> OpenShellCertificationJob:
        policy = contract["policy"]
        return OpenShellCertificationJob(
            job_id=job_id,
            batch_id=str(batch["id"]),
            package_id=str(batch["package_id"]),
            plan_version=int(batch["plan_version"]),
            epoch=int(batch["epoch"]),
            candidate_sha=str(batch["candidate_sha"]),
            candidate_tree_digest=str(batch["candidate_tree_digest"]),
            assembly_base_sha=str(batch["assembly_base_sha"]),
            landing_base_sha=str(batch["landing_base_sha"]),
            target_ref=str(batch["target_ref"]),
            policy=CertificationPolicy(
                str(policy["policy_id"]),
                int(policy["version"]),
                str(policy["checksum"]),
                str(contract["policy_text"]),
            ),
            phase_profile=CertifierPhaseProfile.from_mapping(contract["phase_profile"]),
            image_ref=str(contract["image_ref"]),
            bundle_path=Path(bundle_path),
            bundle_digest=bundle_digest,
            controller_commands=tuple(
                ControllerCommand(
                    str(item["command_id"]),
                    tuple(str(value) for value in item["argv"]),
                    int(item["timeout_seconds"]),
                )
                for item in contract["controller_commands"]
            ),
        )

    def _job_from_row(
        self, row: Mapping[str, Any], bundle_path: Path
    ) -> OpenShellCertificationJob:
        definition = self._definition(row)
        job_value = definition["job"]
        contract = {
            "policy": job_value["policy"],
            "policy_text": definition["policy_text"],
            "phase_profile": job_value["phase_profile"],
            "image_ref": job_value["image_ref"],
            "controller_commands": job_value["controller_commands"],
        }
        batch = {
            "id": row["batch_id"],
            "package_id": row["package_id"],
            "plan_version": row["plan_version"],
            "epoch": row["epoch"],
            "candidate_sha": row["candidate_sha"],
            "candidate_tree_digest": row["candidate_tree_digest"],
            "assembly_base_sha": row["assembly_base_sha"],
            "landing_base_sha": row["landing_base_sha"],
            "target_ref": row["target_ref"],
        }
        job = self._job_from_values(
            str(row["id"]), batch, contract, bundle_path, str(row["bundle_digest"])
        )
        if job.job_digest != row["job_digest"]:
            raise ValidationError("persisted certification job digest is incoherent")
        return job

    def _validate_result(
        self, row: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None:
        try:
            encoded = json_dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError("certification result is not canonical JSON") from exc
        if len(encoded) > 2 * 1024 * 1024:
            raise ValidationError("certification result exceeds the ingestion limit")
        if set(payload) != _RESULT_KEYS:
            raise ValidationError(
                "certification result fields do not match the contract"
            )
        if payload.get("schema") != CERTIFICATION_RESULT_SCHEMA:
            raise ValidationError("certification result schema is invalid")
        claimed_digest = str(payload.get("result_digest") or "")
        unsigned = dict(payload)
        unsigned.pop("result_digest", None)
        if claimed_digest != self._sha256_json(unsigned):
            raise ValidationError("certification result digest does not match payload")
        expected = {
            "job_id": row["id"],
            "job_digest": row["job_digest"],
            "batch_id": row["batch_id"],
            "package_id": row["package_id"],
            "plan_version": int(row["plan_version"]),
            "epoch": int(row["epoch"]),
            "candidate_sha": row["candidate_sha"],
            "candidate_tree_digest": row["candidate_tree_digest"],
            "assembly_base_sha": row["assembly_base_sha"],
            "landing_base_sha": row["landing_base_sha"],
            "target_ref": row["target_ref"],
            "image_ref": row["image_ref"],
            "image_digest": row["image_digest"],
            "bundle_digest": row["bundle_digest"],
            "commands_digest": row["commands_digest"],
        }
        for name in ("plan_version", "epoch"):
            if isinstance(payload.get(name), bool) or not isinstance(
                payload.get(name), int
            ):
                raise ValidationError(
                    "certification result numeric identity is invalid"
                )
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValidationError(
                "certification result identity does not match its job"
            )
        if payload.get("status") not in {"passed", "failed"}:
            raise ValidationError("certification result status is invalid")
        policy = payload.get("policy")
        expected_policy = {
            "policy_id": row["policy_id"],
            "version": int(row["policy_version"]),
            "checksum": row["policy_checksum"],
        }
        if (
            not isinstance(policy, Mapping)
            or set(policy) != set(expected_policy)
            or isinstance(policy.get("version"), bool)
            or not isinstance(policy.get("version"), int)
            or policy != expected_policy
        ):
            raise ValidationError("certification result policy identity changed")
        isolation = payload.get("isolation")
        required_isolation = {
            "schema": CERTIFICATION_ISOLATION_SCHEMA,
            "network": "disabled",
            "landing_credentials": "absent",
            "planner_commands": "rejected",
            "policy_source": "trusted_controller",
            "policy_id": row["policy_id"],
            "policy_version": int(row["policy_version"]),
            "policy_checksum": row["policy_checksum"],
            "landlock": "hard_requirement",
            "run_as_user": "non_root",
            "input_format": "credential_free_git_bundle",
            "assembly_base_transport": "controller_bound_argv",
        }
        if (
            not isinstance(isolation, Mapping)
            or isinstance(isolation.get("policy_version"), bool)
            or not isinstance(isolation.get("policy_version"), int)
            or any(
                isolation.get(key) != value for key, value in required_isolation.items()
            )
        ):
            raise ValidationError("certification isolation attestation is incomplete")
        if set(isolation) != _ISOLATION_KEYS:
            raise ValidationError("certification isolation fields are not exact")
        environment = isolation.get("launcher_environment")
        if (
            not isinstance(environment, list)
            or not environment
            or environment != sorted(set(environment))
            or any(
                not isinstance(name, str) or name not in _SAFE_LAUNCH_ENV_NAMES
                for name in environment
            )
        ):
            raise ValidationError("certification launcher environment is invalid")
        definition = self._definition(row)
        try:
            phase_profile = CertifierPhaseProfile.from_mapping(
                definition["job"]["phase_profile"]
            )
        except (KeyError, TypeError, CertificationValidationError) as exc:
            raise ValidationError(
                "persisted certifier phase profile is invalid"
            ) from exc
        phase_manifest = payload.get("phase_manifest")
        if not isinstance(phase_manifest, Mapping):
            raise ValidationError("certifier phase manifest is malformed")
        if phase_manifest:
            try:
                validate_certifier_phase_manifest(
                    phase_manifest,
                    assembly_base_sha=str(row["assembly_base_sha"]),
                    candidate_sha=str(row["candidate_sha"]),
                    phase_profile=phase_profile,
                )
            except CertificationValidationError as exc:
                raise ValidationError("certifier phase manifest is invalid") from exc
        elif payload.get("status") == "passed":
            raise ValidationError("passed certification lacks a phase manifest")
        if (
            payload.get("status") == "passed"
            and phase_manifest.get("authoritative", {}).get("mode") == "rejected"
        ):
            raise ValidationError("passed certification used a rejected test selection")
        commands = definition["job"]["controller_commands"]
        checks = payload.get("checks")
        if not isinstance(checks, list):
            raise ValidationError("certification result checks are malformed")
        if len(checks) > len(commands):
            raise ValidationError("certification result has unexpected commands")
        for index, check in enumerate(checks):
            if not isinstance(check, Mapping):
                raise ValidationError("certification check is malformed")
            if set(check) != _CHECK_KEYS:
                raise ValidationError("certification check fields are not exact")
            command = commands[index]
            if (
                check.get("command_id") != command["command_id"]
                or check.get("argv") != command["argv"]
            ):
                raise ValidationError("certification result command identity changed")
            returncode = check.get("returncode")
            if isinstance(returncode, bool) or not isinstance(returncode, int):
                raise ValidationError("certification check returncode is invalid")
            timed_out = check.get("timed_out")
            if not isinstance(timed_out, bool):
                raise ValidationError("certification check timeout flag is invalid")
            status = check.get("status")
            if status not in {"pass", "fail"} or (returncode == 0) != (
                status == "pass"
            ):
                raise ValidationError("certification check status is incoherent")
            if timed_out and returncode != 124:
                raise ValidationError(
                    "timed-out certification check has wrong returncode"
                )
            for output_name in ("stdout", "stderr"):
                output = check.get(output_name)
                if not isinstance(output, str) or len(output) > 16_000:
                    raise ValidationError("certification check output is invalid")

        if not isinstance(payload.get("started_at"), str) or not isinstance(
            payload.get("completed_at"), str
        ):
            raise ValidationError("certification result timestamps are invalid")
        started = self._parse_time(payload.get("started_at"))
        completed = self._parse_time(payload.get("completed_at"))
        if started is None or completed is None or completed < started:
            raise ValidationError("certification result timestamps are invalid")
        if not re.fullmatch(
            r"mac-cert-[A-Za-z0-9._-]{1,80}", str(payload.get("sandbox_name") or "")
        ):
            raise ValidationError("certification sandbox identity is invalid")
        cleanup = payload.get("cleanup_status")
        if cleanup not in {"deleted", "failed"}:
            raise ValidationError("certification cleanup status is not terminal")
        failure_class = payload.get("failure_class")
        failure_reason = payload.get("failure_reason")
        if (
            not isinstance(failure_class, str)
            or not isinstance(failure_reason, str)
            or len(failure_class) > 256
            or len(failure_reason) > 16_000
        ):
            raise ValidationError("certification failure detail is invalid")
        if payload["status"] == "passed":
            if (
                len(checks) != len(commands)
                or any(item["status"] != "pass" for item in checks)
                or cleanup != "deleted"
                or failure_class
                or failure_reason
            ):
                raise ValidationError(
                    "passed certification lacks complete successful checks"
                )
        elif not failure_class or not failure_reason:
            raise ValidationError("failed certification lacks a classified reason")
        if cleanup == "failed" and failure_class != "sandbox_cleanup_failed":
            raise ValidationError("failed cleanup lacks its exact failure class")

    def _certification_contract(
        self, repository_id: str, *, source: Optional[Any] = None
    ) -> JsonDict:
        query = "SELECT metadata FROM project_repositories WHERE id = ? AND enabled = 1"
        if source is None:
            row = self.store.query_one(query, (repository_id,))
        else:
            row = source.execute(query, (repository_id,)).fetchone()
        if row is None:
            raise ValidationError("certification repository is unavailable")
        try:
            metadata = json_loads(row["metadata"], {}) or {}
        except (TypeError, ValueError) as exc:
            raise ValidationError("repository metadata is malformed") from exc
        if not isinstance(metadata, Mapping):
            raise ValidationError("repository metadata is malformed")
        repository_contract = metadata.get("repository_contract")
        if not isinstance(repository_contract, Mapping):
            raise ValidationError("repository has no certification contract")
        return normalize_repository_certification_contract(repository_contract)

    def _lock_repository_contract(self, conn: Any, repository_id: str) -> JsonDict:
        locked = conn.execute(
            "UPDATE project_repositories SET updated_at = updated_at "
            "WHERE id = ? AND enabled = 1",
            (repository_id,),
        )
        if locked.rowcount != 1:
            raise TransitionError("certification repository became unavailable")
        return self._certification_contract(repository_id, source=conn)

    def _batch(self, batch_id: str) -> JsonDict:
        row = self.store.query_one(
            "SELECT * FROM work_package_integration_batches WHERE id = ?",
            (self._required(batch_id, "certification batch id"),),
        )
        if row is None:
            raise ValidationError("integration batch not found: %s" % batch_id)
        value = dict(row)
        required = (
            "repository_id",
            "candidate_sha",
            "candidate_tree_digest",
            "candidate_ref",
            "candidate_fence",
            "integration_task_id",
        )
        if any(value.get(name) in {None, ""} for name in required):
            raise ValidationError("integration batch has no exact fenced candidate")
        return value

    def _lock_batch(self, conn: Any, expected: Mapping[str, Any]) -> None:
        changed = conn.execute(
            "UPDATE work_package_integration_batches SET updated_at = updated_at "
            "WHERE id = ?",
            (expected["id"],),
        )
        if changed.rowcount != 1:
            raise TransitionError("integration batch disappeared")
        current = conn.execute(
            "SELECT * FROM work_package_integration_batches WHERE id = ?",
            (expected["id"],),
        ).fetchone()
        self._assert_batch_matches_job(current, expected)

    @staticmethod
    def _assert_batch_matches_job(batch: Any, job: Mapping[str, Any]) -> None:
        if batch is None:
            raise TransitionError("integration batch disappeared")
        batch_value = dict(batch)
        job_value = dict(job)
        try:
            expected = (
                job_value.get("batch_id") or job_value["id"],
                job_value["package_id"],
                int(job_value["plan_version"]),
                int(job_value["epoch"]),
                job_value["repository_id"],
                job_value["candidate_sha"],
                job_value["candidate_tree_digest"],
                job_value["candidate_ref"],
                int(job_value["candidate_fence"]),
                job_value["assembly_base_sha"],
                job_value["landing_base_sha"],
                job_value["target_ref"],
                "verifying",
            )
            observed = (
                batch_value["id"],
                batch_value["package_id"],
                int(batch_value["plan_version"]),
                int(batch_value["epoch"]),
                batch_value["repository_id"],
                batch_value["candidate_sha"],
                batch_value["candidate_tree_digest"],
                batch_value["candidate_ref"],
                int(batch_value["candidate_fence"]),
                batch_value["assembly_base_sha"],
                batch_value["landing_base_sha"],
                batch_value["target_ref"],
                batch_value["state"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransitionError(
                "certification batch or job identity is malformed"
            ) from exc
        if observed != expected:
            raise TransitionError("certification job no longer matches the exact batch")

    @staticmethod
    def _assert_job_row_identity(
        before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> None:
        fields = (
            "id",
            "batch_id",
            "package_id",
            "plan_version",
            "epoch",
            "repository_id",
            "candidate_sha",
            "candidate_tree_digest",
            "candidate_ref",
            "candidate_fence",
            "assembly_base_sha",
            "landing_base_sha",
            "target_ref",
            "policy_id",
            "policy_version",
            "policy_checksum",
            "image_ref",
            "image_digest",
            "bundle_digest",
            "commands_digest",
            "job_digest",
            "definition",
        )
        if any(before[name] != after[name] for name in fields):
            raise TransitionError("certification job identity changed")

    def _assert_same_job(
        self,
        row: Mapping[str, Any],
        job: OpenShellCertificationJob,
        successor: Mapping[str, Any],
    ) -> None:
        self._assert_persisted_job(row)
        definition = self._definition(row)
        if (
            row["id"] != job.job_id
            or row["job_digest"] != job.job_digest
            or row["bundle_digest"] != job.bundle_digest
            or definition["job"] != job.identity()
            or definition["certification_task_id"] != successor["task_id"]
            or definition["certification_node_key"] != successor["node_key"]
            or definition["integration_task_id"] != successor["integration_task_id"]
            or definition["integration_node_key"] != successor["integration_node_key"]
            or definition["integration_station_receipt_id"]
            != successor["integration_station_receipt_id"]
        ):
            raise TransitionError("existing certification job identity differs")

    def _assert_persisted_job(self, row: Mapping[str, Any]) -> None:
        definition = self._definition(row)
        expected_definition_keys = {
            "schema",
            "job",
            "policy_text",
            "candidate_ref",
            "candidate_fence",
            "repository_id",
            "integration_task_id",
            "integration_node_key",
            "integration_station_receipt_id",
            "certification_task_id",
            "certification_node_key",
            "prepared_by",
        }
        if set(definition) != expected_definition_keys:
            raise ValidationError("certification job definition fields are not exact")
        job = self._job_from_row(row, Path("unused.bundle"))
        self._validate_job(job)
        if (
            definition["job"] != job.identity()
            or definition["candidate_ref"] != row["candidate_ref"]
            or definition["candidate_fence"] != int(row["candidate_fence"])
            or definition["repository_id"] != row["repository_id"]
            or not str(definition["integration_task_id"] or "").strip()
            or not str(definition["integration_node_key"] or "").strip()
            or not str(definition["integration_station_receipt_id"] or "").strip()
            or not str(definition["certification_task_id"] or "").strip()
            or not str(definition["certification_node_key"] or "").strip()
            or not str(definition["prepared_by"] or "").strip()
            or job.image_digest != row["image_digest"]
            or job.commands_digest != row["commands_digest"]
        ):
            raise ValidationError("persisted certification job identity is incoherent")

    @staticmethod
    def _assert_terminal_certification(
        conn: Any,
        job: Mapping[str, Any],
        *,
        certification_id: str,
        result_digest: str,
        status: str,
    ) -> None:
        try:
            definition = json_loads(job["definition"], None)
        except (TypeError, ValueError) as exc:
            raise ValidationError("certification job definition is malformed") from exc
        task_id = (
            str(definition.get("certification_task_id") or "")
            if isinstance(definition, Mapping)
            else ""
        )
        row = conn.execute(
            "SELECT id FROM work_package_certifications WHERE id = ? "
            "AND batch_id = ? AND package_id = ? AND plan_version = ? AND epoch = ? "
            "AND candidate_sha = ? AND assembly_base_sha = ? "
            "AND landing_base_sha = ? AND target_ref = ? "
            "AND verification_digest = ? AND certification_task_id = ? AND ("
            "(? = 'passed' AND status IN ('passed', 'published', 'invalidated')) OR "
            "(? = 'failed' AND status IN ('failed', 'invalidated')))",
            (
                certification_id,
                job["batch_id"],
                job["package_id"],
                int(job["plan_version"]),
                int(job["epoch"]),
                job["candidate_sha"],
                job["assembly_base_sha"],
                job["landing_base_sha"],
                job["target_ref"],
                result_digest,
                task_id,
                status,
                status,
            ),
        ).fetchone()
        if row is None:
            raise TransitionError(
                "terminal certification job has no exact certification record"
            )

    def _job_row(self, job_id: str) -> Any:
        row = self.store.query_one(
            "SELECT * FROM work_package_certification_jobs WHERE id = ?",
            (self._required(job_id, "certification job id"),),
        )
        if row is None:
            raise ValidationError("certification job not found: %s" % job_id)
        return row

    @staticmethod
    def _definition(row: Mapping[str, Any]) -> JsonDict:
        try:
            value = json_loads(row["definition"], {})
        except (TypeError, ValueError) as exc:
            raise ValidationError("certification job definition is malformed") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != CERTIFICATION_JOB_RECORD_SCHEMA
        ):
            raise ValidationError("certification job definition is malformed")
        return value

    def _job_public(self, row: Mapping[str, Any], *, created: bool) -> JsonDict:
        self._assert_persisted_job(row)
        definition = self._definition(row)
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "package_id": row["package_id"],
            "plan_version": int(row["plan_version"]),
            "epoch": int(row["epoch"]),
            "candidate_sha": row["candidate_sha"],
            "candidate_tree_digest": row["candidate_tree_digest"],
            "candidate_ref": row["candidate_ref"],
            "candidate_fence": int(row["candidate_fence"]),
            "integration_task_id": definition["integration_task_id"],
            "integration_node_key": definition["integration_node_key"],
            "integration_station_receipt_id": definition[
                "integration_station_receipt_id"
            ],
            "certification_task_id": definition["certification_task_id"],
            "certification_node_key": definition["certification_node_key"],
            "policy_id": row["policy_id"],
            "policy_version": int(row["policy_version"]),
            "image_digest": row["image_digest"],
            "bundle_digest": row["bundle_digest"],
            "commands_digest": row["commands_digest"],
            "job_digest": row["job_digest"],
            "state": row["state"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "lease_fence": int(row["lease_fence"]),
            "result_digest": row["result_digest"],
            "certification_id": row["certification_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "created": created,
        }

    @staticmethod
    def _required(value: Any, label: str) -> str:
        result = str(value or "").strip()
        if not result:
            raise ValidationError("%s is required" % label)
        return result

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("%s must be a positive integer" % label)
        if value < 1:
            raise ValidationError("%s must be a positive integer" % label)
        return value

    @staticmethod
    def _validate_job(job: OpenShellCertificationJob) -> None:
        try:
            job.validate()
        except CertificationValidationError as exc:
            raise ValidationError("certification job is invalid") from exc

    @staticmethod
    def _sha256_json(value: Any) -> str:
        return (
            "sha256:%s" % hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with Path(path).open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        except OSError as exc:
            raise ValidationError("certification bundle is unavailable") from exc
        return "sha256:%s" % digest.hexdigest()

    def _authority_now(self, conn: Any) -> datetime:
        """The clock the lease fence is judged against.

        In production this is the *database's* clock, so that concurrent hub
        processes agree on whether a lease is still live no matter how their
        host clocks drift.

        A caller-injected clock overrides it, and must: the fence tests advance
        time to prove a stale lease becomes reclaimable, and there is no way to
        move the server's clock_timestamp(). This used to work by accident --
        the branch below was reached whenever the store was not Postgres, which
        under the old SQLite test backend was always. With the suite on
        Postgres that fallback stopped applying, time travel silently stopped
        working, and the affected tests failed with "live owner" because the
        lease never appeared to expire. Keying on the injected clock states the
        seam instead of inferring it from the backend.
        """
        if self._clock_injected or type(self.store).__module__ != "mac.store_postgres":
            return self._now().astimezone(timezone.utc)
        row = conn.execute(
            "SELECT clock_timestamp() AS authoritative_now"
        ).fetchone()
        if row is None:
            raise ValidationError("certification authority clock is unavailable")
        return self._parse_time(row["authoritative_now"]) or self._now()

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "CERTIFICATION_CONTRACT_SCHEMA",
    "CERTIFICATION_INGESTION_SCHEMA",
    "CERTIFICATION_JOB_RECORD_SCHEMA",
    "CertificationIngestionResult",
    "CertificationJobBusyError",
    "CertificationJobClaim",
    "CertificationJobLeaseLostError",
    "WorkPackageCertificationService",
    "normalize_repository_certification_contract",
]
