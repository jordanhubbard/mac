import base64
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mac.agentbus_control import (
    DEBUG_TERMINAL_OUTPUT_SCHEMA,
    HERMES_CONFIG_APPLY_CONTENT_TYPE,
    HERMES_CONFIG_APPLY_TOPIC,
)
from mac.api import create_app
from mac.deploy_env import parse_env_text
from mac.services import ControlPlane, sign_verification_manifest


def test_hub_tick_loop_gated_by_env(monkeypatch):
    # mac-selfdrive: the hub self-drives tick() only when the interval env is
    # set (>0), so the CLI/tests/replicas don't each spawn a competing ticker.
    monkeypatch.delenv("MAC_HUB_TICK_INTERVAL_SECONDS", raising=False)
    app = create_app(control_plane=ControlPlane.in_memory())
    assert getattr(app.state, "hub_tick_thread", None) is None

    # A large interval means the daemon sleeps well past the test's lifetime, so
    # it spawns but never fires tick() here — validates wiring without flakiness.
    monkeypatch.setenv("MAC_HUB_TICK_INTERVAL_SECONDS", "999")
    app2 = create_app(control_plane=ControlPlane.in_memory())
    thread = getattr(app2.state, "hub_tick_thread", None)
    assert thread is not None and thread.daemon is True and thread.is_alive()


def test_well_known_acp_manifest_is_public_and_advertises_mac_extensions(monkeypatch):
    # ADR 0006 Phase 3: the discovery manifest is unauthenticated even when the
    # hub is token-protected, and carries protocolVersion + mac's _meta extensions.
    monkeypatch.setenv("MAC_API_TOKEN", "secret-token")
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    resp = client.get("/.well-known/acp")  # no Authorization header
    assert resp.status_code == 200
    manifest = resp.json()
    assert manifest["protocolVersion"] == 1
    assert isinstance(manifest["protocolVersion"], int)
    assert manifest["_meta"]["mac"] == {
        "sandbox": True,
        "decomposition": True,
        "evidence": True,
    }
    assert manifest["agentCapabilities"]["loadSession"] is False
    assert manifest["agentCapabilities"]["_meta"]["mac"]["evidence"] is True


def test_post_tasks_accepts_summary_alias_for_description():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    resp = client.post(
        "/tasks",
        json={"title": "summary alias", "summary": "worker instructions"},
    )

    assert resp.status_code == 200
    created = resp.json()
    assert created["description"] == "worker instructions"
    detail = client.get("/tasks/%s" % created["id"]).json()["task"]
    assert detail["description"] == "worker instructions"


def test_repositories_onboard_creates_contract_backed_task():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.post(
        "/repositories/onboard",
        json={"repository_url": "https://github.com/NVIDIA-dev/taskbrain.git"},
    )
    assert resp.status_code == 200
    task = resp.json()
    assert task["project"] == "taskbrain"
    origin = task["metadata"]["origin"]
    assert origin["type"] == "direct_task"
    assert origin["repository_url"] == "https://github.com/NVIDIA-dev/taskbrain.git"
    assert origin["onboarding"] is True
    # A malformed URL is a client error (400), never a 500.
    bad = client.post("/repositories/onboard", json={"repository_url": "not-a-url"})
    assert bad.status_code == 400


def test_fastapi_exposes_core_workflow_and_redacts_secrets():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    assert client.get("/health").json() == {"status": "ok"}
    machine_response = client.post("/machines", json={"hostname": "host-1"})
    assert machine_response.status_code == 200
    machine = machine_response.json()
    agent_response = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python", "deploy"]},
    )
    assert agent_response.status_code == 200
    agent = agent_response.json()
    task_response = client.post(
        "/tasks",
        json={"title": "API task", "required_capabilities": ["python"]},
    )
    assert task_response.status_code == 200
    task = task_response.json()

    assignment_response = client.post("/dispatch/assign", json={"lease_seconds": 900})
    assert assignment_response.status_code == 200
    assignment = assignment_response.json()
    assert assignment["task"]["id"] == task["id"]
    assert assignment["agent"]["id"] == agent["id"]

    secret_response = client.post(
        "/secrets",
        json={
            "name": "deploy-token",
            "value": "never-return-this",
            "scopes": {"capabilities": ["deploy"]},
            "created_by": "human",
        },
    )
    assert secret_response.status_code == 200
    secret = secret_response.json()
    listed = client.get("/secrets").json()
    assert listed[0]["value"] == "***REDACTED***"
    handle = client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "deploy"},
    ).json()
    assert handle["handle"].startswith("secret://")
    assert "never-return-this" not in str(handle)
    revealed = client.post(
        "/secrets/%s/reveal" % secret["id"],
        json={"audit_id": handle["audit_id"], "accessor_agent_id": agent["id"]},
    ).json()
    assert revealed["value"] == "never-return-this"


