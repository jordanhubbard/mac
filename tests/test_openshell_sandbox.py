"""Tests for OpenShell sandbox enforcement in the task executor (sandbox-01).

Enforcement is gated by MAC_OPENSHELL_SANDBOX (default OFF). When on, the run is
a lifecycle — `create` (upload the task workspace, run the agent confined, keep)
-> `download` (sync edits + evidence back) -> `delete` — because OpenShell
sandboxes are container copies with no bind-mount. These tests pin the
default-OFF guarantee, the create-argv construction (policy always passed,
workspace uploaded, agent run in /sandbox/<basename> with the in-sandbox
workspace env), the lifecycle orchestration (download + always-teardown), the
--yolo<->sandbox coupling, the loopback URL rewrite, and the Landlock
fail-closed precheck. They never spawn OpenShell.
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
    # Tests run on non-Landlock hosts (Mac/CI); bypass the kernel precheck so the
    # orchestration tests exercise the lifecycle. The precheck has its own tests.
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
    yield


class _Result:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode


class FakeRunner:
    """Records (argv, workspace, audit_id, opts) calls; returns a 0 result."""

    def __init__(self, rc: int = 0, raises: bool = False):
        self.calls = []
        self._rc = rc
        self._raises = raises

    def __call__(self, argv, workspace, audit_id, opts):
        self.calls.append((list(argv), workspace, audit_id, opts))
        if self._raises:
            raise RuntimeError("agent boom")
        return _Result(self._rc)


def _policy_of(out):
    return out[out.index("--policy") + 1] if "--policy" in out else None


def _build(workspace="/work/task-7", argv=None):
    """Build a create-argv directly (no env gating — that's _invoke_agent's job)."""
    ws = Path(workspace)
    return te._build_sandbox_create_argv("sb-test", ws, te._workspace_basename(ws), argv or _ARGV)


def _inner(out):
    """The `bash -lc <inner>` command string after the `--` separator."""
    i = out.index("--")
    assert out[i + 1 : i + 3] == ["bash", "-lc"]
    return out[i + 3]


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
# default OFF -> the agent runs directly, unwrapped (critical safety property)
# ---------------------------------------------------------------------------


def test_invoke_unsandboxed_runs_plain_argv():
    r = FakeRunner()
    te._invoke_agent(r, "do the thing", Path("/work/t"), "tid", {})
    assert len(r.calls) == 1
    argv = r.calls[0][0]
    assert "openshell" not in argv[0] and argv[0].endswith("python")
    assert "--yolo" in argv


# ---------------------------------------------------------------------------
# create-argv: a policy is ALWAYS passed (no silent image-default fallback)
# ---------------------------------------------------------------------------


def test_build_has_create_prefix_and_bundled_policy():
    out = _build()
    assert out[:4] == ["openshell", "sandbox", "create", "--no-auto-providers"]
    assert _policy_of(out) == str(te._bundled_default_policy())


def test_bundled_default_policy_exists():
    assert te._bundled_default_policy().is_file()


def test_build_explicit_policy_used(monkeypatch, tmp_path):
    p = tmp_path / "my-policy.yaml"
    p.write_text("version: 1\n")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(p))
    assert _policy_of(_build()) == str(p)


def test_build_missing_explicit_policy_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        _build()


def test_build_deployed_policy_preferred_over_bundled(tmp_path):
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    deployed = mac_dir / "openshell-policy.yaml"
    deployed.write_text("version: 1\n")  # HOME already == tmp_path
    assert _policy_of(_build()) == str(deployed)


# ---------------------------------------------------------------------------
# create-argv construction: name, workspace upload, in-sandbox run + env
# ---------------------------------------------------------------------------


def test_build_names_the_sandbox():
    out = _build()
    assert "--name" in out and out[out.index("--name") + 1] == "sb-test"


def test_build_uploads_workspace_to_sandbox_root():
    out = _build("/work/task-7")
    assert "--upload" in out
    assert "/work/task-7:/sandbox" in out


def test_build_runs_agent_in_workspace_subdir_with_yolo():
    inner = _inner(_build("/work/task-7"))
    assert inner.startswith("cd /sandbox/task-7 && exec ")
    assert "hermes_cli.main" in inner and "--yolo" in inner


def test_build_repoints_workspace_env_into_sandbox():
    out = _build("/work/task-7")
    assert "MAC_TASK_WORKSPACE=/sandbox/task-7" in out
    assert "MAC_TASK_FILE=/sandbox/task-7/task.json" in out


def test_build_separator_appears_once_before_command():
    out = _build()
    assert out.count("--") == 1
    assert out.index("--") < out.index("bash")


def test_build_bin_override(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_BIN", "/opt/openshell/bin/openshell")
    assert _build()[0] == "/opt/openshell/bin/openshell"


def test_build_create_args_spliced(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from img --upload /a:/b --env HOME=/tmp")
    out = _build()
    for tok in ("--from", "img", "--upload", "/a:/b", "--env", "HOME=/tmp"):
        assert tok in out
        assert out.index(tok) < out.index("bash")


def test_build_quotes_prompt_safely():
    # A shell-hostile prompt must not be able to break out of the cd-wrapper.
    argv = te._hermes_argv("do; rm -rf / # $(whoami)")
    inner = _inner(_build(argv=argv))
    # the dangerous text is single-quoted inside the exec'd command, not bare
    assert "rm -rf /" not in inner.replace("'do; rm -rf / # $(whoami)'", "")


# ---------------------------------------------------------------------------
# env passthrough (forwarded into the sandbox via --env)
# ---------------------------------------------------------------------------


def test_env_passthrough_only_set_vars(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_ENV_PASSTHROUGH", "FOO,BAR,FOO")
    monkeypatch.setenv("FOO", "fooval")
    monkeypatch.delenv("BAR", raising=False)
    out = _build()
    assert out.count("FOO=fooval") == 1
    assert not any(tok.startswith("BAR=") for tok in out)


def test_env_passthrough_default_list_used(monkeypatch):
    monkeypatch.setenv("MAC_HUB_URL", "http://hub:8789")
    assert "MAC_HUB_URL=http://hub:8789" in _build()


# ---------------------------------------------------------------------------
# sandbox lifecycle orchestration: create -> download -> always delete
# ---------------------------------------------------------------------------


def test_invoke_sandboxed_runs_full_lifecycle(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "sb1")
    steps = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args) or (True, "")))
    r = FakeRunner()
    te._invoke_agent(r, "do it", Path("/work/task-7"), "tid", {})
    # 1 audited create call (runs the agent), then download + delete out-of-band
    assert len(r.calls) == 1
    create = r.calls[0][0]
    assert create[:3] == ["openshell", "sandbox", "create"] and "--upload" in create
    assert steps[0] == ["download", "sb1", "/sandbox/task-7", "/work/task-7"]
    assert steps[1] == ["delete", "sb1"]


def test_invoke_sandboxed_tears_down_on_agent_failure(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "sb2")
    steps = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args[0]) or (True, "")))
    with pytest.raises(RuntimeError, match="agent boom"):
        te._invoke_agent(FakeRunner(raises=True), "do it", Path("/work/task-7"), "tid", {})
    assert "delete" in steps  # teardown still ran (finally)


def test_invoke_sandboxed_keep_skips_delete(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    steps = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args[0]) or (True, "")))
    te._invoke_agent(FakeRunner(), "do it", Path("/work/task-7"), "tid", {})
    assert "download" in steps and "delete" not in steps


# ---------------------------------------------------------------------------
# --yolo <-> sandbox coupling (never an unguarded YOLO agent)
# ---------------------------------------------------------------------------


def test_unsandboxed_allowed_by_default(monkeypatch):
    argv = te._unsandboxed_agent_argv("do the thing")
    assert "openshell" not in argv[0] and argv[0].endswith("python") and "--yolo" in argv


def test_unsandboxed_explicit_allow(monkeypatch):
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    assert "--yolo" in te._unsandboxed_agent_argv("do the thing")


def test_unsandboxed_fail_closed_raises(monkeypatch):
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError, match="without an OpenShell sandbox"):
        te._unsandboxed_agent_argv("do the thing")


def test_invoke_unsandboxed_fail_closed_raises(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError, match="without an OpenShell sandbox"):
        te._invoke_agent(FakeRunner(), "do it", Path("/work/t"), "tid", {})


def test_invoke_sandbox_overrides_failclosed_hatch(monkeypatch):
    # sandbox on -> safe regardless of the unsandboxed hatch (no raise)
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    r = FakeRunner()
    te._invoke_agent(r, "do it", Path("/work/t"), "tid", {})
    assert r.calls[0][0][0] == "openshell"


# ---------------------------------------------------------------------------
# in-image runtime path + loopback URL rewriting
# ---------------------------------------------------------------------------


def test_hermes_argv_uses_image_python_override(monkeypatch):
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/opt/mac-venv/bin/python")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    argv = te._hermes_argv("do it")
    assert argv[0] == "/opt/mac-venv/bin/python"
    assert argv[1:4] == ["-m", "hermes_cli.main", "chat"]
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


# ---------------------------------------------------------------------------
# Landlock fail-closed precheck
# ---------------------------------------------------------------------------


def test_landlock_precheck_fail_closed(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.setattr(te, "_kernel_has_landlock", lambda: False)
    with pytest.raises(RuntimeError, match="does not expose .*Landlock"):
        te._invoke_agent(FakeRunner(), "do it", Path("/work/t"), "tid", {})


def test_landlock_precheck_passes_when_present(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.setattr(te, "_kernel_has_landlock", lambda: True)
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    r = FakeRunner()
    te._invoke_agent(r, "do it", Path("/work/t"), "tid", {})
    assert r.calls[0][0][:3] == ["openshell", "sandbox", "create"]


# --- child HERMES_YOLO_MODE env (fixes the approval.py import-order freeze) ---


def test_sets_child_yolo_env_when_sandboxed(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    te._invoke_agent(FakeRunner(), "x", Path("/work/t"), "tid", {})
    assert os.environ.get("HERMES_YOLO_MODE") == "1"


def test_sets_child_yolo_env_when_unsandboxed_allowed(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    te._invoke_agent(FakeRunner(), "x", Path("/work/t"), "tid", {})
    assert os.environ.get("HERMES_YOLO_MODE") == "1"


def test_failclosed_does_not_set_yolo_env(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError):
        te._invoke_agent(FakeRunner(), "x", Path("/work/t"), "tid", {})
    assert os.environ.get("HERMES_YOLO_MODE") != "1"
