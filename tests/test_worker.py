from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time
import types
from typing import Any, Dict, Optional

import pytest

from fastapi.testclient import TestClient

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_SCHEMA,
    REPO_UPDATE_TOPIC,
)
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane, sign_verification_manifest
from mac.worker import MacWorker, SubprocessExecutor, WorkerExecution, build_parser, register_worker


def api_transport(client: TestClient):
    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        request = getattr(client, method.lower())
        kwargs: Dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        response = request(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    return transport


def register_worker_fixture(cp: ControlPlane):
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])
    return agent


def test_mac_worker_cli_defaults_to_deployed_hub_env(monkeypatch):
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.setenv("MAC_HUB_URL", "http://hub.example.internal:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker-token")
    monkeypatch.setenv("MAC_HERMES_INSTANCE_ID", "hermes_rocky")

    args = build_parser().parse_args(["--register", "--heartbeat-only"])

    assert args.url == "http://hub.example.internal:8789"
    assert args.token == "worker-token"
    assert args.hermes_instance_id == "hermes_rocky"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_fixture(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "mac-tests@example.invalid")
    _git(seed, "config", "user.name", "mac tests")
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(work)],
        check=True,
        capture_output=True,
    )
    return seed, work


def _commit_fixture_update(seed: Path, text: str) -> str:
    (seed / "README.md").write_text(text, encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "update")
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def _repository_task_metadata(repo: Path) -> Dict[str, Any]:
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "repo-beads-mac",
        "bootstrap": {"command": "true"},
        "test": {"command": "true"},
    }
    return {
        "origin": {
            "type": "beads",
            "repository_id": "repo_test",
            "repository_name": "repo-test",
            "repository_path": str(repo),
            "source": "repo-beads-test",
            "bead_id": "repo-worktree-test",
            "repository_contract": contract,
        },
        "execution_contract": {
            "schema": "mac.task_execution_contract.v1",
            "type": "repository",
            "quality": "strong",
            "source": "test",
            "repository_path": str(repo),
            "repository_contract": contract,
        },
    }