def test_fastapi_exposes_hermes_identity_boundary(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", raising=False)
    monkeypatch.delenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", raising=False)
    monkeypatch.delenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", raising=False)
    monkeypatch.delenv("MAC_HERMES_INSTANCE_ID", raising=False)
    monkeypatch.delenv("MAC_WORKER_HERMES_INSTANCE_ID", raising=False)

    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    tenant = client.post("/tenants", json={"name": "team"}).json()
    persona = client.post(
        "/personas",
        json={
            "tenant_id": tenant["id"],
            "name": "Rocky",
            "soul_ref": "hermes://team/rocky/SOUL.md",
            "memory_scope": "hermes://team/rocky/memory",
        },
    ).json()
    hermes = client.post(
        "/hermes-instances",
        json={
            "tenant_id": tenant["id"],
            "name": "rocky",
            "persona_id": persona["id"],
            "home_ref": "hermes://team/rocky",
        },
    ).json()
    binding = client.post(
        "/platform-bindings",
        json={
            "tenant_id": tenant["id"],
            "hermes_instance_id": hermes["id"],
            "platform": "telegram",
            "external_id": "chat-42",
        },
    ).json()

    context = client.get("/hermes-instances/%s/context" % hermes["id"]).json()
    assert context["memory_contract"]["user_memory_authority"] == "hermes"
    assert context["platform_bindings"][0]["id"] == binding["id"]

    machine = client.post("/machines", json={"hostname": "rocky-host"}).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "rocky",
            "capabilities": ["ops"],
            "hermes_instance_id": hermes["id"],
        },
    ).json()
    dependency = client.post(
        "/hermes-instances/%s/tasks" % hermes["id"],
        json={
            "title": "Prepare project",
            "project": "nanolang",
            "required_capabilities": ["ops"],
        },
    ).json()
    task = client.post(
        "/hermes-instances/%s/tasks" % hermes["id"],
        json={
            "title": "Follow up from chat",
            "project": "nanolang",
            "platform_binding_id": binding["id"],
            "conversation_ref": "telegram://chat-42/99",
            "dependencies": [dependency["id"]],
        },
    ).json()
    cp.claim_task(dependency["id"], agent["id"])
    assert task["metadata"]["origin"]["hermes_instance_id"] == hermes["id"]
    assert task["metadata"]["memory_boundary"]["mac_records_operational_provenance_only"] is True

    work_context = client.get("/hermes-instances/%s/work-context" % hermes["id"]).json()
    assert work_context["schema"] == "mac.hermes_work_context.v1"
    assert work_context["authority"]["tasks"] == "mac"
    assert work_context["authority"]["projects"] == "mac"
    assert {item["id"] for item in work_context["tasks"]} == {dependency["id"], task["id"]}
    assert work_context["projects"][0]["project"] == "nanolang"
    assert work_context["projects"][0]["task_count"] == 2
    assert work_context["projects"][0]["blocked_count"] == 1
    assert work_context["agents"][0]["hermes_instance_id"] == hermes["id"]
    assert work_context["agents"][0]["active_task_ids"] == [dependency["id"]]
    assert work_context["relationships"]["task_dependencies"][0]["task_id"] == task["id"]
    assert work_context["relationships"]["agent_assignments"][0]["agent_id"] == agent["id"]
    assert any(
        operation["name"] == "get_work_context"
        for operation in work_context["operations"]["api"]
    )
    assert any(
        operation["name"] == "get_runtime_proof"
        for operation in work_context["operations"]["api"]
    )
    operation_names = {operation["name"] for operation in work_context["operations"]["api"]}
    assert {
        "list_tasks",
        "create_project",
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
    tasks = client.get("/tasks").json()
    assert {item["id"] for item in tasks} == {dependency["id"], task["id"]}
    projects = client.get("/projects").json()
    assert projects[0]["project"] == "nanolang"
    assert projects[0]["task_count"] == 2
    project_detail = client.get("/projects/nanolang").json()
    assert project_detail["project"] == "nanolang"
    assert project_detail["summary"]["blocked_count"] == 1
    assert {item["id"] for item in project_detail["tasks"]} == {dependency["id"], task["id"]}
    created_project = client.post(
        "/projects",
        json={
            "name": "c26",
            "description": "RISC-V home computer proof",
            "metadata": {"inception": True},
        },
    ).json()
    assert created_project["name"] == "c26"
    project_detail = client.get("/projects/c26").json()
    assert project_detail["record"]["description"] == "RISC-V home computer proof"
    assert project_detail["summary"]["task_count"] == 0
    assert any("mac task list" in command for command in work_context["operations"]["mac_cli"])
    assert any("mac project create" in command for command in work_context["operations"]["mac_cli"])
    assert any("mac project list" in command for command in work_context["operations"]["mac_cli"])
    assert any("mac-hermes work-context" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes runtime-proof" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes tasks" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes projects" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes create-project" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes project-items" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes claim-next" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes command-audit" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes web-search" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("mac-hermes agents" in command for command in work_context["operations"]["mac_hermes_cli"])
    assert any("hgmac fleets create" in command for command in work_context["operations"]["hgmac_cli"])
    assert any("hgmac tasks create" in command for command in work_context["operations"]["hgmac_cli"])
    assert any("hgmac projects create" in command for command in work_context["operations"]["hgmac_cli"])
    assert any("hgmac agents create" in command for command in work_context["operations"]["hgmac_cli"])
    assert work_context["operations"]["dashboard"]["entrypoint"] == "/ui"
    assert {"work", "projects", "map", "fleets", "agents", "tasks", "hermes", "observability"} <= set(
        work_context["operations"]["dashboard"]["views"]
    )
    assert {"view", "project", "task_state", "selected", "obs_subject_type", "obs_event_prefix"} <= set(
        work_context["operations"]["dashboard"]["url_state_parameters"]
    )
    assert "/ui?view=fleets&selected={fleet_id}" in work_context["operations"]["dashboard"]["deep_link_templates"]["fleets"]
    assert "/ui?view=projects&project={project}" in work_context["operations"]["dashboard"]["deep_link_templates"]["projects"]
    assert "/ui?view=work&project={project}" in work_context["operations"]["dashboard"]["deep_link_templates"]["projects"]

    runtime_proof = client.get("/hermes-instances/%s/runtime-proof" % hermes["id"]).json()
    assert runtime_proof["schema"] == "mac.hermes_runtime_proof.v1"
    assert runtime_proof["ready"] is True
    assert runtime_proof["checks"]["api_work_context_schema"] is True
    assert runtime_proof["checks"]["agent_bound_to_hermes_instance"] is True
    assert runtime_proof["checks"]["live_object_alignment_consistent"] is True
    assert runtime_proof["checks"]["runtime_markdown_contract_present"] is True
    assert runtime_proof["checks"]["runtime_session_capabilities_available"] is True
    assert runtime_proof["checks"]["first_class_object_matrix_ready"] is True
    assert runtime_proof["checks"]["dashboard_projection_available"] is True
    assert runtime_proof["checks"]["dashboard_url_state_contract_present"] is True
    assert runtime_proof["checks"]["work_context_dashboard_contract_present"] is True
    assert runtime_proof["evidence"]["work_context"]["bound_agent_ids"] == [agent["id"]]
    dashboard_url_contract = runtime_proof["evidence"]["ui"]["dashboard_url_contract"]
    assert dashboard_url_contract["schema"] == "mac.hermes.dashboard_url_contract.v1"
    assert dashboard_url_contract["ready"] is True
    assert dashboard_url_contract["entrypoint"] == "/ui"
    assert any(
        url.startswith("/ui?view=fleets&selected=")
        for url in dashboard_url_contract["object_deep_links"]["fleets"]["samples"]
    )
    assert "/ui?view=projects&project=nanolang" in dashboard_url_contract["object_deep_links"]["projects"]["samples"]
    assert "/ui?view=work&project=nanolang" in dashboard_url_contract["object_deep_links"]["projects"]["samples"]
    assert any(
        url.startswith("/ui?view=agents&selected=%s" % agent["id"])
        for url in dashboard_url_contract["object_deep_links"]["agents"]["samples"]
    )
    assert any(
        url.startswith("/ui?view=work&selected=")
        for url in dashboard_url_contract["object_deep_links"]["tasks"]["samples"]
    )
    assert runtime_proof["evidence"]["ui"]["dashboard_operation_contract"]["entrypoint"] == "/ui"
    live_alignment = runtime_proof["evidence"]["live_alignment"]
    assert live_alignment["schema"] == "mac.hermes.live_object_alignment.v1"
    assert live_alignment["ready"] is True
    assert live_alignment["tasks"]["live_count"] == 2
    assert set(live_alignment["tasks"]["work_context_ids"]) == {dependency["id"], task["id"]}
    assert live_alignment["projects"]["work_context_names"] == ["nanolang", "c26"]
    assert live_alignment["fleets"]["ready"] is True
    assert live_alignment["agents"]["ready"] is True
    assert "get_runtime_proof" in runtime_proof["evidence"]["api"]["operation_names"]
    assert "list_tasks" in runtime_proof["evidence"]["api"]["task_operation_names"]
    assert "list_projects" in runtime_proof["evidence"]["api"]["project_operation_names"]
    assert "list_fleets" in runtime_proof["evidence"]["api"]["fleet_operation_names"]
    assert "list_project_items" in runtime_proof["evidence"]["api"]["project_operation_names"]
    assert "list_agents" in runtime_proof["evidence"]["api"]["agent_operation_names"]
    assert runtime_proof["evidence"]["first_class_objects"]["fleets"]["ready"] is True
    assert runtime_proof["evidence"]["first_class_objects"]["tasks"]["ready"] is True
    assert runtime_proof["evidence"]["first_class_objects"]["projects"]["ready"] is True
    assert runtime_proof["evidence"]["first_class_objects"]["agents"]["ready"] is True
    degraded_runtime_proof = cp.hermes_runtime_proof(
        hermes["id"],
        hermes_startup={
            "task_project_runtime": {
                "required": True,
                "ready": True,
                "hermes_instance_id": hermes["id"],
                "prompt_bridge": {"required": True, "present": True},
                "markdown_contract": {"ready": True, "missing_snippets": []},
                "first_class_object_names": ["fleets", "tasks", "projects", "agents"],
                "session_capability_names": [
                    "mac_api",
                    "mac_cli",
                    "mac_hermes_cli",
                    "shell_execution",
                    "workspace_file_access",
                    "hgmac_agent_ops_cli",
                    "beads_issue_tracker",
                    "git_source_control",
                    "quality_gate",
                    "hermes_oneshot_executor",
                    "command_audit",
                    "web_search",
                ],
                "session_capability_availability": {
                    "ready": False,
                    "missing": ["mac_cli"],
                },
            },
        },
    )
    assert degraded_runtime_proof["ready"] is False
    assert degraded_runtime_proof["checks"]["runtime_first_class_object_model_declared"] is True
    assert degraded_runtime_proof["checks"]["runtime_session_capabilities_available"] is False
    assert "runtime_session_capabilities_available" in degraded_runtime_proof["missing"]
    posted_runtime_proof = client.post(
        "/hermes-instances/%s/runtime-proof" % hermes["id"],
        json={
            "hermes_startup": {
                "task_project_runtime": {
                    "status": "agent_submitted_ready",
                    "required": True,
                    "ready": True,
                    "hermes_instance_id": hermes["id"],
                    "prompt_bridge": {"required": True, "present": True},
                    "markdown_contract": {"ready": True, "missing_snippets": []},
                    "first_class_object_names": ["fleets", "tasks", "projects", "agents"],
                    "session_capability_names": [
                        "mac_api",
                        "mac_cli",
                        "mac_hermes_cli",
                        "shell_execution",
                        "workspace_file_access",
                        "hgmac_agent_ops_cli",
                        "beads_issue_tracker",
                        "git_source_control",
                        "quality_gate",
                        "hermes_oneshot_executor",
                        "command_audit",
                        "web_search",
                    ],
                    "session_capability_availability": {
                        "ready": True,
                        "missing": [],
                    },
                },
            },
        },
    ).json()
    assert posted_runtime_proof["ready"] is True
    assert posted_runtime_proof["checks"]["runtime_first_class_object_model_declared"] is True
    assert posted_runtime_proof["evidence"]["hermes_runtime"]["hermes_instance_id"] == hermes["id"]
    assert "hermes_oneshot_executor" in posted_runtime_proof["evidence"]["first_class_objects"]["tasks"]["runtime_capabilities"]
    assert "hermes_oneshot_executor" in posted_runtime_proof["evidence"]["hermes_runtime"]["session_capability_names"]

    active_only = client.get(
        "/hermes-instances/%s/work-context?include_completed=false&task_limit=1" % hermes["id"]
    ).json()
    assert active_only["task_limit"] == 1
    assert active_only["task_truncated"] is True

    state = client.get("/dashboard/state").json()
    assert state["hermes_work_contexts"][hermes["id"]]["projects"][0]["project"] == "nanolang"
    assert state["hermes_runtime_proofs"][hermes["id"]]["ready"] is True
    assert (
        state["hermes_runtime_proofs"][hermes["id"]]["evidence"]["ui"]["dashboard_source"]
        == "agent_submitted_runtime_proof"
    )
    assert (
        state["hermes_runtime_proofs"][hermes["id"]]["evidence"]["hermes_runtime"]["status"]
        == "agent_submitted_ready"
    )


def test_workflow_runs_route_is_not_shadowed_by_workflow_slug_route():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    response = client.get("/workflows/runs")

    assert response.status_code == 200
    assert response.json() == []


def test_default_review_tick_requires_admin_not_write():
    """mac-iez: /reviews/default/tick is the closest thing to an
    auto-merge button in an autonomous swarm. A `write`-scope token —
    same as a task author — must NOT be able to flush every reviewable
    task to COMPLETED. Admin only."""
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={"writer": ["write"], "admin": ["admin"]},
        )
    )

    blocked = client.post(
        "/reviews/default/tick",
        headers={"Authorization": "Bearer writer"},
    )
    assert blocked.status_code == 403

    allowed = client.post(
        "/reviews/default/tick",
        headers={"Authorization": "Bearer admin"},
    )
    assert allowed.status_code == 200


def test_dashboard_state_exposes_session_scope_capabilities():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={"reader": ["read"], "writer": ["read", "write"], "admin": ["admin"]},
        )
    )

    reader = client.get(
        "/dashboard/state",
        headers={"Authorization": "Bearer reader"},
    ).json()
    writer = client.get(
        "/dashboard/state",
        headers={"Authorization": "Bearer writer"},
    ).json()
    admin = client.get(
        "/dashboard/state",
        headers={"Authorization": "Bearer admin"},
    ).json()

    assert reader["session"]["mode"] == "read-only"
    assert reader["session"]["can_write"] is False
    assert writer["session"]["mode"] == "read-write"
    assert writer["session"]["can_write"] is True
    assert admin["session"]["mode"] == "admin"
    assert admin["session"]["is_admin"] is True


def test_agent_scoped_tokens_can_report_only_their_openshell_telemetry():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("openshell-host")
    agent = cp.register_agent(machine.id, "openshell-agent")
    other = cp.register_agent(machine.id, "other-agent")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
                "admin-token": ["admin"],
            },
        )
    )

    own_status = client.post(
        "/agents/%s/openshell/status" % agent.id,
        headers={"Authorization": "Bearer agent-token"},
        json={"status": "active", "sandbox_id": "sandbox-own"},
    )
    assert own_status.status_code == 200, own_status.text

    spoofed_status = client.post(
        "/agents/%s/openshell/status" % other.id,
        headers={"Authorization": "Bearer agent-token"},
        json={"status": "active", "sandbox_id": "sandbox-other"},
    )
    assert spoofed_status.status_code == 403

    own_event = client.post(
        "/action-events",
        headers={"Authorization": "Bearer agent-token"},
        json={
            "agent_id": agent.id,
            "actor": agent.id,
            "action_type": "openshell.network",
            "action_name": "connect",
            "subject_type": "agent",
            "subject_id": agent.id,
            "outcome": "allowed",
        },
    )
    assert own_event.status_code == 200, own_event.text

    missing_agent = client.post(
        "/action-events",
        headers={"Authorization": "Bearer agent-token"},
        json={
            "actor": agent.id,
            "action_type": "openshell.network",
            "action_name": "connect",
        },
    )
    assert missing_agent.status_code == 403

    spoofed_event = client.post(
        "/action-events",
        headers={"Authorization": "Bearer agent-token"},
        json={
            "agent_id": other.id,
            "actor": other.id,
            "action_type": "openshell.network",
            "action_name": "connect",
        },
    )
    assert spoofed_event.status_code == 403

    admin_event = client.post(
        "/action-events",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "agent_id": other.id,
            "actor": "operator",
            "action_type": "openshell.policy",
            "action_name": "decision",
        },
    )
    assert admin_event.status_code == 200, admin_event.text


def test_auth_tokens_support_hashed_at_rest_form():
    """mac-glh0: operators can configure tokens as ``sha256:<hex>``
    digests of the live token so a leaked env file doesn't expose the
    plaintext bearer credential. The live bearer token still works at
    request time — mac hashes the incoming token and matches against
    the stored hash."""
    import hashlib

    live = "live-token-1234567890abcdef"
    hashed = "sha256:" + hashlib.sha256(live.encode("utf-8")).hexdigest()
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={hashed: ["admin"]},
        )
    )
    # The live token still authenticates against the hashed registration.
    ok = client.get("/agents", headers={"Authorization": "Bearer %s" % live})
    assert ok.status_code == 200, ok.text
    # The literal hash string does NOT authenticate.
    spoofed = client.get("/agents", headers={"Authorization": "Bearer %s" % hashed})
    assert spoofed.status_code in {401, 403}


