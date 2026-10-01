"""Kubernetes Job executor for MAC task attempts.

Builds and launches Kubernetes Jobs that run individual task attempts, tracking
their identifiers and results for the orchestrator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from mac.fleet_env import resolve_first


def _q(value: str) -> str:
    return quote(value, safe="")


JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

DEFAULT_EXECUTOR_TIMEOUT_SECONDS = 1500  # 25 min < default activeDeadline 30 min

DEFAULT_EVIDENCE_MANIFEST_PATH = "/tmp/mac-evidence.json"


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
    # Resolve fleet-aware (honors env["MAC_FLEET"]) so a legacy flat token can't
    # shadow the scoped MAC_*__<FLEET> form (mac-g55y).
    token = resolve_first(["MAC_WORKER_TOKEN", "MAC_API_TOKEN"], env=env) or ""
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
    """Execute a single leased task and return the execution result."""
    env = env if env is not None else os.environ
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
            "/tasks/%s/start?agent_id=%s&lease_id=%s" % (_q(task_id), _q(agent_id), _q(lease_id)),
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


def _default_subprocess_executor(env: Dict[str, str]) -> Callable[[JsonDict], _ExecResult]:
    """Returns a callable that runs the configured task executor command."""
    cmd = (env.get("MAC_TASK_EXECUTOR_COMMAND") or "").strip()
    timeout = int(env.get("MAC_TASK_EXECUTOR_TIMEOUT_SECONDS") or DEFAULT_EXECUTOR_TIMEOUT_SECONDS)
    manifest_path = env.get("MAC_TASK_EVIDENCE_MANIFEST_PATH") or DEFAULT_EVIDENCE_MANIFEST_PATH
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
        return None, ("manifest must be a JSON object, got %s" % type(loaded).__name__)
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
) -> JobExecutionResult:
    detail: JsonDict = {
        "reason": reason,
        "manual_repair_required": True,
    }
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
    """Run the task runner entry point and return its exit code."""
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