def _write_worker_manifest(
    task_dir: Path,
    *,
    head_sha: str = "abcdef1234567890abcdef1234567890abcdef12",
    evidence_type: str = "test",
    remote_ref: str = "refs/heads/task/example",
    files_changed: Optional[list[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    repo: Dict[str, Any] = {
        "head_sha": head_sha,
        "pushed": True,
        "remote_ref": remote_ref,
        "dirty": False,
    }
    if files_changed:
        repo["files_changed"] = files_changed
    manifest: Dict[str, Any] = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": evidence_type,
        "repo": repo,
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    if extra:
        manifest.update(extra)
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def _write_repo_worker_manifest(task_dir: Path, worktree: Path) -> Dict[str, Any]:
    return _write_worker_manifest(
        task_dir,
        head_sha=_git(worktree, "rev-parse", "HEAD"),
        remote_ref="refs/heads/%s" % (_git(worktree, "branch", "--show-current") or "task"),
    )


def test_mac_worker_claims_for_specific_agent_and_submits_for_review(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    skipped = cp.create_task("Docs task", required_capabilities=["docs"])
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        assert task_payload["id"] == task.id
        assert (task_dir / "task.json").exists()
        _write_worker_manifest(task_dir)
        return WorkerExecution(0, "tests passed", stdout="tests passed\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert result.task["id"] == task.id
    reviewed = cp.get_task(task.id)
    assert reviewed.state == TaskState.NEEDS_REVIEW.value
    assert reviewed.owner_agent_id is None
    assert reviewed.lease_id is None
    assert cp.get_task(skipped.id).state == TaskState.OPEN.value
    evidence = cp.list_evidence(task.id)
    assert evidence[0].summary == "tests passed"
    assert evidence[0].metadata["returncode"] == 0
    observations = cp.list_observability(layer="worker", limit=20)
    names = {item.name for item in observations}
    assert "worker.task_claimed" in names
    assert "worker.execution.duration_ms" in names
    assert any(item.subject_id == task.id for item in observations)


def test_mac_worker_accepts_structured_passed_result_evidence(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        _write_worker_manifest(
            task_dir,
            evidence_type="repo_change",
            files_changed=["src/example.py"],
            extra={
                "tests": {
                    "framework": "pytest",
                    "command": "python -m pytest tests/test_example.py",
                    "result": "passed",
                    "passed": 3,
                    "failed": 0,
                    "additional_smoke": {
                        "result": "passed",
                        "passed": 132,
                        "failed": 0,
                    },
                },
                "checks": {
                    "branch_pushed": True,
                    "git_head_matches_remote": True,
                    "working_tree_clean": True,
                },
            },
        )
        return WorkerExecution(0, "manifest validates")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value


def test_mac_worker_processes_review_nudge_and_records_signed_verdict(tmp_path: Path):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("review-host")
    executor_agent = cp.register_agent(machine.id, "executor", capabilities=["python"])
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    task = cp.create_task(
        "Reviewable repo task",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, executor_agent.id)
    cp.start_task(task.id, executor_agent.id)
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abc123abc123abc123abc123abc123abc123abcd",
            "remote_ref": "origin/main",
            "pushed": True,
            "dirty": False,
            "files_changed": ["src/example.py"],
        },
        "checks": [{"name": "pytest", "status": "passed", "returncode": 0}],
        "signed_by": executor_agent.id,
    }
    executor_manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor_agent.id), executor_manifest
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/executor-result.json",
        "executor completed",
        executor_agent.id,
        metadata={"returncode": 0, "verification": executor_manifest},
    )
    cp.submit_for_review(task.id, executor_agent.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    assert first["reviewer_agent_id"] == reviewer.id
    client = TestClient(create_app(control_plane=cp))

    def review_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        context = task_payload["metadata"]["review_context"]
        assert context["task_id"] == task.id
        assert context["review_id"] == first["review_id"]
        assert context["executor_evidence_id"] == evidence.id
        assert context["review_claim"]["review_id"] == first["review_id"]
        assert context["review_claim"]["reviewer_agent_id"] == reviewer.id
        assert context["review_claim"]["executor_evidence_id"] == evidence.id
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "review_id": context["review_id"],
            "reviewed_evidence_id": context["executor_evidence_id"],
            "repo": dict(executor_manifest["repo"]),
            "checks": [{"name": "reviewer independent verification", "returncode": 0}],
            "worktree_digest": "sha256:" + ("0" * 64),
            "findings": ["executor evidence is signed and tests passed"],
        }
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return WorkerExecution(0, "review approved", stdout="approved\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        reviewer.id,
        tmp_path,
        review_executor,
        attestation_key=cp._agent_attestation_key(reviewer.id),
    )

    result = worker.run_once()

    assert result.status == "review_verdict_recorded"
    verdict_evidence = cp.list_evidence(task.id)[-1]
    manifest = verdict_evidence.metadata["verification"]
    assert verdict_evidence.kind == "review"
    assert manifest["evidence_type"] == "review_verdict"
    assert manifest["signed_by"] == reviewer.id
    assert manifest["reviewed_evidence_id"] == evidence.id
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert cp.get_agent(reviewer.id).status == "idle"
    task_metadata = cp.get_task(task.id).metadata
    assert (
        task_metadata["review_claims"][first["review_id"]]["reviewer_agent_id"]
        == reviewer.id
    )
    assert "task.review_claimed" in {event.event_type for event in cp.task_history(task.id)}


def test_review_nudge_prepares_review_worktree_and_git_main_publication(tmp_path: Path):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("review-host")
    executor_agent = cp.register_agent(machine.id, "executor", capabilities=["python"])
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    _seed, repo = _git_fixture(tmp_path)
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    remote_url = _git(repo, "remote", "get-url", "origin")
    branch = "mac/review-proof"
    _git(repo, "checkout", "-b", branch)
    (repo / "README.md").write_text("reviewed change\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "reviewed change")
    _git(repo, "push", "-u", "origin", branch)
    reviewed_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    metadata = _repository_task_metadata(repo)
    metadata["publication_target"] = "git://main"
    task = cp.create_task(
        "Reviewable pushed branch",
        project="repo-beads-mac",
        required_capabilities=["python"],
        metadata=metadata,
    )
    cp.claim_task(task.id, executor_agent.id)
    cp.start_task(task.id, executor_agent.id)
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": reviewed_head,
            "remote_ref": "refs/heads/%s" % branch,
            "remote_url": remote_url,
            "path": str(repo),
            "pushed": True,
            "dirty": False,
            "files_changed": ["README.md"],
        },
        "checks": [{"name": "executor tests", "status": "passed", "returncode": 0}],
        "signed_by": executor_agent.id,
    }
    executor_manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor_agent.id), executor_manifest
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/executor-result.json",
        "executor completed",
        executor_agent.id,
        metadata={"returncode": 0, "verification": executor_manifest},
    )
    cp.submit_for_review(task.id, executor_agent.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    client = TestClient(create_app(control_plane=cp))

    def review_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        context = task_payload["metadata"]["review_context"]
        runtime = task_payload["metadata"]["runtime"]
        assert context["review_claim"]["project"] == "repo-beads-mac"
        assert context["review_claim"]["repository_worktree"] == str(repo)
        assert context["review_claim"]["repository_files_changed"] == ["README.md"]
        review_worktree = Path(runtime["repository_worktree"])
        assert review_worktree.is_dir()
        assert _git(review_worktree, "rev-parse", "HEAD") == reviewed_head
        assert context["review_repository_worktree"]["repository_worktree"] == str(review_worktree)
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "review_id": context["review_id"],
            "reviewed_evidence_id": context["executor_evidence_id"],
            "repo": {
                "head_sha": reviewed_head,
                "pushed": True,
                "dirty": False,
                "files_changed": ["README.md"],
            },
            "checks": [
                {
                    "name": "reviewer checkout head",
                    "command": "git rev-parse HEAD",
                    "returncode": 0,
                    "status": "pass",
                }
            ],
            "worktree_digest": "sha256:" + ("1" * 64),
            "findings": ["review worktree checked out pushed executor branch"],
        }
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return WorkerExecution(0, "review approved", stdout="approved\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        reviewer.id,
        tmp_path / "workspaces",
        review_executor,
        attestation_key=cp._agent_attestation_key(reviewer.id),
    )

    result = worker.run_once()

    assert result.status == "review_verdict_recorded"
    verdict_manifest = cp.list_evidence(task.id)[-1].metadata["verification"]
    assert verdict_manifest["repo"]["remote_ref"] == "refs/heads/%s" % branch
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert cp.list_publications(task.id)[0].target == "git://main"
    assert _git(repo, "rev-parse", "HEAD") == reviewed_head
    _git(repo, "fetch", "origin", "main")
    assert _git(repo, "rev-parse", "origin/main") == reviewed_head