def test_secret_routes_rate_limit_per_principal():
    """mac-xc8u: a compromised token must not be able to enumerate every
    (secret_id, audit_id) pair at line rate. The route refuses after
    the configured per-window cap is hit."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("h")
    agent = cp.register_agent(machine.id, "a", capabilities=["deploy"])
    secret = cp.create_secret(
        "tok", "v", {"capabilities": ["deploy"], "tenant_id": "t-a"}, created_by="human"
    )
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tok": {"scopes": ["secret", "write"], "agent_id": agent.id, "tenant_id": "t-a"},
            },
        )
    )
    # First request should succeed.
    first = client.post(
        "/secrets/%s/access" % secret.id,
        headers={"Authorization": "Bearer tok"},
        json={"accessor_agent_id": agent.id, "purpose": "deploy"},
    )
    assert first.status_code == 200, first.text
    # Burn through the rate-limit window with a tight loop. We don't
    # care about each call's body — only that the limiter ultimately
    # refuses one within the limit.
    seen_refusal = False
    for _ in range(200):
        r = client.post(
            "/secrets/%s/access" % secret.id,
            headers={"Authorization": "Bearer tok"},
            json={"accessor_agent_id": agent.id, "purpose": "deploy"},
        )
        if r.status_code == 403 and "rate limit" in r.json().get("detail", "").lower():
            seen_refusal = True
            break
    assert seen_refusal, "expected rate-limit refusal within the burst window"


def test_create_app_refuses_open_mode_on_non_loopback_bind(monkeypatch):
    """mac-853j: a hub that's deployed without MAC_API_TOKEN and bound
    to 0.0.0.0 used to leave /secrets/{id}/reveal open to the network.
    Refuse to start in that combination unless MAC_API_ALLOW_OPEN is set.
    """
    from mac.models import ValidationError as _VE
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKENS", raising=False)
    monkeypatch.setenv("MAC_BIND_HOST", "0.0.0.0")
    with pytest.raises(_VE, match="mac-853j"):
        create_app(control_plane=ControlPlane.in_memory(), auth_tokens={})
    # Override permits open mode (dev opt-in).
    monkeypatch.setenv("MAC_API_ALLOW_OPEN", "1")
    create_app(control_plane=ControlPlane.in_memory(), auth_tokens={})
    # Loopback bind also fine without tokens.
    monkeypatch.delenv("MAC_API_ALLOW_OPEN", raising=False)
    monkeypatch.setenv("MAC_BIND_HOST", "127.0.0.1")
    create_app(control_plane=ControlPlane.in_memory(), auth_tokens={})


def test_secret_routes_bind_actor_and_tenant_to_principal():
    """mac-k30g + mac-01g0: an agent-bound token cannot reveal secrets
    as another agent, and a tenant-bound token cannot reach a secret
    scoped to a different tenant.
    """
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("h")
    agent_a = cp.register_agent(machine.id, "a", capabilities=["deploy"])
    agent_b = cp.register_agent(machine.id, "b", capabilities=["deploy"])
    # Seed a secret scoped to tenant-a (no agent_id binding).
    secret = cp.create_secret(
        "tok",
        "supersecret",
        {"capabilities": ["deploy"], "tenant_id": "tenant-a"},
        created_by="human",
    )
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                # Token bound to agent_a, scopes secret + write.
                "tok-a": {"scopes": ["secret", "write"], "agent_id": agent_a.id, "tenant_id": "tenant-a"},
                # Token bound to agent_b, also scoped to tenant-a — but agent_b is not the secret scope target.
                "tok-b": {"scopes": ["secret", "write"], "agent_id": agent_b.id, "tenant_id": "tenant-a"},
                # Tenant-b token: same secret-scope, different tenant.
                "tok-c": {"scopes": ["secret", "write"], "agent_id": agent_a.id, "tenant_id": "tenant-b"},
                "admin": ["admin"],
            },
        )
    )

    # tok-a requesting secret access AS agent_a is allowed.
    ok = client.post(
        "/secrets/%s/access" % secret.id,
        headers={"Authorization": "Bearer tok-a"},
        json={"accessor_agent_id": agent_a.id, "purpose": "deploy"},
    )
    assert ok.status_code == 200, ok.text

    # tok-b trying to pose as agent_a is refused.
    blocked = client.post(
        "/secrets/%s/access" % secret.id,
        headers={"Authorization": "Bearer tok-b"},
        json={"accessor_agent_id": agent_a.id, "purpose": "deploy"},
    )
    assert blocked.status_code == 403, blocked.text

    # tok-c (different tenant) is refused on the tenant gate.
    cross = client.post(
        "/secrets/%s/access" % secret.id,
        headers={"Authorization": "Bearer tok-c"},
        json={"accessor_agent_id": agent_a.id, "purpose": "deploy"},
    )
    assert cross.status_code == 403, cross.text


def test_agent_id_path_routes_bind_to_principal():
    """mac-wcfy: heartbeat/claim-next/command-audit took agent_id from
    the URL with no check that the token represents that agent. A
    bound agent token must not be able to act as a peer."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("h")
    agent_a = cp.register_agent(machine.id, "a", capabilities=["python"])
    agent_b = cp.register_agent(machine.id, "b", capabilities=["python"])
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tok-a": {"scopes": ["agent"], "agent_id": agent_a.id},
                "admin": ["admin"],
            },
        )
    )
    # heartbeat as self: ok
    ok = client.post(
        "/agents/%s/heartbeat" % agent_a.id,
        headers={"Authorization": "Bearer tok-a"},
        json={},
    )
    assert ok.status_code == 200
    # heartbeat as peer: refused
    blocked = client.post(
        "/agents/%s/heartbeat" % agent_b.id,
        headers={"Authorization": "Bearer tok-a"},
        json={},
    )
    assert blocked.status_code == 403, blocked.text
    # command-audit as peer: refused
    blocked = client.post(
        "/agents/%s/command-audit" % agent_b.id,
        headers={"Authorization": "Bearer tok-a"},
        json={"phase": "started", "argv": ["/bin/true"], "cwd": "/tmp"},
    )
    assert blocked.status_code == 403, blocked.text
    # admin can act as anyone
    ok = client.post(
        "/agents/%s/heartbeat" % agent_b.id,
        headers={"Authorization": "Bearer admin"},
        json={},
    )
    assert ok.status_code == 200


