"""Stopping the fleet is one operation, and one command confirms it.

Before these commands existed the authority to stop the fleet was complete and
the vocabulary was not: an operator had to snapshot every agent's hold by hand,
iterate ``mac agent hold``, discover that a hold does not drain, chase the
still-executing tasks one at a time, and then reconstruct "is it stopped?" from
``mac agent list --json``. Every one of those steps had to be composed
correctly under pressure.

What is asserted here is the vocabulary, not the plumbing underneath it:

* one command stops the fleet, and one command says whether it is stopped;
* drain is a different thing from hold, and reports what it is waiting on;
* re-enabling restores the PRE-STOP state -- an agent quarantined before the
  stop is still quarantined after the start;
* the two commands that used to exit 0 without doing anything now either take
  effect or fail.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac import fleet_control
from mac.cli import _set_output_json, main
from mac.test_support import dsn_for


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run `mac --json --db <tmp> <args>`; return (rc, parsed stdout)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    if not raw:
        return rc, None
    try:
        return rc, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return rc, raw


def _run_text(tmp_path, *args):
    """Same, without --json, so the human rendering is exercised too.

    ``--json`` is a module-level flag that `main` only ever turns ON, so a
    previous `_run` in this process would otherwise leak into the text mode
    this is here to check.
    """
    _set_output_json(False)
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def _agent(tmp_path, name):
    rc, machine = _run(tmp_path, "admin", "machine", "register", name + "-host")
    assert rc == 0, machine
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], name)
    assert rc == 0, agent
    return agent


def _task(tmp_path, title):
    rc, task = _run(tmp_path, "task", "create", title)
    assert rc == 0, task
    return task


def _by_id(rows):
    return {row["id"]: row for row in rows}


# ---------------------------------------------------------------------------
# fleet status: the answer to "is the fleet stopped?"
# ---------------------------------------------------------------------------


def test_status_reports_running_while_any_agent_can_be_dispatched(tmp_path):
    _agent(tmp_path, "status-running-a")
    _agent(tmp_path, "status-running-b")

    rc, status = _run(tmp_path, "admin", "fleet", "status")

    assert rc == 0
    assert status["state"] == "running"
    assert status["agents"] == {
        "total": 2,
        "held": 0,
        "held_by_fleet_stop": 0,
        "dispatchable": 2,
    }


def test_a_partial_hold_is_not_a_stop(tmp_path):
    """One agent left dispatchable means the fleet is still running.

    Reporting "stopped" here is the failure that matters: the operator
    concludes no new work can start and walks away while one agent keeps
    claiming tasks.
    """
    _agent(tmp_path, "partial-a")
    held = _agent(tmp_path, "partial-b")
    assert _run(tmp_path, "agent", "hold", held["id"], "--reason", "one only")[0] == 0

    rc, status = _run(tmp_path, "admin", "fleet", "status")

    assert rc == 0
    assert status["state"] == "running"
    assert status["agents"]["dispatchable"] == 1


def test_status_text_output_is_one_line_plus_what_is_executing(tmp_path):
    _agent(tmp_path, "status-text")

    rc, text = _run_text(tmp_path, "admin", "fleet", "status")

    assert rc == 0
    assert text.splitlines()[0].startswith("running: 1/1 agents dispatchable")


# ---------------------------------------------------------------------------
# fleet stop: one command
# ---------------------------------------------------------------------------


def test_stop_holds_every_agent_and_status_confirms_it(tmp_path):
    first = _agent(tmp_path, "stop-all-a")
    second = _agent(tmp_path, "stop-all-b")

    rc, stopped = _run(tmp_path, "admin", "fleet", "stop", "--reason", "power work")

    assert rc == 0
    assert {item["agent_id"] for item in stopped["held"]} == {first["id"], second["id"]}
    assert stopped["state"] == "stopped"

    rc, agents = _run(tmp_path, "agent", "list")
    assert rc == 0
    for row in agents:
        assert row["dispatch_hold"] is True
        assert row["dispatch_hold_reason"] == "fleet-stop: power work"

    rc, status = _run(tmp_path, "admin", "fleet", "status")
    assert rc == 0
    assert status["state"] == "stopped"
    assert status["agents"]["held_by_fleet_stop"] == 2


def test_stop_records_the_reason_on_every_hold(tmp_path):
    _agent(tmp_path, "stop-reason")

    rc, stopped = _run(
        tmp_path, "admin", "fleet", "stop", "--reason", "hub migration window"
    )

    assert rc == 0
    assert stopped["reason"] == "hub migration window"
    assert fleet_control.is_fleet_stop_reason(stopped["hold_reason"])
    assert "hub migration window" in stopped["hold_reason"]


def test_stop_snapshots_the_pre_stop_state(tmp_path):
    """The snapshot answers "what did this look like before I touched it?"."""
    quarantined = _agent(tmp_path, "snapshot-held")
    free = _agent(tmp_path, "snapshot-free")
    assert _run(tmp_path, "agent", "hold", quarantined["id"], "--reason", "bad disk")[0] == 0

    rc, stopped = _run(tmp_path, "admin", "fleet", "stop")

    assert rc == 0
    snapshot = {row["agent_id"]: row for row in stopped["snapshot"]["agents"]}
    assert snapshot[quarantined["id"]]["dispatch_hold"] is True
    assert snapshot[quarantined["id"]]["dispatch_hold_reason"] == "bad disk"
    assert snapshot[free["id"]]["dispatch_hold"] is False


def test_stop_does_not_overwrite_an_existing_hold_reason(tmp_path):
    """Overwriting it would destroy the only record of why that agent was held."""
    quarantined = _agent(tmp_path, "keep-reason")
    assert _run(tmp_path, "agent", "hold", quarantined["id"], "--reason", "bad disk")[0] == 0

    rc, stopped = _run(tmp_path, "admin", "fleet", "stop", "--reason", "unrelated")

    assert rc == 0
    assert [item["agent_id"] for item in stopped["already_held"]] == [quarantined["id"]]
    assert stopped["already_held"][0]["reason"] == "bad disk"
    assert stopped["already_held"][0]["by_fleet_stop"] is False

    rc, agent = _run(tmp_path, "agent", "show", quarantined["id"])
    assert rc == 0
    assert agent["dispatch_hold_reason"] == "bad disk"


def test_stop_is_idempotent(tmp_path):
    _agent(tmp_path, "idempotent-stop")
    assert _run(tmp_path, "admin", "fleet", "stop")[0] == 0

    rc, again = _run(tmp_path, "admin", "fleet", "stop")

    assert rc == 0
    assert again["held"] == []
    assert again["already_held"][0]["by_fleet_stop"] is True
    assert again["state"] == "stopped"


# ---------------------------------------------------------------------------
# drain: distinguishable from hold, and it says what it waits on
# ---------------------------------------------------------------------------


def test_hold_alone_does_not_drain(tmp_path):
    """A stop without --drain returns while an agent is still executing.

    This is the discovery that cost an operator two surprised agents. It is
    not a bug -- it is what a hold means -- so the command reports `draining`
    rather than `stopped` and does not pretend otherwise.
    """
    agent = _agent(tmp_path, "drain-none")
    task = _task(tmp_path, "work in flight")
    assert _run(tmp_path, "task", "claim", task["id"], agent["id"])[0] == 0

    rc, stopped = _run(tmp_path, "admin", "fleet", "stop")

    assert rc == 0
    assert stopped["state"] == "draining"
    assert stopped["drain"] is None
    assert [item["task_id"] for item in stopped["in_flight"]] == [task["id"]]


def test_status_distinguishes_draining_from_stopped(tmp_path):
    agent = _agent(tmp_path, "drain-status")
    task = _task(tmp_path, "still running")
    assert _run(tmp_path, "task", "claim", task["id"], agent["id"])[0] == 0
    assert _run(tmp_path, "admin", "fleet", "stop")[0] == 0

    rc, status = _run(tmp_path, "admin", "fleet", "status")

    assert rc == 0
    assert status["state"] == "draining"
    assert status["in_flight_count"] == 1
    assert status["in_flight"][0]["agent_name"] == "drain-status"


def test_drain_that_times_out_fails_and_names_what_it_waited_on(tmp_path):
    """A drain that gives up must not exit 0: the fleet is not stopped."""
    agent = _agent(tmp_path, "drain-timeout")
    task = _task(tmp_path, "never finishes")
    assert _run(tmp_path, "task", "claim", task["id"], agent["id"])[0] == 0

    rc, stopped = _run(
        tmp_path,
        "admin", "fleet", "stop",
        "--drain",
        "--timeout", "0",
        "--poll-interval", "0",
    )

    assert rc == 1
    assert stopped["drain"]["complete"] is False
    assert [item["task_id"] for item in stopped["drain"]["waiting_on"]] == [task["id"]]
    assert stopped["state"] == "draining"


def test_drain_returns_when_the_fleet_is_already_quiescent(tmp_path):
    _agent(tmp_path, "drain-quiet")

    rc, stopped = _run(
        tmp_path, "admin", "fleet", "stop", "--drain", "--timeout", "5"
    )

    assert rc == 0
    assert stopped["drain"]["complete"] is True
    assert stopped["drain"]["waiting_on"] == []
    assert stopped["state"] == "stopped"


def test_drain_finishes_once_the_work_does(tmp_path):
    """The drain loop is driven against a plane whose work completes mid-wait.

    Exercised through a stub rather than a real agent because the point is the
    loop -- poll, report, poll again, return -- and a real executor finishing a
    task on a timer would test the executor.
    """
    class _Plane:
        def __init__(self):
            self.polls = 0

        def list_agents(self):
            return [{"id": "a1", "name": "worker", "dispatch_hold": True}]

        def list_tasks(self, state=None):
            if state != "running":
                return []
            self.polls += 1
            return [] if self.polls > 4 else [{"id": "t1", "state": "running"}]

        def set_agent_dispatch_hold(self, agent_id, reason):  # pragma: no cover - held
            raise AssertionError("already held")

    slept = []
    result = fleet_control.fleet_stop(
        _Plane(),
        reason="stub",
        drain=True,
        timeout_seconds=60,
        poll_seconds=1,
        sleep=slept.append,
        monotonic=lambda: float(len(slept)),
    )

    assert result["drain"]["complete"] is True
    assert result["drain"]["polls"] > 1
    assert result["state"] == "stopped"


def test_drain_never_sleeps_past_its_deadline(tmp_path):
    """A 30s poll interval must not turn a 5s timeout into a 30s one."""
    class _Plane:
        def list_agents(self):
            return [{"id": "a1", "name": "worker", "dispatch_hold": True}]

        def list_tasks(self, state=None):
            return [{"id": "t1", "state": "running"}] if state == "running" else []

    slept = []
    result = fleet_control.fleet_stop(
        _Plane(),
        reason="stub",
        drain=True,
        timeout_seconds=5,
        poll_seconds=30,
        sleep=slept.append,
        monotonic=lambda: sum(slept),
    )

    assert result["drain"]["complete"] is False
    assert sum(slept) <= 5


# ---------------------------------------------------------------------------
# fleet start: restore the pre-stop state, not a uniform state
# ---------------------------------------------------------------------------


def test_start_releases_the_stop_and_leaves_a_prior_quarantine_held(tmp_path):
    """The whole point of restoring rather than resetting.

    Releasing everything would quietly put a machine somebody quarantined for
    a bad disk back into rotation.
    """
    quarantined = _agent(tmp_path, "restore-held")
    ordinary = _agent(tmp_path, "restore-free")
    assert _run(tmp_path, "agent", "hold", quarantined["id"], "--reason", "bad disk")[0] == 0
    assert _run(tmp_path, "admin", "fleet", "stop", "--reason", "maintenance")[0] == 0

    rc, started = _run(tmp_path, "admin", "fleet", "start")

    assert rc == 0
    assert [item["agent_id"] for item in started["released"]] == [ordinary["id"]]
    assert [item["agent_id"] for item in started["kept_held"]] == [quarantined["id"]]

    agents = _by_id(_run(tmp_path, "agent", "list")[1])
    assert agents[ordinary["id"]]["dispatch_hold"] is False
    assert agents[quarantined["id"]]["dispatch_hold"] is True
    assert agents[quarantined["id"]]["dispatch_hold_reason"] == "bad disk"


def test_start_release_all_is_the_other_operation_and_must_be_named(tmp_path):
    quarantined = _agent(tmp_path, "release-all-held")
    _agent(tmp_path, "release-all-free")
    assert _run(tmp_path, "agent", "hold", quarantined["id"], "--reason", "bad disk")[0] == 0
    assert _run(tmp_path, "admin", "fleet", "stop")[0] == 0

    rc, started = _run(tmp_path, "admin", "fleet", "start", "--release-all")

    assert rc == 0
    assert started["kept_held"] == []
    assert len(started["released"]) == 2
    for row in _run(tmp_path, "agent", "list")[1]:
        assert row["dispatch_hold"] is False


def test_stop_then_start_round_trip_returns_to_running(tmp_path):
    _agent(tmp_path, "roundtrip-a")
    _agent(tmp_path, "roundtrip-b")

    assert _run(tmp_path, "admin", "fleet", "stop", "--reason", "round trip")[0] == 0
    assert _run(tmp_path, "admin", "fleet", "status")[1]["state"] == "stopped"

    rc, started = _run(tmp_path, "admin", "fleet", "start")

    assert rc == 0
    assert started["state"] == "running"
    assert _run(tmp_path, "admin", "fleet", "status")[1]["state"] == "running"


def test_start_on_a_running_fleet_changes_nothing(tmp_path):
    _agent(tmp_path, "start-noop")

    rc, started = _run(tmp_path, "admin", "fleet", "start")

    assert rc == 0
    assert started["released"] == []
    assert started["state"] == "running"


# ---------------------------------------------------------------------------
# the two silent no-ops
# ---------------------------------------------------------------------------


def test_task_update_dependencies_takes_effect(tmp_path):
    """It used to reach argparse as an unrecognized argument.

    The damage was real: a task was dispatched and run WITHOUT the dependency
    that was supposed to hold it behind a migration.
    """
    blocker = _task(tmp_path, "must land first")
    dependent = _task(tmp_path, "must wait")

    rc, updated = _run(
        tmp_path, "task", "update", dependent["id"], "--dependencies", blocker["id"]
    )

    assert rc == 0
    assert updated["dependencies"] == [blocker["id"]]
    assert updated["state"] == "waiting"

    rc, reread = _run(tmp_path, "task", "show", dependent["id"])
    assert rc == 0
    assert reread["task"]["dependencies"] == [blocker["id"]]


def test_task_update_dependencies_accepts_several_and_can_clear_them(tmp_path):
    first = _task(tmp_path, "dep one")
    second = _task(tmp_path, "dep two")
    dependent = _task(tmp_path, "waits on both")

    rc, updated = _run(
        tmp_path,
        "task", "update", dependent["id"],
        "--dependencies", "%s,%s" % (first["id"], second["id"]),
    )
    assert rc == 0
    assert sorted(updated["dependencies"]) == sorted([first["id"], second["id"]])

    rc, cleared = _run(tmp_path, "task", "update", dependent["id"], "--dependencies", "")

    assert rc == 0
    assert cleared["dependencies"] == []
    assert cleared["state"] == "open"


def test_task_update_dependencies_rejects_a_task_depending_on_itself(tmp_path):
    task = _task(tmp_path, "self dependency")

    rc, _ = _run(tmp_path, "task", "update", task["id"], "--dependencies", task["id"])

    assert rc != 0


def test_project_pause_is_visible_in_the_status_anybody_reads_back(tmp_path):
    """`project pause` exited 0 and `project show` still said `active`.

    The pause was real, buried in metadata. Nothing the operator looked at
    said so, which is indistinguishable from a no-op.
    """
    rc, project = _run(tmp_path, "project", "create", "pause-visible")
    assert rc == 0, project

    rc, paused = _run(tmp_path, "project", "pause", "pause-visible")

    assert rc == 0
    assert paused["status"] == "paused"
    assert paused["metadata"]["dispatch_paused"] is True

    rc, shown = _run(tmp_path, "project", "show", "pause-visible")
    assert rc == 0
    # Both the operator-facing summary (what `project list` renders) and the
    # stored record. The summary is the one that used to say `active`.
    assert shown["summary"]["status"] == "paused"
    assert shown["record"]["status"] == "paused"


def test_project_activate_restores_active(tmp_path):
    assert _run(tmp_path, "project", "create", "pause-restore")[0] == 0
    assert _run(tmp_path, "project", "pause", "pause-restore")[0] == 0

    rc, activated = _run(tmp_path, "project", "activate", "pause-restore")

    assert rc == 0
    assert activated["status"] == "active"
    assert activated["metadata"]["dispatch_paused"] is False


def test_project_activate_refuses_an_archived_project_rather_than_pretending(tmp_path):
    """Clearing dispatch_paused on an archived project would not dispatch it.

    Reporting success anyway is the same silent no-op in the other direction.
    """
    assert _run(tmp_path, "project", "create", "pause-archived")[0] == 0
    assert _run(
        tmp_path, "project", "update", "pause-archived", "--status", "archived"
    )[0] == 0

    rc, _ = _run(tmp_path, "project", "activate", "pause-archived")

    assert rc != 0
    shown = _run(tmp_path, "project", "show", "pause-archived")[1]
    assert shown["record"]["status"] == "archived"


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dispatchable,in_flight,expected",
    [
        (3, 0, "running"),
        (1, 4, "running"),
        (0, 2, "draining"),
        (0, 0, "stopped"),
    ],
)
def test_classify_state(dispatchable, in_flight, expected):
    assert fleet_control.classify_state(dispatchable, in_flight) == expected


def test_a_hold_reason_round_trips_through_its_marker():
    reason = fleet_control.fleet_stop_reason("scheduled maintenance")

    assert fleet_control.is_fleet_stop_reason(reason)
    assert not fleet_control.is_fleet_stop_reason("bad disk")
    assert not fleet_control.is_fleet_stop_reason(None)
    # A hand-written reason that merely mentions the words is not a marker.
    assert not fleet_control.is_fleet_stop_reason("held during the fleet-stop window")


def test_an_empty_stop_reason_still_produces_a_recognizable_marker():
    assert fleet_control.is_fleet_stop_reason(fleet_control.fleet_stop_reason(""))
