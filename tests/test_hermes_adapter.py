import json

from fastapi.testclient import TestClient

from mac.cli import main as mac_cli_main
from mac.api import create_app
from mac.hermes_adapter import (
    ConversationTaskInput,
    HermesMacAdapter,
    MacApiClient,
    MacApiError,
    PlatformBindingSpec,
    build_parser,
    main as mac_hermes_main,
)
from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane
from mac.store import SQLiteStore


def api_transport(client):
    def transport(method, path, payload):
        if method == "GET":
            response = client.get(path)
        elif method == "POST":
            response = client.post(path, json=payload)
        else:
            raise MacApiError("unsupported test method: %s" % method)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json()

    return transport


def register_agent(cp, name, capabilities):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name, capabilities=capabilities)


def test_mac_hermes_cli_defaults_to_deployed_hub_env(monkeypatch):
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.setenv("MAC_HUB_URL", "http://hub.example.internal:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker-token")

    args = build_parser().parse_args(["work-brief", "hermes_rocky"])

    assert args.url == "http://hub.example.internal:8789"
    assert args.token == "worker-token"


def finish_task(cp, task_id):
    from mac.services import sign_verification_manifest
    from tests.conftest import submit_review_verdict

    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task, _lease = cp.claim_task(task_id, worker.id)
    assert task.state == TaskState.CLAIMED.value
    cp.start_task(task_id, worker.id)
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "test",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/example",
            "dirty": False,
        },
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    manifest["signed_by"] = worker.id
    manifest["signature"] = sign_verification_manifest(cp._agent_attestation_key(worker.id), manifest)
    evidence = cp.add_evidence(
        task_id,
        "test",
        "artifact://pytest",
        "tests passed",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task_id, worker.id)
    review = cp.request_review(task_id, reviewer.id)
    verdict_id = submit_review_verdict(cp, task_id, reviewer.id, evidence.id)
    cp.submit_review(review.id, ReviewStatus.APPROVED.value, reviewer.id, evidence_id=verdict_id)
    cp.publish_task(task_id, "test://publish", reviewer.id, evidence_id=evidence.id)


def test_hermes_adapter_registers_identity_and_creates_sanitized_task():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))

    registration = adapter.register_identity(
        "personal",
        "Rocky",
        "rocky",
        "hermes://personal/rocky/SOUL.md",
        "hermes://personal/rocky/memory",
        platform_bindings=[PlatformBindingSpec("slack", "T123/C456", "#ops")],
    )
    repeat = adapter.register_identity(
        "personal",
        "Rocky",
        "rocky",
        "hermes://personal/rocky/SOUL.md",
        "hermes://personal/rocky/memory",
    )
    assert repeat["tenant"]["id"] == registration["tenant"]["id"]
    assert repeat["persona"]["id"] == registration["persona"]["id"]
    assert repeat["hermes_instance"]["id"] == registration["hermes_instance"]["id"]

    task = adapter.create_task_from_conversation(
        registration["hermes_instance"]["id"],
        ConversationTaskInput(
            title="Investigate failed deploy",
            summary="Deploy failed after the package publish step.",
            platform_binding_id=registration["platform_bindings"][0]["id"],
            conversation_ref="slack://T123/C456/1712345678.000100",
            project="deploy",
            required_capabilities=["ops"],
            snippets=["User-visible error: publish returned 500"],
            metadata={
                "ticket": "INC-42",
                "private_memory": "do not copy",
                "api_token": "do not copy",
                "raw_messages": ["do not copy"],
            },
        ),
    )

    assert task["metadata"]["origin"]["type"] == "hermes_interaction"
    assert task["metadata"]["sanitized_conversation"]["summary"].startswith("Deploy failed")
    assert task["metadata"]["ticket"] == "INC-42"
    assert "private_memory" not in task["metadata"]
    assert "api_token" not in task["metadata"]
    assert "raw_messages" not in task["metadata"]
    assert "do not copy" not in task["description"]

    work_context = adapter.work_context(registration["hermes_instance"]["id"])
    assert work_context["schema"] == "mac.hermes_work_context.v1"
    assert work_context["projects"][0]["project"] == "deploy"
    assert work_context["tasks"][0]["id"] == task["id"]
    assert work_context["tasks"][0]["origin"]["hermes_instance_id"] == registration["hermes_instance"]["id"]
    assert any(
        operation["name"] == "create_task_from_conversation"
        for operation in work_context["operations"]["api"]
    )
    operation_names = {operation["name"] for operation in work_context["operations"]["api"]}
    assert {
        "add_child_tasks",
        "list_tasks",
        "list_projects",
        "get_project",
        "list_project_items",
        "register_beads_repository",
        "list_beads_repositories",
        "poll_beads_repositories",
        "claim_next_task",
        "record_command_audit",
        "list_command_audit",
        "list_agents",
        "get_agent",
        "get_agent_identity",
    } <= operation_names
    assert any(
        "mac-hermes tasks" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes projects" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes project-items" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes agents" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes claim-next" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes add-child-task" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes command-audit" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "mac-hermes web-search" in command
        for command in work_context["operations"]["mac_hermes_cli"]
    )
    assert any(
        "hgmac agents create" in command
        for command in work_context["operations"]["hgmac_cli"]
    )
    assert any(
        "hgmac tasks add-child" in command
        for command in work_context["operations"]["hgmac_cli"]
    )
    assert adapter.work_context_brief(registration["hermes_instance"]["id"]).startswith(
        "MAC work context:"
    )


