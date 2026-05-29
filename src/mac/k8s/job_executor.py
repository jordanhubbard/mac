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
        if self.status in ("submitted-for-review", "failed"):
            return 0
        return 2

def run_one_lease(
    *,
    mac: Optional[Any] = None,
    executor: Optional[Callable[[JsonDict], "_ExecResult"]] = None,
    env: Optional[Dict[str, str]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> JobExecutionResult:
    env = env if env is not None else os.environ
    task_id = env.get("MAC_TASK_ID", "").strip()
    lease_id = env.get("MAC_LEASE_ID", "").strip()
    agent_id = env.get("MAC_AGENT_ID", "").strip() or "mac-task-runner"
    mac_url = env.get("MAC_URL") or env.get("MAC_HUB_URL", "")
    token = env.get("MAC_WORKER_TOKEN") or env.get("MAC_API_TOKEN", "")

    if not task_id or not lease_id:
        return JobExecutionResult(
            status="missing-env",
            task_id=task_id or None,
            lease_id=lease_id or None,
            returncode=None,
            error="MAC_TASK_ID and MAC_LEASE_ID are required in the Job env",
        )

    if mac is None:
        mac = _default_mac_client(mac_url, token)

    if executor is None:
        executor = _default_subprocess_executor(env)

    sleeper = sleeper or time.sleep

    try:
        task = mac.get("/tasks/%s" % _q(task_id))
    except Exception as exc:  # noqa: BLE001
        return JobExecutionResult(
            status="no-evidence",
            task_id=task_id,
            lease_id=lease_id,
            returncode=None,
            error="GET /tasks/{id} failed: %s" % exc,
        )

    try:
        mac.post(
            "/tasks/%s/start?agent_id=%s" % (_q(task_id), _q(agent_id)),
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

    started = time.monotonic()
    try:
        exec_result = executor(task)
    except Exception as exc:  # noqa: BLE001
        return _record_failure_evidence(
            mac, task_id, lease_id, agent_id, returncode=-1, error=str(exc)
        )

    duration_ms = (time.monotonic() - started) * 1000.0

    evidence = _submit_execution_evidence(
        mac,
        task_id=task_id,
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
                "/tasks/%s/submit-for-review?agent_id=%s"
                % (_q(task_id), _q(agent_id)),
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
            return JobExecutionResult(
                status="no-evidence",
                task_id=task_id,
                lease_id=lease_id,
                returncode=0,
                evidence_id=evidence.get("id"),
                duration_ms=duration_ms,
                error="submit-for-review failed: %s" % exc,
            )

    # Non-zero executor exit -> transition to failed.
    try:
        mac.post(
            "/tasks/%s/transition" % _q(task_id),
            {
                "target_state": "failed",
                "actor": agent_id,
                "detail": {
                    "reason": "executor_nonzero_exit",
                    "returncode": exec_result.returncode,
                    "evidence_id": evidence.get("id"),
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error("failed-transition POST failed: %s", exc)
    return JobExecutionResult(
        status="failed",
        task_id=task_id,
        lease_id=lease_id,
        returncode=exec_result.returncode,
        evidence_id=evidence.get("id"),
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
        manifest, manifest_error = _read_verification_manifest(manifest_path)
        return _ExecResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=proc.stderr or "",
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8", "replace")).hexdigest(),
            verification_manifest=manifest,
            manifest_path=manifest_path,
            manifest_error=manifest_error,
        )

    return _run

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
    from mac.hermes_adapter import MacApiClient

    return MacApiClient(mac_url, token=token)

def _submit_execution_evidence(
    mac: Any,
    *,
    task_id: str,
    agent_id: str,
    exec_result: _ExecResult,
    duration_ms: float,
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
        return mac.post(
            "/tasks/%s/evidence" % _q(task_id),
            {
                "kind": "log",
                "uri": "stdout://mac-task-runner",
                "summary": "executor returncode=%d duration_ms=%.1f"
                % (exec_result.returncode, duration_ms),
                "created_by": agent_id,
                "metadata": metadata,
            },
        )
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
    try:
        mac.post(
            "/tasks/%s/transition" % _q(task_id),
            {
                "target_state": "failed",
                "actor": agent_id,
                "detail": {
                    "reason": "executor_exception",
                    "error": error,
                    "evidence_id": evidence.get("id"),
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error("failed-transition POST failed: %s", exc)
    return JobExecutionResult(
        status="failed",
        task_id=task_id,
        lease_id=lease_id,
        returncode=returncode,
        evidence_id=evidence.get("id"),
        error=error,
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
