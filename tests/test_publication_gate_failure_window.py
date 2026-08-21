"""The publication gate must not re-truncate the hub's anchored capture.

`_hub_verify_run_contract_test` selects the bytes of a failing contract run
that say WHY it failed, because a blind tail of that run says nothing: the
pytest failure is printed first, then an unconditional whole-repo `coverage
report` (one row per source file, ~14KB), then a coverage summary whose floors
both PASSED, and only last OpenShell's generic "ssh exited with status 1".

The publication gate calls that same runner, and then applied
``output_tail[-2000:]`` to what came back. A tail of an anchored excerpt is a
tail again -- it cuts out precisely the middle the capture existed to keep --
and `contract_gate.output_tail` is the whole diagnosis a publication failure
carries. So the fix at the hub's capture site was undone one layer down, on the
path that decides whether a change may land.

These tests pin the composition, not either half: whatever the runner hands
back, what the gate records still names the reason.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.contract_failure import capture_failure_window
from mac.merge_queue import validate_projected_merge_contract
from mac.services import hub_verification_unavailable_reason

# A whole-repo `coverage report` is one row per source file, emitted AFTER the
# failure and before the exit. It is what a blind tail keeps.
COVERAGE_TABLE = "\n".join(
    "src/mac/module_%03d.py%s500     40    92%%" % (i, " " * 8) for i in range(235)
)

# pytest's own progress output, which precedes the failure it reports.
PYTEST_PROGRESS = "\n".join(
    "tests/test_module_%03d.py ......................... [ %2d%%]" % (i, i % 100)
    for i in range(400)
)

# The real rejection reason from a run on 2026-08-21, verbatim in shape: a
# generated artifact the change left stale. It is announced once, in the
# middle, and it is actionable -- which is the whole point of keeping it.
STALE_REGISTRY = (
    "stale generated environment registry: src/mac/data/env_config_registry.json, "
    "docs/env-config-reference.md; run scripts/generate-env-config-registry.py"
)

TAIL_AFTER_THE_VERDICT = (
    "\ncoverage safety: statements 70802/77880 (90.91%, floor 90.00%); "
    "branches 20708/25192 (82.20%, floor 80.00%)\n"
    "  - Uploading files to /sandbox...\n  + Files uploaded\n"
    "Error:   x ssh exited with status exit status: 1"
)

FAILING_RUN = (
    "============================= test session starts ==============================\n"
    "collected 1218 items\n"
    + PYTEST_PROGRESS
    + "\n"
    + STALE_REGISTRY
    + "\n=========================== short test summary info ===========================\n"
    "FAILED tests/test_task_batch.py::test_the_preview_and_the_apply_agree\n"
    "3 failed, 1204 passed, 11 skipped in 612.44s\n"
    + COVERAGE_TABLE
    + TAIL_AFTER_THE_VERDICT
)


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
    (r / "f.txt").write_text("line1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "topic", "main")
    (r / "topic.txt").write_text("topic\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "topic change")
    _git(r, "checkout", "-q", "main")
    return r


def _gate(repo: Path, output: str):
    """Run the real gate against a runner that fails with ``output``."""
    return validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (1, output),
    )


def test_the_gate_records_why_the_contract_run_failed(repo: Path):
    """The bug, stated as the behaviour that matters."""
    verdict = _gate(repo, FAILING_RUN)

    assert verdict.passed is False
    assert STALE_REGISTRY in verdict.output_tail


def test_the_recorded_diagnosis_names_the_failing_test(repo: Path):
    """`contract_gate.output_tail` IS the diagnosis a publication failure
    carries (services.py raises `error or output_tail`). A diagnosis nobody can
    act on costs an attempt and teaches the next one nothing."""
    verdict = _gate(repo, FAILING_RUN)

    assert "short test summary info" in verdict.output_tail
    assert "test_the_preview_and_the_apply_agree" in verdict.output_tail
    assert "3 failed, 1204 passed" in verdict.output_tail


def test_the_recorded_output_is_still_bounded(repo: Path):
    """Keeping the reason is not a licence to record the whole run: the tail
    was there to bound a ~50KB capture, and that bound still holds."""
    verdict = _gate(repo, FAILING_RUN)

    assert len(FAILING_RUN) > 20000
    assert len(verdict.output_tail) <= 6000
    assert "chars omitted" in verdict.output_tail


def test_the_blind_tail_that_caused_this_would_still_fail(repo: Path):
    """Guards against a revert to a tail with a bigger number: the coverage
    table grows with the repo, so any fixed tail loses this race."""
    blind = FAILING_RUN[-2000:]

    assert STALE_REGISTRY not in blind
    assert hub_verification_unavailable_reason(blind) == "ssh exited with status"


def test_a_rejection_stays_classifiable_as_a_rejection(repo: Path):
    """The verdict/transport split (#478, #522) only works on text that still
    contains a verdict signature. That is a property of the capture, so it is
    checked on what the gate actually stores."""
    verdict = _gate(repo, FAILING_RUN)

    assert hub_verification_unavailable_reason(verdict.output_tail) is None


def test_the_capture_composes_with_itself(repo: Path):
    """Why the gate may call the same capture the runner already applied.

    Every stage that bounds this text calls `capture_failure_window`, so the
    real path applies it twice. Re-selecting an anchored window re-finds the
    same anchor; slicing a tail of a tail does not.
    """
    once = capture_failure_window(FAILING_RUN)
    verdict = _gate(repo, once)

    assert STALE_REGISTRY in once
    assert STALE_REGISTRY in verdict.output_tail
    assert verdict.output_tail == capture_failure_window(once)


def test_a_short_failure_is_recorded_verbatim(repo: Path):
    """Most gate failures are one line (a clone error, an empty command). They
    must not grow "chars omitted" scaffolding around themselves."""
    verdict = _gate(repo, "full repository contract test failed: boom")

    assert verdict.output_tail == "full repository contract test failed: boom"


def test_a_passing_run_is_bounded_the_same_way(repo: Path):
    """`output_tail` is set on success too. It goes through one capture, so a
    passing run cannot be the path that records an unbounded blob."""
    verdict = validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (0, PYTEST_PROGRESS + "\n" + COVERAGE_TABLE),
    )

    assert verdict.passed is True
    assert len(verdict.output_tail) <= 6000