def test_hermes_adapter_exposes_project_bridge_operations():
    calls = []

    def transport(method, path, payload):
        calls.append((method, path, payload))
        return {"path": path, "payload": payload}

    adapter = HermesMacAdapter(MacApiClient("http://hub:8789", transport=transport))

    adapter.import_project_item(
        "repo-beads-mac",
        "mac-123",
        "Ship project bridge",
        payload={"summary": "track this", "secret": "drop"},
        description="Track project bridge work",
        project="repo-beads-mac",
        priority=7,
        required_capabilities=["ops"],
        dependencies=["task_parent"],
        metadata={"team": "core", "api_token": "drop"},
    )
    adapter.create_project(
        "c26",
        "RISC-V home computer proof",
        metadata={"team": "retro", "api_token": "drop"},
    )
    adapter.list_project_items()
    adapter.list_projects()
    adapter.project_detail("repo-beads-mac")
    adapter.register_beads_repository(
        "mac",
        "/repo/mac",
        source="repo-beads-mac",
        project="repo-beads-mac",
        required_capabilities=["ops", "tests"],
        poll_interval_seconds=30,
        metadata={"team": "core", "api_token": "drop"},
    )
    adapter.list_beads_repositories(enabled=True)
    adapter.poll_beads_repositories(repository="mac", force=True, actor="agent_1")
    adapter.list_agents()
    adapter.agent_detail("agent_1")
    adapter.agent_identity("agent_1")

    assert calls == [
        (
            "POST",
            "/bridge/items",
            {
                "source": "repo-beads-mac",
                "external_id": "mac-123",
                "title": "Ship project bridge",
                "description": "Track project bridge work",
                "project": "repo-beads-mac",
                "priority": 7,
                "payload": {"summary": "track this"},
                "required_capabilities": ["ops"],
                "dependencies": ["task_parent"],
                "metadata": {"team": "core"},
                "actor": "hermes",
            },
        ),
        (
            "POST",
            "/projects",
            {
                "name": "c26",
                "description": "RISC-V home computer proof",
                "metadata": {"team": "retro"},
                "status": "active",
                "actor": "hermes",
            },
        ),
        ("GET", "/bridge/items", None),
        ("GET", "/projects", None),
        ("GET", "/projects/repo-beads-mac", None),
        (
            "POST",
            "/bridge/beads/repositories",
            {
                "name": "mac",
                "path": "/repo/mac",
                "source": "repo-beads-mac",
                "project": "repo-beads-mac",
                "required_capabilities": ["ops", "tests"],
                "enabled": True,
                "poll_interval_seconds": 30,
                "metadata": {"team": "core"},
                "actor": "hermes",
            },
        ),
        ("GET", "/bridge/beads/repositories?enabled=true", None),
        (
            "POST",
            "/bridge/beads/poll",
            {"repository": "mac", "force": True, "actor": "agent_1"},
        ),
        ("GET", "/agents", None),
        ("GET", "/agents/agent_1", None),
        ("GET", "/agents/agent_1/identity", None),
    ]


