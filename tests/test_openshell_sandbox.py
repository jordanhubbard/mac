"""Tests for OpenShell sandbox wrapping in the task executor (sandbox-01).

The wrap is gated by MAC_OPENSHELL_SANDBOX. These tests pin the default-OFF
guarantee (zero behavior change), the policy-resolution order, and the exact
`openshell sandbox create ... -- <argv>` construction so the seam can't drift.
They never spawn OpenShell.

A policy is ALWAYS passed when enabled (explicit -> deployed -> bundled
fail-closed default), so enabling can never silently fall back to OpenShell's
own image-default profile.
"""

from __future__ import annotations

from pathlib import Path

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
def _clean(monkeypatch, tmp_path):
    """Empty OpenShell config + isolated HOME so ~/.mac/openshell-policy.yaml
    (an operator-deployed policy) can't leak in from the dev machine and make
    resolution non-deterministic."""
    for name in _OPENSHELL_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


def _policy_of(out):
    """Return the value passed after --policy, or None."""
    return out[out.index("--policy") + 1] if "--policy" in out else None


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
    assert te._maybe_wrap_openshell(_ARGV) == _ARGV


def test_disabled_returns_a_copy():
    assert te._maybe_wrap_openshell(_ARGV) is not _ARGV


# ---------------------------------------------------------------------------
# enabled -> a policy is ALWAYS passed (no silent image-default fallback)
# ---------------------------------------------------------------------------


def test_enabled_falls_back_to_bundled_policy(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    out = te._maybe_wrap_openshell(_ARGV)
    assert out[:4] == ["openshell", "sandbox", "create", "--no-auto-providers"]
    # --policy is always present, resolving to the bundled fail-closed default
    assert _policy_of(out) == str(te._bundled_default_policy())
    # original argv preserved verbatim after the separator
    sep = out.index("--")
    assert out[sep + 1 :] == _ARGV


def test_bundled_default_policy_exists():
    """The fail-closed default must ship in the package (it's the fallback)."""
    assert te._bundled_default_policy().is_file()


def test_explicit_policy_used(monkeypatch, tmp_path):
    p = tmp_path / "my-policy.yaml"
    p.write_text("version: 1\n")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(p))
    out = te._maybe_wrap_openshell(_ARGV)
    assert _policy_of(out) == str(p)


def test_missing_explicit_policy_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        te._maybe_wrap_openshell(_ARGV)


def test_deployed_policy_preferred_over_bundled(monkeypatch, tmp_path):
    """~/.mac/openshell-policy.yaml wins when no explicit policy is set."""
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    deployed = mac_dir / "openshell-policy.yaml"
    deployed.write_text("version: 1\n")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")  # HOME already == tmp_path
    out = te._maybe_wrap_openshell(_ARGV)
    assert _policy_of(out) == str(deployed)


# ---------------------------------------------------------------------------
# flag construction
# ---------------------------------------------------------------------------


def test_separator_appears_once_and_before_argv(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from my-img")
    out = te._maybe_wrap_openshell(_ARGV)
    assert out.count("--") == 1
    assert out.index("--") < out.index(_ARGV[0])


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
    assert out.count("FOO=fooval") == 1
    assert not any(tok.startswith("BAR=") for tok in out)


def test_env_passthrough_default_list_used(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_HUB_URL", "http://hub:8789")
    out = te._maybe_wrap_openshell(_ARGV)
    assert "MAC_HUB_URL=http://hub:8789" in out
