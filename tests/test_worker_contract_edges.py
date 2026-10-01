"""Worker verification contracts that affect publish, review, and prepush."""

from __future__ import annotations

import json
import subprocess

from mac import worker


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_verification_contract_dispatches_all_evidence_types() -> None:
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
    assert worker._worker_verification_contract_problems(anchor, "repo_change") == []
    assert worker._worker_verification_contract_problems(anchor, "documentation") == []

    deployment = worker._worker_verification_contract_problems({}, "deployment")
    assert "deployment evidence requires at least one passing check" in deployment
    assert "deployment evidence requires targets, services, or artifacts" in deployment

    test_problems = worker._worker_verification_contract_problems({}, "test")
    assert "test evidence requires at least one passing check or test" in test_problems
    artifact = worker._worker_verification_contract_problems({}, "artifact")
    assert "artifact evidence requires artifacts" in artifact
    no_change = worker._worker_verification_contract_problems({}, "no_change")
    assert "no_change evidence requires a reason" in no_change
    local_no_change = {
        "reason": "The requested behavior is already present.",
        "repo": {
            "head_sha": sha,
            "dirty": False,
            "pushed": False,
            "files_changed": [],
        },
        "tests": [{"returncode": 0}],
    }
    assert worker._worker_verification_contract_problems(local_no_change, "no_change") == []
    assert worker._worker_verification_contract_problems({}, "review_verdict") == []
    assert worker._worker_verification_contract_problems({}, "unknown") == [
        "unsupported verification.evidence_type: unknown"
    ]


def test_operator_result_verification_substance_paths() -> None:
    assert (
        worker._worker_verification_contract_problems(
            {"artifacts": [{"uri": "x"}]}, "operator_result"
        )
        == []
    )
    assert (
        worker._worker_verification_contract_problems(
            {"findings": [{"summary": "x"}]}, "operator_result"
        )
        == []
    )
    assert (
        "requires summary"
        in worker._worker_verification_contract_problems({}, "operator_result")[0]
    )
    assert (
        "not substantive"
        in worker._worker_verification_contract_problems(
            {"summary": "hello hello hello"}, "operator_result"
        )[0]
    )
    assert (
        worker._worker_verification_contract_problems(
            {"summary": "Analyzed the rollout failures and documented three concrete fixes."},
            "operator_result",
        )
        == []
    )
    assert (
        worker._worker_verification_contract_problems(
            {
                "summary": "Established ground truth and documented the evidence gap.",
                "findings": [{"status": "not_actionable"}],
            },
            "investigation",
        )
        == []
    )
    assert (
        worker._worker_verification_contract_problems(
            {
                "children": [{"title": "Inspect"}, {"title": "Repair"}],
                "ordering_rationale": "Inspect before repair.",
                "coverage_claim": "Diagnosis and repair cover the parent scope.",
            },
            "plan_decomposed",
        )
        == []
    )