def test_agentbus_repo_update_refuses_fleet_wide_without_admin():
    """mac-si4l: /agentbus/repo-update is a fleet-wide restart primitive.
    A non-admin token with agent binding must not be able to broadcast
    restart=true to every peer.
    """
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("h")
    agent_a = cp.register_agent(machine.id, "a", capabilities=["ops"])
    agent_b = cp.register_agent(machine.id, "b", capabilities=["ops"])  # peer to be spammed
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tok-a": {"scopes": ["agent"], "agent_id": agent_a.id},
                "admin": ["admin"],
            },
        )
    )
    # all_agents=True from a non-admin token: refused
    blocked = client.post(
        "/agentbus/repo-update",
        headers={"Authorization": "Bearer tok-a"},
        json={
            "sender_agent_id": agent_a.id,
            "all_agents": True,
            "restart": True,
        },
    )
    assert blocked.status_code == 403, blocked.text
    # Explicitly enumerating peers is the same privileged operation as
    # all_agents=True; a compromised agent token must not bypass the admin gate.
    blocked = client.post(
        "/agentbus/repo-update",
        headers={"Authorization": "Bearer tok-a"},
        json={
            "sender_agent_id": agent_a.id,
            "recipient_agent_ids": [agent_a.id, agent_b.id],
            "restart": True,
        },
    )
    assert blocked.status_code == 403, blocked.text
    # Self-directed update remains available to the bound agent token.
    ok = client.post(
        "/agentbus/repo-update",
        headers={"Authorization": "Bearer tok-a"},
        json={
            "sender_agent_id": agent_a.id,
            "recipient_agent_ids": [agent_a.id],
            "restart": True,
            "request_id": "self-update",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["count"] == 1
    # Admin can fan out
    ok = client.post(
        "/agentbus/repo-update",
        headers={"Authorization": "Bearer admin"},
        json={
            "sender_agent_id": agent_a.id,
            "all_agents": True,
            "restart": False,
        },
    )
    assert ok.status_code == 200
    published = ok.json()
    assert published["schema"] == "mac.agentbus.repo_update_publish.v1"
    assert published["count"] == 2
    assert {stream["recipient_agent_id"] for stream in published["streams"]} == {
        agent_a.id,
        agent_b.id,
    }


def test_evidence_created_by_bound_to_principal():
    """mac-rreh: ``created_by`` on /tasks/{id}/evidence used to be
    self-asserted in the payload. An agent-bound token must not be
    able to forge evidence under another agent's name."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("h")
    agent_a = cp.register_agent(machine.id, "a", capabilities=["python"])
    agent_b = cp.register_agent(machine.id, "b", capabilities=["python"])
    task = cp.create_task("t", required_capabilities=["python"])
    cp.claim_task(task.id, agent_a.id)
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tok-a": {"scopes": ["agent", "write"], "agent_id": agent_a.id},
            },
        )
    )
    # Forging as agent_b is refused
    blocked = client.post(
        "/tasks/%s/evidence" % task.id,
        headers={"Authorization": "Bearer tok-a"},
        json={
            "kind": "log",
            "uri": "file:///tmp/e",
            "summary": "ok",
            "created_by": agent_b.id,
        },
    )
    assert blocked.status_code == 403, blocked.text
    # Honest evidence as agent_a is OK
    ok = client.post(
        "/tasks/%s/evidence" % task.id,
        headers={"Authorization": "Bearer tok-a"},
        json={
            "kind": "log",
            "uri": "file:///tmp/e",
            "summary": "ok",
            "created_by": agent_a.id,
        },
    )
    assert ok.status_code == 200, ok.text


def test_attestation_key_rotation_requires_global_fleet_token():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={
                "tenant": {"scopes": ["write"], "tenant_id": "tenant-a"},
                "admin": ["admin"],
            },
        )
    )
    machine = client.post(
        "/machines",
        headers={"Authorization": "Bearer admin"},
        json={"hostname": "host-1"},
    ).json()
    agent = client.post(
        "/agents",
        headers={"Authorization": "Bearer admin"},
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    original_key = agent["attestation_key"]

    blocked = client.post(
        "/agents/%s/attestation-key/rotate" % agent["id"],
        headers={"Authorization": "Bearer tenant"},
    )
    assert blocked.status_code == 403

    rotated = client.post(
        "/agents/%s/attestation-key/rotate" % agent["id"],
        headers={"Authorization": "Bearer admin"},
    )
    assert rotated.status_code == 200
    rotated_key = rotated.json()["attestation_key"]
    assert rotated_key
    assert rotated_key != original_key


def test_attestation_key_verify_uses_challenge_response():
    cp = ControlPlane.in_memory()
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tenant": {"scopes": ["write"], "tenant_id": "tenant-a"},
                "admin": ["admin"],
            },
        )
    )
    machine = client.post(
        "/machines",
        headers={"Authorization": "Bearer admin"},
        json={"hostname": "host-1"},
    ).json()
    agent = client.post(
        "/agents",
        headers={"Authorization": "Bearer admin"},
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    challenge = {
        "schema": "mac.agent_attestation_challenge.v1",
        "purpose": "attestation-key-healthcheck",
        "agent_id": agent["id"],
        "nonce": "test-nonce",
    }

    blocked = client.post(
        "/agents/%s/attestation-key/verify" % agent["id"],
        headers={"Authorization": "Bearer tenant"},
        json={"challenge": challenge, "signature": "v1:bad"},
    )
    assert blocked.status_code == 403

    valid = client.post(
        "/agents/%s/attestation-key/verify" % agent["id"],
        headers={"Authorization": "Bearer admin"},
        json={
            "challenge": challenge,
            "signature": sign_verification_manifest(agent["attestation_key"], challenge),
        },
    )
    assert valid.status_code == 200
    assert valid.json()["valid"] is True

    invalid = client.post(
        "/agents/%s/attestation-key/verify" % agent["id"],
        headers={"Authorization": "Bearer admin"},
        json={"challenge": challenge, "signature": "v1:wrong"},
    )
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False


def test_fastapi_can_require_scoped_bearer_tokens():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={
                "writer": ["write"],
                "reader": ["read"],
            },
        )
    )

    assert client.get("/health").status_code == 200
    assert client.post("/machines", json={"hostname": "host-1"}).status_code == 403
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer reader"},
        json={"hostname": "host-1"},
    ).status_code == 403
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer writer"},
        json={"hostname": "host-1"},
    ).status_code == 200
    assert client.get("/machines", headers={"Authorization": "Bearer reader"}).status_code == 200


def test_deploy_scope_is_required_for_runtimes_environments_and_rollouts():
    cp = ControlPlane.in_memory()
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "writer": ["write"],
                "deployer": ["write", "deploy"],
            },
        )
    )

    # /runtimes requires deploy scope, not write.
    assert client.post(
        "/runtimes",
        headers={"Authorization": "Bearer writer"},
        json={"name": "rt", "manifest": {"image": "python:3.12@sha256:abc123"}, "created_by": "ops"},
    ).status_code == 403
    assert client.post(
        "/runtimes",
        headers={"Authorization": "Bearer deployer"},
        json={"name": "rt", "manifest": {"image": "python:3.12@sha256:abc123"}, "created_by": "ops"},
    ).status_code == 200
    runtime = cp.get_runtime("rt")
    machine = cp.register_machine("delta-host")
    agent = cp.register_agent(machine.id, "delta-worker", capabilities=["python"])
    task = cp.create_task("delta task", project="mac")
    delta_payload = {
        "task_id": task.id,
        "agent_id": agent.id,
        "package_manager": "pip",
        "commands": ["python -m venv .venv", "./.venv/bin/pip install rich==13.7.1"],
        "added_dependencies": ["rich==13.7.1"],
        "reason": "task-local wheel",
        "base_runtime_id": runtime.id,
        "lockfile_path": "requirements.txt",
        "lockfile_digest": "sha256:" + "d" * 64,
    }
    assert client.post(
        "/runtime-deltas",
        headers={"Authorization": "Bearer writer"},
        json=delta_payload,
    ).status_code == 403
    delta_response = client.post(
        "/runtime-deltas",
        headers={"Authorization": "Bearer deployer"},
        json=delta_payload,
    )
    assert delta_response.status_code == 200
    delta_id = delta_response.json()["id"]
    assert client.post(
        "/runtime-deltas/%s/validate" % delta_id,
        headers={"Authorization": "Bearer deployer"},
        json={"actor": "ops"},
    ).json()["status"] == "validated"

    # /environments also requires deploy.
    tenant = cp.register_tenant("team-a")
    assert client.post(
        "/environments",
        headers={"Authorization": "Bearer writer"},
        json={"name": "prod", "tenant_id": tenant.id},
    ).status_code == 403
    assert client.post(
        "/environments",
        headers={"Authorization": "Bearer deployer"},
        json={"name": "prod", "tenant_id": tenant.id},
    ).status_code == 200


def test_tenant_bound_token_cannot_cross_tenants_or_touch_global_fleet():
    cp = ControlPlane.in_memory()
    tenant_a = cp.register_tenant("alpha")
    tenant_b = cp.register_tenant("beta")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "alpha-writer": {
                    "scopes": ["write", "deploy"],
                    "tenant_id": tenant_a.id,
                },
                "admin": ["admin"],
            },
        )
    )

    # Same-tenant write succeeds.
    persona_ok = client.post(
        "/personas",
        headers={"Authorization": "Bearer alpha-writer"},
        json={
            "tenant_id": tenant_a.id,
            "name": "Rocky",
            "soul_ref": "h://a/r/SOUL.md",
            "memory_scope": "h://a/r/mem",
        },
    )
    assert persona_ok.status_code == 200

    # Cross-tenant write is refused.
    persona_xtenant = client.post(
        "/personas",
        headers={"Authorization": "Bearer alpha-writer"},
        json={
            "tenant_id": tenant_b.id,
            "name": "Boris",
            "soul_ref": "h://b/r/SOUL.md",
            "memory_scope": "h://b/r/mem",
        },
    )
    assert persona_xtenant.status_code == 403

    # Tenant-bound principal cannot create a new tenant.
    assert client.post(
        "/tenants",
        headers={"Authorization": "Bearer alpha-writer"},
        json={"name": "gamma"},
    ).status_code == 403

    # Tenant-bound principal cannot register a global-fleet machine.
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer alpha-writer"},
        json={"hostname": "host-1"},
    ).status_code == 403

    # Admin still can.
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer admin"},
        json={"hostname": "host-1"},
    ).status_code == 200

    # Tasks created by the tenant-bound principal get tenant_id stamped.
    task = client.post(
        "/tasks",
        headers={"Authorization": "Bearer alpha-writer"},
        json={"title": "scoped task"},
    ).json()
    assert task["metadata"]["origin"]["tenant_id"] == tenant_a.id


def test_unknown_bearer_token_is_rejected_with_constant_time_compare():
    # We can't assert timing directly, but we can verify the wrong-token path
    # returns 403 and that a token that differs only in the final byte is
    # still rejected (rather than partially matched).
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={"correct-token-32chars-xxxxxxxxx": ["admin"]},
        )
    )
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer correct-token-32chars-xxxxxxxxy"},
        json={"hostname": "h"},
    ).status_code == 403
    assert client.post(
        "/machines",
        headers={"Authorization": "Bearer correct-token-32chars-xxxxxxxxx"},
        json={"hostname": "h"},
    ).status_code == 200


def test_registration_payloads_are_size_capped():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    huge_labels = {"k": "x" * (64 * 1024 + 1)}
    response = client.post("/machines", json={"hostname": "h", "labels": huge_labels})
    assert response.status_code == 400
    assert "machine.labels exceeds" in response.json()["detail"]

    huge_metadata = {"blob": "y" * (64 * 1024 + 1)}
    task_response = client.post("/tasks", json={"title": "t", "metadata": huge_metadata})
    assert task_response.status_code == 400


def test_agent_registration_observes_created_agent_in_existing_fleet():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    fleet = client.post("/fleets", json={"name": "mac"}).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "rocky",
            "agent_id": "agent_rocky",
            "hermes_instance_id": None,
            "fleet_id": fleet["id"],
            "capabilities": ["python", "review"],
            "resources": {"workspace": "/home/rocky/.mac"},
        },
    ).json()

    assert agent["id"] == "agent_rocky"
    refreshed = client.get("/fleets/%s" % fleet["id"]).json()
    assert refreshed["agent_ids"] == []
    assert refreshed["observed_agent_ids"] == ["agent_rocky"]
    assert refreshed["unmanaged_agent_ids"] == ["agent_rocky"]


def test_dashboard_exposes_and_updates_hermes_fleet_config_surface(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model: local-model\nplugins:\n  enabled:\n    - local-plugin\n",
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text("OPENAI_API_KEY=local-secret\n", encoding="utf-8")
    registry_path = tmp_path / "fleets.yaml"
    registry_path.write_text(
        """
version: 1
fleets:
  classic:
    fleet_name: classic
    hub_agent: classic
    defaults:
      hermes:
        gateway_model: openai/gpt-5
        gateway_provider: custom
        gateway_base_url: https://gateway.example.test/v1
        config:
          model: fleet-model
        env:
          OPENAI_API_KEY: fleet-secret
        plugins:
          enabled:
            - image_gen/nvidia
        skills:
          disabled:
            - old-skill
    agents: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(registry_path))

    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "agent_id": "agent_worker"},
    ).json()
    fleet = client.post(
        "/fleets",
        json={"name": "classic", "agent_ids": [agent["id"]]},
    ).json()

    state = client.get("/dashboard/state").json()
    surfaces = state["hermes_config_surfaces"]
    assert len(surfaces) == 1
    surface = surfaces[0]
    assert surface["schema"] == "mac.hermes_config_surface.v1"
    assert surface["fleet_name"] == "classic"
    assert surface["agent_count"] == 1
    assert surface["runtime"]["gateway_model"] == "openai/gpt-5"
    model_field = next(item for item in surface["config_fields"] if item["key"] == "model")
    assert model_field["value"] == "fleet-model"
    assert model_field["desired"] is True
    openai_key = next(item for item in surface["env_vars"] if item["name"] == "OPENAI_API_KEY")
    assert openai_key["desired"] is True
    assert openai_key["redacted_value"].startswith("<redacted:")
    assert "fleet-secret" not in json.dumps(surface)
    assert "plugins" in surface and "skills" in surface

    response = client.put(
        "/dashboard/hermes/fleets/%s/config-surface" % fleet["id"],
        json={
            "runtime": {"gateway_model": "openai/gpt-5-mini"},
            "config": {"tools.web_search.enabled": True},
            "env": {"NVIDIA_API_KEY": "nvidia-secret"},
            "plugins": {"enabled": ["image_gen/nvidia"], "disabled": ["local-plugin"]},
            "skills": {"disabled": ["imagegen"]},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["registry_updated"] is True
    assert body["local_apply"]["applied"] is True

    published = client.post(
        "/dashboard/hermes/fleets/%s/config-surface/apply" % fleet["id"],
        json={"sender_agent_id": agent["id"], "request_id": "req-hermes-apply"},
    )
    assert published.status_code == 200, published.text
    apply_body = published.json()
    assert apply_body["schema"] == "mac.dashboard.hermes_config_apply.v1"
    assert apply_body["count"] == 1
    assert "nvidia-secret" not in json.dumps(apply_body["payload_redacted"])
    stream = apply_body["streams"][0]
    assert stream["topic"] == HERMES_CONFIG_APPLY_TOPIC
    assert stream["content_type"] == HERMES_CONFIG_APPLY_CONTENT_TYPE
    chunks = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": agent["id"]},
    ).json()
    assert chunks[0]["payload"]["schema"] == "mac.agentbus.hermes_config_apply.v1"
    assert chunks[0]["payload"]["payload"]["config"]["tools"]["web_search"]["enabled"] is True
    assert chunks[0]["payload"]["request_id"] == "req-hermes-apply"

    refreshed = client.get("/dashboard/state").json()["hermes_config_surfaces"][0]
    assert refreshed["apply_status"][0]["state"] == "sent"

    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    desired = registry["fleets"]["classic"]["defaults"]["hermes"]
    assert desired["gateway_model"] == "openai/gpt-5-mini"
    assert desired["config"]["tools"]["web_search"]["enabled"] is True
    assert desired["env"]["NVIDIA_API_KEY"] == "nvidia-secret"
    assert desired["plugins"]["disabled"] == ["local-plugin"]
    assert desired["skills"]["disabled"] == ["imagegen"]

    local_config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert local_config["tools"]["web_search"]["enabled"] is True
    assert local_config["plugins"]["disabled"] == ["local-plugin"]
    assert local_config["skills"]["disabled"] == ["imagegen"]
    local_env = parse_env_text((hermes_home / ".env").read_text(encoding="utf-8"))
    assert local_env["NVIDIA_API_KEY"] == "nvidia-secret"


def test_fleet_observed_agents_endpoint_does_not_change_configured_members():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    configured = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "configured", "agent_id": "agent_configured"},
    ).json()
    unmanaged = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "unmanaged", "agent_id": "agent_unmanaged"},
    ).json()
    fleet = client.post(
        "/fleets",
        json={"name": "mac", "agent_ids": [configured["id"]]},
    ).json()

    observed = client.post(
        "/fleets/mac/observed-agents",
        json={"agent_id": unmanaged["id"], "source": "test"},
    )

    assert observed.status_code == 200, observed.text
    body = observed.json()
    assert body["agent_ids"] == [configured["id"]]
    assert body["observed_agent_ids"] == [unmanaged["id"]]
    assert body["unmanaged_agent_ids"] == [unmanaged["id"]]
    assert client.get("/fleets/%s" % fleet["id"]).json()["agent_ids"] == [configured["id"]]


