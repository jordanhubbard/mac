"""Repository-wide pytest hooks for opt-in test-portfolio measurement."""

from __future__ import annotations

import json
import os
from pathlib import Path


_PORTFOLIO_RESULTS: dict[str, dict[str, object]] = {}


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
