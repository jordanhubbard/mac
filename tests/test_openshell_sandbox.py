"""Tests for OpenShell sandbox wrapping in the task executor (sandbox-01).

The wrap is a pure argv transform gated by MAC_OPENSHELL_SANDBOX. These tests
pin the default-OFF guarantee (zero behavior change) and the exact `openshell
sandbox create ... -- <argv>` construction so the seam can't drift silently.
They never spawn OpenShell.
"""

from __future__ import annotations

import pytest

from mac import task_executor as te

_ARGV = ["/home/x/.mac/venv/bin/python", "-m", "hermes_cli.main", "chat", "--query", "do it", "--yolo"]

_OPENSHELL_ENVS = [
    "MAC_OPENSHELL_SANDBOX",
    "MAC_OPENSHELL_BIN",
    "MAC_OPENSHELL_POLICY",
    "MAC_OPENSHELL_SANDBOX_NAME",
    "MAC_OPENSHELL_KEEP",
    "MAC_OPENSHELL_CREATE_ARGS",
    "MAC_OPENSHELL_ENV_PASSTHROUGH",
]


@pytest.fixture(autouse=True)
def _clean_openshell_env(monkeypatch):
    """Start every test from a known-empty OpenShell config."""
    for name in _OPENSHELL_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# truthy / enabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_truthy_true(monkeypatch, val):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", val)
    assert te._openshell_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_truthy_false(monkeypatch, val):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", val)
    assert te._openshell_enabled() is False


def test_enabled_default_off():
    assert te._openshell_enabled() is False


# ---------------------------------------------------------------------------
# default OFF -> unchanged argv (the critical safety property)
# ---------------------------------------------------------------------------


def test_disabled_returns_equal_argv():
    out = te._maybe_wrap_openshell(_ARGV)
    assert out == _ARGV


def test_disabled_returns_a_copy():
    """Returns a distinct list so callers can't mutate module state."""
    out = te._maybe_wrap_openshell(_ARGV)
    assert out is not _ARGV


# ---------------------------------------------------------------------------
# enabled -> wrapped construction
# ---------------------------------------------------------------------------


def test_enabled_minimal_no_policy(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    out = te._maybe_wrap_openshell(_ARGV)
    assert out[:4] == ["openshell", "sandbox", "create", "--no-auto-providers"]
    assert "--policy" not in out
    # original argv preserved verbatim after the separator
    sep = out.index("--")
    assert out[sep + 1 :] == _ARGV


def test_separator_appears_once_and_before_argv(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from my-img")
    out = te._maybe_wrap_openshell(_ARGV)
    assert out.count("--") == 1
    assert out.index("--") < out.index(_ARGV[0])


def test_policy_flag(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", "/etc/mac/policy.yaml")
    out = te._maybe_wrap_openshell(_ARGV)
    assert "--policy" in out
    assert out[out.index("--policy") + 1] == "/etc/mac/policy.yaml"


def test_bin_override(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_BIN", "/opt/openshell/bin/openshell")
    out = te._maybe_wrap_openshell(_ARGV)
    assert out[0] == "/opt/openshell/bin/openshell"


def test_name_and_keep(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "dbg-run")
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    out = te._maybe_wrap_openshell(_ARGV)
    assert "--name" in out and out[out.index("--name") + 1] == "dbg-run"
    assert "--keep" in out


def test_no_name_or_keep_by_default(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    out = te._maybe_wrap_openshell(_ARGV)
    assert "--name" not in out
    assert "--keep" not in out


def test_create_args_shell_split(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from img --upload /a:/b")
    out = te._maybe_wrap_openshell(_ARGV)
    # tokens are split and land before the separator
    for tok in ("--from", "img", "--upload", "/a:/b"):
        assert tok in out
        assert out.index(tok) < out.index("--")


# ---------------------------------------------------------------------------
# env passthrough
# ---------------------------------------------------------------------------


def test_env_passthrough_only_set_vars(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_ENV_PASSTHROUGH", "FOO,BAR,FOO")
    monkeypatch.setenv("FOO", "fooval")
    monkeypatch.delenv("BAR", raising=False)
    out = te._maybe_wrap_openshell(_ARGV)
    assert "--env" in out
    # FOO forwarded exactly once (dedup), BAR (unset) skipped
    assert out.count("FOO=fooval") == 1
    assert not any(tok.startswith("BAR=") for tok in out)


def test_env_passthrough_default_list_used(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_HUB_URL", "http://hub:8789")
    out = te._maybe_wrap_openshell(_ARGV)
    assert "MAC_HUB_URL=http://hub:8789" in out
