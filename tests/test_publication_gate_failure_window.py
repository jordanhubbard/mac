"""The publication gate threw the reason away after the hub verifier kept it.

The hub verifier stopped taking a blind ``out[-2000:]`` and started keeping an
anchored window around the text that announces the failure. The publication
path then ran the SAME verification through
``validate_projected_merge_contract``, which reduced the result with its own
``output_tail[-2000:]`` -- a tail of an excerpt whose middle IS the answer --
and reported a constant ``error`` that the caller reads in preference to the
tail. So a genuine rejection arrived at the operator as:

    git publication contract gate failed on the projected current-main merge:
    full repository contract test failed

which is true of every failing gate and diagnostic of none, with the captured
evidence re-truncated back to the coverage table and "ssh exited with status".

These tests use a realistic failing transcript: the failure is announced
several hundred lines in and followed by a whole-repo coverage table, so any
fixed tail loses the race as the repo grows.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.contract_output import (
    CONTRACT_FAILURE_ANCHORS,
    capture_failure_window,
    failure_reason_line,
)
from mac.merge_queue import validate_projected_merge_contract
from mac.services import hub_verification_unavailable_reason

# One row per source file, printed AFTER the failure and BEFORE the exit --
# which is exactly what a tail keeps and why a tail cannot work here.
COVERAGE_TABLE = "\n".join(
    "src/mac/module_%03d.py%s500     40    92%%" % (i, " " * 8) for i in range(235)
)

PYTEST_PROGRESS = "\n".join(
    "tests/test_module_%03d.py ......................... [ %2d%%]" % (i, i % 100)
    for i in range(400)
)

FAILING_RUN = (
    "============================= test session starts ==============================\n"
    "collected 1218 items\n"
    + PYTEST_PROGRESS
    + "\n=================================== FAILURES ===================================\n"
    "E   AssertionError: expected 1 got 0\n"
    "=========================== short test summary info ===========================\n"
    "FAILED tests/test_task_batch.py::test_the_preview_and_the_apply_agree\n"
    "3 failed, 1204 passed, 11 skipped in 612.44s\n"
    + COVERAGE_TABLE
    + "\ncoverage safety: statements 70802/77880 (90.91%, floor 90.00%); "
    "branches 20708/25192 (82.20%, floor 80.00%)\n"
    "  - Uploading files to /sandbox...\n  + Files uploaded\n"
    "Error:   x ssh exited with status exit status: 1"
)

# The preflight refuses before any test runs, so there is no pytest summary at
# all -- the whole reason is one line. This is what rejected a live task on
# 2026-08-21.
STALE_ARTIFACT_RUN = (
    "run-contract-tests.sh: running fail-fast repository contract preflight\n"
    + PYTEST_PROGRESS
    + "\nstale generated environment registry: src/mac/data/env_config_registry.json, "
    "docs/env-config-reference.md; run scripts/generate-env-config-registry.py\n"
    + COVERAGE_TABLE
    + "\nError:   x ssh exited with status exit status: 1"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "gate@example.invalid")
    _git(root, "config", "user.name", "Gate")
    (root / "base.txt").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "topic")
    (root / "topic.txt").write_text("topic\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "topic")
    _git(root, "checkout", "-q", "main")
    return root


def _gate(repo: Path, output: str):
    return validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (1, output),
    )


@pytest.mark.parametrize(
    "output,expected",
    [
        (FAILING_RUN, "3 failed, 1204 passed"),
        (STALE_ARTIFACT_RUN, "stale generated environment registry"),
    ],
)
def test_the_refusal_names_the_reason_instead_of_a_constant(repo, output, expected):
    """The bug, stated as the behaviour that matters.

    `error` is what the caller reports (`contract_gate.error or output_tail`),
    so a constant here is the whole failure: it is what the operator reads and
    what the eviction record keeps.
    """
    verdict = _gate(repo, output)

    assert verdict.passed is False
    assert expected in verdict.error


def test_the_reason_survives_the_eviction_records_200_character_cut(repo):
    """The eviction reason is cut to 200 characters, so the reason has to be
    at the FRONT of `error`, not appended after an excerpt."""
    verdict = _gate(repo, STALE_ARTIFACT_RUN)

    assert "stale generated environment registry" in verdict.error[:200]


def test_the_captured_output_still_classifies_as_a_rejection(repo):
    """The tail this replaced kept a passing coverage line and a generic
    transport message, so the classifier called a real rejection a dead
    harness -- no verdict signed, review retried, same output again."""
    verdict = _gate(repo, FAILING_RUN)

    assert hub_verification_unavailable_reason(verdict.output_tail) is None
    assert "short test summary info" in verdict.output_tail
    assert "test_the_preview_and_the_apply_agree" in verdict.output_tail


def test_the_blind_tail_this_replaced_would_still_lose_the_reason():
    """Guards the fix against being reverted to a tail with a bigger number."""
    assert hub_verification_unavailable_reason(
        FAILING_RUN[-2000:]
    ) == "ssh exited with status"


def test_the_capture_is_still_bounded(repo):
    """Keeping the reason must not mean keeping the whole run: the verdict is
    stored on the task and read back on every publication sweep."""
    verdict = _gate(repo, FAILING_RUN)

    assert len(verdict.output_tail) < len(FAILING_RUN) // 2
    assert "chars omitted" in verdict.output_tail


def test_the_capture_composes_with_itself():
    """This gate's runner is the hub verifier, which has already captured. A
    second reduction must be a no-op on the reason, or the fix upstream is
    undone here -- which is precisely what happened."""
    once = capture_failure_window(FAILING_RUN)
    twice = capture_failure_window(once)

    assert "3 failed, 1204 passed" in twice
    assert hub_verification_unavailable_reason(twice) is None


def test_a_passing_run_is_bounded_without_an_anchor():
    """No anchor is present in a passing run, so the capture falls back to head
    and tail rather than dropping everything or keeping everything."""
    passing = "start\n" + PYTEST_PROGRESS + "\n" + COVERAGE_TABLE + "\nall good"

    captured = capture_failure_window(passing)

    assert captured.startswith("start")
    assert captured.endswith("all good")
    assert len(captured) < len(passing)


def test_short_output_is_returned_whole():
    assert capture_failure_window("suite failed") == "suite failed"


def test_the_reason_line_skips_pytest_banners():
    """`=== short test summary info ===` announces a section; the count line
    below it states the result. Returning the banner would swap one
    uninformative constant for another."""
    reason = failure_reason_line(FAILING_RUN)

    assert reason == "3 failed, 1204 passed, 11 skipped in 612.44s"


def test_the_reason_line_falls_back_to_the_last_line_when_unrecognised():
    """An unrecognised reason is still a better answer than a fixed sentence;
    the alternative is silently reporting the constant again."""
    assert failure_reason_line("boom: the thing broke") == "boom: the thing broke"
    assert failure_reason_line("") == ""


def test_every_anchor_is_reachable_after_capture():
    """A signature the classifier keys on but the capture drops is the bug in
    miniature, so assert the two agree for each of them."""
    for anchor in CONTRACT_FAILURE_ANCHORS:
        run = PYTEST_PROGRESS + "\nverdict line: %s here\n" % anchor + COVERAGE_TABLE

        assert anchor in capture_failure_window(run).lower(), anchor
