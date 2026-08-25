"""Bulk task mutation, and the guarantees that make it safe to offer at all.

A batch is the one operation where being slightly wrong is expensive: the
blast radius is chosen by an expression, and the operator sees a count rather
than the tasks. Three properties carry that weight, and each is pinned here
because each was, at one point in this implementation, not actually true:

* the dry run runs the same validation the apply does -- it used to skip it
  entirely and cheerfully preview a batch that would fail on every task;
* a truncated group is never applied -- expect_count compared the full match
  count while the loop walked a smaller slice;
* one task's refusal costs only that task.

Plus the destructive one: `set metadata=` replaced metadata wholesale, so a
batch would have erased needs_input, no_dispatch and work-package links across
every matched task at once.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane
from mac.task_batch import BatchCountMismatch, BatchOperationError


def _plane():
    return ControlPlane.in_memory()


def _parked(cp, count, *, project=None, metadata=None):
    tasks = []
    for index in range(count):
        task = cp.create_task("parked %d" % index, project=project, metadata=dict(metadata or {}))
        cp.request_task_input(task.id, [{"question": "which database?"}], "worker-1")
        tasks.append(task)
    return tasks


# --- selection ------------------------------------------------------------


def test_a_selector_resolves_to_the_group_it_names():
    cp = _plane()
    _parked(cp, 3, project="mac")
    _parked(cp, 2, project="other")
    cp.create_task("ordinary", project="mac")

    selection = cp.task_batches.select("state=needs_input project=mac")

    assert selection.matched == 3
    assert all(task.project == "mac" for task in selection.tasks)


def test_selection_reports_truncation_rather_than_hiding_it():
    cp = _plane()
    _parked(cp, 5)

    selection = cp.task_batches.select("state=needs_input", limit=2)

    assert selection.matched == 5, "the true size must survive the limit"
    assert len(selection.tasks) == 2
    assert selection.truncated is True


# --- the dry run is the same code path as the apply ----------------------


def test_a_dry_run_changes_nothing():
    cp = _plane()
    _parked(cp, 3)

    outcome = cp.task_batches.apply(
        "state=needs_input", "answer", answer="postgres", actor="jordan"
    )

    assert outcome.applied is False
    assert len(outcome.changed) == 3
    assert cp.task_batches.select("state=needs_input").matched == 3


@pytest.mark.parametrize(
    ("operation", "options", "fragment"),
    [
        ("answer", {}, "requires the answer text"),
        ("set", {}, "at least one field"),
        ("close", {"target_state": "nonsense"}, "must be one of"),
        ("cancel", {"reasons": "typo"}, "does not take reasons"),
        ("nonsense", {}, "unknown batch operation"),
        ("release", {"reason": "unused"}, "does not take reason"),
    ],
)
def test_the_dry_run_rejects_what_the_apply_would_reject(operation, options, fragment):
    """Validation used to live inside the per-task loop, which a dry run skips.

    So a preview of `answer` with no answer text reported "3 tasks would
    change" and then failed all three. A preview that does not run the same
    checks is not a preview.
    """
    cp = _plane()
    _parked(cp, 3)

    with pytest.raises(BatchOperationError) as excinfo:
        cp.task_batches.apply("state=needs_input", operation, **options)
    assert fragment in str(excinfo.value)


def test_an_unknown_option_is_refused_not_dropped():
    """The write side must be no laxer than the selector side.

    A swallowed `reasons=` typo would cancel the whole group with no reason
    recorded at all.
    """
    cp = _plane()
    _parked(cp, 2)
    with pytest.raises(BatchOperationError):
        cp.task_batches.apply("state=needs_input", "cancel", reasons="typo", apply=True)


# --- applying -------------------------------------------------------------


def test_answering_a_group_returns_all_of_them_to_the_queue():
    cp = _plane()
    _parked(cp, 3, project="mac")
    _parked(cp, 2, project="other")

    outcome = cp.task_batches.apply(
        "state=needs_input project=mac",
        "answer",
        answer="postgres, us-west",
        actor="jordan",
        apply=True,
    )

    assert outcome.applied is True
    assert len(outcome.changed) == 3
    assert not outcome.failed
    assert cp.task_batches.select("state=needs_input project=mac").matched == 0
    # Scoping is the whole point: the other project is untouched.
    assert cp.task_batches.select("state=needs_input project=other").matched == 2


def test_one_refusal_costs_only_that_task():
    cp = _plane()
    _parked(cp, 3)
    cp.create_task("never asked anything")

    outcome = cp.task_batches.apply(
        "state=needs_input,open", "answer", answer="x", actor="jordan", apply=True
    )

    assert outcome.matched == 4
    assert len(outcome.changed) == 3
    assert len(outcome.failed) == 1
    # The refusal is predicted from the task's state, so it names both the
    # state found and the states the operation applies to.
    error = outcome.failed[0]["error"]
    assert "task is open" in error and "needs_input" in error
    # It names its type too, so a state-machine refusal is distinguishable
    # from a programming error.
    assert error.startswith("ValidationError")


def test_metadata_is_merged_never_replaced():
    """update_task replaces metadata; in bulk that is unrecoverable.

    Erasing needs_input across a whole group would strand exactly the tasks
    this feature exists to rescue.
    """
    cp = _plane()
    _parked(cp, 2, metadata={"keep": "me"})

    cp.task_batches.apply("state=needs_input", "set", metadata_merge={"triaged": "yes"}, apply=True)

    for task in cp.task_batches.select("state=needs_input").tasks:
        metadata = cp.get_task(task.id).metadata
        assert metadata["keep"] == "me", "existing metadata was destroyed"
        assert metadata["triaged"] == "yes"
        assert "needs_input" in metadata, "the parked question was destroyed"


def test_set_changes_the_fields_it_is_given():
    cp = _plane()
    _parked(cp, 2, project="mac")

    cp.task_batches.apply("state=needs_input project=mac", "set", priority=7, apply=True)

    assert all(
        cp.get_task(task.id).priority == 7
        for task in cp.task_batches.select("state=needs_input").tasks
    )


# --- guards ---------------------------------------------------------------


def test_a_group_that_changed_size_is_not_applied():
    """The scripted-use guard: a batch written against a preview must not act
    on a larger set later."""
    cp = _plane()
    _parked(cp, 3)

    with pytest.raises(BatchCountMismatch):
        cp.task_batches.apply(
            "state=needs_input", "answer", answer="x", apply=True, expect_count=99
        )
    assert cp.task_batches.select("state=needs_input").matched == 3


def test_a_truncated_group_is_never_applied():
    """expect_count compares the FULL match count while the loop walks the
    truncated slice, so a limited apply could pass the guard and then touch a
    different set than the operator approved."""
    cp = _plane()
    _parked(cp, 5)

    with pytest.raises(BatchCountMismatch) as excinfo:
        cp.task_batches.apply("state=needs_input", "release", apply=True, limit=2)
    assert "truncated" in str(excinfo.value)
    assert cp.task_batches.select("state=needs_input").matched == 5


def test_an_empty_selector_cannot_reach_the_apply_path():
    cp = _plane()
    _parked(cp, 3)
    from mac.task_selection import SelectorError

    with pytest.raises(SelectorError):
        cp.task_batches.apply("", "release", apply=True)


# --- audit ----------------------------------------------------------------


def test_a_batch_is_one_reviewable_act():
    cp = _plane()
    _parked(cp, 3, project="mac")

    outcome = cp.task_batches.apply(
        "state=needs_input project=mac",
        "answer",
        answer="postgres",
        actor="jordan",
        apply=True,
    )

    assert outcome.batch_id
    # The recorded selector must be replayable, or the audit names a group
    # nobody can reconstruct.
    from mac.task_selection import parse_selector

    assert parse_selector(outcome.selector).expression == outcome.selector


# --- what the review found the first implementation got wrong ------------


def test_the_preview_and_the_apply_agree_on_refusals():
    """A preview that does not check state preconditions is not a preview.

    The dry run used to list every selected task as "would change" without
    consulting the state machine, so it could promise N changes and deliver
    none of them.
    """
    cp = _plane()
    _parked(cp, 3)
    cp.create_task("never asked anything")

    preview = cp.task_batches.apply("state=needs_input,open", "answer", answer="x")
    applied = cp.task_batches.apply(
        "state=needs_input,open", "answer", answer="x", actor="jordan", apply=True
    )

    assert (len(preview.changed), len(preview.failed)) == (3, 1)
    assert (len(applied.changed), len(applied.failed)) == (3, 1)


def test_a_group_that_changed_membership_without_changing_size_is_refused():
    """expect_count cannot see this; the token can.

    One task cancelled and another created leaves the count identical, and the
    batch would act on a task that was never previewed.
    """
    cp = _plane()
    parked = _parked(cp, 2)
    previewed = cp.task_batches.select("state=needs_input")

    cp.close_task(
        parked[0].id,
        "cancelled",
        "ops",
        {"reason": "x", "disposition": "not_applicable"},
    )
    _parked(cp, 1)
    current = cp.task_batches.select("state=needs_input")
    assert current.matched == previewed.matched, "the count must be unchanged"

    with pytest.raises(BatchCountMismatch) as excinfo:
        cp.task_batches.apply(
            "state=needs_input",
            "answer",
            answer="x",
            apply=True,
            expect_token=previewed.token,
        )
    assert "no longer the one previewed" in str(excinfo.value)


def test_unmet_never_selects_a_terminal_task():
    """`unmet=` neutralises the task gates to ask "can the fleet run this?".

    Without a state bound that question is answered for cancelled and
    completed tasks too, so `unmet=... cancel` would re-touch the entire
    historical ledger.
    """
    cp = _plane()
    machine = cp.register_machine("host", resources={"cpu": 4, "memory_gb": 8})
    cp.register_agent(machine.id, "worker", capabilities=["python"])

    live = cp.create_task("live", required_capabilities=["cuda"])
    dead = cp.create_task("dead", required_capabilities=["cuda"])
    cp.close_task(dead.id, "cancelled", "ops", {"reason": "x", "disposition": "not_applicable"})

    selected = cp.task_batches.select("unmet=agent_capabilities_missing")
    ids = {task.id for task in selected.tasks}

    assert live.id in ids
    assert dead.id not in ids


def test_a_directly_constructed_empty_selector_is_still_refused():
    """parse_selector refuses "", but TaskSelector() is constructible."""
    from mac.task_selection import SelectorError, TaskSelector

    cp = _plane()
    _parked(cp, 2)
    with pytest.raises(SelectorError):
        cp.task_batches.select(TaskSelector())


def test_the_audit_names_the_tasks_it_touched():
    """Counts do not answer "which tasks did batch X change?"."""
    cp = _plane()
    _parked(cp, 2, project="mac")
    outcome = cp.task_batches.apply(
        "state=needs_input project=mac",
        "answer",
        answer="pg",
        actor="jordan",
        apply=True,
    )
    assert len(outcome.changed) == 2
    assert outcome.selection_token
    # The ids reach the durable record, not just the counts.
    import json

    rows = cp.store.query_all(
        "SELECT detail FROM observability_events WHERE name = ? ORDER BY sequence DESC",
        ("task.batch.applied",),
    )
    assert rows, "the batch recorded no observation"
    detail = json.loads(rows[0]["detail"])
    assert set(detail["changed"]) == set(outcome.changed)
    assert detail["batch_id"] == outcome.batch_id


# --- metadata: nothing is forbidden, everything is visible ---------------


def test_wholesale_replacement_is_allowed_and_reports_what_it_removes():
    """The asymmetry that used to exist was arbitrary.

    A single task could have its metadata replaced; a group could not, purely
    because it was dangerous. The real problem was that the danger was
    invisible -- a preview of ids and titles cannot show that four hundred
    tasks are about to lose `needs_input`. Making the loss visible is the fix;
    forbidding the operation was not.
    """
    cp = _plane()
    _parked(cp, 3, metadata={"keep": "me"})

    preview = cp.task_batches.apply("state=needs_input", "set", metadata_replace={"fresh": "start"})

    impact = preview.metadata_impact
    assert impact is not None
    assert impact["tasks_changed"] == 3
    assert impact["removed_keys"]["keep"] == 3
    # execution_contract is regenerated on every write, so it is not a loss
    # and must not be reported as one -- a preview that cries wolf gets
    # skimmed, and the real warnings go with it.
    assert "execution_contract" not in impact["removed_keys"]
    # The keys the control plane itself depends on are named separately,
    # because losing one changes how the task is treated, not just what it
    # records.
    assert impact["load_bearing_keys_removed"]["needs_input"] == 3

    cp.task_batches.apply(
        "state=needs_input", "set", metadata_replace={"fresh": "start"}, apply=True
    )

    # It really did replace -- this is destructive, and offered plainly rather
    # than forbidden. The consequence is precisely why `needs_input` is flagged
    # load-bearing: the task is still PARKED, but the question it was parked on
    # is gone, so it now sits in the inbox with nothing to answer. Legal, and
    # something an operator must choose knowingly rather than discover.
    still_parked = cp.list_tasks("needs_input")
    assert len(still_parked) == 3
    for task in still_parked:
        assert task.metadata["fresh"] == "start"
        assert "keep" not in task.metadata
        assert "needs_input" not in task.metadata
        # Regenerated, exactly as the impact report predicted.
        assert "execution_contract" in task.metadata


def test_merge_is_deep_so_a_sibling_key_survives():
    """A shallow update replaces a nested object wholesale.

    Merging {"origin": {"kind": "x"}} would silently drop origin.tenant_id --
    the same invisible loss as a replace, just harder to notice.
    """
    cp = _plane()
    _parked(cp, 2, metadata={"origin": {"kind": "generator", "tenant_id": "t1"}})

    preview = cp.task_batches.apply(
        "state=needs_input", "set", metadata_merge={"origin": {"kind": "manual"}}
    )
    assert preview.metadata_impact["removed_keys"] == {}

    cp.task_batches.apply(
        "state=needs_input",
        "set",
        metadata_merge={"origin": {"kind": "manual"}},
        apply=True,
    )
    for task in cp.task_batches.select("state=needs_input").tasks:
        origin = cp.get_task(task.id).metadata["origin"]
        assert origin == {"kind": "manual", "tenant_id": "t1"}


def test_a_path_can_be_set_and_unset_precisely():
    cp = _plane()
    _parked(cp, 2, metadata={"keep": "me"})

    cp.task_batches.apply(
        "state=needs_input",
        "set",
        metadata_set={"triage.owner": "jordan"},
        apply=True,
    )
    cp.task_batches.apply("state=needs_input", "set", metadata_unset=["keep"], apply=True)

    for task in cp.task_batches.select("state=needs_input").tasks:
        metadata = cp.get_task(task.id).metadata
        assert metadata["triage"] == {"owner": "jordan"}
        assert "keep" not in metadata
        assert "needs_input" in metadata, "a precise edit touched nothing else"


def test_unsetting_an_absent_path_is_not_an_error():
    cp = _plane()
    _parked(cp, 1)
    outcome = cp.task_batches.apply(
        "state=needs_input", "set", metadata_unset=["never.was.here"], apply=True
    )
    assert not outcome.failed


def test_the_preview_impact_is_computed_by_the_code_that_writes():
    """Same function, so the preview cannot describe a different result."""
    cp = _plane()
    _parked(cp, 2, metadata={"a": 1, "b": {"c": 2}})
    options = {"metadata_merge": {"b": {"d": 3}}, "metadata_unset": ["a"]}

    preview = cp.task_batches.apply("state=needs_input", "set", **options)
    cp.task_batches.apply("state=needs_input", "set", apply=True, **options)

    assert preview.metadata_impact["removed_keys"] == {"a": 2}
    for task in cp.task_batches.select("state=needs_input").tasks:
        metadata = cp.get_task(task.id).metadata
        assert "a" not in metadata
        assert metadata["b"] == {"c": 2, "d": 3}


def test_metadata_impact_is_absent_when_the_operation_does_not_touch_metadata():
    cp = _plane()
    _parked(cp, 2)
    assert cp.task_batches.apply("state=needs_input", "set", priority=3).metadata_impact is None
