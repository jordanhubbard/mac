"""Every first-class object supports create/list/show/update/delete.

work-package was the exception: it had no update and no delete, in the control
plane or the API. The help text said so honestly, which is better than lying,
but a beginner told "each object supports CRUD" then finds one that does not.

`update` is descriptive fields only. The PLAN belongs to `replan`, which
installs a compiled replacement into a paused package -- pointing the most
predictable verb in the vocabulary at the most consequential operation is the
trap that kept `task update` from being an alias of `edit`.

`delete` is `cancel`, because a package is an audited record. Nothing
hard-deletes one, exactly as nothing hard-deletes a task.
"""

from __future__ import annotations

import pytest

from mac.models import TransitionError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


#: Legal routes through the database's own state machine.
_PATH_TO = {
    "admitted": (),
    "active": ("active",),
    "paused": ("active", "paused"),
    "completed": ("active", "completed"),
    "failed": ("active", "failed"),
}


def _package(cp, goal="ship the thing", state="draft"):
    """A package row written directly.

    Assembly needs a repository, an accepted candidate set and a compiled plan;
    none of that is what these tests are about, and building it would test the
    assembler rather than CRUD.

    A non-draft package needs the plan-version and epoch rows it points at:
    work_packages CHECKs that anything past draft has plan_version >= 1, and
    history carries a composite foreign key to the epoch. Writing 0/0 for a
    "completed" package would be a state the database cannot hold, so the test
    would be asserting against a shape production never produces.
    """
    from mac.models import new_id, utcnow, json_dumps

    package_id = new_id("wp")
    now = utcnow()
    versioned = state not in {"draft", "cancelled"}
    version = 1 if versioned else 0
    with cp.store.transaction() as conn:
        # Draft first, with 0/0. A trigger checks that a package's current
        # epoch pointer resolves to a real epoch row, and that row cannot exist
        # before the package it references -- so this is the order real
        # assembly uses, not a shortcut around it.
        conn.execute(
            "INSERT INTO work_packages "
            "(id, project, goal, state, current_plan_version, current_epoch, "
            " metadata, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (package_id, "mac", goal, "draft", 0, 0,
             json_dumps({}), "test", now, now),
        )
        if versioned:
            conn.execute(
                "INSERT INTO work_package_plan_versions "
                "(package_id, version, definition, plan_digest, reason, "
                " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (package_id, 1, "{}", "sha256:%s" % ("0" * 64), "test", "test", now),
            )
            conn.execute(
                "INSERT INTO work_package_epochs "
                "(package_id, epoch, plan_version, planning_base_ref, "
                " planning_base_sha, status, reason, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, 1, 1, "refs/heads/main", "0" * 40, "active",
                 "test", "test", now),
            )
            conn.execute(
                "UPDATE work_packages SET current_plan_version = 1, "
                "current_epoch = 1, state = 'admitted' WHERE id = ?",
                (package_id,),
            )
            # A trigger enforces the state machine, and draft -> completed is
            # not an edge in it. Walking the real path keeps the fixture
            # honest: `completed` has no outgoing transitions at all, which is
            # exactly why cancel refuses it.
            for step in _PATH_TO.get(state, ()):
                conn.execute(
                    "UPDATE work_packages SET state = ? WHERE id = ?",
                    (step, package_id),
                )
    return package_id


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------


def test_update_changes_the_goal(cp):
    package_id = _package(cp)

    result = cp.update_work_package(package_id, goal="ship a different thing")

    assert result["goal"] == "ship a different thing"


def test_update_merges_metadata_rather_than_replacing_it(cp):
    """Replacing would silently drop keys the caller never mentioned."""
    package_id = _package(cp)
    cp.update_work_package(package_id, metadata={"owner": "jkh"})

    cp.update_work_package(package_id, metadata={"lane": "managed"})

    assert cp.describe_work_package(package_id)["package"]["metadata"] == {
        "owner": "jkh",
        "lane": "managed",
    }


def test_update_needs_something_to_change(cp):
    package_id = _package(cp)

    with pytest.raises(ValidationError):
        cp.update_work_package(package_id)


def test_a_finished_package_cannot_be_edited(cp):
    """Editing a completed package's stated goal rewrites what the work was
    for, after the fact."""
    package_id = _package(cp, state="completed")

    with pytest.raises(TransitionError):
        cp.update_work_package(package_id, goal="something else entirely")


def test_update_does_not_touch_the_plan(cp):
    """The whole reason update is not an alias of replan."""
    package_id = _package(cp)

    cp.update_work_package(package_id, goal="new goal")

    package = cp.describe_work_package(package_id)["package"]
    assert package["current_plan_version"] == 0
    assert package["current_epoch"] == 0


# --------------------------------------------------------------------------
# delete (cancel)
# --------------------------------------------------------------------------


def test_cancel_makes_the_package_terminal(cp):
    package_id = _package(cp)

    result = cp.cancel_work_package(package_id, reason="superseded")

    assert result["state"] == "cancelled"


def test_cancel_keeps_the_record(cp):
    """`delete` on an audited object must not destroy history."""
    package_id = _package(cp)
    cp.cancel_work_package(package_id, reason="superseded")

    assert cp.describe_work_package(package_id)["package"]["id"] == package_id


def test_cancel_requires_a_reason(cp):
    package_id = _package(cp)

    with pytest.raises(ValidationError):
        cp.cancel_work_package(package_id, reason="")


def test_cancelling_twice_is_not_an_error(cp):
    """Idempotent, so a retry after a dropped response does not fail."""
    package_id = _package(cp)
    cp.cancel_work_package(package_id, reason="superseded")

    assert cp.cancel_work_package(package_id, reason="superseded")["state"] == "cancelled"


def test_a_completed_package_cannot_be_cancelled(cp):
    """It already landed; cancelling would misdescribe history."""
    package_id = _package(cp, state="completed")

    with pytest.raises(TransitionError):
        cp.cancel_work_package(package_id, reason="changed my mind")


# --------------------------------------------------------------------------
# the promise the help text makes
# --------------------------------------------------------------------------


def test_no_first_class_object_advertises_a_crud_gap():
    """`mac help` tells a beginner every object supports create/list/show/
    update/delete. An object that does not makes the first sentence they read
    wrong."""
    from mac.cli_surface import FIRST_CLASS

    gaps = {
        obj.name: [verb for verb, impl in obj.crud.items() if impl is None]
        for obj in FIRST_CLASS
        if any(impl is None for impl in obj.crud.values())
    }

    assert gaps == {}