def test_fastapi_serves_dashboard_shell_without_api_token():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={"reader": ["read"]},
        )
    )

    ui_response = client.get("/ui")
    assert ui_response.status_code == 200
    assert "MAC Control Plane" in ui_response.text
    assert "/ui/assets/app.js?v=" in ui_response.text
    assert 'data-view="observability"' in ui_response.text

    script_response = client.get("/ui/assets/app.js")
    assert script_response.status_code == 200
    assert "requestJSON" in script_response.text
    assert "data-action=\"dispatchTick\"" in script_response.text
    assert "renderObservability" in script_response.text
    assert "/observability/stream" in script_response.text
    assert "Unified Events" in script_response.text
    assert "obs_subject_type" in script_response.text

    assert client.get("/agents").status_code == 403
    assert client.get("/agents", headers={"Authorization": "Bearer reader"}).status_code == 200


def test_fastapi_exposes_dashboard_read_models_and_redacts_secret_values():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "worker",
            "capabilities": ["python", "deploy"],
            "resources": {"capacity": 2},
        },
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "Dashboard task", "required_capabilities": ["python"]},
    ).json()
    runtime = client.post(
        "/runtimes",
        json={
            "name": "dashboard-runtime",
            "manifest": {"image": "python:3.12@sha256:abc123"},
            "created_by": "human",
        },
    ).json()
    runtime_delta = client.post(
        "/runtime-deltas",
        json={
            "task_id": task["id"],
            "agent_id": agent["id"],
            "package_manager": "pip",
            "commands": ["python -m venv .venv", "./.venv/bin/pip install rich==13.7.1"],
            "added_dependencies": ["rich==13.7.1"],
            "reason": "dashboard dependency proposal",
            "base_runtime_id": runtime["id"],
            "lockfile_path": "requirements.txt",
            "lockfile_digest": "sha256:" + "e" * 64,
        },
    ).json()
    project = client.post("/projects", json={"name": "Dashboard Project"}).json()
    fleet = client.post(
        "/fleets",
        json={"name": "Dashboard Fleet", "agent_ids": [agent["id"]]},
    ).json()
    secret = client.post(
        "/secrets",
        json={
            "name": "dashboard-token",
            "value": "never-render-this",
            "scopes": {"capabilities": ["deploy"]},
            "created_by": "human",
        },
    ).json()
    handle = client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "dashboard"},
    ).json()

    state = client.get("/dashboard/state").json()
    assert state["server_time"].endswith("+00:00")
    assert state["updated_at"] == state["server_time"]
    assert state["overview"]["counts"]["agents"] == 1
    assert state["dispatch"]["open_task_count"] == 1
    assert state["session"]["mode"] == "admin"
    assert state["session"]["can_write"] is True
    assert state["dispatch"]["tasks"][0]["eligible_agent_count"] == 1
    assert state["tasks"][0]["task"]["id"] == task["id"]
    assert state["hermes_startup"]["operator_health"]["status"] in {"healthy", "degraded"}
    assert state["secrets"][0]["value"] == "***REDACTED***"
    assert "never-render-this" not in str(state)
    assert state["secret_audits"][0]["id"] == handle["audit_id"]
    assert state["runtime_deltas"][0]["id"] == runtime_delta["id"]
    assert state["runtime_deltas"][0]["status"] == "proposed"
    assert "observability" in state
    assert state["observability"]["counts"]["events"] >= 1
    assert "openshell_policies" in state
    assert "openshell_policy_versions" in state
    assert "openshell_agent_statuses" in state
    assert state["openshell_agent_statuses"][0]["agent_id"] == agent["id"]
    assert state["openshell_agent_statuses"][0]["effective"]["fail_closed"] is False
    assert "events" in state
    event_subjects = {event["subject_type"] for event in state["events"]}
    assert {"task", "agent", "project", "fleet"} <= event_subjects
    assert any(event["subject_id"] == project["id"] for event in state["events"])
    assert any(event["subject_id"] == fleet["id"] for event in state["events"])
    assert "notifications" in state
    assert "integration_findings" in state
    assert "integration_observations" in state
    assert "roles" in state
    assert "provisioning_requests" in state
    assert "workflows" in state
    assert "workflow_runs" in state
    assert "agentbus_streams" in state
    assert "artifacts" in state
    assert "bridge_items" in state
    assert "beads_repositories" in state
    assert "project_summaries" in state
    assert "swarm_summary" in state
    assert "fleets" in state
    unassigned = next(item for item in state["project_summaries"] if item["project"] == "unassigned")
    assert unassigned["ready_count"] == 1
    assert state["swarm_summary"]["agent_total"] == 1
    assert "memory_records" in state
    assert "nap_schedules" in state
    assert "nap_runs" in state

    streamed = client.get("/dashboard/stream", params={"timeout_seconds": 0})
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("application/x-ndjson")
    dashboard_lines = [json.loads(line) for line in streamed.text.splitlines() if line]
    assert dashboard_lines
    assert dashboard_lines[0]["server_time"].endswith("+00:00")
    assert dashboard_lines[0]["updated_at"] == dashboard_lines[0]["server_time"]

    timeline = client.get("/dashboard/tasks/%s/timeline" % task["id"]).json()
    assert timeline["task"]["title"] == "Dashboard task"
    assert timeline["summary"]["state"] == "open"

    agent_detail = client.get("/dashboard/agents/%s" % agent["id"]).json()
    assert agent_detail["availability"]["eligible"] is True


def test_dashboard_state_caps_high_volume_task_and_message_data():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    task = client.post("/tasks", json={"title": "High volume task"}).json()

    for index in range(75):
        cp.add_evidence(
            task["id"],
            "test",
            "artifact://evidence/%03d" % index,
            "evidence %03d" % index,
            agent["id"],
            sync_beads=False,
        )
    for index in range(250):
        cp.send_message(
            "dispatcher",
            agent["id"],
            "status_update",
            {"status": "update-%03d" % index},
        )

    state = client.get("/dashboard/state").json()
    detail = next(item for item in state["tasks"] if item["task"]["id"] == task["id"])

    assert len(detail["history"]) == 50
    assert len(detail["evidence"]) == 25
    assert detail["history_limited_to"] == 50
    assert detail["evidence_limited_to"] == 25
    assert len(state["messages"]) == 200

    timeline = client.get("/dashboard/tasks/%s/timeline" % task["id"]).json()
    assert len(timeline["history"]) > len(detail["history"])
    assert len(timeline["evidence"]) == 75
    assert len(client.get("/messages").json()) == 250
    assert len(client.get("/messages?limit=10").json()) == 10


def test_dashboard_workflow_planner_previews_and_accepts_task_chain():
    cp = ControlPlane.in_memory()
    app = create_app(control_plane=cp)

    def planner(request):
        assert request["goal"] == "ship c26 desktop"
        return {
            "plan_id": "plan_c26_desktop",
            "nodes": [
                {
                    "node_id": "plan",
                    "title": "Plan c26 desktop",
                    "description": "Define the implementation slices and review gates.",
                    "required_capabilities": ["planning"],
                },
                {
                    "node_id": "implement",
                    "title": "Implement c26 desktop",
                    "description": "Build the first interactive desktop UI.",
                    "required_capabilities": ["python", "review"],
                    "depends_on": ["plan"],
                    "priority": 3,
                },
            ],
        }

    app.state.workflow_plan_model = planner
    client = TestClient(app)
    project = client.post(
        "/projects",
        json={"name": "c26", "description": "C26 emulator"},
    )
    assert project.status_code == 200

    preview = client.post(
        "/dashboard/workflow-plan/preview",
        json={
            "goal": "ship c26 desktop",
            "project": "c26",
            "required_capabilities": ["python"],
            "max_tasks": 4,
            "context": {"surface": "dashboard"},
        },
    )
    assert preview.status_code == 200, preview.text
    draft = preview.json()
    assert draft["schema"] == "mac.dashboard.workflow_plan.v1"
    assert draft["source"] == "injected"
    assert draft["plan_id"] == "plan_c26_desktop"
    assert [node["node_id"] for node in draft["nodes"]] == ["plan", "implement"]

    draft["nodes"][1]["title"] = "Implement edited c26 desktop"
    accept = client.post(
        "/dashboard/workflow-plan/accept",
        json={
            "goal": draft["goal"],
            "project": draft["project"],
            "plan_id": draft["plan_id"],
            "nodes": draft["nodes"],
            "actor": "human",
            "metadata": {"accepted_from": "ui"},
        },
    )
    assert accept.status_code == 200, accept.text
    result = accept.json()
    assert result["schema"] == "mac.dashboard.workflow_plan_accept.v1"
    assert result["project"] == "c26"
    assert len(result["created"]) == 2

    first_id = result["node_task_ids"]["plan"]
    second_id = result["node_task_ids"]["implement"]
    first = cp.get_task(first_id).to_dict()
    second = cp.get_task(second_id).to_dict()
    assert first["state"] == "open"
    assert second["title"] == "Implement edited c26 desktop"
    assert second["state"] == "blocked"
    assert second["dependencies"] == [first_id]
    assert second["metadata"]["origin"] == {
        "type": "dashboard_workflow_plan",
        "plan_id": "plan_c26_desktop",
        "node_id": "implement",
    }
    assert second["metadata"]["workflow"]["type"] == "planned_task_chain"
    assert second["metadata"]["workflow"]["depends_on_nodes"] == ["plan"]
    assert second["metadata"]["workflow_plan"] == {"accepted_from": "ui"}


def test_tasks_expose_lifecycle_timestamps_and_child_relationships():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    parent = client.post(
        "/tasks",
        json={
            "title": "Parent implementation",
            "project": "nanolang",
            "required_capabilities": ["python"],
        },
    ).json()

    assert parent["started_at"] is None
    assert parent["completed_at"] is None
    assert parent["last_updated_at"] == parent["updated_at"]

    claim = client.post(
        "/tasks/%s/claim" % parent["id"],
        params={"agent_id": agent["id"]},
    ).json()
    started = client.post(
        "/tasks/%s/start" % parent["id"],
        params={"agent_id": agent["id"]},
    ).json()
    assert started["started_at"] is not None
    assert started["completed_at"] is None
    assert started["last_updated_at"] == started["updated_at"]

    split = client.post(
        "/tasks/%s/children" % parent["id"],
        json={
            "actor": agent["id"],
            "children": [
                {
                    "title": "Implement parser child",
                    "description": "Smaller scoped parser work",
                },
                {
                    "title": "Test parser child",
                    "required_capabilities": ["python", "test"],
                },
            ],
        },
    ).json()
    child_ids = [child["id"] for child in split["children"]]

    assert split["parent"]["state"] == "blocked"
    assert split["parent"]["owner_agent_id"] is None
    assert split["parent"]["lease_id"] is None
    assert split["parent"]["dependencies"] == child_ids
    assert split["parent"]["started_at"] == started["started_at"]
    assert split["relationships"]["blocked_by"] == child_ids
    assert cp.get_lease(claim["lease"]["id"]).status == "released"

    first_child = split["children"][0]
    assert first_child["project"] == "nanolang"
    assert first_child["required_capabilities"] == ["python"]
    assert first_child["metadata"]["relationships"]["parent_task_id"] == parent["id"]
    assert first_child["metadata"]["relationships"]["blocks"] == [parent["id"]]

    detail = client.get("/tasks/%s" % parent["id"]).json()
    assert detail["task"]["last_updated_at"] == detail["task"]["updated_at"]
    assert any(event["event_type"] == "task.children_added" for event in detail["history"])

    terminal = client.post(
        "/tasks",
        json={"title": "Terminal timing", "required_capabilities": ["python"]},
    ).json()
    client.post("/tasks/%s/claim" % terminal["id"], params={"agent_id": agent["id"]})
    client.post("/tasks/%s/start" % terminal["id"], params={"agent_id": agent["id"]})
    failed = client.post(
        "/tasks/%s/transition" % terminal["id"],
        json={"target_state": "failed", "actor": agent["id"], "detail": {"reason": "test"}},
    ).json()
    assert failed["completed_at"] is not None
    assert failed["last_updated_at"] == failed["updated_at"]


