"""Saved task groups: a name for a selector, not for a list of tasks.

The distinction is the whole design. A group stores its expression and
re-evaluates on every use, so "everything parked in mac" keeps meaning that as
tasks enter and leave the state. Materialising the members would freeze a list
that is wrong by the time anyone reads it -- and a bulk operation run against
a stale list acts on the wrong tasks, silently.

Groups are terms in the same grammar rather than a separate "saved" mode,
which is what lets one be refined in place: ``group=parked-mac priority>=5``.
"""

from __future__ import annotations

import pytest

from mac.models import NotFoundError
from mac.services import ControlPlane
from mac.task_selection import SelectorError


def _plane():
    return ControlPlane.in_memory()


def _parked(cp, count, *, project=None, priority=1):
    for index in range(count):
        task = cp.create_task("parked %d" % index, project=project, priority=priority)
        cp.request_task_input(task.id, [{"question": "which database?"}], "worker-1")


def test_a_group_is_a_saved_expression():
    cp = _plane()
    _parked(cp, 3, project="mac")

    saved = cp.task_groups.save(
        "parked-mac", "state=needs_input project=mac", description="the mac inbox"
    )

    assert saved["name"] == "parked-mac"
    assert saved["expression"] == "state=needs_input project=mac"
    assert saved["description"] == "the mac inbox"
    assert cp.task_batches.select("group=parked-mac").matched == 3


def test_a_group_re_evaluates_rather_than_freezing_its_members():
    """The reason the expression is stored instead of the ids."""
    cp = _plane()
    _parked(cp, 2, project="mac")
    cp.task_groups.save("parked-mac", "state=needs_input project=mac")
    assert cp.task_batches.select("group=parked-mac").matched == 2

    _parked(cp, 3, project="mac")

    assert cp.task_batches.select("group=parked-mac").matched == 5


def test_a_group_can_be_refined_in_place():
    """Groups are terms in the same grammar, not a separate mode."""
    cp = _plane()
    _parked(cp, 1, project="mac", priority=9)
    _parked(cp, 2, project="mac", priority=1)
    cp.task_groups.save("parked-mac", "state=needs_input project=mac")

    assert cp.task_batches.select("group=parked-mac").matched == 3
    assert cp.task_batches.select("group=parked-mac priority>=5").matched == 1


def test_groups_compose():
    cp = _plane()
    _parked(cp, 1, project="mac", priority=9)
    _parked(cp, 2, project="mac", priority=1)
    cp.task_groups.save("parked-mac", "state=needs_input project=mac")
    cp.task_groups.save("urgent", "group=parked-mac priority>=5")

    assert cp.task_batches.select("group=urgent").matched == 1


# --- a group that cannot resolve is refused when saved, not when used ----


def test_a_group_referencing_itself_is_refused():
    cp = _plane()
    with pytest.raises(SelectorError, match="references itself"):
        cp.task_groups.save("loop", "group=loop")


def test_a_cycle_between_groups_is_refused():
    cp = _plane()
    cp.task_groups.save("a", "state=open")
    cp.task_groups.save("b", "group=a")
    # Redefining `a` in terms of `b` would close the loop.
    with pytest.raises(SelectorError):
        cp.task_groups.save("a", "group=b")


def test_a_group_naming_a_missing_group_is_refused():
    cp = _plane()
    with pytest.raises(SelectorError, match="unknown task group"):
        cp.task_groups.save("broken", "group=nonexistent")


def test_an_invalid_expression_is_refused_at_save_time():
    """Failing here beats failing when someone runs a bulk operation."""
    cp = _plane()
    with pytest.raises(SelectorError, match="unknown selector key"):
        cp.task_groups.save("bad", "colour=blue")


def test_a_group_needs_a_name():
    cp = _plane()
    with pytest.raises(SelectorError, match="needs a name"):
        cp.task_groups.save("   ", "state=open")


# --- lifecycle ------------------------------------------------------------


def test_saving_the_same_name_updates_it():
    cp = _plane()
    cp.task_groups.save("inbox", "state=needs_input")
    cp.task_groups.save("inbox", "state=needs_input project=mac", description="scoped")

    saved = cp.task_groups.get("inbox")
    assert saved["expression"] == "state=needs_input project=mac"
    assert saved["description"] == "scoped"
    assert len(cp.task_groups.list()) == 1


def test_groups_are_listed_and_deleted():
    cp = _plane()
    cp.task_groups.save("alpha", "state=open")
    cp.task_groups.save("beta", "state=failed")

    assert [row["name"] for row in cp.task_groups.list()] == ["alpha", "beta"]

    cp.task_groups.delete("alpha")
    assert [row["name"] for row in cp.task_groups.list()] == ["beta"]

    with pytest.raises(NotFoundError):
        cp.task_groups.delete("alpha")
    with pytest.raises(NotFoundError):
        cp.task_groups.get("alpha")


# --- what the audit records ----------------------------------------------


def test_a_batch_records_the_expanded_terms_not_the_group_name():
    """A group definition can change after the batch ran.

    Recording `group=parked-mac` would name something whose meaning has since
    moved; recording the expanded terms says what actually selected the tasks.
    """
    cp = _plane()
    _parked(cp, 2, project="mac")
    cp.task_groups.save("parked-mac", "state=needs_input project=mac")

    outcome = cp.task_batches.apply(
        "group=parked-mac", "answer", answer="postgres", actor="jordan", apply=True
    )

    assert outcome.selector == "state=needs_input project=mac"
    assert "group=" not in outcome.selector
    assert len(outcome.changed) == 2


def test_a_group_term_never_reaches_evaluation_unexpanded():
    """Defence in depth: an unexpanded group must fail loudly, not match all."""
    from mac.task_selection import matches, parse_selector

    with pytest.raises(SelectorError, match="was not expanded"):
        matches(parse_selector("group=whatever"), object())
