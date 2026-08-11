"""Backfilling a ledger that predates recorded filers.

A private agent runs only its owner's tasks, so tasks with no filer can never
run on one. With 7,984 such tasks on the live hub, marking a worker private
takes it out of service entirely -- which is exactly what happened.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _seed(tmp_path, count=3):
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)
    human = cp.register_human(username="jordanh")
    for index in range(count):
        cp.create_task("historic %d" % index, project="mac")
    return cp, human


def test_unowned_tasks_are_reassigned(tmp_path):
    cp, human = _seed(tmp_path)

    rc, out = _run(tmp_path, "task", "reassign", "--human", "jordanh")

    assert rc in (None, 0)
    assert out["reassigned"] == 3
    assert all(t.created_by_human == human.id for t in cp.list_tasks(None, project="mac"))


def test_a_dry_run_changes_nothing(tmp_path):
    """A bulk re-file over thousands of records is worth previewing."""
    cp, _human = _seed(tmp_path)

    rc, out = _run(tmp_path, "task", "reassign", "--human", "jordanh", "--dry-run")

    assert rc in (None, 0)
    assert out["would_reassign"] == 3
    assert all(t.created_by_human is None for t in cp.list_tasks(None, project="mac"))


def test_a_task_already_filed_by_someone_else_is_left_alone(tmp_path):
    """Overwriting a recorded filer is a different and less reversible act."""
    cp, _human = _seed(tmp_path, count=1)
    other = cp.register_human(username="someone-else")
    owned = cp.create_task("theirs", project="mac", created_by_human=other.id)

    _rc, out = _run(tmp_path, "task", "reassign", "--human", "jordanh")

    assert out["reassigned"] == 1
    assert cp.get_task(owned.id).created_by_human == other.id


def test_overwrite_refiles_everything(tmp_path):
    cp, human = _seed(tmp_path, count=1)
    other = cp.register_human(username="someone-else")
    owned = cp.create_task("theirs", project="mac", created_by_human=other.id)

    _rc, out = _run(tmp_path, "task", "reassign", "--human", "jordanh", "--overwrite")

    assert out["reassigned"] == 2
    assert cp.get_task(owned.id).created_by_human == human.id


def test_rerunning_it_reassigns_nothing(tmp_path):
    """Idempotent, so a backfill interrupted halfway can simply be run again."""
    _cp, _human = _seed(tmp_path)
    _run(tmp_path, "task", "reassign", "--human", "jordanh")

    _rc, out = _run(tmp_path, "task", "reassign", "--human", "jordanh")

    assert out["reassigned"] == 0
