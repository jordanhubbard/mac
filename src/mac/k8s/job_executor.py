from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from mac.models import metadata_declares_read_only_report_repository

def _q(value: str) -> str:
    return quote(value, safe="")

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

DEFAULT_EXECUTOR_TIMEOUT_SECONDS = 1500  # 25 min < default activeDeadline 30 min

DEFAULT_EVIDENCE_MANIFEST_PATH = "/tmp/mac-evidence.json"

READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON = (
    "read_only_report_requires_openshell_isolation"
)

@dataclass
class JobExecutionResult:
    status: str
    task_id: Optional[str]
    lease_id: Optional[str]
    returncode: Optional[int]
    evidence_id: Optional[str] = None
    error: Optional[str] = None
    stdout_sha256: Optional[str] = None
    duration_ms: Optional[float] = None

    def exit_code(self) -> int:
        if self.status in ("submitted-for-review", "blocked", "failed"):
            return 0
        return 2

def _resolve_mac_and_executor(
    env: Dict[str, str],
    mac: Optional[Any],
    executor: Optional[Callable[[JsonDict], "_ExecResult"]],
) -> "tuple[Any, Callable[[JsonDict], _ExecResult]]":
    mac_url = env.get("MAC_URL") or env.get("MAC_HUB_URL", "")
    token = env.get("MAC_WORKER_TOKEN") or env.get("MAC_API_TOKEN", "")
    if mac is None:
        mac = _default_mac_client(mac_url, token)
    if executor is None:
        executor = _default_subprocess_executor(env)
    return mac, executor


def _fetch_task_or_fail(
    mac: Any,
    task_id: str,
    *,
    lease_id: Optional[str],
) -> "tuple[Optional[JsonDict], Optional[JobExecutionResult]]":
    try:
        return mac.get("/tasks/%s" % _q(task_id)), None
    except Exception as exc:  # noqa: BLE001
        return None, JobExecutionResult(
            status="no-evidence",
            task_id=task_id,
            lease_id=lease_id,
            returncode=None,
            error="GET /tasks/{id} failed: %s" % exc,
        )


def _execute_timed(
    executor: Callable[[JsonDict], "_ExecResult"], task: JsonDict
) -> "tuple[_ExecResult, float]":
    started = time.monotonic()
    exec_result = executor(task)
    duration_ms = (time.monotonic() - started) * 1000.0
    return exec_result, duration_ms


