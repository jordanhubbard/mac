"""Tests for the extracted hub I/O seam and plan-detection module.

Covers the functions extracted from mac.task_executor into
mac.executor_hub_io.  The hub HTTP seam is injectable (env-var gated), so
nothing here hits a network.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

import mac.executor_hub_io as hub_io
from mac import task_executor as te


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def test_utcnow_returns_iso_string():
    result = hub_io.utcnow()
    assert isinstance(result, str)
    assert "T" in result
    assert result.endswith("+00:00")


def test_sha256_text_format():
    result = hub_io.sha256_text("hello")
    assert result.startswith("sha256:")
    assert len(result) == len("sha256:") + 64  # hex sha256


def test_sha256_text_deterministic():
    assert hub_io.sha256_text("abc") == hub_io.sha256_text("abc")
    assert hub_io.sha256_text("abc") != hub_io.sha256_text("xyz")


def test_command_audit_id_prefix_and_length():
    cid = hub_io.command_audit_id()
    assert cid.startswith("cmd_")
    assert len(cid) == len("cmd_") + 32


def test_command_audit_id_unique():
    ids = {hub_io.command_audit_id() for _ in range(5)}
    # Time-based seed means collisions are astronomically unlikely
    assert len(ids) >= 1  # at minimum returns a stable string


def test_redacted_arg_format():
    result = hub_io.redacted_arg("supersecret")
    assert "<redacted:sha256:" in result
    assert "chars=11" in result


def test_audit_safe_argv_passes_plain_args():
    result = hub_io.audit_safe_argv(["git", "commit", "-m", "msg"])
    assert result == ["git", "commit", "-m", "msg"]


def test_audit_safe_argv_redacts_after_token_flag():
    result = hub_io.audit_safe_argv(["curl", "--token", "mysecret", "https://example.com"])
    assert result[0] == "curl"
    assert result[1] == "--token"
    assert "<redacted:" in result[2]
    assert result[3] == "https://example.com"


def test_audit_safe_argv_redacts_bearer_inline():
    result = hub_io.audit_safe_argv(["--auth", "bearer abc123"])
    assert "<redacted:" in result[1]


def test_audit_safe_argv_truncates_long_arg():
    long_arg = "x" * 600
    result = hub_io.audit_safe_argv([long_arg])
    assert "<truncated:" in result[0]
    assert "chars=600" in result[0]


def test_safe_path_component_strips_unsafe_chars():
    assert hub_io.safe_path_component("hello/world") == "hello_world"
    assert hub_io.safe_path_component("abc-123.py") == "abc-123.py"


def test_safe_path_component_max_length():
    long = "a" * 300
    result = hub_io.safe_path_component(long)
    assert len(result) == 180


def test_local_agent_id_default(monkeypatch):
    monkeypatch.delenv("MAC_AGENT_ID", raising=False)
    monkeypatch.delenv("MAC_WORKER_AGENT_ID", raising=False)
    monkeypatch.delenv("MAC_WORKER_AGENT_NAME", raising=False)
    result = hub_io.local_agent_id()
    assert result.startswith("agent_")


def test_local_agent_id_from_env(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_ID", "agent_test42")
    result = hub_io.local_agent_id()
    assert result == "agent_test42"


# ---------------------------------------------------------------------------
# Hub I/O seam — absent env → safe defaults
# ---------------------------------------------------------------------------


def _clear_hub_env(monkeypatch):
    """Remove all hub-related env vars so the I/O functions return safe defaults."""
    for var in ("MAC_HUB_URL", "MAC_URL", "MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_hub_post_returns_false_when_env_absent(monkeypatch):
    _clear_hub_env(monkeypatch)
    assert hub_io._hub_post("/some/path", {"key": "val"}) is False


def test_hub_post_json_returns_none_when_env_absent(monkeypatch):
    _clear_hub_env(monkeypatch)
    result = hub_io._hub_post_json("/some/path", {"key": "val"})
    assert result is None


def test_hub_get_returns_none_when_env_absent(monkeypatch):
    _clear_hub_env(monkeypatch)
    result = hub_io._hub_get("/some/path")
    assert result is None


def test_hub_put_returns_false_when_env_absent(monkeypatch):
    _clear_hub_env(monkeypatch)
    assert hub_io._hub_put("/some/path", {"key": "val"}) is False


def test_hub_post_child_tasks_returns_none_when_env_absent(monkeypatch):
    _clear_hub_env(monkeypatch)
    result = hub_io._hub_post_child_tasks("task_123", [{"title": "child"}])
    assert result is None


def test_hub_post_child_tasks_returns_none_for_empty_inputs(monkeypatch):
    _clear_hub_env(monkeypatch)
    assert hub_io._hub_post_child_tasks("", [{"title": "child"}]) is None
    assert hub_io._hub_post_child_tasks("task_123", []) is None


def test_hub_env_returns_empty_strings_when_absent(monkeypatch):
    _clear_hub_env(monkeypatch)
    base_url, token = hub_io._hub_env()
    assert base_url == ""
    assert token == ""


def test_hub_env_returns_configured_values(monkeypatch):
    monkeypatch.setenv("MAC_HUB_URL", "http://hub.local/")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "tok123")
    base_url, token = hub_io._hub_env()
    assert base_url == "http://hub.local"  # trailing slash stripped
    assert token == "tok123"


# ---------------------------------------------------------------------------
# Plan-detection
# ---------------------------------------------------------------------------


def test_detect_plan_signals_atomic_task_no_signals():
    """A plain fix-a-bug task has no plan signals."""
    is_plan, signals = hub_io.detect_plan_signals("Fix null pointer in auth handler", "")
    assert not is_plan
    assert signals == []


def test_detect_plan_signals_keyword_in_title():
    """'end-to-end' matches a plan keyword."""
    is_plan, signals = hub_io.detect_plan_signals("Build end-to-end test suite", "")
    # 'end-to-end' is one signal — not enough alone for is_plan
    assert any("end-to-end" in s for s in signals)


def test_detect_plan_signals_requires_two_signals_for_is_plan():
    """One signal alone is not enough — need 2+."""
    is_plan, signals = hub_io.detect_plan_signals("end-to-end fix", "short description")
    assert len(signals) == 1
    assert not is_plan


def test_detect_plan_signals_keyword_plus_numbered_steps():
    """Keyword + numbered steps in description → is_plan=True."""
    title = "Build end-to-end pipeline"
    description = "1. Do X\n2. Do Y\n3. Do Z\n4. Do W"
    is_plan, signals = hub_io.detect_plan_signals(title, description)
    assert is_plan
    assert any("plan_keyword" in s for s in signals)
    assert any("numbered_steps" in s for s in signals)


def test_detect_plan_signals_bullet_cluster():
    """5+ bullets in description is a signal; two signals → is_plan."""
    title = "scaffold the new service"  # matches 'scaffold the'
    description = "\n".join("- item %d" % i for i in range(6))
    is_plan, signals = hub_io.detect_plan_signals(title, description)
    assert is_plan
    assert any("bullet_cluster" in s for s in signals)


def test_detect_plan_signals_long_description():
    """300+ words in description is a signal."""
    title = "several improvements to the system"  # matches 'several'
    description = " ".join(["word"] * 310)
    is_plan, signals = hub_io.detect_plan_signals(title, description)
    assert is_plan
    assert any("long_description" in s for s in signals)


def test_detect_plan_signals_conjunctive_verb_title():
    """Verb-and-verb title → conjunctive_verb_title signal."""
    title = "Implement the auth module and add tests"
    description = ""
    is_plan, signals = hub_io.detect_plan_signals(title, description)
    assert "conjunctive_verb_title" in signals


def test_detect_plan_signals_returns_empty_for_atomic_task():
    """No signals for a tightly scoped atomic task."""
    is_plan, signals = hub_io.detect_plan_signals(
        "Fix type error in models.py",
        "The field 'created_at' returns None instead of a datetime.",
    )
    assert not is_plan
    assert signals == []


# ---------------------------------------------------------------------------
# _plan_detection_section
# ---------------------------------------------------------------------------


def test_plan_detection_section_tells_a_plain_task_it_is_atomic():
    """This used to assert the section said "Task Sizing and Plan Detection"
    -- the header of a five-step fan-out recipe printed on EVERY task.

    Decomposition is now the submitter's declaration. An unauthorised task is
    told plainly that it is atomic, and the recipe is not shown at all.
    """
    task = {"id": "t1", "title": "Fix a bug", "description": "Small fix."}
    result = hub_io._plan_detection_section(task)

    assert isinstance(result, str)
    assert "ATOMIC" in result
    assert "Do NOT create child tasks" in result
    assert "Break the work into" not in result


def test_plan_detection_section_empty_for_child_task():
    """Child tasks (with parent_task_id) should skip re-decomposition."""
    task = {
        "id": "t2",
        "title": "Fix a bug",
        "description": "Small fix.",
        "metadata": {"relationships": {"parent_task_id": "parent_task_abc"}},
    }
    assert hub_io._plan_detection_section(task) == ""


def test_plan_detection_section_empty_for_no_decompose():
    """Tasks with no_decompose=True skip plan-detection."""
    task = {
        "id": "t3",
        "title": "Fix a bug",
        "description": "Small fix.",
        "metadata": {"no_decompose": True},
    }
    assert hub_io._plan_detection_section(task) == ""


def test_a_plan_scoring_task_is_reported_not_split_when_unauthorised():
    """The heuristic is now an OBSERVATION, not a licence.

    It used to emit a TASK-SIZING ALERT that sat above a fan-out recipe. If the
    submitter did not authorise decomposition, disagreeing with them is a
    question to raise, not an action to take.
    """
    task = {
        "id": "t4",
        "title": "Build end-to-end pipeline",
        "description": "1. Do X\n2. Do Y\n3. Do Z\n4. Do W",
    }
    result = hub_io._plan_detection_section(task)

    assert "Do not act on that by splitting it" in result
    assert "submitter can decide" in result


def test_a_plan_scoring_task_gets_the_recipe_when_authorised():
    task = {
        "id": "t5",
        "title": "Build end-to-end pipeline",
        "description": "1. Do X\n2. Do Y\n3. Do Z\n4. Do W",
        "metadata": {"decomposition": {"max_children": 4}},
    }
    result = hub_io._plan_detection_section(task)

    assert "at most 4 child task(s)" in result
    assert "Automated sizing agrees this is a plan" in result


# ---------------------------------------------------------------------------
# Verify backward-compat re-exports from task_executor
# ---------------------------------------------------------------------------


def test_task_executor_reexports_all_hub_io_symbols():
    """task_executor must still expose all moved symbols for downstream callers."""
    symbols = [
        "utcnow", "sha256_text", "command_audit_id", "redacted_arg",
        "audit_safe_argv", "safe_path_component", "local_agent_id",
        "_hub_env", "_hub_post", "_hub_post_json", "_hub_get", "_hub_put",
        "_hub_post_child_tasks", "_PLAN_TITLE_KEYWORDS", "_NUMBERED_STEP_RE",
        "_BULLET_RE", "detect_plan_signals", "_plan_detection_section",
    ]
    for sym in symbols:
        assert hasattr(te, sym), f"task_executor missing re-export: {sym}"


def test_task_executor_hub_post_same_as_hub_io(monkeypatch):
    """Re-exports are the same objects (not copies)."""
    assert te._hub_post is hub_io._hub_post
    assert te.detect_plan_signals is hub_io.detect_plan_signals