def test_dashboard_exposes_service_links_with_redacted_credentials(monkeypatch):
    monkeypatch.setenv("MAC_HERMES_STARTUP_CHECK", "0")
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.internal:8090")
    monkeypatch.setenv("TOKENHUB_ADMIN_TOKEN", "secret-admin-token")
    monkeypatch.setenv("TOKENHUB_API_KEY", "secret-client-token")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "secret-qdrant-token")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.internal:3002")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "secret-firecrawl-token")
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    state = client.get("/dashboard/state").json()
    services = {item["id"]: item for item in state["service_links"]}

    assert services["tokenhub"]["auth"]["credential_pass_through"] is True
    assert services["tokenhub"]["auth"]["pass_through_url"] == "/dashboard/service-links/tokenhub/sso"
    assert services["qdrant"]["ui_url"] == "http://qdrant.internal:6333/dashboard"
    assert services["firecrawl"]["health_url"] == "http://firecrawl.internal:3002/health"
    rendered = str(state)
    assert "secret-admin-token" not in rendered
    assert "secret-client-token" not in rendered
    assert "secret-qdrant-token" not in rendered
    assert "secret-firecrawl-token" not in rendered

    response = client.get("/dashboard/service-links/tokenhub/sso", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("http://tokenhub.internal:8090/admin/v1/session/claim?")
    assert "secret-admin-token" not in location


def test_dashboard_models_large_swarm_by_project_and_limits_dispatch_candidates():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "swarm-host"}).json()
    for index in range(75):
        client.post(
            "/agents",
            json={
                "machine_id": machine["id"],
                "name": "agent-%03d" % index,
                "capabilities": ["python"],
            },
        )
    story = client.post(
        "/tasks",
        json={
            "title": "Story ready for writing",
            "project": "nanolang",
            "required_capabilities": ["python"],
        },
    ).json()
    blocker = client.post(
        "/tasks",
        json={"title": "Dependency library", "project": "libcore"},
    ).json()
    client.post(
        "/tasks",
        json={
            "title": "Story waiting on libcore",
            "project": "nanolang",
            "dependencies": [blocker["id"]],
        },
    )

    state = client.get("/dashboard/state").json()

    nanolang = next(project for project in state["project_summaries"] if project["project"] == "nanolang")
    assert nanolang["ready_count"] == 1
    assert nanolang["blocked_count"] == 1
    assert nanolang["cross_project_dependency_count"] == 1
    assert nanolang["frontier_tasks"][0]["id"] == story["id"]
    assert state["swarm_summary"]["agent_total"] == 75
    assert state["dispatch"]["tasks"][0]["candidate_count"] == 75
    assert len(state["dispatch"]["tasks"][0]["candidates"]) == 60
    assert state["dispatch"]["tasks"][0]["candidate_truncated"] is True


def test_fastapi_exposes_operator_notifications():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "host"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "notified", "required_capabilities": ["python"]},
    ).json()

    claim = client.post("/tasks/%s/claim" % task["id"], params={"agent_id": agent["id"]})
    assert claim.status_code == 200
    notifications = client.get("/notifications", params={"subject_id": task["id"]}).json()
    assert notifications[0]["event_type"] == "task.claimed"
    delivered = client.post(
        "/notifications/%s/delivered" % notifications[0]["id"],
        json={"status": "delivered"},
    ).json()
    assert delivered["status"] == "delivered"
    assert delivered["delivered_at"] is not None


def test_fastapi_exposes_agent_crud_operations():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "crud-host"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()

    shown = client.get("/agents/%s" % agent["id"])
    assert shown.status_code == 200
    assert shown.json()["name"] == "worker"

    updated = client.put(
        "/agents/%s" % agent["id"],
        json={
            "name": "worker-renamed",
            "capabilities": ["python", "ops"],
            "resources": {"cpu": 8},
            "health_status": "healthy",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "worker-renamed"
    assert updated.json()["capabilities"] == ["ops", "python"]

    bulk = client.post(
        "/agents/bulk",
        json={"agent_ids": [agent["id"]], "status": "draining"},
    )
    assert bulk.status_code == 200
    assert bulk.json()["updated_count"] == 1
    assert bulk.json()["updated"][0]["status"] == "draining"

    disabled = client.post("/agents/%s/disable" % agent["id"])
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "offline"

    deleted = client.delete("/agents/%s" % agent["id"])
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": agent["id"]}
    assert client.get("/agents/%s" % agent["id"]).status_code == 404


def test_fastapi_exposes_first_class_object_crud_e2e():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "crud-host"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()

    fleet = client.post(
        "/fleets",
        json={
            "name": "classic",
            "description": "Rocky-era fleet",
            "metadata": {"hub": "rocky"},
            "agent_ids": [agent["id"]],
        },
    )
    assert fleet.status_code == 200, fleet.text
    fleet_body = fleet.json()
    assert fleet_body["name"] == "classic"
    assert fleet_body["agent_ids"] == [agent["id"]]
    assert client.get("/fleets/classic").json()["id"] == fleet_body["id"]
    assert client.get("/fleets").json()[0]["name"] == "classic"

    fleet_update = client.put(
        "/fleets/%s" % fleet_body["id"],
        json={"status": "inactive", "agent_ids": [], "metadata": {"hub": "rocky", "vpn": "tailscale"}},
    )
    assert fleet_update.status_code == 200, fleet_update.text
    assert fleet_update.json()["status"] == "inactive"
    assert fleet_update.json()["agent_ids"] == []

    project = client.post(
        "/projects",
        json={"name": "nanolang", "description": "language repo", "metadata": {"repo": "nanolang"}},
    )
    assert project.status_code == 200, project.text
    assert project.json()["name"] == "nanolang"
    assert client.get("/projects/nanolang").json()["project"] == "nanolang"

    task = client.post(
        "/tasks",
        json={"title": "Implement parser", "project": "nanolang", "required_capabilities": ["python"]},
    )
    assert task.status_code == 200, task.text
    task_body = task.json()
    assert client.get("/tasks/%s" % task_body["id"]).json()["task"]["title"] == "Implement parser"

    task_update = client.put(
        "/tasks/%s" % task_body["id"],
        json={"title": "Implement parser v2", "priority": 7, "required_capabilities": ["python", "tests"]},
    )
    assert task_update.status_code == 200, task_update.text
    assert task_update.json()["title"] == "Implement parser v2"
    assert task_update.json()["required_capabilities"] == ["python", "tests"]

    project_update = client.put(
        "/projects/nanolang",
        json={"name": "nanolang-core", "status": "active", "metadata": {"repo": "nanolang-core"}},
    )
    assert project_update.status_code == 200, project_update.text
    assert project_update.json()["name"] == "nanolang-core"
    assert client.get("/tasks/%s" % task_body["id"]).json()["task"]["project"] == "nanolang-core"

    agent_update = client.put(
        "/agents/%s" % agent["id"],
        json={"name": "worker-2", "status": "idle", "health_status": "healthy"},
    )
    assert agent_update.status_code == 200, agent_update.text
    assert agent_update.json()["name"] == "worker-2"

    state = client.get("/dashboard/state").json()
    assert state["overview"]["counts"]["fleets"] == 1
    assert state["fleets"][0]["name"] == "classic"

    assert client.delete("/tasks/%s" % task_body["id"]).json() == {"deleted": task_body["id"]}
    assert client.delete("/projects/nanolang-core").json() == {"deleted": "nanolang-core"}
    assert client.delete("/fleets/classic").json() == {"deleted": "classic"}
    assert client.delete("/agents/%s" % agent["id"]).json() == {"deleted": agent["id"]}


def test_fastapi_exposes_workflow_drafts_preview_and_notifier_channels():
    cp = ControlPlane.in_memory()
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="quality",
        system_prompt="review carefully",
        level="ic",
    )
    client = TestClient(create_app(control_plane=cp))

    draft = client.post(
        "/workflows/drafts",
        json={
            "goal": "Check a release",
            "proposed_steps": [
                {
                    "node_key": "check_release",
                    "role_required": "qa",
                    "instructions": "Run release checks",
                    "required_capabilities": ["python"],
                }
            ],
            "questions": [{"id": "target", "prompt": "Which release?"}],
            "answers": {"target": "v1"},
        },
    )
    assert draft.status_code == 200, draft.text
    draft_body = draft.json()
    assert draft_body["status"] == "draft"

    preview = client.post("/workflows/drafts/%s/preview" % draft_body["id"], json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["tasks"][0]["node_key"] == "check_release"

    approved = client.post(
        "/workflows/drafts/%s/approve" % draft_body["id"],
        json={"slug": "release-check", "name": "Release Check"},
    )
    assert approved.status_code == 200, approved.text
    workflow = approved.json()
    assert workflow["metadata"]["draft_id"] == draft_body["id"]

    workflow_preview = client.post("/workflows/%s/preview" % workflow["id"], json={})
    assert workflow_preview.status_code == 200, workflow_preview.text
    assert workflow_preview.json()["workflow_id"] == workflow["id"]

    notifier = client.post(
        "/notifier/channels",
        json={
            "name": "ops-slack",
            "channel_type": "slack",
            "event_types": ["task.failed", "task.completed"],
            "target": {"platform": "slack"},
        },
    )
    assert notifier.status_code == 200, notifier.text
    state = client.get("/dashboard/state").json()
    assert state["workflow_drafts"]
    assert state["notifier_channels"][0]["name"] == "ops-slack"


def test_fastapi_exposes_integration_authority_ledger():
    cp = ControlPlane.in_memory()
    finding = cp.record_integration_finding(
        "beads_repository",
        "repo-1",
        "beads.export_drift.jsonl_only_ready",
        "Beads export drift",
        {"jsonl_only_ready_ids": ["mac-test"]},
        fingerprint="fp-test",
    )
    observation = cp.record_integration_observation(
        "beads_repository",
        "repo-1",
        "beads_db",
        "ok",
        fingerprint="obs-test",
        detail={"ready_count": 0},
    )
    client = TestClient(create_app(control_plane=cp))

    findings = client.get("/integrations/findings", params={"status": "open"}).json()
    observations = client.get("/integrations/observations", params={"authority": "beads_db"}).json()
    state = client.get("/dashboard/state").json()

    assert findings[0]["id"] == finding.id
    assert findings[0]["detail"]["jsonl_only_ready_ids"] == ["mac-test"]
    assert observations[0]["id"] == observation.id
    assert state["integration_findings"][0]["id"] == finding.id
    assert state["integration_observations"][0]["id"] == observation.id


def test_events_endpoint_returns_unified_stream():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    tenant = client.post("/tenants", json={"name": "ops"}).json()
    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "audited", "required_capabilities": ["python"]},
    ).json()
    client.post(
        "/tasks/%s/claim" % task["id"],
        params={"agent_id": agent["id"]},
    )
    secret = client.post(
        "/secrets",
        json={
            "name": "audit-token",
            "value": "v",
            "scopes": {"capabilities": ["python"]},
            "created_by": "human",
        },
    ).json()
    client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "test"},
    )

    events = client.get("/events", params={"limit": 200}).json()
    types = {event["subject_type"] for event in events}
    assert {"task", "secret"} <= types

    task_only = client.get(
        "/events",
        params={"subject_type": "task", "subject_id": task["id"]},
    ).json()
    assert task_only
    assert all(event["subject_id"] == task["id"] for event in task_only)


