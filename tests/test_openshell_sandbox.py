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

_ARGV = [
    "/opt/mac-venv/bin/python",
    "-m",
    "mac.agent_command",
    "--command-file",
    "/sandbox/task-7/.mac-agent-command.json",
    "--prompt-file",
    "/sandbox/task-7/.mac-agent-prompt",
]

_OPENSHELL_ENVS = [
    "MAC_OPENSHELL_SANDBOX",
    "MAC_OPENSHELL_BIN",
    "MAC_OPENSHELL_POLICY",
    "MAC_OPENSHELL_SANDBOX_NAME",
    "MAC_OPENSHELL_KEEP",
    "MAC_OPENSHELL_GC",
    "MAC_OPENSHELL_STALE_AFTER_SECONDS",
    "MAC_OPENSHELL_CREATE_ARGS",
    "MAC_OPENSHELL_ENV_PASSTHROUGH",
    "MAC_OPENSHELL_ALLOW_NO_LANDLOCK",
    "MAC_OPENSHELL_HOST_ALIAS",
    "MAC_OPENSHELL_PROGRESS_INTERVAL",
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
    monkeypatch.setenv("MAC_OPENSHELL_PROGRESS_INTERVAL", "0")
    monkeypatch.setattr(te, "_merge_sandbox_download_tree", lambda download_root, workspace: None)
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
    """The non-login `/bin/bash -c <inner>` after the `--` separator."""
    i = out.index("--")
    assert out[i + 1 : i + 3] == ["/bin/bash", "-c"]
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


def test_invoke_unsandboxed_uses_private_prompt_wrapper(tmp_path):
    r = FakeRunner()
    workspace = tmp_path / "t"
    workspace.mkdir()
    te._invoke_agent(r, "do the thing", workspace, "tid", {})
    assert len(r.calls) == 1
    argv = r.calls[0][0]
    assert "openshell" not in argv[0] and argv[0].endswith("python")
    assert "mac.agent_command" in argv
    assert "do the thing" not in argv


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


def test_build_labels_sandbox_for_safe_orphan_collection():
    out = _build()
    labels = [out[index + 1] for index, token in enumerate(out) if token == "--label"]
    assert "mac.owner=mac" in labels
    assert "mac.kind=task" in labels
    assert "mac.keep=false" in labels
    assert any(label.startswith("mac.pid=") for label in labels)


def test_build_marks_debug_kept_sandbox(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    assert "mac.keep=true" in _build()


def test_build_uploads_workspace_to_sandbox_root():
    out = _build("/work/task-7")
    assert "--upload" in out
    assert "/work/task-7:/sandbox" in out


def test_build_runs_private_agent_wrapper_in_workspace_subdir():
    inner = _inner(_build("/work/task-7"))
    assert inner.startswith("cd /sandbox/task-7\n")
    assert "mac_sandbox_toolchain_setup" in inner
    assert 'rm -rf "$MAC_TASK_REPO_WORKTREE/.git"' in inner
    assert 'git -C "$MAC_TASK_REPO_WORKTREE" init -q' in inner
    assert "MAC OpenShell sandbox baseline" in inner
    assert inner.index("mac_sandbox_toolchain_setup") < inner.index("sandbox baseline")
    assert inner.index("sandbox baseline") < inner.index("\nexec ")
    assert "\nexec " in inner
    assert "mac.agent_command" in inner
    assert "hermes_cli.main" not in inner


def test_build_whitelists_uploaded_paths_for_git():
    # The workspace is tar-uploaded, so its files can be owned by a different
    # uid than the sandbox user; without a safe.directory whitelist every git
    # command against uploaded paths dies with "dubious ownership" — including
    # the contract tests the agent runs before declaring done (observed live:
    # workers failed verification on the only 4 tests that run git against the
    # checkout, then correctly refused to push).
    inner = _inner(_build("/work/task-7"))
    assert "GIT_CONFIG_KEY_0=safe.directory" in inner
    assert "GIT_CONFIG_VALUE_0='*'" in inner
    # Must be in force before the git snapshot section AND the agent exec.
    assert inner.index("safe.directory") < inner.index('init -q')
    assert inner.index("safe.directory") < inner.index("\nexec ")


def test_private_env_file_repoints_workspace_without_argv_exposure(tmp_path):
    workspace = tmp_path / "task-7"
    workspace.mkdir()
    env_file, toolchain_file = te._write_sandbox_runtime_files(
        workspace, "/sandbox/task-7"
    )
    content = env_file.read_text(encoding="utf-8")
    assert "HOME=/tmp" in content
    assert "MAC_TASK_WORKSPACE=/sandbox/task-7" in content
    assert "MAC_TASK_FILE=/sandbox/task-7/task.json" in content
    assert toolchain_file.stat().st_mode & 0o777 == 0o700


def test_build_separator_appears_once_before_command():
    out = _build()
    assert out.count("--") == 1
    assert out.index("--") < out.index("/bin/bash")


def test_build_bin_override(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_BIN", "/opt/openshell/bin/openshell")
    assert _build()[0] == "/opt/openshell/bin/openshell"


def test_build_create_args_spliced(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from img --upload /a:/b")
    out = _build()
    for tok in ("--from", "img", "--upload", "/a:/b"):
        assert tok in out
        assert out.index(tok) < out.index("/bin/bash")


def test_build_rejects_direct_prompt_bearing_agent_argv():
    with pytest.raises(ValueError, match="private-file command wrapper"):
        _build(argv=te._hermes_argv("do; rm -rf / # $(whoami)"))


def test_build_rejects_env_values_in_extra_argv(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from img --env TOKEN=secret")
    with pytest.raises(ValueError, match="may not contain --env"):
        _build()


# ---------------------------------------------------------------------------
# env passthrough (copied into the sandbox via a private file)
# ---------------------------------------------------------------------------


def test_env_passthrough_only_set_vars(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_ENV_PASSTHROUGH", "FOO,BAR,FOO")
    monkeypatch.setenv("FOO", "fooval")
    monkeypatch.delenv("BAR", raising=False)
    values = te._openshell_environment()
    assert values == {"FOO": "fooval"}


def test_env_passthrough_rejects_host_path(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_ENV_PASSTHROUGH", "MAC_HUB_URL,PATH")
    with pytest.raises(ValueError, match="PATH may not be forwarded"):
        te._openshell_environment()


def test_env_passthrough_default_list_used(monkeypatch):
    monkeypatch.setenv("MAC_HUB_URL", "http://hub:8789")
    assert te._openshell_environment()["MAC_HUB_URL"] == "http://hub:8789"


def test_env_passthrough_defaults_include_yolo_bypass(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    assert te._openshell_environment()["HERMES_YOLO_MODE"] == "1"


def test_env_passthrough_defaults_include_task_route_context(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("MAC_TASK_ID", "task_route")
    monkeypatch.setenv("MAC_LEASE_ID", "lease_route")

    values = te._openshell_environment()

    assert values["MAC_TASK_ID"] == "task_route"
    assert values["MAC_LEASE_ID"] == "lease_route"


# ---------------------------------------------------------------------------
# sandbox lifecycle orchestration: create -> download -> always delete
# ---------------------------------------------------------------------------


def test_invoke_sandboxed_runs_full_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "sb1")
    steps = []
    events = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args) or (True, "")))
    monkeypatch.setattr(
        te,
        "emit_telemetry",
        lambda event, **detail: events.append((event, detail)) or True,
    )
    r = FakeRunner()
    workspace = tmp_path / "task-7"
    workspace.mkdir()
    te._invoke_agent(r, "do it", workspace, "tid", {})
    # 1 audited create call (runs the agent), then download + delete out-of-band
    assert len(r.calls) == 1
    create = r.calls[0][0]
    assert create[:3] == ["openshell", "sandbox", "create"] and "--upload" in create
    assert steps[0][:3] == ["download", "sb1", "/sandbox/task-7"]
    assert Path(steps[0][3]).name.startswith(".task-7-openshell-download-")
    assert steps[1] == ["delete", "sb1"]
    salvage = (workspace / "openshell-salvage.json").read_text(encoding="utf-8")
    assert '"harvested": true' in salvage
    assert [event for event, _detail in events if event.startswith("sandbox_")] == [
        "sandbox_started",
        "sandbox_agent_completed",
        "sandbox_harvested",
        "sandbox_deleted",
    ]


def test_sandbox_create_argv_is_small_and_contains_no_prompt_or_tokens(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "mac-super-secret-token")
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    workspace = tmp_path / "task-large"
    prompt = "private-task-prompt-" + ("x" * 25000)
    runner = FakeRunner()

    te._invoke_agent(runner, prompt, workspace, "tid", {})

    create = runner.calls[0][0]
    joined = " ".join(create)
    assert "mac-super-secret-token" not in joined
    assert "private-task-prompt" not in joined
    assert "--env" not in create
    assert len(joined) < 4000


def test_progress_monitor_emits_state_transitions_from_sandbox_snapshot(
    monkeypatch, tmp_path
):
    snapshots = iter(
        [
            {
                "ready": "1",
                "head": "a" * 40,
                "changed_count": "0",
                "changed_digest": "clean",
                "manifest": "0",
            },
            {
                "ready": "1",
                "head": "b" * 40,
                "changed_count": "2",
                "changed_digest": "digest-2",
                "manifest": "1",
            },
        ]
    )
    events = []
    monkeypatch.setenv("MAC_OPENSHELL_PROGRESS_INTERVAL", "0")
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", "a" * 40)
    monkeypatch.setattr(
        te, "_sandbox_progress_snapshot", lambda *args: next(snapshots)
    )
    monkeypatch.setattr(
        te,
        "emit_telemetry",
        lambda event, **detail: events.append((event, detail)) or True,
    )
    monitor = te._SandboxProgressMonitor("sb", "task", tmp_path, "tid")

    monitor.observe()
    monitor.observe()

    assert [event for event, _detail in events] == [
        "sandbox_ready",
        "sandbox_head_observed",
        "sandbox_first_mutation",
        "sandbox_head_observed",
        "sandbox_manifest_observed",
    ]
    assert monitor.evidence()["changed_file_digest"] == "digest-2"
    assert monitor.evidence()["manifest_observed"] is True


def test_invoke_sandboxed_harvests_then_tears_down_on_agent_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "sb2")
    steps = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args[0]) or (True, "")))
    workspace = tmp_path / "task-7"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="agent boom"):
        te._invoke_agent(FakeRunner(raises=True), "do it", workspace, "tid", {})
    assert steps[-2:] == ["download", "delete"]
    assert '"runner_completed": false' in (
        workspace / "openshell-salvage.json"
    ).read_text(encoding="utf-8")


