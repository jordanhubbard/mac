"""Typed executor infrastructure failures are retried, not repaired by hand.

Live 2026-10-05 on natasha:
- task_71cfdfd5 passed `make build && make test-quick`, then the finalizer's
  canonical fetch could not connect to github.com:443 for 136 s and the push
  was skipped.
- task_e95cc31a never started work, because the in-sandbox OpenCode preflight
  timed out (rc=124, class=timeout).

The worker reported both as verification_contract_failed ("repo evidence
requires changed files"), so the hub failed each one at attempt 1 as
non-retryable manual repair. Now the host's own records name the cause, and the
hub requeues the attempt without charging it, under a bounded budget. Real
test failures and invalid evidence still fail their gates, and nothing here
excuses a claim of publication.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from mac import executor_sandbox
from mac import task_executor as te
from mac.api import create_app
from mac.attempt_failure_classifier import classify_attempt_failure
from mac.hermes_adapter import MacApiClient
from mac.infrastructure_failure import (
    BLOCK_REASON,
    executor_infrastructure_failure,
    git_transport_failure,
    is_git_transport_failure,
)
from mac.models import TaskState
from mac.services import ControlPlane, _blocked_attempt_retry_kind
from mac.worker import MacWorker, WorkerExecution
from tests.conftest import verifier_test_item

HEAD = "c" * 40
# The 10-05 finalizer's fetch error, as git reports it.
GITHUB_UNREACHABLE = (
    "fetch of canonical branch 'main' from https://github.com/jordanhubbard/nanolang.git "
    "failed: fatal: unable to access 'https://github.com/jordanhubbard/nanolang.git/': "
    "Failed to connect to github.com port 443 after 136197 ms: Couldn't connect to server"
)
NOT_PUSHED = "repo evidence requires pushed=true with remote_ref, or pr_url"
NO_FILES = "repo evidence requires changed files"


def _transport_manifest():
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer",
        "repo": {
            "head_sha": HEAD,
            "pushed": False,
            "remote_ref": "refs/heads/mac/task-lease_1",
            "dirty": False,
            "files_changed": ["src/vm.c"],
        },
        "tests": [verifier_test_item(HEAD)],
        "push": {"status": "skipped", "reason": "canonical freshness check failed"},
        "freshness_error": GITHUB_UNREACHABLE,
        "infrastructure_failure": git_transport_failure(
            "publication_preflight", GITHUB_UNREACHABLE
        ),
    }


def _preflight_manifest(preflight_class="timeout", agent=""):
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer",
        "repo": {"head_sha": HEAD, "pushed": False, "dirty": False, "files_changed": []},
        "tests": [verifier_test_item(HEAD)],
        "coding_agents": {
            "order": ["opencode"],
            "order_source": "hub",
            "runs": [
                {
                    "agent": agent,
                    "model": "",
                    "returncode": 42,
                    "skipped": [
                        {
                            "agent": "opencode",
                            "failure_class": "preflight_failed",
                            "detail": "opencode did not pass the in-sandbox preflight",
                            "preflight_failure_class": preflight_class,
                        }
                    ],
                }
            ],
        },
    }


# -- classification of the host's records ---------------------------------


def test_git_transport_errors_are_recognised_and_refusals_are_not():
    assert is_git_transport_failure(GITHUB_UNREACHABLE)
    assert is_git_transport_failure("fatal: Could not resolve host: github.com")
    assert is_git_transport_failure("error: RPC failed; curl 56 GnuTLS recv error (-54)")
    assert is_git_transport_failure("git operation exceeded finalizer phase budget")
    # A credential or permission problem is not fixed by retrying.
    assert not is_git_transport_failure(
        "fatal: unable to access 'https://github.com/x/y.git/': The requested URL "
        "returned error: 403"
    )
    assert not is_git_transport_failure("fatal: Authentication failed for 'https://github.com/'")
    assert not is_git_transport_failure("canonical tip abc is not an ancestor of task HEAD")
    assert git_transport_failure("guarded_push", "! [rejected] non-fast-forward") is None


def test_a_real_unreachable_remote_reads_as_transport(tmp_path):
    # What git itself prints when nothing listens (port 9, discard).
    fetch = subprocess.run(
        ["git", "ls-remote", "http://127.0.0.1:9/repo.git"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fetch.returncode != 0
    assert is_git_transport_failure(fetch.stderr), fetch.stderr


def test_typed_causes_come_only_from_unpublished_evidence():
    transport = executor_infrastructure_failure(_transport_manifest())
    assert transport["kind"] == "git_transport"
    assert transport["phase"] == "publication_preflight"

    preflight = executor_infrastructure_failure(_preflight_manifest())
    assert preflight["kind"] == "coding_agent_preflight"
    assert preflight["agents"] == [{"agent": "opencode", "preflight_failure_class": "timeout"}]

    # A configuration failure stays an ordinary failure.
    assert executor_infrastructure_failure(_preflight_manifest("agent_binary_missing")) is None
    assert executor_infrastructure_failure(_preflight_manifest("probe_failed")) is None
    # A coding agent that ran owns its outcome.
    assert executor_infrastructure_failure(_preflight_manifest(agent="opencode")) is None
    # Nothing excuses a claim of publication.
    claimed = _transport_manifest()
    claimed["repo"]["pushed"] = True
    assert executor_infrastructure_failure(claimed) is None
    # A forged record that names no transport error is ignored.
    forged = _transport_manifest()
    forged["infrastructure_failure"]["error"] = "tests failed"
    assert executor_infrastructure_failure(forged) is None


def test_the_executor_records_why_the_preflight_failed(monkeypatch):
    class Choice:
        agent = "opencode"

        def route_fingerprint(self):
            return "fp-opencode"

    monkeypatch.setitem(
        executor_sandbox._SANDBOX_PREFLIGHT_CACHE,
        "fp-opencode",
        {"verified": False, "failure_class": "timeout", "cached_monotonic": 0.0},
    )
    cls = executor_sandbox._cached_preflight_failure_class(Choice())
    assert cls == "timeout"
    record = executor_sandbox._with_preflight_class(
        {"agent": "opencode", "failure_class": "preflight_failed"}, {"opencode": cls}
    )
    assert record["preflight_failure_class"] == "timeout"
    untouched = executor_sandbox._with_preflight_class(
        {"agent": "claude", "failure_class": "agent_binary_missing"}, {"claude": "timeout"}
    )
    assert "preflight_failure_class" not in untouched


# -- the finalizer names a transport failure -------------------------------


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def test_the_finalizer_records_an_unreachable_canonical_remote(tmp_path, monkeypatch):
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
    _git(work, "checkout", "-b", "task/offline")
    (work / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", _git(work, "rev-parse", "main").stdout.strip())
    monkeypatch.setenv("MAC_TASK_REPO_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("MAC_TASK_REPO_LEASE_ID", "lease-test")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-offline",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    # Nothing listens here: the fetch fails the way 10-05's did.
                    "canonical_remote_url": "http://127.0.0.1:9/repo.git",
                    "test": {"command": "true"},
                },
            },
        },
    }
    ws = tmp_path / "ws"
    ws.mkdir()

    te.run_deterministic_git_finalizer(ws, task)

    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["repo"]["pushed"] is False
    failure = manifest["infrastructure_failure"]
    assert failure["kind"] == "git_transport"
    assert failure["phase"] == "publication_preflight"
    assert executor_infrastructure_failure(manifest) == failure


# -- the worker and the hub ------------------------------------------------


def _worker(cp, tmp_path, executor):
    agent = cp.register_agent(cp.register_machine("h").id, "natasha", capabilities=["python"])
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


def _task(cp, max_attempts=4):
    return cp.create_task(
        "Preserve ownership facts",
        required_capabilities=["python"],
        max_attempts=max_attempts,
        metadata={"repository_contract": {"test": {"command": "make test-quick"}}},
    )


def _blocked_event(cp, task_id):
    return next(e for e in reversed(cp.task_history(task_id)) if e.to_state == "blocked")


def _age_block(cp, task_id):
    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", task_id)
    )


@pytest.mark.parametrize(
    ("manifest_fn", "kind"),
    [(_transport_manifest, "git_transport"), (_preflight_manifest, "coding_agent_preflight")],
)
def test_an_infrastructure_failure_is_requeued_without_charge(tmp_path, manifest_fn, kind):
    cp = ControlPlane.in_memory()
    task = _task(cp)
    attempts = []

    def executor(task_payload, task_dir):
        attempts.append(task_payload)
        manifest = manifest_fn() if len(attempts) == 1 else _published_manifest()
        (task_dir / "mac-evidence.json").write_text(json.dumps(manifest))
        return WorkerExecution(0, "Executor returned evidence")

    worker = _worker(cp, tmp_path, executor)
    assert worker.run_once().status == "blocked"

    blocked = _blocked_event(cp, task.id)
    assert blocked.detail["reason"] == BLOCK_REASON
    assert blocked.detail["manual_repair_required"] is False
    assert blocked.detail["failure_class"] == "environment"
    assert blocked.detail["infrastructure_failure"]["kind"] == kind
    # The cause is on the task, not left to the output tail.
    diagnosis = blocked.detail["diagnosis"]
    assert diagnosis["problem"].startswith("Executor infrastructure failure")
    assert diagnosis["failure"] == BLOCK_REASON
    if kind == "git_transport":
        assert "github.com port 443" in blocked.detail["error"]
        assert blocked.detail["unpushed_head_sha"] == HEAD
    else:
        assert "opencode (timeout)" in blocked.detail["error"]

    _age_block(cp, task.id)
    cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    assert reopened.attempt_count == 0
    assert reopened.metadata["infrastructure_requeues"] == 1
    reopen = [e for e in cp.task_history(task.id) if e.event_type == "task.auto_reopened"][-1]
    assert reopen.detail["attempt_refunded"] is True
    assert reopen.detail["infrastructure_failure"]["kind"] == kind

    assert worker.run_once().status == "submitted_for_review"
    assert len(attempts) == 2


def _published_manifest():
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Preserved ownership facts.",
        "repo": {
            "head_sha": HEAD,
            "pushed": True,
            "remote_ref": "refs/heads/mac/task-lease_2",
            "dirty": False,
            "files_changed": ["src/vm.c"],
        },
        "tests": [verifier_test_item(HEAD)],
    }


def test_requeues_are_bounded_then_charged(monkeypatch):
    monkeypatch.setenv("MAC_INFRASTRUCTURE_REQUEUES", "1")
    cp = ControlPlane.in_memory()
    task = _task(cp, max_attempts=2)
    detail = {
        "reason": BLOCK_REASON,
        "failure": BLOCK_REASON,
        "failure_class": "environment",
        "manual_repair_required": False,
        "problems": [NOT_PUSHED, NO_FILES],
        "infrastructure_failure": git_transport_failure(
            "publication_preflight", GITHUB_UNREACHABLE
        ),
    }

    def block(attempt, error):
        cp._transition_task_internal(
            task.id, TaskState.BLOCKED.value, "agent_natasha", {**detail, "error": error}
        )
        cp.store.execute(
            "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
            (attempt, "2000-01-01T00:00:00+00:00", task.id),
        )

    block(1, "fetch failed after 136197 ms")
    cp.tick(limit=0)
    first = cp.get_task(task.id)
    assert first.state == TaskState.OPEN.value and first.attempt_count == 0

    # The budget is spent: the next one consumes an attempt like any
    # transient failure instead of looping forever.
    block(1, "fetch failed after 98012 ms")
    cp.tick(limit=0)
    second = cp.get_task(task.id)
    assert second.state == TaskState.OPEN.value
    assert second.attempt_count == 1
    assert second.metadata["infrastructure_requeues"] == 1

    # And the same failure again stops, as any repeated transient one does.
    block(1, "fetch failed after 98012 ms")
    cp.tick(limit=0)
    assert cp.get_task(task.id).state == TaskState.FAILED.value


def test_real_failures_keep_their_dispositions():
    assert (
        _blocked_attempt_retry_kind(
            {
                "reason": BLOCK_REASON,
                "manual_repair_required": False,
                "problems": [NOT_PUSHED, NO_FILES],
            }
        )
        == "infrastructure_transient"
    )
    # Invalid evidence is still deterministic.
    assert (
        _blocked_attempt_retry_kind(
            {"reason": "verification_contract_failed", "problems": [NO_FILES]}
        )
        == "non_retryable"
    )
    # A red gate is still the agent's work.
    assert (
        _blocked_attempt_retry_kind(
            {"reason": "repository_gate_failed", "manual_repair_required": False}
        )
        == "work"
    )
    # An explicit manual-repair verdict wins over the label.
    assert (
        _blocked_attempt_retry_kind(
            {"reason": BLOCK_REASON, "manual_repair_required": True, "problems": [NO_FILES]}
        )
        == "non_retryable"
    )


def test_a_preflight_timeout_is_an_environment_failure_not_scope():
    classification = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": BLOCK_REASON,
                    "infrastructure_failure": {
                        "kind": "coding_agent_preflight",
                        "agents": [{"agent": "opencode", "preflight_failure_class": "timeout"}],
                    },
                },
            }
        ]
    )
    assert classification.failure_class == "environment"


def test_a_gate_failure_is_not_excused_by_a_transport_record(tmp_path):
    """Tests that failed are the work's verdict, whatever the network did."""
    cp = ControlPlane.in_memory()
    task = _task(cp)

    def executor(task_payload, task_dir):
        manifest = _transport_manifest()
        manifest["tests"] = [
            verifier_test_item(
                HEAD,
                name="repository bootstrap and test gate",
                command="make test-quick",
                returncode=1,
                status="fail",
                stdout="FAILED tests/test_vm.py::test_closure - assert 3 == 4\n",
                test_count=3,
            )
        ]
        (task_dir / "mac-evidence.json").write_text(json.dumps(manifest))
        return WorkerExecution(0, "Executor returned evidence")

    _worker(cp, tmp_path, executor).run_once()

    assert _blocked_event(cp, task.id).detail["reason"] == "repository_gate_failed"

