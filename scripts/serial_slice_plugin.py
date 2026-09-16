"""Loaded only for the contract runner's serial marker slice.

Coverage's ``patch = ["subprocess"]`` traces docker/sudo/launchctl children.
After a wide xdist bulk phase that overhead blows sub-second process-group
deadlines, especially on Darwin. The parent pytest process remains measured
by ``coverage run``; this plugin drops the child-tracing env so the serial
slice exercises the product, not the tracer.
"""

from __future__ import annotations

import os


def pytest_configure(config) -> None:  # noqa: ARG001
    os.environ.pop("COVERAGE_PROCESS_START", None)
    os.environ.pop("COVERAGE_PROCESS_CONFIG", None)
