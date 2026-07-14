"""Tests: reviewer protocol/infrastructure failures must not re-run executor patches.

Invariants under test
---------------------
1. A reviewer protocol failure (missing semantic verdict, blind-protocol
   non-compliance, nonzero reviewer returncode) retracts the review but
   does NOT increment ``task.attempt_count`` — executor budget is preserved.

2. Multiple successive reviewer protocol failures still keep the task in
   ``needs_review`` / ``reviewing`` state and never create divergent
   branches (the task does not go back to OPEN with a fresh executor
   claim in between).

3. After protocol failures the workflow either reassigns a different eligible
   reviewer or blocks the task for repair — it does not re-execute the
   executor patch.

4. ``build_observation`` records separate ``executor_attempt_count`` and
   ``review_attempt_count`` totals so the two kinds of budget expenditure
   are independently visible in experiment reports.
"""

from __future__ import annotations

import json

import pytest

from mac.models import ReviewStatus, TaskState
from mac.review_experiments import (
    ASSIGNMENT_SCHEMA,
    build_assignment,
    build_observation,
    build_outcome,
    append_outcome,
    build_report,
)
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Helpers (mirrors pattern from test_control_plane.py)
# ---------------------------------------------------------------------------


def _cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name, capabilities=None):
    capabilities = capabilities or []
    resources = {}
    if "python" in capabilities:
        resources["commands"] = {
            "schema": "mac.command_inventory.v1",
            "available": ["python3", "git", "gh"],
        }
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=capabilities, resources=resources)


def _verified_repo_metadata(cp, agent_id, head_sha="abcdef1234567890abcdef1234567890abcdef12"):
    from mac.services import sign_verification_manifest
    from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA, codegraph_relevant_files

    files = ["src/mac/example.py"]
    relevant = codegraph_relevant_files(files)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": head_sha,
            "pushed": True,
            "remote_ref": "refs/heads/task/example",
            "dirty": False,
            "files_changed": files,
        },
        "tests": [{"name": "contract", "returncode": 0}],
        "llm": {"model": "test-executor-model", "family": "test", "provider": "test"},
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test",
            "relevant_files": relevant,
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected", "src/mac/example.py"], "returncode": 0},
            ],
        },
    }
    key = cp._agent_attestation_key(agent_id)
    if key:
        manifest["signed_by"] = agent_id
        manifest["signature"] = sign_verification_manifest(key, manifest)
    return {"returncode": 0, "verification": manifest}


# ---------------------------------------------------------------------------
# Core invariant 1 & 2: protocol failure must not consume executor attempts
# ---------------------------------------------------------------------------


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def test_reviewer_protocol_failure_does_not_increment_executor_attempt_count(cp):
    """task.attempt_count must stay at 1 after a reviewer produces a nonzero
    returncode (infrastructure failure). The task goes back to NEEDS_REVIEW so
    a new reviewer can be assigned — no re-claim of the task by an executor.
    """
    worker = _register_agent(cp, "worker", ["python"])
    _register_agent(cp, "reviewer-a", ["review"])
    _register_agent(cp, "reviewer-b", ["review"])
    task = cp.create_task("Protocol rejection invariant", required_capabilities=["python"], max_attempts=3)

    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    assert cp.get_task(task.id).attempt_count == 1

    executor_ev = cp.add_evidence(
        task.id, "test", "file://repo", "executor finished",
        worker.id, metadata=_verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    # Assign first reviewer
    cp.advance_default_review_workflow(task.id)
    pending = [r for r in cp.list_reviews(task.id) if r.status == ReviewStatus.PENDING.value]
    assert len(pending) == 1
    reviewer_a_id = pending[0].reviewer_agent_id

    # Reviewer produces a protocol-failure evidence (nonzero returncode)
    cp.add_evidence(
        task.id, "review", "file://review-failed",
        "reviewer harness crashed",
        reviewer_a_id,
        metadata={
            "returncode": 65,
            "review_id": pending[0].id,
            "executor_evidence_id": executor_ev.id,
        },
    )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "reviewer_protocol_failed"

    # KEY ASSERTION: executor attempt_count must be unchanged at 1
    task_after = cp.get_task(task.id)
    assert task_after.attempt_count == 1, (
        "reviewer protocol failure must not consume an executor attempt slot; "
        "got attempt_count=%d" % task_after.attempt_count
    )

    # Task must still be in needs_review / reviewing — NOT back to open
    assert task_after.state in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}, (
        "task must stay in review state after reviewer protocol failure, not go to open; "
        "got state=%s" % task_after.state
    )


