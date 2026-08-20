"""The hub threw away the reason it failed BEFORE anything classified it.

`_hub_verify_run_contract_test` captured `out[-2000:]` -- a blind tail -- and
then deleted the sandbox that produced it. On this repository's gate that tail
is structurally guaranteed to contain no failure:

    run-contract-tests.sh
      1. prints the pytest failure                       <- the reason
      2. prints an unconditional whole-repo `coverage report`  (~14KB)
      3. prints a coverage summary whose floors both PASSED
      4. exits with the pytest status saved in step 1

so the surviving 2000 bytes are the coverage table's last rows, a PASSING
coverage line, and OpenShell's generic "ssh exited with status 1". Every string
in `_HUB_VERIFY_VERDICT_SIGNATURES` lives in the discarded prefix, which made a
genuine rejection unclassifiable BY CONSTRUCTION: recorded as a dead harness,
no verdict signed, review retried, identical output produced again.

Observed 2026-08-20: six tasks retried 3-4 times each over ~6 hours while
`completed` stayed frozen at 724, `reviewing` sat at 19, and all three agents
were idle. PRs #478 and #522 both changed only how the surviving text was
CLASSIFIED, and neither moved the fleet, because by then the text was gone.

Head-and-tail is not sufficient either -- `test_head_and_tail_still_misses_it`
below is the proof. The failure sits between several hundred lines of pytest
progress and the coverage table, out of reach of both ends.
"""

from __future__ import annotations

import subprocess as _subprocess

import pytest

from mac.contract_failure import capture_failure_window
from mac.services import (
    _HUB_VERIFY_VERDICT_SIGNATURES,
    _hub_review_failure_excerpt,
    HUB_VERIFY_OUTPUT_CAPTURE_LIMIT,
    ControlPlane,
    hub_verification_unavailable_reason,
    hub_verify_captured_output,
)


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()

FAILURE_LINE = (
    "FAILED tests/test_repository_contract.py::test_contract_command "
    "- AssertionError: expected the scoped selection"
)
SSH_TAIL = "Error:   x ssh exited with status exit status: 1"


def _coverage_table(rows: int = 400) -> str:
    """The ~14KB of PASSING per-file rows that displaced the failure."""
    return "\n".join(
        "src/mac/module_%03d.py                 %4d   %3d    %4d    %2d  9%d%%"
        % (index, 300 + index, index % 40, 120 + index, index % 20, index % 10)
        for index in range(rows)
    )


