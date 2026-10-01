"""Reviews left mid-flight by the retired hub-verify run are decided on the next tick.

Before hub-verify was deleted, a task in REVIEWING held a pending review owned
by the virtual hub-reviewer while the hub re-ran the contract tests. Those
tasks must not strand: the next advance decides that same review from the
worker's validated evidence, or blocks with a reason when the evidence only
ever validated because a hub-side run was going to supply the test result.
"""

from mac.models import json_dumps
from mac.services import (
    DEFAULT_HUB_REVIEWER_AGENT_ID,
    ControlPlane,
    sign_verification_manifest,
)
from tests.conftest import submit_review_verdict, verifier_test_item

import pytest


def agent(cp, name, capabilities):
    machine = cp.register_machine(name + "-host", resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(
        machine.id,
        name,
        capabilities=capabilities,
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
            }
        },
    )


def _evidence(cp, task, worker, lease, head, tests):
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": head,
            "pushed": True,
            "remote_ref": "refs/heads/task/recovered",
            "dirty": False,
            "files_changed": ["src/feature.py"],
        },
        "tests": tests,
        "signed_by": worker.id,
    }
    key = cp._agent_attestation_key(worker.id)
    assert key
    manifest["signature"] = sign_verification_manifest(key, manifest)
    return cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result/" + head,
        "executor finished",
        worker.id,
        lease_id=lease.id,
        metadata={"returncode": 0, "verification": manifest},
    )


def submit_attempt(cp, task, worker, head):
    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id, lease_id=lease.id)
    evidence = _evidence(cp, task, worker, lease, head, [verifier_test_item(head)])
    cp.submit_for_review(task.id, worker.id, lease_id=lease.id)
    return evidence


@pytest.fixture
def cp():
    plane = ControlPlane.in_memory()
    try:
        yield plane
    finally:
        plane.store.close()


@pytest.fixture
def task(cp):
    return cp.create_task(
        "Recover an interrupted review",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://publish",
            "origin": {
                "repository_contract": {"canonical_remote_url": "git@github.com:org/repo.git"}
            },
        },
    )


def _hub_verify_era_pending_review(cp, task_id):
    """The state hub-verify left behind: REVIEWING, hub-reviewer review pending."""
    reviewer = cp._ensure_hub_reviewer_agent(actor="test")
    assert reviewer is not None and reviewer.id == DEFAULT_HUB_REVIEWER_AGENT_ID
    review = cp.request_review(task_id, reviewer.id)
    assert cp.get_task(task_id).state == "reviewing"
    assert review.status == "pending"
    return review


def test_pending_hub_verify_review_is_approved_from_worker_evidence(cp, task):
    worker = agent(cp, "executor", ["python"])
    evidence = submit_attempt(cp, task, worker, "a" * 40)
    review = _hub_verify_era_pending_review(cp, task.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == "completed"
    # The in-flight review itself is decided; no second review is opened.
    assert [(r.id, r.status) for r in cp.list_reviews(task.id)] == [(review.id, "approved")]
    verdict = cp.get_evidence(cp.get_review(review.id).evidence_id).metadata["verification"]
    assert verdict["verified_by"] == "worker_evidence_v1"
    assert verdict["reviewed_evidence_id"] == evidence.id
    assert verdict["review_id"] == review.id
    assert verdict["repo"]["head_sha"] == "a" * 40
    assert verdict["signed_by"] == DEFAULT_HUB_REVIEWER_AGENT_ID
    assert len(cp.list_publications(task.id)) == 1

    # Re-ticking is a no-op: still one review, one publication.
    assert cp.advance_default_review_workflow(task.id)["status"] == "already_published"
    assert len(cp.list_reviews(task.id)) == 1
    assert len(cp.list_publications(task.id)) == 1


def test_pending_hub_verify_review_with_deferred_tests_blocks_with_reason(cp, task):
    """Evidence that deferred its tests to hub-verify can never validate now.

    It must block with a reason on the next tick instead of waiting forever for
    a test run nothing will perform.
    """
    worker = agent(cp, "executor", ["python"])
    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id, lease_id=lease.id)
    deferred = _evidence(
        cp,
        task,
        worker,
        lease,
        "c" * 40,
        [
            {
                "name": "repository contract test",
                "command": "scripts/run-contract-tests.sh",
                "returncode": None,
                "status": "deferred",
                "execution_environment": "hub_verify_pending",
                "stdout": "",
                "stderr": "",
            }
        ],
    )
    _evidence(cp, task, worker, lease, "d" * 40, [verifier_test_item("d" * 40)])
    cp.submit_for_review(task.id, worker.id, lease_id=lease.id)
    # Bind the review to the deferred evidence, as the hub-verify era did
    # when it accepted deferred tests as "pending hub verify".
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["review_target"] = dict(
        metadata.get("review_target") or {}, executor_evidence_id=deferred.id
    )
    cp.store.execute("UPDATE tasks SET metadata = ? WHERE id = ?", (json_dumps(metadata), task.id))
    review = _hub_verify_era_pending_review(cp, task.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "blocked"
    assert result["reason"] == "review_evidence_not_verifiable"
    assert result["executor_evidence_id"] == deferred.id
    assert result["manual_repair_required"] is True
    assert cp.get_task(task.id).state == "blocked"
    assert cp.get_review(review.id).status == "retracted"
    assert not cp.list_publications(task.id)


def test_pending_review_by_another_reviewer_is_superseded(cp, task):
    worker = agent(cp, "executor", ["python"])
    legacy = agent(cp, "legacy-reviewer", ["review"])
    submit_attempt(cp, task, worker, "a" * 40)
    stale = cp.request_review(task.id, legacy.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_review(stale.id).status == "retracted"
    assert cp.get_review(stale.id).reason == "superseded_by_worker_evidence"
    approved = [r for r in cp.list_reviews(task.id) if r.status == "approved"]
    assert [r.reviewer_agent_id for r in approved] == [DEFAULT_HUB_REVIEWER_AGENT_ID]


def test_recovered_attempt_gets_a_fresh_verdict_for_its_own_evidence(cp, task):
    """An approval for the previous attempt does not carry over to a new one."""
    worker = agent(cp, "executor", ["python"])
    reviewer = agent(cp, "reviewer", ["review"])
    first = submit_attempt(cp, task, worker, "a" * 40)
    old_review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, first.id)
    cp.submit_review(old_review.id, "approved", reviewer.id, evidence_id=verdict_id)
    cp.stop_task(task.id, actor="operator", reason="recover interrupted review")
    cp.reopen_task(task.id, actor="operator", reason="submit a fresh attempt")
    second = submit_attempt(cp, task, worker, "b" * 40)
    assert cp.reviews.current_review_target_evidence_id(task.id) == second.id

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    reviews = cp.list_reviews(task.id)
    assert len(reviews) == 2
    fresh = next(r for r in reviews if r.id != old_review.id)
    assert fresh.status == "approved"
    verdict = cp.get_evidence(fresh.evidence_id).metadata["verification"]
    assert verdict["reviewed_evidence_id"] == second.id
    assert verdict["repo"]["head_sha"] == "b" * 40
    # The previous attempt's approval is history, untouched.
    assert cp.get_review(old_review.id).evidence_id == verdict_id
    assert len(cp.list_publications(task.id)) == 1