def test_hermes_adapter_summarizes_result_and_prepares_memory_writeback():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))
    registration = adapter.register_identity(
        "team",
        "Natasha",
        "natasha",
        "hermes://team/natasha/SOUL.md",
        "hermes://team/natasha/memory",
    )
    task = adapter.create_task_from_conversation(
        registration["hermes_instance"]["id"],
        ConversationTaskInput(
            title="Fix build",
            summary="The build is failing in CI.",
            required_capabilities=["ops"],
        ),
    )
    finish_task(cp, task["id"])

    summary = adapter.task_summary(task["id"])
    assert summary["state"] == "completed"
    assert summary["approved_review_count"] == 1
    assert adapter.user_reply_for_task(task["id"]) == "Fix build is complete and published to test://publish."

    writes = []
    writeback = adapter.write_completed_task_to_memory(
        registration["hermes_instance"]["id"],
        task["id"],
        sink=writes.append,
    )
    assert writes[0]["memory_scope"] == "hermes://team/natasha/memory"
    assert writes[0]["content"] == "Fix build is complete and published to test://publish."
    assert writeback["record"]["subject_type"] == "hermes_memory"
    assert cp.search_memory(task_id=task["id"])[0].record_type == "task_result_writeback"


def test_hermes_adapter_performs_task_lifecycle_operations_through_api():
    from mac.services import sign_verification_manifest
    from tests.conftest import submit_review_verdict

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))
    registration = adapter.register_identity(
        "team",
        "Rocky",
        "rocky",
        "hermes://team/rocky/SOUL.md",
        "hermes://team/rocky/memory",
    )
    worker = register_agent(cp, "worker", ["ops"])
    reviewer = register_agent(cp, "reviewer", ["review"])
    task = adapter.create_task_from_conversation(
        registration["hermes_instance"]["id"],
        ConversationTaskInput(
            title="Ship lifecycle bridge",
            summary="Exercise the task operation bridge.",
            project="mac",
            required_capabilities=["ops"],
        ),
    )

    open_tasks = adapter.list_tasks(state=TaskState.OPEN.value)
    assert [item["id"] for item in open_tasks] == [task["id"]]

    dry_run = adapter.claim_next_task(
        worker.id,
        lease_seconds=120,
        allowed_projects=["mac"],
        dry_run=True,
    )
    assert dry_run is not None
    assert dry_run["dry_run"] is True
    assert dry_run["task"]["id"] == task["id"]
    assert dry_run["lease"] is None
    assert cp.get_task(task["id"]).state == TaskState.OPEN.value

    claim = adapter.claim_task(task["id"], worker.id, lease_seconds=300)
    assert claim["task"]["state"] == TaskState.CLAIMED.value
    assert claim["lease"]["agent_id"] == worker.id
    assert adapter.start_task(task["id"], worker.id)["state"] == TaskState.RUNNING.value

    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "test",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/task/hermes-bridge",
            "dirty": False,
        },
        "checks": [{"name": "pytest", "returncode": 0}],
    }
    manifest["signed_by"] = worker.id
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(worker.id),
        manifest,
    )
    evidence = adapter.add_evidence(
        task["id"],
        "test",
        "artifact://pytest",
        "tests passed",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    assert evidence["task_id"] == task["id"]
    assert adapter.submit_for_review(task["id"], worker.id)["state"] == TaskState.NEEDS_REVIEW.value

    review = adapter.request_review(task["id"], reviewer.id, actor="hermes")
    assert review["reviewer_agent_id"] == reviewer.id
    claim_review = adapter.claim_review(
        review["id"],
        reviewer.id,
        executor_evidence_id=evidence["id"],
        actor="hermes",
    )
    assert claim_review["status"] == "claimed"
    verdict_id = submit_review_verdict(cp, task["id"], reviewer.id, evidence["id"])
    decision = adapter.submit_review(
        review["id"],
        ReviewStatus.APPROVED.value,
        reviewer.id,
        evidence_id=verdict_id,
    )
    assert decision["status"] == ReviewStatus.APPROVED.value
    publication = adapter.publish_task(
        task["id"],
        "test://publish",
        reviewer.id,
        evidence_id=evidence["id"],
    )
    assert publication["status"] == "published"
    assert adapter.task_summary(task["id"])["state"] == TaskState.COMPLETED.value


def test_hermes_adapter_transition_operation_updates_mac_task_state():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))
    registration = adapter.register_identity(
        "team",
        "Natasha",
        "natasha",
        "hermes://team/natasha/SOUL.md",
        "hermes://team/natasha/memory",
    )
    task = adapter.create_task_from_conversation(
        registration["hermes_instance"]["id"],
        ConversationTaskInput(title="Pause task", summary="Mark this blocked."),
    )

    blocked = adapter.transition_task(
        task["id"],
        TaskState.BLOCKED.value,
        "hermes",
        {"reason": "waiting for user"},
    )

    assert blocked["state"] == TaskState.BLOCKED.value
    assert cp.task_history(task["id"])[-1].detail["reason"] == "waiting for user"


