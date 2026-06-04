from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, urlencode

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_CONTENT_TYPE,
    REPO_UPDATE_RESULT_SCHEMA,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_SCHEMA,
    REPO_UPDATE_TOPIC,
)
from mac.hermes_adapter import MacApiClient, MacApiError


JsonDict = Dict[str, Any]
Executor = Callable[[JsonDict, Path], "WorkerExecution"]
CommandAuditSink = Callable[[JsonDict], None]
StatusUpdateSink = Callable[[JsonDict], JsonDict]
SAFE_GIT_REF_RE = r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$"
VERIFICATION_SCHEMA = "mac.worker_evidence.v1"
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# mac-raud: validate git remote URLs supplied by upstream evidence
# before passing to ``git clone``. We accept https/http, ssh://, git://,
# git@host:path forms; nothing that could be parsed as a flag.
_GIT_REMOTE_URL_RE = re.compile(
    r"^(?:https?://|ssh://|git://|file://|git@|/)[A-Za-z0-9._\-:/@%+~?=&]*$"
)
# Git ref name rules (simplified — see git-check-ref-format).
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")


def _validate_git_remote_url(value: str) -> str:
    """Reject git remote URLs that could smuggle argv flags or escape
    the URL shape entirely (mac-raud).
    """
    if not value or value.startswith("-"):
        raise ValueError("git remote URL is empty or looks like a flag: %r" % value)
    if len(value) > 2048:
        raise ValueError("git remote URL exceeds 2048 byte limit")
    if not _GIT_REMOTE_URL_RE.match(value):
        raise ValueError("git remote URL does not match a recognised scheme: %r" % value)
    return value


def _validate_git_ref(value: str) -> str:
    """Reject git refs that could be confused with argv flags."""
    if not value or value.startswith("-"):
        raise ValueError("git ref is empty or looks like a flag: %r" % value)
    if len(value) > 512:
        raise ValueError("git ref exceeds 512 byte limit")
    if not _GIT_REF_RE.match(value):
        raise ValueError("git ref contains disallowed characters: %r" % value)
    return value
REQUIRED_CHANGED_FILE_KEYS = (
    "required_changed_files",
    "required_files",
    "required_repo_files",
)


@dataclass
class WorkerExecution:
    returncode: int
    summary: str
    stdout: str = ""
    stderr: str = ""
    metadata: JsonDict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass
class WorkerRunResult:
    status: str
    task: Optional[JsonDict] = None
    lease: Optional[JsonDict] = None
    evidence: Optional[JsonDict] = None
    error: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


