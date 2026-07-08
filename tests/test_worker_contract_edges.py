"""Coverage for worker verification and operating-system boundaries."""

from __future__ import annotations

import json
import subprocess

import pytest

from mac import worker


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_execution_environment_summary_collects_bootstrap_and_deltas(tmp_path) -> None:
    assert worker.MacWorker._execution_env_summary(object(), tmp_path) == ""
    (tmp_path / "mac-evidence.json").write_text("not-json")
    assert worker.MacWorker._execution_env_summary(object(), tmp_path) == ""
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(
            {
                "bootstrap": {"command": "uv sync", "returncode": 2},
                "tests": [
                    "invalid",
                    {"environment_delta": "invalid"},
                    {
                        "environment_delta": {
                            "installed": ["one", "two"],
                            "added": ["three"],
                            "missing": ["four"],
                            "missing_commands": ["five"],
                        }
                    },
                ],
            }
        )
    )
    assert worker.MacWorker._execution_env_summary(object(), tmp_path) == (
        "bootstrap: uv sync (rc 2); installed: one, two; added: three; missing: four"
    )


def test_verification_contract_dispatches_all_evidence_types(monkeypatch) -> None:
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda _manifest: ["cg"])
    sha = "a" * 40
    anchor = {
        "repo": {
            "head_sha": sha,
            "dirty": False,
            "pushed": True,
            "remote_ref": "refs/heads/task",
            "files_changed": ["src/a.py"],
        },
        "tests": [{"returncode": 0}],
    }
    assert worker._worker_verification_contract_problems(anchor, "repo_change") == ["cg"]
    assert worker._worker_verification_contract_problems(anchor, "documentation") == ["cg"]

    deployment = worker._worker_verification_contract_problems({}, "deployment")
    assert "deployment evidence requires at least one passing check" in deployment
    assert "deployment evidence requires targets, services, or artifacts" in deployment
    assert "cg" in deployment

    test_problems = worker._worker_verification_contract_problems({}, "test")
    assert "test evidence requires at least one passing check or test" in test_problems
    artifact = worker._worker_verification_contract_problems({}, "artifact")
    assert "artifact evidence requires artifacts" in artifact
    no_change = worker._worker_verification_contract_problems({}, "no_change")
    assert "no_change evidence requires a reason" in no_change
    assert worker._worker_verification_contract_problems({}, "review_verdict") == ["cg"]
    assert worker._worker_verification_contract_problems({}, "unknown") == [
        "unsupported verification.evidence_type: unknown"
    ]


def test_operator_result_verification_substance_paths() -> None:
    assert worker._worker_verification_contract_problems(
        {"artifacts": [{"uri": "x"}]}, "operator_result"
    ) == []
    assert worker._worker_verification_contract_problems(
        {"findings": [{"summary": "x"}]}, "operator_result"
    ) == []
    assert "requires summary" in worker._worker_verification_contract_problems(
        {}, "operator_result"
    )[0]
    assert "not substantive" in worker._worker_verification_contract_problems(
        {"summary": "hello hello hello"}, "operator_result"
    )[0]
    assert worker._worker_verification_contract_problems(
        {"summary": "Analyzed the rollout failures and documented three concrete fixes."},
        "operator_result",
    ) == []


def test_executor_verification_manifest_shapes(tmp_path) -> None:
    path = tmp_path / "executor-evidence.json"
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path) == {}
    path.write_text("not-json")
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path) == {}
    path.write_text("[]")
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path) == {}
    path.write_text(json.dumps({"metadata": {"verification": {"repo": {"head_sha": "a"}}}}))
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path)["repo"] == {
        "head_sha": "a"
    }
    path.write_text(json.dumps({"verification": {"repo": {"head_sha": "b"}}}))
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path)["repo"] == {
        "head_sha": "b"
    }


