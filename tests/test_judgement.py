"""Hub judgement: checklist findings become privileged interventions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.judgement import (
    Finding,
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
        machine.id,
        name,
        capabilities=capabilities or ["ops", "python", "review"],
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
        cp.submit_review(review.id, "rejected", reviewer.id, reason="no %d" % index)
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

    fake = Finding(
        kind="excessive_reviewing_population", summary="pile", recommended_action="fleet_stop"
    )
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


def test_terminal_and_already_stopped_findings_do_not_consume_action_budget(cp):
    failed = cp.create_task("failed historical row", project="mac")
    stopped = cp.create_task("already parked", project="mac")
    live = cp.create_task("live intervention", project="mac")
    with cp.store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", ("failed", failed.id))
    cp.stop_task(stopped.id, actor="test", reason="fixture")
    findings = [
        Finding(
            kind="old_failure",
            task_id=failed.id,
            summary="terminal",
            recommended_action="stop_task",
        ),
        Finding(
            kind="already_parked",
            task_id=stopped.id,
            summary="stopped",
            recommended_action="stop_task",
        ),
        Finding(
            kind="live_problem",
            task_id=live.id,
            summary="actionable",
            recommended_action="stop_task",
        ),
    ]

    actions = _process(cp, max_actions_per_cycle=1)._act_on_findings(
        findings, actor="test", run_id="budget"
    )

    assert [action["action"] for action in actions] == [
        "skipped",
        "already_stopped",
        "task_stopped",
    ]
    assert cp.get_task(live.id).state == TaskState.STOPPED.value


def test_merged_reconciliation_precedes_and_does_not_consume_intervention_budget(cp):
    first = cp.create_task("first intervention", project="mac")
    second = cp.create_task("second intervention", project="mac")
    merged = cp.create_task("already merged", project="mac")
    findings = [
        Finding(
            kind="ordinary_first",
            task_id=first.id,
            summary="first",
            recommended_action="stop_task",
        ),
        Finding(
            kind="ordinary_second",
            task_id=second.id,
            summary="second",
            recommended_action="stop_task",
        ),
        Finding(
            kind="merged_task_not_reconciled",
            task_id=merged.id,
            summary="merged",
            detail={
                "pr_number": 777,
                "url": "https://example.test/777",
                "base_ref_name": "main",
                "head_sha": "a" * 40,
                "merge_sha": "b" * 40,
            },
            recommended_action="reconcile_merged_task",
        ),
    ]

    actions = _process(cp, max_actions_per_cycle=1)._act_on_findings(
        findings, actor="test", run_id="merged-first"
    )

    assert [action["action"] for action in actions] == [
        "task_reconciled",
        "task_stopped",
        "skipped",
    ]
    assert actions[-1]["reason"] == "cycle_budget"
    assert cp.get_task(merged.id).state == TaskState.COMPLETED.value
    assert cp.get_task(first.id).state == TaskState.STOPPED.value
    assert cp.get_task(second.id).state == TaskState.OPEN.value


def test_merged_pull_request_reconciles_the_named_repository_task(cp):
    head_sha = "a" * 40
    merge_sha = "b" * 40
    task = cp.create_task(
        "merged out of band",
        project="mac",
        metadata={
            "execution_contract": {
                "type": "repository",
                "repository_contract": {"canonical_branch": "main"},
            }
        },
    )

    def lister(_root):
        return {
            "open": [],
            "merged": [
                {
                    "number": 776,
                    "title": "land repair (%s)" % task.id,
                    "body": "",
                    "headRefName": "codex/repair",
                    "baseRefName": "main",
                    "headRefOid": head_sha,
                    "mergeCommit": {"oid": merge_sha},
                    "mergedAt": "2026-09-07T23:03:55Z",
                    "url": "https://example.test/776",
                    "mergeable": "UNKNOWN",
                }
            ],
        }

    report = _process(cp, pr_lister=lister).run_once()

    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert any(action["action"] == "task_reconciled" for action in report["actions"])
    proofs = [
        evidence.metadata["verification"]["canonical_integration"]
        for evidence in cp.list_evidence(task.id)
        if evidence.metadata.get("verification", {}).get("canonical_integration")
    ]
    assert proofs[-1]["canonical_tip_sha"] == merge_sha
    assert proofs[-1]["reviewed_head_sha"] == head_sha


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


def test_open_pull_request_preserves_semantic_review_intervention(cp):
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
    assert "unlanded_pull_request" not in kinds
    assert "semantic_reviewer_still_assigned" in kinds
    assert closed == []
    assert cp.get_task(task.id).state == TaskState.STOPPED.value


@pytest.mark.parametrize("state", ["needs_review", "reviewing", "blocked", "failed"])
def test_open_pull_request_does_not_stop_pending_hub_review(cp, state):
    reviewer = _register_agent(cp, "hub-reviewer", resources={"virtual": True})
    task = _park_in_review(cp, "independent verification", reviewer)
    with cp.store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task.id))

    report = _process(
        cp,
        pr_lister=lambda _root: {
            "open": [{"number": 803, "title": task.id}],
            "merged": [],
        },
    ).run_once()
    kinds = [finding["kind"] for finding in report["findings"]]
    assert ("unlanded_pull_request" in kinds) == (state in {"blocked", "failed"})
    assert cp.get_task(task.id).state == ("stopped" if state == "blocked" else state)


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
