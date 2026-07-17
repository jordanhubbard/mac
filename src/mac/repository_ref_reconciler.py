from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from mac.models import new_id
from mac.repository_hygiene import (
    DEFAULT_CLEANUP_GRACE_SECONDS,
    RepositoryHygieneError,
    RepositoryRefAudit,
    audit_repository_refs,
    cleanup_evidence_metadata,
    list_managed_remote_refs,
    prune_repository_refs,
    query_open_pull_requests,
    redact_repository_hygiene_text,
    refresh_remote_base_ref,
    resolve_remote_base_ref,
    retire_protected_remote_ref_exact,
    verify_repository_remote,
)


REPOSITORY_REF_RECONCILER_SCHEMA = "mac.repository_ref_reconciler.v1"
RECONCILER_MODES = frozenset({"off", "audit", "prune"})
MIN_INTERVAL_SECONDS = 60.0
MAX_INTERVAL_SECONDS = 7 * 24 * 60 * 60.0
MAX_INITIAL_DELAY_SECONDS = 24 * 60 * 60.0
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_log = logging.getLogger("mac.repository_ref_reconciler")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_error(error: Any) -> str:
    return redact_repository_hygiene_text(error).strip()[:500]


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class _ProtectedRefAuthority:
    repository_id: str
    ref_kind: str
    ref: str
    expected_sha: str
    terminal_state: str
    terminal_at: str
    eligible_after: str
    task_id: Optional[str] = None
    batch_id: Optional[str] = None


