"""Behavioral tests for the `mac nap` CLI subcommands.

Each test exercises a nap subcommand end-to-end against a real in-file
SQLite database (tmp_path), confirming the command emits valid JSON and
round-trips through the same ControlPlane layer the HTTP API uses.

Pattern mirrors tests/cli/test_mac_cli.py: _run(tmp_path, ...) captures
stdout, parses JSON, and returns (exit_code, parsed_output).

Subcommands covered:
  - nap configure <agent_id>
  - nap show <agent_id>
  - nap next <agent_id>
  - nap begin <agent_id>
  - nap complete <run_id>
  - nap fail <run_id> --reason=...
  - nap list  /  nap list --agent-id=<id>
  - nap due
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.test_support import dsn_for, ephemeral_dsn

from mac.cli import main


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _register_agent(tmp_path, name="worker-1", machine_name=None):
    """Register a machine + agent and return the agent dict."""
    machine_name = machine_name or ("%s-host" % name)
    rc, machine = _run(tmp_path, "machine", "register", machine_name)
    assert rc == 0, machine
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], name)
    assert rc == 0, agent
    return agent


# ---------------------------------------------------------------------------
# nap configure
# ---------------------------------------------------------------------------


def _assert_configure_defaults(schedule):
    assert schedule["enabled"] is True
    assert 0 <= schedule["offset_minutes"] < 60
    assert schedule["window_minutes"] > 0


def _assert_configure_offset_45(schedule):
    assert schedule["offset_minutes"] == 45


def _assert_configure_window_20(schedule):
    assert schedule["window_minutes"] == 20


def _assert_configure_disabled(schedule):
    assert schedule["enabled"] is False


@pytest.mark.parametrize(
    "extra_args, check",
    [
        pytest.param([], _assert_configure_defaults, id="defaults"),
        pytest.param(
            ["--offset-minutes", "45"],
            _assert_configure_offset_45,
            id="explicit_offset",
        ),
        pytest.param(
            ["--offset-minutes", "30", "--window-minutes", "20"],
            _assert_configure_window_20,
            id="window_minutes",
        ),
        pytest.param(
            ["--disabled"], _assert_configure_disabled, id="disabled_flag"
        ),
    ],
)
def test_nap_configure_variants(tmp_path, extra_args, check):
    agent = _register_agent(tmp_path)
    rc, schedule = _run(tmp_path, "nap", "configure", agent["id"], *extra_args)
    assert rc == 0
    assert schedule["agent_id"] == agent["id"]
    check(schedule)


def test_nap_configure_is_idempotent(tmp_path):
    agent = _register_agent(tmp_path)
    rc, s1 = _run(
        tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "30"
    )
    assert rc == 0
    rc, s2 = _run(
        tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "30"
    )
    assert rc == 0
    assert s1["offset_minutes"] == s2["offset_minutes"] == 30


# ---------------------------------------------------------------------------
# nap show
# ---------------------------------------------------------------------------


def test_nap_show_returns_schedule_after_configure(tmp_path):
    agent = _register_agent(tmp_path)
    rc, configured = _run(
        tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "45"
    )
    assert rc == 0

    rc, shown = _run(tmp_path, "nap", "show", agent["id"])
    assert rc == 0
    assert shown["agent_id"] == agent["id"]
    assert shown["offset_minutes"] == 45


def test_nap_show_null_for_unconfigured_agent(tmp_path):
    """show returns null JSON if no schedule exists for the agent."""
    agent = _register_agent(tmp_path)
    # Delete the auto-created schedule so the agent has none.
    import sqlite3

    db_path = ephemeral_dsn()
    # Ensure DB is initialised by running any command first.
    _run(tmp_path, "nap", "configure", agent["id"])
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM nap_schedules WHERE agent_id = ?", (agent["id"],)
        )

    rc, result = _run(tmp_path, "nap", "show", agent["id"])
    assert rc == 0
    assert result is None


# ---------------------------------------------------------------------------
# nap next
# ---------------------------------------------------------------------------


def _assert_next_window_present(window):
    assert window is not None
    assert "start" in window
    assert "end" in window
    assert window["offset_minutes"] == 45


def _assert_next_window_absent(window):
    assert window is None


@pytest.mark.parametrize(
    "configure_args, check",
    [
        pytest.param(
            ["--offset-minutes", "45"],
            _assert_next_window_present,
            id="enabled_returns_window_fields",
        ),
        pytest.param(
            ["--disabled"],
            _assert_next_window_absent,
            id="disabled_returns_null",
        ),
    ],
)
def test_nap_next_variants(tmp_path, configure_args, check):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"], *configure_args)

    rc, window = _run(tmp_path, "nap", "next", agent["id"])
    assert rc == 0
    check(window)


# ---------------------------------------------------------------------------
# nap begin
# ---------------------------------------------------------------------------


def test_nap_begin_transitions_agent_to_draining(tmp_path):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])

    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0
    assert run["id"].startswith("nap_run_") or "_" in run["id"]
    assert run["agent_id"] == agent["id"]
    assert run["status"] == "running"

    # Agent should now be DRAINING
    rc, agents = _run(tmp_path, "agent", "list")
    assert rc == 0
    found = next(a for a in agents if a["id"] == agent["id"])
    assert found["status"] == "draining"


# ---------------------------------------------------------------------------
# nap complete
# ---------------------------------------------------------------------------


def test_nap_complete_marks_run_completed(tmp_path):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0

    rc, completed = _run(tmp_path, "nap", "complete", run["id"])
    assert rc == 0
    assert completed["status"] == "completed"
    assert completed["id"] == run["id"]


def test_nap_complete_restores_agent_to_idle(tmp_path):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0
    rc, _ = _run(tmp_path, "nap", "complete", run["id"])
    assert rc == 0

    rc, agents = _run(tmp_path, "agent", "list")
    assert rc == 0
    found = next(a for a in agents if a["id"] == agent["id"])
    assert found["status"] == "idle"


# ---------------------------------------------------------------------------
# nap fail
# ---------------------------------------------------------------------------


def test_nap_fail_marks_run_failed(tmp_path):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0

    rc, failed = _run(
        tmp_path, "nap", "fail", run["id"], "--reason", "qdrant unreachable"
    )
    assert rc == 0
    assert failed["status"] == "failed"
    assert failed["id"] == run["id"]


def test_nap_fail_restores_agent_to_idle(tmp_path):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0
    _run(tmp_path, "nap", "fail", run["id"], "--reason", "timeout")

    rc, agents = _run(tmp_path, "agent", "list")
    assert rc == 0
    found = next(a for a in agents if a["id"] == agent["id"])
    assert found["status"] == "idle"


# ---------------------------------------------------------------------------
# nap list
# ---------------------------------------------------------------------------


def test_nap_list_returns_empty_list_initially(tmp_path):
    _register_agent(tmp_path)
    rc, runs = _run(tmp_path, "nap", "list")
    assert rc == 0
    assert isinstance(runs, list)
    assert runs == []


def test_nap_list_shows_run_after_begin(tmp_path):
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0

    rc, runs = _run(tmp_path, "nap", "list")
    assert rc == 0
    assert isinstance(runs, list)
    assert any(r["id"] == run["id"] for r in runs)


def test_nap_list_agent_id_filter(tmp_path):
    a1 = _register_agent(tmp_path, name="worker-1", machine_name="host-1")
    a2 = _register_agent(tmp_path, name="worker-2", machine_name="host-2")
    _run(tmp_path, "nap", "configure", a1["id"])
    _run(tmp_path, "nap", "configure", a2["id"])
    rc, run1 = _run(tmp_path, "nap", "begin", a1["id"])
    assert rc == 0
    rc, run2 = _run(tmp_path, "nap", "begin", a2["id"])
    assert rc == 0

    rc, runs = _run(tmp_path, "nap", "list", "--agent-id", a1["id"])
    assert rc == 0
    ids = {r["id"] for r in runs}
    assert run1["id"] in ids
    assert run2["id"] not in ids


# ---------------------------------------------------------------------------
# store round-trip: begin → complete → list
# ---------------------------------------------------------------------------


def test_nap_begin_complete_run_appears_in_list(tmp_path):
    """Full begin→complete round-trip: run ends in 'completed' and visible in list."""
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])

    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0

    rc, completed = _run(tmp_path, "nap", "complete", run["id"])
    assert rc == 0
    assert completed["status"] == "completed"

    rc, runs = _run(tmp_path, "nap", "list", "--agent-id", agent["id"])
    assert rc == 0
    found = next((r for r in runs if r["id"] == run["id"]), None)
    assert found is not None
    assert found["status"] == "completed"


def test_nap_begin_fail_run_appears_in_list(tmp_path):
    """begin→fail round-trip: run ends in 'failed' and visible in list."""
    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])

    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0

    rc, failed = _run(tmp_path, "nap", "fail", run["id"], "--reason", "network error")
    assert rc == 0
    assert failed["status"] == "failed"

    rc, runs = _run(tmp_path, "nap", "list", "--agent-id", agent["id"])
    assert rc == 0
    found = next((r for r in runs if r["id"] == run["id"]), None)
    assert found is not None
    assert found["status"] == "failed"


# ---------------------------------------------------------------------------
# nap due
# ---------------------------------------------------------------------------


def _assert_due_contains_agent(due, agent):
    assert any(d["agent_id"] == agent["id"] for d in due)


def _assert_due_entry_fields(due, agent):
    entry = next((d for d in due if d["agent_id"] == agent["id"]), None)
    assert entry is not None
    assert "window_start" in entry
    assert "window_end" in entry
    assert "in_window" in entry


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(
            _assert_due_contains_agent, id="lists_agent_with_open_window"
        ),
        pytest.param(_assert_due_entry_fields, id="returns_expected_fields"),
    ],
)
def test_nap_due_variants(tmp_path, check):
    """An agent with offset_minutes=0 and no prior completion is due hourly.

    Both variants share identical setup and execution; each asserts a
    distinct facet of the due-list response (membership vs. entry fields).
    """
    from datetime import datetime, timezone

    agent = _register_agent(tmp_path)
    # Configure with offset 0 so each hourly window starts on the hour.
    _run(tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "0")

    # Use a reference timestamp after the top of the current hour.
    as_of = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)

    rc, due = _run(tmp_path, "nap", "due", "--as-of", as_of.isoformat())
    assert rc == 0
    assert isinstance(due, list)
    check(due, agent)


def test_nap_due_excludes_completed_agent(tmp_path):
    """An agent that already completed this cycle is not in the due list."""
    from datetime import datetime, timezone

    agent = _register_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "0")

    # Begin and complete the nap.
    rc, run = _run(tmp_path, "nap", "begin", agent["id"])
    assert rc == 0
    rc, _ = _run(tmp_path, "nap", "complete", run["id"])
    assert rc == 0

    # The agent should now be absent from due list.
    as_of = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)
    rc, due = _run(tmp_path, "nap", "due", "--as-of", as_of.isoformat())
    assert rc == 0
    assert not any(d["agent_id"] == agent["id"] for d in due)
