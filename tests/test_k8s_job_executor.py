"""Tests for the mac-task-runner single-Job executor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from mac.k8s.job_executor import (
    DEFAULT_EVIDENCE_MANIFEST_PATH,
    JobExecutionResult,
    _default_subprocess_executor,
    _ExecResult,
    _read_verification_manifest,
    _submit_execution_evidence,
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
    # Per spec §6.3, the Job pod NO LONGER renews the lease — the
    # runner Deployment owns renewal. No /leases/{id}/renew calls
    # should appear in the Job's POST traffic.
    assert not any("/renew" in p for p in paths), (
        "Job pod must not renew the lease; runner Deployment owns renewal"
    )


def test_job_pod_does_not_renew_lease() -> None:
    """Regression guard for spec §6.3 — even on a long-running executor
    we must never see a /renew call from the Job pod itself."""

    def _slow_exec(_task: Dict[str, Any]) -> _ExecResult:
        return _ExecResult(returncode=0, stdout="ok", stdout_sha256="z")

    mac = _FakeMac()
    run_one_lease(env=_env(), mac=mac, executor=_slow_exec, sleeper=_no_sleep)
    for p in mac.posts:
        assert "/renew" not in p["path"], (
            "Job pod made an unexpected /renew call: %s" % p
        )


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


# ----------------------------------------------------------------------
# PR3: verification manifest file → metadata.verification plumbing.
# Without this hook, the review-readiness gate at services.py:4749
# rejects every Job-produced evidence as "missing_verification_manifest".
# ----------------------------------------------------------------------


class TestReadVerificationManifest:
    def test_missing_file_returns_none_with_error(self, tmp_path: Path) -> None:
        manifest, err = _read_verification_manifest(str(tmp_path / "absent.json"))
        assert manifest is None
        assert err is not None and "not found" in err

    def test_empty_file_returns_none_with_error(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.json"
        p.write_text("", encoding="utf-8")
        manifest, err = _read_verification_manifest(str(p))
        assert manifest is None
        assert err is not None and "empty" in err

    def test_invalid_json_returns_none_with_error(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not-json", encoding="utf-8")
        manifest, err = _read_verification_manifest(str(p))
        assert manifest is None
        assert err is not None and "JSON" in err

    def test_non_object_returns_none_with_error(self, tmp_path: Path) -> None:
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        manifest, err = _read_verification_manifest(str(p))
        assert manifest is None
        assert err is not None and "object" in err

    def test_valid_manifest_returns_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.json"
        payload = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "x",
            "signed_by": "agent-1",
            "signature": "v1:abc",
        }
        p.write_text(json.dumps(payload), encoding="utf-8")
        manifest, err = _read_verification_manifest(str(p))
        assert err is None
        assert manifest == payload


class TestEvidenceSubmissionMergesManifest:
    """The Job executor MUST merge the manifest read from disk into
    `metadata.verification` of the POST /tasks/{id}/evidence body so
    mac's review-readiness gate sees a signed manifest."""

    def test_manifest_present_is_merged_into_metadata(self) -> None:
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "ok",
            "signed_by": "agent-1",
            "signature": "v1:xyz",
        }
        mac = _FakeMac()
        result = _ExecResult(
            returncode=0,
            stdout="ok",
            stdout_sha256="abc",
            verification_manifest=manifest,
            manifest_path="/tmp/mac-evidence.json",
        )
        _submit_execution_evidence(
            mac,
            task_id="task-1",
            agent_id="agent-1",
            exec_result=result,
            duration_ms=42.0,
        )
        ev_post = next(p for p in mac.posts if p["path"].endswith("/evidence"))
        md = ev_post["body"]["metadata"]
        assert md["verification"] == manifest
        assert md["verification_manifest_path"] == "/tmp/mac-evidence.json"
        assert "verification_manifest_error" not in md

    def test_manifest_error_is_recorded_without_verification_key(self) -> None:
        mac = _FakeMac()
        result = _ExecResult(
            returncode=0,
            stdout="",
            stdout_sha256="abc",
            verification_manifest=None,
            manifest_path="/tmp/mac-evidence.json",
            manifest_error="manifest file not found at /tmp/mac-evidence.json",
        )
        _submit_execution_evidence(
            mac,
            task_id="task-1",
            agent_id="agent-1",
            exec_result=result,
            duration_ms=1.0,
        )
        ev_post = next(p for p in mac.posts if p["path"].endswith("/evidence"))
        md = ev_post["body"]["metadata"]
        # `verification` is NOT set so the gate rejects with
        # "missing_verification_manifest" — the operator-debuggable
        # outcome documented in PR3.
        assert "verification" not in md
        assert "not found" in md["verification_manifest_error"]

    def test_manifest_absent_does_not_emit_verification_key(self) -> None:
        # Today's behaviour for a non-codex executor that doesn't drop
        # a manifest file. metadata.verification stays unset; the gate
        # still rejects (as before this PR), but no spurious key is
        # written.
        mac = _FakeMac()
        result = _ExecResult(returncode=0, stdout="x", stdout_sha256="h")
        _submit_execution_evidence(
            mac,
            task_id="task-1",
            agent_id="agent-1",
            exec_result=result,
            duration_ms=1.0,
        )
        ev_post = next(p for p in mac.posts if p["path"].endswith("/evidence"))
        md = ev_post["body"]["metadata"]
        assert "verification" not in md
        assert "verification_manifest_path" not in md
        assert "verification_manifest_error" not in md


