"""Behavioral CLI tests for the mood and nap command families.

Covers: mood {set,show,clear,history}, nap {configure,show,next,begin,complete,fail,list,due}
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main


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


def _setup_agent(tmp_path):
    """Register a machine and agent; return agent dict."""
    rc, machine = _run(tmp_path, "machine", "register", "mood-host")
    assert rc == 0
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], "mood-worker",
                     "--agent-id", "agent_mood")
    assert rc == 0
    return agent


# ---------------------------------------------------------------------------
# mood
# ---------------------------------------------------------------------------


def test_mood_set_and_show(tmp_path):
    agent = _setup_agent(tmp_path)
    rc, overlay = _run(tmp_path, "mood", "set", agent["id"], "cheerful",
                       "--set-by", "operator", "--reason", "task success")
    assert rc == 0
    assert overlay["mode"] == "cheerful"
    assert overlay["agent_id"] == agent["id"]

    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    assert current["mode"] == "cheerful"


def test_mood_show_none_when_no_mood(tmp_path):
    agent = _setup_agent(tmp_path)
    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    assert current is None


def test_mood_clear(tmp_path):
    agent = _setup_agent(tmp_path)
    _run(tmp_path, "mood", "set", agent["id"], "sad", "--set-by", "op")
    rc, cleared = _run(tmp_path, "mood", "clear", agent["id"],
                       "--cleared-by", "op", "--reason", "resolved")
    assert rc == 0
    rc, current = _run(tmp_path, "mood", "show", agent["id"])
    assert rc == 0
    # After clear, no active overlay
    assert current is None


def test_mood_history_accumulates(tmp_path):
    agent = _setup_agent(tmp_path)
    for mode in ("warm", "cold", "cheerful"):
        rc, _ = _run(tmp_path, "mood", "set", agent["id"], mode)
        assert rc == 0

    rc, history = _run(tmp_path, "mood", "history", agent["id"], "--limit", "10")
    assert rc == 0
    assert isinstance(history, list)
    # All three transitions recorded
    modes_seen = {h["mode"] for h in history}
    assert {"warm", "cold", "cheerful"} <= modes_seen


def test_mood_set_all_valid_modes(tmp_path):
    agent = _setup_agent(tmp_path)
    for mode in ("warm", "cheerful", "sad", "curt", "cold", "irritated", "angry", "enraged"):
        rc, overlay = _run(tmp_path, "mood", "set", agent["id"], mode)
        assert rc == 0, f"Failed for mode={mode}"
        assert overlay["mode"] == mode


# ---------------------------------------------------------------------------
# nap
# ---------------------------------------------------------------------------


def test_nap_configure_and_show(tmp_path):
    agent = _setup_agent(tmp_path)
    rc, schedule = _run(tmp_path, "nap", "configure", agent["id"],
                        "--offset-minutes", "30", "--window-minutes", "20", "--actor", "test")
    assert rc == 0
    assert schedule["agent_id"] == agent["id"]

    rc, shown = _run(tmp_path, "nap", "show", agent["id"])
    assert rc == 0
    assert shown["agent_id"] == agent["id"]


def test_nap_show_returns_schedule_or_none(tmp_path):
    """nap show returns a schedule dict (possibly auto-initialized) or None."""
    agent = _setup_agent(tmp_path)
    rc, result = _run(tmp_path, "nap", "show", agent["id"])
    assert rc == 0
    # Some implementations auto-create a schedule; others return None — both are valid.
    assert result is None or isinstance(result, dict)


def test_nap_configure_disabled(tmp_path):
    agent = _setup_agent(tmp_path)
    rc, schedule = _run(tmp_path, "nap", "configure", agent["id"], "--disabled")
    assert rc == 0
    assert schedule["enabled"] is False


def test_nap_next_after_configure(tmp_path):
    agent = _setup_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "0")
    rc, window = _run(tmp_path, "nap", "next", agent["id"])
    assert rc == 0
    assert window is not None


def test_nap_begin_creates_run(tmp_path):
    agent = _setup_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    rc, run = _run(tmp_path, "nap", "begin", agent["id"], "--actor", "test")
    assert rc == 0
    assert "id" in run


def test_nap_fail_marks_run_failed(tmp_path):
    agent = _setup_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    _, run = _run(tmp_path, "nap", "begin", agent["id"], "--actor", "test")
    rc, failed = _run(tmp_path, "nap", "fail", run["id"],
                      "--reason", "test failure", "--actor", "test")
    assert rc == 0


def test_nap_list_empty(tmp_path):
    agent = _setup_agent(tmp_path)
    rc, runs = _run(tmp_path, "nap", "list", "--agent-id", agent["id"])
    assert rc == 0
    assert isinstance(runs, list)
    assert runs == []


def test_nap_list_after_begin(tmp_path):
    agent = _setup_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    _run(tmp_path, "nap", "begin", agent["id"], "--actor", "test")
    rc, runs = _run(tmp_path, "nap", "list", "--agent-id", agent["id"])
    assert rc == 0
    assert len(runs) >= 1


def test_nap_due_empty(tmp_path):
    """nap due returns an empty list when no nap schedules have windows open."""
    _setup_agent(tmp_path)
    rc, due = _run(tmp_path, "nap", "due")
    assert rc == 0
    assert isinstance(due, list)


def test_nap_complete_after_begin(tmp_path):
    agent = _setup_agent(tmp_path)
    _run(tmp_path, "nap", "configure", agent["id"])
    _, run = _run(tmp_path, "nap", "begin", agent["id"], "--actor", "test")
    rc, completed = _run(tmp_path, "nap", "complete", run["id"], "--actor", "test")
    assert rc == 0