def test_prepare_review_workspace_hides_executor_treatment_from_blind_pass(
    monkeypatch, tmp_path
) -> None:
    instance = object.__new__(worker.MacWorker)
    instance.workspace = tmp_path
    monkeypatch.setattr(
        instance,
        "_prepare_review_repository_worktree",
        lambda *_args: {
            "schema": "mac.review_repository_worktree.v1",
            "repository_worktree": "/review/repo",
            "repository_base_sha": "a" * 40,
            "repository_reviewed_head_sha": "b" * 40,
        },
    )
    task_detail = {
        "task": {
            "id": "task_1",
            "title": "Review safely",
            "description": "Preserve the original acceptance criteria.",
            "state": "reviewing",
            "owner_agent_id": "executor-agent",
            "metadata": {
                "custom_acceptance": {"must_preserve": True},
                "model": "executor/model",
                "review_model": "reviewer/model",
                "activity": [{"summary": "executor changed secret.py"}],
                "latest_review_claim": {"tests": [{"status": "pass"}]},
                "review_claims": {"review_1": {"repository_files_changed": ["secret.py"]}},
                "runtime": {"repository_head_sha": "b" * 40},
                "target_agent_id": "executor-agent",
            },
        },
        "evidence": [{"id": "ev_1", "metadata": {"verification": {"status": "complete"}}}],
    }
    claim = {
        "claim": {
            "schema": "mac.review_claim.detail.v1",
            "task_id": "task_1",
            "review_id": "review_1",
            "reviewer_agent_id": "reviewer-agent",
            "executor_evidence_id": "ev_1",
            "checks": [{"name": "tests", "status": "pass"}],
            "repository_files_changed": ["secret.py"],
            "work_summary": "executor explanation",
        }
    }

    task_dir = instance._prepare_review_workspace(
        "task_1", "review_1", "ev_1", task_detail, {"id": "msg_1"}, claim
    )

    original = json.loads((task_dir / "executor-task.json").read_text())
    review_task = json.loads((task_dir / "task.json").read_text())["task"]
    serialized = json.dumps({"original": original, "review": review_task})
    assert original["metadata"]["custom_acceptance"] == {"must_preserve": True}
    assert review_task["metadata"]["review_model"] == "reviewer/model"
    assert review_task["metadata"]["review_context"]["review_claim"] == {
        "executor_evidence_id": "ev_1",
        "review_id": "review_1",
        "reviewer_agent_id": "reviewer-agent",
        "schema": "mac.review_claim.detail.v1",
        "task_id": "task_1",
    }
    assert "executor/model" not in serialized
    assert "secret.py" not in serialized
    assert "executor explanation" not in serialized
    assert "latest_review_claim" not in serialized


def test_task_iteration_override_separates_executor_and_reviewer_budgets() -> None:
    metadata = {"max_iterations": 30, "review_max_iterations": "12"}

    assert worker._task_iteration_override({"metadata": metadata}) == 30
    assert worker._task_iteration_override(
        {"metadata": {**metadata, "review_context": {"review_id": "review_1"}}}
    ) == 12
    assert worker._task_iteration_override(
        {"metadata": {"max_iterations": 0}}
    ) is None
    assert worker._task_iteration_override(
        {"metadata": {"max_iterations": 501}}
    ) is None


def test_subprocess_executor_exports_task_iteration_budget(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    execution = worker.SubprocessExecutor(["executor"])(
        {"id": "task_1", "metadata": {"max_iterations": 12}}, tmp_path
    )

    assert execution.returncode == 0
    assert captured["env"]["MAC_TASK_MAX_ITERATIONS"] == "12"


def test_subprocess_executor_does_not_inherit_task_scoped_overrides(
    monkeypatch, tmp_path
) -> None:
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setenv("MAC_TASK_MODEL", "stale/model")
    monkeypatch.setenv("MAC_TASK_MAX_ITERATIONS", "999")
    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    worker.SubprocessExecutor(["executor"])(
        {"id": "task_unpinned", "metadata": {}}, tmp_path
    )

    assert "MAC_TASK_MODEL" not in captured["env"]
    assert "MAC_TASK_MAX_ITERATIONS" not in captured["env"]


def test_review_verdict_compares_executor_changed_files(monkeypatch, tmp_path) -> None:
    assert worker._worker_review_verdict_executor_repo_problems(tmp_path, {}) == []
    (tmp_path / "executor-evidence.json").write_text(
        json.dumps({"verification": {"repo": {"files_changed": ["./src//a.py", "b.py"]}}})
    )
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda manifest: [manifest["repo"]["files_changed"]])
    problems = worker._worker_review_verdict_executor_repo_problems(
        tmp_path, {"repo": {"files_changed": ["other.py"]}}
    )
    assert "must match executor evidence" in problems[0]
    assert problems[1] == ["src/a.py", "b.py"]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ([{"status": "pass"}], True),
        ("invalid", False),
        ({"returncode": 0}, True),
        ({"returncode": "bad"}, False),
        ({"failed": 1, "status": "pass"}, False),
        ({"result": "successful"}, True),
        ({"passed": True}, True),
        ({"success": 2, "failed": 0}, True),
        ({"ok": True, "satisfied": True}, True),
        ({"nested": {"outcome": "ok"}}, True),
        ({"status": "fail"}, False),
    ],
)
def test_worker_verification_item_passed_shapes(item, expected: bool) -> None:
    assert worker._worker_verification_item_passed(item) is expected


