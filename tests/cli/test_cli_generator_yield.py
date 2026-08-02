"""`mac task generator-yield` shows each origin's record and gate standing.

This is the reporting half of the yield rule: a generator that cannot show
its own yield should not be allowed to file, so the yield has to be visible
from the CLI rather than only inside the gate.
"""
from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.models import TaskState
from mac.test_support import dsn_for


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


def test_generator_yield_reports_an_empty_ledger(tmp_path):
    rc, report = _run(tmp_path, "task", "generator-yield")
    assert rc == 0
    assert report["schema"] == "mac.generator_yield.v1"
    assert report["origins"] == []
    # The policy in force is part of the report: a reader must be able to see
    # what threshold a verdict was made against.
    assert report["floor"] > 0
    assert report["min_sample"] >= 1


def test_generator_yield_separates_generators_from_humans(tmp_path):
    dsn = dsn_for(tmp_path)

    from mac.services import ControlPlane
    from mac.store import open_postgres_store

    cp = ControlPlane(open_postgres_store(dsn, initialize_schema=True))
    made = []
    for index in range(6):
        made.append(
            cp.create_task(
                "generated %d" % index,
                project="mac",
                metadata={"origin": {"type": "self_heal"}},
            )
        )
    cp.create_task(
        "a human asked for this",
        project="mac",
        metadata={"origin": {"type": "direct_task"}},
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, made[0].id),
    )

    rc, report = _run(tmp_path, "task", "generator-yield")
    assert rc == 0
    by_origin = {row["origin_type"]: row for row in report["origins"]}

    assert by_origin["self_heal"]["generator"] is True
    assert by_origin["self_heal"]["filed"] == 6
    assert by_origin["self_heal"]["completed"] == 1

    assert by_origin["direct_task"]["generator"] is False
    assert by_origin["direct_task"]["allowed"] is True
    assert by_origin["direct_task"]["reason"] == "not_a_generator"
