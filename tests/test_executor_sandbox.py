"""Ownership and compatibility tests for the executor sandbox extraction."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_task_executor_aliases_canonical_sandbox_module() -> None:
    task_executor = importlib.import_module("mac.task_executor")
    executor_sandbox = importlib.import_module("mac.executor_sandbox")

    assert task_executor is executor_sandbox
    assert callable(task_executor.main)


def test_compatibility_monkeypatch_changes_canonical_module(monkeypatch) -> None:
    task_executor = importlib.import_module("mac.task_executor")
    executor_sandbox = importlib.import_module("mac.executor_sandbox")
    sentinel = object()

    monkeypatch.setattr(task_executor, "_openshell_bin", sentinel)

    assert executor_sandbox._openshell_bin is sentinel


def test_loopback_urls_are_rewritten_for_the_sandbox_host() -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")

    assert (
        sandbox._rewrite_host_local_url(
            "http://127.0.0.1:8789/v1", "host.openshell.internal"
        )
        == "http://host.openshell.internal:8789/v1"
    )
    assert (
        sandbox._rewrite_host_local_url(
            "https://hub.example.test/v1", "host.openshell.internal"
        )
        == "https://hub.example.test/v1"
    )


def test_workspace_paths_map_only_children_into_the_sandbox(tmp_path: Path) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    workspace = tmp_path / "task-worktree"
    child = workspace / "repo" / "src"
    child.mkdir(parents=True)

    assert sandbox._workspace_basename(workspace) == "task-worktree"
    assert (
        sandbox._sandbox_path_for_workspace_child(workspace, "/sandbox/task", str(child))
        == "/sandbox/task/repo/src"
    )
    assert (
        sandbox._sandbox_path_for_workspace_child(
            workspace, "/sandbox/task", str(tmp_path / "outside")
        )
        is None
    )
