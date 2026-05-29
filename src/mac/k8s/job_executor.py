"""``mac-task-runner`` — single-shot executor that runs inside one K8s Job.

The runner Deployment in ``mac.k8s.runner`` claims a lease and then
``kubectl create``s one Job per claim. Each Job's container invokes this
module's ``run_one_lease`` (via the ``mac-task-runner`` console script
entry point) with ``MAC_TASK_ID``, ``MAC_LEASE_ID``, ``MAC_URL``,
``MAC_AGENT_ID``, and ``MAC_WORKER_TOKEN`` already in env.

Lifecycle inside the Job:

  1. POST /tasks/{id}/start            (transition open→running)
  2. run the configured executor       (subprocess; honors timeout)
  3. POST /tasks/{id}/evidence         (record stdout digest + returncode)
  4. POST /tasks/{id}/submit-for-review   on success
     OR POST /tasks/{id}/transition target=failed   on non-zero exit
  5. sys.exit(0) iff evidence was submitted

Lease renewal is **not** performed inside the Job pod. Per spec §6.3
(Job-per-task Role Specialisation), the runner Deployment that
created this Job owns the per-claim renewal goroutine. The Job pod
authors evidence + lifecycle records as the role agent
(``MAC_AGENT_ID``), but never holds authority over the lease.

Evidence is recorded BEFORE the process exits. A clean Job exit means
mac-api saw the evidence; a crashed Job means the lease will expire
(the runner's renewal stops on the next Job-status poll) and mac-api
re-opens the task for another runner cycle.
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

# Default on-disk path the role executor writes a signed verification
# manifest to. Picked up by ``_submit_execution_evidence`` and merged
# into ``metadata.verification`` of the POST /tasks/{id}/evidence body,
# which is what mac's ``_assess_default_review_evidence`` (services.py
# 11204) verifies. Override via MAC_TASK_EVIDENCE_MANIFEST_PATH.
DEFAULT_EVIDENCE_MANIFEST_PATH = "/tmp/mac-evidence.json"


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

    # Transition open → running.  mac-api's /tasks/{id}/start expects
    # `agent_id` as a QUERY parameter, not a JSON body (api.py:2393).
    # Same convention as src/mac/worker.py:420.
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

    # Per spec §6.3, the runner Deployment renews the lease; the Job
    # pod does NOT. If the Job runs long enough for the lease to
    # expire (e.g. runner pod evicted mid-Job), mac-api will reopen
    # the task and another runner replica will reclaim it. Idempotency
    # on evidence + transitions handles the duplicate-execution case.
    started = time.monotonic()
    try:
        exec_result = executor(task)
    except Exception as exc:  # noqa: BLE001
        return _record_failure_evidence(
            mac, task_id, lease_id, agent_id, returncode=-1, error=str(exc)
        )

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
        # mac-api's /tasks/{id}/submit-for-review takes `agent_id` as a
        # query param (api.py:2409), same as /start.
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


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

@dataclass
class _ExecResult:
    returncode: int
    stdout: str
    stderr: str = ""
    stdout_sha256: Optional[str] = None
    # Verification manifest produced by the role executor (e.g. the
    # codex coder/reviewer scripts in deploy/codex-runner). When set,
    # ``_submit_execution_evidence`` merges it into
    # ``metadata.verification`` so the review-readiness gate at
    # services.py:4749 sees a signed manifest. ``None`` when the
    # executor produced no file or the file failed to parse — the
    # evidence is still POSTed (with a clear log line) so operators
    # can debug from the durable history.
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
        # Tell the executor where to drop the verification manifest. The
        # default matches DEFAULT_EVIDENCE_MANIFEST_PATH so role
        # executors that don't read this env (legacy stubs) still land
        # in the expected place.
        proc_env.setdefault("MAC_TASK_EVIDENCE_MANIFEST_PATH", manifest_path)
        # Best-effort: pre-clean any stale manifest from a previous run
        # so a crashed executor does not leak its predecessor's
        # signature into the next evidence body. Failure is ignored
        # because /tmp may be readonly on some test fixtures.
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
    """Best-effort load of a manifest file written by the role executor.

    Returns ``(manifest, error)``. ``manifest`` is ``None`` whenever the
    file is missing OR the JSON is not a dict OR parsing failed; the
    caller surfaces both into the evidence metadata so operators can
    debug why the review-readiness gate rejected a given run.
    """
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
    """Build a real `MacApiClient` for production use; importable lazily
    so unit tests don't pay the import cost."""
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
    # When the role executor wrote a verification manifest, merge it
    # into ``metadata.verification``. mac's review-readiness gate
    # (services.py:4749 → _assess_default_review_evidence) reads this
    # exact key to validate the signed manifest. Without this hook the
    # gate rejects every Job-produced evidence with
    # "missing_verification_manifest" — which is exactly what PR2c hit
    # at /submit-for-review.
    if exec_result.verification_manifest is not None:
        metadata["verification"] = exec_result.verification_manifest
    if exec_result.manifest_path:
        metadata["verification_manifest_path"] = exec_result.manifest_path
    if exec_result.manifest_error:
        # Recorded so operators inspecting the durable history can tell
        # "executor crashed before writing" from "executor wrote bad
        # JSON". The review-readiness gate doesn't read this key — it
        # only checks for `verification`.
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
