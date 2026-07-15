from __future__ import annotations

import copy
import logging
import os
import re
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

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
            )

        cleanup = prune_repository_refs(
            repo,
            audits,
            execute=mode == "prune",
            recorder=record if mode == "prune" else None,
        )
        counts = Counter(item.classification for item in audits)
        result: Dict[str, Any] = {
            **identity,
            "status": "warning" if pr_warning else "completed",
            "mode": mode,
            "base_ref": base_ref,
            "managed_ref_count": len(audits),
            "counts": dict(sorted(counts.items())),
            "eligible_count": sum(1 for item in audits if item.eligible),
            "deleted_count": len(cleanup.get("deleted") or []),
            "canonical_remote_verified": bool(canonical_remote),
        }
        if pr_warning:
            result["warning"] = pr_warning
        return result

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
