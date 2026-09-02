"""Behavioral CLI tests for machine inventory and agent hardware projection.

Both subcommands are exercised:
- exit code 0
- parsed JSON output contains expected fields
- ``list`` returns a list; ``show`` returns the same record
- hardware summary is populated for a machine that carries hardware data
"""

from __future__ import annotations

import io
import json
import sys
from unittest import mock

from mac.test_support import dsn_for
from mac.cli import main


# ---------------------------------------------------------------------------
# shared helper
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run ``mac --db <tmp> --json <args>`` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# machine list
# ---------------------------------------------------------------------------


def test_machine_list_empty(tmp_path):
    """``mac machine list`` on a fresh DB returns an empty list."""
    rc, result = _run(tmp_path, "admin", "machine", "list")
    assert rc == 0
    assert result == []


def test_machine_list_shows_registered(tmp_path):
    """After registering a machine it appears in ``mac machine list``."""
    rc, machine = _run(tmp_path, "admin", "machine", "register", "host-1")
    assert rc == 0
    assert machine["hostname"] == "host-1"

    rc, machines = _run(tmp_path, "admin", "machine", "list")
    assert rc == 0
    assert isinstance(machines, list)
    assert len(machines) == 1
    listed = machines[0]
    for field in ("id", "hostname", "trusted", "last_seen_at"):
        assert field in listed, "expected field %r in machine list entry" % field
    assert listed["id"] == machine["id"]
    assert listed["hostname"] == "host-1"


def test_machine_list_multiple(tmp_path):
    """``mac machine list`` shows all registered machines."""
    for hostname in ("alpha", "beta", "gamma"):
        rc, _ = _run(tmp_path, "admin", "machine", "register", hostname)
        assert rc == 0

    rc, machines = _run(tmp_path, "admin", "machine", "list")
    assert rc == 0
    assert len(machines) == 3
    hostnames = {m["hostname"] for m in machines}
    assert hostnames == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# machine show
# ---------------------------------------------------------------------------


def test_machine_show_returns_full_record(tmp_path):
    """``mac machine show <id>`` returns the full machine record."""
    rc, machine = _run(tmp_path, "admin", "machine", "register", "host-show")
    assert rc == 0
    machine_id = machine["id"]

    rc, shown = _run(tmp_path, "admin", "machine", "show", machine_id)
    assert rc == 0
    assert shown is not None
    for field in ("id", "hostname", "trusted", "last_seen_at", "labels", "resources", "hardware"):
        assert field in shown, "expected field %r in machine show output" % field
    assert shown["id"] == machine_id
    assert shown["hostname"] == "host-show"


def test_machine_show_list_consistency(tmp_path):
    """``machine list`` and ``machine show`` agree on core fields."""
    rc, machine = _run(tmp_path, "admin", "machine", "register", "consistent-host")
    assert rc == 0

    rc, machines = _run(tmp_path, "admin", "machine", "list")
    assert rc == 0
    assert len(machines) == 1
    listed = machines[0]

    rc, show_result = _run(tmp_path, "admin", "machine", "show", listed["id"])
    assert rc == 0

    for field in ("id", "hostname", "trusted"):
        assert listed[field] == show_result[field], "field %r mismatch: list=%r show=%r" % (
            field,
            listed[field],
            show_result[field],
        )


def test_machine_list_text_output(tmp_path):
    """``mac machine list`` in text mode prints one line per machine."""
    rc, machine = _run(tmp_path, "admin", "machine", "register", "text-host")
    assert rc == 0

    # Use a fresh DB path so previous JSON-mode _run calls don't taint state.
    # Call main directly without --json so text mode is exercised.
    import mac.cli as _cli_mod

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        with mock.patch.object(_cli_mod, "_stdout_is_interactive", return_value=True):
            rc_text = main(["--db", dsn_for(tmp_path), "admin", "machine", "list"])
    finally:
        sys.stdout = old
    assert rc_text == 0
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert machine["id"] in lines[0]
    assert "text-host" in lines[0]


def test_machine_list_hardware_summary(tmp_path):
    """A machine registered with hardware data shows a non-empty hw summary."""
    import json as _json
    import mac.cli as _cli_mod

    hw_resources = _json.dumps(
        {
            "hardware": {
                "os": "linux",
                "arch": "x86_64",
                "cpu_count": 8,
                "memory_mb": 16384,
            }
        }
    )
    rc, machine = _run(
        tmp_path, "admin", "machine", "register", "hw-host", "--resources", hw_resources
    )
    assert rc == 0

    # Text mode: hw summary should appear on the line
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        with mock.patch.object(_cli_mod, "_stdout_is_interactive", return_value=True):
            main(["--db", dsn_for(tmp_path), "admin", "machine", "list"])
    finally:
        sys.stdout = old
    line = out.getvalue().strip()
    # The hw summary contains os/arch; "linux/x86_64" should be on the line.
    assert "linux/x86_64" in line or "8c" in line, (
        "expected hardware summary tokens in line: %r" % line
    )


def test_agent_hardware_lists_registered_machine_projection(tmp_path):
    """The one agent-hardware CLI contract not covered by another domain suite."""

    rc, machine = _run(tmp_path, "admin", "machine", "register", "agent-hw-host")
    assert rc == 0
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], "agent-hw")
    assert rc == 0

    rc, hardware = _run(tmp_path, "agent", "hardware")

    assert rc == 0
    assert isinstance(hardware, list)
    projected = next(row for row in hardware if row.get("agent") == agent["name"])
    assert "hardware" in projected
    assert "runnable_models" in projected
