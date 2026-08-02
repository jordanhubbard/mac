"""CLI regression tests for `mac task release` on staged tasks carrying
controller-owned publication routing metadata (release-fix / regression_tests
node).

Mirrors the control-plane and HTTP coverage at the CLI surface: `mac task
create --no-dispatch` stages a task, the control plane attaches routing
metadata, then `mac task release` un-stages it by removing ONLY ``no_dispatch``
without touching controller-owned fields.

Cases:
1. release of a plain staged task clears ``no_dispatch``.
2. release of a staged task with routing metadata succeeds (rc == 0).
3. after release the persisted metadata differs only by removal of
   ``no_dispatch`` — routing fields byte-for-byte unchanged.
4. release of a task that is not held is a no-op.
"""

from __future__ import annotations

import io
import json
import sys

from mac.test_support import control_plane_on, dsn_for, store_on
from mac.cli import main
from mac.services import ControlPlane


ROUTING_KEYS = (
    "publication_route",
    "publication_lane",
    "managed_fast_lane",
    "work_package",
)


def _db(tmp_path):
    return dsn_for(tmp_path)


def _run(db, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(db), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    if not raw:
        return rc, None
    try:
        return rc, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return rc, raw


def _plane(db):
    return control_plane_on(db)


def _persisted_metadata(db, task_id):
    row = _plane(db).store.query_one(
        "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
    )
    return json.loads(row["metadata"])


def _create_staged(db, title="staged via cli"):
    rc, task = _run(db, "task", "create", title, "--no-dispatch")
    assert rc == 0, f"task create failed: {task}"
    assert task["metadata"].get("no_dispatch") is True
    return task


def _attach_controller_routing(db, task_id):
    cp = _plane(db)
    row = cp.store.query_one(
        "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
    )
    md = json.loads(row["metadata"])
    md["publication_route"] = {
        "schema": "mac.task_publication_route.v1",
        "lane": "managed",
        "route_state": "managed_held",
    }
    md["publication_lane"] = "managed"
    md["managed_fast_lane"] = {
        "schema": "mac.managed_single_task.route.v1",
        "activation": "legacy_compatibility",
    }
    md["work_package"] = {"id": "pkg_cli_regression", "node_type": "mutation"}
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(md), task_id),
    )


# Case 1 -------------------------------------------------------------------


def test_cli_release_plain_staged_task(tmp_path):
    db = _db(tmp_path)
    task = _create_staged(db)

    rc, released = _run(db, "task", "release", task["id"], "--actor", "operator")
    assert rc == 0, released
    assert released["metadata"].get("no_dispatch") is None

    after = _persisted_metadata(db, task["id"])
    assert "no_dispatch" not in after


# Cases 2 & 3 --------------------------------------------------------------


def test_cli_release_after_routing_attached_preserves_fields(tmp_path):
    db = _db(tmp_path)
    task = _create_staged(db)
    _attach_controller_routing(db, task["id"])

    before = _persisted_metadata(db, task["id"])
    assert before["no_dispatch"] is True

    rc, released = _run(db, "task", "release", task["id"], "--actor", "operator")
    # Case 2: the command succeeds (no ValidationError bubbling to non-zero rc).
    assert rc == 0, released
    assert released["metadata"].get("no_dispatch") is None

    after = _persisted_metadata(db, task["id"])
    assert "no_dispatch" not in after

    # Case 3: differs only by no_dispatch removal; routing fields unchanged.
    expected = dict(before)
    expected.pop("no_dispatch", None)
    assert after == expected
    for key in ROUTING_KEYS:
        assert after[key] == before[key]


# Case 4 -------------------------------------------------------------------


def test_cli_release_unheld_task_is_noop(tmp_path):
    db = _db(tmp_path)
    rc, task = _run(db, "task", "create", "not staged")
    assert rc == 0, task
    before = _persisted_metadata(db, task["id"])

    rc, released = _run(db, "task", "release", task["id"], "--actor", "operator")
    assert rc == 0, released

    after = _persisted_metadata(db, task["id"])
    assert after == before
