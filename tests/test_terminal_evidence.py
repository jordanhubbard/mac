"""Terminal-evidence detection and the claim refusal it produces.

Pure-function coverage for :mod:`mac.terminal_evidence`. The control-plane
wiring is exercised separately in ``test_terminal_evidence_claim_gate.py``.
"""

from __future__ import annotations

import pytest

from mac.terminal_evidence import (
    TerminalEvidenceKind,
    claim_refusal,
    describe_terminal_evidence,
    detect_terminal_evidence,
    evidence_carries_canonical_integration,
    lineage_authorization,
)


TASK_ID = "task_" + "a" * 32
PRIOR_ID = "task_" + "b" * 32
HEAD = "c" * 40
CANONICAL_REF = "refs/heads/main"


def _evidence(evidence_id, verification):
    return {"id": evidence_id, "metadata": {"verification": verification}}


def _canonical_integration(**overrides):
    integration = {
        "status": "pass",
        "remote_verified": True,
        "canonical_ref": CANONICAL_REF,
        "canonical_tip_sha": HEAD,
    }
    integration.update(overrides)
    return {"repo": {"head_sha": HEAD}, "canonical_integration": integration}


def test_canonical_integration_predicate_accepts_the_three_landing_shapes():
    fast_forward = _canonical_integration()
    merge_commit = _canonical_integration(
        canonical_tip_sha="d" * 40,
        reviewed_head_sha=HEAD,
        contains_reviewed_head=True,
    )
    squash = _canonical_integration(
        canonical_tip_sha="e" * 40,
        reviewed_head_sha=HEAD,
        squash_merged=True,
    )
    for manifest in (fast_forward, merge_commit, squash):
        assert evidence_carries_canonical_integration(
            {"verification": manifest}, CANONICAL_REF
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "fail"},
        {"remote_verified": False},
        {"canonical_ref": "refs/heads/other"},
        # A tip that is neither the reviewed head nor claims to contain it.
        {"canonical_tip_sha": "f" * 40},
    ],
    ids=["not-pass", "not-remote-verified", "wrong-ref", "unrelated-tip"],
)
def test_canonical_integration_predicate_refuses_incomplete_proof(overrides):
    manifest = _canonical_integration(**overrides)
    assert not evidence_carries_canonical_integration(
        {"verification": manifest}, CANONICAL_REF
    )


def test_flat_legacy_metadata_without_a_verification_envelope_still_matches():
    assert evidence_carries_canonical_integration(
        _canonical_integration(), CANONICAL_REF
    )


def test_detects_canonical_integration_and_prefers_it_over_a_merged_pr():
    verdict = detect_terminal_evidence(
        {"id": TASK_ID, "state": "open"},
        [
            _evidence(
                "ev_1",
                {"repo": {"pull_request": {"merged": True, "number": 498}}},
            ),
            _evidence("ev_2", _canonical_integration()),
        ],
        canonical_ref=CANONICAL_REF,
    )
    assert verdict.present
    assert verdict.kind == TerminalEvidenceKind.CANONICAL_INTEGRATION.value
    assert verdict.detail["evidence_id"] == "ev_2"
    assert "main" in describe_terminal_evidence(verdict)


def test_detects_a_merged_pull_request_without_a_canonical_ref():
    # This is task_f33a2da7's shape: the work merged as a pull request, and the
    # row is still `open` because a fleet restart put it back.
    verdict = detect_terminal_evidence(
        {"id": TASK_ID, "state": "open"},
        [
            _evidence(
                "ev_1",
                {
                    "repo": {
                        "pull_request": {
                            "merged": True,
                            "number": 498,
                            "url": "https://github.com/example/mac/pull/498",
                        }
                    }
                },
            )
        ],
    )
    assert verdict.present
    assert verdict.kind == TerminalEvidenceKind.MERGED_PULL_REQUEST.value
    assert verdict.detail["pull_request"]["number"] == 498
    assert "/pull/498" in describe_terminal_evidence(verdict)


def test_an_open_pull_request_is_not_terminal_evidence():
    # A pull request that exists but has not merged is exactly the state a
    # duplicate must still be allowed to supersede.
    verdict = detect_terminal_evidence(
        {"id": TASK_ID, "state": "open"},
        [
            _evidence(
                "ev_1",
                {
                    "repo": {
                        "pull_request": {
                            "state": "open",
                            "url": "https://github.com/example/mac/pull/498",
                        }
                    }
                },
            )
        ],
    )
    assert not verdict.present
    assert describe_terminal_evidence(verdict) == "no terminal evidence"


def test_recorded_completion_is_terminal_evidence():
    verdict = detect_terminal_evidence(
        {"id": TASK_ID, "state": "completed", "completed_at": "2026-08-19T00:00:00Z"},
        [],
    )
    assert verdict.present
    assert verdict.kind == TerminalEvidenceKind.RECORDED_COMPLETION.value


def test_completed_without_a_timestamp_is_not_claimed_as_terminal_evidence():
    verdict = detect_terminal_evidence({"id": TASK_ID, "state": "completed"}, [])
    assert not verdict.present


def test_claim_refusal_names_the_evidence_and_the_two_legitimate_next_actions():
    refusal = claim_refusal(
        {"id": TASK_ID, "state": "open"},
        [_evidence("ev_1", _canonical_integration())],
        canonical_ref=CANONICAL_REF,
    )
    assert refusal is not None
    assert refusal["task_id"] == TASK_ID
    assert refusal["terminal_evidence"]["present"] is True
    assert "replacement" in refusal["remediation"]
    assert "retry" in refusal["remediation"]


def test_a_row_without_terminal_evidence_is_claimable():
    assert (
        claim_refusal({"id": TASK_ID, "state": "open"}, [], canonical_ref=CANONICAL_REF)
        is None
    )


def test_explicit_retry_lineage_is_the_only_exemption():
    task = {
        "id": TASK_ID,
        "state": "open",
        "metadata": {"lineage": {"retry_of": {"kind": "task", "ref": PRIOR_ID}}},
    }
    assert lineage_authorization(task)["authorized_by"] == "retry_of"
    assert (
        claim_refusal(
            task,
            [_evidence("ev_1", _canonical_integration())],
            canonical_ref=CANONICAL_REF,
        )
        is None
    )


def test_a_bare_open_state_is_never_authorization():
    # The whole bug: `state == "open"` said claimable, and nothing else was
    # consulted. An empty lineage block must not read as an exemption.
    task = {"id": TASK_ID, "state": "open", "metadata": {"lineage": {}}}
    assert lineage_authorization(task) == {}
    assert (
        claim_refusal(
            task,
            [_evidence("ev_1", _canonical_integration())],
            canonical_ref=CANONICAL_REF,
        )
        is not None
    )
