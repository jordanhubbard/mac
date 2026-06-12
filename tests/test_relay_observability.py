"""Tests for the NeMo Relay observability seam (relay_observability.py).

All tests run without nemo-relay installed (the default).  The seam must
behave as a transparent no-op in that configuration so all existing tests
continue to pass unchanged.

Separate parametrised tests simulate relay-present behaviour by monkey-patching
the module's internal flags — no nemo-relay wheel is needed in the test env.
"""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

import mac.relay_observability as ro


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_module_with_env(**env_overrides):
    """Reload relay_observability under a custom environment."""
    old_env = {k: os.environ.get(k) for k in env_overrides}
    for k, v in env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        # Remove cached module so import guard re-runs
        sys.modules.pop("mac.relay_observability", None)
        fresh = importlib.import_module("mac.relay_observability")
        return fresh
    finally:
        for k, orig in old_env.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
        # Restore the original module reference
        sys.modules["mac.relay_observability"] = ro


# ---------------------------------------------------------------------------
# is_available() -- relay absent
# ---------------------------------------------------------------------------


def test_is_available_returns_false_when_nemo_relay_absent():
    """Default test env has no nemo-relay; seam must report unavailable."""
    assert ro._NEMO_RELAY_AVAILABLE is False
    assert ro.is_available() is False


def test_is_available_returns_false_when_env_var_not_set(monkeypatch):
    monkeypatch.delenv("MAC_RELAY_OBSERVABILITY", raising=False)
    assert ro.is_available() is False


def test_is_available_returns_false_when_env_var_is_zero(monkeypatch):
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "0")
    assert ro.is_available() is False


def test_is_available_returns_false_when_env_var_is_false(monkeypatch):
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "false")
    assert ro.is_available() is False


# ---------------------------------------------------------------------------
# flush() -- relay absent
# ---------------------------------------------------------------------------


def test_flush_is_noop_when_relay_absent():
    """flush() must not raise and must not call anything when relay absent."""
    ro.flush()  # Should not raise


# ---------------------------------------------------------------------------
# create_agent_scope() -- relay absent (the transparent no-op path)
# ---------------------------------------------------------------------------


def test_create_agent_scope_yields_without_error_when_relay_absent():
    ran = []
    with ro.create_agent_scope("test-session-id"):
        ran.append(True)
    assert ran == [True]


def test_create_agent_scope_yields_even_when_body_raises_relay_absent():
    """Scope must not suppress exceptions from the body."""
    with pytest.raises(RuntimeError, match="body error"):
        with ro.create_agent_scope("test-session-id"):
            raise RuntimeError("body error")


# ---------------------------------------------------------------------------
# Simulated relay-present behaviour (monkey-patch _NEMO_RELAY_AVAILABLE)
# ---------------------------------------------------------------------------


def _make_fake_nemo_relay():
    """Build a minimal fake nemo_relay module that records calls."""
    fake_nr = MagicMock()

    # ScopeType.Agent must be a concrete value (not another MagicMock chain)
    fake_nr.ScopeType = MagicMock()
    fake_nr.ScopeType.Agent = "AGENT"

    # scope.scope() must be a real context manager
    entered = []
    exited = []

    @contextmanager
    def _fake_scope(name, scope_type, *, data=None, **kwargs):
        entered.append((name, scope_type, data))
        yield MagicMock()  # scope handle
        exited.append(name)

    fake_nr.scope.scope = _fake_scope
    fake_nr._entered = entered
    fake_nr._exited = exited
    return fake_nr


def test_create_agent_scope_opens_relay_scope_when_available(monkeypatch):
    fake_nr = _make_fake_nemo_relay()
    fake_flush = MagicMock()

    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_nemo_relay", fake_nr)
    monkeypatch.setattr(ro, "_flush_subscribers", fake_flush)

    with ro.create_agent_scope("my-session"):
        pass

    assert len(fake_nr._entered) == 1
    name, scope_type, data = fake_nr._entered[0]
    assert "my-session" in name
    assert scope_type == "AGENT"
    assert data is not None
    assert data.get("session_id") == "my-session"


def test_create_agent_scope_name_truncated_at_128_chars(monkeypatch):
    fake_nr = _make_fake_nemo_relay()
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_nemo_relay", fake_nr)
    monkeypatch.setattr(ro, "_flush_subscribers", MagicMock())

    long_id = "x" * 200
    with ro.create_agent_scope(long_id):
        pass

    name, _, _ = fake_nr._entered[0]
    assert len(name) <= 128


