"""The selector grammar, and the ways it must not silently widen a group.

A selector chooses the tasks a bulk mutation is about to touch, so the failure
that matters here is not "the expression was rejected" -- it is "the
expression was accepted and meant something other than what was typed". Every
test below pins one way that could happen.

Several of these pin real defects found in review of the first implementation:
``text!=`` selected precisely the tasks it was meant to exclude; ``capability~``
quietly meant equality; ``unmet!=<typo>`` matched every task in the ledger; and
a quoted value produced an expression that could not be parsed back, which is
the expression the batch audit record stores.
"""

from __future__ import annotations

import pytest

from mac.task_selection import (
    SelectorError,
    compile_sql,
    matches,
    parse_selector,
)


class _Task:
    """The attribute surface a selector reads."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "task_1")
        self.title = kwargs.get("title", "")
        self.description = kwargs.get("description", "")
        self.project = kwargs.get("project")
        self.state = kwargs.get("state", "open")
        self.priority = kwargs.get("priority", 0)
        self.attempt_count = kwargs.get("attempt_count", 0)
        self.max_attempts = kwargs.get("max_attempts", 3)
        self.owner_agent_id = kwargs.get("owner_agent_id")
        self.required_capabilities = kwargs.get("required_capabilities", [])
        self.metadata = kwargs.get("metadata", {})


# --- parsing refuses rather than guesses ---------------------------------


@pytest.mark.parametrize(
    ("expression", "fragment"),
    [
        ("", "refusing to match every task"),
        ("nonsense", "expected key=value"),
        ("colour=blue", "unknown selector key"),
        ("state", "expected key=value"),
        ("priority>=abc", "expects a number"),
        ("state~=open", "unknown operator"),
        ("state=!open", "unknown operator"),
        ("title~=x", "unknown operator"),
        ("metadata.=x", "needs a path"),
        ("title>=5", "not numeric"),
        ("priority>=1,2", "single number"),
        ('title~"unbalanced', "unbalanced"),
        ("unmet=agent_capabilty_missing", "unknown rejection code"),
    ],
)
def test_a_selector_that_cannot_be_trusted_is_refused(expression, fragment):
    with pytest.raises(SelectorError) as excinfo:
        parse_selector(expression)
    assert fragment in str(excinfo.value)


def test_an_empty_selector_never_means_everything():
    """The one default that must not exist."""
    with pytest.raises(SelectorError):
        parse_selector("   ")


def test_unknown_keys_name_the_valid_ones():
    with pytest.raises(SelectorError) as excinfo:
        parse_selector("colour=blue")
    message = str(excinfo.value)
    assert "state" in message and "project" in message and "metadata.<path>" in message


# --- negation is the direction that inverts silently ---------------------


def test_text_negation_excludes_rather_than_selects():
    """The inverted-logic defect: text!= used to return exactly the wrong set.

    Both halves were wrong the same way -- compile_sql emitted the positive
    LIKE, and _term_matches ignored the operator -- so the bug survived the
    SQL-narrows-then-Python-confirms design that catches most mismatches.
    """
    hit = _Task(title="postgres migration")
    miss = _Task(title="unrelated work")

    exclude = parse_selector("text!=postgres")
    assert matches(exclude, hit) is False
    assert matches(exclude, miss) is True

    include = parse_selector("text~postgres")
    assert matches(include, hit) is True
    assert matches(include, miss) is False

    # And SQL must not pre-select the excluded rows behind Python's back.
    where, _params = compile_sql(exclude)
    assert "LIKE" not in where


@pytest.mark.parametrize("key", ["state", "project", "title", "priority", "capability"])
def test_negation_is_the_complement_of_equality(key):
    """For every negatable key, `x!=v` must match exactly what `x=v` does not."""
    tasks = [
        _Task(state="open", project="mac", title="alpha", priority=1,
              required_capabilities=["python"]),
        _Task(state="failed", project="other", title="beta", priority=2,
              required_capabilities=["cuda"]),
    ]
    value = {"state": "open", "project": "mac", "title": "alpha",
             "priority": "1", "capability": "python"}[key]
    positive = parse_selector("%s=%s" % (key, value))
    negative = parse_selector("%s!=%s" % (key, value))
    for task in tasks:
        assert matches(positive, task) is not matches(negative, task)


# --- the grammar means what it documents ---------------------------------


def test_contains_on_capability_is_a_substring_not_an_equality():
    task = _Task(required_capabilities=["postgres-admin"])
    assert matches(parse_selector("capability~post"), task) is True
    assert matches(parse_selector("capability=post"), task) is False
    assert matches(parse_selector("capability=postgres-admin"), task) is True


def test_a_comma_list_is_any_of():
    assert matches(parse_selector("state=open,failed"), _Task(state="failed")) is True
    assert matches(parse_selector("state=open,failed"), _Task(state="blocked")) is False


def test_separate_terms_are_all_of():
    task = _Task(state="open", project="mac")
    assert matches(parse_selector("state=open project=mac"), task) is True
    assert matches(parse_selector("state=open project=other"), task) is False


def test_numeric_bounds():
    assert matches(parse_selector("priority>=5"), _Task(priority=7)) is True
    assert matches(parse_selector("priority>=5"), _Task(priority=2)) is False
    assert matches(parse_selector("attempts<=1"), _Task(attempt_count=0)) is True
    assert matches(parse_selector("attempts<=1"), _Task(attempt_count=3)) is False


def test_metadata_paths_reach_into_nested_json():
    task = _Task(metadata={"needs_input": {"asked_by": "worker-1"}})
    assert matches(parse_selector("metadata.needs_input.asked_by=worker-1"), task) is True
    assert matches(parse_selector("metadata.needs_input.asked_by=worker-2"), task) is False
    # A path that does not exist is simply not a match, never an error.
    assert matches(parse_selector("metadata.absent.path=x"), task) is False


def test_metadata_booleans_compare_as_words():
    """`no_dispatch` is JSON true; an operator types no_dispatch=true."""
    task = _Task(metadata={"no_dispatch": True})
    assert matches(parse_selector("metadata.no_dispatch=true"), task) is True


# --- the recorded expression has to survive being recorded ---------------


@pytest.mark.parametrize(
    "expression",
    [
        "state=needs_input project=mac",
        'title~"foo bar"',
        "state=open,failed priority>=3",
        "metadata.needs_input.asked_by=worker-1",
        "unmet=agent_capabilities_missing",
    ],
)
def test_an_expression_round_trips(expression):
    """The batch audit stores the rendered selector.

    An expression that cannot be parsed back is an audit record nobody can
    replay, which defeats the point of recording which group was touched.
    A quoted value used to render unquoted and fail to re-parse.
    """
    once = parse_selector(expression)
    twice = parse_selector(once.expression)
    assert twice.expression == once.expression


# --- SQL narrows; Python decides -----------------------------------------


def test_sql_is_parameterised():
    where, params = compile_sql(parse_selector("state=open project='; DROP TABLE tasks--'"))
    assert "DROP TABLE" not in where
    assert "; DROP TABLE tasks--" in params


def test_sql_never_excludes_a_row_python_would_match():
    """The invariant the split design rests on, executed against a real store.

    compile_sql is a narrowing prefilter and `matches` decides. If SQL drops a
    row the matcher would accept, the group is silently smaller than asked
    for -- and "smaller" is not the safe direction either when the operation
    is `set` or `reopen`.

    This test previously computed the Python side only and asserted it was
    non-empty, so it passed while `capability~gpu` returned zero rows
    end-to-end. A differential test has to run both sides.
    """
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    fixtures = [
        ("Postgres Migration", ["gpu-h100-cluster"], "mac", {"origin": {"kind": "human"}}),
        ("postgres cleanup", ["python"], None, {"no_dispatch": True}),
        ("100% complete", ["café"], "mac", {"origin": {"kind": "generator"}}),
        ("path a\\b end", ["build-\U0001f680"], "other", {"depth": 3}),
        ("under_score", [], "mac", {}),
        ("plain", ["python", "cuda"], None, {"no_dispatch": False}),
    ]
    for title, capabilities, project, metadata in fixtures:
        cp.create_task(
            title,
            required_capabilities=capabilities,
            project=project,
            metadata=metadata,
        )
    everything = cp.list_tasks()

    expressions = [
        "state=open",
        "state!=open",
        "project!=mac",
        "project=mac",
        "text~postgres",
        "text~POSTGRES",
        "text!=postgres",
        "title~100%",
        "title~under_score",
        "title~a\\b",
        "capability=python",
        "capability~gpu",
        "capability~h100",
        "capability=café",
        "capability=build-\U0001f680",
        "capability!=cuda",
        "priority>=0",
        # Metadata paths now resolve through a GIN-indexed generated column
        # rather than in Python. JSON and Python spell values differently --
        # true vs True, 3 vs "3" -- so these are precisely where a pushed-down
        # predicate could disagree with the matcher.
        "metadata.origin.kind=generator",
        "metadata.origin.kind=human",
        "metadata.no_dispatch=true",
        "metadata.no_dispatch=false",
        "metadata.depth=3",
        "metadata.origin.kind~gener",
        "metadata.absent.path=x",
        "metadata.origin.kind!=generator",
    ]
    for expression in expressions:
        selector = parse_selector(expression)
        python_hits = {task.id for task in everything if matches(selector, task)}
        sql_hits = {task.id for task in cp.task_batches.select(expression).tasks}

        # The prefilter may return extra rows; it must never lose one.
        assert python_hits <= sql_hits or sql_hits == python_hits, (
            "%s: SQL dropped %s that the matcher accepts"
            % (expression, sorted(python_hits - sql_hits))
        )
        # And the end-to-end answer must equal the matcher's answer exactly.
        assert sql_hits == python_hits, (
            "%s: end-to-end %s != matcher %s"
            % (expression, sorted(sql_hits), sorted(python_hits))
        )


def test_a_null_project_still_answers_negation():
    """NULL columns are where SQL and Python disagree most easily."""
    selector = parse_selector("project!=mac")
    assert matches(selector, _Task(project=None)) is True
    where, _ = compile_sql(selector)
    assert "IS NULL" in where
