"""Unit tests for the review failure taxonomy and classifier.

Covers:
- Infrastructure class detection (transport errors, reviewer unavailability,
  timeouts, hub verification errors, credential/access failures)
- Semantic class detection (reviewer rejections, code-quality findings,
  protocol noncompliance)
- Compound ``reviewer_protocol_failure:<sub>`` reason strings
- Free-text error fallback matching
- Evidence-type hint
- Convenience helpers (is_infrastructure_failure, is_semantic_failure)
- Unknown / empty input handling
"""

from __future__ import annotations

import pytest

from mac.review_failure_classifier import (
    ReviewFailureClassification,
    classify_review_failure,
    is_infrastructure_failure,
    is_semantic_failure,
)


# ---------------------------------------------------------------------------
# Infrastructure failure reasons — must be retryable
# ---------------------------------------------------------------------------


class TestInfrastructureReasons:
    """Direct reason-string matches for infrastructure failures."""

    @pytest.mark.parametrize(
        "reason",
        [
            "reviewer_not_available",
            "reviewer_unavailable",
            "reviewer_stale",
        ],
    )
    def test_reviewer_unavailability_reasons(self, reason: str) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is True
        assert result.failure_class == "reviewer_unavailable"

    @pytest.mark.parametrize(
        "reason",
        [
            "review_verdict_wait_cap_hit",
            "review_retraction_cap_hit",
        ],
    )
    def test_reviewer_timeout_reasons(self, reason: str) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is True
        assert result.failure_class == "reviewer_timeout"

    @pytest.mark.parametrize(
        "reason",
        [
            "review_clone_failed",
            "clone_failed",
            "transport_error",
        ],
    )
    def test_transport_error_reasons(self, reason: str) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is True
        assert result.failure_class == "transport_error"

    @pytest.mark.parametrize(
        "reason",
        [
            "hub_verification_error",
            "hub_verification_failed",
        ],
    )
    def test_hub_verification_error_reasons(self, reason: str) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is True
        assert result.failure_class == "hub_verification_error"

    @pytest.mark.parametrize(
        "reason",
        [
            "credential_error",
            "authentication",
            "authorization",
            "repository_missing",
            "network",
        ],
    )
    def test_credential_access_reasons(self, reason: str) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is True
        assert result.failure_class == "credential_access"


# ---------------------------------------------------------------------------
# Semantic failure reasons — must NOT be retryable
# ---------------------------------------------------------------------------


class TestSemanticReasons:
    """Direct reason-string matches for semantic/code-quality failures."""

    @pytest.mark.parametrize(
        "reason",
        [
            "rejected",
            "changes_requested",
            "semantic_rejection",
        ],
    )
    def test_semantic_rejection_reasons(self, reason: str) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is False
        assert result.failure_class == "semantic_rejection"

    def test_reviewer_findings(self) -> None:
        result = classify_review_failure("reviewer_findings")
        assert result.is_infrastructure is False
        assert result.failure_class == "reviewer_findings"

    def test_semantic_verdict_invalid_is_semantic(self) -> None:
        """A malformed verdict is still a semantic outcome, not infrastructure."""
        result = classify_review_failure("semantic_verdict_invalid")
        assert result.is_infrastructure is False
        assert result.failure_class == "semantic_verdict_invalid"

    def test_blind_protocol_noncompliant_is_semantic(self) -> None:
        """Blind-review protocol violation is semantic, not a transport failure."""
        result = classify_review_failure("blind_protocol_noncompliant")
        assert result.is_infrastructure is False
        assert result.failure_class == "blind_protocol_noncompliant"


# ---------------------------------------------------------------------------
# Compound reviewer_protocol_failure:<sub> strings
# ---------------------------------------------------------------------------


