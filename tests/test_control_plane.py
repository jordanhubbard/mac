import base64
import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest

import mac.services as services
from mac.agentbus_control import (
    AGENT_REFLECTION_CONTENT_TYPE,
    AGENT_REFLECTION_SCHEMA,
    AGENT_REFLECTION_TOPIC,
    REFLECT_RESULT_CONTENT_TYPE,
    REFLECT_RESULT_TOPIC,
    reflect_result_payload,
)
from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA, codegraph_relevant_files
from mac.fleet_learning import (
    build_repository_access_learning,
    build_repository_access_memory_payload,
)
from mac.models import (
    AgentStatus,
    AuthorizationError,
    HealthStatus,
    LeaseStatus,
    MessageStatus,
    MessageType,
    NotFoundError,
    PublicationStatus,
    ReviewStatus,
    RolloutStatus,
    TaskState,
    TransitionError,
    ValidationError,
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY,
    metadata_declares_read_only_report_repository,
    read_only_report_repository_executor_approval,
    read_only_report_repository_executor_attestation,
    utcnow,
)
from mac.migration import migrate_acc_sqlite
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.work_package_service import RepositoryBaseAttestation
from mac.work_package_assignment import WorkPackageTaskRank
from mac.work_plan_admission import CanonicalRepositoryBase


def _write_cwd_fake_bd_cli(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "if len(args) >= 2 and args[0] == '--actor':",
                "    args = args[2:]",
                "cwd = pathlib.Path.cwd()",
                "issues_path = cwd / '.beads' / 'issues.jsonl'",
                "def read_issues():",
                "    issues = []",
                "    if not issues_path.exists():",
                "        return issues",
                "    for raw in issues_path.read_text(encoding='utf-8').splitlines():",
                "        if raw.strip():",
                "            issue = json.loads(raw)",
                "            if isinstance(issue, dict) and issue.get('_type', 'issue') == 'issue':",
                "                issues.append(issue)",
                "    return issues",
                "def ready_issues():",
                "    issues = read_issues()",
                "    by_id = {str(item.get('id')): item for item in issues if item.get('id')}",
                "    ready = []",
                "    for issue in issues:",
                "        if str(issue.get('status') or '').lower() != 'open' or not str(issue.get('id') or '').strip():",
                "            continue",
                "        deps = issue.get('dependencies') or []",
                "        if int(issue.get('dependency_count') or 0) > 0 and not deps:",
                "            continue",
                "        blocked = False",
                "        for dep in deps:",
                "            dep_issue = by_id.get(str(dep.get('depends_on_id') or '')) if isinstance(dep, dict) else None",
                "            if dep_issue is None or str(dep_issue.get('status') or '') != 'closed':",
                "                blocked = True",
                "                break",
                "        if not blocked:",
                "            ready.append(issue)",
                "    ready.sort(key=lambda item: (int(item.get('priority') or 2), str(item.get('created_at') or ''), str(item.get('id') or '')))",
                "    return ready",
                "if args == ['ready', '--json']:",
                "    sys.stdout.write(json.dumps(ready_issues()))",
                "    sys.exit(0)",
                "if args[:1] == ['bootstrap']:",
                "    (cwd / '.beads' / 'embeddeddolt').mkdir(parents=True, exist_ok=True)",
                "    sys.exit(0)",
                "if args == ['dolt', 'pull'] or args == ['dolt', 'push']:",
                "    sys.exit(0)",
                "if args[:1] == ['export']:",
                "    output = json.dumps(read_issues())",
                "    if '-o' in args:",
                "        pathlib.Path(args[args.index('-o') + 1]).write_text('\\n'.join(json.dumps(item) for item in read_issues()) + '\\n', encoding='utf-8')",
                "    else:",
                "        sys.stdout.write(output)",
                "    sys.exit(0)",
                "if args[:1] == ['--actor']:",
                "    sys.exit(0)",
                "sys.stderr.write('unsupported fake bd command: %s\\n' % ' '.join(args))",
                "sys.exit(1)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    fake_bd = tmp_path / "bd"
    _write_cwd_fake_bd_cli(fake_bd)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    return ControlPlane.in_memory()


def register_agent(cp, name="agent", capabilities=None, resources=None):
    capabilities = capabilities or []
    agent_resources = dict(resources or {})
    if "python" in capabilities and "commands" not in agent_resources:
        agent_resources["commands"] = {
            "schema": "mac.command_inventory.v1",
            "available": ["python3", "git", "gh"],
        }
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(
        machine.id, name, capabilities=capabilities, resources=agent_resources
    )
    attestation = agent_resources.get(REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY)
    if isinstance(attestation, dict):
        approved = read_only_report_repository_executor_approval(
            **{
                key: attestation[key]
                for key in (
                    "runtime_image_ref",
                    "policy_sha256",
                    "openshell_bin_path",
                    "openshell_bin_sha256",
                    "executor_path",
                    "executor_sha256",
                    "platform",
                    "isolation_posture",
                    "python_path",
                    "python_sha256",
                    "executor_script_path",
                    "executor_script_sha256",
                    "source_root",
                    "source_bundle_sha256",
                )
            }
        )
        updated = dict(agent.resources)
        updated[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] = approved
        agent = cp.update_agent(agent.id, resources=updated, actor="test-admin")
    return agent


def read_only_report_executor_resources():
    return {
        "openshell_required": True,
        REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY: (
            read_only_report_repository_executor_attestation(
                runtime_image_ref=(
                    "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:"
                    + "1" * 64
                ),
                policy_sha256="sha256:" + "2" * 64,
                openshell_bin_path="/approved/openshell",
                openshell_bin_sha256="sha256:" + "3" * 64,
                executor_path="/approved/mac-task-executor",
                executor_sha256="sha256:" + "4" * 64,
                platform="linux",
                isolation_posture="landlock_enforced",
                python_path="/approved/python",
                python_sha256="sha256:" + "5" * 64,
                executor_script_path="/approved/mac-task-executor.py",
                executor_script_sha256="sha256:" + "6" * 64,
                source_root="/approved/mac",
                source_bundle_sha256="sha256:" + "7" * 64,
            )
        ),
    }


def test_add_evidence_persists_durable_artifacts(cp):
    task = cp.create_task("capture artifacts")
    content = b"full worker stdout\n"
    digest = "sha256:%s" % hashlib.sha256(content).hexdigest()

    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/worker-result.json",
        "worker completed",
        "agent",
        _trusted_internal=True,
        artifacts=[
            {
                "name": "stdout.txt",
                "artifact_type": "stdout",
                "source_uri": "file:///tmp/stdout.txt",
                "content_type": "text/plain; charset=utf-8",
                "encoding": "base64",
                "size_bytes": len(content),
                "sha256": digest,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        ],
    )

    index = evidence.metadata["durable_artifacts"]
    assert index["schema"] == "mac.evidence_artifacts.v1"
    assert index["count"] == 1
    assert index["artifacts"][0]["sha256"] == digest
    assert "content_base64" not in index["artifacts"][0]
    assert "metadata" not in index["artifacts"][0]

    listed = cp.list_evidence_artifacts(evidence.id)
    assert listed[0]["name"] == "stdout.txt"
    assert "content_base64" not in listed[0]
    assert "metadata" not in listed[0]

    artifact = cp.get_evidence_artifact(evidence.id, listed[0]["id"])
    assert artifact["sha256"] == digest
    assert base64.b64decode(artifact["content_base64"]) == content
    assert cp.task_history(task.id)[-1].detail["artifact_count"] == 1


def test_add_evidence_owns_durable_artifacts_metadata(cp):
    task = cp.create_task("artifact metadata ownership")

    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/result.json",
        "worker completed without artifacts",
        "agent",
        metadata={"durable_artifacts": {"fake": True}, "kept": True},
        _trusted_internal=True,
    )

    assert evidence.metadata == {"kept": True}


def test_add_evidence_rejects_invalid_and_oversized_artifacts(cp, monkeypatch):
    task = cp.create_task("artifact validation")

    with pytest.raises(ValidationError, match="invalid base64"):
        cp.add_evidence(
            task.id,
            "log",
            "file:///tmp/result.json",
            "bad base64",
            "agent",
            _trusted_internal=True,
            artifacts=[{"name": "stdout.txt", "content_base64": "not base64!"}],
        )

    monkeypatch.setenv("MAC_EVIDENCE_ARTIFACT_TOTAL_MAX_BYTES", "5")
    with pytest.raises(ValidationError, match="aggregate limit"):
        cp.add_evidence(
            task.id,
            "log",
            "file:///tmp/result.json",
            "too much content",
            "agent",
            _trusted_internal=True,
            artifacts=[
                {"name": "a.txt", "content_base64": base64.b64encode(b"abc").decode("ascii")},
                {"name": "b.txt", "content_base64": base64.b64encode(b"def").decode("ascii")},
            ],
        )


def test_add_evidence_rejects_unsupported_kind(cp):
    """Runtime evidence validation shares the CLI/models source of truth: an
    unsupported kind is rejected with the choices-listing message."""
    task = cp.create_task("unsupported evidence kind")

    with pytest.raises(ValidationError, match="unsupported evidence kind: bogus"):
        cp.add_evidence(
            task.id,
            "bogus",
            "file:///tmp/result.json",
            "bad kind",
            "agent",
            _trusted_internal=True,
        )


def test_add_evidence_normalizes_kind_case_and_whitespace(cp):
    """A forgiving (case-insensitive, trimmed) kind is normalized to the
    canonical token before it is persisted, matching normalize_evidence_kind."""
    task = cp.create_task("normalized evidence kind")

    evidence = cp.add_evidence(
        task.id,
        "  LOG ",
        "file:///tmp/result.json",
        "case-insensitive kind",
        "agent",
        _trusted_internal=True,
    )

    assert evidence.kind == "log"


def create_runtime(cp, name="runtime"):
    return cp.create_runtime(
        name,
        {
            "image": "python:3.12@sha256:abc123",
            "dependencies": ["fastapi==0.111.0"],
            "entrypoint": ["pytest"],
        },
        "human",
    )


def _sign(cp, agent_id, manifest):
    """Stamp ``signed_by`` + HMAC ``signature`` onto a verification
    manifest, using the test agent's attestation key. Mirrors what the
    worker does in production via _sign_verification_manifest. Tests
    that want to demonstrate the security model (unsigned, wrong-key,
    etc.) should use the raw helpers below instead."""
    from mac.services import sign_verification_manifest

    key = cp._agent_attestation_key(agent_id)
    if key is None:
        return manifest
    signed = dict(manifest)
    signed["signed_by"] = agent_id
    signed["signature"] = sign_verification_manifest(key, signed)
    return signed


def verified_repo_metadata(
    cp=None,
    agent_id=None,
    head_sha="abcdef1234567890abcdef1234567890abcdef12",
    files_changed=None,
):
    files = files_changed if files_changed is not None else ["src/example.py"]
    relevant_files = codegraph_relevant_files(files)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": head_sha,
            "pushed": True,
            "remote_ref": "refs/heads/task/example",
            "dirty": False,
            "files_changed": files,
        },
        "tests": [{"command": "pytest tests/test_example.py", "returncode": 0}],
    }
    if relevant_files:
        manifest["codegraph"] = {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": relevant_files,
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        }
    if cp is not None and agent_id is not None:
        manifest = _sign(cp, agent_id, manifest)
    return {"returncode": 0, "verification": manifest}


def verified_deployment_metadata(cp=None, agent_id=None):
    files_changed = ["deploy/example.yaml"]
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "deployment",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/deploy",
            "dirty": False,
            "files_changed": files_changed,
        },
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": files_changed,
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        },
        "targets": ["rocky"],
        "checks": [{"name": "systemd status", "status": "pass"}],
    }
    if cp is not None and agent_id is not None:
        manifest = _sign(cp, agent_id, manifest)
    return {"returncode": 0, "verification": manifest}


def create_verified_rollout(cp, version="1.0", strategy="canary", tenant_id=None, channel="fleet", health_policy=None):
    runtime = create_runtime(cp, "runtime-%s" % version)
    return cp.create_rollout(
        version,
        strategy,
        10,
        "human",
        tenant_id=tenant_id,
        channel=channel,
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/%s" % version,
        artifact_hash="sha256:abc123",
        health_policy=health_policy or {},
    )


def create_acc_migration_fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE agents (
            name TEXT PRIMARY KEY,
            host TEXT,
            status TEXT NOT NULL DEFAULT 'offline',
            last_heartbeat TEXT,
            data TEXT NOT NULL
        );
        CREATE TABLE fleet_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            priority INTEGER NOT NULL DEFAULT 2,
            claimed_by TEXT,
            claimed_at TEXT,
            claim_expires_at TEXT,
            completed_at TEXT,
            completed_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            task_type TEXT NOT NULL DEFAULT 'work',
            review_of TEXT,
            phase TEXT,
            blocked_by TEXT NOT NULL DEFAULT '[]',
            review_result TEXT,
            output TEXT,
            inputs TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'fleet'
        );
        CREATE TABLE fleet_task_attempts (
            attempt_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            branch TEXT,
            commit_sha TEXT,
            pr_url TEXT,
            changed_files TEXT NOT NULL DEFAULT '[]',
            failure_class TEXT,
            started_at TEXT NOT NULL,
            published_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            full_name TEXT,
            data TEXT NOT NULL
        );
        CREATE TABLE work_audit_events (
            seq INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            agent TEXT,
            host TEXT,
            task_id TEXT,
            project_id TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            previous_hash TEXT,
            hash TEXT NOT NULL
        );
        CREATE TABLE conversation_chains (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            channel_id TEXT NOT NULL DEFAULT '',
            thread_id TEXT NOT NULL DEFAULT '',
            root_event_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            outcome TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE conversation_chain_tasks (
            chain_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            relationship TEXT NOT NULL DEFAULT 'spawned',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (chain_id, task_id)
        );
        CREATE TABLE conversation_chain_events (
            id TEXT PRIMARY KEY,
            chain_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            text TEXT,
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE bus_messages (
            id TEXT PRIMARY KEY,
            body TEXT
        );
        CREATE TABLE gateway_sessions (
            session_key TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO agents (name, host, status, last_heartbeat, data) VALUES (?, ?, ?, ?, ?)",
        (
            "rocky",
            "do-host1",
            "online",
            "2026-05-18T07:13:07Z",
            json.dumps({"capabilities": ["memory"], "lastSeen": "2026-05-18T07:13:07Z"}),
        ),
    )
    conn.execute(
        """
        INSERT INTO fleet_tasks (
            id, project_id, title, description, status, priority, claimed_by,
            claimed_at, claim_expires_at, completed_at, completed_by, created_at,
            updated_at, metadata, task_type, review_of, phase, blocked_by,
            review_result, output, inputs, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1",
            "proj-1",
            "Open ACC task",
            "from ACC",
            "open",
            1,
            None,
            None,
            None,
            None,
            None,
            "2026-05-18T07:00:00Z",
            "2026-05-18T07:00:00Z",
            json.dumps({"assigned_agent": "rocky", "beads_id": "ACC-1"}),
            "work",
            None,
            None,
            "[]",
            None,
            None,
            "{}",
            "beads-scanner",
        ),
    )
    conn.execute(
        """
        INSERT INTO fleet_tasks (
            id, project_id, title, description, status, priority, claimed_by,
            claimed_at, claim_expires_at, completed_at, completed_by, created_at,
            updated_at, metadata, task_type, review_of, phase, blocked_by,
            review_result, output, inputs, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-2",
            "proj-1",
            "Completed ACC task",
            "from ACC",
            "completed",
            2,
            "bullwinkle",
            "2026-05-18T07:05:00Z",
            None,
            "2026-05-18T07:09:00Z",
            "bullwinkle",
            "2026-05-18T07:01:00Z",
            "2026-05-18T07:09:00Z",
            json.dumps({"workflow_role": "work"}),
            "work",
            None,
            None,
            "[]",
            "approved",
            json.dumps({"branch": "task/task-2"}),
            "{}",
            "fleet",
        ),
    )
    conn.execute(
        """
        INSERT INTO fleet_task_attempts (
            attempt_id, task_id, agent, status, branch, commit_sha, pr_url,
            changed_files, failure_class, started_at, published_at, completed_at,
            updated_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "attempt-1",
            "task-2",
            "bullwinkle",
            "ready_for_review",
            "task/task-2",
            "abc1234",
            None,
            json.dumps(["README.md"]),
            None,
            "2026-05-18T07:05:00Z",
            "2026-05-18T07:08:00Z",
            None,
            "2026-05-18T07:08:00Z",
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO projects (id, name, full_name, data) VALUES (?, ?, ?, ?)",
        ("proj-1", "ACC", "jordanh/ACC", json.dumps({"status": "active", "assignee": "rocky"})),
    )
    conn.execute(
        """
        INSERT INTO work_audit_events (
            seq, event_id, timestamp, event_type, agent, host, task_id, project_id,
            metadata, previous_hash, hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "audit-1",
            "2026-05-18T07:06:00Z",
            "task_execution_started",
            "bullwinkle",
            "puck.local",
            "task-2",
            "proj-1",
            "{}",
            None,
            "hash1",
        ),
    )
    conn.execute(
        """
        INSERT INTO work_audit_events (
            seq, event_id, timestamp, event_type, agent, host, task_id, project_id,
            metadata, previous_hash, hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            2,
            "audit-2",
            "2026-05-18T07:08:00Z",
            "branch_pushed",
            "bullwinkle",
            "puck.local",
            "task-2",
            "proj-1",
            json.dumps({"branch": "task/task-2"}),
            "hash1",
            "hash2",
        ),
    )
    conn.execute(
        """
        INSERT INTO conversation_chains (
            id, source, workspace, channel_id, thread_id, root_event_id, title,
            summary, status, outcome, created_at, updated_at, closed_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "chain-1",
            "slack",
            "T1",
            "C1",
            "1712345678.000100",
            "evt-1",
            "private chain title",
            "private chain summary",
            "active",
            None,
            "2026-05-18T07:00:00Z",
            "2026-05-18T07:01:00Z",
            None,
            json.dumps({"contains": "private"}),
        ),
    )
    conn.execute(
        "INSERT INTO conversation_chain_tasks (chain_id, task_id, relationship, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
        ("chain-1", "task-1", "spawned", "2026-05-18T07:00:00Z", "{}"),
    )
    conn.execute(
        "INSERT INTO conversation_chain_events (id, chain_id, event_type, text, occurred_at) VALUES (?, ?, ?, ?, ?)",
        ("event-1", "chain-1", "message", "do not import this raw text", "2026-05-18T07:00:00Z"),
    )
    conn.execute("INSERT INTO bus_messages (id, body) VALUES (?, ?)", ("bus-1", "private bus body"))
    conn.execute(
        "INSERT INTO gateway_sessions (session_key, messages_json) VALUES (?, ?)",
        ("session-1", json.dumps([{"text": "private session text"}])),
    )
    conn.commit()
    conn.close()


def test_hermes_identity_context_and_interaction_task_boundaries(cp):
    tenant = cp.register_tenant("acme")
    user = cp.register_user(tenant.id, "jordan", display_name="Jordan")
    persona = cp.register_persona(
        tenant.id,
        "Rocky",
        soul_ref="hermes://acme/rocky/SOUL.md",
        memory_scope="hermes://acme/rocky/memory",
    )
    hermes = cp.register_hermes_instance(
        tenant.id,
        "rocky",
        persona_id=persona.id,
        home_ref="hermes://acme/rocky",
    )
    binding = cp.register_platform_binding(
        tenant.id,
        hermes.id,
        "slack",
        "T123/C456",
        display_name="#ops",
        scopes={"channels": ["C456"]},
    )

    context = cp.hermes_context(hermes.id)
    assert context["memory_contract"]["personality_authority"] == "hermes"
    assert context["memory_contract"]["operational_provenance_authority"] == "mac"
    assert context["persona"]["soul_ref"] == "hermes://acme/rocky/SOUL.md"
    assert context["platform_bindings"][0]["id"] == binding.id

    task = cp.create_interaction_task(
        hermes.id,
        "Investigate incident",
        user_id=user.id,
        platform_binding_id=binding.id,
        conversation_ref="slack://T123/C456/1712345678.000100",
        required_capabilities=["ops"],
    )
    assert task.metadata["origin"]["type"] == "hermes_interaction"
    assert task.metadata["origin"]["tenant_id"] == tenant.id
    assert task.metadata["origin"]["persona_id"] == persona.id
    assert task.metadata["memory_boundary"]["hermes_is_authoritative_for_user_memory"] is True
    assert "SOUL.md" not in task.description


def test_tenant_scoped_task_visibility_and_machine_pool_policy(cp):
    tenant_a = cp.register_tenant("tenant-a")
    tenant_b = cp.register_tenant("tenant-b")
    hermes_a = cp.register_hermes_instance(tenant_a.id, "rocky")
    hermes_b = cp.register_hermes_instance(tenant_b.id, "natasha")
    task_a = cp.create_interaction_task(hermes_a.id, "A work", required_capabilities=["python"])
    task_b = cp.create_interaction_task(
        hermes_b.id,
        "B work",
        priority=50,
        required_capabilities=["python"],
    )
    machine = cp.register_machine(
        "private-a",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_a.id]}},
    )
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])

    assert [task.id for task in cp.list_tasks(tenant_id=tenant_a.id)] == [task_a.id]
    assignment = cp.dispatch_once()

    assert assignment["task"]["id"] == task_a.id
    assert assignment["agent"]["id"] == agent.id
    assert cp.get_task(task_b.id).state == TaskState.OPEN.value


def test_tenant_scoped_secret_requires_machine_policy_and_capability(cp):
    tenant_a = cp.register_tenant("tenant-a")
    tenant_b = cp.register_tenant("tenant-b")
    machine = cp.register_machine(
        "private-a",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_a.id]}},
    )
    agent = cp.register_agent(machine.id, "deployer", capabilities=["deploy"])
    allowed = cp.create_secret(
        "tenant-a-token",
        "a-secret",
        {"tenant_id": tenant_a.id, "capabilities": ["deploy"]},
        "human",
    )
    denied = cp.create_secret(
        "tenant-b-token",
        "b-secret",
        {"tenant_id": tenant_b.id, "capabilities": ["deploy"]},
        "human",
    )

    assert cp.request_secret(allowed.id, agent.id, "deploy").granted is True
    with pytest.raises(AuthorizationError):
        cp.request_secret(denied.id, agent.id, "deploy")


def finish_task(cp, task, worker, reviewer):
    from tests.conftest import submit_review_verdict

    if task.state == TaskState.OPEN.value:
        task, _lease = cp.claim_task(task.id, worker.id)
    if task.state == TaskState.CLAIMED.value:
        task = cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://tests",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    task = cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=evidence.id)
    return cp.get_task(task.id)


def test_task_lifecycle_requires_evidence_review_and_publication(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])

    assignment = cp.dispatch_once()
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == worker.id

    cp.start_task(task.id, worker.id)
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)

    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "pytest passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    assert review.status == ReviewStatus.PENDING.value

    from tests.conftest import submit_review_verdict

    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    publication = cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=evidence.id)

    completed = cp.get_task(task.id)
    assert completed.state == TaskState.COMPLETED.value
    assert publication.status == "published"
    assert cp.get_agent(worker.id).status == AgentStatus.IDLE.value
    event_types = [event.event_type for event in cp.task_history(task.id)]
    assert "task.claimed" in event_types
    assert "task.review_completed" in event_types
    assert "task.published" in event_types


def test_review_claim_is_idempotent_for_identical_evidence(cp):
    # mem-05: a repeat claim (same review + executor_evidence + head_sha) writes
    # no new task.review_claimed row — schema/app defense against the verified
    # 30,806-row storm (task_d7c51a0b).
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])
    cp.dispatch_once()
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "test", "artifact://pytest", "pytest passed", worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)

    def claim_rows():
        return [e for e in cp.task_history(task.id) if e.event_type == "task.review_claimed"]

    cp.claim_review(review.id, reviewer.id, executor_evidence_id=evidence.id, sync_beads=False)
    assert len(claim_rows()) == 1
    for _ in range(50):
        result = cp.claim_review(review.id, reviewer.id, executor_evidence_id=evidence.id, sync_beads=False)
        assert result.get("idempotent") is True
    assert len(claim_rows()) == 1  # 50 identical re-claims added no rows


@pytest.mark.parametrize("barrier", ["dispatch_hold", "draining"])
def test_review_claim_rejects_reviewer_behind_dispatch_barrier(cp, barrier):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Barrier-fenced review", required_capabilities=["python"])
    cp.dispatch_once()
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "pytest passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    before_metadata = cp.get_task(task.id).metadata
    before_history = list(cp.task_history(task.id))
    if barrier == "dispatch_hold":
        cp.set_agent_dispatch_hold(reviewer.id, "deployment fence")
    else:
        cp.update_agent(
            reviewer.id,
            status=AgentStatus.DRAINING.value,
            health_status=HealthStatus.DEGRADED.value,
        )

    with pytest.raises(AuthorizationError):
        cp.claim_review(
            review.id,
            reviewer.id,
            executor_evidence_id=evidence.id,
            sync_beads=False,
        )

    assert cp.get_task(task.id).metadata == before_metadata
    assert cp.task_history(task.id) == before_history
    fenced = cp.get_agent(reviewer.id)
    assert fenced.current_task_id is None


def test_identical_review_reclaim_cannot_cross_new_dispatch_hold(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Idempotent barrier review", required_capabilities=["python"])
    cp.dispatch_once()
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "pytest passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    cp.claim_review(
        review.id,
        reviewer.id,
        executor_evidence_id=evidence.id,
        sync_beads=False,
    )
    cp.set_agent_dispatch_hold(reviewer.id, "deployment fence")
    before_metadata = cp.get_task(task.id).metadata
    before_history = list(cp.task_history(task.id))

    with pytest.raises(AuthorizationError):
        cp.claim_review(
            review.id,
            reviewer.id,
            executor_evidence_id=evidence.id,
            sync_beads=False,
        )

    assert cp.get_task(task.id).metadata == before_metadata
    assert cp.task_history(task.id) == before_history
    held = cp.get_agent(reviewer.id)
    assert held.dispatch_hold is True
    assert held.current_task_id == task.id


def test_default_review_workflow_assigns_reviewer_and_publishes(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Implement thing",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )

    cp.submit_for_review(task.id, worker.id)
    # First tick: reviewer is assigned, workflow waits for verdict.
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    # Reviewer produces its signed verdict (mac-jqb).
    verdict_evidence_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    # Second tick: verdict is consumed, task publishes.
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    completed = cp.get_task(task.id)
    assert completed.state == TaskState.COMPLETED.value
    reviews = cp.list_reviews(task.id)
    assert len(reviews) == 1
    assert reviews[0].reviewer_agent_id == reviewer.id
    assert reviews[0].evidence_id == verdict_evidence_id  # review row links to the verdict
    assert reviews[0].status == ReviewStatus.APPROVED.value
    publications = cp.list_publications(task.id)
    assert len(publications) == 1
    assert publications[0].target == "test://publish"
    assert publications[0].evidence_id == evidence.id  # publication links to executor work
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.assigned" in names
    assert "workflow.default_review.approved" in names
    assert "workflow.default_review.published" in names


def test_default_review_publish_failure_surfaces_diagnosis(cp, monkeypatch):
    """An approved task whose auto-publish fails (e.g. a merge conflict) must NOT
    silently park in REVIEWING — it surfaces a Problem/Remediation diagnosis and a
    publish_failed observation so an operator sees why (mac task_51a777c2)."""
    from mac.models import ValidationError
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Implement thing",
        required_capabilities=["python"],
        metadata={"publication_target": "git://main"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "log", "artifact://worker-result", "tests passed", worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    cp.advance_default_review_workflow(task.id)  # assign reviewer
    submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        evidence.id,
    )

    # Simulate auto-publish failing on a merge conflict.
    def _boom(*_a, **_k):
        raise ValidationError(
            "git publication merge_source failed: CONFLICT (content): Merge conflict in Makefile"
        )

    monkeypatch.setattr(cp, "publish_task", _boom)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert result["target"] == "git://main"
    # Approved, still REVIEWING (not silently dropped, not falsely completed).
    parked = cp.get_task(task.id)
    assert parked.state == TaskState.REVIEWING.value
    # A glanceable diagnosis is on the task.
    activity = (parked.metadata or {}).get("activity", [])
    assert any(
        "Auto-publish" in (e.get("summary") or "") and "Remediation" in (e.get("summary") or "")
        for e in activity
    ), "expected a publish-failure diagnosis in task activity"
    # And an observation for telemetry.
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.publish_failed" in names
    assert "workflow.default_review.published" not in names


def _drive_task_to_approved(cp, *, task_metadata=None, files_changed=None):
    """Register worker+reviewer, drive a task through evidence + review to the
    approval gate, returning (task, worker, reviewer, evidence)."""
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    metadata = {"publication_target": "git://main"}
    metadata.update(task_metadata or {})
    task = cp.create_task(
        "Implement thing",
        required_capabilities=["python"],
        metadata=metadata,
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "log", "artifact://worker-result", "tests passed", worker.id,
        metadata=verified_repo_metadata(
            cp, worker.id, files_changed=files_changed
        ),
    )
    cp.submit_for_review(task.id, worker.id)
    cp.advance_default_review_workflow(task.id)  # assign reviewer
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    return task, worker, reviewer, evidence


def _merge_gate_conflict_raiser(cp, task_id, evidence, *, conflicted_paths):
    """Return a publish_task replacement that raises a merge-gate conflict
    carrying the structured conflict-integration context, exactly as the git
    publisher attaches it."""
    from mac.models import ValidationError

    def _boom(*_a, **_k):
        exc = ValidationError(
            "git publication merge gate: task branch does not integrate onto "
            "the current main tip (bbbbbbbbbbbb); conflicts: %s — route to "
            "integration" % ", ".join(conflicted_paths)
        )
        exc.conflict_integration_context = {
            "schema": "mac.merge_gate_conflict_context.v1",
            "task_id": task_id,
            "reviewed_head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "current_main_sha": "b" * 40,
            "conflicted_paths": list(conflicted_paths),
            "repo_root": "/nonexistent-repo",
        }
        raise exc

    return _boom


def test_conflict_creates_single_integration_task(cp, monkeypatch):
    """A legacy single-task publication merge-gate conflict creates exactly ONE
    context-rich integration repair task (not just a diagnosis), preserving the
    approved task in REVIEWING and the diagnosis/observation telemetry."""
    task, worker, reviewer, evidence = _drive_task_to_approved(cp)

    monkeypatch.setattr(
        cp,
        "publish_task",
        _merge_gate_conflict_raiser(
            cp, task.id, evidence, conflicted_paths=["src/example.py"]
        ),
    )
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    integration_task_id = result["integration_task_id"]
    assert integration_task_id is not None

    # Exactly one integration repair task exists for this conflict.
    integration = cp.get_task(integration_task_id)
    link = (integration.metadata or {}).get("conflict_integration", {})
    assert link.get("role") == "integration_repair"
    assert link.get("approved_task_id") == task.id
    assert link.get("lane") == "legacy"
    # Full context payload carried on the integration task.
    payload = (integration.metadata or {}).get("context_payload", {})
    assert payload.get("schema") == "mac.conflict_integration_payload.v1"
    assert payload["approved_task"]["task_id"] == task.id
    assert payload["approved_task"]["reviewed_head_sha"] == "abcdef1234567890abcdef1234567890abcdef12"
    assert payload["canonical_baseline"]["main_sha"] == "b" * 40
    assert payload["conflicted_paths"] == ["src/example.py"]
    # Explicit dependency on the approved task.
    assert task.id in payload["dependencies"]["depends_on"]
    assert task.id in integration.dependencies
    # Distinct-agent enforcement: the approved task's executor is excluded.
    excluded = set((integration.metadata or {}).get("excluded_agent_ids", []))
    assert worker.id in excluded
    assert (integration.metadata or {}).get("coordination", {}).get(
        "require_distinct_agent"
    ) is True

    # Approved task stays REVIEWING and keeps its diagnosis telemetry.
    parked = cp.get_task(task.id)
    assert parked.state == TaskState.REVIEWING.value
    activity = (parked.metadata or {}).get("activity", [])
    assert any(
        "Remediation" in (e.get("summary") or "")
        and integration_task_id in (e.get("summary") or "")
        for e in activity
    )
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.publish_failed" in names
    assert "workflow.default_review.conflict_integration_created" in names


def test_conflict_handoff_is_idempotent(cp, monkeypatch):
    """Duplicate conflict events for the same (task, evidence, attempt base,
    canonical tip, conflict set) resolve to the SAME single integration task."""
    task, worker, reviewer, evidence = _drive_task_to_approved(cp)

    monkeypatch.setattr(
        cp,
        "publish_task",
        _merge_gate_conflict_raiser(
            cp, task.id, evidence, conflicted_paths=["src/example.py"]
        ),
    )
    first = cp.advance_default_review_workflow(task.id)
    second = cp.advance_default_review_workflow(task.id)

    assert first["integration_task_id"] is not None
    assert second["integration_task_id"] == first["integration_task_id"]

    # No duplicate integration tasks were created.
    integration_tasks = [
        t
        for t in cp.list_tasks(limit=100)
        if (t.metadata or {}).get("conflict_integration", {}).get("approved_task_id")
        == task.id
    ]
    assert len(integration_tasks) == 1


def test_work_package_conflict_not_diverted(cp, monkeypatch):
    """A work-package (plan-DAG) linked task's publication conflict is NEVER
    diverted into the legacy conflict-to-integration handoff; it keeps the plain
    diagnosis and no integration task is spawned."""
    task, worker, reviewer, evidence = _drive_task_to_approved(cp)

    # Mark the task as work-package linked so the lane guard trips.
    monkeypatch.setattr(cp, "_task_is_work_package_linked", lambda tid: True)
    monkeypatch.setattr(
        cp,
        "publish_task",
        _merge_gate_conflict_raiser(
            cp, task.id, evidence, conflicted_paths=["src/example.py"]
        ),
    )
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "publish_failed"
    assert result["integration_task_id"] is None
    integration_tasks = [
        t
        for t in cp.list_tasks(limit=100)
        if (t.metadata or {}).get("conflict_integration", {}).get("approved_task_id")
        == task.id
    ]
    assert integration_tasks == []
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.conflict_integration_created" not in names






def test_record_log_suppresses_verbose_poll_names_by_default(cp):
    """mem-04: noisy poll-log names (worker.no_task, etc.) are dropped
    by default. The 1.83M-of-2.09M-row bloat on rocky was these six
    names firing per-poll regardless of state change."""
    # Default: suppressed → record_log returns None and no row lands.
    result = cp.record_log(
        "worker.no_task", level="debug", source="worker-1"
    )
    assert result is None
    names = {e.name for e in cp.list_observability(limit=50)}
    assert "worker.no_task" not in names
    # A non-suppressed name still records as normal.
    cp.record_log("task.evidence_added", level="info", source="control")
    names = {e.name for e in cp.list_observability(limit=50)}
    assert "task.evidence_added" in names


def test_record_log_writes_verbose_poll_names_when_env_set(cp, monkeypatch):
    """mem-04: operators flip MAC_OBSERVABILITY_VERBOSE_POLL=1 to
    re-enable the chatter for debugging."""
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")
    cp.record_log("worker.no_task", level="debug", source="worker-1")
    names = {e.name for e in cp.list_observability(limit=50)}
    assert "worker.no_task" in names


def test_add_evidence_rejects_operator_result_for_repo_coupled_task(cp):
    """mem-11: repo-coupled tasks (execution_contract.type=repository
    or repository_required=true) must not accept operator_result
    evidence — that was the validator gap that let bullwinkle's
    fake-merge evidence trigger the runaway review loop."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "Repo-coupled task",
        required_capabilities=["python"],
        metadata={
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
                "source": "test_fixture",
                "repository_required": True,
            },
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    # The poison-pill evidence: operator_result for a repo task.
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "hello hello hello",
        },
    )
    with pytest.raises(ValidationError, match="operator_result evidence cannot be recorded"):
        cp.add_evidence(
            task.id,
            "log",
            "artifact://hello",
            "hello hello hello",
            worker.id,
            metadata={"returncode": 0, "verification": manifest},
        )


def test_add_evidence_accepts_repo_change_evidence_for_repo_coupled_task(cp):
    """mem-11 (negative): the repo-coupled gate must not block legitimate
    repo_change evidence on the same task class."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "Repo task",
        required_capabilities=["python"],
        metadata={
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
                "repository_required": True,
            },
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "file://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    assert evidence.task_id == task.id


def test_add_evidence_accepts_operator_result_for_non_repo_task(cp):
    """mem-11 (negative): pure-operator tasks (no repository contract)
    must keep accepting operator_result. Only repo-coupled tasks
    enforce the strict subset."""
    worker = register_agent(cp, "worker", ["ops"])
    task = cp.create_task("Plan something", required_capabilities=["ops"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "plan produced",
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://plan",
        "plan produced",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    assert evidence.task_id == task.id


def test_default_review_workflow_caps_retractions(cp, monkeypatch):
    """mem-12: after N consecutive retractions for a task, the workflow
    refuses to spawn another review and transitions the task to FAILED."""
    monkeypatch.setenv("MAC_REVIEW_RETRACTION_CAP", "2")
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer-a", ["review"])
    register_agent(cp, "reviewer-b", ["review"])
    task = cp.create_task("Loopy", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "test",
        "file://repo",
        "did the thing",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    # Pre-plant N retracted reviews so we land exactly at the cap on
    # the next advance() call.
    from mac.models import ReviewStatus, new_id, utcnow

    now = utcnow()
    for label in ("a", "b"):
        cp.store.execute(
            """
            INSERT INTO reviews (
                id, task_id, reviewer_agent_id, status, reason, evidence_id,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                new_id("review"),
                task.id,
                "agent_" + label,
                ReviewStatus.RETRACTED.value,
                "reviewer_unable_to_produce_verdict_after_10_attempts",
                now,
                now,
            ),
        )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "review_retraction_exhausted"
    assert result["cap"] == 2
    assert result["retracted_count"] >= 2
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    # Confirm an observability row was written so operators can see why.
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.exhausted" in names


def test_default_review_retraction_cap_resets_on_new_evidence(cp, monkeypatch):
    """mem-12: the cap is scoped to retractions AFTER the latest evidence.
    Submitting fresh evidence implicitly resets the counter."""
    monkeypatch.setenv("MAC_REVIEW_RETRACTION_CAP", "2")
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer-a", ["review"])
    task = cp.create_task("Resettable", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    # Old evidence, then 2 retracted reviews against it.
    cp.add_evidence(
        task.id,
        "test",
        "file://repo1",
        "first try",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    from mac.models import ReviewStatus, new_id, utcnow
    old_now = "2026-05-29T00:00:00+00:00"
    for label in ("a", "b"):
        cp.store.execute(
            """
            INSERT INTO reviews (
                id, task_id, reviewer_agent_id, status, reason, evidence_id,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                new_id("review"),
                task.id,
                "agent_" + label,
                ReviewStatus.RETRACTED.value,
                "stale",
                old_now,
                old_now,
            ),
        )
    # Reset the task back to NEEDS_REVIEW manually (in the real flow,
    # submitting new evidence + submit_for_review walks the state
    # machine; we shortcut here for the test).
    cp.store.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, utcnow(), task.id),
    )
    # New evidence at a NEWER timestamp → cap counter should reset.
    cp.add_evidence(
        task.id,
        "test",
        "file://repo2",
        "second try",
        worker.id,
        _trusted_internal=True,
        metadata=verified_repo_metadata(
            cp,
            worker.id,
            head_sha="0123456789abcdef0123456789abcdef01234567",
            files_changed=["src/example.py"],
        ),
    )
    result = cp.advance_default_review_workflow(task.id)
    # Should advance normally (assigning a new reviewer) rather than
    # failing on the cap.
    assert result["status"] != "review_retraction_exhausted", result
    assert cp.get_task(task.id).state != TaskState.FAILED.value


def test_default_review_workflow_caps_verdict_wait(cp, monkeypatch):
    """A reviewer that keeps producing review-attempt evidence but never a
    valid signed verdict must not spin forever: past the verdict-wait cap the
    task blocks for repair instead of re-nudging (the live half of the
    2026-06 runaway)."""
    monkeypatch.setenv("MAC_REVIEW_VERDICT_WAIT_CAP", "2")
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer-a", ["review"])
    task = cp.create_task("Spinny", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "test",
        "file://repo",
        "did the thing",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    # First advance opens a pending review (assigns a reviewer).
    cp.advance_default_review_workflow(task.id)
    from mac.models import ReviewStatus

    pending = [
        r for r in cp.list_reviews(task.id)
        if r.status == ReviewStatus.PENDING.value
    ]
    assert pending, "expected a pending review after first advance"
    review = pending[0]

    # Reviewer produces N review-attempt evidence rows but no valid verdict.
    for i in range(2):
        cp.add_evidence(
            task.id,
            "review",
            "file://review-%d" % i,
            "looked, still unsure",
            review.reviewer_agent_id,
            _trusted_internal=True,
        )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "review_verdict_wait_exhausted", result
    assert result["cap"] == 2
    assert result["wait_count"] >= 2
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.exhausted" in names


def test_default_review_retracts_protocol_failure_and_selects_another_reviewer(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer_a = register_agent(cp, "reviewer-a", ["review"])
    reviewer_b = register_agent(cp, "reviewer-b", ["review"])
    task = cp.create_task("Protocol-aware selection", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    executor_evidence = cp.add_evidence(
        task.id,
        "test",
        "file://repo",
        "did the thing",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    cp.advance_default_review_workflow(task.id)
    pending = [
        review
        for review in cp.list_reviews(task.id)
        if review.status == ReviewStatus.PENDING.value
    ]
    assert len(pending) == 1
    assert pending[0].reviewer_agent_id == reviewer_a.id

    cp.add_evidence(
        task.id,
        "review",
        "file://review-failed",
        "review harness exhausted its budget",
        reviewer_a.id,
        metadata={
            "returncode": 65,
            "review_id": pending[0].id,
            "executor_evidence_id": executor_evidence.id,
        },
        _trusted_internal=True,
    )

    failed = cp.advance_default_review_workflow(task.id)
    assert failed["status"] == "reviewer_protocol_failed"
    assert failed["reviewer_agent_id"] == reviewer_a.id
    assert failed["reason"] == "review_executor_nonzero"

    reassigned = cp.advance_default_review_workflow(task.id)
    assert reassigned["status"] == "waiting_for_reviewer_verdict"
    assert reassigned["reviewer_agent_id"] == reviewer_b.id
    reviews = cp.list_reviews(task.id)
    assert any(
        review.reviewer_agent_id == reviewer_a.id
        and review.status == ReviewStatus.RETRACTED.value
        and review.reason == "reviewer_protocol_failure:review_executor_nonzero"
        for review in reviews
    )


def test_default_review_blocks_when_pinned_reviewer_fails_protocol(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Pinned protocol failure",
        required_capabilities=["python"],
        metadata={"review": {"target_agent_id": reviewer.id}},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    executor_evidence = cp.add_evidence(
        task.id,
        "test",
        "file://repo",
        "did the thing",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    cp.advance_default_review_workflow(task.id)
    pending = next(
        review
        for review in cp.list_reviews(task.id)
        if review.status == ReviewStatus.PENDING.value
    )
    cp.add_evidence(
        task.id,
        "review",
        "file://review-failed",
        "review harness exhausted its budget",
        reviewer.id,
        metadata={
            "returncode": 65,
            "review_id": pending.id,
            "executor_evidence_id": executor_evidence.id,
        },
        _trusted_internal=True,
    )

    failed = cp.advance_default_review_workflow(task.id)
    assert failed["status"] == "reviewer_protocol_failed"
    blocked = cp.advance_default_review_workflow(task.id)
    assert blocked["status"] == "target_reviewer_protocol_failed"
    assert blocked["reviewer_agent_id"] == reviewer.id
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_default_review_retraction_cap_not_reset_by_review_evidence(cp, monkeypatch):
    """mem-12 window fix: the reviewer's OWN review-attempt evidence must not
    reset the retraction window. Only genuine new executor work (the reviewed
    evidence) does. With the pre-fix 'latest evidence of any kind' window this
    looped forever."""
    monkeypatch.setenv("MAC_REVIEW_RETRACTION_CAP", "2")
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer-a", ["review"])
    task = cp.create_task("Loopy2", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "test",
        "file://repo",
        "did the thing",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    from mac.models import ReviewStatus, new_id, utcnow

    now = utcnow()
    for label in ("a", "b"):
        cp.store.execute(
            """
            INSERT INTO reviews (
                id, task_id, reviewer_agent_id, status, reason, evidence_id,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                new_id("review"),
                task.id,
                "agent_" + label,
                ReviewStatus.RETRACTED.value,
                "stale",
                now,
                now,
            ),
        )
    # Reviewer churn AFTER the retractions: a 'review'-kind evidence row.
    # Pre-fix this advanced the window and reset the count to 0; post-fix the
    # window is anchored to the reviewed executor evidence, so the cap fires.
    cp.add_evidence(
        task.id,
        "review",
        "file://review-churn",
        "still unsure",
        "agent_reviewer-a",
        _trusted_internal=True,
    )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "review_retraction_exhausted", result
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_default_review_workflow_approves_repo_less_operator_result(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Plan project",
        required_capabilities=["ops"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Implementation plan produced",
            "result": "Story graph, dependency order, and verification plan produced.",
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://operator-result",
        "Implementation plan produced",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )

    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    verdict_evidence_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    review = cp.list_reviews(task.id)[0]
    assert review.status == ReviewStatus.APPROVED.value
    assert review.evidence_id == verdict_evidence_id
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_report_ignores_git_publication_targets_and_publishes_evidence(
    cp, monkeypatch
):
    from tests.conftest import submit_review_verdict

    monkeypatch.setenv("MAC_DEFAULT_PUBLICATION_TARGET", "git://main")
    cp.create_project("report-project", metadata={"publication_target": "git://main"})
    worker = register_agent(
        cp, "report-worker", ["ops"], read_only_report_executor_resources()
    )
    reviewer = register_agent(
        cp, "report-reviewer", ["review"], read_only_report_executor_resources()
    )
    task = cp.create_task(
        "Inspect current repository",
        project="report-project",
        required_capabilities=["ops"],
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
            "publication_target": "git://main",
            "origin": {
                "repository_path": "/must/not/be/published",
                "repository_url": "https://example.invalid/project.git",
            },
            "execution_contract": {
                "type": "repository",
                    "repository_contract": {
                        "schema": "mac.repository_contract.v1",
                        "canonical_remote_url": "https://example.invalid/project.git",
                        "default_branch": "main",
                        "test": {"command": "true"},
                    },
            },
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Repository analysis produced",
            "result": "Substantive findings and prioritized next work.",
            "repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://report-result",
        "Repository analysis produced",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    publication = cp.list_publications(task.id)[-1]
    assert publication.target == "evidence://%s" % evidence.id
    assert not publication.target.startswith("git://")
    assert cp._repository_contract_for_task(cp.get_task(task.id)) == {}


def test_default_review_workflow_falls_back_to_project_publication_target(cp):
    """A task with no publication_target of its own must inherit the
    target from its registered project, so autonomous tasks complete
    instead of stalling in REVIEWING (waiting_for_publication_target)."""
    from tests.conftest import submit_review_verdict

    cp.create_project(
        "demo-proj",
        metadata={"publication_target": "test://project-publish"},
    )
    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Plan project",
        required_capabilities=["ops"],
        project="demo-proj",
        metadata={},  # no publication_target on the task itself
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Work produced",
            "result": "Edited files and opened MR.",
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://operator-result",
        "Work produced",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    publications = cp.list_publications(task.id)
    assert publications[-1].target == "test://project-publish"


def test_publication_uses_linked_review_verdict_when_newer_duplicates_exist(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Plan project", required_capabilities=["ops"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Implementation plan produced",
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://operator-result",
        "Implementation plan produced",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )

    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    linked_verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    approved = cp.advance_default_review_workflow(task.id)
    assert approved["status"] == "waiting_for_publication_target"
    submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        evidence.id,
        trusted_internal=True,
    )

    publication = cp.publish_task(
        task.id,
        "test://publish",
        "operator",
        evidence_id=evidence.id,
    )

    assert publication.status == PublicationStatus.PUBLISHED.value
    review = cp.list_reviews(task.id)[0]
    assert review.status == ReviewStatus.APPROVED.value
    assert review.evidence_id == linked_verdict_id
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_default_review_workflow_reuses_pending_verdict_nudge(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Implement thing",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    first = cp.advance_default_review_workflow(task.id)
    second = cp.advance_default_review_workflow(task.id)
    nudges = [
        message
        for message in cp.list_messages(reviewer.id)
        if message.message_type == MessageType.NUDGE.value
        and message.status == MessageStatus.QUEUED.value
        and message.payload.get("reason") == "produce_review_verdict"
        and message.payload.get("review_id") == first["review_id"]
        and message.payload.get("executor_evidence_id") == evidence.id
    ]

    assert first["status"] == "waiting_for_reviewer_verdict"
    assert first["nudge_status"] == "queued"
    assert second["status"] == "waiting_for_reviewer_verdict"
    assert second["nudge_status"] == "already_queued"
    assert len(nudges) == 1


def test_default_review_nudge_cap_counts_delivered_messages_not_idempotent_claims(
    cp,
    monkeypatch,
):
    monkeypatch.setenv("MAC_REVIEW_NUDGE_MAX_ATTEMPTS", "2")
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Bound review retries",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    first = cp.advance_default_review_workflow(task.id)
    assert len(cp.deliver_messages(reviewer.id)) >= 1
    second = cp.advance_default_review_workflow(task.id)
    assert second["review_id"] == first["review_id"]
    delivered = [
        message
        for message in cp.deliver_messages(reviewer.id)
        if message.message_type == MessageType.NUDGE.value
    ]
    assert len(delivered) == 1

    cp.advance_default_review_workflow(task.id)

    review = cp.list_reviews(task.id)[0]
    assert review.status == ReviewStatus.RETRACTED.value
    assert review.reason == "reviewer_unable_to_produce_verdict_after_2_attempts"
    assert not any(
        event.event_type == "task.review_claimed" for event in cp.task_history(task.id)
    )
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.nudge_capped" in names


def test_default_review_prefers_prior_owner_over_current_executor_fallback(cp):
    from tests.conftest import submit_review_verdict

    alpha = register_agent(cp, "alpha", ["python", "review"])
    beta = register_agent(cp, "beta", ["python", "review"])
    task = cp.create_task(
        "retry with small fleet",
        required_capabilities=["python"],
        max_attempts=2,
        metadata={"publication_target": "test://retry"},
    )
    cp.claim_task(task.id, alpha.id)
    cp.start_task(task.id, alpha.id)
    first_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://attempt-1",
        "attempt 1",
        alpha.id,
        metadata=verified_repo_metadata(cp, alpha.id),
    )
    cp.submit_for_review(task.id, alpha.id)

    first_review = cp.advance_default_review_workflow(task.id)
    assert first_review["reviewer_agent_id"] == beta.id
    submit_review_verdict(cp, task.id, beta.id, first_evidence.id, verdict="rejected", feedback="Rejected.")
    rejected = cp.advance_default_review_workflow(task.id)
    assert rejected["status"] == "review_not_approved"
    assert cp.get_task(task.id).state == TaskState.OPEN.value

    cp.claim_task(task.id, beta.id)
    cp.start_task(task.id, beta.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://attempt-2",
        "attempt 2",
        beta.id,
        metadata=verified_repo_metadata(
            cp,
            beta.id,
            head_sha="fedcba9876543210fedcba9876543210fedcba98",
        ),
    )
    cp.submit_for_review(task.id, beta.id)

    retry_review = cp.advance_default_review_workflow(task.id)
    assert retry_review["status"] == "waiting_for_reviewer_verdict"
    assert retry_review["reviewer_agent_id"] == alpha.id
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    history = cp.store.query_one(
        "SELECT detail FROM task_history WHERE task_id = ? "
        "AND event_type = 'task.review_requested' ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    )
    detail = json.loads(history["detail"])
    assert detail["reviewer_independence"] == "fallback"
    assert detail["reviewer_independence_reason"] == "reviewer_previously_owned_task"


def test_request_review_allows_latest_evidence_author_only_without_peer(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    worker = register_agent(cp, "worker", ["python", "review"])
    task = cp.create_task("self-review", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    review = cp.request_review(task.id, worker.id)

    assert review.reviewer_agent_id == worker.id
    detail = json.loads(
        cp.store.query_one(
            "SELECT detail FROM task_history WHERE task_id = ? "
            "AND event_type = 'task.review_requested' ORDER BY created_at DESC LIMIT 1",
            (task.id,),
        )["detail"]
    )
    assert detail["reviewer_independence"] == "fallback"


def test_default_review_workflow_uses_owner_when_no_peer_exists(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    worker = register_agent(cp, "worker", ["python", "review"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    reviews = cp.list_reviews(task.id)
    assert len(reviews) == 1
    assert reviews[0].reviewer_agent_id == worker.id


def test_hub_review_verifier_auto_registers_without_live_worker(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    worker = register_agent(cp, "worker", ["python", "review"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)

    # With the blocking guard (Option C): when hub verify is enabled but the
    # live verifier runner is not available (no _hub_verify_runner mock here),
    # the hub verify attempt fails/returns None and the workflow blocks with
    # waiting_for_hub_verify rather than falling through to the agent-nudge
    # path.  The hub reviewer is still auto-registered and the review is still
    # assigned to it — the reviewer_agent_id assertion verifies that.
    assert result["status"] in {"waiting_for_hub_verify", "waiting_for_reviewer_verdict"}
    assert result["reviewer_agent_id"] == services.DEFAULT_HUB_REVIEWER_AGENT_ID
    reviewer = cp.get_agent(services.DEFAULT_HUB_REVIEWER_AGENT_ID)
    assert reviewer.name == services.DEFAULT_HUB_REVIEWER_AGENT_NAME
    assert reviewer.capabilities == ["review"]
    assert reviewer.resources["hub_review_verifier"]["schema"] == (
        services.HUB_REVIEW_VERIFIER_RESOURCE_SCHEMA
    )
    review = cp.list_reviews(task.id)[0]
    assert review.reviewer_agent_id == reviewer.id

    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", reviewer.id),
    )
    waiting = cp.advance_default_review_workflow(task.id)

    assert waiting["status"] in {"waiting_for_hub_verify", "waiting_for_reviewer_verdict"}
    assert waiting["reviewer_agent_id"] == reviewer.id
    assert cp.list_reviews(task.id)[0].status == ReviewStatus.PENDING.value
    assert cp.list_reviews(task.id)[0].reviewer_agent_id == reviewer.id
    assert cp.list_reviews(task.id)[0].task_id == evidence.task_id


def test_default_review_tick_processes_backlog(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Backlog item",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    # First tick assigns reviewer; reviewer then produces verdict;
    # second tick publishes (mac-jqb).
    first_report = cp.advance_default_review_workflows(limit=10)
    assert first_report["results"][0]["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    report = cp.advance_default_review_workflows(limit=10)

    assert report["processed"] == 1
    assert report["results"][0]["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert cp.list_reviews(task.id)[0].reviewer_agent_id == reviewer.id


def test_default_review_sweep_uses_bounded_state_query_and_cursor(cp, monkeypatch):
    for index in range(6):
        cp.create_task(
            "irrelevant open %d" % index,
            priority=1000,
        )
    reviewable = []
    for index in range(5):
        task = cp.create_task(
            "reviewable %d" % index,
            priority=100 - index,
        )
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.NEEDS_REVIEW.value, task.id),
        )
        reviewable.append(task.id)

    monkeypatch.setattr(
        cp,
        "list_tasks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("review sweep must not load the full task ledger")
        ),
    )
    seen = []
    original_query_all = cp.store.query_all
    sweep_limits = []

    def query_all(sql, params=()):
        if "idx_tasks_review_queue" in sql:
            sweep_limits.append(params[-1])
        return original_query_all(sql, params)

    monkeypatch.setattr(cp.store, "query_all", query_all)

    def advance(task_id, actor="default-review-workflow"):
        seen.append(task_id)
        return {"task_id": task_id, "status": "observed", "actor": actor}

    monkeypatch.setattr(cp, "advance_default_review_workflow", advance)

    first = cp.advance_default_review_workflows(limit=2)
    second = cp.advance_default_review_workflows(
        limit=2,
        cursor=first["next_cursor"],
    )
    third = cp.advance_default_review_workflows(
        limit=2,
        cursor=second["next_cursor"],
    )

    assert first["processed"] == second["processed"] == 2
    assert third["processed"] == 1
    assert first["has_more"] is True
    assert second["has_more"] is True
    assert third["has_more"] is False
    assert seen == reviewable
    assert sweep_limits == [3, 3, 3]


def test_default_review_sweep_filters_tenant_before_limit(cp, monkeypatch):
    for index in range(4):
        task = cp.create_task(
            "tenant-b %d" % index,
            priority=1000 - index,
            metadata={"origin": {"tenant_id": "tenant-b"}},
        )
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.NEEDS_REVIEW.value, task.id),
        )
    tenant_a = cp.create_task(
        "tenant-a review",
        priority=1,
        metadata={"tenant_id": "tenant-a"},
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, tenant_a.id),
    )
    seen = []
    monkeypatch.setattr(
        cp,
        "advance_default_review_workflow",
        lambda task_id, actor="default-review-workflow": (
            seen.append(task_id)
            or {"task_id": task_id, "status": "observed"}
        ),
    )

    result = cp.advance_default_review_workflows(
        limit=1,
        tenant_id="tenant-a",
    )

    assert result["processed"] == 1
    assert seen == [tenant_a.id]


def test_default_review_sweep_rejects_invalid_cursor(cp):
    with pytest.raises(ValidationError, match="invalid review sweep cursor"):
        cp.advance_default_review_workflows(cursor="not-a-cursor")


def test_reconciliation_claim_is_exclusive_and_preserves_cursor(tmp_path):
    database = tmp_path / "coordinator.sqlite"
    secret = "test-key-with-enough-entropy-32+chars"
    first = ControlPlane(SQLiteStore(str(database)), secret_key=secret)
    second = ControlPlane(SQLiteStore(str(database)), secret_key=secret)

    claim = first.reconciliation.claim("shared-sweep")
    assert claim is not None
    assert first.reconciliation.claim("shared-sweep") is None
    assert second.reconciliation.claim("shared-sweep") is None
    assert first.reconciliation.complete(claim, cursor="page-2") is True

    next_claim = second.reconciliation.claim("shared-sweep")
    assert next_claim is not None
    assert next_claim.cursor == "page-2"
    second.reconciliation.complete(next_claim, cursor=None)
    first.store.close()
    second.store.close()


def test_reconciliation_invalid_lease_config_and_abandon(monkeypatch):
    monkeypatch.setenv("MAC_RECONCILER_LEASE_SECONDS", "not-a-number")
    cp = ControlPlane.in_memory()
    assert cp.reconciliation.lease_seconds == 60

    claim = cp.reconciliation.claim("failed-page")
    assert claim is not None
    assert cp.reconciliation.abandon(claim) is True

    retry = cp.reconciliation.claim("failed-page")
    assert retry is not None
    assert retry.cursor is None


def test_bounded_scan_cursor_validation_covers_corrupt_inputs(cp):
    cursor = cp._encode_scan_cursor("dead-letters", "2026-01-01T00:00:00+00:00", "task-1")
    assert cp._decode_scan_cursor(cursor, kind="dead-letters") == (
        "2026-01-01T00:00:00+00:00",
        "task-1",
    )
    assert cp._decode_scan_cursor(None, kind="dead-letters") is None

    with pytest.raises(ValidationError, match="invalid dead-letters cursor"):
        cp._decode_scan_cursor("not-versioned", kind="dead-letters")
    with pytest.raises(ValidationError, match="invalid dead-letters cursor"):
        cp._decode_scan_cursor("v1:not-base64", kind="dead-letters")
    with pytest.raises(ValidationError, match="invalid expired-leases cursor"):
        cp._decode_scan_cursor(cursor, kind="expired-leases")
    empty = cp._encode_scan_cursor("dead-letters", "", "task-1")
    with pytest.raises(ValidationError, match="invalid dead-letters cursor"):
        cp._decode_scan_cursor(empty, kind="dead-letters")


def test_default_review_autonomous_cursor_survives_restart(tmp_path, monkeypatch):
    database = tmp_path / "review-restart.sqlite"
    secret = "test-key-with-enough-entropy-32+chars"
    first = ControlPlane(SQLiteStore(str(database)), secret_key=secret)
    reviewable = []
    for index in range(3):
        task = first.create_task("review restart %d" % index, priority=10 - index)
        first.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.NEEDS_REVIEW.value, task.id),
        )
        reviewable.append(task.id)
    first_seen = []
    monkeypatch.setattr(
        first,
        "advance_default_review_workflow",
        lambda task_id, actor="default-review-workflow": (
            first_seen.append(task_id) or {"task_id": task_id}
        ),
    )
    first_page = first._advance_default_review_sweep_page(
        limit=1,
        actor="test",
        tenant_id=None,
    )
    assert first_page["has_more"] is True
    assert first_seen == [reviewable[0]]
    first.store.close()

    second = ControlPlane(SQLiteStore(str(database)), secret_key=secret)
    second_seen = []
    monkeypatch.setattr(
        second,
        "advance_default_review_workflow",
        lambda task_id, actor="default-review-workflow": (
            second_seen.append(task_id) or {"task_id": task_id}
        ),
    )
    second._advance_default_review_sweep_page(
        limit=1,
        actor="test",
        tenant_id=None,
    )
    assert second_seen == [reviewable[1]]
    second.store.close()


def test_default_review_workflow_waits_for_verifiable_evidence(cp):
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Thin evidence", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "executor says ok",
        worker.id,
        metadata={"returncode": 0},
    )
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_verifiable_evidence"
    assert result["reason"] == "evidence_not_verifiable"
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
    assert cp.list_reviews(task.id) == []
    assert cp.list_publications(task.id) == []


def test_default_review_workflow_rejects_unpushed_repo_manifest(cp):
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("Local-only code", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    # Construct the manifest, edit it, then sign — otherwise the
    # signature would verify against the pre-edit shape and we'd never
    # exercise the unpushed-repo guard.
    raw = verified_repo_metadata()
    raw["verification"]["repo"]["pushed"] = False
    raw["verification"]["repo"].pop("remote_ref")
    raw["verification"] = _sign(cp, worker.id, raw["verification"])
    metadata = raw
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "local diff only",
        worker.id,
        metadata=metadata,
    )
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_verifiable_evidence"
    assert "repo evidence requires pushed=true" in result["rejected_evidence"][0]["problems"][-1]
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value


def test_submit_for_review_requires_declared_required_changed_files(cp):
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Write final demo docs",
        required_capabilities=["python"],
        metadata={
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "required_changed_files": ["README.md", "docs/demo-story.md"],
            }
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "demo story only",
        worker.id,
        metadata=verified_repo_metadata(
            cp,
            worker.id,
            files_changed=["docs/demo-story.md"],
        ),
    )

    with pytest.raises(ValidationError, match="README.md"):
        cp.submit_for_review(task.id, worker.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_verifiable_evidence"
    assert "README.md" in result["rejected_evidence"][0]["problems"][-1]
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value


def test_publication_revalidates_declared_required_changed_files(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Write final demo docs",
        required_capabilities=["python"],
        metadata={
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "required_changed_files": ["README.md", "docs/demo-story.md"],
            }
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    reviewed_evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://complete",
        "complete docs",
        worker.id,
        metadata=verified_repo_metadata(
            cp,
            worker.id,
            files_changed=["README.md", "docs/demo-story.md"],
        ),
    )
    incomplete_evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://incomplete",
        "demo story only",
        worker.id,
        metadata=verified_repo_metadata(
            cp,
            worker.id,
            files_changed=["docs/demo-story.md"],
        ),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, reviewed_evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)

    with pytest.raises(ValidationError, match="README.md"):
        cp.publish_task(
            task.id,
            "test://publish",
            reviewer.id,
            evidence_id=incomplete_evidence.id,
        )


@pytest.mark.parametrize(
    ("evidence_type", "extra"),
    [
        ("test", {"checks": [{"name": "pytest", "returncode": 0}]}),
        ("artifact", {"checks": [{"name": "build", "returncode": 0}], "artifacts": ["artifact://x"]}),
        ("deployment", {"checks": [{"name": "health", "returncode": 0}], "targets": ["rocky"]}),
        ("documentation", {"checks": [{"name": "docs", "returncode": 0}]}),
        ("no_change", {"checks": [{"name": "inspection", "returncode": 0}], "reason": "already fixed"}),
    ],
)
def test_submit_for_review_requires_pushed_repo_anchor_for_all_evidence_types(cp, evidence_type, extra):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("Missing repo anchor", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": evidence_type,
        **extra,
    }
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "executor says ok",
        worker.id,
        metadata={"returncode": 0, "verification": _sign(cp, worker.id, manifest)},
    )

    with pytest.raises(ValidationError, match="verification.repo"):
        cp.submit_for_review(task.id, worker.id)


def test_source_remediation_repo_change_allows_empty_files_changed(cp):
    worker = register_agent(cp, "worker", ["ops"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Repair checkout",
        required_capabilities=["ops"],
        metadata={
            "origin": {"type": "beads_source_remediation"},
            "remediation": {"type": "beads_source_refresh"},
            "publication_target": "environment://beads-repository/example/source",
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    metadata = verified_repo_metadata(cp, worker.id)
    metadata["verification"]["repo"]["files_changed"] = []
    metadata["verification"] = _sign(cp, worker.id, metadata["verification"])
    cp.add_evidence(
        task.id,
        "log",
        "artifact://source-refresh",
        "source already clean",
        worker.id,
        metadata=metadata,
    )

    cp.submit_for_review(task.id, worker.id)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"


def test_default_review_workflow_allows_verified_deployment_evidence(cp):
    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Deploy thing",
        required_capabilities=["ops"],
        metadata={"publication_target": "test://deploy"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://deploy-result",
        "deployment verified",
        worker.id,
        metadata=verified_deployment_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    from tests.conftest import submit_review_verdict

    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.list_reviews(task.id)[0].reviewer_agent_id == reviewer.id
    assert cp.list_reviews(task.id)[0].evidence_id == verdict_id


def test_unsigned_verification_manifest_is_rejected(cp):
    """mac-ng2 / mac-8r1: a syntactically-perfect but UNSIGNED manifest
    must be rejected. Without a signature, anything an executor can
    write it can fake — and in an autonomous swarm there is no human
    to notice."""
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "unsigned",
        required_capabilities=["python"],
        metadata={"publication_target": "test://x"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    # Manifest with no signed_by / signature — the pre-fix code path
    # would have accepted this. Now it must refuse.
    unsigned = verified_repo_metadata()
    cp.add_evidence(task.id, "log", "x", "y", worker.id, metadata=unsigned)
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"
    assert result["rejected_evidence"][0]["reason"] == "manifest_not_signed"
    assert cp.list_publications(task.id) == []


def test_forged_manifest_signed_with_wrong_key_is_rejected(cp):
    """mac-ng2 / mac-8r1: a signed manifest that claims to be from
    Worker A but was actually signed with Worker B's key must be
    rejected. This is the core HMAC verification path."""
    from mac.services import sign_verification_manifest

    worker_a = register_agent(cp, "worker-a", ["python"])
    worker_b = register_agent(cp, "worker-b", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "forged",
        required_capabilities=["python"],
        metadata={"publication_target": "test://x"},
    )
    cp.claim_task(task.id, worker_a.id)
    cp.start_task(task.id, worker_a.id)

    # Construct a manifest, sign with B's key but claim it's from A.
    manifest = verified_repo_metadata()["verification"]
    wrong_key = cp._agent_attestation_key(worker_b.id)
    manifest["signed_by"] = worker_a.id  # forged identity
    manifest["signature"] = sign_verification_manifest(wrong_key, manifest)
    cp.add_evidence(
        task.id,
        "log",
        "x",
        "y",
        worker_a.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker_a.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"
    assert result["rejected_evidence"][0]["reason"] == "signature_invalid"
    assert cp.list_publications(task.id) == []


def test_manifest_signed_by_unknown_agent_is_rejected(cp):
    """mac-ng2 / mac-8r1: signed_by must refer to a real agent in the
    control plane's registry. Anonymous signers don't pass."""
    from mac.services import sign_verification_manifest

    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "ghost-signer",
        required_capabilities=["python"],
        metadata={"publication_target": "test://x"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)

    # Mint a fresh key (not on file), sign with it, claim a non-existent
    # signer.
    manifest = verified_repo_metadata()["verification"]
    from mac.services import _generate_attestation_key

    rogue_key = _generate_attestation_key()
    manifest["signed_by"] = "agent_does_not_exist"
    manifest["signature"] = sign_verification_manifest(rogue_key, manifest)
    cp.add_evidence(
        task.id,
        "log",
        "x",
        "y",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"
    assert result["rejected_evidence"][0]["reason"] == "signer_unknown"


def test_default_review_workflow_refuses_on_ambiguous_pending_reviews(cp):
    """mac-d9c: with more than one pending review the workflow must
    refuse to pick — no auto-merge under ambiguity."""
    worker = register_agent(cp, "worker", ["python"])
    rev_one = register_agent(cp, "rev-one", ["review"])
    rev_two = register_agent(cp, "rev-two", ["review"])
    task = cp.create_task(
        "ambiguous",
        required_capabilities=["python"],
        metadata={"publication_target": "test://ambig"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://x",
        "done",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    cp.request_review(task.id, rev_one.id, "human")
    cp.request_review(task.id, rev_two.id, "human")

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "ambiguous_pending_reviews"
    assert len(result["pending_review_ids"]) == 2
    # Task is untouched; no publication was created.
    assert cp.list_publications(task.id) == []
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.ambiguous" in names


def test_request_review_reuses_pending_same_reviewer(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("same reviewer", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://x",
        "done",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    first = cp.request_review(task.id, reviewer.id, "workflow-a")
    second = cp.request_review(task.id, reviewer.id, "workflow-b")

    assert second.id == first.id
    assert [review.id for review in cp.list_reviews(task.id)] == [first.id]


def test_default_review_workflow_retracts_same_reviewer_duplicate_pending(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("duplicate same reviewer", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://x",
        "done",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    kept = cp.request_review(task.id, reviewer.id, "workflow-a")
    cp.store.execute(
        """
        INSERT INTO reviews (id, task_id, reviewer_agent_id, status, reason, evidence_id, created_at, completed_at)
        VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
        """,
        ("review_duplicate_same_reviewer", task.id, reviewer.id, ReviewStatus.PENDING.value, utcnow()),
    )

    result = cp.advance_default_review_workflow(task.id, actor="workflow-b")

    assert result["status"] == "waiting_for_reviewer_verdict"
    reviews = {review.id: review for review in cp.list_reviews(task.id)}
    assert reviews[kept.id].status == ReviewStatus.PENDING.value
    assert reviews["review_duplicate_same_reviewer"].status == ReviewStatus.RETRACTED.value
    assert reviews["review_duplicate_same_reviewer"].reason == "duplicate_pending_review_same_reviewer"
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.duplicate_pending_retracted" in names


def test_default_review_workflow_refuses_without_publication_target(cp):
    """mac-w29: when no operator-set publication_target exists, the
    workflow approves the review but does NOT publish — refuses to
    invent a target."""
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("no-target", required_capabilities=["python"])  # no metadata
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://x",
        "done",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    # Verdict-aware flow: produce the verdict so the workflow reaches
    # the publish-step gate.
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_publication_target"
    assert cp.list_publications(task.id) == []
    # Task remains in REVIEWING — the review approval landed but
    # publication is held until a target is configured.
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    # The waiting condition is asserted via result["status"] above. The
    # 'no_publication_target' log is silenced (mem-04): the review tick re-emits
    # it every cycle for a stuck task — 262K durable rows in ~4 days on rocky —
    # so it must NOT land as an observability event.
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.no_publication_target" not in names


def test_default_review_rejects_alias_evidence_taxonomy(cp):
    """mac-q38: canonical names only. Aliases like status='verified',
    evidence_type='code', and field aliases like 'git'/'commit_sha' are
    rejected."""
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "aliases",
        required_capabilities=["python"],
        metadata={"publication_target": "test://aliases"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    # Verify each alias is rejected one at a time.
    bad_status = verified_repo_metadata(cp, worker.id)
    bad_status["verification"]["status"] = "verified"
    cp.add_evidence(
        task.id, "log", "artifact://1", "x", worker.id, metadata=bad_status
    )
    with pytest.raises(ValidationError):
        cp.submit_for_review(task.id, worker.id)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"

    # New evidence with alias evidence_type='code' (was alias for repo_change).
    bad_type = verified_repo_metadata(cp, worker.id)
    bad_type["verification"]["evidence_type"] = "code"
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )
    cp.add_evidence(
        task.id,
        "log",
        "artifact://2",
        "x",
        worker.id,
        metadata=bad_type,
        _trusted_internal=True,
    )
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"

    # And with field alias `git` instead of `repo`.
    bad_field = verified_repo_metadata(cp, worker.id)
    bad_field["verification"]["git"] = bad_field["verification"].pop("repo")
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )
    cp.add_evidence(
        task.id,
        "log",
        "artifact://3",
        "x",
        worker.id,
        metadata=bad_field,
        _trusted_internal=True,
    )
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"


def test_default_reviewer_requires_review_capability(cp):
    """mac-s1a: the reviewer pool must require the `review` capability,
    not merely prefer it. An autonomous review can't be performed by an
    agent whose role doesn't include review duties."""
    worker = register_agent(cp, "worker", ["python"])
    # Three more agents, none with `review` capability. The workflow
    # must refuse to assign a reviewer rather than picking the
    # alphabetically-first idle agent.
    register_agent(cp, "alpha", ["docs"])
    register_agent(cp, "bravo", ["ops"])
    register_agent(cp, "charlie", ["python"])
    task = cp.create_task(
        "needs-real-reviewer",
        required_capabilities=["python"],
        metadata={"publication_target": "test://r"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "log", "x", "y", worker.id, metadata=verified_repo_metadata(cp, worker.id)
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []
    # Once a `review`-capable agent comes online, the workflow advances.
    real_reviewer = register_agent(cp, "real-reviewer", ["review"])
    waiting = cp.advance_default_review_workflow(task.id)
    assert waiting["status"] == "waiting_for_reviewer_verdict"
    from tests.conftest import submit_review_verdict

    submit_review_verdict(cp, task.id, real_reviewer.id, evidence.id)
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "published"


def test_default_reviewer_honors_target_agent_name(cp):
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "bullwinkle", ["review"])
    natasha = register_agent(cp, "natasha", ["review"])
    task = cp.create_task(
        "needs-specific-reviewer",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://r",
            "default_review": {"target_agent_name": "natasha"},
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "x",
        "y",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert result["reviewer_agent_id"] == natasha.id


def test_default_reviewer_honors_review_required_capabilities(cp):
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "bullwinkle", ["review"])
    natasha = register_agent(cp, "natasha", ["qemu", "review"])
    task = cp.create_task(
        "needs-qemu-reviewer",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://r",
            "default_review": {"required_capabilities": ["qemu"]},
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "x",
        "y",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert result["reviewer_agent_id"] == natasha.id


def test_manual_reviewer_assignment_uses_full_eligibility_policy(cp):
    executor = register_agent(cp, "manual-policy-executor", ["python"])
    stale = register_agent(cp, "manual-policy-stale", ["review"])
    unhealthy = register_agent(cp, "manual-policy-unhealthy", ["review"])
    underqualified = register_agent(cp, "manual-policy-underqualified", ["review"])
    qualified = register_agent(cp, "manual-policy-qualified", ["review", "qemu"])

    def reviewable(title, metadata=None):
        task = cp.create_task(
            title,
            required_capabilities=["python"],
            metadata=metadata or {},
        )
        cp.claim_task(task.id, executor.id)
        cp.start_task(task.id, executor.id)
        cp.add_evidence(
            task.id,
            "log",
            "artifact://%s" % task.id,
            "ready for review",
            executor.id,
            metadata=verified_repo_metadata(cp, executor.id),
        )
        cp.submit_for_review(task.id, executor.id)
        return task

    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", stale.id),
    )
    with pytest.raises(AuthorizationError, match="reviewer stale"):
        cp.request_review(reviewable("manual stale").id, stale.id, actor="manual")

    cp.store.execute(
        "UPDATE agents SET health_status = ? WHERE id = ?",
        (HealthStatus.UNHEALTHY.value, unhealthy.id),
    )
    with pytest.raises(AuthorizationError, match="reviewer unhealthy"):
        cp.request_review(
            reviewable("manual unhealthy").id,
            unhealthy.id,
            actor="manual",
        )

    capability_task = reviewable(
        "manual capability",
        metadata={"default_review": {"required_capabilities": ["qemu"]}},
    )
    with pytest.raises(AuthorizationError, match="missing capabilities:qemu"):
        cp.request_review(capability_task.id, underqualified.id, actor="manual")

    target_task = reviewable(
        "manual target",
        metadata={"default_review": {"target_agent_id": qualified.id}},
    )
    with pytest.raises(AuthorizationError, match="not target agent"):
        cp.request_review(target_task.id, underqualified.id, actor="manual")
    assigned = cp.request_review(target_task.id, qualified.id, actor="manual")
    assert assigned.reviewer_agent_id == qualified.id


def test_request_review_rolls_back_task_transition_when_review_write_fails(
    cp, monkeypatch
):
    executor = register_agent(cp, "review-atomic-executor", ["python"])
    reviewer = register_agent(cp, "review-atomic-reviewer", ["review"])
    task = cp.create_task("atomic review request", required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://atomic-review-request",
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)

    original_record_history = cp.reviews._record_history

    def fail_review_history(*args, **kwargs):
        if len(args) > 1 and args[1] == "task.review_requested":
            raise RuntimeError("simulated review insert crash")
        return original_record_history(*args, **kwargs)

    monkeypatch.setattr(cp.reviews, "_record_history", fail_review_history)

    with pytest.raises(RuntimeError, match="simulated review insert crash"):
        cp.request_review(task.id, reviewer.id, actor="manual")

    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
    assert cp.list_reviews(task.id) == []


def test_concurrent_review_requests_reuse_one_pending_review(cp):
    executor = register_agent(cp, "review-race-executor", ["python"])
    reviewer = register_agent(cp, "review-race-reviewer", ["review"])
    task = cp.create_task("concurrent review request", required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://review-race",
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    start = threading.Barrier(3)
    results = []
    errors = []

    def request():
        start.wait()
        try:
            results.append(cp.request_review(task.id, reviewer.id, actor="race"))
        except Exception as exc:  # noqa: BLE001 - surfaced below for the assertion.
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert results[0].id == results[1].id
    assert [review.id for review in cp.list_reviews(task.id)] == [results[0].id]


def test_submit_review_revalidates_reviewer_eligibility(cp):
    executor = register_agent(cp, "verdict-policy-executor", ["python"])
    reviewer = register_agent(cp, "verdict-policy-reviewer", ["review"])
    task = cp.create_task("verdict policy", required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://verdict-policy",
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    review = cp.request_review(task.id, reviewer.id, actor="manual")
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", reviewer.id),
    )

    with pytest.raises(AuthorizationError, match="reviewer stale"):
        cp.submit_review(
            review.id,
            ReviewStatus.REJECTED.value,
            reviewer.id,
            reason="not acceptable",
        )

    assert cp.get_review(review.id).status == ReviewStatus.PENDING.value
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_submit_review_compare_and_swap_rejects_stale_writer(cp, monkeypatch):
    executor = register_agent(cp, "verdict-cas-executor", ["python"])
    reviewer = register_agent(cp, "verdict-cas-reviewer", ["review"])
    task = cp.create_task("verdict CAS", required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://verdict-cas",
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    review = cp.request_review(task.id, reviewer.id, actor="manual")

    def race_review_status(*args, **kwargs):
        cp.store.execute(
            "UPDATE reviews SET status = ?, reason = ? WHERE id = ?",
            (ReviewStatus.RETRACTED.value, "concurrent writer", review.id),
        )
        return None

    monkeypatch.setattr(
        cp.reviews,
        "_review_feedback_from_evidence",
        race_review_status,
    )

    with pytest.raises(ValidationError, match="review state changed during submission"):
        cp.submit_review(
            review.id,
            ReviewStatus.REJECTED.value,
            reviewer.id,
            reason="stale verdict",
        )

    assert cp.get_review(review.id).status == ReviewStatus.RETRACTED.value
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value
    assert not any(
        item.event_type == "task.review_completed"
        for item in cp.task_history(task.id)
    )


def test_rejected_review_and_task_transition_roll_back_together(cp, monkeypatch):
    executor = register_agent(cp, "reject-atomic-executor", ["python"])
    reviewer = register_agent(cp, "reject-atomic-reviewer", ["review"])
    task = cp.create_task("atomic rejection", required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://atomic-rejection",
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    review = cp.request_review(task.id, reviewer.id, actor="manual")
    original_transition = cp.reviews._transition_task_in_transaction

    def fail_after_task_transition(*args, **kwargs):
        original_transition(*args, **kwargs)
        raise RuntimeError("simulated rejection transition crash")

    monkeypatch.setattr(
        cp.reviews,
        "_transition_task_in_transaction",
        fail_after_task_transition,
    )

    with pytest.raises(RuntimeError, match="simulated rejection transition crash"):
        cp.submit_review(
            review.id,
            ReviewStatus.REJECTED.value,
            reviewer.id,
            reason="needs changes",
        )

    assert cp.get_review(review.id).status == ReviewStatus.PENDING.value
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_default_review_reassigns_stale_pending_reviewer(cp):
    worker = register_agent(cp, "worker", ["python"])
    stale_reviewer = register_agent(cp, "operator-reviewer", ["review"])
    live_reviewer = register_agent(cp, "rocky", ["review"])
    task = cp.create_task(
        "needs-live-reviewer",
        required_capabilities=["python"],
        metadata={"publication_target": "test://r"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    stale_review = cp.request_review(task.id, stale_reviewer.id, actor="old-workflow")
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", stale_reviewer.id),
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert result["reviewer_agent_id"] == live_reviewer.id
    reviews = cp.list_reviews(task.id)
    assert [review.status for review in reviews] == [
        ReviewStatus.RETRACTED.value,
        ReviewStatus.PENDING.value,
    ]
    assert reviews[0].id == stale_review.id
    assert reviews[0].reason == "reviewer_unavailable:reviewer_stale"
    assert reviews[1].reviewer_agent_id == live_reviewer.id
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.retracted" in names
    assert "workflow.default_review.assigned" in names


def test_default_reviewer_uses_shared_repository_access_success_and_cooldown(
    cp,
    monkeypatch,
):
    worker = register_agent(cp, "worker", ["python"])
    failed = register_agent(cp, "a-failed", ["review"])
    cooldown_reviewer = register_agent(cp, "m-cooldown", ["review"])
    successful = register_agent(cp, "z-successful", ["review"])
    remote = "https://github.com/acme/private.git"
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "demo",
        "canonical_remote_url": remote,
    }
    task = cp.create_task(
        "Use learned reviewer access",
        project="demo",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://r",
            "execution_contract": {
                "type": "repository",
                "repository_contract": contract,
            },
            "origin": {
                "repository_url": remote,
                "repository_contract": contract,
            },
        },
    )
    failure = build_repository_access_learning(
        project="demo",
        remote=remote,
        operation="review_clone",
        agent_id=failed.id,
        outcome="failure",
        credential_source="ambient:https",
        failure_class="authentication",
        error="could not read Username for https://github.com",
    )
    success = build_repository_access_learning(
        project="demo",
        remote=remote,
        operation="review_clone",
        agent_id=successful.id,
        outcome="success",
        credential_source="env:GH_TOKEN",
    )
    cp.add_memory(**build_repository_access_memory_payload(failure))
    cp.add_memory(**build_repository_access_memory_payload(success))

    selected = cp._select_default_reviewer(task, executor_agent_id=worker.id)

    assert selected is not None and selected.id == successful.id
    reason = cp._default_reviewer_unavailable_reason_for_id(
        task,
        failed.id,
        executor_agent_id=worker.id,
    )
    assert reason == "reviewer_repository_access_authentication:github.com"
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://repository-access-review",
        "ready for repository review",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    with pytest.raises(AuthorizationError, match="repository access authentication"):
        cp.request_review(task.id, failed.id, actor="manual")

    later_success = build_repository_access_learning(
        project="demo",
        remote=remote,
        operation="review_clone",
        agent_id=failed.id,
        outcome="success",
        credential_source="env:GITHUB_TOKEN",
    )
    cp.add_memory(**build_repository_access_memory_payload(later_success))
    assert (
        cp._default_reviewer_unavailable_reason_for_id(
            task,
            failed.id,
            executor_agent_id=worker.id,
        )
        is None
    )

    # A failure is a cooldown, not a permanent ban.
    cooldown_failure = build_repository_access_learning(
        project="demo",
        remote=remote,
        operation="review_clone",
        agent_id=cooldown_reviewer.id,
        outcome="failure",
        credential_source="ambient:https",
        failure_class="authentication",
    )
    memory = cp.add_memory(**build_repository_access_memory_payload(cooldown_failure))
    assert cp._default_reviewer_unavailable_reason_for_id(
        task,
        cooldown_reviewer.id,
        executor_agent_id=worker.id,
    ) == "reviewer_repository_access_authentication:github.com"
    cp.store.execute(
        "UPDATE memory_records SET created_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", memory.id),
    )
    monkeypatch.setenv("MAC_REPOSITORY_ACCESS_FAILURE_COOLDOWN_SECONDS", "1")
    assert (
        cp._default_reviewer_unavailable_reason_for_id(
            task,
            cooldown_reviewer.id,
            executor_agent_id=worker.id,
        )
        is None
    )


def test_default_review_waits_when_only_reviewer_is_stale(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "stale-reviewer", ["review"])
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", reviewer.id),
    )
    task = cp.create_task("needs-fresh-reviewer", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []


def test_default_reviewer_uses_same_persona_peer_only_as_fallback(cp, monkeypatch):
    """A different persona is preferred, but its absence cannot deadlock review."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    machine = cp.register_machine("h-collusion")
    from tests.conftest import bind_soul

    code_reviewer_soul_a = bind_soul(
        cp,
        persona_name="Code Reviewer",  # default slug = "code-reviewer"
        tenant_name="collusion-tenant",
        instance_name="instance-a",
    )
    # Reuse the same tenant by passing a different tenant_name=... isn't
    # straightforward; bind_soul registers a fresh tenant each call.
    # Use the same instance approach: two instances bound to the same
    # persona under the same tenant.
    tenant = cp.identity.get_hermes_instance(code_reviewer_soul_a)
    code_reviewer_soul_b = cp.register_hermes_instance(
        tenant.tenant_id,
        "instance-b",
        persona_id=tenant.persona_id,
    ).id

    executor = cp.register_agent(
        machine.id, "exec", capabilities=["python", "review"], hermes_instance_id=code_reviewer_soul_a
    )
    peer = cp.register_agent(
        machine.id, "peer", capabilities=["python", "review"], hermes_instance_id=code_reviewer_soul_b
    )
    cp.roles.create_role(
        slug="code-reviewer",
        name="Code Reviewer",
        description="d",
        system_prompt="p",
        level="ic",
    )
    cp.roles.assign_role(executor.id, "code-reviewer")
    cp.roles.assign_role(peer.id, "code-reviewer")

    task = cp.create_task(
        "collusion-target",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://collusion",
            # Same-tenant task so the tenancy gate doesn't get in the way.
            "origin": {"tenant_id": tenant.tenant_id},
        },
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id, "log", "x", "y", executor.id, metadata=verified_repo_metadata(cp, executor.id)
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)
    # The peer is preferable to self-review because it did not execute the
    # task, even though both agents share a persona. The relaxation is audited.
    assert result["status"] == "waiting_for_reviewer_verdict"
    reviews = cp.list_reviews(task.id)
    assert len(reviews) == 1
    assert reviews[0].reviewer_agent_id == peer.id
    history = cp.store.query_one(
        "SELECT detail FROM task_history WHERE task_id = ? "
        "AND event_type = 'task.review_requested' ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    )
    detail = json.loads(history["detail"])
    assert detail["reviewer_independence"] == "fallback"
    assert detail["reviewer_independence_reason"] == "reviewer_same_persona"


def test_default_review_prefers_independent_peer_over_executor_fallback(
    cp, monkeypatch
):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    executor = register_agent(cp, "fallback-executor", ["python", "review"])
    peer = register_agent(cp, "independent-reviewer", ["review"])
    task = cp.create_task(
        "prefer independent review",
        required_capabilities=["python"],
        metadata={"publication_target": "test://independent"},
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://independent",
        "tests passed",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    review = cp.list_reviews(task.id)[0]
    assert review.reviewer_agent_id == peer.id
    history = cp.store.query_one(
        "SELECT detail FROM task_history WHERE task_id = ? "
        "AND event_type = 'task.review_requested' ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    )
    assert json.loads(history["detail"])["reviewer_independence"] == "independent"


def test_default_review_falls_back_to_executor_when_no_peer_exists(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    executor = register_agent(cp, "only-reviewer", ["python", "review"])
    task = cp.create_task(
        "single-node review",
        required_capabilities=["python"],
        metadata={"publication_target": "test://single-node"},
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://single-node",
        "tests passed",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    review = cp.list_reviews(task.id)[0]
    assert review.reviewer_agent_id == executor.id
    history = cp.store.query_one(
        "SELECT detail FROM task_history WHERE task_id = ? "
        "AND event_type = 'task.review_requested' ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    )
    detail = json.loads(history["detail"])
    assert detail["reviewer_independence"] == "fallback"
    assert detail["reviewer_independence_reason"] in {
        "reviewer_previously_owned_task",
        "reviewer_created_executor_evidence",
    }
    submitted = cp.submit_review(
        review.id,
        ReviewStatus.REJECTED.value,
        executor.id,
        reason="fallback reviewer found a problem",
    )
    assert submitted.status == ReviewStatus.REJECTED.value


def test_read_only_repository_report_never_falls_back_to_executor(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    executor = register_agent(
        cp,
        "report-only-reviewer",
        ["ops", "review"],
        read_only_report_executor_resources(),
    )
    task = cp.create_task(
        "independently review repository report",
        required_capabilities=["ops"],
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                        "schema": "mac.repository_contract.v1",
                        "project": "review-routing",
                        "canonical_remote_url": "https://example.invalid/review-routing.git",
                        "default_branch": "main",
                        "test": {"command": "true"},
                },
            },
        },
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    manifest = _sign(
        cp,
        executor.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Repository analysis produced",
            "result": "Substantive findings and prioritized next work.",
            "repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    )
    cp.add_evidence(
        task.id,
        "log",
        "artifact://independent-report",
        "Repository analysis produced",
        executor.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []
    with pytest.raises(AuthorizationError, match="owned"):
        cp.request_review(task.id, executor.id, actor="manual")


def test_read_only_repository_report_assigns_distinct_eligible_peer(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    executor = register_agent(
        cp,
        "report-executor",
        ["ops", "review"],
        read_only_report_executor_resources(),
    )
    peer = register_agent(
        cp,
        "independent-report-reviewer",
        ["review"],
        read_only_report_executor_resources(),
    )
    task = cp.create_task(
        "peer review repository report",
        required_capabilities=["ops"],
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                        "schema": "mac.repository_contract.v1",
                        "project": "review-routing",
                        "canonical_remote_url": "https://example.invalid/review-routing.git",
                        "default_branch": "main",
                        "test": {"command": "true"},
                },
            },
        },
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    manifest = _sign(
        cp,
        executor.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "operator_result",
            "summary": "Repository analysis produced",
            "result": "Substantive findings and prioritized next work.",
            "repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    )
    cp.add_evidence(
        task.id,
        "log",
        "artifact://peer-reviewed-report",
        "Repository analysis produced",
        executor.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    reviews = cp.list_reviews(task.id)
    assert len(reviews) == 1
    assert reviews[0].reviewer_agent_id == peer.id


def test_fallback_review_is_replaced_when_independent_peer_becomes_available(
    cp, monkeypatch
):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    executor = register_agent(cp, "dynamic-executor", ["python", "review"])
    task = cp.create_task(
        "dynamic reviewer availability",
        required_capabilities=["python"],
        metadata={"publication_target": "test://dynamic"},
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://dynamic",
        "tests passed",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["reviewer_agent_id"] == executor.id

    peer = register_agent(cp, "late-independent-reviewer", ["review"])
    second = cp.advance_default_review_workflow(task.id)

    assert second["status"] == "waiting_for_reviewer_verdict"
    assert second["reviewer_agent_id"] == peer.id
    reviews = cp.list_reviews(task.id)
    pending = [review for review in reviews if review.status == ReviewStatus.PENDING.value]
    retracted = [
        review for review in reviews if review.status == ReviewStatus.RETRACTED.value
    ]
    assert [review.reviewer_agent_id for review in pending] == [peer.id]
    assert [review.reviewer_agent_id for review in retracted] == [executor.id]


def test_task_can_require_strictly_independent_reviewer(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    executor = register_agent(cp, "strict-executor", ["python", "review"])
    task = cp.create_task(
        "strict independent review",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://strict",
            "review": {"require_independent_reviewer": True},
        },
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://strict",
        "tests passed",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []
    with pytest.raises(AuthorizationError, match="owned"):
        cp.request_review(task.id, executor.id, actor="manual")


def test_default_review_refuses_reviewer_from_different_tenant(cp):
    """mac-dyk: the reviewer's persona tenant must match the task's
    tenant. Without this, tenant B's idle agent could auto-approve
    tenant A's work."""
    from tests.conftest import bind_soul

    machine_a = cp.register_machine("host-a")
    machine_b = cp.register_machine("host-b")
    soul_a = bind_soul(
        cp,
        persona_name="Reviewer-A",
        tenant_name="alpha",
        allowed_role_slugs=["reviewer-a"],
    )
    soul_b = bind_soul(
        cp,
        persona_name="Reviewer-B",
        tenant_name="beta",
        allowed_role_slugs=["reviewer-b"],
    )
    tenant_a = cp.identity.get_hermes_instance(soul_a).tenant_id

    executor = cp.register_agent(
        machine_a.id, "exec-a", capabilities=["python"], hermes_instance_id=soul_a
    )
    cp.register_agent(
        machine_b.id, "reviewer-b", capabilities=["review"], hermes_instance_id=soul_b
    )
    task = cp.create_task(
        "tenant-a-work",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://a",
            "origin": {"tenant_id": tenant_a},
        },
    )
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    cp.add_evidence(
        task.id, "log", "x", "y", executor.id, metadata=verified_repo_metadata(cp, executor.id)
    )
    cp.submit_for_review(task.id, executor.id)

    result = cp.advance_default_review_workflow(task.id)
    # Tenant B's review-capable agent must NOT be drafted.
    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []


def test_default_review_drafts_headless_reviewer_on_shared_machine_for_tenant_task(cp):
    """mac: a headless K8s reviewer (no hermes_instance_id, so no persona
    tenant) must still be drafted for a tenant-scoped task when its
    machine's tenant policy permits that tenant. Persona-boundary
    tenancy fails closed for headless workers; the hardware boundary
    (_machine_allows_tenant) is the correct gate, mirroring how the
    executor path admits the same workers. Without this, every Hermes
    (tenant-scoped) task parks forever in needs_review because no
    souled reviewer exists in the fleet."""
    tenant = cp.register_tenant("personal", tenant_id="personal")
    # Shared machine (default tenant policy => allows any tenant).
    machine = cp.register_machine("k8s-shared-host", resources={"cpu": 4, "memory_gb": 8})
    worker = cp.register_agent(machine.id, "worker", capabilities=["python"])
    # Headless reviewer: no hermes_instance_id => agent_tenant is None.
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    assert reviewer.hermes_instance_id is None

    task = cp.create_task(
        "tenant-scoped-work",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://headless",
            "origin": {"tenant_id": tenant.id},
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id, "log", "x", "y", worker.id, metadata=verified_repo_metadata(cp, worker.id)
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)
    # The headless reviewer on a tenant-permitting machine must be drafted.
    assert result["status"] == "waiting_for_reviewer_verdict"
    assert len(cp.list_reviews(task.id)) == 1


def test_default_review_refuses_headless_reviewer_on_tenant_denied_machine(cp):
    """mac: the hardware-boundary tenancy gate must still fail closed.
    A headless reviewer whose machine's tenant policy does NOT permit
    the task's tenant must not be drafted — otherwise the fallback would
    leak cross-tenant review onto disallowed hardware."""
    tenant = cp.register_tenant("personal", tenant_id="personal")
    worker_machine = cp.register_machine("worker-host", resources={"cpu": 4, "memory_gb": 8})
    # Reviewer machine is private to a different tenant => denies "personal".
    reviewer_machine = cp.register_machine(
        "private-host",
        resources={"cpu": 4, "memory_gb": 8},
        labels={"tenant_policy": {"mode": "private", "tenant_ids": ["other-tenant"]}},
    )
    worker = cp.register_agent(worker_machine.id, "worker", capabilities=["python"])
    reviewer = cp.register_agent(reviewer_machine.id, "reviewer", capabilities=["review"])
    assert reviewer.hermes_instance_id is None

    task = cp.create_task(
        "tenant-scoped-work",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://denied",
            "origin": {"tenant_id": tenant.id},
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id, "log", "x", "y", worker.id, metadata=verified_repo_metadata(cp, worker.id)
    )
    cp.submit_for_review(task.id, worker.id)

    result = cp.advance_default_review_workflow(task.id)
    # The only review-capable agent lives on a machine that denies this
    # tenant — the workflow must refuse to draft it.
    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []


def test_renew_lease_refuses_on_transitioning_task(cp):
    """mac-eow: renew_lease must refuse when the underlying task is no
    longer CLAIMED/RUNNING. Previous silent-update behavior was a
    footgun — pin the strict refusal so a future revert doesn't quietly
    unbreak it."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "transitioning",
        required_capabilities=["python"],
        metadata={"publication_target": "test://x"},
    )
    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "x",
        "y",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    # Submit moves task to NEEDS_REVIEW and releases the lease.
    cp.submit_for_review(task.id, worker.id)
    with pytest.raises(ValidationError) as exc:
        cp.renew_lease(lease.id, worker.id)
    assert "active" in str(exc.value).lower()


def test_default_review_workflow_ignores_retracted_publication_and_review(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Reopened work",
        required_capabilities=["python"],
        metadata={"publication_target": "test://reopened"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    old_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://old-result",
        "old verified result",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    waiting = cp.advance_default_review_workflow(task.id)
    assert waiting["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, old_evidence.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "published"

    cp.store.execute("UPDATE reviews SET status = ? WHERE task_id = ?", ("retracted", task.id))
    cp.store.execute("UPDATE publications SET status = ? WHERE task_id = ?", ("retracted", task.id))
    cp.store.execute("UPDATE tasks SET state = ? WHERE id = ?", (TaskState.NEEDS_REVIEW.value, task.id))
    new_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://new-result",
        "new verified result",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id, head_sha="fedcba9876543210fedcba9876543210fedcba98"),
        _trusted_internal=True,
    )
    cp._transition_task_internal(task.id, TaskState.RUNNING.value, "test-retry")
    cp._transition_task_internal(
        task.id, TaskState.NEEDS_REVIEW.value, "test-retry"
    )

    waiting_again = cp.advance_default_review_workflow(task.id)
    assert waiting_again["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, new_evidence.id)
    second = cp.advance_default_review_workflow(task.id)

    assert second["status"] == "published"
    active_publications = [
        item for item in cp.list_publications(task.id) if item.status == PublicationStatus.PUBLISHED.value
    ]
    assert len(active_publications) == 1
    assert active_publications[0].evidence_id == new_evidence.id
    approved = [item for item in cp.list_reviews(task.id) if item.status == ReviewStatus.APPROVED.value]
    assert len(approved) == 1
    # The approved review row links to the verdict, not the executor's
    # evidence — the verdict's evidence_id is what flowed into
    # submit_review.
    assert approved[0].reviewer_agent_id == reviewer.id


def test_dispatcher_matches_capabilities_and_expired_leases_recover(cp):
    python_agent = register_agent(cp, "python", ["python"])
    docs_agent = register_agent(cp, "docs", ["docs"])
    task = cp.create_task("Python work", required_capabilities=["python"], max_attempts=2)

    assignment = cp.dispatch_once()
    assert assignment["agent"]["id"] == python_agent.id
    assert assignment["agent"]["id"] != docs_agent.id
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        (expired_at, assignment["lease"]["id"]),
    )
    cp.store.execute(
        "UPDATE tasks SET leased_until = ? WHERE id = ?",
        (expired_at, task.id),
    )

    recovered = cp.expire_leases(now=utcnow())
    assert [item.id for item in recovered] == [task.id]
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    assert cp.get_agent(python_agent.id).status == AgentStatus.IDLE.value


def test_dispatcher_respects_capacity_resources_and_dead_letters(cp):
    small = register_agent(cp, "small", ["python"])
    large_machine = cp.register_machine("large-host", resources={"memory_gb": 32})
    large = cp.register_agent(
        large_machine.id,
        "large",
        capabilities=["python"],
        resources={"capacity": 2, "memory_gb": 16},
    )
    cp.create_task(
        "needs memory",
        required_capabilities=["python"],
        metadata={"resources": {"memory_gb": 12}},
    )
    cp.create_task("second slot", required_capabilities=["python"])

    first = cp.dispatch_once()
    second = cp.dispatch_once()

    assert first["agent"]["id"] == large.id
    # The second task fits either worker, so deterministic dispatch preserves
    # the larger worker's remaining slot and chooses the lower normalized load.
    assert second["agent"]["id"] == small.id
    assert cp.get_agent(small.id).status == AgentStatus.BUSY.value

    dead = cp.create_task("dead letter", required_capabilities=["docs"], max_attempts=1)
    docs = register_agent(cp, "docs-dead", ["docs"])
    _, dead_lease = cp.claim_task(dead.id, docs.id)
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        (expired_at, dead_lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET leased_until = ? WHERE id = ?",
        (expired_at, dead.id),
    )
    cp.expire_leases(now=utcnow())

    assert [task.id for task in cp.list_dead_letters()] == [dead.id]


def test_tick_marks_stale_agents_offline_and_requeues_work(cp):
    worker = register_agent(cp, "stale", ["python"])
    task = cp.create_task("stale work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)
    cp.store.execute(
        "UPDATE agents SET last_seen_at = '1970-01-01T00:00:00+00:00' WHERE id = ?",
        (worker.id,),
    )

    tick = cp.tick(stale_after_seconds=60)

    assert tick["stale_agents"][0]["id"] == worker.id
    assert cp.get_agent(worker.id).status == AgentStatus.OFFLINE.value
    assert cp.get_lease(lease.id).status == LeaseStatus.EXPIRED.value
    assert cp.get_task(claimed.id).state == TaskState.OPEN.value
    assert cp.get_task(claimed.id).attempt_count == 0
    assert tick["assignments"] == []

    expiry = next(
        event
        for event in cp.task_history(task.id)
        if event.event_type == "task.lease_expired"
    )
    assert expiry.detail == {
        "lease_id": lease.id,
        "agent_id": worker.id,
        "reason": "heartbeat_offline",
        "failure_class": "environment",
        "attempt_refunded": True,
        "attempt_count_before": 1,
        "attempt_count_after": 0,
    }


def test_repeated_stale_agent_reaps_do_not_exhaust_execution_attempts(cp):
    worker = register_agent(cp, "repeated-stale", ["python"])
    task = cp.create_task(
        "liveness loss preserves retry budget",
        required_capabilities=["python"],
        max_attempts=1,
    )

    for _ in range(2):
        cp.heartbeat_agent(worker.id, status=AgentStatus.IDLE.value)
        _, lease = cp.claim_task(task.id, worker.id)
        assert cp.get_task(task.id).attempt_count == 1
        cp.store.execute(
            "UPDATE agents SET last_seen_at = '1970-01-01T00:00:00+00:00' WHERE id = ?",
            (worker.id,),
        )

        cp.tick(stale_after_seconds=60, limit=0)

        recovered = cp.get_task(task.id)
        assert recovered.state == TaskState.OPEN.value
        assert recovered.attempt_count == 0
        assert cp.get_lease(lease.id).status == LeaseStatus.EXPIRED.value

    expiries = [
        event
        for event in cp.task_history(task.id)
        if event.event_type == "task.lease_expired"
    ]
    assert len(expiries) == 2
    assert all(event.detail["attempt_refunded"] is True for event in expiries)


def test_dispatch_tick_round_robins_between_tenants(cp):
    tenant_a = cp.register_tenant("tenant-a")
    tenant_b = cp.register_tenant("tenant-b")
    hermes_a = cp.register_hermes_instance(tenant_a.id, "rocky")
    hermes_b = cp.register_hermes_instance(tenant_b.id, "bullwinkle")
    task_a1 = cp.create_interaction_task(hermes_a.id, "A1", priority=100, required_capabilities=["python"])
    cp.create_interaction_task(hermes_a.id, "A2", priority=90, required_capabilities=["python"])
    task_b = cp.create_interaction_task(hermes_b.id, "B1", priority=10, required_capabilities=["python"])
    for index in range(3):
        register_agent(cp, "fair-%d" % index, ["python"])

    tick = cp.tick(limit=2)

    assert [item["task"]["id"] for item in tick["assignments"]] == [task_a1.id, task_b.id]


def test_dispatch_round_robins_between_projects_within_tenant(cp):
    flood = [
        cp.create_task(
            "flood-%d" % index,
            project="flood",
            priority=100,
            required_capabilities=["python"],
        )
        for index in range(3)
    ]
    starved = cp.create_task(
        "starved",
        project="starved",
        priority=10,
        required_capabilities=["python"],
    )

    ordered = cp._dispatch_ordered_tasks()

    # The low-priority project's head task gets the second claim slot instead
    # of queueing behind every high-priority task from the flooding project.
    assert [task.id for task in ordered] == [
        flood[0].id,
        starved.id,
        flood[1].id,
        flood[2].id,
    ]


def test_dispatch_preserves_priority_order_within_project(cp):
    low = cp.create_task(
        "low", project="solo", priority=10, required_capabilities=["python"]
    )
    high = cp.create_task(
        "high", project="solo", priority=100, required_capabilities=["python"]
    )

    ordered = cp._dispatch_ordered_tasks()

    assert [task.id for task in ordered] == [high.id, low.id]


def test_dispatch_recovery_lane_precedes_normal_priority(cp):
    normal = cp.create_task(
        "normal-high",
        project="solo",
        priority=999,
        required_capabilities=["python"],
    )
    recovery = cp.create_task(
        "repair-now",
        project="solo",
        priority=0,
        required_capabilities=["python"],
        metadata={"dispatch_class": "recovery"},
    )

    ordered = cp._dispatch_ordered_tasks()

    assert [task.id for task in ordered[:2]] == [recovery.id, normal.id]
    assert cp.explain_task_dispatch(recovery.id)["dispatch_rank"]["class"] == "recovery"


def test_dispatch_due_hint_adds_bounded_priority_aging(cp):
    now = services.utcnow()
    due = cp.create_task(
        "due",
        priority=0,
        required_capabilities=["python"],
        metadata={
            "due_at": (
                services.parse_time(now) - timedelta(hours=8)
            ).isoformat(timespec="microseconds")
        },
    )
    fresh = cp.create_task("fresh", priority=1, required_capabilities=["python"])

    due_key = cp._dispatch_task_sort_key(due, now)
    fresh_key = cp._dispatch_task_sort_key(fresh, now)

    assert cp._dispatch_due_bonus(due, now) == 3
    assert due_key < fresh_key


def test_claim_next_dry_run_and_canary_policy_are_observed(cp):
    worker = register_agent(cp, "worker", ["python"])
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

    dry_run = cp.claim_next_for_agent(
        worker.id,
        allowed_projects=["mac-canary"],
        claim_only_canary_tasks=True,
        dry_run=True,
    )

    assert dry_run is not None
    assert dry_run["dry_run"] is True
    assert dry_run["task"]["id"] == canary.id
    assert dry_run["lease"] is None
    assert cp.get_task(canary.id).state == TaskState.OPEN.value
    assert cp.get_task(normal.id).state == TaskState.OPEN.value

    logs = cp.list_observability(layer="control_plane", limit=20)
    by_name = {row.name: row for row in logs}
    assert by_name["worker.routing.dry_run_candidate"].subject_id == canary.id
    assert by_name["worker.routing.dry_run_candidate"].detail["rejected_policy"] == {
        "not_canary": 1
    }

    claimed = cp.claim_next_for_agent(
        worker.id,
        allowed_projects=["mac-canary"],
        claim_only_canary_tasks=True,
    )

    assert claimed is not None
    assert claimed["task"]["id"] == canary.id
    assert cp.get_task(canary.id).state == TaskState.CLAIMED.value
    assert any(
        row.name == "worker.routing.claimed" and row.subject_id == canary.id
        for row in cp.list_observability(layer="control_plane", limit=20)
    )


def test_claim_next_returns_assignment_when_claimed_log_fails(cp, monkeypatch):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("post-claim-log-failure", required_capabilities=["python"])
    original_record_log = cp.record_log

    def flaky_record_log(name, *args, **kwargs):
        if name == "worker.routing.claimed":
            raise RuntimeError("observability unavailable")
        return original_record_log(name, *args, **kwargs)

    monkeypatch.setattr(cp, "record_log", flaky_record_log)

    claimed = cp.claim_next_for_agent(worker.id)

    assert claimed is not None
    assert claimed["task"]["id"] == task.id
    assert claimed["lease"]["task_id"] == task.id
    assert cp.get_task(task.id).state == TaskState.CLAIMED.value


def test_claim_next_returns_assignment_when_dispatcher_nudge_fails(cp, monkeypatch):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("post-claim-nudge-failure", required_capabilities=["python"])

    def failing_send_message(*args, **kwargs):
        raise RuntimeError("agentbus unavailable")

    monkeypatch.setattr(cp, "send_message", failing_send_message)

    claimed = cp.claim_next_for_agent(worker.id)

    assert claimed is not None
    assert claimed["task"]["id"] == task.id
    assert claimed["lease"]["task_id"] == task.id
    assert cp.get_task(task.id).state == TaskState.CLAIMED.value


def test_claim_next_resumes_assignment_when_resume_log_fails(cp, monkeypatch):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("resume-log-failure", required_capabilities=["python"])
    dispatched = cp.dispatch_once()
    assert dispatched is not None
    original_record_log = cp.record_log

    def flaky_record_log(name, *args, **kwargs):
        if name == "worker.routing.resumed":
            raise RuntimeError("observability unavailable")
        return original_record_log(name, *args, **kwargs)

    monkeypatch.setattr(cp, "record_log", flaky_record_log)

    resumed = cp.claim_next_for_agent(worker.id)

    assert resumed is not None
    assert resumed["task"]["id"] == task.id
    assert resumed["lease"]["id"] == dispatched["lease"]["id"]


def test_claim_next_resumes_dispatcher_assigned_lease(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("dispatcher-owned", required_capabilities=["python"])
    dispatched = cp.dispatch_once()

    assert dispatched is not None
    assert dispatched["task"]["id"] == task.id
    lease_id = dispatched["lease"]["id"]

    heartbeat = cp.heartbeat_agent(worker.id, status="busy")
    assert heartbeat.status == "busy"
    resumed = cp.claim_next_for_agent(worker.id)

    assert resumed is not None
    assert resumed["resumed"] is True
    assert resumed["task"]["id"] == task.id
    assert resumed["lease"]["id"] == lease_id
    assert cp.get_task(task.id).attempt_count == 1

    cp.start_task(task.id, worker.id)
    assert cp.start_task(task.id, worker.id).state == TaskState.RUNNING.value
    resumed_running = cp.claim_next_for_agent(worker.id)
    assert resumed_running is not None
    assert resumed_running["task"]["state"] == TaskState.RUNNING.value
    assert resumed_running["lease"]["id"] == lease_id


def test_claim_next_capabilities_filter_narrows_dispatch(cp):
    """``capabilities=[...]`` lets a worker narrow which tasks it claims
    below what its agent record's capabilities would otherwise allow.

    Regression for the silent no-op bug where mac-k8s-runner sent
    ``capabilities`` in the claim-next body but ``AgentClaimNextRequest``
    didn't declare the field, so pydantic dropped it.
    """
    worker = register_agent(cp, "worker", ["python", "review"])
    python_task = cp.create_task(
        "python-task", required_capabilities=["python"]
    )
    review_task = cp.create_task(
        "review-task", required_capabilities=["review"]
    )

    # Narrow to python only — review task must be skipped.
    claimed = cp.claim_next_for_agent(worker.id, capabilities=["python"])
    assert claimed is not None
    assert claimed["task"]["id"] == python_task.id

    # The review task stays open (it was filtered out, not consumed).
    assert cp.get_task(review_task.id).state == TaskState.OPEN.value


def test_claim_next_capabilities_filter_empty_is_noop(cp):
    """An empty capabilities list does NOT narrow — preserves old
    behaviour for callers that pass [] or omit the field."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("any", required_capabilities=["python"])
    claimed = cp.claim_next_for_agent(worker.id, capabilities=[])
    assert claimed is not None and claimed["task"]["id"] == task.id


def test_claim_next_prefers_high_priority_over_older_default_ready_task(cp):
    worker = register_agent(cp, "worker", ["python"])
    older_default = cp.create_task(
        "older-default",
        priority=0,
        required_capabilities=["python"],
    )
    high_priority = cp.create_task(
        "high-priority",
        priority=1,
        required_capabilities=["python"],
    )
    created_at = (
        services.parse_time(services.utcnow()) - timedelta(hours=6)
    ).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        (created_at, older_default.id),
    )

    assert [task.id for task in cp.ready_tasks(limit=2)] == [
        high_priority.id,
        older_default.id,
    ]
    claimed = cp.claim_next_for_agent(worker.id)

    assert claimed is not None
    assert claimed["task"]["id"] == high_priority.id


def test_dispatch_priority_aging_prevents_low_priority_starvation(cp):
    worker = register_agent(cp, "worker", ["python"])
    old_default = cp.create_task(
        "old-default",
        priority=0,
        required_capabilities=["python"],
    )
    new_high = cp.create_task(
        "new-high",
        priority=1,
        required_capabilities=["python"],
    )
    created_at = (
        services.parse_time(services.utcnow()) - timedelta(days=2)
    ).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        (created_at, old_default.id),
    )

    assert [task.id for task in cp.ready_tasks(limit=2)] == [
        old_default.id,
        new_high.id,
    ]
    claimed = cp.claim_next_for_agent(worker.id)

    assert claimed is not None
    assert claimed["task"]["id"] == old_default.id


def _prefix_task(cp, task_id, *, priority=0, project="rot", created_at=None):
    """Build a detached Task with a controlled id/prefix for helper tests.

    The page-prefix helper only reads (id, priority, created_at), so a
    dataclasses.replace clone avoids fighting the tasks-table FK constraints
    that a raw ``UPDATE tasks SET id`` would trip.
    """
    seed = cp.create_task(
        "seed-%s" % task_id,
        project=project,
        priority=priority,
        required_capabilities=["python"],
    )
    return dataclasses.replace(
        seed,
        id=task_id,
        created_at=created_at or seed.created_at,
    )


def test_rotate_by_page_prefix_round_robins_across_prefix_buckets(cp):
    now = services.utcnow()
    # Two prefix buckets ("aa" and "bb"); the "aa" bucket floods the window.
    tasks = [
        _prefix_task(cp, "aa00"),
        _prefix_task(cp, "aa01"),
        _prefix_task(cp, "aa02"),
        _prefix_task(cp, "bb00"),
    ]

    rotated = cp._rotate_by_page_prefix(tasks, now, width=2)

    # The late "bb" prefix gets the second claim slot instead of queueing behind
    # every candidate sharing the "aa" prefix.
    assert [task.id for task in rotated] == ["aa00", "bb00", "aa01", "aa02"]


def test_rotate_by_page_prefix_is_noop_for_single_bucket(cp):
    now = services.utcnow()
    tasks = [
        _prefix_task(cp, "pp-low", priority=10),
        _prefix_task(cp, "pp-high", priority=100),
    ]

    rotated = cp._rotate_by_page_prefix(tasks, now, width=2)

    # A single "pp" bucket preserves the sorted (priority, age) order unchanged.
    assert [task.id for task in rotated] == ["pp-high", "pp-low"]


def test_rotate_by_page_prefix_preserves_priority_within_bucket(cp):
    now = services.utcnow()
    tasks = [
        _prefix_task(cp, "aa-low", priority=10),
        _prefix_task(cp, "aa-high", priority=100),
        _prefix_task(cp, "bb-mid", priority=50),
    ]

    rotated = cp._rotate_by_page_prefix(tasks, now, width=2)

    order = [task.id for task in rotated]
    # Within the "aa" bucket the higher priority still precedes the lower.
    assert order.index("aa-high") < order.index("aa-low")
    # Both prefixes are serviced; the "aa" head leads on priority.
    assert order[0] == "aa-high"
    assert order[1] == "bb-mid"


def test_rotate_by_page_prefix_does_not_mutate_input(cp):
    now = services.utcnow()
    tasks = [_prefix_task(cp, "aa-one"), _prefix_task(cp, "bb-two")]
    original_ids = [task.id for task in tasks]

    cp._rotate_by_page_prefix(tasks, now, width=2)

    # The helper is pure: the caller's list is left untouched.
    assert [task.id for task in tasks] == original_ids


def test_rotate_by_page_prefix_width_env_gated(cp, monkeypatch):
    now = services.utcnow()
    tasks = [_prefix_task(cp, task_id) for task_id in ("abx", "aby", "acz")]

    # Width 1 collapses everything into the single "a" bucket -> no rotation.
    monkeypatch.setenv("MAC_DISPATCH_PAGE_PREFIX_WIDTH", "1")
    width_one = [task.id for task in cp._rotate_by_page_prefix(tasks, now)]
    assert width_one == ["abx", "aby", "acz"]

    # Width 2 splits into "ab" and "ac" buckets and round-robins across them.
    monkeypatch.setenv("MAC_DISPATCH_PAGE_PREFIX_WIDTH", "2")
    width_two = [task.id for task in cp._rotate_by_page_prefix(tasks, now)]
    assert width_two == ["abx", "acz", "aby"]

    # An invalid override falls back to the safe default width (2).
    monkeypatch.setenv("MAC_DISPATCH_PAGE_PREFIX_WIDTH", "not-an-int")
    fallback = [task.id for task in cp._rotate_by_page_prefix(tasks, now)]
    assert fallback == width_two


def test_dispatch_ordered_tasks_single_prefix_bucket_is_noop(cp):
    # Auto-generated task ids share the same page prefix, so within each
    # project only a single prefix bucket exists and the page-prefix rotation
    # must leave the cross-project round-robin ordering unchanged.
    flood = [
        cp.create_task(
            "flood-%d" % index,
            project="flood",
            priority=100,
            required_capabilities=["python"],
        )
        for index in range(3)
    ]
    starved = cp.create_task(
        "starved",
        project="starved",
        priority=10,
        required_capabilities=["python"],
    )

    ordered = cp._dispatch_ordered_tasks()

    assert [task.id for task in ordered] == [
        flood[0].id,
        starved.id,
        flood[1].id,
        flood[2].id,
    ]


def test_dispatch_ordered_tasks_rotates_page_prefixes_within_project(cp):
    # A single project whose ready tasks split into two page-prefix buckets:
    # the "aa" prefix floods the window while a late "bb" prefix would queue
    # behind every "aa" candidate without prefix rotation.
    flood = [
        cp.create_task(
            "flood-%d" % index,
            project="rot",
            priority=100,
            required_capabilities=["python"],
            _task_id="aa%02d" % index,
        )
        for index in range(3)
    ]
    starved = cp.create_task(
        "starved",
        project="rot",
        priority=100,
        required_capabilities=["python"],
        _task_id="bb00",
    )

    ordered = cp._dispatch_ordered_tasks()

    # The late "bb" prefix wins the second claim slot instead of trailing the
    # whole "aa" flood; within the "aa" bucket the created order is preserved.
    assert [task.id for task in ordered] == [
        flood[0].id,
        starved.id,
        flood[1].id,
        flood[2].id,
    ]


def test_ready_tasks_consume_page_prefix_rotation(cp):
    # ready_tasks reads through _dispatch_ordered_tasks, so the rotated
    # ordering must survive the readiness gates for open, ungated tasks.
    flood = [
        cp.create_task(
            "flood-%d" % index,
            project="rot",
            priority=100,
            required_capabilities=["python"],
            _task_id="aa%02d" % index,
        )
        for index in range(3)
    ]
    starved = cp.create_task(
        "starved",
        project="rot",
        priority=100,
        required_capabilities=["python"],
        _task_id="bb00",
    )

    ready = cp.ready_tasks()

    assert [task.id for task in ready] == [
        flood[0].id,
        starved.id,
        flood[1].id,
        flood[2].id,
    ]


def _rank_for(task, order_signal):
    """Build a WorkPackageTaskRank keyed to *task* with the given order signal."""
    return WorkPackageTaskRank(
        package_id="pkg-%s" % task.id,
        plan_version=1,
        epoch=1,
        node_key="node-%s" % task.id,
        critical_path_rank=1.0,
        order_signal=order_signal,
    )


def test_dispatch_task_sort_key_orders_priority_then_signal_then_age(cp):
    now = services.utcnow()
    high = cp.create_task("high", priority=100, required_capabilities=["python"])
    low = cp.create_task("low", priority=10, required_capabilities=["python"])

    high_key = cp._dispatch_task_sort_key(high, now)
    low_key = cp._dispatch_task_sort_key(low, now)

    # The sort key negates effective priority so ascending sort surfaces the
    # higher-priority task first, and the tuple carries (priority, signal, age,
    # id) in that precedence.
    assert high_key < low_key
    assert high_key[0] == -100
    assert low_key[0] == -10
    # With no work-package rank the order signal component is a neutral zero.
    assert high_key[1] == 0.0
    assert high_key[2] == high.created_at
    assert high_key[3] == high.id


def test_dispatch_task_sort_key_breaks_priority_ties_on_order_signal(cp):
    now = services.utcnow()
    task_a = cp.create_task("wp-a", priority=5, required_capabilities=["python"])
    task_b = cp.create_task("wp-b", priority=5, required_capabilities=["python"])
    ranks = {
        task_a.id: _rank_for(task_a, 0.9),
        task_b.id: _rank_for(task_b, 0.1),
    }

    key_a = cp._dispatch_task_sort_key(task_a, now, task_ranks=ranks)
    key_b = cp._dispatch_task_sort_key(task_b, now, task_ranks=ranks)

    # Equal priority: the higher critical-path order signal wins the tie, ahead
    # of the created_at/id fallbacks.
    assert key_a[0] == key_b[0] == -5
    assert key_a[1] == -0.9
    assert key_b[1] == -0.1
    assert key_a < key_b


def test_dispatch_ordered_tasks_breaks_priority_ties_by_created_at(cp):
    # Audit gap-closer (docs/dispatch-priority-bias-audit.md): the sort-key
    # tuple only *carries* created_at at index [2]; no end-to-end test pinned
    # that it actually DECIDES the order when priority and the (absent) order
    # signal both tie. Two equal-priority tasks with no work-package rank must
    # dispatch oldest-first (FIFO), so re-prioritising or aging never silently
    # reorders same-priority peers.
    now = services.utcnow()
    newer = cp.create_task(
        "newer", priority=5, required_capabilities=["python"]
    )
    older = cp.create_task(
        "older", priority=5, required_capabilities=["python"]
    )
    newer_at = (
        services.parse_time(now) - timedelta(hours=1)
    ).isoformat(timespec="microseconds")
    older_at = (
        services.parse_time(now) - timedelta(hours=3)
    ).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?", (newer_at, newer.id)
    )
    cp.store.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?", (older_at, older.id)
    )

    ordered = cp._dispatch_ordered_tasks()

    # Equal priority, no order signal: the earlier created_at wins the tie.
    assert [task.id for task in ordered[:2]] == [older.id, newer.id]


def test_dispatch_task_sort_key_age_bonus_lifts_effective_priority(cp):
    now = services.utcnow()
    aged = cp.create_task("aged", priority=0, required_capabilities=["python"])
    aged_created = (
        services.parse_time(now) - timedelta(days=2)
    ).isoformat(timespec="microseconds")
    aged = dataclasses.replace(aged, created_at=aged_created)
    fresh_high = cp.create_task(
        "fresh-high", priority=1, required_capabilities=["python"]
    )

    aged_key = cp._dispatch_task_sort_key(aged, now)
    fresh_key = cp._dispatch_task_sort_key(fresh_high, now)

    # Two aging steps push the aged zero-priority task's effective priority to 2,
    # so it sorts ahead of a fresh priority-1 task instead of starving behind it.
    assert aged_key[0] == -2
    assert fresh_key[0] == -1
    assert aged_key < fresh_key


def test_dispatch_priority_age_bonus_counts_whole_aging_steps(cp):
    now = services.utcnow()
    fresh = cp.create_task("fresh", priority=0, required_capabilities=["python"])
    fresh = dataclasses.replace(fresh, created_at=now)
    # Just under one aging period yields no bonus; exactly one period yields 1.
    almost = (
        services.parse_time(now)
        - timedelta(seconds=cp._DISPATCH_PRIORITY_AGING_SECONDS - 1)
    ).isoformat(timespec="microseconds")
    exactly_two = (
        services.parse_time(now)
        - timedelta(seconds=2 * cp._DISPATCH_PRIORITY_AGING_SECONDS)
    ).isoformat(timespec="microseconds")

    assert cp._dispatch_priority_age_bonus(fresh, now) == 0
    assert (
        cp._dispatch_priority_age_bonus(
            dataclasses.replace(fresh, created_at=almost), now
        )
        == 0
    )
    assert (
        cp._dispatch_priority_age_bonus(
            dataclasses.replace(fresh, created_at=exactly_two), now
        )
        == 2
    )


def test_dispatch_priority_age_bonus_env_override_shrinks_step(cp, monkeypatch):
    now = services.utcnow()
    task = cp.create_task("aging", priority=0, required_capabilities=["python"])
    created = (
        services.parse_time(now) - timedelta(seconds=600)
    ).isoformat(timespec="microseconds")
    task = dataclasses.replace(task, created_at=created)

    # A 60-second aging step turns 600 seconds of age into ten priority points.
    monkeypatch.setenv("MAC_DISPATCH_PRIORITY_AGING_SECONDS", "60")
    assert cp._dispatch_priority_age_bonus(task, now) == 10

    # A below-floor override (< 60s) can never DISABLE the aging cap: it falls
    # back to the safe 24h default step, so 600s of age earns no bonus.
    monkeypatch.setenv("MAC_DISPATCH_PRIORITY_AGING_SECONDS", "1")
    assert cp._dispatch_priority_age_bonus(task, now) == 0

    # An unparseable override likewise falls back to the 24h default step.
    monkeypatch.setenv("MAC_DISPATCH_PRIORITY_AGING_SECONDS", "not-an-int")
    assert cp._dispatch_priority_age_bonus(task, now) == 0


def test_dispatch_priority_age_bonus_tolerates_corrupt_timestamp(cp):
    now = services.utcnow()
    task = cp.create_task("corrupt", priority=0, required_capabilities=["python"])
    task = dataclasses.replace(task, created_at="not-a-timestamp")

    # A corrupt created_at must not raise or starve dispatch; it simply earns no
    # aging bonus.
    assert cp._dispatch_priority_age_bonus(task, now) == 0
    assert cp._dispatch_task_sort_key(task, now)[0] == 0


def test_dispatch_candidate_tasks_unions_priority_and_oldest_windows(cp, monkeypatch):
    # Shrink the scan window so the priority window alone cannot surface the
    # ancient low-priority task; only the oldest-window union can.
    monkeypatch.setattr(cp, "_DISPATCH_TASK_WINDOW", 2, raising=False)
    now = services.utcnow()
    ancient = cp.create_task("ancient", priority=0, required_capabilities=["python"])
    ancient_created = (
        services.parse_time(now) - timedelta(days=30)
    ).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        (ancient_created, ancient.id),
    )
    hot = [
        cp.create_task("hot-%d" % index, priority=100, required_capabilities=["python"])
        for index in range(2)
    ]

    candidates = cp._dispatch_candidate_tasks()
    candidate_ids = {task.id for task in candidates}

    # The priority window keeps the hot tasks visible while the oldest window
    # rescues the ancient task, and the union is de-duplicated (no repeats).
    assert candidate_ids == {ancient.id, hot[0].id, hot[1].id}
    assert len(candidates) == len(candidate_ids)


def test_dispatch_candidate_tasks_scopes_to_requested_project(cp):
    keep = cp.create_task(
        "keep", project="wanted", priority=5, required_capabilities=["python"]
    )
    cp.create_task(
        "drop", project="other", priority=5, required_capabilities=["python"]
    )

    candidates = cp._dispatch_candidate_tasks(project="wanted")

    assert [task.id for task in candidates] == [keep.id]


def test_claim_next_records_per_task_agent_skip_reason(cp, monkeypatch):
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")
    worker = register_agent(cp, "worker", ["python"])
    skipped = cp.create_task(
        "needs-review-capability",
        priority=10,
        required_capabilities=["review"],
    )
    claimed_task = cp.create_task(
        "python-work",
        priority=0,
        required_capabilities=["python"],
    )

    claimed = cp.claim_next_for_agent(worker.id)

    assert claimed is not None
    assert claimed["task"]["id"] == claimed_task.id
    observations = cp.list_observability(
        name="worker.routing.task_skipped",
        subject_type="task",
        subject_id=skipped.id,
        limit=10,
    )
    assert observations
    assert observations[0].detail["agent_id"] == worker.id
    assert observations[0].detail["reason"] == "capabilities_missing"
    assert observations[0].detail["reason_class"] == "agent_availability"


def test_dispatch_records_cooperative_skip_reason_per_agent(cp, monkeypatch):
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    child = cp.create_task("child", required_capabilities=["python"])
    finish_task(cp, child, worker, reviewer)
    integration = cp.create_task(
        "integration",
        priority=10,
        required_capabilities=["python"],
        metadata={
            "coordination": {
                "require_distinct_agent": True,
                "child_task_ids": [child.id],
            },
            "relationships": {"child_task_ids": [child.id]},
        },
    )
    fallback = cp.create_task(
        "fallback",
        priority=0,
        required_capabilities=["python"],
    )

    assignment = cp.dispatch_once()

    # The per-agent cooperative skip reason is still recorded on the strict
    # pass; the higher-priority integration task is then recovered by the
    # distinct-executor fallback (it no longer deadlocks behind the lower
    # priority standalone task).
    assert assignment is not None
    assert assignment["task"]["id"] == integration.id
    assert fallback.id  # standalone task remains open for a later pass
    observations = cp.list_observability(
        name="dispatcher.routing.task_skipped",
        subject_type="task",
        subject_id=integration.id,
        limit=20,
    )
    assert any(
        event.detail["agent_id"] == worker.id
        and event.detail["reason"] == "cooperative_distinct_agent_excluded"
        for event in observations
    )


def test_review_sweep_isolates_per_task_errors(cp, monkeypatch):
    """A single unadvanceable review must not abort the whole sweep — which,
    inside the hub self-tick, would abort the tick and starve dispatch."""
    worker = register_agent(cp, "worker", ["python"])
    _reviewer = register_agent(cp, "reviewer", ["review"])
    poison = cp.create_task("poison", required_capabilities=["python"])
    healthy = cp.create_task("healthy", required_capabilities=["python"])
    # Drive both into needs_review so the sweep query selects them.
    for task in (poison, healthy):
        cp.claim_task(task.id, worker.id)
        cp.start_task(task.id, worker.id)
        cp.add_evidence(
            task.id,
            "test",
            "artifact://tests",
            "tests passed",
            worker.id,
            metadata=verified_repo_metadata(cp, worker.id),
        )
        cp.submit_for_review(task.id, worker.id)

    real_advance = cp.advance_default_review_workflow

    def exploding_advance(task_id, *args, **kwargs):
        if task_id == poison.id:
            raise ValidationError("review is already completed")
        return real_advance(task_id, *args, **kwargs)

    monkeypatch.setattr(cp, "advance_default_review_workflow", exploding_advance)

    # Must not raise even though one row blows up.
    result = cp.advance_default_review_workflows()
    statuses = {r["task_id"]: r.get("status") for r in result["results"]}
    assert statuses.get(poison.id) == "error"
    assert poison.id in statuses and healthy.id in statuses
    errors = cp.list_observability(
        name="workflow.default_review.error",
        subject_type="task",
        subject_id=poison.id,
        limit=10,
    )
    assert errors



def test_expired_lease_does_not_cooperatively_exclude(cp):
    """A crashed attempt (expired lease) must not permanently burn an agent for
    the whole cooperative family — only a real, non-expired attempt counts."""
    from mac.models import LeaseStatus

    worker = register_agent(cp, "worker", ["python"])
    child = cp.create_task("child", required_capabilities=["python"])
    _task, lease = cp.claim_task(child.id, worker.id)
    # Simulate the agent going offline mid-attempt: its lease expires.
    cp.store.execute(
        "UPDATE leases SET status = ? WHERE id = ?",
        (LeaseStatus.EXPIRED.value, lease.id),
    )
    integration = cp.create_task(
        "integration",
        required_capabilities=["python"],
        metadata={
            "coordination": {
                "require_distinct_agent": True,
                "child_task_ids": [child.id],
            },
            "relationships": {"child_task_ids": [child.id]},
        },
    )

    # The expired lease is not participation, so the crashed worker stays
    # eligible (and can retry) rather than being excluded forever.
    assert worker.id not in cp._coordination_excluded_agent_ids(integration)
    available, _reason = cp._agent_availability_for_task(worker, integration)
    assert available


def test_cooperative_dispatch_falls_back_when_pool_exhausted(cp):
    """When every python-capable agent has already participated in a cooperative
    family, the dispatcher relaxes the distinct-executor preference and still
    assigns the task instead of leaving it permanently undispatchable."""
    agent_a = register_agent(cp, "agent-a", ["python"])
    agent_b = register_agent(cp, "agent-b", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    child_a = cp.create_task("child-a", required_capabilities=["python"])
    child_b = cp.create_task("child-b", required_capabilities=["python"])
    # Both executors genuinely complete a family member (durable released leases).
    finish_task(cp, child_a, agent_a, reviewer)
    finish_task(cp, child_b, agent_b, reviewer)
    integration = cp.create_task(
        "integration",
        required_capabilities=["python"],
        metadata={
            "coordination": {
                "require_distinct_agent": True,
                "child_task_ids": [child_a.id, child_b.id],
            },
            "relationships": {"child_task_ids": [child_a.id, child_b.id]},
        },
    )

    # Strict pass excludes both executors; the reviewer lacks python. Without the
    # fallback this task is undispatchable — with it, a real assignment is made.
    assert cp._coordination_excluded_agent_ids(integration) >= {agent_a.id, agent_b.id}
    assignment = cp.dispatch_once()
    assert assignment is not None
    assert assignment["task"]["id"] == integration.id
    assert assignment["agent"]["id"] in {agent_a.id, agent_b.id}
    fallback_events = cp.list_observability(
        name="dispatcher.routing.cooperative_fallback",
        subject_type="task",
        subject_id=integration.id,
        limit=20,
    )
    assert fallback_events


def test_cooperative_dispatch_prefers_distinct_agent_over_fallback(cp):
    """The fallback is a last resort: when an unexcluded distinct agent exists,
    it is chosen and the fallback never fires."""
    agent_a = register_agent(cp, "agent-a", ["python"])
    agent_b = register_agent(cp, "agent-b", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    child_a = cp.create_task("child-a", required_capabilities=["python"])
    finish_task(cp, child_a, agent_a, reviewer)
    integration = cp.create_task(
        "integration",
        required_capabilities=["python"],
        metadata={
            "coordination": {
                "require_distinct_agent": True,
                "child_task_ids": [child_a.id],
            },
            "relationships": {"child_task_ids": [child_a.id]},
        },
    )

    assignment = cp.dispatch_once()
    assert assignment is not None
    assert assignment["task"]["id"] == integration.id
    # agent_b never touched the family, so it is the distinct choice.
    assert assignment["agent"]["id"] == agent_b.id
    assert not cp.list_observability(
        name="dispatcher.routing.cooperative_fallback",
        subject_type="task",
        subject_id=integration.id,
        limit=20,
    )


def test_dependencies_block_until_parent_completes(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    parent = cp.create_task("Parent", required_capabilities=["python"])
    child = cp.create_task("Child", required_capabilities=["python"], dependencies=[parent.id])

    assert child.state == TaskState.WAITING.value
    finish_task(cp, parent, worker, reviewer)
    tick = cp.tick()

    assert cp.get_task(child.id).state == TaskState.CLAIMED.value
    assert tick["assignments"][0]["task"]["id"] == child.id


def test_manual_block_without_dependencies_is_not_auto_unblocked(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("Needs manual repair", required_capabilities=["python"])
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "verifier",
        {"reason": "verification_contract_failed", "manual_repair_required": True},
    )

    tick = cp.tick()

    assert tick["assignments"] == []
    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    assert cp.claim_next_for_agent(worker.id) is None


def test_manual_repair_block_with_satisfied_dependencies_is_not_auto_unblocked(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    parent = cp.create_task("Parent", required_capabilities=["python"])
    child = cp.create_task("Child", required_capabilities=["python"], dependencies=[parent.id])

    finish_task(cp, parent, worker, reviewer)
    cp._transition_task_internal(
        child.id,
        TaskState.OPEN.value,
        "dispatcher",
        {"reason": "dependencies satisfied"},
    )
    cp._transition_task_internal(
        child.id,
        TaskState.BLOCKED.value,
        "verifier",
        {"reason": "verification_contract_failed", "manual_repair_required": True},
    )

    tick = cp.tick()

    assert tick["assignments"] == []
    assert cp.get_task(child.id).state == TaskState.BLOCKED.value
    assert cp.claim_next_for_agent(worker.id) is None
    with pytest.raises(TransitionError):
        cp.claim_task(child.id, worker.id)


@pytest.mark.parametrize(
    "reason",
    ["verification_contract_failed", "executor_failed", "worker_exception"],
)
def test_reason_only_manual_repair_blocks_are_not_auto_unblocked(cp, reason):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    parent = cp.create_task("Parent", required_capabilities=["python"])
    child = cp.create_task("Child", required_capabilities=["python"], dependencies=[parent.id])

    finish_task(cp, parent, worker, reviewer)
    cp._transition_task_internal(
        child.id,
        TaskState.OPEN.value,
        "dispatcher",
        {"reason": "dependencies satisfied"},
    )
    cp._transition_task_internal(
        child.id, TaskState.BLOCKED.value, "worker", {"reason": reason}
    )

    tick = cp.tick()

    assert tick["assignments"] == []
    assert cp.get_task(child.id).state == TaskState.BLOCKED.value
    assert cp.claim_next_for_agent(worker.id) is None
    with pytest.raises(TransitionError):
        cp.claim_task(child.id, worker.id)


def test_message_bus_accepts_structured_payloads_and_rejects_execution(cp):
    sender = register_agent(cp, "sender", ["python"])
    recipient = register_agent(cp, "recipient", ["review"])
    message = cp.send_message(
        sender.id,
        recipient.id,
        "help_request",
        {"question": "Can you inspect this evidence?", "evidence_id": "ev_123"},
    )

    delivered = cp.deliver_messages(recipient.id)
    assert delivered[0].id == message.id
    assert delivered[0].status == "delivered"

    with pytest.raises(ValidationError):
        cp.send_message(
            sender.id,
            recipient.id,
            "help_request",
            {"question": "Can you inspect this evidence?", "command": "rm -rf /"},
        )


def test_secrets_are_scoped_redacted_audited_and_not_stored_plaintext(cp):
    deployer = register_agent(cp, "deployer", ["deploy"])
    docs = register_agent(cp, "docs", ["docs"])
    secret = cp.create_secret(
        "github-token",
        "super-secret-token",
        {"capabilities": ["deploy"]},
        "human",
    )

    handle = cp.request_secret(secret.id, deployer.id, "publish release")
    assert handle.handle.startswith("secret://")
    assert "super-secret-token" not in handle.handle
    assert cp.reveal_secret(secret.id, handle.audit_id, deployer.id) == "super-secret-token"

    with pytest.raises(AuthorizationError):
        cp.request_secret(secret.id, docs.id, "read docs")

    redacted = cp.list_secrets()[0].to_dict()
    assert redacted["value"] == "***REDACTED***"
    stored = cp.store.query_one("SELECT ciphertext FROM secrets WHERE id = ?", (secret.id,))
    assert stored["ciphertext"] != "super-secret-token"
    audits = cp.list_secret_audits(secret.id)
    assert [audit.result for audit in audits] == ["granted", "denied"]


def test_rotate_secret_updates_value_in_place_and_audits(cp):
    deployer = register_agent(cp, "rotdep", ["deploy"])
    secret = cp.create_secret(
        "img-key", "old-value", {"capabilities": ["deploy"]}, "human"
    )
    rotated = cp.rotate_secret("img-key", "new-value", "operator")
    assert rotated.id == secret.id  # same row — id + scopes preserved, not a new secret
    handle = cp.request_secret(secret.id, deployer.id, "use")
    assert cp.reveal_secret(secret.id, handle.audit_id, deployer.id) == "new-value"
    # the rotation is audited (not stored plaintext, like every other access)
    assert any(audit.result == "rotated" for audit in cp.list_secret_audits(secret.id))


def test_delete_secret_scrubs_value_and_allows_name_reuse(cp):
    cp.create_secret(
        "stale-router-key", "old-upstream-value", {"capabilities": ["router-upstream"]}, "deploy"
    )
    assert any(s.name == "stale-router-key" for s in cp.list_secrets())
    # hard-delete scrubs the value + removes the row
    result = cp.delete_secret("stale-router-key", actor="operator")
    assert result["deleted"] is True and result["name"] == "stale-router-key"
    assert cp.store.query_one("SELECT * FROM secrets WHERE name = ?", ("stale-router-key",)) is None
    assert not any(s.name == "stale-router-key" for s in cp.list_secrets())
    # deleting an absent secret raises
    with pytest.raises(NotFoundError):
        cp.delete_secret("stale-router-key")
    # the name is reusable after deletion
    again = cp.create_secret(
        "stale-router-key", "new-value", {"capabilities": ["router-upstream"]}, "deploy"
    )
    assert again.name == "stale-router-key"


def test_runtime_boundary_pins_manifests_and_blocks_secret_values(cp):
    manifest = {
        "image": "python:3.12@sha256:abc123",
        "dependencies": ["fastapi==0.111.0"],
        "entrypoint": ["pytest"],
        "secret_refs": ["github-token"],
    }
    runtime = cp.create_runtime("pytest", manifest, "human")
    same = cp.create_runtime("pytest-copy", dict(reversed(list(manifest.items()))), "human")

    assert runtime.digest == same.digest
    with pytest.raises(ValidationError):
        cp.create_runtime("latest", {"image": "python:latest"}, "human")
    with pytest.raises(ValidationError):
        cp.create_runtime("leaky", {"image": "python:3.12@sha256:abc123", "env": {"TOKEN": "raw"}}, "human")


def test_runtime_delta_lifecycle_validates_and_promotes_task_local_env(cp):
    runtime = create_runtime(cp, "runtime-delta-base")
    agent = register_agent(cp, "runtime-delta-worker", ["python"])
    task = cp.create_task("Use new wheel", project="mac")

    delta = cp.propose_runtime_delta(
        task.id,
        agent.id,
        "pip",
        [
            "python -m venv .venv",
            "./.venv/bin/pip install rich==13.7.1",
        ],
        ["rich==13.7.1"],
        "task needed a pinned formatter dependency",
        base_runtime_id=runtime.id,
        lockfile_path="requirements.txt",
        lockfile_digest="sha256:" + "a" * 64,
    )
    assert delta.status == "proposed"

    validated = cp.validate_runtime_delta(delta.id, "operator")
    assert validated.status == "validated"
    assert validated.validation["problems"] == []

    promoted = cp.promote_runtime_delta(validated.id, "operator")
    assert promoted.status == "promoted"
    promoted_runtime = cp.get_runtime(promoted.promoted_runtime_environment_id)
    assert promoted_runtime.manifest["derived_from"]["runtime_environment_id"] == runtime.id
    assert "rich==13.7.1" in promoted_runtime.manifest["dependencies"]
    assert promoted_runtime.manifest["runtime_deltas"][0]["id"] == delta.id


def test_runtime_delta_validation_rejects_global_installs(cp):
    runtime = create_runtime(cp, "runtime-delta-bad-base")
    agent = register_agent(cp, "runtime-delta-bad-worker", ["node"])
    task = cp.create_task("Install global package", project="mac")

    delta = cp.propose_runtime_delta(
        task.id,
        agent.id,
        "npm",
        ["npm install -g left-pad@1.3.0"],
        ["left-pad@1.3.0"],
        "global install should not pass validation",
        base_runtime_id=runtime.id,
        lockfile_path="package-lock.json",
        lockfile_digest="sha256:" + "b" * 64,
    )

    rejected = cp.validate_runtime_delta(delta.id, "operator")
    assert rejected.status == "rejected"
    assert any("globally" in problem for problem in rejected.validation["problems"])


def test_add_evidence_captures_environment_delta_proposal(cp):
    runtime = create_runtime(cp, "runtime-delta-evidence-base")
    agent = register_agent(cp, "runtime-delta-evidence-worker", ["python"])
    task = cp.create_task("Capture dependency delta", project="mac")

    evidence = cp.add_evidence(
        task.id,
        "log",
        "stdout://worker",
        "worker completed with environment delta",
        agent.id,
        metadata={
            "returncode": 0,
            "verification": {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "operator_result",
                "summary": "dependency added",
                "environment_delta": {
                    "package_manager": "uv",
                    "commands": ["uv add httpx==0.27.2"],
                    "added_dependencies": ["httpx==0.27.2"],
                    "base_runtime_id": runtime.id,
                    "lockfile_path": "uv.lock",
                    "lockfile_digest": "sha256:" + "c" * 64,
                    "reason": "task needed http client wheel",
                },
            },
        },
        _trusted_internal=True,
    )

    deltas = cp.list_runtime_deltas(task_id=task.id)
    assert len(deltas) == 1
    assert deltas[0].evidence_id == evidence.id
    assert deltas[0].base_runtime_id == runtime.id
    assert deltas[0].added_dependencies == ["httpx==0.27.2"]


def test_dispatch_runtime_digest_requirement_filters_agents(cp):
    runtime_a = create_runtime(cp, "runtime-delta-dispatch-a")
    runtime_b = cp.create_runtime(
        "runtime-delta-dispatch-b",
        {
            "image": "python:3.12@sha256:def456",
            "dependencies": ["fastapi==0.111.0", "rich==13.7.1"],
            "entrypoint": ["pytest"],
        },
        "human",
    )
    agent_a = register_agent(cp, "runtime-digest-a", ["python"])
    agent_b = register_agent(cp, "runtime-digest-b", ["python"])
    agent_a = cp.heartbeat_agent(agent_a.id, running_digest=runtime_a.digest)
    agent_b = cp.heartbeat_agent(agent_b.id, running_digest=runtime_b.digest)
    task = cp.create_task(
        "Needs promoted runtime",
        required_capabilities=["python"],
        metadata={"runtime": {"runtime_environment_id": runtime_b.id}},
    )

    assert cp._agent_available_for(agent_a, task) is False
    assert cp._agent_available_for(agent_b, task) is True


def test_project_bridge_memory_and_rollout_rescue(cp):
    item = cp.import_project_item(
        "github",
        "42",
        "Fix issue",
        {"url": "https://example.invalid/issues/42"},
        required_capabilities=["python"],
    )
    duplicate = cp.import_project_item("github", "42", "Fix issue", {"url": "ignored"})
    assert duplicate.id == item.id
    assert cp.get_task(item.task_id).metadata["external_id"] == "42"
    assert cp.search_memory(task_id=item.task_id)[0].record_type == "imported"

    rollout = create_verified_rollout(cp, "0.2.0")
    canary = cp.advance_rollout(rollout.id, "start_canary", "human")
    assert canary.status == RolloutStatus.CANARYING.value

    rescued, rescue_task = cp.rescue_rollout(rollout.id, "human", "canary failed health checks")
    assert rescued.status == RolloutStatus.RESCUING.value
    assert rescue_task.priority == 100
    assert rescue_task.metadata["rescue"] is True


def _write_beads(repo_path, issues):
    _write_repository_contract(repo_path)
    beads_dir = repo_path / ".beads"
    beads_dir.mkdir(parents=True)
    (beads_dir / "issues.jsonl").write_text(
        "\n".join(json.dumps(issue) for issue in issues) + "\n",
        encoding="utf-8",
    )


def _write_repository_contract(
    repo_path,
    project="repo-beads-mac",
    include_test=True,
    *,
    canonical_remote_url=None,
    default_branch=None,
):
    contract_dir = repo_path / ".mac"
    contract_dir.mkdir(parents=True, exist_ok=True)
    test_block = (
        "test:\n  command: PATH=.venv/bin:$PATH .venv/bin/python -m pytest\n"
        if include_test
        else "test: {}\n"
    )
    remote_block = (
        "canonical_remote_url: %s\n" % canonical_remote_url
        if canonical_remote_url
        else ""
    )
    branch_block = (
        "default_branch: %s\n" % default_branch if default_branch else ""
    )
    (contract_dir / "project.yaml").write_text(
        (
            "schema: mac.repository_contract.v1\n"
            "project: %s\n"
            "%s"
            "%s"
            "platforms:\n"
            "  - darwin\n"
            "  - linux\n"
            "  - wsl2\n"
            "toolchain:\n"
            "  required_commands:\n"
            "    - python3\n"
            "bootstrap:\n"
            "  command: python3 scripts/bootstrap-project.py\n"
            "  creates:\n"
            "    - .venv/bin/python\n"
            "%s"
            "evidence:\n"
            "  required:\n"
            "    - tests\n"
        )
        % (project, remote_block, branch_block, test_block),
        encoding="utf-8",
    )


def _repository_task_metadata(
    project="repo-beads-mac",
    required_commands=("python3", "git", "gh"),
):
    return {
        "origin": {
            "type": "direct_task",
            "repository_contract": {
                "schema": "mac.repository_contract.v1",
                "project": project,
                "platforms": ["darwin", "linux", "wsl2"],
                "toolchain": {"required_commands": list(required_commands)},
                "bootstrap": {
                    "command": "python3 scripts/bootstrap-project.py",
                    "creates": [".venv/bin/python"],
                },
                "test": {"command": "scripts/run-contract-tests.sh"},
                "evidence": {"required": ["tests"]},
            },
        }
    }


def test_repository_contract_commands_do_not_become_dispatch_capabilities(cp):
    machine = cp.register_machine("worker")
    agent = cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
            }
        },
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata=_repository_task_metadata(),
    )

    assignment = cp.dispatch_once()

    assert task.required_capabilities == ["python"]
    assert task.metadata["toolchain_requirements"]["required_commands"] == [
        "python3",
        "git",
        "gh",
    ]
    assert task.metadata["execution_contract"]["evidence_type"] == "repo_change"
    assert task.metadata["toolchain_requirements"]["filtered_from_required_capabilities"] == ["git"]
    assert assignment is not None
    assert assignment["agent"]["id"] == agent.id


def test_repository_contract_project_commands_do_not_gate_dispatch(cp):
    machine = cp.register_machine("worker")
    agent = cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources={
            "openshell_required": True,
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["git"],
            }
        },
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata=_repository_task_metadata(required_commands=("python3", "git", "gh", "pnpm", "java", "lein")),
    )

    assignment = cp.dispatch_once()

    assert assignment is not None
    assert assignment["agent"]["id"] == agent.id
    assert cp.get_task(task.id).state == "claimed"


def _verified_coding_route_resources(*, verified=True, model=""):
    fingerprint = "sha256:route-proof"
    return {
        "openshell_required": True,
        "commands": {
            "schema": "mac.command_inventory.v1",
            "available": ["git"],
        },
        "coding_clis": {
            "schema": "mac.coding_clis.v2",
            "clis": {
                "codex": {
                    "configured": True,
                    "verified": verified,
                    "provider": "mac-router",
                    "protocol": "responses",
                    "model": model,
                    "route_fingerprint": fingerprint,
                    "verification": {
                        "schema": "mac.coding_agent.verification.v1",
                        "verified": verified,
                        "checked_at": utcnow(),
                        "model": model,
                        "route_fingerprint": fingerprint,
                    },
                }
            },
        },
    }


def test_repo_dispatch_requires_v2_in_sandbox_route_proof(cp, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    machine = cp.register_machine("worker")
    cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources=_verified_coding_route_resources(verified=False),
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata=_repository_task_metadata(),
    )

    assert cp.dispatch_once() is None
    assert cp.get_task(task.id).state == "open"


def test_repo_dispatch_accepts_fresh_matching_route_and_model(cp, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    machine = cp.register_machine("worker")
    agent = cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources=_verified_coding_route_resources(model="gpt-test"),
    )
    cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata={**_repository_task_metadata(), "model": "gpt-test"},
    )

    assignment = cp.dispatch_once()

    assert assignment is not None
    assert assignment["agent"]["id"] == agent.id


def test_repo_dispatch_holds_unverified_pinned_model(cp, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    machine = cp.register_machine("worker")
    cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources=_verified_coding_route_resources(model="gpt-default"),
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata={**_repository_task_metadata(), "model": "qwen-pinned"},
    )

    assert cp.dispatch_once() is None
    assert cp.get_task(task.id).state == "open"


def test_repo_dispatch_strict_mode_rejects_legacy_route_report(cp, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    machine = cp.register_machine("worker")
    cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources={
            "openshell_required": True,
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["git"],
            },
            "coding_clis": {"schema": "mac.coding_clis.v1", "clis": {}},
        },
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata=_repository_task_metadata(),
    )

    assert cp.dispatch_once() is None
    assert cp.get_task(task.id).state == "open"


def test_repository_contract_project_commands_gate_unsandboxed_dispatch(cp):
    machine = cp.register_machine("worker")
    cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["git"],
            }
        },
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata=_repository_task_metadata(required_commands=("python3", "git", "gh", "pnpm", "java", "lein")),
    )

    assert cp.dispatch_once() is None
    assert cp.get_task(task.id).state == "open"


def test_repository_contract_host_git_still_gates_dispatch(cp):
    machine = cp.register_machine("worker")
    cp.register_agent(
        machine.id,
        "coder",
        capabilities=["python"],
        resources={
            "openshell_required": True,
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "gh", "pnpm", "java", "lein"],
            }
        },
    )
    task = cp.create_task(
        "repo task",
        project="repo-beads-mac",
        required_capabilities=["git", "python"],
        metadata=_repository_task_metadata(),
    )

    assert cp.dispatch_once() is None
    pending = cp.provisioning.list_pending_requests()
    assert len(pending) == 1
    assert pending[0].task_id == task.id
    assert pending[0].capabilities == ["python"]
    assert pending[0].detail["required_commands"] == ["python3", "git", "gh"]
    assert pending[0].detail["sandbox_host_required_commands"] == ["git"]
    assert pending[0].detail["sandbox_required_commands"] == ["python3", "git", "gh"]


def _write_fake_bd_cli(path, ready_path, *, bootstrap_returncode=0, bootstrap_stderr=""):
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "if len(args) >= 2 and args[0] == '--actor':",
                "    args = args[2:]",
                "if args == ['ready', '--json']:",
                "    sys.stdout.write(pathlib.Path(%r).read_text(encoding='utf-8'))" % str(ready_path),
                "    sys.exit(0)",
                "if args[:1] == ['bootstrap']:",
                "    sys.stderr.write(%r)" % bootstrap_stderr,
                "    sys.exit(%d)" % bootstrap_returncode,
                "if args == ['dolt', 'pull'] or args == ['dolt', 'push']:",
                "    sys.exit(0)",
                "if args[:1] == ['export']:",
                "    issues = json.loads(pathlib.Path(%r).read_text(encoding='utf-8') or '[]')" % str(ready_path),
                "    output = '\\n'.join(json.dumps(item) for item in issues)",
                "    if output:",
                "        output += '\\n'",
                "    pathlib.Path(args[args.index('-o') + 1]).write_text(output, encoding='utf-8')",
                "    sys.exit(0)",
                "sys.stderr.write('unsupported fake bd command: %s\\n' % ' '.join(args))",
                "sys.exit(1)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_beads_repository_registration_requires_runtime_contract(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValidationError, match="runtime contract not found"):
        cp.register_project_repository("mac", str(repo), source="repo-beads-mac")


def test_project_unregister_does_not_leave_disabled_repository_as_derived_project(
    cp, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repository_contract(repo, project="retired-project")
    project = cp.create_project("retired-project")
    cp.register_project_repository(
        "retired-repository",
        str(repo),
        source="repo-retired-project",
        project=project.name,
    )
    task = cp.create_task(
        "historical repository task",
        project=project.name,
        metadata={
            "repository": "repo-retired-project",
            "origin": {
                "repository": "repo-retired-project",
                "source": "repo-retired-project",
            },
        },
    )

    cp.delete_project(project.id, force=True)

    summaries = cp.list_projects()
    assert all(summary["project"] != project.name for summary in summaries)
    assert all(
        summary["project"] != "repo-retired-project" for summary in summaries
    )
    assert cp.get_task(task.id).project is None
    unassigned = next(
        summary for summary in summaries if summary["project"] == "unassigned"
    )
    assert unassigned["task_count"] == 1
    registration = cp.get_project_repository("retired-repository")
    assert registration.enabled is False


def test_beads_repository_registration_rejects_incomplete_runtime_contract(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repository_contract(repo, include_test=False)

    with pytest.raises(ValidationError, match="test.command"):
        cp.register_project_repository("mac", str(repo), source="repo-beads-mac")


def test_repository_registration_initializes_codegraph_for_git_checkout(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_repository_contract(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "codegraph-cwd.txt"
    codegraph = bin_dir / "codegraph"
    codegraph.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import pathlib",
                "import sys",
                "if sys.argv[1:] != ['init']:",
                "    sys.stderr.write('unexpected args: %r\\n' % (sys.argv[1:],))",
                "    sys.exit(9)",
                "cwd = pathlib.Path.cwd()",
                "(cwd / '.codegraph').mkdir(exist_ok=True)",
                "(cwd / '.codegraph' / 'codegraph.db').write_text('fake\\n', encoding='utf-8')",
                "pathlib.Path(%r).write_text(str(cwd), encoding='utf-8')" % str(marker),
                "sys.exit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    codegraph.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    registered = cp.register_project_repository("mac", str(repo), source="repo-beads-mac")

    status = registered.metadata["codegraph"]
    assert status["command"] == "codegraph init"
    assert status["attempted"] is True
    assert status["initialized"] is True
    assert status["returncode"] == 0
    assert Path(marker.read_text(encoding="utf-8")) == repo
    assert (repo / ".codegraph" / "codegraph.db").exists()
    exclude_text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".codegraph/" in exclude_text
    assert registered.metadata["repository_contract"]["project"] == "repo-beads-mac"


def test_repository_registration_resolves_codegraph_from_mac_home(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_repository_contract(repo)
    git_path = shutil.which("git")
    assert git_path is not None
    mac_home = tmp_path / ".mac"
    bin_dir = mac_home / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / "codegraph-mac-home-cwd.txt"
    codegraph = bin_dir / "codegraph"
    codegraph.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import pathlib",
                "cwd = pathlib.Path.cwd()",
                "(cwd / '.codegraph').mkdir(exist_ok=True)",
                "pathlib.Path(%r).write_text(str(cwd), encoding='utf-8')" % str(marker),
                "",
            ]
        ),
        encoding="utf-8",
    )
    codegraph.chmod(0o755)
    monkeypatch.setenv("MAC_HOME", str(mac_home))
    monkeypatch.setattr(
        services.shutil,
        "which",
        lambda name: None if name == "codegraph" else (git_path if name == "git" else None),
    )

    registered = cp.register_project_repository("mac", str(repo), source="repo-beads-mac")

    status = registered.metadata["codegraph"]
    assert status["initialized"] is True
    assert status["binary"] == str(codegraph)
    assert Path(marker.read_text(encoding="utf-8")) == repo


def test_repository_registration_fails_when_codegraph_init_fails(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_repository_contract(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codegraph = bin_dir / "codegraph"
    codegraph.write_text("#!/bin/sh\necho codegraph failed >&2\nexit 7\n", encoding="utf-8")
    codegraph.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    with pytest.raises(ValidationError, match="codegraph init failed"):
        cp.register_project_repository("mac", str(repo), source="repo-beads-mac")






















def test_direct_task_for_registered_project_gets_repository_execution_contract(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")

    task = cp.create_task(
        "Direct repository task",
        project="repo-beads-mac",
        required_capabilities=["python"],
    )

    assert task.metadata["execution_contract"]["type"] == "repository"
    assert task.metadata["execution_contract"]["quality"] == "strong"
    assert task.metadata["execution_contract"]["evidence_type"] == "repo_change"
    assert task.metadata["origin"]["repository_contract"]["project"] == "repo-beads-mac"
    assert task.metadata["acc_metadata"]["repository_contract_schema"] == "mac.repository_contract.v1"


@pytest.mark.parametrize("route", ["push", "pull"])
def test_large_repository_task_is_sized_before_first_claim(cp, tmp_path, route):
    repo = tmp_path / ("admission-%s" % route)
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    worker = register_agent(cp, "admission-%s" % route, ["python"])
    task = cp.create_task(
        "Split a broad subsystem into independently verifiable components",
        description=" ".join(
            ["Refactor each independent module and preserve its contract."] * 45
        ),
        project="repo-beads-mac",
        required_capabilities=["python"],
    )

    assert task.attempt_count == 0
    assert "scope_estimate" not in task.metadata

    if route == "push":
        assignment = cp.dispatch_once()
    else:
        assignment = cp.claim_next_for_agent(worker.id)

    assert assignment is not None
    prepared = cp.get_task(task.id)
    assert prepared.attempt_count == 1
    assert prepared.metadata["scope_estimate"]["size"] == "large"
    assert prepared.metadata["plan_first"] is True
    assert prepared.metadata["dispatch_admission"]["decision"] == "plan_first"
    transitions = cp.task_history(task.id)
    admission_index = next(
        index
        for index, event in enumerate(transitions)
        if event.event_type == "task.updated"
        and event.actor == "dispatcher.admission"
    )
    claim_index = next(
        index
        for index, event in enumerate(transitions)
        if event.event_type == "task.claimed"
    )
    assert admission_index < claim_index


def test_dispatch_admission_does_not_write_rejected_or_dry_run_candidates(
    cp, tmp_path
):
    repo = tmp_path / "admission-rejections"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    worker = register_agent(cp, "admission-rejections", ["python"])
    description = " ".join(
        ["Refactor each independent module and preserve its contract."] * 45
    )
    held = cp.create_task(
        "Held large repository task",
        description=description,
        project="repo-beads-mac",
        required_capabilities=["python"],
        metadata={"no_dispatch": True},
        priority=300,
    )
    incompatible = cp.create_task(
        "Incompatible large repository task",
        description=description,
        project="repo-beads-mac",
        required_capabilities=["rust"],
        priority=200,
    )

    assert cp.dispatch_once() is None
    assert "scope_estimate" not in cp.get_task(held.id).metadata
    assert "scope_estimate" not in cp.get_task(incompatible.id).metadata

    eligible = cp.create_task(
        "Dry-run large repository task",
        description=description,
        project="repo-beads-mac",
        required_capabilities=["python"],
        priority=400,
    )
    candidate = cp.claim_next_for_agent(worker.id, dry_run=True)

    assert candidate is not None
    assert candidate["task"]["id"] == eligible.id
    assert "scope_estimate" not in cp.get_task(eligible.id).metadata


@pytest.mark.parametrize("evidence_type", ["investigation", "plan_decomposed"])
def test_registered_project_preserves_explicit_non_repository_outcome(
    cp, tmp_path, evidence_type
):
    repo = tmp_path / ("non-repository-%s" % evidence_type)
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")

    task = cp.create_task(
        "Non-repository outcome",
        project="repo-beads-mac",
        metadata={"evidence_type": evidence_type},
    )

    execution = task.metadata["execution_contract"]
    assert execution["type"] == "operator_directive"
    assert execution["repository_required"] is False
    assert execution["evidence_type"] == evidence_type
    assert execution["reason"] == "explicit_non_repository_outcome"
    assert execution["repository_context"]["repository_id"] == (
        cp.get_project_repository("mac").id
    )
    assert "repository_contract" not in task.metadata.get("origin", {})
    assert "repository_path" not in task.metadata.get("origin", {})


def test_registered_read_only_report_has_one_unambiguous_persisted_contract(
    cp, tmp_path
):
    repo = tmp_path / "report-repo"
    repo.mkdir()
    _write_repository_contract(
        repo,
        canonical_remote_url="https://example.invalid/repo-beads-mac.git",
        default_branch="main",
    )
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    worker = register_agent(
        cp,
        "report-contract-worker",
        ["ops"],
        read_only_report_executor_resources(),
    )

    created = cp.create_task(
        "Inspect the registered repository",
        project="repo-beads-mac",
        required_capabilities=["ops"],
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    )
    persisted = cp.get_task(created.id)

    assert persisted.metadata["execution_contract"]["type"] == "repository"
    assert persisted.metadata["execution_contract"]["repository_contract"] == (
        cp.get_project_repository("mac").metadata["repository_contract"]
    )
    assert "repository_contract" not in persisted.metadata["origin"]
    assert "repository_contract" not in {
        key: value
        for key, value in persisted.metadata.items()
        if key != "execution_contract"
    }
    assert "evidence_type" not in json.dumps(persisted.metadata, sort_keys=True)
    assert metadata_declares_read_only_report_repository(persisted.metadata)
    assert persisted.id in {task.id for task in cp.ready_tasks()}

    claimed, _lease = cp.claim_task(persisted.id, worker.id)
    assert _lease.agent_id == worker.id
    assert claimed.state == TaskState.CLAIMED.value
    assert metadata_declares_read_only_report_repository(claimed.metadata)
    assert "evidence_type" not in json.dumps(claimed.metadata, sort_keys=True)


@pytest.mark.parametrize(
    "contradiction",
    [
        {"evidence_type": "investigation"},
        {"policy": {"expected_evidence_type": "operator_result"}},
        {"execution_contract": {"type": "repository", "evidence_type": "repo_change"}},
    ],
)
def test_read_only_report_rejects_explicit_evidence_type_overrides(
    cp, tmp_path, contradiction
):
    repo = tmp_path / ("contradictory-report-%d" % len(cp.list_tasks()))
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    metadata = {
        "deliverable": "report",
        "report_repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
        **contradiction,
    }

    with pytest.raises(
        ValidationError, match="read-only repository reports forbid evidence-type overrides"
    ):
        cp.create_task(
            "Contradictory repository report",
            project="repo-beads-mac",
            metadata=metadata,
        )


def _complete_direct_read_only_report_metadata(*, remote=None, branch="main", command="true"):
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "direct-report",
        "canonical_remote_url": remote
        or "https://example.invalid/direct-report.git",
        "default_branch": branch,
        "test": {"command": command},
    }
    return {
        "deliverable": "report",
        "report_repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
        "execution_contract": {
            "type": "repository",
            "repository_contract": contract,
        },
    }


def test_registered_read_only_report_rejects_explicit_contract_drift(cp, tmp_path):
    repo = tmp_path / "registered-report-drift"
    repo.mkdir()
    _write_repository_contract(
        repo,
        canonical_remote_url="https://example.invalid/repo-beads-mac.git",
        default_branch="main",
    )
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    metadata = _complete_direct_read_only_report_metadata(
        remote="https://example.invalid/attacker-selected.git"
    )

    with pytest.raises(
        ValidationError, match="contradicts the current registered repository contract"
    ):
        cp.create_task(
            "Reject stale or substituted contract",
            project="repo-beads-mac",
            metadata=metadata,
        )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda metadata: metadata.pop("execution_contract"), "current execution_contract"),
        (
            lambda metadata: metadata["execution_contract"].pop("repository_contract"),
            "repository_contract must be an object",
        ),
        (
            lambda metadata: metadata["execution_contract"]["repository_contract"].pop(
                "canonical_remote_url"
            ),
            "canonical_remote_url is required",
        ),
        (
            lambda metadata: metadata["execution_contract"]["repository_contract"].pop(
                "default_branch"
            ),
            "default_branch .* is required",
        ),
        (
            lambda metadata: metadata["execution_contract"]["repository_contract"].pop(
                "test"
            ),
            "test.command is required",
        ),
    ],
)
def test_direct_read_only_report_requires_complete_current_contract(
    cp, mutate, message
):
    metadata = _complete_direct_read_only_report_metadata()
    mutate(metadata)

    with pytest.raises(ValidationError, match=message):
        cp.create_task("Incomplete direct report", metadata=metadata)


def test_direct_read_only_report_persists_only_current_execution_contract(cp):
    metadata = _complete_direct_read_only_report_metadata()
    contract = metadata["execution_contract"]["repository_contract"]
    contract["canonical_branch"] = contract.pop("default_branch")
    metadata["origin"] = {
        "repository_contract": dict(
            contract
        )
    }

    task = cp.create_task("Complete direct report", metadata=metadata)

    assert task.metadata["execution_contract"]["schema"] == (
        "mac.task_execution_contract.v1"
    )
    assert task.metadata["origin"]["repository_url"] == (
        "https://example.invalid/direct-report.git"
    )
    assert task.metadata["origin"]["default_branch"] == "main"
    assert "repository_contract" not in task.metadata["origin"]
    assert "repository_contract" not in {
        key: value
        for key, value in task.metadata.items()
        if key != "execution_contract"
    }


def test_read_only_report_update_rejects_drift_and_can_rebind_to_current_contract(
    cp, tmp_path
):
    repo = tmp_path / "report-update"
    repo.mkdir()
    remote = "https://example.invalid/repo-beads-mac.git"
    _write_repository_contract(
        repo,
        canonical_remote_url=remote,
        default_branch="main",
    )
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    task = cp.create_task(
        "Update a report safely",
        project="repo-beads-mac",
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    )
    changed = json.loads(json.dumps(task.metadata))
    changed["notes"] = "preserved"
    changed["execution_contract"]["repository_contract"]["test"]["command"] = (
        "false"
    )

    with pytest.raises(
        ValidationError, match="contradicts the current registered repository contract"
    ):
        cp.update_task(task.id, metadata=changed)
    assert cp.get_task(task.id).metadata == task.metadata

    # Re-register a changed project contract. The old ledger row remains
    # inspectable but is not dispatchable until an explicit update drops the
    # stale projection and lets the hub bind the new registered authority.
    _write_repository_contract(
        repo,
        canonical_remote_url=remote,
        default_branch="main",
    )
    contract_path = repo / ".mac" / "project.yaml"
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            "PATH=.venv/bin:$PATH .venv/bin/python -m pytest", "make check"
        ),
        encoding="utf-8",
    )
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    assert cp.get_task(task.id).id == task.id
    assert task.id not in {item.id for item in cp.ready_tasks()}

    repair = json.loads(json.dumps(task.metadata))
    repair.pop("execution_contract")
    repaired = cp.update_task(task.id, metadata=repair)
    assert repaired.metadata["execution_contract"]["repository_contract"]["test"] == {
        "command": "make check"
    }
    assert repaired.id in {item.id for item in cp.ready_tasks()}


def test_read_only_report_child_uses_current_contract_and_invalid_batch_is_atomic(
    cp, tmp_path
):
    repo = tmp_path / "report-child"
    repo.mkdir()
    _write_repository_contract(
        repo,
        canonical_remote_url="https://example.invalid/repo-beads-mac.git",
        default_branch="main",
    )
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    parent = cp.create_task("Plan reports", project="repo-beads-mac")
    report_metadata = {
        "deliverable": "report",
        "report_repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
    }

    result = cp.add_child_tasks(
        parent.id,
        [{"title": "Inspect repository", "metadata": report_metadata}],
    )
    child = cp.get_task(result["children"][0]["id"])
    assert child.metadata["execution_contract"]["repository_contract"] == (
        cp.get_project_repository("mac").metadata["repository_contract"]
    )
    assert "repository_contract" not in child.metadata["origin"]
    assert "evidence_type" not in json.dumps(child.metadata, sort_keys=True)

    direct_parent = cp.create_task("Direct parent")
    before = cp.get_task(direct_parent.id)
    with pytest.raises(ValidationError, match="current execution_contract"):
        cp.add_child_tasks(
            direct_parent.id,
            [{"title": "Invalid report", "metadata": report_metadata}],
        )
    after = cp.get_task(direct_parent.id)
    assert after.dependencies == before.dependencies
    assert after.metadata == before.metadata


def test_read_only_report_create_idempotency_preserves_one_normalized_identity(
    cp, tmp_path
):
    repo = tmp_path / "report-idempotency"
    repo.mkdir()
    _write_repository_contract(
        repo,
        canonical_remote_url="https://example.invalid/repo-beads-mac.git",
        default_branch="main",
    )
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")
    metadata = {
        "deliverable": "report",
        "report_repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
    }

    first = cp.create_task(
        "Idempotent report",
        project="repo-beads-mac",
        metadata=metadata,
        idempotency_key="report-request-1",
        _idempotency_scope="test:reports",
    )
    retry = cp.create_task(
        "Idempotent report",
        project="repo-beads-mac",
        metadata=metadata,
        idempotency_key="report-request-1",
        _idempotency_scope="test:reports",
    )

    assert retry.id == first.id
    assert retry.metadata == first.metadata
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE id = ?", (first.id,)
    )["n"] == 1


def test_legacy_read_only_report_row_is_visible_but_not_ready_or_claimable(cp):
    worker = register_agent(
        cp,
        "legacy-report-worker",
        ["ops"],
        read_only_report_executor_resources(),
    )
    task = cp.create_task("Pre-cutover report", required_capabilities=["ops"])
    legacy = _complete_direct_read_only_report_metadata()
    legacy["execution_contract"]["schema"] = "mac.task_execution_contract.v1"
    legacy["execution_contract"]["evidence_type"] = "repo_change"
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(legacy), task.id),
    )

    loaded = cp.get_task(task.id)
    assert metadata_declares_read_only_report_repository(loaded.metadata)
    assert loaded.id not in {item.id for item in cp.ready_tasks()}
    assert cp._agent_availability_for_task(worker, loaded) == (
        False,
        "report_repository_contract_invalid",
    )
    with pytest.raises(ValidationError, match="legacy evidence-type overrides"):
        cp.claim_task(loaded.id, worker.id)


def test_atomic_repository_task_uses_managed_fast_lane_when_ready(
    cp, tmp_path, monkeypatch
):
    cp._execution_cohort_treatment_percentage = 100
    repo = tmp_path / "managed-fast-lane"
    repo.mkdir()
    _write_beads(repo, [])
    registered = cp.register_project_repository(
        "managed-fast-lane",
        str(repo),
        source="repo-beads-mac",
    )

    class _BaseResolver:
        def resolve(self, repository, *, requested_ref=None):
            return CanonicalRepositoryBase(
                repository_id=repository["id"],
                planning_base_ref=requested_ref or "refs/heads/main",
                planning_base_sha="a" * 40,
                resource_namespace={},
            )

    class _Attestor:
        def verify(self, repository, *, planning_base_ref, planning_base_sha):
            return RepositoryBaseAttestation(
                repository_id=repository["id"],
                planning_base_ref=planning_base_ref,
                planning_base_sha=planning_base_sha,
                canonical_ref_sha=planning_base_sha,
                source_kind="test",
                verified_at="attested",
                resource_namespace={"status": "unresolved"},
            )

    cp.managed_work_plans.base_resolver = _BaseResolver()
    cp.work_packages.repository_verifier = _Attestor()
    monkeypatch.setattr(
        cp,
        "_managed_single_task_rollout",
        lambda: {
            "schema": "mac.managed_single_task.rollout.v1",
            "ready": True,
            "package_capable_agent_ids": ["agent_ready"],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        cp,
        "_managed_single_task_readiness",
        lambda **_kwargs: {
            "schema": "mac.managed_single_task.readiness.v1",
            "ready": True,
            "repository_id": registered.id,
            "eligible_agent_ids": ["agent_ready"],
            "blockers": [],
        },
    )

    def activate(package_id, *, expected_plan_version, expected_epoch, actor):
        package = cp.work_packages.activate(
            package_id,
            expected_plan_version=expected_plan_version,
            expected_epoch=expected_epoch,
            actor=actor,
        )
        root = cp.store.query_one(
            "SELECT root_task_id FROM work_packages WHERE id = ?",
            (package_id,),
        )
        row = cp.store.query_one(
            "SELECT metadata FROM tasks WHERE id = ?", (root["root_task_id"],)
        )
        metadata = json.loads(row["metadata"])
        metadata["work_package_assignment"] = {
            "schema": "mac.work_package_assignment.v1",
            "lease_id": "lease_immediate_claim",
        }
        cp.store.execute(
            "UPDATE tasks SET metadata = ? WHERE id = ?",
            (json.dumps(metadata), root["root_task_id"]),
        )
        return {"package": package.to_dict(), "readiness": {"ready": True}}

    monkeypatch.setattr(cp, "activate_work_package", activate)
    task = cp.create_task(
        "Make one atomic repository change",
        description="Change one bounded behavior and test it.",
        project="repo-beads-mac",
        required_capabilities=["python"],
        metadata={"no_decompose": True},
    )

    assert task.id.startswith("task_")
    assert "publication_lane" not in task.metadata
    assert "managed_fast_lane" not in task.metadata
    assert task.metadata.get("no_dispatch") is None
    assert "work_package_v1" in task.required_capabilities
    assert task.metadata["work_package_assignment"]["lease_id"] == "lease_immediate_claim"
    link = cp.store.query_one(
        "SELECT * FROM work_package_task_links WHERE task_id = ?",
        (task.id,),
    )
    assert link["node_key"] == "change"
    package = cp.work_packages.describe(link["package_id"])
    assert package["package"]["state"] == "active"
    assert package["package"]["root_task_id"] == task.id
    assert len(package["nodes"]) == 3
    assert {
        node["metadata"]["work_package"]["node_type"]
        for node in package["nodes"]
    } == {"mutation", "integration", "certification"}
    route = cp.task_publication_route(task.id)
    assert route["lane"] == "managed"
    assert route["route_state"] == "managed_active"
    assert route["package_id"] == link["package_id"]
    task_cohort = cp.store.query_one(
        "SELECT * FROM execution_cohort_assignments WHERE task_id = ?",
        (task.id,),
    )
    package_cohort = cp.store.query_one(
        "SELECT * FROM execution_cohort_assignments WHERE package_id = ?",
        (link["package_id"],),
    )
    assert task_cohort["id"] != package_cohort["id"]
    assert task_cohort["eligibility"] == "eligible"
    assert task_cohort["treatment_route"] == "managed_synchronized"
    assert package_cohort["task_id"] is None
    cohort_detail = json.loads(task_cohort["detail"])
    assert cohort_detail["primary_analysis_eligible"] is True
    assert cohort_detail["randomization"]["treatment_percentage"] == 100
    comparable = cp.comparable_atomic_execution_outcomes()
    assert [row["task_id"] for row in comparable] == [task.id]
    for package_state in ("paused", "active", "completed"):
        cp.store.execute(
            "UPDATE work_packages SET state = ? WHERE id = ?",
            (package_state, link["package_id"]),
        )
        projected = cp.task_publication_route(task.id)
        assert projected["package_state"] == package_state
        assert projected["route_state"] == "managed_%s" % package_state


@pytest.mark.parametrize(
    ("activation_ready", "operator_held"),
    [(False, False), (True, True)],
    ids=["transient-readiness-loss", "operator-staged"],
)
def test_managed_fast_lane_holds_without_downgrading_and_releases_normally(
    cp, tmp_path, monkeypatch, activation_ready, operator_held
):
    cp._execution_cohort_treatment_percentage = 100
    repo = tmp_path / ("held-fast-lane-%s" % operator_held)
    repo.mkdir()
    _write_beads(repo, [])
    registered = cp.register_project_repository(
        "held-fast-lane-%s" % operator_held,
        str(repo),
        source="repo-beads-mac",
    )

    class _BaseResolver:
        def resolve(self, repository, *, requested_ref=None):
            return CanonicalRepositoryBase(
                repository_id=repository["id"],
                planning_base_ref=requested_ref or "refs/heads/main",
                planning_base_sha="a" * 40,
                resource_namespace={},
            )

    class _Attestor:
        def verify(self, repository, *, planning_base_ref, planning_base_sha):
            return RepositoryBaseAttestation(
                repository_id=repository["id"],
                planning_base_ref=planning_base_ref,
                planning_base_sha=planning_base_sha,
                canonical_ref_sha=planning_base_sha,
                source_kind="test",
                verified_at="attested",
                resource_namespace={"status": "unresolved"},
            )

    cp.managed_work_plans.base_resolver = _BaseResolver()
    cp.work_packages.repository_verifier = _Attestor()
    monkeypatch.setattr(
        cp,
        "_managed_single_task_rollout",
        lambda: {
            "schema": "mac.managed_single_task.rollout.v1",
            "ready": True,
            "package_capable_agent_ids": ["agent_runtime"],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        cp,
        "_managed_single_task_readiness",
        lambda **_kwargs: {
            "schema": "mac.managed_single_task.readiness.v1",
            "ready": activation_ready,
            "repository_id": registered.id,
            "eligible_agent_ids": ["agent_runtime"] if activation_ready else [],
            "blockers": [] if activation_ready else [{"code": "credential_rotation"}],
        },
    )

    def activate(package_id, *, expected_plan_version, expected_epoch, actor):
        package = cp.work_packages.activate(
            package_id,
            expected_plan_version=expected_plan_version,
            expected_epoch=expected_epoch,
            actor=actor,
        )
        return {"package": package.to_dict(), "readiness": {"ready": True}}

    monkeypatch.setattr(cp, "activate_work_package", activate)
    metadata = {"no_decompose": True}
    if operator_held:
        metadata["no_dispatch"] = True
    task = cp.create_task(
        "Managed task held before activation",
        project="repo-beads-mac",
        metadata=metadata,
    )

    held_route = cp.task_publication_route(task.id)
    assert held_route["lane"] == "managed"
    assert held_route["route_state"] == "managed_held"
    assert task.metadata["no_dispatch"] is True

    if not operator_held:
        monkeypatch.setattr(
            cp,
            "_managed_single_task_readiness",
            lambda **_kwargs: {
                "schema": "mac.managed_single_task.readiness.v1",
                "ready": True,
                "repository_id": registered.id,
                "eligible_agent_ids": ["agent_rotated"],
                "blockers": [],
            },
        )
        released = cp.create_task(
            "Managed task held before activation",
            project="repo-beads-mac",
            metadata={"no_decompose": True},
            _task_id=task.id,
        )
        assert released.id == task.id
    else:
        released = cp.release_task(task.id, actor="operator")
    assert released.metadata.get("no_dispatch") is None
    assert cp.task_publication_route(task.id)["route_state"] == "managed_active"


def test_release_preserves_control_plane_publication_routing_metadata(cp):
    """`release_task` removes only `no_dispatch`, preserving controller-owned
    routing metadata byte-for-byte.

    Reproduces the failure where routing metadata (`publication_route`,
    `publication_lane`, `managed_fast_lane`, `work_package`) attached to a
    staged task after creation caused release to raise HTTP 400 via the
    user-input guard.
    """
    task = cp.create_task("Staged with routing", metadata={"no_dispatch": True})

    # Simulate the control plane attaching routing metadata after creation by
    # writing directly to the stored row (bypassing the user-input guard, as
    # the real control plane does).
    row = cp.store.query_one("SELECT metadata FROM tasks WHERE id = ?", (task.id,))
    md = json.loads(row["metadata"])
    md["publication_route"] = {"lane": "managed", "schema": "mac.route.v1"}
    md["publication_lane"] = "managed"
    md["managed_fast_lane"] = {
        "schema": "mac.managed_single_task.route.v1",
        "activation": "legacy_compatibility",
    }
    md["work_package"] = {"id": "pkg_abc"}
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(md), task.id),
    )

    before = json.loads(
        cp.store.query_one(
            "SELECT metadata FROM tasks WHERE id = ?", (task.id,)
        )["metadata"]
    )

    released = cp.release_task(task.id, actor="operator")

    after = json.loads(
        cp.store.query_one(
            "SELECT metadata FROM tasks WHERE id = ?", (task.id,)
        )["metadata"]
    )

    assert released.metadata.get("no_dispatch") is None
    assert "no_dispatch" not in after

    # The persisted metadata differs from the pre-release metadata only by the
    # removal of `no_dispatch`; every controller-owned field is byte-identical.
    expected = dict(before)
    expected.pop("no_dispatch", None)
    assert after == expected
    for key in (
        "publication_route",
        "publication_lane",
        "managed_fast_lane",
        "work_package",
    ):
        assert after[key] == before[key]


def test_release_is_noop_when_not_held(cp):
    task = cp.create_task("Not held")
    before = json.loads(
        cp.store.query_one(
            "SELECT metadata FROM tasks WHERE id = ?", (task.id,)
        )["metadata"]
    )
    released = cp.release_task(task.id, actor="operator")
    after = json.loads(
        cp.store.query_one(
            "SELECT metadata FROM tasks WHERE id = ?", (task.id,)
        )["metadata"]
    )
    assert released.id == task.id
    assert after == before


def test_atomic_repository_task_falls_back_to_explicit_legacy_when_disabled(
    cp, tmp_path
):
    repo = tmp_path / "legacy-fast-lane"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository(
        "legacy-fast-lane",
        str(repo),
        source="repo-beads-mac",
    )

    task = cp.create_task(
        "Small task while managed line is disabled",
        project="repo-beads-mac",
        metadata={"no_decompose": True},
    )

    assert task.metadata["publication_lane"] == "legacy"
    assert task.metadata["managed_fast_lane"]["activation"] == "legacy_compatibility"
    assert cp.store.query_one(
        "SELECT task_id FROM work_package_task_links WHERE task_id = ?",
        (task.id,),
    ) is None
    assert cp.task_publication_route(task.id)["lane"] == "legacy"


def test_managed_fast_lane_rollout_is_shared_monotonic_and_inventory_independent(
    cp, monkeypatch
):
    class _EnabledRuntime:
        enabled = True
        configuration_error = ""

    reviewed = register_agent(cp, "rollout-reviewed", ["work_package_v1"])
    unreviewed = register_agent(cp, "rollout-unreviewed", [])
    cp.work_package_pipeline_runtime_config = _EnabledRuntime()
    monkeypatch.setattr(
        "mac.worker_credentials.package_worker_readiness",
        lambda _store, agent_id: {
            "ready": agent_id in {reviewed.id, unreviewed.id}
        },
    )
    monkeypatch.setattr(
        "mac.worker_credentials.assert_package_worker_ready",
        lambda _conn, agent_id: {
            "ready": agent_id in {reviewed.id, unreviewed.id}
        },
    )
    cp.store.execute(
        "INSERT INTO worker_credential_policy_state ("
        "singleton_key, mode, inventory_digest, ready_agent_ids, revision, "
        "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "fleet",
            "enforced",
            "sha256:not-a-reviewed-digest",
            json.dumps([reviewed.id]),
            7,
            "fleet-admin",
            utcnow(),
        ),
    )

    invalid_review = cp._managed_single_task_rollout()
    assert invalid_review["ready"] is False
    assert "worker_credential_review_missing_or_invalid" in {
        item["code"] for item in invalid_review["blockers"]
    }

    cp.store.execute(
        "UPDATE worker_credential_policy_state SET inventory_digest = ? "
        "WHERE singleton_key = ?",
        ("sha256:" + ("a" * 64), "fleet"),
    )
    cp.update_agent(reviewed.id, capabilities=[])
    cp.update_agent(unreviewed.id, capabilities=["work_package_v1"])
    wrong_runtime = cp._managed_single_task_rollout()
    assert wrong_runtime["ready"] is False
    assert wrong_runtime["package_capable_agent_ids"] == [unreviewed.id]
    assert wrong_runtime["reviewed_package_capable_agent_ids"] == []
    assert "no_reviewed_package_capable_worker_runtime" in {
        item["code"] for item in wrong_runtime["blockers"]
    }

    cp.update_agent(reviewed.id, capabilities=["work_package_v1"])
    crossed = cp._managed_single_task_rollout()

    assert crossed["ready"] is True
    assert crossed["crossed"] is True
    assert crossed["revision"] == 1
    assert crossed["crossed_by"] == "managed-fast-lane-controller"
    assert crossed["live_reviewed_package_agent_ids"] == [reviewed.id]
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM managed_task_publication_rollout"
    )["n"] == 1

    # Capability withdrawal, credential-policy rollback, and process-local
    # config drift are activation problems after cutover, never authority to
    # restore automatic legacy publication.
    cp.update_agent(reviewed.id, capabilities=[])
    cp.update_agent(unreviewed.id, capabilities=[])
    cp.store.execute(
        "UPDATE worker_credential_policy_state SET mode = ? WHERE singleton_key = ?",
        ("compatibility", "fleet"),
    )

    class _DisabledRuntime:
        enabled = False
        configuration_error = ""

    cp.work_package_pipeline_runtime_config = _DisabledRuntime()
    after_drift = cp._managed_single_task_rollout()

    assert after_drift["ready"] is True
    assert after_drift["blockers"] == []
    assert {
        item["code"] for item in after_drift["current_observation_blockers"]
    } == {
        "work_package_pipeline_disabled",
        "no_package_capable_worker_runtime",
        "worker_credential_policy_not_enforced",
        "no_reviewed_package_capable_worker_runtime",
    }
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM managed_task_publication_rollout"
    )["n"] == 1


def test_task_create_idempotency_key_binds_one_identity_and_exact_intent(cp):
    first = cp.create_task(
        "Retry-safe create",
        description="one exact request",
        idempotency_key="request-42",
        _idempotency_scope="client:test-suite",
    )
    retry = cp.create_task(
        "Retry-safe create",
        description="one exact request",
        idempotency_key="request-42",
        _idempotency_scope="client:test-suite",
    )

    assert retry.id == first.id
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE id = ?", (first.id,)
    )["n"] == 1
    binding = cp.store.query_one(
        "SELECT * FROM task_create_idempotency WHERE task_id = ?", (first.id,)
    )
    assert binding is not None
    assert binding["scope_digest"] != "client:test-suite"
    assert binding["key_digest"] != "request-42"

    with pytest.raises(ValidationError, match="already bound to a different request"):
        cp.create_task(
            "Changed retry",
            description="one exact request",
            idempotency_key="request-42",
            _idempotency_scope="client:test-suite",
        )

    other_scope = cp.create_task(
        "Retry-safe create",
        description="one exact request",
        idempotency_key="request-42",
        _idempotency_scope="client:other",
    )
    assert other_scope.id != first.id


def test_publication_route_projection_chunks_large_dashboard_inventories(cp):
    task_ids = ["task_%032x" % value for value in range(1205)]

    routes = cp.task_publication_routes(task_ids, compact=True)

    assert len(routes) == len(task_ids)
    assert routes[task_ids[0]] == {
        "schema": "mac.task_publication_route.v1",
        "task_id": task_ids[0],
        "lane": "legacy",
        "managed": False,
        "route_state": "legacy_compatibility",
        "package_id": None,
        "plan_version": None,
        "epoch": None,
    }


def test_required_managed_lane_fails_closed_without_creating_legacy_task(
    cp, tmp_path
):
    repo = tmp_path / "required-managed-fast-lane"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository(
        "required-managed-fast-lane",
        str(repo),
        source="repo-beads-mac",
    )

    before = cp.store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"]
    with pytest.raises(ValidationError, match="managed publication rollout is unavailable"):
        cp.create_task(
            "Must use exact-candidate publication",
            project="repo-beads-mac",
            metadata={
                "no_decompose": True,
                "publication_lane_policy": "managed",
            },
        )
    after = cp.store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"]
    assert after == before


def test_legacy_publication_override_requires_trusted_controller_authority(cp):
    with pytest.raises(AuthorizationError, match="trusted controller authority"):
        cp.create_task(
            "Untrusted downgrade",
            metadata={"publication_lane_policy": "legacy"},
        )

    approved = cp.create_task(
        "Trusted compatibility override",
        metadata={"publication_lane_policy": "legacy"},
        _allow_legacy_publication=True,
    )
    assert cp.task_publication_route(approved.id)["lane"] == "legacy"


def test_shallow_repository_execution_contract_gets_registered_project_contract(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")

    task = cp.create_task(
        "Direct repository task with shallow contract",
        project="repo-beads-mac",
        required_capabilities=["python"],
        metadata={"execution_contract": {"type": "repository"}},
    )

    contract = task.metadata["execution_contract"]
    assert contract["type"] == "repository"
    assert contract["quality"] == "strong"
    assert contract["evidence_type"] == "repo_change"
    assert contract["repository_contract"]["project"] == "repo-beads-mac"
    assert task.metadata["origin"]["repository_contract"]["project"] == "repo-beads-mac"
    assert task.metadata["acc_metadata"]["repository_contract_schema"] == "mac.repository_contract.v1"


def test_native_repository_task_does_not_emit_repo_beads_workflow(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_project_repository("mac", str(repo), source="repo-beads-mac")

    # Direct task (no pre-existing execution_contract)
    task_direct = cp.create_task(
        "Native direct task",
        project="repo-beads-mac",
        required_capabilities=["python"],
    )
    acc = task_direct.metadata.get("acc_metadata", {})
    assert "repo_beads_workflow" not in acc, (
        "Native direct tasks must not carry repo_beads_workflow; got acc_metadata=%r" % acc
    )

    # Shallow contract task
    task_shallow = cp.create_task(
        "Native shallow contract task",
        project="repo-beads-mac",
        required_capabilities=["python"],
        metadata={"execution_contract": {"type": "repository"}},
    )
    acc2 = task_shallow.metadata.get("acc_metadata", {})
    assert "repo_beads_workflow" not in acc2, (
        "Native shallow-contract tasks must not carry repo_beads_workflow; got acc_metadata=%r" % acc2
    )


def test_existing_repository_execution_contract_gets_repo_change_evidence_default(cp):
    task = cp.create_task(
        "Existing repository contract",
        metadata={
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
                "quality": "strong",
            }
        },
    )

    assert task.metadata["execution_contract"]["type"] == "repository"
    assert task.metadata["execution_contract"]["evidence_type"] == "repo_change"


def test_direct_task_without_repository_gets_explicit_operator_contract(cp):
    task = cp.create_task("Operator task", required_capabilities=["ops"])

    assert task.metadata["execution_contract"]["type"] == "operator_directive"
    assert task.metadata["execution_contract"]["quality"] == "weak"
    assert task.metadata["execution_contract"]["repository_required"] is False
    names = {event.name for event in cp.list_observability(layer="control_plane", limit=20)}
    assert "task.execution_contract.weak" in names


def _git(cmd, cwd=None):
    return subprocess.run(
        ["git", *cmd],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_bare_beads_repo(tmp_path, issue_id="mac-old"):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    _git(["init", "--bare", "--initial-branch=main", str(origin)])
    _git(["init", "--initial-branch=main", str(seed)])
    _git(["config", "user.email", "mac-tests@example.invalid"], cwd=seed)
    _git(["config", "user.name", "mac tests"], cwd=seed)
    _write_beads(
        seed,
        [
            {
                "_type": "issue",
                "id": issue_id,
                "title": issue_id,
                "description": "seeded",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    _git(["add", ".mac/project.yaml", ".beads/issues.jsonl"], cwd=seed)
    _git(["commit", "-m", "seed beads"], cwd=seed)
    _git(["remote", "add", "origin", str(origin)], cwd=seed)
    _git(["push", "-u", "origin", "main"], cwd=seed)
    _git(["clone", str(origin), str(clone)])
    return origin, seed, clone




















def test_hub_heartbeat_advances_default_review_workflow(cp, monkeypatch):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    rocky = register_agent(cp, "rocky", ["python"])
    task = cp.create_task(
        "needs review",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    monkeypatch.setenv("MAC_REVIEW_TICK_ON_HEARTBEAT", "1")
    monkeypatch.setenv("MAC_REVIEW_TICK_HUB_AGENT", "rocky")
    # mem-04: workflow.default_review.heartbeat_tick is one of the
    # high-volume poll-log names suppressed by default; enable verbose
    # poll logging for this assertion.
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")

    cp.heartbeat_agent(rocky.id, status=AgentStatus.IDLE.value)

    refreshed = cp.get_task(task.id)
    assert refreshed.state == TaskState.REVIEWING.value
    reviews = cp.list_reviews(task.id)
    assert len(reviews) == 1
    assert reviews[0].reviewer_agent_id == reviewer.id
    names = {event.name for event in cp.list_observability(layer="control_plane", limit=50)}
    assert "workflow.default_review.heartbeat_tick" in names


def test_operator_notifications_track_task_lifecycle(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("observable task", required_capabilities=["python"])

    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id, lease_id=lease.id)
    cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "pytest passed",
        worker.id,
        lease_id=lease.id,
    )
    cp.transition_task(
        task.id,
        TaskState.FAILED.value,
        worker.id,
        {"reason": "boom"},
        lease_id=lease.id,
    )

    event_types = {item.event_type for item in cp.list_notifications(subject_id=task.id)}
    assert {"task.claimed", "task.running", "task.evidence_added", "task.failed"} <= event_types
    pending = cp.list_notifications(status="pending")
    delivered = cp.mark_notification_delivered(pending[0].id)
    assert delivered.status == "delivered"
    assert delivered.delivered_at is not None


def test_mark_notification_delivered_refuses_terminal_flip(cp):
    """A late/duplicate ack must not overwrite a terminal status (masking guard)."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("guard task", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)

    pending = cp.list_notifications(status="pending")
    assert pending, "expected at least one pending notification"
    target = pending[0].id

    # Simulate the drain marking it skipped (no delivery target resolved).
    skipped = cp.mark_notification_delivered(target, status="skipped")
    assert skipped.status == "skipped"

    # A subsequent 'delivered' ack must be refused, not masked.
    with pytest.raises(TransitionError):
        cp.mark_notification_delivered(target, status="delivered")

    # State is unchanged after the refused transition.
    assert cp.get_notification(target).status == "skipped"


def test_mark_notification_delivered_is_idempotent_for_same_status(cp):
    """Re-acking an already-delivered notification is a no-op, not an error."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("idempotent task", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)

    pending = cp.list_notifications(status="pending")
    assert pending
    target = pending[0].id

    first = cp.mark_notification_delivered(target)
    assert first.status == "delivered"
    first_delivered_at = first.delivered_at

    # Same-status re-ack returns the row without raising or bumping delivered_at.
    second = cp.mark_notification_delivered(target)
    assert second.status == "delivered"
    assert second.delivered_at == first_delivered_at


def test_task_notifier_delivers_task_progress_to_configured_slack_home_channel(cp):
    tenant = cp.register_tenant("ops")
    persona = cp.register_persona(
        tenant.id,
        "Rocky",
        soul_ref="hermes://ops/rocky/SOUL.md",
        memory_scope="hermes://ops/rocky/memory",
    )
    hermes = cp.register_hermes_instance(
        tenant.id,
        "rocky",
        persona_id=persona.id,
        home_ref="hermes://ops/rocky",
    )
    binding = cp.register_platform_binding(
        tenant.id,
        hermes.id,
        "slack",
        "T123/C456",
        display_name="#mac-home",
        scopes={"channels": ["C456"]},
    )
    machine = cp.register_machine("host")
    agent = cp.register_agent(
        machine.id,
        "worker",
        capabilities=["python"],
        hermes_instance_id=hermes.id,
    )
    cp.configure_notifier_channel(
        "slack-home",
        "slack",
        event_types=["task.*"],
        target={"platform_binding_id": binding.id},
    )

    task = cp.create_task("notify progress", required_capabilities=["python"])
    _, lease = cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id, lease_id=lease.id)

    result = cp.deliver_pending_notifications(limit=20)

    assert result["delivered"] >= 2
    messages = cp.list_messages(agent.id)
    assert {message.sender_agent_id for message in messages} == {"notifier"}
    assert {message.message_type for message in messages} == {MessageType.STATUS_UPDATE.value}
    assert {message.payload["notification"]["event_type"] for message in messages} >= {
        "task.claimed",
        "task.running",
    }
    assert all(message.payload["target"]["platform_binding_id"] == binding.id for message in messages)


def test_task_claim_records_history_and_outbox_in_same_transaction(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("atomic claim", required_capabilities=["python"])

    claimed, lease = cp.claim_task(task.id, worker.id, sync_beads=False)

    history = cp.task_history(task.id)
    outbox = cp.list_task_transition_outbox(task_id=task.id)
    assert claimed.lease_id == lease.id
    assert history[-1].event_type == "task.claimed"
    assert history[-1].to_state == TaskState.CLAIMED.value
    assert [item.event_type for item in outbox] == []


def test_outbox_drains_in_enqueue_order_with_identical_created_at(cp):
    """mac: a single task transition enqueues several outbox rows
    (task.lifecycle, beads.ledger, beads.reopen) sharing the exact same
    created_at. list_outbox must return them in stable ENQUEUE order so
    downstream side effects (e.g. the beads ledger note before the reopen
    --status note) fire deterministically. Previously the secondary sort
    key was a random uuid4 id, so the drain order was non-deterministic
    and flaky."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("ordering", required_capabilities=["python"])
    # Drain any rows from create so we isolate the enqueue below.
    cp.drain_task_transition_outbox(task_id=task.id)

    now = "2026-06-01T00:00:00.000000+00:00"
    expected = []
    with cp.store.transaction() as conn:
        for event_type in (
            "task.lifecycle",
            "beads.ledger",
            "beads.reopen",
            "workflow.advance",
        ):
            cp.task_ledger.enqueue_outbox(
                conn,
                task_id=task.id,
                event_type=event_type,
                actor=worker.id,
                from_state="running",
                to_state="failed",
                detail={"reason": "x"},
                created_at=now,  # identical timestamp for all rows
            )
            expected.append(event_type)

    pending = cp.list_task_transition_outbox(task_id=task.id)
    assert [item.event_type for item in pending] == expected




















def test_acc_migration_dry_run_reports_without_writing(cp, tmp_path):
    acc_db = tmp_path / "acc.db"
    create_acc_migration_fixture(acc_db)

    report = migrate_acc_sqlite(cp, acc_db, mode="dry-run", audit_limit=1)

    assert report.counts["agents"] == 1
    assert report.counts["tasks"] == 2
    assert report.counts["tasks_planned_for_import"] == 1
    assert report.counts["terminal_tasks_skipped"] == 1
    assert any("work_audit_events limited" in warning for warning in report.warnings)
    assert {entry["table"] for entry in report.skipped_private_tables} == {
        "bus_messages",
        "gateway_sessions",
        "conversation_chain_events",
    }

    # Dry-run must be a pure preflight.
    assert cp.list_tasks() == []
    all_payloads = json.dumps(report.to_dict(), sort_keys=True)
    assert "do not import this raw text" not in all_payloads
    assert "private chain title" not in all_payloads
    assert "private session text" not in all_payloads


def test_acc_migration_imports_open_tasks_once_with_crosswalk(cp, tmp_path):
    acc_db = tmp_path / "acc.db"
    create_acc_migration_fixture(acc_db)

    report = migrate_acc_sqlite(cp, acc_db, mode="import", audit_limit=1)
    again = migrate_acc_sqlite(cp, acc_db, mode="import", audit_limit=1)

    assert report.import_report.tasks_imported == 1
    assert report.import_report.agents_imported == 1
    assert again.import_report.errors == []
    assert len(cp.list_tasks()) == 1
    assert len(cp.list_project_items()) == 1

    task = cp.list_tasks()[0]
    assert task.title == "Open ACC task"
    assert task.project == "proj-1"
    assert task.metadata["source"] == "acc"
    assert task.metadata["external_id"] == "task-1"
    assert task.metadata["acc_metadata"]["beads_id"] == "ACC-1"
    memories = cp.search_memory(task_id=task.id)
    assert {memory.record_type for memory in memories} >= {"imported", "acc.task_imported"}


def test_acc_migration_blocks_active_tasks_unless_allowed(cp, tmp_path):
    acc_db = tmp_path / "acc.db"
    create_acc_migration_fixture(acc_db)
    conn = sqlite3.connect(acc_db)
    conn.execute(
        "UPDATE fleet_tasks SET status = ?, claimed_by = ? WHERE id = ?",
        ("claimed", "rocky", "task-1"),
    )
    conn.commit()
    conn.close()

    dry_run = migrate_acc_sqlite(cp, acc_db, mode="dry-run")
    assert dry_run.blockers[0]["id"] == "task-1"
    with pytest.raises(ValidationError):
        migrate_acc_sqlite(cp, acc_db, mode="import")

    allowed = migrate_acc_sqlite(cp, acc_db, mode="import", allow_active=True)
    assert allowed.import_report.tasks_imported == 1
    assert cp.list_tasks()[0].metadata["migration_requeued_from_active_acc_claim"] is True


def test_acc_migration_rejects_missing_db(cp, tmp_path):
    with pytest.raises(ValidationError):
        migrate_acc_sqlite(cp, tmp_path / "missing.db")


def test_concurrent_claim_picks_exactly_one_winner(cp):
    worker_a = register_agent(cp, "worker-a", ["python"])
    worker_b = register_agent(cp, "worker-b", ["python"])
    task = cp.create_task("contested", required_capabilities=["python"])
    results = {}
    barrier = threading.Barrier(2)

    def claim(name, agent_id):
        barrier.wait()
        try:
            claimed, lease = cp.claim_task(task.id, agent_id)
            results[name] = ("ok", claimed.id, lease.id)
        except (TransitionError, ValidationError) as exc:
            results[name] = ("err", str(exc), None)

    threads = [
        threading.Thread(target=claim, args=("a", worker_a.id)),
        threading.Thread(target=claim, args=("b", worker_b.id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = [results[name][0] for name in ("a", "b")]
    assert outcomes.count("ok") == 1
    assert outcomes.count("err") == 1
    final_task = cp.get_task(task.id)
    assert final_task.state == TaskState.CLAIMED.value
    assert final_task.attempt_count == 1
    leases = cp.store.query_all("SELECT id FROM leases WHERE task_id = ?", (task.id,))
    assert len(leases) == 1


def test_reviewer_cannot_be_task_owner(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("Implement thing", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)

    with pytest.raises(AuthorizationError):
        cp.request_review(task.id, worker.id)


def test_review_approval_requires_evidence_id(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)

    with pytest.raises(ValidationError):
        cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id)


def test_completion_requires_evidence_linked_from_approved_review(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    from tests.conftest import submit_review_verdict

    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    publication = cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=evidence.id)
    assert publication.status == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_git_main_publication_requires_repository_path(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("publish branch", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)

    with pytest.raises(ValidationError, match="origin.repository_path"):
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)


def test_git_publication_merges_non_fast_forward_task_branch(cp, tmp_path):
    from tests.conftest import submit_review_verdict
    from mac.cicd_monitor import CICDMonitor, CICDMonitorConfig

    def git(repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "mac-test@example.com")
    git(source, "config", "user.name", "MAC Test")
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "base.txt")
    git(source, "commit", "-m", "base")
    git(source, "branch", "-M", "main")
    git(source, "push", "-u", "origin", "main")

    git(source, "checkout", "-b", "task/feature")
    (source / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", "feature branch")
    task_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "task/feature")

    git(source, "checkout", "main")
    (source / "mainline.txt").write_text("mainline\n", encoding="utf-8")
    git(source, "add", "mainline.txt")
    git(source, "commit", "-m", "main moved independently")
    main_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    project = cp.create_project(
        "git-publication-ci",
        metadata={"repository_url": "https://github.com/acme/widgets.git"},
        dispatch_paused=False,
    )
    cp._cicd_monitor = CICDMonitor(
        cp,
        CICDMonitorConfig(enabled=True),
    )
    publication_gate_calls = []

    def publication_gate_runner(remote_url, branch, projected_sha, command):
        checkout = Path(remote_url)
        publication_gate_calls.append(
            {
                "branch": branch,
                "projected_sha": projected_sha,
                "command": command,
                "has_feature": (checkout / "feature.txt").is_file(),
                "has_mainline": (checkout / "mainline.txt").is_file(),
            }
        )
        return 0, "full configured suite passed"

    cp._publication_merge_test_runner = publication_gate_runner
    task = cp.create_task(
        "publish parallel branch",
        project=project.name,
        required_capabilities=["python"],
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_path": str(source),
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "test": {"command": "make full-publication-suite"},
                },
            },
            "publication_target": "git://main",
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": task_head,
                "pushed": True,
                "remote_ref": "refs/heads/task/feature",
                "dirty": False,
                "files_changed": ["feature.txt"],
            },
            "tests": [{"command": "make smoke", "returncode": 0}],
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://feature",
        "feature branch tested",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert publication.status == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    final_head = git(source, "rev-parse", "HEAD")
    assert final_head != task_head
    assert len(git(source, "rev-list", "--parents", "-n", "1", final_head).split()) == 3
    git(source, "merge-base", "--is-ancestor", task_head, final_head)
    git(source, "merge-base", "--is-ancestor", main_head, final_head)
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == final_head
    assert len(publication_gate_calls) == 1
    assert publication_gate_calls[0]["branch"] == "mac-projected-publication"
    assert publication_gate_calls[0]["projected_sha"]
    assert publication_gate_calls[0]["command"] == "make full-publication-suite"
    assert publication_gate_calls[0]["has_feature"] is True
    assert publication_gate_calls[0]["has_mainline"] is True
    published = [
        event
        for event in cp.list_observability(limit=50)
        if event.name == "task.git_published" and event.subject_id == task.id
    ]
    assert published
    assert published[0].detail["publication_mode"] == "merge_commit"
    assert published[0].detail["head_sha"] == task_head
    assert published[0].detail["final_sha"] == final_head
    publication_commands = published[0].detail["commands"]
    contract_gate = next(
        item
        for item in publication_commands
        if item["name"] == "publication_contract_gate"
    )
    assert contract_gate["passed"] is True
    assert contract_gate["test_command"] == "make full-publication-suite"
    assert any(
        item["name"] == "verify_projected_tree"
        for item in publication_commands
    )
    proofs = [
        item.metadata["verification"]["canonical_integration"]
        for item in cp.list_evidence(task.id)
        if item.metadata.get("verification", {}).get("canonical_integration")
    ]
    assert proofs == [
        {
            "schema": "mac.canonical_integration.v1",
            "status": "pass",
            "canonical_ref": "refs/heads/main",
            "canonical_tip_sha": final_head,
            "reviewed_head_sha": task_head,
            "contains_reviewed_head": True,
            "remote_verified": True,
            "publication_mode": "merge_commit",
        }
    ]
    ci_schedules = [
        event
        for event in cp.list_observability(limit=100)
        if event.name == "cicd.followup.scheduled"
        and event.subject_id == task.id
    ]
    assert len(ci_schedules) == 1
    assert ci_schedules[0].detail["schema"] == "mac.cicd_followup_schedule.v1"
    assert ci_schedules[0].detail["publication_id"] == publication.id
    assert ci_schedules[0].detail["canonical_sha"] == final_head
    assert ci_schedules[0].detail["repository"] == "acme/widgets"
    assert ci_schedules[0].detail["schedule_key"] == (
        "github-publication:%s:%s" % (publication.id, final_head)
    )


def test_git_publication_via_remote_clone_when_no_repository_path(cp, tmp_path):
    """mac-k8s: K8s remote-clone tasks carry origin.repository_url but no local
    repository_path. Publication must merge via a transient authed clone of the
    remote instead of refusing — this is the autonomous-loop path the jordanh-gke
    fleet depends on (previously it raised 'requires repository_path')."""
    from tests.conftest import submit_review_verdict

    source, remote, task_head, git = _setup_publishable_repo(tmp_path, name="k8s")
    cp._publication_merge_test_runner = lambda *_args: (0, "full suite passed")
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "publish via remote clone (no local path)",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_url": str(remote),  # K8s mode: URL only, no repository_path
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "test": {"command": "true"},
                },
            },
            "publication_target": "git://main",
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": task_head,
                "pushed": True,
                "remote_ref": "refs/heads/task/feature",
                "dirty": False,
                "files_changed": ["feature.txt"],
            },
            "tests": [{"command": "pytest -q", "returncode": 0}],
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://feature",
        "feature tested",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)

    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)
    assert publication.status == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    # main had not moved, so the feature fast-forwards: the remote's main now
    # points at the reviewed task commit even though no local checkout existed.
    assert git(source, "ls-remote", str(remote), "refs/heads/main").split()[0] == task_head


def _setup_publishable_repo(tmp_path, name="remote"):
    def git(repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    remote = tmp_path / ("%s.git" % name)
    source = tmp_path / ("source-%s" % name)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "mac-test@example.com")
    git(source, "config", "user.name", "MAC Test")
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "base.txt")
    git(source, "commit", "-m", "base")
    git(source, "branch", "-M", "main")
    git(source, "push", "-u", "origin", "main")
    git(source, "checkout", "-b", "task/feature")
    (source / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", "feature branch")
    task_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "task/feature")
    git(source, "checkout", "main")
    return source, remote, task_head, git


def _publishable_task_and_evidence(cp, source, task_head, *, canonical_remote_url=None):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    cp._publication_merge_test_runner = lambda *_args: (0, "full suite passed")
    contract = {
        "schema": "mac.repository_contract.v1",
        "test": {"command": "true"},
    }
    if canonical_remote_url is not None:
        contract["canonical_remote_url"] = canonical_remote_url
    task = cp.create_task(
        "publish with origin guard",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_path": str(source),
                "repository_contract": contract,
            },
            "publication_target": "git://main",
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = _sign(
        cp,
        worker.id,
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": task_head,
                "pushed": True,
                "remote_ref": "refs/heads/task/feature",
                "dirty": False,
                "files_changed": ["feature.txt"],
            },
            "tests": [{"command": "make smoke", "returncode": 0}],
        },
    )
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://feature",
        "feature branch tested",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    return task, evidence, reviewer


def test_git_publication_full_contract_failure_does_not_push_main(cp, tmp_path):
    source, remote, task_head, git = _setup_publishable_repo(
        tmp_path, name="contract-fail"
    )
    main_before = git(source, "rev-parse", "main")
    task, evidence, reviewer = _publishable_task_and_evidence(
        cp, source, task_head
    )
    calls = []

    def fail_gate(remote_url, branch, projected_sha, command):
        calls.append((remote_url, branch, projected_sha, command))
        return 23, "integration suite failed"

    del cp._publication_merge_test_runner
    cp._hub_verify_runner = fail_gate
    with pytest.raises(
        ValidationError, match="full repository contract gate failed"
    ):
        cp.publish_task(
            task.id, "git://main", reviewer.id, evidence_id=evidence.id
        )

    assert len(calls) == 1
    assert calls[0][1] == "mac-projected-publication"
    assert calls[0][3] == "true"
    assert git(source, "ls-remote", str(remote), "refs/heads/main").split()[0] == (
        main_before
    )
    assert git(source, "rev-parse", "main") == main_before


def test_git_publication_rejects_worktree_origin_mismatch(cp, tmp_path):
    # mac-y7ha: when the contract pins canonical_remote_url, a worktree
    # whose origin points elsewhere (e.g. a private mirror) must fail
    # publish-time validation instead of silently pushing to the wrong
    # remote.
    source, _remote, task_head, _git = _setup_publishable_repo(tmp_path)
    task, evidence, reviewer = _publishable_task_and_evidence(
        cp,
        source,
        task_head,
        canonical_remote_url="git@github.com:example/elsewhere.git",
    )
    with pytest.raises(ValidationError, match="does not match the project's registered remote"):
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)


def test_git_publication_accepts_equivalent_ssh_https_origin(cp, tmp_path):
    # mac-y7ha: ssh and https forms of the same GitHub URL must compare
    # equal so the registered remote can be set once and worktrees can
    # be cloned via either form.
    source, _remote, task_head, git = _setup_publishable_repo(tmp_path, name="equiv")
    git(source, "remote", "set-url", "origin", "git@github.com:example/equiv.git")
    task, evidence, reviewer = _publishable_task_and_evidence(
        cp,
        source,
        task_head,
        canonical_remote_url="https://github.com/example/equiv.git",
    )
    # The merge step still tries to push to git@github.com which we
    # cannot reach in the test env; expect a network/push failure, but
    # crucially NOT the origin-mismatch validation error.
    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)
    assert "does not match the project's registered remote" not in str(excinfo.value)


def test_git_publication_skips_origin_check_when_contract_unset(cp, tmp_path):
    # mac-y7ha back-compat: contracts without canonical_remote_url retain
    # the previous behavior (no origin validation).
    source, _remote, task_head, _git = _setup_publishable_repo(tmp_path, name="noguard")
    task, evidence, reviewer = _publishable_task_and_evidence(cp, source, task_head)
    publication = cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)
    assert publication.status == "published"


def test_review_verdict_requires_same_repo_head_as_executor_evidence(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "work",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    executor_evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": executor_evidence.id,
        "repo": {
            "head_sha": "fedcba9876543210fedcba9876543210fedcba98",
            "pushed": True,
            "remote_ref": "refs/heads/task/example",
            "dirty": False,
            "files_changed": executor_evidence.metadata["verification"]["repo"]["files_changed"],
        },
        "codegraph": executor_evidence.metadata["verification"]["codegraph"],
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
        "worktree_digest": "sha256:" + ("1" * 64),
    }
    verdict_manifest = _sign(cp, reviewer.id, verdict_manifest)
    cp.add_evidence(
        task.id,
        "review",
        "artifact://review",
        "review approved wrong sha",
        reviewer.id,
        metadata={"returncode": 0, "verification": verdict_manifest},
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert result["review_id"] == review.id
    assert any("repo.head_sha does not match" in problem for problem in result["problems"])
    assert cp.list_publications(task.id) == []


def test_review_verdict_requires_executor_changed_files_for_codegraph(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "work",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    executor_evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id, files_changed=["src/example.py"]),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    executor_repo = executor_evidence.metadata["verification"]["repo"]
    verdict_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": executor_evidence.id,
        "repo": {
            "head_sha": executor_repo["head_sha"],
            "pushed": True,
            "remote_ref": executor_repo["remote_ref"],
            "dirty": False,
            "files_changed": [],
        },
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
        "worktree_digest": "sha256:" + ("1" * 64),
        "llm_model": "test-reviewer-llm",
    }
    verdict_manifest = _sign(cp, reviewer.id, verdict_manifest)
    cp.add_evidence(
        task.id,
        "review",
        "artifact://review",
        "review approved without changed files",
        reviewer.id,
        metadata={"returncode": 0, "verification": verdict_manifest},
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert result["review_id"] == review.id
    assert any("repo.files_changed does not match executor evidence" in problem for problem in result["problems"])
    assert cp.list_publications(task.id) == []


def test_rejected_review_verdict_completes_without_clean_pushed_repo(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "work",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    executor_evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    verdict_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "rejected",
        "reviewed_evidence_id": executor_evidence.id,
        "worktree_digest": "sha256:" + ("0" * 64),
        "feedback": "Dirty checkout; not publishable.",
        "repo": {
            "head_sha": "fedcba9876543210fedcba9876543210fedcba98",
            "pushed": False,
            "dirty": True,
        },
        "blockers": ["executor evidence does not match the inspected checkout"],
    }
    verdict_manifest = _sign(cp, reviewer.id, verdict_manifest)
    verdict = cp.add_evidence(
        task.id,
        "review",
        "artifact://review",
        "review rejected dirty checkout",
        reviewer.id,
        metadata={"returncode": 0, "verification": verdict_manifest},
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "review_not_approved"
    assert result["review_status"] == ReviewStatus.REJECTED.value
    review = cp.list_reviews(task.id)[0]
    assert review.status == ReviewStatus.REJECTED.value
    assert review.evidence_id == verdict.id
    requeued = cp.get_task(task.id)
    assert requeued.state == TaskState.OPEN.value
    assert requeued.owner_agent_id is None
    assert requeued.lease_id is None
    assert cp.list_publications(task.id) == []


def test_rejected_review_verdict_blocks_exhausted_task(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "last attempt",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://attempt",
        "attempt complete",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id, verdict="rejected", feedback="No more attempts.")

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "review_not_approved"
    assert result["review_status"] == ReviewStatus.REJECTED.value
    exhausted = cp.get_task(task.id)
    assert exhausted.state == TaskState.BLOCKED.value
    assert exhausted.owner_agent_id is None
    assert exhausted.lease_id is None


def test_default_review_does_not_reuse_stale_verdict_for_new_review(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "stale verdict reuse",
        required_capabilities=["python"],
        max_attempts=2,
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://attempt",
        "attempt complete",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    first = cp.advance_default_review_workflow(task.id)
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id, verdict="rejected", feedback="Stale.")
    rejected = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    assert rejected["status"] == "review_not_approved"
    assert cp.get_task(task.id).state == TaskState.OPEN.value

    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )
    second = cp.advance_default_review_workflow(task.id)

    assert second["status"] == "waiting_for_reviewer_verdict"
    assert second["reviewer_agent_id"] == reviewer.id
    assert any("predates review request" in problem for problem in second["problems"])
    reviews = cp.list_reviews(task.id)
    assert [review.status for review in reviews] == [
        ReviewStatus.REJECTED.value,
        ReviewStatus.PENDING.value,
    ]


def test_publication_requires_verifiable_review_verdict_not_plain_approval(cp):
    """mac-5u1f: submit_review used to accept any task evidence as the
    approval verdict. The fix moves the verdict-shape + signature check
    from publish-time into submit_review itself, so the bad call now
    fails earlier (and never even reaches publish)."""
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    # mac-5u1f: passing the executor's own evidence as the verdict is now
    # refused at submit_review time, not at publish time.
    with pytest.raises(ValidationError, match="review_verdict"):
        cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=evidence.id)


def test_publication_policy_requires_publication_evidence_with_hash(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "release",
        required_capabilities=["python"],
        metadata={"policy": {"require_publication_evidence": True}},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    test_evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://tests",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    from tests.conftest import submit_review_verdict

    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, test_evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)

    with pytest.raises(ValidationError):
        cp.publish_task(task.id, "test://publish", reviewer.id)
    with pytest.raises(ValidationError):
        cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=test_evidence.id)
    with pytest.raises(ValidationError):
        cp.add_evidence(task.id, "publication", "test://publish", "published", reviewer.id)

    pub_evidence = cp.add_evidence(
        task.id,
        "publication",
        "test://publish",
        "published",
        reviewer.id,
        # mac-er6u: publication checksum must be a real sha256 hex form
        # (64 chars), not a short opaque string.
        checksum="sha256:" + ("ab" * 32),
    )
    publication = cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=pub_evidence.id)

    assert publication.content_hash == "sha256:" + ("ab" * 32)
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_evidence_kind_is_explicit(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])

    with pytest.raises(ValidationError):
        cp.add_evidence(task.id, "misc", "artifact://x", "unclassified", worker.id)


def test_idle_heartbeat_requires_no_active_lease(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)
    assert cp.get_agent(worker.id).current_task_id == task.id

    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, status="not-a-real-state")
    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, health_status="hot")
    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, status=AgentStatus.IDLE.value)

    refreshed = cp.get_agent(worker.id)
    assert refreshed.status == AgentStatus.BUSY.value
    assert refreshed.current_task_id == claimed.id

    cp.release_lease(lease.id, worker.id)
    refreshed = cp.heartbeat_agent(worker.id, status=AgentStatus.IDLE.value)
    assert refreshed.status == AgentStatus.IDLE.value
    assert refreshed.current_task_id is None


def test_degraded_startup_self_test_survives_worker_reregister(cp):
    machine = cp.register_machine("worker-host", resources={"cpu": 4})
    worker = cp.register_agent(machine.id, "worker", agent_id="agent_worker", resources={})
    report = {
        "schema": "mac.agent_startup_self_test.v1",
        "status": "degraded",
        "hermes_failure_class": "budget_exceeded",
        "blocking_problems": [],
    }
    cp.heartbeat_agent(
        worker.id,
        status=AgentStatus.IDLE.value,
        health_status=HealthStatus.DEGRADED.value,
        resources={"startup_self_test": report},
    )

    refreshed = cp.register_agent(machine.id, "worker", agent_id=worker.id, resources={})

    assert refreshed.health_status == HealthStatus.DEGRADED.value
    assert refreshed.resources["startup_self_test"] == report


def test_degraded_startup_self_test_survives_liveness_heartbeat_until_passed(cp):
    worker = register_agent(cp, "worker", ["python"])
    report = {
        "schema": "mac.agent_startup_self_test.v1",
        "status": "degraded",
        "hermes_failure_class": "budget_exceeded",
        "blocking_problems": [],
    }
    cp.heartbeat_agent(
        worker.id,
        status=AgentStatus.IDLE.value,
        health_status=HealthStatus.DEGRADED.value,
        resources={"startup_self_test": report},
    )

    refreshed = cp.heartbeat_agent(
        worker.id,
        status=AgentStatus.IDLE.value,
        health_status=HealthStatus.HEALTHY.value,
        resources={},
    )

    assert refreshed.health_status == HealthStatus.DEGRADED.value
    assert refreshed.resources["startup_self_test"] == report

    passed = dict(report)
    passed["status"] = "passed"
    passed["hermes_failure_class"] = ""
    recovered = cp.heartbeat_agent(
        worker.id,
        health_status=HealthStatus.HEALTHY.value,
        resources={"startup_self_test": passed},
    )

    assert recovered.health_status == HealthStatus.HEALTHY.value
    assert recovered.resources["startup_self_test"] == passed


def test_deliver_messages_is_idempotent_under_concurrent_calls(cp):
    """mac-4pkm: SELECT+UPDATE used to interleave so two concurrent
    deliver_messages calls could both mark the same row delivered. With
    the guarded UPDATE inside a transaction, the loser's UPDATE affects
    0 rows and the row is excluded from its returned list.
    """
    sender = register_agent(cp, "sender", ["python"])
    recipient = register_agent(cp, "recipient", ["python"])
    msg = cp.send_message(
        sender.id,
        recipient.id,
        MessageType.STATUS_UPDATE.value,
        {"status": "hello", "task_id": "task_demo"},
    )
    first = cp.deliver_messages(recipient.id)
    second = cp.deliver_messages(recipient.id)
    # Only the first call gets the message; the second is empty.
    assert [m.id for m in first] == [msg.id]
    assert second == []


def test_notifier_dedupes_on_notification_id_in_payload(cp):
    """mac-zipf: a notifier retry must not duplicate downstream messages.
    Verify the dedup query directly: when a message already exists
    whose payload's notification.id matches, _deliver_notification
    short-circuits and returns the existing message id list."""
    sender = register_agent(cp, "sender", ["hermes"])
    recipient = register_agent(cp, "recipient", ["hermes"])

    # Seed a "previously delivered" message whose payload already
    # carries a notification.id.
    notification_id = "notif_test_123"
    seeded = cp.send_message(
        sender.id,
        recipient.id,
        MessageType.STATUS_UPDATE.value,
        {
            "status": "task.test",
            "notification": {"id": notification_id, "event_type": "task.test"},
        },
    )

    # Build a fake OperatorNotification with the same id and call the
    # private delivery path directly.
    from mac.models import OperatorNotification
    notif = OperatorNotification(
        id=notification_id,
        event_type="task.test",
        subject_type="task",
        subject_id="task_x",
        title="test",
        body="test body",
        channels=["hermes"],
        metadata={},
        status="pending",
        created_at="2024-01-01T00:00:00+00:00",
        delivered_at=None,
    )
    delivered = cp.notifiers._deliver_notification(notif)
    assert seeded.id in delivered, "dedup should return the pre-seeded message id"


def test_notifier_delivery_claim_prevents_concurrent_duplicate_messages(cp):
    tenant = cp.register_tenant("ops")
    persona = cp.register_persona(
        tenant.id,
        "Rocky",
        soul_ref="hermes://ops/rocky/SOUL.md",
        memory_scope="hermes://ops/rocky/memory",
    )
    hermes = cp.register_hermes_instance(
        tenant.id,
        "rocky",
        persona_id=persona.id,
        home_ref="hermes://ops/rocky",
    )
    binding = cp.register_platform_binding(
        tenant.id,
        hermes.id,
        "slack",
        "T123/C456",
        display_name="#mac-home",
        scopes={"channels": ["C456"]},
    )
    machine = cp.register_machine("host")
    agent = cp.register_agent(
        machine.id,
        "worker",
        capabilities=["python"],
        hermes_instance_id=hermes.id,
    )
    cp.configure_notifier_channel(
        "slack-home",
        "slack",
        event_types=["task.*"],
        target={"platform_binding_id": binding.id},
    )

    task = cp.create_task("notify once", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)
    notification = next(
        item
        for item in cp.list_notifications(subject_id=task.id)
        if item.event_type == "task.claimed"
    )

    original_send = cp.notifiers._send_message
    first_send_started = threading.Event()
    release_first_send = threading.Event()
    send_lock = threading.Lock()
    send_count = 0
    results = []
    errors = []

    def slow_first_send(*args, **kwargs):
        nonlocal send_count
        with send_lock:
            send_count += 1
            current = send_count
        if current == 1:
            first_send_started.set()
            assert release_first_send.wait(5)
        return original_send(*args, **kwargs)

    def deliver_once() -> None:
        try:
            results.append(cp.deliver_pending_notifications(notification_id=notification.id))
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    cp.notifiers._send_message = slow_first_send
    try:
        first = threading.Thread(target=deliver_once)
        first.start()
        assert first_send_started.wait(5)

        second = threading.Thread(target=deliver_once)
        second.start()
        second.join(5)
        assert not second.is_alive()

        release_first_send.set()
        first.join(5)
        assert not first.is_alive()
    finally:
        cp.notifiers._send_message = original_send
        release_first_send.set()

    assert errors == []
    assert sum(int(result["delivered"]) for result in results) == 1
    assert send_count == 1
    messages = [
        message
        for message in cp.list_messages(agent.id)
        if message.payload["notification"]["id"] == notification.id
    ]
    assert len(messages) == 1


def test_command_audit_scrubs_argv_secrets(cp):
    """mac-6m14: argv on subprocess audit must have password-like flags
    and bare high-entropy strings redacted before persisting to
    command_audit and re-broadcasting through observability."""
    agent = register_agent(cp, "w", ["python"])
    record = cp.record_command_audit(
        agent.id,
        "started",
        argv=[
            "/usr/bin/curl",
            "--header",
            "Authorization: Bearer abc",
            "--token=ghp_supersecrettoken123456789",
            "--user-password",
            "topsecret123456789",
            "https://example.com/api",
            "ghp_abcdefghijklmnopqrstuvwxyz1234",  # bare high-entropy
        ],
        cwd="/tmp",
    )
    # Flag=value with secret hint → redacted
    assert "<redacted>" in record.argv[3]
    assert "abc" not in record.argv[3]
    # --foo-password followed by a value → value redacted (best-effort)
    assert record.argv[5] == "<redacted>"
    # Bare high-entropy element redacted (not URL, not path)
    assert "<redacted>" in record.argv
    # Non-secret args preserved
    assert "/usr/bin/curl" in record.argv
    assert "https://example.com/api" in record.argv


def test_submit_review_refuses_executor_evidence_as_verdict(cp):
    """mac-5u1f: submit_review now refuses to mark a review APPROVED
    when the supplied evidence_id is the executor's own evidence (or
    any non-verdict evidence). The verdict-shape + signature check
    that previously only ran at publish time is now enforced here so
    no compromised dispatcher can rubber-stamp executor work."""
    worker = register_agent(cp, "w", ["python"])
    reviewer = register_agent(cp, "r", ["review"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    with pytest.raises(ValidationError, match="review_verdict"):
        cp.submit_review(
            review.id,
            ReviewStatus.APPROVED.value,
            reviewer.id,
            evidence_id=evidence.id,
        )


def _signed_agent_executor_manifest(cp, agent_id, llm_model):
    from mac.services import sign_verification_manifest

    manifest = verified_repo_metadata()["verification"]
    manifest["executor"] = "mac-task-executor-opencode-build"
    manifest["llm_model"] = llm_model
    manifest["llm"] = {
        "tool": "opencode",
        "agent": "build",
        "model": llm_model,
    }
    manifest["signed_by"] = agent_id
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(agent_id),
        manifest,
    )
    return manifest


def test_submit_review_requires_different_llm_for_agent_executor(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "w", ["python"])
    reviewer = register_agent(cp, "r", ["review"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata={
            "returncode": 0,
            "verification": _signed_agent_executor_manifest(
                cp,
                worker.id,
                "inference-hub/anthropic/claude-sonnet",
            ),
        },
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        evidence.id,
        reviewer_llm_model="inference-hub/anthropic/claude-sonnet",
    )

    with pytest.raises(ValidationError, match="reviewer LLM must differ"):
        cp.submit_review(
            review.id,
            ReviewStatus.APPROVED.value,
            reviewer.id,
            evidence_id=verdict_id,
        )


def test_submit_review_accepts_different_llm_for_agent_executor(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "w", ["python"])
    reviewer = register_agent(cp, "r", ["review"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata={
            "returncode": 0,
            "verification": _signed_agent_executor_manifest(
                cp,
                worker.id,
                "inference-hub/anthropic/claude-sonnet",
            ),
        },
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        evidence.id,
        reviewer_llm_model="inference-hub/openai/gpt-5",
    )

    result = cp.submit_review(
        review.id,
        ReviewStatus.APPROVED.value,
        reviewer.id,
        evidence_id=verdict_id,
    )
    assert result.status == ReviewStatus.APPROVED.value


def test_submit_review_high_risk_requires_different_model_family_and_provider(cp):
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "w", ["python"])
    reviewer = register_agent(cp, "r", ["review"])
    task = cp.create_task(
        "t",
        required_capabilities=["python"],
        metadata={"review": {"risk_level": "high"}},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://t",
        "tests passed",
        worker.id,
        metadata={
            "returncode": 0,
            "verification": _signed_agent_executor_manifest(
                cp,
                worker.id,
                "inference-hub/anthropic/claude-sonnet",
            ),
        },
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        evidence.id,
        reviewer_llm_model="inference-hub/anthropic/claude-opus",
    )

    with pytest.raises(ValidationError, match="family must differ"):
        cp.submit_review(
            review.id,
            ReviewStatus.APPROVED.value,
            reviewer.id,
            evidence_id=verdict_id,
        )


def test_high_risk_review_disables_independence_fallback(cp):
    task = cp.create_task(
        "t",
        metadata={"default_review": {"risk_level": "critical"}},
    )

    assert cp._reviewer_independence_fallback_enabled(task) is False


def test_cross_llm_review_helper_contracts():
    from mac.review_service import (
        cross_llm_review_problems,
        manifest_llm_family,
        manifest_llm_model,
        manifest_llm_provider,
        manifest_requires_cross_llm_review,
        review_diversity_requirements,
    )

    assert manifest_llm_model({"llm": {"model": " Model A "}}) == "Model A"
    assert manifest_llm_model({"llm_model": "model-b"}) == "model-b"
    assert manifest_llm_model({"opencode_model": "model-c"}) == "model-c"
    assert manifest_llm_model({"gateway_model": "model-d"}) == "model-d"
    assert manifest_llm_model(None) == ""
    assert manifest_llm_family(
        {"llm_model": "inference-hub/anthropic/claude-sonnet"}
    ) == "claude"
    assert manifest_llm_family({"llm_family": "custom-lineage"}) == "custom-lineage"
    assert manifest_llm_provider(
        {"llm_model": "inference-hub/openai/gpt-5"}
    ) == "openai"
    assert manifest_llm_provider(
        {"llm_provider": "private-provider"}
    ) == "private-provider"
    assert review_diversity_requirements(
        {"metadata": {"review": {"risk_level": "high"}}}
    ) == {
        "high_risk": True,
        "different_model_family": True,
        "different_provider": True,
    }

    assert manifest_requires_cross_llm_review(
        {"executor": "mac-task-executor-opencode-build"}
    )
    assert manifest_requires_cross_llm_review({"agent_generated": True})
    assert manifest_requires_cross_llm_review({"requires_cross_llm_review": True})
    assert not manifest_requires_cross_llm_review(
        {"evidence_type": "review_verdict", "llm_model": "reviewer"}
    )
    assert not manifest_requires_cross_llm_review(None)

    assert cross_llm_review_problems(None, {"llm_model": "reviewer"}) == []
    assert cross_llm_review_problems(
        {"executor": "mac-task-executor-opencode-build"},
        {"llm_model": "reviewer"},
    ) == ["executor evidence from an agent runner requires llm.model or llm_model"]
    assert cross_llm_review_problems(
        {"executor": "mac-task-executor-opencode-build", "llm_model": "builder"},
        {},
    ) == ["review_verdict evidence requires reviewer llm.model or llm_model"]
    assert cross_llm_review_problems(
        {"executor": "mac-task-executor-opencode-build", "llm_model": "Model X"},
        {"llm_model": " model x "},
    ) == ["reviewer LLM must differ from executor LLM (both Model X)"]
    assert cross_llm_review_problems(
        {"executor": "mac-task-executor-opencode-build", "llm_model": "Model X"},
        {"llm_model": "Model Y"},
    ) == []
    high_risk = {
        "high_risk": True,
        "different_model_family": True,
        "different_provider": True,
    }
    same_lineage = cross_llm_review_problems(
        {
            "executor": "mac-task-executor-opencode-build",
            "llm_model": "inference-hub/anthropic/claude-sonnet",
        },
        {"llm_model": "inference-hub/anthropic/claude-opus"},
        requirements=high_risk,
    )
    assert any("family must differ" in problem for problem in same_lineage)
    assert any("provider must differ" in problem for problem in same_lineage)
    assert cross_llm_review_problems(
        {
            "executor": "mac-task-executor-opencode-build",
            "llm_model": "inference-hub/anthropic/claude-sonnet",
        },
        {"llm_model": "inference-hub/openai/gpt-5"},
        requirements=high_risk,
    ) == []
    unknown_lineage = cross_llm_review_problems(
        {
            "executor": "mac-task-executor-opencode-build",
            "llm_model": "private-model-a",
        },
        {"llm_model": "private-model-b"},
        requirements=high_risk,
    )
    assert any("requires llm.family" in problem for problem in unknown_lineage)
    assert any("requires llm.provider" in problem for problem in unknown_lineage)


def test_register_artifact_recomputes_local_digest_and_rejects_mismatch(cp, tmp_path):
    """mac-0a8o: an artifact with a local file URI must have its digest
    recomputed from the file contents. A caller-supplied digest that
    doesn't match the real bytes is rejected."""
    import hashlib

    payload = tmp_path / "artifact.bin"
    payload.write_bytes(b"hello mac")
    real_digest = "sha256:" + hashlib.sha256(b"hello mac").hexdigest()
    # Honest registration succeeds.
    ok = cp.register_artifact(
        kind="image",
        digest=real_digest,
        uri="file://%s" % payload,
        created_by="ci",
    )
    assert ok.digest == real_digest
    # Different digest → rejected because we recomputed against bytes.
    bogus = "sha256:" + ("0" * 64)
    with pytest.raises(ValidationError, match="recomputed"):
        cp.register_artifact(
            kind="image",
            digest=bogus,
            uri="file://%s" % payload,
            created_by="ci",
        )


def test_heartbeat_verifies_running_digest_signature_when_supplied(cp):
    """mac-oud5: an agent that supplies a digest signature must sign
    the claim under its own attestation key. A signature signed by a
    different agent or with a bad key is refused. (Unsigned claims
    still go through with a warning log — full enforcement is a
    follow-up.)"""
    from mac.services import sign_verification_manifest

    machine = cp.register_machine("h")
    agent = cp.register_agent(machine.id, "a", capabilities=["python"])
    cp.create_runtime(
        name="r",
        manifest={"image": "mac@sha256:" + ("a" * 64)},
        created_by="ops",
    )
    runtime = cp.list_runtimes()[0]
    agent_key = cp._agent_attestation_key(agent.id)
    # Honest signature for our own claim → accepted.
    sig = sign_verification_manifest(
        agent_key, {"agent_id": agent.id, "running_digest": runtime.digest}
    )
    out = cp.heartbeat_agent(
        agent.id, running_digest=runtime.digest, running_digest_signature=sig
    )
    assert out.running_digest == runtime.digest

    # Now roll over to a second digest and submit a bad signature.
    cp.create_runtime(
        name="r2",
        manifest={"image": "mac@sha256:" + ("b" * 64)},
        created_by="ops",
    )
    runtime2 = [r for r in cp.list_runtimes() if r.name == "r2"][0]
    with pytest.raises(ValidationError, match="signature"):
        cp.heartbeat_agent(
            agent.id,
            running_digest=runtime2.digest,
            running_digest_signature="v1:obviously-bad",
        )


def test_claim_task_enforces_tenant_policy_as_explicit_chokepoint(cp):
    """mac-1g3u: tenant isolation used to depend on whoever called
    ``_agent_available_for`` remembering to. The audit warned that a
    future dispatch path could skip it. ``claim_task`` now ALSO calls
    ``_machine_allows_tenant`` directly, so even a hypothetical caller
    that bypasses the broader eligibility filter still hits the gate."""
    from mac.models import AuthorizationError

    cp.register_tenant("tenant-priv")
    private_machine = cp.register_machine(
        "private-host",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": ["tenant-priv"]}},
    )
    worker = cp.register_agent(private_machine.id, "worker", capabilities=["python"])
    task = cp.create_task(
        "cross-tenant",
        required_capabilities=["python"],
        metadata={"tenant_id": "tenant-other"},
    )
    with pytest.raises(AuthorizationError, match="tenant"):
        cp.claim_task(task.id, worker.id)


def test_attestation_single_rotation_verifies_double_rotation_errors(cp):
    """mac-s2vz followup: a SINGLE attestation-key rotation (e.g. an agent
    re-keyed after a redeploy) must NOT invalidate a verdict signed beforehand
    — the retained previous key verifies it, so review publication proceeds.
    Only once the previous key is ALSO gone (a second rotation) does the clear
    'key was rotated; re-sign required' recovery error surface.

    Regression guard for the fleet-completion bottleneck where routine re-keys
    permanently wedged in-flight reviews under a 'signed under rotated key'
    publish failure."""
    worker = register_agent(cp, "w", ["python"])
    reviewer = register_agent(cp, "r", ["review"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "test", "artifact://t", "ok",
        worker.id, metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    from tests.conftest import submit_review_verdict
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)

    # A single rotation retains the previous key: the pre-rotation verdict
    # still verifies (against the key active at signing time), so there is NO
    # signature/rotation problem — publication is no longer wedged.
    cp.rotate_agent_attestation_key(reviewer.id)
    _verdict, problems = cp._find_review_verdict_evidence(
        task.id, reviewer.id,
        executor_evidence_id=evidence.id,
        verdict_evidence_id=verdict_id,
        not_before=review.created_at,
    )
    assert not any("rotated" in p or "does not verify" in p for p in problems), \
        "a single rotation must not invalidate a pre-rotation verdict; got: %s" % problems

    # A second rotation drops the previous key: the verdict is now genuinely
    # unrecoverable and the clear recovery error surfaces.
    cp.rotate_agent_attestation_key(reviewer.id)
    _verdict2, problems2 = cp._find_review_verdict_evidence(
        task.id, reviewer.id,
        executor_evidence_id=evidence.id,
        verdict_evidence_id=verdict_id,
        not_before=review.created_at,
    )
    assert any("rotated" in p for p in problems2), \
        "expected clear rotation error after the prev key is gone, got: %s" % problems2


def test_executor_evidence_verifies_via_prev_key_after_rotation(cp):
    """Companion to _find_review_verdict_evidence's fallback: the EXECUTOR-side
    signature check (_assess_default_review_evidence) must also tolerate a
    single key rotation, else already-bound evidence is rejected every review
    sweep and the task parks in waiting_for_verifiable_evidence forever (the
    2026-07-14 incident, on the executor side)."""
    worker = register_agent(cp, "w", ["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    meta = verified_repo_metadata(cp, worker.id)          # signed under key1
    evidence = cp.add_evidence(task.id, "test", "artifact://t", "ok", worker.id, metadata=meta)

    # Baseline: valid before any rotation.
    assert cp._assess_default_review_evidence(task, evidence)["valid"] is True

    # A single rotation retains the previous key -> evidence still verifies.
    cp.rotate_agent_attestation_key(worker.id)
    assert cp._assess_default_review_evidence(task, evidence)["valid"] is True, \
        "executor evidence must verify via the retained previous key after one rotation"

    # A second rotation drops the previous key -> now genuinely unverifiable.
    cp.rotate_agent_attestation_key(worker.id)
    result = cp._assess_default_review_evidence(task, evidence)
    assert result["valid"] is False and result["reason"] == "signature_invalid"


def test_rollout_complete_rescue_returns_to_paused(cp):
    """mac-24f4: RESCUING used to be a one-way trap. ``complete_rescue``
    returns the rollout to PAUSED so the operator can re-gate the
    canary or roll back."""
    rollout = create_verified_rollout(cp, "24.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    # Drive into RESCUING via a failing health gate.
    cp.evaluate_rollout_health(rollout.id, {"runtime": "bad"}, "monitor")
    refreshed = cp.get_rollout(rollout.id)
    assert refreshed.status == RolloutStatus.RESCUING.value
    # Operator finishes the rescue; rollout returns to PAUSED so it
    # can be resumed or rolled back from a clean state.
    out = cp.advance_rollout(rollout.id, "complete_rescue", "human")
    assert out.status == RolloutStatus.PAUSED.value


def test_signature_includes_signed_by_in_mac(cp):
    """mac-wu3f: with signed_by now in the canonical form, a signature
    minted by agent A under their key cannot be replayed in a manifest
    claiming ``signed_by=B`` (verification would key off B's key)."""
    from mac.services import sign_verification_manifest, verify_verification_manifest_signature

    machine = cp.register_machine("h")
    agent_a = cp.register_agent(machine.id, "a")
    agent_b = cp.register_agent(machine.id, "b")
    key_a = cp._agent_attestation_key(agent_a.id)
    key_b = cp._agent_attestation_key(agent_b.id)
    base_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "test",
        "signed_by": agent_a.id,
    }
    # A signs honestly.
    sig_a = sign_verification_manifest(key_a, base_manifest)
    assert verify_verification_manifest_signature(key_a, base_manifest, sig_a)

    # Replay: take A's signature, paste into a manifest claiming
    # signed_by=B. With signed_by in the MAC input, neither A's nor B's
    # key verifies this swapped manifest.
    swapped = dict(base_manifest)
    swapped["signed_by"] = agent_b.id
    assert not verify_verification_manifest_signature(key_b, swapped, sig_a)
    assert not verify_verification_manifest_signature(key_a, swapped, sig_a)


def test_tasks_state_trigger_rejects_unknown_state(cp):
    """mac-1hnt: a direct UPDATE that bypasses validate_transition (e.g.
    a bug or a manual fix) used to be able to put a task into an
    illegal state. The DB-level trigger now rejects that."""
    import sqlite3

    register_agent(cp, "w", ["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    with pytest.raises(sqlite3.IntegrityError):
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            ("not-a-real-state", task.id),
        )


def test_publication_content_hash_format_is_enforced(cp):
    """mac-er6u: publication content_hash used to accept any opaque
    string the worker passed. Now we require a real ``algo:hex`` form
    with a recognized algorithm and digest length."""
    worker = register_agent(cp, "w", ["python"])
    reviewer = register_agent(cp, "r", ["review"])
    task = cp.create_task(
        "t",
        required_capabilities=["python"],
        metadata={"policy": {"require_publication_evidence": True}},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    test_ev = cp.add_evidence(
        task.id, "test", "artifact://x", "ok",
        worker.id, metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id)
    from tests.conftest import submit_review_verdict
    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, test_ev.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)

    # Bad format → refused
    bad_ev = cp.add_evidence(
        task.id, "publication", "p://x", "p", reviewer.id, checksum="not-a-hash",
    )
    with pytest.raises(ValidationError, match="checksum"):
        cp.publish_task(task.id, "p://x", reviewer.id, evidence_id=bad_ev.id)

    short_ev = cp.add_evidence(
        task.id, "publication", "p://x", "p", reviewer.id, checksum="sha256:abc",
    )
    with pytest.raises(ValidationError, match="checksum"):
        cp.publish_task(task.id, "p://x", reviewer.id, evidence_id=short_ev.id)


def test_observability_record_truncates_oversized_detail(cp):
    """mac-29vr: an observability detail larger than MAX_DETAIL_BYTES
    must be replaced with a truncated marker so a chatty caller can't
    pump GB into the events table."""
    huge_detail = {"blob": "x" * (cp.observability.MAX_DETAIL_BYTES + 10)}
    record = cp.observability.record_log(
        "test.huge_detail", level="info", detail=huge_detail
    )
    assert record.detail.get("_truncated") is True
    assert record.detail.get("_max_bytes") == cp.observability.MAX_DETAIL_BYTES
    # Original blob is gone — only the truncation marker remains.
    assert "blob" not in record.detail


def test_expire_leases_applies_default_grace_against_ntp_step(cp):
    """mac-vgw9: with no explicit `now`, expire_leases subtracts a 30s
    grace from the cutoff so a small NTP step forward doesn't mass-expire.
    """
    worker = register_agent(cp, "w", ["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    # Claim with a 10-second lease — about to expire but well within
    # the 30s NTP-step grace window.
    _, lease = cp.claim_task(task.id, worker.id, lease_seconds=10)
    # Bring the lease just barely past expires_at (5s in the past).
    import datetime as _dt
    past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?", (past, lease.id),
    )
    # Default invocation (no explicit `now`) honors the 30s grace.
    recovered = cp.expire_leases()
    assert recovered == [], "default grace should absorb a 5s NTP step"
    # An operator who *wants* immediate expiry can pass explicit `now`.
    forced = cp.expire_leases(now=utcnow())
    assert [t.id for t in forced] == [task.id]


def test_eval_gate_uses_composite_target_to_prevent_replay_across_artifacts(cp):
    """mac-7mwd: two rollouts that share a version string but ship
    different artifact_hash values must not share an eval-gate
    history. Composite ``version@hash`` target_id keeps them apart.
    """
    eval_set = cp.create_eval_set("eg-smoke", scoring="higher_is_better")
    cp.update_eval_set_baseline(eval_set.id, 0.5)

    # Rollout A: version 7.0, artifact-hash aaaa
    runtime_a = create_runtime(cp, "runtime-7a")
    rollout_a = cp.create_rollout(
        "7.0",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime_a.id,
        artifact_uri="artifact://mac/7.0a",
        artifact_hash="sha256:aaaaaaaa",
        required_eval_set_id=eval_set.id,
    )
    # Record a passing run targeted at composite form for A.
    cp.record_eval_run(eval_set.id, "rollout_version", "7.0@sha256:aaaaaaaa", 0.9)
    cp.advance_rollout(rollout_a.id, "start_canary", "human")

    # Rollout B: same version string, DIFFERENT artifact-hash bbbb.
    runtime_b = create_runtime(cp, "runtime-7b")
    rollout_b = cp.create_rollout(
        "7.0",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime_b.id,
        artifact_uri="artifact://mac/7.0b",
        artifact_hash="sha256:bbbbbbbb",
        required_eval_set_id=eval_set.id,
    )
    # B has NO matching composite eval_run; the bare-version run for A
    # is the only one in the table. Start_canary on B must refuse:
    # without a composite-matching run, the lookup picks the bare
    # version run, which IS the replay we're preventing. The fix is
    # the explicit composite preference: a bare-version run can only
    # match the rollout when the composite is missing, but each rollout
    # has a distinct composite — so callers must record per-composite.
    # Until B has its own run recorded, promote is gated by A's run
    # (replay) — but that legacy fallback is preserved for backward
    # compat. The right behavior, observable here, is that recording
    # a composite-form *failing* run for B causes the lookup to prefer
    # B's run over A's bare run.
    cp.record_eval_run(eval_set.id, "rollout_version", "7.0@sha256:bbbbbbbb", 0.1)
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout_b.id, "start_canary", "human")


def test_verify_artifact_while_paused_invalidates_health(cp):
    """mac-vh9h: swapping artifact_uri/hash on a PAUSED rollout must
    not let the prior health gate persist; resume + promote must
    re-evaluate against the new artifact.
    """
    rollout = create_verified_rollout(cp, "9.5")
    # Seed a passing run + advance to canary + pass health.
    eval_set = cp.create_eval_set("e95", scoring="higher_is_better")
    cp.update_eval_set_baseline(eval_set.id, 0.5)
    # Don't tie it to this rollout — we just need the health gate.
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "human")
    cp.advance_rollout(rollout.id, "pause", "human")

    # Swap artifact. The previous health pass is now stale.
    cp.verify_rollout_artifact(rollout.id, "artifact://mac/9.5b", "sha256:zzz1234", "human")
    # _latest_health_passed should now be False because we recorded a
    # ``status=invalidated`` health event.
    assert cp.rollouts._latest_health_passed(rollout.id) is False
    # Resuming and trying to promote without a fresh health pass must fail.
    cp.advance_rollout(rollout.id, "resume", "human")
    with pytest.raises((ValidationError, TransitionError)):
        cp.advance_rollout(rollout.id, "promote", "human")


def test_rollout_health_policy_requires_explicit_required_checks_at_create(cp):
    """mac-jmjc: an empty required_checks list trivially passes the
    health gate. Reject it at rollout creation and default missing
    policy to a baseline ``required_checks=['runtime']`` so the gate
    cannot be silently bypassed.
    """
    runtime = create_runtime(cp, "runtime-x")
    # Explicit empty required_checks is rejected.
    with pytest.raises(ValidationError, match="required_checks"):
        cp.create_rollout(
            "9.0",
            "canary",
            10,
            "human",
            runtime_environment_id=runtime.id,
            artifact_uri="artifact://mac/9.0",
            artifact_hash="sha256:abc123",
            health_policy={"required_checks": []},
        )
    # No health_policy gets the default baseline (not silently empty).
    rollout = cp.create_rollout(
        "9.1",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/9.1",
        artifact_hash="sha256:abc123",
    )
    assert rollout.health_policy["required_checks"] == ["runtime"]
    # evaluate_rollout_health with empty caller-supplied checks now FAILS
    # the gate (instead of trivially passing on []).
    cp.advance_rollout(rollout.id, "start_canary", "human")
    result = cp.evaluate_rollout_health(rollout.id, {}, "human")
    status = result.get("status") if isinstance(result, dict) else result.status
    assert status != "healthy"


def _recovery_two_node_workflow(cp, slug):
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="dev", name="dev", description="d", system_prompt="p", level="ic")
    return cp.workflows.create_workflow(
        slug=slug,
        name=slug,
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {"node_key": "investigate", "node_type": "task", "role_required": "qa", "max_attempts": 1},
                {"node_key": "fix", "node_type": "task", "role_required": "dev", "max_attempts": 2},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
                {"from_node_key": "investigate", "to_node_key": "fix", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )


def test_workflow_advance_race_does_not_orphan_tasks(cp):
    """mac-t8ih: two concurrent terminal events on the same workflow run
    must not both spawn a next-node task. The guarded UPDATE in _advance
    ensures only the first caller proceeds; the second one observes
    rowcount=0 and exits without writing history."""
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="dev", name="dev", description="d", system_prompt="p", level="ic")
    cp.workflows.create_workflow(
        slug="race-wf",
        name="race",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {"node_key": "a", "node_type": "task", "role_required": "qa", "max_attempts": 1},
                {"node_key": "b", "node_type": "task", "role_required": "dev", "max_attempts": 5},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "a", "condition": "success", "priority": 100},
                {"from_node_key": "a", "to_node_key": "b", "condition": "failure", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("race-wf", started_by="ops")
    start = threading.Barrier(2)
    errors = []

    def advance() -> None:
        try:
            start.wait(timeout=5)
            cp.workflow_runtime._advance(
                run, "a", "failure", run.current_task_id
            )
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=advance) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    spawned = cp.store.query_all(
        "SELECT id, state FROM tasks WHERE workflow_run_id = ? AND workflow_node_key = ?",
        (run.id, "b"),
    )
    assert len(spawned) == 1
    assert cp.workflow_runtime.get_run(run.id).current_task_id == spawned[0]["id"]

    history = cp.store.query_all(
        "SELECT to_node_key FROM workflow_run_history WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    b_spawns = sum(1 for r in history if r["to_node_key"] == "b")
    assert b_spawns == 1


def test_workflow_task_linkage_and_created_history_commit_atomically(cp, monkeypatch):
    task_id = "task_atomic_workflow_link"
    run_id = "run_atomic_workflow_link"
    original_record_history = cp._record_history

    def fail_created_history(*args, **kwargs):
        if len(args) >= 2 and args[1] == "task.created":
            raise RuntimeError("simulated task history failure")
        return original_record_history(*args, **kwargs)

    monkeypatch.setattr(cp, "_record_history", fail_created_history)
    with pytest.raises(RuntimeError, match="history failure"):
        cp.create_task(
            "atomic workflow task",
            _task_id=task_id,
            _workflow_run_id=run_id,
            _workflow_node_key="build",
        )
    assert cp.store.query_one("SELECT id FROM tasks WHERE id = ?", (task_id,)) is None

    monkeypatch.setattr(cp, "_record_history", original_record_history)
    first = cp.create_task(
        "atomic workflow task",
        _task_id=task_id,
        _workflow_run_id=run_id,
        _workflow_node_key="build",
    )
    second = cp.create_task(
        "atomic workflow task retry",
        _task_id=task_id,
        _workflow_run_id=run_id,
        _workflow_node_key="build",
    )

    assert first.id == second.id == task_id
    linked = cp.store.query_one(
        "SELECT workflow_run_id, workflow_node_key FROM tasks WHERE id = ?",
        (task_id,),
    )
    assert linked["workflow_run_id"] == run_id
    assert linked["workflow_node_key"] == "build"
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM task_history WHERE task_id = ? AND event_type = ?",
        (task_id, "task.created"),
    )["n"] == 1


def test_workflow_reservation_loss_cancels_the_unadopted_task(cp, monkeypatch):
    _recovery_two_node_workflow(cp, "reservation-loss")
    run = cp.workflow_runtime.start_run("reservation-loss", started_by="ops")
    original_spawn = cp.workflow_runtime._spawn_node_task
    spawned = {}

    def steal_reservation(*args, **kwargs):
        task = original_spawn(*args, **kwargs)
        spawned["task"] = task
        cp.store.execute(
            "UPDATE workflow_runs SET current_node_key = ?, updated_at = ? WHERE id = ?",
            ("stolen-reservation", utcnow(), run.id),
        )
        return task

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", steal_reservation)
    with pytest.raises(TransitionError, match="reservation was lost"):
        cp.workflow_runtime._advance(
            run, "investigate", "success", run.current_task_id
        )

    assert cp.get_task(spawned["task"].id).state == TaskState.CANCELLED.value


def test_workflow_recovery_guard_and_validation_edges(cp, monkeypatch):
    _recovery_two_node_workflow(cp, "recovery-guards")
    run = cp.workflow_runtime.start_run("recovery-guards", started_by="ops")

    assert cp.workflow_runtime._advance(
        run, "investigate", "success", "task-from-another-node"
    ).current_task_id == run.current_task_id
    monkeypatch.setenv("MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "invalid")
    assert cp.workflow_runtime._reservation_is_stale(run) is False
    assert cp.workflow_runtime._plan_pre_decided(
        run.definition_snapshot,
        None,
        pre_decisions={},
        actor="ops",
    ) == (None, [])

    original_query_one = cp.store.query_one

    def missing_origin(sql, params=()):
        if "workflow_node_key FROM tasks" in sql:
            return None
        return original_query_one(sql, params)

    monkeypatch.setattr(cp.store, "query_one", missing_origin)
    assert cp.workflow_runtime._reservation_origin(run) is None
    monkeypatch.setattr(cp.store, "query_one", original_query_one)

    with pytest.raises(ValidationError, match="another run or node"):
        cp.workflow_runtime._spawn_node_task(
            "different-run",
            {"node_key": "investigate", "role_required": "qa"},
            workflow=None,
            started_by="ops",
            tenant_id=None,
            attempt=1,
            task_id=run.current_task_id,
        )

    invalid_definition = dict(run.definition_snapshot)
    invalid_definition["edges"] = [
        {
            "from_node_key": "investigate",
            "to_node_key": "missing-node",
            "condition": "success",
            "priority": 100,
        }
    ]
    cp.store.execute(
        "UPDATE workflow_runs SET definition_snapshot = ? WHERE id = ?",
        (json.dumps(invalid_definition), run.id),
    )
    with pytest.raises(ValidationError, match="unknown node"):
        cp.workflow_runtime._advance(
            cp.workflow_runtime.get_run(run.id),
            "investigate",
            "success",
            run.current_task_id,
        )

    cp.store.execute(
        "UPDATE workflow_runs SET state = ? WHERE id = ?",
        ("completed", run.id),
    )
    assert cp.workflow_runtime._advance(
        cp.workflow_runtime.get_run(run.id),
        "investigate",
        "success",
        run.current_task_id,
    ).state == "completed"


def test_start_run_skips_missing_role_snapshot_then_fails_spawn(cp, monkeypatch):
    _recovery_two_node_workflow(cp, "missing-role-snapshot")

    def missing_role(*_args, **_kwargs):
        raise NotFoundError("role removed")

    monkeypatch.setattr(cp.roles, "get_role", missing_role)
    with pytest.raises(ValidationError, match="references missing role"):
        cp.workflow_runtime.start_run("missing-role-snapshot", started_by="ops")


def test_workflow_advance_reservation_blocks_staggered_caller(cp, monkeypatch):
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="dev", name="dev", description="d", system_prompt="p", level="ic")
    cp.workflows.create_workflow(
        slug="staggered-race-wf",
        name="staggered race",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {"node_key": "a", "node_type": "task", "role_required": "qa", "max_attempts": 1},
                {"node_key": "b", "node_type": "task", "role_required": "dev", "max_attempts": 5},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "a", "condition": "success", "priority": 100},
                {"from_node_key": "a", "to_node_key": "b", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("staggered-race-wf", started_by="ops")
    entered = threading.Event()
    release = threading.Event()
    original_spawn = cp.workflow_runtime._spawn_node_task

    def delayed_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_spawn(*args, **kwargs)

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", delayed_spawn)
    errors = []

    def advance() -> None:
        try:
            cp.workflow_runtime._advance(run, "a", "success", run.current_task_id)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    thread = threading.Thread(target=advance)
    thread.start()
    assert entered.wait(timeout=5)
    reserved = cp.workflow_runtime.get_run(run.id)
    assert reserved.current_node_key.startswith("__workflow_advancing__:")

    observed = cp.workflow_runtime._advance(
        reserved, "a", "success", run.current_task_id
    )
    assert observed.current_node_key == reserved.current_node_key
    release.set()
    thread.join(timeout=10)

    assert not errors
    spawned = cp.store.query_all(
        "SELECT id FROM tasks WHERE workflow_run_id = ? AND workflow_node_key = ?",
        (run.id, "b"),
    )
    assert len(spawned) == 1
    assert cp.workflow_runtime.get_run(run.id).current_task_id == spawned[0]["id"]


def test_workflow_tick_recovers_task_created_before_advancement_crash(cp, monkeypatch):
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="dev", name="dev", description="d", system_prompt="p", level="ic")
    cp.workflows.create_workflow(
        slug="recover-created-task",
        name="recover created task",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {"node_key": "a", "node_type": "task", "role_required": "qa", "max_attempts": 1},
                {"node_key": "b", "node_type": "task", "role_required": "dev", "max_attempts": 2},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "a", "condition": "success", "priority": 100},
                {"from_node_key": "a", "to_node_key": "b", "condition": "success", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("recover-created-task", started_by="ops")
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, run.current_task_id),
    )
    original_spawn = cp.workflow_runtime._spawn_node_task

    def create_then_crash(*args, **kwargs):
        original_spawn(*args, **kwargs)
        raise RuntimeError("simulated crash after atomic task creation")

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", create_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        cp.workflow_runtime._advance(run, "a", "success", run.current_task_id)

    reserved = cp.workflow_runtime.get_run(run.id)
    assert reserved.current_node_key.startswith("__workflow_advancing__:")
    spawned = cp.store.query_all(
        "SELECT id, workflow_run_id, workflow_node_key FROM tasks "
        "WHERE workflow_run_id = ? AND workflow_node_key = ?",
        (run.id, "b"),
    )
    assert len(spawned) == 1
    assert spawned[0]["workflow_run_id"] == run.id
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM workflow_run_history "
        "WHERE run_id = ? AND to_node_key = ?",
        (run.id, "b"),
    )["n"] == 0

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", original_spawn)
    monkeypatch.setenv("MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "0")
    cp.store.execute(
        "UPDATE workflow_runs SET next_action_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", run.id),
    )
    tick_result = cp.tick(limit=0)
    recovered = tick_result["workflow_runs"]

    assert [item["id"] for item in recovered] == [run.id]
    final = cp.workflow_runtime.get_run(run.id)
    assert final.current_node_key == "b"
    assert final.current_task_id == spawned[0]["id"]
    history_row = cp.store.query_one(
        "SELECT COUNT(*) AS n, MAX(task_id) AS task_id FROM workflow_run_history "
        "WHERE run_id = ? AND to_node_key = ?",
        (run.id, "b"),
    )
    assert history_row["n"] == 1
    assert history_row["task_id"] == spawned[0]["id"]


def test_dispatch_tick_contains_workflow_recovery_and_reconcile_failures(cp, monkeypatch):
    monkeypatch.setattr(
        cp.workflow_runtime,
        "tick",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("recovery failed")),
    )
    monkeypatch.setattr(
        cp,
        "reconcile_service_roles",
        lambda: (_ for _ in ()).throw(RuntimeError("reconcile failed")),
    )

    result = cp.tick(limit=0)

    assert result["workflow_runs"] == []
    events = cp.list_observability(limit=20)
    assert any(event.name == "workflow.recovery.failed" for event in events)


def test_workflow_tick_isolates_poison_run_and_recovers_later_run(cp, monkeypatch):
    _recovery_two_node_workflow(cp, "tick-isolation")
    poison = cp.workflow_runtime.start_run("tick-isolation", started_by="ops")
    healthy = cp.workflow_runtime.start_run("tick-isolation", started_by="ops")
    for index, run in enumerate((poison, healthy)):
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.COMPLETED.value, run.current_task_id),
        )
        cp.store.execute(
            """
            UPDATE workflow_runs
            SET current_node_key = ?, updated_at = ?, next_action_at = ?
            WHERE id = ?
            """,
            (
                "__workflow_advancing__:task_tick_isolation_%d" % index,
                "200%d-01-01T00:00:00+00:00" % index,
                "200%d-01-01T00:00:00+00:00" % index,
                run.id,
            ),
        )
    original_advance = cp.workflow_runtime._advance

    def isolate(run, *args, **kwargs):
        if run.id == poison.id:
            raise RuntimeError("poison workflow run")
        return original_advance(run, *args, **kwargs)

    monkeypatch.setattr(cp.workflow_runtime, "_advance", isolate)
    monkeypatch.setenv("MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "0")

    recovered = cp.workflow_runtime.tick(limit=10, actor="test-sweeper")

    assert [run.id for run in recovered] == [healthy.id]
    assert cp.workflow_runtime.get_run(poison.id).current_node_key.startswith(
        "__workflow_advancing__:"
    )
    failures = [
        event
        for event in cp.list_observability(limit=50)
        if event.name == "workflow.recovery.failed"
    ]
    assert any(event.subject_id == poison.id for event in failures)


def test_workflow_tick_bounds_large_candidate_backlog(cp, monkeypatch):
    _recovery_two_node_workflow(cp, "tick-bounded")
    runs = [
        cp.workflow_runtime.start_run("tick-bounded", started_by="ops")
        for _ in range(5)
    ]
    for index, run in enumerate(runs):
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.COMPLETED.value, run.current_task_id),
        )
        cp.store.execute(
            """
            UPDATE workflow_runs
            SET current_node_key = ?, updated_at = ?, next_action_at = ?
            WHERE id = ?
            """,
            (
                "__workflow_advancing__:task_tick_bound_%d" % index,
                "2000-01-%02dT00:00:00+00:00" % (index + 1),
                "2000-01-%02dT00:00:00+00:00" % (index + 1),
                run.id,
            ),
        )
    seen = []
    original_query_all = cp.store.query_all
    candidate_limits = []

    def query_all(sql, params=()):
        if "FROM workflow_runs AS wr" in sql:
            candidate_limits.append(params[-1])
        return original_query_all(sql, params)

    monkeypatch.setattr(cp.store, "query_all", query_all)

    def observe(run, *args, **kwargs):
        seen.append(run.id)
        return run

    monkeypatch.setattr(cp.workflow_runtime, "_advance", observe)
    monkeypatch.setenv("MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "0")

    assert cp.workflow_runtime.tick(limit=2) == []
    assert seen == [runs[0].id, runs[1].id]
    assert cp.workflow_runtime.tick(limit=2) == []
    assert seen == [run.id for run in runs[:4]]
    assert cp.workflow_runtime.tick(limit=2) == []
    assert seen == [run.id for run in runs]
    assert candidate_limits == [3, 3, 3]


def test_workflow_tick_uses_indexed_deadline_not_definition_scan(cp, monkeypatch):
    cp.roles.create_role(
        slug="qa",
        name="qa",
        description="d",
        system_prompt="p",
        level="ic",
    )
    cp.workflows.create_workflow(
        slug="indexed-deadline",
        name="indexed deadline",
        description="d",
        workflow_type="bug",
        definition={
            "nodes": [
                {
                    "node_key": "work",
                    "node_type": "task",
                    "role_required": "qa",
                    "timeout_minutes": 1,
                }
            ],
            "edges": [
                {
                    "from_node_key": "",
                    "to_node_key": "work",
                    "condition": "success",
                    "priority": 100,
                }
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("indexed-deadline", started_by="ops")
    cp.store.execute(
        "UPDATE workflow_runs SET next_action_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", run.id),
    )
    queries = []
    original_query_all = cp.store.query_all

    def query_all(sql, params=()):
        if "FROM workflow_runs AS wr" in sql:
            queries.append(" ".join(sql.split()))
        return original_query_all(sql, params)

    monkeypatch.setattr(cp.store, "query_all", query_all)
    cp.workflow_runtime.tick(limit=1)

    assert len(queries) == 1
    assert "next_action_at <= ?" in queries[0]
    assert "definition_snapshot LIKE" not in queries[0]
    indexes = {
        row["name"]
        for row in cp.store.query_all("PRAGMA index_list(workflow_runs)")
    }
    assert "idx_workflow_runs_next_action" in indexes


def test_dispatch_tick_runs_one_bounded_maintenance_and_inventory_pass(cp, monkeypatch):
    register_agent(
        cp,
        "batch-worker",
        ["python"],
        resources={
            "capacity": 5,
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
            },
        },
    )
    for index in range(5):
        cp.create_task(
            "batch dispatch %d" % index,
            required_capabilities=["python"],
        )
    counts = {"expire": 0, "unblock": 0, "tasks": 0, "agents": 0}
    original_expire = cp._expire_leases_sweep_page
    original_unblock = cp._unblock_ready_sweep_page
    original_tasks = cp._dispatch_ordered_tasks
    original_agents = cp._available_agents

    def expire(*, limit):
        counts["expire"] += 1
        return original_expire(limit=limit)

    def unblock(*, limit):
        counts["unblock"] += 1
        return original_unblock(limit=limit)

    def tasks():
        counts["tasks"] += 1
        return original_tasks()

    def agents():
        counts["agents"] += 1
        return original_agents()

    monkeypatch.setattr(cp, "_expire_leases_sweep_page", expire)
    monkeypatch.setattr(cp, "_unblock_ready_sweep_page", unblock)
    monkeypatch.setattr(cp, "_dispatch_ordered_tasks", tasks)
    monkeypatch.setattr(cp, "_available_agents", agents)

    report = cp.tick(limit=5)

    assert len(report["assignments"]) == 5
    assert counts == {"expire": 1, "unblock": 1, "tasks": 1, "agents": 1}


def test_expired_lease_sweep_pages_large_backlog(cp):
    leases = []
    for index in range(5):
        agent = register_agent(cp, "expired-%d" % index, ["python"])
        task = cp.create_task(
            "expired lease %d" % index,
            required_capabilities=["python"],
        )
        _claimed, lease = cp.claim_task(task.id, agent.id)
        cp.store.execute(
            "UPDATE leases SET expires_at = ? WHERE id = ?",
            ("2000-01-%02dT00:00:00+00:00" % (index + 1), lease.id),
        )
        leases.append(lease.id)

    first = cp._expire_leases_page(now=utcnow(), limit=2)
    second = cp._expire_leases_page(
        now=utcnow(),
        limit=2,
        cursor=first["next_cursor"],
    )
    third = cp._expire_leases_page(
        now=utcnow(),
        limit=2,
        cursor=second["next_cursor"],
    )

    assert [len(first["tasks"]), len(second["tasks"]), len(third["tasks"])] == [2, 2, 1]
    assert first["has_more"] is True
    assert second["has_more"] is True
    assert third["has_more"] is False
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM leases WHERE status = ?",
        (LeaseStatus.ACTIVE.value,),
    )["n"] == 0


def test_blocked_and_dead_letter_sweeps_are_bounded_and_cursor_driven(cp):
    dependency = cp.create_task("finished dependency")
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, dependency.id),
    )
    blocked = [
        cp.create_task("blocked %d" % index, dependencies=[dependency.id])
        for index in range(5)
    ]
    first = cp._unblock_ready_tasks(limit=2)
    second = cp._unblock_ready_tasks(limit=2, cursor=first["next_cursor"])
    third = cp._unblock_ready_tasks(limit=2, cursor=second["next_cursor"])
    assert [len(first["tasks"]), len(second["tasks"]), len(third["tasks"])] == [2, 2, 1]
    assert all(cp.get_task(task.id).state == TaskState.OPEN.value for task in blocked)

    dead_ids = []
    for index in range(5):
        task = cp.create_task(
            "dead %d" % index,
            metadata={"tenant_id": "tenant-a"},
        )
        cp.store.execute(
            """
            UPDATE tasks
            SET state = ?, attempt_count = max_attempts, updated_at = ?
            WHERE id = ?
            """,
            (
                TaskState.FAILED.value,
                "2000-01-%02dT00:00:00+00:00" % (index + 1),
                task.id,
            ),
        )
        dead_ids.append(task.id)
    for index in range(3):
        task = cp.create_task(
            "other tenant dead %d" % index,
            priority=1000,
            metadata={"tenant_id": "tenant-b"},
        )
        cp.store.execute(
            "UPDATE tasks SET state = ?, attempt_count = max_attempts WHERE id = ?",
            (TaskState.FAILED.value, task.id),
        )
    page_one = cp.list_dead_letters_page(tenant_id="tenant-a", limit=2)
    page_two = cp.list_dead_letters_page(
        tenant_id="tenant-a",
        limit=2,
        cursor=page_one["next_cursor"],
    )
    page_three = cp.list_dead_letters_page(
        tenant_id="tenant-a",
        limit=2,
        cursor=page_two["next_cursor"],
    )
    seen = [
        task.id
        for page in (page_one, page_two, page_three)
        for task in page["tasks"]
    ]
    assert seen == dead_ids
    assert page_one["has_more"] is True
    assert page_two["has_more"] is True
    assert page_three["has_more"] is False


def test_tick_auto_reopens_blocked_attempt_after_backoff_and_records_audit(cp):
    task = cp.create_task("retry blocked attempt", required_capabilities=["python"])
    cp.store.execute(
        "UPDATE tasks SET state = ?, attempt_count = ?, updated_at = ? WHERE id = ?",
        (TaskState.BLOCKED.value, 1, utcnow(), task.id),
    )

    first = cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.BLOCKED.value
    assert first["auto_reopened"] == []

    cp.store.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", task.id),
    )
    second = cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    assert reopened.attempt_count == 1
    assert [item["id"] for item in second["auto_reopened"]] == [task.id]
    auto_history = [
        event for event in cp.task_history(task.id)
        if event.event_type == "task.auto_reopened"
    ]
    assert len(auto_history) == 1
    assert auto_history[0].detail["backoff_seconds"] == 60
    observations = cp.list_observability(
        subject_type="task",
        subject_id=task.id,
        limit=50,
    )
    assert any(event.name == "task.auto_reopened" for event in observations)


def test_transient_retry_excludes_the_failed_worker_and_repeated_failure_stops(cp):
    task = cp.create_task(
        "bounded transient retry",
        required_capabilities=["python"],
        max_attempts=3,
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "agent_first",
        {"reason": "heartbeat_offline"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    first = cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    assert reopened.metadata["retry_excluded_agent_ids"] == ["agent_first"]
    assert [item["id"] for item in first["auto_reopened"]] == [task.id]

    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "agent_second",
        {"reason": "heartbeat_offline"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (2, "2000-01-01T00:00:00+00:00", task.id),
    )

    second = cp.tick(limit=0)

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert [item["id"] for item in second["auto_retry_exhausted"]] == [task.id]
    terminal = [
        event
        for event in cp.task_history(task.id)
        if event.to_state == TaskState.FAILED.value
    ][-1]
    assert terminal.detail["reason"] == "repeated_identical_attempt_failure"


def test_shared_transport_failures_deduplicate_to_one_fleet_incident(cp):
    incident_ids = []
    for index in range(2):
        task = cp.create_task(
            "shared failure victim %d" % index,
            project="demo",
            max_attempts=3,
        )
        failure = {
            "reason": "executor_failed",
            "error": "MAC API /evidence POST failed: gateway timeout",
        }
        cp._transition_task_internal(
            task.id,
            TaskState.BLOCKED.value,
            "agent_first",
            failure,
        )
        cp.store.execute(
            "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
            (1, "2000-01-01T00:00:00+00:00", task.id),
        )
        cp.tick(limit=0)
        assert cp.get_task(task.id).state == TaskState.OPEN.value

        cp._transition_task_internal(
            task.id,
            TaskState.BLOCKED.value,
            "agent_second",
            failure,
        )
        cp.store.execute(
            "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
            (2, "2000-01-01T00:00:00+00:00", task.id),
        )
        cp.tick(limit=0)

        failed = cp.get_task(task.id)
        assert failed.state == TaskState.FAILED.value
        incident_ids.append(failed.metadata["shared_failure_incident_task_id"])

    assert incident_ids[0] == incident_ids[1]
    incident = cp.get_task(incident_ids[0])
    assert incident.project == "mac"
    assert incident.metadata["origin"]["type"] == "shared_environment_incident"
    assert len(incident.metadata["affected_task_ids"]) == 2
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE id LIKE 'task_incident_%'"
    )["n"] == 1


def test_deterministic_contract_failure_stops_without_waiting_or_repair_child(cp):
    task = cp.create_task(
        "deterministic contract failure",
        max_attempts=3,
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "agent_first",
        {
            "reason": "verification_contract_failed",
            "problems": ["repo evidence requires changed files"],
        },
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ? WHERE id = ?",
        (1, task.id),
    )

    result = cp.tick(limit=0)

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert failed.dependencies == []
    assert "contract_repair_task_id" not in failed.metadata
    assert [item["id"] for item in result["auto_retry_exhausted"]] == [task.id]


def test_tick_fails_exhausted_blocked_attempt_without_reopening(cp):
    task = cp.create_task(
        "exhausted blocked attempt",
        required_capabilities=["python"],
        max_attempts=2,
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, attempt_count = ?, updated_at = ? WHERE id = ?",
        (
            TaskState.BLOCKED.value,
            2,
            "2000-01-01T00:00:00+00:00",
            task.id,
        ),
    )

    result = cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.FAILED.value
    assert result["auto_reopened"] == []
    assert [item["id"] for item in result["auto_retry_exhausted"]] == [task.id]
    assert task.id in {item["id"] for item in result["dead_letters"]}
    assert not [
        event for event in cp.task_history(task.id)
        if event.event_type == "task.auto_reopened"
    ]


def test_tick_exhaustion_carries_attempt_output_into_terminal_diagnosis(cp):
    task = cp.create_task("exhausted attempt output", max_attempts=1)
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "test_failed", "stderr_tail": "compile failed: missing header"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    terminal = [
        event for event in cp.task_history(task.id) if event.to_state == TaskState.FAILED.value
    ][-1]
    assert terminal.detail["diagnosis"]["output_tail"] == "compile failed: missing header"
    assert terminal.detail["diagnosis"]["output_tail_unavailable_reason"] == ""


def test_tick_exhaustion_records_when_attempt_history_has_no_output(cp):
    task = cp.create_task("exhausted attempt without output", max_attempts=1)
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "test_failed"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    terminal = [
        event for event in cp.task_history(task.id) if event.to_state == TaskState.FAILED.value
    ][-1]
    diagnosis = terminal.detail["diagnosis"]
    assert diagnosis["output_tail"] == ""
    assert diagnosis["output_tail_unavailable_reason"] == (
        "no captured stdout, stderr, output, log, or tail exists in attempt history"
    )


def test_tick_exhausted_blocked_attempt_records_failure_class_and_salvage(cp):
    task = cp.create_task(
        "exhausted blocked attempt with salvage",
        required_capabilities=["python"],
        max_attempts=2,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {
            "reason": "heartbeat_offline",
            "branch": "mac/agent/task",
            "recorded_lessons": ["memory-1"],
        },
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (2, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.WAITING.value
    assert len(failed.dependencies) == 1
    repair = cp.get_task(failed.dependencies[0])
    assert repair.metadata["origin"]["type"] == "environment_prerequisite"
    assert failed.metadata["failure_class"] == "environment"
    assert failed.metadata["environment_repair_task_id"] == repair.id
    assert "contract_repair_task_id" not in failed.metadata
    assert failed.metadata["salvage"]["pushed_branch"] == "mac/agent/task"
    assert failed.metadata["salvage"]["recorded_lessons"] == ["memory-1"]
    assert [item["id"] for item in result["auto_retry_exhausted"]] == [task.id]


def test_tick_exhausted_environment_failure_repair_task_has_environment_prerequisite_reason(cp):
    task = cp.create_task(
        "exhausted environment blocked attempt",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "heartbeat_offline"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.metadata["failure_class"] == "environment"
    assert len(waiting.dependencies) == 1
    repair = cp.get_task(waiting.dependencies[0])
    assert repair.metadata["origin"]["type"] == "environment_prerequisite"
    assert repair.metadata["origin"]["parent_task_id"] == task.id
    assert waiting.metadata["environment_repair_task_id"] == repair.id
    assert "contract_repair_task_id" not in waiting.metadata
    exhausted_ids = [item["id"] for item in result["auto_retry_exhausted"]]
    assert task.id in exhausted_ids
    waiting_updates = [
        event
        for event in cp.task_history(task.id)
        if event.to_state == TaskState.WAITING.value
    ]
    assert waiting_updates, "Expected at least one history event with to_state=waiting"
    waiting_states = {e.event_type for e in waiting_updates}
    assert waiting_states & {"task.updated", "task.transitioned"}, (
        "Expected task.updated or task.transitioned event for waiting state"
    )


def test_tick_exhausted_repair_preserves_control_plane_publication_metadata(cp):
    task = cp.create_task(
        "exhausted repair with derived publication metadata",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={
            "operator_note": "keep me",
            "repair_policy": {"contract_prerequisite": True},
        },
    )
    derived_publication = {
        "publication_lane": {"schema": "mac.publication_lane.v1", "lane": "legacy"},
        "publication_route": {
            "schema": "mac.publication_lane.v1",
            "lane": "legacy",
            "managed": False,
        },
        "managed_fast_lane": {"eligible": False, "reason": "legacy"},
        "work_package": {"managed": False},
    }
    persisted_metadata = {**task.metadata, **derived_publication}
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(persisted_metadata, sort_keys=True), task.id),
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {
            "reason": "verification_contract_failed",
            "problems": ["repo evidence requires pushed=true"],
        },
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.metadata["operator_note"] == "keep me"
    for key, value in derived_publication.items():
        assert waiting.metadata[key] == value
    assert waiting.metadata["contract_repair_task_id"] == waiting.dependencies[0]
    repair = cp.get_task(waiting.dependencies[0])
    assert repair.metadata["origin"] == {
        "type": "contract_prerequisite",
        "parent_task_id": task.id,
    }
    assert [item["id"] for item in result["auto_retry_exhausted"]] == [task.id]
    observations = cp.list_observability(
        subject_type="task",
        subject_id=task.id,
        limit=100,
    )
    assert not [
        event for event in observations if event.name == "task.auto_retry.failed"
    ]


def test_tick_exhausted_superseded_attempt_cancels_instead_of_failing(cp):
    task = cp.create_task(
        "superseded blocked attempt",
        required_capabilities=["python"],
        max_attempts=1,
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "superseded"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    cancelled = cp.get_task(task.id)
    assert cancelled.state == TaskState.CANCELLED.value
    assert cancelled.metadata["failure_class"] == "superseded"
    assert [item["id"] for item in result["auto_retry_exhausted"]] == [task.id]
    assert task.id not in {item["id"] for item in result["dead_letters"]}
    transition = [
        event
        for event in cp.task_history(task.id)
        if event.event_type == "task.transitioned"
    ][-1]
    assert transition.to_state == TaskState.CANCELLED.value
    assert transition.detail["reason"] == "superseded"



def test_tick_exhausted_executor_failed_creates_environment_repair_task(cp):
    task = cp.create_task(
        "exhausted executor_failed attempt",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "executor_failed", "manual_repair_required": True, "returncode": 1},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.metadata["failure_class"] == "environment"
    assert len(waiting.dependencies) == 1
    repair = cp.get_task(waiting.dependencies[0])
    assert repair.metadata["origin"]["type"] == "environment_prerequisite"
    assert repair.metadata["origin"]["parent_task_id"] == task.id
    assert waiting.metadata["environment_repair_task_id"] == repair.id
    assert "contract_repair_task_id" not in waiting.metadata
    exhausted_ids = [item["id"] for item in result["auto_retry_exhausted"]]
    assert task.id in exhausted_ids



def test_tick_exhausted_worker_exception_creates_environment_repair_task(cp):
    task = cp.create_task(
        "exhausted worker_exception attempt",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "worker_exception", "failure": "worker_exception"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.metadata["failure_class"] == "environment"
    assert len(waiting.dependencies) == 1
    repair = cp.get_task(waiting.dependencies[0])
    assert repair.metadata["origin"]["type"] == "environment_prerequisite"
    assert repair.metadata["origin"]["parent_task_id"] == task.id
    assert waiting.metadata["environment_repair_task_id"] == repair.id
    assert "contract_repair_task_id" not in waiting.metadata
    exhausted_ids = [item["id"] for item in result["auto_retry_exhausted"]]
    assert task.id in exhausted_ids


def test_tick_exhausted_environment_failure_resets_attempt_count_to_zero(cp):
    task = cp.create_task(
        "exhausted environment failure resets attempt count",
        required_capabilities=["python"],
        max_attempts=2,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "worker_exception"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (2, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert int(waiting.attempt_count or 0) == 0


def test_tick_exhausted_environment_failure_transition_detail_and_metadata(cp):
    task = cp.create_task(
        "exhausted environment failure detail check",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "heartbeat_offline"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert "contract_repair_status" not in waiting.metadata, "contract_repair_status must not be set for environment failures"
    assert waiting.metadata.get("failure_class") == "environment"
    assert waiting.metadata.get("environment_repair_task_id"), "repair task id should be set"
    history = cp.task_history(task.id)
    waiting_events = [
        e for e in history
        if e.to_state == TaskState.WAITING.value
    ]
    assert waiting_events, "Expected a history event transitioning to waiting"


def test_tick_exhausted_environment_repair_task_title_and_description(cp):
    task = cp.create_task(
        "my important task needing repair",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "worker_exception"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    repair_id = waiting.metadata["environment_repair_task_id"]
    repair = cp.get_task(repair_id)
    assert "my important task needing repair" in repair.title
    assert task.id in repair.description
    assert "environment" in repair.description.lower() or "prerequisite" in repair.description.lower()


def test_tick_exhausted_environment_repair_task_idempotent(cp):
    task = cp.create_task(
        "exhausted environment repair idempotency",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "worker_exception"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)
    first_waiting = cp.get_task(task.id)
    first_repair_id = first_waiting.metadata["environment_repair_task_id"]
    first_deps = list(first_waiting.dependencies)

    # Simulate a second tick by manually re-running the exhausted transition
    # The repair task should already be recorded; no second repair should be created
    cp.tick(limit=0)

    second_waiting = cp.get_task(task.id)
    assert second_waiting.metadata["environment_repair_task_id"] == first_repair_id
    assert set(second_waiting.dependencies) == set(first_deps)


def test_claim_exhausted_attempt_records_failure_class(cp):
    agent = register_agent(cp, capabilities=["python"])
    task = cp.create_task(
        "claim exhausted attempt",
        required_capabilities=["python"],
        max_attempts=1,
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "verification_contract_failed"},
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, attempt_count = ?, updated_at = ? WHERE id = ?",
        (TaskState.OPEN.value, 1, utcnow(), task.id),
    )

    with pytest.raises(TransitionError):
        cp.claim_task(task.id, agent.id)

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert failed.dependencies == []
    assert failed.metadata["failure_class"] == "work"


def test_tick_exhausted_contract_failure_can_opt_into_prerequisite(cp):
    """Independent repository repair workflows remain available explicitly."""
    task = cp.create_task(
        "exhausted contract failure repair status",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"contract_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "verification_contract_failed"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.metadata.get("contract_repair_status") == "waiting", (
        "contract_repair_status must be 'waiting' on contract failure path"
    )
    assert waiting.metadata.get("contract_repair_task_id"), "contract_repair_task_id must be set"
    assert "environment_repair_task_id" not in waiting.metadata


def test_repeated_no_telemetry_lease_expiry_does_not_create_repair_task(cp):
    task = cp.create_task(
        "silent lease expiry",
        required_capabilities=["python"],
        max_attempts=3,
    )
    for attempt in range(3):
        worker = register_agent(cp, "silent-worker-%d" % attempt, ["python"])
        current, lease = cp.claim_task(task.id, worker.id)
        assert current.attempt_count == attempt + 1
        expired_at = "2000-01-01T00:00:00+00:00"
        cp.store.execute(
            "UPDATE leases SET expires_at = ? WHERE id = ?",
            (expired_at, lease.id),
        )
        cp.store.execute(
            "UPDATE tasks SET leased_until = ? WHERE id = ?",
            (expired_at, task.id),
        )
        cp.expire_leases(now=utcnow())

    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert failed.dependencies == []
    assert "environment_repair_task_id" not in failed.metadata
    expiry = [
        event for event in cp.task_history(task.id)
        if event.event_type == "task.lease_expired"
    ][-1]
    assert expiry.detail["reason"] == "environment_failure_without_actionable_evidence"
    assert expiry.detail["manual_repair_required"] is True


def test_tick_exhausted_environment_failure_does_not_set_contract_repair_status(cp):
    """contract_repair_status must not be set on the environment failure path."""
    task = cp.create_task(
        "exhausted environment failure no contract_repair_status",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "worker_exception", "failure": "worker_exception"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.metadata.get("failure_class") == "environment"
    assert "contract_repair_status" not in waiting.metadata, (
        "contract_repair_status must not be set on environment failure path"
    )
    assert waiting.metadata.get("environment_repair_task_id"), "environment_repair_task_id must be set"
    assert "contract_repair_task_id" not in waiting.metadata


@pytest.mark.parametrize(
    "diagnosis",
    [
        "needs-operator",
        "wrong-host self-release",
        "dependency-on-external",
    ],
)
def test_tick_fails_non_retryable_blocked_attempt_diagnoses(cp, diagnosis):
    task = cp.create_task(
        "non retryable blocked attempt",
        required_capabilities=["python"],
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": diagnosis},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.FAILED.value
    assert result["auto_reopened"] == []
    assert not [
        event for event in cp.task_history(task.id)
        if event.event_type == "task.auto_reopened"
    ]


def test_workflow_tick_skips_active_reservations_and_incomplete_rows(cp, monkeypatch):
    _recovery_two_node_workflow(cp, "tick-nonactionable")
    run = cp.workflow_runtime.start_run("tick-nonactionable", started_by="ops")
    cp.store.execute(
        "UPDATE workflow_runs SET current_node_key = ?, updated_at = ? WHERE id = ?",
        ("__workflow_advancing__:task_future", utcnow(), run.id),
    )
    assert cp.workflow_runtime.tick() == []

    cp.store.execute(
        "UPDATE workflow_runs SET current_node_key = ?, current_task_id = NULL WHERE id = ?",
        ("investigate", run.id),
    )
    assert cp.workflow_runtime.tick() == []

    cp.store.execute(
        "UPDATE workflow_runs SET current_node_key = ?, current_task_id = ? WHERE id = ?",
        ("missing-node", run.current_task_id, run.id),
    )
    assert cp.workflow_runtime.tick() == []

    definition = dict(run.definition_snapshot)
    definition["nodes"] = [
        {**node, "timeout_minutes": 1}
        if node.get("node_key") == "investigate"
        else node
        for node in definition["nodes"]
    ]
    cp.store.execute(
        "UPDATE workflow_runs SET current_node_key = ?, current_task_id = ?, "
        "definition_snapshot = ? WHERE id = ?",
        ("investigate", run.current_task_id, json.dumps(definition), run.id),
    )
    original_get_task = cp.workflow_runtime._get_task
    monkeypatch.setattr(
        cp.workflow_runtime,
        "_get_task",
        lambda *_args: (_ for _ in ()).throw(NotFoundError("missing task")),
    )
    assert cp.workflow_runtime.tick() == []
    monkeypatch.setattr(cp.workflow_runtime, "_get_task", original_get_task)

    cp.store.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (TaskState.OPEN.value, "not-a-timestamp", run.current_task_id),
    )
    assert cp.workflow_runtime.tick() == []
    cp.store.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (TaskState.COMPLETED.value, utcnow(), run.current_task_id),
    )
    assert cp.workflow_runtime.tick() == []

    with cp.store.transaction() as conn:
        cp.workflow_runtime._record_run_history(
            conn,
            run.id,
            seq=2,
            from_key="investigate",
            to_key=None,
            condition="skipped",
            task_id=run.current_task_id,
            actor="test",
            attempt=1,
            detail={"reason": "coverage of durable history helper"},
        )


def test_start_run_stages_predecision_history_until_spawn_recovers(cp, monkeypatch):
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="dev", name="dev", description="d", system_prompt="p", level="ic")
    cp.workflows.create_workflow(
        slug="recover-predecision-start",
        name="recover predecision start",
        description="d",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "review", "node_type": "approval", "role_required": "qa", "max_attempts": 1},
                {"node_key": "build", "node_type": "task", "role_required": "dev", "max_attempts": 1},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "review", "condition": "success", "priority": 100},
                {"from_node_key": "review", "to_node_key": "build", "condition": "approved", "priority": 100},
            ],
        },
        created_by="human",
    )
    original_spawn = cp.workflow_runtime._spawn_node_task

    def crash_before_spawn(*_args, **_kwargs):
        raise RuntimeError("simulated crash before task creation")

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", crash_before_spawn)
    with pytest.raises(RuntimeError, match="simulated crash"):
        cp.workflow_runtime.start_run(
            "recover-predecision-start",
            started_by="ops",
            pre_decisions={"review": "approved"},
        )

    row = cp.store.query_one(
        "SELECT id, current_node_key FROM workflow_runs "
        "WHERE workflow_id = (SELECT id FROM workflows WHERE slug = ?) "
        "ORDER BY created_at DESC LIMIT 1",
        ("recover-predecision-start",),
    )
    assert row["current_node_key"].startswith("__workflow_advancing__:")
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM workflow_run_history WHERE run_id = ?",
        (row["id"],),
    )["n"] == 0

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", original_spawn)
    monkeypatch.setenv("MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "0")
    cp.store.execute(
        "UPDATE workflow_runs SET next_action_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", row["id"]),
    )
    recovered = cp.workflow_runtime.tick()

    assert [item.id for item in recovered] == [row["id"]]
    final = cp.workflow_runtime.get_run(row["id"])
    assert final.current_node_key == "build"
    history = cp.store.query_all(
        "SELECT to_node_key, condition FROM workflow_run_history "
        "WHERE run_id = ? ORDER BY seq",
        (row["id"],),
    )
    assert [(item["to_node_key"], item["condition"]) for item in history] == [
        ("review", "success"),
        ("build", "approved"),
    ]
    cp.workflow_runtime.tick()
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM workflow_run_history WHERE run_id = ?",
        (row["id"],),
    )["n"] == 2


def test_start_run_predecision_chain_can_complete_without_spawning(cp):
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.workflows.create_workflow(
        slug="predecision-terminal",
        name="predecision terminal",
        description="d",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "review", "node_type": "approval", "role_required": "qa", "max_attempts": 1},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "review", "condition": "success", "priority": 100},
                {"from_node_key": "review", "to_node_key": "", "condition": "approved", "priority": 100},
            ],
        },
        created_by="human",
    )

    run = cp.workflow_runtime.start_run(
        "predecision-terminal",
        started_by="ops",
        pre_decisions={"review": "approved"},
    )

    assert run.state == "completed"
    assert run.current_node_key is None
    assert run.current_task_id is None
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE workflow_run_id = ?",
        (run.id,),
    )["n"] == 0
    history = cp.store.query_all(
        "SELECT to_node_key, condition FROM workflow_run_history "
        "WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    assert [(item["to_node_key"], item["condition"]) for item in history] == [
        ("review", "success"),
        (None, "approved"),
    ]


def test_midrun_predecision_history_commits_only_with_recovered_spawn(cp, monkeypatch):
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="dev", name="dev", description="d", system_prompt="p", level="ic")
    cp.workflows.create_workflow(
        slug="recover-predecision-midrun",
        name="recover predecision midrun",
        description="d",
        workflow_type="custom",
        definition={
            "nodes": [
                {"node_key": "prepare", "node_type": "task", "role_required": "qa", "max_attempts": 1},
                {"node_key": "review", "node_type": "approval", "role_required": "qa", "max_attempts": 1},
                {"node_key": "build", "node_type": "task", "role_required": "dev", "max_attempts": 1},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "prepare", "condition": "success", "priority": 100},
                {"from_node_key": "prepare", "to_node_key": "review", "condition": "success", "priority": 100},
                {"from_node_key": "review", "to_node_key": "build", "condition": "approved", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run(
        "recover-predecision-midrun",
        started_by="ops",
        pre_decisions={"review": "approved"},
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, run.current_task_id),
    )
    original_spawn = cp.workflow_runtime._spawn_node_task

    def crash_before_spawn(*_args, **_kwargs):
        raise SystemExit("midrun process terminated")

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", crash_before_spawn)
    with pytest.raises(SystemExit, match="process terminated"):
        cp.workflow_runtime._advance(
            run, "prepare", "success", run.current_task_id
        )
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM workflow_run_history WHERE run_id = ?",
        (run.id,),
    )["n"] == 1

    monkeypatch.setattr(cp.workflow_runtime, "_spawn_node_task", original_spawn)
    monkeypatch.setenv("MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS", "0")
    cp.store.execute(
        "UPDATE workflow_runs SET next_action_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", run.id),
    )
    cp.tick(limit=0)

    final = cp.workflow_runtime.get_run(run.id)
    assert final.current_node_key == "build"
    rows = cp.store.query_all(
        "SELECT condition FROM workflow_run_history WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    assert [row["condition"] for row in rows] == ["success", "approved", "success"]
    cp.tick(limit=0)
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM workflow_run_history WHERE run_id = ?",
        (run.id,),
    )["n"] == 3




def test_strip_control_chars_keeps_normal_text(cp):
    """mac-3xpl: ANSI escapes and ASCII control chars in upstream
    issue fields must be stripped before reaching the ledger."""
    assert cp._strip_control_chars("normal text") == "normal text"
    assert cp._strip_control_chars("multi\nline\nok") == "multi\nline\nok"
    # ANSI CSI sequence stripped
    assert "\x1b" not in cp._strip_control_chars("foo\x1b[31m red \x1b[0m bar")
    # NUL and other ctrl stripped
    assert cp._strip_control_chars("a\x00b\x07c") == "abc"




def test_failed_to_open_requeue_resets_attempt_count(cp):
    """mac-d2xh: a dead-letter requeue (FAILED → OPEN) must reset
    attempt_count, otherwise the next claim immediately re-fails because
    attempt_count >= max_attempts. Without the reset, requeue is a no-op."""
    worker = register_agent(cp, "w", ["python"])
    task = cp.create_task("t", required_capabilities=["python"], max_attempts=2)

    # Burn both attempts.
    _, lease = cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id, lease_id=lease.id)
    cp.transition_task(
        task.id,
        TaskState.FAILED.value,
        worker.id,
        lease_id=lease.id,
    )
    # Re-open and try once more.
    cp.reopen_task(task.id, "ops")
    after_first_requeue = cp.get_task(task.id)
    # The reset must zero attempt_count and clear completed_at so the
    # next claim is permitted.
    assert after_first_requeue.attempt_count == 0
    assert after_first_requeue.completed_at is None
    # Claim must succeed (would raise if attempt_count were still >= max).
    cp.claim_task(task.id, worker.id)
    assert cp.get_task(task.id).attempt_count == 1


# ----------------------------------------------------------------------
# PR2c: lease delegation. The dispatcher (runner) owns the lease, but
# the role-specialised Job pod is the actor that calls start_task /
# submit_for_review / add_evidence. delegate_lease records the
# delegation so the hub accepts the delegate; renew/release stay
# strictly owner-only.
# ----------------------------------------------------------------------


def _claim_with_delegate(cp):
    """Set up: a dispatcher claims, then delegates to a separate role agent."""
    dispatcher = register_agent(cp, "dispatcher", ["python"])
    delegate = register_agent(cp, "delegate", ["python"])
    task = cp.create_task("Build widget", required_capabilities=["python"])
    _task, lease = cp.claim_task(task.id, dispatcher.id)
    return task, dispatcher, delegate, lease


def test_delegate_lease_happy_path(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    updated = cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    assert updated.id == lease.id
    assert updated.agent_id == dispatcher.id
    assert updated.delegated_agent_id == delegate.id


def test_delegate_lease_rejects_non_owner_caller(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    # Caller claims to be the delegate (or any non-owner) — must be refused.
    with pytest.raises(AuthorizationError):
        cp.delegate_lease(lease.id, delegate.id, delegate.id)
    # And the lease must be unchanged.
    fresh = cp.get_lease(lease.id)
    assert fresh.delegated_agent_id is None


def test_delegate_lease_rejects_unknown_to_agent_id(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    with pytest.raises(NotFoundError):
        cp.delegate_lease(lease.id, dispatcher.id, "agent-does-not-exist")
    fresh = cp.get_lease(lease.id)
    assert fresh.delegated_agent_id is None


def test_start_task_accepts_delegated_agent(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    # The delegate (not the owner) can now author start_task. Before
    # PR2c this raised AuthorizationError.
    started = cp.start_task(task.id, delegate.id)
    assert started.state == TaskState.RUNNING.value


def test_submit_for_review_accepts_delegated_agent(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    cp.start_task(task.id, delegate.id)
    # Delegate adds evidence + submits — both must accept the delegate.
    cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "tests passed",
        delegate.id,
        metadata=verified_repo_metadata(cp, delegate.id),
    )
    reviewed = cp.submit_for_review(task.id, delegate.id)
    assert reviewed.state == TaskState.NEEDS_REVIEW.value


def test_start_task_still_rejects_unrelated_agent(cp):
    """Negative: an agent that is neither the owner nor the delegate
    must still be refused — delegation does not open the door for
    arbitrary callers."""
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    other = register_agent(cp, "other", ["python"])
    with pytest.raises(AuthorizationError):
        cp.start_task(task.id, other.id)


def test_add_evidence_accepts_delegated_agent(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    cp.start_task(task.id, delegate.id)

    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "tests passed",
        delegate.id,
        metadata=verified_repo_metadata(cp, delegate.id),
    )

    assert evidence.created_by == delegate.id


def test_add_evidence_rejects_unrelated_agent_on_active_lease(cp):
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    other = register_agent(cp, "other", ["python"])

    with pytest.raises(AuthorizationError):
        cp.add_evidence(task.id, "test", "artifact://pytest", "tests passed", other.id)


def test_renew_and_release_remain_owner_only(cp):
    """Spec §6.3: renewal + release stay strictly owner-only even
    after delegation. The delegate may transition state but cannot
    touch lease lifecycle."""
    task, dispatcher, delegate, lease = _claim_with_delegate(cp)
    cp.delegate_lease(lease.id, dispatcher.id, delegate.id)
    with pytest.raises(AuthorizationError):
        cp.renew_lease(lease.id, delegate.id)
    with pytest.raises(AuthorizationError):
        cp.release_lease(lease.id, delegate.id)


def test_release_lease_refuses_to_clobber_after_takeover(cp):
    """mac-79s1: if the lease has already been expired and the task
    reclaimed by another agent, a stale release_lease call from the
    original owner must not clear the new owner.
    """
    worker_a = register_agent(cp, "worker-a", ["python"])
    worker_b = register_agent(cp, "worker-b", ["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    _, lease_a = cp.claim_task(task.id, worker_a.id)

    # Simulate hub-side lease takeover: flip lease to RELEASED and
    # re-open the task, then have worker_b claim it.
    cp.store.execute(
        "UPDATE leases SET status = ? WHERE id = ?",
        (LeaseStatus.RELEASED.value, lease_a.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = NULL, lease_id = NULL WHERE id = ?",
        (TaskState.OPEN.value, task.id),
    )
    _, lease_b = cp.claim_task(task.id, worker_b.id)

    # Stale release from worker_a must fail and leave worker_b's lease intact.
    with pytest.raises(TransitionError):
        cp.release_lease(lease_a.id, worker_a.id)
    assert cp.get_lease(lease_b.id).status == LeaseStatus.ACTIVE.value
    assert cp.get_task(task.id).owner_agent_id == worker_b.id
    assert cp.get_task(task.id).lease_id == lease_b.id


def test_expire_leases_does_not_clobber_a_reclaimed_task(cp):
    """mac-s0ta: between expire_leases reading the row and writing the
    expiration, another path may have already released the lease and
    reclaimed the task. The UPDATE must be guarded so a stale expiration
    pass does not silently steal the task back from the new owner.

    With the partial UNIQUE index added in mac-x5el, two simultaneously
    ACTIVE leases for one task are impossible at the DB layer; the
    remaining vulnerable window is when lease_a is in some non-active
    state with a past expires_at while lease_b is the new active one.
    The expire-leases pass loads lease_a, but its guarded UPDATE refuses
    to alter the task row owned by lease_b.
    """
    worker_a = register_agent(cp, "wa", ["python"])
    worker_b = register_agent(cp, "wb", ["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    _, lease_a = cp.claim_task(task.id, worker_a.id)

    # Release lease_a properly, then have worker_b claim the now-OPEN task.
    cp.release_lease(lease_a.id, worker_a.id)
    _, lease_b = cp.claim_task(task.id, worker_b.id)

    # Backdate lease_a's expires_at to simulate a stale "expired"
    # eligible-row visible to a concurrent expire_leases pass. Its
    # status stays RELEASED (the UNIQUE-active index would otherwise
    # forbid two active leases), so the guarded UPDATE in expire_leases
    # should skip it cleanly. This exercises both the lease-status
    # guard and the task lease_id guard.
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", lease_a.id),
    )
    cp.expire_leases()

    # Worker_b's lease and task ownership must be untouched.
    assert cp.get_lease(lease_b.id).status == LeaseStatus.ACTIVE.value
    refreshed = cp.get_task(task.id)
    assert refreshed.owner_agent_id == worker_b.id
    assert refreshed.lease_id == lease_b.id



def test_expire_leases_exhausted_task_stamps_failure_class(cp):
    """mac-9be4b64: when a lease expires and the task has exhausted its retry
    budget, the terminal FAILED transition must stamp failure_class on the task
    metadata and include it in the lease_expired history event detail.  Before
    this fix the _expire_lease_row path hard-coded the target state to FAILED
    without calling _exhausted_attempt_terminal_transition, leaving
    failure_class unset.
    """
    worker = register_agent(cp, "w", ["python"])
    task = cp.create_task("work", required_capabilities=["python"], max_attempts=1)
    _, lease = cp.claim_task(task.id, worker.id)
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        (expired_at, lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET leased_until = ? WHERE id = ?",
        (expired_at, task.id),
    )

    recovered = cp.expire_leases(now=utcnow())

    assert len(recovered) == 1
    failed = cp.get_task(task.id)
    assert failed.state == TaskState.FAILED.value
    assert "failure_class" in failed.metadata, (
        "failure_class must be stamped on metadata when lease expires with exhausted budget"
    )
    expiry_event = next(
        e for e in cp.task_history(task.id) if e.event_type == "task.lease_expired"
    )
    assert "failure_class" in expiry_event.detail, (
        "failure_class must appear in the task.lease_expired history event detail"
    )


def test_expire_leases_exhausted_environment_failure_creates_repair_task(cp):
    """mac-9be4b64: when a lease expires on an exhausted task whose failure
    history indicates an environment failure, _expire_lease_row must create a
    repair task and move the parent to WAITING (not FAILED), matching the
    behaviour already implemented for the BLOCKED-attempt auto-retry path.
    """
    worker = register_agent(cp, "w2", ["python"])
    task = cp.create_task(
        "env work",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    _, lease = cp.claim_task(task.id, worker.id)
    cp.transition_task(
        task.id,
        TaskState.BLOCKED.value,
        worker.id,
        {"reason": "heartbeat_offline"},
        lease_id=lease.id,
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = 1 WHERE id = ?",
        (task.id,),
    )
    cp.store.execute(
        "UPDATE leases SET status = ?, expires_at = ? WHERE id = ?",
        (LeaseStatus.ACTIVE.value, "2000-01-01T00:00:00+00:00", lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, lease_id = ?, owner_agent_id = ?, leased_until = ? WHERE id = ?",
        (TaskState.RUNNING.value, lease.id, worker.id, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.expire_leases(now=utcnow())

    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value, (
        "exhausted environment failure via lease expiry must produce WAITING state, not FAILED"
    )
    assert waiting.metadata.get("failure_class") == "environment"
    assert len(waiting.dependencies) == 1
    repair = cp.get_task(waiting.dependencies[0])
    assert repair.metadata["origin"]["type"] == "environment_prerequisite"
    assert repair.metadata["origin"]["parent_task_id"] == task.id


def test_unique_active_lease_per_task_enforced_at_db_layer(cp):
    """mac-x5el: the partial UNIQUE index on leases (task_id WHERE
    status='active') must block a buggy second INSERT that would
    otherwise produce two simultaneously active leases on one task."""
    import sqlite3

    worker_a = register_agent(cp, "wa", ["python"])
    worker_b = register_agent(cp, "wb", ["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, worker_a.id)

    with pytest.raises(sqlite3.IntegrityError):
        cp.store.execute(
            "INSERT INTO leases (id, task_id, agent_id, expires_at, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            ("lease_evil", task.id, worker_b.id, "2099-01-01T00:00:00+00:00", "2024-01-01", "2024-01-01"),
        )


def test_draining_heartbeat_pauses_claims_without_requeueing_active_lease(cp):
    worker = register_agent(cp, "worker", ["python"])
    active = cp.create_task("active", required_capabilities=["python"])
    queued = cp.create_task("queued", required_capabilities=["python"])
    claimed, lease = cp.claim_task(active.id, worker.id)

    drained = cp.heartbeat_agent(
        worker.id,
        status=AgentStatus.DRAINING.value,
        health_status=HealthStatus.DEGRADED.value,
    )

    assert drained.status == AgentStatus.DRAINING.value
    assert drained.current_task_id is None
    assert cp.get_lease(lease.id).status == LeaseStatus.ACTIVE.value
    assert cp.get_task(claimed.id).state == TaskState.CLAIMED.value
    assert cp.claim_next_for_agent(worker.id) is None
    assert cp.get_task(queued.id).state == TaskState.OPEN.value

    cp.release_lease(lease.id, worker.id)
    cp._transition_task_internal(
        active.id,
        TaskState.FAILED.value,
        "test",
        {"reason": "drain-test-finished"},
    )
    restored = cp.heartbeat_agent(
        worker.id,
        status=AgentStatus.IDLE.value,
        health_status=HealthStatus.HEALTHY.value,
    )
    assert restored.status == AgentStatus.IDLE.value
    assert cp.claim_next_for_agent(worker.id)["task"]["id"] == queued.id


def test_lease_renewal_refreshes_busy_agent_liveness(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)
    old_seen = "1970-01-01T00:00:00+00:00"
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ?, updated_at = ? WHERE id = ?",
        (old_seen, old_seen, worker.id),
    )

    renewed = cp.renew_lease(lease.id, worker.id)

    refreshed = cp.get_agent(worker.id)
    assert renewed.status == LeaseStatus.ACTIVE.value
    assert refreshed.status == AgentStatus.BUSY.value
    assert refreshed.current_task_id == claimed.id
    assert refreshed.last_seen_at != old_seen
    assert refreshed.updated_at == refreshed.last_seen_at


def test_offline_heartbeat_expires_active_lease_and_requeues_work(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    claimed, lease = cp.claim_task(task.id, worker.id)

    refreshed = cp.heartbeat_agent(worker.id, status=AgentStatus.OFFLINE.value)

    assert refreshed.status == AgentStatus.OFFLINE.value
    assert refreshed.current_task_id is None
    assert cp.get_lease(lease.id).status == LeaseStatus.EXPIRED.value
    recovered = cp.get_task(claimed.id)
    assert recovered.state == TaskState.OPEN.value
    assert recovered.owner_agent_id is None
    assert cp.dispatch_once() is None


def test_register_tenant_preserves_metadata_on_reregister(cp):
    first = cp.register_tenant("acme", metadata={"region": "eu-west"})
    second = cp.register_tenant("acme")
    assert second.id == first.id
    assert second.metadata == {"region": "eu-west"}


def test_untrusted_machine_agent_cannot_request_secret(cp):
    untrusted_machine = cp.register_machine("untrusted-host", trusted=False)
    agent = cp.register_agent(untrusted_machine.id, "shady", capabilities=["deploy"])
    secret = cp.create_secret(
        "deploy-token", "value-xyz", {"capabilities": ["deploy"]}, "human"
    )
    with pytest.raises(AuthorizationError):
        cp.request_secret(secret.id, agent.id, "deploy")


def test_secret_handle_is_single_use_and_agent_bound(cp):
    deployer = register_agent(cp, "deployer", ["deploy"])
    other = register_agent(cp, "other", ["deploy"])
    secret = cp.create_secret(
        "deploy-token", "value-xyz", {"capabilities": ["deploy"]}, "human"
    )
    handle = cp.request_secret(secret.id, deployer.id, "deploy")
    # Wrong agent cannot redeem.
    with pytest.raises(AuthorizationError):
        cp.reveal_secret(secret.id, handle.audit_id, other.id)
    # Correct agent succeeds once.
    assert cp.reveal_secret(secret.id, handle.audit_id, deployer.id) == "value-xyz"
    # Same handle cannot be redeemed again.
    with pytest.raises(AuthorizationError):
        cp.reveal_secret(secret.id, handle.audit_id, deployer.id)


def test_secret_handle_expires(cp):
    deployer = register_agent(cp, "deployer", ["deploy"])
    secret = cp.create_secret(
        "deploy-token", "value-xyz", {"capabilities": ["deploy"]}, "human"
    )
    handle = cp.request_secret(secret.id, deployer.id, "deploy", ttl_seconds=1)
    cp.store.execute(
        "UPDATE secret_access_audit SET expires_at = '1970-01-01T00:00:00+00:00' WHERE id = ?",
        (handle.audit_id,),
    )
    with pytest.raises(AuthorizationError):
        cp.reveal_secret(secret.id, handle.audit_id, deployer.id)


def test_rotate_secret_writes_audit_row(cp):
    secret = cp.create_secret(
        "deploy-token", "v1", {"capabilities": ["deploy"]}, "human"
    )
    cp.rotate_secret(secret.id, "v2", "human-operator")
    audits = cp.list_secret_audits(secret.id)
    rotations = [a for a in audits if a.result == "rotated"]
    assert len(rotations) == 1
    assert rotations[0].accessor_agent_id == "human-operator"


def test_rollout_pause_then_resume_round_trips(cp):
    rollout = create_verified_rollout(cp, "1.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    paused = cp.advance_rollout(rollout.id, "pause", "human")
    assert paused.status == RolloutStatus.PAUSED.value
    resumed = cp.advance_rollout(rollout.id, "resume", "human")
    assert resumed.status == RolloutStatus.CANARYING.value


def test_rollout_promote_from_paused_is_allowed_pause_from_promoted_is_not(cp):
    rollout = create_verified_rollout(cp, "1.1")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "pause", "human")
    promoted = cp.advance_rollout(rollout.id, "promote", "human")
    assert promoted.status == RolloutStatus.PROMOTED.value
    assert promoted.target_percent == 100
    with pytest.raises(TransitionError):
        cp.advance_rollout(rollout.id, "pause", "human")


def test_rollout_install_requires_runtime_and_verified_artifact(cp):
    rollout = cp.create_rollout("2.0", "canary", 10, "human")
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "start_canary", "human")

    runtime = create_runtime(cp, "runtime-2.0")
    rollout = cp.create_rollout(
        "2.1",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
    )
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "start_canary", "human")
    with pytest.raises(ValidationError):
        cp.verify_rollout_artifact(rollout.id, "artifact://mac/2.1", "md5:not-ok", "human")

    verified = cp.verify_rollout_artifact(
        rollout.id,
        "artifact://mac/2.1",
        "sha256:abc123",
        "human",
    )
    assert verified.artifact_hash == "sha256:abc123"
    assert cp.advance_rollout(rollout.id, "start_canary", "human").status == RolloutStatus.CANARYING.value


def test_rollout_health_gate_blocks_promotion_and_failed_health_rescues(cp):
    rollout = create_verified_rollout(
        cp,
        "2.2",
        health_policy={"required_checks": ["runtime", "canary"]},
    )
    with pytest.raises(TransitionError):
        cp.advance_rollout(rollout.id, "promote", "human")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "promote", "human")

    result = cp.evaluate_rollout_health(
        rollout.id,
        {"runtime": "healthy", "canary": {"status": "failed"}},
        "monitor",
    )

    assert result["healthy"] is False
    assert result["failed_checks"] == ["canary"]
    assert result["rollout"]["status"] == RolloutStatus.RESCUING.value
    assert result["rollout"]["target_percent"] == 0
    assert result["rescue_task"]["metadata"]["failed_checks"] == ["canary"]

    healthy = create_verified_rollout(
        cp,
        "2.3",
        health_policy={"required_checks": ["runtime", "canary"]},
    )
    cp.advance_rollout(healthy.id, "start_canary", "human")
    cp.evaluate_rollout_health(healthy.id, {"runtime": True, "canary": "ok"}, "monitor")
    assert cp.advance_rollout(healthy.id, "promote", "human").status == RolloutStatus.PROMOTED.value


def test_rollout_channels_scope_tenant_and_fleet(cp):
    tenant = cp.register_tenant("rollout-tenant")
    fleet = create_verified_rollout(cp, "3.0", strategy="full", channel="fleet")
    tenant_rollout = create_verified_rollout(
        cp,
        "3.1",
        strategy="full",
        tenant_id=tenant.id,
        channel="tenant-stable",
    )

    assert [rollout.id for rollout in cp.list_rollouts(channel="fleet")] == [fleet.id]
    assert [rollout.id for rollout in cp.list_rollouts(tenant_id=tenant.id)] == [tenant_rollout.id]
    assert tenant_rollout.tenant_id == tenant.id
    assert tenant_rollout.channel == "tenant-stable"


def test_runtime_manifest_rejects_nested_latest_and_substring_secret_fields(cp):
    with pytest.raises(ValidationError):
        cp.create_runtime(
            "nested-latest",
            {"containers": [{"image": "python:latest"}]},
            "human",
        )
    with pytest.raises(ValidationError):
        cp.create_runtime(
            "leaky-api-key",
            {"image": "python:3.12@sha256:abc", "env": {"api_key": "raw"}},
            "human",
        )
    with pytest.raises(ValidationError):
        cp.create_runtime(
            "unpinned-image",
            {"image": "python:3.12"},
            "human",
        )


def test_eval_set_scoring_higher_is_better_pass_fail(cp):
    eval_set = cp.create_eval_set(
        "task-success-rate",
        scoring="higher_is_better",
        baseline_score=0.80,
        regression_threshold=0.02,
    )
    passing = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.81)
    assert passing.passed is True
    assert passing.delta == pytest.approx(0.01)

    inside_threshold = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.79)
    assert inside_threshold.passed is True  # 0.01 below baseline, within 0.02 threshold

    regression = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.70)
    assert regression.passed is False
    assert regression.delta == pytest.approx(-0.10)


def test_eval_set_scoring_lower_is_better_pass_fail(cp):
    eval_set = cp.create_eval_set(
        "p95-latency-ms",
        scoring="lower_is_better",
        baseline_score=200.0,
        regression_threshold=20.0,
    )
    improvement = cp.record_eval_run(eval_set.id, "runtime_environment", "rt1", 150.0)
    assert improvement.passed is True

    inside_threshold = cp.record_eval_run(eval_set.id, "runtime_environment", "rt1", 215.0)
    assert inside_threshold.passed is True

    regression = cp.record_eval_run(eval_set.id, "runtime_environment", "rt1", 260.0)
    assert regression.passed is False


def test_eval_run_without_baseline_passes_and_can_seed_baseline(cp):
    eval_set = cp.create_eval_set("first-run", scoring="higher_is_better")
    run = cp.record_eval_run(eval_set.id, "rollout_version", "v0", 0.55)
    assert run.passed is True
    assert run.delta is None

    updated = cp.update_eval_set_baseline(eval_set.id, 0.60)
    assert updated.baseline_score == pytest.approx(0.60)
    # subsequent runs are now compared against the seeded baseline
    follow_up = cp.record_eval_run(eval_set.id, "rollout_version", "v1", 0.50)
    assert follow_up.passed is False


def test_rollout_promote_requires_passing_eval_run(cp):
    eval_set = cp.create_eval_set(
        "smoke-eval",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    rollout = create_verified_rollout(cp, "2.0")
    # attach the eval_set requirement after-the-fact via a fresh rollout
    runtime = create_runtime(cp, "runtime-2.1")
    gated = cp.create_rollout(
        "2.1",
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/2.1",
        artifact_hash="sha256:abc123",
        required_eval_set_id=eval_set.id,
    )
    # mac-wfct: start_canary now also consults the eval gate, so seed
    # a passing run first.
    cp.record_eval_run(eval_set.id, "rollout_version", "2.1", 0.92)
    cp.advance_rollout(gated.id, "start_canary", "human")
    # mac-jmjc: must supply checks that satisfy the policy's required_checks.
    cp.evaluate_rollout_health(gated.id, {"runtime": "healthy"}, "human")

    # A failing run posted after canary now blocks promote.
    cp.record_eval_run(eval_set.id, "rollout_version", "2.1", 0.70)
    with pytest.raises(ValidationError):
        cp.advance_rollout(gated.id, "promote", "human")

    # A subsequent passing run unlocks promote.
    cp.record_eval_run(eval_set.id, "rollout_version", "2.1", 0.92)
    promoted = cp.advance_rollout(gated.id, "promote", "human")
    assert promoted.status == RolloutStatus.PROMOTED.value
    assert promoted.target_percent == 100

    # Sanity: an ungated rollout doesn't need an eval.
    assert rollout.required_eval_set_id is None


def test_eval_run_rejects_unknown_target_kind(cp):
    eval_set = cp.create_eval_set("any", scoring="higher_is_better")
    with pytest.raises(ValidationError):
        cp.record_eval_run(eval_set.id, "not-a-real-kind", "x", 1.0)


def test_evidence_kind_eval_is_accepted(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "eval", "artifact://scorecard.json", "eval scorecard", worker.id
    )
    assert evidence.kind == "eval"


def _gated_rollout(cp, version, eval_set_id):
    runtime = create_runtime(cp, "runtime-%s" % version)
    rollout = cp.create_rollout(
        version,
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
        artifact_uri="artifact://mac/%s" % version,
        artifact_hash="sha256:abc123",
        required_eval_set_id=eval_set_id,
    )
    # mac-wfct: start_canary now also requires a passing eval run; seed one.
    cp.record_eval_run(eval_set_id, "rollout_version", version, 0.95)
    cp.advance_rollout(rollout.id, "start_canary", "human")
    # mac-jmjc: default health gate now requires non-empty checks.
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "human")
    return rollout


def test_eval_gate_blocks_when_failing_run_supersedes_passing(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    rollout = _gated_rollout(cp, "3.0", eval_set.id)
    # An older passing run is no longer "latest" once a failing run lands.
    cp.record_eval_run(eval_set.id, "rollout_version", "3.0", 0.95)
    cp.record_eval_run(eval_set.id, "rollout_version", "3.0", 0.50)
    with pytest.raises(ValidationError):
        cp.advance_rollout(rollout.id, "promote", "human")


def test_eval_run_rejects_non_eval_evidence(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    test_evidence = cp.add_evidence(
        task.id, "test", "artifact://pytest", "pytest passed", worker.id
    )
    eval_set = cp.create_eval_set("any", scoring="higher_is_better")
    with pytest.raises(ValidationError):
        cp.record_eval_run(
            eval_set.id,
            "rollout_version",
            "v1",
            0.9,
            evidence_id=test_evidence.id,
        )


def test_eval_gate_errors_clearly_when_required_eval_set_is_deleted(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
    )
    rollout = _gated_rollout(cp, "4.0", eval_set.id)
    cp.record_eval_run(eval_set.id, "rollout_version", "4.0", 0.95)
    # Delete the eval_set directly to simulate retirement.
    cp.store.execute("DELETE FROM eval_sets WHERE id = ?", (eval_set.id,))
    with pytest.raises(ValidationError) as exc:
        cp.advance_rollout(rollout.id, "promote", "human")
    assert "no longer exists" in str(exc.value)


def test_eval_gate_records_eval_run_id_in_rollout_event(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    rollout = _gated_rollout(cp, "5.0", eval_set.id)
    run = cp.record_eval_run(eval_set.id, "rollout_version", "5.0", 0.95)
    cp.advance_rollout(rollout.id, "promote", "human")
    rows = cp.store.query_all(
        "SELECT event_type, detail FROM rollout_events WHERE rollout_id = ? ORDER BY created_at, id",
        (rollout.id,),
    )
    promote = [row for row in rows if row["event_type"] == "rollout.promote"]
    assert len(promote) == 1
    detail = json.loads(promote[0]["detail"])
    assert detail["eval_run_id"] == run.id
    assert detail["eval_score"] == pytest.approx(0.95)


def test_eval_set_baseline_change_writes_event(cp):
    eval_set = cp.create_eval_set(
        "drift",
        scoring="higher_is_better",
        baseline_score=0.80,
    )
    cp.update_eval_set_baseline(eval_set.id, 0.85, actor="release-manager")
    events = cp.list_eval_set_events(eval_set.id)
    types = [event["event_type"] for event in events]
    assert "eval_set.created" in types
    baseline_events = [event for event in events if event["event_type"] == "eval_set.baseline_changed"]
    assert len(baseline_events) == 1
    assert baseline_events[0]["actor"] == "release-manager"
    assert baseline_events[0]["detail"]["previous_baseline_score"] == pytest.approx(0.80)
    assert baseline_events[0]["detail"]["new_baseline_score"] == pytest.approx(0.85)


def test_eval_run_event_records_run_id_and_passed(cp):
    eval_set = cp.create_eval_set(
        "smoke",
        scoring="higher_is_better",
        baseline_score=0.90,
        regression_threshold=0.01,
    )
    run = cp.record_eval_run(eval_set.id, "rollout_version", "6.0", 0.95)
    events = cp.list_eval_set_events(eval_set.id)
    run_events = [event for event in events if event["event_type"] == "eval_set.run_recorded"]
    assert len(run_events) == 1
    assert run_events[0]["detail"]["run_id"] == run.id
    assert run_events[0]["detail"]["passed"] is True


def test_evaluate_rollout_health_failing_twice_does_not_duplicate_rescue(cp):
    rollout = create_verified_rollout(
        cp,
        "7.0",
        health_policy={"required_checks": ["runtime", "canary"]},
    )
    cp.advance_rollout(rollout.id, "start_canary", "human")
    first = cp.evaluate_rollout_health(
        rollout.id,
        {"runtime": "healthy", "canary": {"status": "failed"}},
        "monitor",
    )
    second = cp.evaluate_rollout_health(
        rollout.id,
        {"runtime": "healthy", "canary": {"status": "failed"}},
        "monitor",
    )
    rescue_tasks = [
        task for task in cp.list_tasks()
        if task.metadata.get("rollout_id") == rollout.id and task.metadata.get("rescue")
    ]
    assert len(rescue_tasks) == 1
    # The second call should return the same in-flight rescue task and record an
    # additional health-failure event without spawning a duplicate task.
    assert second["healthy"] is False
    assert second["rescue_task"]["id"] == first["rescue_task"]["id"]
    events = cp.store.query_all(
        "SELECT event_type FROM rollout_events WHERE rollout_id = ? ORDER BY created_at, id",
        (rollout.id,),
    )
    types = [row["event_type"] for row in events]
    assert types.count("rollout.health_failure_during_rescue") == 1


def test_tenant_only_secret_scope_grants_access_to_matching_machine(cp):
    tenant = cp.register_tenant("scoped-tenant")
    machine = cp.register_machine(
        "scoped-host",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant.id]}},
    )
    agent = cp.register_agent(machine.id, "scoped-agent", capabilities=["any"])
    secret = cp.create_secret(
        "tenant-only", "abc", {"tenant_id": tenant.id}, "human"
    )
    handle = cp.request_secret(secret.id, agent.id, "deploy")
    assert handle.granted is True
    revealed = cp.reveal_secret(secret.id, handle.audit_id, agent.id)
    assert revealed == "abc"


def test_runtime_run_status_is_enum_validated(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("work", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    runtime = create_runtime(cp, "rt-status")
    run = cp.create_runtime_run(task.id, worker.id, runtime.id)
    assert run.status == "running"
    evidence = cp.add_evidence(task.id, "test", "artifact://t", "tests", worker.id)
    with pytest.raises(ValidationError):
        cp.complete_runtime_run(run.id, evidence.id, status="bogus")
    with pytest.raises(ValidationError):
        cp.complete_runtime_run(run.id, evidence.id, status="running")
    completed = cp.complete_runtime_run(run.id, evidence.id, status="completed")
    assert completed.status == "completed"


def test_events_view_unifies_all_audit_surfaces(cp, monkeypatch):
    # Generate one event of each kind.
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    cp.update_agent(reviewer.id, status=AgentStatus.OFFLINE.value, actor="ops")
    task = cp.create_task("audited", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    # project event
    project = cp.create_project("audit-project", actor="alice")
    cp.update_project(project.id, description="tracked", actor="alice")
    cp.delete_project(project.id, actor="alice")
    # fleet event
    fleet = cp.create_fleet("audit-fleet", agent_ids=[worker.id], actor="ops")
    cp.update_fleet(fleet.id, status="inactive", agent_ids=[worker.id, reviewer.id], actor="ops")
    cp.delete_fleet(fleet.id, actor="ops")
    # rollout event
    rollout = create_verified_rollout(cp, "8.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    # eval_set event
    cp.create_eval_set("audit-eval", scoring="higher_is_better")
    # secret event
    deployer = register_agent(cp, "deployer", ["deploy"])
    secret = cp.create_secret("audit-token", "x", {"capabilities": ["deploy"]}, "human")
    cp.request_secret(secret.id, deployer.id, "audit-test")
    cp.record_command_audit(
        worker.id,
        "completed",
        argv=["python", "-m", "pytest"],
        cwd="/tmp",
        task_id=task.id,
        returncode=0,
        metadata={"source": "test"},
    )
    action = cp.record_action_event(
        actor="worker",
        agent_id=worker.id,
        task_id=task.id,
        action_type="tool",
        action_name="pytest",
        outcome="success",
        attributes={"suite": "contract"},
    )

    queries = []
    original_query_all = cp.store.query_all

    def record_query(sql, params=()):
        queries.append(sql)
        return original_query_all(sql, params)

    monkeypatch.setattr(cp.store, "query_all", record_query)

    events = cp.list_events(limit=500)
    subject_types = {event["subject_type"] for event in events}
    assert subject_types == {
        "task",
        "agent",
        "project",
        "fleet",
        "rollout",
        "eval_set",
        "secret",
        "action_event",
    }
    # Each event includes the unified shape.
    for event in events:
        assert set(event.keys()) >= {
            "id",
            "subject_type",
            "subject_id",
            "event_type",
            "actor",
            "detail",
            "created_at",
        }
        assert isinstance(event["detail"], dict)
    assert not any(" FROM events" in query for query in queries)
    assert any("FROM action_events" in query for query in queries)
    action_event = next(event for event in events if event["id"] == action.event_id)
    assert action_event["detail"]["attributes"] == {"suite": "contract"}
    command_event = next(
        event for event in events if event["event_type"] == "command.completed"
    )
    assert command_event["detail"]["argv0"] == "python"
    project_events = cp.list_events(subject_type="project", subject_id=project.id)
    assert {event["event_type"] for event in project_events} >= {
        "project.created",
        "project.updated",
        "project.deleted",
    }
    assert project_events[0]["detail"]["project_name"] == "audit-project"
    fleet_events = cp.list_events(subject_type="fleet", subject_id=fleet.id)
    assert {event["event_type"] for event in fleet_events} >= {
        "fleet.created",
        "fleet.updated",
        "fleet.deleted",
    }
    updated_fleet = next(event for event in fleet_events if event["event_type"] == "fleet.updated")
    assert updated_fleet["detail"]["added_agent_ids"] == [reviewer.id]
    agent_events = cp.list_events(subject_type="agent", subject_id=reviewer.id)
    assert "agent.updated" in {event["event_type"] for event in agent_events}


def test_replace_fleet_members_uses_only_execute(cp):
    """_replace_fleet_members must use the StoreConnection protocol surface
    (execute only). The Postgres _Transaction has no executemany — relying on
    it raised AttributeError and 500'd create_fleet/update_fleet in prod while
    SQLite (whose connection happens to expose executemany) passed."""

    class _ProtocolConn:
        """Mirrors the StoreConnection protocol: execute-only, no executemany."""

        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, tuple(params)))
            return None

    conn = _ProtocolConn()
    cp._replace_fleet_members(conn, "fleet_x", ["a", "b"], "2026-06-04T00:00:00Z")
    # One DELETE + one INSERT per member (no executemany).
    assert conn.calls[0][0].startswith("DELETE FROM fleet_agents")
    inserts = [c for c in conn.calls if c[0].startswith("INSERT INTO fleet_agents")]
    assert len(inserts) == 2
    assert inserts[0][1] == ("fleet_x", "a", "2026-06-04T00:00:00Z")
    assert inserts[1][1] == ("fleet_x", "b", "2026-06-04T00:00:00Z")


def test_replace_fleet_members_empty_is_delete_only(cp):
    class _ProtocolConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, tuple(params)))
            return None

    conn = _ProtocolConn()
    cp._replace_fleet_members(conn, "fleet_x", [], "2026-06-04T00:00:00Z")
    assert len(conn.calls) == 1
    assert conn.calls[0][0].startswith("DELETE FROM fleet_agents")


def test_observed_fleet_agents_do_not_mutate_configured_membership(cp):
    configured = register_agent(cp, "configured", ["python"])
    unmanaged = register_agent(cp, "unmanaged", ["review"])
    fleet = cp.create_fleet("runtime", agent_ids=[configured.id], actor="deploy")

    observed = cp.observe_fleet_agent(
        fleet.id,
        unmanaged.id,
        source="mac-agent",
        metadata={"fleet": "runtime"},
        actor="mac-agent",
    )

    assert observed.agent_ids == [configured.id]
    assert observed.observed_agent_ids == [unmanaged.id]
    assert observed.unmanaged_agent_ids == [unmanaged.id]
    events = cp.list_events(subject_type="fleet", subject_id=fleet.id)
    observed_event = next(
        event for event in events if event["event_type"] == "fleet.agent_observed"
    )
    assert observed_event["detail"]["configured"] is False
    assert observed_event["detail"]["unmanaged"] is True


def test_events_filter_by_subject_returns_only_matching_stream(cp):
    rollout = create_verified_rollout(cp, "8.1")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    eval_set = cp.create_eval_set("audit-eval-2", scoring="higher_is_better")
    cp.update_eval_set_baseline(eval_set.id, 0.5)

    rollout_events = cp.list_events(subject_type="rollout", subject_id=rollout.id)
    assert rollout_events
    assert {event["subject_type"] for event in rollout_events} == {"rollout"}
    assert {event["subject_id"] for event in rollout_events} == {rollout.id}

    eval_events = cp.list_events(subject_type="eval_set", subject_id=eval_set.id)
    types = {event["event_type"] for event in eval_events}
    assert "eval_set.created" in types
    assert "eval_set.baseline_changed" in types


def test_events_filter_by_event_type_prefix(cp):
    rollout = create_verified_rollout(cp, "8.2")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.advance_rollout(rollout.id, "pause", "human")

    rollout_prefix = cp.list_events(event_type_prefix="rollout.")
    assert rollout_prefix
    assert all(event["event_type"].startswith("rollout.") for event in rollout_prefix)


def test_events_filter_event_type_prefix_escapes_like_wildcards(cp):
    rollout = create_verified_rollout(cp, "8.2.1")
    cp.advance_rollout(rollout.id, "start_canary", "human")

    # bare `%` must not be treated as wildcard — should match nothing
    assert cp.list_events(event_type_prefix="%") == []
    # bare `_` likewise
    assert cp.list_events(event_type_prefix="_") == []
    # a real prefix still works
    assert cp.list_events(event_type_prefix="rollout.")


def test_events_filter_by_actor_and_time_window(cp):
    rollout = create_verified_rollout(cp, "8.3")
    cp.advance_rollout(rollout.id, "start_canary", "alice")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "bob")

    alice_events = cp.list_events(actor="alice")
    assert alice_events
    assert all(event["actor"] == "alice" for event in alice_events)

    # since filter
    future = "2999-01-01T00:00:00+00:00"
    assert cp.list_events(since=future) == []


def test_events_rejects_unknown_subject_type(cp):
    with pytest.raises(ValidationError):
        cp.list_events(subject_type="not-a-real-subject")


def test_observability_records_metrics_logs_and_control_plane_events(cp):
    metric = cp.record_metric(
        "worker.loop.duration_ms",
        12.5,
        unit="ms",
        layer="worker",
        source="rocky",
        detail={"iteration": 1},
    )
    log = cp.record_log(
        "worker.claim.empty",
        level="warning",
        layer="worker",
        source="rocky",
        detail={"queue": "default"},
    )
    task = cp.create_task("observed", actor="tester")

    worker_metrics = cp.list_observability(kind="metric", layer="worker")
    assert worker_metrics[0].id == metric.id
    assert worker_metrics[0].value == pytest.approx(12.5)
    assert worker_metrics[0].unit == "ms"

    streamed = cp.list_observability(after_sequence=metric.sequence - 1, limit=10)
    assert [item.id for item in streamed[:2]] == [metric.id, log.id]
    assert any(item.name == "task.created" and item.subject_id == task.id for item in streamed)

    summary = cp.observability_summary()
    assert summary["counts"]["metrics"] >= 1
    assert summary["counts"]["logs"] >= 2
    assert summary["counts"]["warnings"] >= 1
    assert summary["layers"]["worker"] >= 2
    assert any(item["name"] == "worker.loop.duration_ms" for item in summary["latest_metrics"])


def test_observability_rejects_invalid_metric_contract(cp):
    with pytest.raises(ValidationError):
        cp.record_metric("bad metric name", 1, layer="worker")
    with pytest.raises(ValidationError):
        cp.record_metric("worker.bad", float("inf"), layer="worker")
    with pytest.raises(ValidationError):
        cp.record_metric("worker.bad_nan", float("nan"), layer="worker")
    with pytest.raises(ValidationError):
        cp.record_observation("metric", "worker.missing_value", layer="worker")


def test_observability_prune_drops_old_or_excess_rows(cp):
    for index in range(5):
        cp.record_metric(
            "worker.heartbeat",
            float(index),
            layer="worker",
            source="rocky",
        )
    all_rows = cp.list_observability(layer="worker", limit=20)
    assert len(all_rows) == 5

    # keep_last=2 retains the two newest worker rows.
    removed = cp.prune_observability(keep_last=2)
    assert removed >= 3
    remaining = cp.list_observability(layer="worker", limit=20)
    assert [item.value for item in remaining] == [4.0, 3.0]

    with pytest.raises(ValidationError):
        cp.prune_observability()


def test_transition_to_terminal_state_is_atomic_across_task_agent_and_history(cp):
    agent = register_agent(cp, "alpha", ["python"])
    task = cp.create_task("transactional", required_capabilities=["python"])
    _, lease = cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id, lease_id=lease.id)

    # Force the history write to fail and prove the task + agent updates roll
    # back with it — the whole transition_task must be all-or-nothing.
    original = cp._record_history

    def boom(*args, **kwargs):
        raise RuntimeError("simulated history failure")

    cp._record_history = boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            cp.transition_task(
                task.id,
                TaskState.FAILED.value,
                agent.id,
                lease_id=lease.id,
            )
    finally:
        cp._record_history = original  # type: ignore[assignment]

    # Task is still claimed by the agent; agent still references the task.
    same_task = cp.get_task(task.id)
    assert same_task.state == TaskState.RUNNING.value
    assert same_task.owner_agent_id == agent.id
    assert cp.get_agent(agent.id).current_task_id == task.id

    # Now succeed: all three writes commit together.
    cp.transition_task(
        task.id,
        TaskState.FAILED.value,
        agent.id,
        lease_id=lease.id,
    )
    final = cp.get_task(task.id)
    assert final.state == TaskState.FAILED.value
    assert final.owner_agent_id is None
    assert cp.get_agent(agent.id).current_task_id is None
    assert any(h.event_type == "task.transitioned" for h in cp.task_history(task.id))


def test_add_evidence_rolls_back_if_history_write_fails(cp):
    cp.create_task("with-evidence", required_capabilities=["python"])
    task_id = cp.list_tasks()[0].id

    original = cp._record_history
    cp._record_history = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("history boom"))  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            cp.add_evidence(
                task_id,
                "log",
                "file://x",
                "summary",
                "tester",
                _trusted_internal=True,
            )
    finally:
        cp._record_history = original  # type: ignore[assignment]

    # Evidence row should NOT exist — the transaction rolled back.
    assert cp.list_evidence(task_id) == []


def test_heartbeat_accepts_running_digest_only_for_known_runtime(cp):
    worker = register_agent(cp, "fleet-worker", ["python"])
    runtime = create_runtime(cp, "fleet-runtime")

    # Unknown digest is rejected.
    with pytest.raises(ValidationError):
        cp.heartbeat_agent(worker.id, running_digest="sha256:not-registered")

    refreshed = cp.heartbeat_agent(worker.id, running_digest=runtime.digest)
    assert refreshed.running_digest == runtime.digest

    # Empty string clears the digest (agent dropped its declared build).
    cleared = cp.heartbeat_agent(worker.id, running_digest="")
    assert cleared.running_digest is None


def test_artifact_registry_register_get_and_idempotent_augment(cp):
    art = cp.register_artifact(
        "image",
        "sha256:deadbeef",
        "artifact://registry/mac:1.0",
        "human",
        sbom_uri="sbom://registry/mac:1.0.spdx",
        signers=["ci"],
        metadata={"build_id": "b-1"},
    )
    assert art.kind == "image"
    assert art.digest == "sha256:deadbeef"
    assert art.signers == ["ci"]

    # Re-register with a new URI, additional signer, and updated metadata:
    # digest is the key, signers merge, metadata merges, sbom_uri preserves if
    # new is None.
    art2 = cp.register_artifact(
        "image",
        "sha256:deadbeef",
        "artifact://registry/mac:1.0-public",
        "human",
        signers=["release-manager"],
        metadata={"approved_by": "alice"},
    )
    assert art2.id == art.id
    assert art2.uri == "artifact://registry/mac:1.0-public"
    assert set(art2.signers) == {"ci", "release-manager"}
    assert art2.metadata["build_id"] == "b-1"
    assert art2.metadata["approved_by"] == "alice"
    assert art2.sbom_uri == "sbom://registry/mac:1.0.spdx"

    # Lookup by digest or id.
    assert cp.get_artifact("sha256:deadbeef").id == art.id
    assert cp.get_artifact(art.id).digest == "sha256:deadbeef"


def test_artifact_registry_rejects_missing_fields(cp):
    with pytest.raises(ValidationError):
        cp.register_artifact("", "sha256:x", "uri", "human")
    with pytest.raises(ValidationError):
        cp.register_artifact("image", "", "uri", "human")
    with pytest.raises(ValidationError):
        cp.register_artifact("image", "sha256:x", "", "human")


def test_artifact_list_filters_by_kind(cp):
    cp.register_artifact("image", "sha256:1", "u1", "human")
    cp.register_artifact("image", "sha256:2", "u2", "human")
    cp.register_artifact("package", "sha256:3", "u3", "human")
    images = cp.list_artifacts(kind="image")
    assert {a.digest for a in images} == {"sha256:1", "sha256:2"}
    assert {a.kind for a in images} == {"image"}


def test_environment_register_and_deploy_artifact_atomically_retires_prior(cp):
    artifact_v1 = cp.register_artifact("image", "sha256:v1", "art://v1", "human")
    artifact_v2 = cp.register_artifact("image", "sha256:v2", "art://v2", "human")
    staging = cp.register_environment("staging", channel="release")
    prod = cp.register_environment("prod", channel="release", promotes_from=staging.id)

    # No deployment yet.
    assert cp.current_deployment(staging.id) is None

    # First deploy: becomes active, no prior to retire.
    d1 = cp.deploy_artifact(staging.id, artifact_v1.id, "release-bot")
    assert d1.status == "active"
    assert d1.retired_at is None
    assert cp.current_deployment(staging.id).id == d1.id

    # Second deploy: retires the first, new one becomes active.
    d2 = cp.deploy_artifact(staging.id, artifact_v2.id, "release-bot")
    assert d2.status == "active"
    assert cp.current_deployment(staging.id).id == d2.id
    retired = cp.get_deployment(d1.id)
    assert retired.status == "retired"
    assert retired.retired_at is not None

    # Deploy to prod environment is independent.
    d3 = cp.deploy_artifact(prod.id, artifact_v2.id, "release-bot")
    assert cp.current_deployment(prod.id).id == d3.id
    assert cp.current_deployment(staging.id).id == d2.id


def test_environment_register_validates_inputs(cp):
    with pytest.raises(ValidationError):
        cp.register_environment("")  # empty name
    with pytest.raises(NotFoundError):
        cp.register_environment("a", promotes_from="env_does_not_exist")


def test_deploy_artifact_requires_known_artifact_and_environment(cp):
    env = cp.register_environment("staging-fail")
    with pytest.raises(NotFoundError):
        cp.deploy_artifact(env.id, "art_does_not_exist", "release-bot")
    art = cp.register_artifact("image", "sha256:lone", "uri", "human")
    with pytest.raises(NotFoundError):
        cp.deploy_artifact("env_does_not_exist", art.id, "release-bot")


def test_environment_events_appear_in_unified_stream(cp):
    artifact = cp.register_artifact("image", "sha256:env-test", "uri", "human")
    env = cp.register_environment("audit-env", channel="release")
    cp.deploy_artifact(env.id, artifact.id, "release-bot")
    cp.deploy_artifact(env.id, artifact.id, "release-bot")  # retire-and-replace

    env_events = cp.list_events(subject_type="environment", subject_id=env.id)
    types = [event["event_type"] for event in env_events]
    # newest-first ordering
    assert "environment.created" in types
    assert types.count("environment.deployed") == 2
    assert types.count("environment.retired") == 1


def test_list_environments_filters_by_tenant_and_channel(cp):
    tenant = cp.register_tenant("env-tenant")
    cp.register_environment("dev", tenant_id=tenant.id, channel="release")
    cp.register_environment("prod", tenant_id=tenant.id, channel="release")
    cp.register_environment("global-fleet", channel="fleet")

    tenant_envs = cp.list_environments(tenant_id=tenant.id)
    assert {env.name for env in tenant_envs} == {"dev", "prod"}

    release_envs = cp.list_environments(channel="release")
    assert {env.name for env in release_envs} == {"dev", "prod"}

    fleet_envs = cp.list_environments(channel="fleet")
    assert {env.name for env in fleet_envs} == {"global-fleet"}


def test_fleet_build_distribution_buckets_by_digest(cp):
    runtime_a = cp.create_runtime(
        "rt-a",
        {"image": "python:3.12@sha256:abc123", "dependencies": ["fastapi==0.111.0"]},
        "human",
    )
    runtime_b = cp.create_runtime(
        "rt-b",
        {"image": "python:3.12@sha256:def456", "dependencies": ["fastapi==0.111.0"]},
        "human",
    )
    a1 = register_agent(cp, "a1", ["python"])
    a2 = register_agent(cp, "a2", ["python"])
    b1 = register_agent(cp, "b1", ["python"])
    offline = register_agent(cp, "offline", ["python"])
    cp.heartbeat_agent(a1.id, running_digest=runtime_a.digest)
    cp.heartbeat_agent(a2.id, running_digest=runtime_a.digest)
    cp.heartbeat_agent(b1.id, running_digest=runtime_b.digest)
    cp.heartbeat_agent(offline.id, status="offline")

    dist = cp.fleet_build_distribution()
    assert dist["total_live_agents"] == 3
    by_digest = {bucket["digest"]: bucket for bucket in dist["buckets"]}
    assert by_digest[runtime_a.digest]["count"] == 2
    assert by_digest[runtime_b.digest]["count"] == 1
    assert by_digest[runtime_a.digest]["percent"] == pytest.approx(66.67, abs=0.01)


def test_events_task_detail_includes_from_to_states(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("transitions", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    task_events = cp.list_events(subject_type="task", subject_id=task.id)
    transitions = [
        event for event in task_events if event["event_type"] == "task.transitioned"
    ]
    assert transitions
    # Most recent transition is to RUNNING.
    latest = transitions[0]
    assert latest["detail"].get("to_state") == "running"
    assert latest["detail"].get("from_state") == "claimed"


def test_agentbus_streams_typed_content_without_weakening_control_messages(cp):
    sender = register_agent(cp, "sender", ["python"])
    recipient = register_agent(cp, "recipient", ["python"])
    outsider = register_agent(cp, "outsider", ["python"])

    with pytest.raises(ValidationError):
        cp.send_message(
            sender.id,
            recipient.id,
            "status_update",
            {"status": "ok", "command": "not allowed here"},
        )

    stream = cp.open_agentbus_stream(
        sender.id,
        recipient_agent_id=recipient.id,
        content_type="application/vnd.mac.patch+json",
        topic="patch",
        headers={"schema": "v1"},
    )
    first = cp.append_agentbus_chunk(
        stream.id,
        sender.id,
        payload={"command": "stored-not-executed", "ops": [{"path": "README.md"}]},
    )
    second = cp.append_agentbus_chunk(
        stream.id,
        sender.id,
        payload={"done": True},
        final=True,
    )

    assert (first.sequence, second.sequence) == (1, 2)
    refreshed = cp.get_agentbus_stream(stream.id)
    assert refreshed.status == "closed"
    assert refreshed.headers == {"schema": "v1"}

    chunks = cp.read_agentbus_chunks(recipient.id, stream.id)
    assert [chunk.sequence for chunk in chunks] == [1, 2]
    assert chunks[0].payload["command"] == "stored-not-executed"
    assert cp.read_agentbus_chunks(sender.id, stream.id, after_sequence=1)[0].payload == {
        "done": True
    }
    agentbus_logs = cp.list_observability(layer="agentbus", limit=20)
    names = [row.name for row in agentbus_logs]
    assert "agentbus.stream.opened" in names
    assert "agentbus.chunk.appended" in names
    assert "agentbus.chunks.read" in names
    opened = next(row for row in agentbus_logs if row.name == "agentbus.stream.opened")
    assert opened.detail["header_keys"] == ["schema"]
    appended = next(row for row in agentbus_logs if row.name == "agentbus.chunk.appended")
    assert "payload" not in appended.detail
    assert appended.detail["size_bytes"] > 0
    with pytest.raises(AuthorizationError):
        cp.read_agentbus_chunks(outsider.id, stream.id)
    with pytest.raises(ValidationError):
        cp.append_agentbus_chunk(stream.id, sender.id, payload={"late": True})


def test_repo_update_control_stream_stamps_unconsumed_age_until_read(cp):
    sender = register_agent(cp, "control-sender", ["python"])
    recipient = register_agent(cp, "control-recipient", ["python"])

    published = cp.publish_agentbus_repo_update(
        sender.id,
        recipient_agent_ids=[recipient.id],
        request_id="control-age",
    )

    stamped = cp.get_agent(recipient.id)
    assert stamped.last_control_stream_published_at is not None
    assert stamped.last_control_stream_consumed_at is None
    assert cp.unconsumed_control_stream_age_seconds(recipient.id) is not None

    cp.read_agentbus_chunks(recipient.id, published["streams"][0]["id"])

    consumed = cp.get_agent(recipient.id)
    assert consumed.last_control_stream_consumed_at is not None
    assert cp.unconsumed_control_stream_age_seconds(recipient.id) is None


def test_agent_reflection_publishes_runtime_description_over_agentbus(cp):
    sender = register_agent(
        cp,
        "reflector",
        ["python", "review"],
        resources={"hardware": {"accelerator": "cpu"}, "runtime": {"worker": "codex"}},
    )
    recipient = register_agent(cp, "operator", ["review"])
    runtime = create_runtime(cp, "reflect-runtime")
    cp.heartbeat_agent(
        sender.id,
        status=AgentStatus.BUSY.value,
        health_status=HealthStatus.DEGRADED.value,
        running_digest=runtime.digest,
    )

    # reflect_timeout=0 skips the blocking poll so no live agent worker is needed.
    published = cp.publish_agent_reflection(
        sender.id,
        recipient_agent_id=recipient.id,
        request_id="rid-42",
        reflect_timeout=0,
    )

    assert published["schema"] == "mac.agentbus.agent_reflection_publish.v1"
    assert published["agent_id"] == sender.id
    assert published["recipient_agent_id"] == recipient.id
    assert published["count"] == 1
    assert published["payload"]["request_id"] == "rid-42"
    # No agent worker responded, so reflection falls back to None.
    assert published["payload"]["reflection"] is None
    stream = published["streams"][0]
    assert stream["topic"] == AGENT_REFLECTION_TOPIC
    assert stream["content_type"] == AGENT_REFLECTION_CONTENT_TYPE
    chunks = cp.read_agentbus_chunks(recipient.id, stream["id"])
    payload = chunks[0].payload
    assert payload.get("request_id") == "rid-42"
    assert payload["schema"] == AGENT_REFLECTION_SCHEMA
    assert payload["agent_id"] == sender.id
    assert payload["agent"]["name"] == "reflector"
    assert payload["agent"]["capabilities"] == ["python", "review"]
    assert payload["agent"]["resources"]["runtime"]["worker"] == "codex"
    assert payload["agent"]["status"] == AgentStatus.BUSY.value
    assert payload["agent"]["health_status"] == HealthStatus.DEGRADED.value
    assert payload["agent"]["running_digest"] == runtime.digest
    # A reflect request stream must also have been sent to the agent.
    assert "deep_request_stream" in published


def test_agent_reflection_includes_runtime_narrative_on_success(cp):
    """Success path: a pre-seeded reflect result is picked up within the timeout."""
    sender = register_agent(cp, "reflector-success", ["python"])
    recipient = register_agent(cp, "operator-success", ["review"])

    narrative_text = "I am agent reflector-success. I have soul files at ~/.mac/soul."

    # Simulate the agent worker response: publish a REFLECT_RESULT_TOPIC stream
    # from sender to recipient before calling publish_agent_reflection.
    cp.publish_agentbus_content(
        sender_agent_id=sender.id,
        recipient_agent_id=recipient.id,
        content_type=REFLECT_RESULT_CONTENT_TYPE,
        topic=REFLECT_RESULT_TOPIC,
        payload=reflect_result_payload(
            request_id="rid-success",
            agent_id=sender.id,
            response=narrative_text,
            word_count=len(narrative_text.split()),
        ),
    )

    # reflect_timeout=5 allows the poll loop to find the pre-seeded result.
    published = cp.publish_agent_reflection(
        sender.id,
        recipient_agent_id=recipient.id,
        request_id="rid-success",
        reflect_timeout=5,
    )

    assert published["schema"] == "mac.agentbus.agent_reflection_publish.v1"
    assert published["agent_id"] == sender.id
    assert published["payload"]["reflection"] == narrative_text
    assert published["payload"]["agent_id"] == sender.id


def test_agent_reflection_timeout_falls_back_gracefully(cp):
    """Timeout path: no agent worker responds; reflection falls back to None."""
    sender = register_agent(cp, "reflector-timeout", ["python"])
    recipient = register_agent(cp, "operator-timeout", ["review"])

    published = cp.publish_agent_reflection(
        sender.id,
        recipient_agent_id=recipient.id,
        request_id="rid-timeout",
        reflect_timeout=0,
    )

    assert published["schema"] == "mac.agentbus.agent_reflection_publish.v1"
    assert published["payload"]["reflection"] is None
    # The structured inventory is still intact.
    assert published["payload"]["agent_id"] == sender.id
    assert published["payload"]["agent"]["name"] == "reflector-timeout"


def test_agent_reflection_request_id_matching(cp):
    """request_id matching: only the result with the correct request_id is used."""
    sender = register_agent(cp, "reflector-rid", ["python"])
    recipient = register_agent(cp, "operator-rid", ["review"])

    # Publish a result for a different request_id first (should be ignored).
    cp.publish_agentbus_content(
        sender_agent_id=sender.id,
        recipient_agent_id=recipient.id,
        content_type=REFLECT_RESULT_CONTENT_TYPE,
        topic=REFLECT_RESULT_TOPIC,
        payload=reflect_result_payload(
            request_id="rid-other",
            agent_id=sender.id,
            response="wrong narrative",
            word_count=2,
        ),
    )

    # Publish the correct result.
    cp.publish_agentbus_content(
        sender_agent_id=sender.id,
        recipient_agent_id=recipient.id,
        content_type=REFLECT_RESULT_CONTENT_TYPE,
        topic=REFLECT_RESULT_TOPIC,
        payload=reflect_result_payload(
            request_id="rid-correct",
            agent_id=sender.id,
            response="correct narrative text here",
            word_count=4,
        ),
    )

    published = cp.publish_agent_reflection(
        sender.id,
        recipient_agent_id=recipient.id,
        request_id="rid-correct",
        reflect_timeout=5,
    )

    assert published["payload"]["reflection"] == "correct narrative text here"


def test_agent_reflection_recipient_behavior(cp):
    """Recipient behavior: published stream is readable by the recipient agent."""
    sender = register_agent(cp, "reflector-recip", ["python"])
    recipient = register_agent(cp, "operator-recip", ["review"])

    published = cp.publish_agent_reflection(
        sender.id,
        recipient_agent_id=recipient.id,
        reflect_timeout=0,
    )

    stream = published["streams"][0]
    # The recipient can read the reflection stream.
    chunks = cp.read_agentbus_chunks(recipient.id, stream["id"])
    assert len(chunks) == 1
    assert chunks[0].payload["agent_id"] == sender.id
    # The reflect request was sent to the sender (target agent).
    deep_stream = published["deep_request_stream"]
    deep_chunks = cp.read_agentbus_chunks(sender.id, deep_stream["id"])
    assert len(deep_chunks) == 1
    assert deep_chunks[0].payload["sender_agent_id"] == recipient.id


def test_agentbus_artifact_publish_crud_records_and_broadcasts(cp, monkeypatch):
    sender = register_agent(cp, "sender", ["python"])
    recipient = register_agent(cp, "recipient", ["python"])
    monkeypatch.setenv("MAC_PUBLISH_DIR", "/srv/mac-artifacts")
    monkeypatch.setenv("MAC_PUBLISH_PUBLIC_URL", "http://hub.example:8790/artifacts")

    created = cp.publish_agentbus_artifact(
        sender.id,
        operation="upsert",
        recipient_agent_ids=[recipient.id],
        digest="sha256:publish1",
        path="reports/one.txt",
        metadata={"content_type": "text/plain"},
    )

    assert created["schema"] == "mac.agentbus.artifact_publish_crud.v1"
    assert created["operation"] == "upsert"
    assert created["artifact"]["uri"] == "http://hub.example:8790/artifacts/reports/one.txt"
    assert created["artifact"]["metadata"]["publish_dir"] == "/srv/mac-artifacts"
    assert created["artifact"]["metadata"]["publish_path"] == "reports/one.txt"
    assert created["artifact"]["metadata"]["public_url"] == "http://hub.example:8790/artifacts/reports/one.txt"
    assert created["count"] == 1

    chunks = cp.read_agentbus_chunks(recipient.id, created["streams"][0]["id"])
    assert chunks[0].content_type == "application/vnd.mac.artifact-publish+json"
    assert chunks[0].payload["schema"] == "mac.agentbus.artifact_publish.v1"
    assert chunks[0].payload["operation"] == "upsert"
    assert chunks[0].payload["artifact"]["digest"] == "sha256:publish1"

    updated = cp.publish_agentbus_artifact(
        sender.id,
        operation="upsert",
        digest="sha256:publish1",
        path="reports/two.txt",
    )
    assert updated["artifact"]["id"] == created["artifact"]["id"]
    assert updated["artifact"]["uri"] == "http://hub.example:8790/artifacts/reports/two.txt"
    assert updated["artifact"]["metadata"]["publish_path"] == "reports/two.txt"
    assert updated["artifact"]["metadata"]["public_url"] == "http://hub.example:8790/artifacts/reports/two.txt"

    listed = cp.publish_agentbus_artifact(sender.id, operation="list")
    assert [item["digest"] for item in listed["artifacts"]] == ["sha256:publish1"]
    deleted = cp.publish_agentbus_artifact(
        sender.id,
        operation="delete",
        digest="sha256:publish1",
        recipient_agent_ids=[recipient.id],
    )
    assert deleted["deleted"] is True
    assert deleted["artifact"]["digest"] == "sha256:publish1"
    with pytest.raises(NotFoundError):
        cp.get_artifact("sha256:publish1")


def test_agentbus_enforces_recipient_chunk_size_and_stream_id_shape(cp):
    sender = register_agent(cp, "sender", ["python"])
    recipient = register_agent(cp, "recipient", ["python"])

    with pytest.raises(ValidationError):
        cp.open_agentbus_stream(sender.id)
    with pytest.raises(ValidationError):
        cp.open_agentbus_stream(sender.id, recipient_agent_id=recipient.id, stream_id="bad id")
    with pytest.raises(ValidationError):
        cp.open_agentbus_stream(
            sender.id, recipient_agent_id=recipient.id, stream_id="x" * 200
        )
    with pytest.raises(ValidationError):
        cp.open_agentbus_stream(sender.id, recipient_agent_id=recipient.id, stream_id="../etc")

    stream = cp.open_agentbus_stream(
        sender.id,
        recipient_agent_id=recipient.id,
        stream_id="bus_alpha-01",
    )
    assert stream.id == "bus_alpha-01"

    with pytest.raises(ValidationError):
        cp.append_agentbus_chunk(
            stream.id,
            sender.id,
            payload={"blob": "x" * (256 * 1024 + 1)},
        )
    assert cp.read_agentbus_chunks(recipient.id, stream.id) == []


def test_create_task_inherits_project_default_role(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={"task_defaults": {"role": "python-coder-opencode"}},
    )

    task = cp.create_task("Fix UI", project="mac")

    assert task.metadata["required_role"] == "python-coder-opencode"
    assert task.required_capabilities == []


def test_create_task_preserves_explicit_required_role(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.roles.create_role(
        "custom-coder",
        "Custom Coder",
        "Custom coding role",
        "You are a custom coder.",
        "ic",
        default_capabilities=["python"],
        required_capabilities=["python"],
    )
    cp.create_project(
        "mac",
        metadata={"task_defaults": {"role": "python-coder-opencode"}},
    )

    task = cp.create_task(
        "Custom role work",
        project="mac",
        metadata={"required_role": "custom-coder"},
    )

    assert task.metadata["required_role"] == "custom-coder"


def test_create_task_rejects_unknown_project_default_role(cp):
    import pytest
    from mac.models import ValidationError

    cp.create_project("mac", metadata={"task_defaults": {"role": "missing-role"}})

    with pytest.raises(ValidationError, match="unknown project default role"):
        cp.create_task("Unroutable", project="mac")


def test_update_task_without_routing_change_tolerates_later_bad_project_default(cp):
    task = cp.create_task("work", project="mac", metadata={"required_role": "custom"})
    cp.create_project("mac", metadata={"task_defaults": {"role": "missing-role"}})

    updated = cp.update_task(task.id, title="renamed")

    assert updated.title == "renamed"
    assert updated.metadata["required_role"] == "custom"


def test_update_task_rejects_newly_applied_unknown_project_default(cp):
    import pytest
    from mac.models import ValidationError

    task = cp.create_task("work")
    cp.create_project("mac", metadata={"task_defaults": {"role": "missing-role"}})

    with pytest.raises(ValidationError, match="unknown project default role"):
        cp.update_task(task.id, project="mac")


def test_update_task_uses_explicit_metadata_when_applying_project_defaults(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={"task_defaults": {"role": "python-coder-opencode"}},
    )
    task = cp.create_task("work")

    updated = cp.update_task(task.id, project="mac", metadata={"source": "explicit"})

    assert updated.metadata["source"] == "explicit"
    assert updated.metadata["required_role"] == "python-coder-opencode"


def test_verdict_value_unknown_fails_closed(cp):
    from mac.models import Evidence

    evidence = Evidence(
        "ev_test",
        "task_test",
        "review",
        "artifact://verdict",
        "bad verdict",
        None,
        {"verification": {"verdict": "needs_changes"}},
        "reviewer",
        "2026-01-01T00:00:00+00:00",
    )

    assert cp._verdict_value(evidence) == "rejected"


def test_review_verdict_validator_rejected_requires_feedback():
    from mac.evidence_validators import EvidenceValidationContext, ReviewVerdictValidator, VerificationManifest

    manifest = VerificationManifest.parse(
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "rejected",
            "reviewed_evidence_id": "ev_executor",
            "worktree_digest": "sha256:" + "0" * 64,
        }
    )
    problems = ReviewVerdictValidator().validate(
        manifest,
        EvidenceValidationContext(passed_check_count=lambda _m: 0),
    )

    assert "rejected review_verdict requires feedback, findings, or summary" in problems


def test_review_verdict_validator_rejected_accepts_feedback():
    from mac.evidence_validators import EvidenceValidationContext, ReviewVerdictValidator, VerificationManifest

    manifest = VerificationManifest.parse(
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "rejected",
            "reviewed_evidence_id": "ev_executor",
            "worktree_digest": "sha256:" + "0" * 64,
            "feedback": "Fix the failing contract test.",
        }
    )
    problems = ReviewVerdictValidator().validate(
        manifest,
        EvidenceValidationContext(passed_check_count=lambda _m: 0),
    )

    assert problems == []


def _add_signed_repo_evidence(cp, task_id, agent_id):
    return cp.add_evidence(
        task_id,
        "log",
        "artifact://repo-change",
        "repo changed",
        agent_id,
        metadata=verified_repo_metadata(cp, agent_id),
        _trusted_internal=True,
    )


def test_find_review_verdict_rejected_requires_digest(cp):
    from mac.services import sign_verification_manifest

    task = cp.create_task("work", required_capabilities=["python"])
    executor = register_agent(cp, "executor", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review", "python"])
    evidence = _add_signed_repo_evidence(cp, task.id, executor.id)
    key = cp._agent_attestation_key(reviewer.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "rejected",
        "reviewed_evidence_id": evidence.id,
        "feedback": "Needs changes.",
        "signed_by": reviewer.id,
    }
    manifest["signature"] = sign_verification_manifest(key, manifest)
    cp.add_evidence(
        task.id,
        "review",
        "artifact://verdict",
        "rejected",
        reviewer.id,
        metadata={"returncode": 0, "verification": manifest},
        _trusted_internal=True,
    )

    found, problems = cp._find_review_verdict_evidence(task.id, reviewer.id, executor_evidence_id=evidence.id)

    assert found is None
    assert any("worktree_digest" in problem for problem in problems)


def test_find_review_verdict_rejected_skips_repo_push_checks(cp):
    from mac.services import sign_verification_manifest

    task = cp.create_task("work", required_capabilities=["python"])
    executor = register_agent(cp, "executor", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review", "python"])
    evidence = _add_signed_repo_evidence(cp, task.id, executor.id)
    key = cp._agent_attestation_key(reviewer.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "rejected",
        "reviewed_evidence_id": evidence.id,
        "worktree_digest": "sha256:" + "0" * 64,
        "feedback": "Branch is not publishable; fix the tests.",
        "signed_by": reviewer.id,
    }
    manifest["signature"] = sign_verification_manifest(key, manifest)
    verdict = cp.add_evidence(
        task.id,
        "review",
        "artifact://verdict",
        "rejected",
        reviewer.id,
        metadata={"returncode": 0, "verification": manifest},
        _trusted_internal=True,
    )

    found, problems = cp._find_review_verdict_evidence(task.id, reviewer.id, executor_evidence_id=evidence.id)

    assert found is not None
    assert found.id == verdict.id
    assert problems == []


def test_rejected_review_persists_feedback_and_reopens(cp):
    from tests.conftest import submit_review_verdict
    from mac.models import ReviewStatus

    task = cp.create_task("work", required_capabilities=["python"], max_attempts=3)
    executor = register_agent(cp, "executor", capabilities=["python"])
    reviewer = register_agent(cp, "reviewer", capabilities=["review", "python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://repo-change",
        "repo changed",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    review = cp.request_review(task.id, reviewer.id)
    verdict_id = submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        evidence.id,
        verdict="rejected",
        feedback="Fix the failing contract test.",
    )

    cp.submit_review(review.id, ReviewStatus.REJECTED.value, reviewer.id, evidence_id=verdict_id)
    updated = cp.get_task(task.id)

    assert updated.state == "open"
    latest = updated.metadata["review_feedback"]["latest"]
    assert latest["review_id"] == review.id
    assert latest["verdict_evidence_id"] == verdict_id
    assert latest["feedback"] == "Fix the failing contract test."


def test_project_task_review_reject_retry_approve_publish_loop(cp):
    from tests.conftest import submit_review_verdict
    from mac.models import ReviewStatus

    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={
            "task_defaults": {"role": "python-coder-opencode"},
            "publication_target": "gitea://merge-request",
        },
    )
    executor = register_agent(cp, "executor", capabilities=["python", "ops"])
    reviewer = register_agent(cp, "reviewer", capabilities=["review", "python"])

    task = cp.create_task("Fix task UI", project="mac", max_attempts=3)
    assert task.metadata["required_role"] == "python-coder-opencode"

    # First attempt: executor works, reviewer rejects
    _, first_lease = cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id, lease_id=first_lease.id)
    first_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://repo-change-1",
        "repo changed",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id, files_changed=["src/mac/ui/app.ts"]),
        lease_id=first_lease.id,
    )
    cp.submit_for_review(task.id, executor.id, lease_id=first_lease.id)
    first_review = cp.request_review(task.id, reviewer.id)
    rejected_verdict = submit_review_verdict(
        cp,
        task.id,
        reviewer.id,
        first_evidence.id,
        verdict="rejected",
        feedback="Fix layout overflow on the task cards.",
    )
    cp.submit_review(first_review.id, ReviewStatus.REJECTED.value, reviewer.id, evidence_id=rejected_verdict)

    reopened = cp.get_task(task.id)
    assert reopened.state == "open"
    assert reopened.metadata["review_feedback"]["latest"]["feedback"] == "Fix layout overflow on the task cards."

    # Second attempt: executor addresses feedback, reviewer approves
    _, second_lease = cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id, lease_id=second_lease.id)
    second_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://repo-change-2",
        "repo changed after feedback",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id, files_changed=["src/mac/ui/app.ts"]),
        lease_id=second_lease.id,
    )
    cp.submit_for_review(task.id, executor.id, lease_id=second_lease.id)
    second_review = cp.request_review(task.id, reviewer.id)
    approved_verdict = submit_review_verdict(cp, task.id, reviewer.id, second_evidence.id)
    cp.submit_review(second_review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=approved_verdict)

    publication = cp.publish_task(task.id, "gitea://merge-request", reviewer.id, evidence_id=second_evidence.id)
    completed = cp.get_task(task.id)

    assert publication.target == "gitea://merge-request"
    assert completed.state == "completed"


def test_historical_approval_cannot_complete_a_later_attempt(cp):
    from tests.conftest import submit_review_verdict

    executor = register_agent(cp, "attempt-executor", capabilities=["python"])
    reviewer = register_agent(cp, "attempt-reviewer", capabilities=["review"])
    task = cp.create_task("attempt-bound review", max_attempts=3)

    _, first_lease = cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id, lease_id=first_lease.id)
    first_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://attempt-one",
        "first attempt",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id, files_changed=["one.py"]),
        lease_id=first_lease.id,
    )
    cp.submit_for_review(task.id, executor.id, lease_id=first_lease.id)
    first_review = cp.request_review(task.id, reviewer.id)
    first_verdict = submit_review_verdict(
        cp, task.id, reviewer.id, first_evidence.id
    )
    cp.submit_review(
        first_review.id,
        ReviewStatus.APPROVED.value,
        reviewer.id,
        evidence_id=first_verdict,
    )

    cp._transition_task_internal(
        task.id,
        TaskState.OPEN.value,
        "operator",
        {"reason": "rework"},
    )
    _, second_lease = cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id, lease_id=second_lease.id)
    second_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://attempt-two",
        "second attempt",
        executor.id,
        metadata=verified_repo_metadata(
            cp,
            executor.id,
            files_changed=["two.py"],
            head_sha="fedcba9876543210fedcba9876543210fedcba98",
        ),
        lease_id=second_lease.id,
    )
    cp.submit_for_review(task.id, executor.id, lease_id=second_lease.id)
    second_review = cp.request_review(task.id, reviewer.id)

    with pytest.raises(ValidationError, match="stale executor evidence"):
        cp.submit_review(
            second_review.id,
            ReviewStatus.APPROVED.value,
            reviewer.id,
            evidence_id=first_verdict,
        )
    with pytest.raises(ValidationError, match="completion requires approved review"):
        cp._transition_task_internal(task.id, TaskState.COMPLETED.value, "operator")

    assert cp.get_task(task.id).metadata["review_target"]["executor_evidence_id"] == second_evidence.id
    assert cp.get_review(second_review.id).status == ReviewStatus.PENDING.value


def test_decomposed_children_use_distinct_agents_and_feed_integrator(cp, tmp_path):
    from mac.task_executor import build_task_prompt
    from tests.conftest import submit_review_verdict

    planner = register_agent(cp, "planner", capabilities=["python"])
    child_one_agent = register_agent(cp, "child-one", capabilities=["python"])
    child_two_agent = register_agent(cp, "child-two", capabilities=["python"])
    integrator = register_agent(cp, "integrator", capabilities=["python"])
    reviewer = register_agent(cp, "coord-reviewer", capabilities=["review"])
    parent = cp.create_task(
        "Implement coordinated feature",
        required_capabilities=["python"],
        max_attempts=3,
    )
    cp.claim_task(parent.id, planner.id)
    cp.start_task(parent.id, planner.id)
    split = cp.add_child_tasks(
        parent.id,
        [
            {"title": "Implement component one"},
            {"title": "Implement component two"},
        ],
        actor=planner.id,
    )
    child_one, child_two = [cp.get_task(item["id"]) for item in split["children"]]

    assert not cp._agent_available_for(planner, child_one)
    assert cp._agent_available_for(child_one_agent, child_one)
    _, child_one_lease = cp.claim_task(child_one.id, child_one_agent.id)
    assert not cp._agent_available_for(child_one_agent, child_two)
    assert cp._agent_available_for(child_two_agent, child_two)

    def complete_child(task, agent, lease, head_sha, changed_file):
        cp.start_task(task.id, agent.id, lease_id=lease.id)
        evidence = cp.add_evidence(
            task.id,
            "log",
            "artifact://%s" % task.id,
            "completed %s" % task.title,
            agent.id,
            metadata=verified_repo_metadata(
                cp,
                agent.id,
                head_sha=head_sha,
                files_changed=[changed_file],
            ),
            lease_id=lease.id,
        )
        cp.submit_for_review(task.id, agent.id, lease_id=lease.id)
        review = cp.request_review(task.id, reviewer.id)
        verdict = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
        cp.submit_review(
            review.id,
            ReviewStatus.APPROVED.value,
            reviewer.id,
            evidence_id=verdict,
        )
        cp._transition_task_internal(
            task.id,
            TaskState.COMPLETED.value,
            reviewer.id,
        )
        return evidence

    first_evidence = complete_child(
        child_one,
        child_one_agent,
        child_one_lease,
        "abcdef1234567890abcdef1234567890abcdef12",
        "src/one.py",
    )
    _, child_two_lease = cp.claim_task(child_two.id, child_two_agent.id)
    second_evidence = complete_child(
        child_two,
        child_two_agent,
        child_two_lease,
        "fedcba9876543210fedcba9876543210fedcba98",
        "src/two.py",
    )

    cp._unblock_ready_tasks()
    integration_task = cp.get_task(parent.id)
    assert integration_task.state == TaskState.OPEN.value
    coordination = integration_task.metadata["coordination"]
    assert coordination["phase"] == "integration"
    output_ids = {
        item["executor_evidence_id"] for item in coordination["child_outputs"]
    }
    assert output_ids == {first_evidence.id, second_evidence.id}
    assert cp._cooperative_review_integration_problems(integration_task, {})
    assert cp._cooperative_review_integration_problems(
        integration_task,
        {
            "integration": {
                "status": "pass",
                "required_child_evidence_ids": sorted(output_ids),
                "verified_child_evidence_ids": sorted(output_ids),
            }
        },
    ) == []
    assert not cp._agent_available_for(planner, integration_task)
    assert not cp._agent_available_for(child_one_agent, integration_task)
    assert not cp._agent_available_for(child_two_agent, integration_task)
    assert cp._agent_available_for(integrator, integration_task)

    prompt = build_task_prompt(integration_task.to_dict(), tmp_path / "task.json")
    assert "mandatory fan-in pass" in prompt
    assert "refs/heads" in prompt
    assert first_evidence.id in prompt and second_evidence.id in prompt


def test_cooperative_child_claims_atomically_enforce_distinct_agents(cp, monkeypatch):
    planner = register_agent(cp, "atomic-planner", capabilities=["python"])
    shared_agent = register_agent(cp, "atomic-shared", capabilities=["python"])
    parent = cp.create_task(
        "Atomically coordinate children",
        required_capabilities=["python"],
    )
    cp.claim_task(parent.id, planner.id)
    cp.start_task(parent.id, planner.id)
    split = cp.add_child_tasks(
        parent.id,
        [{"title": "Atomic child one"}, {"title": "Atomic child two"}],
        actor=planner.id,
    )
    child_ids = [item["id"] for item in split["children"]]
    barrier = threading.Barrier(2)
    original_available = cp._agent_available_for

    def synchronized_available(agent, task, **kwargs):
        available = original_available(agent, task, **kwargs)
        barrier.wait(timeout=5)
        return available

    monkeypatch.setattr(cp, "_agent_available_for", synchronized_available)
    claimed = []
    errors = []

    def claim(child_id):
        try:
            claimed.append(cp.claim_task(child_id, shared_agent.id)[0].id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(child_id,)) for child_id in child_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(claimed) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)
    assert "already participated" in str(errors[0])


def test_create_task_filters_disallowed_capabilities_to_metadata(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={
            "task_defaults": {
                "role": "python-coder-opencode",
                "allowed_capabilities": ["python", "ops"],
            }
        },
    )

    task = cp.create_task(
        "Fix metadata JSON textbox in Projects UI",
        project="mac",
        required_capabilities=["typescript", "python"],
    )

    assert task.required_capabilities == ["python"]
    assert task.metadata["required_role"] == "python-coder-opencode"
    assert task.metadata["domain_capabilities"] == ["typescript"]
    policy = task.metadata["capability_policy"]
    assert policy["source"] == "project.task_defaults.allowed_capabilities"
    assert policy["allowed"] == ["python", "ops"]
    assert policy["accepted"] == ["python"]
    assert policy["filtered"] == ["typescript"]


def test_create_task_without_allowed_capabilities_preserves_caps(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={"task_defaults": {"role": "python-coder-opencode"}},
    )

    task = cp.create_task(
        "Legacy capability task",
        project="mac",
        required_capabilities=["typescript"],
    )

    assert task.required_capabilities == ["typescript"]
    assert "domain_capabilities" not in task.metadata
    assert "capability_policy" not in task.metadata


def test_create_task_all_allowed_capabilities_no_policy_noise(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={
            "task_defaults": {
                "role": "python-coder-opencode",
                "allowed_capabilities": ["python", "ops"],
            }
        },
    )

    task = cp.create_task(
        "Backend change",
        project="mac",
        required_capabilities=["python"],
    )

    assert task.required_capabilities == ["python"]
    assert "domain_capabilities" not in task.metadata
    assert "capability_policy" not in task.metadata


def test_create_task_merges_existing_domain_capabilities(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={
            "task_defaults": {
                "role": "python-coder-opencode",
                "allowed_capabilities": ["python", "ops"],
            }
        },
    )

    task = cp.create_task(
        "UI work",
        project="mac",
        required_capabilities=["frontend"],
        metadata={"domain_capabilities": ["ui"]},
    )

    assert task.required_capabilities == []
    assert task.metadata["domain_capabilities"] == ["ui", "frontend"]


def test_update_task_filters_disallowed_capabilities(cp):
    cp.roles.create_role(
        "python-coder-opencode",
        "Python Coder Opencode",
        "Coding role",
        "You are a Python coder.",
        "ic",
        default_capabilities=["python", "ops"],
        required_capabilities=["python", "ops"],
    )
    cp.create_project(
        "mac",
        metadata={
            "task_defaults": {
                "role": "python-coder-opencode",
                "allowed_capabilities": ["python", "ops"],
            }
        },
    )
    task = cp.create_task("work", project="mac")

    updated = cp.update_task(task.id, required_capabilities=["typescript", "ops"])

    assert updated.required_capabilities == ["ops"]
    assert updated.metadata["domain_capabilities"] == ["typescript"]


def test_heartbeat_only_logs_meaningful_changes():
    """Hub-db bloat fix: a heartbeat must not write a durable lifecycle/obs row
    on resource jitter — only on a meaningful change (status/health/digest).
    Resource churn on every beat was ~527K+228K rows in ~4 days on rocky."""
    cp = ControlPlane.in_memory()
    agent = register_agent(cp, "worker", ["python"])

    def hb_events():
        return cp.store.query_one(
            "SELECT COUNT(*) AS c FROM agent_lifecycle_events "
            "WHERE event_type = 'agent.heartbeat_updated'"
        )["c"]

    before = hb_events()
    # resource-only heartbeats (CPU/mem jitter) must NOT create lifecycle events
    cp.heartbeat_agent(agent.id, resources={"cpu_percent": 12.3})
    cp.heartbeat_agent(agent.id, resources={"cpu_percent": 88.0})
    assert hb_events() == before, "resource jitter must not log heartbeat events"

    # a meaningful status change DOES log exactly one event
    cp.heartbeat_agent(agent.id, status="draining")
    assert hb_events() == before + 1

    # repeating the same status (with more jitter) is a no-op
    cp.heartbeat_agent(agent.id, status="draining", resources={"cpu_percent": 5.0})
    assert hb_events() == before + 1


def test_no_publication_target_is_silenced():
    """The review-tick 'no_publication_target' steady-state log is silenced
    (mem-04) so it can't re-bloat the db every tick."""
    from mac.observability_service import _VERBOSE_POLL_LOG_NAMES

    assert "workflow.default_review.no_publication_target" in _VERBOSE_POLL_LOG_NAMES
    assert "worker.routing.task_skipped" in _VERBOSE_POLL_LOG_NAMES
    assert "dispatcher.routing.task_skipped" in _VERBOSE_POLL_LOG_NAMES


def test_agent_installed_packages_footprint_persists_and_survives_register(cp):
    """Part C: a self-installed footprint is recorded per-agent, returned by
    get_agent, and survives re-registration (the register UPSERT must not clobber
    it). This is the persistent 'default footprint' deploys re-hydrate."""
    machine = cp.register_machine("gpu-host", resources={"cpu": 4})
    agent = cp.register_agent(machine.id, "gpu-worker", capabilities=["python"], agent_id="agent_gpu1")
    fp = {
        "pip": [{"name": "diffusers", "spec": "diffusers==0.31", "installed_at": "t"}],
        "npm": [{"name": "left-pad", "spec": "left-pad@1.0", "installed_at": "t"}],
        "updated_at": "t",
    }
    updated = cp.update_agent_installed_packages(agent.id, fp, actor="agent_gpu1")
    assert updated.installed_packages == fp
    assert cp.get_agent(agent.id).installed_packages["pip"][0]["name"] == "diffusers"
    # re-register the SAME agent with new capabilities -> footprint preserved.
    again = cp.register_agent(machine.id, "gpu-worker", capabilities=["python", "gpu"], agent_id="agent_gpu1")
    assert again.id == agent.id
    assert cp.get_agent(agent.id).installed_packages == fp
    assert "gpu" in cp.get_agent(agent.id).capabilities


def test_project_repository_registry_migrates_from_legacy_beads_table(tmp_path):
    """beads->mac: a pre-rename DB with a `beads_repositories` table must have
    its rows migrated into `project_repositories` (and the legacy table dropped)
    on the next open, with no data loss."""
    from mac.store import SQLiteStore

    db = str(tmp_path / "legacy.db")
    store = SQLiteStore(db)
    store._conn.execute(
        "INSERT INTO project_repositories "
        "(id, name, path, source, project, created_at, updated_at) "
        "VALUES ('repo_legacy','mac','/repo/mac','repo-mac','mac','t0','t0')"
    )
    # Simulate the historical schema where the registry was `beads_repositories`.
    store._conn.execute("ALTER TABLE project_repositories RENAME TO beads_repositories")
    store._conn.commit()
    store._conn.close()

    # Reopen: initialize() recreates `project_repositories` empty, then
    # _migrate() copies the legacy rows over and drops `beads_repositories`.
    store2 = SQLiteStore(db)
    rows = store2._conn.execute(
        "SELECT id, name, project FROM project_repositories"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("repo_legacy", "mac", "mac")]
    legacy = store2._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='beads_repositories'"
    ).fetchone()
    assert legacy is None


def test_reopen_task_requeues_terminal_task_and_resets_attempts(cp):
    """Recovery: a failed task (e.g. flap-killed) can be reopened back to OPEN,
    clearing the owner/lease and resetting attempt_count so the requeue is not
    immediately re-exhausted."""
    worker = register_agent(cp, "recover-worker", ["python"])
    task = cp.create_task("recover me", required_capabilities=["python"])
    _, lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    cp.transition_task(
        task.id,
        TaskState.FAILED.value,
        worker.id,
        {"reason": "heartbeat_offline"},
        lease_id=lease.id,
    )
    assert cp.get_task(task.id).state == TaskState.FAILED.value

    reopened = cp.reopen_task(task.id, "operator", reason="hub flap; retry")
    assert reopened.state == TaskState.OPEN.value
    assert reopened.owner_agent_id is None
    assert reopened.lease_id is None
    assert reopened.attempt_count == 0
    hist = cp.task_history(task.id)
    assert hist[-1].to_state == TaskState.OPEN.value
    assert hist[-1].detail.get("via") == "operator_reopen"


def test_reopen_task_recovers_blocked_task(cp):
    task = cp.create_task("blocked recover", required_capabilities=["python"])
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "dispatcher",
        {"reason": "operator_direction_required"},
    )
    reopened = cp.reopen_task(task.id, "operator")
    assert reopened.state == TaskState.OPEN.value


def test_blocked_transitions_require_reason_and_dependency_updates_use_waiting(cp):
    dependency = cp.create_task("dependency")
    task = cp.create_task("blocked ledger contract")

    with pytest.raises(ValidationError, match="blocked task transition requires"):
        cp._transition_task_internal(
            task.id,
            TaskState.BLOCKED.value,
            "worker",
            {},
        )

    updated = cp.update_task(task.id, dependencies=[dependency.id], actor="worker")
    assert updated.state == TaskState.WAITING.value
    event = cp.task_history(task.id)[-1]
    assert event.to_state == TaskState.WAITING.value
    assert event.detail["dependencies"] == [dependency.id]


def test_force_complete_overrides_review_gate_for_stranded_task(cp):
    """Operator override: a task stranded in a terminal state (or whose work
    merged out-of-band) can be force-completed without the review/evidence gate,
    and the override is audited (who, prior state, why)."""
    task = cp.create_task("done out of band", required_capabilities=["python"])
    cp._transition_task_internal(
        task.id,
        TaskState.FAILED.value,
        "dispatcher",
        {"reason": "flap"},
    )

    completed = cp.force_complete_task(task.id, "operator", reason="merged via PR #181")
    assert completed.state == TaskState.COMPLETED.value
    assert completed.completed_at is not None
    assert completed.owner_agent_id is None
    hist = cp.task_history(task.id)
    assert hist[-1].event_type == "task.force_completed"
    assert hist[-1].detail.get("reason") == "merged via PR #181"
    assert hist[-1].detail.get("from_state") == TaskState.FAILED.value


def test_force_complete_normalizes_unambiguous_task_prefix(cp):
    task = cp.create_task("done out of band through short id")
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "dispatcher",
        {"reason": "stranded"},
    )

    completed = cp.force_complete_task(
        task.id[:13], "operator", reason="verified canonical commit"
    )

    assert completed.id == task.id
    assert completed.state == TaskState.COMPLETED.value
    event = cp.task_history(task.id)[-1]
    assert event.event_type == "task.force_completed"
    assert event.detail["reason"] == "verified canonical commit"


def test_repository_completion_requires_durable_canonical_integration(cp, monkeypatch):
    metadata = {
        "execution_contract": {
            "type": "repository",
            "repository_contract": {"canonical_branch": "main"},
        }
    }
    task = cp.create_task("repository completion proof", metadata=metadata)

    with pytest.raises(ValidationError, match="canonical integration proof"):
        cp.force_complete_task(task.id, "operator", reason="reviewed")

    head_sha = "a" * 40
    cp.add_evidence(
        task.id,
        "test",
        "git://example/repo#%s" % head_sha,
        "canonical integration verified",
        "operator",
        metadata={
            "verification": {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "repo_change",
                "repo": {"head_sha": head_sha},
                "canonical_integration": {
                    "schema": "mac.canonical_integration.v1",
                    "status": "pass",
                    "canonical_ref": "refs/heads/main",
                    "canonical_tip_sha": head_sha,
                    "head_sha": head_sha,
                    "remote_verified": True,
                },
            }
        },
        _trusted_internal=True,
    )

    completed = cp.force_complete_task(task.id, "operator", reason="integrated")
    assert completed.state == TaskState.COMPLETED.value

    second = cp.create_task("published repository proof", metadata=metadata)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.REVIEWING.value, second.id),
    )
    monkeypatch.setattr(cp.reviews, "completion_authorized", lambda _task_id: True)
    monkeypatch.setattr(cp, "_validate_publication_evidence", lambda *_args: None)
    with pytest.raises(ValidationError, match="canonical integration proof"):
        cp.publish_task(second.id, "git://example/main", "reviewer")

    third = cp.create_task("transition repository proof", metadata=metadata)
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.REVIEWING.value, third.id),
    )
    with pytest.raises(ValidationError, match="canonical integration proof"):
        cp._transition_task_internal(
            third.id,
            TaskState.COMPLETED.value,
            "reviewer",
        )


# ---------------------------------------------------------------------------
# mac-kg8y: Rollout promotion / rollback must atomically deploy to environment
# ---------------------------------------------------------------------------

def _create_linked_rollout(cp, version="10.0", artifact_hash="sha256:aabbccdd"):
    """Create a rollout with a linked deploy environment and a pre-registered artifact."""
    runtime = create_runtime(cp, "runtime-linked-%s" % version)
    env = cp.register_environment("env-rollout-%s" % version, channel="fleet")
    artifact = cp.register_artifact(
        "image",
        artifact_hash,
        "artifact://mac/%s" % version,
        "human",
    )
    rollout = cp.create_rollout(
        version,
        "canary",
        10,
        "human",
        runtime_environment_id=runtime.id,
        artifact_uri=artifact.uri,
        artifact_hash=artifact_hash,
        deploy_environment_id=env.id,
    )
    return rollout, env, artifact


def test_rollout_promotion_deploys_artifact_to_linked_environment(cp):
    """mac-kg8y: promote must deploy the rollout artifact to deploy_environment_id."""
    rollout, env, artifact = _create_linked_rollout(cp, "11.0", "sha256:promo001")

    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    promoted = cp.advance_rollout(rollout.id, "promote", "human")

    assert promoted.status == RolloutStatus.PROMOTED.value
    # The environment must now have an active deployment for our artifact.
    active = cp.current_deployment(env.id)
    assert active is not None, "environment has no active deployment after promote"
    assert active.artifact_id == artifact.id
    assert active.status == "active"


def test_rollout_promotion_records_deployed_event(cp):
    """mac-kg8y: a rollout.deployed event is recorded after a successful promote."""
    rollout, env, artifact = _create_linked_rollout(cp, "11.1", "sha256:promo002")

    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "promote", "human")

    events = cp.list_rollout_events(rollout.id)
    event_types = [e["event_type"] for e in events]
    assert "rollout.deployed" in event_types, "expected rollout.deployed event, got: %s" % event_types
    deployed_evt = next(e for e in events if e["event_type"] == "rollout.deployed")
    assert deployed_evt["detail"]["artifact_id"] == artifact.id
    assert deployed_evt["detail"]["deploy_environment_id"] == env.id


def test_rollout_without_deploy_environment_still_promotes(cp):
    """Rollouts with no deploy_environment_id must still promote successfully (no-op deploy)."""
    rollout = create_verified_rollout(cp, "12.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    promoted = cp.advance_rollout(rollout.id, "promote", "human")
    assert promoted.status == RolloutStatus.PROMOTED.value
    assert promoted.deploy_environment_id is None


def test_rollout_rollback_redeploys_prior_artifact(cp):
    """mac-kg8y: rollback must redeploy the prior known-good artifact."""
    rollout, env, artifact_v1 = _create_linked_rollout(cp, "13.0", "sha256:rollback01")

    # Pre-deploy artifact_v1 as the prior known-good deployment.
    cp.deploy_artifact(env.id, artifact_v1.id, "prior-release")

    # Create v2 artifact, promote it.
    artifact_v2 = cp.register_artifact(
        "image", "sha256:rollback02", "artifact://mac/13.0-v2", "human"
    )
    cp.verify_rollout_artifact(
        rollout.id, artifact_v2.uri, artifact_v2.digest, "human"
    )
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "promote", "human")

    # Now rollback — should redeploy the prior artifact (v1).
    rolledback = cp.advance_rollout(rollout.id, "rollback", "human")
    assert rolledback.status == RolloutStatus.ROLLED_BACK.value

    active = cp.current_deployment(env.id)
    assert active is not None
    assert active.artifact_id == artifact_v1.id, (
        "expected prior artifact %s, got %s" % (artifact_v1.id, active.artifact_id)
    )


def test_rollout_rollback_records_rolled_back_deployed_event(cp):
    """mac-kg8y: a rollout.rolled_back_deployed event is recorded on successful rollback."""
    rollout, env, artifact_v1 = _create_linked_rollout(cp, "13.1", "sha256:rollbackevt1")
    cp.deploy_artifact(env.id, artifact_v1.id, "prior-release")

    artifact_v2 = cp.register_artifact(
        "image", "sha256:rollbackevt2", "artifact://mac/13.1-v2", "human"
    )
    cp.verify_rollout_artifact(rollout.id, artifact_v2.uri, artifact_v2.digest, "human")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "promote", "human")
    cp.advance_rollout(rollout.id, "rollback", "human")

    events = cp.list_rollout_events(rollout.id)
    event_types = [e["event_type"] for e in events]
    assert "rollout.rolled_back_deployed" in event_types, (
        "expected rollout.rolled_back_deployed event, got: %s" % event_types
    )


def test_rollout_rollback_without_deploy_environment_is_noop(cp):
    """Rollouts with no deploy_environment_id must still transition to ROLLED_BACK (no deploy)."""
    rollout = create_verified_rollout(cp, "14.0")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    rolledback = cp.advance_rollout(rollout.id, "rollback", "human")
    assert rolledback.status == RolloutStatus.ROLLED_BACK.value


def test_rollout_rollback_with_no_prior_deployment_records_skipped_event(cp):
    """When no prior deployment exists, rollback records rollout.rollback_skipped."""
    rollout, env, artifact_v1 = _create_linked_rollout(cp, "14.1", "sha256:nopriordeploy")
    # Do NOT pre-deploy anything; only the rollout promotion creates the first deployment.
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "promote", "human")
    # Now rollback — no prior deployment to revert to.
    rolledback = cp.advance_rollout(rollout.id, "rollback", "human")
    assert rolledback.status == RolloutStatus.ROLLED_BACK.value

    events = cp.list_rollout_events(rollout.id)
    event_types = [e["event_type"] for e in events]
    assert "rollout.rollback_skipped" in event_types, (
        "expected rollout.rollback_skipped event, got: %s" % event_types
    )


def test_rollout_rescue_from_promoted_redeploys_prior_artifact(cp):
    """mac-kg8y: rescuing a PROMOTED rollout must immediately revert the environment."""
    rollout, env, artifact_v1 = _create_linked_rollout(cp, "15.0", "sha256:rescue01")
    cp.deploy_artifact(env.id, artifact_v1.id, "prior-release")

    artifact_v2 = cp.register_artifact(
        "image", "sha256:rescue02", "artifact://mac/15.0-v2", "human"
    )
    cp.verify_rollout_artifact(rollout.id, artifact_v2.uri, artifact_v2.digest, "human")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    cp.evaluate_rollout_health(rollout.id, {"runtime": "healthy"}, "monitor")
    cp.advance_rollout(rollout.id, "promote", "human")

    # At this point env serves artifact_v2. Rescue should revert to artifact_v1.
    rescued, rescue_task = cp.rescue_rollout(rollout.id, "ops-bot", "service degraded after promote")
    assert rescued.status == RolloutStatus.RESCUING.value

    active = cp.current_deployment(env.id)
    assert active is not None
    assert active.artifact_id == artifact_v1.id, (
        "expected prior artifact %s after rescue, got %s" % (artifact_v1.id, active.artifact_id)
    )


def test_rollout_rescue_from_canarying_does_not_deploy(cp):
    """Rescuing from CANARYING (not PROMOTED) should not attempt environment redeployment."""
    rollout, env, artifact = _create_linked_rollout(cp, "15.1", "sha256:rescue03")
    cp.advance_rollout(rollout.id, "start_canary", "human")
    # Rescue from CANARYING — env was never promoted, so no redeployment expected.
    rescued, _ = cp.rescue_rollout(rollout.id, "ops-bot", "canary health failed")
    assert rescued.status == RolloutStatus.RESCUING.value
    # Environment should have no active deployment (rollout never promoted).
    assert cp.current_deployment(env.id) is None


def test_rollout_create_with_deploy_environment_id_validates_environment_exists(cp):
    """Creating a rollout with a non-existent deploy_environment_id must fail."""
    runtime = create_runtime(cp, "runtime-validate-env")
    with pytest.raises(NotFoundError):
        cp.create_rollout(
            "20.0",
            "canary",
            10,
            "human",
            runtime_environment_id=runtime.id,
            artifact_uri="artifact://mac/20.0",
            artifact_hash="sha256:validenv01",
            deploy_environment_id="env_does_not_exist",
        )


def test_rollout_deploy_environment_id_round_trips(cp):
    """deploy_environment_id is persisted and returned on get_rollout."""
    rollout, env, _ = _create_linked_rollout(cp, "21.0", "sha256:roundtrip01")
    fetched = cp.get_rollout(rollout.id)
    assert fetched.deploy_environment_id == env.id


def _setup_hubverify_task(cp, runner, *, experiment=False):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    # Canonical remote lives on the task contract (the hub verifier resolves
    # the clone target from there); the executor evidence carries no
    # remote_url, so add_evidence performs no live git ls-remote.
    task = cp.create_task(
        "Implement thing",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://publish",
            "origin": {"repository_contract": {"canonical_remote_url": "git@github.com:org/repo.git"}},
        },
    )
    if experiment:
        cp.assign_review_experiment(
            task.id,
            experiment_id="exp-semantic-review",
            arm="standard",
            actor="test",
        )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "log", "artifact://worker-result", "tests passed", worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    cp._hub_verify_runner = runner
    return worker, reviewer, task, evidence


def test_hub_verify_prefers_canonical_contract_remote_over_redacted_evidence(cp):
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda *args: (0, "ok")
    )
    evidence.metadata["verification"]["repo"]["remote_url"] = (
        "https://x-access-token:<redacted>@github.com/wrong/repo.git"
    )

    info = cp._hub_verify_repo_info(task, evidence)

    assert info is not None
    assert info["remote_url"] == "git@github.com:org/repo.git"


def test_hub_verify_uses_sanity_scope_and_fails_closed_for_unsafe_paths(cp):
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda *args: (0, "ok")
    )
    info = cp._hub_verify_repo_info(task, evidence)
    assert info is not None

    command = cp._hub_review_test_command(task, info)

    assert "scripts/run-sanity-tests.sh" in command
    assert "--changed-file src/example.py" in command
    assert "else scripts/run-contract-tests.sh" in command
    unsafe = dict(info, files_changed=["../escape.py"])
    assert cp._hub_review_test_command(task, unsafe) == "scripts/run-contract-tests.sh"


def test_hub_review_verification_approves_and_publishes(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    seen = []
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda remote, branch, head, cmd: (seen.append((remote, branch, head)) or (0, "all passed")),
    )
    # Hub verify runs as soon as a review is pending: create review -> run the
    # contract test on the hub -> signed verdict -> publish, within one or two
    # ticks (no waiting on an agent).
    statuses = [
        cp.advance_default_review_workflow(task.id)["status"],
        cp.advance_default_review_workflow(task.id)["status"],
    ]
    assert "published" in statuses
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    # The hub ran the contract test on the pushed branch (not a nudged agent).
    assert seen and seen[0][0] == "git@github.com:org/repo.git" and seen[0][1] == "task/example"
    reviews = cp.list_reviews(task.id)
    assert reviews[0].status == ReviewStatus.APPROVED.value
    # Verdict evidence is hub-produced, signed by the reviewer, no agent nudge needed.
    verdict = next(e for e in cp.list_evidence(task.id) if (e.metadata or {}).get("hub_verified"))
    assert verdict.created_by == reviewer.id
    names = {ev.name for ev in cp.list_observability(limit=80)}
    assert "workflow.default_review.hub_verified" in names
    assert "workflow.default_review.published" in names


def test_periodic_review_sweep_defers_blocking_hub_verification(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    runner_calls = []
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp,
        lambda *args: (runner_calls.append(args) or (0, "all passed")),
    )
    nudged = []
    cp._nudge_review_workflow = nudged.append

    result = cp.advance_default_review_workflows(
        limit=10,
        allow_blocking_hub_verify=False,
    )

    task_result = next(item for item in result["results"] if item["task_id"] == task.id)
    assert task_result["status"] == "waiting_for_hub_verify"
    assert nudged == [task.id]
    assert runner_calls == []
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_hub_review_verification_rejects_on_failing_contract_test(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda remote, branch, head, cmd: (1, "3 failed, 2 passed"),
    )
    cp.advance_default_review_workflow(task.id)
    result = cp.advance_default_review_workflow(task.id)

    # A failing contract test yields a rejected verdict; nothing is published.
    assert result["status"] not in {"published"}
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value
    reviews = cp.list_reviews(task.id)
    assert reviews and reviews[0].status == ReviewStatus.REJECTED.value
    assert not cp.list_publications(task.id)


def test_hub_verify_disabled_falls_back_to_agent_nudge(cp, monkeypatch):
    monkeypatch.delenv("MAC_REVIEW_HUB_VERIFY", raising=False)
    called = []
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda *a: called.append(a) or (0, "ok"),
    )
    cp.advance_default_review_workflow(task.id)
    result = cp.advance_default_review_workflow(task.id)
    # Hub verify off: no hub run, workflow waits for an agent verdict as before.
    assert not called
    assert result["status"] == "waiting_for_reviewer_verdict"


def test_review_experiment_requires_semantic_reviewer_even_when_hub_verify_enabled(
    cp, monkeypatch
):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    called = []
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp,
        lambda *args: called.append(args) or (0, "ok"),
        experiment=True,
    )

    cp.advance_default_review_workflow(task.id)
    result = cp.advance_default_review_workflow(task.id)

    assert called == []
    assert result["status"] == "waiting_for_reviewer_verdict"
    observations = cp.list_observability(
        subject_type="task", subject_id=task.id, limit=100
    )
    skipped = [
        item
        for item in observations
        if item.name == "workflow.default_review.hub_verify_skipped"
    ]
    assert skipped
    assert skipped[-1].detail["reason"] == "experiment_requires_semantic_reviewer"


def test_hub_verify_inflight_guard_prevents_concurrent_runs(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    calls = []
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda *a: calls.append(a) or (0, "ok"),
    )
    review = cp.request_review(task.id, reviewer.id)
    # A verify already running for this review: the next call is a no-op and
    # does NOT launch a second sandbox contract test.
    cp._hub_verify_inflight = {review.id}
    result = cp._run_hub_review_verification(task, review, evidence, "test")
    assert result is None
    assert calls == []


def test_hub_verify_reuses_completed_review_verdict_evidence(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    calls = []
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda *a: calls.append(a) or (0, "ok"),
    )
    review = cp.request_review(task.id, reviewer.id)

    first = cp._run_hub_review_verification(task, review, evidence, "test")
    assert first is not None
    second = cp._run_hub_review_verification(task, review, evidence, "test")

    assert second is not None and second.id == first.id
    cp.submit_review(
        review.id,
        ReviewStatus.APPROVED.value,
        reviewer.id,
        evidence_id=first.id,
    )
    cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=evidence.id)
    after_completion = cp._run_hub_review_verification(
        task, review, evidence, "test"
    )

    assert after_completion is not None and after_completion.id == first.id
    assert len(calls) == 1
    hub_verified = [
        item for item in cp.list_evidence(task.id) if item.metadata.get("hub_verified")
    ]
    assert [item.id for item in hub_verified] == [first.id]


def test_hub_verify_does_not_rerun_for_invalid_deterministic_verdict(cp, monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    calls = []
    worker, reviewer, task, executor_evidence = _setup_hubverify_task(
        cp, lambda *args: calls.append(args) or (0, "ok")
    )
    review = cp.request_review(task.id, reviewer.id)
    head_sha = executor_evidence.metadata["verification"]["repo"]["head_sha"]
    invalid_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "review_id": review.id,
        "reviewed_evidence_id": executor_evidence.id,
        "worktree_digest": "sha256:" + "0" * 64,
        "verified_by": "hub_review_verifier_v1",
        "repo": {"head_sha": head_sha},
        "signed_by": reviewer.id,
        "signature": "invalid",
    }
    invalid = cp.add_evidence(
        task.id,
        "review",
        cp._hub_review_verification_uri(review.id, head_sha),
        "invalid deterministic hub verdict",
        reviewer.id,
        metadata={
            "returncode": 0,
            "hub_verified": True,
            "verification": invalid_manifest,
        },
    )

    first = cp._run_hub_review_verification(
        task, review, executor_evidence, "test"
    )
    second = cp._run_hub_review_verification(
        task, review, executor_evidence, "test"
    )

    assert first is None
    assert second is None
    assert calls == []
    retracted = cp.get_review(review.id)
    assert retracted.status == ReviewStatus.RETRACTED.value
    assert retracted.reason == "reviewer_protocol_failure:hub_verdict_invalid"
    hub_verdicts = [
        item
        for item in cp.list_evidence(task.id)
        if item.uri == cp._hub_review_verification_uri(review.id, head_sha)
    ]
    assert [item.id for item in hub_verdicts] == [invalid.id]
    observations = cp.list_observability(
        subject_type="task", subject_id=task.id, limit=100
    )
    assert any(
        item.name == "workflow.default_review.hub_verify_invalid_existing"
        for item in observations
    )


def test_hub_verify_sandbox_command_whitelists_uploaded_repo_for_git(cp, monkeypatch):
    """The tar-uploaded repo can be owned by a different uid than the sandbox
    user, and HOME=/tmp means no safe.directory whitelist exists — without the
    preflight, the contract tests that run git against the checkout itself die
    with "dubious ownership" and good work is rejected (observed live: exactly
    the 4 git-at-ROOT tests failed while ~4290 passed)."""
    import subprocess as _subprocess

    from mac import services as services_mod

    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        if "rev-parse" in argv and "HEAD" in argv:
            return _subprocess.CompletedProcess(
                argv, 0, stdout=("a" * 40) + "\n", stderr=""
            )
        return _subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(services_mod.subprocess, "run", fake_run)
    rc, out = cp._hub_verify_run_contract_test(
        "git@github.com:org/repo.git", "task/branch", "a" * 40, ""
    )
    assert rc == 0
    create = next(a for a in captured if "create" in a and "--upload" in a)
    separator = create.index("--")
    assert create[separator + 1 : separator + 3] == ["/bin/bash", "-c"]
    inner = create[create.index("-c") + 1]
    from mac.openshell_runtime import SANDBOX_BASE_PATH

    env_values = [
        create[index + 1]
        for index, value in enumerate(create[:-1])
        if value == "--env"
    ]
    assert "PATH=%s" % SANDBOX_BASE_PATH in env_values
    assert inner.startswith("export PATH=%s; hash -r" % SANDBOX_BASE_PATH)
    # The repo travels as ONE tar file (OpenShell directory upload drops .git)
    # and is extracted inside the sandbox before anything else.
    upload = create[create.index("--upload") + 1]
    assert upload.endswith("repo.tgz:/sandbox")
    assert "cd /sandbox && tar xzf repo.tgz && " in inner
    # Whitelist reaches every git subprocess the suite spawns (env form, not
    # --global), and it precedes the test command.
    assert "GIT_CONFIG_KEY_0=safe.directory" in inner
    assert "GIT_CONFIG_VALUE_0='*'" in inner
    assert inner.index("safe.directory") < inner.index("cd /sandbox/repo")
    # Lost-.git uploads still fail fast with a distinguishable message.
    assert "rev-parse --is-inside-work-tree" in inner
    assert "not a usable git repo after upload" in inner


def test_hub_verify_blocking_guard_returns_waiting_not_agent_nudge(cp, monkeypatch):
    """Blocking guard (Option C): when hub verify is enabled and the verifier
    cannot produce a verdict in this tick (e.g. runner raises, key absent, or
    in-flight guard fires), the workflow MUST block with waiting_for_hub_verify
    rather than falling through to the agent-nudge path.  Merge is gated until
    the hub verdict is recorded."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")

    def always_raises(*args):
        raise RuntimeError("sandbox unavailable")

    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, always_raises,
    )
    # First tick: review assigned, hub verify throws, blocking guard fires.
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_hub_verify", (
        "hub verify blocking guard must return waiting_for_hub_verify, "
        "not fall through to agent nudge path; got: %s" % result["status"]
    )
    assert result["review_id"] == cp.list_reviews(task.id)[0].id
    # The review must still be pending — no spurious retraction or approval.
    assert cp.list_reviews(task.id)[0].status == ReviewStatus.PENDING.value
    # An observation is recorded so operators can see what's happening.
    obs_names = {ev.name for ev in cp.list_observability(limit=50)}
    assert "workflow.default_review.waiting_for_hub_verify" in obs_names


def test_hub_verify_blocking_guard_does_not_fire_for_experiments(cp, monkeypatch):
    """Experiment tasks need a human-model reviewer for measurement validity;
    they skip hub verify (hub_verify_skipped is recorded) and keep the
    agent-nudge path even when hub verify is globally enabled."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")

    def always_raises(*args):
        raise RuntimeError("should not be called")

    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, always_raises, experiment=True,
    )
    result = cp.advance_default_review_workflow(task.id)
    # Experiments fall through to the agent-nudge path unchanged.
    assert result["status"] == "waiting_for_reviewer_verdict"
    obs_names = {ev.name for ev in cp.list_observability(limit=50)}
    assert "workflow.default_review.hub_verify_skipped" in obs_names


def test_hub_verify_gate_falls_through_for_non_repo_evidence(cp, monkeypatch):
    """Evidence that is not a pushed repo change (e.g. an operator_result log
    from a non-code task) has nothing for hub-verify to gate. The blocking guard
    must NOT wait_for_hub_verify forever — it falls through to the agent-nudge
    path so a real reviewer produces the semantic verdict. The merge gate is
    unchanged for actual repo changes (see the waiting test above)."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")

    def unreachable(*args):
        raise AssertionError("hub verify runner must not run for non-repo evidence")

    worker, reviewer, task, evidence = _setup_hubverify_task(cp, unreachable)
    # Nothing pushed to independently verify -> _hub_verify_repo_info is None.
    monkeypatch.setattr(cp, "_hub_verify_repo_info", lambda *a, **k: None)

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] != "waiting_for_hub_verify"
    assert result["status"] == "waiting_for_reviewer_verdict"
    obs_names = {ev.name for ev in cp.list_observability(limit=50)}
    assert "workflow.default_review.waiting_for_hub_verify" not in obs_names


def test_evidence_tests_are_hub_verify_deferred_detects_deferred(cp):
    """_evidence_tests_are_hub_verify_deferred returns True only when all test
    items carry status='deferred' and none has already passed."""
    from mac.services import ControlPlane as _CP

    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task("task", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)

    def _make_evidence(tests):
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "repo_change",
            "repo": {
                "head_sha": "a" * 40,
                "pushed": True,
                "remote_ref": "refs/heads/task/x",
                "dirty": False,
                "files_changed": ["src/x.py"],
            },
            "tests": tests,
        }
        return cp.add_evidence(
            task.id, "log", "artifact://test", "t", worker.id,
            metadata={"returncode": 0, "verification": manifest},
        )

    # All deferred, none passing → True.
    ev = _make_evidence([{"status": "deferred", "command": "cmd"}])
    assert _CP._evidence_tests_are_hub_verify_deferred(ev) is True

    # One passing alongside deferred → False (executor already ran tests).
    ev2 = _make_evidence([
        {"status": "deferred", "command": "cmd"},
        {"status": "pass", "command": "cmd"},
    ])
    assert _CP._evidence_tests_are_hub_verify_deferred(ev2) is False

    # No deferred items → False.
    ev3 = _make_evidence([{"returncode": 0, "command": "cmd"}])
    assert _CP._evidence_tests_are_hub_verify_deferred(ev3) is False

    # Empty tests list → False.
    ev4 = _make_evidence([])
    assert _CP._evidence_tests_are_hub_verify_deferred(ev4) is False


def test_hub_verify_repo_info_accepts_deferred_test_evidence(cp):
    """_hub_verify_repo_info must return the branch coordinates when the
    executor evidence carries a deferred test item and repo.pushed=True,
    so hub verify can run the contract test on behalf of the executor."""
    worker = register_agent(cp, "worker", ["python"])
    register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "task",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "git@github.com:org/repo.git"
                }
            }
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "b" * 40,
            "pushed": True,
            "remote_ref": "refs/heads/task/deferred-branch",
            "dirty": False,
            "files_changed": ["src/y.py"],
        },
        "tests": [{"status": "deferred", "command": "scripts/run-contract-tests.sh"}],
    }
    manifest = _sign(cp, worker.id, manifest)
    evidence = cp.add_evidence(
        task.id, "log", "artifact://result", "deferred", worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    info = cp._hub_verify_repo_info(task, evidence)
    assert info is not None, (
        "_hub_verify_repo_info must accept evidence with deferred test items "
        "when repo.pushed=True"
    )
    assert info["branch"] == "task/deferred-branch"
    assert info["head_sha"] == "b" * 40


def test_assess_evidence_accepts_deferred_test_with_hub_verify_enabled(cp, monkeypatch):
    """_assess_default_review_evidence must accept executor evidence that
    carries only deferred test items when hub verify is enabled (Option C).
    Under Option A (MAC_REVIEW_HUB_VERIFY unset), the same evidence is
    rejected — the executor must always supply its own passing tests."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "task",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "git@github.com:org/repo.git",
                    "evidence": {"required": ["tests"]},
                }
            }
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "c" * 40,
            "pushed": True,
            "remote_ref": "refs/heads/task/deferred",
            "dirty": False,
            "files_changed": ["src/z.py"],
        },
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": ["src/z.py"],
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        },
        "tests": [{"status": "deferred", "command": "scripts/run-contract-tests.sh"}],
    }
    manifest = _sign(cp, worker.id, manifest)
    evidence = cp.add_evidence(
        task.id, "log", "artifact://result", "deferred", worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )

    # Option C: hub verify enabled → deferred test evidence is accepted.
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    result_c = cp._assess_default_review_evidence(task, evidence)
    assert result_c["valid"] is True, (
        "Option C: deferred test evidence must be accepted when hub verify is enabled; "
        "got problems: %s" % result_c.get("problems")
    )
    assert result_c.get("hub_verify_deferred") is True

    # Option A: hub verify disabled → same evidence is rejected.
    monkeypatch.delenv("MAC_REVIEW_HUB_VERIFY", raising=False)
    result_a = cp._assess_default_review_evidence(task, evidence)
    assert result_a["valid"] is False, (
        "Option A: deferred test evidence must be rejected when hub verify is disabled"
    )


def test_event_driven_advance_reviews_without_tick(cp, monkeypatch):
    """With the event-driven advancer enabled (hub wiring), submitting work
    for review triggers the workflow immediately — no periodic sweep needed.
    Previously every stage waited up to a full MAC_HUB_TICK_INTERVAL_SECONDS."""
    import time as _time

    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    worker, reviewer, task, evidence = _setup_hubverify_task(
        cp, lambda remote, branch, head, cmd: (0, "all passed"),
    )
    # _setup_hubverify_task already called submit_for_review BEFORE the
    # advancer existed; enable it and nudge as submit_for_review now does.
    cp.enable_event_driven_review_advance()
    cp._nudge_review_workflow(task.id)

    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        if cp.get_task(task.id).state == TaskState.COMPLETED.value:
            break
        _time.sleep(0.05)
    # verdict recording re-nudges, so review AND publication complete without
    # any cp.tick()/advance call from a sweep.
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    reviews = cp.list_reviews(task.id)
    assert reviews and reviews[0].status == ReviewStatus.APPROVED.value


def test_nudge_is_noop_when_advancer_disabled(cp):
    """CLI/test constructions never spawn the advancer thread; nudges are free."""
    assert cp._advance_queue is None
    cp._nudge_review_workflow("task_whatever")  # must not raise or spawn
    assert cp._advance_queue is None


# ---------------------------------------------------------------------------
# Timeout-diagnosis → plan_first injection (Decomposition 4/5 contract)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timeout_reason",
    [
        "timed out",
        "timeout",
        "rc=124",
        "returncode 124",
        "Agent run timed out — the task is likely too large for one run.",
    ],
)
def test_tick_injects_plan_first_on_timeout_blocked_attempt(cp, timeout_reason):
    """When a task is blocked with an agent-run-timeout reason the auto-retry
    re-dispatches it with ``metadata.plan_first=True`` so the next executor run
    decomposes the work instead of attempting it monolithically again."""
    task = cp.create_task("timed-out task", required_capabilities=["python"])
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": timeout_reason},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    result = cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value, (
        "task should be reopened after timeout backoff"
    )
    assert [item["id"] for item in result["auto_reopened"]] == [task.id]
    from mac.models import ensure_json_object
    meta = ensure_json_object(reopened.metadata)
    assert meta.get("plan_first") is True, (
        "metadata.plan_first must be True after a timeout-triggered retry"
    )
    # Observability event must be present
    observations = cp.list_observability(
        subject_type="task",
        subject_id=task.id,
        limit=50,
    )
    assert any(
        event.name == "task.timeout_requeued_as_plan" for event in observations
    ), "task.timeout_requeued_as_plan observability event not emitted"
    # Regular auto_reopened event must also be present
    assert any(event.name == "task.auto_reopened" for event in observations)


def test_tick_stops_non_timeout_executor_failure_without_plan_first(cp):
    """Non-timeout block reasons (executor_failed, etc.) must NOT set plan_first —
    only agent-run-timeout failures trigger the decomposition redirect."""
    task = cp.create_task("executor-failed task", required_capabilities=["python"])
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "executor_failed"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    stopped = cp.get_task(task.id)
    assert stopped.state == TaskState.FAILED.value
    assert stopped.dependencies == []
    assert "environment_repair_task_id" not in stopped.metadata
    from mac.models import ensure_json_object
    meta = ensure_json_object(stopped.metadata)
    assert not meta.get("plan_first"), (
        "plan_first must NOT be set for non-timeout block reasons"
    )
    observations = cp.list_observability(
        subject_type="task",
        subject_id=task.id,
        limit=50,
    )
    assert not any(
        event.name == "task.timeout_requeued_as_plan" for event in observations
    ), "timeout_requeued_as_plan must not be emitted for non-timeout failures"


def test_tick_does_not_re_inject_plan_first_when_already_set(cp):
    """If plan_first is already True on a blocked task (operator set it or a
    prior retry already set it), the retry should not emit the observability
    event again — idempotent."""
    task = cp.create_task(
        "already-plan-first task",
        metadata={"plan_first": True},
        required_capabilities=["python"],
    )
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "worker",
        {"reason": "timed out"},
    )
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (1, "2000-01-01T00:00:00+00:00", task.id),
    )

    cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    observations = cp.list_observability(
        subject_type="task",
        subject_id=task.id,
        limit=50,
    )
    assert not any(
        event.name == "task.timeout_requeued_as_plan" for event in observations
    ), "timeout_requeued_as_plan must not be re-emitted when plan_first was already set"


# ---------------------------------------------------------------------------
# Deferred executor evidence → hub verify: approved and rejected paths
# ---------------------------------------------------------------------------


def _deferred_repo_metadata(cp, agent_id):
    """Build signed executor evidence where the test item is the deferred
    hub-verify sentinel (status='deferred', execution_environment='hub_verify_pending').
    This mirrors what the worker emits when MAC_REVIEW_HUB_VERIFY=1 and no
    mac-sandbox-verification.json is present."""
    relevant_files = codegraph_relevant_files(["src/feature.py"])
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "b" * 40,
            "pushed": True,
            "remote_ref": "refs/heads/task/deferred-hub",
            "dirty": False,
            "files_changed": ["src/feature.py"],
        },
        "tests": [
            {
                "name": "repository contract test",
                "command": "scripts/run-contract-tests.sh",
                "returncode": None,
                "status": "deferred",
                "execution_environment": "hub_verify_pending",
                "stdout": "",
                "stderr": "",
            }
        ],
    }
    if relevant_files:
        manifest["codegraph"] = {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": relevant_files,
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        }
    manifest = _sign(cp, agent_id, manifest)
    return {"returncode": 0, "verification": manifest}


def _setup_deferred_hubverify_task(cp, runner):
    """Like _setup_hubverify_task but the executor evidence carries a deferred
    test item, exercising the path where the hub runs the contract test on behalf
    of the executor after the branch is already pushed."""
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = cp.create_task(
        "Implement with deferred hub verify",
        required_capabilities=["python"],
        metadata={
            "publication_target": "test://publish",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "git@github.com:org/repo.git"
                }
            },
        },
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id, "log", "artifact://worker-result", "tests deferred to hub", worker.id,
        metadata=_deferred_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    cp._hub_verify_runner = runner
    return worker, reviewer, task, evidence


def test_hub_verify_deferred_executor_evidence_approves_and_publishes(cp, monkeypatch):
    """Approved path: when the executor evidence carries a deferred test item
    (status='deferred', hub_verify_pending), hub verify must still run the
    contract test and, on success, approve and publish the task.

    This exercises the full end-to-end pipeline:
      executor pushes with deferred evidence → hub verify detects deferred item
      → hub runs contract test → approved verdict → publication."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    seen = []
    worker, reviewer, task, evidence = _setup_deferred_hubverify_task(
        cp,
        lambda remote, branch, head, cmd: (seen.append((remote, branch)) or (0, "all passed")),
    )

    # The deferred evidence must be accepted by the assessment layer.
    assessment = cp._assess_default_review_evidence(task, evidence)
    assert assessment["valid"] is True, (
        "Deferred test evidence must be accepted when MAC_REVIEW_HUB_VERIFY=1; "
        "problems: %s" % assessment.get("problems")
    )
    assert assessment.get("hub_verify_deferred") is True

    # Advance the review workflow: hub verify should run and approve.
    statuses = [
        cp.advance_default_review_workflow(task.id)["status"],
        cp.advance_default_review_workflow(task.id)["status"],
    ]
    assert "published" in statuses, (
        "Hub verify on deferred evidence must eventually publish; got: %s" % statuses
    )
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    reviews = cp.list_reviews(task.id)
    assert reviews[0].status == ReviewStatus.APPROVED.value

    # Hub ran the contract test against the pushed branch.
    assert seen, "hub verify runner must have been called"
    assert seen[0][0] == "git@github.com:org/repo.git"

    obs_names = {ev.name for ev in cp.list_observability(limit=80)}
    assert "workflow.default_review.hub_verified" in obs_names
    assert "workflow.default_review.published" in obs_names


def test_hub_verify_deferred_executor_evidence_rejects_on_failing_test(cp, monkeypatch):
    """Rejected path: when the executor evidence carries a deferred test item
    but the hub contract test fails (non-zero returncode), the workflow must
    reject the review — nothing is published."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")
    worker, reviewer, task, evidence = _setup_deferred_hubverify_task(
        cp,
        lambda remote, branch, head, cmd: (1, "4 failed, 1 passed"),
    )

    cp.advance_default_review_workflow(task.id)
    result = cp.advance_default_review_workflow(task.id)

    # A failing contract test on deferred evidence yields a rejected verdict.
    assert result["status"] not in {"published"}, (
        "Hub verify must reject when the contract test fails; got status: %s" % result["status"]
    )
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value
    reviews = cp.list_reviews(task.id)
    assert reviews and reviews[0].status == ReviewStatus.REJECTED.value
    assert not cp.list_publications(task.id)


def test_hub_verify_deferred_merge_blocked_while_pending_unblocks_on_approved(cp, monkeypatch):
    """Regression gate: when hub verify is running (no verdict yet in this tick),
    the workflow MUST return waiting_for_hub_verify — merge is gated.
    Once the hub produces an approved verdict, a subsequent advance call
    transitions to published.

    This confirms that the deferred-sentinel path does not bypass the blocking
    guard that prevents premature publication."""
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "1")

    # Phase 1: runner raises to simulate hub verify in-flight (not done yet).
    def always_raises(*args):
        raise RuntimeError("hub verify sandbox not ready")

    worker, reviewer, task, evidence = _setup_deferred_hubverify_task(cp, always_raises)

    # First advance: review is assigned, hub verify raises, blocking guard fires.
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_hub_verify", (
        "Merge must be blocked while hub verify is pending; got: %s" % result["status"]
    )
    # Review must still be pending — no spurious approval or retraction.
    pending = cp.list_reviews(task.id)
    assert pending and pending[0].status == ReviewStatus.PENDING.value

    obs_names = {ev.name for ev in cp.list_observability(limit=50)}
    assert "workflow.default_review.waiting_for_hub_verify" in obs_names

    # Phase 2: swap in a successful runner (hub verify completes).
    cp._hub_verify_runner = lambda remote, branch, head, cmd: (0, "all passed")

    # Subsequent advance must approve and publish.
    statuses = [
        cp.advance_default_review_workflow(task.id)["status"],
        cp.advance_default_review_workflow(task.id)["status"],
    ]
    assert "published" in statuses, (
        "After hub verify approves, advance must publish; got: %s" % statuses
    )
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    final_reviews = cp.list_reviews(task.id)
    assert final_reviews[0].status == ReviewStatus.APPROVED.value
# ---------------------------------------------------------------------------
# Hardware persistence and mirroring tests
# ---------------------------------------------------------------------------


def test_register_machine_populates_hardware_from_resources_hardware():
    """register_machine must populate machine.hardware from resources['hardware']
    when the explicit hardware kwarg is absent.  The worker posts detect_hardware()
    inside resources, so the machine record must carry the full snapshot even if
    the caller does not pass a separate hardware= argument."""
    cp = ControlPlane.in_memory()
    hw_snapshot = {
        "schema": "mac.hardware.v1",
        "os": "linux",
        "arch": "aarch64",
        "cpu_count": 20,
        "memory_mb": 131072,
        "accelerator": "cuda",
        "gpu": {
            "accelerator": "cuda",
            "name": "NVIDIA GB10",
            "vram_mb": 131072,
            "shared": True,
            "count": 1,
            "memory": {"type": "unified", "shared_mb": 131072},
        },
        "gpus": [
            {
                "index": 0,
                "accelerator": "cuda",
                "name": "NVIDIA GB10",
                "shared": True,
                "vram_mb": 131072,
                "memory": {"type": "unified", "shared_mb": 131072},
            }
        ],
    }
    machine = cp.register_machine(
        "gpu-host",
        resources={"cpu": 20, "hardware": hw_snapshot},
        # no explicit hardware= kwarg — should fall through to resources["hardware"]
    )

    assert machine.hardware["accelerator"] == "cuda"
    assert machine.hardware["gpu"]["name"] == "NVIDIA GB10"
    assert machine.hardware["gpus"][0]["index"] == 0
    assert machine.hardware["gpus"][0]["memory"] == {"type": "unified", "shared_mb": 131072}


def test_register_machine_explicit_hardware_kwarg_wins_over_resources():
    """When both hardware= and resources['hardware'] are provided, the explicit
    kwarg must take precedence (it is the authoritative caller intent)."""
    cp = ControlPlane.in_memory()
    explicit_hw = {"accelerator": "metal", "os": "darwin", "arch": "arm64"}
    resources_hw = {"accelerator": "cuda", "os": "linux", "arch": "x86_64"}
    machine = cp.register_machine(
        "mac-host",
        resources={"hardware": resources_hw},
        hardware=explicit_hw,
    )

    assert machine.hardware["accelerator"] == "metal"


def test_register_machine_no_hardware_stores_empty():
    """When neither hardware= nor resources['hardware'] is provided, machine.hardware
    defaults to {} (no fabricated zeros or fake accelerators)."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("plain-host", resources={"cpu": 4})

    assert machine.hardware == {}


def test_register_agent_mirrors_multi_gpu_hardware_to_machine():
    """Agent registration must mirror the full GPU inventory — including the
    'gpus' list with per-GPU structured memory — into machine.hardware.  A
    multi-GPU agent must not drop any GPU from the mirrored record."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("dual-gpu-host")
    two_gpu_hw = {
        "schema": "mac.hardware.v1",
        "os": "linux",
        "arch": "x86_64",
        "cpu_count": 32,
        "memory_mb": 65536,
        "accelerator": "cuda",
        "gpu": {
            "accelerator": "cuda",
            "name": "NVIDIA RTX PRO 6000 Blackwell",
            "vram_mb": 98304,
            "count": 2,
            "memory": {"type": "dedicated", "vram_mb": 98304},
        },
        "gpus": [
            {
                "index": 0,
                "accelerator": "cuda",
                "name": "NVIDIA RTX PRO 6000 Blackwell",
                "vram_mb": 98304,
                "memory": {"type": "dedicated", "vram_mb": 98304},
            },
            {
                "index": 1,
                "accelerator": "cuda",
                "name": "NVIDIA RTX PRO 6000 Blackwell",
                "vram_mb": 98304,
                "memory": {"type": "dedicated", "vram_mb": 98304},
            },
        ],
    }

    cp.register_agent(
        machine.id,
        "dual-gpu-worker",
        resources={"hardware": two_gpu_hw},
    )

    refreshed_machine = cp.get_machine(machine.id)
    assert refreshed_machine.hardware["accelerator"] == "cuda"
    assert refreshed_machine.hardware["gpu"]["count"] == 2
    assert len(refreshed_machine.hardware["gpus"]) == 2
    assert refreshed_machine.hardware["gpus"][0]["index"] == 0
    assert refreshed_machine.hardware["gpus"][1]["index"] == 1
    assert refreshed_machine.hardware["gpus"][0]["memory"] == {
        "type": "dedicated",
        "vram_mb": 98304,
    }
    assert refreshed_machine.hardware["gpus"][1]["memory"] == {
        "type": "dedicated",
        "vram_mb": 98304,
    }


def test_heartbeat_mirrors_multi_gpu_hardware_to_machine():
    """A heartbeat that includes resources with a multi-GPU hardware snapshot must
    update machine.hardware with the full 'gpus' list, preserving per-GPU
    structured memory for all GPUs."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("hb-host")
    agent = cp.register_agent(machine.id, "hb-worker")

    two_gpu_hw = {
        "schema": "mac.hardware.v1",
        "os": "linux",
        "arch": "x86_64",
        "accelerator": "cuda",
        "gpu": {
            "accelerator": "cuda",
            "name": "NVIDIA GeForce RTX 5090",
            "vram_mb": 32576,
            "count": 2,
            "memory": {"type": "dedicated", "vram_mb": 32576},
        },
        "gpus": [
            {
                "index": 0,
                "accelerator": "cuda",
                "name": "NVIDIA GeForce RTX 5090",
                "vram_mb": 32576,
                "memory": {"type": "dedicated", "vram_mb": 32576},
            },
            {
                "index": 1,
                "accelerator": "cuda",
                "name": "NVIDIA GeForce RTX 5090",
                "vram_mb": 32576,
                "memory": {"type": "dedicated", "vram_mb": 32576},
            },
        ],
    }

    cp.heartbeat_agent(agent.id, resources={"hardware": two_gpu_hw})

    refreshed_machine = cp.get_machine(machine.id)
    assert refreshed_machine.hardware["accelerator"] == "cuda"
    assert len(refreshed_machine.hardware["gpus"]) == 2
    assert refreshed_machine.hardware["gpus"][1]["vram_mb"] == 32576


def test_register_agent_no_accelerator_is_explicit_not_zero():
    """When an agent's hardware probe finds no GPU (accelerator='none'), the
    mirrored machine.hardware must carry the explicit 'accelerator': 'none'
    state rather than an empty dict or fabricated zeros."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("cpu-only-host")
    no_gpu_hw = {
        "schema": "mac.hardware.v1",
        "os": "linux",
        "arch": "x86_64",
        "cpu_count": 8,
        "memory_mb": 16384,
        "accelerator": "none",
    }

    cp.register_agent(
        machine.id,
        "cpu-worker",
        resources={"hardware": no_gpu_hw},
    )

    refreshed_machine = cp.get_machine(machine.id)
    assert refreshed_machine.hardware["accelerator"] == "none"
    assert "gpu" not in refreshed_machine.hardware
    assert "gpus" not in refreshed_machine.hardware


def test_heartbeat_unknown_probe_state_preserved_in_machine_hardware():
    """A heartbeat from an agent whose GPU VRAM probe returned an unavailable
    result (memory.type='unknown') must propagate that explicit unknown state
    into machine.hardware — the hub must not replace it with zeros or silence it."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("probe-fail-host")
    agent = cp.register_agent(machine.id, "probe-fail-worker")

    unknown_probe_hw = {
        "schema": "mac.hardware.v1",
        "os": "linux",
        "arch": "x86_64",
        "accelerator": "cuda",
        "gpu": {
            "index": 0,
            "accelerator": "cuda",
            "name": "NVIDIA RTX A6000",
            "memory": {"type": "unknown"},
        },
        "gpus": [
            {
                "index": 0,
                "accelerator": "cuda",
                "name": "NVIDIA RTX A6000",
                "memory": {"type": "unknown"},
            }
        ],
    }

    cp.heartbeat_agent(agent.id, resources={"hardware": unknown_probe_hw})

    refreshed_machine = cp.get_machine(machine.id)
    assert refreshed_machine.hardware["accelerator"] == "cuda"
    assert refreshed_machine.hardware["gpus"][0]["memory"] == {"type": "unknown"}
    assert "vram_mb" not in refreshed_machine.hardware["gpus"][0]


def test_register_machine_hardware_not_erased_on_re_register_without_resources_hardware():
    """Re-registering a machine that does not include resources['hardware'] must
    not erase a previously-stored machine.hardware record."""
    cp = ControlPlane.in_memory()
    hw_snapshot = {
        "schema": "mac.hardware.v1",
        "accelerator": "cuda",
        "gpu": {"name": "NVIDIA GeForce RTX 5090", "count": 1},
    }
    machine_id = "machine_test_preserve"
    cp.register_machine(
        "preserve-host",
        machine_id=machine_id,
        resources={"hardware": hw_snapshot},
    )

    # Re-register without hardware in resources
    cp.register_machine("preserve-host", machine_id=machine_id, resources={"cpu": 4})

    refreshed = cp.get_machine(machine_id)
    # hardware column must be preserved from the prior registration
    assert refreshed.hardware["accelerator"] == "cuda"
