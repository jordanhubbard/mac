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
written on the worst possible thread. ``mac.hub_serve`` is the process entry
point (needed for dual bind); it must keep access_log=False.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_fleet_installer_starts_hub_serve():
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")

    launch = [
        line
        for line in script.splitlines()
        if "mac.hub_serve" in line and line.strip().startswith("exec")
    ]

    assert launch, "no mac.hub_serve launch line found in the installer"


def test_hub_serve_disables_the_access_log():
    source = (ROOT / "src" / "mac" / "hub_serve.py").read_text(encoding="utf-8")
    assert "access_log=False" in source


def test_the_systemd_unit_starts_hub_serve():
    unit = (ROOT / "deploy" / "systemd" / "mac.service").read_text(encoding="utf-8")
    assert "mac.hub_serve" in unit


def test_the_hub_launcher_raises_its_descriptor_limit():
    """macOS gives a LaunchDaemon 256 descriptors. The hub holds one per polling
    agent, one per pooled Postgres connection, and one per sandbox subprocess
    pipe, so it runs out and then cannot open anything at all.

    Observed on the fleet hub: EMFILE in a crash loop out of the HGX autoscaler
    ("[Errno 24] Too many open files"), /health degrading from 16ms to 1.8s,
    and dispatch stopping entirely -- three idle agents unable to claim three
    ready tasks.

    mac-agent-service has always raised this limit. The hub, which needs it far
    more, never did.
    """
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")

    launcher = script[script.index("HERMES_REDACT_SECRETS") - 2000 :]
    launcher = launcher[: launcher.index("mac.hub_serve") + 40]

    assert "ulimit -n" in launcher
    assert "MAC_SERVICE_NOFILE_LIMIT" in launcher