class TestProtocolFailureCompound:
    """Compound strings produced by _review_attempt_protocol_failure()."""

    def test_review_executor_nonzero_is_infrastructure(self) -> None:
        result = classify_review_failure(
            "reviewer_protocol_failure:review_executor_nonzero"
        )
        assert result.is_infrastructure is True
        assert result.failure_class == "review_executor_nonzero"

    def test_semantic_verdict_invalid_compound_is_semantic(self) -> None:
        result = classify_review_failure(
            "reviewer_protocol_failure:semantic_verdict_invalid"
        )
        assert result.is_infrastructure is False
        assert result.failure_class == "semantic_verdict_invalid"

    def test_blind_protocol_noncompliant_compound_is_semantic(self) -> None:
        result = classify_review_failure(
            "reviewer_protocol_failure:blind_protocol_noncompliant"
        )
        assert result.is_infrastructure is False
        assert result.failure_class == "blind_protocol_noncompliant"

    def test_unknown_sub_reason_is_infrastructure(self) -> None:
        """Unknown protocol-failure sub-reason falls back to infrastructure."""
        result = classify_review_failure(
            "reviewer_protocol_failure:some_unknown_harness_crash"
        )
        assert result.is_infrastructure is True

    def test_prefix_case_insensitive(self) -> None:
        result = classify_review_failure(
            "REVIEWER_PROTOCOL_FAILURE:review_executor_nonzero"
        )
        assert result.is_infrastructure is True


# ---------------------------------------------------------------------------
# Free-text error fallback matching
# ---------------------------------------------------------------------------


class TestErrorTextFallback:
    """When reason is unknown, error text drives classification."""

    def test_authentication_error_text(self) -> None:
        result = classify_review_failure(
            "some_unrecognized_reason",
            error="could not clone repository: authentication failed for 'https://github.com/...'",
        )
        assert result.is_infrastructure is True

    def test_permission_denied_error_text(self) -> None:
        result = classify_review_failure(
            "",
            error="Permission denied (publickey). fatal: Could not read from remote repository.",
        )
        assert result.is_infrastructure is True
        assert result.failure_class == "credential_access"

    def test_could_not_resolve_host(self) -> None:
        result = classify_review_failure(
            "",
            error="fatal: unable to access '...': Could not resolve host: github.com",
        )
        assert result.is_infrastructure is True
        assert result.failure_class == "transport_error"

    def test_git_clone_in_error(self) -> None:
        result = classify_review_failure(
            "worker_exception",
            error="refusing review clone: git clone failed with returncode 128",
        )
        assert result.is_infrastructure is True
        assert result.failure_class == "transport_error"

    def test_reviewer_not_available_in_error(self) -> None:
        result = classify_review_failure(
            "",
            error="reviewer agent is not available (last seen 600s ago)",
        )
        assert result.is_infrastructure is True
        assert result.failure_class == "reviewer_unavailable"

    def test_semantic_rejection_in_error_blob(self) -> None:
        result = classify_review_failure(
            "",
            error="review verdict: rejected",
        )
        assert result.is_infrastructure is False
        assert result.failure_class == "semantic_rejection"

    def test_hub_verification_error_in_reason_text(self) -> None:
        result = classify_review_failure(
            "hub_verification_failed_sandbox",
            error="hub verif sandbox exited with code 1",
        )
        assert result.is_infrastructure is True


# ---------------------------------------------------------------------------
# Evidence-type hint
# ---------------------------------------------------------------------------


class TestEvidenceTypeHint:
    """evidence_type='review_verdict' implies a semantic outcome."""

    def test_review_verdict_evidence_type(self) -> None:
        result = classify_review_failure(
            "completely_unknown_reason",
            evidence_type="review_verdict",
        )
        assert result.is_infrastructure is False
        assert result.failure_class == "semantic_rejection"

    def test_non_review_verdict_evidence_type_does_not_force_semantic(self) -> None:
        result = classify_review_failure(
            "reviewer_not_available",
            evidence_type="repo_change",
        )
        # reviewer_not_available is infra regardless of evidence_type
        assert result.is_infrastructure is True


