"""Tests for OpenShell gateway sandboxing (sandbox-01).

The gateway entrypoint self-re-execs under an ephemeral OpenShell sandbox when
MAC_OPENSHELL_GATEWAY is enabled. Default OFF; a policy is always passed; the
re-exec is guarded so it doesn't recurse once inside the sandbox. No process is
ever actually replaced here — os.execvp is patched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mac import hermes_gateway as hg

_ENVS = [
    "MAC_OPENSHELL_GATEWAY",
    "MAC_OPENSHELL_BIN",
    "MAC_OPENSHELL_GATEWAY_POLICY",
    "MAC_OPENSHELL_GATEWAY_CREATE_ARGS",
    "_MAC_OPENSHELL_GATEWAY_ACTIVE",
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for n in _ENVS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.mac/openshell-gateway-policy.yaml
    yield


# --- enable gate ---


def test_disabled_by_default():
    assert hg._gateway_sandbox_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "On", "yes"])
def test_enabled_truthy(monkeypatch, v):
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY", v)
    assert hg._gateway_sandbox_enabled() is True


# --- policy resolution ---


def test_resolve_bundled_fallback():
    out = hg._resolve_gateway_policy()
    assert out.endswith("gateway-default-policy.yaml")
    assert Path(out).is_file()


def test_resolve_explicit(monkeypatch, tmp_path):
    p = tmp_path / "gw.yaml"
    p.write_text("version: 1\n")
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY_POLICY", str(p))
    assert hg._resolve_gateway_policy() == str(p)


def test_resolve_missing_explicit_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY_POLICY", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        hg._resolve_gateway_policy()


def test_resolve_deployed_preferred(tmp_path):
    macd = tmp_path / ".mac"
    macd.mkdir()
    dep = macd / "openshell-gateway-policy.yaml"
    dep.write_text("version: 1\n")  # HOME already == tmp_path
    assert hg._resolve_gateway_policy() == str(dep)


# --- argv construction ---


def test_build_argv_shape():
    argv = hg._build_gateway_sandbox_argv()
    assert argv[:4] == ["admin", "openshell", "sandbox", "create", "--no-auto-providers"]
    assert "--policy" in argv
    assert argv.count("--") == 1
    assert argv[-3:] == [sys.executable, "-m", "mac.hermes_gateway"]
    assert "--keep" not in argv and "--name" not in argv  # ephemeral


def test_build_argv_create_args_and_bin(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_BIN", "/opt/os/openshell")
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY_CREATE_ARGS", "--from gw-img --cpu 2")
    argv = hg._build_gateway_sandbox_argv()
    assert argv[0] == "/opt/os/openshell"
    for tok in ("--from", "gw-img", "--cpu", "2"):
        assert tok in argv and argv.index(tok) < argv.index("--")


# --- re-exec guard ---


def test_reexec_noop_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(os, "execvp", lambda *a: called.append(a))
    hg._maybe_reexec_under_openshell()
    assert called == []


def test_reexec_execs_when_enabled(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY", "1")
    captured = {}
    monkeypatch.setattr(os, "execvp", lambda f, a: captured.update(file=f, args=a))
    hg._maybe_reexec_under_openshell()
    assert captured["file"] == "openshell"
    assert captured["args"][:3] == ["admin", "openshell", "sandbox", "create"]
    assert captured["args"][-2:] == ["-m", "mac.hermes_gateway"]
    assert os.environ.get("_MAC_OPENSHELL_GATEWAY_ACTIVE") == "1"


def test_reexec_noop_when_already_active(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY", "1")
    monkeypatch.setenv("_MAC_OPENSHELL_GATEWAY_ACTIVE", "1")
    called = []
    monkeypatch.setattr(os, "execvp", lambda *a: called.append(a))
    hg._maybe_reexec_under_openshell()
    assert called == []  # already inside the sandbox


def test_main_with_injected_cli_does_not_reexec(monkeypatch):
    # Even with the flag on, the test-injection path must not re-exec.
    monkeypatch.setenv("MAC_OPENSHELL_GATEWAY", "1")

    def _boom(*a):
        raise AssertionError("must not re-exec when _cli_main is injected")

    monkeypatch.setattr(os, "execvp", _boom)
    monkeypatch.setattr("mac.hermes_vendor.ensure_on_path", lambda: None)
    monkeypatch.setattr(hg, "log_provider_decision", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", list(sys.argv))  # isolate argv mutation
    assert hg.main(_cli_main=lambda: 0) == 0