def test_hermes_adapter_records_and_lists_command_audit():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    adapter = HermesMacAdapter(MacApiClient("http://testserver", transport=api_transport(client)))
    worker = register_agent(cp, "worker", ["ops"])
    task = cp.create_task("Audit shell work", project="mac")

    record = adapter.record_command_audit(
        worker.id,
        phase="started",
        argv=["git", "status", "--token", "secret-token", "password=hidden"],
        cwd="/workspace/mac",
        task_id=task.id,
        metadata={"purpose": "test", "api_token": "hidden"},
    )

    assert record["agent_id"] == worker.id
    assert record["task_id"] == task.id
    assert record["phase"] == "started"
    assert record["argv"] == ["git", "status", "--token", "<redacted>", "<redacted>"]
    assert record["metadata"] == {"purpose": "test"}
    records = adapter.list_command_audit(agent_id=worker.id, task_id=task.id, limit=5)
    assert [item["id"] for item in records] == [record["id"]]


def test_hermes_adapter_exposes_firecrawl_web_research_bridge():
    calls = []

    def request(method, path, payload):
        calls.append((method, path, payload))
        if path.startswith("/v2/crawl/"):
            return {"success": True, "status": "completed", "data": []}
        return {"success": True, "path": path, "payload": payload}

    adapter = HermesMacAdapter(
        MacApiClient("http://mac.invalid"),
        web_client=MacApiClient("http://firecrawl:3002", transport=request),
    )

    assert adapter.web_search("release notes", limit=2)["success"] is True
    assert adapter.web_scrape("https://example.com", formats=["markdown", "html"])["success"] is True
    assert adapter.web_crawl("https://example.com", limit=1, formats=["markdown"])["success"] is True
    assert adapter.web_crawl_status("crawl_1")["status"] == "completed"
    assert calls == [
        ("POST", "/v2/search", {"query": "release notes", "limit": 2}),
        ("POST", "/v2/scrape", {"url": "https://example.com", "formats": ["markdown", "html"]}),
        (
            "POST",
            "/v2/crawl",
            {
                "url": "https://example.com",
                "limit": 1,
                "scrapeOptions": {"formats": ["markdown"]},
            },
        ),
        ("GET", "/v2/crawl/crawl_1", None),
    ]


