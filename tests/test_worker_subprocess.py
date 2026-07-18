"""Isolation tests for the extracted worker subprocess boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from mac.api_client import MacApiError
from mac.worker_subprocess import (
    SubprocessExecutor,
    _cargo_path_dirs,
    _ensure_json_object,
)


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


def test_read_only_review_fences_git_credentials_without_repository_context(
    tmp_path, monkeypatch
) -> None:
    # Isolate the child-environment fence; approved-wrapper identity has its
    # own fail-closed tests and intentionally rejects this inline Python probe.
    monkeypatch.setattr(
        "mac.worker_subprocess._assert_approved_read_only_report_host_executor",
        lambda _argv, _environment: None,
    )
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-child")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!publish")
    monkeypatch.setenv("OPENAI_API_KEY", "model-credential")
    names = [
        "GH_TOKEN",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_TERMINAL_PROMPT",
        "MAC_TASK_REPO_ACCESS_SCHEMA",
        "MAC_TASK_REPO_ACCESS_MODE",
        "OPENAI_API_KEY",
    ]
    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import json, os; print(json.dumps({name: os.environ.get(name) for name in %r}))"
            % names,
        ],
        timeout=5,
    )
    task = {
        "id": "task_read_only_review",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
            "review_context": {"executor_evidence_id": "evidence_x"},
        },
    }

    result = executor(task, tmp_path)
    child = json.loads(result.stdout)

    assert child["GH_TOKEN"] is None
    assert child["GIT_CONFIG_COUNT"] is None
    assert child["GIT_CONFIG_KEY_0"] is None
    assert child["GIT_CONFIG_VALUE_0"] is None
    assert child["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert child["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert child["GIT_TERMINAL_PROMPT"] == "0"
    assert child["MAC_TASK_REPO_ACCESS_SCHEMA"] == "mac.report_repository_access.v1"
    assert child["MAC_TASK_REPO_ACCESS_MODE"] == "read_only"
    assert child["OPENAI_API_KEY"] == "model-credential"


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


def test_cargo_path_dirs_only_returns_existing_dirs(tmp_path, monkeypatch) -> None:
    cargo_home = tmp_path / "cargo"
    (cargo_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))

    dirs = _cargo_path_dirs()

    assert dirs[0] == str(cargo_home / "bin")
    # Nonexistent system dirs must not be returned.
    assert all(os.path.isdir(entry) for entry in dirs)


def test_cargo_path_dirs_falls_back_to_home_cargo(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CARGO_HOME", raising=False)
    home = tmp_path / "home"
    (home / ".cargo" / "bin").mkdir(parents=True)
    monkeypatch.setattr("mac.worker_subprocess.Path.home", classmethod(lambda cls: home))

    dirs = _cargo_path_dirs()

    assert str(home / ".cargo" / "bin") in dirs


def test_cargo_path_dirs_respects_cargo_home(tmp_path, monkeypatch) -> None:
    cargo_home = tmp_path / "custom-cargo"
    (cargo_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setattr(
        "mac.worker_subprocess.Path.home",
        classmethod(lambda cls: tmp_path / "unused-home"),
    )

    dirs = _cargo_path_dirs()

    assert str(cargo_home / "bin") in dirs
    assert str(tmp_path / "unused-home" / ".cargo" / "bin") not in dirs


def test_executor_prepends_cargo_dirs_to_child_path(tmp_path, monkeypatch) -> None:
    cargo_home = tmp_path / "cargo"
    cargo_bin = cargo_home / "bin"
    cargo_bin.mkdir(parents=True)
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    original_environ_path = os.environ["PATH"]

    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['PATH'])",
        ],
        timeout=10,
    )

    result = executor({"id": "task_path", "metadata": {}}, tmp_path)

    child_path = result.stdout.strip()
    entries = child_path.split(os.pathsep)
    assert entries[0] == str(cargo_bin)
    # Existing entries and their order are preserved after the injected dirs.
    assert entries[-2:] == ["/usr/bin", "/bin"]
    # os.environ itself must not be mutated.
    assert os.environ["PATH"] == original_environ_path


def test_executor_does_not_duplicate_existing_cargo_dir(tmp_path, monkeypatch) -> None:
    cargo_home = tmp_path / "cargo"
    cargo_bin = cargo_home / "bin"
    cargo_bin.mkdir(parents=True)
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("PATH", f"{cargo_bin}:/usr/bin:/bin")

    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['PATH'])",
        ],
        timeout=10,
    )

    result = executor({"id": "task_path_dup", "metadata": {}}, tmp_path)

    entries = result.stdout.strip().split(os.pathsep)
    assert entries.count(str(cargo_bin)) == 1


def test_executor_injects_home_cargo_dir_when_cargo_home_unset(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("CARGO_HOME", raising=False)
    home = tmp_path / "home"
    cargo_bin = home / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    monkeypatch.setattr(
        "mac.worker_subprocess.Path.home", classmethod(lambda cls: home)
    )
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['PATH'])",
        ],
        timeout=10,
    )

    result = executor({"id": "task_home_cargo", "metadata": {}}, tmp_path)

    entries = result.stdout.strip().split(os.pathsep)
    assert entries[0] == str(cargo_bin)
    assert entries[-2:] == ["/usr/bin", "/bin"]


def test_executor_does_not_inject_nonexistent_cargo_dir(tmp_path, monkeypatch) -> None:
    # CARGO_HOME and Path.home() both point at directories whose cargo bin/ does
    # not exist on disk, so no cargo dir may be injected into the child PATH.
    cargo_home = tmp_path / "missing-cargo"
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setattr(
        "mac.worker_subprocess.Path.home",
        classmethod(lambda cls: tmp_path / "unused-home"),
    )
    # Isolate the child PATH from host system dirs (e.g. /usr/local/bin) that are
    # also cargo candidates, so this assertion is host-independent.
    sentinel = tmp_path / "orig-path"
    sentinel.mkdir()
    monkeypatch.setenv("PATH", str(sentinel))

    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['PATH'])",
        ],
        timeout=10,
    )

    result = executor({"id": "task_missing_cargo", "metadata": {}}, tmp_path)

    entries = result.stdout.strip().split(os.pathsep)
    # The non-existent cargo candidate must be absent from the child PATH.
    assert str(cargo_home / "bin") not in entries
    assert str(tmp_path / "unused-home" / ".cargo" / "bin") not in entries
    # The original PATH entry is preserved.
    assert str(sentinel) in entries