class TestSubprocessExecutorReadsManifest:
    """End-to-end check that the subprocess executor reads the manifest
    file from the path it tells the subprocess to write to."""

    def test_executor_picks_up_manifest_written_by_subprocess(
        self, tmp_path: Path
    ) -> None:
        manifest_path = tmp_path / "mac-evidence.json"
        # Build a tiny one-shot "executor" that copies a pre-staged
        # manifest into the path the executor told it to use. Avoid
        # embedding the JSON in the subprocess command — that would
        # require shell-escaping a dict repr and is fragile across
        # python repr formats.
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "test",
            "signed_by": "agent-1",
            "signature": "v1:fake",
        }
        staged = tmp_path / "staged.json"
        staged.write_text(json.dumps(manifest), encoding="utf-8")
        cmd = (
            "%s -c "
            "'import os, shutil; "
            "shutil.copyfile(os.environ[\"STAGED\"], "
            "os.environ[\"MAC_TASK_EVIDENCE_MANIFEST_PATH\"])'"
        ) % sys.executable
        env = {
            "MAC_TASK_EXECUTOR_COMMAND": cmd,
            "MAC_TASK_EVIDENCE_MANIFEST_PATH": str(manifest_path),
            "STAGED": str(staged),
        }
        executor = _default_subprocess_executor(env)
        result = executor({"id": "task-1", "title": "x"})
        assert result.returncode == 0, "executor stderr: %s" % result.stderr
        assert result.verification_manifest == manifest
        assert result.manifest_path == str(manifest_path)
        assert result.manifest_error is None

    def test_executor_records_missing_manifest_error(
        self, tmp_path: Path
    ) -> None:
        manifest_path = tmp_path / "mac-evidence.json"
        # Executor that does NOT write a manifest.
        env = {
            "MAC_TASK_EXECUTOR_COMMAND": "true",
            "MAC_TASK_EVIDENCE_MANIFEST_PATH": str(manifest_path),
        }
        executor = _default_subprocess_executor(env)
        result = executor({"id": "task-1", "title": "x"})
        assert result.returncode == 0
        assert result.verification_manifest is None
        assert result.manifest_error is not None
        assert "not found" in result.manifest_error

    def test_executor_default_path_is_constant(self) -> None:
        # Sanity check: the default path matches the constant the role
        # executor scripts (deploy/codex-runner/mac-task-executor-codex)
        # use as their default. Drift here means the file the script
        # writes is not the file the executor reads.
        env = {"MAC_TASK_EXECUTOR_COMMAND": "true"}
        executor = _default_subprocess_executor(env)
        # We can't test the result.manifest_path directly without
        # writing a file in the well-known location (which would be a
        # bad test). Instead: just assert the constant exists at the
        # expected name and value.
        assert DEFAULT_EVIDENCE_MANIFEST_PATH == "/tmp/mac-evidence.json"


def test_run_one_lease_threads_manifest_through_to_evidence_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle: executor writes a manifest, run_one_lease POSTs
    evidence with metadata.verification set."""
    manifest_path = tmp_path / "mac-evidence.json"
    signed = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "PR3 e2e",
        "signed_by": "runner-1",
        "signature": "v1:fake-sig",
    }

    def _exec_writes_manifest(_task: Dict[str, Any]) -> _ExecResult:
        manifest_path.write_text(json.dumps(signed), encoding="utf-8")
        return _ExecResult(
            returncode=0,
            stdout="ok",
            stdout_sha256="h",
            verification_manifest=signed,
            manifest_path=str(manifest_path),
        )

    mac = _FakeMac()
    result = run_one_lease(
        env=_env(),
        mac=mac,
        executor=_exec_writes_manifest,
        sleeper=_no_sleep,
    )
    assert result.status == "submitted-for-review"
    ev_post = next(p for p in mac.posts if p["path"].endswith("/evidence"))
    assert ev_post["body"]["metadata"]["verification"] == signed