# ---------------------------------------------------------------------------
# Empty and edge-case inputs
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Empty, None-equivalent, and mixed-case inputs."""

    def test_empty_reason_returns_unknown(self) -> None:
        result = classify_review_failure("")
        assert result.failure_class == "unknown"
        assert result.is_infrastructure is False

    def test_none_equivalent_reason(self) -> None:
        result = classify_review_failure(None)  # type: ignore[arg-type]
        assert result.failure_class == "unknown"

    def test_whitespace_reason(self) -> None:
        result = classify_review_failure("   ")
        assert result.failure_class == "unknown"

    def test_completely_unrecognised_reason(self) -> None:
        result = classify_review_failure("xyzzy_not_a_real_reason")
        assert result.failure_class == "unknown"

    def test_reason_case_insensitive(self) -> None:
        result = classify_review_failure("REVIEWER_NOT_AVAILABLE")
        assert result.is_infrastructure is True

    def test_returns_named_tuple(self) -> None:
        result = classify_review_failure("rejected")
        assert isinstance(result, ReviewFailureClassification)
        assert hasattr(result, "failure_class")
        assert hasattr(result, "is_infrastructure")


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


class TestConvenienceHelpers:
    """is_infrastructure_failure and is_semantic_failure wrappers."""

    def test_is_infrastructure_failure_true(self) -> None:
        assert is_infrastructure_failure("reviewer_not_available") is True

    def test_is_infrastructure_failure_false_for_semantic(self) -> None:
        assert is_infrastructure_failure("rejected") is False

    def test_is_semantic_failure_true(self) -> None:
        assert is_semantic_failure("rejected") is True

    def test_is_semantic_failure_false_for_infrastructure(self) -> None:
        assert is_semantic_failure("reviewer_not_available") is False

    def test_is_semantic_failure_false_for_unknown(self) -> None:
        # Unknown is not classified as semantic (false negative is better
        # than misclassifying infrastructure as semantic)
        assert is_semantic_failure("totally_unknown_reason_xyz") is False

    def test_is_infrastructure_failure_with_error_text(self) -> None:
        assert (
            is_infrastructure_failure(
                "unknown_reason",
                error="authentication failed for reviewer clone",
            )
            is True
        )


# ---------------------------------------------------------------------------
# Taxonomy contract: all documented infrastructure classes are retryable
# ---------------------------------------------------------------------------


class TestTaxonomyContract:
    """Verify that the full documented taxonomy is correctly classified."""

    INFRASTRUCTURE_CASES = [
        # (reason, expected_class)
        ("reviewer_not_available", "reviewer_unavailable"),
        ("reviewer_stale", "reviewer_unavailable"),
        ("review_verdict_wait_cap_hit", "reviewer_timeout"),
        ("review_retraction_cap_hit", "reviewer_timeout"),
        ("transport_error", "transport_error"),
        ("clone_failed", "transport_error"),
        ("hub_verification_error", "hub_verification_error"),
        ("authentication", "credential_access"),
        ("authorization", "credential_access"),
        ("network", "credential_access"),
    ]

    SEMANTIC_CASES = [
        # (reason, expected_class)
        ("rejected", "semantic_rejection"),
        ("changes_requested", "semantic_rejection"),
        ("semantic_verdict_invalid", "semantic_verdict_invalid"),
        ("blind_protocol_noncompliant", "blind_protocol_noncompliant"),
    ]

    @pytest.mark.parametrize("reason,expected_class", INFRASTRUCTURE_CASES)
    def test_infrastructure_class_mapping(
        self, reason: str, expected_class: str
    ) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is True, (
            "Expected infrastructure for reason=%r but got is_infrastructure=False" % reason
        )
        assert result.failure_class == expected_class, (
            "Expected class=%r for reason=%r but got %r"
            % (expected_class, reason, result.failure_class)
        )

    @pytest.mark.parametrize("reason,expected_class", SEMANTIC_CASES)
    def test_semantic_class_mapping(
        self, reason: str, expected_class: str
    ) -> None:
        result = classify_review_failure(reason)
        assert result.is_infrastructure is False, (
            "Expected semantic for reason=%r but got is_infrastructure=True" % reason
        )
        assert result.failure_class == expected_class, (
            "Expected class=%r for reason=%r but got %r"
            % (expected_class, reason, result.failure_class)
        )
