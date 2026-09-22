"""Adversarial semantic-acceptance coverage for deterministic canaries."""

from __future__ import annotations

import hashlib
import json

import pytest

from mac.semantic_acceptance import (
    ACCEPTANCE_SCHEMA,
    CANARY_VERIFIER_DIGEST,
    CANARY_VERIFIER_ID,
    VERIFIER_DIGEST,
    VERIFIER_ID,
    acceptance_result_problems,
    canonical_digest,
    evaluate_acceptance,
)
from mac.services import ControlPlane, sign_verification_manifest
from tests.conftest import submit_review_verdict


def _canary_metadata(payload: object) -> dict:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "schema": "mac.canary_workload.v1",
        "canary": True,
        "workload": {
            "family": "security_hash_chain",
            "expected_result_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        },
    }


def _manifest(result: str) -> dict:
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "operator_result": {"summary": "deterministic canary", "result": result},
    }


def test_canary_expected_digest_migrates_to_content_addressed_acceptance() -> None:
    payload = {"count": 97, "family": "security_hash_chain"}
    result = evaluate_acceptance(
        _canary_metadata(payload),
        _manifest(
            "work log\nMAC_CANARY_RESULT=%s\n"
            % json.dumps(payload, sort_keys=True, separators=(",", ":"))
        ),
    )
    assert result["required"] is True
    assert result["status"] == "pass"
    assert result["verifier"] == {
        "id": CANARY_VERIFIER_ID,
        "digest": CANARY_VERIFIER_DIGEST,
    }


def test_malicious_duplicate_marker_fails_closed() -> None:
    payload = {"answer": 42}
    marker = "MAC_CANARY_RESULT=%s" % json.dumps(payload, separators=(",", ":"))
    result = evaluate_acceptance(_canary_metadata(payload), _manifest(marker + "\n" + marker))
    assert result["status"] == "fail"
    assert result["problems"] == [
        "canary evidence must contain exactly one result marker line",
        "output.digest does not match its pattern",
        "acceptance output does not equal expected_output",
    ]


def test_structurally_valid_but_wrong_value_fails_closed() -> None:
    expected = {"answer": 42}
    observed = {"answer": 41}
    result = evaluate_acceptance(
        _canary_metadata(expected),
        _manifest("MAC_CANARY_RESULT=%s" % json.dumps(observed, separators=(",", ":"))),
    )
    assert result["status"] == "fail"
    assert "acceptance output does not equal expected_output" in result["problems"]


@pytest.mark.parametrize(
    "operator_result",
    [
        {"summary": "renamed", "results": "MAC_CANARY_RESULT={}"},
        {"summary": "omitted"},
    ],
)
def test_renamed_or_omitted_result_field_fails_closed(operator_result: dict) -> None:
    result = evaluate_acceptance(
        _canary_metadata({}),
        {"operator_result": operator_result},
    )
    assert result["status"] == "fail"
    assert result["problems"] == ["acceptance input is absent at /operator_result/result"]


def test_noncanonical_payload_and_embedded_marker_are_not_accepted() -> None:
    payload = {"a": 1, "b": 2}
    noncanonical = '{"b": 2, "a": 1}'
    result = evaluate_acceptance(
        _canary_metadata(payload),
        _manifest("prefix MAC_CANARY_RESULT=%s" % noncanonical),
    )
    assert result["status"] == "fail"
    assert "canary evidence must contain exactly one result marker line" in result["problems"]


def test_replacement_attempt_cannot_reuse_prior_signed_acceptance() -> None:
    metadata = _canary_metadata({"answer": 42})
    first = evaluate_acceptance(metadata, _manifest('MAC_CANARY_RESULT={"answer":42}'))
    replacement = evaluate_acceptance(metadata, _manifest('MAC_CANARY_RESULT={"answer":41}'))
    assert first["status"] == "pass"
    assert acceptance_result_problems(replacement, first) == [
        "signed verdict semantic acceptance result does not match deterministic replay"
    ]


