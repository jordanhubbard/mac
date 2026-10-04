"""A repository gate that ran and failed at submission is retryable work.

Live 2026-10-03/04 (Aviation task_6c9f4ab2, nanolang task_2739cdd5 and
task_44cc86fa): the pre-push gate ran on the agent's head and one test failed.
The worker reported verification_contract_failed with manual repair, so the
task went BLOCKED and then FAILED on attempt 1 of 3. A red gate is the agent's
work, not invalid evidence: it consumes an attempt and retries with the gate
output. Structurally invalid evidence still needs manual repair.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.evidence_validators import (
    GATE_FAILURE_MAX_CHARS,
    GATE_FAILURE_MAX_LINES,
    repository_gate_failure,
)
from mac.executor_prompt import build_task_prompt
from mac.hermes_adapter import MacApiClient
from mac.models import TaskState
from mac.services import ControlPlane, _blocked_attempt_retry_kind
from mac.worker import MacWorker, WorkerExecution
from tests.conftest import verifier_test_item


HEAD = "a" * 40
GATE_OUTPUT = (
    "running 14 tests\n"
    "ok 1 - test_parser\n"
    "FAILED tests/test_vm.py::test_closure_capture - AssertionError: assert 3 == 4\n"
    "export GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123\n"
    "make: *** [test-quick] Error 1\n"
)
VERIFIER_PROBLEM = (
    "repo_change evidence requires a repository verifier test result for "
    "repo.head_sha; none qualifies (tests[0] (repository bootstrap and test gate): "
    "status is 'fail', not 'pass'; returncode is 1, not 0)"
)
NOT_PUSHED = "repo evidence requires pushed=true with remote_ref, or pr_url"
NO_PASS = "repo code evidence requires at least one passing test/check"


def _failed_gate_item(**overrides):
    item = verifier_test_item(
        HEAD,
        name="repository bootstrap and test gate",
        command="make bootstrap && make test-quick",
        returncode=1,
        status="fail",
        stdout=GATE_OUTPUT,
        test_count=14,
    )
    item.update(overrides)
    return item


def _manifest(*, gate_failed=True, **item_overrides):
    pushed = not gate_failed
    repo = {
        "head_sha": HEAD,
        "dirty": False,
        "pushed": pushed,
        "files_changed": ["src/vm.c"],
    }
    if pushed:
        repo["remote_ref"] = "refs/heads/mac/task"
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Fixed closure capture in the VM.",
        "repo": repo,
        "tests": [_failed_gate_item(**item_overrides) if gate_failed else verifier_test_item(HEAD)],
    }


def _gate_detail(attempt_output=GATE_OUTPUT):
    manifest = _manifest(stdout=attempt_output)
    return {
        "reason": "repository_gate_failed",
        "failure": "repository_gate_failed",
        "manual_repair_required": False,
        "problems": [NOT_PUSHED, NO_PASS, VERIFIER_PROBLEM],
        "repository_gate_failure": repository_gate_failure(
            manifest, [NOT_PUSHED, NO_PASS, VERIFIER_PROBLEM]
        ),
    }


def _block_attempt(cp, task_id, attempt, detail, actor="agent_worker"):
    cp._transition_task_internal(task_id, TaskState.BLOCKED.value, actor, detail)
    # The retry backs off from the block time; move it into the past.
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (attempt, "2000-01-01T00:00:00+00:00", task_id),
    )


def _worker(cp, tmp_path, executor):
    agent = cp.register_agent(cp.register_machine("h").id, "worker", capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def transport(method, path, payload):
        response = client.request(method, path, json=payload)
        response.raise_for_status()
        return response.json() if response.content else None

    return MacWorker(
        MacApiClient("http://mac.test", transport=transport),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )


def _gate_task(cp, max_attempts=3):
    return cp.create_task(
        "Fix closure capture",
        required_capabilities=["python"],
        max_attempts=max_attempts,
        metadata={"repository_contract": {"test": {"command": "make test-quick"}}},
    )


def test_ran_and_failed_gate_is_extracted_bounded_and_scrubbed():
    failure = repository_gate_failure(_manifest(), [NOT_PUSHED, NO_PASS, VERIFIER_PROBLEM])

    assert failure is not None
    assert failure["returncode"] == 1
    assert failure["head_sha"] == HEAD
    assert failure["name"] == "repository bootstrap and test gate"
    assert (
        "FAILED tests/test_vm.py::test_closure_capture - AssertionError: assert 3 == 4"
        in failure["failing_lines"]
    )
    assert "make: *** [test-quick] Error 1" in failure["failing_lines"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in json.dumps(failure)

    noisy = "\n".join("line %d %s" % (index, "x" * 200) for index in range(500))
    bounded = repository_gate_failure(
        _manifest(stdout=noisy), [NOT_PUSHED, NO_PASS, VERIFIER_PROBLEM]
    )
    assert len(bounded["output_tail"]) <= GATE_FAILURE_MAX_CHARS
    assert len(bounded["output_tail"].splitlines()) <= GATE_FAILURE_MAX_LINES
    assert bounded["output_tail"].endswith("line 499 " + "x" * 200)


def test_structurally_invalid_evidence_is_not_a_gate_failure():
    problems = [NOT_PUSHED, NO_PASS, VERIFIER_PROBLEM]
    # The gate never ran (verifier unavailable).
    assert (
        repository_gate_failure(
            _manifest(status="unavailable", execution_environment="openshell_verification_pending"),
            problems,
        )
        is None
    )
    # The gate ran on some other head.
    assert repository_gate_failure(_manifest(executed_head_sha="b" * 40), problems) is None
    # A recorded "fail" with exit code 0 is not a gate that failed.
    assert repository_gate_failure(_manifest(returncode=0), problems) is None
    # Any structural problem beside the gate's consequences keeps manual repair.
    assert (
        repository_gate_failure(_manifest(), [*problems, "repo evidence must declare dirty=false"])
        is None
    )
    assert repository_gate_failure(_manifest(), [NOT_PUSHED]) is None
    assert repository_gate_failure(None, problems) is None
    assert repository_gate_failure({"repo": {"head_sha": HEAD}}, problems) is None


def test_gate_failure_at_submission_retries_with_gate_tail_then_succeeds(tmp_path):
    cp = ControlPlane.in_memory()
    task = _gate_task(cp)
    attempts = []

    def executor(task_payload, task_dir):
        attempts.append(task_payload)
        manifest = _manifest(gate_failed=len(attempts) == 1)
        (task_dir / "mac-evidence.json").write_text(json.dumps(manifest))
        return WorkerExecution(0, "Executor returned evidence")

    worker = _worker(cp, tmp_path, executor)
    first = worker.run_once()

    assert first.status == "blocked"
    blocked = next(
        event for event in reversed(cp.task_history(task.id)) if event.to_state == "blocked"
    )
    assert blocked.detail["reason"] == "repository_gate_failed"
    assert blocked.detail["manual_repair_required"] is False
    assert blocked.detail["repository_gate_failure"]["returncode"] == 1

    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", task.id),
    )
    result = cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert [item["id"] for item in result["auto_reopened"]] == [task.id]
    assert reopened.state == TaskState.OPEN.value
    assert reopened.attempt_count == 1
    gate = reopened.metadata["repository_gate_failure"]
    assert gate["failed_attempt"] == 1
    assert any("test_closure_capture" in line for line in gate["failing_lines"])

    second = worker.run_once()

    assert second.status == "submitted_for_review"
    assert len(attempts) == 2
    prompt = build_task_prompt(attempts[1])
    assert "Retry after a failed repository test gate" in prompt
    assert "test_closure_capture - AssertionError: assert 3 == 4" in prompt
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in prompt
    assert "Retry after a failed repository test gate" not in build_task_prompt(attempts[0])


def test_gate_failures_retry_until_max_attempts_then_fail():
    cp = ControlPlane.in_memory()
    task = _gate_task(cp, max_attempts=3)

    for attempt in (1, 2):
        # Identical gate failures: only max_attempts bounds them.
        _block_attempt(cp, task.id, attempt, _gate_detail())
        cp.tick(limit=0)
        assert cp.get_task(task.id).state == TaskState.OPEN.value

    _block_attempt(cp, task.id, 3, _gate_detail())
    result = cp.tick(limit=0)

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert [item["id"] for item in result["auto_retry_exhausted"]] == [task.id]
    terminal = [
        event for event in cp.task_history(task.id) if event.to_state == TaskState.FAILED.value
    ][-1]
    assert terminal.detail["reason"] == "max attempts"
    assert failed.metadata["failure_class"] == "work"


def test_structurally_invalid_submission_still_requires_manual_repair(tmp_path):
    cp = ControlPlane.in_memory()
    task = _gate_task(cp)

    def executor(task_payload, task_dir):
        # The gate result names another commit: not this head's verdict.
        manifest = _manifest(executed_head_sha="b" * 40)
        (task_dir / "mac-evidence.json").write_text(json.dumps(manifest))
        return WorkerExecution(0, "Executor returned evidence")

    result = _worker(cp, tmp_path, executor).run_once()

    assert result.status == "blocked"
    blocked = next(
        event for event in reversed(cp.task_history(task.id)) if event.to_state == "blocked"
    )
    assert blocked.detail["reason"] == "verification_contract_failed"
    assert blocked.detail["manual_repair_required"] is True
    assert "repository_gate_failure" not in blocked.detail

    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", task.id),
    )
    cp.tick(limit=0)

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert failed.attempt_count == 1
    assert "repository_gate_failure" not in failed.metadata


def test_executor_failed_classification_is_unchanged():
    executor_detail = {
        "reason": "executor_failed",
        "manual_repair_required": True,
        "returncode": 2,
        "error": "executor exited 2",
        "output_tail": "[gate] phase tests: 12.0s rc=2\n" + GATE_OUTPUT,
    }
    assert _blocked_attempt_retry_kind(executor_detail) == "non_retryable"
    assert _blocked_attempt_retry_kind({**_gate_detail(), "manual_repair_required": True}) == (
        "non_retryable"
    )
    assert _blocked_attempt_retry_kind(_gate_detail()) == "work"
    # A contract failure without a gate verdict keeps its deterministic stop.
    assert (
        _blocked_attempt_retry_kind(
            {"reason": "verification_contract_failed", "problems": [NOT_PUSHED]}
        )
        == "non_retryable"
    )


def test_operator_reopen_drops_the_previous_gate_failure():
    cp = ControlPlane.in_memory()
    task = _gate_task(cp)
    _block_attempt(cp, task.id, 1, _gate_detail())
    cp.tick(limit=0)
    assert "repository_gate_failure" in cp.get_task(task.id).metadata

    _block_attempt(cp, task.id, 3, {"reason": "verification_contract_failed"})
    cp.tick(limit=0)
    assert cp.get_task(task.id).state == TaskState.FAILED.value
    cp.reopen_task(task.id, actor="operator")

    assert "repository_gate_failure" not in cp.get_task(task.id).metadata