def test_mac_worker_skips_stale_review_nudge_and_processes_next(tmp_path: Path):
    from tests.conftest import submit_review_verdict

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("review-host")
    executor_agent = cp.register_agent(machine.id, "executor", capabilities=["python"])
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])

    def create_reviewable_task(title: str):
        task = cp.create_task(
            title,
            required_capabilities=["python"],
            metadata={"publication_target": "test://publish"},
        )
        cp.claim_task(task.id, executor_agent.id)
        cp.start_task(task.id, executor_agent.id)
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": "abc123abc123abc123abc123abc123abc123abcd",
                "remote_ref": "origin/main",
                "pushed": True,
                "dirty": False,
                "files_changed": ["src/example.py"],
            },
            "checks": [{"name": "pytest", "status": "passed", "returncode": 0}],
            "signed_by": executor_agent.id,
        }
        manifest["signature"] = sign_verification_manifest(
            cp._agent_attestation_key(executor_agent.id), manifest
        )
        evidence = cp.add_evidence(
            task.id,
            "log",
            "file:///tmp/executor-result.json",
            "executor completed",
            executor_agent.id,
            metadata={"returncode": 0, "verification": manifest},
        )
        cp.submit_for_review(task.id, executor_agent.id)
        review_tick = cp.advance_default_review_workflow(task.id)
        return task, evidence, review_tick, manifest

    stale_task, stale_evidence, stale_tick, _ = create_reviewable_task("Stale review")
    stale_verdict_id = submit_review_verdict(
        cp, stale_task.id, reviewer.id, stale_evidence.id
    )
    cp.submit_review(
        stale_tick["review_id"],
        ReviewStatus.APPROVED.value,
        reviewer.id,
        evidence_id=stale_verdict_id,
    )
    current_task, current_evidence, current_tick, executor_manifest = create_reviewable_task(
        "Current review"
    )
    client = TestClient(create_app(control_plane=cp))

    def review_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        context = task_payload["metadata"]["review_context"]
        assert context["task_id"] == current_task.id
        assert context["review_id"] == current_tick["review_id"]
        assert context["executor_evidence_id"] == current_evidence.id
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "review_id": context["review_id"],
            "reviewed_evidence_id": context["executor_evidence_id"],
            "repo": dict(executor_manifest["repo"]),
            "checks": [{"name": "reviewer independent verification", "returncode": 0}],
            "worktree_digest": "sha256:" + ("0" * 64),
            "findings": ["executor evidence is signed and tests passed"],
        }
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return WorkerExecution(0, "review approved", stdout="approved\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        reviewer.id,
        tmp_path,
        review_executor,
        attestation_key=cp._agent_attestation_key(reviewer.id),
    )

    result = worker.run_once()

    assert result.status == "review_verdict_recorded"
    assert cp.list_reviews(current_task.id)[0].status == ReviewStatus.APPROVED.value
    assert cp.get_task(current_task.id).state == TaskState.COMPLETED.value


def test_mac_worker_forwards_notifier_status_updates_to_slack_home_channels(
    tmp_path: Path,
    monkeypatch,
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "slack_accounts.json").write_text(
        json.dumps(
            [
                {"name": "omgjkh", "bot_token": "xoxb-one"},
                {"name": "offtera", "bot_token": "xoxb-two"},
            ]
        ),
        encoding="utf-8",
    )
    (hermes_home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {"name": "omgjkh", "team_id": "T1", "channel_id": "C1"},
                {"name": "offtera", "team_id": "T2", "channel_id": "C2"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    sent = []

    class FakeWebClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def chat_postMessage(self, channel: str, text: str) -> Dict[str, Any]:
            sent.append({"token": self.token, "channel": channel, "text": text})
            return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "slack_sdk",
        types.SimpleNamespace(WebClient=FakeWebClient),
    )

    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    cp.send_message(
        "notifier",
        agent.id,
        "status_update",
        {
            "schema": "mac.notifier.task_progress.v1",
            "status": "task.completed",
            "channel_type": "slack",
            "notification": {
                "title": "Task completed",
                "body": "Rocky completed lifecycle proof",
                "event_type": "task.completed",
                "subject_id": "task_live",
            },
            "target": {"channel_type": "slack"},
        },
    )
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
    )

    result = worker.run_once()

    assert result.status == "no_task"
    assert sent == [
        {
            "token": "xoxb-one",
            "channel": "C1",
            "text": "Task completed\nRocky completed lifecycle proof\ntask.completed task_live",
        },
        {
            "token": "xoxb-two",
            "channel": "C2",
            "text": "Task completed\nRocky completed lifecycle proof\ntask.completed task_live",
        },
    ]
    assert cp.list_messages(agent.id)[0].status == "delivered"
    assert any(
        event.name == "worker.notifier.status_forwarded"
        for event in cp.list_observability(limit=20)
    )


