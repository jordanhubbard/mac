"""Repository-wide pytest hooks for opt-in test-portfolio measurement, for
test-gate checkpointing (src/mac/test_checkpoint.py), and for making the
``pythonpath`` ini option reach child processes.

The first two are inert unless the corresponding environment variable is set,
so a plain ``pytest`` invocation behaves exactly as it did before either
existed. The third always runs; see ``_export_ini_pythonpath``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


_PORTFOLIO_RESULTS: dict[str, dict[str, object]] = {}


# --------------------------------------------------------------------------
# Test-gate checkpointing.
#
# Two hooks, both driven by environment variables that only
# scripts/run-contract-tests.sh sets:
#
#   MAC_TEST_CHECKPOINT_SKIP_FILE   newline-delimited test FILE paths whose
#                                   results are being carried forward from a
#                                   previous run; deselect them.
#   MAC_TEST_CHECKPOINT_RESULTS_DIR directory to append this run's per-test
#                                   outcomes into, one JSONL file per process
#                                   so xdist workers never interleave writes.
#
# Deselection happens at collection time, AFTER the modules have been imported,
# so an import error or a collection-time failure inside a carried-forward file
# is still a failure. Skipping is by whole FILE, never by individual test: the
# checkpoint module explains why (module-scoped fixture state).
# --------------------------------------------------------------------------


# True when this interpreter was launched from inside a running test. Captured
# at import time, before any test of THIS session runs, so it can only be true
# for a pytest that some other pytest spawned.
#
# This matters: the suite has tests that shell out to pytest, and they inherit
# MAC_TEST_CHECKPOINT_RESULTS_DIR. Without this guard a mini fixture project's
# deliberately-failing test was recorded into the real repository's checkpoint —
# observed on the first end-to-end smoke run of this feature. Results from a
# process the gate did not schedule must never enter the checkpoint.
_CHECKPOINT_SPAWNED_BY_A_TEST = bool(os.environ.get("PYTEST_CURRENT_TEST"))
_CHECKPOINT_OWNED = False


# --------------------------------------------------------------------------
# Make ``pythonpath = ["src"]`` (pyproject) reach child processes.
#
# The ini option only prepends to the *pytest process's* ``sys.path``. It does
# not touch ``PYTHONPATH``, so a test that shells out to
# ``[sys.executable, "-m", "mac.cli", ...]`` imports whatever ``mac`` that
# interpreter has installed -- NOT the worktree pytest itself imported.
#
# On a dev box the two agree, because .venv carries an editable install. In the
# OpenShell verification sandbox they do not: there is no .venv, the runner
# resolves /opt/mac-venv/bin/python, and that image bakes a released `mac`.
# Every in-process assertion then tests the worktree while every subprocess
# assertion tests the image, and the suite fails on code the diff never
# touched. Observed as `mac --version` exiting 2 with "the following arguments
# are required: SUBCOMMAND" (tests/cli/test_cli_version_flag.py) and as a
# worker child that never releases its lease because the baked worker.py has no
# shutdown_grace_seconds (tests/test_worker_shutdown_abandon.py) -- one cause,
# two unrelated-looking failures, neither reproducible anywhere else.
#
# Exporting the same entries closes the gap for every subprocess at once, so
# individual tests no longer each have to remember to rebuild PYTHONPATH by
# hand (several already do; they keep working -- prepending is idempotent).
# This deliberately does NOT weaken the environment-scrubbing contracts: code
# that strips PYTHONPATH before spawning a managed process still strips it.
# --------------------------------------------------------------------------


def _export_ini_pythonpath(config) -> None:
    """Prepend the ``pythonpath`` ini entries to ``os.environ['PYTHONPATH']``."""

    try:
        entries = config.getini("pythonpath")
    except (ValueError, KeyError):  # option absent on this pytest
        return

    roots: list[str] = []
    for entry in entries or ():
        try:
            resolved = Path(str(config.rootpath), str(entry)).resolve()
        except OSError:
            continue
        if resolved.is_dir():
            roots.append(str(resolved))
    if not roots:
        return

    existing = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    merged = roots + [p for p in existing if p not in roots]
    os.environ["PYTHONPATH"] = os.pathsep.join(merged)


def pytest_configure(config) -> None:
    """Decide whether this session owns the checkpoint recording namespace."""

    _export_ini_pythonpath(config)

    global _CHECKPOINT_OWNED
    expected = os.environ.get("MAC_TEST_CHECKPOINT_ROOT", "").strip()
    if not expected or _CHECKPOINT_SPAWNED_BY_A_TEST:
        _CHECKPOINT_OWNED = False
        return
    try:
        _CHECKPOINT_OWNED = Path(expected).resolve() == Path(str(config.rootpath)).resolve()
    except OSError:
        _CHECKPOINT_OWNED = False


def _checkpoint_skip_files() -> set[str]:
    path = os.environ.get("MAC_TEST_CHECKPOINT_SKIP_FILE", "").strip()
    if not path:
        return set()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        # Fail OPEN: an unreadable skip list means we skip nothing and the
        # complete selection runs. A corrupt checkpoint must never be able to
        # make a red suite look green.
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _checkpoint_results_path() -> Path | None:
    directory = os.environ.get("MAC_TEST_CHECKPOINT_RESULTS_DIR", "").strip()
    if not directory or not _CHECKPOINT_OWNED:
        return None
    worker = os.environ.get("PYTEST_XDIST_WORKER", "") or ("pid%d" % os.getpid())
    return Path(directory) / ("%s.jsonl" % worker)


def pytest_collection_modifyitems(config, items: list) -> None:
    """Deselect whole test files whose results are carried forward."""

    if not _CHECKPOINT_OWNED:
        return
    skip_files = _checkpoint_skip_files()
    if not skip_files:
        return
    root = Path(str(config.rootpath))
    kept: list = []
    dropped: list = []
    for item in items:
        try:
            relative = str(Path(str(item.fspath)).resolve().relative_to(root.resolve()))
        except ValueError:
            relative = ""
        (dropped if relative in skip_files else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def _portfolio_output_path() -> str:
    return os.environ.get("MAC_TEST_PORTFOLIO_OUTPUT", "").strip()


def pytest_runtest_setup(item) -> None:
    """Attribute parent and subsequently spawned Python work to one node id."""

    if not _portfolio_output_path():
        return
    try:
        from coverage import Coverage

        current = Coverage.current()
        if current is not None:
            context = "test|" + item.nodeid
            current.switch_context(context)
            # coverage.py's subprocess patch serializes its startup config in
            # this environment variable. Refresh its static context for each
            # test so child-process arcs remain attributable to the test that
            # launched them instead of an anonymous process bucket.
            child_config = current.config.copy()
            child_config.context = context
            os.environ["COVERAGE_PROCESS_CONFIG"] = child_config.serialize()
    except Exception:
        # Attribution must never change test behavior. The report exposes any
        # missing contexts instead of hiding a test result.
        return


def pytest_runtest_logreport(report) -> None:
    """Accumulate phase duration and the strongest outcome for each node id."""

    _checkpoint_record(report)
    if not _portfolio_output_path():
        return
    record = _PORTFOLIO_RESULTS.setdefault(
        report.nodeid,
        {"nodeid": report.nodeid, "duration_seconds": 0.0, "outcome": "passed"},
    )
    record["duration_seconds"] = float(record["duration_seconds"]) + float(report.duration)
    if report.failed:
        record["outcome"] = "failed"
    elif report.skipped and record["outcome"] != "failed":
        record["outcome"] = "skipped"


def _checkpoint_record(report) -> None:
    """Append this phase report's outcome for the checkpoint recorder.

    Every phase is written, not just ``call``: a setup or teardown error is a
    failure, and the reader takes the strongest (worst) outcome per node id. A
    test whose outcome is never written is simply not carried forward, which is
    the safe direction.
    """

    path = _checkpoint_results_path()
    if path is None:
        return
    if report.passed and report.when != "call":
        # Setup/teardown success on its own says nothing; the call phase does.
        return
    outcome = "failed" if report.failed else ("skipped" if report.skipped else "passed")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"nodeid": report.nodeid, "outcome": outcome}, sort_keys=True) + "\n"
            )
    except OSError:
        # Recording must never change a test result. A run that cannot write
        # its checkpoint simply produces no checkpoint.
        return


def pytest_sessionfinish(session, exitstatus: int) -> None:
    """Persist timing/outcome evidence only when explicitly requested."""

    output = _portfolio_output_path()
    if not output:
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "mac.test_portfolio_timings.v1",
                "exitstatus": int(exitstatus),
                "tests": sorted(_PORTFOLIO_RESULTS.values(), key=lambda item: str(item["nodeid"])),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
