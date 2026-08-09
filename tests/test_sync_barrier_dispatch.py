"""The barrier must hold through the real dispatch path, not just in the gate.

test_sync_execution_mode.py covers evaluate_pair against hand-built snapshots.
That is necessary and not sufficient: the gate is only reached if the snapshot
builder actually populates execution_mode and the agent's barrier state from
the ledger. A rule that is correct in a function nothing calls with real data is
the recurring failure in this codebase, so these tests go through ControlPlane.
"""

from __future__ import annotations

import pytest

from mac.allocator import AGENT_SYNC_BARRIER, EXECUTION_MODE_SYNC, evaluate_pair
from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _lifecycle(cp):
    return cp.dispatch


def _snapshot(cp, task_id):
    return cp.dispatch._v2_snapshot_task(
        cp.get_task(task_id),
        projects={record.name: record for record in cp.list_project_records()},
        agent_ids_by_name={},
    )


def test_execution_mode_survives_the_round_trip_through_the_ledger(cp):
    """Stored in metadata, read back into the allocation snapshot. If this
    fails the mode is a field nothing downstream ever sees."""
    task = cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_1"},
    )

    assert _snapshot(cp, task.id).execution_mode == EXECUTION_MODE_SYNC


def test_a_task_with_no_mode_is_async_after_the_round_trip(cp):
    task = cp.create_task("ordinary", project="mac")

    assert _snapshot(cp, task.id).execution_mode == "async"


def test_the_barrier_state_is_derived_from_unfinished_sync_tasks(cp):
    cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_1"},
    )

    head, running = _lifecycle(cp)._sync_barrier_state("agent_1")

    assert head is not None
    assert running is False


def test_a_finished_barrier_stops_quiescing_the_agent(cp):
    """Derived rather than stored precisely for this.

    A stored quiescing flag has to be cleared by whatever finishes the barrier,
    and a missed path leaves the worker quiesced forever with nothing saying
    why. Recomputing cannot get stuck.
    """
    task = cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_1"},
    )
    cp.close_task(task.id, "cancelled", "operator", {"reason": "barrier finished"})

    head, _running = _lifecycle(cp)._sync_barrier_state("agent_1")

    assert head is None


def test_a_barrier_for_another_agent_is_not_this_agent_s_barrier(cp):
    cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_2"},
    )

    head, _running = _lifecycle(cp)._sync_barrier_state("agent_1")

    assert head is None


def test_the_oldest_unfinished_barrier_is_the_head(cp):
    """FIFO is decided here, so this is where getting it backwards would show."""
    first = cp.create_task(
        "first",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_1"},
    )
    cp.create_task(
        "second",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_1"},
    )

    head, _running = _lifecycle(cp)._sync_barrier_state("agent_1")

    assert head == first.id


def test_an_ordinary_task_never_creates_a_barrier(cp):
    """The blast radius check: if async work quiesced its agent, the fleet
    would deadlock on the first task it accepted."""
    cp.create_task("ordinary", project="mac", metadata={"target_agent_id": "agent_1"})

    head, _running = _lifecycle(cp)._sync_barrier_state("agent_1")

    assert head is None


def _agent(cp, name="worker1"):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name)


def test_the_agent_snapshot_carries_the_barrier_the_allocator_reads(cp):
    """The wiring, not the helper.

    Deleting sync_queue_head_task_id from _v2_snapshot_agent left every other
    test in this file green while making the feature completely inert: the
    barrier state was computed correctly and then dropped before the allocator
    ever saw it. Asserting on _sync_barrier_state alone cannot catch that.
    """
    agent = _agent(cp)
    cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": agent.id},
    )

    snapshot = cp.dispatch._v2_snapshot_agent(cp.get_agent(agent.id))

    assert snapshot.sync_queue_head_task_id is not None
    assert snapshot.sync_barrier_pending


def test_an_agent_with_no_barrier_snapshots_clean(cp):
    agent = _agent(cp, "worker2")
    cp.create_task("ordinary", project="mac")

    snapshot = cp.dispatch._v2_snapshot_agent(cp.get_agent(agent.id))

    assert snapshot.sync_queue_head_task_id is None
    assert not snapshot.sync_barrier_pending


def test_a_quiesced_agent_refuses_async_work_end_to_end(cp):
    """The whole chain: ledger -> snapshot -> allocator gate."""
    agent = _agent(cp, "worker3")
    cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": agent.id},
    )
    ordinary = cp.create_task("ordinary", project="mac")

    evaluation = evaluate_pair(
        _snapshot(cp, ordinary.id),
        cp.dispatch._v2_snapshot_agent(cp.get_agent(agent.id)),
    )

    assert not evaluation.allowed
    assert any(
        reason.split(":", 1)[0] == AGENT_SYNC_BARRIER
        for reason in evaluation.agent_rejections
    )


# --------------------------------------------------------------------------
# Refused at the door, not only in the allocator
# --------------------------------------------------------------------------


def test_creating_an_untargeted_barrier_is_refused(cp):
    """An allocator-only refusal leaves the task sitting in the ledger looking
    ready forever. Say no while someone is still there to read it."""
    with pytest.raises(ValidationError) as excinfo:
        cp.create_task("barrier", project="mac", metadata={"execution_mode": "sync"})

    assert "target_agent_id" in str(excinfo.value)


def test_the_refusal_names_the_ambiguity(cp):
    """"Invalid task" would send someone to look at the title."""
    with pytest.raises(ValidationError) as excinfo:
        cp.create_task("barrier", project="mac", metadata={"execution_mode": "sync"})

    assert "single" in str(excinfo.value) and "fleet" in str(excinfo.value)


def test_a_targeted_barrier_is_accepted(cp):
    task = cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_id": "agent_1"},
    )

    assert task.id


def test_a_barrier_targeted_by_name_is_accepted(cp):
    """target_agent_name is resolved later by the snapshot builder, so
    refusing it here would reject a legitimate route."""
    task = cp.create_task(
        "barrier",
        project="mac",
        metadata={"execution_mode": "sync", "target_agent_name": "worker5"},
    )

    assert task.id


def test_ordinary_task_creation_is_untouched(cp):
    assert cp.create_task("ordinary", project="mac").id
