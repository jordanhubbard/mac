"""Behavioral tests for `mac mood` CLI subcommands.

Subcommands covered:
  - mood set   <agent_id> <mode> [--reason] [--set-by] [--ttl-seconds]
  - mood show  <agent_id>
  - mood clear <agent_id> [--cleared-by] [--reason]
  - mood history <agent_id> [--limit]

Each test invokes the CLI end-to-end against an in-file SQLite database
(tmp_path) and asserts on exit code, returned JSON shape, and store
round-trips.  The _run helper mirrors the pattern used in test_mac_cli.py
and test_cli_nap.py.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

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
        rc = main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _register_agent(tmp_path, name="worker-1", machine_name=None):
    """Register a machine + agent and return the agent record dict."""
    host = machine_name or (name + "-host")
    rc, machine = _run(tmp_path, "machine", "register", host)
    assert rc == 0, machine
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], name)
    assert rc == 0, agent
    return agent


# ---------------------------------------------------------------------------
# mood set
# ---------------------------------------------------------------------------


def test_mood_set_returns_overlay_record(tmp_path):
    """mood set creates a MoodOverlay and returns it as JSON."""
    agent = _register_agent(tmp_path)
    rc, overlay = _run(tmp_path, "mood", "set", agent["id"], "warm")
    assert rc == 0
    assert overlay["id"].startswith("mood_")
    assert overlay["agent_id"] == agent["id"]
    assert overlay["mode"] == "warm"
    assert overlay["cleared_at"] is None


def test_mood_set_with_reason_and_set_by(tmp_path):
    """mood set --reason and --set-by are reflected in the overlay."""
    agent = _register_agent(tmp_path)
    rc, overlay = _run(
        tmp_path,
        "mood", "set", agent["id"], "cheerful",
        "--reason", "three consecutive approvals",
        "--set-by", "hub",
    )
    assert rc == 0
    assert overlay["mode"] == "cheerful"
    assert overlay["reason"] == "three consecutive approvals"
    assert overlay["set_by"] == "hub"


def test_mood_set_replaces_prior_active_overlay(tmp_path):
    """Setting a second mood clears the first (only one active at a time)."""
    agent = _register_agent(tmp_path)

    rc, first = _run(tmp_path, "mood", "set", agent["id"], "warm")
    assert rc == 0

    rc, second = _run(tmp_path, "mood", "set", agent["id"], "irritated",
                      "--reason", "task timed out")
    assert rc == 0
    assert second["mode"] == "irritated"

    # The active mood should now be the second one.
    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    assert current["id"] == second["id"]


def test_mood_set_multiple_modes(tmp_path):
    """Each valid mood mode can be set without error."""
    agent = _register_agent(tmp_path)
    for mode in ("warm", "cheerful", "sad", "curt", "cold", "irritated",
                 "angry", "enraged"):
        rc, overlay = _run(tmp_path, "mood", "set", agent["id"], mode)
        assert rc == 0, f"mood set failed for mode {mode!r}: {overlay}"
        assert overlay["mode"] == mode


# ---------------------------------------------------------------------------
# mood show
# ---------------------------------------------------------------------------


def test_mood_show_returns_current_overlay(tmp_path):
    """mood show returns the overlay set by the most recent mood set."""
    agent = _register_agent(tmp_path)
    rc, overlay = _run(tmp_path, "mood", "set", agent["id"], "sad",
                       "--reason", "rollback required")
    assert rc == 0

    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    assert current["id"] == overlay["id"]
    assert current["mode"] == "sad"
    assert current["reason"] == "rollback required"


def test_mood_show_returns_null_when_no_mood_set(tmp_path):
    """mood show returns null (None) when the agent has no active mood."""
    agent = _register_agent(tmp_path)
    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    assert current is None


def test_mood_show_returns_null_after_clear(tmp_path):
    """mood show returns null after the overlay has been cleared."""
    agent = _register_agent(tmp_path)
    _run(tmp_path, "mood", "set", agent["id"], "warm")
    _run(tmp_path, "mood", "clear", agent["id"])

    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    assert current is None


# ---------------------------------------------------------------------------
# mood clear
# ---------------------------------------------------------------------------


def test_mood_clear_returns_cleared_overlay(tmp_path):
    """mood clear returns the overlay record with cleared_at set."""
    agent = _register_agent(tmp_path)
    rc, overlay = _run(tmp_path, "mood", "set", agent["id"], "curt")
    assert rc == 0

    rc, cleared = _run(tmp_path, "mood", "clear", agent["id"])
    assert rc == 0
    assert cleared["id"] == overlay["id"]
    assert cleared["cleared_at"] is not None


def test_mood_clear_with_reason(tmp_path):
    """mood clear --reason is stored in the cleared overlay."""
    agent = _register_agent(tmp_path)
    _run(tmp_path, "mood", "set", agent["id"], "angry")
    rc, cleared = _run(tmp_path, "mood", "clear", agent["id"],
                       "--reason", "cooled down after break")
    assert rc == 0
    assert cleared["cleared_reason"] == "cooled down after break"


def test_mood_clear_with_cleared_by(tmp_path):
    """mood clear --cleared-by is stored on the overlay."""
    agent = _register_agent(tmp_path)
    _run(tmp_path, "mood", "set", agent["id"], "warm")
    rc, cleared = _run(tmp_path, "mood", "clear", agent["id"],
                       "--cleared-by", "hub")
    assert rc == 0
    assert cleared["cleared_by"] == "hub"


def test_mood_clear_when_no_active_mood_returns_null(tmp_path):
    """mood clear is a no-op (returns null) when no mood is active."""
    agent = _register_agent(tmp_path)
    rc, result = _run(tmp_path, "mood", "clear", agent["id"])
    assert rc == 0
    assert result is None


# ---------------------------------------------------------------------------
# mood history
# ---------------------------------------------------------------------------


def test_mood_history_returns_empty_list_when_no_overlays(tmp_path):
    """mood history returns [] when the agent has never had a mood set."""
    agent = _register_agent(tmp_path)
    rc, history = _run(tmp_path, "mood", "history", agent["id"])
    assert rc == 0
    assert history == []


def test_mood_history_contains_set_mood(tmp_path):
    """mood history lists the overlay after mood set."""
    agent = _register_agent(tmp_path)
    rc, overlay = _run(tmp_path, "mood", "set", agent["id"], "cheerful",
                       "--reason", "fast merge")
    assert rc == 0

    rc, history = _run(tmp_path, "mood", "history", agent["id"])
    assert rc == 0
    assert len(history) == 1
    assert history[0]["id"] == overlay["id"]
    assert history[0]["mode"] == "cheerful"


def test_mood_history_accumulates_across_transitions(tmp_path):
    """mood history includes both set and cleared overlays in order."""
    agent = _register_agent(tmp_path)
    _run(tmp_path, "mood", "set", agent["id"], "warm")
    _run(tmp_path, "mood", "set", agent["id"], "sad")   # replaces first
    _run(tmp_path, "mood", "clear", agent["id"])         # clears second

    rc, history = _run(tmp_path, "mood", "history", agent["id"])
    assert rc == 0
    # warm + sad both recorded; order newest-first or oldest-first both valid
    modes = [h["mode"] for h in history]
    assert "warm" in modes
    assert "sad" in modes


def test_mood_history_limit_flag(tmp_path):
    """mood history --limit N restricts the result set."""
    agent = _register_agent(tmp_path)
    for mode in ("warm", "cheerful", "sad"):
        _run(tmp_path, "mood", "set", agent["id"], mode)

    rc, history = _run(tmp_path, "mood", "history", agent["id"], "--limit", "2")
    assert rc == 0
    assert len(history) <= 2


def test_mood_history_is_scoped_per_agent(tmp_path):
    """mood history only returns overlays for the requested agent."""
    a1 = _register_agent(tmp_path, name="worker-1", machine_name="host-1")
    a2 = _register_agent(tmp_path, name="worker-2", machine_name="host-2")

    _run(tmp_path, "mood", "set", a1["id"], "warm")
    _run(tmp_path, "mood", "set", a2["id"], "cold")

    rc, h1 = _run(tmp_path, "mood", "history", a1["id"])
    assert rc == 0
    agent_ids = {h["agent_id"] for h in h1}
    assert agent_ids == {a1["id"]}

    rc, h2 = _run(tmp_path, "mood", "history", a2["id"])
    assert rc == 0
    agent_ids2 = {h["agent_id"] for h in h2}
    assert agent_ids2 == {a2["id"]}