def test_execute_assignment_routes_plan_to_durable_children(tmp_path) -> None:
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "plan_decomposed",
        "children": [{"title": "Inspect"}, {"title": "Repair"}],
        "ordering_rationale": "Inspect before repair.",
        "coverage_claim": "Diagnosis and repair cover the parent scope.",
        "signed_by": "agent-planner",
        "signature": "test-signature",
    }

    class Client:
        def __init__(self):
            self.posts = []

        def post(self, path, payload):
            self.posts.append((path, payload))
            if path.endswith("/children"):
                return {"parent": {"id": "task-plan", "state": "waiting"}}
            return {}

    class Harness:
        execute_assignment = worker.MacWorker.execute_assignment
        agent_id = "agent-planner"
        lease_seconds = 0
        lease_renew_interval_seconds = 0

        def __init__(self):
            self.client = Client()

        def _observe_log(self, *args, **kwargs):
            return None

        def _observe_metric(self, *args, **kwargs):
            return None

        # execute_assignment publishes the in-flight lease for the shutdown
        # watchdog to release if the process is torn down mid-task.
        def _set_active_assignment(self, task_id, lease_id):
            return None

        def _clear_active_assignment(self, task_id, lease_id):
            return None

        def _prepare_task_workspace(self, task, lease):
            (tmp_path / "task.json").write_text(json.dumps({"task": task, "lease": lease}))
            return tmp_path

        def _execute_task(self, task, lease, task_dir):
            return worker.WorkerExecution(0, "planned")

        def _assignment_is_current(self, task_id, lease_id):
            return True

        def _record_execution(self, task_id, task_dir, execution, *, lease_id, attempt_state=None):
            return {
                "id": "evidence-plan",
                "metadata": {"verification": manifest},
            }

    harness = Harness()
    result = harness.execute_assignment(
        {"id": "task-plan", "title": "Plan work", "metadata": {}},
        {"id": "lease-plan"},
    )

    assert result.status == "decomposed"
    assert result.task == {"id": "task-plan", "state": "waiting"}
    assert result.evidence["id"] == "evidence-plan"
    child_posts = [
        (path, payload) for path, payload in harness.client.posts if path.endswith("/children")
    ]
    assert child_posts == [
        (
            "/tasks/task-plan/children",
            {
                "children": manifest["children"],
                "actor": "agent-planner",
                "lease_id": "lease-plan",
            },
        )
    ]
    assert not [path for path, _payload in harness.client.posts if "submit-for-review" in path]


def test_plan_policy_rejection_reports_verification_failure_not_environment(
    tmp_path,
) -> None:
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "plan_decomposed",
        "children": [{"title": "Inspect"}, {"title": "Repair"}],
        "ordering_rationale": "Inspect before repair.",
        "coverage_claim": "Diagnosis and repair cover the parent scope.",
    }

    class Client:
        def __init__(self):
            self.posts = []

        def post(self, path, payload):
            self.posts.append((path, payload))
            if path.endswith("/children"):
                raise worker.MacApiError(
                    "cannot add child tasks: this task did not authorise decomposition"
                )
            return {"id": "task-plan", "state": payload.get("target_state", "running")}

    class Harness:
        execute_assignment = worker.MacWorker.execute_assignment
        agent_id = "agent-planner"
        lease_seconds = 0
        lease_renew_interval_seconds = 0

        def __init__(self):
            self.client = Client()

        def _observe_log(self, *args, **kwargs):
            return None

        def _observe_metric(self, *args, **kwargs):
            return None

        def _set_active_assignment(self, task_id, lease_id):
            return None

        def _clear_active_assignment(self, task_id, lease_id):
            return None

        def _prepare_task_workspace(self, task, lease):
            (tmp_path / "task.json").write_text(json.dumps({"task": task, "lease": lease}))
            (tmp_path / "sandbox-hub-connectivity.json").write_text(
                json.dumps({"ready": True, "reason": "ready"})
            )
            return tmp_path

        def _execute_task(self, task, lease, task_dir):
            return worker.WorkerExecution(0, "planned")

        def _assignment_is_current(self, task_id, lease_id):
            return True

        def _record_execution(self, task_id, task_dir, execution, *, lease_id, attempt_state=None):
            return {"id": "evidence-plan", "metadata": {"verification": manifest}}

        def _execution_submission_problems(self, task_dir, evidence):
            return []

        def _post_task_activity(self, *args, **kwargs):
            return None

    harness = Harness()
    result = harness.execute_assignment(
        {"id": "task-plan", "title": "Plan work", "metadata": {}},
        {"id": "lease-plan"},
    )

    transitions = [
        payload for path, payload in harness.client.posts if path.endswith("/transition")
    ]
    assert transitions
    assert transitions[-1]["detail"].get("reason") != "sandbox_hub_environment_fault"
    assert "did not authorise decomposition" in str(result.error)


def test_task_iteration_override_bounds_executor_budget() -> None:
    metadata = {"max_iterations": 30, "review_max_iterations": "12"}

    assert worker._task_iteration_override({"metadata": metadata}) == 30
    assert worker._task_iteration_override({"metadata": {"max_iterations": 0}}) is None
    assert worker._task_iteration_override({"metadata": {"max_iterations": 501}}) is None


