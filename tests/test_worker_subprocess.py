"""Isolation tests for the extracted worker subprocess boundary."""

from __future__ import annotations

import subprocess
import sys

import pytest

from mac.api_client import MacApiError
from mac.worker_subprocess import SubprocessExecutor, _ensure_json_object


def test_executor_requires_a_command() -> None:
    with pytest.raises(MacApiError, match="executor command is required"):
        SubprocessExecutor([])


def test_executor_starts_without_an_active_process() -> None:
    executor = SubprocessExecutor([sys.executable, "-c", "pass"])
    assert executor.has_active_process() is False
    assert executor.cancel_current() is False


def test_cancel_current_terminates_the_active_tree(monkeypatch) -> None:
    executor = SubprocessExecutor([sys.executable, "-c", "pass"])
    process = object()
    terminated = []
    monkeypatch.setattr(
        "mac.worker_subprocess._terminate_process_tree", terminated.append
    )
    executor._active_process = process  # type: ignore[assignment]

    assert executor.cancel_current("operator stop") is True
    assert terminated == [process]
    assert executor._cancel_reason == "operator stop"


def test_executor_runs_command_and_emits_audit(tmp_path) -> None:
    records = []
    executor = SubprocessExecutor(
        [sys.executable, "-c", "print('subprocess-ok')"], timeout=5
    )
    executor.audit_sink = records.append
    executor.audit_context = {"task_id": "task_test", "lease_id": "lease_test"}

    result = executor({"id": "task_test", "metadata": {}}, tmp_path)

    assert result.returncode == 0
    assert result.stdout.strip() == "subprocess-ok"
    assert [record["phase"] for record in records] == ["started", "completed"]
    assert records[-1]["task_id"] == "task_test"


def test_executor_timeout_preserves_timeout_evidence(tmp_path) -> None:
    records = []
    executor = SubprocessExecutor(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.01
    )
    executor.audit_sink = records.append

    with pytest.raises(subprocess.TimeoutExpired):
        executor({"id": "task_timeout", "metadata": {}}, tmp_path)

    assert records[-1]["phase"] == "timeout"
    assert records[-1]["metadata"]["timeout_seconds"] == 0.01
    assert executor.has_active_process() is False


def test_audit_failures_do_not_mask_task_execution() -> None:
    executor = SubprocessExecutor([sys.executable, "-c", "pass"])
    executor.audit_sink = lambda _record: (_ for _ in ()).throw(RuntimeError("sink"))
    executor._emit_audit({"phase": "started"})
    assert _ensure_json_object({"ok": True}) == {"ok": True}
    assert _ensure_json_object("not-json") == {}
