"""Tests for OpenShell sandbox enforcement in the task executor (sandbox-01).

Enforcement is gated by MAC_OPENSHELL_SANDBOX (default OFF). When on, the run is
a lifecycle — `create` (upload the task workspace, run the agent confined, keep)
-> `download` (sync edits + evidence back) -> preserve repository WIP -> `delete` — because OpenShell
sandboxes are container copies with no bind-mount. These tests pin the
default-OFF guarantee, the create-argv construction (policy always passed,
workspace uploaded, agent run in /sandbox/<basename> with the in-sandbox
workspace env), the lifecycle orchestration (download + always-teardown), the
--yolo<->sandbox coupling, the loopback URL rewrite, and the Landlock
fail-closed precheck. They never spawn OpenShell.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mac import openshell_sandbox_gc as sandbox_gc
from mac import task_executor as te

_REAL_MERGE_SANDBOX_DOWNLOAD_TREE = te._merge_sandbox_download_tree

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
    "MAC_OPENSHELL_REAP_ORPHANS",
    "MAC_OPENSHELL_RECONCILE_LEASES",
    "MAC_OPENSHELL_STALE_AFTER_SECONDS",
    "MAC_OPENSHELL_CREATE_ARGS",
    "MAC_OPENSHELL_ENV_PASSTHROUGH",
    "MAC_OPENSHELL_ALLOW_NO_LANDLOCK",
    "MAC_OPENSHELL_HOST_ALIAS",
    "MAC_OPENSHELL_PROGRESS_INTERVAL",
    "MAC_TASK_REPO_WORKTREE",
    "MAC_TASK_REPO_ACCESS_MODE",
    "MAC_HERMES_PYTHON",
    "MAC_ALLOW_UNSANDBOXED_YOLO",
    "MAC_EXECUTOR_BACKEND",
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
    monkeypatch.setenv("MAC_OPENSHELL_REAP_ORPHANS", "0")
    monkeypatch.setenv("MAC_OPENSHELL_RECONCILE_LEASES", "0")
    # Hard test isolation: an accidental opt-in cannot reach the developer's
    # live OpenShell gateway. Individual tests may replace these fakes with
    # scenario-specific reports.
    empty_gc_report = {
        "scanned": 0,
        "protected": 0,
        "candidates": [],
        "deleted": [],
        "failures": [],
    }
    monkeypatch.setattr(
        sandbox_gc,
        "reconcile_stale_sandboxes",
        lambda **_kwargs: dict(empty_gc_report),
    )
    monkeypatch.setattr(
        sandbox_gc,
        "reap_orphaned_task_sandboxes",
        lambda **_kwargs: dict(empty_gc_report),
    )
    monkeypatch.setattr(
        sandbox_gc,
        "reconcile_task_sandboxes_from_lease_authority",
        lambda **_kwargs: dict(empty_gc_report),
    )
    monkeypatch.setattr(te, "_merge_sandbox_download_tree", lambda download_root, workspace: None)
    yield


def test_background_reaper_uses_only_injected_fake_cli(monkeypatch):
    calls = []

    def fake_reap(**kwargs):
        calls.append(kwargs)
        return {
            "scanned": 0,
            "protected": 0,
            "candidates": [],
            "deleted": [],
            "failures": [],
        }

    monkeypatch.setenv("MAC_OPENSHELL_REAP_ORPHANS", "1")
    monkeypatch.setenv("MAC_OPENSHELL_BIN", "/test-only/fake-openshell")
    monkeypatch.setattr(sandbox_gc, "reap_orphaned_task_sandboxes", fake_reap)

    te._reap_orphaned_task_sandboxes_best_effort("task-test")

    assert calls == [
        {
            "openshell_bin": "/test-only/fake-openshell",
            "apply": True,
        }
    ]


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


@pytest.fixture(scope="module")
def create_argv(tmp_path_factory):
    """The default create-argv, built exactly ONCE for the whole module.

    `_build_sandbox_create_argv` is pure and its output does not depend on
    MAC_TASK_REPO_WORKTREE (that name is only a runtime shell variable inside
    the emitted bash), so every ``_build()`` in this group is byte-identical.
    Rather than recompute the spec per test, build it a single time and let each
    test assert its own field/section. Built under a scrubbed OpenShell env with
    an empty HOME so a dev-machine ~/.mac/openshell-policy.yaml can't leak in and
    make policy resolution non-deterministic (mirrors the autouse `_clean`)."""
    home = tmp_path_factory.mktemp("openshell-clean-home")
    with pytest.MonkeyPatch.context() as mp:
        for name in _OPENSHELL_ENVS:
            mp.delenv(name, raising=False)
        mp.setenv("HOME", str(home))
        return _build()


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


def test_build_has_create_prefix_and_bundled_policy(create_argv):
    out = create_argv
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


def test_build_names_the_sandbox(create_argv):
    out = create_argv
    assert "--name" in out and out[out.index("--name") + 1] == "sb-test"


def test_build_labels_sandbox_for_safe_orphan_collection(create_argv):
    out = create_argv
    labels = [out[index + 1] for index, token in enumerate(out) if token == "--label"]
    assert "mac.owner=mac" in labels
    assert "mac.kind=task" in labels
    assert "mac.keep=false" in labels
    assert any(label.startswith("mac.pid=") for label in labels)


def test_build_marks_debug_kept_sandbox(monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    assert "mac.keep=true" in _build()


def test_build_marks_repository_sandbox_kept_until_wip_is_preserved(
    monkeypatch,
):
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", "/work/task-7/repo")
    assert "mac.keep=true" in _build()


def test_build_does_not_leak_read_only_report_sandbox(monkeypatch):
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", "/work/task-7/repo")
    monkeypatch.setenv("MAC_TASK_REPO_ACCESS_MODE", "read_only")
    assert "mac.keep=false" in _build()


def test_build_uploads_workspace_to_sandbox_root(create_argv):
    out = create_argv
    assert "--upload" in out
    assert "/work/task-7:/sandbox" in out


def test_build_runs_private_agent_wrapper_in_workspace_subdir(create_argv):
    inner = _inner(create_argv)
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


def test_build_whitelists_uploaded_paths_for_git(create_argv):
    # The workspace is tar-uploaded, so its files can be owned by a different
    # uid than the sandbox user; without a safe.directory whitelist every git
    # command against uploaded paths dies with "dubious ownership" — including
    # the contract tests the agent runs before declaring done (observed live:
    # workers failed verification on the only 4 tests that run git against the
    # checkout, then correctly refused to push).
    inner = _inner(create_argv)
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


def test_build_separator_appears_once_before_command(create_argv):
    out = create_argv
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
        _build(argv=["codex", "exec", "do; rm -rf / # $(whoami)"])


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


def test_env_passthrough_defaults_include_repository_credentials(monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("GITEA_TOKEN", "gitea-secret")
    monkeypatch.setenv("GITEA_USER", "git-user")

    values = te._openshell_environment()

    assert values["GH_TOKEN"] == "github-secret"
    assert values["GITEA_TOKEN"] == "gitea-secret"
    assert values["GITEA_USER"] == "git-user"


def test_read_only_report_env_withholds_repository_credentials_and_git_config(
    monkeypatch,
):
    monkeypatch.setenv(
        "MAC_OPENSHELL_ENV_PASSTHROUGH",
        "GH_TOKEN,GIT_ASKPASS,SSH_ASKPASS,SSH_AUTH_SOCK,GIT_CONFIG_COUNT,"
        "GIT_CONFIG_KEY_0,GIT_CONFIG_VALUE_0,OPENAI_API_KEY",
    )
    monkeypatch.setenv("MAC_TASK_REPO_ACCESS_MODE", "read_only")
    monkeypatch.setenv(
        "MAC_TASK_REPO_ACCESS_SCHEMA", "mac.report_repository_access.v1"
    )
    for name in (
        "GH_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        monkeypatch.setenv(name, "must-not-pass")
    monkeypatch.setenv("OPENAI_API_KEY", "model-credential")

    with pytest.raises(ValueError, match="non-allowlisted"):
        te._openshell_environment()


# ---------------------------------------------------------------------------
# sandbox lifecycle orchestration: create -> download -> always delete
# ---------------------------------------------------------------------------


def test_invoke_sandboxed_runs_full_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "sb1")
    monkeypatch.setenv("MAC_CODING_AGENT_SANDBOX", "off")
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


def test_repository_failure_preserves_wip_before_delete(monkeypatch, tmp_path):
    workspace = tmp_path / "task-7"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setattr(te, "_sandbox_name", lambda: "sb-repo-failure")
    monkeypatch.setattr(te, "_sandbox_gc_best_effort", lambda: None)
    monkeypatch.setattr(te, "_reap_orphaned_task_sandboxes_best_effort", lambda *_: None)
    monkeypatch.setattr(
        te,
        "_reconcile_task_sandboxes_from_lease_authority_best_effort",
        lambda *_: None,
    )
    monkeypatch.setattr(te, "_sandbox_download", lambda *_: True)
    deleted = []
    monkeypatch.setattr(
        te, "_sandbox_delete", lambda name: deleted.append(name) or True
    )
    preserved = {
        "schema": te.REPOSITORY_WIP_BUNDLE_SCHEMA,
        "status": "preserved",
        "salvage_head_sha": "a" * 40,
        "bundle_sha256": "sha256:" + ("b" * 64),
    }
    monkeypatch.setattr(
        te, "preserve_repository_wip_bundle", lambda *_: preserved
    )
    task = {
        "id": "task-repo-failure",
        "metadata": {"execution_contract": {"type": "repository"}},
    }

    result = te._run_sandboxed(
        FakeRunner(rc=124),
        _ARGV,
        workspace,
        task["id"],
        {"task": task},
    )

    assert result.returncode == 124
    assert deleted == ["sb-repo-failure"]
    salvage = json.loads(
        (workspace / "openshell-salvage.json").read_text(encoding="utf-8")
    )
    assert salvage["wip_preservation"] == preserved
    assert salvage["kept"] is False


def test_repository_failure_retains_sandbox_when_wip_preservation_fails(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "task-7"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setattr(te, "_sandbox_name", lambda: "sb-repo-retained")
    monkeypatch.setattr(te, "_sandbox_gc_best_effort", lambda: None)
    monkeypatch.setattr(te, "_reap_orphaned_task_sandboxes_best_effort", lambda *_: None)
    monkeypatch.setattr(
        te,
        "_reconcile_task_sandboxes_from_lease_authority_best_effort",
        lambda *_: None,
    )
    monkeypatch.setattr(te, "_sandbox_download", lambda *_: True)
    deleted = []
    monkeypatch.setattr(
        te, "_sandbox_delete", lambda name: deleted.append(name) or True
    )

    def refuse_preservation(*_args):
        raise te.PreservationMissing("bundle verification failed")

    monkeypatch.setattr(te, "preserve_repository_wip_bundle", refuse_preservation)
    task = {
        "id": "task-repo-failure",
        "metadata": {"execution_contract": {"type": "repository"}},
    }

    result = te._run_sandboxed(
        FakeRunner(rc=124),
        _ARGV,
        workspace,
        task["id"],
        {"task": task},
    )

    assert result.returncode == 124
    assert deleted == []
    salvage = json.loads(
        (workspace / "openshell-salvage.json").read_text(encoding="utf-8")
    )
    assert salvage["kept"] is True
    assert salvage["wip_preservation"]["status"] == "failed"
    assert "bundle verification failed" in salvage["wip_preservation"]["error"]


def test_invoke_sandboxed_keep_skips_delete(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    steps = []
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (steps.append(args[0]) or (True, "")))
    workspace = tmp_path / "task-7"
    workspace.mkdir()
    te._invoke_agent(FakeRunner(), "do it", workspace, "tid", {})
    assert "download" in steps and "delete" not in steps


def test_merge_replaces_prior_internal_symlink_before_real_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "task.json").write_text("trusted host control\n", encoding="utf-8")

    first = tmp_path / "first-download"
    first.mkdir()
    (first / "foo").symlink_to(".")
    _REAL_MERGE_SANDBOX_DOWNLOAD_TREE(first, workspace)
    assert (workspace / "foo").is_symlink()

    second = tmp_path / "second-download"
    (second / "foo").mkdir(parents=True)
    (second / "foo" / "task.json").write_text(
        "nested sandbox output\n", encoding="utf-8"
    )
    _REAL_MERGE_SANDBOX_DOWNLOAD_TREE(second, workspace)

    assert not (workspace / "foo").is_symlink()
    assert (workspace / "foo").is_dir()
    assert (workspace / "foo" / "task.json").read_text(encoding="utf-8") == (
        "nested sandbox output\n"
    )
    assert (workspace / "task.json").read_text(encoding="utf-8") == (
        "trusted host control\n"
    )


def test_merge_replaces_external_destination_symlink_without_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("outside sentinel\n", encoding="utf-8")
    (workspace / "foo").symlink_to(outside, target_is_directory=True)
    download = tmp_path / "download"
    (download / "foo").mkdir(parents=True)
    (download / "foo" / "result.txt").write_text(
        "sandbox result\n", encoding="utf-8"
    )

    _REAL_MERGE_SANDBOX_DOWNLOAD_TREE(download, workspace)

    assert not (workspace / "foo").is_symlink()
    assert (workspace / "foo" / "result.txt").read_text(encoding="utf-8") == (
        "sandbox result\n"
    )
    assert (outside / "result.txt").read_text(encoding="utf-8") == (
        "outside sentinel\n"
    )


@pytest.mark.parametrize("name", ["task.json", "mac-evidence.json", "mac-sandbox-verification.json"])
def test_merge_rejects_symlinked_host_and_evidence_controls(tmp_path, name):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    download = tmp_path / "download"
    download.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("external data\n", encoding="utf-8")
    (download / name).symlink_to(outside)

    with pytest.raises(ValueError, match="control"):
        _REAL_MERGE_SANDBOX_DOWNLOAD_TREE(download, workspace)


# ---------------------------------------------------------------------------
# --yolo <-> sandbox coupling (never an unguarded YOLO agent)
# ---------------------------------------------------------------------------


def test_unsandboxed_allowed_by_default(monkeypatch):
    agent_argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
    argv = te._unsandboxed_agent_argv(agent_argv)
    assert argv == agent_argv


def test_unsandboxed_explicit_allow(monkeypatch):
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    agent_argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
    assert te._unsandboxed_agent_argv(agent_argv) == agent_argv


def test_unsandboxed_fail_closed_raises(monkeypatch):
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    with pytest.raises(RuntimeError, match="without an OpenShell sandbox"):
        te._unsandboxed_agent_argv(
            ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        )


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


def _read_only_report_task(*, review=False):
    metadata = {
        "deliverable": "report",
        "report_repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
    }
    if review:
        metadata["review_context"] = {"executor_evidence_id": "evidence_x"}
    return {"id": "task_read_only", "metadata": metadata}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _exact_read_only_report_workspace(tmp_path: Path):
    workspace = tmp_path / "task-read-only"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "MAC tests")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    (repo / "Makefile").write_text(
        "smoke:\n\t@echo trusted-smoke\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitignore", "Makefile")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "checkout", "--detach", base_sha)
    _git(repo, "update-ref", "-d", "refs/heads/main")
    refs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    task = _read_only_report_task()
    task["metadata"]["execution_contract"] = {
        "repository_contract": {
            "schema": "mac.repository_contract.v1",
            "canonical_remote_url": "https://example.invalid/repo.git",
            "test": {"command": "make smoke"},
        }
    }
    task["metadata"]["runtime"] = {
        "repository_worktree": str(repo),
        "repository_base_sha": base_sha,
        "repository_base_tree": base_tree,
        "repository_refs_digest": hashlib.sha256(refs.encode("utf-8")).hexdigest(),
        "repository_content_digest": te.read_only_repository_content_digest(repo),
        "repository_access_schema": "mac.report_repository_access.v1",
        "repository_access_mode": "read_only",
    }
    (workspace / "task.json").write_text(
        json.dumps({"task": task}), encoding="utf-8"
    )
    (workspace / "repository-worktree.json").write_text(
        json.dumps(task["metadata"]["runtime"]), encoding="utf-8"
    )
    return workspace, repo, task


def test_read_only_verifier_workspace_is_fresh_exact_base(tmp_path):
    workspace, repo, task = _exact_read_only_report_workspace(tmp_path)
    fake = repo / "build" / "tool"
    fake.parent.mkdir()
    fake.write_text("agent-authored ignored output\n", encoding="utf-8")
    poisoned_tool = workspace / ".mac-toolchain" / "bin" / "make"
    poisoned_tool.parent.mkdir(parents=True)
    poisoned_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    verifier_workspace = tmp_path / "verifier" / "workspace"
    relative, verifier_repo = te._prepare_read_only_verifier_workspace(
        workspace, verifier_workspace, task
    )

    assert relative == Path("repo")
    assert verifier_repo == verifier_workspace / "repo"
    assert not (verifier_repo / "build").exists()
    assert not (verifier_workspace / ".mac-toolchain").exists()
    assert _git(verifier_repo, "status", "--porcelain") == ""
    assert _git(verifier_repo, "remote") == ""
    assert _git(verifier_repo, "for-each-ref") == ""


def test_read_only_verification_uses_second_secret_free_sandbox(
    monkeypatch, tmp_path
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    monkeypatch.setenv(
        "MAC_OPENSHELL_CREATE_ARGS",
        "--from ghcr.io/example/runtime@sha256:%s "
        "--upload /host/secret:/tmp/secret --provider codex "
        "--env=GH_TOKEN=secret --name evil --policy /tmp/evil --no-keep "
        "--driver-config-json '{\"host_path\":\"/host/secret\"}' "
        "--approval-mode auto --cpu 2 --gateway fleet"
        % ("a" * 64),
    )
    calls = []
    monkeypatch.setattr(
        te,
        "_read_only_verifier_extra_create_argv",
        lambda: [
            "--from",
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:%s" % ("b" * 64),
            "--cpu",
            "2",
            "--gateway",
            "fleet",
        ],
    )
    payload = {
        "schema": "mac.sandbox_verification.v1",
        "status": "pass",
        "command": "make smoke",
        "returncode": 0,
        "stdout": "trusted-smoke\n",
        "stderr": "",
        "integrity": {
            "schema": "mac.read_only_report_verification_integrity.v1",
            "immutable_inputs": True,
            "cgroup_quiescent": True,
            "fresh_control_process": True,
            "raw_git_control_first": True,
            "exact_base_revalidated": True,
            "problems": [],
        },
    }

    def step(args, *, timeout):
        calls.append(list(args))
        if args[0] == "create":
            joined = " ".join(args)
            assert "/host/secret" not in joined
            assert "GH_TOKEN=secret" not in joined
            assert "--provider" not in args
            assert "evil" not in args
            assert "--driver-config-json" not in args
            assert "--approval-mode" not in args
            assert args[args.index("--cpu") + 1] == "2"
            assert args[args.index("--gateway") + 1] == "fleet"
            upload = Path(args[args.index("--upload") + 1].split(":", 1)[0])
            assert not (upload / ".mac-toolchain").exists()
            assert not (upload / "repo" / "build").exists()
            verifier_script = (
                upload / ".mac-sandbox-repository-verify.sh"
            ).read_text(encoding="utf-8")
            assert "mac.read_only_report_verifier" in verifier_script
            assert "MAC_READ_ONLY_AUTHORITATIVE_VERIFIER" in verifier_script
            return True, ""
        if args[0] == "download":
            Path(args[3]).write_text(json.dumps(payload), encoding="utf-8")
            return True, ""
        if args[0] == "delete":
            return True, ""
        raise AssertionError(args)

    monkeypatch.setattr(te, "_sandbox_step", step)

    assert te._sandbox_run_read_only_repository_verification(
        "mac-task-agent", workspace, task
    )
    assert [call[0] for call in calls] == ["create", "download", "delete"]
    trusted = workspace / te._TRUSTED_READ_ONLY_VERIFICATION_FILE
    assert json.loads(trusted.read_text(encoding="utf-8"))["stdout"] == (
        "trusted-smoke\n"
    )

    # An agent-authored same-named result is replaced only after harvest.
    (workspace / te._SANDBOX_VERIFICATION_FILE).write_text(
        '{"status":"pass","stdout":"fake"}', encoding="utf-8"
    )
    assert te._promote_trusted_read_only_verification(workspace)
    promoted = json.loads(
        (workspace / te._SANDBOX_VERIFICATION_FILE).read_text(encoding="utf-8")
    )
    assert promoted["stdout"] == "trusted-smoke\n"


def test_read_only_verifier_is_deleted_after_unexpected_create_failure(
    monkeypatch, tmp_path
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    calls = []
    monkeypatch.setattr(
        te,
        "_read_only_verifier_extra_create_argv",
        lambda: [
            "--from",
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:%s" % ("b" * 64),
        ],
    )

    def step(args, *, timeout):
        calls.append(list(args))
        if args[0] == "create":
            raise RuntimeError("gateway disconnected")
        if args[0] == "delete":
            return True, ""
        raise AssertionError(args)

    monkeypatch.setattr(te, "_sandbox_step", step)

    with pytest.raises(RuntimeError, match="gateway disconnected"):
        te._sandbox_run_read_only_repository_verification(
            "mac-task-agent", workspace, task
        )
    assert [call[0] for call in calls] == ["create", "delete"]


def test_trusted_read_only_verification_fails_closed_on_atomic_store_error(
    monkeypatch, tmp_path
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    source = tmp_path / "verification.json"
    source.write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "status": "pass",
                "command": "make smoke",
                "returncode": 0,
            }
        ),
        encoding="utf-8",
    )

    def fail_replace(_source, _destination):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(te.os, "replace", fail_replace)

    assert not te._store_trusted_read_only_verification(source, workspace, task)
    assert not (workspace / te._TRUSTED_READ_ONLY_VERIFICATION_FILE).exists()
    assert not list(workspace.glob(".mac-trusted-read-only-*.host-*"))


def test_trusted_read_only_verification_rejects_unbound_legacy_pass(
    tmp_path,
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    source = tmp_path / "legacy-verification.json"
    source.write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "status": "pass",
                "command": "make smoke",
                "returncode": 0,
            }
        ),
        encoding="utf-8",
    )

    assert not te._store_trusted_read_only_verification(
        source, workspace, task
    )
    assert not (workspace / te._TRUSTED_READ_ONLY_VERIFICATION_FILE).exists()


def test_read_only_postcheck_rejects_core_worktree_clean_tree_spoof(
    monkeypatch, tmp_path
):
    workspace, repo, task = _exact_read_only_report_workspace(tmp_path)
    expected_control = te._read_only_git_control_digest(repo)
    alternate = tmp_path / "attacker-clean-worktree"
    alternate.mkdir()
    (alternate / ".gitignore").write_text("build/\n", encoding="utf-8")
    (alternate / "Makefile").write_text(
        "smoke:\n\t@echo trusted-smoke\n", encoding="utf-8"
    )

    # This is the confirmed bypass: plain `git -C` obeys the poisoned local
    # config and reports the alternate tree as clean while the actual checkout
    # has been modified.
    _git(repo, "config", "core.worktree", str(alternate))
    (repo / "Makefile").write_text("mutated actual checkout\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain") == ""
    trusted_status = te._git_for_read_only_verifier(
        repo, ["status", "--porcelain"]
    )
    assert trusted_status.returncode == 0
    assert "Makefile" in trusted_status.stdout
    assert "-C" not in trusted_status.args

    monkeypatch.setattr(
        te,
        "_sandbox_path_for_workspace_child",
        lambda *_args: str(repo),
    )

    def execute_postcheck(args, *, timeout):
        del timeout
        script = args[-1]
        assert script.index("observed_git_control") < script.index(
            "trusted_git status"
        )
        assert "git -C" not in script
        completed = subprocess.run(
            ["/bin/bash", "-c", script],
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0, (
            completed.stderr or completed.stdout
        ).strip()

    monkeypatch.setattr(te, "_sandbox_step", execute_postcheck)

    violation = te._sandbox_read_only_repository_violation(
        "sandbox",
        workspace.name,
        workspace,
        task,
        expected_control,
    )

    assert "Git control metadata changed" in violation


def _stub_successful_read_only_postchecks(monkeypatch):
    monkeypatch.setattr(
        te, "_sandbox_read_only_repository_violation", lambda *_args: ""
    )
    monkeypatch.setattr(
        te, "_sandbox_run_repository_verification", lambda *_args: True
    )
    monkeypatch.setattr(
        te, "_promote_trusted_read_only_verification", lambda *_args: True
    )


def test_read_only_absolute_symlink_harvest_failure_overrides_success(
    monkeypatch, tmp_path
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    _stub_successful_read_only_postchecks(monkeypatch)
    monkeypatch.setattr(
        te, "_merge_sandbox_download_tree", _REAL_MERGE_SANDBOX_DOWNLOAD_TREE
    )
    monkeypatch.setattr(te, "_sandbox_delete", lambda *_args: True)
    monkeypatch.setattr(
        te,
        "_read_only_report_extra_create_argv",
        lambda: [
            "--from",
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
        ],
    )

    def download_with_ignored_absolute_symlink(args, *, timeout):
        del timeout
        assert args[0] == "download"
        ignored = Path(args[3]) / "repo" / "build"
        ignored.mkdir(parents=True)
        (ignored / "escape").symlink_to("/tmp/outside-agent-output")
        return True, ""

    monkeypatch.setattr(te, "_sandbox_step", download_with_ignored_absolute_symlink)

    result = te._run_sandboxed(
        FakeRunner(), _ARGV, workspace, "tid", {"task": task}
    )

    assert result.returncode == 68
    assert "sandbox result harvest failed" in result.mac_read_only_lifecycle_failure
    assert result.mac_read_only_repository_violation == (
        result.mac_read_only_lifecycle_failure
    )


def test_read_only_openshell_delete_api_failure_overrides_success(
    monkeypatch, tmp_path
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    _stub_successful_read_only_postchecks(monkeypatch)
    monkeypatch.setattr(te, "_sandbox_download", lambda *_args: True)
    monkeypatch.setattr(
        te,
        "_read_only_report_extra_create_argv",
        lambda: [
            "--from",
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
        ],
    )
    deleted = []

    def failed_delete(args, *, timeout):
        del timeout
        assert args[0] == "delete"
        assert args[1].startswith("mac-task-")
        deleted.append(args[1])
        return False, "OpenShell gateway refused deletion"

    monkeypatch.setattr(te, "_sandbox_step", failed_delete)

    result = te._run_sandboxed(
        FakeRunner(), _ARGV, workspace, "tid", {"task": task}
    )

    assert result.returncode == 68
    assert "sandbox deletion failed" in result.mac_read_only_lifecycle_failure
    assert result.mac_read_only_repository_violation == (
        result.mac_read_only_lifecycle_failure
    )
    assert len(deleted) == 1


def test_read_only_report_forbids_keep_before_agent_runs(monkeypatch, tmp_path):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    monkeypatch.setenv("MAC_OPENSHELL_KEEP", "1")
    runner = FakeRunner()

    with pytest.raises(RuntimeError, match="forbid MAC_OPENSHELL_KEEP"):
        te._run_sandboxed(
            runner, _ARGV, workspace, "tid", {"task": task}
        )

    assert runner.calls == []


def test_read_only_verification_requires_current_contract_test_command(
    monkeypatch, tmp_path
):
    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    del task["metadata"]["execution_contract"]["repository_contract"]["test"]
    monkeypatch.setattr(
        te,
        "_sandbox_run_read_only_repository_verification",
        lambda *_args: pytest.fail("missing test.command reached verifier sandbox"),
    )

    assert te._sandbox_run_repository_verification(
        "sandbox", workspace.name, workspace, task
    ) is False


@pytest.mark.parametrize("review", [False, True])
def test_read_only_report_and_reviewer_reject_direct_execution(
    monkeypatch, tmp_path, review
):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    with pytest.raises(RuntimeError, match="per-task OpenShell confinement"):
        te._invoke_agent(
            FakeRunner(),
            "inspect",
            tmp_path / "task",
            "tid",
            {"task": _read_only_report_task(review=review)},
        )


def test_read_only_report_rejects_acp_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_EXECUTOR_BACKEND", "acp")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setattr(
        te,
        "_invoke_acp_agent",
        lambda *_args, **_kwargs: pytest.fail("ACP backend was invoked"),
    )
    with pytest.raises(RuntimeError, match="ACP backend is not supported"):
        te._invoke_agent(
            FakeRunner(),
            "inspect",
            tmp_path / "task",
            "tid",
            {"task": _read_only_report_task()},
        )


def test_read_only_report_rejects_host_break_glass(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setattr(
        te,
        "_validated_host_break_glass_authorization",
        lambda _task: {"id": "auth_x"},
    )
    monkeypatch.setattr(te, "_prepare_host_break_glass_environment", lambda _auth: None)
    with pytest.raises(RuntimeError, match="host break-glass execution are forbidden"):
        te._invoke_agent(
            FakeRunner(),
            "inspect",
            tmp_path / "task",
            "tid",
            {"task": _read_only_report_task()},
        )


# ---------------------------------------------------------------------------
# in-image runtime path + loopback URL rewriting
# ---------------------------------------------------------------------------


def test_agent_bundle_uses_image_owned_python_in_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/opt/mac-venv/bin/python")
    bundle = te._write_agent_command_bundle(
        tmp_path,
        "do it",
        ["codex", "exec", te.PROMPT_SENTINEL],
    )
    try:
        assert bundle.argv(sandbox_workspace="/sandbox/task")[0] == "/opt/mac-venv/bin/python"
    finally:
        bundle.cleanup()


def test_agent_bundle_host_wrapper_ignores_hermes_python(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/does/not/exist")
    bundle = te._write_agent_command_bundle(
        tmp_path,
        "do it",
        ["codex", "exec", te.PROMPT_SENTINEL],
    )
    try:
        assert bundle.argv()[0] == sys.executable
    finally:
        bundle.cleanup()


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
    assert "MAC_WORKER_TOKEN" not in values


def test_repo_worktree_aliases_hermes_clone_path(create_argv):
    # The clone alias is emitted unconditionally as a runtime-guarded bash line;
    # it does not depend on MAC_TASK_REPO_WORKTREE being set at build time, so
    # the shared (byte-identical) create-argv exercises this section.
    inner = _inner(create_argv)
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