def test_subprocess_executor_exports_task_iteration_budget(monkeypatch, tmp_path) -> None:
    captured = {}

    class ImmediateProcess:
        pid = 999999
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(worker.subprocess, "Popen", ImmediateProcess)
    monkeypatch.setattr(worker, "_terminate_process_tree", lambda *_a, **_k: None)
    execution = worker.SubprocessExecutor(["executor"])(
        {"id": "task_1", "metadata": {"max_iterations": 12}}, tmp_path
    )

    assert execution.returncode == 0
    assert captured["env"]["MAC_TASK_MAX_ITERATIONS"] == "12"


def test_subprocess_executor_does_not_inherit_task_scoped_overrides(monkeypatch, tmp_path) -> None:
    captured = {}

    class ImmediateProcess:
        pid = 999999
        returncode = 0

        def __init__(self, argv, **kwargs):
            del argv
            captured["env"] = kwargs["env"]

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setenv("MAC_TASK_MODEL", "stale/model")
    monkeypatch.setenv("MAC_TASK_MAX_ITERATIONS", "999")
    monkeypatch.setattr(worker.subprocess, "Popen", ImmediateProcess)
    monkeypatch.setattr(worker, "_terminate_process_tree", lambda *_a, **_k: None)

    worker.SubprocessExecutor(["executor"])({"id": "task_unpinned", "metadata": {}}, tmp_path)

    assert "MAC_TASK_MODEL" not in captured["env"]
    assert "MAC_TASK_MAX_ITERATIONS" not in captured["env"]


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
    assert (
        worker._repository_context_head_is_pushed(
            tmp_path,
            {"head_sha": head, "remote_url": "https://repo", "remote_ref": "refs/heads/task"},
        )
        is True
    )

    monkeypatch.setattr(
        worker,
        "_run_git",
        lambda _repo, args: _completed(0, head + "\n") if args[0] == "rev-parse" else _completed(1),
    )
    assert (
        worker._repository_context_head_is_pushed(
            tmp_path, {"head_sha": head, "remote_ref": "refs/heads/task"}
        )
        is True
    )


def test_restart_systemd_service_result_matrix(monkeypatch) -> None:
    assert worker._restart_systemd_service("../bad")["status"] == "error"
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
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or _completed(),
    )
    worker._run_git(tmp_path, ["status"])
    worker._run_git_in(tmp_path, ["clone", "x"])
    assert calls[0][1]["timeout"] == 120.0
    assert calls[1][1]["timeout"] == 120.0


# ---------------------------------------------------------------------------
# pre-push gate: _repository_finalizer_prepush_problems
# ---------------------------------------------------------------------------


def _valid_repo(sha: str = "a" * 40) -> dict:
    """Minimal repo snapshot that passes all structural prepush checks."""
    return {
        "head_sha": sha,
        "dirty": False,
        "files_changed": ["src/feature.py"],
    }


def test_finalizer_prepush_blocks_a_deferred_code_test() -> None:
    """Nothing downstream runs a deferred test, so it can never authorize a push."""
    deferred_item = {
        "name": "repository contract test",
        "command": "scripts/run-contract-tests.sh",
        "returncode": None,
        "status": "deferred",
        "execution_environment": "hub_verify_pending",
    }

    problems = worker._repository_finalizer_prepush_problems({}, _valid_repo(), deferred_item)
    assert [p for p in problems if "passing test" in p]


def test_finalizer_prepush_blocks_a_failing_test() -> None:
    fail_item = {
        "name": "repository contract test",
        "command": "scripts/run-contract-tests.sh",
        "returncode": 1,
        "status": "fail",
        "stdout": "",
        "stderr": "3 failed",
    }
    problems = worker._repository_finalizer_prepush_problems({}, _valid_repo(), fail_item)
    assert any("passing test" in p for p in problems), problems


def test_sandbox_repository_verification_item_returns_none_without_a_file(tmp_path) -> None:
    """With no sandbox file the helper returns None, never a deferred placeholder."""
    item = worker._sandbox_repository_verification_item(tmp_path, "scripts/run-contract-tests.sh")
    assert item is None
