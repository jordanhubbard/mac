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


@pytest.mark.parametrize("hub_enabled", [True, False])
@pytest.mark.parametrize("forged_rc", [0, 1, None])
def test_native_report_never_trusts_agent_workspace_test_result(
    tmp_path, monkeypatch, hub_enabled, forged_rc
):
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setenv("MAC_REPORT_EXECUTOR_APPROVED_PLATFORM", "darwin")
    monkeypatch.setenv("MAC_REPORT_EXECUTOR_APPROVED_ISOLATION_POSTURE", "macos_host")
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", str(int(hub_enabled)))
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
    if hub_enabled:
        assert not problems
        assert item == worker._hub_verify_deferred_test_item("run-current-tests")
    else:
        assert item is None and problems


def _pending_report(monkeypatch, runner):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    monkeypatch.setenv("MAC_REVIEW_SEMANTIC_REVIEWER", "0")
    cp = ControlPlane.in_memory()
    executor = _agent(cp, "executor", ["ops"], attested=True)
    reviewer = _agent(cp, "reviewer", ["review"], attested=True)
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
    manifest["tests"] = [worker._hub_verify_deferred_test_item("true")]
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
    cp.submit_for_review(task.id, executor.id)
    cp._hub_verify_runner = runner
    return cp, task, evidence, reviewer


@pytest.mark.parametrize("returncode", [0, 1])
def test_pending_native_report_publication_follows_signed_linux_verdict(monkeypatch, returncode):
    calls = []
    cp, task, evidence, reviewer = _pending_report(
        monkeypatch, lambda *args: calls.append(args) or (returncode, "contract tests finished")
    )
    assert cp._assess_default_review_evidence(task, evidence)["valid"] is True
    results = [cp.advance_default_review_workflow(task.id) for _ in range(2)]
    assert calls == [("https://example.invalid/report-routing.git", "main", "a" * 40, "true")]
    assert (cp.get_task(task.id).state == "completed") == (returncode == 0), results
    verdicts = [e for e in cp.list_evidence(task.id) if e.metadata.get("hub_verified")]
    assert len(verdicts) == 1
    manifest = verdicts[0].metadata["verification"]
    assert "repo" not in manifest
    assert manifest["repository_access"]["base_sha"] == "a" * 40
    assert manifest["tests"][0]["returncode"] == returncode
    assert manifest["tests"][0]["execution_environment"] == "openshell_sandbox"


def test_pending_native_report_waits_for_unavailable_verifier(monkeypatch):
    def unavailable(*args):
        raise TimeoutError("verifier unavailable")

    cp, task, evidence, reviewer = _pending_report(monkeypatch, unavailable)
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_hub_verify"
    assert cp.get_task(task.id).state != "completed"
    assert not any(e.metadata.get("hub_verified") for e in cp.list_evidence(task.id))


@pytest.mark.parametrize("changed", ["remote", "branch", "sha", "command", "receipt"])
def test_pending_report_contract_mismatch_cannot_autoapprove(monkeypatch, changed):
    cp, task, evidence, reviewer = _pending_report(
        monkeypatch, lambda *args: pytest.fail("invalid contract ran")
    )
    manifest = copy.deepcopy(evidence.metadata["verification"])
    if changed == "remote":
        manifest["repository_access"]["canonical_remote_url"] = "https://example.invalid/other.git"
    if changed == "branch":
        manifest["repository_access"]["canonical_branch"] = "other"
    if changed == "sha":
        manifest["repository_access"]["base_sha"] = "invalid"
    if changed == "command":
        manifest["tests"][0]["command"] = "old-tests"
    if changed == "receipt":
        manifest["tests"][0]["returncode"] = 0
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(evidence.created_by), manifest
    )
    evidence.metadata["verification"] = manifest
    assert cp._hub_verify_repo_info(task, evidence) is None
    assert cp._assess_default_review_evidence(task, evidence)["valid"] is False


@pytest.mark.parametrize("is_report", [True, False])
def test_hub_fetches_prepared_report_base_without_weakening_pushed_head(
    monkeypatch, tmp_path, is_report
):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        output = ""
        if "rev-parse" in argv:
            fetched = any("fetch" in call for call in calls)
            output = ("a" if fetched else "c") * 40 + "\n"
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
    identity = {}
    kwargs = (
        {"prepared_report": {"base_tree": "b" * 40}, "verifier_identity": identity}
        if is_report
        else {}
    )
    rc, output = ControlPlane._hub_verify_run_contract_test(
        SimpleNamespace(),
        "https://example.invalid/repo.git",
        "main",
        "a" * 40,
        "run-current-tests",
        **kwargs,
    )
    if is_report:
        assert rc == 0, output
        assert identity["platform"] == "linux"
        assert identity["policy_sha256"].startswith("sha256:")
        command = next(argv[-1] for argv in calls if "exec" in argv and "tar xzf repo.tgz" in argv[-1])
        assert "uname -s" in command and "git rev-parse HEAD^{tree}" in command
        assert command.index("git rev-parse HEAD^{tree}") < command.index("run-current-tests")
    else:
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


@pytest.mark.parametrize("fault", ["generic", "command", "source", "failed"])
def test_signed_generic_or_mismatched_verdict_cannot_publish_pending_report(monkeypatch, fault):
    def unavailable(*args):
        raise TimeoutError("still pending")

    cp, task, executor_evidence, _ = _pending_report(monkeypatch, unavailable)
    advanced = cp.advance_default_review_workflow(task.id)
    reviewer_id = advanced["reviewer_agent_id"]
    info = cp._hub_verify_repo_info(task, executor_evidence)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": executor_evidence.id,
        "review_id": advanced["review_id"],
        "worktree_digest": "sha256:" + "0" * 64,
        "verified_by": "hub_review_verifier_v1",
        "repository_access": copy.deepcopy(info["repository_access"]),
        "tests": [
            {
                "name": "hub contract verification",
                "command": "true",
                "returncode": 0,
                "status": "pass",
                "execution_environment": "openshell_sandbox",
            }
        ],
        "signed_by": reviewer_id,
    }
    if fault == "generic":
        manifest["verified_by"] = "default-review-evidence-v1"
    elif fault == "command":
        manifest["tests"][0]["command"] = "unrelated-check"
    elif fault == "source":
        manifest["repository_access"]["base_sha"] = "c" * 40
    else:
        manifest["tests"][0]["returncode"] = 1
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(reviewer_id), manifest
    )
    verdict = cp.add_evidence(
        task.id,
        "review",
        "artifact://invalid-verdict",
        "Invalid review fixture",
        reviewer_id,
        metadata={"returncode": 0, "verification": manifest, "hub_verified": True},
    )
    found, problems = cp._find_review_verdict_evidence(
        task.id,
        reviewer_id,
        executor_evidence_id=executor_evidence.id,
        verdict_evidence_id=verdict.id,
    )
    assert found is None and any("independent report contract" in problem for problem in problems)
    assert cp.get_task(task.id).state != "completed"
