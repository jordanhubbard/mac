"""Tests for the periodic hold/stall sweep (mac.task_hold_sweep).

The load-bearing ones are the attribution tests: on 2026-08-20, triaging held
tasks by "a merged PR mentions this task id" produced 24 hits out of 75 and
every single one was a false positive.  ``test_mention_only_never_closes_a_task``
is the regression that keeps that signal out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from mac.task_hold_sweep import (
    ATTRIBUTION_BRANCH,
    ATTRIBUTION_MENTION_ONLY,
    ATTRIBUTION_NONE,
    ATTRIBUTION_SUBJECT,
    HOLD_ATTEMPTS_EXHAUSTED,
    HOLD_NO_DISPATCH,
    HOLD_REVIEW_KEY,
    VERDICT_BUDGET_RAISED,
    VERDICT_CANCELLED,
    VERDICT_RELEASED,
    VERDICT_REVIEWED_STILL_VALID,
    VERDICT_UNDECIDABLE,
    LedgerView,
    TaskHoldSweepConfig,
    TaskHoldSweeper,
    change_attribution,
    classify_hold,
    decide_verdict,
    hold_fingerprint,
    satisfying_change,
)


TASK_A = "task_" + "a" * 32
TASK_B = "task_" + "b" * 32
TASK_C = "task_" + "c" * 32


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


@dataclass
class FakeTask:
    id: str
    state: str = "open"
    title: str = "held work"
    project: str = "mac"
    metadata: Dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    max_attempts: int = 3
    dependencies: List[str] = field(default_factory=list)


class FakeCP:
    """Just enough control plane to exercise selection, decision and writes."""

    def __init__(self, tasks: Optional[List[FakeTask]] = None) -> None:
        self.tasks: Dict[str, FakeTask] = {t.id: t for t in (tasks or [])}
        self.closed: List[Dict[str, Any]] = []
        self.released: List[str] = []
        self.reviews: List[Dict[str, Any]] = []
        self.logs: List[Any] = []

    # -- reads
    def list_tasks(self, state=None, project=None, **_kwargs):
        rows = list(self.tasks.values())
        if state:
            wanted = set(state) if isinstance(state, (list, tuple, set)) else {state}
            rows = [t for t in rows if t.state in wanted]
        if project:
            rows = [t for t in rows if t.project == project]
        return rows

    def get_task(self, task_id):
        return self.tasks[task_id]

    # -- writes
    def close_task(self, task_id, target_state, actor, detail=None):
        task = self.tasks[task_id]
        task.state = target_state
        self.closed.append(
            {"task_id": task_id, "state": target_state, "actor": actor, "detail": detail or {}}
        )
        return task

    def release_task(self, task_id, *, actor="human"):
        task = self.tasks[task_id]
        task.metadata.pop("no_dispatch", None)
        self.released.append(task_id)
        return task

    def record_task_hold_review(self, task_id, review, *, actor="hold-sweeper", max_attempts=None):
        task = self.tasks[task_id]
        if max_attempts is not None and int(max_attempts) > task.max_attempts:
            task.max_attempts = int(max_attempts)
        task.metadata[HOLD_REVIEW_KEY] = dict(review)
        self.reviews.append({"task_id": task_id, "review": dict(review), "actor": actor})
        return task

    def record_log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def _sweeper(cp: FakeCP, **overrides: Any) -> TaskHoldSweeper:
    base: Dict[str, Any] = {
        "enabled": True,
        "budget": 10,
        "review_ttl_seconds": 3600.0,
        "attempt_grant": 1,
        "max_attempt_grants": 1,
    }
    base.update(overrides)
    return TaskHoldSweeper(cp, TaskHoldSweepConfig(**base))


def _held(task_id: str = TASK_A, **kwargs: Any) -> FakeTask:
    metadata = dict(kwargs.pop("metadata", {}))
    metadata["no_dispatch"] = True
    return FakeTask(id=task_id, metadata=metadata, **kwargs)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_defaults_off_and_hours_scale():
    cfg = TaskHoldSweepConfig.from_env({})
    assert cfg.enabled is False and cfg.active is False
    # "Interval measured in hours" is a requirement, not a preference: this is
    # archaeology over the whole backlog, not a dispatch-tick job.
    assert cfg.interval_seconds >= 60 * 60


def test_config_is_operator_settable():
    cfg = TaskHoldSweepConfig.from_env(
        {
            "MAC_HOLD_SWEEP_ENABLED": "1",
            "MAC_HOLD_SWEEP_INTERVAL_SECONDS": str(2 * 60 * 60),
            "MAC_HOLD_SWEEP_BUDGET": "25",
        }
    )
    assert cfg.active is True
    assert cfg.interval_seconds == 2 * 60 * 60
    assert cfg.budget == 25


def test_config_out_of_range_disables_rather_than_guessing():
    cfg = TaskHoldSweepConfig.from_env(
        {"MAC_HOLD_SWEEP_ENABLED": "1", "MAC_HOLD_SWEEP_INTERVAL_SECONDS": "5"}
    )
    assert cfg.configuration_error and cfg.active is False


# --------------------------------------------------------------------------- #
# Classification and fingerprinting
# --------------------------------------------------------------------------- #


def test_classifies_the_two_stalled_populations():
    held = classify_hold(_held(metadata={"hold": {"reason": "waiting on the fix"}}))
    assert held is not None and held.kind == HOLD_NO_DISPATCH
    assert held.reason == "waiting on the fix"

    stalled = classify_hold(FakeTask(TASK_B, attempt_count=3, max_attempts=3))
    assert stalled is not None and stalled.kind == HOLD_ATTEMPTS_EXHAUSTED
    assert stalled.attempts_exhausted is True


@pytest.mark.parametrize(
    "task",
    [
        FakeTask(TASK_A, state="open"),
        FakeTask(TASK_A, state="running", attempt_count=3, max_attempts=3),
        FakeTask(TASK_A, state="completed", attempt_count=3, max_attempts=3),
        FakeTask(TASK_A, state="claimed", metadata={"no_dispatch": True}),
    ],
)
def test_healthy_and_in_flight_tasks_are_not_candidates(task):
    # A task an agent is holding has a lease for a clock; it is not stalled.
    assert classify_hold(task) is None


def test_fingerprint_tracks_the_hold_but_not_the_review_record():
    task = _held(metadata={"hold": {"reason": "waiting on task_x"}})
    before = hold_fingerprint(task)
    task.metadata[HOLD_REVIEW_KEY] = {"verdict": "reviewed_still_valid"}
    assert hold_fingerprint(task) == before, "recording a verdict must not invalidate it"
    task.metadata["hold"]["reason"] = "waiting on something else"
    assert hold_fingerprint(task) != before


# --------------------------------------------------------------------------- #
# Attribution: the mention-vs-citation distinction
# --------------------------------------------------------------------------- #


def test_mention_only_never_closes_a_task():
    """A PR that merely MENTIONS a task id does not satisfy the check.

    This is the 2026-08-20 false-positive class, verbatim: a real merged PR for
    unrelated work whose body cites this task id as evidence.
    """

    pr = {
        "pr_title": "Wire provisioning demand to bounded HGX autoscaling",
        "branch": "mac/agent_rocky/task_" + "9" * 32,
        "body": "Context: see %s for the backlog discussion. Fixes nothing there." % TASK_A,
        "commit": "a" * 40,
        "merged": True,
    }
    assert change_attribution(pr, TASK_A) == ATTRIBUTION_MENTION_ONLY

    citation, rejected = satisfying_change([pr], TASK_A)
    assert citation is None
    assert rejected[0]["attribution"] == ATTRIBUTION_MENTION_ONLY

    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "x", "satisfied_by": pr}})])
    report = _sweeper(cp).run_once()
    verdict = report["verdicts"][0]
    assert verdict["verdict"] == VERDICT_UNDECIDABLE
    assert "MENTIONS" in verdict["reason"]
    # Left alone: not closed, not released, still held.
    assert cp.closed == [] and cp.released == []
    assert cp.tasks[TASK_A].state == "open"
    assert cp.tasks[TASK_A].metadata["no_dispatch"] is True


def test_subject_and_branch_attribution_are_accepted():
    assert (
        change_attribution({"subject": "MAC task %s: land the fix" % TASK_A}, TASK_A)
        == ATTRIBUTION_SUBJECT
    )
    assert (
        change_attribution({"branch": "mac/agent_x/%s-lease_1" % TASK_A}, TASK_A)
        == ATTRIBUTION_BRANCH
    )
    assert change_attribution({"subject": "unrelated work"}, TASK_A) == ATTRIBUTION_NONE


def test_short_display_id_does_not_match_a_longer_hex_token():
    other = "task_" + "a" * 8 + "f" * 24
    assert change_attribution({"subject": "MAC task %s: work" % other}, TASK_A) == (
        ATTRIBUTION_NONE
    )


def test_an_attributed_change_that_has_not_landed_does_not_close():
    citation, _ = satisfying_change(
        [{"subject": "MAC task %s: the fix" % TASK_A, "commit": "b" * 40, "merged": False}],
        TASK_A,
    )
    assert citation is None


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def test_satisfied_hold_is_released_citing_what_satisfied_it():
    blocker = FakeTask(TASK_B, state="completed")
    held = _held(TASK_A, metadata={"hold": {"reason": "waiting on the decomposition fix",
                                            "until_task": TASK_B}})
    cp = FakeCP([held, blocker])
    report = _sweeper(cp).run_once()

    verdict = next(v for v in report["verdicts"] if v["task_id"] == TASK_A)
    assert verdict["verdict"] == VERDICT_RELEASED
    assert verdict["citation"]["task_ids"] == [TASK_B]
    assert cp.released == [TASK_A]
    assert "no_dispatch" not in cp.tasks[TASK_A].metadata


def test_live_hold_is_recorded_as_reviewed_and_still_valid():
    blocker = FakeTask(TASK_B, state="open")
    held = _held(TASK_A, metadata={"hold": {"reason": "waiting", "until_task": TASK_B}})
    cp = FakeCP([held, blocker])
    report = _sweeper(cp).run_once()

    verdict = next(v for v in report["verdicts"] if v["task_id"] == TASK_A)
    assert verdict["verdict"] == VERDICT_REVIEWED_STILL_VALID
    assert cp.released == [] and cp.closed == []
    # The whole point: an unreviewed hold and a re-justified one are now
    # different objects in the ledger.
    marker = cp.tasks[TASK_A].metadata[HOLD_REVIEW_KEY]
    assert marker["verdict"] == VERDICT_REVIEWED_STILL_VALID
    assert marker["reviewed_at"] and marker["review_count"] == 1
    assert TASK_B in marker["reason"]


def test_landed_work_is_closed_citing_the_change():
    change = {
        "subject": "MAC task %s: add the impact-map canary" % TASK_A,
        "commit": "c" * 40,
        "branch": "mac/agent_x/%s" % TASK_A,
        "merged": True,
    }
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "held", "satisfied_by": change}})])
    report = _sweeper(cp).run_once()

    verdict = report["verdicts"][0]
    assert verdict["verdict"] == VERDICT_CANCELLED
    assert "c" * 40 in verdict["reason"]
    closed = cp.closed[0]
    assert closed["state"] == "cancelled"
    assert closed["detail"]["disposition"] == "not_applicable"
    assert "c" * 40 in closed["detail"]["reason"]
    assert closed["detail"]["hold_sweep"]["citation"]["commit"] == "c" * 40


def test_superseded_hold_is_closed_naming_the_replacement():
    cp = FakeCP(
        [
            _held(TASK_A, metadata={"hold": {"reason": "old plan", "replacement_task_id": TASK_C}}),
            FakeTask(TASK_C, state="open"),
        ]
    )
    report = _sweeper(cp).run_once()
    verdict = next(v for v in report["verdicts"] if v["task_id"] == TASK_A)
    assert verdict["verdict"] == VERDICT_CANCELLED
    detail = cp.closed[0]["detail"]
    assert detail["disposition"] == "superseded"
    assert detail["replacement_task_id"] == TASK_C


def test_replacement_that_is_not_in_the_ledger_is_undecidable():
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"replacement_task_id": TASK_C}})])
    report = _sweeper(cp).run_once()
    assert report["verdicts"][0]["verdict"] == VERDICT_UNDECIDABLE
    assert cp.closed == []


def test_no_longer_wanted_is_cancelled_with_a_reason():
    cp = FakeCP(
        [_held(TASK_A, metadata={"hold": {"disposition": "not_wanted",
                                          "reason": "the incident closed"}})]
    )
    _sweeper(cp).run_once()
    assert cp.closed[0]["detail"]["disposition"] == "not_applicable"
    assert "the incident closed" in cp.closed[0]["detail"]["reason"]


def test_hold_waiting_on_a_change_is_released_not_closed_when_it_lands():
    # "Release me when that change lands" and "this task's work already
    # landed" are different claims. Conflating them would close a task the
    # moment its blocker merged.
    blocker = {"subject": "Fix the decomposition guardrail", "commit": "d" * 40,
               "merged": True}
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "waiting on the fix",
                                                  "until_change": blocker}})])
    report = _sweeper(cp).run_once()

    verdict = report["verdicts"][0]
    assert verdict["verdict"] == VERDICT_RELEASED
    assert verdict["citation"]["kind"] == "landed_blocker"
    assert cp.released == [TASK_A] and cp.closed == []


def test_hold_waiting_on_a_change_that_has_not_landed_stays():
    blocker = {"subject": "Fix the guardrail", "commit": "d" * 40, "merged": False}
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "waiting",
                                                  "until_change": blocker}})])
    report = _sweeper(cp).run_once()
    assert report["verdicts"][0]["verdict"] == VERDICT_REVIEWED_STILL_VALID
    assert cp.released == []


def test_exhausted_hold_with_a_live_reason_keeps_its_hold():
    cp = FakeCP(
        [
            _held(TASK_A, attempt_count=3, max_attempts=3,
                  metadata={"hold": {"reason": "waiting", "until_task": TASK_B}}),
            FakeTask(TASK_B, state="open"),
        ]
    )
    report = _sweeper(cp).run_once()

    verdict = next(v for v in report["verdicts"] if v["task_id"] == TASK_A)
    assert verdict["verdict"] == VERDICT_BUDGET_RAISED
    # The undispatchable budget is fixed; the hold, which is still justified,
    # is not overridden.
    assert cp.tasks[TASK_A].max_attempts == 4
    assert cp.released == []
    assert cp.tasks[TASK_A].metadata["no_dispatch"] is True


def test_hold_with_no_machine_checkable_condition_is_left_untouched():
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "wait for the redesign"}})])
    report = _sweeper(cp).run_once()
    assert report["verdicts"][0]["verdict"] == VERDICT_UNDECIDABLE
    assert cp.closed == [] and cp.released == []
    assert cp.tasks[TASK_A].metadata["no_dispatch"] is True


# --------------------------------------------------------------------------- #
# open + attempts exhausted must never survive a run
# --------------------------------------------------------------------------- #


def test_exhausted_open_task_gets_a_raised_budget():
    cp = FakeCP([FakeTask(TASK_A, state="open", attempt_count=3, max_attempts=3)])
    report = _sweeper(cp).run_once()

    verdict = report["verdicts"][0]
    assert verdict["verdict"] == VERDICT_BUDGET_RAISED
    assert cp.tasks[TASK_A].max_attempts == 4
    assert cp.tasks[TASK_A].state == "open"
    assert cp.tasks[TASK_A].metadata[HOLD_REVIEW_KEY]["attempt_grants"] == 1


def test_exhausted_open_task_goes_terminal_once_its_grants_run_out():
    cp = FakeCP([FakeTask(TASK_A, state="open", attempt_count=3, max_attempts=3)])
    sweeper = _sweeper(cp, review_ttl_seconds=60.0)
    sweeper.run_once()
    # The retry did not land either: attempts catch up with the raised budget.
    cp.tasks[TASK_A].attempt_count = 4
    report = sweeper.run_once()

    verdict = report["verdicts"][0]
    assert verdict["verdict"] == VERDICT_CANCELLED
    assert cp.tasks[TASK_A].state == "cancelled"
    assert cp.closed[0]["detail"]["disposition"] == "failed_attempt"
    # Deliberate, and it says so: not a silent disappearance.
    assert "attempt budget exhausted" in cp.closed[0]["detail"]["reason"]


def test_no_run_leaves_an_open_exhausted_task_in_place():
    """The named invariant, checked over a mixed population."""

    done = "task_" + "d" * 32
    tasks = [
        FakeTask(TASK_A, state="open", attempt_count=3, max_attempts=3),
        FakeTask(TASK_B, state="open", attempt_count=9, max_attempts=3),
        _held(TASK_C, attempt_count=2, max_attempts=2,
              metadata={"hold": {"reason": "waiting on a human"}}),
        # The one that nearly slipped through: a satisfied hold on an
        # exhausted task. Releasing it alone would hand back an open,
        # undispatchable row.
        _held("task_" + "e" * 32, attempt_count=2, max_attempts=2,
              metadata={"hold": {"reason": "waiting", "until_task": done}}),
        FakeTask(done, state="completed"),
    ]
    cp = FakeCP(tasks)
    _sweeper(cp).run_once()
    for task in cp.tasks.values():
        undispatchable = (
            task.state == "open"
            and task.max_attempts > 0
            and task.attempt_count >= task.max_attempts
        )
        assert not undispatchable, "%s is open and undispatchable" % task.id


def test_exhausted_hold_is_released_with_a_budget_that_can_actually_run():
    # Releasing an exhausted task without raising its budget would move it from
    # "held" to "open and undispatchable" — the invisible state, by hand.
    cp = FakeCP([_held(TASK_A, attempt_count=3, max_attempts=3)])
    _sweeper(cp).run_once()
    assert cp.released == [TASK_A]
    assert cp.tasks[TASK_A].max_attempts == 4


# --------------------------------------------------------------------------- #
# Budget, cheap skip, ordering, concurrency
# --------------------------------------------------------------------------- #


def test_run_is_bounded_and_reports_what_it_did_not_reach():
    tasks = [_held("task_%032x" % n) for n in range(25)]
    cp = FakeCP(tasks)
    report = _sweeper(cp, budget=10).run_once()
    assert report["examined"] == 10
    assert len(report["verdicts"]) == 10
    # A bounded sweep that stays silent about the remainder reads as "the
    # backlog is clean" when it is merely truncated.
    assert report["deferred_over_budget"] == 15


def test_unchanged_reviewed_tasks_are_skipped_cheaply():
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "wait for the redesign"}})])
    sweeper = _sweeper(cp)
    first = sweeper.run_once()
    assert first["examined"] == 1

    second = sweeper.run_once()
    assert second["examined"] == 0
    assert second["skipped_unchanged"] == 1

    # A changed hold reason is a different question, so it is asked again.
    cp.tasks[TASK_A].metadata["hold"]["reason"] = "wait for something else"
    third = sweeper.run_once()
    assert third["examined"] == 1


def test_expired_review_ttl_re_examines():
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "wait"}})])
    sweeper = _sweeper(cp, review_ttl_seconds=60.0)
    sweeper.run_once()
    marker = cp.tasks[TASK_A].metadata[HOLD_REVIEW_KEY]
    marker["reviewed_at"] = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    assert sweeper.run_once()["examined"] == 1


def test_never_reviewed_tasks_are_examined_before_reviewed_ones():
    old = _held(TASK_A, metadata={"hold": {"reason": "wait"}})
    old.metadata[HOLD_REVIEW_KEY] = {
        "reviewed_at": _iso(datetime.now(timezone.utc) - timedelta(days=30)),
        "fingerprint": "sha256:stale",
    }
    fresh = _held(TASK_B, metadata={"hold": {"reason": "wait"}})
    cp = FakeCP([old, fresh])
    report = _sweeper(cp, budget=1).run_once()
    assert [v["task_id"] for v in report["verdicts"]] == [TASK_B]


def _decide_now(sweeper: TaskHoldSweeper, cp: FakeCP, task_id: str):
    """Decide one task the way a run would, without applying anything."""

    tasks = cp.list_tasks()
    ledger = LedgerView(
        states={t.id: t.state for t in tasks}, resolver=cp.get_task
    )
    task = cp.tasks[task_id]
    return decide_verdict(
        task, classify_hold(task), ledger=ledger, config=sweeper.config
    )


def test_a_stale_release_from_an_overlapping_run_is_not_applied_twice():
    # Two hub replicas decide against the same snapshot; one applies first.
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "held", "until_task": TASK_B}}),
                 FakeTask(TASK_B, state="completed")])
    first, second = _sweeper(cp), _sweeper(cp)
    stale = _decide_now(second, cp, TASK_A)

    first.run_once()
    applied = second._apply(stale, cp.tasks[TASK_A], actor="hold-sweeper", run_id="sweep_other")

    assert applied["status"] == "changed_under_run"
    assert cp.released == [TASK_A], "the release must not be applied twice"


def test_a_stale_close_from_an_overlapping_run_is_not_applied_twice():
    cp = FakeCP(
        [
            _held(TASK_A, metadata={"hold": {"reason": "old", "replacement_task_id": TASK_C}}),
            FakeTask(TASK_C, state="open"),
        ]
    )
    first, second = _sweeper(cp), _sweeper(cp)
    stale = _decide_now(second, cp, TASK_A)

    first.run_once()
    applied = second._apply(stale, cp.tasks[TASK_A], actor="hold-sweeper", run_id="sweep_other")

    assert applied["status"] == "already_terminal"
    assert len(cp.closed) == 1


def test_a_run_that_overlaps_itself_reports_busy_instead_of_sweeping():
    cp = FakeCP([_held(TASK_A)])
    sweeper = _sweeper(cp)
    assert sweeper._run_lock.acquire(blocking=False)
    try:
        report = sweeper.run_once()
    finally:
        sweeper._run_lock.release()
    assert report["status"] == "busy" and report["verdicts"] == []
    assert cp.reviews == []


def test_a_task_another_run_just_reviewed_is_not_reviewed_again():
    # Same fingerprint, a marker from a different run, inside the TTL: the
    # second run's write would be a duplicate, so it is refused.
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "wait for the redesign"}})])
    first, second = _sweeper(cp), _sweeper(cp)
    stale = _decide_now(second, cp, TASK_A)
    first.run_once()

    applied = second._apply(stale, cp.tasks[TASK_A], actor="hold-sweeper", run_id="sweep_other")
    assert applied["status"] == "claimed_by_concurrent_run"
    assert len(cp.reviews) == 1


def test_a_task_that_moved_under_the_run_is_not_acted_on():
    held = _held(TASK_A, metadata={"hold": {"reason": "held", "until_task": TASK_B}})
    cp = FakeCP([held, FakeTask(TASK_B, state="completed")])
    sweeper = _sweeper(cp)

    original_get = cp.get_task

    def racing_get(task_id):
        task = original_get(task_id)
        if task_id == TASK_A:
            # Somebody cancelled it between the decision and the write.
            task.state = "cancelled"
        return task

    cp.get_task = racing_get
    report = sweeper.run_once()
    assert cp.released == []
    assert report["verdicts"][0]["applied"]["status"] == "already_terminal"


def test_dry_run_decides_without_touching_anything():
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "held", "until_task": TASK_B}}),
                 FakeTask(TASK_B, state="completed")])
    report = _sweeper(cp).run_once(dry_run=True)
    assert report["verdicts"][0]["verdict"] == VERDICT_RELEASED
    assert cp.released == [] and cp.reviews == []


def test_a_failing_task_does_not_abort_the_run():
    cp = FakeCP([_held(TASK_A, metadata={"hold": {"reason": "x", "until_task": TASK_B}}),
                 _held(TASK_C, metadata={"hold": {"reason": "y", "until_task": TASK_B}}),
                 FakeTask(TASK_B, state="completed")])

    def exploding_release(task_id, *, actor="human"):
        if task_id == TASK_A:
            raise RuntimeError("hub write failed")
        return cp.tasks[task_id]

    cp.release_task = exploding_release
    report = _sweeper(cp).run_once()
    assert report["errors"] == 1
    assert {v["verdict"] for v in report["verdicts"]} == {"error", VERDICT_RELEASED}


def test_status_reports_config_and_last_run():
    cp = FakeCP([_held(TASK_A)])
    sweeper = _sweeper(cp)
    assert sweeper.status()["last_report"] is None
    sweeper.run_once()
    status = sweeper.status()
    assert status["config"]["active"] is True
    assert status["last_report"]["examined"] == 1


# --------------------------------------------------------------------------- #
# Against a real control plane
# --------------------------------------------------------------------------- #


def _real_plane(tmp_path):
    from mac.test_support import control_plane_on, dsn_for

    return control_plane_on(dsn_for(tmp_path))


def _force_state(cp, task_id: str, **columns: Any) -> None:
    """Set columns the public API cannot reach cheaply.

    ``attempt_count`` only moves through real failures, and ``completed``
    requires an approved review with evidence.  Both are inputs to this sweep,
    not things it produces, so the test writes them directly rather than
    staging a full execution.
    """

    assignments = ", ".join("%s = ?" % name for name in columns)
    cp.store.execute(
        "UPDATE tasks SET %s WHERE id = ?" % assignments,
        (*columns.values(), task_id),
    )


def test_superseded_close_survives_the_real_cancellation_contract(tmp_path):
    cp = _real_plane(tmp_path)
    replacement = cp.create_task("the replacement", project="mac")
    held = cp.create_task(
        "parked work",
        project="mac",
        metadata={
            "no_dispatch": True,
            "hold": {"reason": "old plan", "replacement_task_id": replacement.id},
        },
    )

    report = _sweeper(cp).run_once()

    verdict = next(v for v in report["verdicts"] if v["task_id"] == held.id)
    assert verdict["verdict"] == VERDICT_CANCELLED
    after = cp.get_task(held.id)
    assert after.state == "cancelled"
    # The narrow metadata write kept the rest of the task's metadata intact.
    assert after.metadata["hold"]["replacement_task_id"] == replacement.id
    assert after.metadata[HOLD_REVIEW_KEY]["verdict"] == VERDICT_CANCELLED


def test_release_and_budget_raise_land_on_the_real_ledger(tmp_path):
    cp = _real_plane(tmp_path)
    blocker = cp.create_task("the blocking fix", project="mac")
    _force_state(cp, blocker.id, state="completed")
    held = cp.create_task(
        "parked work",
        project="mac",
        max_attempts=3,
        metadata={
            "no_dispatch": True,
            "hold": {"reason": "waiting on the fix", "until_task": blocker.id},
        },
    )
    _force_state(cp, held.id, attempt_count=3)

    report = _sweeper(cp).run_once()
    verdict = next(v for v in report["verdicts"] if v["task_id"] == held.id)

    after = cp.get_task(held.id)
    assert verdict["verdict"] == VERDICT_RELEASED
    # Released AND given a budget it can actually run on: releasing alone would
    # have moved it from "held" to "open and undispatchable".
    assert "no_dispatch" not in after.metadata
    assert after.max_attempts == 4
    assert after.state == "open"
    assert after.metadata[HOLD_REVIEW_KEY]["attempt_grants"] == 1


def test_reviewed_still_valid_is_durable_and_repeatable(tmp_path):
    cp = _real_plane(tmp_path)
    blocker = cp.create_task("still running", project="mac")
    held = cp.create_task(
        "parked work",
        project="mac",
        metadata={
            "no_dispatch": True,
            "hold": {"reason": "waiting", "until_task": blocker.id},
        },
    )

    sweeper = _sweeper(cp)
    sweeper.run_once()
    first = cp.get_task(held.id)
    assert first.metadata[HOLD_REVIEW_KEY]["verdict"] == VERDICT_REVIEWED_STILL_VALID
    assert first.metadata["no_dispatch"] is True

    # Second run: unchanged and inside the TTL, so it costs nothing and does
    # not rewrite the record.
    second_report = sweeper.run_once()
    assert second_report["examined"] == 0
    second = cp.get_task(held.id)
    assert second.metadata[HOLD_REVIEW_KEY] == first.metadata[HOLD_REVIEW_KEY]


def test_a_failed_close_does_not_leave_a_marker_claiming_it_closed():
    # A marker written before a close that then fails would say "cancelled"
    # about a task that is still parked, and the cheap skip would believe it
    # for a whole review TTL.
    cp = FakeCP(
        [
            _held(TASK_A, metadata={"hold": {"reason": "old", "replacement_task_id": TASK_C}}),
            FakeTask(TASK_C, state="open"),
        ]
    )

    def refusing_close(*_args, **_kwargs):
        raise RuntimeError("hub write failed")

    cp.close_task = refusing_close
    report = _sweeper(cp).run_once()

    assert report["errors"] == 1
    assert cp.reviews == []
    assert HOLD_REVIEW_KEY not in cp.tasks[TASK_A].metadata
    assert cp.tasks[TASK_A].state == "open"
