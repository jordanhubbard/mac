"""`mac task select`, `mac task batch`, and `mac task group` end to end.

These are the operator's side of task groups: the inbox is only tractable if
answering forty parked tasks costs one command rather than forty. The tests
below drive the real CLI against a real database, because the safety of this
feature lives in defaults -- dry-run, refusal to guess, refusal to act on a
group that moved -- and a default is only real if the command line has it.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import dsn_for, store_on


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue().strip(), err.getvalue().strip()


def _json(tmp_path, *args):
    rc, raw, err = _run(tmp_path, *args)
    assert rc == 0, err or raw
    return json.loads(raw) if raw else None


def _park(tmp_path, count, project="mac"):
    """Create parked tasks through the control plane behind the same DSN."""
    from mac.services import ControlPlane

    cp = ControlPlane(store_on(dsn_for(tmp_path)), secret_key="k" * 32)
    for index in range(count):
        task = cp.create_task("parked %d" % index, project=project)
        cp.request_task_input(task.id, [{"question": "which database?"}], "worker-1")
    return cp


# --- select ---------------------------------------------------------------


def test_task_select_previews_the_group(tmp_path):
    _park(tmp_path, 3, project="mac")
    _park(tmp_path, 2, project="other")

    result = _json(tmp_path, "task", "select", "state=needs_input project=mac")

    assert result["matched"] == 3
    assert result["token"]
    assert len(result["tasks"]) == 3
    # The outstanding question travels with the preview, because that is what
    # the operator is about to answer.
    assert result["tasks"][0]["questions"] == ["which database?"]


def test_task_select_refuses_a_selector_it_cannot_trust(tmp_path):
    _park(tmp_path, 1)
    rc, _out, err = _run(tmp_path, "task", "select", "colour=blue")

    assert rc == 1
    # A malformed selector is operator input, not a crash: the message names
    # the bad key and the valid ones, with no traceback.
    assert "unknown selector key 'colour'" in err
    assert "Traceback" not in err


# --- batch ----------------------------------------------------------------


def test_task_batch_is_a_dry_run_without_apply(tmp_path):
    """The default that matters most: forgetting a flag must not mutate."""
    _park(tmp_path, 3)

    result = _json(
        tmp_path, "task", "batch", "answer", "state=needs_input", "--answer", "postgres"
    )

    assert result["applied"] is False
    assert result["changed_count"] == 3
    assert _json(tmp_path, "task", "select", "state=needs_input")["matched"] == 3


def test_task_batch_applies_and_scopes(tmp_path):
    _park(tmp_path, 3, project="mac")
    _park(tmp_path, 2, project="other")

    result = _json(
        tmp_path, "task", "batch", "answer", "state=needs_input project=mac",
        "--answer", "postgres", "--apply",
    )

    assert result["applied"] is True
    assert result["changed_count"] == 3
    assert _json(tmp_path, "task", "select", "state=needs_input project=mac")["matched"] == 0
    assert _json(tmp_path, "task", "select", "state=needs_input project=other")["matched"] == 2


def test_task_batch_refuses_a_group_that_moved(tmp_path):
    _park(tmp_path, 2)
    previewed = _json(tmp_path, "task", "select", "state=needs_input")
    _park(tmp_path, 1)  # the group grew after the preview

    rc, _out, err = _run(
        tmp_path, "task", "batch", "answer", "state=needs_input",
        "--answer", "x", "--apply", "--expect-token", previewed["token"],
    )

    assert rc == 1
    assert "no longer the one previewed" in err
    assert _json(tmp_path, "task", "select", "state=needs_input")["matched"] == 3


def test_task_batch_reports_a_forgotten_option_before_touching_anything(tmp_path):
    _park(tmp_path, 2)
    rc, _out, err = _run(tmp_path, "task", "batch", "answer", "state=needs_input")

    assert rc == 1
    assert "answer requires the answer text" in err


def test_task_batch_merges_metadata_rather_than_replacing_it(tmp_path):
    cp = _park(tmp_path, 2)
    _json(
        tmp_path, "task", "batch", "set", "state=needs_input",
        "--metadata-merge", json.dumps({"triaged": "yes"}), "--apply",
    )

    for task in cp.list_tasks("needs_input"):
        metadata = cp.get_task(task.id).metadata
        assert metadata["triaged"] == "yes"
        assert "needs_input" in metadata, "the parked question was destroyed"


# --- group ----------------------------------------------------------------


def test_task_group_round_trip(tmp_path):
    _park(tmp_path, 3, project="mac")

    saved = _json(
        tmp_path, "task", "group", "save", "parked-mac",
        "state=needs_input project=mac", "--description", "the mac inbox",
    )
    assert saved["name"] == "parked-mac"

    listed = _json(tmp_path, "task", "group", "list")
    assert [row["name"] for row in listed] == ["parked-mac"]

    shown = _json(tmp_path, "task", "group", "show", "parked-mac")
    assert shown["expression"] == "state=needs_input project=mac"

    # The saved group is usable as a selector term, and refinable in place.
    assert _json(tmp_path, "task", "select", "group=parked-mac")["matched"] == 3
    assert _json(tmp_path, "task", "select", "group=parked-mac priority>=5")["matched"] == 0

    _json(tmp_path, "task", "group", "delete", "parked-mac")
    assert _json(tmp_path, "task", "group", "list") == []


def test_task_group_refuses_an_expression_that_will_not_resolve(tmp_path):
    """Failing at save time beats failing when a batch runs against it."""
    rc, _out, err = _run(
        tmp_path, "task", "group", "save", "broken", "group=nonexistent"
    )

    assert rc == 1
    assert "unknown task group 'nonexistent'" in err
