"""Hub judgement: checklist findings become privileged interventions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.judgement import (
    HOLD_REASON_PREFIX,
    JUDGEMENT_SCHEMA,
    JudgementConfig,
    JudgementProcess,
)
from mac.models import TaskState
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name="rocky", capabilities=None, resources=None):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(
        machine.id, name, capabilities=capabilities or ["ops", "python", "review"],
        resources=resources,
    )


def _park_in_review(cp, title, reviewer, *, reject_times=0):
    """Put a task in REVIEWING with a pending (or rejected) review.

    Judgement inspects ledger state, not the full executor evidence contract,
    so these tests skip submit_for_review.
    """
    task = cp.create_task(title, project="mac")
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.REVIEWING.value, task.id),
        )
    for index in range(reject_times):
        review = cp.request_review(task.id, reviewer.id)
        cp.submit_review(
            review.id, "rejected", reviewer.id, reason="no %d" % index
        )
        with cp.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET state = ? WHERE id = ?",
                (TaskState.REVIEWING.value, task.id),
            )
    if reject_times == 0:
        cp.request_review(task.id, reviewer.id)
    return cp.get_task(task.id)


def _process(
    cp,
    environ=None,
    redeploy_runner=None,
    pr_lister=None,
    pr_closer=None,
    now=None,
    **overrides,
):
    config = JudgementConfig(enabled=True, **overrides)
    return JudgementProcess(
        cp,
        config,
        environ=environ or {},
        redeploy_runner=redeploy_runner,
        pr_lister=pr_lister or (lambda _root: {"open": [], "merged": []}),
        pr_closer=pr_closer or (lambda *_args: {"returncode": 0, "skipped": True}),
        now=now,
    )


def test_config_disabled_by_default_and_validates_numbers():
    assert JudgementConfig.from_env({}).active is False
    on = JudgementConfig.from_env({"MAC_JUDGEMENT_ENABLED": "1"})
    assert on.active is True
    assert on.interval_seconds == 3600.0
    bad = JudgementConfig.from_env(
        {"MAC_JUDGEMENT_ENABLED": "1", "MAC_JUDGEMENT_INTERVAL_SECONDS": "nope"}
    )
    assert bad.active is False
    assert "MAC_JUDGEMENT_INTERVAL_SECONDS" in bad.configuration_error


def test_start_refuses_when_inactive(cp):
    process = JudgementProcess(cp, JudgementConfig(enabled=False))
    assert process.start() is False


def test_review_rejection_loop_stops_the_task(cp):
    reviewer = _register_agent(cp, "reviewer")
    task = _park_in_review(cp, "looping review", reviewer, reject_times=2)

    report = _process(cp).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "review_rejection_loop" in kinds
    assert cp.get_task(task.id).state == TaskState.STOPPED.value
    assert any(action["action"] == "task_stopped" for action in report["actions"])


def test_failed_dependency_deadlock_stops_the_child(cp):
    parent = cp.create_task("failed adr", project="mac")
    child = cp.create_task(
        "blocked release work",
        project="mac",
        dependencies=[parent.id],
    )
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.FAILED.value, parent.id),
        )
    assert cp.get_task(parent.id).state == TaskState.FAILED.value
    assert cp.get_task(child.id).state in {
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
    }

    report = _process(cp).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "failed_dependency_deadlock" in kinds
    assert cp.get_task(child.id).state == TaskState.STOPPED.value


def test_semantic_reviewer_assignment_stops_the_task(cp):
    reviewer = _register_agent(cp, "bullwinkle")
    task = _park_in_review(cp, "needs a real review", reviewer)

    report = _process(cp).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "semantic_reviewer_still_assigned" in kinds
    assert cp.get_task(task.id).state == TaskState.STOPPED.value


def test_stuck_reviewing_holds_the_semantic_reviewer(cp):
    reviewer = _register_agent(cp, "natasha")
    task = _park_in_review(cp, "parked in review", reviewer)
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (stale, task.id),
        )

    report = _process(cp, reviewing_stuck_seconds=60.0).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "stuck_reviewing" in kinds
    held = cp.get_agent(reviewer.id)
    assert bool(getattr(held, "dispatch_hold", False)) is True
    assert str(getattr(held, "dispatch_hold_reason", "")).startswith(HOLD_REASON_PREFIX)


def test_excessive_reviewing_stops_the_fleet_and_can_redeploy(cp):
    worker = _register_agent(cp, "worker")
    reviewer = _register_agent(cp, "reviewer")
    for index in range(3):
        _park_in_review(cp, "review pile %d" % index, reviewer)

    redeploys = []

    def runner(command, repo_root):
        redeploys.append((list(command), repo_root))
        return {"returncode": 0}

    process = _process(
        cp,
        redeploy_runner=runner,
        excessive_reviewing_count=2,
        excessive_reviewing_fraction=0.01,
        repo_root="/tmp/mac-judgement",
        redeploy_command="/bin/true",
    )
    report = process.run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "excessive_reviewing_population" in kinds
    assert any(action["action"] == "fleet_stopped" for action in report["actions"])


def test_redeploy_is_bounded_per_day(cp):
    calls = []

    def runner(command, repo_root):
        calls.append(1)
        return {"returncode": 0}

    clock = {"now": datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)}

    def now():
        return clock["now"]

    process = _process(
        cp,
        redeploy_runner=runner,
        now=now,
        max_redeploys_per_day=1,
        repo_root="/tmp/mac-judgement",
        redeploy_command="/bin/true",
    )
    finding = process._check_excessive_reviewing_population
    # Drive redeploy directly so the test does not depend on a live pile-up.
    from mac.judgement import Finding

    fake = Finding(kind="excessive_reviewing_population", summary="pile", recommended_action="fleet_stop")
    first = process._redeploy(actor="test", run_id="one", finding=fake)
    second = process._redeploy(actor="test", run_id="two", finding=fake)
    assert first["action"] == "redeployed"
    assert second["action"] == "skipped"
    assert second["reason"] == "redeploy_daily_budget"
    assert len(calls) == 1


def test_cycle_budget_caps_interventions(cp):
    reviewer = _register_agent(cp, "reviewer")
    for index in range(4):
        _park_in_review(cp, "loop %d" % index, reviewer, reject_times=2)

    report = _process(cp, max_actions_per_cycle=1).run_once()
    stopped = [action for action in report["actions"] if action["action"] == "task_stopped"]
    skipped = [action for action in report["actions"] if action.get("reason") == "cycle_budget"]
    assert len(stopped) == 1
    assert skipped


def test_orphaned_pull_request_is_closed(cp):
    task = cp.create_task("already done", project="mac")
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.COMPLETED.value, task.id),
        )
    closed = []

    def lister(_root):
        return {
            "open": [
                {
                    "number": 587,
                    "title": "Qdrant leftover (%s)" % task.id,
                    "body": "",
                    "headRefName": "mac/%s" % task.id,
                    "url": "https://example.test/587",
                    "mergeable": "MERGEABLE",
                }
            ],
            "merged": [],
        }

    def closer(number, comment, _root):
        closed.append((number, comment))
        return {"returncode": 0}

    report = _process(cp, pr_lister=lister, pr_closer=closer).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "orphaned_pull_request" in kinds
    assert closed == [(587, closed[0][1])]
    assert "orphaned_pull_request" in closed[0][1]


def test_unlanded_pull_request_stops_review_but_does_not_close(cp):
    reviewer = _register_agent(cp, "bullwinkle")
    task = _park_in_review(cp, "good work never landed", reviewer)
    closed = []

    def lister(_root):
        return {
            "open": [
                {
                    "number": 643,
                    "title": "Docs audit (%s)" % task.id,
                    "body": "",
                    "headRefName": "mac/%s" % task.id,
                    "url": "https://example.test/643",
                    "mergeable": "MERGEABLE",
                }
            ],
            "merged": [],
        }

    def closer(number, comment, _root):
        closed.append(number)
        return {"returncode": 0}

    report = _process(cp, pr_lister=lister, pr_closer=closer).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "unlanded_pull_request" in kinds
    assert closed == []
    assert cp.get_task(task.id).state == TaskState.STOPPED.value


def test_duplicate_open_prs_close_the_older_copy(cp):
    task = cp.create_task("stop wrappers", project="mac")
    closed = []

    def lister(_root):
        return {
            "open": [
                {
                    "number": 641,
                    "title": "stop/restart (%s)" % task.id,
                    "body": "",
                    "headRefName": "a",
                    "url": "https://example.test/641",
                },
                {
                    "number": 642,
                    "title": "stop/restart again (%s)" % task.id,
                    "body": "",
                    "headRefName": "b",
                    "url": "https://example.test/642",
                },
            ],
            "merged": [],
        }

    def closer(number, comment, _root):
        closed.append(number)
        return {"returncode": 0}

    report = _process(cp, pr_lister=lister, pr_closer=closer).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert "duplicate_pull_request" in kinds
    assert closed == [641]


def test_status_binds_the_checklist_skill(cp):
    process = _process(cp, repo_root=str(Path(__file__).resolve().parents[1]))
    status = process.status()
    assert status["schema"] == JUDGEMENT_SCHEMA
    assert status["skill"]["present"] is True
    assert "review_rejection_loop" in status["skill"]["checklist_kinds"]
    assert status["skill"]["missing_kinds"] == []


def test_fleet_start_resumes_only_judgement_holds(cp):
    worker = _register_agent(cp, "worker")
    operator = _register_agent(cp, "operator-session")
    cp.set_agent_dispatch_hold(worker.id, "%sfleet_stop:run" % HOLD_REASON_PREFIX)
    cp.set_agent_dispatch_hold(operator.id, "Interactive session; do not dispatch")
    from mac.judgement import Finding

    process = _process(cp)
    process._fleet_start(
        actor="test",
        run_id="run",
        finding=Finding(kind="excessive_reviewing_population", summary="x"),
    )
    assert bool(cp.get_agent(worker.id).dispatch_hold) is False
    assert bool(cp.get_agent(operator.id).dispatch_hold) is True
