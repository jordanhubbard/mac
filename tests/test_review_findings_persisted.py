"""A review must record WHAT the reviewer said, not only which way it voted.

`reviews.reason` is a caller-chosen template ("reviewer rejected via signed
verdict evidence"), so the durable review row used to carry a boolean and
nothing else. Sampling the live ledger on 2026-08-17 returned **52 reviews
across 38 tasks and exactly four distinct reason strings**:

    27  reviewer approved via signed verdict evidence
    22  reviewer rejected via signed verdict evidence
     2  reviewer_unavailable:reviewer_not_available
     1  reviewer_unavailable:reviewer_stale

Not one carried a finding. That makes the only question worth asking about a
reviewer -- does it make corrections that improve the result? -- unanswerable
from the ledger: a review that caught a real defect is indistinguishable from
one that rubber-stamped, and from one that merely relayed a harness failure.

The evidence already held `summary`, `feedback` and `findings`; they were
extracted into task metadata and dropped from the review row. These tests pin
that they now survive on the row itself, and that the derived counters needed
to measure reviewer value are present.
"""

from __future__ import annotations

import pytest

from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane
from tests.test_control_plane import (
    CODEGRAPH_AUDIT_SCHEMA,
    _sign,
    codegraph_relevant_files,
    register_agent,
    verified_repo_metadata,
)


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _to_review(cp, name):
    executor = register_agent(cp, "%s-exec" % name, ["python"])
    reviewer = register_agent(cp, "%s-rev" % name, ["review"])
    task = cp.create_task(name, required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://%s" % name,
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    review = cp.request_review(task.id, reviewer.id, actor="manual")
    return reviewer, task, review, evidence


def _verdict(cp, reviewer, task, review, evidence, *, status, verification):
    manifest = _sign(
        cp,
        reviewer.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "reviewed_evidence_id": evidence.id,
            "executor_evidence_id": evidence.id,
            "returncode": 0,
            "worktree_digest": "sha256:" + ("c" * 64),
            "repo": {
                "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
                "pushed": True,
                "remote_ref": "refs/heads/task/example",
                "dirty": False,
                "files_changed": ["src/example.py"],
            },
            "tests": [{"command": "pytest tests/test_example.py", "returncode": 0}],
            "codegraph": {
                "schema": CODEGRAPH_AUDIT_SCHEMA,
                "status": "pass",
                "reason": "test_fixture",
                "relevant_files": codegraph_relevant_files(["src/example.py"]),
                "commands": [
                    {"argv": ["codegraph", "sync"], "returncode": 0},
                    {"argv": ["codegraph", "affected"], "returncode": 0},
                ],
            },
            **verification,
        },
    )
    verdict = cp.add_evidence(
        task.id,
        "review",
        "artifact://verdict-%s" % review.id,
        "verdict",
        reviewer.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    return cp.submit_review(
        review.id,
        status,
        reviewer.id,
        reason="reviewer %s via signed verdict evidence"
        % ("rejected" if status == ReviewStatus.REJECTED.value else "approved"),
        evidence_id=verdict.id,
    )


def test_a_rejection_records_the_findings_it_cited(cp):
    reviewer, task, review, evidence = _to_review(cp, "cited-rejection")

    done = _verdict(
        cp,
        reviewer,
        task,
        review,
        evidence,
        status=ReviewStatus.REJECTED.value,
        verification={
            "evidence_type": "review_verdict",
            "verdict": "rejected",
            "summary": "the new test does not fail under the described mutation",
            "findings": [
                {"file": "tests/test_agent_ownership.py", "detail": "assertion never flips"},
            ],
        },
    )

    assert done.findings, "the review row recorded no findings at all"
    assert done.findings["finding_count"] == 1
    assert done.findings["cited_specifics"] is True
    assert "described mutation" in done.findings["summary"]


def test_an_approval_records_its_findings_too(cp):
    """An approval that cites nothing is itself the interesting datum."""
    reviewer, task, review, evidence = _to_review(cp, "cited-approval")

    done = _verdict(
        cp,
        reviewer,
        task,
        review,
        evidence,
        status=ReviewStatus.APPROVED.value,
        verification={
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "summary": "test pins the behaviour and fails without the fix",
        },
    )

    assert done.findings["status"] == ReviewStatus.APPROVED.value
    assert done.findings["cited_specifics"] is True
    assert done.findings["finding_count"] == 0


def test_a_rubber_stamp_is_distinguishable_from_a_reasoned_verdict(cp):
    """The whole point: an empty verdict must be visibly empty."""
    reviewer, task, review, evidence = _to_review(cp, "rubber-stamp")

    done = _verdict(
        cp,
        reviewer,
        task,
        review,
        evidence,
        status=ReviewStatus.APPROVED.value,
        verification={"evidence_type": "review_verdict", "verdict": "approved"},
    )

    assert done.findings["cited_specifics"] is False
    assert done.findings["finding_count"] == 0


def test_a_relayed_harness_failure_is_marked_infrastructure(cp):
    """A rejection that is really a harness fault must not read as judgement.

    This is the shape that destroyed task_4ce995cb: three rejections whose
    stored reason was the standard template while the actual cause was a
    sandbox with 588 collection errors.
    """
    reviewer, task, review, evidence = _to_review(cp, "harness-relay")

    done = _verdict(
        cp,
        reviewer,
        task,
        review,
        evidence,
        status=ReviewStatus.REJECTED.value,
        verification={
            "evidence_type": "review_verdict",
            "verdict": "rejected",
            "feedback": (
                "hub contract verification failed: 36 failed, 84 passed, "
                "588 errors\nError: ssh exited with status exit status: 1"
            ),
        },
    )

    assert done.findings["is_infrastructure"] is True
    assert done.findings["failure_class"] == "hub_verification_error"
    assert done.findings["cited_specifics"] is False, (
        "a relayed harness failure cites nothing about the work and must not "
        "be counted as a reasoned verdict"
    )


def test_findings_survive_a_reload_from_the_store(cp):
    """Persisted on the row, not merely returned by the call that wrote it."""
    reviewer, task, review, evidence = _to_review(cp, "reload")
    _verdict(
        cp,
        reviewer,
        task,
        review,
        evidence,
        status=ReviewStatus.REJECTED.value,
        verification={
            "evidence_type": "review_verdict",
            "verdict": "rejected",
            "summary": "does not satisfy the acceptance criteria",
        },
    )

    reloaded = cp.get_review(review.id)
    assert reloaded.findings["summary"] == "does not satisfy the acceptance criteria"

    listed = [r for r in cp.list_reviews(task.id) if r.id == review.id]
    assert listed and listed[0].findings["summary"]


def test_a_review_with_no_verdict_evidence_records_nothing_rather_than_lying(cp):
    """Absent must stay distinguishable from "said nothing"."""
    reviewer, task, review, evidence = _to_review(cp, "no-evidence")

    done = cp.submit_review(
        review.id,
        ReviewStatus.REJECTED.value,
        reviewer.id,
        reason="reviewer rejected via signed verdict evidence",
    )

    assert done.findings == {}
