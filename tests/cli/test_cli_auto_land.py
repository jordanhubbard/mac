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
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_review_auto_land_dry_run(tmp_path):
    """`review auto-land --dry-run` previews the plan without running the
    contract gate, spawning a reviewer, or landing anything."""
    rc, plan = _run(
        tmp_path,
        "admin", "review",
        "auto-land",
        "task_deadbeef",
        "--base-ref",
        "main",
        "--dry-run",
    )
    assert rc == 0
    assert plan["schema"] == "mac.auto_land.dry_run.v1"
    assert plan["target"] == "task_deadbeef"
    assert plan["base_ref"] == "main"
    assert plan["push"] is False
    assert "contract-gate" in plan["would_run"]
    assert "adversarial-review" in plan["would_run"]