def test_worker_repo_anchor_and_empty_change_exceptions(monkeypatch) -> None:
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda _manifest: [])
    missing = worker._worker_require_pushed_repo_anchor({})
    assert missing == ["repo evidence requires verification.repo object"]
    malformed = worker._worker_require_pushed_repo_anchor(
        {"repo": {"head_sha": "short", "dirty": True, "pushed": False}}
    )
    assert len(malformed) == 3
    assert worker._worker_require_pushed_repo_anchor(
        {"repo": {"head_sha": "a" * 40, "dirty": "0", "pr_url": "https://pr"}}
    ) == []
    assert worker._worker_allows_empty_repo_change_evidence({}, "documentation") is False
    assert worker._worker_allows_empty_repo_change_evidence({"metadata": []}, "repo_change") is False
    assert worker._worker_allows_empty_repo_change_evidence(
        {"metadata": {"origin": {"type": "beads_source_remediation"}}}, "repo_change"
    ) is True
    assert worker._worker_allows_empty_repo_change_evidence(
        {"metadata": {"remediation": {"type": "beads_source_refresh"}}}, "repo_change"
    ) is True


def test_path_and_bound_normalizers() -> None:
    assert worker._normalize_restart_services(None) == []
    assert worker._normalize_restart_services(["a.service", "", "a.service", "b-agent.service"]) == [
        "a.service",
        "b-agent.service",
    ]
    with pytest.raises(ValueError, match="invalid"):
        worker._normalize_restart_services("-bad.service")
    assert worker._bounded_int("bad", 1, 10, 5) == 5
    assert worker._bounded_int(99, 1, 10, 5) == 10
    assert worker._bounded_float("bad", 1.0, 10.0, 5.0) == 5.0
    assert worker._bounded_float(-1, 1.0, 10.0, 5.0) == 1.0
    assert worker._manifest_list(None) == []
    assert worker._manifest_list("x") == ["x"]
    assert worker._metadata_path_list("./src//a.py") == ["src/a.py"]
    assert worker._metadata_path_list(42) == []
    assert worker._nested_dict({"a": []}, "a", "b") == {}
    assert worker._repo_path_satisfies_requirement("src/a.py", "src/*.py") is True
    assert worker._repo_path_satisfies_requirement("", "src/*.py") is False


def test_repository_head_push_checks_remote_url_origin_and_branch(monkeypatch, tmp_path) -> None:
    assert worker._repository_context_head_is_pushed(tmp_path, {}) is False
    head = "a" * 40
    calls = []

    def run_git(_repo, args):
        calls.append(args)
        if args[:2] == ["ls-remote", "https://repo"]:
            return _completed(0, "%s\trefs/heads/task\n" % head)
        return _completed(1)

    monkeypatch.setattr(worker, "_run_git", run_git)
    assert worker._repository_context_head_is_pushed(
        tmp_path,
        {"head_sha": head, "remote_url": "https://repo", "remote_ref": "refs/heads/task"},
    ) is True

    monkeypatch.setattr(
        worker,
        "_run_git",
        lambda _repo, args: _completed(0, head + "\n") if args[0] == "rev-parse" else _completed(1),
    )
    assert worker._repository_context_head_is_pushed(
        tmp_path, {"head_sha": head, "remote_ref": "refs/heads/task"}
    ) is True


def test_restart_systemd_service_result_matrix(monkeypatch) -> None:
    assert worker._restart_systemd_service("../bad") ["status"] == "error"
    assert worker._restart_systemd_service("mac-agent.service")["status"] == "skipped"
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)
    assert worker._restart_systemd_service("demo.service")["reason"] == "systemctl not found"

    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/bin/systemctl")
    monkeypatch.setenv("MAC_SELF_UPDATE_SERVICE_TIMEOUT", "bad")
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("inspect failed")),
    )
    assert worker._restart_systemd_service("demo.service")["error"] == "inspect failed"

    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: _completed(4, "", "denied"))
    assert worker._restart_systemd_service("demo.service")["returncode"] == 4
    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: _completed(0, "not-found\n"))
    assert worker._restart_systemd_service("demo.service")["reason"] == "service not installed"

    calls = []

    def run_success(argv, **_kwargs):
        calls.append(argv)
        return _completed(0, "loaded\n" if "show" in argv else "restarted")

    monkeypatch.setattr(worker.subprocess, "run", run_success)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    result = worker._restart_systemd_service("demo.service")
    assert result["status"] == "restarted"
    assert result["command"] == ["systemctl", "restart", "demo.service"]

    def run_restart_error(argv, **_kwargs):
        if "show" in argv:
            return _completed(0, "loaded\n")
        raise OSError("restart failed")

    monkeypatch.setattr(worker.subprocess, "run", run_restart_error)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    result = worker._restart_systemd_service("demo.service")
    assert result["status"] == "error"
    assert result["command"][:3] == ["sudo", "-n", "systemctl"]


