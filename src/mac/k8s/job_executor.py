"""``mac-task-runner`` — single-shot executor that runs inside one K8s Job.

The runner Deployment in ``mac.k8s.runner`` claims a lease and then
``kubectl create``s one Job per claim. Each Job's container invokes this
module's ``run_one_lease`` (via the ``mac-task-runner`` console script
entry point) with ``MAC_TASK_ID``, ``MAC_LEASE_ID``, ``MAC_URL``,
``MAC_AGENT_ID``, and ``MAC_WORKER_TOKEN`` already in env.

Lifecycle inside the Job:

  1. POST /tasks/{id}/start            (transition open→running)
  2. spawn lease-renewal thread        (POST /leases/{id}/renew every TTL/3)
  3. run the configured executor       (subprocess; honors timeout)
  4. POST /tasks/{id}/evidence         (record stdout digest + returncode)
  5. POST /tasks/{id}/submit-for-review   on success
     OR POST /tasks/{id}/transition target=failed   on non-zero exit
  6. stop renewal thread; sys.exit(0)  iff evidence was submitted

Evidence is recorded BEFORE the process exits. A clean Job exit means
mac-api saw the evidence; a crashed Job means the lease will expire
and mac-api re-opens the task for another runner cycle.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote


def _q(value: str) -> str:
    """URL-encode a path segment. Defensive against any path-rewriting
    layer between the Job pod and mac-api that might normalise `_` to
    `-` or otherwise mangle unreserved characters. `_` IS in RFC 3986's
    unreserved set so technically no encoding is needed, but a
    misbehaving proxy can still touch it; `%5F` is unambiguous."""
    return quote(value, safe="")

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)


# How long the executor subprocess is allowed to run before SIGKILL.
# Operators override per task via task.metadata.k8s.executor_timeout_seconds.
DEFAULT_EXECUTOR_TIMEOUT_SECONDS = 1500  # 25 min < default activeDeadline 30 min

# How frequently to renew the lease (fraction of remaining TTL).
LEASE_RENEW_INTERVAL_SECONDS = 30


@dataclass
class JobExecutionResult:
    status: str            # "submitted-for-review" | "failed" | "no-evidence" | "missing-env"
    task_id: Optional[str]
    lease_id: Optional[str]
    returncode: Optional[int]
    evidence_id: Optional[str] = None
    error: Optional[str] = None
    stdout_sha256: Optional[str] = None
    duration_ms: Optional[float] = None

    def exit_code(self) -> int:
        """0 iff evidence was successfully submitted (clean Job success).

        Any failure to communicate with mac-api or to run the executor
        produces a non-zero exit so the Job controller can surface it.
        Note: "task failed" is still a *clean* Job exit (0) because the
        failure evidence was successfully recorded.
        """
        if self.status in ("submitted-for-review", "failed"):
            return 0
        return 2


# ----------------------------------------------------------------------
# Public entry point used by the binary.
# ----------------------------------------------------------------------

def run_one_lease(
    *,
    mac: Optional[Any] = None,
    executor: Optional[Callable[[JsonDict], "_ExecResult"]] = None,
    env: Optional[Dict[str, str]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> JobExecutionResult:
    """Run the single-task lifecycle inside one K8s Job.

    All parameters are injectable so the function is testable without
    a live mac-api or a real subprocess.

    `mac`: a `MacApiClient`-shaped object with `.post(path, body)`.
    `executor`: callable that takes a task dict and returns `_ExecResult`
                (returncode + stdout text). Defaults to a subprocess
                wrapper.
    `env`: env-like mapping (defaults to ``os.environ``).
    """
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

    # Fetch the task so the executor + evidence step has full context.
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

    # Transition open → running.
    try:
        mac.post(
            "/tasks/%s/start" % _q(task_id),
            {"agent_id": agent_id},
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

    # Start lease-renewal background thread.
    stop_event = threading.Event()
    renewer = threading.Thread(
        target=_lease_renewal_loop,
        args=(mac, lease_id, stop_event, sleeper),
        name="mac-task-runner-lease",
        daemon=True,
    )
    renewer.start()

    started = time.monotonic()
    try:
        exec_result = executor(task)
    except Exception as exc:  # noqa: BLE001
        stop_event.set()
        return _record_failure_evidence(
            mac, task_id, lease_id, agent_id, returncode=-1, error=str(exc)
        )
    finally:
        stop_event.set()
        # Best-effort join; don't block exit on a stuck renewer.
        renewer.join(timeout=2.0)

    duration_ms = (time.monotonic() - started) * 1000.0

    # Submit evidence BEFORE deciding success/failure transition so the
    # durable record is in place no matter what happens next.
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
                "/tasks/%s/submit-for-review" % _q(task_id),
                {"actor": agent_id, "evidence_id": evidence.get("id")},
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


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

@dataclass
class _ExecResult:
    returncode: int
    stdout: str
    stderr: str = ""
    stdout_sha256: Optional[str] = None


def _default_subprocess_executor(env: Dict[str, str]) -> Callable[[JsonDict], _ExecResult]:
    """Returns a callable that runs the configured task executor command."""
    cmd = (env.get("MAC_TASK_EXECUTOR_COMMAND") or "").strip()
    timeout = int(
        env.get("MAC_TASK_EXECUTOR_TIMEOUT_SECONDS")
        or DEFAULT_EXECUTOR_TIMEOUT_SECONDS
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
        proc = subprocess.run(  # noqa: S602 — explicit operator-supplied cmd
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=proc_env,
        )
        stdout = proc.stdout or ""
        return _ExecResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=proc.stderr or "",
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8", "replace")).hexdigest(),
        )

    return _run


def _default_mac_client(mac_url: str, token: str) -> Any:
    """Build a real `MacApiClient` for production use; importable lazily
    so unit tests don't pay the import cost."""
    from mac.hermes_adapter import MacApiClient

    return MacApiClient(mac_url, token=token)


def _lease_renewal_loop(
    mac: Any,
    lease_id: str,
    stop: threading.Event,
    sleeper: Callable[[float], None],
) -> None:
    """Renew the lease every LEASE_RENEW_INTERVAL_SECONDS until stop."""
    while not stop.is_set():
        try:
            mac.post("/leases/%s/renew" % _q(lease_id), {})
        except Exception as exc:  # noqa: BLE001
            log.warning("lease renew failed: %s", exc)
        # Short-poll the stop event in 1s chunks so we exit promptly.
        for _ in range(int(LEASE_RENEW_INTERVAL_SECONDS)):
            if stop.is_set():
                return
            sleeper(1.0)


def _submit_execution_evidence(
    mac: Any,
    *,
    task_id: str,
    agent_id: str,
    exec_result: _ExecResult,
    duration_ms: float,
) -> Optional[JsonDict]:
    try:
        return mac.post(
            "/tasks/%s/evidence" % _q(task_id),
            {
                "kind": "log",
                "uri": "stdout://mac-task-runner",
                "summary": "executor returncode=%d duration_ms=%.1f"
                % (exec_result.returncode, duration_ms),
                "created_by": agent_id,
                "metadata": {
                    "returncode": exec_result.returncode,
                    "stdout_sha256": exec_result.stdout_sha256,
                    "stdout_bytes": len(exec_result.stdout.encode("utf-8", "replace")),
                    "duration_ms": duration_ms,
                    "k8s_job": True,
                },
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


# ----------------------------------------------------------------------
# Console-script main()
# ----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_one_lease()
    # Always surface task_id with explicit quotes so `_` vs `-` confusion
    # in log readers is obvious. Always include the error field — without
    # it `status=no-evidence` is opaque to operators.
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
