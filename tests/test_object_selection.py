"""One selector grammar, for every first-class object.

``task_selection`` gave tasks a selector and it is the right shape. It was also
task-only, and reachable only from ``mac task select`` / ``mac task batch``, so
the obvious spelling of the obvious question did not work::

    mac task list --selector 'state!=cancelled'      # no such flag
    mac project list --selector 'paused=true'        # no such concept

The operator asked for it on the MUTATING verbs too, and gave the reason
plainly: bulk cleanup of thousands of bad tasks is not a thing you do one id
at a time. So the danger is accepted and bounded -- every selector-driven
mutation is dry by default and reports what it would touch.

These tests cover the generalisation. Two properties matter most:

* an unknown key is REFUSED, naming that object's valid keys. A silently
  dropped term widens the group, and on a mutating verb the group is what is
  about to change; and
* an empty selector is refused rather than meaning "everything".
"""

from __future__ import annotations

import pytest

from mac.object_selection import (
    OBJECTS,
    filter_records,
    matches,
    parse,
    valid_keys,
)
from mac.task_selection import SelectorError

TASKS = [
    {
        "id": "t1",
        "state": "open",
        "project": "mac",
        "priority": 5,
        "title": "fix postgres timeout",
        "required_capabilities": ["python"],
        "metadata": {"origin": {"kind": "dream"}},
    },
    {
        "id": "t2",
        "state": "cancelled",
        "project": "mac",
        "priority": 1,
        "title": "obsolete",
        "required_capabilities": [],
    },
    {
        "id": "t3",
        "state": "blocked",
        "project": "nanolang",
        "priority": 9,
        "title": "wav asset",
        "required_capabilities": ["c", "python"],
    },
]


def _ids(records):
    return [r["id"] for r in records]


# --------------------------------------------------------------------------
# The operator's two examples
# --------------------------------------------------------------------------


def test_not_equals_excludes_a_state():
    """ "tasks whose state is not cancelled" -- the first thing asked for."""
    assert _ids(filter_records(TASKS, "state!=cancelled", "task")) == ["t1", "t3"]


def test_equals_selects_a_state():
    assert _ids(filter_records(TASKS, "state=blocked", "task")) == ["t3"]


def test_a_comma_list_is_any_of():
    assert _ids(filter_records(TASKS, "state=open,blocked", "task")) == ["t1", "t3"]


def test_terms_are_anded():
    assert _ids(filter_records(TASKS, "state!=cancelled project=mac", "task")) == ["t1"]


def test_numeric_bounds():
    assert _ids(filter_records(TASKS, "priority>=5", "task")) == ["t1", "t3"]
    assert _ids(filter_records(TASKS, "priority<=1", "task")) == ["t2"]


def test_contains_is_case_insensitive():
    assert _ids(filter_records(TASKS, "title~POSTGRES", "task")) == ["t1"]


def test_list_valued_attributes():
    assert _ids(filter_records(TASKS, "capability=c", "task")) == ["t3"]
    assert _ids(filter_records(TASKS, "capability!=python", "task")) == ["t2"]


def test_metadata_paths():
    assert _ids(filter_records(TASKS, "metadata.origin.kind=dream", "task")) == ["t1"]


# --------------------------------------------------------------------------
# Every first-class object, not just tasks
# --------------------------------------------------------------------------


PROJECTS = [
    {"name": "mac", "dispatch_paused": False},
    {"name": "ova", "dispatch_paused": True},
]
AGENTS = [
    {"id": "a1", "name": "rocky", "status": "idle", "capabilities": ["python"], "capacity": 2},
    {"id": "a2", "name": "natasha", "status": "busy", "capabilities": ["metal"], "capacity": 8},
]


@pytest.mark.parametrize("name", ["task", "project", "agent"])
def test_every_first_class_object_has_a_registry(name):
    assert name in OBJECTS
    assert valid_keys(name)


def test_projects_select_on_dispatch_state():
    assert [p["name"] for p in filter_records(PROJECTS, "paused=true", "project")] == ["ova"]
    assert [p["name"] for p in filter_records(PROJECTS, "paused=false", "project")] == ["mac"]


def test_agents_select_on_status_and_capability():
    assert _ids(filter_records(AGENTS, "status=idle", "agent")) == ["a1"]
    assert _ids(filter_records(AGENTS, "capability=metal", "agent")) == ["a2"]
    assert _ids(filter_records(AGENTS, "capacity>=8", "agent")) == ["a2"]


# --------------------------------------------------------------------------
# Refusals: the half that makes this safe on a mutating verb
# --------------------------------------------------------------------------


def test_an_unknown_key_is_refused_with_the_valid_ones():
    """A silently dropped term widens the group being mutated."""
    with pytest.raises(SelectorError) as excinfo:
        parse("bogus=1", "task")

    message = str(excinfo.value)
    assert "not a selectable attribute of task" in message
    assert "state" in message and "priority" in message


def test_a_key_from_the_wrong_object_is_refused():
    """`state` means something for a task and nothing for a project."""
    with pytest.raises(SelectorError) as excinfo:
        parse("state=open", "project")

    assert "not a selectable attribute of project" in str(excinfo.value)


def test_an_empty_selector_is_refused():
    """Empty must not quietly mean "everything" on a delete."""
    with pytest.raises(SelectorError) as excinfo:
        parse("", "task")

    assert "refusing to match every task" in str(excinfo.value)


def test_an_unparsable_term_is_refused():
    with pytest.raises(SelectorError):
        parse("state", "task")


def test_a_numeric_operator_on_a_text_key_is_refused():
    """`title>=5` is a question with no meaning; guessing at it would be worse."""
    with pytest.raises(SelectorError):
        filter_records(TASKS, "title>=5", "task")


def test_a_non_numeric_bound_is_refused():
    with pytest.raises(SelectorError):
        filter_records(TASKS, "priority>=high", "task")


def test_a_bad_boolean_is_refused():
    with pytest.raises(SelectorError):
        filter_records(PROJECTS, "paused=maybe", "project")


def test_an_unknown_object_is_refused():
    with pytest.raises(SelectorError):
        parse("state=open", "sandwich")


# --------------------------------------------------------------------------
# Robustness: one odd record must not stop a bulk sweep
# --------------------------------------------------------------------------


def test_a_record_missing_the_attribute_does_not_crash():
    records = [{"id": "x"}, {"id": "y", "state": "open"}]

    assert _ids(filter_records(records, "state=open", "task")) == ["y"]


def test_a_record_with_a_non_numeric_value_is_excluded_not_fatal():
    records = [{"id": "x", "priority": None}, {"id": "y", "priority": 7}]

    assert _ids(filter_records(records, "priority>=5", "task")) == ["y"]


def test_not_equals_includes_records_that_lack_the_attribute():
    """A task with no owner is genuinely "not owned by bob"."""
    records = [{"id": "x"}, {"id": "y", "owner_agent_id": "bob"}]

    assert _ids(filter_records(records, "owner!=bob", "task")) == ["x"]


def test_matches_agrees_with_filter():
    terms = parse("state=open", "task")

    assert matches(TASKS[0], terms, "task") is True
    assert matches(TASKS[1], terms, "task") is False
