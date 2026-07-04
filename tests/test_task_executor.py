"""Tests for the extracted autonomous task executor (loop-01).

Covers the logic that used to be an untestable bash heredoc: prompt building,
the fail-closed fallback, deterministic outcome classification, the telemetry
path, and the memory feed (recall in / record out). The agent runner and the
hub HTTP seam are injected, so nothing here spawns Hermes or hits a network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mac import task_executor as te


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_task_evidence_type_defaults_and_honors_contract():
    assert te.task_evidence_type({}) == "operator_result"
    assert te.task_evidence_type({"metadata": {"execution_contract": {"evidence_type": "repo_change"}}}) == "repo_change"
    assert te.task_evidence_type({"metadata": {"execution_contract": {"evidence_type": "bogus"}}}) == "operator_result"
    assert te.task_evidence_type({"metadata": {"execution_contract": {"type": "repository"}}}) == "repo_change"
    assert te.task_evidence_type({"metadata": {"origin": {"repository_contract": {"schema": "mac.repository_contract.v1"}}}}) == "repo_change"


def test_build_task_prompt_injects_recalled_lessons():
    task = {"id": "t1", "title": "Do a thing", "project": "demo"}
    base = te.build_task_prompt(task, Path("/tmp/task.json"), lessons=[])
    assert "Lessons from prior runs" not in base
    assert ".mac-executor-policy.txt" in base
    assert "verification.environment_delta" not in base
    with_lessons = te.build_task_prompt(task, Path("/tmp/task.json"), lessons=["push before reporting", "run the contract tests"])
    assert "Lessons from prior runs" in with_lessons
    assert "push before reporting" in with_lessons
    # The task file pointer is always last.
    assert with_lessons.strip().endswith("/tmp/task.json")


def test_build_task_prompt_demands_autonomy():
    # The "should I proceed?" turn-ending failure mode is explicitly forbidden.
    prompt = te.build_task_prompt({"id": "t1", "title": "x", "project": "p"}, Path("/tmp/task.json"))
    assert "AUTONOMOUS" in prompt
    assert "never ask the operator for confirmation" in prompt


def test_agent_bundle_materializes_owner_only_versioned_policy(tmp_path):
    bundle = te._write_agent_command_bundle(
        tmp_path,
        "small prompt",
        te._hermes_argv(te.PROMPT_SENTINEL),
    )
    try:
        assert bundle.policy_file.stat().st_mode & 0o777 == 0o600
        policy = bundle.policy_file.read_text(encoding="utf-8")
        assert policy.startswith("mac.executor_policy.v1")
        assert "verification.environment_delta" in policy
        assert "small prompt" not in policy
    finally:
        bundle.cleanup()


def test_hermes_argv_honors_bounded_task_iteration_budget(monkeypatch):
    monkeypatch.setenv("MAC_TASK_MAX_ITERATIONS", "12")
    argv = te._hermes_argv("prompt")
    assert argv[argv.index("--max-turns") + 1] == "12"

    monkeypatch.setenv("MAC_TASK_MAX_ITERATIONS", "501")
    assert "--max-turns" not in te._hermes_argv("prompt")


def test_build_task_prompt_warns_repo_tasks_away_from_operator_result():
    task = {
        "id": "t1",
        "title": "Repo work",
        "project": "demo",
        "metadata": {"execution_contract": {"type": "repository"}},
    }
    prompt = te.build_task_prompt(task, Path("/tmp/task.json"))
    assert "default to evidence_type=repo_change" in prompt
    assert "use operator_result only when no repository contract exists" in prompt
    assert "Deterministic host code enforces tests, CodeGraph" in prompt


def test_repository_contract_section_no_repository_is_a_failure():
    # No contract AND no checkout -> a missing contract is a genuine failure.
    section = te.repository_contract_section({"metadata": {"origin": {}}})
    assert "report this as a task contract failure" in section


def test_repository_contract_section_onboarding_when_checkout_present():
    # No contract but a repository_url is set -> this is an ONBOARDING task whose
    # job is to author the contract; it must NOT be told to fail.
    task = {"metadata": {"origin": {"type": "direct_task", "repository_url": "https://github.com/acme/widget.git"}}}
    section = te.repository_contract_section(task)
    assert "ONBOARDING" in section
    assert "task contract failure" not in section
    assert ".mac/project.yaml" in section
    assert "$MAC_TASK_REPO_WORKTREE" in section
    assert "codegraph init" in section
    assert "do not push" in section.lower()


def test_repository_contract_section_shows_existing_contract():
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "widget",
        "bootstrap": {"command": "make bootstrap"},
        "test": {"command": "make test"},
    }
    task = {"metadata": {"origin": {"repository_contract": contract}}}
    section = te.repository_contract_section(task)
    assert "make test" in section
    assert "codegraph affected" in section
    assert "task contract failure" not in section
    assert "do not fetch, rebase, commit, push, or open a PR" in section
    assert "deterministic host finalizer owns canonical freshness" in section
    assert "report the pushed ref" not in section


def test_sandbox_create_maps_repo_worktree_env_inside_upload(tmp_path, monkeypatch):
    workspace = tmp_path / "task"
    repo = workspace / "repo-lease"
    repo.mkdir(parents=True)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    monkeypatch.setenv("MAC_TASK_REPO_BRANCH", "mac/test")
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")

    env_file, _toolchain_file = te._write_sandbox_runtime_files(
        workspace, "/sandbox/task"
    )
    argv = te._build_sandbox_create_argv(
        "sb",
        workspace,
        "task",
        [
            "python",
            "-m",
            "mac.agent_command",
            "--command-file",
            "/sandbox/task/command.json",
            "--prompt-file",
            "/sandbox/task/prompt.txt",
        ],
    )

    private_env = env_file.read_text(encoding="utf-8")
    assert "MAC_TASK_REPO_WORKTREE=/sandbox/task/repo-lease" in private_env
    assert "MAC_TASK_REPO_BRANCH=mac/test" in private_env
    assert str(repo) not in " ".join(argv)
    assert "MAC_TASK_REPO_BRANCH=mac/test" not in " ".join(argv)
    assert "mac_sandbox_toolchain_setup" in argv[-1]


def test_sandbox_toolchain_setup_exports_repository_contract_env(tmp_path):
    workspace = tmp_path / "task"
    workspace.mkdir()
    task_file = workspace / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "repository_contract": {
                            "schema": "mac.repository_contract.v1",
                            "toolchain": {"required_commands": ["git"]},
                            "test": {"command": "make test"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    script = "\n".join(
        [
            te._sandbox_toolchain_setup_shell(),
            "mac_sandbox_toolchain_setup",
            r'''"$MAC_SANDBOX_PYTHON" - <<'PY'
import json, os
assert os.environ["MAC_REPO_TEST_COMMAND"] == "make test"
assert os.environ["MAC_REPO_REQUIRED_COMMANDS"] == "git"
expected_prefix = os.pathsep.join([
    os.path.join(os.environ["MAC_TOOLCHAIN_ROOT"], "bin"),
    os.path.join(os.environ["MAC_TOOLCHAIN_ROOT"], "node_modules", ".bin"),
])
assert os.environ["MAC_SANDBOX_PATH_PREFIX"] == expected_prefix
assert os.environ["PATH"] == expected_prefix + os.pathsep + os.environ["MAC_SANDBOX_BASE_PATH"]
delta_path = os.path.join(os.environ["MAC_TOOLCHAIN_ROOT"], "environment-delta.json")
with open(delta_path, encoding="utf-8") as handle:
    delta = json.load(handle)
assert delta["commands"] == ["git"]
PY''',
        ]
    )

    completed = subprocess.run(
        ["bash", "-lc", script],
        cwd=workspace,
        env={
            **os.environ,
            "MAC_TASK_WORKSPACE": str(workspace),
            "MAC_TASK_FILE": str(task_file),
            "MAC_SANDBOX_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_sandbox_repository_verification_retries_and_records_bootstrap(tmp_path):
    workspace = tmp_path / "task"
    worktree = workspace / "repo"
    worktree.mkdir(parents=True)
    task_file = workspace / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "repository_contract": {
                            "schema": "mac.repository_contract.v1",
                            "bootstrap": {
                                "command": "sh bootstrap.sh",
                                "creates": [".venv/bin/coverage"],
                            },
                            "test": {"command": "test -f .venv/bin/coverage"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (worktree / "bootstrap.sh").write_text(
        "\n".join(
            [
                "if [ ! -f first-attempt ]; then",
                "  touch first-attempt",
                "  exit 1",
                "fi",
                "mkdir -p .venv/bin",
                "touch .venv/bin/coverage",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["bash", "-lc", te._sandbox_repository_verification_shell()],
        cwd=workspace,
        env={
            **os.environ,
            "MAC_TASK_WORKSPACE": str(workspace),
            "MAC_TASK_FILE": str(task_file),
            "MAC_TASK_REPO_WORKTREE": str(worktree),
            "MAC_SANDBOX_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    manifest = json.loads((workspace / "mac-sandbox-verification.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "pass"
    assert manifest["bootstrap"]["status"] == "pass"
    assert manifest["bootstrap"]["missing_before"] == [".venv/bin/coverage"]
    assert (worktree / "first-attempt").exists()
    assert (worktree / ".venv/bin/coverage").exists()


def test_sandbox_repository_verification_does_not_rerun_bootstrap_without_creates(tmp_path):
    workspace = tmp_path / "task"
    worktree = workspace / "repo"
    worktree.mkdir(parents=True)
    task_file = workspace / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "repository_contract": {
                            "schema": "mac.repository_contract.v1",
                            "bootstrap": {"command": "sh bootstrap.sh"},
                            "test": {"command": "test \"$(cat bootstrap-count)\" = 1"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (worktree / "bootstrap.sh").write_text(
        "\n".join(
            [
                "count=0",
                "[ -f bootstrap-count ] && count=$(cat bootstrap-count)",
                "count=$((count + 1))",
                "printf '%s' \"$count\" > bootstrap-count",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["bash", "-lc", te._sandbox_repository_verification_shell()],
        cwd=workspace,
        env={
            **os.environ,
            "MAC_TASK_WORKSPACE": str(workspace),
            "MAC_TASK_FILE": str(task_file),
            "MAC_TASK_REPO_WORKTREE": str(worktree),
            "MAC_SANDBOX_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    manifest = json.loads((workspace / "mac-sandbox-verification.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "pass"
    assert manifest["bootstrap"]["status"] == "pass"
    assert manifest["bootstrap"]["creates"] == []
    assert (worktree / "bootstrap-count").read_text(encoding="utf-8") == "1"


def test_sandbox_toolchain_setup_provisions_gh_when_missing(tmp_path):
    workspace = tmp_path / "task"
    workspace.mkdir()
    task_file = workspace / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "repository_contract": {
                            "schema": "mac.repository_contract.v1",
                            "toolchain": {"required_commands": ["gh"]},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name in ("bash", "chmod", "id", "ln", "mkdir"):
        target = shutil.which(name)
        assert target, name
        (fake_bin / name).symlink_to(target)
    (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'x86_64\\n'\n", encoding="utf-8")
    (fake_bin / "curl").write_text(
        """#!/bin/sh