def test_multiple_reviewer_protocol_failures_do_not_create_divergent_branches(cp):
    """Two consecutive reviewer protocol failures (different reviewers) must
    NOT move the task to OPEN, preventing a second executor claim that would
    create a divergent branch with a different patch.

    After both failures the workflow must either reassign (if a third eligible
    reviewer exists) or block for repair.  It must NEVER leave the task OPEN.
    """
    worker = _register_agent(cp, "worker", ["python"])
    reviewer_a = _register_agent(cp, "reviewer-alpha", ["review"])
    reviewer_b = _register_agent(cp, "reviewer-beta", ["review"])

    task = cp.create_task(
        "No-divergent-branches",
        required_capabilities=["python"],
        max_attempts=3,
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)

    executor_ev = cp.add_evidence(
        task.id, "test", "file://repo-1", "executor done",
        worker.id, metadata=_verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    attempt_count_after_executor = cp.get_task(task.id).attempt_count

    def _fail_reviewer(reviewer_id):
        """Advance, find the pending review, inject a protocol-failure evidence,
        advance again to trigger retraction.  Returns the advance result."""
        cp.advance_default_review_workflow(task.id)
        pending = [
            r for r in cp.list_reviews(task.id)
            if r.status == ReviewStatus.PENDING.value
        ]
        if not pending:
            return None
        review = pending[0]
        cp.add_evidence(
            task.id, "review", "file://fail-%s" % reviewer_id,
            "protocol failure",
            reviewer_id,
            metadata={
                "returncode": 65,
                "review_id": review.id,
                "executor_evidence_id": executor_ev.id,
            },
        )
        return cp.advance_default_review_workflow(task.id)

    result_a = _fail_reviewer(reviewer_a.id)
    assert result_a is not None and result_a["status"] == "reviewer_protocol_failed"

    # After first reviewer failure: task MUST NOT be OPEN
    task_mid = cp.get_task(task.id)
    assert task_mid.state != TaskState.OPEN.value, (
        "task must not go to OPEN after first reviewer protocol failure; "
        "state=%s" % task_mid.state
    )
    assert task_mid.attempt_count == attempt_count_after_executor, (
        "attempt_count must not change after reviewer protocol failure; "
        "expected %d, got %d" % (attempt_count_after_executor, task_mid.attempt_count)
    )

    result_b = _fail_reviewer(reviewer_b.id)
    # With no more eligible reviewers, the task should block (or return a
    # meaningful blocked/exhausted status) — it must never be OPEN.
    task_final = cp.get_task(task.id)
    assert task_final.state != TaskState.OPEN.value, (
        "task must not go to OPEN after second reviewer protocol failure; "
        "state=%s" % task_final.state
    )
    assert task_final.attempt_count == attempt_count_after_executor, (
        "attempt_count must not change after second reviewer protocol failure; "
        "expected %d, got %d" % (attempt_count_after_executor, task_final.attempt_count)
    )


def test_protocol_failure_retries_reviewer_not_executor(cp):
    """When a reviewer fails with a protocol error, the workflow assigns a
    new eligible reviewer on the very next advance — it does NOT open a new
    executor slot.  The executor evidence is preserved and re-used.
    """
    worker = _register_agent(cp, "worker", ["python"])
    reviewer_a = _register_agent(cp, "reviewer-a", ["review"])
    reviewer_b = _register_agent(cp, "reviewer-b", ["review"])

    task = cp.create_task("Retry reviewer not executor", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)

    executor_ev = cp.add_evidence(
        task.id, "test", "file://repo", "executor done",
        worker.id, metadata=_verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    # First advance assigns reviewer-a
    cp.advance_default_review_workflow(task.id)
    pending = [r for r in cp.list_reviews(task.id) if r.status == ReviewStatus.PENDING.value]
    assert len(pending) == 1
    assert pending[0].reviewer_agent_id == reviewer_a.id

    # reviewer-a protocol fails
    cp.add_evidence(
        task.id, "review", "file://fail",
        "reviewer crashed",
        reviewer_a.id,
        metadata={
            "returncode": 65,
            "review_id": pending[0].id,
            "executor_evidence_id": executor_ev.id,
        },
    )
    fail_result = cp.advance_default_review_workflow(task.id)
    assert fail_result["status"] == "reviewer_protocol_failed"

    # Next advance should assign reviewer-b (a different peer), not re-run executor
    reassign_result = cp.advance_default_review_workflow(task.id)
    assert reassign_result["status"] in {
        "waiting_for_reviewer_verdict",
        "waiting_for_reviewer",
    }, "expected workflow to reassign a reviewer, got: %s" % reassign_result["status"]

    new_pending = [r for r in cp.list_reviews(task.id) if r.status == ReviewStatus.PENDING.value]
    if reassign_result["status"] == "waiting_for_reviewer_verdict":
        # A new reviewer was assigned — confirm it's reviewer-b, not a re-run executor
        assert len(new_pending) == 1
        assert new_pending[0].reviewer_agent_id == reviewer_b.id, (
            "reassigned reviewer must be reviewer-b, got %s" % new_pending[0].reviewer_agent_id
        )

    # Executor evidence is unchanged — same ID, not re-submitted
    all_evidence = cp.list_evidence(task.id)
    executor_evidence_items = [
        e for e in all_evidence
        if (e.metadata.get("verification") or {}).get("evidence_type") == "repo_change"
    ]
    assert len(executor_evidence_items) == 1, (
        "exactly one executor evidence item must exist after reviewer protocol failure; "
        "got %d" % len(executor_evidence_items)
    )
    assert executor_evidence_items[0].id == executor_ev.id, (
        "executor evidence ID must not change after reviewer protocol failure"
    )


def test_semantic_verdict_missing_triggers_protocol_failure_not_executor_retry(cp):
    """If the reviewer produces a review_verdict evidence but with a missing or
    invalid semantic_verdict (e.g. 'unknown'), the workflow must treat it as a
    protocol failure — not a semantic rejection — and NOT re-run the executor.
    """
    from mac.services import sign_verification_manifest

    worker = _register_agent(cp, "worker", ["python"])
    reviewer = _register_agent(cp, "reviewer", ["review"])

    task = cp.create_task("Semantic verdict missing", required_capabilities=["python"], max_attempts=3)
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)

    executor_ev = cp.add_evidence(
        task.id, "test", "file://repo", "done",
        worker.id, metadata=_verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    cp.advance_default_review_workflow(task.id)
    pending = [r for r in cp.list_reviews(task.id) if r.status == ReviewStatus.PENDING.value]
    assert pending

    # Reviewer produces evidence with review_verdict type but invalid semantic_verdict
    bad_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "semantic_verdict": "unknown",  # invalid — not 'approved' or 'rejected'
        "reviewed_evidence_id": executor_ev.id,
        "review_id": pending[0].id,
        "llm": {"model": "test-reviewer-llm"},
    }
    key = cp._agent_attestation_key(reviewer.id)
    if key:
        bad_manifest["signed_by"] = reviewer.id
        bad_manifest["signature"] = sign_verification_manifest(key, bad_manifest)

    cp.add_evidence(
        task.id, "review", "file://bad-verdict",
        "invalid semantic_verdict",
        reviewer.id,
        metadata={
            "returncode": 0,
            "review_id": pending[0].id,
            "executor_evidence_id": executor_ev.id,
            "verification": bad_manifest,
        },
    )

    result = cp.advance_default_review_workflow(task.id)
    # The workflow must recognise this as a protocol failure (semantic_verdict_invalid)
    # rather than a semantic rejection that causes the executor patch to re-run.
    assert result["status"] == "reviewer_protocol_failed"
    assert result["reason"] == "semantic_verdict_invalid"

    # attempt_count unchanged — this did NOT count as a task execution attempt
    assert cp.get_task(task.id).attempt_count == 1

    # Task stays in review state, not OPEN
    state = cp.get_task(task.id).state
    assert state in {TaskState.NEEDS_REVIEW.value, TaskState.REVIEWING.value}, (
        "task must remain in review state after semantic_verdict_invalid; got %s" % state
    )


# ---------------------------------------------------------------------------
# Core invariant 4: experiment observation counters
# ---------------------------------------------------------------------------


def test_build_observation_records_separate_executor_and_review_attempt_counts():
    """executor_attempt_count and review_attempt_count must be recorded as
    distinct totals in the observation so the two kinds of budget expenditure
    are independently visible.

    Scenario: one executor run produced one approved verdict, plus two
    retracted reviews (protocol failures). executor_attempt_count = 1,
    review_attempt_count = 3 (2 retracted + 1 approved).
    """
    assignment = build_assignment(
        task_id="task_counter_test",
        experiment_id="exp-budget",
        arm="standard",
    )
    detail: dict = {
        "task": {
            "id": "task_counter_test",
            "project": "mac",
            "state": "completed",
            "attempt_count": 1,  # one executor run
            "metadata": {"review_experiment": assignment},
        },
        "evidence": [
            # Executor evidence
            {
                "id": "ev_executor",
                "metadata": {
                    "verification": {
                        "evidence_type": "repo_change",
                        "llm": {"model": "gpt-4o", "family": "gpt", "provider": "openai"},
                    }
                },
            },
            # Two failed review attempts (no review_verdict evidence_type, just protocol noise)
            {
                "id": "ev_review_fail_1",
                "metadata": {"returncode": 65, "review_id": "r_fail_1"},
            },
            {
                "id": "ev_review_fail_2",
                "metadata": {"returncode": 65, "review_id": "r_fail_2"},
            },
            # Valid approved verdict evidence
            {
                "id": "ev_verdict",
                "created_at": "2026-07-05T10:00:00+00:00",
                "metadata": {
                    "verification": {
                        "evidence_type": "review_verdict",
                        "reviewed_evidence_id": "ev_executor",
                        "verdict": "approved",
                        "semantic_verdict": "approved",
                        "review_id": "r_approved",
                        "llm": {"model": "claude-sonnet-4.5", "family": "claude", "provider": "anthropic"},
                        "review_experiment": {
                            **assignment,
                            "protocol": {"schema": "mac.review_protocol.v1", "protocol_compliant": True},
                        },
                    }
                },
            },
        ],
        "reviews": [
            # Two retracted reviews (protocol failures)
            {"id": "r_fail_1", "reviewer_agent_id": "agent_a", "status": "retracted",
             "created_at": "2026-07-05T09:00:00+00:00"},
            {"id": "r_fail_2", "reviewer_agent_id": "agent_b", "status": "retracted",
             "created_at": "2026-07-05T09:30:00+00:00"},
            # Approved review
            {"id": "r_approved", "reviewer_agent_id": "agent_c", "status": "approved",
             "evidence_id": "ev_verdict", "created_at": "2026-07-05T09:45:00+00:00"},
        ],
        "publications": [],
    }

    obs = build_observation(detail)
    totals = obs["totals"]

    assert totals["executor_attempt_count"] == 1, (
        "executor_attempt_count should be 1 (task.attempt_count); got %d"
        % totals["executor_attempt_count"]
    )
    assert totals["review_attempt_count"] == 3, (
        "review_attempt_count should be 3 (2 retracted + 1 approved review records); got %d"
        % totals["review_attempt_count"]
    )


def test_build_observation_falls_back_to_evidence_count_when_attempt_count_missing():
    """When the task dict lacks attempt_count (e.g. partial hub responses),
    executor_attempt_count is derived by counting evidence rows whose
    verification.evidence_type is an executor type.
    """
    assignment = build_assignment(
        task_id="task_fallback",
        experiment_id="exp-fallback",
        arm="standard",
    )
    detail: dict = {
        "task": {
            "id": "task_fallback",
            "project": "mac",
            "state": "completed",
            # attempt_count intentionally absent
            "metadata": {"review_experiment": assignment},
        },
        "evidence": [
            {
                "id": "ev_exec_1",
                "metadata": {
                    "verification": {"evidence_type": "repo_change"}
                },
            },
            {
                "id": "ev_exec_2",
                "metadata": {
                    "verification": {"evidence_type": "repo_change"}
                },
            },
            {
                "id": "ev_verdict",
                "created_at": "2026-07-05T10:00:00+00:00",
                "metadata": {
                    "verification": {
                        "evidence_type": "review_verdict",
                        "reviewed_evidence_id": "ev_exec_2",
                        "verdict": "approved",
                        "semantic_verdict": "approved",
                        "review_id": "r1",
                        "llm": {"model": "claude-sonnet", "family": "claude", "provider": "anthropic"},
                        "review_experiment": {
                            **assignment,
                            "protocol": {"schema": "mac.review_protocol.v1", "protocol_compliant": True},
                        },
                    }
                },
            },
        ],
        "reviews": [
            {"id": "r1", "reviewer_agent_id": "agent_r", "status": "approved",
             "evidence_id": "ev_verdict", "created_at": "2026-07-05T10:00:00+00:00"},
        ],
        "publications": [],
    }

    obs = build_observation(detail)
    totals = obs["totals"]

    # Two repo_change evidence items → executor_attempt_count = 2 (derived)
    assert totals["executor_attempt_count"] == 2
    assert totals["review_attempt_count"] == 1


def test_build_report_aggregates_executor_and_review_attempt_counts():
    """build_report must sum executor_attempt_count and review_attempt_count
    across observations per arm, so per-arm cost analysis is possible.
    """
    assignment = build_assignment(
        task_id="t1",
        experiment_id="exp-agg",
        arm="blind",
        blind=True,
    )

    def _obs(task_id, executor_count, review_count):
        return {
            "schema": "mac.review_observation.v1",
            "task_id": task_id,
            "task_state": "completed",
            "terminal": True,
            "sample_valid": True,
            "protocol_invalidations": [],
            "created_at": "",
            "completed_at": "",
            "experiment": assignment,
            "review_passes": [],
            "pending_reviews": [],
            "outcomes": [],
            "totals": {
                "review_passes": 1,
                "findings": 0,
                "independent_findings": 0,
                "confirmed_findings": 0,
                "refuted_findings": 0,
                "unresolved_findings": 0,
                "escaped_defects": 0,
                "protocol_invalidations": 0,
                "executor_attempt_count": executor_count,
                "review_attempt_count": review_count,
            },
        }

    observations = [
        _obs("t1", executor_count=1, review_count=3),
        _obs("t2", executor_count=2, review_count=2),
    ]

    report = build_report("exp-agg", observations, min_tasks_per_arm=1, min_validated_outcomes_per_arm=0)
    arms = {arm["arm"]: arm for arm in report["arms"]}
    assert "blind" in arms
    blind_arm = arms["blind"]

    assert blind_arm["executor_attempt_count"] == 3  # 1+2
    assert blind_arm["review_attempt_count"] == 5    # 3+2


# ---------------------------------------------------------------------------
# Regression guard: _manifest_is_complete rejects noncompliant blind verdict
# ---------------------------------------------------------------------------


def test_manifest_is_complete_rejects_noncompliant_blind_protocol_verdict(tmp_path):
    """A review_verdict manifest from a blind assignment whose protocol is
    non-compliant must NOT be treated as a complete, salvageable manifest.
    This prevents protocol-noncompliant evidence from masquerading as a
    real review pass and consuming an executor attempt slot via the
    evidence-salvage path.
    """
    import mac.task_executor as te

    # Simulate blind review that produced a verdict evidence but the blind
    # protocol was not completed (e.g. reviewer saw executor evidence before
    # writing independent findings).
    manifest = {
        "status": "complete",
        "evidence_type": "review_verdict",
        "semantic_verdict": "approved",
        "review_experiment": {
            "blind": True,
            "protocol": {"protocol_compliant": False, "problem": "findings file absent"},
        },
    }
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert te._manifest_is_complete(tmp_path) is False, (
        "_manifest_is_complete must return False for a blind-noncompliant verdict"
    )

    # A compliant blind verdict IS complete
    manifest["review_experiment"]["protocol"]["protocol_compliant"] = True
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert te._manifest_is_complete(tmp_path) is True


# ---------------------------------------------------------------------------
# mac-s2vz followup: a routine attestation-key rotation must not invalidate a
# verdict validly signed BEFORE the rotation (the fleet-completion bottleneck:
# re-keyed agents wedged in-flight reviews under "signed under rotated key").
# ---------------------------------------------------------------------------


def test_attestation_rotation_retains_prev_key_for_pre_rotation_verdicts(cp):
    """Rotation retains the previous key and the verifier accepts a pre-rotation
    signature against the key that was active at signing time; the retention is
    bounded to a single previous key."""
    from mac.services import (
        sign_verification_manifest,
        verify_verification_manifest_signature,
    )

    agent = _register_agent(cp, "reviewer-rot", capabilities=["python"])
    key1 = cp._agent_attestation_key(agent.id)
    assert key1

    manifest = {
        "schema": "mac.worker_evidence.v1",
        "evidence_type": "review_verdict",
        "status": "complete",
        "nonce": "abc123",
    }
    sig = sign_verification_manifest(key1, manifest)
    assert verify_verification_manifest_signature(key1, manifest, sig)

    # Rotate: the current key changes and the previous key is retained.
    key2 = cp.rotate_agent_attestation_key(agent.id)
    assert key2 and key2 != key1
    assert cp._agent_attestation_key(agent.id) == key2
    assert cp._agent_attestation_prev_key(agent.id) == key1

    # The pre-rotation signature no longer verifies against the CURRENT key,
    # but DOES verify against the retained previous (signing-time) key.
    assert not verify_verification_manifest_signature(key2, manifest, sig)
    assert verify_verification_manifest_signature(
        cp._agent_attestation_prev_key(agent.id), manifest, sig
    )

    # Bounded to a single previous key: a second rotation drops key1.
    key3 = cp.rotate_agent_attestation_key(agent.id)
    assert key3 and key3 not in (key1, key2)
    assert cp._agent_attestation_prev_key(agent.id) == key2
    assert cp._agent_attestation_prev_key(agent.id) != key1
