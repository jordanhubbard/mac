"""Behavioral tests for the ``mac dream`` CLI."""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_dream_import_logs_empty_dry_run_is_safe_and_observable(tmp_path):
    dream_logs = tmp_path / "dream_logs"
    dream_logs.mkdir()

    rc, result = _run(
        tmp_path,
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