def run_one_lease(
    *,
    mac: Optional[Any] = None,
    executor: Optional[Callable[[JsonDict], "_ExecResult"]] = None,
    env: Optional[Dict[str, str]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> JobExecutionResult:
    env = env if env is not None else os.environ
    review_id = env.get("MAC_REVIEW_ID", "").strip()
    if review_id:
        return _run_one_review(mac=mac, executor=executor, env=env, sleeper=sleeper)
    task_id = env.get("MAC_TASK_ID", "").strip()
    lease_id = env.get("MAC_LEASE_ID", "").strip()
    agent_id = env.get("MAC_AGENT_ID", "").strip() or "mac-task-runner"

    if not task_id or not lease_id:
        return JobExecutionResult(
            status="missing-env",
            task_id=task_id or None,
            lease_id=lease_id or None,
            returncode=None,
            error="MAC_TASK_ID and MAC_LEASE_ID are required in the Job env",
        )

    mac, executor = _resolve_mac_and_executor(env, mac, executor)

    task, early = _fetch_task_or_fail(mac, task_id, lease_id=lease_id)
    if early is not None:
        return early

    try:
        mac.post(
            "/tasks/%s/start?agent_id=%s&lease_id=%s"
            % (_q(task_id), _q(agent_id), _q(lease_id)),
            {},
        )
    except Exception as exc:  # noqa: BLE001
        # Already running is fine; anything else is a real failure.
        if "already" not in str(exc).lower():
            return JobExecutionResult(
                status="no-evidence",
                task_id=task_id,
                lease_id=lease_id,
                returncode=None,
                error="POST /tasks/{id}/start failed: %s" % exc,
            )

    try:
        exec_result, duration_ms = _execute_timed(executor, task)
    except Exception as exc:  # noqa: BLE001
        return _record_failure_evidence(
            mac, task_id, lease_id, agent_id, returncode=-1, error=str(exc)
        )

    evidence = _submit_execution_evidence(
        mac,
        task_id=task_id,
        lease_id=lease_id,
        agent_id=agent_id,
        exec_result=exec_result,
        duration_ms=duration_ms,
    )
    if evidence is None:
        return JobExecutionResult(
            status="no-evidence",
            task_id=task_id,
            lease_id=lease_id,
            returncode=exec_result.returncode,
            duration_ms=duration_ms,
            stdout_sha256=exec_result.stdout_sha256,
            error="evidence POST failed; the lease will expire and another runner will retry",
        )

    if exec_result.returncode == 0:
        try:
            mac.post(
                "/tasks/%s/submit-for-review?agent_id=%s&lease_id=%s"
                % (_q(task_id), _q(agent_id), _q(lease_id)),
                {},
            )
            return JobExecutionResult(
                status="submitted-for-review",
                task_id=task_id,
                lease_id=lease_id,
                returncode=0,
                evidence_id=evidence.get("id"),
                duration_ms=duration_ms,
                stdout_sha256=exec_result.stdout_sha256,
            )
        except Exception as exc:  # noqa: BLE001
            return _block_task_after_evidence(
                mac,
                task_id,
                lease_id,
                agent_id,
                reason="review_submission_failed",
                evidence_id=evidence.get("id"),
                returncode=0,
                error="submit-for-review failed: %s" % exc,
                duration_ms=duration_ms,
                stdout_sha256=exec_result.stdout_sha256,
            )

    return _block_task_after_evidence(
        mac,
        task_id,
        lease_id,
        agent_id,
        reason="executor_nonzero_exit",
        evidence_id=evidence.get("id"),
        returncode=exec_result.returncode,
        duration_ms=duration_ms,
        stdout_sha256=exec_result.stdout_sha256,
    )

@dataclass
class _ExecResult:
    returncode: int
    stdout: str
    stderr: str = ""
    stdout_sha256: Optional[str] = None
    verification_manifest: Optional[Dict[str, Any]] = None
    manifest_path: Optional[str] = None
    manifest_error: Optional[str] = None

def _run_one_review(
    *,
    mac: Optional[Any],
    executor: Optional[Callable[[JsonDict], "_ExecResult"]],
    env: Dict[str, str],
    sleeper: Optional[Callable[[float], None]],
) -> JobExecutionResult:
    """Review-mode counterpart to the task flow.

    The reviewer Job runs the review wrapper which produces a
    ``mac.worker_evidence.v1`` manifest. We POST it as ``kind="review"``
    and tick the default review workflow so the verdict applies.
    Mirrors host worker.py:1862 ``_record_review_execution`` +
    worker.py:961 ``_advance_review_workflow_after_verdict``.
    """
    task_id = env.get("MAC_TASK_ID", "").strip()
    review_id = env.get("MAC_REVIEW_ID", "").strip()
    target_evidence_id = env.get("MAC_REVIEW_TARGET_EVIDENCE_ID", "").strip()
    agent_id = env.get("MAC_AGENT_ID", "").strip() or "mac-task-runner"
    if not task_id or not review_id:
        return JobExecutionResult(
            status="missing-env",
            task_id=task_id or None,
            lease_id=None,
            returncode=None,
            error="MAC_TASK_ID and MAC_REVIEW_ID are required for review mode",
        )

    if mac is None:
        mac_url = env.get("MAC_URL") or env.get("MAC_HUB_URL", "")
        token = env.get("MAC_WORKER_TOKEN") or env.get("MAC_API_TOKEN", "")
        mac = _default_mac_client(mac_url, token)
    task, early = _fetch_task_or_fail(mac, task_id, lease_id=None)
    if early is not None:
        return early
    # A Job may survive a policy race or be forged independently of the K8s
    # runner.  Re-check the authoritative task immediately before preparing a
    # workspace or invoking the legacy review command.  Do not emit review
    # evidence or block the task: the pending review must remain available for
    # the hub to reroute to an isolated OpenShell reviewer.
    if metadata_declares_read_only_report_repository(task.get("metadata")):
        return JobExecutionResult(
            status="no-evidence",
            task_id=task_id,
            lease_id=None,
            returncode=None,
            error=READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON,
        )
    if executor is None:
        try:
            review_env = _prepare_canonical_review_environment(
                mac,
                env,
                task_id=task_id,
                review_id=review_id,
                target_evidence_id=target_evidence_id,
                reviewer_agent_id=agent_id,
                task_detail=task,
            )
        except Exception as exc:  # noqa: BLE001 - checkout/preparation boundary
            return JobExecutionResult(
                status="no-evidence",
                task_id=task_id,
                lease_id=None,
                returncode=None,
                error="canonical review workspace preparation failed: %s" % exc,
            )
        executor = _default_subprocess_executor(review_env)

    try:
        exec_result, duration_ms = _execute_timed(executor, task)
    except Exception as exc:  # noqa: BLE001
        return _block_task_after_evidence(
            mac,
            task_id,
            None,
            agent_id,
            reason="review_executor_exception",
            evidence_id=None,
            returncode=-1,
            error="review executor raised: %s" % exc,
            detail_extra={"review_id": review_id},
        )

    metadata: JsonDict = {
        "returncode": exec_result.returncode,
        "stdout_sha256": exec_result.stdout_sha256,
        "stdout_bytes": len(exec_result.stdout.encode("utf-8", "replace")),
        "duration_ms": duration_ms,
        "review_id": review_id,
        "executor_evidence_id": target_evidence_id,
        "k8s_review_job": True,
    }
    if exec_result.verification_manifest is not None:
        metadata["verification"] = exec_result.verification_manifest
    if exec_result.manifest_path:
        metadata["verification_manifest_path"] = exec_result.manifest_path
    if exec_result.manifest_error:
        metadata["verification_manifest_error"] = exec_result.manifest_error

    try:
        evidence = mac.post(
            "/tasks/%s/evidence" % _q(task_id),
            {
                "kind": "review",
                "uri": "stdout://mac-review-runner/%s" % review_id,
                "summary": "review executor returncode=%d duration_ms=%.1f"
                % (exec_result.returncode, duration_ms),
                "created_by": agent_id,
                "metadata": metadata,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error("review evidence POST failed for review=%s: %s", review_id, exc)
        return JobExecutionResult(
            status="no-evidence",
            task_id=task_id,
            lease_id=None,
            returncode=exec_result.returncode,
            duration_ms=duration_ms,
            stdout_sha256=exec_result.stdout_sha256,
            error="review evidence POST failed: %s" % exc,
        )

    if exec_result.returncode == 0:
        try:
            mac.post(
                "/reviews/default/tick?limit=10&actor=%s" % _q(agent_id),
                {},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "post-review tick failed for review=%s (evidence already recorded): %s",
                review_id, exc,
            )

    if exec_result.returncode != 0:
        return _block_task_after_evidence(
            mac,
            task_id,
            None,
            agent_id,
            reason="review_executor_failed",
            evidence_id=evidence.get("id") if isinstance(evidence, dict) else None,
            returncode=exec_result.returncode,
            duration_ms=duration_ms,
            stdout_sha256=exec_result.stdout_sha256,
            detail_extra={"review_id": review_id},
        )

    return JobExecutionResult(
        status="submitted-for-review",
        task_id=task_id,
        lease_id=None,
        returncode=exec_result.returncode,
        evidence_id=evidence.get("id") if isinstance(evidence, dict) else None,
        duration_ms=duration_ms,
        stdout_sha256=exec_result.stdout_sha256,
    )


def _prepare_canonical_review_environment(
    mac: Any,
    env: Dict[str, str],
    *,
    task_id: str,
    review_id: str,
    target_evidence_id: str,
    reviewer_agent_id: str,
    task_detail: JsonDict,
) -> Dict[str, str]:
    """Materialize the exact executor evidence and checkout for a review Job."""
    from mac.worker import MacWorker

    workspace_root = Path(
        env.get("MAC_REVIEW_WORKSPACE_ROOT") or "/tmp/mac-review-workspaces"
    )
    preparer = MacWorker(
        mac,
        reviewer_agent_id,
        workspace_root,
        lambda *_args: None,
        agentbus_control_enabled=False,
        attestation_key=env.get("MAC_AGENT_ATTESTATION_KEY")
        or env.get("MAC_ATTESTATION_KEY"),
    )
    task_dir = preparer._prepare_review_workspace(
        task_id,
        review_id,
        target_evidence_id,
        task_detail,
        {"id": "k8s-review-%s" % review_id},
        {
            "claim": {
                "review_id": review_id,
                "reviewer_agent_id": reviewer_agent_id,
                "executor_evidence_id": target_evidence_id,
            }
        },
    )
    task_payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    review_task = task_payload.get("task", task_payload)
    runtime = (
        (review_task.get("metadata") or {}).get("runtime")
        if isinstance(review_task, dict)
        else None
    )
    prepared = dict(env)
    prepared["MAC_TASK_WORKSPACE"] = str(task_dir)
    prepared["MAC_TASK_FILE"] = str(task_dir / "task.json")
    prepared["MAC_TASK_EVIDENCE_MANIFEST_PATH"] = str(
        task_dir / "mac-evidence.json"
    )
    prepared["MAC_WORKER_AGENT_ID"] = reviewer_agent_id
    if prepared.get("MAC_AGENT_ATTESTATION_KEY"):
        prepared["MAC_ATTESTATION_KEY"] = prepared["MAC_AGENT_ATTESTATION_KEY"]
    if isinstance(runtime, dict) and runtime.get("repository_worktree"):
        prepared["MAC_TASK_REPO_WORKTREE"] = str(runtime["repository_worktree"])
    return prepared


def _default_subprocess_executor(env: Dict[str, str]) -> Callable[[JsonDict], _ExecResult]:
    """Returns a callable that runs the configured task executor command."""
    cmd = (env.get("MAC_TASK_EXECUTOR_COMMAND") or "").strip()
    timeout = int(
        env.get("MAC_TASK_EXECUTOR_TIMEOUT_SECONDS")
        or DEFAULT_EXECUTOR_TIMEOUT_SECONDS
    )
    manifest_path = (
        env.get("MAC_TASK_EVIDENCE_MANIFEST_PATH")
        or DEFAULT_EVIDENCE_MANIFEST_PATH
    )
    if not cmd:
        def _noop(_task: JsonDict) -> _ExecResult:
            return _ExecResult(
                returncode=1,
                stdout="",
                stderr="MAC_TASK_EXECUTOR_COMMAND is unset; refusing to run a no-op task.",
            )

        return _noop

    def _run(task: JsonDict) -> _ExecResult:
        proc_env = dict(env)
        proc_env.setdefault("MAC_TASK_ID", str(task.get("id") or ""))
        proc_env.setdefault("MAC_TASK_TITLE", str(task.get("title") or ""))
        proc_env.setdefault("MAC_TASK_EVIDENCE_MANIFEST_PATH", manifest_path)
        try:
            if os.path.exists(manifest_path):
                os.unlink(manifest_path)
        except OSError:
            pass
        proc = subprocess.run(  # noqa: S602 — explicit operator-supplied cmd
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=proc_env,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # The executor output is captured (so it can be hashed into
        # evidence), which means it never reaches the pod's own logs. A
        # terminated pod would otherwise show nothing useful. Forward a
        # bounded tail of both streams to the runner's stdout/stderr so
        # postmortems work — errors usually appear at the tail.
        _forward_to_pod_logs("executor stdout", stdout)
        _forward_to_pod_logs("executor stderr", stderr, stream=sys.stderr)
        manifest, manifest_error = _read_verification_manifest(manifest_path)
        return _ExecResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8", "replace")).hexdigest(),
            verification_manifest=manifest,
            manifest_path=manifest_path,
            manifest_error=manifest_error,
        )

    return _run

POD_LOG_FORWARD_TAIL_BYTES = 16000


def _forward_to_pod_logs(
    label: str,
    text: str,
    *,
    stream: Any = None,
    tail_bytes: int = POD_LOG_FORWARD_TAIL_BYTES,
) -> None:
    """Echo a bounded tail of captured executor output to the runner's
    own stdout/stderr so terminated pod logs retain the most relevant
    (trailing) portion. Never raises — logging must not break a run."""
    if not text:
        return
    target = stream if stream is not None else sys.stdout
    try:
        if len(text) > tail_bytes:
            shown = text[-tail_bytes:]
            header = "mac-task-runner: %s (last %d bytes):" % (label, tail_bytes)
        else:
            shown = text
            header = "mac-task-runner: %s:" % label
        print(header, file=target)
        print(shown, file=target)
        target.flush()
    except Exception:  # noqa: BLE001 — best-effort pod logging
        pass

def _read_verification_manifest(
    path: str,
) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, "manifest file not found at %s" % path
    except OSError as exc:
        return None, "manifest file read failed: %s" % exc
    if not raw.strip():
        return None, "manifest file is empty"
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, "manifest is not valid JSON: %s" % exc
    if not isinstance(loaded, dict):
        return None, (
            "manifest must be a JSON object, got %s" % type(loaded).__name__
        )
    return loaded, None

def _default_mac_client(mac_url: str, token: str) -> Any:
    from mac.api_client import MacApiClient

    return MacApiClient(mac_url, token=token)

def _submit_execution_evidence(
    mac: Any,
    *,
    task_id: str,
    agent_id: str,
    exec_result: _ExecResult,
    duration_ms: float,
    lease_id: Optional[str] = None,
) -> Optional[JsonDict]:
    metadata: JsonDict = {
        "returncode": exec_result.returncode,
        "stdout_sha256": exec_result.stdout_sha256,
        "stdout_bytes": len(exec_result.stdout.encode("utf-8", "replace")),
        "duration_ms": duration_ms,
        "k8s_job": True,
    }
    if exec_result.verification_manifest is not None:
        metadata["verification"] = exec_result.verification_manifest
    if exec_result.manifest_path:
        metadata["verification_manifest_path"] = exec_result.manifest_path
    if exec_result.manifest_error:
        metadata["verification_manifest_error"] = exec_result.manifest_error
    try:
        body: JsonDict = {
            "kind": "log",
            "uri": "stdout://mac-task-runner",
            "summary": "executor returncode=%d duration_ms=%.1f"
            % (exec_result.returncode, duration_ms),
            "created_by": agent_id,
            "metadata": metadata,
        }
        if lease_id:
            body["lease_id"] = lease_id
        return mac.post("/tasks/%s/evidence" % _q(task_id), body)
    except Exception as exc:  # noqa: BLE001
        log.error("evidence POST failed for task=%s: %s", task_id, exc)
        return None

def _record_failure_evidence(
    mac: Any,
    task_id: str,
    lease_id: str,
    agent_id: str,
    *,
    returncode: int,
    error: str,
) -> JobExecutionResult:
    evidence = _submit_execution_evidence(
        mac,
        task_id=task_id,
        lease_id=lease_id,
        agent_id=agent_id,
        exec_result=_ExecResult(
            returncode=returncode,
            stdout=error,
            stderr=error,
            stdout_sha256=hashlib.sha256(error.encode("utf-8")).hexdigest(),
        ),
        duration_ms=0.0,
    )
    if evidence is None:
        return JobExecutionResult(
            status="no-evidence",
            task_id=task_id,
            lease_id=lease_id,
            returncode=returncode,
            error=error,
        )
    return _block_task_after_evidence(
        mac,
        task_id=task_id,
        lease_id=lease_id,
        agent_id=agent_id,
        reason="executor_exception",
        evidence_id=evidence.get("id"),
        returncode=returncode,
        error=error,
    )


def _block_task_after_evidence(
    mac: Any,
    task_id: str,
    lease_id: Optional[str],
    agent_id: str,
    *,
    reason: str,
    evidence_id: Optional[str],
    returncode: Optional[int],
    error: Optional[str] = None,
    duration_ms: Optional[float] = None,
    stdout_sha256: Optional[str] = None,
    detail_extra: Optional[JsonDict] = None,
) -> JobExecutionResult:
    detail: JsonDict = {
        "reason": reason,
        "manual_repair_required": True,
    }
    if detail_extra:
        detail.update(detail_extra)
    if returncode is not None:
        detail["returncode"] = returncode
    if evidence_id:
        detail["evidence_id"] = evidence_id
    if error:
        detail["error"] = error
    try:
        mac.post(
            "/tasks/%s/transition" % _q(task_id),
            {
                "target_state": "blocked",
                "actor": agent_id,
                "lease_id": lease_id,
                "detail": detail,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error("blocked-transition POST failed: %s", exc)
    return JobExecutionResult(
        status="blocked",
        task_id=task_id,
        lease_id=lease_id,
        returncode=returncode,
        evidence_id=evidence_id,
        error=error,
        duration_ms=duration_ms,
        stdout_sha256=stdout_sha256,
    )

def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_one_lease()
    print(
        "mac-task-runner: status=%s task=%r lease=%r returncode=%s evidence=%r error=%r"
        % (
            result.status,
            result.task_id,
            result.lease_id,
            result.returncode,
            result.evidence_id,
            result.error,
        )
    )
    return result.exit_code()

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
