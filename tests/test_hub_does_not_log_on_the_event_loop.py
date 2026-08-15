"""The hub must not write an access log from its event loop.

uvicorn emits the access line synchronously, on the loop thread, once per
request. Every agent in the fleet polls the hub continuously, so that log had
reached 626MB and 5.4 million lines on the fleet hub -- and a thread dump taken
while the hub was unresponsive caught the loop here:

    flush (logging/__init__.py:1137)
    emit -> handle -> _log -> info
    send (uvicorn/protocols/http/httptools_impl.py:491)
    run_asgi -> asyncio loop

While the loop is in flush() it is not accepting connections, so health probes
fail and the supervisor restarts the process -- killing whatever publication
was in flight.

Measured on the fleet hub with it disabled: a simple read went from 3.46s to
0.43s, and the allocator path began completing instead of hitting the client's
30s deadline.

The hub keeps its own structured observability. The access log was a duplicate,
written on the worst possible thread.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_fleet_installer_disables_the_access_log():
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")

    launch = [
        line
        for line in script.splitlines()
        if "uvicorn" in line and "mac.api:create_app" in line and line.strip().startswith("exec")
    ]

    assert launch, "no uvicorn launch line found in the installer"
    for line in launch:
        assert "--no-access-log" in line, line.strip()[:120]


def test_the_systemd_unit_disables_it_too():
    unit = (ROOT / "deploy" / "systemd" / "mac.service").read_text(encoding="utf-8")

    assert "--no-access-log" in unit