out=""
want_out=0
for arg in "$@"; do
  if [ "$want_out" = 1 ]; then out="$arg"; want_out=0; continue; fi
  if [ "$arg" = "-o" ]; then want_out=1; fi
done
if [ -n "$out" ]; then
  case "$out" in
    *gh-release.json) printf '{"assets":[{"name":"gh_test_linux_amd64.tar.gz","browser_download_url":"https://example.invalid/gh.tar.gz"}]}' > "$out" ;;
    *) printf 'fake gh tarball' > "$out" ;;
  esac
else
  printf '{"assets":[{"name":"gh_test_linux_amd64.tar.gz","browser_download_url":"https://example.invalid/gh.tar.gz"}]}'
fi
""",
        encoding="utf-8",
    )
    (fake_bin / "tar").write_text(
        """#!/bin/sh
dest=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-C" ]; then dest="$2"; shift 2; continue; fi
  shift
done
mkdir -p "$dest/bin"
printf '#!/bin/sh\\nprintf "gh fake\\\\n"\\n' > "$dest/bin/gh"
chmod +x "$dest/bin/gh"
""",
        encoding="utf-8",
    )
    for name in ("curl", "tar", "uname"):
        (fake_bin / name).chmod(0o755)

    script = "\n".join(
        [
            te._sandbox_toolchain_setup_shell(),
            "mac_sandbox_toolchain_setup",
            r'''"$MAC_SANDBOX_PYTHON" - <<'PY'
import json, os, shutil, subprocess
assert shutil.which("gh"), os.environ.get("PATH")
assert subprocess.run(["gh"], capture_output=True, text=True).returncode == 0
delta_path = os.path.join(os.environ["MAC_TOOLCHAIN_ROOT"], "environment-delta.json")
with open(delta_path, encoding="utf-8") as handle:
    delta = json.load(handle)
assert delta["commands"] == ["gh"]
assert delta["missing_after"] == []
PY''',
        ]
    )

    completed = subprocess.run(
        ["bash", "-lc", script],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": str(fake_bin),
            "MAC_SANDBOX_BASE_PATH": str(fake_bin),
            "MAC_TASK_WORKSPACE": str(workspace),
            "MAC_TASK_FILE": str(task_file),
            "MAC_SANDBOX_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_sandboxed_repo_task_runs_verification_before_download(tmp_path, monkeypatch):
    workspace = tmp_path / "task"
    workspace.mkdir()
    monkeypatch.setenv("MAC_OPENSHELL_PROGRESS_INTERVAL", "0")
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setattr(te, "_ensure_landlock_or_fail", lambda: None)
    monkeypatch.setattr(te, "_sandbox_name", lambda: "sb")
    monkeypatch.setattr(te, "_merge_sandbox_download_tree", lambda download_root, workspace: None)
    repo = workspace / "repo"
    repo.mkdir()
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    steps = []
    uploaded_scripts = []

    def fake_step(args, *, timeout):
        steps.append(args)
        if args[0] == "upload":
            uploaded_scripts.append(Path(args[2]).read_text(encoding="utf-8"))
        return True, ""

    def fake_runner(argv, cwd, audit_id, opts):
        steps.append(["runner", *argv[:3]])
        return _FakeResult(0, stdout="ok\n")

    monkeypatch.setattr(te, "_sandbox_step", fake_step)
    task = {
        "id": "t1",
        "metadata": {
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "test": {"command": "make test"},
                    "toolchain": {"required_commands": ["git", "pnpm"]},
                },
            }
        },
    }

    result = te._run_sandboxed(
        fake_runner,
        [
            "python",
            "-m",
            "mac.agent_command",
            "--command-file",
            "/sandbox/task/command.json",
            "--prompt-file",
            "/sandbox/task/prompt.txt",
        ],
        workspace,
        "t1",
        {"task": task},
    )

    assert result.returncode == 0
    assert steps[0][0] == "runner"
    assert steps[1][:2] == ["upload", "sb"]
    verify_script = Path(steps[1][2])
    assert verify_script.name == ".mac-sandbox-repository-verify.sh"
    assert not verify_script.exists(), "executor-only verification script should be cleaned"
    assert steps[1][3] == "/sandbox/task/.mac-sandbox-repository-verify.sh"
    assert steps[2][:2] == ["exec", "--name"]
    assert steps[2][-2:] == ["bash", "/sandbox/task/.mac-sandbox-repository-verify.sh"]
    assert all("\n" not in str(arg) and "\r" not in str(arg) for arg in steps[2])
    assert "export MAC_TASK_FILE=/sandbox/task/task.json" in uploaded_scripts[0]
    assert "export MAC_TASK_WORKSPACE=/sandbox/task" in uploaded_scripts[0]
    assert "export MAC_TASK_REPO_WORKTREE=/sandbox/task/repo" in uploaded_scripts[0]
    assert "mac-sandbox-verification.json" in te._sandbox_repository_verification_shell()
    assert steps[3][0] == "download"
    assert steps[4][0] == "delete"


def test_sandbox_repository_verifier_kills_process_tree_on_timeout(tmp_path):
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "execution_contract": {
                            "repository_contract": {
                                "test": {"command": "sleep 30 & wait"}
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    script = te._sandbox_repository_verification_shell(
        {
            "MAC_SANDBOX_PYTHON": sys.executable,
            "MAC_TASK_FILE": str(task_file),
            "MAC_TASK_WORKSPACE": str(tmp_path),
            "MAC_TASK_REPO_WORKTREE": str(tmp_path),
            "MAC_WORKER_REPOSITORY_TEST_TIMEOUT": "0.1",
        }
    )

    started = time.monotonic()
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 124
    assert elapsed < 3
    payload = json.loads(
        (tmp_path / "mac-sandbox-verification.json").read_text(encoding="utf-8")
    )
    assert payload["returncode"] == 124
    assert payload["status"] == "fail"
    assert "timed out" in payload["error"]


def test_sandbox_repository_verifier_does_not_wait_for_inherited_output_pipe(tmp_path):
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "execution_contract": {
                            "repository_contract": {
                                "test": {"command": "sleep 30 &"}
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    script = te._sandbox_repository_verification_shell(
        {
            "MAC_SANDBOX_PYTHON": sys.executable,
            "MAC_TASK_FILE": str(task_file),
            "MAC_TASK_WORKSPACE": str(tmp_path),
            "MAC_TASK_REPO_WORKTREE": str(tmp_path),
            "MAC_WORKER_REPOSITORY_TEST_TIMEOUT": "5",
        }
    )

    started = time.monotonic()
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert elapsed < 3
    payload = json.loads(
        (tmp_path / "mac-sandbox-verification.json").read_text(encoding="utf-8")
    )
    assert payload["returncode"] == 0
    assert payload["status"] == "pass"
    assert "error" not in payload


def test_sandbox_download_merge_preserves_host_git_metadata_and_skips_runtime_dirs(tmp_path):
    workspace = tmp_path / "task"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    repository_context = {
        "schema": "mac.repository_task_worktree.v1",
        "repository_worktree": str(repo),
    }
    (workspace / "repository-worktree.json").write_text(json.dumps(repository_context), encoding="utf-8")
    (repo / ".git").write_text("gitdir: /host/git/worktrees/repo\n", encoding="utf-8")
    (repo / ".git.bak-old" / "objects").mkdir(parents=True)
    (repo / ".git.bak-old" / "objects" / "stale").write_text("stale backup\n", encoding="utf-8")
    (repo / "old.py").write_text("remove me\n", encoding="utf-8")
    (repo / "same.py").write_text("old\n", encoding="utf-8")

    download = tmp_path / "download"
    download.mkdir()
    (download / "repository-worktree.json").write_text(json.dumps(repository_context), encoding="utf-8")
    sandbox_repo = download / "repo"
    (sandbox_repo / ".git").mkdir(parents=True)
    (sandbox_repo / ".git" / "description").write_text("sandbox git dir\n", encoding="utf-8")
    (sandbox_repo / ".git.bak").write_text("sandbox transfer backup\n", encoding="utf-8")
    (sandbox_repo / ".git.bak123" / "objects").mkdir(parents=True)
    (sandbox_repo / ".git.bak123" / "objects" / "description").write_text(
        "sandbox transfer backup dir\n",
        encoding="utf-8",
    )
    (sandbox_repo / ".venv" / "bin").mkdir(parents=True)
    (sandbox_repo / ".venv" / "bin" / "python").write_text("container venv\n", encoding="utf-8")
    (sandbox_repo / "fixtures" / "node_modules").mkdir(parents=True)
    (sandbox_repo / "fixtures" / "node_modules" / "package.json").write_text("{}\n", encoding="utf-8")
    (sandbox_repo / "same.py").write_text("new\n", encoding="utf-8")
    (sandbox_repo / "new.py").write_text("added\n", encoding="utf-8")
    (download / "mac-evidence.json").write_text('{"status":"complete"}\n', encoding="utf-8")

    te._merge_sandbox_download_tree(download, workspace)

    assert (repo / ".git").is_file()
    assert (repo / ".git").read_text(encoding="utf-8").startswith("gitdir:")
    assert not (repo / ".git" / "description").exists()
    assert not (repo / ".git.bak").exists()
    assert not (repo / ".git.bak123").exists()
    assert not (repo / ".git.bak-old").exists()
    assert not (repo / ".venv").exists()
    assert not (repo / "old.py").exists()
    assert (repo / "same.py").read_text(encoding="utf-8") == "new\n"
    assert (repo / "new.py").read_text(encoding="utf-8") == "added\n"
    assert (repo / "fixtures" / "node_modules" / "package.json").exists()
    assert (workspace / "mac-evidence.json").exists()


def test_sandbox_download_merge_failure_is_best_effort(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "task"
    workspace.mkdir()

    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))

    def fail_merge(download_root, workspace):
        raise RuntimeError("merge boom")

    monkeypatch.setattr(te, "_merge_sandbox_download_tree", fail_merge)

    assert te._sandbox_download("sb", "task", workspace) is False
    assert "sandbox download merge failed: merge boom" in capsys.readouterr().err


def test_build_telemetry_record_shape():
    rec = te.build_telemetry_record("started", task_id="t1", level="info", detail={"kind": "task"})
    assert rec["name"] == "executor.started"
    assert rec["layer"] == "executor"
    assert rec["subject_type"] == "task" and rec["subject_id"] == "t1"
    assert rec["detail"]["schema"] == "mac.executor_telemetry.v1"
    assert rec["detail"]["kind"] == "task"


def test_build_learning_record_shape():
    task = {"id": "t1", "title": "Ship X", "project": "demo", "metadata": {"origin": {"repository_name": "demo-repo"}}}
    outcome = {"evidence_type": "repo_change", "outcome": "success", "signals": {"pushed": True}, "error_signature": ""}
    rec = te.build_learning_record(task, outcome)
    assert rec["subject_type"] == "project" and rec["subject_id"] == "demo"
    assert rec["record_type"] == "deployment_learning:demo"
    assert rec["created_by"] == "mac-hermes-task-executor"
    content = json.loads(rec["content"])
    assert content["schema"] == "mac.deployment_learning.v1"
    assert content["repository"] == "demo-repo"
    assert content["outcome"] == "success"


# ---------------------------------------------------------------------------
# Fail-closed fallback (the loop-01 invariant must survive the extraction)
# ---------------------------------------------------------------------------


def test_fallback_writes_unverified_operator_result_no_synthetic_check(tmp_path):
    task = {"id": "t1", "title": "x", "project": "demo"}
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="Mapped the milestones."), None)
    manifest = json.loads((tmp_path / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["summary"] == "Mapped the milestones."
    assert "checks" not in manifest  # no fabricated passing check


def test_fallback_does_not_write_operator_result_for_repo_coupled_task(tmp_path):
    task = {
        "id": "t1",
        "title": "repo task",
        "project": "demo",
        "metadata": {"execution_contract": {"type": "repository"}},
    }
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="Changed repo."), None)
    assert not (tmp_path / "mac-evidence.json").exists()


def test_fallback_skips_on_failure_review_and_existing(tmp_path):
    task = {"id": "t1"}
    # non-zero exit → no fabrication
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(1, stdout="boom"), None)
    assert not (tmp_path / "mac-evidence.json").exists()
    # review context → finalizer owns the manifest
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="x"), {"review_id": "r"})
    assert not (tmp_path / "mac-evidence.json").exists()
    # existing manifest (finalizer already wrote) → don't overwrite
    (tmp_path / "mac-evidence.json").write_text('{"kept": true}')
    te.write_fallback_evidence_manifest(tmp_path, task, _FakeResult(0, stdout="x"), None)
    assert json.loads((tmp_path / "mac-evidence.json").read_text()) == {"kept": True}


# ---------------------------------------------------------------------------
# Outcome classification (drives the memory feed)
# ---------------------------------------------------------------------------


def test_classify_outcome_success_and_failure(tmp_path):
    task = {"id": "t1", "project": "demo"}
    # success: pushed repo_change with passing tests
    (tmp_path / "mac-evidence.json").write_text(json.dumps({
        "evidence_type": "repo_change",
        "repo": {"pushed": True, "files_changed": ["a.py"]},
        "tests": {"returncode": 0, "status": "pass"},
        "checks": [{"name": "git_finalizer", "returncode": 0, "status": "pass"}],
    }))
    ok = te.classify_outcome(tmp_path, task, 0)
    assert ok["outcome"] == "success"
    assert ok["signals"]["pushed"] is True and ok["signals"]["tests"] == "pass"

    # failure: tests failed
    (tmp_path / "mac-evidence.json").write_text(json.dumps({
        "evidence_type": "repo_change",
        "repo": {"pushed": True, "files_changed": ["a.py"]},
        "tests": {"returncode": 1, "status": "fail"},
        "checks": [{"name": "git_finalizer", "returncode": 1, "status": "fail"}],
        "summary": "tests broke",
    }))
    bad = te.classify_outcome(tmp_path, task, 0)
    assert bad["outcome"] == "failure"
    assert bad["error_signature"]


def test_classify_outcome_failure_when_no_evidence(tmp_path):
    out = te.classify_outcome(tmp_path, {"id": "t1"}, 0)
    assert out["outcome"] == "failure"


def test_agent_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("MAC_EXECUTOR_AGENT_TIMEOUT", raising=False)
    assert te._agent_timeout() == 900.0
    monkeypatch.setenv("MAC_EXECUTOR_AGENT_TIMEOUT", "120")
    assert te._agent_timeout() == 120.0
    monkeypatch.setenv("MAC_EXECUTOR_AGENT_TIMEOUT", "0")  # disable the bound
    assert te._agent_timeout() is None


def test_manifest_is_complete(tmp_path):
    assert te._manifest_is_complete(tmp_path) is False
    (tmp_path / "mac-evidence.json").write_text('{"status":"complete","evidence_type":"operator_result"}')
    assert te._manifest_is_complete(tmp_path) is True
    (tmp_path / "mac-evidence.json").write_text(
        '{"status":"complete","evidence_type":"review_verdict",'
        '"semantic_verdict":"invalid"}'
    )
    assert te._manifest_is_complete(tmp_path) is False
    (tmp_path / "mac-evidence.json").write_text(
        '{"status":"complete","evidence_type":"review_verdict",'
        '"semantic_verdict":"approved","review_experiment":{"blind":true,'
        '"protocol":{"protocol_compliant":false}}}'
    )
    assert te._manifest_is_complete(tmp_path) is False
    (tmp_path / "mac-evidence.json").write_text(
        '{"status":"complete","evidence_type":"review_verdict",'
        '"semantic_verdict":"rejected"}'
    )
    assert te._manifest_is_complete(tmp_path) is True
    (tmp_path / "mac-evidence.json").write_text('{"status":"running"}')  # partial
    assert te._manifest_is_complete(tmp_path) is False


def test_main_salvages_evidence_when_agent_run_times_out(tmp_path, monkeypatch):
    # loop-01 resilience: the agent wrote a valid deliverable, then a trailing
    # turn hung and the run was bounded (rc=124). The deliverable must NOT be
    # discarded — main() salvages it and reports success.
    task = {"id": "t1", "title": "Plan X", "project": "demo", "metadata": {"publication_target": "test://x"}}
    task_file = tmp_path / "task.json"; task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file)); monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
    posts = []
    monkeypatch.setattr(te, "_hub_post", lambda path, payload, **kw: posts.append((path, payload)) or True)
    monkeypatch.setattr(te, "_hub_get", lambda path, **kw: [])

    def timed_out_runner(argv, cwd, task_id, metadata):
        # agent produced a real deliverable before the trailing turn hung
        (ws / "mac-evidence.json").write_text(json.dumps({
            "schema": "mac.worker_evidence.v1", "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Produced a substantive plan with several distinct points.",
        }))
        return _FakeResult(124, stdout="...", stderr="agent run timed out")

    rc = te.main(runner=timed_out_runner)
    assert rc == 0, "valid deliverable should be salvaged despite the timeout"
    names = {p[1]["name"] for p in posts if p[0] == "/observability/logs"}
    assert "executor.evidence_salvaged" in names
    # outcome recorded as success (memory feed)
    mem = [p for p in posts if p[0] == "/memory"]
    assert mem and json.loads(mem[0][1]["content"])["outcome"] == "success"


def test_main_fails_when_timeout_and_no_evidence(tmp_path, monkeypatch):
    task = {"id": "t1", "title": "Plan X", "project": "demo", "metadata": {"publication_target": "test://x"}}
    task_file = tmp_path / "task.json"; task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file)); monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
    monkeypatch.setattr(te, "_hub_post", lambda *a, **k: True)
    monkeypatch.setattr(te, "_hub_get", lambda *a, **k: [])
    # timeout with NO deliverable written → honest failure, not salvaged
    rc = te.main(runner=lambda *a: _FakeResult(124, stderr="agent run timed out"))
    assert rc == 124


# ---------------------------------------------------------------------------
# Hub seam: telemetry + memory are best-effort and gated
# ---------------------------------------------------------------------------


def test_hub_post_noop_without_env(monkeypatch):
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    assert te.emit_telemetry("started", task_id="t1") is False
    assert te.record_deployment_learning({"id": "t1", "project": "demo"}, {"outcome": "success"}) is False


def test_recall_deployment_lessons_via_injected_get(monkeypatch):
    captured = {}

    def fake_get(path, *, timeout=5.0):
        captured["path"] = path
        return [{"content": "always push before reporting"}, {"summary": "run contract tests"}, {"nope": 1}]

    monkeypatch.setattr(te, "_hub_get", fake_get)
    lessons = te.recall_deployment_lessons({"title": "Ship X", "project": "demo"})
    assert lessons == ["always push before reporting", "run contract tests"]
    assert "/v1/memory/recall?" in captured["path"]
    assert "project=demo" in captured["path"]


def test_recall_falls_back_to_direct_memory_records(monkeypatch):
    # Vector recall empty (no embeddings yet) → fall back to the project's
    # deployment_learning records so the very next task still gets hindsight.
    learning = json.dumps({
        "schema": "mac.deployment_learning.v1",
        "task_title": "Router deployment failure",
        "evidence_type": "repo_change",
        "outcome": "failure",
        "error_signature": "check:git_finalizer rc=1",
    })

    def fake_get(path, *, timeout=5.0):
        if path.startswith("/v1/memory/recall"):
            return []  # vector tier not populated yet
        return [
            {
                "record_type": "deployment_learning:demo",
                "content": json.dumps(
                    {
                        "schema": "mac.deployment_learning.v1",
                        "task_title": "Unrelated spreadsheet export",
                        "outcome": "success",
                    }
                ),
                "created_at": "2026-06-01T00:00:00Z",
            },
            {"record_type": "deployment_learning:demo", "content": learning, "created_at": "2026-05-31T00:00:00Z"},
            {"record_type": "other", "content": "ignored", "created_at": "2026-05-31T01:00:00Z"},
        ]

    monkeypatch.setattr(te, "_hub_get", fake_get)
    lessons = te.recall_deployment_lessons(
        {"title": "Fix router deployment", "project": "demo"}
    )
    assert lessons == [
        "[failure] Router deployment failure (repo_change) — failed: check:git_finalizer rc=1"
    ]


def test_recall_includes_structured_common_fleet_learning(monkeypatch):
    success = {
        "schema": "mac.fleet_learning.v1",
        "kind": "repository_access",
        "project": "demo",
        "repository_host": "github.com",
        "transport": "https",
        "operation": "review_clone",
        "agent_id": "agent_success",
        "credential_source": "env:GH_TOKEN",
        "outcome": "success",
        "failure_class": "",
        "error_signature": "",
        "recommendation": "Prefer this known-successful reviewer pattern.",
    }

    def fake_get(path, *, timeout=5.0):
        if path.startswith("/memory?") and "fleet_learning" in path:
            return [
                {
                    "record_type": "fleet_learning:repository_access",
                    "content": json.dumps(success),
                    "created_at": "2026-06-30T00:00:00Z",
                }
            ]
        if path.startswith("/v1/memory/recall"):
            return []
        return []

    monkeypatch.setattr(te, "_hub_get", fake_get)
    lessons = te.recall_deployment_lessons(
        {
            "title": "Review private repository",
            "project": "demo",
            "metadata": {
                "origin": {
                    "repository_url": "https://github.com/acme/private.git"
                }
            },
        }
    )

    assert len(lessons) == 1
    assert "[fleet success] review_clone on github.com" in lessons[0]
    assert "env:GH_TOKEN" in lessons[0]
    assert "Prefer this known-successful reviewer pattern" in lessons[0]


# ---------------------------------------------------------------------------
# git finalizer against a real temp repo
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _install_fake_codegraph(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "codegraph"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'case "${1:-}" in',
                "  init)",
                '    mkdir -p "$2/.codegraph"',
                '    echo "indexed $2"',
                "    ;;",
                "  sync)",
                '    mkdir -p "$2/.codegraph"',
                '    echo "synced $2"',
                "    ;;",
                "  affected)",
                "    cat >/dev/null",
                '    echo \'{"affected":[]}\'',
                "    ;;",
                "  unlock)",
                "    ;;",
                "  *)",
                '    echo "unexpected codegraph command: $*" >&2',
                "    exit 2",
                "    ;;",
                "esac",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("MAC_CODEGRAPH_BIN", str(script))
    # Finalizer tests model a worker-prepared repository context. The worker
    # records the canonical base before the agent starts; make that contract
    # explicit instead of relying on the old finalizer's implicit HEAD~1/main.
    work = tmp_path / "work"
    if work.is_dir():
        for base_ref in ("main", "develop"):
            base = _git(work, "rev-parse", "--verify", base_ref)
            if base.returncode == 0 and base.stdout.strip():
                monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", base.stdout.strip())
                monkeypatch.setenv("MAC_TASK_REPO_DEFAULT_BRANCH", base_ref)
                monkeypatch.setenv("MAC_TASK_REPO_LEASE_ID", "lease-test")
                break
    return script


def test_git_finalizer_emits_repo_change_from_real_state(tmp_path, monkeypatch):
    # origin (bare) + worktree clone
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    # a feature branch with an uncommitted edit the finalizer should commit+push
    _git(work, "checkout", "-b", "task/x")
    (work / "feature.py").write_text("print('x')\n")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t1",
        "metadata": {
            "publication_target": "git://main",
            "origin": {"repository_contract": {
                "canonical_remote_url": origin.as_uri(),
                "test": {"command": "true"},
            }},
        },
    }
    te.run_deterministic_git_finalizer(ws, task)
    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "repo_change"
    assert manifest["repo"]["pushed"] is True
    assert "feature.py" in manifest["repo"]["files_changed"]
    assert manifest["codegraph"]["status"] == "pass"
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "pass"


def test_git_finalizer_pushes_to_canonical_remote_when_origin_differs(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    canonical = tmp_path / "canonical.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "--bare", str(canonical))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    # Push main to canonical so the freshness fetch can resolve it.
    _git(work, "push", canonical.as_uri(), "main")
    _git(work, "checkout", "-b", "task/canonical")
    (work / "feature.py").write_text("print('canonical')\n", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t1",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": canonical.as_uri(),
                    "test": {"command": "true"},
                },
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is True
    assert manifest["repo"]["push_remote"] == canonical.as_uri()
    assert manifest["push"]["remote"] == canonical.as_uri()
    assert (
        _git(tmp_path, "ls-remote", str(canonical), "refs/heads/task/canonical")
        .stdout.strip()
    )
    assert (
        _git(tmp_path, "ls-remote", str(origin), "refs/heads/task/canonical")
        .stdout.strip()
        == ""
    )


def test_git_finalizer_runs_contract_bootstrap_before_tests(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    scripts = work / "scripts"
    scripts.mkdir()
    (scripts / "bootstrap-project.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "venv_python = Path('.venv/bin/python')",
                "venv_python.parent.mkdir(parents=True, exist_ok=True)",
                "venv_python.write_text('#!/bin/sh\\nexit 0\\n', encoding='utf-8')",
                "venv_python.chmod(0o755)",
            ]
        ),
        encoding="utf-8",
    )
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "-b", "task/bootstrap")
    (work / "feature.py").write_text("print('x')\n", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t1",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": origin.as_uri(),
                    "bootstrap": {
                        "command": "python3 scripts/bootstrap-project.py",
                        "creates": [".venv/bin/python"],
                    },
                    "test": {"command": ".venv/bin/python -c 'print(123)'"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert (work / ".venv/bin/python").exists()
    assert manifest["bootstrap"]["status"] == "pass"
    # mac-wjy3: verification.tests must be a LIST of result objects so the strict
    # evidence validator accepts it (a bare dict reads as tests:null/missing).
    assert isinstance(manifest["tests"], list)
    assert manifest["tests"][0]["returncode"] == 0
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "pass"


def test_repository_bootstrap_runs_when_creates_omitted(tmp_path):
    (tmp_path / "bootstrap.sh").write_text("touch bootstrapped\n", encoding="utf-8")
    task = {
        "metadata": {
            "origin": {
                "repository_contract": {
                    "bootstrap": {"command": "sh bootstrap.sh"},
                }
            }
        }
    }

    result = te._run_repository_bootstrap_if_needed(tmp_path, task)

    assert result["status"] == "pass"
    assert result["creates"] == []
    assert (tmp_path / "bootstrapped").exists()


def test_git_finalizer_fails_when_bootstrap_fails_even_if_tests_pass(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "-b", "task/bootstrap-fail")
    (work / "feature.py").write_text("print('x')\n", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t1",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": origin.as_uri(),
                    "bootstrap": {"command": "false"},
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is False
    # base_sha records the canonical tip so the reviewer can compute a
    # non-empty base..head diff (base != head here).
    assert len(manifest["repo"]["base_sha"]) == 40
    assert manifest["repo"]["base_sha"] != manifest["repo"]["head_sha"]
    assert manifest["bootstrap"]["status"] == "fail"
    assert manifest["tests"][0]["status"] == "pass"
    assert manifest["push"]["status"] == "skipped"
    assert manifest["push"]["reason"] == "bootstrap/tests failed"
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "fail"


# ---------------------------------------------------------------------------
# Canonical freshness check tests
# ---------------------------------------------------------------------------


def _setup_two_repo_worktree(tmp_path):
    """Helper: return (origin_bare, canonical_bare, work_clone, main_sha).

    origin and canonical both start with main at the same SHA.
    work is checked out on a feature branch one commit ahead of main.
    """
    origin = tmp_path / "origin.git"
    canonical = tmp_path / "canonical.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "--bare", str(canonical))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    _git(work, "push", canonical.as_uri(), "main")
    # Ensure the bare repos advertise main as the default branch so that
    # subsequent clones of them check out main (not the bare-repo default master).
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(canonical, "symbolic-ref", "HEAD", "refs/heads/main")
    main_sha = _git(work, "rev-parse", "main").stdout.strip()
    _git(work, "checkout", "-b", "task/feature")
    (work / "feature.py").write_text("print('feature')\n", encoding="utf-8")
    return origin, canonical, work, main_sha


def test_git_finalizer_blocks_when_canonical_fetch_fails(tmp_path, monkeypatch):
    """Fetch failure is fail-closed: push must be skipped with freshness_error."""
    origin, canonical, work, _main_sha = _setup_two_repo_worktree(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    # Point canonical_remote_url at a non-existent path to force a fetch failure.
    task = {
        "id": "t-fetch-fail",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "file:///nonexistent-repo-does-not-exist",
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is False
    assert manifest["push"]["status"] == "skipped"
    assert manifest["push"]["reason"] == "canonical freshness check failed"
    assert "freshness_error" in manifest
    assert "fetch" in manifest["freshness_error"].lower() or "nonexistent" in manifest["freshness_error"].lower() or manifest["freshness_error"]
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "fail"


def test_git_finalizer_auto_rebases_clean_canonical_advance(tmp_path, monkeypatch):
    """Canonical advanced cleanly under the task -> the finalizer rebases BEFORE
    the contract test and publishes (previously this blocked with "canonical tip
    is not an ancestor", killing every task slower than its fleet peers)."""
    origin, canonical, work, main_sha = _setup_two_repo_worktree(tmp_path)
    # Advance canonical with a NON-conflicting commit AFTER the task branch was created.
    advance_dir = tmp_path / "advance"
    _git(tmp_path, "clone", canonical.as_uri(), str(advance_dir))
    _git(advance_dir, "config", "user.email", "t@t")
    _git(advance_dir, "config", "user.name", "t")
    (advance_dir / "canonical_advance.py").write_text("# canonical advance\n", encoding="utf-8")
    _git(advance_dir, "add", "-A")
    _git(advance_dir, "commit", "-m", "advance canonical")
    _git(advance_dir, "push", "origin", "main")
    new_canonical_sha = _git(advance_dir, "rev-parse", "HEAD").stdout.strip()

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-stale",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": canonical.as_uri(),
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["canonical_sync"]["status"] == "rebased"
    assert manifest["repo"]["canonical_sync"]["canonical_tip"] == new_canonical_sha
    assert manifest["repo"]["pushed"] is True, "cleanly rebased task HEAD must publish"
    assert manifest["repo"]["base_sha"] == new_canonical_sha
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "pass"


def test_git_finalizer_blocks_conflicting_canonical_advance(tmp_path, monkeypatch):
    """Canonical advanced with a CONFLICTING edit -> the sync aborts its rebase
    and the freshness gate still fails closed (no auto-merge of conflicts)."""
    origin, canonical, work, main_sha = _setup_two_repo_worktree(tmp_path)
    advance_dir = tmp_path / "advance"
    _git(tmp_path, "clone", canonical.as_uri(), str(advance_dir))
    _git(advance_dir, "config", "user.email", "t@t")
    _git(advance_dir, "config", "user.name", "t")
    # The task worktree also writes feature.py -> guaranteed rebase conflict.
    (advance_dir / "feature.py").write_text("print('peer conflicting')\n", encoding="utf-8")
    _git(advance_dir, "add", "-A")
    _git(advance_dir, "commit", "-m", "conflicting canonical advance")
    _git(advance_dir, "push", "origin", "main")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-conflict",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": canonical.as_uri(),
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["canonical_sync"]["status"] == "conflict"
    assert manifest["repo"]["pushed"] is False, "conflicting task HEAD must not be pushed"
    assert manifest["push"]["status"] == "skipped"
    assert manifest["push"]["reason"] == "canonical freshness check failed"
    assert "freshness_error" in manifest
    assert "ancestor" in manifest["freshness_error"].lower() or "rebase" in manifest["freshness_error"].lower()
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "fail"


def test_git_finalizer_passes_rebased_task_head(tmp_path, monkeypatch):
    """A task branch rebased onto the new canonical tip must be accepted."""
    origin, canonical, work, _main_sha = _setup_two_repo_worktree(tmp_path)
    # Advance canonical.
    advance_dir = tmp_path / "advance"
    _git(tmp_path, "clone", canonical.as_uri(), str(advance_dir))
    _git(advance_dir, "config", "user.email", "t@t")
    _git(advance_dir, "config", "user.name", "t")
    (advance_dir / "canonical_advance.py").write_text("# canonical advance\n", encoding="utf-8")
    _git(advance_dir, "add", "-A")
    _git(advance_dir, "commit", "-m", "advance canonical")
    _git(advance_dir, "push", "origin", "main")
    new_canonical_sha = _git(advance_dir, "rev-parse", "HEAD").stdout.strip()

    # Rebase the task branch onto the new canonical tip.
    # Fetch the new canonical commit into work and rebase.
    _git(work, "fetch", canonical.as_uri(), "main")
    _git(work, "rebase", "FETCH_HEAD")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-rebased",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": canonical.as_uri(),
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is True, "rebased task HEAD must be accepted"
    assert manifest["repo"]["base_sha"] == new_canonical_sha
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "pass"


def test_git_finalizer_blocks_invalid_canonical_remote_url(tmp_path, monkeypatch):
    """An invalid canonical_remote_url fails validation; push is blocked."""
    origin, _canonical, work, _main_sha = _setup_two_repo_worktree(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-invalid-remote",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "not-a-valid-url",
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is False
    # The manifest records either a fetch failure or validation failure.
    assert manifest["push"]["status"] == "skipped"
    assert "freshness_error" in manifest or manifest["push"].get("reason") == "canonical freshness check failed"
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "fail"


def test_git_finalizer_uses_non_main_canonical_branch(tmp_path, monkeypatch):
    """Non-main canonical branch (e.g. 'develop') is respected; not hardcoded to 'main'."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "develop")
    _git(work, "push", "origin", "develop")
    _git(work, "checkout", "-b", "task/feature-develop")
    (work / "feature.py").write_text("print('develop-feature')\n", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-develop",
        "metadata": {
            "publication_target": "git://develop",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": origin.as_uri(),
                    "default_branch": "develop",
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is True
    assert "feature.py" in manifest["repo"]["files_changed"]
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "pass"


def test_git_finalizer_blocks_when_canonical_remote_is_missing(tmp_path, monkeypatch):
    """A named-origin guess cannot replace the prepared canonical target."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "-b", "task/no-remote")
    (work / "feature.py").write_text("print('x')\n", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    _install_fake_codegraph(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    # No canonical_remote_url or prepared canonical URL: fail closed.
    task = {
        "id": "t-no-remote",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "test": {"command": "true"},
                }
            },
        },
    }

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is False
    assert manifest["push"]["status"] == "skipped"
    assert "remote URL" in manifest["freshness_error"]
    assert {item["name"]: item["status"] for item in manifest["checks"]}["git_finalizer"] == "fail"


def test_repository_contract_canonical_branch_reads_from_contract(tmp_path):
    """_repository_contract_canonical_branch reads default_branch from contract."""
    task = {
        "metadata": {
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "https://github.com/org/repo.git",
                    "default_branch": "develop",
                }
            }
        }
    }
    assert te._repository_contract_canonical_branch(task) == "develop"


