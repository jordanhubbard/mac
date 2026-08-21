"""One reducer, two callers, and the reason survives both.

`_hub_verify_output_excerpt` replaced the hub's blind `out[-2000:]` with an
anchored window, and that fixed the capture site. It did not fix the second
reduction: `validate_projected_merge_contract` took `output_tail[-2000:]` off
the excerpt on its way into publication evidence. An excerpt whose MIDDLE is
the answer does not survive a tail, so the publication gate reproduced the
original bug one layer down -- a genuine rejection reading as a dead harness.

These tests pin the shared selection (`capture_failure_window`) and the
end-to-end property that matters: run a realistic failing
`scripts/run-contract-tests.sh` transcript through the capture site and then
through the publication gate's reduction, and the classifier must still call
it a rejection.
"""
from __future__ import annotations

from mac.contract_failure import CONTRACT_FAILURE_ANCHORS, capture_failure_window
from mac.services import _hub_verify_output_excerpt, hub_verification_unavailable_reason

# The shape run-contract-tests.sh produces, and the reason it defeats a tail:
# the failure is announced FIRST, then an unconditional whole-repo coverage
# report, then a coverage summary whose floors both PASSED, then the exit.
PYTEST_PROGRESS = "\n".join(
    "tests/test_module_%03d.py ....................... [ %2d%%]" % (i, i % 100)
    for i in range(400)
)
FAILED_TEST = "FAILED tests/cli/test_cli_version_flag.py::test_it_prints_the_version"
PYTEST_FAILURE = (
    "=================================== FAILURES ===================================\n"
    "E   AssertionError: expected 1 got 0\n"
    "=========================== short test summary info ===========================\n"
    "%s\n"
    "= 7 failed, 10992 passed, 3 skipped, 1 xfailed in 737.45s =\n" % FAILED_TEST
)
COVERAGE_TABLE = "\n".join(
    "src/mac/module_%03d.py%s500     40    92%%" % (i, " " * 8) for i in range(235)
)
FAILING_RUN = (
    "============================= test session starts ==============================\n"
    "collected 11003 items\n"
    + PYTEST_PROGRESS
    + "\n"
    + PYTEST_FAILURE
    + COVERAGE_TABLE
    + "\ncoverage safety: statements 70465/77526 (90.89%, floor 90.00%); "
    "branches 20641/25112 (82.20%, floor 80.00%)\n"
    "  - Uploading files to /sandbox...\n"
    "Error:   x ssh exited with status exit status: 1"
)


def test_the_publication_gates_second_reduction_no_longer_loses_the_verdict():
    """The bug, stated end to end.

    Capture, then reduce again the way the publication path does. The old
    `[-2000:]` there is asserted to fail on the same input, so this cannot
    quietly pass because the excerpt happened to get small.
    """
    excerpt = _hub_verify_output_excerpt(FAILING_RUN)
    assert hub_verification_unavailable_reason(excerpt) is None

    blind_tail = excerpt[-2000:]
    assert hub_verification_unavailable_reason(blind_tail) == "ssh exited with status"
    assert FAILED_TEST not in blind_tail

    anchored = capture_failure_window(excerpt)
    assert hub_verification_unavailable_reason(anchored) is None
    assert FAILED_TEST in anchored
    assert "7 failed, 10992 passed" in anchored


def test_short_output_is_returned_verbatim():
    """Most gate failures are small; eliding them would add noise for nothing."""
    assert capture_failure_window("boom", limit=100) == "boom"


def test_a_reason_in_the_middle_survives_when_both_ends_do_not_reach_it():
    kept = capture_failure_window(FAILING_RUN, limit=6000)

    assert FAILED_TEST in kept
    assert "short test summary info" in kept
    assert "chars omitted" in kept  # the elision is stated, not silent
    assert kept.startswith("============================= test session starts")
    assert kept.endswith("ssh exited with status exit status: 1")


def test_caller_anchors_outrank_the_generic_ones():
    """How a caller keeps its own classifier fed: pass the strings it keys on."""
    body = "%s\nWIDGET REGISTRY IS STALE\n%s" % ("a" * 40000, "b" * 40000)

    assert "WIDGET REGISTRY IS STALE" not in capture_failure_window(body)
    assert "WIDGET REGISTRY IS STALE" in capture_failure_window(
        body, anchors=("widget registry is stale",)
    )


def test_the_first_matching_anchor_wins_even_against_the_byte_budget():
    """A budget that can evict the only anchor is a budget that reintroduces
    this bug on a large enough log. The head and tail yield instead."""
    body = "%s\nshort test summary info\nFAILED tests/t.py::t\n%s" % (
        "a" * 40000,
        "b" * 40000,
    )

    kept = capture_failure_window(body, limit=200, head=100, tail=100)

    assert "FAILED tests/t.py::t" in kept


def test_lower_priority_anchors_yield_once_the_budget_is_spent():
    """The budget still binds for everything after the first match, so one
    pathological log cannot expand into the whole transcript."""
    body = "\n".join(
        ["z" * 4000] + ["%s\n%s" % (anchor, "z" * 4000) for anchor in CONTRACT_FAILURE_ANCHORS]
    )

    kept = capture_failure_window(body, limit=8000)

    assert len(kept) < len(body)
    # The highest-priority anchor present is the pytest FAILURES banner.
    assert "= failures =" in kept.lower()


def test_no_anchor_anywhere_degrades_to_head_and_tail():
    body = "%s%s" % ("a" * 30000, "b" * 30000)

    kept = capture_failure_window(body, limit=4000, head=1000, tail=1000)

    assert kept.startswith("a" * 1000)
    assert kept.endswith("b" * 1000)
    assert "[58000 chars omitted]" in kept


def test_empty_and_none_are_not_errors():
    assert capture_failure_window("") == ""
    assert capture_failure_window(None) == ""  # type: ignore[arg-type]
