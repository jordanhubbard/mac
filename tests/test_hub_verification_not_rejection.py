"""A verification that could not run must not be signed as a rejection.

On 2026-08-19 every review in a 90-minute window was rejected -- 7 of 7 -- and
the fleet could complete nothing. The signed verdict for one of them ended:

    coverage safety: statements 69300/76238 (90.90%, floor 90.00%);
                     branches   20216/24618 (82.12%, floor 80.00%)
      - Uploading files to /sandbox...
      + Files uploaded
    Error:   x ssh exited with status exit status: 1

Both coverage floors PASSED. The gate the run exists to enforce was satisfied,
and then the coding agent's ssh stream died. That exit status became
``rejected``, was SIGNED, and downstream was indistinguishable from a reviewer
judging the work deficient.

The cost was not abstract: task_832c4d72 was rejected, redone more thoroughly
(58 tests -> 60, 2 files -> 11, ruff and a CodeGraph audit added), and rejected
identically. Four cycles, 7.9M tokens, against a verdict that never depended on
the diff.
"""

from __future__ import annotations

import pytest

from mac.services import hub_verification_unavailable_reason


def test_the_observed_transport_fault_is_recognised():
    """The exact tail of the verdict that blocked the fleet."""
    output = (
        "coverage safety: statements 69300/76238 (90.90%, floor 90.00%); "
        "branches 20216/24618 (82.12%, floor 80.00%)\n"
        "  - Uploading files to /sandbox...\n"
        "  + Files uploaded\n"
        "Error:   x ssh exited with status exit status: 1\n"
    )
    assert hub_verification_unavailable_reason(output) == "ssh exited with status"


@pytest.mark.parametrize(
    "output",
    [
        "Connection reset by peer",
        "connection refused",
        "RetriableError: [resource_exhausted] Error",
        "no acceptable coding agent available/authed; executor will fail closed",
        "coding-agent sandbox preflight (codex): FAILED (rc=1, class=sandbox_policy_denied)",
    ],
)
def test_other_harness_faults_are_recognised(output):
    assert hub_verification_unavailable_reason(output) is not None


@pytest.mark.parametrize(
    "output",
    [
        "FAILED tests/test_thing.py::test_case - AssertionError: 1 != 2\n3 failed, 5 passed",
        "coverage safety: statements 60000/76238 (78.70%, floor 90.00%)",
        "E   ImportError: cannot import name 'thing' from 'mac.module'",
        "ruff: 4 errors found",
    ],
)
def test_a_real_failure_is_still_a_rejection(output):
    """The gate must keep working. These are verdicts about the CHANGE."""
    assert hub_verification_unavailable_reason(output) is None


def test_an_unrecognised_failure_stays_a_rejection():
    """Deliberately narrow, and this is the reason.

    Treating unknown failures as infrastructure would let a genuinely broken
    change read as "could not verify" and retry forever -- failing OPEN on the
    gate this whole path exists to enforce. An unfamiliar failure is a
    rejection until someone adds its signature on purpose.
    """
    assert hub_verification_unavailable_reason("segmentation fault (core dumped)") is None
    assert hub_verification_unavailable_reason("") is None
    assert hub_verification_unavailable_reason(None) is None


def test_the_signature_list_is_about_the_harness_not_the_change():
    """A signature naming a test outcome would silently disable the gate."""
    from mac.services import _HUB_VERIFY_UNAVAILABLE_SIGNATURES

    forbidden = ("assertionerror", "failed", "coverage", "error:", "traceback")
    for signature in _HUB_VERIFY_UNAVAILABLE_SIGNATURES:
        for word in forbidden:
            assert signature != word, (
                "%r would classify ordinary test failures as infrastructure and "
                "turn every rejection into a retry" % signature
            )


def test_feedback_leads_with_the_command_and_exit_status():
    """The reason must not be on the last line of a coverage dump.

    A worker that reads thousands of lines of passing coverage and one
    truncated 'rc' concludes it did not try hard enough. One did exactly that,
    twice.
    """
    import inspect

    from mac import services

    source = inspect.getsource(services.ControlPlane._run_hub_review_verification_locked)
    assert "hub contract verification failed (rc=%d): %s" in source, (
        "rejection feedback must lead with the failing command and its exit status"
    )
