"""ADR 0020: a running task is not edited in place.

Editing a RUNNING task produced a split brain -- the ledger held one
description, the executor worked from another read at claim time, and nothing
reconciled them. On 2026-08-20 a task's acceptance criteria were rewritten
mid-execution and correct work came within a hand-check of being judged
against criteria written after it.
"""
from __future__ import annotations

import pytest

from mac.models import (
    ACTIVE_TASK_STATES,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    TaskState,
    TransitionError,
)
from mac.test_support import control_plane_on, dsn_for


def _cp(tmp_path):
    return control_plane_on(dsn_for(tmp_path))


def _running_task(cp, title="t", **kw):
    task = cp.create_task(title=title, description="original", **kw)
    machine = cp.register_machine("h")
    agent = cp.register_agent(machine.id, "w")
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)
    return cp.get_task(task.id), agent


# --- the state itself -------------------------------------------------------

def test_stopped_is_live_work_not_terminal():
    """Stopping is not cancelling. CANCELLED must keep meaning abandoned."""
    assert TaskState.STOPPED.value not in TERMINAL_TASK_STATES
    assert TaskState.STOPPED.value in ACTIVE_TASK_STATES


def test_a_stopped_task_cannot_go_straight_back_to_running():
    """Re-entry is from the top. The state machine, not convention, enforces
    it: resuming would preserve conclusions drawn from the pre-edit task."""
    out = TASK_TRANSITIONS[TaskState.STOPPED.value]
    assert TaskState.RUNNING.value not in out
    assert TaskState.CLAIMED.value not in out
    assert TaskState.OPEN.value in out


# --- stop -------------------------------------------------------------------

def test_stop_parks_a_running_task_and_records_the_abort_as_unconfirmed(tmp_path):
    """The abort is EVENTUAL -- the worker notices when its lease is no longer
    current. "Probably stopped" must be distinguishable from "stopped"."""
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)

    stopped = cp.stop_task(task.id, actor="op", reason="scope was wrong")

    assert stopped.state == TaskState.STOPPED.value
    history = cp.task_history(task.id)
    event = next(h for h in reversed(history) if h.to_state == "stopped")
    detail = event.detail if isinstance(event.detail, dict) else {}
    assert detail["abort_confirmed"] is False
    assert detail["previous_state"] == TaskState.RUNNING.value
    assert detail["was_in_flight"] is True


def test_a_stopped_task_is_not_dispatchable(tmp_path):
    """The property that matters: nobody else may take it."""
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)
    cp.stop_task(task.id, actor="op")

    ready = [t.id for t in cp.ready_tasks()]
    assert task.id not in ready


def test_stopping_a_terminal_task_is_refused(tmp_path):
    cp = _cp(tmp_path)
    task = cp.create_task(title="t", description="d")
    cp._transition_task_internal(
        task.id, TaskState.CANCELLED.value, "op", {"reason": "x"}
    )

    with pytest.raises(TransitionError):
        cp.stop_task(task.id, actor="op")


def test_stop_is_idempotent(tmp_path):
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)
    cp.stop_task(task.id, actor="op")
    again = cp.stop_task(task.id, actor="op")
    assert again.state == TaskState.STOPPED.value


# --- start ------------------------------------------------------------------

def test_start_returns_a_stopped_task_to_the_queue(tmp_path):
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)
    cp.stop_task(task.id, actor="op")

    started = cp.start_stopped_task(task.id, actor="op")

    assert started.state == TaskState.OPEN.value


def test_start_yields_WAITING_when_the_edit_left_a_dependency_unmet(tmp_path):
    """A task edited into a blocked shape must come back blocked, not
    claimable. Dependencies are evaluated at start, not assumed."""
    cp = _cp(tmp_path)
    blocker = cp.create_task(title="blocker", description="d")
    task, _agent = _running_task(cp)

    cp.update_task(task.id, dependencies=[blocker.id], actor="op")

    after = cp.get_task(task.id)
    assert after.state == TaskState.WAITING.value


def test_starting_a_task_that_is_not_stopped_is_refused(tmp_path):
    cp = _cp(tmp_path)
    task = cp.create_task(title="t", description="d")
    with pytest.raises(TransitionError):
        cp.start_stopped_task(task.id, actor="op")


# --- the atomic update ------------------------------------------------------

def test_updating_a_running_task_applies_the_edit_and_restarts_it(tmp_path):
    """The caller asks for an update and gets an update; the stop/restart is
    below the API so no caller can get the sequence wrong or abandon it."""
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)

    cp.update_task(task.id, description="REWRITTEN CRITERIA", actor="op")

    after = cp.get_task(task.id)
    assert after.description == "REWRITTEN CRITERIA"
    assert after.state == TaskState.OPEN.value          # back in the queue
    assert after.owner_agent_id is None                 # lease revoked


def test_the_executor_is_aborted_rather_than_left_running(tmp_path):
    """The abort must be recorded, or an agent keeps building the old thing."""
    cp = _cp(tmp_path)
    task, agent = _running_task(cp)

    cp.update_task(task.id, description="new", actor="op")

    states = [h.to_state for h in cp.task_history(task.id)]
    assert TaskState.STOPPED.value in states
    assert cp.get_task(task.id).owner_agent_id != agent.id


def test_updating_a_queued_task_does_not_stop_it(tmp_path):
    """The cycle is only for in-flight work. An OPEN task is edited directly;
    stopping it would be pointless churn and would reset nothing."""
    cp = _cp(tmp_path)
    task = cp.create_task(title="t", description="d")

    cp.update_task(task.id, description="edited", actor="op")

    after = cp.get_task(task.id)
    assert after.state == TaskState.OPEN.value
    assert TaskState.STOPPED.value not in [h.to_state for h in cp.task_history(task.id)]


