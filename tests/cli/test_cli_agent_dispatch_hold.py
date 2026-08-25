"""CLI tests for `mac agent hold` and `mac agent resume` subcommands.

Covers:
- mac agent hold <agent_id> --reason <text>  → persists hold and prints agent
- mac agent resume <agent_id>                → clears hold and prints agent
- hold/resume round-trip: hold sets fields, resume clears them
- Missing --reason on hold returns non-zero exit code
"""

from __future__ import annotations

import io
import json
import sys

from mac.test_support import dsn_for
from mac.cli import main


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output).

    parsed_output is the JSON-decoded stdout when the command emits JSON, or
    the raw text string otherwise.  Returns None when stdout is empty.
    """
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    if not raw:
        return rc, None
    try:
        return rc, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return rc, raw


def _register_agent(tmp_path, name="worker-1", machine_name=None):
    """Register a machine + agent and return the agent record."""
    host = machine_name or (name + "-host")
    rc, machine = _run(tmp_path, "admin", "machine", "register", host)
    assert rc == 0, f"machine register failed: {machine}"
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], name)
    assert rc == 0, f"agent register failed: {agent}"
    return agent


# ---------------------------------------------------------------------------
# mac agent hold
# ---------------------------------------------------------------------------


def test_agent_hold_sets_dispatch_hold_flag(tmp_path):
    """mac agent hold <id> --reason <text> sets dispatch_hold=True on the agent."""
    agent = _register_agent(tmp_path, "hold-agent-1")

    rc, held = _run(tmp_path, "agent", "hold", agent["id"], "--reason", "manual quarantine")

    assert rc == 0
    assert held["dispatch_hold"] is True
    assert held["dispatch_hold_reason"] == "manual quarantine"
    assert held["dispatch_hold_at"] is not None


def test_agent_hold_returns_updated_agent_record(tmp_path):
    """mac agent hold returns the full agent record with id and name."""
    agent = _register_agent(tmp_path, "hold-agent-2")

    rc, held = _run(tmp_path, "agent", "hold", agent["id"], "--reason", "test hold")

    assert rc == 0
    assert held["id"] == agent["id"]
    assert held["name"] == "hold-agent-2"


def test_agent_list_health_surfaces_hold_and_unconsumed_control_age(tmp_path):
    sender = _register_agent(tmp_path, "health-sender")
    recipient = _register_agent(tmp_path, "health-recipient")
    rc, _ = _run(
        tmp_path,
        "admin",
        "agentbus",
        "repo-update",
        sender["id"],
        "--recipient-agent-id",
        recipient["id"],
    )
    assert rc == 0
    rc, _ = _run(tmp_path, "agent", "hold", recipient["id"], "--reason", "health check")
    assert rc == 0

    rc, agents = _run(tmp_path, "agent", "list", "--health")

    assert rc == 0
    by_id = {agent["id"]: agent for agent in agents}
    row = by_id[recipient["id"]]
    assert row["dispatch_hold"] is True
    assert row["dispatch_hold_reason"] == "health check"
    assert row["unconsumed_control_stream_age_seconds"] is not None


# ---------------------------------------------------------------------------
# mac agent resume
# ---------------------------------------------------------------------------


def test_agent_resume_clears_dispatch_hold(tmp_path):
    """mac agent resume <id> clears the dispatch hold fields."""
    agent = _register_agent(tmp_path, "resume-agent-1")

    rc, _ = _run(tmp_path, "agent", "hold", agent["id"], "--reason", "hold to clear")
    assert rc == 0

    rc, resumed = _run(tmp_path, "agent", "resume", agent["id"])

    assert rc == 0
    assert resumed["dispatch_hold"] is False
    assert resumed.get("dispatch_hold_reason") is None
    assert resumed.get("dispatch_hold_at") is None


def test_agent_resume_returns_updated_agent_record(tmp_path):
    """mac agent resume returns the full agent record."""
    agent = _register_agent(tmp_path, "resume-agent-2")
    _run(tmp_path, "agent", "hold", agent["id"], "--reason", "temp hold")

    rc, resumed = _run(tmp_path, "agent", "resume", agent["id"])

    assert rc == 0
    assert resumed["id"] == agent["id"]
    assert resumed["name"] == "resume-agent-2"


def test_agent_resume_idempotent_when_not_held(tmp_path):
    """mac agent resume on an unheld agent succeeds and returns dispatch_hold=False."""
    agent = _register_agent(tmp_path, "resume-agent-3")

    rc, resumed = _run(tmp_path, "agent", "resume", agent["id"])

    assert rc == 0
    assert resumed["dispatch_hold"] is False


# ---------------------------------------------------------------------------
# round-trip: hold then resume
# ---------------------------------------------------------------------------


def test_hold_then_resume_round_trip(tmp_path):
    """Full hold → resume round-trip: fields are set then cleared correctly."""
    agent = _register_agent(tmp_path, "roundtrip-cli-agent")

    rc, held = _run(tmp_path, "agent", "hold", agent["id"], "--reason", "roundtrip test")
    assert rc == 0
    assert held["dispatch_hold"] is True
    assert held["dispatch_hold_reason"] == "roundtrip test"

    rc, resumed = _run(tmp_path, "agent", "resume", agent["id"])
    assert rc == 0
    assert resumed["dispatch_hold"] is False
    assert resumed.get("dispatch_hold_reason") is None
    assert resumed.get("dispatch_hold_at") is None
