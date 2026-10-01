"""Exercise report controller/child and signed independent publication boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mac import executor_sandbox as sandbox, services, worker
from mac.worker_subprocess import SubprocessExecutor
from mac.models import ValidationError
from mac.services import ControlPlane, sign_verification_manifest
from tests.test_report_repository_routing import (
    _agent,
    _marker_resources,
    _report_task,
    _signed_report_manifest,
    report_boundary_env as report_boundary_env,
)


def _approve(executor):
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert worker._apply_read_only_report_executor_approval(
        _marker_resources(attestation), os.environ
    )


def test_real_report_child_accepts_controller_name_but_not_operator_override(
    report_boundary_env, tmp_path, monkeypatch
):
    executor, _ = report_boundary_env
    script = Path(os.environ["MAC_TASK_EXECUTOR_SCRIPT"])
    source = Path(__file__).resolve().parents[1] / "src"
    script.write_text(
        "import sys, json, os\nsys.path.insert(0, " + repr(str(source)) + ")\n"
        "from mac.executor_sandbox import _read_only_report_extra_create_argv, _sandbox_name\n"
        "_read_only_report_extra_create_argv(require_approval=False)\n"
        'print(json.dumps({"name": _sandbox_name(), "fixed": os.environ.get("MAC_OPENSHELL_SANDBOX_NAME")}))\n'
    )
    executor.write_text('#!/bin/sh\nexec "$MAC_TASK_EXECUTOR_PYTHON" "$MAC_TASK_EXECUTOR_SCRIPT"\n')
    monkeypatch.setenv("MAC_TASK_EXECUTOR_PYTHON", sys.executable)
    _approve(executor)
    monkeypatch.setenv("MAC_TASK_OPENSHELL_SANDBOX_NAME", "mac-task-deadbeef")
    task = {
        "id": "task_report_child",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    }
    names = []
    for _ in range(2):
        result = SubprocessExecutor([str(executor)], timeout=10)(task, tmp_path)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["fixed"] is None
        assert data["name"].startswith("mac-task-") and len(data["name"]) == 17
        names.append(data["name"])
    assert len(set(names + ["mac-task-deadbeef"])) == 3
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "shared")
    result = SubprocessExecutor([str(executor)], timeout=10)(task, tmp_path)
    assert result.returncode != 0
    assert "fresh per-task sandbox" in result.stderr


def _inspection(tmp_path):
    repo = tmp_path / "inspection"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Report fixture")
    git("config", "user.email", "report@example.invalid")
    (repo / "state.txt").write_text("unchanged\n")
    git("add", "state.txt")
    git("commit", "-m", "base")
    return repo, git


@pytest.mark.parametrize("mutation", ["", "config", "missing"])
def test_native_hermes_dispatch_captures_controls_before_agent(
    report_boundary_env, tmp_path, monkeypatch, mutation
):
    executor, _ = report_boundary_env
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "0")
    monkeypatch.setenv("MAC_EXECUTOR_BACKEND", "hermes")
    _approve(executor)
    repo, git = _inspection(tmp_path)
    task = {
        "id": "task_native_report",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
            "runtime": {
                "repository_worktree": str(repo),
                "repository_base_sha": git("rev-parse", "HEAD"),
                "repository_base_tree": git("rev-parse", "HEAD^{tree}"),
            },
        },
    }
    task["metadata"]["runtime"].update(
        repository_refs_digest=hashlib.sha256(
            subprocess.check_output(
                ["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"]
            )
        ).hexdigest(),
        repository_content_digest=sandbox.read_only_repository_content_digest(repo),
    )
    monkeypatch.delenv("MAC_TASK_REPO_WORKTREE", raising=False)
    monkeypatch.setattr(sandbox, "_agent_argv", lambda *a, **kw: ["fixture-agent"])
    monkeypatch.setattr(sandbox, "_compile_outbound_prompt", lambda prompt, *a, **kw: prompt)
    monkeypatch.setattr(
        sandbox,
        "_write_agent_command_bundle",
        lambda *a: SimpleNamespace(argv=lambda **kw: ["fixture-agent"], cleanup=lambda: None),
    )
    monkeypatch.setattr(sandbox, "_unsandboxed_agent_argv", lambda argv, **kw: argv)
    seen = []

    def run(argv, *args):
        seen.append(True)
        if mutation == "config":
            with (repo / ".git" / "config").open("a") as f:
                f.write("\n[core]\nfsmonitor = malicious-monitor\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    if mutation == "missing":
        task["metadata"]["runtime"].pop("repository_worktree")
        with pytest.raises(RuntimeError, match="task-owned worktree"):
            sandbox._invoke_agent(run, "inspect", tmp_path, task["id"], {"task": task})
        assert not seen
        return
    result = sandbox._invoke_agent(run, "inspect", tmp_path, task["id"], {"task": task})
    assert seen and result.mac_read_only_git_control_digest
    if mutation:
        monkeypatch.setattr(
            sandbox,
            "_git_for_read_only_verifier",
            lambda *a: pytest.fail("post-agent Git ran before raw control check"),
        )
        assert (
            sandbox._read_only_report_repository_violation(
                task, result.mac_read_only_git_control_digest
            )
            == "read-only repository report Git control metadata changed"
        )
    else:
        reason = sandbox._read_only_report_repository_violation(
            task, result.mac_read_only_git_control_digest
        )
        assert reason == ""


@pytest.mark.parametrize("forged_rc", [0, 1, None])
def test_native_report_never_trusts_agent_workspace_test_result(tmp_path, monkeypatch, forged_rc):
    """A native host cannot attest to a Linux test run, and nothing downstream
    runs one for it any more, so a native report has no trustworthy result."""
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setenv("MAC_REPORT_EXECUTOR_APPROVED_PLATFORM", "darwin")
    monkeypatch.setenv("MAC_REPORT_EXECUTOR_APPROVED_ISOLATION_POSTURE", "macos_host")
    task = {
        "metadata": {
            "execution_contract": {
                "repository_contract": {"test": {"command": "run-current-tests"}}
            }
        }
    }
    (tmp_path / "mac-sandbox-verification.json").write_text(
        json.dumps({"command": "run-current-tests", "returncode": forged_rc})
    )
    item, problems = worker._trusted_read_only_report_test_item(tmp_path, task)
    assert item is None
    assert problems == ["native read-only repository reports require a Linux contract test run"]


def _deferred_test_item(command):
    """The placeholder a worker used to submit when hub-verify would run its tests."""
    return {
        "name": "repository contract test",
        "command": command,
        "returncode": None,
        "status": "deferred",
        "execution_environment": "hub_verify_pending",
        "stdout": "",
        "stderr": "",
    }


def test_report_with_deferred_contract_test_is_not_reviewable():
    """Deferred report tests were a request for hub-side verification. That
    second run no longer exists, so the evidence can never be approved: it is
    refused at the evidence gate rather than parked waiting forever."""
    cp = ControlPlane.in_memory()
    executor = _agent(cp, "executor", ["ops"], attested=True)
    task = _report_task(cp)
    task = cp.update_task(
        task.id, metadata={**task.metadata, "publication_target": "test://publish"}
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    manifest = _signed_report_manifest(cp, executor.id)
    manifest["repository_access"].update(
        canonical_remote_url="https://example.invalid/report-routing.git",
        canonical_branch="main",
        base_sha="a" * 40,
        base_tree="b" * 40,
    )
    manifest["tests"] = [_deferred_test_item("true")]
    manifest["checks"] = copy.deepcopy(manifest["tests"])
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor.id), manifest
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://report",
        "Report awaiting independent Linux tests",
        executor.id,
        metadata={"returncode": 0, "verification": manifest},
    )

    assessment = cp._assess_default_review_evidence(cp.get_task(task.id), evidence)
    assert assessment["valid"] is False
    assert assessment["reason"] == "report_contract_test_deferred"
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, executor.id)


def test_pushed_branch_gate_refuses_a_clone_at_another_head(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        output = ""
        if "rev-parse" in argv:
            output = "c" * 40 + "\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE", "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "1" * 64
    )
    monkeypatch.delenv("MAC_HUB_VERIFY_PROFILE", raising=False)
    monkeypatch.delenv("MAC_OPENSHELL_GC", raising=False)
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\n")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(policy))
    rc, output = services.run_repository_contract_test_in_openshell(
        "https://example.invalid/repo.git",
        "main",
        "a" * 40,
        "run-current-tests",
    )
    assert rc != 0 and "HEAD mismatch" in output
    assert not any("fetch" in call or "create" in call for call in calls)


def test_generated_name_remains_the_outer_timeout_cleanup_target(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX_NAME", raising=False)
    monkeypatch.setenv("MAC_TASK_OPENSHELL_SANDBOX_NAME", "mac-task-deadbeef")
    names = []
    monkeypatch.setattr(
        "mac.worker_subprocess._cleanup_task_sandbox_after_timeout",
        lambda name, workspace: names.append(name) or {"harvested": True, "deleted": True},
    )
    script = 'import os,time; print(os.environ["MAC_TASK_OPENSHELL_SANDBOX_NAME"], flush=True); time.sleep(10)'
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        SubprocessExecutor([sys.executable, "-c", script], timeout=1)(
            {"id": "task_timeout_identity", "metadata": {}}, tmp_path
        )
    output = caught.value.output
    if isinstance(output, bytes):
        output = output.decode()
    assert names == [output.strip()]
    assert names[0] != "mac-task-deadbeef"
    assert caught.value.process_tree_terminated is True