def test_mac_worker_records_failed_execution_and_fails_task(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        return WorkerExecution(2, "pytest failed", stderr="pytest failed\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "failed"
    assert result.error == "pytest failed"
    assert cp.get_task(task.id).state == TaskState.FAILED.value
    evidence = cp.list_evidence(task.id)
    assert evidence[0].metadata["returncode"] == 2


def test_mac_worker_fails_successful_execution_without_verification_manifest(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "looked ok", stdout="ok\n"),
    )

    result = worker.run_once()

    assert result.status == "failed"
    assert "status" in (result.error or "")
    assert cp.get_task(task.id).state == TaskState.FAILED.value
    assert cp.list_evidence(task.id)[0].metadata["verification"]["status"] == "missing"


def test_mac_worker_accepts_operator_result_without_repository_anchor(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task(
        "Planning task",
        required_capabilities=["python"],
        metadata={
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "operator_directive",
                "quality": "weak",
                "evidence_type": "operator_result",
            }
        },
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Plan produced",
            "result": "Story graph and verification plan produced.",
        }
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return WorkerExecution(0, "plan produced", stdout="plan produced\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
    manifest = cp.list_evidence(task.id)[0].metadata["verification"]
    assert manifest["evidence_type"] == "operator_result"
    assert manifest["signed_by"] == agent.id


def test_mac_worker_audits_subprocess_commands(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))
    executor_script = tmp_path / "executor.py"
    executor_script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import json, os",
                "workspace = Path(os.environ['MAC_TASK_WORKSPACE'])",
                "manifest = {",
                "  'schema': 'mac.worker_evidence.v1',",
                "  'status': 'complete',",
                "  'evidence_type': 'test',",
                "  'repo': {",
                "    'head_sha': 'abcdef1234567890abcdef1234567890abcdef12',",
                "    'pushed': True,",
                "    'remote_ref': 'refs/heads/task/audit',",
                "    'dirty': False,",
                "  },",
                "  'checks': [{'name': 'pytest', 'returncode': 0}],",
                "}",
                "(workspace / 'mac-evidence.json').write_text(json.dumps(manifest), encoding='utf-8')",
                "print('audited command ran')",
            ]
        ),
        encoding="utf-8",
    )
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        SubprocessExecutor([sys.executable, str(executor_script)]),
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    records = cp.list_command_audit(agent_id=agent.id, task_id=task.id, limit=10)
    phases = [record.phase for record in records]
    assert "started" in phases
    assert "completed" in phases
    completed = next(record for record in records if record.phase == "completed")
    assert completed.returncode == 0
    assert completed.stdout_bytes and completed.stdout_sha256
    assert completed.metadata["argv_sha256"].startswith("sha256:")
    assert completed.argv[0] == sys.executable
    events = cp.list_events(
        subject_type="task",
        subject_id=task.id,
        event_type_prefix="command.",
        limit=10,
    )
    assert {event["event_type"] for event in events} >= {"command.started", "command.completed"}
    # /events must NOT expose raw argv (mac-7osn): callers with read scope on /events
    # must not see flag values that may contain secrets.
    for event in events:
        detail = event["detail"] if isinstance(event["detail"], dict) else json.loads(event["detail"])
        assert "argv" not in detail, f"raw argv leaked into events view: {detail}"
        assert detail.get("argv_redacted") is True
        # argv0 (command name) is fine — same level of disclosure as `ps`
        assert "argv0" in detail


