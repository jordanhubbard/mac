"""A verb given an abbreviated id must act on the resolved one.

REPORTED FROM THE LIVE HUB. `mac task release task_78bf3c52` returned HTTP 500:

    insert or update on table "task_history" violates foreign key constraint
    "task_history_task_id_fkey"
    DETAIL:  Key (task_id)=(task_78bf3c52) is not present in table "tasks".

`_persist_task_metadata_narrow` called `get_task`, which EXPANDS an
abbreviation, and then used the RAW argument for both the UPDATE and the
history insert. Four of the five id uses in `release_task` were already
resolved, which is what let this survive review: the function looks like it
resolves ids, because almost everywhere it does.

TWO DISTINCT FAILURES, and the quiet one is worse:

  * `UPDATE tasks ... WHERE id = <abbreviation>` matches ZERO rows and reports
    success. A metadata write that writes nothing and raises nothing.
  * the history insert violates a foreign key and 500s. Only this one was
    visible, and only because task_history has the constraint.

REACHABLE BY COPY-PASTE. `mac task list` prints short ids by default (git-style,
8 hex), so the id a user has just been shown is exactly the one that fails. And
`release` is the only exit from a `--no-dispatch` hold: claim refuses with
`dispatch_held`, and `open -> completed` is not a legal transition. Every task
filed with `--no-dispatch` was unreleasable.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _short(task_id: str) -> str:
    return task_id[: len("task_") + 8]


def test_release_accepts_the_short_id_the_cli_prints(cp):
    """The reported bug."""
    task = cp.create_task("held", metadata={"no_dispatch": True})

    released = cp.release_task(_short(task.id), actor="test")

    assert released.id == task.id
    assert "no_dispatch" not in (released.metadata or {})


def test_the_metadata_write_actually_lands(cp):
    """The silent half. A 0-row UPDATE reported success, so the hold survived
    while the caller was told it had been cleared."""
    task = cp.create_task("held", metadata={"no_dispatch": True, "keep": "me"})

    cp.release_task(_short(task.id), actor="test")

    stored = cp.get_task(task.id).metadata or {}
    assert "no_dispatch" not in stored
    assert stored.get("keep") == "me", "unrelated metadata must survive"


def test_history_records_the_resolved_id(cp):
    """The loud half: task_history.task_id is a foreign key into tasks."""
    task = cp.create_task("held", metadata={"no_dispatch": True})

    cp.release_task(_short(task.id), actor="test")

    rows = cp.store.query_all(
        "SELECT task_id FROM task_history WHERE task_id = ?", (task.id,)
    )
    assert rows, "no history row was written against the real task id"


def test_the_full_id_still_works(cp):
    task = cp.create_task("held", metadata={"no_dispatch": True})

    assert cp.release_task(task.id, actor="test").id == task.id


def test_releasing_an_unheld_task_is_a_no_op(cp):
    task = cp.create_task("free")

    assert cp.release_task(_short(task.id), actor="test").id == task.id


def test_an_unknown_short_id_is_not_found_not_a_500(cp):
    """A bad id must be a domain error, not a foreign-key violation."""
    from mac.models import NotFoundError

    with pytest.raises(NotFoundError):
        cp.release_task("task_deadbeef", actor="test")