def test_invoke_sandboxed_keep_skips_delete(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    steps = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args[0]) or (True, "")))
    workspace = tmp_path / "task-7"
    workspace.mkdir()
    te._invoke_agent(FakeRunner(), "do it", workspace, "tid", {})
    assert "download" in steps and "delete" not in steps


# ---------------------------------------------------------------------------
# --yolo <-> sandbox coupling (never an unguarded YOLO agent)
# ---------------------------------------------------------------------------


def test_unsandboxed_allowed_by_default(monkeypatch):
    # _unsandboxed_agent_argv now gates an already-built agent argv (the runner
    # selection happens upstream in _agent_argv); pass the Hermes argv here.
    argv = te._unsandboxed_agent_argv(te._hermes_argv("do the thing"))
    assert "openshell" not in argv[0] and argv[0].endswith("python") and "--yolo" in argv


def test_unsandboxed_explicit_allow(monkeypatch):
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    assert "--yolo" in te._unsandboxed_agent_argv(te._hermes_argv("do the thing"))


def test_unsandboxed_fail_closed_raises(monkeypatch):
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError, match="without an OpenShell sandbox"):
        te._unsandboxed_agent_argv(te._hermes_argv("do the thing"))


def test_invoke_unsandboxed_fail_closed_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError, match="without an OpenShell sandbox"):
        te._invoke_agent(FakeRunner(), "do it", tmp_path / "t", "tid", {})


