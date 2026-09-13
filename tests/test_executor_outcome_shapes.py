"""Malformed changed-file evidence must fail the contract, not the executor."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.evidence_validators import validate_evidence_type
from mac.executor_prompt import classify_outcome
from mac.hermes_adapter import MacApiClient
from mac.services import ControlPlane
from mac.worker import MacWorker, WorkerExecution, _worker_verification_contract_problems


MISSING = object()
SHAPE_PROBLEM = "repo.files_changed must be a list of non-empty path strings"


def _manifest(files_changed=MISSING):
    repo = {
        "head_sha": "a" * 40,
        "dirty": False,
        "pushed": True,
        "remote_ref": "refs/heads/task",
    }
    if files_changed is not MISSING:
        repo["files_changed"] = files_changed
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": repo,
        "tests": [{"returncode": 0}],
    }


def _outcome(tmp_path, manifest):
    (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest))
    return classify_outcome(tmp_path, {"id": "task_fixture"}, 0)


@pytest.mark.parametrize(
    "value",
    [0, 1, -1, False, True, 3.5, "", "src/a.py", {}, {"a": 1}, [""], [None], ["a", 1]],
)
def test_malformed_changed_files_fail_outcome_worker_and_hub(tmp_path, value):
    manifest = _manifest(value)
    outcome = _outcome(tmp_path, manifest)

    assert outcome["outcome"] == "failure"
    assert outcome["signals"]["files_changed"] is None
    assert outcome["signals"]["evidence_problem"] == SHAPE_PROBLEM
    assert outcome["error_signature"] == "verification_contract_failed: " + SHAPE_PROBLEM
    assert SHAPE_PROBLEM in _worker_verification_contract_problems(manifest, "repo_change")
    assert SHAPE_PROBLEM in validate_evidence_type(
        "repo_change", manifest, passed_check_count=lambda _manifest: 1
    )


@pytest.mark.parametrize("value", [MISSING, None, []], ids=["missing", "null", "empty"])
def test_repo_change_without_paths_is_not_classified_as_success(tmp_path, value):
    manifest = _manifest(value)
    outcome = _outcome(tmp_path, manifest)

    assert outcome["outcome"] == "failure"
    assert outcome["signals"]["files_changed"] == 0
    assert "repo evidence requires changed files" in outcome["error_signature"]
    assert "repo evidence requires changed files" in _worker_verification_contract_problems(
        manifest, "repo_change"
    )


@pytest.mark.parametrize("legacy_test_dict", [False, True])
def test_valid_paths_and_legacy_test_result_keep_their_meaning(tmp_path, legacy_test_dict):
    manifest = _manifest(["src/a.py", "docs/a file.md"])
    if legacy_test_dict:
        manifest["tests"] = {"returncode": 0}
    outcome = _outcome(tmp_path, manifest)

    assert outcome["outcome"] == "success"
    assert outcome["signals"]["files_changed"] == 2
    assert outcome["signals"]["tests"] == "pass"
    assert _worker_verification_contract_problems(manifest, "repo_change") == []
    assert (
        validate_evidence_type("repo_change", manifest, passed_check_count=lambda _manifest: 1)
        == []
    )


@pytest.mark.parametrize("repo", [MISSING, {}, {"pushed": True, "files_changed": None}])
def test_report_only_evidence_does_not_require_changed_files(tmp_path, repo):
    manifest = {
        "evidence_type": "operator_result",
        "summary": "Compared the deployment records and documented the remaining work.",
    }
    if repo is not MISSING:
        manifest["repo"] = repo
    outcome = _outcome(tmp_path, manifest)

    assert outcome["outcome"] == "success"
    assert "evidence_problem" not in outcome["signals"]


@pytest.mark.parametrize("value", [0, 2, "src/a.py", ["a", 1]])
def test_worker_routes_bad_changed_files_to_contract_failure(tmp_path, value):
    cp = ControlPlane.in_memory()
    agent = cp.register_agent(cp.register_machine("h").id, "worker", capabilities=["python"])
    task = cp.create_task("Evidence shape fixture", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def transport(method, path, payload):
        response = client.request(method, path, json=payload)
        response.raise_for_status()
        return response.json() if response.content else None

    def executor(task_payload, task_dir):
        manifest = _manifest(value)
        (task_dir / "mac-evidence.json").write_text(json.dumps(manifest))
        # Exercise the same memory-feed boundary that previously raised after
        # execution. A legacy startup warning must not turn a shape failure
        # into an infrastructure diagnosis.
        outcome = classify_outcome(task_dir, task_payload, 0)
        assert outcome["outcome"] == "failure"
        return WorkerExecution(
            0,
            "Executor returned evidence",
            stderr="WARNING: launching without an OpenShell sandbox\n",
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=transport),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    result = worker.run_once()

    assert result.status == "blocked"
    assert SHAPE_PROBLEM in result.error
    event = next(
        event for event in reversed(cp.task_history(task.id)) if event.to_state == "blocked"
    )
    assert event.detail["reason"] == "verification_contract_failed"
    assert "executor_execution_boundary_unavailable" not in json.dumps(event.detail)
