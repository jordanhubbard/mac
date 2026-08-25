"""A gate that judged the change is a REJECTION; a harness that died is not.

These two fixes point in opposite directions and the same string sits between
them, so each one is easy to make at the other's expense.

#478 (2026-08-19): a coding agent's ssh stream died AFTER the coverage gate
passed. The non-zero exit was signed as `rejected`, indistinguishable from a
reviewer judging the work deficient. One task was rejected, redone far more
thoroughly, and rejected identically, because the verdict never depended on
the diff.

The fix listed `ssh exited with status` as "verification unavailable". But that
string is the generic wrapper exit printed whenever a remote command returns
non-zero -- it accompanies every failure through the ssh transport, not only a
transport fault. So it inverted the bug: genuine rejections were swallowed as
"could not verify", no verdict was signed, and tasks sat in REVIEWING forever.
Observed 2026-08-20: twelve such events in ninety minutes, twenty tasks
accumulated in REVIEWING, five of them past a hundred hours.
"""

from __future__ import annotations

import pytest

from mac.services import (
    _HUB_VERIFY_VERDICT_SIGNATURES,
    hub_verification_unavailable_reason,
)

SSH_TAIL = "\nError:   x ssh exited with status exit status: 1"


# --- a real verdict is a rejection, whatever the transport did afterwards ---


@pytest.mark.parametrize(
    "output",
    [
        "documentation contract failed: published shell fences outside the "
        "executable book are forbidden (docs/x.md:132)",
        "documentation-inventory.md is stale: regenerate with "
        "scripts/generate-docs-reference.py --write",
        "FAILED tests/test_x.py::test_y\n3 failed, 40 passed in 9.1s",
        "E   AssertionError: expected 1 got 0",
    ],
)
def test_a_judged_failure_is_a_rejection_even_through_a_dying_stream(output):
    """The 2026-08-20 regression. Each of these is real and actionable, and
    each was discarded as 'could not verify'."""
    assert hub_verification_unavailable_reason(output + SSH_TAIL) is None


# --- a harness that died is not a rejection --------------------------------


def test_the_478_case_stays_unavailable():
    """Coverage PASSED, then the stream died. No verdict exists to sign."""
    output = (
        "coverage safety: statements 69300/76238 (90.90%, floor 90.00%); "
        "branches 20216/24618 (82.12%, floor 80.00%)\n"
        "  + Files uploaded" + SSH_TAIL
    )
    assert hub_verification_unavailable_reason(output) == "ssh exited with status"


@pytest.mark.parametrize(
    "output,expected",
    [
        ("Starting sandbox\nconnection reset by peer", "connection reset by peer"),
        ("connection refused", "connection refused"),
        ("no acceptable coding agent", "no acceptable coding agent"),
        ("error: could not create sandbox", "error: could not create sandbox"),
    ],
)
def test_transport_and_environment_faults_stay_unavailable(output, expected):
    assert hub_verification_unavailable_reason(output) == expected


def test_an_unrecognised_failure_is_still_a_rejection():
    """Fail CLOSED on the gate: an unknown failure must not become
    'infrastructure' and retry forever."""
    assert hub_verification_unavailable_reason("something nobody has seen") is None


# --- the discipline that keeps this correct --------------------------------


@pytest.mark.parametrize("signature", sorted(_HUB_VERIFY_VERDICT_SIGNATURES))
def test_no_verdict_signature_appears_in_passing_output(signature):
    """Every verdict signature must appear ONLY on failure.

    This is the trap. `coverage safety:` is emitted whether the floors pass or
    fail, and `repository contract` appears in "running fail-fast repository
    contract preflight" -- a start message. Either would mark the #478
    passed-then-died run as a rejection, reintroducing the exact bug.
    """
    passing = (
        "sanity selection: full\n"
        "run-contract-tests.sh: running fail-fast repository contract preflight\n"
        "  + Files uploaded\n"
        "coverage safety: statements 70371/77427 (90.89%, floor 90.00%); "
        "branches 20606/25076 (82.17%, floor 80.00%)\n"
        "595 passed in 12.11s\n"
    ).lower()
    assert signature not in passing, (
        "%r appears in PASSING output, so it would mark a transport fault as a "
        "rejection -- the #478 bug" % signature
    )
