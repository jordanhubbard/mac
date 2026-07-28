"""Coverage for OpenShell sandbox lifecycle edge cases."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import task_executor as te


def test_sandbox_step_success_failure_and_exception(monkeypatch) -> None:
    monkeypatch.setattr(te, "_openshell_bin", lambda: "openshell")
    monkeypatch.setattr(
        te,
        "_run_captured",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "stdout", ""),
    )
    assert te._sandbox_step(["delete", "x"], timeout=1) == (True, "stdout")
    monkeypatch.setattr(
        te,
        "_run_captured",
        lambda *_a, **_k: subprocess.CompletedProcess([], 2, "", "stderr"),
    )
    assert te._sandbox_step(["delete", "x"], timeout=1) == (False, "stderr")
    monkeypatch.setattr(
        te,
        "_run_captured",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")),
    )
    assert te._sandbox_step(["delete", "x"], timeout=1) == (False, "offline")


def test_sandbox_repository_roots_read_env_and_context_files(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    download = tmp_path / "download"
    repo = workspace / "repo"
    workspace.mkdir()
    download.mkdir()
    repo.mkdir()
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    (workspace / "repository-worktree.json").write_text("not-json")
    (download / "repository-worktree.json").write_text(
        json.dumps({"repository_worktree": str(repo)})
    )
    assert te._sandbox_repository_roots(workspace, download) == {Path("repo")}
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(tmp_path / "outside"))
    (download / "repository-worktree.json").write_text("[]")
    assert te._sandbox_repository_roots(workspace, download) == set()


def test_merge_download_tree_handles_symlinks_backups_and_replacements(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    download = tmp_path / "download"
    workspace.mkdir()
    download.mkdir()
    (workspace / "obsolete.txt").write_text("old")
    (workspace / ".git.bak-old").mkdir()
    (workspace / ".git.bak-file").write_text("old")
    (workspace / "replace").mkdir()
    (workspace / "replace" / "old").write_text("old")
    (download / "replace").write_text("new")
    (download / "target").write_text("target")
    (download / "link").symlink_to("target")
    (download / "dir-target").mkdir()
    (download / "dir-target" / "file").write_text("value")
    (download / "dir-link").symlink_to("dir-target", target_is_directory=True)
    te._merge_sandbox_download_tree(download, workspace)
    assert not (workspace / "obsolete.txt").exists()
    assert not (workspace / ".git.bak-old").exists()
    assert not (workspace / ".git.bak-file").exists()
    assert (workspace / "replace").read_text() == "new"
    assert (workspace / "link").is_symlink()
    assert os.readlink(workspace / "dir-link") == "dir-target"


def test_sandbox_progress_snapshot_parses_known_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        te,
        "_sandbox_step",
        lambda *_a, **_k: (
            True,
            "noise=x\nready=1\nhead=abc\nchanged_count=2\nchanged_digest=d\nmanifest=1\n",
        ),
    )
    snapshot = te._sandbox_progress_snapshot("sandbox", "work", tmp_path)
    assert snapshot == {
        "ready": "1",
        "head": "abc",
        "changed_count": "2",
        "changed_digest": "d",
        "manifest": "1",
    }
    monkeypatch.setattr(te, "_sandbox_step", lambda *_a, **_k: (False, "bad"))
    assert te._sandbox_progress_snapshot("sandbox", "work", tmp_path) is None
    monkeypatch.setattr(te, "_sandbox_step", lambda *_a, **_k: (True, "ready=0"))
    assert te._sandbox_progress_snapshot("sandbox", "work", tmp_path) is None


def test_progress_monitor_transitions_and_stop(monkeypatch, tmp_path) -> None:
    snapshots = iter(
        [
            {
                "ready": "1",
                "head": "new",
                "changed_count": "bad",
                "changed_digest": "d",
                "manifest": "1",
            },
            {
                "ready": "1",
                "head": "new",
                "changed_count": "2",
                "changed_digest": "d2",
                "manifest": "1",
            },
        ]
    )
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", "old")
    monkeypatch.setattr(te, "_sandbox_progress_snapshot", lambda *_a: next(snapshots))
    telemetry = []
    monkeypatch.setattr(te, "emit_telemetry", lambda event, **kwargs: telemetry.append((event, kwargs)))
    monitor = te._SandboxProgressMonitor("name", "work", tmp_path, "task")
    monitor.observe()
    monitor.observe()
    monitor.interval = 0
    monitor.stop()
    monitor.stop()
    assert monitor.ready is True
    assert monitor.mutated is True
    assert monitor.manifest_seen is True
    assert monitor.changed_file_count == 2
    assert {event for event, _ in telemetry} >= {
        "sandbox_ready",
        "sandbox_first_mutation",
        "sandbox_head_observed",
        "sandbox_manifest_observed",
    }


def test_progress_monitor_start_and_clean_stop(monkeypatch, tmp_path) -> None:
    thread = SimpleNamespace(start=lambda: None, join=lambda timeout=None: None)
    monkeypatch.setattr(te.threading, "Thread", lambda **_kwargs: thread)
    monkeypatch.setattr(te, "_sandbox_progress_snapshot", lambda *_a: {"ready": "1"})
    telemetry = []
    monkeypatch.setattr(te, "emit_telemetry", lambda event, **kwargs: telemetry.append(event))
    monitor = te._SandboxProgressMonitor("name", "work", tmp_path, None)
    monitor.interval = 1
    monitor.start()
    monitor.stop()
    assert monitor.thread is thread
    assert "sandbox_no_effect" in telemetry


def test_progress_monitor_does_not_claim_clean_when_snapshot_is_unavailable(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(te, "_sandbox_progress_snapshot", lambda *_a: None)
    telemetry = []
    monkeypatch.setattr(
        te,
        "emit_telemetry",
        lambda event, **kwargs: telemetry.append((event, kwargs)),
    )
    monitor = te._SandboxProgressMonitor("name", "work", tmp_path, "task")
    monitor.interval = 1

    monitor.stop()

    assert [event for event, _detail in telemetry] == [
        "sandbox_observation_unavailable"
    ]
    assert telemetry[0][1]["state"] == "unknown"
    assert monitor.evidence()["ready_observed"] is False


def test_executor_has_no_vendored_hermes_messaging_mcp_bypass() -> None:
    assert not hasattr(te, "_mcp_serve_argv")
    assert not hasattr(te, "_coding_agent_mcp_config_path")


def test_build_probe_argv_validation_and_openshell_probe(monkeypatch, tmp_path) -> None:
    with pytest.raises(ValueError, match="private-file"):
        te._build_sandbox_probe_argv("name", ["agent"], tmp_path)
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--env BAD=1")
    with pytest.raises(ValueError, match="may not contain"):
        te._build_sandbox_probe_argv("name", ["python", "-m", "mac.agent_command"], tmp_path)
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--gpu")
    monkeypatch.setattr(te, "_openshell_bin", lambda: "openshell")
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy")
    argv = te._build_sandbox_probe_argv(
        "name", ["python", "-m", "mac.agent_command"], tmp_path
    )
    assert "--gpu" not in argv
    monkeypatch.setattr(
        te.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 2, "out", "err"),
    )
    assert te._openshell_probe(argv, timeout=1) == (2, "outerr")
    monkeypatch.setattr(te.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")))
    assert te._openshell_probe(argv, timeout=1) == (1, "offline")


def test_runner_choice_telemetry_failure_is_best_effort(monkeypatch, capsys) -> None:
    monkeypatch.setattr(te, "emit_telemetry", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))
    te._record_runner_choice("hermes", [])
    assert "no rationale" in capsys.readouterr().err
