"""The gate must say what it actually did, not what it was configured to do.

Three multi-hour investigations this month ended at the same wall: the hub-side
configuration looked correct, and nothing in the run reported the value that was
actually enforced, the time actually spent, or where the selected tests actually
came from. Each one is one log line.

  * MAC_WORKER_REPOSITORY_TEST_TIMEOUT was 5400 on the host for three
    consecutive canary attempts while the sandbox enforced 1800, because the
    variable was never forwarded in. Every attempt failed with "timed out after
    1800.0s" against a file that plainly said 5400, and the investigation
    stopped at the file each time.

  * "OpenShell hangs on macOS" was the leading suspect for a day. Nothing hung;
    the test phase was simply longer than its budget. Per-phase elapsed would
    have said so immediately.

  * A "focused, 11 tests" selection sounded narrowed. Those 11 paths held 713
    tests, 578 of them from ONE always_run guard. The resolver reported only a
    total, so the decomposition had to be rebuilt by hand.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mac import executor_sandbox

ROOT = Path(__file__).resolve().parents[1]


def _runner_source() -> str:
    return executor_sandbox._sandbox_repository_verification_shell(
        {"MAC_TASK_WORKSPACE": "/sandbox/task", "MAC_TASK_FILE": "/sandbox/task/task.json"}
    )


def test_the_sandbox_reports_the_timeout_it_will_enforce():
    """Not the configured value -- the resolved one, at the point that
    enforces it."""
    source = _runner_source()

    assert "_effective_timeout" in source
    assert "test timeout:" in source
    assert "bootstrap timeout:" in source


def test_the_timeout_report_names_its_source():
    """ "1800.0s" alone is what made the host configuration look authoritative.
    "1800.0s (default; MAC_WORKER_REPOSITORY_TEST_TIMEOUT unset)" is the
    sentence that ends the investigation."""
    source = _runner_source()

    assert "default (%s unset)" in source
    assert "_test_timeout_source" in source


def test_the_sandbox_reports_the_baseline_it_resolved():
    """An unresolved baseline silently escalates every task to the whole-repo
    gate, which is indistinguishable from a slow gate unless it is said out
    loud."""
    source = _runner_source()

    assert "baseline sha:" in source
    assert "unresolved" in source


def test_each_phase_reports_its_elapsed_time():
    """ "Is it hung or is it slow" cost a day. The answer is a subtraction."""
    source = _runner_source()

    assert "phase bootstrap: start" in source
    assert "phase tests: start" in source
    assert "phase bootstrap: %.1fs" in source
    assert "phase tests: %.1fs" in source


def test_a_timed_out_phase_says_which_budget_it_blew():
    source = _runner_source()

    assert "TIMED OUT" in source


def test_the_resolver_splits_the_selection_by_provenance():
    """A selection whose cost is almost entirely cross-cutting guards is not
    narrowed in any way that helps, and a total cannot say so."""
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/resolve-impacted-tests.py",
            "--base",
            "HEAD",
            "--changed-file",
            "tests/test_agent_ownership.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    document = json.loads(completed.stdout)
    if document["mode"] != "focused":
        return  # a full run has no selection to decompose

    provenance = document.get("provenance")
    assert provenance is not None
    assert "tests/test_agent_ownership.py" in provenance["impact"]
    assert "tests/test_control_plane_public_contract.py" in provenance["always_run"]

    # Every selected test is accounted for by exactly one bucket.
    assert sorted(provenance["impact"] + provenance["always_run"]) == sorted(document["tests"])


def test_the_sanity_runner_prints_the_split():
    script = (ROOT / "scripts" / "run-sanity-tests.sh").read_text(encoding="utf-8")

    assert "provenance:" in script
    assert "always_run guards" in script
