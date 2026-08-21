"""The recalled lesson for a rejected review must carry the REASON.

`_hub_verify_output_excerpt` fixed the capture site: a failing contract run's
verdict is in the middle of the output, so a blind tail dropped it and the
rejection was unclassifiable. The same discard survived one hop later, in the
opposite direction.

`_record_review_outcome_lesson` bounded the signed feedback from the LEFT --
`detail[:300]`, `error_signature = detail[:200]` -- and that feedback opens
with a fixed header and the test command. The repository's sanity selector
command alone runs past 200 characters, so every rejection on this project
recorded the SAME `error_signature`: a header plus a command truncated
mid-path. `error_signature` is the field the executor's recall and the
curation prompt classify on, so a constant one classifies nothing, and the
agent that retried the task was told it had failed without being told why.

These tests pin the property that matters: what survives the bound is the
text that announces the failure, and two different failures do not collapse
onto the same signature.
"""

from __future__ import annotations

import json

from mac.services import ControlPlane, _hub_review_lesson_reason


# The real header, verbatim in shape: the command is the repository's
# changed-file sanity selector, which is longer than the 200-character
# error_signature bound all by itself.
FEEDBACK_HEADER = (
    "hub contract verification failed (rc=1): if [ -x scripts/run-sanity-tests.sh ]; "
    "then scripts/run-sanity-tests.sh --changed-file docs/archive/index.md "
    "--changed-file docs/env-config-reference.md "
    "--changed-file docs/reference/documentation-inventory.md "
    "--changed-file src/mac/services.py; fi\n\n"
)

PROGRESS = "".join(
    "%s [%2d%%]\n" % ("." * 72, pct) for pct in range(11, 99, 12)
) * 4

COVERAGE_TABLE = "".join(
    "src/mac/module_%03d.py   %4d   %3d   %3d   %2d   9%d.%02d%%   127, 139, 152\n"
    % (n, 500 + n, n, 40, n % 9, n % 10, n % 100)
    for n in range(200)
)


def _rejection_feedback(*failures: str) -> str:
    """A rejection's signed feedback, in the order a real run prints it."""

    summary = "\n".join("FAILED %s" % name for name in failures)
    return (
        FEEDBACK_HEADER
        + "Created sandbox: mac-hubverify-73f75a5234334583\n"
        + "run-contract-tests.sh: running fail-fast repository contract preflight\n"
        + PROGRESS
        + "=========================== short test summary info ============================\n"
        + summary
        + "\n= %d failed, 11296 passed, 3 skipped in 1566.41s (0:26:06) =\n" % len(failures)
        + COVERAGE_TABLE
        + "coverage safety: statements 69300/76238 (90.90%, floor 90.00%)\n"
        + "Error:   x ssh exited with status exit status: 1\n"
    )


def test_the_signature_names_the_failure_not_the_command() -> None:
    feedback = _rejection_feedback("tests/cli/test_cli_version_flag.py::test_it_exits_zero")

    signature = _hub_review_lesson_reason(feedback, limit=200)

    assert "failed," in signature, (
        "the bounded signature must keep the pytest verdict, which is what "
        "makes a rejection classifiable:\n%s" % signature
    )
    assert "run-sanity-tests.sh" not in signature, (
        "the selector command is boilerplate shared by every rejection; "
        "keeping it is what made the signature constant:\n%s" % signature
    )


def test_two_different_failures_do_not_share_a_signature() -> None:
    one = _hub_review_lesson_reason(
        _rejection_feedback("tests/cli/test_cli_version_flag.py::test_it_exits_zero"),
        limit=200,
    )
    other = _hub_review_lesson_reason(
        _rejection_feedback("tests/test_worker_shutdown_abandon.py::test_sigterm"),
        limit=200,
    )

    assert one != other, (
        "identical signatures for unlike failures make error_signature a "
        "constant, and a constant cannot group, dedupe or explain anything"
    )
    assert "test_cli_version_flag" in one
    assert "test_worker_shutdown_abandon" in other


def test_the_detail_keeps_the_named_tests_as_well_as_the_count() -> None:
    feedback = _rejection_feedback(
        "tests/cli/test_cli_version_flag.py::test_it_exits_zero",
        "tests/test_worker_shutdown_abandon.py::test_sigterm",
    )

    detail = _hub_review_lesson_reason(feedback, limit=300)

    assert "test_worker_shutdown_abandon" in detail
    assert "2 failed," in detail


def test_a_documentation_contract_rejection_keeps_its_own_verdict() -> None:
    # Not every rejection is pytest. The doc-contract failures observed live on
    # 2026-08-20 are announced by a verdict signature too, and print BEFORE the
    # coverage report just the same.
    feedback = (
        FEEDBACK_HEADER
        + PROGRESS
        + "documentation contract failed: documentation-inventory.md is stale: "
        "regenerate with scripts/generate-docs-reference.py --write\n"
        + COVERAGE_TABLE
    )

    signature = _hub_review_lesson_reason(feedback, limit=200)

    assert "documentation contract failed" in signature
    assert "regenerate with" in signature


def test_short_outcomes_are_left_exactly_as_they_are() -> None:
    # The approved path records "published to <target>", well under the bound.
    assert _hub_review_lesson_reason("published to test://x", limit=300) == (
        "published to test://x"
    )


def test_output_with_no_verdict_at_all_still_respects_the_bound() -> None:
    # Nothing announced a reason, so no position is better than any other --
    # but the field is still bounded, because it is stored in every lesson.
    text = "sandbox allocated\n" * 500

    for limit in (200, 300):
        selected = _hub_review_lesson_reason(text, limit=limit)
        assert len(selected) <= limit
        assert selected == text.strip()[:limit]


def test_the_window_never_runs_past_the_end_of_the_text() -> None:
    # A verdict printed as the LAST thing in the output must not produce a
    # short slice: the window is clamped so it still delivers `limit` bytes.
    text = ("x" * 4000) + "\n= 1 failed, 2 passed in 3.00s =\n"

    selected = _hub_review_lesson_reason(text, limit=200)

    assert len(selected) == 200
    assert "1 failed," in selected


def test_the_recorded_lesson_is_the_reason_end_to_end() -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("Fix the thing", required_capabilities=["python"])
    feedback = _rejection_feedback(
        "tests/test_worker_shutdown_abandon.py::test_sigterm"
    )

    cp._record_review_outcome_lesson(
        task.id, outcome="review_rejected", detail=feedback
    )

    records = cp.search_memory(record_type_prefix="deployment_learning")
    assert len(records) == 1
    content = json.loads(records[0].content)
    assert content["outcome"] == "review_rejected"
    # What the next attempt recalls. Before this, both fields were the header.
    assert "test_worker_shutdown_abandon" in content["error_signature"]
    assert "1 failed," in content["detail"]
    assert len(content["error_signature"]) <= 200
    assert len(content["detail"]) <= 300


def test_an_approved_publication_still_records_no_error_signature() -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("Fix the thing", required_capabilities=["python"])

    cp._record_review_outcome_lesson(
        task.id, outcome="approved_published", detail="published to test://x"
    )

    content = json.loads(
        cp.search_memory(record_type_prefix="deployment_learning")[0].content
    )
    assert content["error_signature"] == ""
    assert content["detail"] == "published to test://x"
