"""Regression tests: `mac task release` for staged tasks with controller-owned
routing/optimizer metadata (release-fix / regression_tests node).

These prove that ``ControlPlane.release_task`` un-stages a task by removing ONLY
the ``no_dispatch`` hold, both BEFORE and AFTER the control plane has attached
controller-owned routing metadata (``managed_fast_lane``; ``publication_route``
and ``publication_lane`` were removed once the two-valued lane collapsed to one
reachable value and then to none).  Before the fix, release routed the
metadata through the user-input guard / re-normalization, which either raised a
``ValidationError`` (because that routing metadata is control-plane-owned) or
mutated controller-owned fields.

Cases covered here (mirrored at the HTTP layer in
``tests/api/test_task_release_routing_api.py`` and at the CLI layer in
``tests/cli/test_cli_task_release_routing.py``):

1. release of a plain staged task (no routing metadata) clears ``no_dispatch``.
2. release of a staged task carrying controller-owned routing metadata succeeds
   (no ``ValidationError``) and un-stages the task.
3. after release the persisted metadata differs from the pre-release metadata
   ONLY by removal of ``no_dispatch``; controller-owned fields are byte-for-byte
   unchanged (no re-normalization, no re-added execution_contract/toolchain).
4. release of a task that is not held is a no-op.
5. the user-facing create/update guard still rejects operator-supplied
   publication routing metadata (the guard is not globally weakened).
"""

from __future__ import annotations

import json

import pytest

from mac.models import ValidationError
from mac.services import ControlPlane


ROUTING_KEYS = ("managed_fast_lane",)


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _persisted_metadata(cp, task_id):
    row = cp.store.query_one("SELECT metadata FROM tasks WHERE id = ?", (task_id,))
    return json.loads(row["metadata"])


def _attach_controller_routing(cp, task_id):
    """Attach controller-owned routing metadata the way the real control plane
    does — writing straight to the stored row, bypassing the user-input guard.
    """
    md = _persisted_metadata(cp, task_id)
    md["managed_fast_lane"] = {
        "schema": "mac.managed_single_task.route.v1",
        "activation": "legacy_compatibility",
    }
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(md), task_id),
    )


# ---------------------------------------------------------------------------
# Case 1 — plain staged task releases and un-stages.
# ---------------------------------------------------------------------------


def test_release_plain_staged_task_clears_no_dispatch(cp):
    task = cp.create_task("plain staged", metadata={"no_dispatch": True})
    assert cp._task_dispatch_held(task) is True

    released = cp.release_task(task.id, actor="operator")

    assert released.metadata.get("no_dispatch") is None
    assert cp._task_dispatch_held(released) is False
    # Un-staged: reappears in the ready queue.
    assert task.id in {t.id for t in cp.ready_tasks()}


# ---------------------------------------------------------------------------
# Cases 2 & 3 — release succeeds after routing metadata is attached, and only
# no_dispatch is removed.
# ---------------------------------------------------------------------------


def test_release_after_routing_attached_preserves_controller_fields(cp):
    task = cp.create_task("staged with routing", metadata={"no_dispatch": True})
    _attach_controller_routing(cp, task.id)

    before = _persisted_metadata(cp, task.id)
    assert before["no_dispatch"] is True

    # Case 2: no ValidationError, and it un-stages.
    released = cp.release_task(task.id, actor="operator")
    assert released.metadata.get("no_dispatch") is None
    assert cp._task_dispatch_held(released) is False

    after = _persisted_metadata(cp, task.id)
    assert "no_dispatch" not in after

    # Case 3: differs from pre-release metadata ONLY by no_dispatch removal.
    expected = dict(before)
    expected.pop("no_dispatch", None)
    assert after == expected

    # Controller-owned fields are byte-for-byte identical (no re-normalization).
    for key in ROUTING_KEYS:
        assert after[key] == before[key]


def test_release_does_not_reconcile_execution_contract_or_toolchain(cp):
    """Release must not re-run project-default / execution-contract / toolchain
    reconciliation: whatever fields the task carries (including any
    ``execution_contract`` already normalized at create time) stay byte-for-byte
    identical, and no new keys appear or disappear except ``no_dispatch``."""
    task = cp.create_task("staged minimal", metadata={"no_dispatch": True})
    _attach_controller_routing(cp, task.id)

    before = _persisted_metadata(cp, task.id)

    cp.release_task(task.id, actor="operator")

    after = _persisted_metadata(cp, task.id)
    # The key set changes only by dropping no_dispatch; nothing is re-added.
    assert set(after) == set(before) - {"no_dispatch"}
    # Every surviving field is unchanged (no re-normalization of the
    # execution_contract or toolchain reconciliation on release).
    for key in after:
        assert after[key] == before[key]


# ---------------------------------------------------------------------------
# Case 4 — release of a task that is not held is a no-op.
# ---------------------------------------------------------------------------


def test_release_not_held_is_noop(cp):
    task = cp.create_task("never staged")
    before = _persisted_metadata(cp, task.id)

    released = cp.release_task(task.id, actor="operator")

    after = _persisted_metadata(cp, task.id)
    assert released.id == task.id
    assert after == before


def test_release_not_held_with_routing_is_noop(cp):
    """A task carrying routing metadata but no hold must not be mutated."""
    task = cp.create_task("routed not held")
    _attach_controller_routing(cp, task.id)
    before = _persisted_metadata(cp, task.id)
    assert "no_dispatch" not in before

    released = cp.release_task(task.id, actor="operator")

    after = _persisted_metadata(cp, task.id)
    assert released.id == task.id
    assert after == before


# ---------------------------------------------------------------------------
# Case 5 — the create/update guard still rejects operator-supplied routing
# metadata (the guard is not globally weakened by the release fix).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", list(ROUTING_KEYS))
def test_create_guard_rejects_operator_routing_metadata(cp, key):
    with pytest.raises(ValidationError):
        cp.create_task("operator routing", metadata={key: {"lane": "legacy"}})


@pytest.mark.parametrize("key", list(ROUTING_KEYS))
def test_update_guard_rejects_operator_routing_metadata(cp, key):
    task = cp.create_task("plain")
    with pytest.raises(ValidationError):
        cp.update_task(task.id, metadata={key: {"lane": "legacy"}})


def test_reject_reserved_break_glass_metadata_helper_still_rejects_routing():
    for key in ROUTING_KEYS:
        with pytest.raises(ValidationError):
            ControlPlane._reject_reserved_break_glass_metadata({key: {"x": 1}})
