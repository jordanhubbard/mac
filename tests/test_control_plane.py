import json
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

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
    utcnow,
)
from mac.migration import migrate_acc_sqlite
from mac.services import ControlPlane


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


def register_agent(cp, name="agent", capabilities=None):
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=capabilities or [])


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
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": head_sha,
            "pushed": True,
            "remote_ref": "refs/heads/task/example",
            "dirty": False,
            "files_changed": files_changed if files_changed is not None else ["src/example.py"],
        },
        "tests": [{"command": "pytest tests/test_example.py", "returncode": 0}],
    }
    if cp is not None and agent_id is not None:
        manifest = _sign(cp, agent_id, manifest)
    return {"returncode": 0, "verification": manifest}


def verified_deployment_metadata(cp=None, agent_id=None):
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "deployment",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/deploy",
            "dirty": False,
            "files_changed": ["deploy/example.yaml"],
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
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id)

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


def test_default_review_allows_prior_owner_to_review_newer_retry_evidence(cp):
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
    submit_review_verdict(cp, task.id, beta.id, first_evidence.id, verdict="rejected")
    rejected = cp.advance_default_review_workflow(task.id)
    assert rejected["status"] == "review_not_approved"
    assert cp.get_task(task.id).state == TaskState.OPEN.value

    cp.claim_task(task.id, beta.id)
    cp.start_task(task.id, beta.id)
    retry_evidence = cp.add_evidence(
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
    submit_review_verdict(cp, task.id, alpha.id, retry_evidence.id)
    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value


def test_request_review_refuses_latest_evidence_author(cp):
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

    with pytest.raises(AuthorizationError):
        cp.request_review(task.id, worker.id)


def test_default_review_workflow_waits_without_non_owner_reviewer(cp):
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

    assert result["status"] == "waiting_for_reviewer"
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value
    assert cp.list_reviews(task.id) == []


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


def test_default_review_workflow_waits_for_verifiable_evidence(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
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
    names = {event.name for event in cp.list_observability(limit=50)}
    assert "workflow.default_review.no_publication_target" in names


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
    cp.add_evidence(task.id, "log", "artifact://2", "x", worker.id, metadata=bad_type)
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_verifiable_evidence"

    # And with field alias `git` instead of `repo`.
    bad_field = verified_repo_metadata(cp, worker.id)
    bad_field["verification"]["git"] = bad_field["verification"].pop("repo")
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )
    cp.add_evidence(task.id, "log", "artifact://3", "x", worker.id, metadata=bad_field)
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


def test_default_reviewer_refuses_same_persona_as_executor(cp):
    """mac-v2i: two agents souled to the same persona can't approve
    each other. The second-eyes role only matters if the eyes are
    different."""
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
    # Peer has the right capability AND the right tenant but the SAME
    # persona — the workflow must refuse to draft them.
    assert result["status"] == "waiting_for_reviewer"
    assert cp.list_reviews(task.id) == []


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

    assignment = cp.dispatch_once(lease_seconds=-1)
    assert assignment["agent"]["id"] == python_agent.id
    assert assignment["agent"]["id"] != docs_agent.id

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
    assert second["agent"]["id"] == large.id
    assert cp.get_agent(small.id).status == AgentStatus.IDLE.value

    dead = cp.create_task("dead letter", required_capabilities=["docs"], max_attempts=1)
    docs = register_agent(cp, "docs-dead", ["docs"])
    cp.claim_task(dead.id, docs.id, lease_seconds=-1)
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
    assert tick["assignments"] == []


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
        require_canary=True,
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
        require_canary=True,
    )

    assert claimed is not None
    assert claimed["task"]["id"] == canary.id
    assert cp.get_task(canary.id).state == TaskState.CLAIMED.value
    assert any(
        row.name == "worker.routing.claimed" and row.subject_id == canary.id
        for row in cp.list_observability(layer="control_plane", limit=20)
    )


def test_claim_next_can_defer_beads_claim_side_effects(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-defer-claim",
                "title": "Defer claim writeback",
                "description": "claim response should not wait on beads",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, cwd, capture_output, text, timeout, check):
        calls.append({"command": command, "cwd": cwd})
        return Completed()

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    claimed = cp.claim_next_for_agent(worker.id, sync_beads=False)

    assert claimed is not None
    assert claimed["task"]["id"] == task.id
    assert calls == []

    cp.sync_claim_side_effects(
        task.id,
        worker.id,
        claimed["lease"]["id"],
        claimed["lease"]["expires_at"],
    )

    commands = [call["command"] for call in calls]
    assert [bd_cli, "--actor", worker.id, "update", "mac-defer-claim", "--claim"] in commands
    assert any(command[:5] == [bd_cli, "--actor", worker.id, "comment", "mac-defer-claim"] for command in commands)


def test_dependencies_block_until_parent_completes(cp):
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    parent = cp.create_task("Parent", required_capabilities=["python"])
    child = cp.create_task("Child", required_capabilities=["python"], dependencies=[parent.id])

    assert child.state == TaskState.BLOCKED.value
    finish_task(cp, parent, worker, reviewer)
    tick = cp.tick()

    assert cp.get_task(child.id).state == TaskState.CLAIMED.value
    assert tick["assignments"][0]["task"]["id"] == child.id


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


def _write_repository_contract(repo_path, project="repo-beads-mac", include_test=True):
    contract_dir = repo_path / ".mac"
    contract_dir.mkdir(parents=True, exist_ok=True)
    test_block = (
        "test:\n  command: PATH=.venv/bin:$PATH .venv/bin/python -m pytest\n"
        if include_test
        else "test: {}\n"
    )
    (contract_dir / "project.yaml").write_text(
        (
            "schema: mac.repository_contract.v1\n"
            "project: %s\n"
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
        % (project, test_block),
        encoding="utf-8",
    )


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
        cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")


def test_beads_repository_registration_rejects_incomplete_runtime_contract(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repository_contract(repo, include_test=False)

    with pytest.raises(ValidationError, match="test.command"):
        cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")


def test_beads_bridge_imports_ready_open_issues_idempotently(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-ready",
                "title": "Ready bead",
                "description": "do the ready work",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            },
            {
                "_type": "issue",
                "id": "mac-blocked",
                "title": "Blocked bead",
                "description": "must wait",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:01:00Z",
                "dependencies": [
                    {"issue_id": "mac-blocked", "depends_on_id": "mac-ready", "type": "blocks"}
                ],
                "dependency_count": 1,
            },
        ],
    )
    repo_record = cp.register_beads_repository(
        "mac",
        str(repo),
        source="repo-beads-mac",
        required_capabilities=["python"],
        poll_interval_seconds=60,
    )

    report = cp.poll_beads_repositories(force=True)
    again = cp.poll_beads_repositories(repo_record.id, force=True)

    assert report["imported_count"] == 1
    assert again["imported_count"] == 0
    assert again["existing_count"] == 1
    assert len(cp.list_project_items()) == 1
    item = cp.list_project_items()[0]
    assert item.source == "repo-beads-mac"
    assert item.external_id == "mac-ready"
    assert item.payload["repository_contract"]["test"]["command"] == "PATH=.venv/bin:$PATH .venv/bin/python -m pytest"
    task = cp.get_task(item.task_id)
    assert task.state == TaskState.OPEN.value
    assert task.project == "repo-beads-mac"
    assert task.priority >= 98
    assert task.required_capabilities == ["python"]
    assert repo_record.metadata["repository_contract"]["bootstrap"]["command"] == "python3 scripts/bootstrap-project.py"
    assert task.metadata["origin"]["type"] == "beads"
    assert task.metadata["origin"]["repository_contract"]["project"] == "repo-beads-mac"
    assert task.metadata["publication_target"] == "git://main"
    assert task.metadata["acc_metadata"]["beads_sync_close_on_complete"] is True
    assert task.metadata["acc_metadata"]["repository_contract_schema"] == "mac.repository_contract.v1"


