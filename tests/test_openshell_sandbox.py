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

import os
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
    "MAC_OPENSHELL_ALLOW_NO_LANDLOCK",
    "MAC_OPENSHELL_HOST_ALIAS",
    "MAC_HERMES_PYTHON",
    "MAC_ALLOW_UNSANDBOXED_YOLO",
    "HERMES_YOLO_MODE",
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """Empty OpenShell config + isolated HOME so ~/.mac/openshell-policy.yaml
    (an operator-deployed policy) can't leak in from the dev machine and make
    resolution non-deterministic."""
    for name in _OPENSHELL_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Construction tests run on non-Landlock hosts (Mac/CI); bypass the kernel
    # precheck so they exercise the wrap. The precheck has its own tests below.
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
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


# ---------------------------------------------------------------------------
# _agent_invocation: atomic --yolo <-> sandbox coupling
# ---------------------------------------------------------------------------


def test_agent_invocation_sandbox_wraps_and_keeps_yolo(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    out = te._agent_invocation("do the thing")
    assert out[:4] == ["openshell", "sandbox", "create", "--no-auto-providers"]
    assert "--policy" in out                  # enforced default policy
    assert "--yolo" in out                     # YOLO kept, but now sandboxed
    assert out.index("--") < out.index("--yolo")


def test_agent_invocation_unsandboxed_allowed_by_default(monkeypatch):
    # hatch unset -> default allow (current fleet), unwrapped, --yolo present
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    out = te._agent_invocation("do the thing")
    assert "openshell" not in out[0]
    assert out[0].endswith("python")
    assert "--yolo" in out


def test_agent_invocation_unsandboxed_explicit_allow(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    out = te._agent_invocation("do the thing")
    assert out[:1] != ["openshell"]
    assert "--yolo" in out


def test_agent_invocation_unsandboxed_fail_closed(monkeypatch):
    # hatch off + no sandbox -> refuse to launch unguarded YOLO
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError, match="without an OpenShell sandbox"):
        te._agent_invocation("do the thing")


def test_agent_invocation_sandbox_overrides_failclosed_hatch(monkeypatch):
    # sandbox on -> safe regardless of the hatch value (no raise)
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    out = te._agent_invocation("do the thing")
    assert out[0] == "openshell"
    assert "--yolo" in out


# ---------------------------------------------------------------------------
# image-mode runtime path + URL rewriting + Landlock precheck
# ---------------------------------------------------------------------------


def test_hermes_argv_uses_image_python_override(monkeypatch):
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/opt/mac-venv/bin/python")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    argv = te._hermes_argv("do it")
    assert argv[0] == "/opt/mac-venv/bin/python"
    assert argv[1:4] == ["-m", "hermes_cli.main", "chat"]
    # image runtime: no host vendored PYTHONPATH injected
    assert "/.mac/src/" not in os.environ.get("PYTHONPATH", "")


def test_hermes_argv_host_default_injects_pythonpath(monkeypatch):
    monkeypatch.delenv("MAC_HERMES_PYTHON", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    argv = te._hermes_argv("do it")
    assert argv[0].endswith("/.mac/venv/bin/python")
    assert "/.mac/src/mac/src/mac/_hermes" in os.environ["PYTHONPATH"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "0.0.0.0", "::1"])
def test_rewrite_host_local_url(host):
    alias = "host.openshell.internal"
    src = "http://%s:8789/v1/" % ("[::1]" if host == "::1" else host)
    assert te._rewrite_host_local_url(src, alias) == "http://host.openshell.internal:8789/v1/"


def test_rewrite_leaves_nonloopback_and_nonurls():
    alias = "host.openshell.internal"
    assert te._rewrite_host_local_url("http://100.64.0.1:8789", alias) == "http://100.64.0.1:8789"
    assert te._rewrite_host_local_url("secret-token-127.0.0.1", alias) == "secret-token-127.0.0.1"


def test_env_flags_rewrite_loopback_urls(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_ENV_PASSTHROUGH", "MAC_HUB_URL,MAC_WORKER_TOKEN")
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "tok-127.0.0.1-abc")  # not a URL -> untouched
    flags = te._openshell_env_flags()
    assert "MAC_HUB_URL=http://host.openshell.internal:8789" in flags
    assert "MAC_WORKER_TOKEN=tok-127.0.0.1-abc" in flags


def test_host_alias_override(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_HOST_ALIAS", "10.0.0.1")
    assert te._rewrite_host_local_url("http://127.0.0.1:8789", te._openshell_host_alias()) == "http://10.0.0.1:8789"


def test_landlock_precheck_fail_closed(monkeypatch):
    # sandbox on, kernel lacks Landlock, override absent -> refuse (fail closed)
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.setattr(te, "_kernel_has_landlock", lambda: False)
    with pytest.raises(RuntimeError, match="does not expose .*Landlock"):
        te._maybe_wrap_openshell(_ARGV)


def test_landlock_precheck_passes_when_present(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.setattr(te, "_kernel_has_landlock", lambda: True)
    out = te._maybe_wrap_openshell(_ARGV)
    assert out[:3] == ["openshell", "sandbox", "create"]


# --- child HERMES_YOLO_MODE env (fixes the approval.py import-order freeze) ---


def test_agent_invocation_sets_child_yolo_env_when_sandboxed(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    te._agent_invocation("x")
    assert os.environ.get("HERMES_YOLO_MODE") == "1"


def test_agent_invocation_sets_child_yolo_env_when_unsandboxed_allowed(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    te._agent_invocation("x")
    assert os.environ.get("HERMES_YOLO_MODE") == "1"


def test_agent_invocation_failclosed_does_not_set_yolo_env(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError):
        te._agent_invocation("x")
    assert os.environ.get("HERMES_YOLO_MODE") is None