def test_run_git_timeout_fallbacks(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setenv("MAC_SELF_UPDATE_GIT_TIMEOUT", "bad")
    monkeypatch.setattr(worker.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or _completed())
    worker._run_git(tmp_path, ["status"])
    worker._run_git_in(tmp_path, ["clone", "x"])
    assert calls[0][1]["timeout"] == 120.0
    assert calls[1][1]["timeout"] == 120.0


# ---------------------------------------------------------------------------
# hub-verify deferred mode: _repository_finalizer_prepush_problems
# ---------------------------------------------------------------------------


def _valid_repo(sha: str = "a" * 40) -> dict:
    """Minimal repo snapshot that passes all structural prepush checks."""
    return {
        "head_sha": sha,
        "dirty": False,
        "files_changed": ["src/feature.py"],
    }


def test_finalizer_prepush_no_problems_when_hub_verify_and_deferred_item() -> None:
    """When MAC_REVIEW_HUB_VERIFY=1 and the test item is the deferred sentinel,
    _repository_finalizer_prepush_problems must return NO test-failure problems.
    The hub finalizer will run the contract test after the branch is pushed; the
    worker must not block on a missing local test result."""
    deferred_item = worker._hub_verify_deferred_test_item("scripts/run-contract-tests.sh")
    assert worker._is_hub_verify_deferred_item(deferred_item)

    problems = worker._repository_finalizer_prepush_problems(
        {},
        _valid_repo(),
        deferred_item,
        hub_verify=True,
    )
    test_gate_problems = [p for p in problems if "passing test" in p]
    assert not test_gate_problems, (
        "hub-verify deferred mode must skip the passing-test gate; "
        "got unexpected problems: %s" % test_gate_problems
    )


def test_finalizer_prepush_blocks_when_hub_verify_off_and_no_sandbox_result(tmp_path) -> None:
    """Option A (MAC_REVIEW_HUB_VERIFY unset): when no mac-sandbox-verification.json
    is present, the sandbox helper returns None → caller falls back to running the
    contract test locally.  If the local run also fails, the prepush gate must block
    with a test-failure problem."""
    # Simulate a failing/missing local test by building a fail-status item directly.
    fail_item = {
        "name": "repository contract test",
        "command": "scripts/run-contract-tests.sh",
        "returncode": 1,
        "status": "fail",
        "stdout": "",
        "stderr": "3 failed",
    }
    problems = worker._repository_finalizer_prepush_problems(
        {},
        _valid_repo(),
        fail_item,
        hub_verify=False,
    )
    assert any("passing test" in p for p in problems), (
        "Option A: a failing test item must produce a blocking problem when hub_verify=False; "
        "got problems: %s" % problems
    )


def test_sandbox_repository_verification_item_returns_deferred_when_hub_verify_and_no_file(
    tmp_path,
) -> None:
    """_sandbox_repository_verification_item must return the deferred sentinel when
    hub_verify=True and mac-sandbox-verification.json is absent — not None."""
    command = "scripts/run-contract-tests.sh"
    item = worker._sandbox_repository_verification_item(tmp_path, command, hub_verify=True)
    assert item is not None, "expected deferred sentinel, got None"
    assert worker._is_hub_verify_deferred_item(item), (
        "expected deferred sentinel, got: %s" % item
    )
    assert item["command"] == command


def test_sandbox_repository_verification_item_returns_none_when_hub_verify_off_and_no_file(
    tmp_path,
) -> None:
    """Option A: with hub_verify=False and no sandbox file, the helper returns None
    so the worker falls back to running the contract test locally."""
    item = worker._sandbox_repository_verification_item(
        tmp_path, "scripts/run-contract-tests.sh", hub_verify=False
    )
    assert item is None, "Option A: expected None when no sandbox file and hub_verify=False"