def _failing_contract_run() -> str:
    """One realistic failing run of scripts/run-contract-tests.sh."""
    progress = "\n".join(
        "tests/test_module_%03d.py ....................  [ %2d%%]"
        % (index, min(99, index // 4))
        for index in range(400)
    )
    return "\n".join(
        [
            "run-contract-tests.sh: running fail-fast repository contract preflight",
            "sanity selection: full",
            progress,
            "=================================== FAILURES ===================================",
            "____________________________ test_contract_command _____________________________",
            "E   AssertionError: expected the scoped selection",
            "=========================== short test summary info ============================",
            FAILURE_LINE,
            "1 failed, 4291 passed, 12 skipped in 902.11s",
            "",
            "Name                                  Stmts   Miss  Branch  BrPart  Cover",
            _coverage_table(),
            "TOTAL                                 76238   6938   24618    4402  90.90%",
            "coverage safety: statements 69300/76238 (90.90%, floor 90.00%); "
            "branches 20216/24618 (82.12%, floor 80.00%)",
            "  + Files uploaded",
            SSH_TAIL,
        ]
    )


def _passing_run_that_died_in_transit() -> str:
    """The #478 case at real scale: the gate PASSED, then the stream died.

    No verdict exists to sign here, and capturing more bytes must not invent
    one. This is the failure mode that pulls in the opposite direction from
    everything else in this file, which is why it is tested at the same size.
    """
    progress = "\n".join(
        "tests/test_module_%03d.py ....................  [ %2d%%]"
        % (index, min(99, index // 4))
        for index in range(400)
    )
    return "\n".join(
        [
            "run-contract-tests.sh: running fail-fast repository contract preflight",
            "sanity selection: full",
            progress,
            "4291 passed, 12 skipped in 884.02s",
            "",
            "Name                                  Stmts   Miss  Branch  BrPart  Cover",
            _coverage_table(),
            "TOTAL                                 77427   7056   25076    4470  90.89%",
            "coverage safety: statements 70371/77427 (90.89%, floor 90.00%); "
            "branches 20606/25076 (82.17%, floor 80.00%)",
            "  + Files uploaded",
            SSH_TAIL,
        ]
    )


# --- the bug, stated as an executable fact ---------------------------------

def test_the_blind_tail_could_not_express_a_rejection():
    """What the hub used to keep, and why no classifier could have saved it.

    This is not a test of dead code -- it is the premise. If this ever stops
    holding, the fix below is solving a problem this repository no longer has.
    """
    output = _failing_contract_run()

    blind_tail = output[-2000:]

    assert FAILURE_LINE not in blind_tail
    assert not any(sig in blind_tail.lower() for sig in _HUB_VERIFY_VERDICT_SIGNATURES)
    # ...so the run that failed on a real assertion was filed as a dead harness.
    assert hub_verification_unavailable_reason(blind_tail) == "ssh exited with status"


def test_head_and_tail_still_misses_it():
    """Why the fix anchors instead of widening both ends.

    The failure is announced after several hundred lines of pytest progress and
    before ~14KB of coverage table. A generous head and a generous tail both
    stop short of it.
    """
    output = _failing_contract_run()

    head_and_tail = output[:2000] + output[-1500:]

    assert FAILURE_LINE not in head_and_tail


# --- the fix ---------------------------------------------------------------

def test_the_captured_output_carries_the_failure():
    captured = hub_verify_captured_output(_failing_contract_run())

    assert FAILURE_LINE in captured
    assert "AssertionError: expected the scoped selection" in captured
    # The gate judged the change wanting, so this is a REJECTION -- even though
    # the transport's "ssh exited with status" is still present in the tail.
    assert SSH_TAIL in captured
    assert hub_verification_unavailable_reason(captured) is None


@pytest.mark.parametrize("signature", sorted(_HUB_VERIFY_VERDICT_SIGNATURES))
def test_every_verdict_signature_survives_being_buried(signature):
    """The property the classifier depends on.

    A signature the capture drops is a signature that cannot be classified, no
    matter how carefully `hub_verification_unavailable_reason` is written. So
    each one is buried in the exact position that defeated the blind tail --
    the middle -- and must come back out.
    """
    noise = "\n".join("progress line %05d ......" % index for index in range(4000))
    output = "%s\nthe gate said: %s here\n%s\n%s" % (
        noise, signature, noise, SSH_TAIL
    )

    captured = hub_verify_captured_output(output)

    assert signature in captured.lower()
    assert hub_verification_unavailable_reason(captured) is None


def test_a_transport_death_after_a_pass_is_still_not_a_rejection():
    """The #478 regression, re-run against the wider capture.

    Keeping more bytes is only safe because every verdict signature is required
    to appear ONLY on failure. A passing run has none of them anywhere, so
    seeing more of it cannot manufacture a verdict.
    """
    captured = hub_verify_captured_output(_passing_run_that_died_in_transit())

    assert hub_verification_unavailable_reason(captured) == "ssh exited with status"


def test_the_capture_stays_bounded_and_says_what_it_dropped():
    """Evidence rows are not log files. The window is bounded, and every gap
    is marked so a reader is never silently handed a doctored transcript."""
    output = _failing_contract_run()
    captured = hub_verify_captured_output(output)

    assert len(output) > HUB_VERIFY_OUTPUT_CAPTURE_LIMIT
    # Omission markers are the only thing added, so the bound holds with a
    # small allowance for them.
    assert len(captured) <= HUB_VERIFY_OUTPUT_CAPTURE_LIMIT + 500
    assert "chars omitted" in captured


def test_a_short_failure_is_returned_whole():
    """Most gate failures fit. Eliding them would add noise for nothing."""
    short = "documentation contract failed: published shell fences are forbidden"

    assert hub_verify_captured_output(short) == short
    assert capture_failure_window("") == ""


# --- the excerpt written into the signed manifest --------------------------

def test_the_rejection_feedback_keeps_the_reason_too():
    """`_hub_review_failure_excerpt` re-truncated head-and-tail, which threw
    the reason away a second time -- after the capture had just rescued it."""
    excerpt = _hub_review_failure_excerpt(hub_verify_captured_output(_failing_contract_run()))

    assert FAILURE_LINE in excerpt


def test_the_observation_excerpt_is_small_and_still_useful():
    """The unavailable-path observation asks for head=400/tail=400. It must
    stay small, and it must still be worth reading."""
    excerpt = _hub_review_failure_excerpt(
        hub_verify_captured_output(_failing_contract_run()), head=400, tail=400
    )

    assert FAILURE_LINE in excerpt
    assert len(excerpt) <= 400 + 400 + 4000 + 500


# --- the sandbox boundary itself -------------------------------------------

def test_the_sandbox_runner_captures_instead_of_tailing(cp, monkeypatch):
    """End to end through `_hub_verify_run_contract_test`.

    The sandbox is deleted in that function's `finally`, so anything it does
    not return here is unrecoverable.
    """
    from mac import services as services_mod

    output = _failing_contract_run()

    def fake_run(argv, **kwargs):
        if "rev-parse" in argv and "HEAD" in argv:
            return _subprocess.CompletedProcess(argv, 0, stdout=("a" * 40) + "\n", stderr="")
        if "create" in argv and "--upload" in argv:
            return _subprocess.CompletedProcess(argv, 1, stdout=output, stderr="")
        return _subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(services_mod.subprocess, "run", fake_run)

    returncode, captured = cp._hub_verify_run_contract_test(
        "git@github.com:org/repo.git", "task/branch", "a" * 40, ""
    )

    assert returncode == 1
    assert FAILURE_LINE in captured
    assert hub_verification_unavailable_reason(captured) is None
