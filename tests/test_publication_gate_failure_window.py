"""The publication gate must keep the reason it refused, not just the refusal.

The hub's contract verification stopped keeping a blind ``out[-2000:]`` and
started keeping an anchored window around the text that announces the failure.
That fix was applied at the capture site -- and then undone one layer
downstream.

``validate_projected_merge_contract`` runs the SAME verification at publication
time (``_hub_verify_run_contract_test`` is its default ``test_runner``) and
stored the result as ``output_tail[-2000:]``. A failing contract run prints the
pytest failure, then a whole-repo coverage report (one row per source file,
~14KB), then a coverage summary whose floors both PASSED, and only then exits.
So the surviving 2000 bytes were coverage rows and a passing coverage line: the
anchored window was cut back out and the publication record said nothing.

The operator-facing half was worse. ``diagnosis = error or output_tail`` reads
as "the output when there is no error", but ``error`` is the FIXED string
"full repository contract test failed" whenever the suite ran and failed -- so
the output was unreachable by construction and every refused publication was
reported with the same eight words.

Both halves are asserted here against a realistic run: several hundred lines of
pytest progress, the failure, a large coverage table, and a passing coverage
line at the end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac import contract_output, services
from mac.merge_queue import validate_projected_merge_contract
from mac.models import ValidationError
from mac.services import ControlPlane, hub_verification_unavailable_reason
from tests.test_publication_pull_request import (
    FakeForge,
    build_repo,
    drive_to_approval,
    install_forge,
)

# Everything a failing contract run prints, in the order it prints it.
PYTEST_PROGRESS = "\n".join(
    "tests/test_module_%03d.py ......................... [ %2d%%]" % (i, i % 100)
    for i in range(400)
)
PYTEST_FAILURE = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_projected_merge.py::test_the_two_branches_agree\n"
    "= 4 failed, 9812 passed, 3 skipped in 1204.55s (0:20:04) =\n"
)
COVERAGE_TABLE = "\n".join(
    "src/mac/module_%03d.py%s500     40    92%%" % (i, " " * 8) for i in range(235)
)
COVERAGE_PASSED = (
    "coverage safety: statements 70802/77880 (90.91%, floor 90.00%); "
    "branches 20708/25192 (82.20%, floor 80.00%)\n"
    "Error:   x ssh exited with status exit status: 1"
)
FAILING_RUN = (
    "============================= test session starts ==============================\n"
    "collected 9819 items\n"
    + PYTEST_PROGRESS
    + "\n"
    + PYTEST_FAILURE
    + COVERAGE_TABLE
    + "\n"
    + COVERAGE_PASSED
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
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
    _git(r, "commit", "-qm", "topic")
    _git(r, "checkout", "-q", "main")
    return r


def _failed_gate(repo: Path):
    return validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (1, FAILING_RUN),
    )


def test_the_gate_keeps_the_test_that_failed(repo: Path):
    """The bug, stated as the behaviour that matters."""
    verdict = _failed_gate(repo)

    assert verdict.passed is False
    assert "short test summary info" in verdict.output_tail
    assert "test_the_two_branches_agree" in verdict.output_tail
    assert "4 failed, 9812 passed" in verdict.output_tail


def test_the_blind_tail_that_caused_this_would_still_fail(repo: Path):
    """Guards against a revert to a tail with a bigger number: the coverage
    table grows with the repository, so any fixed tail loses this race."""
    blind = FAILING_RUN[-2000:]

    assert "test_the_two_branches_agree" not in blind
    assert "4 failed, 9812 passed" not in blind
    # ...and what it kept instead reads as a healthy run that lost its ssh.
    assert "floor 90.00%" in blind
    assert hub_verification_unavailable_reason(blind) == "ssh exited with status"


def test_the_kept_output_stays_classifiable_as_a_rejection(repo: Path):
    """The point of anchoring: a verdict signature survives truncation, so the
    gate's own classifier still calls this a rejection rather than a dead
    harness that should be retried forever."""
    verdict = _failed_gate(repo)

    assert hub_verification_unavailable_reason(verdict.output_tail) is None


def test_the_gate_is_still_bounded(repo: Path):
    """Not a licence to store the whole run: the record is bounded, it is just
    no longer bounded by position alone."""
    verdict = _failed_gate(repo)

    assert len(verdict.output_tail) < len(FAILING_RUN) // 2
    assert "chars omitted" in verdict.output_tail


def test_a_short_run_is_kept_verbatim(repo: Path):
    """Runs that fit are not excerpted, so the common case reads unchanged."""
    verdict = validate_projected_merge_contract(
        str(repo),
        "main",
        "topic",
        "scripts/run-contract-tests.sh",
        test_runner=lambda *_args: (17, "contract test failed: 1 failed, 2 passed"),
    )

    assert verdict.output_tail == "contract test failed: 1 failed, 2 passed"
    assert verdict.error == "full repository contract test failed"


def test_a_refused_publication_reports_the_cause_and_the_evidence(
    tmp_path, monkeypatch
):
    """``error or output_tail`` never reached the output, because ``error`` is
    always set once the suite has run. The operator got eight fixed words."""
    cp = ControlPlane.in_memory()
    remote, source, _main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    # No required forge checks -> the hub's own contract gate is the gate.
    install_forge(monkeypatch, forge, checks=())
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    cp._publication_merge_test_runner = lambda *a, **k: (1, FAILING_RUN)

    with pytest.raises(ValidationError) as raised:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    message = str(raised.value)
    assert "full repository contract test failed" in message  # the named cause
    assert "test_the_two_branches_agree" in message           # ...and the reason


def test_both_gates_share_one_capture_rule():
    """The review path and the publication path must not drift apart again:
    the anchored capture is one implementation, re-exported under the name
    `services` already used it by."""
    assert services._hub_verify_output_excerpt is contract_output.failure_window_excerpt
    assert services.hub_verification_unavailable_reason is contract_output.unavailable_reason
    assert services._HUB_VERIFY_VERDICT_SIGNATURES is contract_output.VERDICT_SIGNATURES
    assert contract_output.FAILURE_ANCHORS[0] == "short test summary info"