def test_mac_cli_prints_hermes_work_context(tmp_path, capsys, monkeypatch):
    db = tmp_path / "mac.db"
    monkeypatch.setenv("MAC_SECRET_KEY", "test-secret-key-for-cli-work-context")
    cp = ControlPlane(
        SQLiteStore(str(db)),
        secret_key="test-secret-key-for-cli-work-context",
    )
    tenant = cp.register_tenant("team")
    persona = cp.register_persona(
        tenant.id,
        "Rocky",
        "hermes://team/rocky/SOUL.md",
        "hermes://team/rocky/memory",
    )
    hermes = cp.register_hermes_instance(tenant.id, "rocky", persona_id=persona.id)
    cp.create_interaction_task(
        hermes.id,
        "Track project from Hermes",
        project="mac",
        description="Created through the Hermes boundary.",
    )

    rc = mac_cli_main(["--db", str(db), "hermes", "work-context", hermes.id])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mac.hermes_work_context.v1"
    assert payload["projects"][0]["project"] == "mac"
    assert payload["operations"]["mac_cli"][0].startswith("mac hermes work-context")

    rc = mac_cli_main(["--db", str(db), "project", "list"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["project"] == "mac"

    rc = mac_cli_main(
        [
            "--db",
            str(db),
            "project",
            "create",
            "c26",
            "--description",
            "RISC-V home computer proof",
            "--metadata",
            '{"team":"retro"}',
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "c26"
    assert payload["description"] == "RISC-V home computer proof"

    rc = mac_cli_main(["--db", str(db), "project", "show", "mac"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "mac"
    assert payload["summary"]["task_count"] == 1

    rc = mac_cli_main(
        [
            "--db",
            str(db),
            "hermes",
            "runtime-proof",
            hermes.id,
            "--skip-startup-report",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mac.hermes_runtime_proof.v1"
    assert payload["evidence"]["cli"]["mac_cli_commands"][0].startswith("mac hermes work-context")
    assert any(
        command.startswith("mac hermes runtime-proof")
        for command in payload["evidence"]["cli"]["mac_cli_commands"]
    )


def test_mac_cli_bridge_import_preserves_project_fields(tmp_path, capsys, monkeypatch):
    db = tmp_path / "mac.db"
    monkeypatch.setenv("MAC_SECRET_KEY", "test-secret-key-for-cli-project-import")
    cp = ControlPlane(SQLiteStore(str(db)))
    parent = cp.create_task("Parent task", project="repo-beads-mac")

    rc = mac_cli_main(
        [
            "--db",
            str(db),
            "bridge",
            "import",
            "repo-beads-mac",
            "mac-cli",
            "CLI imported project item",
            "--description",
            "Imported through the MAC CLI.",
            "--project",
            "repo-beads-mac",
            "--priority",
            "13",
            "--payload",
            '{"summary":"track this"}',
            "--required-capabilities",
            "python,ops",
            "--dependencies",
            parent.id,
            "--metadata",
            '{"team":"core"}',
            "--actor",
            "cli-test",
        ]
    )

    assert rc == 0
    item = json.loads(capsys.readouterr().out)
    task = ControlPlane(SQLiteStore(str(db))).get_task(item["task_id"])
    assert task.project == "repo-beads-mac"
    assert task.description == "Imported through the MAC CLI."
    assert task.priority == 13
    assert sorted(task.required_capabilities) == ["ops", "python"]
    assert task.dependencies == [parent.id]
    assert task.metadata["team"] == "core"


def test_mac_hermes_cli_fetches_work_context(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((self.base_url, method, path, payload))
        return {
            "schema": "mac.hermes_work_context.v1",
            "projects": [],
            "tasks": [],
            "agents": [],
            "relationships": {},
            "operations": {},
        }

    monkeypatch.setattr(MacApiClient, "request", request)

    rc = mac_hermes_main(
        [
            "--url",
            "http://hub:8789",
            "work-context",
            "hermes_1",
            "--active-only",
            "--task-limit",
            "7",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "mac.hermes_work_context.v1"
    assert calls == [
        (
            "http://hub:8789",
            "GET",
            "/hermes-instances/hermes_1/work-context?include_completed=false&task_limit=7",
            None,
        )
    ]


def test_mac_hermes_cli_submits_local_startup_for_runtime_proof(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((self.base_url, method, path, payload))
        return {"schema": "mac.hermes_runtime_proof.v1", "ready": True}

    startup = {
        "task_project_runtime": {
            "ready": True,
            "first_class_object_names": ["fleets", "tasks", "projects", "agents"],
        }
    }
    monkeypatch.setattr(MacApiClient, "request", request)
    monkeypatch.setattr("mac.hermes_startup.build_hermes_startup_report", lambda: startup)

    rc = mac_hermes_main(
        [
            "--url",
            "http://hub:8789",
            "runtime-proof",
            "hermes_1",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "mac.hermes_runtime_proof.v1"
    assert calls == [
        (
            "http://hub:8789",
            "POST",
            "/hermes-instances/hermes_1/runtime-proof",
            {"hermes_startup": startup},
        )
    ]


def test_mac_hermes_cli_can_fetch_hub_only_runtime_proof(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((self.base_url, method, path, payload))
        return {"schema": "mac.hermes_runtime_proof.v1", "ready": True}

    monkeypatch.setattr(MacApiClient, "request", request)

    rc = mac_hermes_main(
        [
            "--url",
            "http://hub:8789",
            "runtime-proof",
            "hermes_1",
            "--skip-local-startup",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "mac.hermes_runtime_proof.v1"
    assert calls == [
        (
            "http://hub:8789",
            "GET",
            "/hermes-instances/hermes_1/runtime-proof",
            None,
        )
    ]


def test_mac_hermes_cli_exposes_project_bridge_operations(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((method, path, payload))
        return {"schema": "ok", "path": path, "payload": payload}

    monkeypatch.setattr(MacApiClient, "request", request)
    commands = [
        ["project-items"],
        [
            "create-project",
            "c26",
            "--description",
            "RISC-V home computer proof",
            "--metadata",
            '{"team":"retro","api_token":"drop"}',
        ],
        ["projects"],
        ["project-detail", "repo-beads-mac"],
        [
            "import-project-item",
            "repo-beads-mac",
            "mac-123",
            "Ship project bridge",
            "--payload",
            '{"summary":"track this","secret":"drop"}',
            "--description",
            "Track project bridge work",
            "--project",
            "repo-beads-mac",
            "--priority",
            "7",
            "--required-capabilities",
            "ops,tests",
            "--dependencies",
            "task_parent",
            "--metadata",
            '{"team":"core","api_token":"drop"}',
            "--actor",
            "agent_1",
        ],
        ["beads-repositories", "--enabled"],
        [
            "register-beads-repository",
            "mac",
            "/repo/mac",
            "--source",
            "repo-beads-mac",
            "--project",
            "repo-beads-mac",
            "--required-capabilities",
            "ops",
            "--poll-interval-seconds",
            "30",
            "--metadata",
            '{"team":"core","api_token":"drop"}',
            "--actor",
            "agent_1",
        ],
        [
            "poll-beads-repositories",
            "--repository",
            "mac",
            "--force",
            "--actor",
            "agent_1",
        ],
        ["agents"],
        ["agent-detail", "agent_1"],
        ["agent-identity", "agent_1"],
    ]

    for command in commands:
        rc = mac_hermes_main(["--url", "http://hub:8789", *command])
        assert rc == 0
        capsys.readouterr()

    assert calls == [
        ("GET", "/bridge/items", None),
        (
            "POST",
            "/projects",
            {
                "name": "c26",
                "description": "RISC-V home computer proof",
                "metadata": {"team": "retro"},
                "status": "active",
                "actor": "hermes",
            },
        ),
        ("GET", "/projects", None),
        ("GET", "/projects/repo-beads-mac", None),
        (
            "POST",
            "/bridge/items",
            {
                "source": "repo-beads-mac",
                "external_id": "mac-123",
                "title": "Ship project bridge",
                "description": "Track project bridge work",
                "project": "repo-beads-mac",
                "priority": 7,
                "payload": {"summary": "track this"},
                "required_capabilities": ["ops", "tests"],
                "dependencies": ["task_parent"],
                "metadata": {"team": "core"},
                "actor": "agent_1",
            },
        ),
        ("GET", "/bridge/beads/repositories?enabled=true", None),
        (
            "POST",
            "/bridge/beads/repositories",
            {
                "name": "mac",
                "path": "/repo/mac",
                "source": "repo-beads-mac",
                "project": "repo-beads-mac",
                "required_capabilities": ["ops"],
                "enabled": True,
                "poll_interval_seconds": 30,
                "metadata": {"team": "core"},
                "actor": "agent_1",
            },
        ),
        (
            "POST",
            "/bridge/beads/poll",
            {"repository": "mac", "force": True, "actor": "agent_1"},
        ),
        ("GET", "/agents", None),
        ("GET", "/agents/agent_1", None),
        ("GET", "/agents/agent_1/identity", None),
    ]


def test_mac_hermes_cli_exposes_task_lifecycle_operations(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((method, path, payload))
        return {"schema": "ok", "path": path, "payload": payload}

    monkeypatch.setattr(MacApiClient, "request", request)
    commands = [
        ["tasks", "--state", "open", "--tenant-id", "tenant_1"],
        ["task-detail", "task_1"],
        [
            "claim-next",
            "agent_1",
            "--lease-seconds",
            "45",
            "--allowed-project",
            "mac",
            "--required-metadata",
            '{"canary":true}',
            "--require-canary",
            "--dry-run",
        ],
        ["claim", "task_1", "agent_1", "--lease-seconds", "30"],
        ["start", "task_1", "agent_1"],
        ["transition", "task_1", "blocked", "--actor", "hermes", "--detail", '{"reason":"waiting"}'],
        [
            "evidence",
            "task_1",
            "--kind",
            "test",
            "--uri",
            "artifact://pytest",
            "--summary",
            "tests passed",
            "--created-by",
            "agent_1",
            "--metadata",
            '{"result":"pass"}',
        ],
        ["submit-review", "task_1", "agent_1", "--advance-default-workflow"],
        ["request-review", "task_1", "agent_2", "--actor", "hermes"],
        ["claim-review", "review_1", "agent_2", "--executor-evidence-id", "ev_1"],
        ["review-decision", "review_1", "approved", "agent_2", "--evidence-id", "ev_review"],
        ["publish", "task_1", "git://main", "agent_2", "--evidence-id", "ev_1"],
    ]

    for command in commands:
        rc = mac_hermes_main(["--url", "http://hub:8789", *command])
        assert rc == 0
        capsys.readouterr()

    assert calls == [
        ("GET", "/tasks?state=open&tenant_id=tenant_1", None),
        ("GET", "/tasks/task_1", None),
        (
            "POST",
            "/agents/agent_1/claim-next",
            {
                "lease_seconds": 45,
                "allowed_projects": ["mac"],
                "required_metadata": {"canary": True},
                "require_canary": True,
                "dry_run": True,
            },
        ),
        ("POST", "/tasks/task_1/claim?agent_id=agent_1&lease_seconds=30", {}),
        ("POST", "/tasks/task_1/start?agent_id=agent_1", {}),
        (
            "POST",
            "/tasks/task_1/transition",
            {"target_state": "blocked", "actor": "hermes", "detail": {"reason": "waiting"}},
        ),
        (
            "POST",
            "/tasks/task_1/evidence",
            {
                "kind": "test",
                "uri": "artifact://pytest",
                "summary": "tests passed",
                "created_by": "agent_1",
                "checksum": None,
                "metadata": {"result": "pass"},
            },
        ),
        (
            "POST",
            "/tasks/task_1/submit-for-review?agent_id=agent_1&advance_default_workflow=true",
            {},
        ),
        (
            "POST",
            "/tasks/task_1/reviews",
            {"reviewer_agent_id": "agent_2", "actor": "hermes"},
        ),
        (
            "POST",
            "/reviews/review_1/claim",
            {
                "reviewer_agent_id": "agent_2",
                "executor_evidence_id": "ev_1",
                "actor": "hermes",
            },
        ),
        (
            "POST",
            "/reviews/review_1/decision",
            {
                "status": "approved",
                "reviewer_agent_id": "agent_2",
                "reason": None,
                "evidence_id": "ev_review",
            },
        ),
        (
            "POST",
            "/publications",
            {
                "task_id": "task_1",
                "target": "git://main",
                "created_by": "agent_2",
                "evidence_id": "ev_1",
            },
        ),
    ]


def test_mac_hermes_cli_exposes_command_audit_operations(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((method, path, payload))
        return {"schema": "ok", "path": path, "payload": payload}

    monkeypatch.setattr(MacApiClient, "request", request)

    rc = mac_hermes_main(
        [
            "--url",
            "http://hub:8789",
            "command-audit",
            "record",
            "agent_1",
            "--phase",
            "completed",
            "--argv-json",
            '["git","status","--token","secret"]',
            "--cwd",
            "/workspace/mac",
            "--task-id",
            "task_1",
            "--returncode",
            "0",
            "--metadata",
            '{"purpose":"test","api_token":"hidden"}',
        ]
    )
    assert rc == 0
    capsys.readouterr()

    rc = mac_hermes_main(
        [
            "--url",
            "http://hub:8789",
            "command-audit",
            "list",
            "--agent-id",
            "agent_1",
            "--task-id",
            "task_1",
            "--phase",
            "completed",
            "--limit",
            "5",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    assert calls == [
        (
            "POST",
            "/agents/agent_1/command-audit",
            {
                "command_id": None,
                "phase": "completed",
                "argv": ["git", "status", "--token", "<redacted>"],
                "cwd": "/workspace/mac",
                "task_id": "task_1",
                "lease_id": None,
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "returncode": 0,
                "stdout_sha256": None,
                "stderr_sha256": None,
                "stdout_bytes": None,
                "stderr_bytes": None,
                "metadata": {"purpose": "test"},
            },
        ),
        (
            "GET",
            "/command-audit?agent_id=agent_1&task_id=task_1&phase=completed&limit=5",
            None,
        ),
    ]


def test_mac_hermes_cli_exposes_firecrawl_web_research_commands(monkeypatch, capsys):
    calls = []

    def request(self, method, path, payload):
        calls.append((self.base_url, method, path, payload))
        if path.startswith("/v2/crawl/"):
            return {"success": True, "status": "completed", "data": []}
        return {"success": True, "path": path, "payload": payload}

    monkeypatch.setattr(MacApiClient, "request", request)
    commands = [
        ["web-search", "release notes", "--limit", "2"],
        ["web-scrape", "https://example.com", "--format", "markdown", "--format", "html"],
        ["web-crawl", "https://example.com", "--limit", "1", "--format", "markdown"],
        ["web-crawl-status", "crawl_1"],
    ]

    for command in commands:
        rc = mac_hermes_main(["--web-url", "http://firecrawl:3002", *command])
        assert rc == 0
        capsys.readouterr()

    assert calls == [
        ("http://firecrawl:3002", "POST", "/v2/search", {"query": "release notes", "limit": 2}),
        (
            "http://firecrawl:3002",
            "POST",
            "/v2/scrape",
            {"url": "https://example.com", "formats": ["markdown", "html"]},
        ),
        (
            "http://firecrawl:3002",
            "POST",
            "/v2/crawl",
            {
                "url": "https://example.com",
                "limit": 1,
                "scrapeOptions": {"formats": ["markdown"]},
            },
        ),
        ("http://firecrawl:3002", "GET", "/v2/crawl/crawl_1", None),
    ]