def _number(
    environ: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    raw = str(environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append("%s must be numeric" % name)
        return default
    if value < minimum or value > maximum:
        errors.append("%s must be between %s and %s" % (name, minimum, maximum))
        return default
    return value


@dataclass(frozen=True)
class RepositoryRefReconcilerConfig:
    mode: str = "off"
    interval_seconds: float = 24 * 60 * 60.0
    initial_delay_seconds: float = 300.0
    default_grace_seconds: int = DEFAULT_CLEANUP_GRACE_SECONDS
    remote: str = "origin"
    base_ref: str = ""
    configuration_error: str = ""

    @property
    def enabled(self) -> bool:
        return not self.configuration_error and self.mode in {"audit", "prune"}

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "enabled": self.enabled}

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "RepositoryRefReconcilerConfig":
        env = os.environ if environ is None else environ
        errors: list[str] = []
        mode = (
            str(env.get("MAC_REPOSITORY_REF_RECONCILER_MODE") or "off").strip().lower()
        )
        if mode not in RECONCILER_MODES:
            errors.append(
                "MAC_REPOSITORY_REF_RECONCILER_MODE must be off, audit, or prune"
            )
            mode = "off"
        interval = _number(
            env,
            "MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS",
            24 * 60 * 60.0,
            MIN_INTERVAL_SECONDS,
            MAX_INTERVAL_SECONDS,
            errors,
        )
        initial_delay = _number(
            env,
            "MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS",
            300.0,
            0.0,
            MAX_INITIAL_DELAY_SECONDS,
            errors,
        )
        grace_days = _number(
            env,
            "MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS",
            # Prune agent task-branches on merge (grace 0). They are ephemeral and
            # the only refs this reconciler manages; a grace window would only
            # matter for human ticket-linked PR branches, which it never touches.
            0.0,
            0.0,
            365.0,
            errors,
        )
        remote = str(
            env.get("MAC_REPOSITORY_REF_RECONCILER_REMOTE") or "origin"
        ).strip()
        if not _REMOTE_NAME_RE.fullmatch(remote):
            errors.append("MAC_REPOSITORY_REF_RECONCILER_REMOTE is invalid")
            remote = "origin"
        base_ref = str(env.get("MAC_REPOSITORY_REF_RECONCILER_BASE_REF") or "").strip()
        if base_ref and (
            base_ref.startswith("-")
            or any(character.isspace() for character in base_ref)
        ):
            errors.append("MAC_REPOSITORY_REF_RECONCILER_BASE_REF is invalid")
            base_ref = ""
        return cls(
            mode="off" if errors else mode,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            default_grace_seconds=int(grace_days * 24 * 60 * 60),
            remote=remote,
            base_ref=base_ref,
            configuration_error="; ".join(errors),
        )


class RepositoryRefReconciler:
    """Periodically reconcile registered repositories' managed task refs."""

    def __init__(
        self,
        control_plane: Any,
        config: RepositoryRefReconcilerConfig,
    ) -> None:
        self.control_plane = control_plane
        self.config = config
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None

    def start(self) -> bool:
        if not self.config.enabled:
            if self.config.configuration_error:
                self._observe(
                    "repository.ref.reconciler.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._loop,
                name="mac-repository-ref-reconciler",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        self._observe(
            "repository.ref.reconciler.started",
            "info",
            {"config": self.config.to_dict()},
        )
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("repository.ref.reconciler.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": REPOSITORY_REF_RECONCILER_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "last_report": last_report,
        }

    def run_once(
        self,
        *,
        mode: Optional[str] = None,
        actor: str = "repository-ref-reconciler",
        trigger: str = "operator",
    ) -> Dict[str, Any]:
        selected_mode = str(mode or self.config.mode).strip().lower()
        if selected_mode not in RECONCILER_MODES:
            raise RepositoryHygieneError("reconciler mode must be off, audit, or prune")
        if selected_mode == "off":
            return {
                "schema": REPOSITORY_REF_RECONCILER_SCHEMA,
                "mode": "off",
                "status": "disabled",
                "trigger": trigger,
                "repository_count": 0,
                "repositories": [],
            }
        if not self._run_lock.acquire(blocking=False):
            return {
                "schema": REPOSITORY_REF_RECONCILER_SCHEMA,
                "mode": selected_mode,
                "status": "busy",
                "trigger": trigger,
                "repository_count": 0,
                "repositories": [],
            }

        started_at = _utcnow()
        run_id = "refreconcile_%s" % uuid.uuid4().hex
        results: list[Dict[str, Any]] = []
        try:
            try:
                repositories = list(
                    self.control_plane.list_project_repositories(enabled=True)
                )
            except Exception as exc:  # noqa: BLE001 - report and retry on the next tick.
                report = self._report(
                    run_id,
                    selected_mode,
                    trigger,
                    started_at,
                    [],
                    error=_safe_error(exc),
                )
                self._finish(report)
                return report

            for repository in repositories:
                identity = self._repository_identity(repository)
                try:
                    result = self._reconcile_repository(
                        repository,
                        selected_mode,
                        actor,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate repository failures.
                    result = {
                        **identity,
                        "status": "failed",
                        "error": _safe_error(exc),
                        "eligible_count": 0,
                        "deleted_count": 0,
                    }
                results.append(result)

            report = self._report(
                run_id,
                selected_mode,
                trigger,
                started_at,
                results,
            )
            self._finish(report)
            return report
        finally:
            self._run_lock.release()

    def _loop(self) -> None:
        if self._stop_event.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - a future tick must still run.
                _log.warning("repository-ref reconciliation tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    @staticmethod
    def _repository_identity(repository: Any) -> Dict[str, str]:
        data = (
            repository.to_dict()
            if callable(getattr(repository, "to_dict", None))
            else dict(repository)
            if isinstance(repository, Mapping)
            else {}
        )
        return {
            "repository_id": str(data.get("id") or ""),
            "repository": str(data.get("name") or "unknown"),
            "project": str(data.get("project") or ""),
        }

    def _reconcile_repository(
        self,
        repository: Any,
        mode: str,
        actor: str,
    ) -> Dict[str, Any]:
        data = (
            repository.to_dict()
            if callable(getattr(repository, "to_dict", None))
            else dict(repository)
        )
        identity = self._repository_identity(data)
        metadata = (
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        )
        policy = (
            metadata.get("repository_ref_hygiene")
            if isinstance(metadata.get("repository_ref_hygiene"), dict)
            else {}
        )
        if policy.get("enabled") is False:
            return {
                **identity,
                "status": "skipped",
                "reason": "repository ref hygiene is disabled for this repository",
                "eligible_count": 0,
                "deleted_count": 0,
            }

        repo = Path(str(data.get("path") or "")).expanduser().resolve()
        if not repo.is_dir():
            raise RepositoryHygieneError("registered repository path is unavailable")
        remote = str(policy.get("remote") or self.config.remote).strip()
        contract = (
            metadata.get("repository_contract")
            if isinstance(metadata.get("repository_contract"), dict)
            else {}
        )
        canonical_remote = str(contract.get("canonical_remote_url") or "").strip()
        if mode == "prune" and not canonical_remote:
            raise RepositoryHygieneError(
                "automatic prune requires repository_contract.canonical_remote_url"
            )
        if canonical_remote:
            verify_repository_remote(repo, remote, canonical_remote)

        configured_base = str(policy.get("base_ref") or self.config.base_ref).strip()
        base_ref = resolve_remote_base_ref(
            repo,
            remote,
            configured=configured_base,
        )
        refresh_remote_base_ref(repo, remote, base_ref)
        refs = list_managed_remote_refs(repo, remote)
        open_pull_requests, pr_warning = query_open_pull_requests(repo)
        audits = audit_repository_refs(
            repo,
            refs,
            self.control_plane.task_detail,
            base_ref=base_ref,
            default_grace_seconds=self.config.default_grace_seconds,
            open_pull_requests=open_pull_requests,
        )
        if mode == "prune" and pr_warning:
            raise RepositoryHygieneError("%s; refusing executable cleanup" % pr_warning)

        def record(item: RepositoryRefAudit, action: str, error: str) -> None:
            metadata = cleanup_evidence_metadata(item, action, error=error)
            self.control_plane.add_evidence(
                item.task_id,
                "artifact",
                "urn:mac:repository-ref-cleanup:%s:%s:%s:%s"
                % (item.task_id, item.lease_id, item.sha, action),
                "managed repository ref cleanup %s for %s at %s"
                % (action, item.branch, item.sha),
                actor,
                metadata=metadata,
                _trusted_internal=True,
            )

        cleanup = prune_repository_refs(
            repo,
            audits,
            execute=mode == "prune",
            recorder=record if mode == "prune" else None,
        )
        protected_cleanup = self._reconcile_protected_work_package_refs(
            repo=repo,
            repository_id=str(data.get("id") or ""),
            remote=remote,
            mode=mode,
            actor=actor,
        )
        counts = Counter(item.classification for item in audits)
        result: Dict[str, Any] = {
            **identity,
            "status": (
                "warning"
                if (
                    pr_warning
                    or int(protected_cleanup.get("failed_count") or 0)
                    or int(protected_cleanup.get("audit_debt_count") or 0)
                )
                else "completed"
            ),
            "mode": mode,
            "base_ref": base_ref,
            "managed_ref_count": len(audits),
            "counts": dict(sorted(counts.items())),
            "eligible_count": (
                sum(1 for item in audits if item.eligible)
                + int(protected_cleanup.get("eligible_count") or 0)
            ),
            "deleted_count": (
                len(cleanup.get("deleted") or [])
                + int(protected_cleanup.get("deleted_count") or 0)
            ),
            "canonical_remote_verified": bool(canonical_remote),
            "protected_work_package_refs": protected_cleanup,
        }
        if pr_warning:
            result["warning"] = pr_warning
        return result

    def _reconcile_protected_work_package_refs(
        self,
        *,
        repo: Path,
        repository_id: str,
        remote: str,
        mode: str,
        actor: str,
    ) -> Dict[str, Any]:
        if getattr(self.control_plane, "store", None) is None:
            return {
                "eligible_count": 0,
                "deleted_count": 0,
                "missing_count": 0,
                "failed_count": 0,
                "audit_debt_count": 0,
                "audit_debts": [],
                "outcomes": [],
            }
        now = datetime.now(timezone.utc)
        audit_debts: list[Dict[str, Any]] = []
        authorities = self._protected_ref_authorities(
            repository_id,
            now=now,
            grace_seconds=self.config.default_grace_seconds,
            audit_debts=audit_debts,
        )
        outcomes = []
        deleted = 0
        missing = 0
        failed = 0
        for authority in authorities:
            try:
                if mode == "audit":
                    outcome = retire_protected_remote_ref_exact(
                        repo,
                        remote,
                        authority.ref,
                        authority.expected_sha,
                        execute=False,
                    )
                else:
                    intent_id = self._ensure_ref_retirement_intent(authority, actor)
                    receipt = self.control_plane.store.query_one(
                        "SELECT outcome FROM work_package_ref_retirement_receipts "
                        "WHERE intent_id = ?",
                        (intent_id,),
                    )
                    if receipt is not None:
                        outcome = str(receipt["outcome"])
                    else:
                        outcome = retire_protected_remote_ref_exact(
                            repo,
                            remote,
                            authority.ref,
                            authority.expected_sha,
                            execute=True,
                        )
                        self._record_ref_retirement_result(
                            intent_id,
                            outcome=outcome,
                            error="",
                        )
                if outcome == "deleted":
                    deleted += 1
                elif outcome == "missing":
                    missing += 1
                outcomes.append(
                    {
                        "ref": authority.ref,
                        "ref_kind": authority.ref_kind,
                        "expected_sha": authority.expected_sha,
                        "terminal_state": authority.terminal_state,
                        "outcome": outcome,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one ref must not block peers.
                failed += 1
                error = _safe_error(exc) or "protected ref cleanup failed"
                if mode == "prune":
                    try:
                        intent_id = self._ensure_ref_retirement_intent(authority, actor)
                        self._record_ref_retirement_result(
                            intent_id,
                            outcome="failed",
                            error=error,
                        )
                    except Exception:  # audit persistence failure is reported below.
                        _log.warning(
                            "could not persist protected-ref cleanup failure",
                            exc_info=True,
                        )
                outcomes.append(
                    {
                        "ref": authority.ref,
                        "ref_kind": authority.ref_kind,
                        "expected_sha": authority.expected_sha,
                        "terminal_state": authority.terminal_state,
                        "outcome": "failed",
                        "error": error,
                    }
                )
        return {
            "eligible_count": len(authorities),
            "deleted_count": deleted,
            "missing_count": missing,
            "failed_count": failed,
            "audit_debt_count": len(audit_debts),
            "audit_debts": audit_debts,
            "outcomes": outcomes,
        }

    def _protected_ref_authorities(
        self,
        repository_id: str,
        *,
        now: datetime,
        grace_seconds: int,
        audit_debts: Optional[list[Dict[str, Any]]] = None,
    ) -> list[_ProtectedRefAuthority]:
        grace = timedelta(seconds=max(0, int(grace_seconds)))
        authorities: list[_ProtectedRefAuthority] = []
        store = self.control_plane.store

        batch_rows = store.query_all(
            "SELECT batch.*, finalization.id AS finalization_id "
            "FROM work_package_integration_batches AS batch "
            "LEFT JOIN work_package_publication_finalizations AS finalization "
            "ON finalization.batch_id = batch.id "
            "WHERE batch.repository_id = ? AND batch.candidate_ref IS NOT NULL "
            "AND batch.candidate_sha IS NOT NULL "
            "AND batch.state IN ('published', 'rejected', 'stale', 'cancelled') "
            "ORDER BY batch.id",
            (repository_id,),
        )
        for row in batch_rows:
            state = str(row["state"])
            if state == "published" and row["finalization_id"] is None:
                continue
            authority = self._authority_if_due(
                repository_id=repository_id,
                ref_kind="candidate",
                ref=str(row["candidate_ref"] or ""),
                expected_sha=str(row["candidate_sha"] or ""),
                terminal_state="batch:%s" % state,
                terminal_at=row["completed_at"] or row["updated_at"],
                now=now,
                grace=grace,
                batch_id=str(row["id"]),
            )
            if authority is not None:
                authorities.append(authority)

        # Start from the controller-authored assignment ledger, not successful
        # verification receipts.  Failed verification, pre-candidate rejection,
        # and abandoned leases are all still durable attempt-ref allocations.
        # A missing verification receipt deliberately produces audit debt below:
        # worker-authored head claims are not sufficient authority for deletion.
        attempt_rows = store.query_all(
            "SELECT assignment.lease_id, assignment.attempt_ref, "
            "assignment.task_id, assignment.plan_version, assignment.epoch, "
            "assignment.node_key, assignment.attempt_number, "
            "lease.status AS lease_status, lease.updated_at AS lease_updated_at, "
            "task.state AS task_state, task.attempt_count AS task_attempt_count, "
            "task.lease_id AS current_task_lease_id, "
            "task.updated_at AS task_updated_at, task.completed_at AS task_completed_at, "
            "package.state AS package_state, "
            "package.current_plan_version, package.current_epoch, "
            "package.updated_at AS package_updated_at, "
            "epoch.status AS epoch_status, epoch.superseded_at, "
            "link.node_state, candidate.id AS candidate_id, "
            "candidate.status AS candidate_status, "
            "candidate.submitted_at, candidate.accepted_at, "
            "verification.attempt_head_sha, verification.verified_at "
            "FROM work_package_assignment_audit AS assignment "
            "JOIN work_packages AS package ON package.id = assignment.package_id "
            "JOIN work_package_epochs AS epoch "
            "ON epoch.package_id = assignment.package_id "
            "AND epoch.plan_version = assignment.plan_version "
            "AND epoch.epoch = assignment.epoch "
            "JOIN work_package_task_links AS link "
            "ON link.package_id = assignment.package_id "
            "AND link.plan_version = assignment.plan_version "
            "AND link.epoch = assignment.epoch "
            "AND link.node_key = assignment.node_key "
            "AND link.task_id = assignment.task_id "
            "JOIN leases AS lease ON lease.id = assignment.lease_id "
            "JOIN tasks AS task ON task.id = assignment.task_id "
            "LEFT JOIN work_package_node_candidates AS candidate "
            "ON candidate.assignment_lease_id = assignment.lease_id "
            "AND candidate.task_id = assignment.task_id "
            "AND candidate.attempt_number = assignment.attempt_number "
            "LEFT JOIN evidence_attempt_verifications AS verification "
            "ON verification.lease_id = assignment.lease_id "
            "AND verification.task_id = assignment.task_id "
            "AND verification.attempt_number = assignment.attempt_number "
            "WHERE package.repository_id = ? ORDER BY assignment.attempt_ref",
            (repository_id,),
        )
        consumer_rows = store.query_all(
            "SELECT input.assignment_lease_id, input.candidate_id, "
            "batch.id AS batch_id, batch.state, batch.completed_at, "
            "batch.updated_at, finalization.id AS finalization_id, "
            "finalization.finalized_at AS finalization_created_at "
            "FROM work_package_batch_inputs AS input "
            "JOIN work_package_integration_batches AS batch "
            "ON batch.id = input.batch_id "
            "LEFT JOIN work_package_publication_finalizations AS finalization "
            "ON finalization.batch_id = batch.id "
            "WHERE batch.repository_id = ? "
            "ORDER BY input.assignment_lease_id, batch.id",
            (repository_id,),
        )
        consumers_by_lease: Dict[str, list[Mapping[str, Any]]] = {}
        for consumer in consumer_rows:
            consumers_by_lease.setdefault(
                str(consumer["assignment_lease_id"]), []
            ).append(consumer)
        terminal_batch_states = {"published", "rejected", "stale", "cancelled"}
        for row in attempt_rows:
            lease_status = str(row["lease_status"] or "")
            if lease_status == "active":
                continue
            candidate_status = str(row["candidate_status"] or "")
            terminal_at: Any = self._latest_terminal_time(
                row,
                "lease_updated_at",
                "task_updated_at",
                "task_completed_at",
                "package_updated_at",
                "superseded_at",
                "submitted_at",
                "accepted_at",
            )
            terminal_state = (
                "candidate:%s" % candidate_status
                if candidate_status
                else "lease:%s:no_candidate" % lease_status
            )
            if candidate_status == "accepted":
                consumers = consumers_by_lease.get(str(row["lease_id"]), [])
                if consumers and any(
                    str(item["state"]) not in terminal_batch_states
                    or (
                        str(item["state"]) == "published"
                        and item["finalization_id"] is None
                    )
                    for item in consumers
                ):
                    continue
                if consumers:
                    consumer_times = [
                        self._latest_terminal_time(
                            item,
                            "completed_at",
                            "updated_at",
                            "finalization_created_at",
                        )
                        for item in consumers
                    ]
                    if any(value is None for value in consumer_times):
                        continue
                    terminal_at = max(
                        value for value in consumer_times if value is not None
                    )
                    terminal_state = "batches:%s" % ",".join(
                        sorted({str(item["state"]) for item in consumers})
                    )
                elif self._candidate_is_current(row, accepted=True):
                    continue
                else:
                    terminal_state = "candidate:accepted_noncurrent"
            elif candidate_status == "submitted" and self._candidate_is_current(
                row, accepted=False
            ):
                continue
            elif candidate_status not in {"", "submitted", "rejected", "superseded"}:
                continue

            if terminal_at is None:
                self._append_ref_audit_debt(
                    audit_debts,
                    repository_id=repository_id,
                    row=row,
                    terminal_state=terminal_state,
                    terminal_at=None,
                    eligible_after=None,
                    reason_code="terminal_time_unavailable",
                )
                continue
            parsed_terminal = (
                terminal_at
                if isinstance(terminal_at, datetime)
                else _parse_time(terminal_at)
            )
            if parsed_terminal is None:
                continue
            eligible_after = parsed_terminal + grace
            if eligible_after > now:
                continue

            expected_sha = str(row["attempt_head_sha"] or "")
            if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", expected_sha):
                self._append_ref_audit_debt(
                    audit_debts,
                    repository_id=repository_id,
                    row=row,
                    terminal_state=terminal_state,
                    terminal_at=parsed_terminal,
                    eligible_after=eligible_after,
                    reason_code="controller_head_sha_unavailable",
                )
                continue
            authority = self._authority_if_due(
                repository_id=repository_id,
                ref_kind="attempt",
                ref=str(row["attempt_ref"] or ""),
                expected_sha=expected_sha,
                terminal_state=terminal_state,
                terminal_at=parsed_terminal,
                now=now,
                grace=grace,
                task_id=str(row["task_id"]),
            )
            if authority is not None:
                authorities.append(authority)
        return sorted(authorities, key=lambda item: (item.ref, item.expected_sha))

    @staticmethod
    def _latest_terminal_time(
        row: Mapping[str, Any], *field_names: str
    ) -> Optional[datetime]:
        values = [_parse_time(row[name]) for name in field_names if row[name]]
        return max((value for value in values if value is not None), default=None)

    @staticmethod
    def _candidate_is_current(row: Mapping[str, Any], *, accepted: bool) -> bool:
        if (
            int(row["current_plan_version"] or 0) != int(row["plan_version"])
            or int(row["current_epoch"] or 0) != int(row["epoch"])
            or str(row["epoch_status"] or "") != "active"
            or str(row["package_state"] or "")
            in {"completed", "failed", "cancelled"}
        ):
            return False
        if accepted:
            return str(row["node_state"] or "") in {
                "candidate_accepted",
                "integrated",
                "certified",
            }
        return (
            str(row["node_state"] or "") == "candidate_submitted"
            and int(row["task_attempt_count"] or 0) == int(row["attempt_number"])
            and row["current_task_lease_id"] is None
            and str(row["task_state"] or "") in {"reviewing", "open", "blocked"}
        )

    @staticmethod
    def _append_ref_audit_debt(
        debts: Optional[list[Dict[str, Any]]],
        *,
        repository_id: str,
        row: Mapping[str, Any],
        terminal_state: str,
        terminal_at: Optional[datetime],
        eligible_after: Optional[datetime],
        reason_code: str,
    ) -> None:
        if debts is None:
            return
        debts.append(
            {
                "schema": "mac.work_package.ref_retirement_audit_debt.v1",
                "repository_id": repository_id,
                "ref_kind": "attempt",
                "ref": str(row["attempt_ref"] or ""),
                "task_id": str(row["task_id"] or ""),
                "lease_id": str(row["lease_id"] or ""),
                "terminal_state": terminal_state,
                "terminal_at": (
                    terminal_at.isoformat(timespec="microseconds")
                    if terminal_at is not None
                    else ""
                ),
                "eligible_after": (
                    eligible_after.isoformat(timespec="microseconds")
                    if eligible_after is not None
                    else ""
                ),
                "reason_code": reason_code,
                "reason": (
                    "exact controller-observed attempt head SHA is unavailable; "
                    "automatic deletion is prohibited"
                    if reason_code == "controller_head_sha_unavailable"
                    else "attempt terminal timestamp is unavailable; automatic deletion is prohibited"
                ),
            }
        )

    @staticmethod
    def _authority_if_due(
        *,
        repository_id: str,
        ref_kind: str,
        ref: str,
        expected_sha: str,
        terminal_state: str,
        terminal_at: Any,
        now: datetime,
        grace: timedelta,
        task_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> Optional[_ProtectedRefAuthority]:
        expected_prefixes = (
            ("refs/mac/attempts/",)
            if ref_kind == "attempt"
            else ("refs/mac/integration/", "refs/mac/candidates/")
        )
        if not ref.startswith(expected_prefixes) or not re.fullmatch(
            r"[0-9a-f]{40}(?:[0-9a-f]{24})?", expected_sha
        ):
            return None
        parsed = terminal_at if isinstance(terminal_at, datetime) else _parse_time(terminal_at)
        if parsed is None:
            return None
        eligible = parsed + grace
        if eligible > now:
            return None
        return _ProtectedRefAuthority(
            repository_id=repository_id,
            ref_kind=ref_kind,
            ref=ref,
            expected_sha=expected_sha,
            terminal_state=terminal_state,
            terminal_at=parsed.isoformat(timespec="microseconds"),
            eligible_after=eligible.isoformat(timespec="microseconds"),
            task_id=task_id,
            batch_id=batch_id,
        )

    def _ensure_ref_retirement_intent(
        self,
        authority: _ProtectedRefAuthority,
        actor: str,
    ) -> str:
        intent_id = "wpri_%s" % hashlib.sha256(
            (authority.repository_id + "\0" + authority.ref + "\0" + authority.expected_sha).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        with self.control_plane.store.transaction() as conn:
            conn.execute(
                "INSERT INTO work_package_ref_retirement_intents ("
                "id, repository_id, ref_kind, ref, expected_sha, task_id, batch_id, "
                "terminal_state, terminal_at, eligible_after, created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    intent_id,
                    authority.repository_id,
                    authority.ref_kind,
                    authority.ref,
                    authority.expected_sha,
                    authority.task_id,
                    authority.batch_id,
                    authority.terminal_state,
                    authority.terminal_at,
                    authority.eligible_after,
                    str(actor or "repository-ref-reconciler"),
                    _utcnow(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM work_package_ref_retirement_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
            if row is None or any(
                row[name] != value
                for name, value in (
                    ("repository_id", authority.repository_id),
                    ("ref_kind", authority.ref_kind),
                    ("ref", authority.ref),
                    ("expected_sha", authority.expected_sha),
                    ("task_id", authority.task_id),
                    ("batch_id", authority.batch_id),
                )
            ):
                raise RepositoryHygieneError(
                    "protected-ref retirement intent identity conflict"
                )
        return intent_id

    def _record_ref_retirement_result(
        self,
        intent_id: str,
        *,
        outcome: str,
        error: str,
    ) -> None:
        now = _utcnow()
        with self.control_plane.store.transaction() as conn:
            conn.execute(
                "INSERT INTO work_package_ref_retirement_attempts ("
                "id, intent_id, outcome, error, created_at) VALUES (?, ?, ?, ?, ?)",
                (new_id("wpra"), intent_id, outcome, error, now),
            )
            if outcome in {"deleted", "missing"}:
                receipt_id = "wprr_%s" % hashlib.sha256(
                    intent_id.encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT INTO work_package_ref_retirement_receipts ("
                    "id, intent_id, outcome, completed_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (receipt_id, intent_id, outcome, now),
                )
                receipt = conn.execute(
                    "SELECT outcome FROM work_package_ref_retirement_receipts "
                    "WHERE id = ? AND intent_id = ?",
                    (receipt_id, intent_id),
                ).fetchone()
                if receipt is None or receipt["outcome"] != outcome:
                    raise RepositoryHygieneError(
                        "protected-ref retirement receipt identity conflict"
                    )

    @staticmethod
    def _report(
        run_id: str,
        mode: str,
        trigger: str,
        started_at: str,
        results: list[Dict[str, Any]],
        *,
        error: str = "",
    ) -> Dict[str, Any]:
        failed = sum(1 for result in results if result.get("status") == "failed")
        warnings = sum(1 for result in results if result.get("status") == "warning")
        if error:
            status = "failed"
        elif failed:
            status = "partial_failure" if failed < len(results) else "failed"
        elif warnings:
            status = "completed_with_warnings"
        else:
            status = "completed"
        report: Dict[str, Any] = {
            "schema": REPOSITORY_REF_RECONCILER_SCHEMA,
            "run_id": run_id,
            "mode": mode,
            "status": status,
            "trigger": trigger,
            "started_at": started_at,
            "completed_at": _utcnow(),
            "repository_count": len(results),
            "failed_count": failed,
            "warning_count": warnings,
            "eligible_count": sum(
                int(result.get("eligible_count") or 0) for result in results
            ),
            "deleted_count": sum(
                int(result.get("deleted_count") or 0) for result in results
            ),
            "audit_debt_count": sum(
                int(
                    (result.get("protected_work_package_refs") or {}).get(
                        "audit_debt_count"
                    )
                    or 0
                )
                for result in results
            ),
            "repositories": results,
        }
        if error:
            report["error"] = error
        return report

    def _finish(self, report: Dict[str, Any]) -> None:
        with self._state_lock:
            self._last_report = copy.deepcopy(report)
        level = (
            "warning"
            if report.get("status")
            in {"failed", "partial_failure", "completed_with_warnings"}
            else "info"
        )
        self._observe("repository.ref.reconciler.run", level, report)

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(
                event_type,
                layer="control_plane",
                source="repository-ref-reconciler",
                level=level,
                subject_type="service",
                subject_id="repository-ref-reconciler",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry must not stop reconciliation.
            _log.warning(
                "could not record repository-ref reconciler telemetry", exc_info=True
            )


__all__ = [
    "REPOSITORY_REF_RECONCILER_SCHEMA",
    "RECONCILER_MODES",
    "RepositoryRefReconciler",
    "RepositoryRefReconcilerConfig",
]
