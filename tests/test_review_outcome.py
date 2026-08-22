"""Three-axis review outcomes: harness vs tests vs semantics."""

from __future__ import annotations

from mac.review_outcome import (
    VERDICT_APPROVED,
    VERDICT_INFRASTRUCTURE,
    VERDICT_REJECTED,
    VERDICT_TESTS_FAILED,
    classify_independent_test_outcome,
    compose_canonical_review_verdict,
)


def test_pytest_failures_without_collection_errors_are_tests_failed() -> None:
    output = (
        "FAILED tests/test_batch.py::test_preview_and_apply_agree\n"
        "3 failed, 1204 passed, 11 skipped in 612.44s\n"
    )
    assert classify_independent_test_outcome(output, 1) == VERDICT_TESTS_FAILED


def test_collection_collapse_is_infrastructure() -> None:
    output = (
        "ERROR collecting tests/test_control_plane.py\n"
        "============ 36 failed, 84 passed, 4 skipped, 588 errors in 29.56s "
        "=============\n"
    )
    assert classify_independent_test_outcome(output, 1) == VERDICT_INFRASTRUCTURE


def test_sandbox_encoding_failure_is_infrastructure() -> None:
    output = (
        "UnicodeEncodeError: 'ascii' codec can't encode character '\\xa7'\n"
        "Error:   x ssh exited with status exit status: 1\n"
    )
    assert classify_independent_test_outcome(output, 1) == VERDICT_INFRASTRUCTURE


def test_clean_run_is_pass() -> None:
    assert classify_independent_test_outcome("1204 passed in 10s", 0) == "pass"


def test_nonzero_without_pytest_summary_is_infrastructure() -> None:
    assert classify_independent_test_outcome("ssh exited with status", 1) == VERDICT_INFRASTRUCTURE


def test_compose_evaluates_harness_before_semantics_and_tests() -> None:
    assert (
        compose_canonical_review_verdict(
            harness_ok=False,
            semantic_verdict="approved",
            semantic_valid=True,
            reproducibility="fail",
            independent_pass=False,
        )
        == VERDICT_INFRASTRUCTURE
    )
    assert (
        compose_canonical_review_verdict(
            harness_ok=True,
            semantic_verdict="rejected",
            semantic_valid=True,
            reproducibility="fail",
            independent_pass=False,
        )
        == VERDICT_REJECTED
    )
    assert (
        compose_canonical_review_verdict(
            harness_ok=True,
            semantic_verdict="approved",
            semantic_valid=True,
            reproducibility="fail",
            independent_pass=False,
        )
        == VERDICT_TESTS_FAILED
    )
    assert (
        compose_canonical_review_verdict(
            harness_ok=True,
            semantic_verdict="approved",
            semantic_valid=True,
            reproducibility="pass",
            independent_pass=True,
        )
        == VERDICT_APPROVED
    )