def test_create_agent_scope_stores_hermes_home_override(monkeypatch):
    fake_nr = _make_fake_nemo_relay()
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.setenv("_HERMES_HOME_OVERRIDE", "/custom/hermes/home")
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_nemo_relay", fake_nr)
    monkeypatch.setattr(ro, "_flush_subscribers", MagicMock())

    with ro.create_agent_scope("s1"):
        pass

    _, _, data = fake_nr._entered[0]
    assert data.get("hermes_home_override") == "/custom/hermes/home"


def test_create_agent_scope_no_hermes_home_override_when_absent(monkeypatch):
    fake_nr = _make_fake_nemo_relay()
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.delenv("_HERMES_HOME_OVERRIDE", raising=False)
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_nemo_relay", fake_nr)
    monkeypatch.setattr(ro, "_flush_subscribers", MagicMock())

    with ro.create_agent_scope("s2"):
        pass

    _, _, data = fake_nr._entered[0]
    assert "hermes_home_override" not in (data or {})


def test_create_agent_scope_is_noop_when_env_not_set(monkeypatch):
    """Even if nemo-relay were installed, absent env var = no scope opened."""
    fake_nr = _make_fake_nemo_relay()
    monkeypatch.delenv("MAC_RELAY_OBSERVABILITY", raising=False)
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_nemo_relay", fake_nr)
    monkeypatch.setattr(ro, "_flush_subscribers", MagicMock())

    with ro.create_agent_scope("s3"):
        pass

    assert fake_nr._entered == []


def test_flush_calls_flush_subscribers_when_available(monkeypatch):
    fake_flush = MagicMock()
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_flush_subscribers", fake_flush)

    ro.flush()

    fake_flush.assert_called_once_with()


def test_flush_swallows_exceptions_from_flush_subscribers(monkeypatch):
    def _boom():
        raise RuntimeError("flush exploded")

    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_flush_subscribers", _boom)

    # Must not raise
    ro.flush()


def test_create_agent_scope_survives_scope_exception(monkeypatch):
    """If nemo_relay.scope.scope() itself throws, body still runs via yield fallback."""
    fake_nr = MagicMock()
    fake_nr.ScopeType.Agent = "AGENT"

    @contextmanager
    def _boom(*a, **kw):
        raise RuntimeError("relay internal error")
        yield  # unreachable but makes this a generator

    fake_nr.scope.scope = _boom
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    monkeypatch.setattr(ro, "_NEMO_RELAY_AVAILABLE", True)
    monkeypatch.setattr(ro, "_nemo_relay", fake_nr)
    monkeypatch.setattr(ro, "_flush_subscribers", MagicMock())

    ran = []
    with ro.create_agent_scope("s4"):
        ran.append(True)

    assert ran == [True]


# ---------------------------------------------------------------------------
# task_executor integration: relay scope wraps main()
# ---------------------------------------------------------------------------


def test_task_executor_main_imports_relay_observability():
    """task_executor must import relay_observability at module level."""
    from mac import task_executor as te
    assert hasattr(te, "relay_observability")


def test_task_executor_main_calls_create_agent_scope(tmp_path, monkeypatch):
    """main() must open an agent scope using the task_id as session_id."""
    from mac import task_executor as te, relay_observability

    task_id = "task_test_relay_scope_abc123"
    task_payload = {"task": {"id": task_id, "title": "relay test", "project": "p"}}
    task_file = tmp_path / "task.json"
    task_file.write_text(__import__("json").dumps(task_payload))

    monkeypatch.setenv("MAC_TASK_FILE", str(task_file))
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))

    scopes_opened = []

    @contextmanager
    def _fake_scope(session_id):
        scopes_opened.append(session_id)
        yield

    monkeypatch.setattr(relay_observability, "create_agent_scope", _fake_scope)
    monkeypatch.setattr(relay_observability, "flush", MagicMock())

    import subprocess

    def _fake_runner(argv, cwd, task_id_arg, meta):
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(te, "_hub_post", lambda *a, **kw: False)
    monkeypatch.setattr(te, "_hub_get", lambda *a, **kw: None)

    te.main(runner=_fake_runner)

    assert task_id in scopes_opened
