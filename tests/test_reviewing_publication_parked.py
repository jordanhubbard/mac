"""An approved task with nowhere to publish must not look like work in flight.

Four tasks (task_a308126c, task_a12723af, task_b2ef42ea, task_b4906951) sat in
REVIEWING from 2026-08-01 to 2026-08-05: reviewed, approved, memory recorded, no
agent holding them, no events for four days. They surfaced only because someone
asked "is anything executing right now".

The mechanism is deliberate up to the last step. ``REVIEWING -> COMPLETED``
happens in exactly one place, ``publish_task``, which runs only once a
publication target resolves. ``_default_publication_target`` tries per-task
metadata, then the project, then ``MAC_DEFAULT_PUBLICATION_TARGET``, and returns
None when none apply -- correctly, because inventing a destination is worse. The
four tasks were operator tasks (repository_required false) with project=None, so
a ``git://`` fleet default was skipped by ``_task_git_publishable`` on purpose
and every source missed them.

So the guard is right and what was missing is everything downstream of it. These
tests cover the three consumers added for that:

  * the task records WHY it parked, instead of the reason living in a comment,
  * ``task_stats`` reports ``reviewing_parked`` so it cannot masquerade as work,
  * a diagnostic reports it within a bounded time with nobody looking.

The recoverability half matters as much as the detection half. A parked task is
fixable by setting a target, and a check that kept reporting it afterwards -- or
that fired on tasks which can publish perfectly well -- would be noise, and noise
is what stopped ``lifecycle-stage-dwell`` (one warning, 492 tasks, measured on
the live hub 2026-08-06) from being the thing that caught these.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mac.diagnostics import (
    REVIEWING_PARKED_THRESHOLD_SECONDS,
    _reviewing_publication_parked,
)
from mac.models import TaskState, parse_time, utcnow
from mac.services import PUBLICATION_BLOCK_SCHEMA, ControlPlane


@pytest.fixture()
def cp(monkeypatch):
    # An unset fleet default is the condition under test; a leaked
    # MAC_DEFAULT_PUBLICATION_TARGET from the ambient environment would give
    # every task a destination and make all of this pass vacuously.
    monkeypatch.delenv("MAC_DEFAULT_PUBLICATION_TARGET", raising=False)
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _reviewing_task(cp, *, metadata=None, project="mac", title="approved work"):
    """A task sitting unowned in REVIEWING, as an approved task does.

    Driving the real review workflow needs a signed executor manifest, which is
    deliberately unforgeable; the state these tests care about is "REVIEWING,
    nobody holding it", so they establish that directly.
    """
    task = cp.create_task(title, project=project, metadata=dict(metadata or {}))
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = NULL, updated_at = ? WHERE id = ?",
        (TaskState.REVIEWING.value, utcnow(), task.id),
    )
    return cp.get_task(task.id)


def _age(cp, task_id, seconds):
    """Backdate the task so it reads as parked for ``seconds``."""
    when = (parse_time(utcnow()) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")
    cp.store.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (when, task_id))


# --------------------------------------------------------------------------
# Detection: what is parked, and what only looks like it
# --------------------------------------------------------------------------


def test_an_approved_task_with_no_destination_is_parked(cp):
    task = _reviewing_task(cp)

    parked = cp.parked_reviewing_tasks()

    assert [item["id"] for item in parked] == [task.id]
    assert parked[0]["reason"] == "no_publication_target"


def test_a_task_with_a_per_task_target_is_not_parked(cp):
    _reviewing_task(cp, metadata={"publication_target": "ledger://done"})

    assert cp.parked_reviewing_tasks() == []


def test_a_task_whose_project_supplies_a_target_is_not_parked(cp):
    cp.create_project(
        "delivered",
        dispatch_paused=False,
        metadata={"publication_target": "ledger://delivered"},
    )
    _reviewing_task(cp, project="delivered")

    assert cp.parked_reviewing_tasks() == []


def test_a_non_git_fleet_default_unparks_everything(cp, monkeypatch):
    """Option 2 from the filing: a non-git default gives operator tasks a home.

    It is not switched on here, but if an operator does switch it on the check
    must go quiet, or it reports a problem the operator has already solved.
    """
    _reviewing_task(cp)
    monkeypatch.setenv("MAC_DEFAULT_PUBLICATION_TARGET", "ledger://fleet")

    assert cp.parked_reviewing_tasks() == []


def test_a_git_fleet_default_does_not_unpark_a_non_repo_task(cp, monkeypatch):
    """The exact shape of the four tasks that went missing.

    ``git://`` is gated on ``_task_git_publishable``, which is False for an
    operator task with no repository -- deliberately, since forcing a git
    publish there would raise and block it. So the fleet default is set, looks
    like it covers everything, and does not cover these.
    """
    _reviewing_task(cp, metadata={"execution_contract": {"repository_required": False}})
    monkeypatch.setenv("MAC_DEFAULT_PUBLICATION_TARGET", "git://main")

    assert len(cp.parked_reviewing_tasks()) == 1


def test_a_task_someone_is_holding_is_not_parked(cp):
    """An owned task is mid-review, not abandoned."""
    task = _reviewing_task(cp)
    machine = cp.register_machine("reviewer-host")
    agent = cp.register_agent(machine.id, "reviewer", capabilities=["python"])
    cp.store.execute("UPDATE tasks SET owner_agent_id = ? WHERE id = ?", (agent.id, task.id))

    assert cp.parked_reviewing_tasks() == []


@pytest.mark.parametrize("state", ["open", "running", "completed", "failed"])
def test_only_reviewing_tasks_are_considered(cp, state):
    task = cp.create_task("elsewhere", project="mac")
    cp.store.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task.id))

    assert cp.parked_reviewing_tasks() == []


def test_resolution_is_re_evaluated_rather_than_trusted_from_the_marker(cp):
    """An operator who sets a target has fixed it; stop reporting it.

    The marker is written when the task parks and is never rewritten, so a check
    that keyed off the marker alone would keep reporting a task that has been
    repaired -- and a stale warning is how a real one gets ignored.
    """
    task = _reviewing_task(cp)
    cp._record_publication_block(task.id, reason="no_publication_target")
    assert len(cp.parked_reviewing_tasks()) == 1

    metadata = dict(cp.get_task(task.id).metadata)
    metadata["publication_target"] = "ledger://done"
    cp._persist_task_metadata_narrow(task.id, metadata)

    assert cp.parked_reviewing_tasks() == []


def test_a_task_that_parked_before_the_marker_existed_is_still_found(cp):
    """The four tasks this was filed for carry no marker.

    A check that only found marked tasks would have been blind to every task
    already parked when it shipped -- i.e. to all of the evidence.
    """
    task = _reviewing_task(cp)

    assert "publication_block" not in cp.get_task(task.id).metadata
    assert [item["id"] for item in cp.parked_reviewing_tasks()] == [task.id]


# --------------------------------------------------------------------------
# The marker: the task says why, instead of a code comment saying why
# --------------------------------------------------------------------------


def test_the_marker_records_the_reason_and_the_audit_trail(cp):
    task = _reviewing_task(cp)

    cp._record_publication_block(
        task.id,
        reason="no_publication_target",
        review_id="rev_1",
        evidence_id="ev_1",
    )

    block = cp.get_task(task.id).metadata["publication_block"]
    assert block["schema"] == PUBLICATION_BLOCK_SCHEMA
    assert block["reason"] == "no_publication_target"
    assert block["review_id"] == "rev_1"
    assert block["evidence_id"] == "ev_1"
    assert block["since"]


def test_re_parking_for_the_same_reason_is_a_no_op(cp):
    """Re-review is routine; it must not rewrite the task or its clock.

    This covers the early return only. It does NOT exercise the ``since``
    carry-over below -- an early version of this file asserted the clock here
    and passed happily against a build that reset ``since`` on every write,
    because it never got that far.
    """
    task = _reviewing_task(cp)
    cp._record_publication_block(task.id, reason="no_publication_target")
    before = cp.get_task(task.id)

    cp._record_publication_block(task.id, reason="no_publication_target")

    assert cp.get_task(task.id).metadata == before.metadata


def test_a_changed_reason_keeps_the_original_park_time(cp):
    """The task has been blocked continuously; only the explanation moved.

    Restarting the clock here would let a task whose block is re-characterized
    stay permanently below the reporting threshold -- which is the failure this
    whole check exists to prevent, reintroduced one level down.
    """
    task = _reviewing_task(cp)
    cp._record_publication_block(task.id, reason="no_publication_target")
    first_since = cp.get_task(task.id).metadata["publication_block"]["since"]

    cp._record_publication_block(task.id, reason="publication_target_unresolvable")

    block = cp.get_task(task.id).metadata["publication_block"]
    assert block["reason"] == "publication_target_unresolvable"
    assert block["since"] == first_since, "the park clock restarted"


def test_age_is_measured_from_the_marker_not_from_the_last_touch(cp):
    """Any unrelated metadata write moves updated_at; the park time must not."""
    task = _reviewing_task(cp)
    cp._record_publication_block(task.id, reason="no_publication_target")
    old = (parse_time(utcnow()) - timedelta(days=4)).isoformat(timespec="microseconds")
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["publication_block"]["since"] = old
    cp._persist_task_metadata_narrow(task.id, metadata)

    parked = cp.parked_reviewing_tasks()

    assert parked[0]["since"] == old
    assert parked[0]["age_seconds"] > 3 * 24 * 3600


# --------------------------------------------------------------------------
# task_stats: a parked task must not be counted as work in progress
# --------------------------------------------------------------------------


def test_stats_reports_parked_tasks_separately(cp):
    _reviewing_task(cp)

    stats = cp.task_stats()

    assert stats["reviewing"] == 1
    assert stats["reviewing_parked"] == 1


def test_stats_does_not_report_the_key_when_nothing_is_parked(cp):
    _reviewing_task(cp, metadata={"publication_target": "ledger://done"})

    stats = cp.task_stats()

    assert stats["reviewing"] == 1
    assert "reviewing_parked" not in stats


def test_the_state_counts_still_sum_to_the_task_total(cp):
    """``reviewing_parked`` annotates the states; it must not reclassify one.

    Anything summing the state counts to get a total would start double- or
    under-counting if a parked task were moved out of ``reviewing``.
    """
    _reviewing_task(cp)
    _reviewing_task(cp, metadata={"publication_target": "ledger://done"})
    cp.create_task("open work", project="mac")

    stats = cp.task_stats()
    states = {k: v for k, v in stats.items() if k != "reviewing_parked"}

    assert sum(states.values()) == 3
    assert stats["reviewing_parked"] == 1


def test_stats_is_filtered_by_project(cp):
    cp.create_project("other", dispatch_paused=False)
    _reviewing_task(cp, project="other")

    assert "reviewing_parked" not in cp.task_stats(project="mac")
    assert cp.task_stats(project="other")["reviewing_parked"] == 1


# --------------------------------------------------------------------------
# The diagnostic: bounded-time visibility with nobody looking
# --------------------------------------------------------------------------


def test_the_check_is_quiet_when_nothing_is_parked(cp):
    findings = _reviewing_publication_parked(cp)

    assert [f.severity for f in findings] == ["ok"]


def test_the_check_warns_once_a_task_is_past_the_threshold(cp):
    task = _reviewing_task(cp)
    _age(cp, task.id, REVIEWING_PARKED_THRESHOLD_SECONDS + 60)

    findings = _reviewing_publication_parked(cp)

    assert findings[0].severity == "warn"
    assert findings[0].detail["count"] == 1
    assert findings[0].detail["tasks"][0]["id"] == task.id
    assert "publication_target" in findings[0].detail["remedy"]


def test_a_freshly_parked_task_is_not_reported_yet(cp):
    """A publication barrier that clears normally must not read as a stall."""
    _reviewing_task(cp)

    assert _reviewing_publication_parked(cp)[0].severity == "ok"


def test_the_check_never_mutates_the_task(cp):
    """It reports; the disposition is an operator decision.

    Auto-transitioning would change completion semantics fleet-wide off the back
    of a health check, which is option 2 in the filing and wants a deliberate
    decision rather than being switched on to clear a backlog.
    """
    task = _reviewing_task(cp)
    _age(cp, task.id, REVIEWING_PARKED_THRESHOLD_SECONDS + 60)
    before = cp.get_task(task.id)

    _reviewing_publication_parked(cp)

    after = cp.get_task(task.id)
    assert after.state == before.state == TaskState.REVIEWING.value
    assert after.metadata == before.metadata


def test_the_check_is_registered_so_mac_diagnostics_runs_it(cp):
    """An unregistered check is one nobody runs, which is the original defect."""
    from mac.diagnostics import CHECKS

    assert "reviewing-publication-parked" in {diag.name for diag in CHECKS}