def test_invoke_sandbox_overrides_failclosed_hatch(monkeypatch, tmp_path):
    # sandbox on -> safe regardless of the unsandboxed hatch (no raise)
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    r = FakeRunner()
    te._invoke_agent(r, "do it", tmp_path / "t", "tid", {})
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
    values = te._openshell_environment()
    assert values["MAC_HUB_URL"] == "http://host.openshell.internal:8789"
    assert values["MAC_WORKER_TOKEN"] == "tok-127.0.0.1-abc"


def test_repo_worktree_aliases_hermes_clone_path(monkeypatch):
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", "/work/task-7/repo-lease")
    inner = _inner(_build())
    assert "/sandbox/mac-clone" in inner
    assert 'ln -s "$MAC_TASK_REPO_WORKTREE" /sandbox/mac-clone' in inner


def test_host_alias_override(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_HOST_ALIAS", "10.0.0.1")
    assert te._rewrite_host_local_url("http://127.0.0.1:8789", te._openshell_host_alias()) == "http://10.0.0.1:8789"


# ---------------------------------------------------------------------------
# Landlock fail-closed precheck
# ---------------------------------------------------------------------------


class _FakeSyscall:
    def __init__(self, result):
        self.result = result
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeLibc:
    def __init__(self, result):
        self.syscall = _FakeSyscall(result)


def test_landlock_abi_probe_uses_kernel_version_syscall(monkeypatch):
    libc = _FakeLibc(4)
    monkeypatch.setattr(te.sys, "platform", "linux")
    monkeypatch.setattr(te.ctypes, "CDLL", lambda *args, **kwargs: libc)

    assert te._landlock_abi_version() == 4
    assert te._kernel_has_landlock() is True
    assert len(libc.syscall.calls) == 2
    call = libc.syscall.calls[0]
    assert call[0].value == te._LANDLOCK_CREATE_RULESET_SYSCALL
    assert call[1].value is None
    assert call[2].value == 0
    assert call[3].value == te._LANDLOCK_CREATE_RULESET_VERSION


@pytest.mark.parametrize("result", [-1, 0])
def test_landlock_abi_probe_fails_closed_on_nonpositive_result(monkeypatch, result):
    monkeypatch.setattr(te.sys, "platform", "linux")
    monkeypatch.setattr(te.ctypes, "CDLL", lambda *args, **kwargs: _FakeLibc(result))
    assert te._landlock_abi_version() == 0
    assert te._kernel_has_landlock() is False


def test_landlock_abi_probe_is_linux_only(monkeypatch):
    monkeypatch.setattr(te.sys, "platform", "darwin")

    def unexpected_cdll(*args, **kwargs):
        raise AssertionError("CDLL must not be called off Linux")

    monkeypatch.setattr(te.ctypes, "CDLL", unexpected_cdll)
    assert te._landlock_abi_version() == 0


def test_landlock_precheck_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.setattr(te, "_kernel_has_landlock", lambda: False)
    with pytest.raises(RuntimeError, match="Landlock"):
        te._invoke_agent(FakeRunner(), "do it", tmp_path / "t", "tid", {})


def test_landlock_precheck_passes_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.setattr(te, "_kernel_has_landlock", lambda: True)
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    r = FakeRunner()
    te._invoke_agent(r, "do it", tmp_path / "t", "tid", {})
    assert r.calls[0][0][:3] == ["openshell", "sandbox", "create"]


# --- child HERMES_YOLO_MODE env (fixes the approval.py import-order freeze) ---


def test_sets_child_yolo_env_when_sandboxed(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    te._invoke_agent(FakeRunner(), "x", tmp_path / "t", "tid", {})
    assert os.environ.get("HERMES_YOLO_MODE") == "1"


def test_sets_child_yolo_env_when_unsandboxed_allowed(monkeypatch, tmp_path):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    te._invoke_agent(FakeRunner(), "x", tmp_path / "t", "tid", {})
    assert os.environ.get("HERMES_YOLO_MODE") == "1"


def test_failclosed_does_not_set_yolo_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError):
        te._invoke_agent(FakeRunner(), "x", tmp_path / "t", "tid", {})
    assert os.environ.get("HERMES_YOLO_MODE") != "1"
