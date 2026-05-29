"""Tests for the mac-task-runner single-Job executor."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from mac.k8s.job_executor import (
    JobExecutionResult,
    _ExecResult,
    run_one_lease,
)


class _FakeMac:
    def __init__(
        self,
        *,
        task: Optional[Dict[str, Any]] = None,
        fail_on: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._task = task or {"id": "task-1", "title": "x"}
        self._fail_on = fail_on
        self._evidence = evidence or {"id": "ev-1"}
        self.posts: List[Dict[str, Any]] = []
        self.gets: List[str] = []

    def get(self, path: str) -> Dict[str, Any]:
        self.gets.append(path)
        if self._fail_on == "get-task":
            raise RuntimeError("network down")
        return self._task

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.posts.append({"path": path, "body": body})
        if self._fail_on and path.endswith(self._fail_on):
            raise RuntimeError("simulated %s failure" % self._fail_on)
        if path.endswith("/evidence"):
            return dict(self._evidence)
        return {"ok": True}


def _env(**overrides: str) -> Dict[str, str]:
    base = {
        "MAC_TASK_ID": "task-1",
        "MAC_LEASE_ID": "lease-1",
        "MAC_AGENT_ID": "runner-1",
        "MAC_URL": "http://mac",
        "MAC_WORKER_TOKEN": "tok",
    }
    base.update(overrides)
    return base


def _exec_ok(_task: Dict[str, Any]) -> _ExecResult:
    return _ExecResult(returncode=0, stdout="all good", stdout_sha256="abc123")


def _exec_fail(_task: Dict[str, Any]) -> _ExecResult:
    return _ExecResult(returncode=7, stdout="bad", stdout_sha256="def456")


def _exec_raises(_task: Dict[str, Any]) -> _ExecResult:
    raise RuntimeError("executor crashed")


def _no_sleep(_seconds: float) -> None:
    return None


def test_missing_env_yields_clean_error() -> None:
    result = run_one_lease(env={}, mac=_FakeMac(), executor=_exec_ok, sleeper=_no_sleep)
    assert result.status == "missing-env"
    assert result.exit_code() != 0


def test_happy_path_submits_evidence_then_submit_for_review() -> None:
    mac = _FakeMac()
    result = run_one_lease(env=_env(), mac=mac, executor=_exec_ok, sleeper=_no_sleep)
    assert result.status == "submitted-for-review"
    assert result.returncode == 0
    assert result.evidence_id == "ev-1"
    assert result.exit_code() == 0
    # Strip query strings — /start and /submit-for-review pass
    # agent_id via the query (matches mac-api signatures at api.py:2393
    # and 2409); the path-without-query is what the test cares about.
    paths = [p["path"].split("?", 1)[0] for p in mac.posts]
    assert "/tasks/task-1/start" in paths
    assert "/tasks/task-1/evidence" in paths
    assert "/tasks/task-1/submit-for-review" in paths
    # Evidence must be POSTed BEFORE submit-for-review.
    ev_idx = paths.index("/tasks/task-1/evidence")
    submit_idx = paths.index("/tasks/task-1/submit-for-review")
    assert ev_idx < submit_idx


def test_nonzero_executor_transitions_to_failed() -> None:
    mac = _FakeMac()
    result = run_one_lease(env=_env(), mac=mac, executor=_exec_fail, sleeper=_no_sleep)
    assert result.status == "failed"
    assert result.returncode == 7
    assert result.evidence_id == "ev-1"
    assert result.exit_code() == 0  # failed task is a clean Job exit
    failed_post = next(
        p for p in mac.posts if p["path"].endswith("/transition")
    )
    assert failed_post["body"]["target_state"] == "failed"
    assert failed_post["body"]["detail"]["returncode"] == 7


def test_executor_exception_records_failure_evidence() -> None:
    mac = _FakeMac()
    result = run_one_lease(
        env=_env(), mac=mac, executor=_exec_raises, sleeper=_no_sleep
    )
    assert result.status == "failed"
    assert result.evidence_id == "ev-1"
    assert result.exit_code() == 0
    # Both evidence + failed-transition POSTs were made.
    paths = [p["path"].split("?", 1)[0] for p in mac.posts]
    assert "/tasks/task-1/evidence" in paths
    assert "/tasks/task-1/transition" in paths


def test_evidence_failure_returns_no_evidence_status() -> None:
    mac = _FakeMac(fail_on="/evidence")
    result = run_one_lease(env=_env(), mac=mac, executor=_exec_ok, sleeper=_no_sleep)
    assert result.status == "no-evidence"
    assert result.exit_code() != 0  # non-zero so K8s notices the failure


def test_start_already_running_is_tolerated() -> None:
    class _MacAlreadyRunning(_FakeMac):
        def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
            self.posts.append({"path": path, "body": body})
            if path.endswith("/start"):
                raise RuntimeError("task is already running")
            if path.endswith("/evidence"):
                return {"id": "ev-1"}
            return {}

    mac = _MacAlreadyRunning()
    result = run_one_lease(env=_env(), mac=mac, executor=_exec_ok, sleeper=_no_sleep)
    assert result.status == "submitted-for-review"


def test_get_task_failure_aborts_cleanly() -> None:
    mac = _FakeMac(fail_on="get-task")
    result = run_one_lease(env=_env(), mac=mac, executor=_exec_ok, sleeper=_no_sleep)
    assert result.status == "no-evidence"
    assert result.exit_code() != 0


def test_evidence_metadata_carries_stdout_digest_and_returncode() -> None:
    mac = _FakeMac()
    run_one_lease(env=_env(), mac=mac, executor=_exec_ok, sleeper=_no_sleep)
    evidence_post = next(p for p in mac.posts if p["path"].endswith("/evidence"))
    md = evidence_post["body"]["metadata"]
    assert md["returncode"] == 0
    assert md["stdout_sha256"] == "abc123"
    assert md["k8s_job"] is True


def test_exit_code_table() -> None:
    assert JobExecutionResult(
        status="submitted-for-review",
        task_id="t",
        lease_id="l",
        returncode=0,
    ).exit_code() == 0
    assert JobExecutionResult(
        status="failed", task_id="t", lease_id="l", returncode=1
    ).exit_code() == 0  # failed-but-recorded is a clean Job exit
    assert JobExecutionResult(
        status="no-evidence", task_id="t", lease_id="l", returncode=None
    ).exit_code() != 0
    assert JobExecutionResult(
        status="missing-env", task_id=None, lease_id=None, returncode=None
    ).exit_code() != 0