class SubprocessExecutor:
    def __init__(self, argv: List[str], timeout: Optional[float] = None) -> None:
        if not argv:
            raise MacApiError("executor command is required")
        self.argv = argv
        self.timeout = timeout
        self.audit_sink: Optional[CommandAuditSink] = None
        self.audit_context: JsonDict = {}

    def __call__(self, task: JsonDict, task_dir: Path) -> WorkerExecution:
        env = os.environ.copy()
        repository_context = _load_repository_context(task_dir)
        env.update(
            {
                "MAC_TASK_ID": task["id"],
                "MAC_TASK_FILE": str(task_dir / "task.json"),
                "MAC_TASK_WORKSPACE": str(task_dir),
            }
        )
        if repository_context:
            env.update(_repository_context_env(repository_context))
        command_id = _command_audit_id()
        started_at = _utcnow()
        started_monotonic = time.monotonic()
        base_record = {
            "command_id": command_id,
            "argv": _audit_safe_argv(self.argv),
            "cwd": str(task_dir),
            "task_id": self.audit_context.get("task_id") or task.get("id"),
            "lease_id": self.audit_context.get("lease_id"),
            "started_at": started_at,
            "metadata": {
                "argv_sha256": _sha256_text(json.dumps(self.argv, separators=(",", ":"))),
                **_repository_context_audit_metadata(repository_context),
                **ensure_json_object(self.audit_context.get("metadata")),
            },
        }
        self._emit_audit({**base_record, "phase": "started"})
        try:
            completed = subprocess.run(
                self.argv,
                cwd=str(task_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            completed_at = _utcnow()
            stdout = _coerce_process_output(exc.stdout)
            stderr = _coerce_process_output(exc.stderr)
            self._emit_audit(
                {
                    **base_record,
                    "phase": "timeout",
                    "completed_at": completed_at,
                    "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                    "stdout_sha256": _sha256_text(stdout),
                    "stderr_sha256": _sha256_text(stderr),
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr.encode("utf-8")),
                    "metadata": {
                        **base_record["metadata"],
                        "timeout_seconds": self.timeout,
                    },
                }
            )
            raise
        except OSError as exc:
            completed_at = _utcnow()
            self._emit_audit(
                {
                    **base_record,
                    "phase": "error",
                    "completed_at": completed_at,
                    "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                    "metadata": {**base_record["metadata"], "error": str(exc)},
                }
            )
            raise
        completed_at = _utcnow()
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        self._emit_audit(
            {
                **base_record,
                "phase": "completed" if completed.returncode == 0 else "failed",
                "completed_at": completed_at,
                "duration_ms": (time.monotonic() - started_monotonic) * 1000.0,
                "returncode": completed.returncode,
                "stdout_sha256": _sha256_text(stdout),
                "stderr_sha256": _sha256_text(stderr),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
            }
        )
        return WorkerExecution(
            returncode=completed.returncode,
            summary=_summary_from_output(completed.returncode, stdout, stderr),
            stdout=stdout,
            stderr=stderr,
            metadata={
                "executor": _audit_safe_argv(self.argv),
                "executor_argv_sha256": base_record["metadata"]["argv_sha256"],
            },
        )

    def _emit_audit(self, record: JsonDict) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink(record)
        except Exception:
            pass


def register_worker(
    client: MacApiClient,
    hostname: Optional[str] = None,
    agent_name: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    resources: Optional[JsonDict] = None,
    machine_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
) -> JsonDict:
    """Register or refresh the machine and agent rows for this worker process."""
    host = (hostname or socket.gethostname()).strip()
    if not host:
        raise MacApiError("hostname is required for worker registration")
    name = (agent_name or host).strip()
    if not name:
        raise MacApiError("agent_name is required for worker registration")
    resolved_machine_id = machine_id or _stable_id("machine", host)
    resolved_agent_id = agent_id or _stable_id("agent", name)
    # media-01 capability self-advertisement: a GPU agent announces the media ops
    # it serves via MAC_AGENT_MEDIA_ROUTES (JSON list of route dicts, e.g.
    # [{"op":"image.generate","base_url":"http://host:8189","adapter":"openai_images"}]).
    # The hub composes its /v1/media routing table from live agents'
    # resources["media_routes"], so the fleet uses this agent with zero operator
    # config. Merged into the registration resources here.
    _media_routes = (os.environ.get("MAC_AGENT_MEDIA_ROUTES") or "").strip()
    if _media_routes:
        try:
            _parsed_routes = json.loads(_media_routes)
        except ValueError:
            _parsed_routes = None
        if isinstance(_parsed_routes, list):
            resources = {**(resources or {}), "media_routes": _parsed_routes}
    machine = client.post(
        "/machines",
        {
            "hostname": host,
            "machine_id": resolved_machine_id,
            "labels": {"registered_by": "mac-agent"},
            "resources": resources or {},
            "trusted": True,
        },
    )
    _register_runtime_identity_for_worker(client, name, hermes_instance_id)
    agent = client.post(
        "/agents",
        {
            "machine_id": machine["id"],
            "name": name,
            "agent_id": resolved_agent_id,
            "capabilities": capabilities or [],
            "resources": resources or {},
            "hermes_instance_id": hermes_instance_id,
        },
    )
    _ensure_worker_fleet_membership(client, agent_name=name, agent_id=str(agent["id"]))
    return agent


class MacWorker:
    """Small worker harness for mac-owned tasks.

    This is intentionally narrower than ACC's deployed worker. It proves the
    claim/start/execute/evidence/review handoff without owning Hermes memory or
    pretending to be the final production daemon.
    """

    def __init__(
        self,
        client: MacApiClient,
        agent_id: str,
        workspace: Path,
        executor: Executor,
        lease_seconds: int = 900,
        running_digest: Optional[str] = None,
        poll_interval_seconds: float = 1.0,
        allowed_projects: Optional[List[str]] = None,
        required_metadata: Optional[JsonDict] = None,
        require_canary: bool = False,
        lease_renew_interval_seconds: Optional[float] = None,
        agentbus_control_enabled: bool = True,
        self_update_repo: Optional[Path] = None,
        agentbus_control_state_path: Optional[Path] = None,
        attestation_key: Optional[str] = None,
        status_update_sink: Optional[StatusUpdateSink] = None,
    ) -> None:
        if not agent_id:
            raise MacApiError("agent_id is required")
        self.client = client
        self.agent_id = agent_id
        self.workspace = workspace
        self.executor = executor
        if isinstance(self.executor, SubprocessExecutor):
            self.executor.audit_sink = self._record_command_audit
        self.lease_seconds = lease_seconds
        self.running_digest = running_digest
        # Attestation key for signing verification manifests
        # (mac-ng2). Falls back to MAC_ATTESTATION_KEY when not passed.
        # Without a key the worker still writes evidence — but the
        # default-review workflow will reject it as "manifest_not_signed"
        # and refuse to publish. The CLI surfaces this in deploy via
        # MAC_ATTESTATION_KEY.
        self.attestation_key = attestation_key or os.environ.get("MAC_ATTESTATION_KEY")
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.allowed_projects = list(allowed_projects or [])
        self.required_metadata = dict(required_metadata or {})
        self.require_canary = bool(require_canary)
        self.lease_renew_interval_seconds = lease_renew_interval_seconds
        self.agentbus_control_enabled = bool(agentbus_control_enabled)
        self.self_update_repo = (self_update_repo or _default_self_update_repo()).expanduser().resolve()
        self.agentbus_control_state_path = (
            agentbus_control_state_path
            if agentbus_control_state_path is not None
            else self.workspace / ".mac-agentbus-control.json"
        )
        self.status_update_sink = status_update_sink or self._send_status_update_to_home_channels
        self._stop = False
        self._declared_digest = False
        self._declared_policy = False

    def stop(self) -> None:
        """Signal the run loop to exit after the current task."""
        self._stop = True

    def run_forever(self, max_iterations: Optional[int] = None) -> List[WorkerRunResult]:
        """Loop run_once() with sleep on empty. Bounded by max_iterations for tests.

        Reacts to SIGTERM/SIGINT for graceful shutdown when running as a daemon.
        On exit, marks the agent offline so the control plane can requeue any
        active lease held by this worker. The signal handlers installed for the
        duration of this call are restored before return — the process-wide
        SIGTERM/SIGINT state is not mutated past the worker's lifetime.
        """
        prior_handlers = self._install_signal_handlers()
        results: List[WorkerRunResult] = []
        iterations = 0
        try:
            while not self._stop and (max_iterations is None or iterations < max_iterations):
                iterations += 1
                try:
                    outcome = self.run_once()
                except Exception as exc:  # noqa: BLE001
                    # Loop resilience (loop-01): run_once best-effort-marks the
                    # task failed before any re-raise, so one task's error must
                    # not crash the worker and halt ALL autonomous work. Record
                    # it and continue to the next poll. (SIGTERM/SIGINT set
                    # self._stop via the handlers, not exceptions, so graceful
                    # shutdown is unaffected; KeyboardInterrupt/SystemExit are
                    # BaseException and still propagate.)
                    self._observe_log(
                        "worker.run_once.exception",
                        level="error",
                        detail={"error": str(exc), "iteration": iterations},
                    )
                    results.append(WorkerRunResult(status="error", error=str(exc)))
                    if max_iterations is None:
                        time.sleep(self.poll_interval_seconds)
                    continue
                if outcome.status == "no_task":
                    if max_iterations is None:
                        time.sleep(self.poll_interval_seconds)
                    continue
                results.append(outcome)
        finally:
            self._restore_signal_handlers(prior_handlers)
            self._shutdown()
        return results

    def _install_signal_handlers(self) -> Dict[int, Any]:
        """Install graceful-stop signal handlers; return prior handlers so we
        can restore them. Returns an empty dict if signals can't be installed
        (e.g. when called outside the main thread)."""
        prior: Dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                prior[signum] = signal.signal(signum, lambda *_: self.stop())
            except (ValueError, AttributeError, OSError):
                # signal.signal raises if not in main thread or on platforms
                # without the signal. Tests bound execution via max_iterations.
                pass
        return prior

    def _restore_signal_handlers(self, prior: Dict[int, Any]) -> None:
        for signum, handler in prior.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, AttributeError, OSError):
                pass

    def _shutdown(self) -> None:
        # Best-effort: mark offline so the control plane requeues any active
        # lease tied to this agent. Catch broadly: shutdown must not raise.
        try:
            self.client.post(
                "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
                {"status": "offline"},
            )
        except Exception:  # noqa: BLE001 — shutdown is a boundary
            pass

    def run_once(self) -> WorkerRunResult:
        self._heartbeat()
        control_result = self._process_agentbus_control()
        if control_result and control_result.get("restart_requested"):
            self.stop()
            return WorkerRunResult(
                status="self_update_restart",
                evidence=control_result,
                error=control_result.get("summary"),
            )
        review_result = self._process_review_nudges()
        if review_result is not None:
            return review_result
        self._observe_policy_once()
        assignment = self._claim_next_for_agent()
        if assignment is None:
            self._observe_log("worker.no_task", level="debug", detail={"agent_id": self.agent_id})
            return WorkerRunResult(status="no_task")

        task = assignment["task"]
        lease = assignment["lease"]
        return self.execute_assignment(task, lease)

    def execute_assignment(self, task: JsonDict, lease: JsonDict) -> WorkerRunResult:
        """Execute a task whose lease is already claimed.

        Caller is responsible for the claim; this method only does
        start -> prepare -> execute -> record -> publish -> submit-for-review.
        Suitable for K8s-mode where the runner has pre-claimed the task
        and the Job pod just needs to execute it.
        """
        task_id = task["id"]
        self._observe_log(
            "worker.task_claimed",
            subject_type="task",
            subject_id=task_id,
            detail={"lease_id": lease["id"], "agent_id": self.agent_id},
        )
        try:
            self.client.post(
                "/tasks/%s/start?%s"
                % (quote(task_id, safe=""), urlencode({"agent_id": self.agent_id})),
                {},
            )
            task_dir = self._prepare_task_workspace(task, lease)
            started = time.monotonic()
            execution = self._execute_with_lease_renewal(task, lease, task_dir)
            duration_ms = (time.monotonic() - started) * 1000.0
            self._observe_metric(
                "worker.execution.duration_ms",
                duration_ms,
                unit="ms",
                subject_type="task",
                subject_id=task_id,
                detail={"returncode": execution.returncode},
            )
            self._observe_log(
                "worker.execution.completed",
                level="info" if execution.succeeded else "error",
                subject_type="task",
                subject_id=task_id,
                detail={"returncode": execution.returncode, "summary": execution.summary},
            )
            if not self._assignment_is_current(task_id, lease["id"]):
                return self._stale_result(
                    task_id,
                    lease,
                    "assignment no longer current after executor completed",
                    execution=execution,
                )
            evidence = self._record_execution(task_id, task_dir, execution)
            if execution.succeeded:
                submission_problems = self._execution_submission_problems(task_dir, evidence)
                if submission_problems:
                    self._observe_log(
                        "worker.execution.verification_failed",
                        level="error",
                        subject_type="task",
                        subject_id=task_id,
                        detail={
                            "evidence_id": evidence.get("id"),
                            "problems": submission_problems,
                        },
                    )
                    failed_task = self.client.post(
                        "/tasks/%s/transition" % quote(task_id, safe=""),
                        {
                            "target_state": "failed",
                            "actor": self.agent_id,
                            "detail": {
                                "reason": "verification_contract_failed",
                                "evidence_id": evidence.get("id"),
                                "problems": submission_problems,
                            },
                        },
                    )
                    return WorkerRunResult(
                        status="failed",
                        task=failed_task,
                        lease=lease,
                        evidence=evidence,
                        error="; ".join(submission_problems[:4]),
                    )
                reviewed_task = self.client.post(
                    "/tasks/%s/submit-for-review?%s"
                    % (
                        quote(task_id, safe=""),
                        urlencode(
                            {
                                "agent_id": self.agent_id,
                                "advance_default_workflow": "true",
                            }
                        ),
                    ),
                    {},
                )
                return WorkerRunResult(
                    status="submitted_for_review",
                    task=reviewed_task,
                    lease=lease,
                    evidence=evidence,
                )
            failed_task = self.client.post(
                "/tasks/%s/transition" % quote(task_id, safe=""),
                {
                    "target_state": "failed",
                    "actor": self.agent_id,
                    "detail": {
                        "reason": "executor_failed",
                        "returncode": execution.returncode,
                        "evidence_id": evidence["id"],
                    },
                },
            )
            return WorkerRunResult(
                status="failed",
                task=failed_task,
                lease=lease,
                evidence=evidence,
                error=execution.summary,
            )
        except Exception as exc:
            if not self._assignment_is_current(task_id, lease["id"]):
                return self._stale_result(task_id, lease, str(exc))
            self._observe_log(
                "worker.execution.exception",
                level="error",
                subject_type="task",
                subject_id=task_id,
                detail={"error": str(exc)},
            )
            try:
                self.client.post(
                    "/tasks/%s/transition" % quote(task_id, safe=""),
                    {
                        "target_state": "failed",
                        "actor": self.agent_id,
                        "detail": {"reason": "worker_exception", "error": str(exc)},
                    },
                )
            except Exception:
                pass
            raise

    def _assignment_is_current(self, task_id: str, lease_id: str) -> bool:
        try:
            current = self.client.get("/tasks/%s" % quote(task_id, safe=""))
        except MacApiError:
            # Hub unreachable / transient API error: preserve the older
            # behavior and let the concrete operation surface the
            # failure. Narrowed from bare ``except Exception`` (mac-h3d)
            # so TypeError/KeyError/AttributeError from a malformed
            # response or a programming bug bubbles up instead of being
            # silently treated as "still current."
            return True
        current_task = current.get("task", current)
        return (
            current_task.get("owner_agent_id") == self.agent_id
            and current_task.get("lease_id") == lease_id
            and current_task.get("state") in {"claimed", "running"}
        )

    def _stale_result(
        self,
        task_id: str,
        lease: JsonDict,
        reason: str,
        execution: Optional[WorkerExecution] = None,
    ) -> WorkerRunResult:
        detail: JsonDict = {
            "agent_id": self.agent_id,
            "lease_id": lease["id"],
            "reason": reason,
        }
        if execution is not None:
            detail.update(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                }
            )
        self._observe_log(
            "worker.execution.stale_result",
            level="warning",
            subject_type="task",
            subject_id=task_id,
            detail=detail,
        )
        try:
            current = self.client.get("/tasks/%s" % quote(task_id, safe=""))
            current_task: Optional[JsonDict] = current.get("task", current)
        except Exception:
            current_task = None
        return WorkerRunResult(
            status="stale_result",
            task=current_task,
            lease=lease,
            error=reason,
        )

    def _execute_with_lease_renewal(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
    ) -> WorkerExecution:
        stop = threading.Event()
        thread: Optional[threading.Thread] = None
        interval = self.lease_renew_interval_seconds
        if interval is None:
            interval = max(1.0, min(60.0, float(self.lease_seconds) / 2.0))
        if self.lease_seconds > 0 and interval > 0:
            thread = threading.Thread(
                target=self._renew_lease_until_stopped,
                args=(lease["id"], task["id"], stop, interval),
                daemon=True,
            )
            thread.start()
        try:
            return self._call_executor(
                task,
                task_dir,
                {
                    "task_id": task["id"],
                    "lease_id": lease["id"],
                    "metadata": {"execution_kind": "task"},
                },
            )
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1.0)

    def _renew_lease_until_stopped(
        self,
        lease_id: str,
        task_id: str,
        stop: threading.Event,
        interval_seconds: float,
    ) -> None:
        while not stop.wait(interval_seconds):
            try:
                lease = self.client.post(
                    "/leases/%s/renew" % quote(lease_id, safe=""),
                    {"agent_id": self.agent_id, "lease_seconds": self.lease_seconds},
                )
                self._observe_log(
                    "worker.lease_renewed",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"lease_id": lease_id, "expires_at": lease["expires_at"]},
                )
            except Exception as exc:  # noqa: BLE001 - renewal is best-effort telemetry
                self._observe_log(
                    "worker.lease_renew_failed",
                    level="error",
                    subject_type="task",
                    subject_id=task_id,
                    detail={"lease_id": lease_id, "error": str(exc)},
                )

    def _claim_next_for_agent(self) -> Optional[JsonDict]:
        return self.client.post(
            "/agents/%s/claim-next" % quote(self.agent_id, safe=""),
            self._claim_payload(dry_run=False),
        )

    def dry_run_claim(self) -> Optional[JsonDict]:
        self._heartbeat()
        self._observe_policy_once()
        assignment = self.client.post(
            "/agents/%s/claim-next" % quote(self.agent_id, safe=""),
            self._claim_payload(dry_run=True),
        )
        self._observe_log(
            "worker.routing.dry_run_result",
            level="info" if assignment is not None else "debug",
            subject_type="task" if assignment else None,
            subject_id=(assignment.get("task") or {}).get("id") if assignment else None,
            detail={
                "agent_id": self.agent_id,
                "matched": assignment is not None,
                "policy": self._policy_payload(),
            },
        )
        return assignment

    def _process_review_nudges(self) -> Optional[WorkerRunResult]:
        try:
            messages = self.client.post(
                "/agents/%s/messages/deliver?%s"
                % (quote(self.agent_id, safe=""), urlencode({"limit": 20})),
                {},
            )
        except Exception as exc:  # noqa: BLE001 - message polling must not break task polling.
            self._observe_log(
                "worker.review_nudge.poll_failed",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return None

        if not isinstance(messages, list):
            return None
        skipped_result: Optional[WorkerRunResult] = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("message_type") or "") == "status_update":
                self._handle_status_update_message(message)
                continue
            if str(message.get("message_type") or "") != "nudge":
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("reason") or "") != "produce_review_verdict":
                continue
            result = self._handle_review_verdict_nudge(message, payload)
            if result.status in {"review_not_claimable", "review_nudge_invalid"}:
                skipped_result = result
                continue
            return result
        return skipped_result

    def _handle_status_update_message(self, message: JsonDict) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        if str(payload.get("schema") or "") != "mac.notifier.task_progress.v1":
            return
        try:
            result = self.status_update_sink(payload)
            self._observe_log(
                "worker.notifier.status_forwarded",
                level="info",
                detail={
                    "message_id": message.get("id"),
                    "status": result.get("status"),
                    "sent": result.get("sent", 0),
                    "skipped": result.get("skipped", 0),
                    "failed": result.get("failed", 0),
                },
            )
        except Exception as exc:  # noqa: BLE001 - notification forwarding is best effort.
            self._observe_log(
                "worker.notifier.status_forward_failed",
                level="warning",
                detail={"message_id": message.get("id"), "error": str(exc)},
            )

    def _send_status_update_to_home_channels(self, payload: JsonDict) -> JsonDict:
        channel_type = str(payload.get("channel_type") or "").strip().lower()
        target = ensure_json_object(payload.get("target"))
        target_type = str(target.get("channel_type") or "").strip().lower()
        if channel_type not in {"", "hermes", "slack"} and target_type != "slack":
            return {"status": "skipped", "sent": 0, "skipped": 1, "failed": 0}

        hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        accounts = _load_slack_accounts(hermes_home)
        home_channels = _load_slack_home_channels(hermes_home)
        if not accounts or not home_channels:
            return {"status": "skipped", "sent": 0, "skipped": 1, "failed": 0}

        try:
            from slack_sdk import WebClient  # type: ignore
        except Exception as exc:  # noqa: BLE001 - optional Hermes messaging dependency.
            return {
                "status": "failed",
                "sent": 0,
                "skipped": 0,
                "failed": 1,
                "error": "slack_sdk unavailable: %s" % exc,
            }

        notification = ensure_json_object(payload.get("notification"))
        text = _status_update_slack_text(notification)
        account_by_name = {
            str(account.get("name") or ""): account
            for account in accounts
            if str(account.get("name") or "")
        }
        target_team, target_channel = _target_slack_route(target)
        sent = 0
        failed = 0
        skipped = 0
        for channel in home_channels:
            channel_id = str(channel.get("channel_id") or "").strip()
            team_id = str(channel.get("team_id") or "").strip()
            if not channel_id:
                skipped += 1
                continue
            if target_channel and channel_id != target_channel:
                skipped += 1
                continue
            if target_team and team_id and team_id != target_team:
                skipped += 1
                continue
            account_name = str(channel.get("name") or channel.get("account") or "")
            account = account_by_name.get(account_name)
            if account is None:
                skipped += 1
                continue
            token = str(
                account.get("bot_token")
                or account.get("token")
                or account.get("slack_bot_token")
                or ""
            ).strip()
            if not token:
                skipped += 1
                continue
            try:
                response = WebClient(token=token).chat_postMessage(
                    channel=channel_id,
                    text=text,
                )
                if bool(response.get("ok", True)):
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 - continue across workspaces.
                failed += 1
                self._observe_log(
                    "worker.notifier.slack_send_failed",
                    level="warning",
                    detail={
                        "channel_id": channel_id,
                        "team_id": team_id,
                        "account": account_name,
                        "error": str(exc),
                    },
                )
        status = "sent" if sent else ("failed" if failed else "skipped")
        return {"status": status, "sent": sent, "skipped": skipped, "failed": failed}

    def _handle_review_verdict_nudge(self, message: JsonDict, payload: JsonDict) -> WorkerRunResult:
        task_id = str(payload.get("task_id") or "").strip()
        review_id = str(payload.get("review_id") or "").strip()
        executor_evidence_id = str(payload.get("executor_evidence_id") or "").strip()
        if not task_id or not review_id or not executor_evidence_id:
            error = "review verdict nudge missing task_id, review_id, or executor_evidence_id"
            self._observe_log(
                "worker.review_nudge.invalid",
                level="warning",
                detail={"message_id": message.get("id"), "error": error, "payload": payload},
            )
            return WorkerRunResult(status="review_nudge_invalid", error=error)

        try:
            claim = self.client.post(
                "/reviews/%s/claim" % quote(review_id, safe=""),
                {
                    "reviewer_agent_id": self.agent_id,
                    "executor_evidence_id": executor_evidence_id,
                    "actor": self.agent_id,
                },
            )
            if isinstance(claim, dict) and claim.get("status") != "claimed":
                return WorkerRunResult(
                    status="review_not_claimable",
                    task=(
                        claim.get("task")
                        if isinstance(claim.get("task"), dict)
                        else None
                    ),
                    error=str(claim.get("reason") or "review is not claimable"),
                )
            task_detail = self.client.get("/tasks/%s" % quote(task_id, safe=""))
            task_dir = self._prepare_review_workspace(
                task_id,
                review_id,
                executor_evidence_id,
                task_detail if isinstance(task_detail, dict) else {},
                message,
                claim if isinstance(claim, dict) else {},
            )
            started = time.monotonic()
            execution = self._call_executor(
                self._review_task_payload(task_dir),
                task_dir,
                {
                    "task_id": task_id,
                    "metadata": {
                        "execution_kind": "review",
                        "review_id": review_id,
                        "executor_evidence_id": executor_evidence_id,
                        "nudge_message_id": message.get("id"),
                    },
                },
            )
            duration_ms = (time.monotonic() - started) * 1000.0
            self._observe_metric(
                "worker.review.duration_ms",
                duration_ms,
                unit="ms",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "returncode": execution.returncode,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                },
            )
            evidence = self._record_review_execution(
                task_id,
                task_dir,
                execution,
                review_id=review_id,
                executor_evidence_id=executor_evidence_id,
                message_id=str(message.get("id") or ""),
            )
            if execution.succeeded:
                self._advance_review_workflow_after_verdict(task_id)
            else:
                self._heartbeat()
            status = "review_verdict_recorded" if execution.succeeded else "review_verdict_failed"
            self._observe_log(
                "worker.%s" % status,
                level="info" if execution.succeeded else "error",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "evidence_id": evidence.get("id"),
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                },
            )
            return WorkerRunResult(
                status=status,
                task=(task_detail.get("task") if isinstance(task_detail, dict) else None),
                evidence=evidence,
                error=None if execution.succeeded else execution.summary,
            )
        except Exception as exc:
            self._observe_log(
                "worker.review_nudge.exception",
                level="error",
                subject_type="task",
                subject_id=task_id,
                detail={
                    "message_id": message.get("id"),
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "error": str(exc),
                },
            )
            return WorkerRunResult(status="review_verdict_failed", error=str(exc))

    def _advance_review_workflow_after_verdict(self, task_id: str) -> None:
        try:
            self.client.post(
                "/reviews/default/tick?%s"
                % urlencode({"limit": 10, "actor": self.agent_id}),
                {},
            )
        except Exception as exc:  # noqa: BLE001 - verdict evidence is already recorded.
            self._observe_log(
                "worker.review_workflow.advance_failed",
                level="warning",
                subject_type="task",
                subject_id=task_id,
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )

    def _process_agentbus_control(self) -> Optional[JsonDict]:
        if not self.agentbus_control_enabled:
            return None
        try:
            processed = self._load_agentbus_control_state()
            streams = self.client.get(
                "/agentbus/streams?%s"
                % urlencode({"agent_id": self.agent_id, "status": "closed", "limit": 50})
            )
        except Exception as exc:  # noqa: BLE001 - control bus must not break task polling.
            self._observe_log(
                "worker.agentbus.control_poll_failed",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )
            return None

        if not isinstance(streams, list):
            return None
        for stream in reversed(streams):
            if not isinstance(stream, dict):
                continue
            stream_id = str(stream.get("id") or "")
            if not stream_id or stream_id in processed:
                continue
            if stream.get("recipient_agent_id") != self.agent_id:
                continue
            if stream.get("topic") != REPO_UPDATE_TOPIC:
                continue
            if str(stream.get("content_type") or "").split(";", 1)[0] != REPO_UPDATE_CONTENT_TYPE:
                continue

            result = self._handle_repo_update_stream(stream)
            processed.append(stream_id)
            self._save_agentbus_control_state(processed)
            self._publish_repo_update_result(stream, result)
            if result.get("restart_requested"):
                return result
        return None

    def _handle_repo_update_stream(self, stream: JsonDict) -> JsonDict:
        stream_id = str(stream.get("id") or "")
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(stream_id, safe=""),
                urlencode({"agent_id": self.agent_id, "after_sequence": 0, "limit": 10}),
            )
        )
        payload: Any = None
        if isinstance(chunks, list) and chunks:
            payload = chunks[-1].get("payload") if isinstance(chunks[-1], dict) else None
        try:
            result = self._execute_repo_update(payload, stream_id)
        except Exception as exc:  # noqa: BLE001 - malformed control messages should report failure.
            result = self._repo_update_result(
                stream_id,
                "error",
                "repo update handler failed: %s" % exc,
                {},
            )
        self._observe_log(
            "worker.agentbus.repo_update.%s" % result["status"],
            level="info" if result["status"] in {"updated", "no_update", "skipped"} else "error",
            detail=result,
        )
        return result

    def _execute_repo_update(self, payload: Any, stream_id: str) -> JsonDict:
        request: JsonDict = payload if isinstance(payload, dict) else {}
        if request.get("schema") not in {None, "", REPO_UPDATE_SCHEMA}:
            return self._repo_update_result(
                stream_id,
                "error",
                "unsupported repo update schema: %s" % request.get("schema"),
                request,
            )

        repo = self.self_update_repo.expanduser()
        requested_repo = str(request.get("repo_path") or "").strip()
        if requested_repo:
            try:
                if Path(requested_repo).expanduser().resolve() != repo.resolve():
                    return self._repo_update_result(
                        stream_id,
                        "error",
                        "repo_path does not match this listener's configured update repo",
                        request,
                        repo_path=str(repo),
                    )
            except OSError as exc:
                return self._repo_update_result(
                    stream_id,
                    "error",
                    "could not resolve repo_path: %s" % exc,
                    request,
                    repo_path=str(repo),
                )

        remote = str(request.get("remote") or "origin").strip()
        branch = str(request.get("branch") or "").strip()
        restart = bool(request.get("restart", True))
        if not _safe_git_ref(remote):
            return self._repo_update_result(
                stream_id,
                "error",
                "invalid git remote name",
                request,
                repo_path=str(repo),
            )
        if branch and not _safe_git_ref(branch):
            return self._repo_update_result(
                stream_id,
                "error",
                "invalid git branch/ref name",
                request,
                repo_path=str(repo),
            )
        if not repo.exists():
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo does not exist",
                request,
                repo_path=str(repo),
            )

        inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo is not a git worktree",
                request,
                repo_path=str(repo),
                stderr=inside.stderr,
            )

        dirty = _run_git(repo, ["status", "--porcelain"])
        if dirty.returncode != 0:
            return self._repo_update_result(
                stream_id,
                "error",
                "could not inspect git status",
                request,
                repo_path=str(repo),
                stderr=dirty.stderr,
            )
        if dirty.stdout.strip():
            return self._repo_update_result(
                stream_id,
                "skipped",
                "self-update repo has local modifications",
                request,
                repo_path=str(repo),
            )

        before = _run_git(repo, ["rev-parse", "HEAD"])
        before_sha = before.stdout.strip() if before.returncode == 0 else ""
        pull_args = ["pull", "--ff-only"]
        if branch:
            pull_args.extend([remote, branch])
        pulled = _run_git(repo, pull_args)
        if pulled.returncode != 0:
            return self._repo_update_result(
                stream_id,
                "error",
                "git pull --ff-only failed",
                request,
                repo_path=str(repo),
                before_sha=before_sha,
                stdout=pulled.stdout,
                stderr=pulled.stderr,
            )

        after = _run_git(repo, ["rev-parse", "HEAD"])
        after_sha = after.stdout.strip() if after.returncode == 0 else ""
        updated = bool(before_sha and after_sha and before_sha != after_sha)
        return self._repo_update_result(
            stream_id,
            "updated" if updated else "no_update",
            "repo updated; restart requested" if updated and restart else "repo already current",
            request,
            repo_path=str(repo),
            before_sha=before_sha,
            after_sha=after_sha,
            stdout=pulled.stdout,
            stderr=pulled.stderr,
            restart_requested=updated and restart,
        )

    def _repo_update_result(
        self,
        stream_id: str,
        status: str,
        summary: str,
        request: JsonDict,
        **extra: Any,
    ) -> JsonDict:
        result: JsonDict = {
            "schema": REPO_UPDATE_RESULT_SCHEMA,
            "status": status,
            "summary": summary,
            "agent_id": self.agent_id,
            "stream_id": stream_id,
            "request_id": request.get("request_id"),
            "restart_requested": bool(extra.pop("restart_requested", False)),
        }
        for key, value in extra.items():
            if isinstance(value, str):
                result[key] = value[:4000]
            else:
                result[key] = value
        return result

    def _publish_repo_update_result(self, stream: JsonDict, result: JsonDict) -> None:
        sender = str(stream.get("sender_agent_id") or "")
        if not sender:
            return
        try:
            self.client.post(
                "/agentbus",
                {
                    "sender_agent_id": self.agent_id,
                    "recipient_agent_id": sender,
                    "content_type": REPO_UPDATE_RESULT_CONTENT_TYPE,
                    "topic": REPO_UPDATE_RESULT_TOPIC,
                    "payload": result,
                },
            )
        except Exception as exc:  # noqa: BLE001 - result publishing is best-effort.
            self._observe_log(
                "worker.agentbus.repo_update_result_failed",
                level="warning",
                detail={"stream_id": stream.get("id"), "error": str(exc)},
            )

    def _load_agentbus_control_state(self) -> List[str]:
        try:
            loaded = json.loads(self.agentbus_control_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []
        values = loaded.get("processed_stream_ids") if isinstance(loaded, dict) else []
        if not isinstance(values, list):
            return []
        return [str(value) for value in values if str(value)]

    def _save_agentbus_control_state(self, processed_stream_ids: List[str]) -> None:
        try:
            self.agentbus_control_state_path.parent.mkdir(parents=True, exist_ok=True)
            deduped = list(dict.fromkeys(processed_stream_ids))[-500:]
            self.agentbus_control_state_path.write_text(
                json.dumps({"processed_stream_ids": deduped}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - state loss should not break task polling.
            self._observe_log(
                "worker.agentbus.control_state_write_failed",
                level="warning",
                detail={"path": str(self.agentbus_control_state_path), "error": str(exc)},
            )

    def _claim_payload(self, dry_run: bool) -> JsonDict:
        return {
            "lease_seconds": self.lease_seconds,
            "allowed_projects": self.allowed_projects,
            "required_metadata": self.required_metadata,
            "require_canary": self.require_canary,
            "dry_run": dry_run,
        }

    def _policy_payload(self) -> JsonDict:
        return {
            "allowed_projects": self.allowed_projects,
            "required_metadata": self.required_metadata,
            "require_canary": self.require_canary,
        }

    def _observe_policy_once(self) -> None:
        if self._declared_policy:
            return
        self._declared_policy = True
        self._observe_log(
            "worker.routing.policy",
            detail={"agent_id": self.agent_id, "policy": self._policy_payload()},
        )

    def _prepare_task_workspace(self, task: JsonDict, lease: JsonDict) -> Path:
        task_dir = self.workspace / _safe_path_component(task["id"])
        task_dir.mkdir(parents=True, exist_ok=True)
        repository_context = self._prepare_repository_worktree(task, lease, task_dir)
        if repository_context is not None:
            metadata = task.setdefault("metadata", {})
            if isinstance(metadata, dict):
                runtime = metadata.setdefault("runtime", {})
                if isinstance(runtime, dict):
                    runtime.update(repository_context)
            (task_dir / "repository-worktree.json").write_text(
                json.dumps(repository_context, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        (task_dir / "task.json").write_text(
            json.dumps({"task": task, "lease": lease}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

    def _prepare_repository_worktree(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
    ) -> Optional[JsonDict]:
        origin = _repository_task_origin(task)
        if origin is None:
            return None
        # K8s mode: when there is no usable local source on disk, fall
        # back to ``git clone <remote>`` into the task workspace. The
        # local-path branch is preferred when both are available (host
        # workers continue to use their pre-existing checkout). See
        # CLAUDE.md fork-audit notes for context.
        repository_path = str(origin.get("repository_path") or "").strip()
        local_source: Optional[Path] = None
        if repository_path:
            candidate = self._resolve_repository_source_path(origin)
            if candidate.exists():
                local_source = candidate
        remote_url = self._resolve_repository_remote_url(origin)
        if local_source is None:
            if remote_url:
                return self._prepare_repository_worktree_from_remote(
                    task, lease, task_dir, origin, remote_url
                )
            if repository_path:
                raise RuntimeError(
                    "repository source path does not exist: %s; tried %s"
                    % (
                        repository_path,
                        ", ".join(
                            str(c)
                            for c in _repository_source_candidates(origin, self.self_update_repo)
                        ),
                    )
                )
            raise RuntimeError(
                "repository task origin has neither a local repository_path "
                "nor a repository_url (or MAC_TASK_REPO_URL env)"
            )
        source = local_source

        top_level = _run_git(source, ["rev-parse", "--show-toplevel"])
        if top_level.returncode != 0 or not top_level.stdout.strip():
            raise RuntimeError(
                "repository source path is not a git worktree: %s" % source
            )
        source_root = Path(top_level.stdout.strip()).resolve()
        inside = _run_git(source_root, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeError(
                "repository source path is not a git worktree: %s" % source_root
            )

        dirty = _run_git(source_root, ["status", "--porcelain"])
        if dirty.returncode != 0:
            raise RuntimeError(
                "could not inspect repository source status: %s"
                % ((dirty.stderr or dirty.stdout or "").strip() or source_root)
            )
        dirty_paths = [line.strip() for line in dirty.stdout.splitlines() if line.strip()]
        if dirty_paths:
            self._observe_log(
                "worker.repository.source_dirty",
                level="warning",
                subject_type="task",
                subject_id=str(task.get("id") or ""),
                detail={
                    "repository_path": str(source_root),
                    "dirty_paths": dirty_paths[:50],
                    "dirty_path_count": len(dirty_paths),
                },
            )
            raise RuntimeError(
                "repository source checkout is dirty; refusing to run task outside an isolated clean base: %s"
                % source_root
            )

        head = _run_git(source_root, ["rev-parse", "HEAD"])
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(
                "could not resolve repository source HEAD: %s"
                % ((head.stderr or head.stdout or "").strip() or source_root)
            )
        base_sha = head.stdout.strip()
        branch = _task_worktree_branch(self.agent_id, str(task.get("id") or ""), str(lease.get("id") or ""))
        worktree_dir = task_dir / ("repo-" + _safe_path_component(str(lease.get("id") or "lease")))
        if worktree_dir.exists():
            existing_head = _run_git(worktree_dir, ["rev-parse", "HEAD"])
            if existing_head.returncode == 0 and existing_head.stdout.strip():
                raise RuntimeError(
                    "repository task worktree already exists for this lease: %s" % worktree_dir
                )
            shutil.rmtree(worktree_dir)
        # mac-3qv6: prune any orphaned worktree registration in
        # source_root/.git/worktrees that points at the now-deleted
        # directory. Without this, `git worktree add` below fails with
        # "already exists" even though the on-disk directory is gone.
        _run_git(source_root, ["worktree", "prune"])

        add = _run_git(
            source_root,
            ["worktree", "add", "-b", branch, str(worktree_dir), base_sha],
        )
        if add.returncode != 0:
            raise RuntimeError(
                "could not create repository task worktree: %s"
                % ((add.stderr or add.stdout or "").strip() or worktree_dir)
            )
        remote = _run_git(source_root, ["remote", "get-url", "origin"])
        context: JsonDict = {
            "schema": "mac.repository_task_worktree.v1",
            "checkout_policy": "task_owned_git_worktree",
            "repository_declared_path": str(origin.get("repository_path") or ""),
            "repository_source_path": str(source_root),
            "repository_worktree": str(worktree_dir),
            "repository_branch": branch,
            "repository_base_sha": base_sha,
            "repository_origin_remote": remote.stdout.strip() if remote.returncode == 0 else "",
        }
        self._observe_log(
            "worker.repository.worktree_prepared",
            subject_type="task",
            subject_id=str(task.get("id") or ""),
            detail=context,
        )
        return context

    def _resolve_repository_source_path(self, origin: JsonDict) -> Path:
        for candidate in _repository_source_candidates(origin, self.self_update_repo):
            if candidate.exists():
                return candidate
        return Path(str(origin.get("repository_path") or "")).expanduser()

    def _resolve_repository_remote_url(self, origin: JsonDict) -> str:
        """Return the remote clone URL for the K8s clone path, or "".

        The task ``origin.repository_url`` takes precedence; if the task
        does not carry one, ``MAC_TASK_REPO_URL`` from the environment is
        consulted (the K8s Job pod injects this). Empty string means
        "no remote URL available."""
        raw = str(origin.get("repository_url") or "").strip()
        if not raw:
            raw = os.environ.get("MAC_TASK_REPO_URL", "").strip()
        if not raw:
            return ""
        return _validate_git_remote_url(raw)

    def _prepare_repository_worktree_from_remote(
        self,
        task: JsonDict,
        lease: JsonDict,
        task_dir: Path,
        origin: JsonDict,
        remote_url: str,
    ) -> JsonDict:
        """K8s-mode repository preparation: clone the remote into a
        per-lease directory and check out a task branch.

        This produces the same ``mac.repository_task_worktree.v1`` context
        shape as the local-worktree branch so downstream evidence,
        ``_load_repository_context`` and verification stay unchanged.
        """
        worktree_dir = task_dir / (
            "repo-" + _safe_path_component(str(lease.get("id") or "lease"))
        )
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir)
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        default_branch = str(origin.get("default_branch") or "").strip()
        if not default_branch:
            default_branch = os.environ.get("MAC_TASK_REPO_DEFAULT_BRANCH", "").strip()
        if not default_branch:
            default_branch = "main"
        _validate_git_ref(default_branch)

        auth_url = _inject_git_remote_auth(remote_url)
        clone_args = ["clone", "--depth=1", "--branch", default_branch, "--", auth_url, str(worktree_dir)]
        # ``git -C`` requires an existing directory; clone runs from the
        # parent so we use a separate code path (the helper expects the
        # repo arg to be cwd, so call git directly here).
        clone = _run_git_in(task_dir, clone_args)
        if clone.returncode != 0:
            raise RuntimeError(
                "could not clone repository for K8s task: %s"
                % ((clone.stderr or clone.stdout or "").strip() or remote_url)
            )

        head = _run_git(worktree_dir, ["rev-parse", "HEAD"])
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(
                "could not resolve cloned repository HEAD: %s"
                % ((head.stderr or head.stdout or "").strip() or worktree_dir)
            )
        base_sha = head.stdout.strip()
        branch = _task_worktree_branch(
            self.agent_id, str(task.get("id") or ""), str(lease.get("id") or "")
        )
        checkout = _run_git(worktree_dir, ["checkout", "-b", branch])
        if checkout.returncode != 0:
            raise RuntimeError(
                "could not create task branch in cloned repository: %s"
                % ((checkout.stderr or checkout.stdout or "").strip() or branch)
            )

        # Mirror the local-worktree context shape exactly; downstream
        # readers (evidence validators, _load_repository_context) treat
        # the K8s clone identically to a host-mode git worktree.
        context: JsonDict = {
            "schema": "mac.repository_task_worktree.v1",
            "checkout_policy": "k8s_task_owned_clone",
            "repository_declared_path": str(origin.get("repository_path") or ""),
            "repository_source_path": str(worktree_dir),
            "repository_worktree": str(worktree_dir),
            "repository_branch": branch,
            "repository_base_sha": base_sha,
            "repository_origin_remote": remote_url,
        }
        self._observe_log(
            "worker.repository.worktree_prepared",
            subject_type="task",
            subject_id=str(task.get("id") or ""),
            detail=context,
        )
        return context

    def _prepare_review_workspace(
        self,
        task_id: str,
        review_id: str,
        executor_evidence_id: str,
        task_detail: JsonDict,
        message: JsonDict,
        claim_result: Optional[JsonDict] = None,
    ) -> Path:
        task_dir = self.workspace / "_reviews" / _safe_path_component(review_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        claim = ensure_json_object(claim_result)
        review_repository_context = self._prepare_review_repository_worktree(
            task_dir,
            task_detail,
            executor_evidence_id,
            review_id,
        )
        # Write the specific evidence and the original task as discrete workspace
        # files so the hermes executor can read them on demand.  This keeps the
        # review_context — and therefore the hermes --query prompt — to IDs only,
        # avoiding ARG_MAX blowup as evidence accumulates over a task's lifetime.
        executor_evidence = _task_detail_evidence(task_detail, executor_evidence_id)
        (task_dir / "executor-evidence.json").write_text(
            json.dumps(executor_evidence, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (task_dir / "executor-task.json").write_text(
            json.dumps(task_detail.get("task", {}), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        review_context: JsonDict = {
            "task_id": task_id,
            "review_id": review_id,
            "executor_evidence_id": executor_evidence_id,
            "nudge_message_id": message.get("id"),
            "review_claim": (
                claim.get("claim")
                if isinstance(claim.get("claim"), dict)
                else {}
            ),
        }
        if review_repository_context is not None:
            review_context["review_repository_worktree"] = review_repository_context
        task = {
            "id": "review_%s" % review_id,
            "title": "Review task %s" % task_id,
            "description": (
                "Review the executor evidence for task %s and write a signed "
                "review_verdict manifest." % task_id
            ),
            "required_capabilities": ["review"],
            "metadata": {
                "review_context": review_context,
            },
        }
        if review_repository_context is not None:
            task["metadata"]["runtime"] = review_repository_context
        (task_dir / "task.json").write_text(
            json.dumps({"task": task}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return task_dir

    def _prepare_review_repository_worktree(
        self,
        task_dir: Path,
        task_detail: JsonDict,
        executor_evidence_id: str,
        review_id: str,
    ) -> Optional[JsonDict]:
        evidence = _task_detail_evidence(task_detail, executor_evidence_id)
        manifest = ensure_json_object(
            ensure_json_object(evidence.get("metadata")).get("verification")
        )
        repo = ensure_json_object(manifest.get("repo"))
        head_sha = str(repo.get("head_sha") or "").strip()
        if not GIT_SHA_RE.match(head_sha):
            return None
        remote_ref = str(repo.get("remote_ref") or "").strip()
        remote_url = str(
            repo.get("remote_url")
            or repo.get("origin_url")
            or repo.get("clone_url")
            or ""
        ).strip()
        if not remote_url:
            repo_path_raw = str(repo.get("path") or "").strip()
            repo_path = Path(repo_path_raw).expanduser() if repo_path_raw else None
            if repo_path is not None and repo_path.exists():
                remote = _run_git(repo_path, ["remote", "get-url", "origin"])
                if remote.returncode == 0:
                    remote_url = remote.stdout.strip()
        if not remote_url:
            return None

        # mac-raud: reject hostile remote_url before it reaches git argv.
        try:
            remote_url = _validate_git_remote_url(remote_url)
        except ValueError as exc:
            raise RuntimeError("refusing review clone: %s" % exc) from None
        if remote_ref:
            try:
                remote_ref = _validate_git_ref(remote_ref)
            except ValueError as exc:
                raise RuntimeError("refusing review clone: %s" % exc) from None

        review_repo = task_dir / "review-repo"
        if review_repo.exists():
            shutil.rmtree(review_repo)
        # `--` separator means a remote_url that survives validation
        # still cannot be parsed as a git option.
        clone = subprocess.run(
            ["git", "clone", "--no-checkout", "--", remote_url, str(review_repo)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if clone.returncode != 0:
            raise RuntimeError(
                "could not clone review repository %s: %s"
                % (remote_url, (clone.stderr or clone.stdout or "").strip())
            )

        branch = _remote_branch_from_ref(remote_ref)
        if branch:
            fetch = _run_git(
                review_repo,
                [
                    "fetch",
                    "origin",
                    "+refs/heads/%s:refs/remotes/origin/%s" % (branch, branch),
                ],
            )
        elif remote_ref:
            # remote_ref was validated above; `--` guards against any
            # ref that pattern-matched a flag (mac-raud).
            fetch = _run_git(review_repo, ["fetch", "origin", "--", remote_ref])
        else:
            fetch = _run_git(review_repo, ["fetch", "origin"])
        if fetch.returncode != 0:
            raise RuntimeError(
                "could not fetch reviewed ref %s: %s"
                % (remote_ref or "origin", (fetch.stderr or fetch.stdout or "").strip())
            )

        checkout = _run_git(review_repo, ["checkout", "--detach", head_sha])
        if checkout.returncode != 0:
            raise RuntimeError(
                "could not checkout reviewed head %s: %s"
                % (head_sha, (checkout.stderr or checkout.stdout or "").strip())
            )
        context: JsonDict = {
            "schema": "mac.review_repository_worktree.v1",
            "checkout_policy": "review_git_worktree",
            "repository_worktree": str(review_repo),
            "repository_source_path": str(repo.get("path") or ""),
            "repository_branch": remote_ref or branch or "",
            "repository_base_sha": head_sha,
            "repository_origin_remote": remote_url,
            "repository_review_id": review_id,
            "repository_executor_evidence_id": executor_evidence_id,
            "repository_reviewed_head_sha": head_sha,
            "repository_reviewed_remote_ref": remote_ref,
        }
        (task_dir / "repository-worktree.json").write_text(
            json.dumps(context, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._observe_log(
            "worker.review.repository_worktree_prepared",
            subject_type="task",
            subject_id=str((task_detail.get("task") or {}).get("id") or ""),
            detail=context,
        )
        return context

    def _review_task_payload(self, task_dir: Path) -> JsonDict:
        loaded = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        task = loaded.get("task", loaded)
        return task if isinstance(task, dict) else loaded

    def _record_execution(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
    ) -> JsonDict:
        (task_dir / "stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (task_dir / "stderr.txt").write_text(execution.stderr, encoding="utf-8")
        if execution.succeeded:
            self._auto_publish_repository_worktree(task_id, task_dir)
        result_path = task_dir / "worker-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                    "metadata": self._execution_metadata(task_dir, execution),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        metadata = self._execution_metadata(task_dir, execution)
        return self.client.post(
            "/tasks/%s/evidence" % quote(task_id, safe=""),
            {
                "kind": "log",
                "uri": result_path.resolve().as_uri(),
                "summary": execution.summary,
                "created_by": self.agent_id,
                "metadata": {
                    "returncode": execution.returncode,
                    "stdout": (task_dir / "stdout.txt").resolve().as_uri(),
                    "stderr": (task_dir / "stderr.txt").resolve().as_uri(),
                    **metadata,
                },
            },
        )

    def _auto_publish_repository_worktree(self, task_id: str, task_dir: Path) -> None:
        task = _task_payload_from_workspace(task_dir)
        metadata = ensure_json_object(task.get("metadata"))
        if not (
            metadata.get("repository_auto_publish") is True
            or metadata.get("auto_commit_repository") is True
            or os.environ.get("MAC_WORKER_REPOSITORY_AUTO_PUBLISH", "").lower()
            in {"1", "true", "yes"}
        ):
            return
        context = _load_repository_context(task_dir)
        if not context:
            return
        worktree = Path(str(context.get("repository_worktree") or "")).expanduser()
        if not worktree.exists():
            raise RuntimeError("repository auto-publish worktree missing: %s" % worktree)
        branch = str(context.get("repository_branch") or "").strip()
        if not branch:
            raise RuntimeError("repository auto-publish missing repository_branch")

        status = _run_git(worktree, ["status", "--porcelain"])
        if status.returncode != 0:
            raise RuntimeError(
                "repository auto-publish status failed: %s"
                % ((status.stderr or status.stdout or "").strip() or worktree)
            )
        if status.stdout.strip():
            add = _run_git(worktree, ["add", "-A"])
            if add.returncode != 0:
                raise RuntimeError(
                    "repository auto-publish add failed: %s"
                    % ((add.stderr or add.stdout or "").strip() or worktree)
                )
            staged = _run_git(worktree, ["diff", "--cached", "--quiet"])
            if staged.returncode == 1:
                title = str(task.get("title") or task_id).strip() or task_id
                commit = _run_git(
                    worktree,
                    ["commit", "-m", "MAC task %s: %s" % (task_id, title[:120])],
                )
                if commit.returncode != 0:
                    raise RuntimeError(
                        "repository auto-publish commit failed: %s"
                        % ((commit.stderr or commit.stdout or "").strip() or worktree)
                    )
            elif staged.returncode != 0:
                raise RuntimeError(
                    "repository auto-publish staged diff failed: %s"
                    % ((staged.stderr or staged.stdout or "").strip() or worktree)
                )

        push = _run_git(worktree, ["push", "origin", "HEAD:refs/heads/%s" % branch])
        if push.returncode != 0:
            raise RuntimeError(
                "repository auto-publish push failed: %s"
                % ((push.stderr or push.stdout or "").strip() or branch)
            )
        head = _run_git(worktree, ["rev-parse", "HEAD"])
        remote = _run_git(worktree, ["ls-remote", "origin", "refs/heads/%s" % branch])
        remote_sha = (remote.stdout.split() or [""])[0] if remote.returncode == 0 else ""
        if head.returncode != 0 or not head.stdout.strip() or remote_sha != head.stdout.strip():
            raise RuntimeError("repository auto-publish remote verification failed for %s" % branch)

        self._observe_log(
            "worker.repository.auto_published",
            subject_type="task",
            subject_id=task_id,
            detail={
                "repository_worktree": str(worktree),
                "repository_branch": branch,
                "head_sha": head.stdout.strip(),
                "remote_ref": "refs/heads/%s" % branch,
            },
        )

    def _execution_submission_problems(self, task_dir: Path, evidence: JsonDict) -> List[str]:
        problems: List[str] = []
        metadata = evidence.get("metadata") if isinstance(evidence, dict) else None
        manifest = metadata.get("verification") if isinstance(metadata, dict) else None
        task_payload = _task_payload_from_workspace(task_dir)
        if not isinstance(manifest, dict):
            return ["evidence metadata lacks verification manifest"]
        if str(manifest.get("schema") or "").strip() != VERIFICATION_SCHEMA:
            problems.append("verification.schema must be %s" % VERIFICATION_SCHEMA)
        if str(manifest.get("status") or "").strip().lower() != "complete":
            problems.append('verification.status must be "complete"')
        evidence_type = str(manifest.get("evidence_type") or "").strip().lower()
        if not evidence_type:
            problems.append("verification.evidence_type is required")
        if not str(manifest.get("signed_by") or "").strip() or not str(manifest.get("signature") or "").strip():
            problems.append("verification.signed_by and verification.signature are required")
        if evidence_type:
            problems.extend(
                _worker_verification_contract_problems(
                    manifest,
                    evidence_type,
                    allow_empty_repo_change=_worker_allows_empty_repo_change_evidence(
                        task_payload,
                        evidence_type,
                    ),
                )
            )
            problems.extend(_worker_required_changed_file_problems(task_payload, manifest))

        repository_context = _load_repository_context(task_dir)
        if repository_context:
            worktree = Path(str(repository_context.get("repository_worktree") or ""))
            if not worktree.exists():
                problems.append("repository worktree is missing: %s" % worktree)
            else:
                dirty = _run_git(worktree, ["status", "--porcelain"])
                if dirty.returncode != 0:
                    problems.append(
                        "could not inspect repository worktree status: %s"
                        % ((dirty.stderr or dirty.stdout or "").strip() or worktree)
                    )
                elif dirty.stdout.strip():
                    problems.append("repository worktree has uncommitted changes")
                head = _run_git(worktree, ["rev-parse", "HEAD"])
                repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
                manifest_head = str(repo.get("head_sha") or "").strip() if isinstance(repo, dict) else ""
                if head.returncode == 0 and manifest_head and head.stdout.strip() != manifest_head:
                    problems.append("verification.repo.head_sha does not match worktree HEAD")
        return problems

    def _record_review_execution(
        self,
        task_id: str,
        task_dir: Path,
        execution: WorkerExecution,
        *,
        review_id: str,
        executor_evidence_id: str,
        message_id: str,
    ) -> JsonDict:
        (task_dir / "stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (task_dir / "stderr.txt").write_text(execution.stderr, encoding="utf-8")
        result_path = task_dir / "review-result.json"
        metadata = self._execution_metadata(task_dir, execution)
        result_path.write_text(
            json.dumps(
                {
                    "returncode": execution.returncode,
                    "summary": execution.summary,
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "metadata": metadata,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return self.client.post(
            "/tasks/%s/evidence" % quote(task_id, safe=""),
            {
                "kind": "review",
                "uri": result_path.resolve().as_uri(),
                "summary": execution.summary,
                "created_by": self.agent_id,
                "metadata": {
                    "returncode": execution.returncode,
                    "stdout": (task_dir / "stdout.txt").resolve().as_uri(),
                    "stderr": (task_dir / "stderr.txt").resolve().as_uri(),
                    "review_id": review_id,
                    "executor_evidence_id": executor_evidence_id,
                    "nudge_message_id": message_id,
                    **metadata,
                },
            },
        )

    def _execution_metadata(self, task_dir: Path, execution: WorkerExecution) -> JsonDict:
        metadata = dict(execution.metadata)
        manifest = metadata.get("verification") or self._load_verification_manifest(task_dir)
        manifest = _enrich_verification_manifest_from_repository_context(
            ensure_json_object(manifest),
            _load_repository_context(task_dir),
        )
        metadata["verification"] = self._sign_verification_manifest(manifest)
        metadata.setdefault(
            "workspace_outputs",
            {
                "stdout_sha256": _sha256_file(task_dir / "stdout.txt"),
                "stderr_sha256": _sha256_file(task_dir / "stderr.txt"),
            },
        )
        return metadata

    def _sign_verification_manifest(self, manifest: JsonDict) -> JsonDict:
        """Stamp ``signed_by`` + ``signature`` onto the manifest if an
        attestation key is configured (mac-ng2). Without a key the
        manifest is returned unmodified — the default-review workflow
        will then refuse the evidence as ``manifest_not_signed``,
        which is the correct outcome for an unkeyed worker."""
        if not self.attestation_key or not isinstance(manifest, dict):
            return manifest
        from mac.services import sign_verification_manifest

        signed = dict(manifest)
        signed["signed_by"] = self.agent_id
        signed["signature"] = sign_verification_manifest(self.attestation_key, signed)
        return signed

    def _load_verification_manifest(self, task_dir: Path) -> JsonDict:
        manifest_path = task_dir / "mac-evidence.json"
        if not manifest_path.exists():
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "missing",
                "problems": ["mac-evidence.json was not produced by the executor"],
            }
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - malformed evidence should be captured, not crash reporting
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "invalid",
                "problems": ["could not parse mac-evidence.json: %s" % exc],
                "uri": manifest_path.resolve().as_uri(),
            }
        if not isinstance(loaded, dict):
            return {
                "schema": "mac.worker_evidence.v1",
                "status": "invalid",
                "problems": ["mac-evidence.json must contain a JSON object"],
                "uri": manifest_path.resolve().as_uri(),
            }
        loaded.setdefault("schema", "mac.worker_evidence.v1")
        loaded.setdefault("uri", manifest_path.resolve().as_uri())
        loaded.setdefault("sha256", _sha256_file(manifest_path))
        return loaded

    def _call_executor(
        self,
        task: JsonDict,
        task_dir: Path,
        audit_context: JsonDict,
    ) -> WorkerExecution:
        if isinstance(self.executor, SubprocessExecutor):
            prior_context = self.executor.audit_context
            self.executor.audit_context = audit_context
            try:
                return self.executor(task, task_dir)
            finally:
                self.executor.audit_context = prior_context
        return self.executor(task, task_dir)

    def _record_command_audit(self, record: JsonDict) -> None:
        payload = {
            "command_id": record.get("command_id"),
            "phase": record.get("phase"),
            "argv": record.get("argv") or [],
            "cwd": record.get("cwd") or "",
            "task_id": record.get("task_id"),
            "lease_id": record.get("lease_id"),
            "started_at": record.get("started_at"),
            "completed_at": record.get("completed_at"),
            "duration_ms": record.get("duration_ms"),
            "returncode": record.get("returncode"),
            "stdout_sha256": record.get("stdout_sha256"),
            "stderr_sha256": record.get("stderr_sha256"),
            "stdout_bytes": record.get("stdout_bytes"),
            "stderr_bytes": record.get("stderr_bytes"),
            "metadata": record.get("metadata") or {},
        }
        try:
            self.client.post(
                "/agents/%s/command-audit" % quote(self.agent_id, safe=""),
                payload,
            )
        except Exception:
            pass

    def _heartbeat(self) -> None:
        payload: JsonDict = {"status": "idle"}
        # Declare the build the agent is running. Send the digest at most once
        # per process; subsequent heartbeats are pure liveness pings.
        if self.running_digest and not self._declared_digest:
            payload["running_digest"] = self.running_digest
        self.client.post(
            "/agents/%s/heartbeat" % quote(self.agent_id, safe=""),
            payload,
        )
        if self.running_digest and not self._declared_digest:
            self._declared_digest = True

    def _observe_metric(
        self,
        name: str,
        value: float,
        unit: str = "",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        self._post_observation(
            "/observability/metrics",
            {
                "name": name,
                "value": value,
                "unit": unit,
                "layer": "worker",
                "source": self.agent_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "detail": detail or {},
            },
        )

    def _observe_log(
        self,
        name: str,
        level: str = "info",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        self._post_observation(
            "/observability/logs",
            {
                "name": name,
                "level": level,
                "layer": "worker",
                "source": self.agent_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "detail": detail or {},
            },
        )

    def _post_observation(self, path: str, payload: JsonDict) -> None:
        try:
            self.client.post(path, payload)
        except Exception:
            pass


def _summary_from_output(returncode: int, stdout: str, stderr: str) -> str:
    stream = stdout if stdout.strip() else stderr
    first_line = next((line.strip() for line in stream.splitlines() if line.strip()), "")
    if first_line:
        return first_line[:500]
    return "executor completed" if returncode == 0 else "executor failed with returncode %d" % returncode


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _command_audit_id() -> str:
    seed = "%s:%s:%s" % (time.time_ns(), os.getpid(), threading.get_ident())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _sha256_text(value: str) -> str:
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_json_object(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, dict) else {}


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_slack_accounts(hermes_home: Path) -> List[JsonDict]:
    data = _load_json_file(hermes_home / "slack_accounts.json")
    if isinstance(data, dict):
        raw_accounts = data.get("accounts") or data.get("workspaces") or data
        if isinstance(raw_accounts, dict):
            raw_accounts = [
                {**ensure_json_object(value), "name": key}
                for key, value in raw_accounts.items()
            ]
    else:
        raw_accounts = data
    if not isinstance(raw_accounts, list):
        return []
    return [ensure_json_object(item) for item in raw_accounts if isinstance(item, dict)]


def _load_slack_home_channels(hermes_home: Path) -> List[JsonDict]:
    data = _load_json_file(hermes_home / "slack_home_channels.json")
    if isinstance(data, dict):
        raw_channels = data.get("channels") or data.get("home_channels") or data
        if isinstance(raw_channels, dict):
            raw_channels = [
                {**ensure_json_object(value), "name": key}
                for key, value in raw_channels.items()
            ]
    else:
        raw_channels = data
    if not isinstance(raw_channels, list):
        return []
    return [ensure_json_object(item) for item in raw_channels if isinstance(item, dict)]


def _target_slack_route(target: JsonDict) -> tuple[str, str]:
    team_id = str(target.get("team_id") or "").strip()
    channel_id = str(target.get("channel_id") or "").strip()
    external_id = str(target.get("external_id") or "").strip()
    if external_id and "/" in external_id:
        raw_team, raw_channel = external_id.split("/", 1)
        team_id = team_id or raw_team.strip()
        channel_id = channel_id or raw_channel.strip()
    return team_id, channel_id


def _status_update_slack_text(notification: JsonDict) -> str:
    title = str(notification.get("title") or "Task update").strip()
    body = str(notification.get("body") or "").strip()
    event_type = str(notification.get("event_type") or "").strip()
    subject_id = str(notification.get("subject_id") or "").strip()
    lines = [title]
    if body and body != title:
        lines.append(body)
    context = " ".join(item for item in (event_type, subject_id) if item)
    if context:
        lines.append(context)
    return "\n".join(lines)[:3000]


def _coerce_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _audit_safe_argv(argv: List[str]) -> List[str]:
    safe: List[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            safe.append(_redacted_arg(arg))
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--key", "--secret", "--password"}:
            safe.append(arg)
            redact_next = True
            continue
        if any(marker in lowered for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")):
            safe.append(_redacted_arg(arg))
            continue
        if len(arg) > 512:
            safe.append("<truncated:%s:chars=%d>" % (_sha256_text(arg), len(arg)))
            continue
        safe.append(arg)
    return safe


def _redacted_arg(value: str) -> str:
    return "<redacted:%s:chars=%d>" % (_sha256_text(value), len(value))


def _safe_path_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return ""
    return "sha256:%s" % digest.hexdigest()


def _default_self_update_repo() -> Path:
    configured = os.environ.get("MAC_SELF_UPDATE_REPO")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2]


def _repository_task_origin(task: JsonDict) -> Optional[JsonDict]:
    metadata = task.get("metadata") if isinstance(task, dict) else None
    if not isinstance(metadata, dict):
        return None
    origin = metadata.get("origin")
    if not isinstance(origin, dict):
        return None
    repository_path = str(origin.get("repository_path") or "").strip()
    repository_url = str(origin.get("repository_url") or "").strip()
    # mac-k8s clone path: allow tasks that ship only a remote URL (the
    # Job pod has no local source). Either a local path or a remote URL
    # is now sufficient to identify a repository-mode task.
    if not repository_path and not repository_url:
        return None

    # Dirty-source remediation tasks are the one explicit exception: their
    # purpose is to repair the registered checkout itself.
    remediation = metadata.get("remediation")
    if isinstance(remediation, dict) and remediation.get("type") == "beads_source_refresh":
        return None
    if origin.get("type") == "beads_source_remediation":
        return None

    contract = origin.get("repository_contract")
    execution_contract = metadata.get("execution_contract")
    if isinstance(execution_contract, dict) and execution_contract.get("type") == "repository":
        return dict(origin)
    if isinstance(contract, dict) and contract.get("schema"):
        return dict(origin)
    if str(origin.get("type") or "") in {"beads", "direct_task"}:
        return dict(origin)
    return None


def _repository_source_candidates(origin: JsonDict, self_update_repo: Path) -> List[Path]:
    candidates: List[Path] = []
    raw = str(origin.get("repository_path") or "").strip()
    if raw:
        declared = Path(raw).expanduser()
        candidates.append(declared)
        parts = declared.parts
        if ".mac" in parts:
            idx = parts.index(".mac")
            suffix = Path(*parts[idx + 1 :]) if idx + 1 < len(parts) else Path()
            candidates.append(Path.home() / ".mac" / suffix)

    repository_name = str(origin.get("repository_name") or "").strip()
    if repository_name:
        candidates.append(Path.home() / ".mac" / "src" / _safe_path_component(repository_name))

    source = str(origin.get("source") or "").strip()
    contract = origin.get("repository_contract")
    project = str(contract.get("project") or "").strip() if isinstance(contract, dict) else ""
    if repository_name == "mac" or source == "repo-beads-mac":
        candidates.insert(0, self_update_repo.expanduser())
    elif project == "repo-beads-mac":
        candidates.append(self_update_repo.expanduser())

    seen = set()
    unique: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _task_worktree_branch(agent_id: str, task_id: str, lease_id: str) -> str:
    agent = _safe_path_component(agent_id).strip("._-/") or "agent"
    task = _safe_path_component(task_id).strip("._-/") or "task"
    lease = _safe_path_component(lease_id).strip("._-/") or "lease"
    branch = "mac/%s/%s-%s" % (agent[:32], task[:48], lease[:24])
    return branch[:127].rstrip("./-") or "mac/agent/task"


def _load_repository_context(task_dir: Path) -> JsonDict:
    path = task_dir / "repository-worktree.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _task_payload_from_workspace(task_dir: Path) -> JsonDict:
    path = task_dir / "task.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    task = loaded.get("task")
    return task if isinstance(task, dict) else loaded


def _task_detail_evidence(task_detail: JsonDict, evidence_id: str) -> JsonDict:
    evidence_items = task_detail.get("evidence")
    if not isinstance(evidence_items, list):
        return {}
    for item in evidence_items:
        if isinstance(item, dict) and str(item.get("id") or "") == evidence_id:
            return item
    return {}


def _repository_context_env(context: JsonDict) -> Dict[str, str]:
    mapping = {
        "MAC_TASK_REPO_WORKTREE": context.get("repository_worktree"),
        "MAC_TASK_REPO_SOURCE": context.get("repository_source_path"),
        "MAC_TASK_REPO_BRANCH": context.get("repository_branch"),
        "MAC_TASK_REPO_BASE_SHA": context.get("repository_base_sha"),
        "MAC_TASK_REPO_REMOTE": context.get("repository_origin_remote"),
    }
    return {key: str(value) for key, value in mapping.items() if value not in {None, ""}}


def _enrich_verification_manifest_from_repository_context(
    manifest: JsonDict,
    context: JsonDict,
) -> JsonDict:
    if not manifest or not context:
        return manifest
    enriched = dict(manifest)
    repo_value = manifest.get("repo")
    repo = dict(repo_value) if isinstance(repo_value, dict) else {}
    if context.get("checkout_policy") == "review_git_worktree" and repo:
        reviewed_ref = str(context.get("repository_reviewed_remote_ref") or "").strip()
        branch = str(context.get("repository_branch") or "").strip()
        remote_ref = reviewed_ref or branch
        if remote_ref and not remote_ref.startswith("refs/"):
            remote_ref = "refs/heads/%s" % remote_ref
        defaults = {
            "path": context.get("repository_worktree"),
            "remote_url": context.get("repository_origin_remote"),
            "branch": reviewed_ref or branch,
            "base_sha": context.get("repository_base_sha"),
            "remote_ref": remote_ref,
            "head_sha": context.get("repository_reviewed_head_sha")
            or context.get("repository_base_sha"),
        }
        for key, value in defaults.items():
            if value not in {None, ""} and repo.get(key) in {None, ""}:
                repo[key] = value
        worktree_raw = str(context.get("repository_worktree") or "").strip()
        worktree = Path(worktree_raw).expanduser() if worktree_raw else None
        if "pushed" not in repo and worktree is not None and worktree.exists():
            repo["pushed"] = _repository_context_head_is_pushed(worktree, repo)
        enriched["repo"] = repo
        return enriched
    worktree_raw = str(context.get("repository_worktree") or "").strip()
    worktree = Path(worktree_raw).expanduser() if worktree_raw else None
    if worktree is not None and worktree.exists():
        head = _run_git(worktree, ["rev-parse", "HEAD"])
        if head.returncode == 0 and head.stdout.strip():
            repo["head_sha"] = head.stdout.strip()
        status = _run_git(worktree, ["status", "--porcelain"])
        if status.returncode == 0:
            repo["dirty"] = bool(status.stdout.strip())
        repo["files_changed"] = _repository_context_changed_files(worktree, context)

    defaults = {
        "path": context.get("repository_worktree"),
        "remote_url": context.get("repository_origin_remote"),
        "branch": context.get("repository_branch"),
        "base_sha": context.get("repository_base_sha"),
    }
    if context.get("repository_branch"):
        defaults["remote_ref"] = "refs/heads/%s" % context.get("repository_branch")
    for key, value in defaults.items():
        if value not in {None, ""}:
            repo[key] = value
    if worktree is not None and worktree.exists():
        repo["pushed"] = _repository_context_head_is_pushed(worktree, repo)
    enriched["repo"] = repo
    return enriched


def _repository_context_changed_files(worktree: Path, context: JsonDict) -> List[str]:
    base_sha = str(context.get("repository_base_sha") or "").strip()
    candidates: List[str] = []
    if base_sha:
        diff = _run_git(worktree, ["diff", "--name-only", "%s...HEAD" % base_sha])
        if diff.returncode != 0:
            diff = _run_git(worktree, ["diff", "--name-only", base_sha, "HEAD"])
        if diff.returncode == 0:
            candidates.extend(line.strip() for line in diff.stdout.splitlines())
    for args in (["diff", "--name-only"], ["diff", "--cached", "--name-only"]):
        diff = _run_git(worktree, args)
        if diff.returncode == 0:
            candidates.extend(line.strip() for line in diff.stdout.splitlines())
    return sorted({item for item in candidates if item})


def _repository_context_head_is_pushed(worktree: Path, repo: JsonDict) -> bool:
    head_sha = str(repo.get("head_sha") or "").strip()
    remote_url = str(repo.get("remote_url") or "").strip()
    remote_ref = str(repo.get("remote_ref") or "").strip()
    if not head_sha or not remote_ref:
        return False
    if remote_url:
        remote = _run_git(worktree, ["ls-remote", remote_url, remote_ref])
        if remote.returncode == 0:
            for line in remote.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == head_sha and parts[1] == remote_ref:
                    return True
    branch = _remote_branch_from_ref(remote_ref)
    if branch:
        remote_head = _run_git(worktree, ["rev-parse", "--verify", "origin/%s" % branch])
        if remote_head.returncode == 0 and remote_head.stdout.strip() == head_sha:
            return True
    return False


def _repository_context_audit_metadata(context: JsonDict) -> JsonDict:
    if not context:
        return {}
    return {
        "repository_checkout_policy": context.get("checkout_policy"),
        "repository_worktree": context.get("repository_worktree"),
        "repository_source_path": context.get("repository_source_path"),
        "repository_branch": context.get("repository_branch"),
        "repository_base_sha": context.get("repository_base_sha"),
    }


def _safe_git_ref(value: str) -> bool:
    return bool(value and not value.startswith("-") and re.match(SAFE_GIT_REF_RE, value))


def _manifest_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_repo_relative_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def _metadata_path_list(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return []
    paths: List[str] = []
    seen = set()
    for item in values:
        path = _normalize_repo_relative_path(item)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _nested_dict(root: JsonDict, *keys: str) -> JsonDict:
    node: Any = root
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return dict(node) if isinstance(node, dict) else {}


def _required_changed_files_from_task(task: JsonDict) -> List[str]:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return []
    containers = [
        metadata,
        _nested_dict(metadata, "acceptance"),
        _nested_dict(metadata, "execution_contract"),
        _nested_dict(metadata, "execution_contract", "evidence"),
        _nested_dict(metadata, "execution_contract", "repository_contract"),
        _nested_dict(metadata, "execution_contract", "repository_contract", "evidence"),
        _nested_dict(metadata, "origin", "repository_contract"),
        _nested_dict(metadata, "origin", "repository_contract", "evidence"),
        _nested_dict(metadata, "repository_contract"),
        _nested_dict(metadata, "repository_contract", "evidence"),
    ]
    required: List[str] = []
    seen = set()
    for container in containers:
        if not container:
            continue
        for key in REQUIRED_CHANGED_FILE_KEYS:
            for path in _metadata_path_list(container.get(key)):
                if path not in seen:
                    seen.add(path)
                    required.append(path)
    return required


def _repo_path_satisfies_requirement(changed_path: str, required_path: str) -> bool:
    changed = _normalize_repo_relative_path(changed_path)
    required = _normalize_repo_relative_path(required_path)
    if not changed or not required:
        return False
    if any(char in required for char in "*?["):
        return fnmatch.fnmatchcase(changed, required)
    return changed == required


def _worker_required_changed_file_problems(task: JsonDict, manifest: JsonDict) -> List[str]:
    required = _required_changed_files_from_task(task)
    if not required:
        return []
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    changed = _metadata_path_list(repo.get("files_changed")) if isinstance(repo, dict) else []
    missing = [
        path
        for path in required
        if not any(_repo_path_satisfies_requirement(item, path) for item in changed)
    ]
    if not missing:
        return []
    return ["repo evidence missing required changed files: %s" % ", ".join(missing)]


def _remote_branch_from_ref(remote_ref: str) -> str:
    ref = str(remote_ref or "").strip()
    if not ref:
        return ""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if ref.startswith("origin/"):
        ref = ref[len("origin/"):]
    if _safe_git_ref(ref) and not ref.startswith("refs/"):
        return ref
    return ""


def _worker_verification_contract_problems(
    manifest: JsonDict,
    evidence_type: str,
    *,
    allow_empty_repo_change: bool = False,
) -> List[str]:
    if evidence_type == "repo_change":
        return _worker_repo_verification_problems(
            manifest,
            require_tests=True,
            allow_empty_repo_change=allow_empty_repo_change,
        )
    if evidence_type == "documentation":
        return _worker_repo_verification_problems(manifest, require_tests=False)
    if evidence_type == "deployment":
        problems = _worker_require_pushed_repo_anchor(manifest)
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("deployment evidence requires at least one passing check")
        if not (
            _manifest_list(manifest.get("targets"))
            or _manifest_list(manifest.get("services"))
            or _manifest_list(manifest.get("artifacts"))
        ):
            problems.append("deployment evidence requires targets, services, or artifacts")
        return problems
    if evidence_type in {"test", "artifact"}:
        problems = _worker_require_pushed_repo_anchor(manifest)
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("%s evidence requires at least one passing check or test" % evidence_type)
        if evidence_type == "artifact" and not _manifest_list(manifest.get("artifacts")):
            problems.append("artifact evidence requires artifacts")
        return problems
    if evidence_type == "no_change":
        problems = _worker_require_pushed_repo_anchor(manifest)
        if not str(manifest.get("reason") or manifest.get("no_change_reason") or "").strip():
            problems.append("no_change evidence requires a reason")
        if _worker_passed_verification_check_count(manifest) < 1:
            problems.append("no_change evidence requires at least one passing check")
        return problems
    if evidence_type == "review_verdict":
        return []
    if evidence_type == "operator_result":
        # autonomy-loop fix: mirror the server's substance gate so the worker
        # pre-check fails chatter ("hello hello hello") / placeholder evidence
        # locally and fails the task cleanly, instead of submitting it and
        # crashing on the server's 400. One definition of "substantive" —
        # reused from evidence_validators so worker and server can't drift.
        from mac.evidence_validators import _operator_result_is_substantive

        if _manifest_list(manifest.get("artifacts")) or _manifest_list(manifest.get("findings")):
            return []
        combined = (
            str(manifest.get("summary") or "") + " " + str(manifest.get("result") or "")
        ).strip()
        if not combined:
            return ["operator_result evidence requires summary, result, findings, or artifacts"]
        if not _operator_result_is_substantive(combined):
            return [
                "operator_result evidence is not substantive (degenerate or placeholder "
                "text); provide a real summary/result describing the completed work, or "
                "structured findings/artifacts"
            ]
        return []
    return ["unsupported verification.evidence_type: %s" % evidence_type]


def _worker_repo_verification_problems(
    manifest: JsonDict,
    require_tests: bool,
    *,
    allow_empty_repo_change: bool = False,
) -> List[str]:
    problems = _worker_require_pushed_repo_anchor(manifest)
    repo = manifest.get("repo") if isinstance(manifest.get("repo"), dict) else {}
    files_changed = _manifest_list(repo.get("files_changed")) if isinstance(repo, dict) else []
    if not files_changed and not allow_empty_repo_change:
        problems.append("repo evidence requires changed files")
    if require_tests and _worker_passed_verification_check_count(manifest) < 1:
        problems.append("repo code evidence requires at least one passing test/check")
    return problems


def _worker_allows_empty_repo_change_evidence(task: JsonDict, evidence_type: str) -> bool:
    if str(evidence_type or "").strip().lower() != "repo_change":
        return False
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        return False
    origin = metadata.get("origin")
    remediation = metadata.get("remediation")
    origin_type = origin.get("type") if isinstance(origin, dict) else None
    remediation_type = remediation.get("type") if isinstance(remediation, dict) else None
    return origin_type == "beads_source_remediation" or remediation_type == "beads_source_refresh"


def _worker_require_pushed_repo_anchor(manifest: JsonDict) -> List[str]:
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return ["repo evidence requires verification.repo object"]
    problems: List[str] = []
    head_sha = str(repo.get("head_sha") or "").strip()
    if not GIT_SHA_RE.match(head_sha):
        problems.append("repo.head_sha must be a git SHA")
    dirty = repo.get("dirty")
    if dirty not in {False, "false", "False", 0, "0"}:
        problems.append("repo evidence must declare dirty=false")
    pushed = repo.get("pushed") is True or str(repo.get("pushed") or "").lower() == "true"
    remote_ref = str(repo.get("remote_ref") or "").strip()
    pr_url = str(repo.get("pr_url") or "").strip()
    if not (pushed and remote_ref) and not pr_url:
        problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
    return problems


def _worker_passed_verification_check_count(manifest: JsonDict) -> int:
    count = 0
    for item in _manifest_list(manifest.get("tests")):
        if _worker_verification_item_passed(item):
            count += 1
    for item in _manifest_list(manifest.get("checks")):
        if _worker_verification_item_passed(item):
            count += 1
    return count


PASSING_VERIFICATION_WORDS = {"pass", "passed", "success", "successful", "succeeded", "ok"}


def _worker_int_value(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _worker_verification_item_passed(item: Any) -> bool:
    if isinstance(item, list):
        return any(_worker_verification_item_passed(nested) for nested in item)
    if not isinstance(item, dict):
        return False
    if "returncode" in item:
        return _worker_int_value(item["returncode"]) == 0
    failed = _worker_int_value(item.get("failed"))
    if failed is not None and failed > 0:
        return False
    for key in ("status", "result", "outcome"):
        if str(item.get(key) or "").strip().lower() in PASSING_VERIFICATION_WORDS:
            return True
    for key in ("passed", "success", "succeeded", "ok", "satisfied"):
        value = item.get(key)
        if value is True:
            return True
        number = _worker_int_value(value)
        if number is not None and number > 0 and failed == 0:
            return True
    bool_values = [value for value in item.values() if isinstance(value, bool)]
    if bool_values and len(bool_values) == len(item) and all(bool_values):
        return True
    return any(
        _worker_verification_item_passed(nested)
        for nested in item.values()
        if isinstance(nested, (dict, list))
    )


def _run_git(repo: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    try:
        timeout = float(os.environ.get("MAC_SELF_UPDATE_GIT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_git_in(cwd: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with ``cwd`` as the working directory.

    Used for clone where the target directory does not yet exist (so
    ``git -C <target>`` is invalid). Mirrors ``_run_git`` for timeout
    + capture behaviour so the K8s clone path is testable via the
    same monkeypatch surface."""
    try:
        timeout = float(os.environ.get("MAC_SELF_UPDATE_GIT_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _inject_git_remote_auth(url: str) -> str:
    from mac.gitops import inject_git_remote_auth as _impl
    return _impl(url)


def _stable_id(prefix: str, value: str) -> str:
    return "%s_%s" % (prefix, _safe_path_component(value.lower()).strip("_") or "default")


def _register_runtime_identity_for_worker(
    client: MacApiClient,
    agent_name: str,
    hermes_instance_id: Optional[str],
) -> None:
    if not hermes_instance_id:
        return
    tenant_id = (os.environ.get("MAC_FLEET_TENANT_ID") or "").strip()
    if not tenant_id:
        return
    persona_id = (
        os.environ.get("MAC_HERMES_PERSONA_ID")
        or os.environ.get("MAC_WORKER_PERSONA_ID")
        or _stable_id("persona", agent_name)
    )
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    fleet_name = (
        os.environ.get("MAC_FLEET_NAME")
        or os.environ.get("FLEET_NAME")
        or tenant_id.removeprefix("tenant_")
        or "mac"
    )
    agent_id = os.environ.get("MAC_AGENT_ID") or _stable_id("agent", agent_name)
    client.post(
        "/tenants",
        {
            "name": fleet_name,
            "tenant_id": tenant_id,
            "metadata": {"source": "mac-agent", "fleet": fleet_name},
        },
    )
    client.post(
        "/personas",
        {
            "tenant_id": tenant_id,
            "name": agent_name,
            "soul_ref": str(hermes_home / "SOUL.md"),
            "memory_scope": str(hermes_home),
            "persona_id": persona_id,
            "metadata": {"source": "mac-agent", "agent_id": agent_id},
        },
    )
    client.post(
        "/hermes-instances",
        {
            "tenant_id": tenant_id,
            "name": agent_name,
            "persona_id": persona_id,
            "home_ref": str(hermes_home),
            "instance_id": hermes_instance_id,
            "metadata": {"source": "mac-agent", "agent_id": agent_id, "fleet": fleet_name},
        },
    )


def _fleet_name_from_env(tenant_id: str) -> str:
    explicit = (os.environ.get("MAC_FLEET_NAME") or os.environ.get("FLEET_NAME") or "").strip()
    if explicit:
        return explicit
    if tenant_id.startswith("tenant_"):
        return tenant_id.removeprefix("tenant_") or "mac"
    return "mac"


def _fleet_auto_registration_enabled() -> bool:
    raw = os.environ.get("MAC_AUTO_REGISTER_FLEET")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_fleet_or_none(client: MacApiClient, fleet_name: str) -> Optional[JsonDict]:
    try:
        result = client.get("/fleets/%s" % quote(fleet_name, safe=""))
    except MacApiError as exc:
        if "not found" in str(exc).lower() or "404" in str(exc):
            return None
        raise
    return result if isinstance(result, dict) else None


def _ensure_worker_fleet_membership(
    client: MacApiClient,
    *,
    agent_name: str,
    agent_id: str,
) -> None:
    """Ensure deployed agents are visible through the first-class fleet API."""
    if not _fleet_auto_registration_enabled():
        return
    tenant_id = (os.environ.get("MAC_FLEET_TENANT_ID") or "").strip()
    if not tenant_id:
        return
    fleet_name = _fleet_name_from_env(tenant_id)
    shared_services_manager = (
        os.environ.get("MAC_SHARED_SERVICES_MANAGER_AGENT")
        or os.environ.get("MAC_BEADS_BRIDGE_HUB_AGENT")
        or ""
    ).strip()
    metadata = {
        "source": "mac-agent",
        "fleet": fleet_name,
        "hub_agent": shared_services_manager or agent_name,
    }
    client.post(
        "/tenants",
        {
            "name": fleet_name,
            "tenant_id": tenant_id,
            "metadata": metadata,
        },
    )
    existing = _get_fleet_or_none(client, fleet_name)
    if existing is None:
        try:
            client.post(
                "/fleets",
                {
                    "name": fleet_name,
                    "description": "Auto-registered deployment fleet",
                    "status": "active",
                    "metadata": metadata,
                    "tenant_id": tenant_id,
                    "agent_ids": [agent_id],
                    "fleet_id": _stable_id("fleet", fleet_name),
                    "actor": "mac-agent",
                },
            )
            return
        except MacApiError as exc:
            if "already exists" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
            existing = _get_fleet_or_none(client, fleet_name)
    if not existing:
        return
    current_members = [str(item) for item in existing.get("agent_ids") or [] if str(item).strip()]
    next_members = sorted(set(current_members + [agent_id]))
    existing_metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    next_metadata = {**existing_metadata, **metadata}
    client.request(
        "PUT",
        "/fleets/%s" % quote(str(existing.get("id") or fleet_name), safe=""),
        {
            "status": "active",
            "metadata": next_metadata,
            "tenant_id": tenant_id,
            "agent_ids": next_members,
            "actor": "mac-agent",
        },
    )


def _csv_arg(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_arg(value: Optional[str]) -> JsonDict:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise MacApiError("resources must be a JSON object")
    return loaded


def _read_env_value(path: Path, key: str) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key and value.strip():
                return value.strip()
    except FileNotFoundError:
        return None
    return None


def _write_env_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    replaced = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: List[str] = []
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            name, _old = line.split("=", 1)
            if name.strip() == key:
                updated.append("%s=%s" % (key, value))
                replaced = True
                continue
        updated.append(line)
    if not replaced:
        updated.append("%s=%s" % (key, value))
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _attestation_key_matches_hub(
    client: MacApiClient,
    agent_id: str,
    attestation_key: str,
) -> bool:
    from mac.services import sign_verification_manifest

    challenge = {
        "schema": "mac.agent_attestation_challenge.v1",
        "purpose": "attestation-key-healthcheck",
        "agent_id": agent_id,
        "nonce": secrets.token_urlsafe(32),
    }
    response = client.post(
        "/agents/%s/attestation-key/verify" % quote(agent_id, safe=""),
        {
            "challenge": challenge,
            "signature": sign_verification_manifest(attestation_key, challenge),
        },
    )
    return bool(isinstance(response, dict) and response.get("valid") is True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mac worker harness")
    parser.add_argument(
        "--url",
        default=os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL") or "http://127.0.0.1:8000",
    )
    # Token resolution honors --fleet (or MAC_FLEET) so machines in
    # multiple fleets don't collide on a single MAC_API_TOKEN. See
    # mac.fleet_env (mac-g55y).
    from mac.fleet_env import resolve_first as _resolve_token

    parser.add_argument(
        "--fleet",
        default=os.environ.get("MAC_FLEET"),
        help="fleet name used to scope env var lookup (MAC_API_TOKEN__<FLEET>)",
    )
    parser.add_argument(
        "--token",
        default=_resolve_token(["MAC_TOKEN", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"]),
    )
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID"))
    parser.add_argument(
        "--hermes-instance-id",
        default=os.environ.get("MAC_WORKER_HERMES_INSTANCE_ID")
        or os.environ.get("MAC_HERMES_INSTANCE_ID"),
        help="MAC Hermes instance id to bind this worker agent to",
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="register or refresh this host's machine and agent rows before running",
    )
    parser.add_argument("--machine-id", default=os.environ.get("MAC_MACHINE_ID"))
    parser.add_argument("--hostname", default=os.environ.get("MAC_HOSTNAME"))
    parser.add_argument("--agent-name", default=os.environ.get("MAC_AGENT_NAME"))
    parser.add_argument(
        "--capabilities",
        default=os.environ.get("MAC_WORKER_CAPABILITIES", ""),
        help="comma-separated capabilities to advertise when --register is used",
    )
    parser.add_argument(
        "--resources",
        default=os.environ.get("MAC_WORKER_RESOURCES"),
        help="JSON resource/capacity object to advertise when --register is used",
    )
    parser.add_argument("--workspace", default=".mac-agent-workspaces")
    parser.add_argument("--lease-seconds", type=int, default=900)
    # mac-ehch: hard-cap executor runtime so a wedged subprocess can't
    # keep renewing its lease forever. One hour is well above the median
    # claim duration in production and below the point where a stuck
    # task should be visible to operators. Override with --timeout if a
    # longer-running task is genuinely needed.
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument(
        "--allowed-projects",
        default=os.environ.get("MAC_WORKER_ALLOWED_PROJECTS", ""),
        help="comma-separated projects this worker may claim",
    )
    parser.add_argument(
        "--required-metadata",
        default=os.environ.get("MAC_WORKER_REQUIRED_METADATA"),
        help="JSON object of top-level task metadata key/value pairs required before claiming",
    )
    parser.add_argument(
        "--require-canary",
        action="store_true",
        default=_env_bool("MAC_WORKER_REQUIRE_CANARY", False),
        help="claim only tasks with metadata.canary, metadata.mac_canary, or metadata.worker_canary true",
    )
    parser.add_argument(
        "--running-digest",
        help="runtime_environments.digest the worker is running (declared at first heartbeat)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="run forever (poll for tasks). Default is run_once and exit.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        help="cap iterations in --loop mode (mostly for tests)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds to sleep between polls when no task is available",
    )
    parser.add_argument(
        "--self-update-repo",
        default=os.environ.get("MAC_SELF_UPDATE_REPO"),
        help="git worktree this worker may pull for AgentBus repo-update control messages",
    )
    parser.add_argument(
        "--disable-agentbus-control",
        action="store_true",
        help="disable AgentBus control-message polling before task claims",
    )
    parser.add_argument(
        "--attestation-key-env",
        default=os.environ.get("MAC_ATTESTATION_KEY_ENV"),
        help="env file where a first-registration attestation key should be persisted",
    )
    parser.add_argument(
        "--rotate-missing-attestation-key",
        action="store_true",
        default=_env_bool("MAC_ROTATE_MISSING_ATTESTATION_KEY", False),
        help="rotate and persist this agent's attestation key when no local key is configured",
    )
    parser.add_argument(
        "--rotate-invalid-attestation-key",
        action="store_true",
        default=_env_bool("MAC_ROTATE_INVALID_ATTESTATION_KEY", False),
        help="rotate and persist this agent's attestation key when the local key no longer matches the hub",
    )
    parser.add_argument(
        "--heartbeat-only",
        action="store_true",
        help="register/heartbeat once and exit without claiming tasks",
    )
    parser.add_argument(
        "--dry-run-claim",
        action="store_true",
        help="register/heartbeat and ask the hub what this worker would claim without creating a lease",
    )
    parser.add_argument(
        "--executor",
        nargs=argparse.REMAINDER,
        default=None,
        help="executor argv; pass this flag last, followed by the command and arguments",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Re-resolve token using --fleet if it wasn't already supplied; this
    # lets `--fleet rocky` pick MAC_API_TOKEN__ROCKY without requiring
    # MAC_FLEET to be exported separately (mac-g55y).
    if args.token is None and args.fleet:
        from mac.fleet_env import resolve_first as _rt

        args.token = _rt(["MAC_TOKEN", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"], fleet=args.fleet)
    client = MacApiClient(args.url, token=args.token)
    agent_id = args.agent_id
    try:
        registered: Optional[JsonDict] = None
        attestation_key = os.environ.get("MAC_ATTESTATION_KEY")
        attestation_env_path = Path(args.attestation_key_env).expanduser() if args.attestation_key_env else None
        if not attestation_key and attestation_env_path is not None:
            attestation_key = _read_env_value(attestation_env_path, "MAC_ATTESTATION_KEY")
        if args.register:
            registered = register_worker(
                client,
                hostname=args.hostname,
                agent_name=args.agent_name,
                capabilities=_csv_arg(args.capabilities),
                resources=_json_arg(args.resources),
                machine_id=args.machine_id,
                agent_id=args.agent_id,
                hermes_instance_id=args.hermes_instance_id,
            )
            agent_id = registered["id"]
            if registered.get("attestation_key"):
                attestation_key = str(registered["attestation_key"])
                os.environ["MAC_ATTESTATION_KEY"] = attestation_key
                if attestation_env_path is not None:
                    _write_env_value(attestation_env_path, "MAC_ATTESTATION_KEY", attestation_key)
        if not agent_id:
            raise MacApiError("--agent-id or --register is required")
        if not attestation_key and args.rotate_missing_attestation_key:
            rotated = client.post(
                "/agents/%s/attestation-key/rotate" % quote(agent_id, safe=""),
                {},
            )
            attestation_key = str(rotated["attestation_key"])
            os.environ["MAC_ATTESTATION_KEY"] = attestation_key
            if attestation_env_path is not None:
                _write_env_value(attestation_env_path, "MAC_ATTESTATION_KEY", attestation_key)
        if attestation_key and args.rotate_invalid_attestation_key:
            if not _attestation_key_matches_hub(client, agent_id, attestation_key):
                rotated = client.post(
                    "/agents/%s/attestation-key/rotate" % quote(agent_id, safe=""),
                    {},
                )
                attestation_key = str(rotated["attestation_key"])
                os.environ["MAC_ATTESTATION_KEY"] = attestation_key
                if attestation_env_path is not None:
                    _write_env_value(attestation_env_path, "MAC_ATTESTATION_KEY", attestation_key)
        if args.heartbeat_only:
            heartbeat = client.post(
                "/agents/%s/heartbeat" % quote(agent_id, safe=""),
                {"status": "idle", "running_digest": args.running_digest},
            )
            print(
                json.dumps(
                    {"status": "heartbeat", "agent": heartbeat, "registered": registered},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        required_metadata = _json_arg(args.required_metadata)
        allowed_projects = _csv_arg(args.allowed_projects)
        executor_argv = list(args.executor or [])
        if executor_argv and executor_argv[0] == "--":
            executor_argv = executor_argv[1:]
        if args.dry_run_claim:
            worker = MacWorker(
                client,
                agent_id,
                Path(args.workspace),
                SubprocessExecutor(["true"]),
                lease_seconds=args.lease_seconds,
                running_digest=args.running_digest,
                poll_interval_seconds=args.poll_interval,
                allowed_projects=allowed_projects,
                required_metadata=required_metadata,
                require_canary=args.require_canary,
                agentbus_control_enabled=not args.disable_agentbus_control,
                self_update_repo=Path(args.self_update_repo).expanduser()
                if args.self_update_repo
                else None,
                attestation_key=attestation_key,
            )
            print(json.dumps({"status": "dry_run", "assignment": worker.dry_run_claim()}, indent=2, sort_keys=True))
            return 0
        if not executor_argv:
            raise MacApiError("--executor is required unless --heartbeat-only is set")
        worker = MacWorker(
            client,
            agent_id,
            Path(args.workspace),
            SubprocessExecutor(executor_argv, timeout=args.timeout),
            lease_seconds=args.lease_seconds,
            running_digest=args.running_digest,
            poll_interval_seconds=args.poll_interval,
            allowed_projects=allowed_projects,
            required_metadata=required_metadata,
            require_canary=args.require_canary,
            agentbus_control_enabled=not args.disable_agentbus_control,
            self_update_repo=Path(args.self_update_repo).expanduser()
            if args.self_update_repo
            else None,
            attestation_key=attestation_key,
        )
        if args.loop:
            results = worker.run_forever(max_iterations=args.max_iterations)
            print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
            if any(result.status == "self_update_restart" for result in results):
                return 75
        else:
            result = worker.run_once()
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            if result.status == "self_update_restart":
                return 75
    except MacApiError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