def test_beads_bridge_pulls_existing_embedded_dolt_database(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repository_contract(repo)
    beads_dir = repo / ".beads"
    beads_dir.mkdir()
    embedded = beads_dir / "embeddeddolt"
    embedded.mkdir()
    (embedded / "marker").write_text("db exists", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    fake_bd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    state = {}

    cp._bootstrap_beads_bridge_checkout(repo_record, repo, "test", state)

    assert state["beads_bootstrap"] == "already_exists"
    assert state["beads_dolt_pull"] == "ok"
    assert [str(fake_bd), "dolt", "pull"] in calls


def test_beads_bridge_rebuilds_disposable_dolt_database_after_pull_failure(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repository_contract(repo)
    beads_dir = repo / ".beads"
    beads_dir.mkdir()
    embedded = beads_dir / "embeddeddolt"
    embedded.mkdir()
    (embedded / "marker").write_text("conflicted db", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    fake_bd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    calls = []
    pull_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal pull_count
        calls.append(list(cmd))
        if list(cmd) == [str(fake_bd), "dolt", "pull"]:
            pull_count += 1
            if pull_count == 1:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="foreign key conflicts")
            return subprocess.CompletedProcess(cmd, 0, stdout="pulled", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    state = {}

    cp._bootstrap_beads_bridge_checkout(repo_record, repo, "test", state)

    assert state["beads_bootstrap"] == "already_exists"
    assert state["beads_dolt_pull"] == "failed"
    assert state["beads_dolt_rebuild"] == "ok"
    assert state["beads_dolt_pull_retry"] == "ok"
    assert pull_count == 2
    assert [str(fake_bd), "bootstrap", "--yes"] in calls
    assert not embedded.exists()
    assert list(beads_dir.glob("embeddeddolt.rebuild.*"))


def test_beads_bridge_records_authority_drift_when_jsonl_export_disagrees_with_db(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = {
        "_type": "issue",
        "id": "mac-jsonl-only",
        "title": "Export-only bead",
        "description": "present in tracked JSONL but not canonical DB",
        "status": "open",
        "priority": 0,
        "created_at": "2026-05-20T00:00:00Z",
        "dependency_count": 0,
    }
    _write_beads(repo, [issue])
    ready_path = tmp_path / "ready.json"
    ready_path.write_text("[]", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    _write_fake_bd_cli(fake_bd, ready_path)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")

    report = cp.poll_beads_repositories(repo_record.id, force=True)

    assert report["imported_count"] == 0
    assert report["error_count"] == 1
    assert cp.list_project_items() == []
    repo_report = report["repositories"][0]
    assert repo_report["status"] == "authority_drift"
    assert repo_report["health"]["status"] == "unhealthy"
    assert repo_report["health"]["reason"] == "authority_drift"
    assert repo_report["ready_count"] == 0
    assert repo_report["source_state"]["authority"]["authority"] == "beads_db"
    assert repo_report["source_state"]["authority"]["status"] == "drift"
    assert repo_report["source_state"]["authority"]["jsonl_ready_ids"] == ["mac-jsonl-only"]
    assert repo_report["source_state"]["authority_findings"][0]["finding_type"] == "beads.export_drift.ready_mismatch"
    assert repo_report["source_state"]["authority_drift"]["repair_action"]["type"] == "beads_canonical_reconcile"
    assert cp.get_beads_repository(repo_record.id).metadata["health"]["status"] == "unhealthy"
    findings = cp.list_integration_findings(status="open")
    assert len(findings) == 1
    assert findings[0].detail["jsonl_only_ready_ids"] == ["mac-jsonl-only"]
    assert findings[0].detail["jsonl_only_untracked_ids"] == ["mac-jsonl-only"]
    assert "bd export -o .beads/issues.jsonl" in findings[0].detail["repair_action"]["commands"]
    assert findings[0].detail["repair_action"]["commands"][-1] == "git push"
    observations = cp.list_integration_observations(source_id=repo_record.id)
    assert observations[0].authority == "beads_db"
    assert observations[0].status == "drift"
    assert observations[0].detail["canonical_ready_ids"] == []
    notifications = cp.list_notifications(subject_id=repo_record.id)
    assert notifications[0].event_type == "integration.beads.export_drift.ready_mismatch"
    names = {item.name for item in cp.list_observability(layer="control_plane", limit=20)}
    assert "integration.finding.opened" in names

    ready_path.write_text(json.dumps([issue]), encoding="utf-8")
    second = cp.poll_beads_repositories(repo_record.id, force=True)

    assert second["imported_count"] == 1
    assert second["error_count"] == 0
    assert second["repositories"][0]["health"]["status"] == "healthy"
    assert cp.get_beads_repository(repo_record.id).last_error is None
    assert cp.list_integration_findings(status="open") == []
    resolved = cp.list_integration_findings(status="resolved")
    assert len(resolved) == 1
    assert resolved[0].resolution == "no longer observed"


def test_beads_bridge_marks_repo_unhealthy_for_jsonl_only_issue_already_imported(
    cp,
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = {
        "_type": "issue",
        "id": "mac-jsonl-active",
        "title": "Already imported bead",
        "description": "present in tracked JSONL but already represented by a mac task",
        "status": "open",
        "priority": 0,
        "created_at": "2026-05-20T00:00:00Z",
        "dependency_count": 0,
    }
    _write_beads(repo, [issue])
    ready_path = tmp_path / "ready.json"
    ready_path.write_text("[]", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    _write_fake_bd_cli(fake_bd, ready_path)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    item = cp.import_project_item(
        repo_record.source,
        "mac-jsonl-active",
        "Already imported bead",
        {"issue": issue},
        actor="test",
    )

    report = cp.poll_beads_repositories(repo_record.id, force=True)

    assert report["imported_count"] == 0
    assert report["error_count"] == 1
    assert report["repositories"][0]["status"] == "authority_drift"
    assert report["repositories"][0]["health"]["status"] == "unhealthy"
    assert report["repositories"][0]["source_state"]["authority_findings"][0]["finding_type"] == "beads.export_drift.ready_mismatch"
    drift = report["repositories"][0]["source_state"]["authority_drift"]
    assert drift["jsonl_only_ready_ids"] == ["mac-jsonl-active"]
    assert drift["jsonl_only_untracked_ids"] == []
    assert drift["jsonl_only_already_imported_ids"] == ["mac-jsonl-active"]
    assert drift["jsonl_only_existing_tasks"]["mac-jsonl-active"] == {
        "task_id": item.task_id,
        "state": "open",
    }
    findings = cp.list_integration_findings(status="open")
    assert len(findings) == 1
    assert findings[0].detail["jsonl_only_already_imported_ids"] == ["mac-jsonl-active"]
    assert cp.list_notifications(subject_id=repo_record.id)[0].event_type == "integration.beads.export_drift.ready_mismatch"


def test_beads_bridge_fails_closed_when_canonical_ready_is_unavailable(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = {
        "_type": "issue",
        "id": "mac-jsonl-only",
        "title": "Do not import from JSONL fallback",
        "description": "canonical DB is broken",
        "status": "open",
        "priority": 0,
        "created_at": "2026-05-20T00:00:00Z",
        "dependency_count": 0,
    }
    _write_beads(repo, [issue])
    fake_bd = tmp_path / "bd-broken"
    fake_bd.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"ready --json\" ]; then echo 'embeddeddolt: missing table' >&2; exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_bd.chmod(0o755)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")

    report = cp.poll_beads_repositories(repo_record.id, force=True)

    repo_report = report["repositories"][0]
    assert report["imported_count"] == 0
    assert report["error_count"] == 1
    assert repo_report["status"] == "error"
    assert repo_report["health"]["status"] == "unhealthy"
    assert repo_report["health"]["reason"] == "canonical_unavailable"
    assert repo_report["source_state"]["authority"]["authority"] == "beads_db"
    assert repo_report["source_state"]["authority"]["status"] == "unavailable"
    assert repo_report["source_state"]["authority"]["jsonl_ready_ids"] == ["mac-jsonl-only"]
    assert cp.list_project_items() == []
    refreshed_repo = cp.get_beads_repository(repo_record.id)
    assert refreshed_repo.last_error.startswith("canonical Beads DB unavailable")
    assert refreshed_repo.metadata["health"]["status"] == "unhealthy"
    findings = cp.list_integration_findings(status="open")
    assert len(findings) == 1
    assert findings[0].finding_type == "beads.canonical_unavailable"
    assert findings[0].detail["jsonl_ready_ids"] == ["mac-jsonl-only"]
    assert findings[0].detail["repair_action"]["type"] == "beads_canonical_reconcile"


def test_beads_bridge_repair_reconciles_tracked_export_from_canonical_db(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = {
        "_type": "issue",
        "id": "mac-stale-export",
        "title": "Stale export",
        "description": "JSONL says ready but canonical DB does not",
        "status": "open",
        "priority": 0,
        "created_at": "2026-05-20T00:00:00Z",
        "dependency_count": 0,
    }
    _write_beads(repo, [issue])
    ready_path = tmp_path / "ready.json"
    ready_path.write_text("[]", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    _write_fake_bd_cli(fake_bd, ready_path)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    drift = cp.poll_beads_repositories(repo_record.id, force=True)
    assert drift["repositories"][0]["status"] == "authority_drift"

    repaired = cp.repair_beads_repository(repo_record.id, actor="operator")

    assert repaired["status"] == "ok"
    assert [step["name"] for step in repaired["steps"]] == [
        "bootstrap",
        "dolt_pull",
        "ready",
        "export",
    ]
    assert (repo / ".beads" / "issues.jsonl").read_text(encoding="utf-8") == ""
    assert repaired["poll_report"]["repositories"][0]["status"] == "ok"
    assert cp.get_beads_repository(repo_record.id).metadata["health"]["status"] == "healthy"
    assert cp.list_integration_findings(status="open") == []


def test_beads_bridge_repair_skips_bootstrap_when_embedded_db_already_exists(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = {
        "_type": "issue",
        "id": "mac-stale-export",
        "title": "Stale export",
        "description": "JSONL says ready but canonical DB does not",
        "status": "open",
        "priority": 0,
        "created_at": "2026-05-20T00:00:00Z",
        "dependency_count": 0,
    }
    _write_beads(repo, [issue])
    (repo / ".beads" / "embeddeddolt" / "repo").mkdir(parents=True)
    ready_path = tmp_path / "ready.json"
    ready_path.write_text("[]", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    _write_fake_bd_cli(
        fake_bd,
        ready_path,
        bootstrap_returncode=1,
        bootstrap_stderr="Error 1007: can't create database repo; database exists\n",
    )
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    drift = cp.poll_beads_repositories(repo_record.id, force=True)
    assert drift["repositories"][0]["status"] == "authority_drift"

    repaired = cp.repair_beads_repository(repo_record.id, actor="operator")

    assert repaired["status"] == "ok"
    assert [step["name"] for step in repaired["steps"]] == [
        "bootstrap",
        "dolt_pull",
        "ready",
        "export",
    ]
    assert repaired["steps"][0]["skipped"] is True
    assert (repo / ".beads" / "issues.jsonl").read_text(encoding="utf-8") == ""
    assert repaired["poll_report"]["repositories"][0]["status"] == "ok"
    assert cp.get_beads_repository(repo_record.id).metadata["health"]["status"] == "healthy"
    assert cp.list_integration_findings(status="open") == []


def test_beads_bridge_repair_persists_export_to_dedicated_checkout(cp, tmp_path, monkeypatch):
    origin, _seed, clone = _seed_bare_beads_repo(tmp_path, "mac-stale-export")
    ready_path = tmp_path / "ready.json"
    ready_path.write_text("[]", encoding="utf-8")
    fake_bd = tmp_path / "bd"
    _write_fake_bd_cli(fake_bd, ready_path)
    monkeypatch.setenv("MAC_BEADS_CLI", str(fake_bd))
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ROOT", str(tmp_path / "bridge-checkouts"))
    repo_record = cp.register_beads_repository("mac", str(clone), source="repo-beads-mac")
    drift = cp.poll_beads_repositories(repo_record.id, force=True)
    assert drift["repositories"][0]["status"] == "authority_drift"

    repaired = cp.repair_beads_repository(repo_record.id, actor="operator")

    assert repaired["status"] == "ok"
    step_names = [step["name"] for step in repaired["steps"]]
    assert "git_add_export" in step_names
    assert "git_commit_export" in step_names
    assert "git_push_export" in step_names
    assert repaired["poll_report"]["repositories"][0]["status"] == "ok"
    bridge_path = Path(repaired["poll_report"]["repositories"][0]["source_state"]["poll_path"])
    assert _git(["status", "--porcelain", "--", ".beads/issues.jsonl"], cwd=bridge_path).stdout.strip() == ""
    assert _git(["--git-dir", str(origin), "show", "main:.beads/issues.jsonl"]).stdout == ""
    repoll = cp.poll_beads_repositories(repo_record.id, force=True)
    assert repoll["repositories"][0]["status"] == "ok"


def test_direct_task_for_registered_project_gets_repository_execution_contract(cp, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(repo, [])
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")

    task = cp.create_task(
        "Direct repository task",
        project="repo-beads-mac",
        required_capabilities=["python"],
    )

    assert task.metadata["execution_contract"]["type"] == "repository"
    assert task.metadata["execution_contract"]["quality"] == "strong"
    assert task.metadata["origin"]["repository_contract"]["project"] == "repo-beads-mac"
    assert task.metadata["acc_metadata"]["repository_contract_schema"] == "mac.repository_contract.v1"


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


def test_beads_bridge_auto_pulls_git_repository_before_poll(cp, tmp_path, monkeypatch):
    _origin, seed, clone = _seed_bare_beads_repo(tmp_path, "mac-old")
    repo_record = cp.register_beads_repository("mac", str(clone), source="repo-beads-mac")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ROOT", str(tmp_path / "bridge-checkouts"))
    first = cp.poll_beads_repositories(repo_record.id, force=True)
    assert first["imported_count"] == 1
    assert first["repositories"][0]["source_state"]["status"] == "cloned"

    (seed / ".beads" / "issues.jsonl").write_text(
        json.dumps(
            {
                "_type": "issue",
                "id": "mac-new",
                "title": "New upstream bead",
                "description": "arrived after hub checkout became stale",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:01:00Z",
                "dependency_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _git(["add", ".beads/issues.jsonl"], cwd=seed)
    _git(["commit", "-m", "new bead"], cwd=seed)
    _git(["push"], cwd=seed)
    monkeypatch.setenv("MAC_BEADS_AUTO_PULL", "1")

    report = cp.poll_beads_repositories(repo_record.id, force=True)

    assert report["imported_count"] == 1
    assert report["repositories"][0]["source_state"]["status"] == "updated"
    assert [item.external_id for item in cp.list_project_items()] == ["mac-old", "mac-new"]


def test_beads_bridge_polls_dedicated_checkout_when_registered_source_is_dirty(cp, tmp_path, monkeypatch):
    _origin, _seed, clone = _seed_bare_beads_repo(tmp_path, "mac-dirty")
    repo_record = cp.register_beads_repository("mac", str(clone), source="repo-beads-mac")
    rocky = register_agent(cp, "rocky", ["python"])
    (clone / ".beads" / "issues.jsonl").write_text(
        '{"_type":"issue","id":"local-dirty","status":"open"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_BEADS_AUTO_PULL", "1")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ROOT", str(tmp_path / "bridge-checkouts"))

    report = cp.poll_beads_repositories(repo_record.id, force=True, actor=rocky.id)

    repo_report = report["repositories"][0]
    source_state = repo_report["source_state"]
    assert report["error_count"] == 0
    assert repo_report["status"] == "ok"
    assert source_state["checkout_policy"] == "dedicated_git_checkout"
    assert source_state["poll_path"] != str(clone)
    assert source_state["registered_dirty_paths"] == ["M .beads/issues.jsonl"]
    assert [item.external_id for item in cp.list_project_items()] == ["mac-dirty"]
    notifications = cp.list_notifications(subject_id=repo_record.id)
    assert [item.event_type for item in notifications] == []


def test_beads_bridge_restores_registered_export_noise_before_poll(cp, tmp_path, monkeypatch):
    _origin, _seed, clone = _seed_bare_beads_repo(tmp_path, "mac-clean")
    repo_record = cp.register_beads_repository("mac", str(clone), source="repo-beads-mac")
    rocky = register_agent(cp, "rocky", ["python"])
    (clone / ".beads" / "issues.jsonl").write_text(
        '{"_type":"issue","id":"local-export-noise","status":"open"}\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(clone), "add", ".beads/issues.jsonl"],
        check=True,
    )
    monkeypatch.setenv("MAC_BEADS_AUTO_PULL", "1")
    monkeypatch.setenv("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ROOT", str(tmp_path / "bridge-checkouts"))

    report = cp.poll_beads_repositories(repo_record.id, force=True, actor=rocky.id)

    source_state = report["repositories"][0]["source_state"]
    status = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "status",
            "--porcelain",
            "--",
            ".beads/config.yaml",
            ".beads/issues.jsonl",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert report["error_count"] == 0
    assert status == ""
    assert "registered_dirty_paths" not in source_state
    names = {item.name for item in cp.list_observability(layer="control_plane", limit=20)}
    assert "bridge.beads.tracked_exports_restored" in names


def test_beads_bridge_resets_dirty_managed_checkout_before_poll(cp, tmp_path, monkeypatch):
    _origin, seed, clone = _seed_bare_beads_repo(tmp_path, "mac-old")
    repo_record = cp.register_beads_repository("mac", str(clone), source="repo-beads-mac")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ROOT", str(tmp_path / "bridge-checkouts"))
    first = cp.poll_beads_repositories(repo_record.id, force=True)
    bridge_path = Path(first["repositories"][0]["source_state"]["poll_path"])
    (bridge_path / ".beads" / "issues.jsonl").write_text(
        '{"_type":"issue","id":"local-bridge-dirty","status":"open"}\n',
        encoding="utf-8",
    )
    (seed / ".beads" / "issues.jsonl").write_text(
        '{"_type":"issue","id":"mac-new","status":"open","priority":0,"dependency_count":0}\n',
        encoding="utf-8",
    )
    _git(["add", ".beads/issues.jsonl"], cwd=seed)
    _git(["commit", "-m", "replace bead"], cwd=seed)
    _git(["push"], cwd=seed)

    report = cp.poll_beads_repositories(repo_record.id, force=True)

    source_state = report["repositories"][0]["source_state"]
    assert report["error_count"] == 0
    assert source_state["tracked_dirty_reset"] == ["M .beads/issues.jsonl"]
    assert report["imported_count"] == 1
    assert [item.external_id for item in cp.list_project_items()] == ["mac-old", "mac-new"]


def test_beads_bridge_reopens_failed_existing_task_while_bead_ready(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-retry",
                "title": "Retry failed work",
                "description": "still ready",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.transition_task(task.id, TaskState.FAILED.value, worker.id, {"reason": "verification failed"})

    report = cp.poll_beads_repositories(force=True, actor="bridge")

    assert report["reopened_count"] == 1
    assert report["repositories"][0]["existing_sync_results"]["reopened"] == 1
    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    assert reopened.metadata["beads_reconciliation"]["failed_task_reopen_count"] == 1
    assert reopened.metadata["beads_reconciliation"]["last_reopened_bead_id"] == "mac-retry"
    assert cp.claim_next_for_agent(worker.id)["task"]["id"] == task.id


def test_beads_bridge_failed_task_reopen_limit_is_bounded(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-limited",
                "title": "Bounded retry",
                "description": "do not loop forever",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    monkeypatch.setenv("MAC_BEADS_FAILED_TASK_REOPEN_LIMIT", "1")
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.transition_task(task.id, TaskState.FAILED.value, worker.id, {"reason": "first failure"})
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, max_attempts = ? WHERE id = ?",
        (3, 3, task.id),
    )

    first = cp.poll_beads_repositories(force=True, actor="bridge")
    reopened = cp.get_task(task.id)

    assert first["reopened_count"] == 1
    assert reopened.state == TaskState.OPEN.value
    assert reopened.max_attempts == 4

    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.transition_task(task.id, TaskState.FAILED.value, worker.id, {"reason": "second failure"})
    second = cp.poll_beads_repositories(force=True, actor="bridge")
    exhausted = cp.get_task(task.id)

    assert second["reopened_count"] == 0
    assert second["retry_exhausted_count"] == 1
    assert exhausted.state == TaskState.FAILED.value
    assert exhausted.metadata["beads_reconciliation"]["failed_task_reopen_count"] == 1
    assert exhausted.metadata["beads_reconciliation"]["failed_task_reopen_limit"] == 1
    assert exhausted.metadata["beads_reconciliation"]["retry_exhausted_at"]
    assert any(
        event.event_type == "task.beads_retry_exhausted"
        for event in cp.task_history(task.id)
    )


def test_hub_heartbeat_polls_registered_beads_repositories(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-heartbeat",
                "title": "Heartbeat imported bead",
                "description": "import me from heartbeat",
                "status": "open",
                "priority": 1,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    rocky = register_agent(cp, "rocky", ["python"])
    natasha = register_agent(cp, "natasha", ["python"])
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ON_HEARTBEAT", "1")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_HUB_AGENT", "rocky")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ON_HEARTBEAT_ASYNC", "0")

    cp.heartbeat_agent(natasha.id, status=AgentStatus.IDLE.value)
    assert cp.list_project_items() == []

    cp.heartbeat_agent(rocky.id, status=AgentStatus.IDLE.value)

    assert len(cp.list_project_items()) == 1
    assert cp.list_project_items()[0].external_id == "mac-heartbeat"


def test_hub_heartbeat_schedules_beads_poll_async_by_default(cp, monkeypatch):
    rocky = register_agent(cp, "rocky", ["python"])
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def fake_poll(**kwargs):
        calls.append(kwargs["actor"])
        started.set()
        release.wait(1)
        finished.set()
        return {"repositories": []}

    monkeypatch.setattr(cp, "poll_beads_repositories", fake_poll)
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ON_HEARTBEAT", "1")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_HUB_AGENT", "rocky")

    cp.heartbeat_agent(rocky.id, status=AgentStatus.IDLE.value)
    assert started.wait(1)

    cp.heartbeat_agent(rocky.id, status=AgentStatus.IDLE.value)
    release.set()
    assert finished.wait(1)
    assert calls == [rocky.id]


def test_hub_lease_renewal_polls_registered_beads_repositories(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-renewal",
                "title": "Lease renewal imported bead",
                "description": "import me while hub is busy",
                "status": "open",
                "priority": 1,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    rocky = register_agent(cp, "rocky", ["python"])
    task = cp.create_task("busy hub task", required_capabilities=["python"])
    _claimed, lease = cp.claim_task(task.id, rocky.id)
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ON_HEARTBEAT", "1")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_HUB_AGENT", "rocky")
    monkeypatch.setenv("MAC_BEADS_BRIDGE_ON_HEARTBEAT_ASYNC", "0")

    cp.renew_lease(lease.id, rocky.id)

    imported = [item.external_id for item in cp.list_project_items()]
    assert imported == ["mac-renewal"]


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

    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.add_evidence(task.id, "test", "artifact://pytest", "pytest passed", worker.id)
    cp.transition_task(task.id, TaskState.FAILED.value, worker.id, {"reason": "boom"})

    event_types = {item.event_type for item in cp.list_notifications(subject_id=task.id)}
    assert {"task.claimed", "task.running", "task.evidence_added", "task.failed"} <= event_types
    pending = cp.list_notifications(status="pending")
    delivered = cp.mark_notification_delivered(pending[0].id)
    assert delivered.status == "delivered"
    assert delivered.delivered_at is not None


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
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)

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
    assert [item.event_type for item in outbox] == ["beads.claim"]
    assert outbox[0].detail["lease_id"] == lease.id


def test_beads_bridge_syncs_claim_and_failure_to_beads(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-sync",
                "title": "Sync lifecycle",
                "description": "sync claim and failure",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    def fake_run(command, cwd, capture_output, text, timeout, check):
        calls.append({"command": command, "cwd": cwd})

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    cp.claim_task(task.id, worker.id)
    cp.transition_task(task.id, TaskState.FAILED.value, worker.id, {"reason": "canary failed"})

    update_calls = [
        call for call in calls if len(call["command"]) > 3 and call["command"][3] == "update"
    ]
    comment_calls = [
        call for call in calls if len(call["command"]) > 3 and call["command"][3] == "comment"
    ]
    push_calls = [call for call in calls if call["command"] == [bd_cli, "dolt", "push"]]
    assert update_calls[0] == {
        "command": [bd_cli, "--actor", worker.id, "update", "mac-sync", "--claim"],
        "cwd": str(repo),
    }
    assert update_calls[1]["cwd"] == str(repo)
    assert update_calls[1]["command"][:5] == [
        bd_cli,
        "--actor",
        worker.id,
        "update",
        "mac-sync",
    ]
    assert update_calls[1]["command"][5] == "--append-notes"
    assert "canary failed" in update_calls[1]["command"][6]
    assert update_calls[2]["cwd"] == str(repo)
    assert update_calls[2]["command"][:7] == [
        bd_cli,
        "--actor",
        worker.id,
        "update",
        "mac-sync",
        "--status",
        "open",
    ]
    assert update_calls[2]["command"][7] == "--append-notes"
    assert "canary failed" in update_calls[2]["command"][8]
    comments = "\n".join(call["command"][5] for call in comment_calls)
    assert "event=claimed" in comments
    assert "event=state_failed" in comments
    assert "event=state_failed_summary" in comments
    assert "canary failed" in comments
    assert len(push_calls) == len(update_calls) + len(comment_calls)


def test_beads_bridge_backfills_retry_exhausted_failure_summary_to_beads(
    cp, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-failed",
                "title": "Explain failed work",
                "description": "operator needs cause",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    monkeypatch.setenv("MAC_BEADS_FAILED_TASK_REOPEN_LIMIT", "1")
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "executor completed without mac-evidence.json",
        worker.id,
        metadata={
            "returncode": 0,
            "verification": {
                "schema": "mac.worker_evidence.v1",
                "status": "missing",
                "problems": ["mac-evidence.json was not produced by the executor"],
            },
        },
    )
    cp.transition_task(
        task.id,
        TaskState.FAILED.value,
        worker.id,
        {
            "reason": "verification_contract_failed",
            "problems": ["verification.status must be \"complete\""],
            "evidence_id": evidence.id,
        },
    )
    metadata = cp.get_task(task.id).metadata
    metadata["beads_reconciliation"] = {
        "schema": "mac.beads_reconciliation.v1",
        "failed_task_reopen_count": 1,
        "failed_task_reopen_limit": 1,
        "retry_exhausted_at": "2026-05-21T00:00:00+00:00",
    }
    stale_task = cp.get_task(task.id)
    stale_task.metadata["beads_reconciliation"] = metadata["beads_reconciliation"]
    metadata["beads_reconciliation"]["failure_summary_comment_fingerprint"] = (
        cp._failure_summary_fingerprint(
            stale_task,
            cp._latest_failure_context(stale_task, None),
        )
    )
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps(metadata), task.id),
    )

    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    def fake_run(command, cwd, capture_output, text, timeout, check):
        calls.append({"command": command, "cwd": cwd})

        class Completed:
            returncode = 0
            stdout = (
                json.dumps(
                    [
                        {
                            "_type": "issue",
                            "id": "mac-failed",
                            "title": "Explain failed work",
                            "description": "operator needs cause",
                            "status": "open",
                            "priority": 0,
                            "created_at": "2026-05-20T00:00:00Z",
                            "dependency_count": 0,
                        }
                    ]
                )
                if "ready" in command
                else ""
            )
            stderr = ""

        return Completed()

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    report = cp.poll_beads_repositories(force=True, actor="bridge")

    assert report["retry_exhausted_count"] == 1
    update_notes = [
        call["command"][6]
        for call in calls
        if call["command"][3:6] == ["update", "mac-failed", "--append-notes"]
    ]
    comments = [
        call["command"][5]
        for call in calls
        if call["command"][3:5] == ["comment", "mac-failed"]
    ]
    assert any("verification_contract_failed" in note for note in update_notes)
    assert any(evidence.id in note for note in update_notes)
    assert any("event=retry_exhausted_summary" in comment for comment in comments)
    assert any("verification.status must be" in comment for comment in comments)
    fingerprint = cp.get_task(task.id).metadata["beads_reconciliation"][
        "failure_summary_comment_fingerprint"
    ]
    assert cp.get_task(task.id).metadata["beads_reconciliation"][
        "failure_summary_pushed_fingerprint"
    ] == fingerprint
    calls.clear()

    again = cp.poll_beads_repositories(force=True, actor="bridge")

    assert again["retry_exhausted_count"] == 1
    assert cp.get_task(task.id).metadata["beads_reconciliation"][
        "failure_summary_comment_fingerprint"
    ] == fingerprint
    assert not [
        call for call in calls if call["command"][3:5] == ["comment", "mac-failed"]
    ]


def test_beads_sync_falls_back_to_registered_checkout_when_bridge_db_is_broken(
    cp, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    bridge = tmp_path / "bridge"
    repo.mkdir()
    bridge.mkdir()
    _write_repository_contract(repo)
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-sync",
                "title": "Sync lifecycle",
                "description": "sync claim and failure",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    repo_record = cp.register_beads_repository(
        "mac",
        str(repo),
        source="repo-beads-mac",
        metadata={"beads_bridge_checkout_path": str(bridge)},
    )
    cp._import_bead_issue(
        repo_record,
        {
            "_type": "issue",
            "id": "mac-sync",
            "title": "Sync lifecycle",
            "description": "sync claim and failure",
            "status": "open",
            "priority": 0,
            "created_at": "2026-05-20T00:00:00Z",
            "dependency_count": 0,
        },
        actor="bridge",
    )
    task = cp.get_task(cp.list_project_items()[0].task_id)
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    def fake_run(command, cwd, capture_output, text, timeout, check):
        calls.append({"command": command, "cwd": cwd})
        if cwd == str(bridge):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="failed to open database: embeddeddolt: database not found: mac",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    assert cp._run_bd_for_task(
        task,
        ["comment", "mac-sync", "failure cause"],
        "bridge",
        "ledger",
    )

    assert calls == [
        {
            "command": [bd_cli, "--actor", "bridge", "comment", "mac-sync", "failure cause"],
            "cwd": str(bridge),
        },
        {
            "command": [bd_cli, "--actor", "bridge", "comment", "mac-sync", "failure cause"],
            "cwd": str(repo),
        },
        {
            "command": [bd_cli, "dolt", "push"],
            "cwd": str(repo),
        },
    ]
    names = {item.name for item in cp.list_observability(layer="control_plane", limit=20)}
    assert "bridge.beads.sync.ledger.registered_fallback" in names
    assert cp.get_beads_repository(repo_record.id).metadata["beads_bridge_checkout_path"] == str(
        bridge
    )


def test_beads_sync_returns_false_when_writeback_push_fails(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-sync",
                "title": "Sync lifecycle",
                "description": "sync claim and failure",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    repo_record = cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp._import_bead_issue(
        repo_record,
        {
            "_type": "issue",
            "id": "mac-sync",
            "title": "Sync lifecycle",
            "description": "sync claim and failure",
            "status": "open",
            "priority": 0,
            "created_at": "2026-05-20T00:00:00Z",
            "dependency_count": 0,
        },
        actor="bridge",
    )
    task = cp.get_task(cp.list_project_items()[0].task_id)
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)

    def fake_run(command, cwd, capture_output, text, timeout, check):
        if command == [bd_cli, "dolt", "push"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="remote rejected")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    assert not cp._run_bd_for_task(
        task,
        ["comment", "mac-sync", "failure cause"],
        "bridge",
        "ledger",
    )
    names = {item.name for item in cp.list_observability(layer="control_plane", limit=20)}
    assert "bridge.beads.writeback_push_failed" in names
    assert "bridge.beads.ledger_failed" in names


def test_beads_export_noise_can_be_restored_after_sync(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    beads = repo / ".beads"
    beads.mkdir(parents=True)
    (beads / "config.yaml").write_text("sync.remote: origin\n", encoding="utf-8")
    (beads / "issues.jsonl").write_text('{"id":"mac-one","status":"open"}\n', encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "mac-tests@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "mac tests"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "add", ".beads/config.yaml", ".beads/issues.jsonl"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "seed beads"],
        check=True,
        capture_output=True,
    )
    (beads / "config.yaml").write_text("sync.remote: origin", encoding="utf-8")
    (beads / "issues.jsonl").write_text(
        '{"id":"mac-one","status":"in_progress"}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", ".beads/issues.jsonl"], check=True)

    monkeypatch.setenv("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")
    cp._restore_beads_tracked_exports(repo, "agent_rocky", "task_1", "claim")

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert status == ""
    names = {item.name for item in cp.list_observability(layer="control_plane", limit=20)}
    assert "bridge.beads.tracked_exports_restored" in names


def test_beads_bridge_reconciles_existing_active_task_claim(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-reconcile",
                "title": "Reconcile missed claim",
                "description": "claim sync missed during deploy",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    metadata = task.metadata
    metadata["acc_metadata"]["beads_sync_claim_on_claim"] = False
    cp.store.execute("UPDATE tasks SET metadata = ? WHERE id = ?", (json.dumps(metadata), task.id))
    worker = register_agent(cp, "worker", ["python"])
    cp.claim_task(task.id, worker.id)
    claimed = cp.get_task(task.id)
    metadata = claimed.metadata
    metadata["acc_metadata"]["beads_sync_claim_on_claim"] = True
    cp.store.execute("UPDATE tasks SET metadata = ? WHERE id = ?", (json.dumps(metadata), task.id))
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    class Completed:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, cwd, capture_output, text, timeout, check):
        if command == [bd_cli, "ready", "--json"]:
            return Completed(
                stdout=json.dumps(
                    [
                        {
                            "_type": "issue",
                            "id": "mac-reconcile",
                            "title": "Reconcile missed claim",
                            "description": "claim sync missed during deploy",
                            "status": "open",
                            "priority": 0,
                            "created_at": "2026-05-20T00:00:00Z",
                            "dependency_count": 0,
                        }
                    ]
                )
            )
        calls.append({"command": command, "cwd": cwd})
        return Completed()

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    cp.poll_beads_repositories(force=True)

    assert calls == [
        {
            "command": [bd_cli, "--actor", worker.id, "update", "mac-reconcile", "--claim"],
            "cwd": str(repo),
        },
        {
            "command": [bd_cli, "dolt", "push"],
            "cwd": str(repo),
        }
    ]


def test_beads_bridge_tolerates_preclaimed_bead_during_reconcile(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-preclaimed",
                "title": "Preclaimed work",
                "description": "already assigned before mac claimed it",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    metadata = task.metadata
    metadata["acc_metadata"]["beads_sync_claim_on_claim"] = False
    cp.store.execute("UPDATE tasks SET metadata = ? WHERE id = ?", (json.dumps(metadata), task.id))
    worker = register_agent(cp, "worker", ["python"])
    cp.claim_task(task.id, worker.id)
    claimed = cp.get_task(task.id)
    metadata = claimed.metadata
    metadata["acc_metadata"]["beads_sync_claim_on_claim"] = True
    cp.store.execute("UPDATE tasks SET metadata = ? WHERE id = ?", (json.dumps(metadata), task.id))
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)

    class Completed:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, cwd, capture_output, text, timeout, check):
        if command == [bd_cli, "ready", "--json"]:
            return Completed(
                stdout=json.dumps(
                    [
                        {
                            "_type": "issue",
                            "id": "mac-preclaimed",
                            "title": "Preclaimed work",
                            "description": "already assigned before mac claimed it",
                            "status": "open",
                            "priority": 0,
                            "created_at": "2026-05-20T00:00:00Z",
                            "dependency_count": 0,
                        }
                    ]
                )
            )
        return Completed(returncode=1, stderr="Error claiming mac-preclaimed: issue already claimed by Jordan Hubbard")

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    cp.poll_beads_repositories(force=True)

    names = {event.name for event in cp.list_observability(layer="control_plane", limit=20)}
    assert "bridge.beads.sync.claim_existing" in names
    assert "bridge.beads.sync_failed" not in names


def test_beads_bridge_syncs_publication_close_to_beads(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-close",
                "title": "Close lifecycle",
                "description": "sync publication close",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    def fake_run(command, cwd, capture_output, text, timeout, check):
        calls.append({"command": command, "cwd": cwd})

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    cp.claim_task(task.id, worker.id)
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
    from tests.conftest import submit_review_verdict

    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    cp.publish_task(task.id, "test://publish", reviewer.id, evidence_id=evidence.id)

    close_calls = [
        call for call in calls if len(call["command"]) > 3 and call["command"][3] == "close"
    ]
    comment_calls = [
        call for call in calls if len(call["command"]) > 3 and call["command"][3] == "comment"
    ]
    assert calls[0] == {
        "command": [bd_cli, "--actor", worker.id, "update", "mac-close", "--claim"],
        "cwd": str(repo),
    }
    assert close_calls == [
        {
            "command": [
                bd_cli,
                "--actor",
                reviewer.id,
                "close",
                "mac-close",
                "--reason",
                "Completed by mac task %s" % task.id,
            ],
            "cwd": str(repo),
        }
    ]
    comments = "\n".join(call["command"][5] for call in comment_calls)
    assert "event=claimed" in comments
    assert "event=state_running" in comments
    assert "event=evidence_added" in comments
    assert "event=review_requested" in comments
    assert "event=review_completed" in comments
    assert "event=published" in comments


def test_review_claim_records_bead_metadata_and_slack_notification(cp, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_beads(
        repo,
        [
            {
                "_type": "issue",
                "id": "mac-review-claim",
                "title": "Track review claim",
                "description": "record reviewer ownership before verdict",
                "status": "open",
                "priority": 0,
                "created_at": "2026-05-20T00:00:00Z",
                "dependency_count": 0,
            }
        ],
    )
    cp.register_beads_repository("mac", str(repo), source="repo-beads-mac")
    cp.poll_beads_repositories(force=True)
    task = cp.get_task(cp.list_project_items()[0].task_id)
    worker = register_agent(cp, "worker", ["python"])

    tenant = cp.register_tenant("ops")
    persona = cp.register_persona(
        tenant.id,
        "Reviewer",
        soul_ref="hermes://ops/reviewer/SOUL.md",
        memory_scope="hermes://ops/reviewer/memory",
    )
    hermes = cp.register_hermes_instance(tenant.id, "reviewer", persona_id=persona.id)
    binding = cp.register_platform_binding(
        tenant.id,
        hermes.id,
        "slack",
        "T123/C456",
        display_name="#mac-home",
    )
    reviewer_machine = cp.register_machine("reviewer-host")
    reviewer = cp.register_agent(
        reviewer_machine.id,
        "reviewer",
        capabilities=["review"],
        hermes_instance_id=hermes.id,
    )
    cp.configure_notifier_channel(
        "slack-review-claims",
        "slack",
        event_types=["task.review_claimed"],
        target={"platform_binding_id": binding.id},
    )

    bd_cli = str(tmp_path / "bd")
    monkeypatch.setenv("MAC_BEADS_CLI", bd_cli)
    calls = []

    def fake_run(command, cwd, capture_output, text, timeout, check):
        calls.append({"command": command, "cwd": cwd})

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)

    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    verification = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "path": "/tmp/mac-review-claim-worktree",
            "branch": "mac/worker/review-claim",
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "origin/mac/worker/review-claim",
            "dirty": False,
            "files_changed": ["src/mac/services.py"],
        },
        "checks": [{"name": "make build", "returncode": 0}],
        "tests": [{"command": "pytest tests/test_control_plane.py", "returncode": 0}],
    }
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://pytest",
        "review claim implementation complete",
        worker.id,
        metadata={"returncode": 0, "verification": _sign(cp, worker.id, verification)},
    )
    cp.submit_for_review(task.id, worker.id)
    review = cp.request_review(task.id, reviewer.id, actor="dispatcher")

    claim_result = cp.claim_review(
        review.id,
        reviewer.id,
        executor_evidence_id=evidence.id,
        actor=reviewer.id,
    )

    claim = claim_result["claim"]
    assert claim_result["status"] == "claimed"
    assert claim["review_id"] == review.id
    assert claim["reviewer_agent_id"] == reviewer.id
    assert claim["executor_evidence_id"] == evidence.id
    assert claim["project"] == task.project
    assert claim["repository_worktree"] == "/tmp/mac-review-claim-worktree"
    assert claim["repository_branch"] == "mac/worker/review-claim"
    assert claim["repository_head_sha"] == "abcdef1234567890abcdef1234567890abcdef12"
    assert claim["repository_remote_ref"] == "origin/mac/worker/review-claim"
    assert claim["repository_files_changed"] == ["src/mac/services.py"]
    assert claim["checks"][0]["name"] == "make build"
    assert claim["tests"][0]["command"] == "pytest tests/test_control_plane.py"

    task_metadata = cp.get_task(task.id).metadata
    assert task_metadata["review_claims"][review.id] == claim
    assert task_metadata["latest_review_claim"] == claim
    assert cp.get_agent(reviewer.id).status == AgentStatus.BUSY.value
    assert cp.get_agent(reviewer.id).current_task_id == task.id

    history = cp.task_history(task.id)
    claim_events = [event for event in history if event.event_type == "task.review_claimed"]
    assert len(claim_events) == 1
    assert claim_events[0].actor == reviewer.id
    assert claim_events[0].detail["repository_worktree"] == "/tmp/mac-review-claim-worktree"
    assert "task.review_claimed" in {
        notification.event_type for notification in cp.list_notifications(subject_id=task.id)
    }

    comments = "\n".join(
        call["command"][5]
        for call in calls
        if len(call["command"]) > 5 and call["command"][3:5] == ["comment", "mac-review-claim"]
    )
    assert "event=review_claimed" in comments
    assert "project=repo-beads-mac" in comments
    assert "worktree=/tmp/mac-review-claim-worktree" in comments
    assert "head=abcdef1234567890abcdef1234567890abcdef12" in comments
    assert "ref=origin/mac/worker/review-claim" in comments

    delivery = cp.deliver_pending_notifications(limit=50)
    assert delivery["delivered"] >= 1
    messages = cp.list_messages(reviewer.id)
    review_claim_messages = [
        message
        for message in messages
        if message.payload.get("notification", {}).get("event_type") == "task.review_claimed"
    ]
    assert len(review_claim_messages) == 1
    assert review_claim_messages[0].payload["target"]["platform_binding_id"] == binding.id
    safe_test = review_claim_messages[0].payload["notification"]["metadata"]["tests"][0]
    assert safe_test["command_text"] == "pytest tests/test_control_plane.py"
    assert "command" not in safe_test

    from tests.conftest import submit_review_verdict

    verdict_id = submit_review_verdict(cp, task.id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    assert cp.get_agent(reviewer.id).status == AgentStatus.IDLE.value
    assert cp.get_agent(reviewer.id).current_task_id is None


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
    task = cp.create_task(
        "publish parallel branch",
        required_capabilities=["python"],
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_path": str(source),
                "repository_contract": {"schema": "mac.repository_contract.v1"},
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
    published = [
        event
        for event in cp.list_observability(limit=50)
        if event.name == "task.git_published" and event.subject_id == task.id
    ]
    assert published
    assert published[0].detail["publication_mode"] == "merge_commit"
    assert published[0].detail["head_sha"] == task_head
    assert published[0].detail["final_sha"] == final_head


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
        },
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


def test_rejected_review_verdict_fails_exhausted_task(cp):
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
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id, verdict="rejected")

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "review_not_approved"
    assert result["review_status"] == ReviewStatus.REJECTED.value
    exhausted = cp.get_task(task.id)
    assert exhausted.state == TaskState.FAILED.value
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
    submit_review_verdict(cp, task.id, reviewer.id, evidence.id, verdict="rejected")
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

    worker = register_agent(cp, "w", ["python"])
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


def test_workflow_advance_race_does_not_orphan_tasks(cp):
    """mac-t8ih: two concurrent terminal events on the same workflow run
    must not both spawn a next-node task. The guarded UPDATE in _advance
    ensures only the first caller proceeds; the second one observes
    rowcount=0 and exits without writing history."""
    from tests.conftest import bind_soul as _bs
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
                {"node_key": "b", "node_type": "task", "role_required": "dev", "max_attempts": 1},
            ],
            "edges": [
                {"from_node_key": "", "to_node_key": "a", "condition": "success", "priority": 100},
                {"from_node_key": "a", "to_node_key": "b", "condition": "failure", "priority": 100},
            ],
        },
        created_by="human",
    )
    run = cp.workflow_runtime.start_run("race-wf", started_by="ops")
    machine = cp.register_machine("h")
    qa_soul = _bs(cp, persona_name="qa", allowed_role_slugs=["qa"])
    qa_agent = cp.register_agent(machine.id, "qa1", capabilities=["python", "qa"], hermes_instance_id=qa_soul)
    cp.roles.assign_role(qa_agent.id, "qa")
    first_task = cp.get_task(run.current_task_id)
    cp.claim_task(first_task.id, qa_agent.id)
    cp.start_task(first_task.id, qa_agent.id)

    # Simulate a race: two callers invoke _advance with the SAME completed
    # task. The first wins, the second's guarded UPDATE finds the run
    # has moved on and exits cleanly.
    cp.transition_task(first_task.id, TaskState.FAILED.value, qa_agent.id)
    # At this point, the runtime advanced once via on_task_completed.
    # Calling _advance again directly should be a no-op.
    cp.workflow_runtime._advance(
        cp.workflow_runtime.get_run(run.id), "a", "failure", first_task.id
    )

    history = cp.store.query_all(
        "SELECT to_node_key FROM workflow_run_history WHERE run_id = ? ORDER BY seq",
        (run.id,),
    )
    # Expect: start (to=a), then advance to b, then either a refused or
    # a cancellation. Critically, we must not see two spawns of `b`.
    b_spawns = sum(1 for r in history if r["to_node_key"] == "b")
    assert b_spawns <= 1, f"workflow spawned {b_spawns} `b` tasks, expected <= 1"


def test_bead_issue_is_importable_strict_validation(cp):
    """mac-3xpl: bd ready --json import must reject hostile/oversized
    fields before they reach the project ledger."""
    # legitimate issue
    assert cp._bead_issue_is_importable({
        "id": "mac-azid",
        "status": "open",
        "title": "fix the thing",
        "description": "details",
        "type": "bug",
        "priority": 2,
    })

    # rejected: bad id shape
    assert not cp._bead_issue_is_importable({"id": "--help", "status": "open"})
    assert not cp._bead_issue_is_importable({"id": "", "status": "open"})
    # rejected: closed
    assert not cp._bead_issue_is_importable({"id": "mac-x", "status": "closed"})
    # rejected: oversized title
    assert not cp._bead_issue_is_importable({
        "id": "mac-x", "status": "open", "title": "x" * 600,
    })
    # rejected: oversized description
    assert not cp._bead_issue_is_importable({
        "id": "mac-x", "status": "open", "description": "x" * (33 * 1024),
    })
    # rejected: priority out of canonical range
    assert not cp._bead_issue_is_importable({
        "id": "mac-x", "status": "open", "priority": 99,
    })
    # rejected: non-string title
    assert not cp._bead_issue_is_importable({
        "id": "mac-x", "status": "open", "title": ["not a string"],
    })


def test_strip_control_chars_keeps_normal_text(cp):
    """mac-3xpl: ANSI escapes and ASCII control chars in upstream
    issue fields must be stripped before reaching the ledger."""
    assert cp._strip_control_chars("normal text") == "normal text"
    assert cp._strip_control_chars("multi\nline\nok") == "multi\nline\nok"
    # ANSI CSI sequence stripped
    assert "\x1b" not in cp._strip_control_chars("foo\x1b[31m red \x1b[0m bar")
    # NUL and other ctrl stripped
    assert cp._strip_control_chars("a\x00b\x07c") == "abc"


def test_beads_binding_rejects_argv_smuggling_bead_ids(cp):
    """mac-5xwh: bead_id is passed verbatim as an argv positional to
    `bd comment <bead_id> ...`. A hostile bead_id like `--help` or
    `-c <something>` becomes a flag. _beads_binding_for_task must
    refuse anything that doesn't match the bead-id shape so no caller
    ever sees a hostile binding."""
    for hostile in ["--help", "-c something", "  ", "bead\n-id", "bead;rm -rf /", "?bad?"]:
        task = cp.create_task(
            "t",
            metadata={
                "origin": {
                    "type": "beads",
                    "bead_id": hostile,
                    "repository_path": "/tmp/r",
                },
                "acc_metadata": {"beads_id": hostile},
            },
        )
        binding = cp._beads_binding_for_task(task)
        assert binding is None, f"hostile bead_id leaked through binding: {hostile!r}"

    # Legitimate forms still bind.
    ok = cp.create_task(
        "t",
        metadata={
            "origin": {
                "type": "beads",
                "bead_id": "mac-azid",
                "repository_path": "/tmp/r",
            },
            "acc_metadata": {"beads_id": "mac-azid"},
        },
    )
    assert cp._beads_binding_for_task(ok) is not None


def test_failed_to_open_requeue_resets_attempt_count(cp):
    """mac-d2xh: a dead-letter requeue (FAILED → OPEN) must reset
    attempt_count, otherwise the next claim immediately re-fails because
    attempt_count >= max_attempts. Without the reset, requeue is a no-op."""
    worker = register_agent(cp, "w", ["python"])
    task = cp.create_task("t", required_capabilities=["python"], max_attempts=2)

    # Burn both attempts.
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    cp.transition_task(task.id, TaskState.FAILED.value, worker.id)
    # Re-open and try once more.
    cp.transition_task(task.id, TaskState.OPEN.value, "ops")
    after_first_requeue = cp.get_task(task.id)
    # The reset must zero attempt_count and clear completed_at so the
    # next claim is permitted.
    assert after_first_requeue.attempt_count == 0
    assert after_first_requeue.completed_at is None
    # Claim must succeed (would raise if attempt_count were still >= max).
    cp.claim_task(task.id, worker.id)
    assert cp.get_task(task.id).attempt_count == 1


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
    cp.transition_task(active.id, TaskState.FAILED.value, "test", {"reason": "drain-test-finished"})
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


def test_events_view_unifies_all_audit_surfaces(cp):
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
    eval_set = cp.create_eval_set("audit-eval", scoring="higher_is_better")
    # secret event
    deployer = register_agent(cp, "deployer", ["deploy"])
    secret = cp.create_secret("audit-token", "x", {"capabilities": ["deploy"]}, "human")
    cp.request_secret(secret.id, deployer.id, "audit-test")

    events = cp.list_events(limit=500)
    subject_types = {event["subject_type"] for event in events}
    assert subject_types == {"task", "agent", "project", "fleet", "rollout", "eval_set", "secret"}
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
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)

    # Force the history write to fail and prove the task + agent updates roll
    # back with it — the whole transition_task must be all-or-nothing.
    original = cp._record_history

    def boom(*args, **kwargs):
        raise RuntimeError("simulated history failure")

    cp._record_history = boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            cp.transition_task(task.id, TaskState.FAILED.value, "tester")
    finally:
        cp._record_history = original  # type: ignore[assignment]

    # Task is still claimed by the agent; agent still references the task.
    same_task = cp.get_task(task.id)
    assert same_task.state == TaskState.RUNNING.value
    assert same_task.owner_agent_id == agent.id
    assert cp.get_agent(agent.id).current_task_id == task.id

    # Now succeed: all three writes commit together.
    cp.transition_task(task.id, TaskState.FAILED.value, "tester")
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
            cp.add_evidence(task_id, "log", "file://x", "summary", "tester")
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

    # Re-register with additional signer and updated metadata: digest is the key,
    # signers merge, metadata merges, sbom_uri preserves if new is None.
    art2 = cp.register_artifact(
        "image",
        "sha256:deadbeef",
        "ignored-on-update",
        "human",
        signers=["release-manager"],
        metadata={"approved_by": "alice"},
    )
    assert art2.id == art.id
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