def test_repository_contract_canonical_branch_reads_from_runtime(tmp_path):
    """_repository_contract_canonical_branch falls back to runtime context."""
    task = {
        "metadata": {
            "runtime": {
                "repository_canonical_branch": "release",
            }
        }
    }
    assert te._repository_contract_canonical_branch(task) == "release"


def test_repository_contract_canonical_branch_empty_when_absent():
    """_repository_contract_canonical_branch returns '' when no branch info."""
    assert te._repository_contract_canonical_branch({}) == ""
    assert te._repository_contract_canonical_branch({"metadata": {}}) == ""


def test_review_finalizer_runs_contract_bootstrap_before_tests(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _git(tmp_path, "init", str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    scripts = work / "scripts"
    scripts.mkdir()
    (scripts / "bootstrap-project.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "venv_python = Path('.venv/bin/python')",
                "venv_python.parent.mkdir(parents=True, exist_ok=True)",
                "venv_python.write_text('#!/bin/sh\\nexit 0\\n', encoding='utf-8')",
                "venv_python.chmod(0o755)",
            ]
        ),
        encoding="utf-8",
    )
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "repo": {
                            "head_sha": head,
                            "remote_ref": "refs/heads/task/bootstrap",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (ws / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "approved",
                "summary": "semantic review found no defects",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "secret")
    task = {
        "id": "t1",
        "owner_agent_id": "agent_review",
        "metadata": {
            "origin": {
                "repository_contract": {
                    "bootstrap": {
                        "command": "python3 scripts/bootstrap-project.py",
                        "creates": [".venv/bin/python"],
                    },
                    "test": {"command": ".venv/bin/python -c 'print(123)'"},
                }
            }
        },
    }

    te.run_deterministic_review_verdict(
        ws,
        task,
        {"executor_evidence_id": "ev1", "review_id": "review1"},
    )

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert (work / ".venv/bin/python").exists()
    assert manifest["verdict"] == "approved"
    assert manifest["bootstrap"]["status"] == "pass"
    assert manifest["tests"][0]["returncode"] == 0


def test_review_finalizer_never_upgrades_semantic_rejection(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _git(tmp_path, "init", str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "repo": {
                            "head_sha": head,
                            "remote_ref": "refs/heads/task/semantic-reject",
                            "pushed": True,
                            "dirty": False,
                            "files_changed": ["README.md"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (ws / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "rejected",
                "feedback": "The implementation violates the required architecture.",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "secret")
    task = {
        "id": "t1",
        "owner_agent_id": "agent_review",
        "metadata": {
            "origin": {"repository_contract": {"test": {"command": "true"}}}
        },
    }

    te.run_deterministic_review_verdict(
        ws, task, {"executor_evidence_id": "ev1", "review_id": "review1"}
    )

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == "rejected"
    assert manifest["semantic_verdict"] == "rejected"
    assert manifest["tests"][0]["returncode"] == 0
    assert "violates" in manifest["feedback"]


def test_review_finalizer_requires_exact_executor_head(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _git(tmp_path, "init", str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "reviewed.txt").write_text("reviewed\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "reviewed")
    reviewed_head = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "other.txt").write_text("wrong checkout\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "other")

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "repo": {
                            "head_sha": reviewed_head,
                            "remote_ref": "refs/heads/task/reviewed",
                            "pushed": True,
                            "dirty": False,
                            "files_changed": ["reviewed.txt"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (ws / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "approved",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "secret")

    te.run_deterministic_review_verdict(
        ws,
        {
            "owner_agent_id": "agent_review",
            "metadata": {
                "origin": {"repository_contract": {"test": {"command": "true"}}}
            },
        },
        {"executor_evidence_id": "ev1", "review_id": "review1"},
    )

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == "rejected"
    assert "HEAD does not match" in manifest["feedback"]
    assert manifest["tests"] is None


def test_blind_review_prompts_separate_discovery_from_adjudication(tmp_path):
    assignment = {
        "schema": "mac.review_experiment.v1",
        "experiment_id": "exp-blind",
        "arm": "blind",
        "blind": True,
    }
    task = {"metadata": {"review_experiment": assignment}}

    discovery = te.build_blind_review_discovery_prompt(task, tmp_path, assignment)
    adjudication = te.build_review_prompt(
        task,
        tmp_path,
        {"executor_evidence_id": "ev1", "review_id": "review1"},
    )

    assert "physically withheld" in discovery
    assert "Do not create mac-evidence.json" in discovery
    assert "review-independent-findings.json first" in adjudication
    assert "then read the executor evidence" in adjudication


def test_blind_review_protocol_requires_fresh_structured_findings(tmp_path):
    import subprocess

    assignment = {
        "experiment_id": "exp-blind",
        "arm": "blind",
    }
    (tmp_path / "review-independent-findings.json").write_text(
        json.dumps(
            {
                "schema": "mac.independent_review_findings.v1",
                "experiment_id": "exp-blind",
                "arm": "blind",
                "findings": [],
                "no_findings_reason": "diff and focused checks found no defect",
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.CompletedProcess(["review"], 0, "ok", "")

    protocol = te._blind_review_protocol(
        tmp_path,
        assignment,
        result,
        duration_ms=12.5,
        evidence_hidden=True,
    )

    assert protocol["protocol_compliant"] is True
    assert protocol["executor_evidence_hidden"] is True
    assert protocol["independent_findings_sha256"].startswith("sha256:")


def test_run_executor_physically_withholds_and_restores_evidence_for_blind_pass(
    tmp_path, monkeypatch
):
    import subprocess

    task = {
        "id": "review_1",
        "metadata": {
            "review_context": {
                "task_id": "task_1",
                "review_id": "review_1",
                "executor_evidence_id": "evidence_1",
            },
            "review_experiment": {
                "schema": "mac.review_experiment.v1",
                "experiment_id": "exp-blind",
                "arm": "blind",
                "blind": True,
            },
        },
    }
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}), encoding="utf-8")
    (tmp_path / "executor-evidence.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_invoke(_runner, prompt, workspace, _audit_id, opts):
        calls.append(opts["execution_kind"])
        if opts["execution_kind"] == "review_discovery":
            assert not (workspace / "executor-evidence.json").exists()
            assert not any(
                "executor-evidence" in path.name for path in workspace.iterdir()
            )
            (workspace / "review-independent-findings.json").write_text(
                json.dumps(
                    {
                        "schema": "mac.independent_review_findings.v1",
                        "experiment_id": "exp-blind",
                        "arm": "blind",
                        "findings": [],
                        "no_findings_reason": "independent inspection found none",
                    }
                ),
                encoding="utf-8",
            )
        else:
            assert (workspace / "executor-evidence.json").exists()
            (workspace / "mac-evidence.json").write_text(
                json.dumps(
                    {
                        "schema": "mac.worker_evidence.v1",
                        "status": "complete",
                        "evidence_type": "review_verdict",
                        "verdict": "approved",
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(["agent"], 0, "ok", "")

    monkeypatch.setattr(te, "_invoke_agent", fake_invoke)
    monkeypatch.setattr(te, "run_deterministic_review_verdict", lambda *args: None)
    monkeypatch.setattr(te, "emit_telemetry", lambda *args, **kwargs: True)

    rc = te._run_executor(
        runner=lambda *args, **kwargs: None,
        task=task,
        task_file=task_file,
        task_workspace=tmp_path,
        task_id=task["id"],
        review_context=task["metadata"]["review_context"],
        is_review=True,
    )

    assert rc == 0
    assert calls == ["review_discovery", "review"]
    assert (tmp_path / "executor-evidence.json").exists()
    protocol = json.loads(
        (tmp_path / "review-protocol.json").read_text(encoding="utf-8")
    )
    assert protocol["protocol_compliant"] is True


def test_run_executor_stops_after_noncompliant_blind_discovery(
    tmp_path, monkeypatch
):
    import subprocess

    task = {
        "id": "review_1",
        "metadata": {
            "review_context": {
                "task_id": "task_1",
                "review_id": "review_1",
                "executor_evidence_id": "evidence_1",
            },
            "review_experiment": {
                "schema": "mac.review_experiment.v1",
                "experiment_id": "exp-blind",
                "arm": "blind",
                "blind": True,
            },
        },
    }
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}), encoding="utf-8")
    (tmp_path / "executor-evidence.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_invoke(_runner, _prompt, _workspace, _audit_id, opts):
        calls.append(opts["execution_kind"])
        # The agent returns zero but omits review-independent-findings.json.
        return subprocess.CompletedProcess(["agent"], 0, "stopped", "")

    monkeypatch.setattr(te, "_invoke_agent", fake_invoke)
    monkeypatch.setattr(
        te,
        "run_deterministic_review_verdict",
        lambda *args: pytest.fail("invalid discovery must not reach adjudication"),
    )
    monkeypatch.setattr(te, "emit_telemetry", lambda *args, **kwargs: True)

    rc = te._run_executor(
        runner=lambda *args, **kwargs: None,
        task=task,
        task_file=task_file,
        task_workspace=tmp_path,
        task_id=task["id"],
        review_context=task["metadata"]["review_context"],
        is_review=True,
    )

    assert rc == 65
    assert calls == ["review_discovery"]
    assert not (tmp_path / "mac-evidence.json").exists()
    protocol = json.loads(
        (tmp_path / "review-protocol.json").read_text(encoding="utf-8")
    )
    assert protocol["protocol_compliant"] is False


def test_review_finalizer_signs_experiment_protocol_and_independent_findings(
    tmp_path, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "executor-evidence.json").write_text(
        json.dumps({"metadata": {"verification": {"evidence_type": "operator_result"}}}),
        encoding="utf-8",
    )
    (ws / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "approved",
                "summary": "semantic review passed",
            }
        ),
        encoding="utf-8",
    )
    (ws / "review-independent-findings.json").write_text(
        json.dumps(
            {
                "schema": "mac.independent_review_findings.v1",
                "experiment_id": "exp-blind",
                "arm": "blind",
                "findings": [{"summary": "one independent concern"}],
                "no_findings_reason": "",
            }
        ),
        encoding="utf-8",
    )
    (ws / "review-protocol.json").write_text(
        json.dumps(
            {
                "schema": "mac.review_protocol.v1",
                "mode": "blind_discovery_then_adjudication",
                "protocol_compliant": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "secret")
    task = {
        "metadata": {
            "review_experiment": {
                "schema": "mac.review_experiment.v1",
                "experiment_id": "exp-blind",
                "arm": "blind",
                "blind": True,
            }
        },
    }

    te.run_deterministic_review_verdict(
        ws,
        task,
        {
            "executor_evidence_id": "ev1",
            "review_id": "review1",
            "review_claim": {"reviewer_agent_id": "agent_review"},
        },
    )

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == "approved"
    assert manifest["review_experiment"]["protocol"]["protocol_compliant"] is True
    assert manifest["independent_findings"] == [
        {"summary": "one independent concern"}
    ]
    assert manifest["signed_by"] == "agent_review"
    assert manifest["signature"]


def test_cooperative_integration_check_requires_child_commit_ancestry(tmp_path):
    repo = tmp_path / "repo"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-b", "child")
    (repo / "child.txt").write_text("child\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "child")
    child = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-b", "integration", base)
    task = {
        "metadata": {
            "coordination": {
                "phase": "integration",
                "child_outputs": [
                    {
                        "task_id": "child-task",
                        "status": "ready",
                        "executor_evidence_id": "ev-child",
                        "repo": {"head_sha": child},
                    }
                ],
            }
        }
    }

    failed = te._cooperative_integration_check(task, repo)
    assert failed["status"] == "fail"
    assert "not an ancestor" in failed["problems"][0]

    _git(repo, "merge", "--no-ff", "child", "-m", "integrate child")
    passed = te._cooperative_integration_check(task, repo)
    assert passed["status"] == "pass"
    assert passed["verified_child_evidence_ids"] == ["ev-child"]

    task["metadata"]["coordination"]["child_outputs"].append(
        {"task_id": "missing-child", "status": "missing_evidence"}
    )
    missing = te._cooperative_integration_check(task, repo)
    assert missing["status"] == "fail"
    assert any("missing-child" in problem for problem in missing["problems"])


# ---------------------------------------------------------------------------
# main() end-to-end with injected runner + hub
# ---------------------------------------------------------------------------


def test_main_runs_records_telemetry_and_memory(tmp_path, monkeypatch):
    task = {"id": "t1", "title": "Plan the rollout", "project": "demo", "metadata": {"publication_target": "test://x"}}
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file))
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "0")
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")

    # This test exercises the Hermes -> gateway runner (asserts the --query argv);
    # pin coding-agent preference off so it is independent of which coding-agent
    # CLIs happen to be installed on the dev/CI machine.
    monkeypatch.setenv("MAC_CODING_AGENT", "off")

    posts = []
    monkeypatch.setattr(te, "_hub_post", lambda path, payload, **kw: posts.append((path, payload)) or True)
    monkeypatch.setattr(te, "_hub_get", lambda path, **kw: [{"content": "push before reporting"}])
    # Inject a fake runner: assert it received the recalled lesson, return chatty output.
    seen = {}

    def fake_runner(argv, cwd, task_id, metadata):
        prompt_file = Path(argv[argv.index("--prompt-file") + 1])
        seen["prompt"] = prompt_file.read_text(encoding="utf-8")
        seen["argv"] = list(argv)
        return _FakeResult(0, stdout="Produced the rollout plan and mapped the dependencies.\n")

    rc = te.main(runner=fake_runner)
    assert rc == 0
    # recalled lesson reached the prompt
    assert "push before reporting" in seen["prompt"]
    assert "push before reporting" not in seen["argv"]
    # fallback wrote an unverified operator_result
    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["evidence_type"] == "operator_result"
    # telemetry path fired (started + agent_completed + finalized)
    telemetry = [p for p in posts if p[0] == "/observability/logs"]
    names = {p[1]["name"] for p in telemetry}
    assert {"executor.started", "executor.agent_completed", "executor.finalized"} <= names
    # memory feed recorded a deployment lesson
    memory = [p for p in posts if p[0] == "/memory"]
    assert memory and memory[0][1]["record_type"] == "deployment_learning:demo"


# ---------------------------------------------------------------------------
# Coding-agent runner selection (_invoke_agent routes through the resolver)
# ---------------------------------------------------------------------------


def test_invoke_agent_routes_to_coding_agent_when_available(tmp_path, monkeypatch):
    """When a coding-agent CLI is available + authed, _invoke_agent runs THAT
    (in the checkout, with the same prompt + a materialized MCP config), instead
    of the Hermes -> gateway argv."""
    from mac import coding_agent as ca

    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_OPENSHELL_REQUIRED", "0")  # unconfined -> no preflight gate
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    # Force a Claude choice deterministically (no real PATH/home probing).
    choice = ca.CodingAgentChoice(
        agent="claude", available=True, binary="/usr/local/bin/claude", auth_source="ANTHROPIC_API_KEY"
    )
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)

    captured = {}

    def fake_runner(argv, cwd, task_id, metadata):
        captured["argv"] = argv
        command_file = Path(argv[argv.index("--command-file") + 1])
        captured["agent_argv"] = json.loads(
            command_file.read_text(encoding="utf-8")
        )["argv"]
        return _FakeResult(0, stdout="done\n")

    te._invoke_agent(fake_runner, "fix the bug", tmp_path, "tid", {})
    argv = captured["argv"]
    agent_argv = captured["agent_argv"]
    assert "mac.agent_command" in argv
    assert "fix the bug" not in argv
    assert agent_argv[0] == "/usr/local/bin/claude"
    assert "-p" in agent_argv and agent_argv[-1] == te.PROMPT_SENTINEL
    assert "--dangerously-skip-permissions" in agent_argv
    # Messaging MCP config was materialized in the workspace and wired in (Claude).
    assert "--mcp-config" in agent_argv
    assert (tmp_path / ".mac-coding-agent-mcp.json").is_file()


def test_invoke_agent_falls_back_to_hermes_when_no_coding_agent(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "1")
    monkeypatch.setenv("MAC_OPENSHELL_REQUIRED", "0")
    none = ca.CodingAgentChoice(agent="", available=False)
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: none)

    captured = {}
    te._invoke_agent(
        lambda argv, cwd, task_id, metadata: captured.update(argv=argv) or _FakeResult(0),
        "do it",
        tmp_path,
        "tid",
        {},
    )
    # Fell back to Hermes, still behind the private-prompt wrapper.
    assert "mac.agent_command" in captured["argv"]
    assert "do it" not in captured["argv"]


# ---------------------------------------------------------------------------
# Coding-agent in-sandbox gating (works in the sandbox -> enabled; else gateway)
# ---------------------------------------------------------------------------


def test_agent_argv_sandboxed_uses_coding_agent_only_when_verified(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    choice = ca.CodingAgentChoice(agent="claude", available=True, binary="/b/claude", auth_source="ANTHROPIC_API_KEY")
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    monkeypatch.setattr(te, "_coding_agent_sandbox_ok", lambda c: True)
    argv = te._agent_argv("do it", tmp_path, confined=True)
    assert argv[0] == "/b/claude" and argv[-1] == "do it"
    # No per-invocation MCP wiring on the sandboxed path (host paths don't resolve
    # inside the sandbox); no host MCP config file written.
    assert "--mcp-config" not in argv
    assert not (tmp_path / ".mac-coding-agent-mcp.json").exists()


def test_agent_argv_sandboxed_falls_back_when_not_verified(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    choice = ca.CodingAgentChoice(agent="claude", available=True, binary="/b/claude")
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    monkeypatch.setattr(te, "_coding_agent_sandbox_ok", lambda c: False)
    argv = te._agent_argv("do it", tmp_path, confined=True)
    # Non-repository work can still fall back to the confined Hermes gateway.
    assert "--query" in argv and "hermes_cli.main" in argv


def test_agent_argv_attributes_runner_choice_to_review_task(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    choice = ca.CodingAgentChoice(
        agent="", available=False, rationale=["no coding agent"]
    )
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    emitted = []
    monkeypatch.setattr(
        te,
        "emit_telemetry",
        lambda event, **detail: emitted.append((event, detail)) or True,
    )

    te._agent_argv(
        "do it",
        tmp_path,
        confined=True,
        task={"id": "review_review_1", "metadata": {"review_context": {}}},
    )

    assert emitted == [
        (
            "runner_selected",
            {
                "task_id": "review_review_1",
                "level": "info",
                "schema": "mac.coding_agent.routing.v1",
                "runner": "hermes-gateway",
                "rationale": ["no coding agent"],
            },
        )
    ]


def test_agent_argv_sandboxed_repo_task_falls_back_when_not_verified(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    choice = ca.CodingAgentChoice(agent="codex", available=True, binary="/b/codex")
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    monkeypatch.setattr(te, "_coding_agent_sandbox_ok", lambda c: False)
    task = {"metadata": {"execution_contract": {"type": "repository"}}}
    argv = te._agent_argv("do it", tmp_path, confined=True, task=task)
    assert "--query" in argv and "hermes_cli.main" in argv


def test_agent_argv_sandboxed_repo_task_falls_back_when_no_coding_agent(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    choice = ca.CodingAgentChoice(agent="", available=False, rationale=["no coding agent"])
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    task = {"metadata": {"execution_contract": {"type": "repository"}}}
    argv = te._agent_argv("do it", tmp_path, confined=True, task=task)
    assert "--query" in argv and "hermes_cli.main" in argv


def test_agent_argv_sandboxed_repo_task_strict_mode_fails_closed_when_not_verified(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    choice = ca.CodingAgentChoice(agent="codex", available=True, binary="/b/codex")
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    monkeypatch.setattr(te, "_coding_agent_sandbox_ok", lambda c: False)
    task = {"metadata": {"execution_contract": {"type": "repository"}}}
    argv = te._agent_argv("do it", tmp_path, confined=True, task=task)
    joined = " ".join(argv)
    assert "hermes_cli.main" not in joined
    assert "require a verified in-sandbox coding agent" in joined


def test_agent_argv_sandboxed_repo_task_strict_mode_fails_closed_when_no_coding_agent(tmp_path, monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    choice = ca.CodingAgentChoice(agent="", available=False, rationale=["no coding agent"])
    monkeypatch.setattr(ca, "resolve_coding_agent", lambda *a, **k: choice)
    task = {"metadata": {"execution_contract": {"type": "repository"}}}
    argv = te._agent_argv("do it", tmp_path, confined=True, task=task)
    joined = " ".join(argv)
    assert "hermes_cli.main" not in joined
    assert "no host coding agent is available/authenticated" in joined


def test_sandbox_mode_off_never_probes(monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.setenv("MAC_CODING_AGENT_SANDBOX", "off")
    monkeypatch.setattr(
        te, "_run_coding_agent_preflight", lambda c: (_ for _ in ()).throw(AssertionError("must not probe"))
    )
    choice = ca.CodingAgentChoice(agent="codex", available=True, binary="/b/codex")
    assert te._coding_agent_sandbox_ok(choice) is False


def test_sandbox_mode_trust_skips_probe(monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.setenv("MAC_CODING_AGENT_SANDBOX", "trust")
    monkeypatch.setattr(
        te, "_run_coding_agent_preflight", lambda c: (_ for _ in ()).throw(AssertionError("must not probe"))
    )
    choice = ca.CodingAgentChoice(agent="codex", available=True, binary="/b/codex")
    assert te._coding_agent_sandbox_ok(choice) is True


def test_sandbox_verify_runs_probe_once_and_caches(monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.delenv("MAC_CODING_AGENT_SANDBOX", raising=False)  # default = verify
    te._SANDBOX_PREFLIGHT_CACHE.clear()
    calls = []
    monkeypatch.setattr(te, "_run_coding_agent_preflight", lambda c: calls.append(c.agent) or True)
    choice = ca.CodingAgentChoice(agent="claude", available=True, binary="/b/claude")
    assert te._coding_agent_sandbox_ok(choice) is True
    assert te._coding_agent_sandbox_ok(choice) is True  # second call served from cache
    assert calls == ["claude"]


def test_sandbox_verify_skips_codex_rotating_file_auth_by_default(monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.delenv("MAC_CODING_AGENT_SANDBOX", raising=False)  # default = verify
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH", raising=False)
    te._SANDBOX_PREFLIGHT_CACHE.clear()
    monkeypatch.setattr(
        te, "_run_coding_agent_preflight", lambda c: (_ for _ in ()).throw(AssertionError("must not probe"))
    )
    choice = ca.CodingAgentChoice(
        agent="codex", available=True, binary="/b/codex", auth_source="~/.codex/auth.json"
    )
    assert te._coding_agent_sandbox_ok(choice) is False


def test_sandbox_verify_can_opt_into_codex_file_auth_probe(monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.delenv("MAC_CODING_AGENT_SANDBOX", raising=False)  # default = verify
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH", "1")
    te._SANDBOX_PREFLIGHT_CACHE.clear()
    calls = []
    monkeypatch.setattr(te, "_run_coding_agent_preflight", lambda c: calls.append(c.agent) or True)
    choice = ca.CodingAgentChoice(
        agent="codex", available=True, binary="/b/codex", auth_source="~/.codex/auth.json"
    )
    assert te._coding_agent_sandbox_ok(choice) is True
    assert calls == ["codex"]


def test_preflight_passes_only_on_sentinel_and_always_deletes(monkeypatch):
    from mac import coding_agent as ca

    seen = {}

    def fake_probe(create_argv, *, timeout):
        seen["argv"] = create_argv
        return 0, "noise\n%s\n" % ca.PREFLIGHT_SENTINEL

    deleted = []
    monkeypatch.setattr(te, "_openshell_probe", fake_probe)
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: deleted.append(args) or (True, ""))
    choice = ca.CodingAgentChoice(agent="claude", available=True, binary="/usr/bin/claude")
    assert te._run_coding_agent_preflight(choice) is True
    # The probe runs through private files: neither prompt nor underlying agent
    # command/credentials appear in the host's long-lived create argv.
    assert "create" in seen["argv"]
    joined = " ".join(seen["argv"])
    assert "mac.agent_command" in joined
    assert "/usr/bin/claude" not in joined
    assert ca.PREFLIGHT_PROMPT not in joined
    assert ca.PREFLIGHT_SENTINEL not in joined
    # The throwaway sandbox is always deleted.
    assert deleted and deleted[0][0] == "delete"


def test_preflight_fails_without_sentinel(monkeypatch):
    from mac import coding_agent as ca

    monkeypatch.setattr(te, "_openshell_probe", lambda create_argv, *, timeout: (0, "auth error: not logged in"))
    monkeypatch.setattr(te, "_sandbox_step", lambda args, *, timeout: (True, ""))
    choice = ca.CodingAgentChoice(agent="codex", available=True, binary="/usr/bin/codex")
    assert te._run_coding_agent_preflight(choice) is False


# ---------------------------------------------------------------------------
# Task sizing — detect_plan_signals
# ---------------------------------------------------------------------------


def test_detect_plan_signals_atomic_task_no_signals():
    """A tight, focused task title with no description produces no plan signals."""
    is_plan, signals = te.detect_plan_signals("Fix null pointer in auth handler", "")
    assert is_plan is False
    assert signals == []


def test_detect_plan_signals_keyword_in_title():
    """A single plan keyword in the title contributes signal 1 only (below threshold)."""
    is_plan, signals = te.detect_plan_signals("Build end-to-end test suite", "")
    assert not is_plan  # only 1 signal: plan_keyword
    assert any("plan_keyword" in s for s in signals)


def test_detect_plan_signals_long_title():
    """A very long title alone is one signal — still not enough."""
    long_title = "A" * 125
    is_plan, signals = te.detect_plan_signals(long_title, "")
    assert not is_plan
    assert any("long_title" in s for s in signals)


def test_detect_plan_signals_keyword_plus_numbered_steps():
    """Plan keyword in title + numbered steps in description → is_plan."""
    title = "Implement and deploy the new auth service"
    description = (
        "1. Set up the database schema\n"
        "2. Implement the API endpoints\n"
        "3. Write integration tests\n"
        "4. Deploy to staging\n"
    )
    is_plan, signals = te.detect_plan_signals(title, description)
    assert is_plan is True
    assert any("plan_keyword" in s or "numbered_steps" in s for s in signals)


def test_detect_plan_signals_bullet_cluster():
    """5+ bullet points alone is one signal; combine with a keyword to cross threshold."""
    title = "Set up and configure the monitoring stack"
    description = (
        "- Install Prometheus\n"
        "- Configure node_exporter\n"
        "- Set up Grafana\n"
        "- Write alerting rules\n"
        "- Test end-to-end\n"
        "- Document the setup\n"
    )
    is_plan, signals = te.detect_plan_signals(title, description)
    assert is_plan is True
    assert any("bullet_cluster" in s for s in signals)


def test_detect_plan_signals_long_description():
    """A long description alone (>300 words) is one signal; keyword brings it over."""
    title = "Design architecture for the new pipeline"
    description = " ".join(["word"] * 350)
    is_plan, signals = te.detect_plan_signals(title, description)
    assert is_plan is True
    assert any("long_description" in s for s in signals)
    assert any("plan_keyword" in s for s in signals)


def test_detect_plan_signals_conjunctive_verb_title():
    """Two distinct verb clauses joined by 'and' is a signal; add numbered steps."""
    title = "Implement the parser and add unit tests"
    description = "1. Write parser\n2. Write tests\n3. Verify coverage\n"
    is_plan, signals = te.detect_plan_signals(title, description)
    assert is_plan is True
    assert any("conjunctive_verb_title" in s or "numbered_steps" in s for s in signals)


def test_detect_plan_signals_child_task_exempt():
    """A task that is already a child (has parent_task_id) gets no plan section."""
    task = {
        "id": "t_child",
        "title": "Implement and deploy the full pipeline for everything",
        "description": "1. step\n2. step\n3. step\n4. step\n",
        "metadata": {
            "relationships": {"parent_task_id": "t_parent", "relationship": "child"}
        },
    }
    section = te._plan_detection_section(task)
    assert section == ""


def test_detect_plan_signals_task_with_existing_children_exempt():
    """A task that already has child_task_ids is not prompted to decompose again."""
    task = {
        "id": "t_parent",
        "title": "Build and deploy everything",
        "metadata": {
            "relationships": {
                "child_task_ids": ["t1", "t2"],
                "blocked_by_task_ids": ["t1", "t2"],
            }
        },
    }
    section = te._plan_detection_section(task)
    assert section == ""


def test_plan_detection_section_included_in_prompt_when_plan_signals_present():
    """build_task_prompt includes plan section when task looks like a plan."""
    title = "Implement data pipeline and add monitoring"
    description = (
        "1. Set up ingestion\n"
        "2. Transform data\n"
        "3. Load to warehouse\n"
        "4. Add Grafana dashboards\n"
        "5. Write runbook\n"
    )
    task = {"id": "t1", "title": title, "description": description}
    prompt = te.build_task_prompt(task, Path("/tmp/task.json"), lessons=[])
    assert "Task Sizing and Plan Detection" in prompt
    assert "add_child_tasks" in prompt or "children" in prompt


def test_plan_detection_section_omitted_for_atomic_task():
    """build_task_prompt omits plan section for an obviously atomic task."""
    task = {"id": "t1", "title": "Fix the off-by-one in the tokenizer", "description": ""}
    prompt = te.build_task_prompt(task, Path("/tmp/task.json"), lessons=[])
    # Plan section should NOT be present for a simple task
    assert "TASK-SIZING ALERT" not in prompt
    # But the standard prompt sections must still be present
    assert "mac-evidence.json" in prompt
    assert "first principles" in prompt


# ---------------------------------------------------------------------------
# maybe_auto_decompose
# ---------------------------------------------------------------------------


def test_maybe_auto_decompose_no_manifest(tmp_path):
    """Returns False quietly when no evidence manifest exists."""
    task = {"id": "t1", "title": "something"}
    assert te.maybe_auto_decompose(tmp_path, task) is False


def test_maybe_auto_decompose_no_plan_steps(tmp_path):
    """Returns False when evidence manifest has no plan_steps key."""
    manifest = {"schema": "mac.worker_evidence.v1", "status": "complete",
                "evidence_type": "operator_result", "summary": "done"}
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    task = {"id": "t1", "title": "something"}
    assert te.maybe_auto_decompose(tmp_path, task) is False


def test_maybe_auto_decompose_empty_plan_steps(tmp_path):
    """Returns False when plan_steps is an empty list."""
    manifest = {"schema": "mac.worker_evidence.v1", "status": "complete",
                "evidence_type": "operator_result", "summary": "done", "plan_steps": []}
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    task = {"id": "t1", "title": "something"}
    assert te.maybe_auto_decompose(tmp_path, task) is False


def test_maybe_auto_decompose_child_task_not_decomposed(tmp_path):
    """A child task is never further decomposed, even if it has plan_steps."""
    manifest = {
        "schema": "mac.worker_evidence.v1", "status": "complete",
        "evidence_type": "operator_result", "summary": "done",
        "plan_steps": [{"title": "Sub-step A"}, {"title": "Sub-step B"}],
    }
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    task = {
        "id": "t_child", "title": "something",
        "metadata": {"relationships": {"parent_task_id": "t_parent"}},
    }
    assert te.maybe_auto_decompose(tmp_path, task) is False


def test_maybe_auto_decompose_posts_children_when_hub_present(tmp_path, monkeypatch):
    """When plan_steps are present and hub env is set, child tasks are POSTed."""
    manifest = {
        "schema": "mac.worker_evidence.v1", "status": "complete",
        "evidence_type": "operator_result", "summary": "This is a plan.",
        "plan_steps": [
            {"title": "Step A — implement ingestion", "description": "Build the source connector."},
            {"title": "Step B — implement transform", "description": "Apply data transforms."},
            {"title": "Step C — write tests"},
        ],
    }
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    task = {"id": "task_abc", "title": "Build the full pipeline", "project": "demo"}

    # Capture the POST payload
    captured = {}

    def fake_post_children(task_id, children):
        captured["task_id"] = task_id
        captured["children"] = children
        return {"parent_task_id": task_id, "children": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]}

    monkeypatch.setattr(te, "_hub_post_child_tasks", fake_post_children)

    result = te.maybe_auto_decompose(tmp_path, task)
    assert result is True
    assert captured["task_id"] == "task_abc"
    assert len(captured["children"]) == 3
    titles = [c["title"] for c in captured["children"]]
    assert "Step A — implement ingestion" in titles
    assert "Step B — implement transform" in titles
    assert "Step C — write tests" in titles
    # description is preserved when present
    step_a = next(c for c in captured["children"] if "Step A" in c["title"])
    assert step_a.get("description") == "Build the source connector."


def test_maybe_auto_decompose_skips_steps_without_title(tmp_path, monkeypatch):
    """Steps without a title are silently dropped; only titled steps are posted."""
    manifest = {
        "schema": "mac.worker_evidence.v1", "status": "complete",
        "evidence_type": "operator_result", "summary": "plan",
        "plan_steps": [
            {"title": "Good step"},
            {"description": "No title here"},  # dropped
            {},                                  # dropped
            {"title": "  "},                    # blank title → dropped
            {"title": "Another good step"},
        ],
    }
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    task = {"id": "task_xyz", "title": "Plan task"}

    captured = {}
    monkeypatch.setattr(te, "_hub_post_child_tasks", lambda tid, ch: captured.update({"ch": ch}) or {"ok": True})

    result = te.maybe_auto_decompose(tmp_path, task)
    assert result is True
    assert len(captured["ch"]) == 2
    assert captured["ch"][0]["title"] == "Good step"
    assert captured["ch"][1]["title"] == "Another good step"


def test_maybe_auto_decompose_noop_when_hub_absent(tmp_path, monkeypatch):
    """Returns False (and never raises) when hub env vars are absent."""
    manifest = {
        "schema": "mac.worker_evidence.v1", "status": "complete",
        "evidence_type": "operator_result", "summary": "plan",
        "plan_steps": [{"title": "Step A"}, {"title": "Step B"}],
    }
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    task = {"id": "task_nohub", "title": "Plan task"}

    # Remove hub env
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)

    result = te.maybe_auto_decompose(tmp_path, task)
    assert result is False


def test_main_emits_plan_decomposed_telemetry(tmp_path, monkeypatch):
    """When the agent writes plan_steps, main() calls maybe_auto_decompose and
    emits executor.plan_decomposed telemetry."""
    task = {"id": "t_plan", "title": "Implement and deploy the full pipeline", "project": "demo"}
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"task": task}))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file))
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(ws))
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")

    posts = []
    monkeypatch.setattr(te, "_hub_post", lambda path, payload, **kw: posts.append((path, payload)) or True)
    monkeypatch.setattr(te, "_hub_get", lambda path, **kw: [])

    def plan_runner(argv, cwd, task_id, metadata):
        # Agent writes evidence with plan_steps
        (ws / "mac-evidence.json").write_text(json.dumps({
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Task is a plan; broke into children.",
            "plan_steps": [
                {"title": "Step 1 — implement"},
                {"title": "Step 2 — deploy"},
                {"title": "Step 3 — verify"},
            ],
        }))
        return _FakeResult(0, stdout="Plan decomposed.\n")

    captured_children = {}

    def fake_post_children(task_id, children):
        captured_children["task_id"] = task_id
        captured_children["children"] = children
        return {"ok": True}

    monkeypatch.setattr(te, "_hub_post_child_tasks", fake_post_children)

    rc = te.main(runner=plan_runner)
    assert rc == 0

    # plan_decomposed telemetry should have been emitted
    tel_names = {p[1]["name"] for p in posts if p[0] == "/observability/logs"}
    assert "executor.plan_decomposed" in tel_names

    # children were posted
    assert captured_children.get("task_id") == "t_plan"
    assert len(captured_children.get("children", [])) == 3


# --------------------------------------------------------------------------- #
# run_with_stall_watchdog: progress-based, not total-runtime-based
# --------------------------------------------------------------------------- #


def test_stall_watchdog_kills_silent_hang_quickly(tmp_path):
    # A hung process goes quiet; the watchdog kills on output stall — long
    # before any total-runtime budget — with an explicit diagnosable marker.
    import time as _time
    start = _time.monotonic()
    r = te.run_with_stall_watchdog(
        ["bash", "-c", "echo working; sleep 60"], tmp_path,
        stall_timeout=1.0, hard_timeout=120.0,
    )
    assert r.returncode == 124
    assert "stalled: no output" in r.stderr
    assert "working" in r.stdout            # pre-hang output preserved
    assert _time.monotonic() - start < 20   # killed on stall, not after 60s


def test_stall_watchdog_lets_slow_but_chatty_work_finish(tmp_path):
    # Steady progress output means NO kill even when total runtime exceeds the
    # stall window — the exact scenario stale total-runtime budgets murdered.
    r = te.run_with_stall_watchdog(
        ["bash", "-c", "for i in 1 2 3 4 5 6; do echo tick $i; sleep 0.5; done; echo done"],
        tmp_path, stall_timeout=1.5, hard_timeout=120.0,
    )
    assert r.returncode == 0
    assert "done" in r.stdout


def test_stall_watchdog_hard_ceiling_stops_chatty_loops(tmp_path):
    # The backstop: a pathological always-printing loop still dies at the ceiling.
    r = te.run_with_stall_watchdog(
        ["bash", "-c", "while true; do echo spin; sleep 0.2; done"], tmp_path,
        stall_timeout=5.0, hard_timeout=2.0,
    )
    assert r.returncode == 124
    assert "hard ceiling" in r.stderr


def test_clip_process_text_keeps_failure_tail():
    # Same contract as worker._truncate_process_text: the tail carries the
    # diagnosis. Applies to executor bootstrap/test/sandbox evidence items.
    text = ("y" * 9000) + "\nFAILED tests/x.py::t - kaboom\n2 failed"
    out = te.clip_process_text(text, limit=4000)
    assert len(out) < 4200
    assert "chars omitted" in out
    assert out.endswith("2 failed")
    assert te.clip_process_text("short", limit=4000) == "short"


def test_sandbox_verifier_script_clips_head_and_tail():
    # The in-sandbox verification heredoc is self-contained; its payload
    # builders must use the tail-preserving clip, not a bare [:4000] head cut
    # (the site #204 missed — it ate natasha's failure tail on 2026-07-04).
    import inspect
    src = inspect.getsource(te)
    i = src.find("mac.sandbox_verification.v1")
    assert i > 0
    heredoc_region = src[max(0, i - 6000): i + 3000]
    assert "def clip(value" in heredoc_region
    assert 'clip(stdout)' in heredoc_region
    assert "stdout[:4000]" not in heredoc_region