def test_mac_worker_prepares_repository_task_in_git_worktree(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Repository task",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        runtime = task_payload["metadata"]["runtime"]
        worktree = Path(runtime["repository_worktree"])
        assert worktree.is_dir()
        assert worktree.resolve() != repo.resolve()
        assert runtime["repository_source_path"] == str(repo.resolve())
        assert runtime["repository_base_sha"] == _git(repo, "rev-parse", "HEAD")
        assert _git(worktree, "branch", "--show-current").startswith("mac/")
        assert _git(worktree, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
        assert _git(repo, "status", "--porcelain") == ""
        assert (task_dir / "repository-worktree.json").exists()
        _write_repo_worker_manifest(task_dir, worktree)
        _git(worktree, "push", "origin", "HEAD:refs/heads/%s" % runtime["repository_branch"])
        return WorkerExecution(0, "repo worktree prepared", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    observations = cp.list_observability(layer="worker", limit=20)
    assert any(item.name == "worker.repository.worktree_prepared" for item in observations)


def test_mac_worker_derives_repo_anchor_for_documentation_evidence(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    metadata = _repository_task_metadata(repo)
    metadata["execution_contract"]["evidence_type"] = "documentation"
    task = cp.create_task(
        "Repository docs task",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        runtime = task_payload["metadata"]["runtime"]
        worktree = Path(runtime["repository_worktree"])
        branch = runtime["repository_branch"]
        docs = worktree / "docs"
        docs.mkdir()
        (docs / "implementation-plan.md").write_text("plan\n", encoding="utf-8")
        _git(worktree, "add", "docs/implementation-plan.md")
        _git(worktree, "commit", "-m", "docs: add implementation plan")
        _git(worktree, "push", "origin", "HEAD:refs/heads/%s" % branch)
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(
                {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "documentation",
                    "summary": "docs updated",
                    "repo": {
                        "head_sha": "badbadbadbadbadbadbadbadbadbadbadbadbadb",
                        "pushed": False,
                        "remote_ref": "refs/heads/not-the-task-branch",
                        "dirty": True,
                        "files_changed": ["wrong.md"],
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return WorkerExecution(0, "docs updated", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    evidence = cp.list_evidence(task.id)[0]
    manifest = evidence.metadata["verification"]
    repo_anchor = manifest["repo"]
    assert repo_anchor["head_sha"] == _git(
        tmp_path / "workspaces" / task.id / ("repo-" + result.lease["id"]),
        "rev-parse",
        "HEAD",
    )
    assert repo_anchor["dirty"] is False
    assert repo_anchor["pushed"] is True
    assert repo_anchor["remote_ref"].startswith("refs/heads/mac/")
    assert repo_anchor["files_changed"] == ["docs/implementation-plan.md"]


def test_mac_worker_auto_publishes_repository_worktree_when_enabled(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    metadata = _repository_task_metadata(repo)
    metadata["execution_contract"]["evidence_type"] = "documentation"
    metadata["repository_auto_publish"] = True
    task = cp.create_task(
        "Repository docs auto-publish task",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        docs = worktree / "docs"
        docs.mkdir()
        (docs / "demo-story.md").write_text("demo\n", encoding="utf-8")
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(
                {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "documentation",
                    "summary": "docs updated",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return WorkerExecution(0, "docs updated", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    evidence = cp.list_evidence(task.id)[0]
    repo_anchor = evidence.metadata["verification"]["repo"]
    assert repo_anchor["dirty"] is False
    assert repo_anchor["pushed"] is True
    assert repo_anchor["files_changed"] == ["docs/demo-story.md"]
    branch = repo_anchor["remote_ref"].removeprefix("refs/heads/")
    assert _git(repo, "ls-remote", "origin", "refs/heads/%s" % branch).startswith(
        repo_anchor["head_sha"]
    )


def test_mac_worker_fails_when_required_changed_files_are_missing(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    metadata = _repository_task_metadata(repo)
    metadata["execution_contract"]["evidence_type"] = "documentation"
    metadata["execution_contract"]["required_changed_files"] = [
        "README.md",
        "docs/demo-story.md",
    ]
    metadata["repository_auto_publish"] = True
    task = cp.create_task(
        "Repository docs task with required files",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        docs = worktree / "docs"
        docs.mkdir()
        (docs / "demo-story.md").write_text("demo\n", encoding="utf-8")
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(
                {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "documentation",
                    "summary": "docs updated",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return WorkerExecution(0, "docs updated", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "failed"
    assert "README.md" in (result.error or "")
    assert cp.get_task(task.id).state == TaskState.FAILED.value
    assert cp.list_reviews(task.id) == []


def test_mac_worker_fails_dirty_repository_worktree_after_success(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Dirty result task",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        _write_repo_worker_manifest(task_dir, worktree)
        (worktree / "README.md").write_text("dirty output\n", encoding="utf-8")
        return WorkerExecution(0, "left a dirty tree", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "failed"
    assert "uncommitted changes" in (result.error or "")
    assert cp.get_task(task.id).state == TaskState.FAILED.value


def test_mac_worker_resolves_hub_repository_path_to_local_self_update_repo(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    metadata = _repository_task_metadata(Path("/home/jkh/.mac/src/mac"))
    metadata["origin"]["repository_name"] = "mac"
    metadata["origin"]["source"] = "repo-beads-mac"
    task = cp.create_task(
        "Repository task with hub path",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        runtime = task_payload["metadata"]["runtime"]
        assert runtime["repository_declared_path"] == "/home/jkh/.mac/src/mac"
        assert runtime["repository_source_path"] == str(repo.resolve())
        worktree = Path(runtime["repository_worktree"])
        assert worktree.is_dir()
        _write_repo_worker_manifest(_task_dir, worktree)
        _git(worktree, "push", "origin", "HEAD:refs/heads/%s" % runtime["repository_branch"])
        return WorkerExecution(0, "repo worktree prepared", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        self_update_repo=repo,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"


def test_subprocess_executor_exports_repository_worktree_env(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Repository subprocess task",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))
    executor_script = tmp_path / "executor-env.py"
    executor_script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import json, os, subprocess",
                "workspace = Path(os.environ['MAC_TASK_WORKSPACE'])",
                "worktree = os.environ['MAC_TASK_REPO_WORKTREE']",
                "head = subprocess.check_output(['git', '-C', worktree, 'rev-parse', 'HEAD'], text=True).strip()",
                "payload = {",
                "  'worktree': worktree,",
                "  'source': os.environ.get('MAC_TASK_REPO_SOURCE'),",
                "  'branch': os.environ.get('MAC_TASK_REPO_BRANCH'),",
                "  'base_sha': os.environ.get('MAC_TASK_REPO_BASE_SHA'),",
                "}",
                "(workspace / 'env.json').write_text(json.dumps(payload), encoding='utf-8')",
                "manifest = {",
                "  'schema': 'mac.worker_evidence.v1',",
                "  'status': 'complete',",
                "  'evidence_type': 'test',",
                "  'repo': {",
                "    'head_sha': head,",
                "    'pushed': True,",
                "    'remote_ref': 'refs/heads/%s' % os.environ.get('MAC_TASK_REPO_BRANCH'),",
                "    'dirty': False,",
                "  },",
                "  'checks': [{'name': 'pytest', 'returncode': 0}],",
                "}",
                "(workspace / 'mac-evidence.json').write_text(json.dumps(manifest), encoding='utf-8')",
                "subprocess.check_call(['git', '-C', worktree, 'push', 'origin', 'HEAD:refs/heads/%s' % os.environ.get('MAC_TASK_REPO_BRANCH')])",
            ]
        ),
        encoding="utf-8",
    )
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        SubprocessExecutor([sys.executable, str(executor_script)]),
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    task_dir = tmp_path / "workspaces" / task.id
    env_record = json.loads((task_dir / "env.json").read_text(encoding="utf-8"))
    assert env_record["worktree"]
    assert Path(env_record["worktree"]).is_dir()
    assert env_record["source"] == str(repo.resolve())
    assert env_record["branch"].startswith("mac/")
    assert env_record["base_sha"] == _git(repo, "rev-parse", "HEAD")
    completed = next(
        record
        for record in cp.list_command_audit(agent_id=agent.id, task_id=task.id, limit=10)
        if record.phase == "completed"
    )
    assert completed.metadata["repository_checkout_policy"] == "task_owned_git_worktree"


def test_mac_worker_refuses_dirty_repository_source_for_normal_work(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    task = cp.create_task(
        "Dirty repository task",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        lambda _task, _task_dir: WorkerExecution(0, "should not run"),
    )

    with pytest.raises(RuntimeError, match="repository source checkout is dirty"):
        worker.run_once()

    assert cp.get_task(task.id).state == TaskState.FAILED.value
    observations = cp.list_observability(layer="worker", limit=20)
    assert any(item.name == "worker.repository.source_dirty" for item in observations)
    assert not any((tmp_path / "workspaces" / task.id).glob("repo-*"))


def test_source_remediation_task_can_target_dirty_registered_checkout(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    metadata = _repository_task_metadata(repo)
    metadata["origin"]["type"] = "beads_source_remediation"
    metadata["remediation"] = {
        "type": "beads_source_refresh",
        "repository_path": str(repo),
        "required_workflow": "git_pull_rebase_then_merge_local_changes",
    }
    task = cp.create_task(
        "Repair dirty source",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        assert "runtime" not in task_payload["metadata"]
        assert not any(task_dir.glob("repo-*"))
        _write_worker_manifest(task_dir)
        return WorkerExecution(0, "source repair inspected", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"


def test_source_remediation_repo_change_allows_empty_files_changed_in_worker(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    metadata = _repository_task_metadata(repo)
    metadata["origin"]["type"] = "beads_source_remediation"
    metadata["remediation"] = {
        "type": "beads_source_refresh",
        "repository_path": str(repo),
    }
    task = cp.create_task(
        "No-op source refresh",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        _write_worker_manifest(
            task_dir,
            evidence_type="repo_change",
            files_changed=[],
        )
        return WorkerExecution(0, "source already clean", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value


def test_mac_worker_renews_lease_while_executor_runs(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        time.sleep(0.05)
        _write_worker_manifest(_task_dir)
        return WorkerExecution(0, "tests passed", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        lease_seconds=60,
        lease_renew_interval_seconds=0.01,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert any(event.event_type == "task.lease_renewed" for event in cp.task_history(task.id))


def test_assignment_is_current_propagates_programming_errors_not_silently_true(tmp_path: Path):
    """mac-h3d: _assignment_is_current's exception net was bare
    ``except Exception`` and silently returned True. Narrowed to
    MacApiError so a TypeError from a malformed response (or any
    programming bug) bubbles instead of being treated as "still
    current" — that path lets a worker complete a task it doesn't
    own."""
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))

    original_get = api.get

    def crashing_get(path: str) -> Any:
        if path.startswith("/tasks/"):
            # Simulate a malformed response that would crash the
            # downstream .get("task", ...) call. Pre-fix this was caught
            # by the bare except and returned True; post-fix it should
            # bubble out of _assignment_is_current.
            raise TypeError("simulated malformed response")
        return original_get(path)

    api.get = crashing_get  # type: ignore[assignment]
    worker = MacWorker(api, agent.id, tmp_path, lambda _t, _d: WorkerExecution(0, "ok"))
    # Direct call — no task in flight, but the helper should now raise
    # the TypeError instead of swallowing it.
    with pytest.raises(TypeError):
        worker._assignment_is_current("task_doesnt_matter", "lease_doesnt_matter")


def test_mac_worker_does_not_mutate_task_after_losing_lease(tmp_path: Path):
    cp = ControlPlane.in_memory()
    first = register_worker_fixture(cp)
    machine = cp.register_machine("second-worker-host")
    second = cp.register_agent(machine.id, "second-worker", capabilities=["python"])
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
            timespec="microseconds"
        )
        cp.expire_leases(now=future)
        cp.claim_task(task.id, second.id)
        cp.start_task(task.id, second.id)
        return WorkerExecution(0, "late success", stdout="late success\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        first.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "stale_result"
    current = cp.get_task(task.id)
    assert current.state == TaskState.RUNNING.value
    assert current.owner_agent_id == second.id
    assert cp.list_evidence(task.id) == []
    observations = cp.list_observability(layer="worker", limit=20)
    assert any(item.name == "worker.execution.stale_result" for item in observations)


def test_mac_worker_run_forever_drains_queue_then_reports_offline(tmp_path: Path):
    cp = ControlPlane.in_memory()
    # Capacity is above one so the loop can drain several assignments in a
    # single bounded run; submitted tasks release their executor lease at review.
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(
        machine.id, "worker", capabilities=["python"], resources={"capacity": 3}
    )
    task_ids = [
        cp.create_task("work-%d" % i, required_capabilities=["python"]).id
        for i in range(3)
    ]
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        _write_worker_manifest(_task_dir)
        return WorkerExecution(0, "ok", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        poll_interval_seconds=0.0,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    # max_iterations bounds the loop so the test doesn't hang.
    results = worker.run_forever(max_iterations=5)

    submitted = [r for r in results if r.status == "submitted_for_review"]
    assert {r.task["id"] for r in submitted} == set(task_ids)
    # After the loop the worker marks itself offline (best-effort heartbeat).
    refreshed = cp.get_agent(agent.id)
    assert refreshed.status == "offline"


def test_mac_worker_restores_prior_signal_handlers_after_run_forever(tmp_path: Path):
    import signal

    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))

    def sentinel_handler(*_args):
        return None

    prior_term = signal.signal(signal.SIGTERM, sentinel_handler)
    prior_int = signal.signal(signal.SIGINT, sentinel_handler)
    try:
        worker = MacWorker(
            MacApiClient("http://mac.test", transport=api_transport(client)),
            agent.id,
            tmp_path,
            lambda _t, _d: WorkerExecution(0, "ok"),
            poll_interval_seconds=0.0,
        )
        worker.run_forever(max_iterations=2)

        # The worker must have restored the handlers it found, not left its
        # own stop-callback installed for the rest of the process.
        assert signal.getsignal(signal.SIGTERM) is sentinel_handler
        assert signal.getsignal(signal.SIGINT) is sentinel_handler
    finally:
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_int)


def test_mac_worker_run_forever_tolerates_failing_offline_heartbeat(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    cp.create_task("one", required_capabilities=["python"])

    underlying = TestClient(create_app(control_plane=cp))

    class _FlakyTransport:
        def __init__(self) -> None:
            self.heartbeat_call = 0

        def __call__(self, method: str, path: str, payload):
            # Fail the offline heartbeat that fires from _shutdown.
            if (
                method == "POST"
                and "/agents/" in path
                and path.endswith("/heartbeat")
                and isinstance(payload, dict)
                and payload.get("status") == "offline"
            ):
                raise MacApiError("simulated network failure on shutdown")
            return api_transport(underlying)(method, path, payload)

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_FlakyTransport()),
        agent.id,
        tmp_path,
        lambda _t, _d: (_write_worker_manifest(_d), WorkerExecution(0, "ok"))[1],
        poll_interval_seconds=0.0,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    # Should not raise — _shutdown swallows transport errors.
    results = worker.run_forever(max_iterations=2)
    assert any(r.status == "submitted_for_review" for r in results)


def test_mac_worker_processes_agentbus_repo_update_and_requests_restart(tmp_path: Path):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    seed, work = _git_fixture(tmp_path)
    expected = _commit_fixture_update(seed, "two\n")
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=REPO_UPDATE_CONTENT_TYPE,
        topic=REPO_UPDATE_TOPIC,
        payload={
            "schema": REPO_UPDATE_SCHEMA,
            "remote": "origin",
            "branch": "main",
            "restart": True,
            "request_id": "req-1",
        },
    )
    client = TestClient(create_app(control_plane=cp))

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
        self_update_repo=work,
    )
    result = worker.run_once()

    assert result.status == "self_update_restart"
    assert _git(work, "rev-parse", "HEAD") == expected
    result_streams = [
        stream
        for stream in cp.list_agentbus_streams(agent_id=sender.id, status="closed")
        if stream.topic == REPO_UPDATE_RESULT_TOPIC
    ]
    assert result_streams
    chunks = cp.read_agentbus_chunks(sender.id, result_streams[0].id)
    assert chunks[0].payload["status"] == "updated"
    assert chunks[0].payload["restart_requested"] is True
    assert chunks[0].payload["request_id"] == "req-1"


def test_mac_worker_repo_update_noops_without_restart_when_current(tmp_path: Path):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    _seed, work = _git_fixture(tmp_path)
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=REPO_UPDATE_CONTENT_TYPE,
        topic=REPO_UPDATE_TOPIC,
        payload={"schema": REPO_UPDATE_SCHEMA, "remote": "origin", "branch": "main"},
    )
    client = TestClient(create_app(control_plane=cp))

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
        self_update_repo=work,
    )
    result = worker.run_once()

    assert result.status == "no_task"
    result_streams = [
        stream
        for stream in cp.list_agentbus_streams(agent_id=sender.id, status="closed")
        if stream.topic == REPO_UPDATE_RESULT_TOPIC
    ]
    assert result_streams
    chunks = cp.read_agentbus_chunks(sender.id, result_streams[0].id)
    assert chunks[0].payload["status"] == "no_update"
    assert chunks[0].payload["restart_requested"] is False


def test_mac_worker_declares_running_digest_on_first_heartbeat(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    runtime = cp.create_runtime(
        "worker-runtime",
        {"image": "python:3.12@sha256:abc123", "dependencies": ["fastapi==0.111.0"]},
        "human",
    )
    cp.create_task("declared", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_t: Dict[str, Any], _d: Path) -> WorkerExecution:
        _write_worker_manifest(_d)
        return WorkerExecution(0, "ok")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        running_digest=runtime.digest,
    )
    worker.run_once()

    refreshed = cp.get_agent(agent.id)
    assert refreshed.running_digest == runtime.digest
    distribution = cp.fleet_build_distribution()
    by_digest = {b["digest"]: b for b in distribution["buckets"]}
    assert by_digest[runtime.digest]["count"] == 1


def test_register_worker_creates_identity_then_worker_claims_tasks(tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))

    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
        resources={"capacity": 2},
    )
    task = cp.create_task("registered worker task", required_capabilities=["python"])

    worker = MacWorker(
        api,
        registered["id"],
        tmp_path,
        lambda _t, _d: (_write_worker_manifest(_d), WorkerExecution(0, "ok", stdout="ok\n"))[1],
        attestation_key=registered["attestation_key"],
    )
    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert result.task["id"] == task.id
    assert cp.get_agent(registered["id"]).name == "rocky"
    assert cp.get_agent(registered["id"]).capabilities == ["python"]
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value


def test_register_worker_binds_agent_to_hermes_instance():
    cp = ControlPlane.in_memory()
    tenant = cp.register_tenant("fleet")
    hermes = cp.register_hermes_instance(tenant.id, "rocky")
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))

    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
        hermes_instance_id=hermes.id,
    )

    assert registered["hermes_instance_id"] == hermes.id
    assert cp.get_agent(registered["id"]).hermes_instance_id == hermes.id


def test_register_worker_bootstraps_hermes_identity_from_env(monkeypatch, tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    monkeypatch.setenv("MAC_FLEET_TENANT_ID", "tenant_mac")
    monkeypatch.setenv("MAC_HERMES_PERSONA_ID", "persona_natasha")
    monkeypatch.setenv("MAC_AGENT_ID", "agent_natasha")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    registered = register_worker(
        api,
        hostname="sparky.local",
        agent_name="natasha",
        capabilities=["python"],
        hermes_instance_id="hermes_natasha",
    )

    assert registered["hermes_instance_id"] == "hermes_natasha"
    assert cp.get_hermes_instance("hermes_natasha").persona_id == "persona_natasha"
    assert cp.get_agent(registered["id"]).hermes_instance_id == "hermes_natasha"


def test_worker_detects_stale_local_attestation_key():
    from mac.worker import _attestation_key_matches_hub

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))

    registered = register_worker(
        api,
        hostname="natasha.local",
        agent_name="natasha",
        capabilities=["review"],
    )
    local_key = registered["attestation_key"]
    assert _attestation_key_matches_hub(api, registered["id"], local_key) is True

    cp.rotate_agent_attestation_key(registered["id"])

    assert _attestation_key_matches_hub(api, registered["id"], local_key) is False


def test_mac_worker_dry_run_claim_uses_canary_policy_without_leasing(tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
    )
    normal = cp.create_task(
        "normal",
        project="mac-canary",
        priority=100,
        required_capabilities=["python"],
    )
    canary = cp.create_task(
        "canary",
        project="mac-canary",
        priority=10,
        required_capabilities=["python"],
        metadata={"canary": True},
    )
    worker = MacWorker(
        api,
        registered["id"],
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "unused"),
        allowed_projects=["mac-canary"],
        require_canary=True,
    )

    assignment = worker.dry_run_claim()

    assert assignment is not None
    assert assignment["task"]["id"] == canary.id
    assert assignment["lease"] is None
    assert cp.get_task(normal.id).state == TaskState.OPEN.value
    assert cp.get_task(canary.id).state == TaskState.OPEN.value
    names = {item.name for item in cp.list_observability(layer="worker", limit=20)}
    assert "worker.routing.policy" in names
    assert "worker.routing.dry_run_result" in names


def test_mac_worker_completes_task_even_if_observability_writes_fail(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    api = MacApiClient("http://mac.test", transport=api_transport(client))
    original_post = api.post

    def broken_post(path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if path.startswith("/observability/"):
            raise MacApiError("observability sink is down")
        return original_post(path, payload)

    api.post = broken_post  # type: ignore[assignment]

    worker = MacWorker(
        api,
        agent.id,
        tmp_path,
        lambda _t, _d: (_write_worker_manifest(_d), WorkerExecution(0, "ok", stdout="ok\n"))[1],
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    result = worker.run_once()

    assert result.status == "submitted_for_review"
    assert result.task["id"] == task.id
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
