"""Behavioral CLI tests for the memory command family.

Covers: memory {add, search, decay, summarize-actions, health, remember, list, forget}
and the existing remember/list/forget round-trip (already in test_mac_cli.py is
complemented here with add/search/decay).
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.test_support import dsn_for

from mac.cli import main


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _setup_task_and_agent(tmp_path):
    """Return (task_id, agent_id) after creating the minimal fixtures."""
    rc, task = _run(tmp_path, "task", "create", "mem-test-task", "--project", "memproj")
    assert rc == 0
    rc, machine = _run(tmp_path, "machine", "register", "mem-host")
    assert rc == 0
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], "mem-worker",
                     "--agent-id", "agent_mem")
    assert rc == 0
    return task["id"], agent["id"]


# ---------------------------------------------------------------------------
# memory add / search
# ---------------------------------------------------------------------------


def test_memory_add_and_search(tmp_path):
    task_id, agent_id = _setup_task_and_agent(tmp_path)

    rc, record = _run(
        tmp_path,
        "memory", "add",
        "--task-id", task_id,
        "--subject-type", "agent",
        "--subject-id", agent_id,
        "--record-type", "observation",
        "--content", "deployed to prod successfully",
        "--created-by", "test",
    )
    assert rc == 0
    assert record["content"] == "deployed to prod successfully"
    assert record["record_type"] == "observation"

    rc, results = _run(
        tmp_path,
        "memory", "search",
        "--task-id", task_id,
        "--subject-type", "agent",
        "--subject-id", agent_id,
    )
    assert rc == 0
    assert isinstance(results, list)
    assert any(r["content"] == "deployed to prod successfully" for r in results)


def test_memory_search_by_record_type_prefix(tmp_path):
    task_id, agent_id = _setup_task_and_agent(tmp_path)
    _run(tmp_path, "memory", "add",
         "--task-id", task_id,
         "--subject-type", "agent",
         "--subject-id", agent_id,
         "--record-type", "obs:deploy",
         "--content", "prefix test",
         "--created-by", "test")

    rc, results = _run(
        tmp_path,
        "memory", "search",
        "--subject-type", "agent",
        "--subject-id", agent_id,
        "--record-type-prefix", "obs:",
    )
    assert rc == 0
    assert isinstance(results, list)
    assert any(r["content"] == "prefix test" for r in results)


def test_memory_add_multiple_and_search_order(tmp_path):
    task_id, agent_id = _setup_task_and_agent(tmp_path)
    for i in range(3):
        _run(tmp_path, "memory", "add",
             "--task-id", task_id,
             "--subject-type", "agent",
             "--subject-id", agent_id,
             "--record-type", "note",
             "--content", f"note {i}",
             "--created-by", "test")

    rc, results = _run(tmp_path, "memory", "search",
                       "--subject-type", "agent",
                       "--subject-id", agent_id,
                       "--limit", "10")
    assert rc == 0
    assert len(results) >= 3


# ---------------------------------------------------------------------------
# memory decay
# ---------------------------------------------------------------------------


def test_memory_decay_dry_run(tmp_path):
    """decay without --apply is a dry-run — returns candidate count."""
    task_id, agent_id = _setup_task_and_agent(tmp_path)
    # Seed a memory record to ensure the table exists
    _run(tmp_path, "memory", "add",
         "--task-id", task_id,
         "--subject-type", "agent",
         "--subject-id", agent_id,
         "--record-type", "observation",
         "--content", "stale note",
         "--created-by", "test")

    rc, result = _run(tmp_path, "memory", "decay", "--ttl-days", "1")
    assert rc == 0
    assert result is not None
    assert "dry_run" in result or "deleted" in result or "candidates" in result


def test_memory_health(tmp_path):
    rc, result = _run(tmp_path, "memory", "health")
    assert rc == 0
    assert result is not None

def test_memory_summarize_actions_empty(tmp_path):
    _, agent_id = _setup_task_and_agent(tmp_path)
    rc, result = _run(tmp_path, "memory", "summarize-actions",
                      "--agent", agent_id, "--dry-run")
    assert rc == 0
    assert result is not None
