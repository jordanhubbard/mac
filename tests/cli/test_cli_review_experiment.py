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


def test_review_experiment_cli_lifecycle(tmp_path):
    rc, task = _run(
        tmp_path,
        "task",
        "create",
        "review experiment CLI task",
        "--project",
        "demo",
        "--no-dispatch",
    )
    assert rc == 0

    rc, assignment = _run(
        tmp_path,
        "review",
        "experiment",
        "assign",
        task["id"],
        "cli-exp",
        "--arm",
        "standard",
    )
    assert rc == 0
    assert assignment["arm"] == "standard"

    rc, outcome = _run(
        tmp_path,
        "review",
        "experiment",
        "outcome",
        task["id"],
        "clean_window",
        "confirmed",
        "--detail",
        '{"window_days": 0}',
    )
    assert rc == 0
    assert outcome["status"] == "confirmed"

    rc, observation = _run(
        tmp_path, "review", "experiment", "observe", task["id"]
    )
    assert rc == 0
    assert observation["experiment"]["experiment_id"] == "cli-exp"

    rc, report = _run(
        tmp_path,
        "review",
        "experiment",
        "report",
        "cli-exp",
        "--project",
        "demo",
    )
    assert rc == 0
    assert report["policy"]["status"] == "insufficient_evidence"

