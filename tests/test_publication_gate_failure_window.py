"""The publication gate was the last place the failure reason was thrown away.

The hub's capture was fixed to keep an anchored window around the text that
announces a failure, so a rejection stays classifiable as a rejection. The
publication path then ran the SAME output through
``validate_projected_merge_contract``, whose verdict helper took
``output_tail[-2000:]``.

A blind tail of a contract run is its coverage table plus OpenShell's generic
"ssh exited with status 1" -- run-contract-tests.sh prints the pytest failure
first, then an unconditional whole-repo ``coverage report`` (one row per source
file, ~14KB), then a coverage summary whose floors both PASSED, and only then
exits with the saved pytest status. So the second truncation cut the anchored
middle back out and restored the original bug one layer down: a gate that
rejected a change for a real, actionable reason recorded a transport fault.

Observed as the reason a real task was rejected on 2026-08-21 -- the gate had
found "stale generated environment registry: src/mac/data/env_config_registry
.json; run scripts/generate-env-config-registry.py", which is a verdict
signature, and none of it reached the recorded evidence.

The invariant these tests hold is one sentence: every stage that bounds the
same output calls the same anchored capture, and re-applying that capture
never loses the reason.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.contract_failure import (
    FAILURE_ANCHORS,
    VERDICT_SIGNATURES,
    capture_failure_window,
)
from mac.merge_queue import validate_projected_merge_contract
from mac.services import hub_verification_unavailable_reason

# One row per source file, emitted AFTER the failure and BEFORE the exit, so it
# is exactly what a blind tail keeps. It grows with the repository, which is
# why no fixed tail size wins this race.
COVERAGE_TABLE = "\n".join(
    "src/mac/module_%03d.py%s500     40    92%%" % (i, " " * 8) for i in range(235)
)

PYTEST_PROGRESS = "\n".join(
    "tests/test_module_%03d.py ......................... [ %2d%%]" % (i, i % 100)
    for i in range(400)
)

# The reason, sitting between several hundred lines of progress and the
# coverage table -- out of reach of a head and of a tail alike.
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
    "  - Uploading files to /sandbox...\n  + Files uploaded\n"
    "Error:   x ssh exited with status exit status: 1"
)

#: What the hub's runner actually hands the publication gate: already bounded,
#: already relevance-selected. The gate's job is to record it, not to re-cut it.
HUB_CAPTURE = capture_failure_window(FAILING_RUN)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.invalid")
    _git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "topic", "main")
    (r / "topic.txt").write_text("topic\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "topic change")
    _git(r, "checkout", "-q", "main")
    return r


def _gate_verdict(repo: Path, output: str):
    return validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (1, output),
    )


def test_the_recorded_reason_still_classifies_as_a_rejection(repo: Path):
    """The bug, stated as the behaviour that matters."""
    verdict = _gate_verdict(repo, HUB_CAPTURE)

    assert verdict.passed is False
    assert hub_verification_unavailable_reason(verdict.output_tail) is None


def test_the_recorded_reason_names_what_failed(repo: Path):
    """A rejection nobody can act on is barely better than no verdict."""
    verdict = _gate_verdict(repo, HUB_CAPTURE)

    assert "short test summary info" in verdict.output_tail
    assert "3 failed, 1204 passed" in verdict.output_tail
    assert "test_the_preview_and_the_apply_agree" in verdict.output_tail


def test_the_blind_tail_that_caused_this_would_still_fail(repo: Path):
    """Guards against a revert to a tail with a bigger number.

    The coverage table grows with the repository, so the discarded prefix grows
    with it too; only anchoring on the failure text is stable.
    """
    assert (
        hub_verification_unavailable_reason(HUB_CAPTURE[-2000:])
        == "ssh exited with status"
    )


def test_the_gate_still_bounds_what_it_records(repo: Path):
    """Fixing the truncation must not turn the verdict into a 14KB blob.

    ``ProjectedMergeContractVerdict.to_dict()`` is recorded on the task, so an
    unbounded tail would put a whole contract run into the ledger for every
    failed publication.
    """
    verdict = _gate_verdict(repo, FAILING_RUN)

    assert len(FAILING_RUN) > 10_000
    assert len(verdict.output_tail) < len(FAILING_RUN) // 2


def test_applying_the_capture_again_never_loses_the_reason():
    """Why calling it twice (hub, then gate) is safe where slicing twice is not.

    Re-selecting an anchored window converges -- it re-finds the same anchor
    and settles within two passes -- and every pass keeps the reason. A second
    ``[-2000:]`` instead discards it, which is the defect this file is about.
    """
    text = FAILING_RUN
    for _pass in range(4):
        text = capture_failure_window(text)
        assert "short test summary info" in text
        assert hub_verification_unavailable_reason(text) is None

    assert capture_failure_window(text) == text


def test_a_passing_gate_records_its_output_unchanged(repo: Path):
    """Short output is kept verbatim; the window only applies once it must."""
    verdict = validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (0, "full suite passed"),
    )

    assert verdict.passed is True
    assert verdict.output_tail == "full suite passed"


@pytest.mark.parametrize("signature", sorted(VERDICT_SIGNATURES))
def test_every_verdict_signature_is_also_a_capture_anchor(signature: str):
    """The invariant that ties the capture to the classifier.

    A signature the classifier reads but the capture does not anchor on is a
    rejection that survives classification only by luck of position -- which is
    the whole failure mode. Adding one without the other should fail here
    rather than six hours into a retry loop.
    """
    assert signature in FAILURE_ANCHORS
