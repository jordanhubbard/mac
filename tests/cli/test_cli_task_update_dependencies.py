"""`mac task update --dependencies`.

Lives in tests/cli/ for two reasons. It drives the CLI through `main()`, so
the CLI coverage gate should see it. And tests/cli/conftest.py supplies the
MAC_SECRET_KEY fixture the CLI needs: `run-contract-tests.sh` sweeps MAC_* for
hermeticity and does not re-export MAC_SECRET_KEY, so a CLI test filed at the
top level of tests/ passes locally with the variable exported and fails under
the harness -- which is exactly how this file first went in.

Dependencies are discovered, not known at filing time. Before this flag the
only way to add an edge was cancel-and-refile, which loses the task id, its
history, its attempt count and any attached evidence -- a bad enough trade
that in practice the edge simply never got added, and tasks ran in the wrong
order. `ControlPlane.update_task` and the `TaskUpdate` API model had accepted
`dependencies` all along; only the CLI flag was missing.
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
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _task(cp, title="t", **kw):
    return cp.create_task(title=title, description="d", **kw)


def test_a_dependency_can_be_added_after_the_task_was_filed(tmp_path):
    """The gap this closes."""
    cp = control_plane_on(dsn_for(tmp_path))
    blocker = _task(cp, "blocker")
    dependent = _task(cp, "dependent")
    assert list(cp.get_task(dependent.id).dependencies) == []

    rc, _ = _run(tmp_path, "task", "update", dependent.id, "--dependencies", blocker.id)

    assert rc in (None, 0)
    assert list(cp.get_task(dependent.id).dependencies) == [blocker.id]


def test_the_task_id_and_history_survive_the_edit(tmp_path):
    """The whole point of editing in place rather than cancel-and-refile."""
    cp = control_plane_on(dsn_for(tmp_path))
    blocker = _task(cp, "blocker")
    dependent = _task(cp, "dependent")
    before = cp.get_task(dependent.id)

    _run(tmp_path, "task", "update", dependent.id, "--dependencies", blocker.id)

    after = cp.get_task(dependent.id)
    assert after.id == before.id
    assert after.title == before.title
    assert after.description == before.description
    assert after.attempt_count == before.attempt_count


def test_several_dependencies_are_comma_separated(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    a, b = _task(cp, "a"), _task(cp, "b")
    dependent = _task(cp, "dependent")

    _run(tmp_path, "task", "update", dependent.id, "--dependencies", "%s,%s" % (a.id, b.id))

    assert sorted(cp.get_task(dependent.id).dependencies) == sorted([a.id, b.id])


def test_the_flag_REPLACES_the_set_rather_than_appending(tmp_path):
    """Same semantics as --capabilities. Stated in the help, because a flag
    that appends and one that replaces are indistinguishable from a single
    successful call."""
    cp = control_plane_on(dsn_for(tmp_path))
    a, b = _task(cp, "a"), _task(cp, "b")
    dependent = _task(cp, "dependent", dependencies=[a.id])

    _run(tmp_path, "task", "update", dependent.id, "--dependencies", b.id)

    assert list(cp.get_task(dependent.id).dependencies) == [b.id]


def test_an_empty_string_clears_the_dependencies(tmp_path):
    """Distinguishable from omitting the flag, or an edge could never be
    removed -- which is half the point of editing in place."""
    cp = control_plane_on(dsn_for(tmp_path))
    blocker = _task(cp, "blocker")
    dependent = _task(cp, "dependent", dependencies=[blocker.id])

    _run(tmp_path, "task", "update", dependent.id, "--dependencies", "")

    assert list(cp.get_task(dependent.id).dependencies) == []


def test_omitting_the_flag_leaves_dependencies_alone(tmp_path):
    """`update` changes only what is supplied; a title edit must not silently
    drop the task's edges."""
    cp = control_plane_on(dsn_for(tmp_path))
    blocker = _task(cp, "blocker")
    dependent = _task(cp, "dependent", dependencies=[blocker.id])

    _run(tmp_path, "task", "update", dependent.id, "--title", "renamed")

    after = cp.get_task(dependent.id)
    assert after.title == "renamed"
    assert list(after.dependencies) == [blocker.id]


def test_a_task_cannot_depend_on_itself(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    dependent = _task(cp, "dependent")

    with pytest.raises(Exception):
        cp.update_task(dependent.id, dependencies=[dependent.id])


def test_adding_a_dependency_to_an_in_flight_task_re_evaluates_its_state(tmp_path):
    """The limitation this test used to pin has been fixed, so it now pins the
    fix instead.

    It was written to record that `update_task` replaced the dependency set
    without transitioning the task, leaving a CLAIMED task holding an unmet
    dependency -- the ledger saying blocked while the executor kept going. The
    docstring said in as many words that it documented today's behaviour and
    was not an endorsement of it.

    ADR 0020 then made `update_task` atomic on an in-flight task: it stops the
    executor, applies the edit, and restarts, and the restart evaluates
    dependencies AT THAT MOMENT. So a task edited into a blocked shape now
    comes back WAITING rather than claimable, which is the answer that
    docstring said was the open question.

    Both changes were green on their own branches and collided on main, which
    is the hazard of pinning a limitation: the pin is correct until the day
    someone fixes it, and then it is the thing that breaks. That is the
    intended failure -- far better than the fix landing silently against a
    test that still asserted the old behaviour.
    """
    cp = control_plane_on(dsn_for(tmp_path))
    blocker = _task(cp, "blocker")
    dependent = _task(cp, "dependent")
    machine = cp.register_machine("h")
    agent = cp.register_agent(machine.id, "w")
    cp.claim_task(dependent.id, agent.id)

    _run(tmp_path, "task", "update", dependent.id, "--dependencies", blocker.id)

    after = cp.get_task(dependent.id)
    assert list(after.dependencies) == [blocker.id]
    # WAITING, not claimable: the blocker is unmet at the moment of restart.
    assert after.state == "waiting"
    # And no executor is left holding the pre-edit copy.
    assert after.owner_agent_id is None
