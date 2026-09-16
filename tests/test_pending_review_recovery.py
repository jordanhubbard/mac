"""Recovered attempts reuse their pending review before independent verification."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from mac.services import ControlPlane, sign_verification_manifest


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


def submit_attempt(cp, task, worker, head):
    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id, lease_id=lease.id)
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
        "tests": [{"command": "pytest", "returncode": 0}],
        "signed_by": worker.id,
    }
    key = cp._agent_attestation_key(worker.id)
    assert key
    manifest["signature"] = sign_verification_manifest(key, manifest)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result/" + head,
        "executor finished",
        worker.id,
        lease_id=lease.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id, lease_id=lease.id)
    return evidence


@pytest.fixture
def recovered(monkeypatch, request):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    monkeypatch.delenv("MAC_REVIEW_SEMANTIC_REVIEWER", raising=False)
    cp = ControlPlane.in_memory()
    worker = agent(cp, "executor", ["python"])
    reviewer = agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Recover an interrupted review",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "repository_contract": {"canonical_remote_url": "git@github.com:org/repo.git"}
            },
        },
    )
    first = submit_attempt(cp, task, worker, "a" * 40)
    review = cp.request_review(task.id, reviewer.id)
    prior_status = getattr(request, "param", "pending")
    if prior_status == "approved":
        cp._hub_verify_runner = lambda *args: (0, "all passed")
        result = cp.advance_default_review_workflow(task.id)
        assert result["status"] == "waiting_for_publication_target"
        assert cp.get_review(review.id).status == "approved"
    cp.update_task(
        task.id, metadata={**cp.get_task(task.id).metadata, "publication_target": "test://publish"}
    )
    cp.stop_task(task.id, actor="operator", reason="recover interrupted review")
    cp.reopen_task(task.id, actor="operator", reason="submit a fresh attempt")
    second = submit_attempt(cp, task, worker, "b" * 40)
    current = cp.get_task(task.id)
    assert current.state == "needs_review"
    assert current.attempt_count == 2
    assert cp.reviews.current_review_target_evidence_id(task.id) == second.id
    assert first.id != second.id
    assert [(r.id, r.status) for r in cp.list_reviews(task.id)] == [(review.id, prior_status)]
    return cp, task, review, second


@pytest.mark.parametrize("returncode", [0, 1])
def test_recovered_review_records_current_attempt_verdict_before_publication(recovered, returncode):
    cp, task, review, evidence = recovered
    calls = []

    def runner(remote, branch, head, command):
        calls.append((cp.get_task(task.id).state, head))
        return returncode, "all passed" if returncode == 0 else "1 failed, 2 passed"

    cp._hub_verify_runner = runner
    assert not cp.list_publications(task.id)
    cp.advance_default_review_workflow(task.id)
    assert calls == [("reviewing", "b" * 40)]
    current = cp.get_review(review.id)
    assert current.reviewer_agent_id == review.reviewer_agent_id
    assert current.status == ("approved" if returncode == 0 else "rejected")
    verdict = cp.get_evidence(current.evidence_id).metadata["verification"]
    assert verdict["reviewed_evidence_id"] == evidence.id
    assert verdict["repo"]["head_sha"] == "b" * 40
    assert verdict["review_id"] == review.id
    assert verdict["signed_by"] == review.reviewer_agent_id
    assert verdict["signature"]
    assert len(cp.list_reviews(task.id)) == 1
    if returncode == 0:
        assert cp.get_task(task.id).state == "completed"
        assert len(cp.list_publications(task.id)) == 1
        cp.advance_default_review_workflow(task.id)
        assert len(calls) == 1
        assert len(cp.list_publications(task.id)) == 1
    else:
        assert cp.get_task(task.id).state != "completed"
        assert not cp.list_publications(task.id)


def test_recovered_review_repeated_advance_does_not_start_another_verifier(recovered):
    cp, task, review, evidence = recovered
    entered, release = Event(), Event()
    calls = []

    def runner(remote, branch, head, command):
        calls.append(head)
        entered.set()
        assert release.wait(10), "test did not release verifier boundary"
        return 0, "all passed"

    cp._hub_verify_runner = runner
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(cp.advance_default_review_workflow, task.id)
        try:
            assert entered.wait(10), "verifier did not start"
            assert cp.get_task(task.id).state == "reviewing"
            repeated = cp.advance_default_review_workflow(task.id)
            assert repeated["status"] == "waiting_for_hub_verify"
            assert [(r.id, r.status) for r in cp.list_reviews(task.id)] == [(review.id, "pending")]
            assert cp.reviews.current_review_target_evidence_id(task.id) == evidence.id
            assert calls == ["b" * 40]
            assert not cp.list_publications(task.id)
        finally:
            release.set()
        first.result(timeout=10)
    assert cp.get_task(task.id).state == "completed"
    assert len(cp.list_publications(task.id)) == 1
    assert calls == ["b" * 40]


def test_nonblocking_tick_reconciles_state_without_running_verification(recovered, monkeypatch):
    cp, task, review, evidence = recovered
    nudges, calls = [], []
    monkeypatch.setattr(cp, "_nudge_review_workflow", nudges.append)
    cp._hub_verify_runner = lambda *args: calls.append(args) or (0, "all passed")
    for _ in range(2):
        result = cp.advance_default_review_workflow(task.id, allow_blocking_hub_verify=False)
        assert result["status"] == "waiting_for_hub_verify"
        assert cp.get_task(task.id).state == "reviewing"
    assert not calls
    assert nudges == [task.id, task.id]
    assert cp.reviews.current_review_target_evidence_id(task.id) == evidence.id
    assert [(r.id, r.status) for r in cp.list_reviews(task.id)] == [(review.id, "pending")]
    assert not cp.list_publications(task.id)
    cp.advance_default_review_workflow(task.id)
    assert len(calls) == 1
    assert cp.get_task(task.id).state == "completed"


@pytest.mark.parametrize("recovered", ["approved"], indirect=True)
@pytest.mark.parametrize("returncode", [0, 1])
def test_recovered_approved_review_requires_a_fresh_verdict(recovered, monkeypatch, returncode):
    cp, task, old_review, evidence = recovered
    old_review = cp.get_review(old_review.id)
    old_verdict = cp.get_evidence(old_review.evidence_id).to_dict()
    calls = []
    monkeypatch.setattr(cp, "_nudge_review_workflow", lambda _: None)
    cp._hub_verify_runner = lambda *args: (
        calls.append(args[2])
        or (returncode, "all passed" if returncode == 0 else "1 failed, 2 passed")
    )
    for _ in range(2):
        result = cp.advance_default_review_workflow(task.id, allow_blocking_hub_verify=False)
        assert result["status"] == "waiting_for_hub_verify"
        assert cp.get_task(task.id).state == "reviewing"
        assert not cp.list_publications(task.id)
        assert not calls
    pending = [r for r in cp.list_reviews(task.id) if r.status == "pending"]
    assert len(pending) == 1 and pending[0].id != old_review.id
    cp.advance_default_review_workflow(task.id)
    assert calls == ["b" * 40]
    fresh = cp.get_review(pending[0].id)
    assert fresh.status == ("approved" if returncode == 0 else "rejected")
    verdict = cp.get_evidence(fresh.evidence_id).metadata["verification"]
    assert verdict["reviewed_evidence_id"] == evidence.id
    assert verdict["repo"]["head_sha"] == "b" * 40
    assert verdict["review_id"] == fresh.id
    assert verdict["signature"]
    assert cp.get_review(old_review.id).to_dict() == old_review.to_dict()
    assert cp.get_evidence(old_review.evidence_id).to_dict() == old_verdict
    assert len(cp.list_reviews(task.id)) == 2
    assert len(cp.list_publications(task.id)) == (1 if returncode == 0 else 0)