def test_command_audit_endpoint_records_short_retention_command_events():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "worker", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "audited command", "required_capabilities": ["python"]},
    ).json()

    started = client.post(
        "/agents/%s/command-audit" % agent["id"],
        json={
            "command_id": "cmd-test",
            "phase": "started",
            "argv": ["pytest", "tests/test_worker.py"],
            "cwd": "/repo",
            "task_id": task["id"],
            "started_at": "2026-05-20T00:00:00.000000+00:00",
            "metadata": {"argv_sha256": "sha256:abc"},
        },
    ).json()
    completed = client.post(
        "/agents/%s/command-audit" % agent["id"],
        json={
            "command_id": "cmd-test",
            "phase": "completed",
            "argv": ["pytest", "tests/test_worker.py"],
            "cwd": "/repo",
            "task_id": task["id"],
            "started_at": "2026-05-20T00:00:00.000000+00:00",
            "completed_at": "2026-05-20T00:00:01.000000+00:00",
            "duration_ms": 1000,
            "returncode": 0,
        },
    ).json()

    assert started["command_id"] == completed["command_id"] == "cmd-test"
    listed = client.get("/command-audit", params={"agent_id": agent["id"]}).json()
    assert [item["phase"] for item in listed] == ["completed", "started"]
    dashboard = client.get("/dashboard/state").json()
    assert dashboard["command_audit"][0]["command_id"] == "cmd-test"
    command_events = client.get(
        "/events",
        params={
            "subject_type": "task",
            "subject_id": task["id"],
            "event_type_prefix": "command.",
        },
    ).json()
    assert {event["event_type"] for event in command_events} == {
        "command.started",
        "command.completed",
    }


def test_observability_api_records_lists_and_streams_metrics_and_logs():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    metric = client.post(
        "/observability/metrics",
        json={
            "name": "worker.queue.depth",
            "value": 7,
            "unit": "tasks",
            "layer": "worker",
            "source": "rocky",
            "detail": {"queue": "default"},
        },
    ).json()
    log = client.post(
        "/observability/logs",
        json={
            "name": "worker.dispatch.waiting",
            "level": "warning",
            "layer": "worker",
            "source": "rocky",
            "detail": {"reason": "no lease"},
        },
    ).json()

    assert metric["sequence"] < log["sequence"]
    listed = client.get(
        "/observability",
        params={"kind": "metric", "layer": "worker"},
    ).json()
    assert [item["name"] for item in listed] == ["worker.queue.depth"]
    assert [
        item["name"]
        for item in client.get("/observability/metrics", params={"layer": "worker"}).json()
    ] == ["worker.queue.depth"]
    assert [
        item["name"]
        for item in client.get("/observability/logs", params={"layer": "worker"}).json()
    ] == ["worker.dispatch.waiting"]

    summary = client.get("/observability/summary").json()
    assert summary["counts"]["metrics"] >= 1
    assert summary["counts"]["warnings"] >= 1
    assert any(item["name"] == "worker.queue.depth" for item in summary["latest_metrics"])

    streamed = client.get(
        "/observability/stream",
        params={
            "after_sequence": metric["sequence"] - 1,
            "layer": "worker",
            "timeout_seconds": 0.01,
        },
    )
    assert streamed.status_code == 200
    lines = [json.loads(line) for line in streamed.text.splitlines() if line]
    assert [line["id"] for line in lines[:2]] == [metric["id"], log["id"]]


def test_observability_logs_silenced_poll_name_is_filtered_not_500():
    """mem-04 silences high-volume idle-poll log names (record_log -> None).
    The endpoint must report that as filtered, not 500 on .to_dict() of None."""
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    # a silenced idle-poll name is dropped: 200 + {"filtered": True}, never 500
    dropped = client.post(
        "/observability/logs",
        json={"name": "worker.no_task", "level": "info", "layer": "worker", "source": "rocky"},
    )
    assert dropped.status_code == 200
    assert dropped.json() == {"filtered": True}

    # a normal log name still records and returns the event
    kept = client.post(
        "/observability/logs",
        json={"name": "worker.dispatch.waiting", "level": "warning", "layer": "worker", "source": "rocky"},
    )
    assert kept.status_code == 200
    assert kept.json()["name"] == "worker.dispatch.waiting"


def test_http_observation_middleware_is_off_by_default_and_writes_one_row_when_on():
    off_cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=off_cp))
    assert client.get("/agents").status_code == 200
    assert client.post("/machines", json={"hostname": "h"}).status_code == 200
    assert not any(item.layer == "api" for item in off_cp.list_observability(limit=50))

    on_cp = ControlPlane.in_memory()
    on_client = TestClient(
        create_app(control_plane=on_cp, record_http_observations=True)
    )
    assert on_client.get("/agents").status_code == 200
    api_rows = [item for item in on_cp.list_observability(limit=50) if item.layer == "api"]
    # Exactly one row per non-excluded request (the GET /agents above), no log+metric pair.
    assert len(api_rows) == 1
    assert api_rows[0].kind == "metric"
    assert api_rows[0].name == "http.request.duration_ms"
    assert api_rows[0].detail["path"] == "/agents"


def test_observability_write_endpoints_are_not_self_observed():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp, record_http_observations=True))
    client.post(
        "/observability/logs",
        json={"name": "worker.heartbeat", "layer": "worker", "source": "rocky"},
    )
    api_rows = [item for item in cp.list_observability(limit=20) if item.layer == "api"]
    assert api_rows == []


def test_observability_write_requires_agent_scope_when_auth_enabled():
    client = TestClient(
        create_app(
            control_plane=ControlPlane.in_memory(),
            auth_tokens={"reader": ["read"], "agent": ["agent"]},
        )
    )
    body = {"name": "worker.queue.depth", "value": 1, "layer": "worker"}

    assert client.post(
        "/observability/metrics",
        headers={"Authorization": "Bearer reader"},
        json=body,
    ).status_code == 403
    assert client.post(
        "/observability/metrics",
        headers={"Authorization": "Bearer agent"},
        json=body,
    ).status_code == 200
    assert client.get(
        "/observability",
        headers={"Authorization": "Bearer reader"},
    ).status_code == 200


def test_create_app_refuses_to_start_with_placeholder_secret_key():
    """The deployment env example ships a placeholder long enough to pass the
    32-char length check. Refusing it at startup prevents copy-and-deploy
    deployments from encrypting with a known constant."""
    import pytest
    from mac.models import ValidationError
    from mac.services import ControlPlane
    from mac.store import SQLiteStore

    with pytest.raises(ValidationError):
        ControlPlane(
            SQLiteStore(":memory:"),
            secret_key="REPLACE-ME-WITH-A-32-PLUS-CHAR-RANDOM-STRING",
        )


def test_create_app_via_env_only_works_with_real_secret_key(monkeypatch, tmp_path):
    """Simulate the Docker / systemd path: env-only configuration, fresh empty
    DB directory, no MAC_API_TOKEN. The factory should succeed and /health
    should answer 200."""
    import importlib
    import mac.api as api_module

    db_path = tmp_path / "deploy.db"
    monkeypatch.setenv(
        "MAC_SECRET_KEY",
        "deploy-smoke-key-with-32-plus-characters-of-entropy-abc",
    )
    monkeypatch.setenv("MAC_DB", str(db_path))
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKENS", raising=False)
    # Reload so the conditional `if MAC_SECRET_KEY: app = create_app()` re-runs.
    importlib.reload(api_module)

    client = TestClient(api_module.app)
    assert client.get("/health").json() == {"status": "ok"}
    # The DB file was created on first connect.
    assert db_path.exists()


def test_dashboard_has_typescript_source_without_node_toolchain_files():
    root = Path(__file__).resolve().parents[2]

    assert (root / "src/mac/ui/app.ts").exists()
    assert (root / "src/mac/ui/app.js").exists()
    assert (root / "src/mac/ui/dashboard_api.ts").exists()
    app_js = (root / "src/mac/ui/app.js").read_text(encoding="utf-8")
    index_html = (root / "src/mac/ui/index.html").read_text(encoding="utf-8")
    assert "URLSearchParams" in app_js
    assert "createDashboardApi" in app_js
    assert "showLoginScreen" in app_js
    assert "mac.dashboard.apiBaseUrl" in app_js
    assert "loginApiUrlInput" in app_js
    assert "window.macDashboard" in app_js
    assert "Dashboard data needs a signed-in session or a token with read scope." in app_js
    assert "sessionStorage.removeItem(TOKEN_KEY)" in app_js
    assert "renderWork" in app_js
    assert "renderProjects" in app_js
    assert "renderFleets" in app_js
    assert "Epic / Project Frontier" in app_js
    assert "Agent Resource Table" in app_js
    assert "Fleet CRUD" not in app_js
    assert "data-project-focus" in app_js
    assert "[data-project-focus]" in app_js
    for delete_marker in [
        "data-project-delete",
        "data-agent-delete",
        "data-task-delete",
        "[data-project-delete]",
        "[data-agent-delete]",
        "[data-task-delete]",
    ]:
        assert delete_marker in app_js
    assert "data-fleet-delete" not in app_js
    assert "[data-fleet-delete]" not in app_js
    assert 'closest("[data-project]")' not in app_js
    assert "Project CRUD" not in app_js
    assert 'data-action="fleetCreate"' not in app_js
    assert 'data-action="fleetUpdate"' not in app_js
    assert "fleetCreate" not in app_js
    assert "fleetUpdate" not in app_js
    assert 'data-action="agentCreate"' in app_js
    assert 'data-action="agentUpdate"' in app_js
    assert 'data-action="taskCreate"' in app_js
    assert 'data-action="taskUpdate"' in app_js
    assert 'data-action="projectCreate"' in app_js
    assert 'data-action="projectUpdate"' in app_js
    assert "putJSON" in app_js
    assert "deleteJSON" in app_js
    assert "sessionAccessBadge" in app_js
    assert "agentFleetLabel" in app_js
    assert "topologySelectionDetail" in app_js
    assert "Hermes Instance ID" in app_js
    assert "Read-only token" in app_js
    assert "renderWorkflows" in app_js
    assert "Plan Workflow" in app_js
    assert "Generate Plan" in app_js
    assert "Proposed Task Graph" in app_js
    assert "workflowPlanPreview" in app_js
    assert "/dashboard/workflow-plan/preview" in app_js
    assert "workflowPlanAccept" in app_js
    assert "/dashboard/workflow-plan/accept" in app_js
    assert "workflowPlanNodeAdd" in app_js
    assert "workflowPlanNodeMove" in app_js
    assert "workflowPlanNodeDelete" in app_js
    assert "workflowPlanCancel" in app_js
    assert "data-plan-field" in app_js
    assert "Workflow accepted:" in app_js
    assert "Definition Draft Builder" in app_js
    assert "workflowGraph" in app_js
    assert "hermes_runtime_proofs" in app_js
    assert "Runtime Proof" in app_js
    assert "Live alignment" in app_js
    assert "live_alignment" in app_js
    assert "Dashboard URLs" in app_js
    assert "Dashboard Links" in app_js
    assert "dashboard_url_contract" in app_js
    assert "dashboardLinkChip" in app_js
    assert "object_deep_links" in app_js
    assert "Session caps" in app_js
    assert "Session Capabilities" in app_js
    assert "Bridge Commands" in app_js
    assert "First-Class Objects" in app_js
    assert "firstClassCouplingMatrix" in app_js
    assert "UI Projection" in app_js
    assert "Hermes CLI" in app_js
    assert "mac_cli_commands" in app_js
    assert "runtime_capabilities" in app_js
    assert "Runtime Deltas" in app_js
    assert "runtimeDeltaValidate" in app_js
    assert "web research" in app_js
    assert "command audit" in app_js
    assert "Objects" in app_js
    assert "Task ops" in app_js
    assert "Project ops" in app_js
    assert "Agent ops" in app_js
    assert 'data-view="work"' in index_html
    assert 'data-view="projects"' in index_html
    assert 'data-view="map"' in index_html
    assert 'data-view="fleets"' in index_html
    assert 'data-view="workflows"' in index_html
    assert not (root / "package.json").exists()
    assert not (root / "package-lock.json").exists()


