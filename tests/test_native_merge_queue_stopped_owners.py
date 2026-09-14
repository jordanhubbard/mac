"""Stopped owners must not hold up independently approved successors."""

import pytest

from mac.models import TaskState
from mac.native_merge_queue import NativeMergeQueue, WindowBounds
from mac.services import ControlPlane

REPO = "https://github.invalid/acme/widgets.git"


@pytest.fixture
def cohort():
    cp = ControlPlane.in_memory()
    front = cp.create_task("front", metadata={"retained_ref": "refs/heads/front"})
    behind = cp.create_task("behind")
    queue = NativeMergeQueue(cp.store, bounds=WindowBounds(floor=2, ceiling=4))
    return cp, queue, front, behind


def claim(queue, task, head, owner):
    return queue.claim_slot(
        repository=REPO, branch="main", task_id=task.id, head_sha=head * 40, owner=owner
    ).entry


def record_tested(queue, entry, owner):
    assert queue.record_tested(
        entry.id,
        owner=owner,
        base_sha="A" * 40,
        base_tree="old-tip",
        merge_tree="old-projection",
    )


@pytest.mark.parametrize("state", ["queued", "testing", "tested"])
def test_stop_removes_front_without_waiting_for_lease_or_abandonment(cohort, state):
    cp, queue, front, behind = cohort
    if state == "queued":
        entry = queue.admit(repository=REPO, branch="main", task_id=front.id, head_sha="F" * 40)
    else:
        entry = claim(queue, front, "F", "old-publisher")
        if state == "tested":
            record_tested(queue, entry, "old-publisher")
    successor = claim(queue, behind, "B", "current-publisher")
    record_tested(queue, successor, "current-publisher")
    cp.stop_task(front.id, actor="operator", reason="pause this work")
    history = cp.task_history(front.id)

    result = queue.reconcile_front(
        REPO, "main", canonical_tip_tree="new-tip", driver_task_id=behind.id
    )

    assert result["action"] == "evict"
    assert result["entry_id"] == entry.id
    assert [item.task_id for item in queue.live_entries(REPO, "main")] == [behind.id]
    assert queue.entry(successor.id).state == "queued"
    assert not queue.entry(successor.id).tested_merge_tree
    assert queue.may_land(successor.id, canonical_tip_tree="new-tip")[0] is False
    assert cp.get_task(front.id).state == TaskState.STOPPED.value
    assert cp.get_task(front.id).metadata["retained_ref"] == "refs/heads/front"
    assert cp.task_history(front.id) == history


def test_recent_stale_projection_reset_cannot_extend_stopped_owner_lifetime(cohort):
    cp, queue, front, behind = cohort
    entry = claim(queue, front, "F", "old-publisher")
    record_tested(queue, entry, "old-publisher")
    queue.admit(repository=REPO, branch="main", task_id=behind.id, head_sha="B" * 40)
    queue.release(entry.id, owner="old-publisher")
    queue._requeue_live(REPO, "main", event="historical stale-projection recovery")
    cp.stop_task(front.id, actor="operator")

    result = queue.reconcile_front(REPO, "main", canonical_tip_tree="new-tip")

    assert result["action"] == "evict"
    assert queue.front(REPO, "main").task_id == behind.id
    assert queue.reconcile_front(REPO, "main", driver_task_id=behind.id)["action"] == "none"


def test_explicit_restart_can_readmit_evicted_task_without_reusing_old_projection(cohort):
    cp, queue, front, _behind = cohort
    original = claim(queue, front, "F", "old-publisher")
    record_tested(queue, original, "old-publisher")
    cp.stop_task(front.id, actor="operator")
    assert queue.reconcile_front(REPO, "main")["action"] == "evict"
    cp.start_stopped_task(front.id, actor="operator")

    restarted = claim(queue, front, "F", "new-publisher")

    assert restarted.id != original.id
    assert restarted.state == "testing"
    assert not restarted.tested_merge_tree
    assert queue.may_land(restarted.id, canonical_tip_tree="old-tip")[0] is False


def test_owner_restarted_before_observation_keeps_normal_lease_protection(cohort):
    cp, queue, front, _behind = cohort
    entry = claim(queue, front, "F", "publisher")
    cp.stop_task(front.id, actor="operator")
    cp.start_stopped_task(front.id, actor="operator")

    assert queue.reconcile_front(REPO, "main")["action"] == "none"
    assert queue.front(REPO, "main").id == entry.id


def test_unknown_owner_is_not_assumed_stopped(cohort):
    _cp, queue, _front, _behind = cohort
    entry = queue.claim_slot(
        repository=REPO, branch="main", task_id="unknown", head_sha="F" * 40, owner="publisher"
    ).entry

    assert queue.reconcile_front(REPO, "main")["action"] == "none"
    assert queue.front(REPO, "main").id == entry.id
