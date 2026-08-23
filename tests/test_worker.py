import base64
from pathlib import Path
import json
import subprocess
import sys
import threading
import time
import types
from typing import Any, Dict, Optional

import pytest

from fastapi.testclient import TestClient

from mac.agentbus_control import (
    DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
    DEBUG_TERMINAL_INPUT_SCHEMA,
    DEBUG_TERMINAL_INPUT_TOPIC,
    DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
    DEBUG_TERMINAL_OPEN_SCHEMA,
    DEBUG_TERMINAL_OPEN_TOPIC,
    DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
    DEBUG_TERMINAL_OUTPUT_SCHEMA,
    DEBUG_TERMINAL_OUTPUT_TOPIC,
    debug_terminal_input_payload,
    debug_terminal_open_payload,
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_SCHEMA,
    REPO_UPDATE_TOPIC,
)
from mac.api import create_app
from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA, codegraph_relevant_files
from mac.fleet_learning import (
    REPOSITORY_ACCESS_RECORD_TYPE,
    build_repository_access_learning,
    build_repository_access_memory_payload,
    parse_repository_access_learning,
)
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane, sign_verification_manifest
from mac.worker import (
    MacWorker,
    SubprocessExecutor,
    WorkerExecution,
    _detect_command_inventory,
    _openshell_containerfile_changed,
    build_parser,
    register_worker,
)


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


def test_worker_claim_request_is_transport_only_and_policy_is_hub_visible(tmp_path: Path):
    worker = MacWorker(
        object(),  # type: ignore[arg-type]
        "agent_policy",
        tmp_path,
        lambda _task, _directory: WorkerExecution(0, "unused"),
        lease_seconds=321,
        allowed_projects=["mac", "nanolang"],
        required_metadata={"language": "python"},
        claim_only_canary_tasks=True,
    )

    assert worker._claim_payload(dry_run=False) == {
        "lease_seconds": 321,
        "dry_run": False,
    }
    assert worker._dispatch_policy_resource() == {
        "schema": "mac.dispatch_policy.v2",
        "preferred_projects": ["mac", "nanolang"],
    }


def _codegraph_fixture(files: list[str]) -> Dict[str, Any]:
    relevant_files = codegraph_relevant_files(files)
    return {
        "schema": CODEGRAPH_AUDIT_SCHEMA,
        "status": "pass",
        "reason": "test_fixture",
        "relevant_files": relevant_files,
        "commands": [
            {"argv": ["codegraph", "sync"], "returncode": 0},
            {"argv": ["codegraph", "affected"], "returncode": 0},
        ],
    }


def test_mac_worker_cli_defaults_to_deployed_hub_env(monkeypatch):
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    # MAC_WORKER_HERMES_INSTANCE_ID takes precedence over MAC_HERMES_INSTANCE_ID
    # in the parser default; a deployed worker host leaks its own value here, so
    # clear it to keep this assertion about MAC_HERMES_INSTANCE_ID hermetic.
    monkeypatch.delenv("MAC_WORKER_HERMES_INSTANCE_ID", raising=False)
    monkeypatch.setenv("MAC_HUB_URL", "http://hub.example.internal:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker-token")
    monkeypatch.setenv("MAC_HERMES_INSTANCE_ID", "hermes_rocky")

    args = build_parser().parse_args(["--register", "--heartbeat-only"])

    assert args.url == "http://hub.example.internal:8789"
    # Token resolution is deferred to main() so a fleet-scoped value can win
    # over the legacy flat form (mac-g55y); the parser leaves it unset.
    assert args.token is None
    from mac.fleet_env import resolve_first

    assert (
        resolve_first(
            ["MAC_TOKEN", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"], fleet=args.fleet
        )
        == "worker-token"
    )
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
    relevant_files = codegraph_relevant_files(files_changed or [])
    manifest: Dict[str, Any] = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": evidence_type,
        "repo": repo,
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    if relevant_files:
        manifest["codegraph"] = _codegraph_fixture(files_changed or [])
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


def test_run_forever_survives_run_once_exception(tmp_path: Path):
    # loop-01: one task's error (here the executor raising) must NOT crash the
    # worker loop and halt all autonomous work. run_once best-effort-fails the
    # task and re-raises; run_forever must catch, record, and keep polling.
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def boom_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        raise RuntimeError("executor blew up")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        boom_executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    # Does not raise: the loop absorbs the exception and returns a result.
    results = worker.run_forever(max_iterations=1)
    assert results and results[-1].status == "error"
    assert "executor blew up" in (results[-1].error or "")
    # The task was best-effort-blocked before the re-raise.
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


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
    assert reviewed.state == TaskState.REVIEWING.value
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


def test_mac_worker_executes_assignment_already_claimed_by_dispatcher(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Push-dispatched task", required_capabilities=["python"])
    assignment = cp.dispatch_once()
    assert assignment is not None
    assert assignment["task"]["id"] == task.id

    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        assert task_payload["id"] == task.id
        _write_worker_manifest(task_dir)
        return WorkerExecution(0, "resumed dispatcher assignment")

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
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    assert any(
        row.name == "worker.routing.resumed" and row.subject_id == task.id
        for row in cp.list_observability(
            layer="worker",
            name="worker.routing.resumed",
            subject_id=task.id,
            limit=200,
        )
    )


def test_mac_worker_submits_durable_evidence_artifacts(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task(
        "Artifact capture task",
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
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(
                {
                    "schema": "mac.worker_evidence.v1",
                    "status": "complete",
                    "evidence_type": "operator_result",
                    "summary": "Captured output",
                    "result": "The worker output was captured durably.",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (task_dir / "openshell-salvage.json").write_text(
            json.dumps(
                {
                    "schema": "mac.openshell_salvage.v1",
                    "harvested": True,
                    "progress": {"changed_file_digest": "sha256:test"},
                }
            ),
            encoding="utf-8",
        )
        return WorkerExecution(
            0,
            "captured output",
            stdout="durable stdout\n",
            stderr="durable stderr\n",
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    evidence = cp.list_evidence(task.id)[0]
    index = evidence.metadata["durable_artifacts"]["artifacts"]
    by_name = {item["name"]: item for item in index}
    assert {
        "worker-result.json",
        "stdout.txt",
        "stderr.txt",
        "mac-evidence.json",
        "openshell-salvage.json",
    } <= set(by_name)
    assert by_name["openshell-salvage.json"]["sha256"].startswith("sha256:")
    assert "content_base64" not in by_name["stdout.txt"]
    stdout_artifact = cp.get_evidence_artifact(evidence.id, by_name["stdout.txt"]["id"])
    stderr_artifact = cp.get_evidence_artifact(evidence.id, by_name["stderr.txt"]["id"])
    assert base64.b64decode(stdout_artifact["content_base64"]) == b"durable stdout\n"
    assert base64.b64decode(stderr_artifact["content_base64"]) == b"durable stderr\n"


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
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_mac_worker_processes_review_nudge_and_records_signed_verdict(tmp_path: Path, semantic_reviewer_on):
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
        "codegraph": _codegraph_fixture(["src/example.py"]),
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
            "codegraph": _codegraph_fixture(["src/example.py"]),
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


def test_review_nudge_prepares_review_worktree_and_git_main_publication(tmp_path: Path, semantic_reviewer_on):
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
    hub_checkout_head = _git(repo, "rev-parse", "HEAD")

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
        assert context["review_claim"]["reviewer_agent_id"] == reviewer.id
        assert context["review_claim"]["executor_evidence_id"] == evidence.id
        assert "project" not in context["review_claim"]
        assert "repository_worktree" not in context["review_claim"]
        assert "repository_files_changed" not in context["review_claim"]
        review_worktree = Path(runtime["repository_worktree"])
        assert review_worktree.is_dir()
        assert _git(review_worktree, "rev-parse", "HEAD") == reviewed_head
        assert _git(review_worktree, "remote", "get-url", "origin") == remote_url
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
    publication_gate_calls: list[tuple[str, str, str, str]] = []

    def publication_gate(
        repo_dir: str, projected_branch: str, projected_sha: str, command: str
    ) -> tuple[int, str]:
        publication_gate_calls.append(
            (repo_dir, projected_branch, projected_sha, command)
        )
        return 0, "projected full contract passed"

    cp._publication_merge_test_runner = publication_gate

    result = worker.run_once()

    assert result.status == "review_verdict_recorded"
    verdict_manifest = cp.list_evidence(task.id)[-1].metadata["verification"]
    assert verdict_manifest["repo"]["remote_ref"] == "refs/heads/%s" % branch
    assert len(publication_gate_calls) == 1
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert cp.list_publications(task.id)[0].target == "git://main"
    # Publication is isolated from the long-lived hub checkout. The remote
    # canonical branch advances, while this checkout stays untouched.
    assert _git(repo, "rev-parse", "HEAD") == hub_checkout_head
    assert (
        _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        == reviewed_head
    )
    _git(repo, "fetch", "origin", "main")
    assert _git(repo, "rev-parse", "origin/main") == reviewed_head
    learnings = cp.search_memory(
        subject_type="agent",
        subject_id=reviewer.id,
        record_type=REPOSITORY_ACCESS_RECORD_TYPE,
    )
    parsed = [parse_repository_access_learning(item.content) for item in learnings]
    assert [item["outcome"] for item in parsed if item is not None] == ["success"]
    assert parsed[0] is not None and parsed[0]["credential_source"] == "local"


def test_private_review_clone_uses_env_token_but_persists_only_clean_remote(
    tmp_path: Path,
    monkeypatch,
):
    remote_url = "https://github.com/acme/private.git"
    token = "review-token-secret"
    head_sha = "abc123abc123abc123abc123abc123abc123abcd"
    task_detail = {
        "task": {"id": "task-private", "project": "demo"},
        "evidence": [
            {
                "id": "ev-private",
                "metadata": {
                    "verification": {
                        "repo": {
                            "head_sha": head_sha,
                            "base_sha": "def456def456def456def456def456def456def4",
                            "remote_ref": "refs/heads/mac/private-review",
                            "remote_url": remote_url,
                        }
                    }
                },
            }
        ],
    }

    class RecordingClient:
        def __init__(self):
            self.posts = []

        def post(self, path, payload):
            self.posts.append((path, payload))
            return {"id": "mem-private"} if path == "/memory" else {}

    client = RecordingClient()
    worker = MacWorker(client, "agent-reviewer", tmp_path, lambda *_args: None)
    commands = []

    def successful_git(argv, *args, **kwargs):
        command = list(argv)
        commands.append(command)
        if command[:3] == ["git", "clone", "--no-checkout"]:
            Path(command[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.setattr("mac.worker.subprocess.run", successful_git)
    task_dir = tmp_path / "review"
    task_dir.mkdir()

    context = worker._prepare_review_repository_worktree(
        task_dir,
        task_detail,
        "ev-private",
        "review-private",
    )

    clone = next(command for command in commands if command[:3] == ["git", "clone", "--no-checkout"])
    assert "x-access-token:%s@github.com" % token in clone[4]
    scrub = next(command for command in commands if "set-url" in command)
    assert scrub[-1] == remote_url
    fetch = next(command for command in commands if "fetch" in command)
    assert any("x-access-token:%s@github.com" % token in arg for arg in fetch)
    assert context is not None and context["repository_origin_remote"] == remote_url
    serialized_context = (task_dir / "repository-worktree.json").read_text(
        encoding="utf-8"
    )
    assert token not in serialized_context
    memory_payload = next(payload for path, payload in client.posts if path == "/memory")
    assert token not in json.dumps(memory_payload, sort_keys=True)
    learning = parse_repository_access_learning(memory_payload["content"])
    assert learning is not None
    assert learning["outcome"] == "success"
    assert learning["credential_source"] == "env:GH_TOKEN"


def test_review_auth_failure_learns_and_reassigns_to_successful_peer(
    tmp_path: Path,
    monkeypatch,
    semantic_reviewer_on,
):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("review-host")
    executor_agent = cp.register_agent(machine.id, "executor", capabilities=["python"])
    failing_reviewer = cp.register_agent(machine.id, "a-failing", capabilities=["review"])
    successful_reviewer = cp.register_agent(machine.id, "b-success", capabilities=["review"])
    remote_url = "https://github.com/acme/private.git"
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "demo",
        "canonical_remote_url": remote_url,
    }
    task = cp.create_task(
        "Review private repository",
        project="demo",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://publish",
            "execution_contract": {
                "type": "repository",
                "repository_contract": contract,
            },
            "origin": {
                "repository_url": remote_url,
                "repository_contract": contract,
            },
        },
    )
    cp.claim_task(task.id, executor_agent.id)
    cp.start_task(task.id, executor_agent.id)
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abc123abc123abc123abc123abc123abc123abcd",
            "base_sha": "def456def456def456def456def456def456def4",
            "remote_ref": "refs/heads/mac/review-proof",
            "remote_url": remote_url,
            "pushed": True,
            "dirty": False,
            "files_changed": ["src/example.py"],
        },
        "codegraph": _codegraph_fixture(["src/example.py"]),
        "checks": [{"name": "pytest", "status": "passed", "returncode": 0}],
        "signed_by": executor_agent.id,
    }
    executor_manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor_agent.id), executor_manifest
    )
    monkeypatch.setenv("MAC_VALIDATE_REMOTE_REFS", "0")
    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/executor-result.json",
        "executor completed",
        executor_agent.id,
        metadata={"returncode": 0, "verification": executor_manifest},
    )
    cp.submit_for_review(task.id, executor_agent.id)

    known_success = build_repository_access_learning(
        project="demo",
        remote=remote_url,
        operation="review_clone",
        agent_id=successful_reviewer.id,
        outcome="success",
        credential_source="env:GH_TOKEN",
    )
    cp.add_memory(**build_repository_access_memory_payload(known_success))
    first_review = cp.request_review(
        task.id,
        failing_reviewer.id,
        actor="test",
    )
    first_tick = cp.advance_default_review_workflow(task.id)
    assert first_tick["review_id"] == first_review.id
    assert first_tick["executor_evidence_id"] == evidence.id

    client = TestClient(create_app(control_plane=cp))
    real_run = subprocess.run

    def fail_private_clone(argv, *args, **kwargs):
        if list(argv[:3]) == ["git", "clone", "--no-checkout"]:
            return subprocess.CompletedProcess(
                argv,
                128,
                "",
                "fatal: could not read Username for 'https://github.com': "
                "No such device or address",
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("mac.worker.subprocess.run", fail_private_clone)
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        failing_reviewer.id,
        tmp_path / "workspaces",
        lambda *_args: pytest.fail("review executor must not run after clone failure"),
        attestation_key=cp._agent_attestation_key(failing_reviewer.id),
    )

    result = worker.run_once()

    assert result.status == "review_verdict_failed"
    assert "could not read Username" in (result.error or "")
    assert not (
        tmp_path / "workspaces" / "_reviews" / first_review.id / "review-repo"
    ).exists()
    reviews = cp.list_reviews(task.id)
    assert [review.status for review in reviews] == [
        ReviewStatus.RETRACTED.value,
        ReviewStatus.PENDING.value,
    ]
    assert reviews[0].reviewer_agent_id == failing_reviewer.id
    assert "reviewer_repository_access_authentication:github.com" in (
        reviews[0].reason or ""
    )
    assert reviews[1].reviewer_agent_id == successful_reviewer.id
    memories = cp.search_memory(
        subject_type="agent",
        subject_id=failing_reviewer.id,
        record_type=REPOSITORY_ACCESS_RECORD_TYPE,
    )
    failure = parse_repository_access_learning(memories[-1].content)
    assert failure is not None
    assert failure["outcome"] == "failure"
    assert failure["failure_class"] == "authentication"
    assert "No such device or address" in failure["error_signature"]


def test_mac_worker_skips_stale_review_nudge_and_processes_next(tmp_path: Path, semantic_reviewer_on):
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
            "codegraph": _codegraph_fixture(["src/example.py"]),
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
            "codegraph": _codegraph_fixture(["src/example.py"]),
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
    client = TestClient(create_app(control_plane=cp, auth_tokens={}))
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
            "text": "[task.completed] Rocky completed lifecycle proof",
        },
        {
            "token": "xoxb-two",
            "channel": "C2",
            "text": "[task.completed] Rocky completed lifecycle proof",
        },
    ]
    assert cp.list_messages(agent.id)[0].status == "delivered"
    assert any(
        event.name == "worker.notifier.status_forwarded"
        for event in cp.list_observability(limit=20)
    )


