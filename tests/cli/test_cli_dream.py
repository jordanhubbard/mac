"""Behavioral tests for the ``mac dream`` CLI."""

from __future__ import annotations

import io
import json
import sys

from mac.test_support import dsn_for
from mac.cli import main


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _seed_learning(tmp_path, count: int = 6) -> None:
    """Put raw learning records in the ledger for a dream to curate."""
    for index in range(count):
        outcome = "success" if index % 2 else "failure"
        _run(
            tmp_path,
            "admin",
            "memory",
            "add",
            "--subject-type",
            "project",
            "--subject-id",
            "demo",
            "--record-type",
            "deployment_learning:demo",
            "--content",
            json.dumps(
                {
                    "schema": "mac.deployment_learning.v1",
                    "outcome": outcome,
                    "evidence_type": "repo_change",
                    "error_signature": "" if outcome == "success" else "check failed",
                }
            ),
            "--created-by",
            "test",
        )


def _memory_count(tmp_path) -> int:
    return len(_run(tmp_path, "admin", "memory", "list")[1] or [])


def test_dream_run_writes_a_candidate_store_not_live_memory(tmp_path):
    _seed_learning(tmp_path)
    before = _memory_count(tmp_path)

    rc, result = _run(tmp_path, "admin", "dream", "run", "--project", "demo")

    assert rc == 0
    assert result["schema"] == "mac.dream_run.v2"
    # Copy-on-write: running a dream must not touch the live store.
    assert _memory_count(tmp_path) == before


def test_dream_list_and_show_surface_gates(tmp_path):
    _seed_learning(tmp_path)
    _, run = _run(tmp_path, "admin", "dream", "run", "--project", "demo")

    rc, listed = _run(tmp_path, "admin", "dream", "list", "--limit", "5")
    assert rc == 0
    assert any(item["id"] == run["run_id"] for item in listed)

    rc, shown = _run(tmp_path, "admin", "dream", "show", run["run_id"])
    assert rc == 0
    assert shown["id"] == run["run_id"]
    assert {gate["name"] for gate in shown["gates"]} >= {
        "compression",
        "privacy",
        "provenance_coverage",
        "retrieval_quality",
    }


def test_dream_promote_does_not_grow_the_store(tmp_path):
    _seed_learning(tmp_path)
    _, run = _run(tmp_path, "admin", "dream", "run", "--project", "demo")
    assert run["state"] == "ready_for_review"
    before = _memory_count(tmp_path)

    rc, promotion = _run(tmp_path, "admin", "dream", "promote", run["run_id"])

    assert rc == 0
    assert promotion["status"] == "promoted"
    assert promotion["net_change"] <= 0
    assert _memory_count(tmp_path) <= before


def test_dream_discard_marks_the_run(tmp_path):
    _seed_learning(tmp_path)
    _, run = _run(tmp_path, "admin", "dream", "run", "--project", "demo")

    rc, result = _run(
        tmp_path, "admin", "dream", "discard", run["run_id"], "--reason", "not useful"
    )

    assert rc == 0
    assert result["status"] == "discarded"
    assert _run(tmp_path, "admin", "dream", "show", run["run_id"])[1]["state"] == "discarded"


def test_dream_import_logs_empty_dry_run_is_safe_and_observable(tmp_path):
    dream_logs = tmp_path / "dream_logs"
    dream_logs.mkdir()

    rc, result = _run(
        tmp_path,
        "admin",
        "dream",
        "import-logs",
        "--dream-logs-dir",
        str(dream_logs),
        "--agent-id",
        "agent_test",
        "--no-embed",
        "--dry-run",
    )

    assert rc == 0
    assert result == {
        "schema": "mac.dream_log_import.v1",
        "source_dir": str(dream_logs),
        "dry_run": True,
        "scanned": 0,
        "imported": 0,
        "skipped_duplicate": 0,
        "skipped_empty": 0,
        "embedded": 0,
        "errors": [],
        "imported_ids": [],
    }
