"""`mac task stop` / `mac task start` through the CLI (ADR 0020).

These live in tests/cli/ deliberately. The coverage gate discovers tested
subcommands by scanning THIS directory for `_run(...)` calls, so a CLI test
filed anywhere else leaves the subcommand looking untested — which is how this
one was caught.
"""
from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.models import TaskState
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


def _running(cp):
    task = cp.create_task(title="t", description="original")
    machine = cp.register_machine("h")
    agent = cp.register_agent(machine.id, "w")
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)
    return task, agent


def test_stop_parks_a_running_task(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    task, _agent = _running(cp)

    rc, _out = _run(tmp_path, "task", "stop", task.id, "--reason", "scope was wrong")

    assert rc in (None, 0)
    assert cp.get_task(task.id).state == TaskState.STOPPED.value


def test_start_without_an_agent_returns_a_stopped_task_to_the_queue(tmp_path):
    """The operator half of the pair. With an agent_id `start` is the
    executor's lease-fenced verb instead; the two cannot be confused because a
    stopped task has no edge to RUNNING."""
    cp = control_plane_on(dsn_for(tmp_path))
    task, _agent = _running(cp)
    _run(tmp_path, "task", "stop", task.id)

    rc, _out = _run(tmp_path, "task", "start", task.id)

    assert rc in (None, 0)
    assert cp.get_task(task.id).state == TaskState.OPEN.value


def test_a_stopped_task_is_not_offered_as_ready_work(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    task, _agent = _running(cp)
    _run(tmp_path, "task", "stop", task.id)

    _rc, ready = _run(tmp_path, "task", "ready")

    ids = [row.get("id") for row in (ready or [])] if isinstance(ready, list) else []
    assert task.id not in ids
