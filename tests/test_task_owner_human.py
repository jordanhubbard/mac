"""A task records WHO filed it, not only which agent ran it.

`tasks.created_by_human` and the first-class `Human` principal both already
existed -- the column is created by store_postgres.ensure_column and the model
is in models.py. Nothing ever wrote to the column. So the ledger could answer
"which agent is running this" and could not answer "whose task is this", and
`mac task list --mine` had nothing to filter on.

This is the smallest slice of task_de8abe37 (multi-user on one hub): it adds a
writer and a filter. It changes no isolation semantics -- reads stay global,
which several loops depend on.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


@pytest.fixture()
def alice(cp):
    return cp.register_human(username="alice", display_name="Alice")


def test_a_task_records_who_filed_it(cp, alice):
    task = cp.create_task("alice's work", project="mac", created_by_human="alice")

    assert cp.get_task(task.id).created_by_human == alice.id


def test_the_stable_id_is_stored_not_the_typed_username(cp, alice):
    """A username is an external anchor that can change. Storing it would let a
    rename silently re-point a task's record of who filed it."""
    task = cp.create_task("work", project="mac", created_by_human="alice")

    assert cp.get_task(task.id).created_by_human == alice.id
    assert cp.get_task(task.id).created_by_human != "alice"


def test_an_unknown_human_is_refused(cp):
    """Otherwise ownership becomes a free-text string that looks like a
    principal and is not one."""
    with pytest.raises(ValidationError) as excinfo:
        cp.create_task("work", project="mac", created_by_human="nobody")

    assert "no such human" in str(excinfo.value)


def test_the_refusal_says_how_to_fix_it(cp):
    with pytest.raises(ValidationError) as excinfo:
        cp.create_task("work", project="mac", created_by_human="nobody")

    assert "register" in str(excinfo.value)


def test_a_task_filed_by_nobody_is_still_allowed(cp):
    """Every existing task has no filer. Requiring one would break them all."""
    task = cp.create_task("unattributed", project="mac")

    assert cp.get_task(task.id).created_by_human is None


def test_listing_can_be_scoped_to_one_human(cp, alice):
    bob = cp.register_human(username="bob", display_name="Bob")
    cp.create_task("alice one", project="mac", created_by_human="alice")
    cp.create_task("alice two", project="mac", created_by_human="alice")
    cp.create_task("bob one", project="mac", created_by_human="bob")

    mine = cp.list_tasks(created_by_human=alice.id)

    assert {t.title for t in mine} == {"alice one", "alice two"}


def test_listing_is_unscoped_by_default(cp, alice):
    """Reads stay global. Scoping them as a side effect of adding owners would
    change isolation semantics that fleet diagnostics and the yield gate rely
    on."""
    cp.create_task("mine", project="mac", created_by_human="alice")
    cp.create_task("theirs", project="mac")

    assert len(cp.list_tasks()) == 2
