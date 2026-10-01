"""The verifier's captured output must keep the part that says why it failed.

`run-contract-tests.sh` prints the pytest failure FIRST, then an unconditional
whole-repo `coverage report` (~14KB), then the coverage safety summary, and
only then exits with the saved pytest status. A blind tail of that output
keeps the coverage table and OpenShell's generic "ssh exited with status 1",
and drops the failure. Observed 2026-08-20: six tasks retried 3-4 times each
over ~6 hours because nobody could read why they failed.
"""

from __future__ import annotations

import subprocess


from mac import gitops, services

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
        if argv[0] in ("git", "tar", "bash"):
            return done(0)
        if "delete" in argv:
            return done(0)
        return done(1, FAILING_RUN, UPLOAD_AND_SSH)  # the sandbox create+run

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setattr(gitops, "askpass_remote_auth", lambda url: (url, {}), raising=False)


def _capture(monkeypatch):
    _fake_run(monkeypatch)
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE",
        "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
    )
    return services.run_repository_contract_test_in_openshell(
        "https://example.invalid/r.git", "b", HEAD_SHA, "scripts/run-contract-tests.sh"
    )


def test_the_captured_evidence_names_which_test_failed(monkeypatch):
    """A rejection an operator cannot act on is barely better than no verdict:
    diagnosing one such run ended with the sandbox gone and nothing recorded."""
    _returncode, output = _capture(monkeypatch)

    assert "short test summary info" in output
    assert "3 failed, 1204 passed" in output


def test_hub_verify_runs_bootstrap_before_test_command(monkeypatch):
    """A repository whose test.command assumes a pre-built toolchain (e.g.
    ``.venv/bin/pytest``) is unrunnable in a fresh sandbox unless
    bootstrap.command has already created that toolchain. Observed live on
    the mac-fleet-canary repository: every hub_verify attempt failed with
    ``.venv/bin/pytest: No such file or directory`` (exit 127) because the
    sandbox never ran bootstrap.command -- permanently stranding every task
    that used a venv-relative test command."""
    captured_argv = []

    def run(argv, **kwargs):
        done = lambda rc, out="", err="": subprocess.CompletedProcess(argv, rc, out, err)
        if argv[0] == "git" and "rev-parse" in argv:
            return done(0, HEAD_SHA + "\n")
        if argv[0] in ("git", "tar", "bash") or "create" in argv or "upload" in argv:
            return done(0)
        if "delete" in argv:
            return done(0)
        captured_argv.append(argv)
        return done(0, "all passed")

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setattr(gitops, "askpass_remote_auth", lambda url: (url, {}), raising=False)
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE",
        "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
    )
    services.run_repository_contract_test_in_openshell(
        "https://example.invalid/r.git",
        "b",
        HEAD_SHA,
        ".venv/bin/pytest -q",
        'python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"',
    )

    bootstrap = next(argv[-1] for argv in captured_argv if "python3 -m venv .venv" in argv[-1])
    test = next(argv[-1] for argv in captured_argv if ".venv/bin/pytest -q" in argv[-1])
    assert "tar xzf repo.tgz" in bootstrap
    assert "tar xzf repo.tgz" not in test


def test_hub_verify_refuses_the_obsolete_local_hermes_image(monkeypatch):
    monkeypatch.setenv("MAC_HUB_VERIFY_IMAGE", "localhost/mac-hermes:net")

    rc, output = services.run_repository_contract_test_in_openshell(
        "https://example.invalid/r.git",
        "b",
        HEAD_SHA,
        "true",
    )

    assert rc == 1
    assert "immutable repository-owned OpenShell runtime image" in output