def test_mac_worker_records_failed_execution_and_blocks_task(tmp_path: Path):
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

    assert result.status == "blocked"
    assert result.error == "pytest failed"
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    evidence = cp.list_evidence(task.id)
    assert evidence[0].metadata["returncode"] == 2


def test_mac_worker_marks_openshell_verifier_start_failure_retryable(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        return WorkerExecution(
            1,
            "executor failed with returncode 1",
            stderr=(
                "[executor] WARNING: sandbox repository verification failed: "
                "OpenShell repository verifier did not start within 60.0s\n"
            ),
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "blocked"
    detail = cp.task_history(task.id)[-1].detail
    assert detail["failure"] == "openshell_repository_verifier_start_failed"
    assert detail["manual_repair_required"] is False
    assert "did not start within 60.0s" in detail["output_tail"]


def test_mac_worker_marks_openshell_verifier_eagain_retryable(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Python task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        return WorkerExecution(
            68,
            "executor failed with returncode 68",
            stderr=(
                "OpenShell repository verification failed "
                "(verifier_infrastructure): could not launch OpenShell "
                "repository verifier: BlockingIOError: "
                "Resource temporarily unavailable\n"
            ),
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
    )

    result = worker.run_once()

    assert result.status == "blocked"
    detail = cp.task_history(task.id)[-1].detail
    assert detail["failure"] == "openshell_repository_verifier_transport_failed"
    assert detail["manual_repair_required"] is False
    assert "Resource temporarily unavailable" in detail["output_tail"]


def test_mac_worker_blocks_successful_execution_without_verification_manifest(tmp_path: Path):
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

    assert result.status == "blocked"
    assert "status" in (result.error or "")
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
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
            "operator_result": {
                "summary": "Plan produced",
                "result": "Story graph and verification plan produced.",
            },
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
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
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


def test_subprocess_executor_timeout_kills_descendants_in_other_sessions(tmp_path: Path):
    executor_script = tmp_path / "executor_tree.py"
    executor_script.write_text(
        "\n".join(
            [
                "import os, subprocess, sys, time",
                "from pathlib import Path",
                "workspace = Path(os.environ['MAC_TASK_WORKSPACE'])",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)",
                "(workspace / 'child.pid').write_text(str(child.pid), encoding='utf-8')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    executor = SubprocessExecutor([sys.executable, str(executor_script)], timeout=0.5)

    with pytest.raises(subprocess.TimeoutExpired):
        executor({"id": "task_timeout_tree"}, task_dir)

    child_pid = int((task_dir / "child.pid").read_text(encoding="utf-8"))
    import psutil

    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_subprocess_executor_explicit_cancel_is_audited(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    executor = SubprocessExecutor(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=30.0
    )
    records: list[Dict[str, Any]] = []
    executor.audit_sink = records.append
    result: list[WorkerExecution] = []

    thread = threading.Thread(
        target=lambda: result.append(executor({"id": "task_cancel_tree"}, task_dir))
    )
    thread.start()
    deadline = time.monotonic() + 5.0
    while not executor.has_active_process() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert executor.cancel_current("ledger task cancelled") is True
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result and result[0].returncode != 0
    cancelled = [item for item in records if item.get("phase") == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["metadata"]["cancel_reason"] == "ledger task cancelled"


def test_worker_cancels_executor_tree_when_ledger_assignment_is_cancelled(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("cancel running executor", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))
    executor = SubprocessExecutor(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=30.0
    )
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        lease_seconds=60,
        lease_renew_interval_seconds=0.05,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    results: list[Any] = []
    thread = threading.Thread(target=lambda: results.append(worker.run_once()))
    thread.start()
    deadline = time.monotonic() + 5.0
    while not executor.has_active_process() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert executor.has_active_process()

    cp._transition_task_internal(
        task.id,
        TaskState.CANCELLED.value,
        "operator",
        {"reason": "test cancellation"},
    )
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert results and results[0].status == "stale_result"
    assert cp.list_evidence(task.id) == []
    observations = cp.list_observability(layer="worker", limit=50)
    cancelled = [item for item in observations if item.name == "worker.execution.cancelled"]
    assert cancelled and cancelled[0].detail["process_tree_terminated"] is True


def test_worker_timeout_harvests_finalizer_progress_artifact(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("timeout with partial finalizer progress", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))
    executor_script = tmp_path / "timeout_executor.py"
    executor_script.write_text(
        "\n".join(
            [
                "import json, os, time",
                "from pathlib import Path",
                "workspace = Path(os.environ['MAC_TASK_WORKSPACE'])",
                "progress = {'schema': 'mac.finalizer_progress.v1', 'phase': 'guarded_push', 'status': 'running'}",
                "(workspace / 'finalizer-progress.json').write_text(json.dumps(progress), encoding='utf-8')",
                "bundle_name = 'repository-wip-lease-timeout-deadbeef.bundle'",
                "(workspace / bundle_name).write_bytes(b'# v2 git bundle\\nfixture')",
                "wip = {'schema': 'mac.repository_wip_bundle.v1', 'status': 'preserved', 'bundle_name': bundle_name}",
                "(workspace / 'repository-wip.json').write_text(json.dumps(wip), encoding='utf-8')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        SubprocessExecutor([sys.executable, str(executor_script)], timeout=0.5),
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "blocked"
    assert result.evidence is not None
    artifacts = result.evidence["metadata"]["durable_artifacts"]["artifacts"]
    progress_artifacts = [
        artifact for artifact in artifacts if artifact["artifact_type"] == "finalizer_progress"
    ]
    assert len(progress_artifacts) == 1
    assert progress_artifacts[0]["name"] == "finalizer-progress.json"
    artifact_types = {
        artifact["artifact_type"]: artifact for artifact in artifacts
    }
    assert artifact_types["repository_wip"]["name"] == "repository-wip.json"
    assert (
        artifact_types["repository_wip_bundle"]["name"]
        == "repository-wip-lease-timeout-deadbeef.bundle"
    )
    history = cp.task_history(task.id)
    timeout_transition = [
        item
        for item in history
        if item.event_type == "task.transitioned" and item.to_state == TaskState.BLOCKED.value
    ][-1]
    assert timeout_transition.detail["reason"] == "executor_timeout"
    assert timeout_transition.detail["process_tree_terminated"] is True


def test_validate_git_remote_url_rejects_argv_smuggling():
    """mac-raud: remote_url from worker-supplied evidence must not be
    interpretable as a git option."""
    from mac.worker import _validate_git_remote_url

    # legitimate URLs pass
    assert _validate_git_remote_url("https://github.com/foo/bar.git") == "https://github.com/foo/bar.git"
    assert _validate_git_remote_url("git@github.com:foo/bar.git") == "git@github.com:foo/bar.git"
    assert _validate_git_remote_url("ssh://git@host/repo") == "ssh://git@host/repo"
    # file:// is allowed (used by test fixtures and legit local mirrors)
    assert _validate_git_remote_url("file:///tmp/repo") == "file:///tmp/repo"

    # smuggling attempts rejected
    for hostile in [
        "--upload-pack=/tmp/x",
        "-c something",
        "--config=user.name=evil",
        "",  # empty
        "x" * 3000,  # oversized
        "http://host with space/repo",
        "ftp://bad-scheme/repo",
    ]:
        with pytest.raises(ValueError):
            _validate_git_remote_url(hostile)


def test_worker_cli_has_default_executor_timeout():
    """mac-ehch: without a default --timeout, a wedged executor keeps
    renewing its lease forever because the renew thread only stops when
    subprocess.run returns. Confirm the CLI default is now finite."""
    from mac.worker import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "--url", "http://localhost:0",
        "--agent-id", "agent_test",
        "--workspace", "/tmp/wf",
        "--executor", "/bin/true",
    ])
    assert args.timeout is not None
    assert 60 <= args.timeout <= 24 * 3600, (
        f"default --timeout {args.timeout} outside the reasonable 1m–24h window"
    )


def test_validate_git_ref_rejects_flags_and_meta_chars():
    """mac-raud: remote_ref must look like a git ref, not a flag."""
    from mac.worker import _validate_git_ref

    assert _validate_git_ref("refs/heads/main") == "refs/heads/main"
    assert _validate_git_ref("main") == "main"
    for hostile in ["", "-flag", "--config", "branch with space", "a;b", "a$b", "$(injection)"]:
        with pytest.raises(ValueError):
            _validate_git_ref(hostile)


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


def test_mac_worker_finalizes_dirty_repository_despite_incomplete_manifest(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    metadata = _repository_task_metadata(repo)
    metadata["execution_contract"]["evidence_type"] = "documentation"
    task = cp.create_task(
        "Repository docs finalization task",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        (worktree / "README.md").write_text("demo\n", encoding="utf-8")
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
    assert repo_anchor["files_changed"] == ["README.md"]
    branch = repo_anchor["remote_ref"].removeprefix("refs/heads/")
    assert _git(repo, "ls-remote", "origin", "refs/heads/%s" % branch).startswith(
        repo_anchor["head_sha"]
    )


def test_mac_worker_finalizes_missing_repository_manifest(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Repository task forgot manifest",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        (worktree / "README.md").write_text("executor changed this\n", encoding="utf-8")
        return WorkerExecution(0, "changed repo without evidence", stdout="ok\n")

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
    assert manifest["status"] == "complete"
    assert manifest["evidence_type"] == "repo_change"
    assert manifest["tests"][0]["returncode"] == 0
    assert repo_anchor["dirty"] is False
    assert repo_anchor["pushed"] is True
    assert repo_anchor["files_changed"] == ["README.md"]
    assert _git(repo, "ls-remote", "origin", repo_anchor["remote_ref"]).startswith(
        repo_anchor["head_sha"]
    )


def test_mac_worker_auto_rebases_when_canonical_advances_cleanly(
    tmp_path: Path,
    monkeypatch,
):
    """Canonical advanced (non-conflicting) while the task worked -> the
    finalizer rebases BEFORE the contract test and publishes. Previously this
    blocked with "canonical tip is not an ancestor", killing every task slower
    than its fleet peers."""
    monkeypatch.setattr(
        "mac.worker.run_codegraph_audit",
        lambda _worktree, files: _codegraph_fixture(list(files)),
    )
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Repository task prepared on stale base",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        (worktree / "README.md").write_text("task edit\n", encoding="utf-8")
        (seed / "canonical.txt").write_text("canonical advanced\n", encoding="utf-8")
        _git(seed, "add", "canonical.txt")
        _git(seed, "commit", "-m", "canonical advance")
        _git(seed, "push", "origin", "main")
        return WorkerExecution(0, "changed repo after canonical advanced", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    manifest = cp.list_evidence(task.id)[0].metadata["verification"]
    assert manifest["repo"]["canonical_sync"]["status"] == "rebased"
    assert manifest["repo"]["pushed"] is True
    assert manifest["repo"]["freshness"]["ok"] is True
    # The rebased HEAD contains the canonical advance and the task's work.
    assert manifest["repo"]["freshness"]["canonical_tip_sha"] == _git(
        seed, "rev-parse", "HEAD"
    )


def test_mac_worker_blocks_publication_on_conflicting_canonical_advance(
    tmp_path: Path,
    monkeypatch,
):
    """A CONFLICTING canonical advance must still fail closed: the sync aborts
    its rebase (work intact) and the freshness gate reports precisely."""
    monkeypatch.setattr(
        "mac.worker.run_codegraph_audit",
        lambda _worktree, files: _codegraph_fixture(list(files)),
    )
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Repository task conflicting with canonical advance",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        # Both sides edit README.md -> guaranteed rebase conflict.
        (worktree / "README.md").write_text("task edit\n", encoding="utf-8")
        _commit_fixture_update(seed, "canonical advanced\n")
        return WorkerExecution(0, "conflicting repo change", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "blocked"
    manifest = cp.list_evidence(task.id)[0].metadata["verification"]
    assert manifest["repo"]["canonical_sync"]["status"] == "conflict"
    assert manifest["repo"]["pushed"] is False
    assert manifest["repo"]["freshness"]["ok"] is False
    assert "not an ancestor" in manifest["repo"]["freshness"]["error"]
    assert _git(repo, "ls-remote", "origin", manifest["repo"]["remote_ref"]) == ""


def test_mac_worker_publishes_after_merging_new_canonical_tip(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "mac.worker.run_codegraph_audit",
        lambda _worktree, files: _codegraph_fixture(list(files)),
    )
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Repository task refreshes canonical base",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        _commit_fixture_update(seed, "canonical advanced\n")
        _git(worktree, "fetch", "origin", "main")
        _git(worktree, "merge", "--ff-only", "FETCH_HEAD")
        (worktree / "README.md").write_text("task after merge\n", encoding="utf-8")
        return WorkerExecution(0, "merged canonical and changed repo", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review"
    manifest = cp.list_evidence(task.id)[0].metadata["verification"]
    assert manifest["repo"]["pushed"] is True
    assert manifest["repo"]["freshness"]["ok"] is True
    assert manifest["repo"]["freshness"]["canonical_tip_sha"] == _git(
        seed, "rev-parse", "HEAD"
    )


def test_mac_worker_does_not_push_invalid_finalized_repository_manifest(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    metadata = _repository_task_metadata(repo)
    metadata["execution_contract"]["required_changed_files"] = ["docs/demo-story.md"]
    task = cp.create_task(
        "Repository task forgot manifest and required file",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        (worktree / "README.md").write_text("wrong tracked file\n", encoding="utf-8")
        return WorkerExecution(0, "changed wrong repo file without evidence", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "blocked"
    evidence = cp.list_evidence(task.id)[0]
    manifest = evidence.metadata["verification"]
    repo_anchor = manifest["repo"]
    problems = " ".join(manifest.get("problems") or [])
    assert manifest["status"] == "complete"
    assert manifest["evidence_type"] == "repo_change"
    assert manifest["tests"][0]["returncode"] == 0
    assert repo_anchor["dirty"] is False
    assert repo_anchor["pushed"] is False
    assert repo_anchor["files_changed"] == ["README.md"]
    assert _git(repo, "ls-remote", "origin", repo_anchor["remote_ref"]) == ""
    assert "repo evidence missing required changed files: docs/demo-story.md" in problems
    assert "refusing to push" in problems
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_mac_worker_fails_when_required_changed_files_are_missing(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    metadata = _repository_task_metadata(repo)
    metadata["execution_contract"]["evidence_type"] = "documentation"
    metadata["execution_contract"]["required_changed_files"] = [
        "docs/demo-story.md",
    ]
    task = cp.create_task(
        "Repository docs task with required files",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        (worktree / "README.md").write_text("docs task touched tracked file\n", encoding="utf-8")
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

    assert result.status == "blocked"
    assert "docs/demo-story.md" in (result.error or "")
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    assert cp.list_reviews(task.id) == []


def test_mac_worker_rescues_dirty_worktree_when_contract_test_passes(tmp_path: Path):
    """An agent that edits TRACKED files and passes its contract test but forgets
    to `git commit` leaves the worktree dirty. The worker must commit the verified
    work, re-run the contract test, and push it (mac task_94aa4ed5) — not block it.
    Previously only UNTRACKED new files were rescued, so a modified-tracked edit
    was wrongly blocked despite passing tests (it even wrote a 'done' manifest)."""
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
        # Modified TRACKED file, left uncommitted: the agent did the work and the
        # contract test passes, but it slipped on `git commit`.
        (worktree / "README.md").write_text("verified but uncommitted\n", encoding="utf-8")
        return WorkerExecution(0, "did the work, forgot to commit", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review", result.error
    assert cp.get_task(task.id).state != TaskState.BLOCKED.value
    # The uncommitted edit is durably committed + pushed (not lost to a dirty block).
    evidence = cp.list_evidence(task.id)[0]
    manifest = evidence.metadata["verification"]
    repo_anchor = manifest["repo"]
    assert manifest["tests"][0]["returncode"] == 0
    assert repo_anchor["dirty"] is False
    assert repo_anchor["pushed"] is True
    assert repo_anchor["files_changed"] == ["README.md"]
    assert _git(repo, "ls-remote", "origin", repo_anchor["remote_ref"]).startswith(
        repo_anchor["head_sha"]
    )


def test_mac_worker_commits_and_pushes_untracked_files_during_repository_rescue(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "mac.worker.run_codegraph_audit",
        lambda _worktree, files: _codegraph_fixture(list(files)),
    )
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    task = cp.create_task(
        "Dirty result with untracked file",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        (worktree / "leaked_module.py").write_text("print('leak')\n", encoding="utf-8")
        return WorkerExecution(0, "left untracked file", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review", result.error
    evidence = cp.list_evidence(task.id)[0]
    manifest = evidence.metadata["verification"]
    repo_anchor = manifest["repo"]
    assert repo_anchor["dirty"] is False
    assert repo_anchor["pushed"] is True
    assert "leaked_module.py" in repo_anchor["files_changed"]
    assert _git(repo, "show", "%s:leaked_module.py" % repo_anchor["head_sha"]) == "print('leak')"
    assert _git(repo, "ls-remote", "origin", repo_anchor["remote_ref"]).startswith(
        repo_anchor["head_sha"]
    )


def test_mac_worker_blocks_dirty_worktree_when_contract_test_fails(tmp_path: Path):
    """The dirty-worktree rescue is test-gated: when the contract test does NOT
    pass after committing the agent's uncommitted edits, the worker commits but
    REFUSES to push and blocks the task. Unverified dirt is never silently
    accepted — the test-pass gate is what makes the rescue safe (mac task_94aa4ed5)."""
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    metadata = _repository_task_metadata(repo)
    # Contract test fails -> the rescue must commit but refuse to push.
    metadata["execution_contract"]["repository_contract"]["test"]["command"] = "false"
    task = cp.create_task(
        "Dirty result, failing contract test",
        required_capabilities=["python"],
        metadata=metadata,
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        worktree = Path(task_payload["metadata"]["runtime"]["repository_worktree"])
        _write_repo_worker_manifest(task_dir, worktree)
        (worktree / "README.md").write_text("unverified dirty edit\n", encoding="utf-8")
        return WorkerExecution(0, "left dirty tree, test will fail", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "blocked", result.error
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    evidence = cp.list_evidence(task.id)[0]
    manifest = evidence.metadata["verification"]
    repo_anchor = manifest["repo"]
    problems = " ".join(manifest.get("problems") or [])
    assert repo_anchor["pushed"] is False
    assert "refusing to push" in problems
    assert _git(repo, "ls-remote", "origin", repo_anchor["remote_ref"]) == ""


def test_mac_worker_adopts_agent_pushed_branch_when_worktree_matches(tmp_path: Path):
    """A sandboxed agent whose worktree gitlink is unusable commits + pushes the
    task branch from a throwaway in-sandbox clone; the HOST worktree keeps the
    same edits UNCOMMITTED. The worker must adopt the already-pushed tip (the
    work is durably on the remote) instead of false-blocking it as dirty."""
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    _seed, repo = _git_fixture(tmp_path)
    origin = tmp_path / "origin.git"
    task = cp.create_task(
        "Agent pushed from sandbox clone",
        required_capabilities=["python"],
        metadata=_repository_task_metadata(repo),
    )
    client = TestClient(create_app(control_plane=cp))

    def executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        runtime = task_payload["metadata"]["runtime"]
        worktree = Path(runtime["repository_worktree"])
        branch = runtime["repository_branch"]
        # Agent's in-sandbox fresh-clone push: the same edit, committed + pushed
        # to the task branch on origin from a SIDE clone, so the host worktree
        # never advances (mirrors the unusable-gitlink workaround in the sandbox).
        side = tmp_path / "agent-side-clone"
        subprocess.run(["git", "clone", str(origin), str(side)], check=True, capture_output=True)
        _git(side, "config", "user.email", "agent@example.invalid")
        _git(side, "config", "user.name", "agent")
        _git(side, "checkout", "-b", branch)
        (side / "README.md").write_text("agent edit\n", encoding="utf-8")
        _git(side, "add", "-A")
        _git(side, "commit", "-m", "agent work")
        _git(side, "push", "origin", "HEAD:refs/heads/%s" % branch)
        # Host worktree carries the SAME edit, uncommitted.
        (worktree / "README.md").write_text("agent edit\n", encoding="utf-8")
        _write_repo_worker_manifest(task_dir, worktree)
        return WorkerExecution(0, "agent pushed from sandbox clone", stdout="ok\n")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "submitted_for_review", result.error
    assert cp.get_task(task.id).state != TaskState.BLOCKED.value


def test_openshell_containerfile_changed_detects_sandbox_image_drift(tmp_path: Path):
    """The drift detector flags a pull that changed the sandbox Containerfile (so
    refresh-source rebuilds the image) and ignores unrelated changes / no-ops."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    cf = repo / "deploy" / "openshell"
    cf.mkdir(parents=True)
    (cf / "mac-hermes.Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add containerfile")
    after_cf = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "unrelated change")
    after_other = _git(repo, "rev-parse", "HEAD")

    assert _openshell_containerfile_changed(repo, base, after_cf) is True
    assert _openshell_containerfile_changed(repo, after_cf, after_other) is False
    assert _openshell_containerfile_changed(repo, base, base) is False


def test_lease_renewal_ticker_heartbeats_busy_agent_so_it_is_not_marked_stale(tmp_path: Path):
    """The execution-time lease-renewal ticker must also heartbeat the agent
    (status=busy) so a long task doesn't let the worker's last_seen_at go stale --
    which would otherwise get it dropped as a reviewer (reviewer_stale)."""
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        lambda *a, **k: WorkerExecution(0, "ok"),
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    posts: list = []
    stop = threading.Event()
    real_post = worker.client.post

    def recording_post(path, body=None):
        posts.append((path, body))
        if path.endswith("/heartbeat"):
            stop.set()  # end the ticker loop after its first heartbeat
        return real_post(path, body)

    worker.client.post = recording_post  # type: ignore[assignment]
    # lease id need not exist: renew fails (caught), then the heartbeat must fire.
    worker._renew_lease_until_stopped("lease_missing", "task_x", stop, 0.01)

    heartbeats = [body for path, body in posts if path.endswith("/heartbeat")]
    assert heartbeats, "lease-renewal ticker did not heartbeat the agent"
    assert heartbeats[0]["status"] == "busy"
    assert heartbeats[0]["health_status"] == "healthy"
    # Busy heartbeats now refresh the complete worker resource attestation so
    # report-executor eligibility cannot remain stale during a long task.
    assert isinstance(heartbeats[0]["resources"], dict)


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


def test_repository_contract_test_prefers_sandbox_verification_artifact(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))
    task_dir = tmp_path / "task"
    worktree = tmp_path / "repo"
    task_dir.mkdir()
    worktree.mkdir()
    (task_dir / "mac-sandbox-verification.json").write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "command": "make test",
                "returncode": 0,
                "status": "pass",
                "stdout": "verified inside sandbox\n",
                "stderr": "",
                "worktree": "/sandbox/task/repo",
                "environment_delta": {
                    "schema": "mac.sandbox_environment_delta.v1",
                    "commands": ["git", "pnpm", "java", "lein"],
                    "missing_after": [],
                },
            }
        ),
        encoding="utf-8",
    )
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
    )

    item = worker._run_repository_contract_test(worktree, "false", task_dir=task_dir)

    assert item["status"] == "pass"
    assert item["command"] == "make test"
    assert item["execution_environment"] == "openshell_sandbox"
    assert item["environment_delta"]["missing_after"] == []


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

    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
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
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


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
    cp.claim_task(task.id, first.id)
    client = TestClient(create_app(control_plane=cp))

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        current_lease_id = cp.get_task(task.id).lease_id
        assert current_lease_id is not None
        expired_at = "2000-01-01T00:00:00+00:00"
        cp.store.execute(
            "UPDATE leases SET expires_at = ?, updated_at = ? WHERE id = ?",
            (expired_at, expired_at, current_lease_id),
        )
        cp.store.execute(
            "UPDATE tasks SET leased_until = ?, updated_at = ? WHERE id = ?",
            (expired_at, expired_at, task.id),
        )
        cp.expire_leases()
        _, second_lease = cp.claim_task(task.id, second.id)
        cp.start_task(task.id, second.id, lease_id=second_lease.id)
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


def test_mac_worker_repo_update_preempts_idle_heartbeat_failure(tmp_path: Path):
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
        },
    )
    client = TestClient(create_app(control_plane=cp))

    def heartbeat_fails(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        if method == "POST" and path.endswith(f"/agents/{agent.id}/heartbeat"):
            raise MacApiError("agent cannot report idle while holding an active lease")
        return api_transport(client)(method, path, payload)

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=heartbeat_fails),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
        self_update_repo=work,
    )
    result = worker.run_once()

    assert result.status == "self_update_restart"
    assert _git(work, "rev-parse", "HEAD") == expected


def test_mac_worker_dispatch_hold_read_failure_stops_before_controls(
    monkeypatch, tmp_path: Path
):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))

    def hold_read_fails(
        method: str, path: str, payload: Optional[Dict[str, Any]]
    ) -> Any:
        if method == "GET" and path == f"/agents/{agent.id}":
            raise MacApiError("hub hold read unavailable")
        return api_transport(client)(method, path, payload)

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=hold_read_fails),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
    )
    controls: list[bool] = []
    monkeypatch.setattr(
        worker,
        "_process_agentbus_control",
        lambda **kwargs: controls.append(True),
    )

    with pytest.raises(MacApiError, match="hold read unavailable"):
        worker.run_once()

    assert controls == []


def test_mac_worker_heartbeat_recovery_processes_only_repo_update_controls(
    monkeypatch, tmp_path: Path
):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
        topic=DEBUG_TERMINAL_OPEN_TOPIC,
        payload={"schema": DEBUG_TERMINAL_OPEN_SCHEMA},
    )
    client = TestClient(create_app(control_plane=cp))

    def heartbeat_fails(
        method: str, path: str, payload: Optional[Dict[str, Any]]
    ) -> Any:
        if method == "POST" and path.endswith(f"/agents/{agent.id}/heartbeat"):
            raise MacApiError("heartbeat rejected")
        return api_transport(client)(method, path, payload)

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=heartbeat_fails),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
    )
    terminal_controls: list[str] = []
    monkeypatch.setattr(
        worker,
        "_handle_debug_terminal_open_stream",
        lambda stream: terminal_controls.append(str(stream.get("id"))) or {},
    )

    with pytest.raises(MacApiError, match="heartbeat rejected"):
        worker.run_once()

    assert terminal_controls == []


def test_mac_worker_opens_debug_terminal_and_streams_pty_output(tmp_path: Path):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    session_id = "term_worker_test"
    input_stream_id = session_id + ".in"
    output_stream_id = session_id + ".out"
    cp.open_agentbus_stream(
        sender_agent_id=sender.id,
        recipient_agent_id=agent.id,
        content_type=DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
        topic=DEBUG_TERMINAL_INPUT_TOPIC,
        headers={"schema": DEBUG_TERMINAL_INPUT_SCHEMA, "terminal_session_id": session_id},
        stream_id=input_stream_id,
    )
    cp.open_agentbus_stream(
        sender_agent_id=agent.id,
        recipient_agent_id=sender.id,
        content_type=DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
        topic=DEBUG_TERMINAL_OUTPUT_TOPIC,
        headers={"schema": DEBUG_TERMINAL_OUTPUT_SCHEMA, "terminal_session_id": session_id},
        stream_id=output_stream_id,
    )
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
        topic=DEBUG_TERMINAL_OPEN_TOPIC,
        payload=debug_terminal_open_payload(
            session_id=session_id,
            input_stream_id=input_stream_id,
            output_stream_id=output_stream_id,
            sender_agent_id=sender.id,
            shell="/bin/sh",
            cwd=str(tmp_path),
            rows=24,
            cols=100,
            ttl_seconds=60,
            request_id="req-terminal-worker",
        ),
    )
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
        poll_interval_seconds=0.0,
    )

    assert worker.run_once().status == "no_task"
    cp.append_agentbus_chunk(
        input_stream_id,
        sender_agent_id=sender.id,
        payload=debug_terminal_input_payload(
            session_id=session_id,
            data_b64=base64.b64encode(b"printf 'worker_terminal_ok\\n'\nexit\n").decode("ascii"),
        ),
    )

    output_text = ""
    for _ in range(30):
        worker.run_once()
        chunks = cp.read_agentbus_chunks(sender.id, output_stream_id)
        parts = []
        for chunk in chunks:
            payload = chunk.payload if isinstance(chunk.payload, dict) else {}
            if payload.get("data_b64"):
                parts.append(base64.b64decode(payload["data_b64"]).decode(errors="ignore"))
        output_text = "".join(parts)
        if "worker_terminal_ok" in output_text and cp.get_agentbus_stream(output_stream_id).status == "closed":
            break
        time.sleep(0.02)

    assert "worker_terminal_ok" in output_text
    assert cp.get_agentbus_stream(output_stream_id).status == "closed"


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


def test_mac_worker_repo_update_restarts_requested_services_after_result(
    monkeypatch, tmp_path: Path
):
    cp = ControlPlane.in_memory()
    sender_machine = cp.register_machine("sender-host")
    sender = cp.register_agent(sender_machine.id, "sender")
    agent = register_worker_fixture(cp)
    seed, work = _git_fixture(tmp_path)
    expected = _commit_fixture_update(seed, "two\n")
    restarted: list[str] = []

    def fake_restart(service: str):
        restarted.append(service)
        return {"service": service, "status": "restarted", "returncode": 0}

    monkeypatch.setattr("mac.worker._restart_systemd_service", fake_restart)
    cp.publish_agentbus_content(
        sender.id,
        recipient_agent_id=agent.id,
        content_type=REPO_UPDATE_CONTENT_TYPE,
        topic=REPO_UPDATE_TOPIC,
        payload={
            "schema": REPO_UPDATE_SCHEMA,
            "remote": "origin",
            "branch": "main",
            "restart": False,
            "restart_services": ["mac.service"],
            "request_id": "req-service",
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

    assert result.status == "no_task"
    assert _git(work, "rev-parse", "HEAD") == expected
    assert restarted == ["mac.service"]

    payloads = []
    for stream in cp.list_agentbus_streams(agent_id=sender.id, status="closed"):
        if stream.topic == REPO_UPDATE_RESULT_TOPIC:
            payloads.extend(chunk.payload for chunk in cp.read_agentbus_chunks(sender.id, stream.id))
    by_status = {payload["status"]: payload for payload in payloads}
    assert by_status["updated"]["service_restart_requested"] is True
    assert by_status["updated"]["restart_services"] == ["mac.service"]
    assert by_status["service_restarted"]["service_restarts"] == [
        {"service": "mac.service", "status": "restarted", "returncode": 0}
    ]


def test_repo_update_service_restart_skips_current_worker_service(monkeypatch):
    from mac.worker import _restart_systemd_service

    monkeypatch.setenv("MAC_AGENT_SERVICE_NAME", "mac-agent.service")

    result = _restart_systemd_service("mac-agent.service")

    assert result == {
        "service": "mac-agent.service",
        "status": "skipped",
        "reason": "worker service restart is handled by the repo-update restart flag",
    }


def test_repo_update_result_publish_can_extend_retries_for_service_result(
    monkeypatch, tmp_path: Path
):
    attempts: list[Dict[str, Any]] = []
    sleeps: list[float] = []

    class _Client:
        def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            attempts.append({"path": path, "payload": payload})
            if len(attempts) < 3:
                raise MacApiError("hub restarting")
            return {"ok": True}

    worker = MacWorker(
        _Client(),  # type: ignore[arg-type]
        "agent_rocky",
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
    )
    monkeypatch.setattr("mac.worker.time.sleep", lambda seconds: sleeps.append(seconds))

    worker._publish_repo_update_result(
        {"id": "stream_1", "sender_agent_id": "agent_hub"},
        {"status": "service_restarted"},
        attempts=3,
        delay_seconds=0.25,
    )

    assert [item["path"] for item in attempts] == ["/agentbus", "/agentbus", "/agentbus"]
    assert sleeps == [0.25, 0.25]


def test_repo_update_result_publish_default_retry_window_stays_short(
    monkeypatch, tmp_path: Path
):
    attempts: list[Dict[str, Any]] = []
    sleeps: list[float] = []
    observed: list[Dict[str, Any]] = []

    class _Client:
        def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            attempts.append({"path": path, "payload": payload})
            raise MacApiError("hub unavailable")

    worker = MacWorker(
        _Client(),  # type: ignore[arg-type]
        "agent_rocky",
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
    )
    monkeypatch.setattr("mac.worker.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(worker, "_observe_log", lambda *args, **kwargs: observed.append(kwargs))

    worker._publish_repo_update_result(
        {"id": "stream_1", "sender_agent_id": "agent_hub"},
        {"status": "updated"},
    )

    assert len(attempts) == 5
    assert sleeps == [0.5, 0.5, 0.5, 0.5]
    assert observed[-1]["detail"]["error"] == "hub unavailable"


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


def test_worker_generation_barrier_heartbeats_draining_until_authorized(
    monkeypatch, tmp_path: Path
):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))
    barrier = tmp_path / "deploy-start-barrier"
    generation = "sha256:revision:agent_worker:attempt-1"
    barrier.write_text(generation + "\n", encoding="utf-8")
    monkeypatch.setenv("MAC_WORKER_DEPLOY_GENERATION", generation)
    monkeypatch.setenv("MAC_WORKER_DEPLOY_BARRIER_FILE", str(barrier))

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspace",
        lambda _t, _d: WorkerExecution(0, "unused"),
    )
    monkeypatch.setattr(worker, "_maybe_start_coding_route_probe", lambda: None)
    monkeypatch.setattr(worker, "_maybe_command_inventory_resources", lambda: {})

    worker._heartbeat()
    draining = cp.get_agent(agent.id)
    assert draining.status == "draining"
    assert draining.resources["deployment_generation"] == generation

    barrier.unlink()
    worker._heartbeat()
    authorized = cp.get_agent(agent.id)
    assert authorized.status == "idle"
    assert authorized.resources["deployment_generation"] == generation


def test_worker_generation_barrier_registers_draining_atomically(
    monkeypatch, tmp_path: Path
):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("barrier-worker-host", machine_id="machine_barrier")
    agent = cp.register_agent(
        machine.id,
        "barrier-worker",
        agent_id="agent_barrier_worker",
        capabilities=["python"],
    )
    assert agent.status == "idle"
    barrier = tmp_path / "deploy-start-barrier"
    generation = "revision:barrier-worker:attempt-3"
    barrier.write_text(generation + "\n", encoding="utf-8")
    monkeypatch.setenv("MAC_WORKER_DEPLOY_GENERATION", generation)
    monkeypatch.setenv("MAC_WORKER_DEPLOY_BARRIER_FILE", str(barrier))
    client = TestClient(create_app(control_plane=cp))

    registered = register_worker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        hostname="barrier-worker-host",
        agent_name="barrier-worker",
        capabilities=["python"],
        machine_id=machine.id,
        agent_id=agent.id,
    )

    assert registered["status"] == "draining"
    assert registered["health_status"] == "degraded"
    assert registered["resources"]["deployment_generation"] == generation
    persisted = cp.get_agent(agent.id)
    assert persisted.status == "draining"
    assert persisted.health_status == "degraded"


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
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_register_worker_reports_command_inventory_without_command_capability(
    tmp_path: Path,
    monkeypatch,
):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "gh", "python3"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_MAX", "not-an-int")

    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
    )

    refreshed = cp.get_agent(registered["id"])
    commands = refreshed.resources["commands"]
    assert commands["schema"] == "mac.command_inventory.v1"
    assert {"git", "gh", "python3"} <= set(commands["available"])
    assert refreshed.capabilities == ["python"]
    assert "git" not in refreshed.capabilities

    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_INTERVAL_SECONDS", "not-a-float")
    worker = MacWorker(api, registered["id"], tmp_path, lambda _t, _d: WorkerExecution(0, "ok"))
    heartbeat_resources = worker._maybe_command_inventory_resources()
    assert heartbeat_resources is not None
    assert {"git", "gh", "python3"} <= set(heartbeat_resources["commands"]["available"])
    assert heartbeat_resources["coding_clis"]["schema"] == "mac.coding_clis.v2"


def test_worker_publishes_matching_sandbox_route_verification(
    tmp_path: Path,
    monkeypatch,
):
    from mac import coding_agent, task_executor

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", resources={})
    choice = coding_agent.CodingAgentChoice(
        agent="codex",
        available=True,
        binary="/usr/local/bin/codex",
        auth_source="MAC_CODEX_TOKEN",
        provider="mac-router",
        protocol="responses",
        auth_kind="bearer_env",
        endpoint="https://hub.example/v1",
        model="*",
    )
    report = {
        "schema": "mac.coding_agent.verification.v1",
        "agent": "codex",
        "provider": "mac-router",
        "protocol": "responses",
        "auth_kind": "bearer_env",
        "auth_source": "MAC_CODEX_TOKEN",
        "endpoint": "https://hub.example/v1",
        "model": "*",
        "route_fingerprint": choice.route_fingerprint(),
        "verified": True,
        "checked_at": "2026-07-08T00:00:00+00:00",
        "returncode": 0,
        "failure_class": "",
    }
    def resolve_for_test(*, accept=None, which=None, verify_all=False, exclude=None):
        assert accept is not None
        assert which is task_executor.coding_agent_sandbox_which
        assert verify_all is True
        if accept(choice):
            return choice
        return coding_agent.CodingAgentChoice(agent="", available=False)

    monkeypatch.setattr(coding_agent, "resolve_coding_agent", resolve_for_test)
    monkeypatch.setattr(
        task_executor,
        "coding_agent_sandbox_verification",
        lambda selected: report,
    )
    monkeypatch.setattr(
        coding_agent,
        "_DETECTORS",
        {
            **coding_agent._DETECTORS,
            "codex": lambda *_args: (
                True,
                choice.binary,
                choice.auth_source,
                "codex: configured for test",
            ),
        },
    )
    monkeypatch.setenv("MAC_CODEX_TOKEN", "secret-not-reported")
    monkeypatch.setenv("MAC_CODEX_BASE_URL", choice.endpoint)
    monkeypatch.setenv("MAC_CODEX_PROVIDER", choice.provider)

    worker = MacWorker(api, agent.id, tmp_path, lambda _t, _d: WorkerExecution(0, "ok"))
    worker._probe_coding_route()
    resources = worker._maybe_command_inventory_resources()

    codex = resources["coding_clis"]["clis"]["codex"]
    assert codex["configured"] is True
    assert codex["verified"] is True
    assert codex["provider"] == "mac-router"
    assert codex["protocol"] == "responses"
    assert "secret-not-reported" not in json.dumps(resources)


def test_worker_verifies_darwin_host_route_without_openshell(
    tmp_path: Path,
    monkeypatch,
):
    from mac import coding_agent

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    machine = cp.register_machine("darwin-host")
    agent = cp.register_agent(
        machine.id,
        "darwin-worker",
        resources={"openshell_required": False},
    )
    choice = coding_agent.CodingAgentChoice(
        agent="opencode",
        available=True,
        binary="/Users/test/.mac/bin/opencode",
        auth_source="~/.local/share/opencode/auth.json",
        provider="opencode",
        protocol="opencode-run",
        auth_kind="api_key_file",
    )

    def resolve_for_test(*, accept=None, which=None, verify_all=False, exclude=None):
        assert accept is not None
        assert which is None
        assert verify_all is True
        return (
            choice
            if accept(choice)
            else coding_agent.CodingAgentChoice(agent="", available=False)
        )

    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "0")
    monkeypatch.setattr(coding_agent, "resolve_coding_agent", resolve_for_test)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, coding_agent.PREFLIGHT_SENTINEL, ""
        ),
    )
    monkeypatch.setattr(
        coding_agent,
        "_DETECTORS",
        {
            **coding_agent._DETECTORS,
            "opencode": lambda *_args: (
                True,
                choice.binary,
                choice.auth_source,
                "opencode: configured for test",
            ),
        },
    )

    worker = MacWorker(
        api,
        agent.id,
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "ok"),
    )
    worker._probe_coding_route()
    resources = worker._maybe_command_inventory_resources()

    opencode = resources["coding_clis"]["clis"]["opencode"]
    assert opencode["verified"] is True
    assert opencode["verification"]["execution_binary"] == choice.binary
    assert worker._coding_route_report["agent"] == "opencode"


def test_worker_falls_through_failed_claude_and_publishes_verified_codex(
    tmp_path: Path,
    monkeypatch,
):
    from mac import coding_agent, task_executor

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", resources={})
    choices = [
        coding_agent.CodingAgentChoice(
            agent="claude",
            available=True,
            binary="/usr/local/bin/claude",
            auth_source="ANTHROPIC_API_KEY",
            provider="anthropic",
            protocol="anthropic-messages",
            auth_kind="api_key",
            endpoint="https://api.anthropic.com",
        ),
        coding_agent.CodingAgentChoice(
            agent="codex",
            available=True,
            binary="/usr/local/bin/codex",
            auth_source="OPENAI_API_KEY",
            provider="openai",
            protocol="responses",
            auth_kind="bearer_env",
            endpoint="https://api.openai.com/v1",
        ),
    ]
    attempted = []

    def resolve_for_test(*, accept=None, which=None, verify_all=False, exclude=None):
        assert accept is not None
        assert which is task_executor.coding_agent_sandbox_which
        assert verify_all is True
        for candidate in choices:
            if accept(candidate):
                return candidate
        return coding_agent.CodingAgentChoice(agent="", available=False)

    def verify_for_test(candidate):
        attempted.append(candidate.agent)
        return {
            **candidate.observable(),
            "schema": "mac.coding_agent.verification.v1",
            "agent": candidate.agent,
            "route_fingerprint": candidate.route_fingerprint(),
            "verified": candidate.agent == "codex",
            "checked_at": "2026-07-16T00:00:00+00:00",
            "failure_class": "" if candidate.agent == "codex" else "probe_failed",
        }

    monkeypatch.setattr(coding_agent, "resolve_coding_agent", resolve_for_test)
    monkeypatch.setattr(
        task_executor,
        "coding_agent_sandbox_verification",
        verify_for_test,
    )
    monkeypatch.setattr(
        coding_agent,
        "_DETECTORS",
        {
            **coding_agent._DETECTORS,
            "claude": lambda *_args: (
                True,
                choices[0].binary,
                choices[0].auth_source,
                "claude: configured for test",
            ),
            "codex": lambda *_args: (
                True,
                choices[1].binary,
                choices[1].auth_source,
                "codex: configured for test",
            ),
        },
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-not-reported")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-not-reported")

    worker = MacWorker(api, agent.id, tmp_path, lambda _t, _d: WorkerExecution(0, "ok"))
    worker._probe_coding_route()
    resources = worker._maybe_command_inventory_resources()

    clis = resources["coding_clis"]["clis"]
    assert attempted == ["claude", "codex"]
    assert clis["claude"]["verification_status"] == "failed"
    assert clis["claude"]["verification"]["failure_class"] == "probe_failed"
    assert clis["codex"]["verification_status"] == "verified"
    assert clis["codex"]["verified"] is True
    assert worker._coding_route_report["agent"] == "codex"
    assert worker._coding_route_report["verified"] is True
    assert "secret-not-reported" not in json.dumps(resources)


def test_worker_probes_and_advertises_cursor_from_task_image_not_host_path(
    tmp_path: Path,
    monkeypatch,
):
    from mac import coding_agent, task_executor

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", resources={})
    attempted = []

    monkeypatch.setenv("MAC_CODING_AGENT", "cursor")
    monkeypatch.setenv("CURSOR_API_KEY", "secret-not-reported")
    monkeypatch.setattr(
        coding_agent,
        "_service_augmented_which",
        lambda _env, _home: lambda _name: None,
    )

    def verify_for_test(candidate):
        attempted.append(candidate.agent)
        return {
            **candidate.observable(),
            "schema": "mac.coding_agent.verification.v1",
            "agent": candidate.agent,
            "binary": candidate.binary,
            "execution_binary": candidate.binary,
            "binary_status": "present",
            "route_fingerprint": candidate.route_fingerprint(),
            "verified": True,
            "checked_at": "2026-07-28T00:00:00+00:00",
            "failure_class": "",
        }

    monkeypatch.setattr(
        task_executor,
        "coding_agent_sandbox_verification",
        verify_for_test,
    )

    worker = MacWorker(api, agent.id, tmp_path, lambda _t, _d: WorkerExecution(0, "ok"))
    worker._probe_coding_route()
    resources = worker._maybe_command_inventory_resources()

    cursor = resources["coding_clis"]["clis"]["cursor"]
    assert attempted == ["cursor"]
    assert cursor["on_path"] is True
    assert cursor["host_on_path"] is False
    assert cursor["configured"] is True
    assert cursor["verified"] is True
    assert cursor["binary_status"] == "present"
    assert "secret-not-reported" not in json.dumps(resources)


def test_command_inventory_explicitly_probes_codegraph_when_scan_truncated(
    tmp_path: Path,
    monkeypatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codegraph = bin_dir / "codegraph"
    codegraph.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codegraph.chmod(0o755)
    # Force the directory scan to stop almost immediately. The explicit command
    # probe list must still discover baseline tools that matter for repo work.
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_MAX", "1")

    commands = _detect_command_inventory()

    assert "codegraph" in commands["available"]
    assert commands["paths"]["codegraph"] == str(codegraph)


def test_command_inventory_explicitly_probes_cargo_when_scan_truncated(
    tmp_path: Path,
    monkeypatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cargo.chmod(0o755)
    rustup = bin_dir / "rustup"
    rustup.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rustup.chmod(0o755)
    # Force the directory scan to stop almost immediately. The explicit command
    # probe list must still discover cargo and rustup for Rust toolchain detection.
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_MAX", "1")

    commands = _detect_command_inventory()

    assert "cargo" in commands["available"]
    assert commands["paths"]["cargo"] == str(cargo)
    assert "rustup" in commands["available"]
    assert commands["paths"]["rustup"] == str(rustup)


def test_command_inventory_file_probe_finds_rust_tools_in_cargo_bin(
    tmp_path: Path,
    monkeypatch,
):
    # Simulate a launchd PATH that excludes ~/.cargo/bin; the secondary
    # file-based probe must still discover Rust tools from the well-known
    # ~/.cargo/bin location via Path.is_file() + os.access() checks.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    cargo_bin = tmp_path / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    for tool in ("cargo", "rustc", "rustup"):
        t = cargo_bin / tool
        t.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        t.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_dir))
    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_MAX", "1")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))

    commands = _detect_command_inventory()

    for tool in ("cargo", "rustc", "rustup"):
        assert tool in commands["available"], f"{tool} should be found via file probe"
        assert commands["paths"][tool] == str(cargo_bin / tool)


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


def test_register_worker_auto_registers_deployment_fleet(monkeypatch, tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    monkeypatch.setenv("MAC_FLEET_NAME", "rocky")
    monkeypatch.setenv("MAC_FLEET_TENANT_ID", "tenant_rocky")
    monkeypatch.setenv("MAC_SHARED_SERVICES_MANAGER_AGENT", "rocky")
    monkeypatch.setenv("MAC_HERMES_PERSONA_ID", "persona_rocky")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "missing-fleets.yaml"))

    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
        hermes_instance_id="hermes_rocky",
    )

    fleets = cp.list_fleets()
    assert len(fleets) == 1
    fleet = fleets[0]
    assert fleet.id == "fleet_rocky"
    assert fleet.name == "rocky"
    assert fleet.tenant_id == "tenant_rocky"
    assert fleet.status == "active"
    assert fleet.agent_ids == []
    assert fleet.observed_agent_ids == [registered["id"]]
    assert fleet.unmanaged_agent_ids == [registered["id"]]
    assert fleet.metadata["source"] == "mac-agent"
    assert fleet.metadata["hub_agent"] == "rocky"


def test_register_worker_adds_additional_agents_to_deployment_fleet(monkeypatch, tmp_path: Path):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    monkeypatch.setenv("MAC_FLEET_NAME", "rocky")
    monkeypatch.setenv("MAC_FLEET_TENANT_ID", "tenant_rocky")
    monkeypatch.setenv("MAC_SHARED_SERVICES_MANAGER_AGENT", "rocky")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "missing-fleets.yaml"))

    rocky = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
        hermes_instance_id="hermes_rocky",
    )
    natasha = register_worker(
        api,
        hostname="natasha.local",
        agent_name="natasha",
        capabilities=["review"],
        hermes_instance_id="hermes_natasha",
    )

    fleet = cp.get_fleet("rocky")
    assert fleet.agent_ids == []
    assert fleet.observed_agent_ids == sorted([rocky["id"], natasha["id"]])
    assert fleet.unmanaged_agent_ids == sorted([rocky["id"], natasha["id"]])
    assert fleet.metadata["hub_agent"] == "rocky"


def test_register_worker_configures_membership_when_deployed_registry_lists_agent(
    monkeypatch, tmp_path: Path
):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    api = MacApiClient("http://mac.test", transport=api_transport(client))
    registry = tmp_path / "fleets.yaml"
    registry.write_text(
        """
version: 1
fleets:
  rocky:
    fleet_name: rocky
    hub_agent: rocky
    agents:
      - name: rocky
        enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(registry))
    monkeypatch.setenv("MAC_FLEET_NAME", "rocky")
    monkeypatch.setenv("MAC_FLEET_TENANT_ID", "tenant_rocky")
    monkeypatch.setenv("MAC_SHARED_SERVICES_MANAGER_AGENT", "rocky")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    registered = register_worker(
        api,
        hostname="rocky.local",
        agent_name="rocky",
        capabilities=["python"],
        hermes_instance_id="hermes_rocky",
    )

    fleet = cp.get_fleet("rocky")
    assert fleet.agent_ids == [registered["id"]]
    assert fleet.observed_agent_ids == [registered["id"]]
    assert fleet.unmanaged_agent_ids == []
    assert fleet.metadata["topology_source"] == str(registry)


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
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_self_install_name_parsers():
    assert MacWorker._pip_base_name("diffusers==0.31") == "diffusers"
    assert MacWorker._pip_base_name("torch>=2 ; python_version>'3'") == "torch"
    assert MacWorker._pip_base_name("Pillow_SIMD") == "pillow-simd"
    assert MacWorker._npm_base_name("@scope/pkg@1.2.3") == "@scope/pkg"
    assert MacWorker._npm_base_name("left-pad@1.0") == "left-pad"


def test_mac_worker_self_install_pip_audits_and_reports_footprint(tmp_path: Path, monkeypatch):
    """Part C1+C2 round-trip: ensure_pip rejects flag-smuggling, runs pip in the
    agent venv, audits the command, reports the footprint to the hub, and is
    idempotent (skips an already-satisfied spec)."""
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        lambda *a: None,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "machome"))
    monkeypatch.setattr(worker, "_pip_installed", lambda py: {})

    calls: Dict[str, Any] = {}

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _Result()

    monkeypatch.setattr("mac.worker.subprocess.run", fake_run)

    result = worker.ensure_pip(["diffusers==0.31", "-rmalicious.txt"], reason="need diffusers")
    assert result["ok"] is True
    assert "diffusers==0.31" in calls["argv"]
    assert "-rmalicious.txt" not in calls["argv"]  # flag-smuggling rejected

    # footprint persisted on the hub (exercises the C2 endpoint + column)
    footprint = cp.get_agent(agent.id).installed_packages
    assert any(e["name"] == "diffusers" for e in footprint["pip"])

    # idempotency: report it as already installed at the pinned version -> no
    # subprocess invocation (version-aware probe sees it satisfied).
    monkeypatch.setattr(worker, "_pip_installed", lambda py: {"diffusers": "0.31"})
    calls.clear()
    again = worker.ensure_pip(["diffusers==0.31"], reason="again")
    assert again.get("skipped") == "already satisfied"
    assert "argv" not in calls


def test_register_worker_advertises_multi_modality_routes(monkeypatch):
    """Part B1b: a GPU agent advertises image + audio + video media routes from
    its configured model lists, each at its modality's port."""
    cp = ControlPlane.in_memory()
    api = MacApiClient("http://mac.test", transport=api_transport(TestClient(create_app(control_plane=cp))))
    monkeypatch.setattr(
        "mac.hardware.detect_hardware",
        lambda: {"accelerator": "cuda", "gpu": {"name": "GB10", "vram_mb": 48000},
                 "memory_mb": 120000, "os": "linux", "arch": "x86_64", "cpu_count": 20},
    )
    monkeypatch.delenv("MAC_AGENT_MEDIA_ROUTES", raising=False)
    monkeypatch.setenv("MAC_AGENT_GEN_MODEL", "sdxl-turbo")
    monkeypatch.setenv("MAC_AGENT_GEN_AUDIO_MODELS", "bark, musicgen-small")
    monkeypatch.setenv("MAC_AGENT_GEN_VIDEO_MODELS", "animatediff")
    monkeypatch.setenv("MAC_AGENT_GEN_HOST", "natasha")
    registered = register_worker(api, hostname="natasha", agent_name="natasha", capabilities=["python"])
    routes = cp.get_agent(registered["id"]).resources["media_routes"]
    by_op = {r["op"]: r for r in routes}
    assert {"image.generate", "audio.tts", "audio.music", "video.generate"} <= set(by_op)
    assert ":8189/" in by_op["image.generate"]["base_url"]
    assert ":8190/" in by_op["audio.tts"]["base_url"]
    assert ":8190/" in by_op["audio.music"]["base_url"]
    assert ":8191/" in by_op["video.generate"]["base_url"]


def test_worker_advertises_only_held_service_ops(tmp_path: Path, monkeypatch):
    """media-01 advertise-on-hold: the worker claims service-roles up to capacity
    and re-stamps media_routes to ONLY the ops it currently holds."""
    cp = ControlPlane.in_memory()
    cp.seed_service_roles(["image.generate", "audio.tts", "video.generate"])
    api = MacApiClient("http://mac.test", transport=api_transport(TestClient(create_app(control_plane=cp))))
    monkeypatch.setattr(
        "mac.hardware.detect_hardware",
        lambda: {"accelerator": "cuda", "gpu": {"vram_mb": 48000}, "memory_mb": 120000},
    )
    monkeypatch.delenv("MAC_AGENT_MEDIA_ROUTES", raising=False)
    monkeypatch.setenv("MAC_AGENT_GEN_MODEL", "sdxl-turbo")
    monkeypatch.setenv("MAC_AGENT_GEN_AUDIO_MODELS", "bark")
    monkeypatch.setenv("MAC_AGENT_GEN_HOST", "natasha")
    registered = register_worker(
        api, hostname="natasha", agent_name="natasha",
        capabilities=["gpu", "cuda"], resources={"capacity": 1},
    )
    worker = MacWorker(api, registered["id"], tmp_path, lambda *a: None,
                       attestation_key=registered.get("attestation_key"))
    worker._sync_service_claims()
    held = set(cp.service_roles.held_ops_for_agent(registered["id"]))
    routes_ops = {r["op"] for r in cp.get_agent(registered["id"]).resources.get("media_routes", [])}
    assert len(held) == 1  # capacity 1 -> holds exactly one op
    assert routes_ops == held  # advertises only the held op
    assert routes_ops <= {"image.generate", "audio.tts"}


def test_worker_advertises_all_willing_when_no_service_roles(tmp_path: Path, monkeypatch):
    """Back-compat: a fleet that seeds NO service_roles keeps advertise-all (the
    sync's 'managed' set is empty, so every willing op is advertised)."""
    cp = ControlPlane.in_memory()  # no seed_service_roles
    api = MacApiClient("http://mac.test", transport=api_transport(TestClient(create_app(control_plane=cp))))
    monkeypatch.setattr(
        "mac.hardware.detect_hardware",
        lambda: {"accelerator": "cuda", "gpu": {"vram_mb": 48000}, "memory_mb": 120000},
    )
    monkeypatch.delenv("MAC_AGENT_MEDIA_ROUTES", raising=False)
    monkeypatch.setenv("MAC_AGENT_GEN_MODEL", "sdxl-turbo")
    monkeypatch.setenv("MAC_AGENT_GEN_AUDIO_MODELS", "bark")
    monkeypatch.setenv("MAC_AGENT_GEN_HOST", "natasha")
    registered = register_worker(api, hostname="natasha", agent_name="natasha", capabilities=["gpu", "cuda"])
    worker = MacWorker(api, registered["id"], tmp_path, lambda *a: None,
                       attestation_key=registered.get("attestation_key"))
    worker._sync_service_claims()
    routes_ops = {r["op"] for r in cp.get_agent(registered["id"]).resources.get("media_routes", [])}
    assert routes_ops == {"image.generate", "audio.tts"}  # all willing (unmanaged)


def test_command_inventory_finds_cargo_on_path(
    tmp_path: Path,
    monkeypatch,
):
    """Regression: cargo is still discovered via PATH when it is present there."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cargo.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    commands = _detect_command_inventory()

    assert "cargo" in commands["available"], "cargo must appear in available when it is on PATH"
    assert commands["paths"]["cargo"] == str(cargo)


def test_dispatch_treats_command_inventory_as_advisory():
    """A stale command inventory cannot strand otherwise runnable work.

    The executor/bootstrap path owns tool installation and concrete execution
    failure.  Allocator v2 uses durable capabilities/resources as its hard
    placement contract instead of a worker-local PATH snapshot.
    """
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    # Register an agent with python but WITHOUT cargo in its command inventory.
    cp.register_agent(
        machine.id,
        "no-cargo-worker",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
                "paths": {},
                "truncated": False,
            }
        },
    )
    # Create a repository task that requires cargo in its toolchain.
    cargo_task_metadata = {
        "origin": {
            "type": "direct_task",
            "repository_contract": {
                "schema": "mac.repository_contract.v1",
                "project": "rust-project",
                "platforms": ["darwin", "linux"],
                "toolchain": {"required_commands": ["cargo"]},
                "bootstrap": {"command": "cargo build"},
                "test": {"command": "cargo test"},
                "evidence": {"required": ["tests"]},
            },
        }
    }
    cp.create_task(
        "Rust build task",
        required_capabilities=["python"],
        metadata=cargo_task_metadata,
    )

    assignment = cp.dispatch_once()

    assert assignment is not None
    assert assignment["task"]["id"]


def test_dispatch_gate_allows_agent_with_cargo_for_cargo_task():
    """An agent whose command inventory includes cargo can be dispatched a task
    that requires cargo in toolchain_requirements.required_commands."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    # Register an agent WITH cargo in its command inventory.
    cp.register_agent(
        machine.id,
        "cargo-worker",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh", "cargo"],
                "paths": {"cargo": "/home/user/.cargo/bin/cargo"},
                "truncated": False,
            }
        },
    )
    cargo_task_metadata = {
        "origin": {
            "type": "direct_task",
            "repository_contract": {
                "schema": "mac.repository_contract.v1",
                "project": "rust-project",
                "platforms": ["darwin", "linux"],
                "toolchain": {"required_commands": ["cargo"]},
                "bootstrap": {"command": "cargo build"},
                "test": {"command": "cargo test"},
                "evidence": {"required": ["tests"]},
            },
        }
    }
    task = cp.create_task(
        "Rust build task",
        required_capabilities=["python"],
        metadata=cargo_task_metadata,
    )

    assignment = cp.dispatch_once()

    assert assignment is not None, (
        "An agent with cargo in its command inventory must be eligible for a "
        "task that requires cargo"
    )
    assert assignment["task"]["id"] == task.id


def test_worker_exception_records_diagnostics_and_output_tail(tmp_path: Path):
    """A non-timeout executor crash must leave a diagnosable failure trail.

    Regression: the generic ``except Exception`` handler in
    ``execute_assignment`` used to post a blocked transition carrying only
    ``error=str(exc)``. ``_diagnostic_output_tail`` scans stdout/stderr/output/
    *_tail keys, none of which were present, so every ``worker_exception``
    retry recorded an empty ``output_tail`` with "transition supplied no ...
    field" and blocked with no actionable diagnostics. The handler now captures
    the traceback as durable evidence and surfaces it (plus the exception type
    and evidence id) on the transition detail.
    """
    from mac.services import _diagnostic_output_tail

    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task(
        "executor crashes with a python exception",
        required_capabilities=["python"],
    )
    client = TestClient(create_app(control_plane=cp))

    def _crashing_executor(_task: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        raise RuntimeError("boom: unhandled executor failure")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path / "workspaces",
        _crashing_executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    with pytest.raises(RuntimeError, match="boom: unhandled executor failure"):
        worker.run_once()

    history = cp.task_history(task.id)
    blocked = [
        item
        for item in history
        if item.event_type == "task.transitioned"
        and item.to_state == TaskState.BLOCKED.value
    ][-1]
    assert blocked.detail["reason"] == "worker_exception"
    assert blocked.detail["exception_type"] == "RuntimeError"
    assert "boom: unhandled executor failure" in blocked.detail["output_tail"]
    assert "Traceback" in blocked.detail["output_tail"]

    # The hub's diagnosis must now find a real output tail instead of the
    # "no ... field" placeholder.
    output_tail, unavailable = _diagnostic_output_tail(blocked.detail)
    assert unavailable == ""
    assert "boom: unhandled executor failure" in output_tail

    # A durable evidence artifact was recorded and referenced on the transition.
    evidence = cp.list_evidence(task.id)
    assert evidence, "worker exception must record durable evidence"
    assert blocked.detail.get("evidence_id") == evidence[-1].id
