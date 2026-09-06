"""A cancelled dependency whose disposition asserts its goal was met
elsewhere satisfies even the strict default "all_success" join, not just
"all_settled".

Confirmed live: a repository-onboarding task was closed
`--cancelled --disposition not_applicable` after its actual goal (a
registered repository contract) was achieved through a different, equally
valid path (a direct `mac admin bridge repository register` call). Its 10
dependents had no join-policy opt-in -- the default is "all_success" -- and
stayed permanently blocked/waiting, requiring dependencies to be cleared by
hand on every one of them.

Note: "duplicate"/"superseded" dispositions already redirect the dependency
edge to their replacement_task_id (see _canonical_task_dependency_ids /
whatever rewires the edge on close) -- the join then correctly waits on the
replacement instead of releasing blindly, which is MORE correct than a bare
release and is exercised separately below. Only "not_applicable" (no
replacement -- "this need doesn't exist anymore, full stop") hits the new
_dependency_state_satisfies_join branch directly and releases immediately.

A genuine "this isn't happening" cancellation (no disposition, or
disposition=preserve/failed_attempt) must still NOT satisfy "all_success" --
that half is already covered by test_all_success_is_unaffected in
test_dependency_settled_without_output.py and is re-asserted here too, so the
two behaviors stay visibly paired.
"""

from __future__ import annotations

import pytest

from mac.models import TaskState
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _force_complete(cp, task_id: str) -> None:
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?", (TaskState.COMPLETED.value, task_id)
    )


def test_cancelled_not_applicable_satisfies_all_success_immediately(cp):
    child = cp.create_task("author a project contract", project="mac")
    parent = cp.create_task(
        "ordinary downstream work",
        project="mac",
        dependencies=[child.id],
    )

    cp.close_task(
        child.id,
        "cancelled",
        "operator",
        detail={
            "disposition": "not_applicable",
            "reason": "goal achieved through a different path",
        },
    )

    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is True


@pytest.mark.parametrize("disposition", ["superseded", "duplicate"])
def test_cancelled_with_replacement_redirects_the_join_not_releases_it(cp, disposition):
    """The join must not release blindly when there IS a replacement --
    it should wait on the replacement's own completion instead."""
    child = cp.create_task("author a project contract", project="mac")
    parent = cp.create_task(
        "ordinary downstream work",
        project="mac",
        dependencies=[child.id],
    )
    replacement = cp.create_task("the equally valid other path", project="mac")

    cp.close_task(
        child.id,
        "cancelled",
        "operator",
        detail={
            "disposition": disposition,
            "reason": "goal achieved through a different path",
            "replacement_task_id": replacement.id,
        },
    )

    # Redirected, not yet satisfied: the replacement hasn't completed.
    assert cp._canonical_task_dependency_ids(parent.id) == [replacement.id]
    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is False

    _force_complete(cp, replacement.id)

    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is True


def test_cancelled_with_no_disposition_still_blocks_all_success(cp):
    child = cp.create_task("do the thing", project="mac")
    parent = cp.create_task(
        "ordinary downstream work",
        project="mac",
        dependencies=[child.id],
    )

    cp.close_task(child.id, "cancelled", "operator", detail={"reason": "abandoned"})

    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is False


def test_cancelled_failed_attempt_still_blocks_all_success(cp):
    child = cp.create_task("do the thing", project="mac")
    parent = cp.create_task(
        "ordinary downstream work",
        project="mac",
        dependencies=[child.id],
    )

    cp.close_task(
        child.id,
        "cancelled",
        "operator",
        detail={"disposition": "failed_attempt", "reason": "retries exhausted"},
    )

    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is False