def test_verifier_version_drift_is_unavailable_not_advisory() -> None:
    value = {"answer": 42}
    metadata = {
        "acceptance_check": {
            "schema": ACCEPTANCE_SCHEMA,
            "verifier": {"id": VERIFIER_ID, "digest": "sha256:" + "0" * 64},
            "input": {"source": "executor_manifest", "pointer": "/value"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "expected_output": {"digest": canonical_digest(value)},
        }
    }
    result = evaluate_acceptance(metadata, {"value": value})
    assert result["status"] == "fail"
    assert result["problems"] == ["acceptance verifier is unavailable or its version drifted"]


def test_mandatory_but_absent_contract_fails_closed() -> None:
    result = evaluate_acceptance({"acceptance_required": True}, _manifest("anything"))
    assert result["status"] == "fail"
    assert result["problems"] == ["mandatory acceptance_check is absent"]


def test_canary_with_missing_expected_digest_fails_closed() -> None:
    result = evaluate_acceptance(
        {"schema": "mac.canary_workload.v1", "workload": {"family": "cpu_prime_sieve"}},
        _manifest("MAC_CANARY_RESULT={}"),
    )
    assert result["required"] is True
    assert result["status"] == "fail"
    assert result["problems"] == ["mandatory acceptance_check is absent"]


def test_deterministic_replay_is_byte_stable() -> None:
    value = {"items": [3, 2, 1], "ok": True}
    metadata = {
        "acceptance_check": {
            "schema": ACCEPTANCE_SCHEMA,
            "verifier": {"id": VERIFIER_ID, "digest": VERIFIER_DIGEST},
            "input": {"source": "executor_manifest", "pointer": "/value"},
            "input_schema": {"type": "object", "required": ["items", "ok"]},
            "output_schema": {
                "type": "object",
                "required": ["digest"],
                "properties": {"digest": {"type": "string"}},
                "additionalProperties": False,
            },
            "expected_output": {"digest": canonical_digest(value)},
        }
    }
    first = evaluate_acceptance(metadata, {"value": value})
    second = evaluate_acceptance(metadata, {"value": {"ok": True, "items": [3, 2, 1]}})
    assert first == second
    assert first["status"] == "pass"
    assert acceptance_result_problems(second, first) == []


def _reviewing_canary(
    result: str = 'MAC_CANARY_RESULT={"answer":42}',
) -> tuple[ControlPlane, object, object, object, dict]:
    cp = ControlPlane.in_memory()
    executor_machine = cp.register_machine("acceptance-executor-host")
    executor = cp.register_agent(executor_machine.id, "acceptance-executor")
    reviewer_machine = cp.register_machine("acceptance-reviewer-host")
    reviewer = cp.register_agent(
        reviewer_machine.id, "acceptance-reviewer", capabilities=["review"]
    )
    payload = {"answer": 42}
    task = cp.create_task("semantic acceptance", metadata=_canary_metadata(payload))
    _, lease = cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id, lease_id=lease.id)
    executor_manifest = _manifest(result)
    executor_manifest["signed_by"] = executor.id
    executor_manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor.id), executor_manifest
    )
    evidence = cp.add_evidence(
        task.id,
        "artifact",
        "artifact://canary-result",
        "deterministic canary result",
        executor.id,
        lease_id=lease.id,
        metadata={"returncode": 0, "verification": executor_manifest},
    )
    cp.submit_for_review(task.id, executor.id, lease_id=lease.id)
    review = cp.request_review(task.id, reviewer.id)
    return cp, task, reviewer, review, evidence


def test_approval_accepts_signed_semantic_result_bound_to_executor_evidence() -> None:
    cp, task, reviewer, review, evidence = _reviewing_canary()
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    accepted = cp.submit_review(review.id, "approved", reviewer.id, evidence_id=verdict_id)
    assert accepted.status == "approved"
    manifest = cp.get_evidence(verdict_id).metadata["verification"]
    assert manifest["acceptance"]["status"] == "pass"
    assert manifest["review_status"] == {"structural": "pass", "semantic": "pass"}


def test_approval_rejects_signed_verdict_with_absent_semantic_result() -> None:
    cp, task, reviewer, review, evidence = _reviewing_canary()
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": evidence.id,
        "checks": [{"name": "structural only", "returncode": 0}],
        "worktree_digest": "sha256:" + "0" * 64,
        "signed_by": reviewer.id,
    }
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(reviewer.id), manifest
    )
    verdict = cp.add_evidence(
        task.id,
        "review",
        "artifact://forged-structural-only-verdict",
        "structural-only verdict",
        reviewer.id,
        metadata={"returncode": 0, "verification": manifest},
        _trusted_internal=True,
    )
    with pytest.raises(Exception, match="missing semantic acceptance result"):
        cp.submit_review(review.id, "approved", reviewer.id, evidence_id=verdict.id)


def test_removed_semantic_reviewer_records_fail_closed_typed_rejection() -> None:
    cp, task, reviewer, review, evidence = _reviewing_canary('MAC_CANARY_RESULT={"answer":41}')
    verdict = cp._record_semantic_reviewer_removed_verdict(
        cp.get_task(task.id), review, evidence, "test"
    )
    assert verdict is not None
    manifest = verdict.metadata["verification"]
    assert manifest["verdict"] == "rejected"
    assert manifest["review_status"] == {"structural": "pass", "semantic": "fail"}
    assert manifest["acceptance"]["status"] == "fail"
    assert manifest["checks"][-1] == {
        "name": "task_acceptance",
        "returncode": 1,
        "status": "fail",
    }