def test_fastapi_exposes_typed_agentbus_streams_and_ndjson_events():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "bus-host"}).json()
    sender = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "sender"},
    ).json()
    recipient = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "recipient"},
    ).json()

    stream = client.post(
        "/agentbus/streams",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content_type": "application/vnd.mac.delta+json",
            "topic": "delta",
        },
    ).json()
    first = client.post(
        "/agentbus/streams/%s/chunks" % stream["id"],
        json={
            "sender_agent_id": sender["id"],
            "payload": {"seq": 1, "text": "hello"},
        },
    ).json()
    second = client.post(
        "/agentbus/streams/%s/chunks" % stream["id"],
        json={
            "sender_agent_id": sender["id"],
            "payload": {"seq": 2, "done": True},
            "final": True,
        },
    ).json()

    assert [first["sequence"], second["sequence"]] == [1, 2]
    chunks = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": recipient["id"]},
    ).json()
    assert [chunk["payload"]["seq"] for chunk in chunks] == [1, 2]

    events = client.get(
        "/agentbus/streams/%s/events" % stream["id"],
        params={"agent_id": recipient["id"], "timeout_seconds": 0.01},
    )
    lines = [line for line in events.text.splitlines() if line]
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]

    published = client.post(
        "/agentbus",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content_type": "text/plain",
            "payload_encoding": "text",
            "payload": "one-shot",
        },
    ).json()
    assert published["stream"]["status"] == "closed"
    assert published["chunk"]["payload"] == "one-shot"


def test_dashboard_terminal_session_creates_agentbus_streams_and_streams_events():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "terminal-host"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "terminal-agent"},
    ).json()

    opened = client.post(
        "/dashboard/agents/%s/terminal-sessions" % agent["id"],
        json={"rows": 24, "cols": 100, "ttl_seconds": 60},
    ).json()

    assert opened["schema"] == "mac.dashboard.terminal_session.v1"
    assert opened["agent_id"] == agent["id"]
    assert opened["input_stream"]["topic"] == "mac.debug.terminal.input.v1"
    assert opened["output_stream"]["topic"] == "mac.debug.terminal.output.v1"
    listed = client.get("/dashboard/terminal-sessions").json()
    assert listed["schema"] == "mac.dashboard.terminal_sessions.v1"
    assert listed["terminal_sessions"][0]["session_id"] == opened["session_id"]
    assert listed["terminal_sessions"][0]["agent_id"] == agent["id"]
    assert listed["terminal_sessions"][0]["input_stream_id"] == opened["input_stream_id"]
    assert listed["terminal_sessions"][0]["output_stream_id"] == opened["output_stream_id"]

    client.post(
        "/dashboard/terminal-sessions/%s/input" % opened["session_id"],
        json={
            "input_stream_id": opened["input_stream_id"],
            "data": "printf hello\n",
        },
    )
    input_chunks = client.get(
        "/agentbus/streams/%s/chunks" % opened["input_stream_id"],
        params={"agent_id": agent["id"]},
    ).json()
    assert input_chunks[-1]["payload"]["session_id"] == opened["session_id"]
    assert base64.b64decode(input_chunks[-1]["payload"]["data_b64"]).decode() == "printf hello\n"

    client.post(
        "/agentbus/streams/%s/chunks" % opened["output_stream_id"],
        json={
            "sender_agent_id": agent["id"],
            "payload": {
                "schema": DEBUG_TERMINAL_OUTPUT_SCHEMA,
                "session_id": opened["session_id"],
                "event": "output",
                "data_b64": base64.b64encode(b"hello\n").decode("ascii"),
            },
            "final": True,
        },
    )
    events = client.get(
        "/dashboard/terminal-sessions/%s/events" % opened["session_id"],
        params={
            "output_stream_id": opened["output_stream_id"],
            "after_sequence": 0,
            "timeout_seconds": 0,
        },
    )
    lines = [json.loads(line) for line in events.text.splitlines() if line]
    assert base64.b64decode(lines[-1]["payload"]["data_b64"]).decode() == "hello\n"


def test_fastapi_publishes_agentbus_repo_update_to_all_agents():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "bus-host"}).json()
    sender = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "sender"},
    ).json()
    recipient = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "recipient"},
    ).json()

    published = client.post(
        "/agentbus/repo-update",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_ids": [recipient["id"]],
            "remote": "origin",
            "branch": "main",
            "request_id": "req-api",
        },
    ).json()

    assert published["schema"] == "mac.agentbus.repo_update_publish.v1"
    assert published["count"] == 1
    stream = published["streams"][0]
    assert stream["topic"] == "mac.repo.update.v1"
    assert stream["content_type"] == "application/vnd.mac.repo-update+json"
    chunks = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": recipient["id"]},
    ).json()
    assert chunks[0]["payload"]["schema"] == "mac.agentbus.repo_update.v1"
    assert chunks[0]["payload"]["request_id"] == "req-api"


def test_fastapi_agentbus_artifact_publish_crud(monkeypatch):
    monkeypatch.setenv("MAC_PUBLISH_DIR", "/srv/mac-artifacts")
    monkeypatch.setenv("MAC_PUBLISH_PUBLIC_URL", "http://hub.example:8790/artifacts")
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "artifact-bus-host"}).json()
    sender = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "sender"},
    ).json()
    recipient = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "recipient"},
    ).json()

    published = client.post(
        "/agentbus/artifact-publish",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_ids": [recipient["id"]],
            "digest": "sha256:api-artifact",
            "path": "out/report.txt",
            "metadata": {"content_type": "text/plain"},
        },
    ).json()

    assert published["schema"] == "mac.agentbus.artifact_publish_crud.v1"
    assert published["artifact"]["uri"] == "http://hub.example:8790/artifacts/out/report.txt"
    assert published["artifact"]["metadata"]["publish_dir"] == "/srv/mac-artifacts"
    stream = published["streams"][0]
    chunks = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": recipient["id"]},
    ).json()
    assert chunks[0]["payload"]["schema"] == "mac.agentbus.artifact_publish.v1"
    assert chunks[0]["payload"]["artifact"]["digest"] == "sha256:api-artifact"

    listed = client.post(
        "/agentbus/artifact-publish",
        json={"sender_agent_id": sender["id"], "operation": "list"},
    ).json()
    assert [item["digest"] for item in listed["artifacts"]] == ["sha256:api-artifact"]
    deleted = client.post(
        "/agentbus/artifact-publish",
        json={
            "sender_agent_id": sender["id"],
            "operation": "delete",
            "digest": "sha256:api-artifact",
        },
    ).json()
    assert deleted["deleted"] is True
    assert client.get("/artifacts/sha256:api-artifact").status_code == 404


def test_fastapi_project_import_preserves_first_class_task_fields():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    parent = cp.create_task("Parent task", project="repo-beads-mac")

    item = client.post(
        "/bridge/items",
        json={
            "source": "repo-beads-mac",
            "external_id": "mac-api",
            "title": "API imported project item",
            "description": "Imported with explicit project fields.",
            "project": "repo-beads-mac",
            "priority": 42,
            "payload": {"summary": "track this"},
            "required_capabilities": ["python"],
            "dependencies": [parent.id],
            "metadata": {"team": "core"},
            "actor": "api-test",
        },
    ).json()
    task = client.get("/tasks/%s" % item["task_id"]).json()["task"]

    assert task["project"] == "repo-beads-mac"
    assert task["description"] == "Imported with explicit project fields."
    assert task["priority"] == 42
    assert task["required_capabilities"] == ["python"]
    assert task["dependencies"] == [parent.id]
    assert task["metadata"]["team"] == "core"
    assert task["metadata"]["source"] == "repo-beads-mac"
    assert task["metadata"]["external_id"] == "mac-api"






def test_agentbus_rejects_broadcast_oversized_and_unauthorized_readers():
    import time as _time

    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "bus-host"}).json()
    sender = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "sender"}
    ).json()
    recipient = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "recipient"}
    ).json()
    outsider = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "outsider"}
    ).json()

    no_recipient = client.post(
        "/agentbus/streams", json={"sender_agent_id": sender["id"]}
    )
    assert no_recipient.status_code == 422

    stream = client.post(
        "/agentbus/streams",
        json={"sender_agent_id": sender["id"], "recipient_agent_id": recipient["id"]},
    ).json()

    huge = client.post(
        "/agentbus/streams/%s/chunks" % stream["id"],
        json={"sender_agent_id": sender["id"], "payload": {"blob": "x" * (256 * 1024 + 1)}},
    )
    assert huge.status_code == 400

    listed = client.get(
        "/agentbus/streams/%s/chunks" % stream["id"],
        params={"agent_id": outsider["id"]},
    )
    assert listed.status_code == 403

    events = client.get(
        "/agentbus/streams/%s/events" % stream["id"],
        params={"agent_id": outsider["id"], "timeout_seconds": 0.01},
    )
    assert events.status_code == 403

    close = client.post(
        "/agentbus/streams/%s/close" % stream["id"],
        params={"sender_agent_id": sender["id"]},
    )
    assert close.status_code == 200

    started = _time.monotonic()
    capped = client.get(
        "/agentbus/streams/%s/events" % stream["id"],
        params={
            "agent_id": recipient["id"],
            "timeout_seconds": 600,
            "poll_interval_seconds": 0.001,
        },
    )
    elapsed = _time.monotonic() - started
    assert capped.status_code == 200
    # Closed-stream short-circuit must return promptly even though caller
    # asked for a 10-minute timeout — proves the server isn't honoring the
    # client-controlled value verbatim.
    assert elapsed < 5


def test_agentbus_event_clamps_have_correct_bounds():
    from mac.api import (
        AGENTBUS_MAX_EVENT_POLL_SECONDS,
        AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS,
        AGENTBUS_MIN_EVENT_POLL_SECONDS,
        _agentbus_clamp_poll_interval,
        _agentbus_clamp_timeout,
    )

    assert _agentbus_clamp_timeout(-1) == 0.0
    assert _agentbus_clamp_timeout(0.5) == 0.5
    assert _agentbus_clamp_timeout(600) == AGENTBUS_MAX_EVENT_TIMEOUT_SECONDS
    assert _agentbus_clamp_poll_interval(0.0) == AGENTBUS_MIN_EVENT_POLL_SECONDS
    assert _agentbus_clamp_poll_interval(0.5) == 0.5
    assert _agentbus_clamp_poll_interval(100) == AGENTBUS_MAX_EVENT_POLL_SECONDS


def test_agentbus_events_delivers_chunks_appended_after_request_starts():
    import threading
    import time as _time

    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "bus-host"}).json()
    sender = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "sender"}
    ).json()
    recipient = client.post(
        "/agents", json={"machine_id": machine["id"], "name": "recipient"}
    ).json()
    stream = client.post(
        "/agentbus/streams",
        json={"sender_agent_id": sender["id"], "recipient_agent_id": recipient["id"]},
    ).json()

    def appender() -> None:
        _time.sleep(0.4)
        client.post(
            "/agentbus/streams/%s/chunks" % stream["id"],
            json={"sender_agent_id": sender["id"], "payload": {"seq": 1}, "final": True},
        )

    thread = threading.Thread(target=appender)
    thread.start()
    try:
        started = _time.monotonic()
        events = client.get(
            "/agentbus/streams/%s/events" % stream["id"],
            params={
                "agent_id": recipient["id"],
                "timeout_seconds": 5,
                "poll_interval_seconds": 0.25,
            },
        )
        elapsed = _time.monotonic() - started
    finally:
        thread.join()

    assert events.status_code == 200
    lines = [line for line in events.text.splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["payload"] == {"seq": 1}
    # Should observe the chunk well before the 5s deadline.
    assert elapsed < 3
