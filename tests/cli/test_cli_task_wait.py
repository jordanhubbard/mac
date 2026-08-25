"""`mac task wait` end to end.

The pure logic is covered in tests/test_task_wait.py. These exist because that
proves nothing about whether the command reads the right tasks, scopes them to
the project, or ever returns -- and a wait that does not return is the one bug
that cannot be worked around by whoever hit it.

The blocked / needs_input departure rules are covered in tests/test_task_wait.py
against the state machine rather than here: neither state is reachable through
an operator API by design (needs_input is fenced to an active worker lease,
blocked is set by dependency supervision), so reproducing them at CLI level
would mean manufacturing a state the control plane deliberately guards.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

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
    return rc, out.getvalue()


def _lines(raw):
    return [json.loads(line) for line in raw.strip().splitlines() if line.strip()]


def test_a_project_with_nothing_running_returns_immediately(tmp_path):
    """The degenerate case has to be instant, or every script that calls this
    defensively pays for it."""
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)

    rc, raw = _run(tmp_path, "task", "wait", "--project", "mac", "--timeout", "5")

    assert rc in (None, 0)
    assert _lines(raw)[-1]["done"] is True


def test_a_finished_project_reports_nothing_pending(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)
    task = cp.create_task("done already", project="mac")
    cp.close_task(task.id, "cancelled", "operator", {"reason": "not needed"})

    _rc, raw = _run(tmp_path, "task", "wait", "--project", "mac", "--timeout", "5")

    assert _lines(raw)[-1]["still_pending"] == []


def test_a_still_running_task_holds_the_wait_open_until_the_timeout(tmp_path):
    """The property that matters most: it must NOT return while work it is
    watching is still running."""
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)
    cp.create_task("still going", project="mac")

    with pytest.raises(SystemExit) as excinfo:
        _run(
            tmp_path, "task", "wait", "--project", "mac", "--timeout", "2", "--poll-interval", "0.2"
        )

    assert excinfo.value.code == 1


def test_the_initial_feed_names_what_is_being_waited_on(tmp_path):
    """So the caller can tell immediately whether the wait is watching what
    they meant."""
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)
    task = cp.create_task("open work", project="mac")

    with pytest.raises(SystemExit):
        _run(
            tmp_path, "task", "wait", "--project", "mac", "--timeout", "1", "--poll-interval", "0.2"
        )

    # The feed is written before the timeout, so re-run capturing it.
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        main(
            [
                "--db",
                dsn_for(tmp_path),
                "--json",
                "task",
                "wait",
                "--project",
                "mac",
                "--timeout",
                "1",
                "--poll-interval",
                "0.2",
            ]
        )
    except SystemExit:
        pass
    finally:
        sys.stdout = old
    first = _lines(out.getvalue())[0]
    assert first["task_id"] == task.id
    assert first["event"] == "waiting"


def test_waiting_without_a_project_is_refused(tmp_path):
    """Waiting on the whole ledger would never return."""
    rc, raw = _run(tmp_path, "task", "wait", "--project", "")
    assert rc not in (None, 0) or "project" in raw.lower()
