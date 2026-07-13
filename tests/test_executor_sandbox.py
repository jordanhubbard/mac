"""Ownership and compatibility tests for the executor sandbox extraction."""

from __future__ import annotations

import importlib


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