def test_a_claimed_task_is_also_stopped_before_editing(tmp_path):
    """CLAIMED has the same exposure: the agent holds the lease and is about
    to read the task."""
    cp = _cp(tmp_path)
    task = cp.create_task(title="t", description="d")
    machine = cp.register_machine("h")
    agent = cp.register_agent(machine.id, "w")
    cp.claim_task(task.id, agent.id)

    cp.update_task(task.id, description="edited", actor="op")

    assert TaskState.STOPPED.value in [h.to_state for h in cp.task_history(task.id)]


# --- re-entry accounting ----------------------------------------------------

def test_a_restart_does_not_consume_the_attempt_budget(tmp_path):
    """Stopping a task to correct its scope must not burn a retry: it failed
    at nothing."""
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)
    before = cp.get_task(task.id).attempt_count

    cp.update_task(task.id, description="new", actor="op")

    assert cp.get_task(task.id).attempt_count == before


def test_restarts_are_counted_separately_from_attempts(tmp_path):
    """And a restart must not RESET attempts either, or a repeatedly edited
    task could never exhaust them and would be unkillable."""
    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)

    cp.update_task(task.id, description="one", actor="op")
    after_first = cp.get_task(task.id)
    assert (after_first.metadata or {}).get("restart_count") == 1

    cp.stop_task(task.id, actor="op")
    cp.start_stopped_task(task.id, actor="op")

    assert (cp.get_task(task.id).metadata or {}).get("restart_count") == 2


def test_the_next_agent_sees_the_new_text_not_the_old(tmp_path):
    """THE TEST THAT MATTERS (ADR 0020).

    A task can finish successfully having built the wrong thing. What has to
    be true is that the work which follows a restart is derived from the NEW
    criteria -- so the task the next claimer reads must be the edited one, with
    no executor still holding the pre-edit copy.
    """
    cp = _cp(tmp_path)
    task, first_agent = _running_task(cp)
    assert cp.get_task(task.id).description == "original"

    cp.update_task(task.id, description="BUILD THE OTHER THING", actor="op")

    # Nobody holds it, and what is now claimable is the edited task.
    reclaimable = cp.get_task(task.id)
    assert reclaimable.owner_agent_id is None
    assert reclaimable.description == "BUILD THE OTHER THING"

    cp.claim_task(task.id, first_agent.id)
    assert cp.get_task(task.id).description == "BUILD THE OTHER THING"


def test_stop_and_restart_routes_work_in_hub_mode(tmp_path):
    from fastapi.testclient import TestClient
    from mac.api import create_app

    cp = _cp(tmp_path)
    task, _agent = _running_task(cp)
    client = TestClient(create_app(control_plane=cp))

    stopped = client.post(
        "/tasks/%s/stop" % task.id,
        json={"actor": "operator", "reason": "correct scope"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"

    restarted = client.post(
        "/tasks/%s/restart" % task.id,
        json={"actor": "operator"},
    )
    assert restarted.status_code == 200
    assert restarted.json()["state"] == "open"


# --- the blast radius that nearly shipped ----------------------------------

def test_a_metadata_only_update_does_not_stop_a_running_task(tmp_path):
    """Only fields that change WHAT THE AGENT BUILDS trigger the cycle.

    Gating on state alone made every internal bookkeeping write abort and
    restart the task it was recording against. An agent does not work from
    metadata, so changing it cannot produce a split brain.
    """
    cp = _cp(tmp_path)
    task, agent = _running_task(cp)

    cp.update_task(task.id, metadata={"note": "bookkeeping"}, actor="hub")

    after = cp.get_task(task.id)
    assert after.state == TaskState.RUNNING.value
    assert after.owner_agent_id == agent.id
    assert TaskState.STOPPED.value not in [h.to_state for h in cp.task_history(task.id)]


def test_a_priority_change_does_not_stop_a_running_task(tmp_path):
    """Priority is a scheduling attribute, not a work definition. The next
    claim picks it up; stopping live work to reprioritise costs more than it
    protects."""
    cp = _cp(tmp_path)
    task, agent = _running_task(cp)

    cp.update_task(task.id, priority=100, actor="op")

    after = cp.get_task(task.id)
    assert after.priority == 100
    assert after.state == TaskState.RUNNING.value
    assert after.owner_agent_id == agent.id


def test_lease_expiry_repair_does_not_revoke_the_lease_it_is_reconciling(tmp_path):
    """The regression this nearly shipped with.

    Lease expiry attaches a repair task as a DEPENDENCY of the failing task --
    a scope-bearing field on a RUNNING task. Routed through the public
    update_task it triggered the stop/restart cycle mid-expiry and revoked the
    very lease the path was reconciling, which two scheduler-safety tests
    caught by asserting the stranded task still held its lease.

    Hub-internal maintenance uses _update_task_fields. The discriminator is not
    just which fields change, but whether the writer is the hub or an operator.
    """
    cp = _cp(tmp_path)
    blocker = cp.create_task(title="repair", description="d")
    task, agent = _running_task(cp)
    lease_before = cp.get_task(task.id).lease_id
    assert lease_before

    cp._update_task_fields(
        task.id, dependencies=[blocker.id], actor="dispatcher.tick"
    )

    after = cp.get_task(task.id)
    assert after.lease_id == lease_before
    assert after.state == TaskState.RUNNING.value
    assert list(after.dependencies) == [blocker.id]
