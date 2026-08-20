"""The verdict must survive the capture, not just the classifier.

#478 then #522 argued about which strings mean "the harness died" versus "the
gate judged the change". Both reasoned about the text the classifier receives.
Neither looked at how that text is produced.

`_hub_verify_run_contract_test` kept `out[-2000:]` -- a blind tail. And
`run-contract-tests.sh` prints the pytest failure FIRST, then an unconditional
whole-repo `coverage report` (235 source files, ~14KB), then the coverage
safety summary, and only then exits with the saved pytest status. So on every
test failure the 2000 surviving bytes hold the coverage table's tail, a
coverage line whose floors both PASSED, and OpenShell's generic
"ssh exited with status 1".

Every signature in _HUB_VERIFY_VERDICT_SIGNATURES lives in the discarded
prefix. The rejection is therefore unclassifiable by construction: read as a
dead harness, no verdict signed, review retried, identical outcome. Observed
2026-08-20: six tasks, 3-4 identical retries each over ~6 hours, `completed`
frozen at 724 while all three agents sat idle.

The head+tail excerpt this repo already had (_hub_review_failure_excerpt, and
its docstring makes exactly this argument) was applied downstream of the
truncation -- head and tail of a tail.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from mac import gitops, services
from mac.services import ControlPlane, hub_verification_unavailable_reason

HEAD_SHA = "a" * 40

# A whole-repo `coverage report` is one row per source file. It is emitted
# after the failure and before the exit, so it is what a blind tail keeps.
COVERAGE_TABLE = "\n".join(
    "src/mac/module_%03d.py%s500     40    92%%" % (i, " " * 8) for i in range(235)
)

# pytest's own progress output, which precedes the failure it reports.
PYTEST_PROGRESS = "\n".join(
    "tests/test_module_%03d.py ......................... [ %2d%%]" % (i, i % 100)
    for i in range(400)
)

PYTEST_FAILURE = (
    "=================================== FAILURES ===================================\n"
    "E   AssertionError: expected 1 got 0\n"
    "=========================== short test summary info ===========================\n"
    "FAILED tests/test_task_batch.py::test_the_preview_and_the_apply_agree\n"
    "3 failed, 1204 passed, 11 skipped in 612.44s\n"
)

FAILING_RUN = (
    "============================= test session starts ==============================\n"
    "collected 1218 items\n"
    + PYTEST_PROGRESS
    + "\n"
    + PYTEST_FAILURE
    + COVERAGE_TABLE
    + "\ncoverage safety: statements 70802/77880 (90.91%, floor 90.00%); "
    "branches 20708/25192 (82.20%, floor 80.00%)\n"
)

UPLOAD_AND_SSH = (
    "  - Uploading files to /sandbox...\n  + Files uploaded\n"
    "Error:   x ssh exited with status exit status: 1"
)


def _fake_run(monkeypatch):
    """Stub only the process boundary, so the real capture path is exercised."""

    def run(argv, **kwargs):
        done = lambda rc, out="", err="": subprocess.CompletedProcess(argv, rc, out, err)
        if argv[0] == "git" and "rev-parse" in argv:
            return done(0, HEAD_SHA + "\n")
        if argv[0] in ("git", "tar"):
            return done(0)
        if "delete" in argv:
            return done(0)
        return done(1, FAILING_RUN, UPLOAD_AND_SSH)  # the sandbox create+run

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setattr(
        gitops, "askpass_remote_auth", lambda url: (url, {}), raising=False
    )


def _capture(monkeypatch):
    _fake_run(monkeypatch)
    plane = types.SimpleNamespace()  # no _hub_verify_runner -> the real path
    return ControlPlane._hub_verify_run_contract_test(
        plane, "https://example.invalid/r.git", "b", HEAD_SHA, "scripts/run-contract-tests.sh"
    )


def test_a_failing_gate_is_classified_as_a_rejection_not_a_dead_harness(monkeypatch):
    """The bug, stated as the behaviour that matters."""
    returncode, output = _capture(monkeypatch)

    assert returncode == 1
    assert hub_verification_unavailable_reason(output) is None


def test_the_captured_evidence_names_which_test_failed(monkeypatch):
    """A rejection an operator cannot act on is barely better than no verdict:
    diagnosing one such run ended with the sandbox gone and nothing recorded."""
    _returncode, output = _capture(monkeypatch)

    assert "short test summary info" in output
    assert "3 failed, 1204 passed" in output


def test_the_blind_tail_that_caused_this_would_still_fail(monkeypatch):
    """Guards the fix against being reverted to a tail with a bigger number:
    the coverage table grows with the repo, so any fixed tail loses this race."""
    assert hub_verification_unavailable_reason(
        (FAILING_RUN + UPLOAD_AND_SSH)[-2000:]
    ) == "ssh exited with status"


def test_a_genuinely_dead_harness_is_still_unavailable(monkeypatch):
    """#522 must survive this fix: no gate ran, so there is no verdict to sign."""
    monkeypatch.setattr(
        gitops, "askpass_remote_auth", lambda url: (url, {}), raising=False
    )

    def run(argv, **kwargs):
        done = lambda rc, out="", err="": subprocess.CompletedProcess(argv, rc, out, err)
        if argv[0] == "git" and "rev-parse" in argv:
            return done(0, HEAD_SHA + "\n")
        if argv[0] in ("git", "tar") or "delete" in argv:
            return done(0)
        return done(1, "", "Error:   x ssh exited with status exit status: 255")

    monkeypatch.setattr(services.subprocess, "run", run)
    plane = types.SimpleNamespace()
    _rc, output = ControlPlane._hub_verify_run_contract_test(
        plane, "https://example.invalid/r.git", "b", HEAD_SHA, "scripts/run-contract-tests.sh"
    )

    assert hub_verification_unavailable_reason(output) == "ssh exited with status"


def test_the_excerpt_survives_the_second_truncation_on_the_way_to_the_worker(
    monkeypatch,
):
    """The rejection feedback is the only thing the worker gets to read.

    It used to be re-excerpted on the way out. Head-and-tail of an excerpt
    whose middle IS the answer removes the answer, and a worker that cannot
    see the reason answers a rejection with more tests and a bigger diff --
    observed twice on one task.
    """
    _returncode, output = _capture(monkeypatch)

    feedback = "hub contract verification failed (rc=%d): %s\n\n%s" % (
        1, "scripts/run-contract-tests.sh", output.strip() or "nonzero exit",
    )

    assert "3 failed, 1204 passed" in feedback
    assert "test_the_preview_and_the_apply_agree" in feedback
