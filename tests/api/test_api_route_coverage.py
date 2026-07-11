from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Tuple

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mac.agentbus_control import (
    DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
    DEBUG_TERMINAL_INPUT_SCHEMA,
    DEBUG_TERMINAL_INPUT_TOPIC,
    DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
    DEBUG_TERMINAL_OUTPUT_SCHEMA,
    DEBUG_TERMINAL_OUTPUT_TOPIC,
)
from mac.api import create_app
from mac.services import ControlPlane, sign_verification_manifest


RouteKey = Tuple[str, str]


@dataclass(frozen=True)
class RequestCase:
    path: str
    kwargs: Dict[str, Any]
    expected_statuses: Tuple[int, ...] = (200,)


def _ok(response):
    assert 200 <= response.status_code < 400, response.text
    return response.json() if response.content else None


def _route_keys(app) -> list[RouteKey]:
    keys: list[RouteKey] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods or []):
            if method in {"HEAD", "OPTIONS"}:
                continue
            keys.append((method, route.path))
    return keys


def _ordered_route_keys(app) -> list[RouteKey]:
    keys = _route_keys(app)
    deletes = [key for key in keys if key[0] == "DELETE"]
    non_deletes = [key for key in keys if key[0] != "DELETE"]
    return non_deletes + deletes


def _workflow_definition() -> Dict[str, Any]:
    return {
        "nodes": [
            {"node_key": "investigate", "node_type": "task", "role_required": "qa", "max_attempts": 1},
            {"node_key": "fix", "node_type": "task", "role_required": "dev", "max_attempts": 1},
        ],
        "edges": [
            {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
            {"from_node_key": "investigate", "to_node_key": "fix", "condition": "success", "priority": 100},
            {"from_node_key": "fix", "to_node_key": "", "condition": "success", "priority": 100},
        ],
    }


def _runtime_manifest() -> Dict[str, Any]:
    return {
        "python": "3.11",
        "dependencies": ["pytest==8.4.0"],
        "commands": ["uv run pytest"],
    }


def _operator_manifest(summary: str) -> Dict[str, Any]:
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": summary,
        "checks": [{"name": "operator result reviewed", "returncode": 0}],
    }


def _signed_operator_manifest(summary: str, *, signed_by: str, key: str) -> Dict[str, Any]:
    manifest = _operator_manifest(summary)
    manifest["signed_by"] = signed_by
    manifest["signature"] = sign_verification_manifest(key, manifest)
    return manifest


def _delta_payload(ctx: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": ctx["runtime_task_id"],
        "agent_id": ctx["agent_id"],
        "package_manager": "uv",
        "commands": ["uv add requests==2.32.3"],
        "added_dependencies": ["requests==2.32.3"],
        "reason": "route coverage needs a validated runtime delta",
        "project": ctx["project_name"],
        "base_runtime_id": ctx["runtime_id"],
        "lockfile_path": "uv.lock",
        "lockfile_digest": "sha256:" + "a" * 64,
    }


def _prepare_reviewing_task(
    cp: ControlPlane,
    *,
    task_id: str,
    worker_id: str,
    worker_key: str,
    reviewer_id: str,
) -> Dict[str, Any]:
    cp.claim_task(task_id, worker_id, sync_beads=False)
    cp.start_task(task_id, worker_id, drain_outbox=False)
    evidence = cp.add_evidence(
        task_id,
        "test",
        "artifact://operator-result",
        "substantive operator result with concrete verification evidence",
        worker_id,
        metadata={
            "returncode": 0,
            "verification": _signed_operator_manifest(
                "route coverage completed real work",
                signed_by=worker_id,
                key=worker_key,
            ),
        },
        sync_beads=False,
    )
    cp.submit_for_review(task_id, worker_id, drain_outbox=False)
    review = cp.request_review(task_id, reviewer_id)
    return {"executor_evidence_id": evidence.id, "review_id": review.id}


def _prepare_publishable_task(
    cp: ControlPlane,
    *,
    task_id: str,
    worker_id: str,
    worker_key: str,
    reviewer_id: str,
    reviewer_key: str,
) -> Dict[str, Any]:
    prepared = _prepare_reviewing_task(
        cp,
        task_id=task_id,
        worker_id=worker_id,
        worker_key=worker_key,
        reviewer_id=reviewer_id,
    )
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": prepared["executor_evidence_id"],
        "worktree_digest": "sha256:" + "0" * 64,
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
        "signed_by": reviewer_id,
    }
    manifest["signature"] = sign_verification_manifest(reviewer_key, manifest)
    verdict = cp.add_evidence(
        task_id,
        "review",
        "artifact://review-verdict",
        "reviewer approved route coverage evidence",
        reviewer_id,
        metadata={"returncode": 0, "verification": manifest},
        sync_beads=False,
    )
    cp.submit_review(
        prepared["review_id"],
        "approved",
        reviewer_id,
        evidence_id=verdict.id,
    )
    return {
        **prepared,
        "verdict_evidence_id": verdict.id,
    }


def _seed_route_state(client: TestClient, cp: ControlPlane, tmp_path) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}

    tenant = _ok(client.post("/tenants", json={"name": "Route Coverage Tenant"}))
    ctx["tenant_id"] = tenant["id"]
    user = _ok(
        client.post(
            "/users",
            json={"tenant_id": tenant["id"], "handle": "operator", "display_name": "Operator"},
        )
    )
    ctx["user_id"] = user["id"]
    persona = _ok(
        client.post(
            "/personas",
            json={
                "tenant_id": tenant["id"],
                "name": "Rocky",
                "soul_ref": "hermes://route/rocky/SOUL.md",
                "memory_scope": "hermes://route/rocky/memory",
                "metadata": {"role_slugs": ["rocky", "qa"]},
            },
        )
    )
    ctx["persona_id"] = persona["id"]
    hermes = _ok(
        client.post(
            "/hermes-instances",
            json={
                "tenant_id": tenant["id"],
                "name": "rocky-route",
                "persona_id": persona["id"],
                "home_ref": "hermes://route/rocky",
            },
        )
    )
    ctx["instance_id"] = hermes["id"]
    binding = _ok(
        client.post(
            "/platform-bindings",
            json={
                "tenant_id": tenant["id"],
                "hermes_instance_id": hermes["id"],
                "platform": "slack",
                "external_id": "C-route",
                "display_name": "route-home",
            },
        )
    )
    ctx["platform_binding_id"] = binding["id"]

    project = _ok(
        client.post(
            "/projects",
            json={"name": "route-project", "description": "route coverage project"},
        )
    )
    ctx["project_name"] = project["name"]
    ctx["delete_project_name"] = _ok(
        client.post("/projects", json={"name": "route-project-delete"})
    )["name"]
    route_repo = tmp_path / "route-repo"
    (route_repo / ".mac").mkdir(parents=True)
    (route_repo / ".mac" / "project.yaml").write_text(
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: route-project",
                "platforms:",
                "  - linux",
                "toolchain:",
                "  required_commands:",
                "    - python3",
                "bootstrap:",
                "  command: python3 -m venv .venv",
                "test:",
                "  command: pytest",
                "evidence:",
                "  required:",
                "    - tests",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ctx["repository_path"] = str(route_repo)

    machine = _ok(
        client.post(
            "/machines",
            json={"hostname": "route-host", "resources": {"cpu": 16, "memory_gb": 64}},
        )
    )
    ctx["machine_id"] = machine["id"]

    def agent(
        name: str,
        capabilities: Iterable[str],
        *,
        hermes_instance_id: str | None = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "machine_id": machine["id"],
            "name": name,
            "capabilities": list(capabilities),
        }
        if hermes_instance_id:
            payload["hermes_instance_id"] = hermes_instance_id
        return _ok(
            client.post(
                "/agents",
                json=payload,
            )
        )

    default_agent = agent(
        "rocky-route",
        ["python", "deploy", "ops", "qa", "review"],
        hermes_instance_id=hermes["id"],
    )
    ctx["agent_id"] = default_agent["id"]
    ctx["agent_attestation_key"] = default_agent["attestation_key"]
    reviewer = agent("natasha-route", ["review", "qa"])
    ctx["reviewer_agent_id"] = reviewer["id"]
    ctx["reviewer_attestation_key"] = reviewer["attestation_key"]
    ctx["terminal_session_id"] = "term_route_case"
    ctx["terminal_input_stream_id"] = "term_route_case.in"
    ctx["terminal_output_stream_id"] = "term_route_case.out"
    cp.open_agentbus_stream(
        sender_agent_id=default_agent["id"],
        recipient_agent_id=default_agent["id"],
        content_type=DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
        topic=DEBUG_TERMINAL_INPUT_TOPIC,
        headers={
            "schema": DEBUG_TERMINAL_INPUT_SCHEMA,
            "terminal_session_id": ctx["terminal_session_id"],
        },
        stream_id=ctx["terminal_input_stream_id"],
    )
    cp.open_agentbus_stream(
        sender_agent_id=default_agent["id"],
        recipient_agent_id=default_agent["id"],
        content_type=DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
        topic=DEBUG_TERMINAL_OUTPUT_TOPIC,
        headers={
            "schema": DEBUG_TERMINAL_OUTPUT_SCHEMA,
            "terminal_session_id": ctx["terminal_session_id"],
        },
        stream_id=ctx["terminal_output_stream_id"],
    )
    ctx["delegate_agent_id"] = agent("bullwinkle-route", ["python"])["id"]
    ctx["delete_agent_id"] = agent("delete-route-agent", ["python"])["id"]
    ctx["disable_agent_id"] = agent("disable-route-agent", ["python"])["id"]
    ctx["bulk_agent_id"] = agent("bulk-route-agent", ["python"])["id"]
    ctx["claim_agent_id"] = agent("claim-route-agent", ["python"])["id"]
    ctx["claim_next_agent_id"] = agent("claim-next-route-agent", ["python"])["id"]
    ctx["lease_agent_id"] = agent("lease-route-agent", ["python"])["id"]
    ctx["start_agent_id"] = agent("start-route-agent", ["python"])["id"]
    submit_agent = agent("submit-route-agent", ["python"])
    ctx["submit_agent_id"] = submit_agent["id"]
    ctx["submit_agent_key"] = submit_agent["attestation_key"]
    review_worker = agent("review-route-worker", ["python"])
    ctx["review_worker_id"] = review_worker["id"]
    ctx["review_worker_key"] = review_worker["attestation_key"]
    publish_worker = agent("publish-route-worker", ["python"])
    ctx["publish_worker_id"] = publish_worker["id"]
    ctx["publish_worker_key"] = publish_worker["attestation_key"]
    ctx["nap_agent_id"] = agent("nap-route-agent", ["ops"])["id"]
    ctx["nap_begin_agent_id"] = agent("nap-begin-route-agent", ["ops"])["id"]
    ctx["nap_fail_agent_id"] = agent("nap-fail-route-agent", ["ops"])["id"]
    ctx["attest_rotate_agent_id"] = agent("attest-rotate-route-agent", ["python"])["id"]
    attest_verify = agent("attest-verify-route-agent", ["python"])
    ctx["attest_verify_agent_id"] = attest_verify["id"]
    ctx["attest_verify_key"] = attest_verify["attestation_key"]
    ctx["dispatch_hold_agent_id"] = agent("dispatch-hold-route-agent", ["python"])["id"]

    policy_text = """
version: 1
network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: 127.0.0.1
        port: 8789
        protocol: rest
"""
    openshell_policy = _ok(
        client.post(
            "/openshell/policies",
            json={
                "name": "route-policy",
                "description": "route coverage OpenShell policy",
                "policy_text": policy_text,
                "created_by": "operator",
            },
        )
    )
    ctx["openshell_policy_id"] = openshell_policy["id"]
    # Scientific optimizer fixtures (route coverage for /optimizer/*): a
    # control+treatment policy pair and one experiment, so GET-by-id routes
    # resolve and action routes act on real rows.
    sci_control = _ok(client.post("/optimizer/policies", json={
        "name": "route-sci-control", "project": ctx["project_name"],
        "parameters": {"plan_first": True}, "created_by": "route-coverage"}))
    sci_treatment = _ok(client.post("/optimizer/policies", json={
        "name": "route-sci-treatment", "project": ctx["project_name"],
        "parameters": {"plan_first": False}, "created_by": "route-coverage"}))
    ctx["sci_policy_id"] = sci_control["id"]
    ctx["sci_policy2_id"] = sci_treatment["id"]
    sci_exp = _ok(client.post("/optimizer/experiments", json={
        "name": "route-sci-experiment", "project": ctx["project_name"],
        "hypothesis": "treatment beats control on route coverage",
        "control_policy_id": sci_control["id"],
        "treatment_policy_id": sci_treatment["id"],
        "primary_metric": "accepted_success", "created_by": "route-coverage"}))
    ctx["sci_experiment_id"] = sci_exp["id"]
    sci_exp2 = _ok(client.post("/optimizer/experiments", json={
        "name": "route-sci-experiment-promote", "project": ctx["project_name"],
        "hypothesis": "promote-path route coverage",
        "control_policy_id": sci_control["id"],
        "treatment_policy_id": sci_treatment["id"],
        "primary_metric": "accepted_success", "created_by": "route-coverage"}))
    _ok(client.post("/optimizer/experiments/%s/start" % sci_exp2["id"],
                    json={"actor": "route-coverage"}))
    ctx["sci_experiment2_id"] = sci_exp2["id"]
    _ok(
        client.post(
            "/openshell/policies/%s/assignments" % openshell_policy["id"],
            json={"target_type": "agent", "target_id": default_agent["id"], "created_by": "operator"},
        )
    )
    _ok(
        client.post(
            "/agents/%s/openshell/status" % default_agent["id"],
            json={
                "status": "active",
                "sandbox_id": "sandbox-route",
                "policy_id": openshell_policy["id"],
                "policy_version": openshell_policy["version"],
                "checksum": openshell_policy["checksum"],
                "detail": {"route": True},
            },
        )
    )
    _ok(
        client.post(
            "/action-events",
            json={
                "agent_id": default_agent["id"],
                "sandbox_id": "sandbox-route",
                "actor": default_agent["id"],
                "action_type": "openshell.network",
                "action_name": "connect",
                "subject_type": "agent",
                "subject_id": default_agent["id"],
                "outcome": "allowed",
                "policy_id": openshell_policy["id"],
                "policy_version": openshell_policy["version"],
                "attributes": {"host": "127.0.0.1"},
            },
        )
    )

    fleet = _ok(
        client.post(
            "/fleets",
            json={"name": "route-fleet", "description": "route fleet", "agent_ids": [default_agent["id"]]},
        )
    )
    ctx["fleet_id"] = fleet["id"]
    ctx["delete_fleet_id"] = _ok(
        client.post("/fleets", json={"name": "route-fleet-delete"})
    )["id"]

    role_qa = _ok(
        client.post(
            "/roles",
            json={
                "slug": "qa",
                "name": "QA",
                "description": "quality role",
                "system_prompt": "review carefully",
                "level": "ic",
                "default_capabilities": ["qa", "review"],
            },
        )
    )
    ctx["role_id"] = role_qa["id"]
    ctx["role_slug"] = role_qa["slug"]
    _ok(
        client.post(
            "/roles",
            json={
                "slug": "dev",
                "name": "Dev",
                "description": "developer role",
                "system_prompt": "build carefully",
                "level": "ic",
                "default_capabilities": ["python"],
            },
        )
    )
    ctx["delete_role_id"] = _ok(
        client.post(
            "/roles",
            json={
                "slug": "route-delete-role",
                "name": "Delete Role",
                "description": "delete role",
                "system_prompt": "unused",
                "level": "ic",
            },
        )
    )["id"]

    def task(title: str, *, caps: list[str] | None = None, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return _ok(
            client.post(
                "/tasks",
                json={
                    "title": title,
                    "project": ctx["project_name"],
                    "required_capabilities": caps or ["python"],
                    "metadata": metadata or {},
                },
            )
        )

    base_task = task("route base task")
    ctx["task_id"] = base_task["id"]
    ctx["delete_task_id"] = task("route delete task", caps=["delete-only"])["id"]
    ctx["parent_task_id"] = task("route parent task")["id"]
    ctx["transition_task_id"] = task("route transition task")["id"]
    ctx["reopen_task_id"] = task("route reopen task")["id"]
    ctx["force_complete_task_id"] = task("route force-complete task")["id"]
    ctx["claim_task_id"] = task("route claim task")["id"]
    ctx["claim_next_task_id"] = task("route claim-next task")["id"]
    ctx["evidence_task_id"] = task("route evidence task")["id"]
    ctx["runtime_task_id"] = task("route runtime task")["id"]
    ctx["break_glass_task_id"] = task("route break-glass authorize task")["id"]
    revoke_break_glass_task = task("route break-glass revoke task")
    ctx["break_glass_authorization_id"] = cp.authorize_task_break_glass(
        revoke_break_glass_task["id"],
        ctx["agent_id"],
        reason="route coverage host recovery",
        authorized_by="route-coverage",
        ttl_seconds=300,
    ).id

    lease_task = task("route lease task")
    _, lease = cp.claim_task(lease_task["id"], ctx["lease_agent_id"], sync_beads=False)
    ctx["lease_id"] = lease.id

    start_task = task("route start task")
    cp.claim_task(start_task["id"], ctx["start_agent_id"], sync_beads=False)
    ctx["start_task_id"] = start_task["id"]

    submit_task = task("route submit task")
    cp.claim_task(submit_task["id"], ctx["submit_agent_id"], sync_beads=False)
    cp.start_task(submit_task["id"], ctx["submit_agent_id"], drain_outbox=False)
    submit_evidence = cp.add_evidence(
        submit_task["id"],
        "test",
        "artifact://submit",
        "submit route verification evidence",
        ctx["submit_agent_id"],
        metadata={
            "returncode": 0,
            "verification": _signed_operator_manifest(
                "submit route has verification evidence",
                signed_by=ctx["submit_agent_id"],
                key=ctx["submit_agent_key"],
            ),
        },
        sync_beads=False,
    )
    ctx["submit_task_id"] = submit_task["id"]
    ctx["submit_evidence_id"] = submit_evidence.id

    review_task = task("route review task")
    review_ready = _prepare_reviewing_task(
        cp,
        task_id=review_task["id"],
        worker_id=ctx["review_worker_id"],
        worker_key=ctx["review_worker_key"],
        reviewer_id=ctx["reviewer_agent_id"],
    )
    ctx["review_task_id"] = review_task["id"]
    ctx["review_id"] = review_ready["review_id"]
    ctx["executor_evidence_id"] = review_ready["executor_evidence_id"]

    publication_task = task("route publication task")
    publish_ready = _prepare_publishable_task(
        cp,
        task_id=publication_task["id"],
        worker_id=ctx["publish_worker_id"],
        worker_key=ctx["publish_worker_key"],
        reviewer_id=ctx["reviewer_agent_id"],
        reviewer_key=ctx["reviewer_attestation_key"],
    )
    ctx["publication_task_id"] = publication_task["id"]
    ctx["publication_evidence_id"] = publish_ready["executor_evidence_id"]

    workflow = _ok(
        client.post(
            "/workflows",
            json={
                "slug": "route-workflow",
                "name": "Route Workflow",
                "description": "route workflow",
                "workflow_type": "custom",
                "definition": _workflow_definition(),
                "created_by": "human",
            },
        )
    )
    ctx["workflow_id"] = workflow["id"]
    workflow_run = _ok(
        client.post(
            "/workflows/%s/start" % workflow["id"],
            json={"started_by": "operator", "input": {"ticket": "route-coverage"}},
        )
    )
    ctx["workflow_run_id"] = workflow_run["id"]
    ctx["delete_workflow_id"] = _ok(
        client.post(
            "/workflows",
            json={
                "slug": "route-workflow-delete",
                "name": "Route Workflow Delete",
                "description": "route workflow delete",
                "workflow_type": "custom",
                "definition": _workflow_definition(),
                "created_by": "human",
            },
        )
    )["id"]
    draft = _ok(
        client.post(
            "/workflows/drafts",
            json={
                "goal": "Route coverage draft",
                "proposed_steps": [
                    {
                        "node_key": "check",
                        "role_required": "qa",
                        "instructions": "Check the route coverage fixture",
                    }
                ],
                "questions": [{"id": "scope", "prompt": "What scope?", "required": False}],
                "answers": {"scope": "api-routes"},
            },
        )
    )
    ctx["draft_id"] = draft["id"]

    provisioning = _ok(
        client.post(
            "/provisioning/requests",
            json={"reason": "route coverage provision", "capabilities": ["python"], "tenant_id": tenant["id"]},
        )
    )
    ctx["request_id"] = provisioning["id"]
    ctx["cancel_request_id"] = _ok(
        client.post(
            "/provisioning/requests",
            json={"reason": "route coverage cancel", "capabilities": ["ops"], "tenant_id": tenant["id"]},
        )
    )["id"]

    _ok(
        client.post(
            "/agents/%s/nap-schedule" % ctx["nap_agent_id"],
            json={"offset_minutes": 15, "window_minutes": 30, "actor": "operator"},
        )
    )
    ctx["nap_run_id"] = _ok(
        client.post("/agents/%s/nap-runs" % ctx["nap_agent_id"], json={"actor": "operator"})
    )["id"]
    ctx["nap_fail_run_id"] = _ok(
        client.post("/agents/%s/nap-runs" % ctx["nap_fail_agent_id"], json={"actor": "operator"})
    )["id"]

    notification = cp.record_notification(
        "route.coverage",
        "Route coverage notification",
        "A deterministic notification for API route coverage",
        subject_type="task",
        subject_id=base_task["id"],
    )
    ctx["notification_id"] = notification.id
    notifier = _ok(
        client.post(
            "/notifier/channels",
            json={
                "name": "route-channel",
                "channel_type": "slack",
                "enabled": False,
                "target": {"channel": "C-route"},
            },
        )
    )
    ctx["channel_id"] = notifier["id"]
    ctx["delete_channel_id"] = _ok(
        client.post(
            "/notifier/channels",
            json={
                "name": "route-channel-delete",
                "channel_type": "slack",
                "enabled": False,
                "target": {"channel": "C-delete"},
            },
        )
    )["id"]

    communication_identity = _ok(
        client.post(
            "/communication/identities",
            json={
                "name": "route-hive",
                "display_name": "Route Hive",
                "description": "Shared identity for route coverage",
                "is_default": True,
            },
        )
    )
    ctx["communication_identity_id"] = communication_identity["id"]
    ctx["delete_communication_identity_id"] = _ok(
        client.post(
            "/communication/identities",
            json={"name": "route-hive-delete", "display_name": "Delete Route Hive"},
        )
    )["id"]
    communication_account = _ok(
        client.post(
            "/communication/accounts",
            json={
                "identity_id": communication_identity["id"],
                "channel": "slack",
                "account_id": "route-primary",
                "credential_refs": {"bot": "secret://route/slack/bot"},
                "config": {"default": True},
            },
        )
    )
    ctx["communication_account_id"] = communication_account["id"]
    ctx["delete_communication_account_id"] = _ok(
        client.post(
            "/communication/accounts",
            json={
                "identity_id": communication_identity["id"],
                "channel": "telegram",
                "account_id": "route-delete",
            },
        )
    )["id"]
    release_account = _ok(
        client.post(
            "/communication/accounts",
            json={
                "identity_id": communication_identity["id"],
                "channel": "signal",
                "account_id": "route-release",
            },
        )
    )
    representation = _ok(
        client.post(
            "/communication/representations",
            json={
                "subject_kind": "agent",
                "subject_id": default_agent["id"],
                "identity_id": communication_identity["id"],
                "mode": "delegated",
            },
        )
    )
    ctx["representation_binding_id"] = representation["id"]
    ctx["delete_representation_binding_id"] = _ok(
        client.post(
            "/communication/representations",
            json={
                "subject_kind": "project",
                "subject_id": "route-representation-delete",
                "identity_id": communication_identity["id"],
                "mode": "delegated",
            },
        )
    )["id"]
    gateway_lease = _ok(
        client.post(
            "/communication/gateway-leases/acquire",
            json={
                "account_id": communication_account["id"],
                "agent_id": default_agent["id"],
            },
        )
    )
    ctx["gateway_identity_lease_id"] = gateway_lease["id"]
    ctx["gateway_identity_fencing_token"] = gateway_lease["fencing_token"]
    release_lease = _ok(
        client.post(
            "/communication/gateway-leases/acquire",
            json={
                "account_id": release_account["id"],
                "agent_id": default_agent["id"],
            },
        )
    )
    ctx["release_gateway_identity_lease_id"] = release_lease["id"]
    ctx["release_gateway_identity_fencing_token"] = release_lease["fencing_token"]
    for action in ("ack", "fail"):
        delivery = _ok(
            client.post(
                "/communication/deliveries",
                json={
                    "target": "channel:C-route",
                    "body": "Route coverage delivery for %s" % action,
                    "origin_agent_id": default_agent["id"],
                    "identity_id": communication_identity["id"],
                    "account_id": communication_account["id"],
                    "channel": "slack",
                    "idempotency_key": "route-coverage-%s" % action,
                },
            )
        )
        ctx["%s_human_delivery_id" % action] = delivery["id"]
    cp.record_integration_observation(
        "repository",
        "route-repo",
        "github",
        "ok",
        fingerprint="route-observation",
        detail={"branch": "main"},
    )

    _ok(
        client.post(
            "/messages",
            json={
                "sender_agent_id": default_agent["id"],
                "recipient_agent_id": reviewer["id"],
                "message_type": "status_update",
                "payload": {"status": "ok", "text": "route coverage message"},
            },
        )
    )
    stream = _ok(
        client.post(
            "/agentbus/streams",
            json={
                "sender_agent_id": default_agent["id"],
                "recipient_agent_id": reviewer["id"],
                "topic": "route.coverage",
            },
        )
    )
    ctx["stream_id"] = stream["id"]
    _ok(
        client.post(
            "/agentbus/streams/%s/chunks" % stream["id"],
            json={
                "sender_agent_id": default_agent["id"],
                "payload": {"text": "initial chunk"},
            },
        )
    )

    secret = _ok(
        client.post(
            "/secrets",
            json={
                "name": "route-secret",
                "value": "route-secret-value",
                "scopes": {"capabilities": ["python"]},
                "created_by": "operator",
            },
        )
    )
    ctx["secret_id"] = secret["id"]
    ctx["secret_name"] = secret["name"]
    access = _ok(
        client.post(
            "/secrets/%s/access" % secret["id"],
            json={"accessor_agent_id": default_agent["id"], "purpose": "route coverage"},
        )
    )
    ctx["secret_audit_id"] = access["audit_id"]
    ctx["delete_secret_name"] = _ok(
        client.post(
            "/secrets",
            json={
                "name": "route-secret-delete",
                "value": "delete-me",
                "scopes": {"capabilities": ["python"]},
                "created_by": "operator",
            },
        )
    )["name"]

    artifact = _ok(
        client.post(
            "/artifacts",
            json={
                "kind": "runtime",
                "digest": "sha256:" + "1" * 64,
                "uri": "https://example.test/artifacts/runtime.tar",
                "created_by": "operator",
            },
        )
    )
    ctx["artifact_id"] = artifact["id"]
    ctx["delete_artifact_id"] = _ok(
        client.post(
            "/artifacts",
            json={
                "kind": "runtime",
                "digest": "sha256:" + "9" * 64,
                "uri": "https://example.test/artifacts/delete-me.tar",
                "created_by": "operator",
            },
        )
    )["id"]
    thread = _ok(
        client.post(
            "/conversation-threads",
            json={
                "platform_binding_id": binding["id"],
                "external_thread_id": "thread-route",
                "summary": "route coverage thread",
                "latest_task_id": base_task["id"],
            },
        )
    )
    ctx["thread_id"] = thread["id"]
    memory = _ok(
        client.post(
            "/memory",
            json={
                "task_id": base_task["id"],
                "subject_type": "task",
                "subject_id": base_task["id"],
                "record_type": "summary",
                "content": "Route coverage memory record",
                "created_by": "operator",
            },
        )
    )
    ctx["memory_id"] = memory["id"]
    _ok(
        client.post(
            "/vector-refs",
            json={
                "memory_id": memory["id"],
                "vector_db": "qdrant",
                "collection": "mac_memory_medium",
                "point_id": "route-point-1",
                "embedding_model": "test-embed",
            },
        )
    )

    environment = _ok(
        client.post(
            "/environments",
            json={"name": "route-env", "tenant_id": tenant["id"], "channel": "fleet"},
        )
    )
    ctx["env_id"] = environment["id"]
    runtime = _ok(
        client.post(
            "/runtimes",
            json={"name": "route-runtime", "manifest": _runtime_manifest(), "created_by": "operator"},
        )
    )
    ctx["runtime_id"] = runtime["id"]
    ctx["runtime_digest"] = runtime["digest"]
    delta = _ok(client.post("/runtime-deltas", json=_delta_payload(ctx)))
    ctx["delta_id"] = delta["id"]
    ctx["validate_delta_id"] = _ok(client.post("/runtime-deltas", json=_delta_payload(ctx)))["id"]
    ctx["reject_delta_id"] = _ok(client.post("/runtime-deltas", json=_delta_payload(ctx)))["id"]
    promote_delta = _ok(client.post("/runtime-deltas", json=_delta_payload(ctx)))
    cp.validate_runtime_delta(promote_delta["id"], "operator")
    ctx["promote_delta_id"] = promote_delta["id"]
    runtime_evidence = cp.add_evidence(
        ctx["runtime_task_id"],
        "test",
        "artifact://runtime-evidence",
        "runtime route evidence",
        default_agent["id"],
        metadata={"verification": _operator_manifest("runtime route evidence")},
        artifacts=[
            {
                "name": "route-runtime-evidence.txt",
                "artifact_type": "test-output",
                "source_uri": "artifact://route/runtime-evidence.txt",
                "content_type": "text/plain; charset=utf-8",
                "content_base64": base64.b64encode(b"route runtime durable artifact\n").decode("ascii"),
            }
        ],
        sync_beads=False,
    )
    runtime_run = _ok(
        client.post(
            "/runtime-runs",
            json={
                "task_id": ctx["runtime_task_id"],
                "agent_id": default_agent["id"],
                "environment_id": runtime["id"],
            },
        )
    )
    ctx["runtime_run_id"] = runtime_run["id"]
    ctx["runtime_evidence_id"] = runtime_evidence.id
    ctx["evidence_artifact_id"] = cp.list_evidence_artifacts(runtime_evidence.id)[0]["id"]

    eval_set = _ok(
        client.post(
            "/eval-sets",
            json={
                "name": "route-eval",
                "baseline_score": 0.75,
                "regression_threshold": 0.05,
                "created_by": "operator",
            },
        )
    )
    ctx["eval_set_id"] = eval_set["id"]
    _ok(
        client.post(
            "/eval-runs",
            json={
                "eval_set_id": eval_set["id"],
                "target_kind": "runtime_environment",
                "target_id": runtime["id"],
                "score": 0.8,
                "detail": {"suite": "route"},
            },
        )
    )

    def rollout(version: str) -> Dict[str, Any]:
        return _ok(
            client.post(
                "/rollouts",
                json={
                    "version": version,
                    "strategy": "full",
                    "target_percent": 0,
                    "created_by": "operator",
                    "tenant_id": tenant["id"],
                    "runtime_environment_id": runtime["id"],
                    "artifact_uri": "https://example.test/artifacts/%s.tar" % version,
                    "artifact_hash": "sha256:" + "2" * 64,
                    "health_policy": {"required_checks": ["runtime"]},
                },
            )
        )

    ctx["rollout_id"] = rollout("route-rollout")["id"]
    ctx["advance_rollout_id"] = rollout("route-rollout-advance")["id"]
    ctx["artifact_rollout_id"] = rollout("route-rollout-artifact")["id"]
    ctx["health_rollout_id"] = rollout("route-rollout-health")["id"]
    ctx["rescue_rollout_id"] = rollout("route-rollout-rescue")["id"]

    return ctx


def _path_for(method: str, path_template: str, ctx: Mapping[str, Any]) -> str:
    special: Dict[RouteKey, Dict[str, str]] = {
        ("POST", "/tasks/{task_id}/children"): {"task_id": "parent_task_id"},
        ("DELETE", "/tasks/{task_id}"): {"task_id": "delete_task_id"},
        ("POST", "/tasks/{task_id}/transition"): {"task_id": "transition_task_id"},
        ("POST", "/tasks/{task_id}/reopen"): {"task_id": "reopen_task_id"},
        ("POST", "/tasks/{task_id}/force-complete"): {"task_id": "force_complete_task_id"},
        ("POST", "/tasks/{task_id}/claim"): {"task_id": "claim_task_id"},
        ("POST", "/tasks/{task_id}/start"): {"task_id": "start_task_id"},
        ("POST", "/tasks/{task_id}/submit-for-review"): {"task_id": "submit_task_id"},
        ("POST", "/tasks/{task_id}/evidence"): {"task_id": "evidence_task_id"},
        ("POST", "/tasks/{task_id}/break-glass-authorizations"): {
            "task_id": "break_glass_task_id"
        },
        ("POST", "/break-glass-authorizations/{authorization_id}/revoke"): {
            "authorization_id": "break_glass_authorization_id"
        },
        ("POST", "/tasks/{task_id}/reviews"): {"task_id": "review_task_id"},
        ("DELETE", "/fleets/{fleet_id_or_name}"): {"fleet_id_or_name": "delete_fleet_id"},
        ("DELETE", "/projects/{project}"): {"project": "delete_project_name"},
        ("POST", "/agents/{agent_id}/attestation-key/rotate"): {"agent_id": "attest_rotate_agent_id"},
        ("POST", "/agents/{agent_id}/attestation-key/verify"): {"agent_id": "attest_verify_agent_id"},
        ("POST", "/agents/{agent_id}/disable"): {"agent_id": "disable_agent_id"},
        ("DELETE", "/agents/{agent_id}"): {"agent_id": "delete_agent_id"},
        ("POST", "/agents/bulk"): {},
        ("POST", "/agents/{agent_id}/dispatch-hold"): {"agent_id": "dispatch_hold_agent_id"},
        ("DELETE", "/agents/{agent_id}/dispatch-hold"): {"agent_id": "dispatch_hold_agent_id"},
        ("POST", "/agents/{agent_id}/claim-next"): {"agent_id": "claim_next_agent_id"},
        ("POST", "/agents/{agent_id}/nap-runs"): {"agent_id": "nap_begin_agent_id"},
        ("GET", "/agents/{agent_id}/nap-schedule"): {"agent_id": "nap_agent_id"},
        ("GET", "/agents/{agent_id}/nap-schedule/next"): {"agent_id": "nap_agent_id"},
        ("POST", "/agents/{agent_id}/nap-schedule"): {"agent_id": "nap_agent_id"},
        ("PUT", "/agents/{agent_id}/nap-schedule"): {"agent_id": "nap_agent_id"},
        ("POST", "/agents/{agent_id}/nap-cycle"): {"agent_id": "nap_agent_id"},
        ("POST", "/agents/{agent_id}/nap-consolidate"): {"agent_id": "nap_agent_id"},
        ("POST", "/agents/{agent_id}/service-claims/sync"): {"agent_id": "agent_id"},
        ("GET", "/nap-runs/{run_id}"): {"run_id": "nap_run_id"},
        ("POST", "/nap-runs/{run_id}/complete"): {"run_id": "nap_run_id"},
        ("POST", "/nap-runs/{run_id}/fail"): {"run_id": "nap_fail_run_id"},
        ("POST", "/provisioning/requests/{request_id}/cancel"): {"request_id": "cancel_request_id"},
        ("DELETE", "/roles/{role_id}"): {"role_id": "delete_role_id"},
        ("DELETE", "/workflows/{workflow_id}"): {"workflow_id": "delete_workflow_id"},
        ("DELETE", "/artifacts/{artifact_id_or_digest}"): {"artifact_id_or_digest": "delete_artifact_id"},
        ("POST", "/runtime-deltas/{delta_id}/validate"): {"delta_id": "validate_delta_id"},
        ("POST", "/runtime-deltas/{delta_id}/reject"): {"delta_id": "reject_delta_id"},
        ("POST", "/runtime-deltas/{delta_id}/promote"): {"delta_id": "promote_delta_id"},
        ("POST", "/runtime-runs/{run_id}/complete"): {"run_id": "runtime_run_id"},
        ("POST", "/rollouts/{rollout_id}/advance"): {"rollout_id": "advance_rollout_id"},
        ("POST", "/rollouts/{rollout_id}/artifact"): {"rollout_id": "artifact_rollout_id"},
        ("POST", "/rollouts/{rollout_id}/health"): {"rollout_id": "health_rollout_id"},
        ("POST", "/rollouts/{rollout_id}/rescue"): {"rollout_id": "rescue_rollout_id"},
        ("DELETE", "/notifier/channels/{channel_id_or_name}"): {"channel_id_or_name": "delete_channel_id"},
        ("DELETE", "/communication/identities/{identity_id_or_name}"): {
            "identity_id_or_name": "delete_communication_identity_id"
        },
        ("DELETE", "/communication/accounts/{account_id}"): {
            "account_id": "delete_communication_account_id"
        },
        ("DELETE", "/communication/representations/{binding_id}"): {
            "binding_id": "delete_representation_binding_id"
        },
        ("POST", "/communication/gateway-leases/{lease_id}/renew"): {
            "lease_id": "gateway_identity_lease_id"
        },
        ("POST", "/communication/gateway-leases/{lease_id}/release"): {
            "lease_id": "release_gateway_identity_lease_id"
        },
        ("POST", "/communication/deliveries/{delivery_id}/ack"): {
            "delivery_id": "ack_human_delivery_id"
        },
        ("POST", "/communication/deliveries/{delivery_id}/fail"): {
            "delivery_id": "fail_human_delivery_id"
        },
        ("DELETE", "/secrets/{name}"): {"name": "delete_secret_name"},
        ("GET", "/optimizer/policies/{policy_id}"): {"policy_id": "sci_policy_id"},
        ("POST", "/optimizer/policies/{policy_id}/promote"): {"policy_id": "sci_policy_id"},
        ("POST", "/optimizer/projects/{project}/rollback/{policy_id}"): {"policy_id": "sci_policy_id"},
        ("GET", "/optimizer/experiments/{experiment_id}"): {"experiment_id": "sci_experiment_id"},
        ("POST", "/optimizer/experiments/{experiment_id}/start"): {"experiment_id": "sci_experiment_id"},
        ("POST", "/optimizer/experiments/{experiment_id}/pause"): {"experiment_id": "sci_experiment_id"},
        ("POST", "/optimizer/experiments/{experiment_id}/promote"): {"experiment_id": "sci_experiment2_id"},
        ("GET", "/optimizer/experiments/{experiment_id}/evidence"): {"experiment_id": "sci_experiment_id"},
        ("POST", "/optimizer/experiments/{experiment_id}/observe/{task_id}"): {"experiment_id": "sci_experiment_id"},
        ("POST", "/optimizer/experiments/{experiment_id}/analyze"): {"experiment_id": "sci_experiment_id"},
    }
    values = {
        "service_id": "qdrant",
        "agent_id": ctx["agent_id"],
        "artifact_id_or_digest": ctx["artifact_id"],
        "channel_id_or_name": ctx["channel_id"],
        "delta_id": ctx["delta_id"],
        "draft_id": ctx["draft_id"],
        "env_id": ctx["env_id"],
        "evidence_id": ctx["runtime_evidence_id"],
        "experiment_id": "route-review-experiment",
        "eval_set_id": ctx["eval_set_id"],
        "flag": "show_reasoning",
        "fleet_id_or_name": ctx["fleet_id"],
        "instance_id": ctx["instance_id"],
        "artifact_id": ctx["evidence_artifact_id"],
        "identity_id_or_name": ctx["communication_identity_id"],
        "account_id": ctx["communication_account_id"],
        "binding_id": ctx["representation_binding_id"],
        "delivery_id": ctx["ack_human_delivery_id"],
        "key": "route-memory-key",
        "lease_id": ctx["lease_id"],
        "machine_id": ctx["machine_id"],
        "name": ctx["secret_name"],
        "notification_id": ctx["notification_id"],
        "policy_id": ctx["openshell_policy_id"],
        "sci_policy_id": ctx["sci_policy_id"],
        "sci_experiment_id": ctx["sci_experiment_id"],
        "sci_experiment2_id": ctx["sci_experiment2_id"],
        "project": ctx["project_name"],
        "request_id": ctx["request_id"],
        "review_id": ctx["review_id"],
        "role_id": ctx["role_id"],
        "role_id_or_slug": ctx["role_slug"],
        "rollout_id": ctx["rollout_id"],
        "run_id": ctx["workflow_run_id"],
        "secret_id": ctx["secret_id"],
        "session_id": ctx["terminal_session_id"],
        "stream_id": ctx["stream_id"],
        "task_id": ctx["task_id"],
        "thread_id": ctx["thread_id"],
        "workflow_id": ctx["workflow_id"],
        "workflow_id_or_slug": ctx["workflow_id"],
    }
    for param, ctx_key in special.get((method, path_template), {}).items():
        values[param] = ctx[ctx_key]
    path = path_template
    for param, value in values.items():
        path = path.replace("{%s}" % param, str(value))
    return path


def _case_for(method: str, path_template: str, ctx: Mapping[str, Any]) -> RequestCase:
    path = _path_for(method, path_template, ctx)
    kwargs: Dict[str, Any] = {}
    expected = (200,)

    if method == "POST" and path_template.startswith("/optimizer/experiments/{experiment_id}/"):
        # Realistic guard responses count as coverage for lifecycle routes:
        # start (400: one active experiment per project — exp2 is running),
        # pause of a non-active exp (400), promote without min validated
        # samples (400), observe of an unassigned task (404). The happy paths
        # for create/start are exercised by the ctx fixtures themselves.
        expected = (200, 400, 404)
    if method == "GET":
        if path_template == "/dashboard/service-links/tokenhub/sso":
            kwargs["follow_redirects"] = False
            expected = (303,)
        elif path_template == "/agentbus/streams/{stream_id}/chunks":
            kwargs["params"] = {"agent_id": ctx["reviewer_agent_id"], "after_sequence": 0, "limit": 10}
        elif path_template == "/agentbus/streams/{stream_id}/events":
            kwargs["params"] = {
                "agent_id": ctx["reviewer_agent_id"],
                "after_sequence": 0,
                "timeout_seconds": 0,
            }
        elif path_template == "/dashboard/terminal-sessions/{session_id}/events":
            kwargs["params"] = {
                "output_stream_id": ctx["terminal_output_stream_id"],
                "after_sequence": 0,
                "timeout_seconds": 0,
                "poll_interval_seconds": 0.25,
            }
        elif path_template == "/observability/stream":
            kwargs["params"] = {"after_sequence": 0, "timeout_seconds": 0, "poll_interval_seconds": 0.25}
        elif path_template == "/action-events/stream":
            kwargs["params"] = {"timeout_seconds": 0, "poll_interval_seconds": 0.25}
        elif path_template == "/dashboard/stream":
            kwargs["params"] = {"timeout_seconds": 0, "poll_interval_seconds": 0.25}
        elif path_template == "/v1/memory/recall":
            kwargs["params"] = {"q": "route coverage", "limit": 1}
        elif path_template == "/v1/memory/dreams/recall":
            kwargs["params"] = {"q": "route coverage dream", "limit": 1, "min_confidence": "low"}
        elif path_template == "/tasks/search":
            kwargs["params"] = {"q": "route coverage"}
        return RequestCase(path, kwargs, expected)

    if method == "DELETE":
        if path_template in {"/agents/{agent_id}/mood", "/v1/agents/{agent_id}/mood"}:
            kwargs["json"] = {"cleared_by": "operator", "reason": "route coverage"}
        elif path_template == "/v1/agents/{agent_id}/config-flags/{flag}":
            kwargs["json"] = {"channel": "slack:CROUTE", "reason": "route coverage"}
        return RequestCase(path, kwargs, expected)

    bodies: Dict[RouteKey, Dict[str, Any]] = {
        ("POST", "/tenants"): {"name": "Route Coverage Tenant Case"},
        ("POST", "/users"): {"tenant_id": ctx["tenant_id"], "handle": "operator-case"},
        ("POST", "/personas"): {
            "tenant_id": ctx["tenant_id"],
            "name": "Case Persona",
            "soul_ref": "hermes://route/case/SOUL.md",
            "memory_scope": "hermes://route/case/memory",
        },
        ("POST", "/hermes-instances"): {
            "tenant_id": ctx["tenant_id"],
            "name": "case-hermes",
            "persona_id": ctx["persona_id"],
            "home_ref": "hermes://route/case",
        },
        ("POST", "/hermes-instances/{instance_id}/runtime-proof"): {
            "hermes_startup": {
                "task_project_runtime": {
                    "required": True,
                    "ready": True,
                    "hermes_instance_id": ctx["instance_id"],
                    "prompt_bridge": {"required": True, "present": True},
                    "markdown_contract": {"ready": True, "missing_snippets": []},
                    "first_class_object_names": ["fleets", "tasks", "projects", "agents"],
                    "session_capability_names": ["mac_api", "quality_gate"],
                    "session_capability_availability": {"ready": True, "missing": []},
                }
            }
        },
        ("POST", "/hermes-instances/{instance_id}/tasks"): {
            "title": "case hermes task",
            "project": ctx["project_name"],
            "platform_binding_id": ctx["platform_binding_id"],
            "conversation_ref": "slack://C-route/123",
        },
        ("POST", "/platform-bindings"): {
            "tenant_id": ctx["tenant_id"],
            "hermes_instance_id": ctx["instance_id"],
            "platform": "slack",
            "external_id": "C-route-case",
        },
        ("POST", "/tasks"): {
            "title": "case task",
            "project": ctx["project_name"],
            "required_capabilities": ["python"],
        },
        ("PUT", "/tasks/{task_id}"): {
            "title": "route base task updated",
            "priority": 5,
            "metadata": {"route_case": True},
        },
        ("POST", "/tasks/{task_id}/review-experiment"): {
            "experiment_id": "route-review-experiment",
            "arm": "standard",
            "actor": "route-coverage",
        },
        ("POST", "/tasks/{task_id}/review-outcomes"): {
            "kind": "clean_window",
            "status": "confirmed",
            "severity_weight": 0,
            "source": "route-coverage",
            "detail": {"window_days": 0},
            "actor": "route-coverage",
        },
        ("POST", "/repositories/onboard"): {
            "repository_url": "https://github.com/example/route-coverage.git",
            "required_capabilities": ["python"],
        },
        ("POST", "/a2a"): {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg-route-coverage",
                    "parts": [{"kind": "text", "text": "route coverage a2a task"}],
                }
            },
        },
        ("POST", "/tasks/{task_id}/children"): {
            "children": [{"title": "child route task", "required_capabilities": ["python"]}]
        },
        ("POST", "/fleets"): {"name": "route-fleet-case", "agent_ids": [ctx["agent_id"]]},
        ("PUT", "/fleets/{fleet_id_or_name}"): {
            "description": "updated route fleet",
            "agent_ids": [ctx["agent_id"]],
            "metadata": {"route_case": True},
        },
        ("POST", "/fleets/{fleet_id_or_name}/observed-agents"): {
            "agent_id": ctx["agent_id"],
            "source": "route-test",
            "metadata": {"seen": True},
        },
        ("PUT", "/dashboard/hermes/fleets/{fleet_id_or_name}/config-surface"): {
            "runtime": {
                "gateway_model": "route-coverage-model",
                "gateway_provider": "custom",
            },
            "config": {"route.coverage.enabled": True},
            "env": {"ROUTE_COVERAGE_TOKEN": "route-token"},
            "plugins": {"enabled": ["route-plugin"], "disabled": []},
            "skills": {"disabled": ["route-skill"]},
            "apply_local": False,
        },
        ("POST", "/projects"): {"name": "route-project-case", "description": "created by route coverage"},
        ("PUT", "/projects/{project}"): {
            "description": "updated route project",
            "metadata": {"route_case": True},
        },
        ("POST", "/tasks/{task_id}/transition"): {
            "target_state": "blocked",
            "actor": "operator",
            "detail": {"reason": "route coverage transition"},
        },
        ("POST", "/tasks/{task_id}/reopen"): {
            "actor": "operator",
            "reason": "route coverage reopen",
        },
        ("POST", "/tasks/{task_id}/force-complete"): {
            "actor": "operator",
            "reason": "route coverage force-complete",
        },
        ("POST", "/leases/{lease_id}/renew"): {"agent_id": ctx["lease_agent_id"], "lease_seconds": 120},
        ("POST", "/leases/{lease_id}/delegate"): {
            "agent_id": ctx["lease_agent_id"],
            "to_agent_id": ctx["delegate_agent_id"],
        },
        ("POST", "/tasks/{task_id}/evidence"): {
            "kind": "test",
            "uri": "artifact://route-evidence",
            "summary": "route evidence summary",
            "created_by": ctx["agent_id"],
            "metadata": {"verification": _operator_manifest("route evidence summary is substantive")},
        },
        ("POST", "/machines"): {"hostname": "route-host-case", "resources": {"cpu": 4}},
        ("POST", "/agents"): {
            "machine_id": ctx["machine_id"],
            "name": "route-agent-case",
            "capabilities": ["python"],
        },
        ("PUT", "/agents/{agent_id}"): {"health_status": "healthy", "resources": {"cpu": 8}},
        ("POST", "/agents/{agent_id}/attestation-key/verify"): {
            "challenge": {
                "schema": "mac.agent_attestation_challenge.v1",
                "purpose": "route-coverage",
                "agent_id": ctx["attest_verify_agent_id"],
                "nonce": "route-nonce",
            },
            "signature": sign_verification_manifest(
                ctx["attest_verify_key"],
                {
                    "schema": "mac.agent_attestation_challenge.v1",
                    "purpose": "route-coverage",
                    "agent_id": ctx["attest_verify_agent_id"],
                    "nonce": "route-nonce",
                },
            ),
        },
        ("POST", "/agents/bulk"): {"agent_ids": [ctx["bulk_agent_id"]], "health_status": "healthy"},
        ("POST", "/agents/{agent_id}/dispatch-hold"): {"reason": "route-coverage quarantine"},
        ("POST", "/tasks/{task_id}/break-glass-authorizations"): {
            "agent_id": ctx["agent_id"],
            "reason": "route coverage host repair",
            "ttl_seconds": 300,
        },
        ("POST", "/break-glass-authorizations/{authorization_id}/revoke"): {
            "reason": "route coverage revocation"
        },
        ("POST", "/roles"): {
            "slug": "route-role-case",
            "name": "Route Role Case",
            "description": "case role",
            "system_prompt": "handle the case",
            "level": "ic",
        },
        ("PUT", "/roles/{role_id}"): {"description": "updated qa route role"},
        ("POST", "/roles/seed"): {},
        ("POST", "/agents/{agent_id}/role"): {"role_id_or_slug": ctx["role_slug"]},
        ("POST", "/provisioning/requests"): {
            "reason": "case provision",
            "capabilities": ["python"],
            "tenant_id": ctx["tenant_id"],
        },
        ("POST", "/provisioning/requests/{request_id}/fulfill"): {"agent_id": ctx["agent_id"]},
        ("POST", "/provisioning/requests/{request_id}/cancel"): {"reason": "route coverage cancel"},
        ("POST", "/workflows"): {
            "slug": "route-workflow-case",
            "name": "Route Workflow Case",
            "description": "route workflow case",
            "workflow_type": "custom",
            "definition": _workflow_definition(),
            "created_by": "operator",
        },
        ("PUT", "/workflows/{workflow_id}"): {"description": "updated route workflow"},
        ("POST", "/workflows/preview"): {
            "definition": _workflow_definition(),
            "input": {"ticket": "preview"},
        },
        ("POST", "/workflows/drafts"): {
            "goal": "case draft",
            "proposed_steps": [{"node_key": "check", "role_required": "qa"}],
        },
        ("PUT", "/workflows/drafts/{draft_id}"): {"answers": {"scope": "updated"}},
        ("POST", "/workflows/drafts/{draft_id}/preview"): {"input": {"scope": "route"}},
        ("POST", "/workflows/drafts/{draft_id}/approve"): {
            "slug": "route-draft-approved",
            "name": "Route Draft Approved",
        },
        ("POST", "/dashboard/workflow-plan/preview"): {
            "goal": "route coverage workflow plan",
            "project": ctx["project_name"],
            "required_capabilities": ["python"],
            "max_tasks": 2,
            "context": {"route": True},
        },
        ("POST", "/dashboard/workflow-plan/accept"): {
            "goal": "route coverage accepted workflow",
            "project": ctx["project_name"],
            "plan_id": "plan_route_accept",
            "nodes": [
                {
                    "node_id": "plan",
                    "title": "Route plan task",
                    "description": "Plan the route coverage task chain.",
                    "required_capabilities": ["python"],
                },
                {
                    "node_id": "verify",
                    "title": "Route verify task",
                    "description": "Verify the route coverage task chain.",
                    "required_capabilities": ["python"],
                    "depends_on": ["plan"],
                },
            ],
        },
        ("POST", "/dashboard/hermes/fleets/{fleet_id_or_name}/config-surface/apply"): {
            "sender_agent_id": ctx["agent_id"],
            "request_id": "route-hermes-apply",
        },
        ("POST", "/dashboard/agents/{agent_id}/terminal-sessions"): {
            "sender_agent_id": ctx["agent_id"],
            "shell": "/bin/sh",
            "rows": 24,
            "cols": 100,
            "ttl_seconds": 60,
            "request_id": "route-terminal-open",
        },
        ("POST", "/dashboard/terminal-sessions/{session_id}/input"): {
            "input_stream_id": ctx["terminal_input_stream_id"],
            "data": "echo route\n",
        },
        ("POST", "/dashboard/terminal-sessions/{session_id}/resize"): {
            "input_stream_id": ctx["terminal_input_stream_id"],
            "rows": 30,
            "cols": 120,
        },
        ("POST", "/dashboard/terminal-sessions/{session_id}/close"): {
            "input_stream_id": ctx["terminal_input_stream_id"],
        },
        ("POST", "/workflows/{workflow_id_or_slug}/preview"): {"input": {"ticket": "route"}},
        ("POST", "/workflows/import-yaml"): {
            "yaml": """
id: route-yaml
name: Route YAML
description: imported route workflow
workflow_type: custom
nodes:
  - node_key: investigate
    node_type: task
    role_required: QA
    max_attempts: 1
edges:
  - from_node_key: ""
    to_node_key: investigate
    condition: success
    priority: 100
  - from_node_key: investigate
    to_node_key: ""
    condition: success
    priority: 100
""",
            "created_by": "operator",
        },
        ("POST", "/workflows/seed"): {},
        ("POST", "/workflows/{workflow_id_or_slug}/start"): {
            "started_by": "operator",
            "input": {"ticket": "route-start"},
        },
        ("POST", "/workflows/runs/{run_id}/cancel"): {"reason": "route coverage", "actor": "operator"},
        ("POST", "/agents/{agent_id}/mood"): {"mode": "warm", "set_by": "operator"},
        ("PUT", "/agents/{agent_id}/mood"): {"mode": "cheerful", "set_by": "operator"},
        ("POST", "/v1/agents/{agent_id}/mood"): {"mode": "warm", "reason": "route coverage"},
        ("PUT", "/v1/agents/{agent_id}/config-flags/{flag}"): {
            "value": True,
            "channel": "slack:CROUTE",
            "reason": "route coverage",
        },
        ("POST", "/v1/agents/{agent_id}/memory"): {
            "content": "route coverage learning: the hub answers on :8789",
            "record_type": "agent_learning:route_coverage",
        },
        ("POST", "/agents/{agent_id}/nap-schedule"): {"offset_minutes": 20, "window_minutes": 30},
        ("PUT", "/agents/{agent_id}/nap-schedule"): {"offset_minutes": 25, "window_minutes": 30},
        ("POST", "/agents/{agent_id}/nap-runs"): {"actor": "operator"},
        ("POST", "/nap-runs/{run_id}/complete"): {"actor": "operator", "detail": {"summary": "rested"}},
        ("POST", "/nap-runs/{run_id}/fail"): {"actor": "operator", "reason": "route coverage failure case"},
        ("POST", "/agents/{agent_id}/nap-cycle"): {
            "actor": "operator",
            "embed_into_medium": False,
            "emit_dream_artifacts": True,
        },
        ("POST", "/agents/{agent_id}/nap-consolidate"): {
            "embed_into_medium": False,
            "emit_dream_artifacts": True,
            "created_by": "operator",
        },
        ("POST", "/agents/{agent_id}/heartbeat"): {
            "status": "idle",
            "health_status": "healthy",
            "resources": {"cpu": 8},
        },
        ("POST", "/agents/{agent_id}/reflect"): {
            "recipient_agent_id": ctx["reviewer_agent_id"],
            "request_id": "route-reflect",
        },
        ("POST", "/agents/{agent_id}/claim-next"): {"lease_seconds": 60, "capabilities": ["python"]},
        ("POST", "/agents/{agent_id}/service-claims/sync"): {
            "willing_ops": ["image.generate"],
            "lease_seconds": 60,
        },
        ("POST", "/agents/{agent_id}/command-audit"): {
            "phase": "completed",
            "argv": ["pytest", "tests/api/test_api_route_coverage.py"],
            "cwd": "/repo",
            "task_id": ctx["task_id"],
            "returncode": 0,
        },
        ("POST", "/openshell/policies"): {
            "name": "route-policy-case",
            "description": "case OpenShell policy",
            "policy_text": "version: 1\nnetwork_policies: {}\n",
            "created_by": "operator",
        },
        ("PUT", "/openshell/policies/{policy_id}"): {
            "description": "updated route OpenShell policy",
            "metadata": {"route_case": True},
            "updated_by": "operator",
        },
        ("POST", "/openshell/policies/{policy_id}/render"): {},
        ("POST", "/openshell/policies/{policy_id}/assignments"): {
            "target_type": "agent",
            "target_id": ctx["agent_id"],
            "created_by": "operator",
        },
        ("POST", "/agents/{agent_id}/openshell/status"): {
            "status": "active",
            "sandbox_id": "sandbox-route-case",
            "policy_id": ctx["openshell_policy_id"],
            "policy_version": 1,
            "detail": {"route_case": True},
        },
        ("POST", "/action-events"): {
            "agent_id": ctx["agent_id"],
            "actor": ctx["agent_id"],
            "action_type": "route.coverage",
            "action_name": "case",
            "subject_type": "agent",
            "subject_id": ctx["agent_id"],
            "outcome": "success",
            "attributes": {"route_case": True},
        },
        ("POST", "/agents/{agent_id}/installed-packages"): {
            "installed_packages": {"python": {"version": "3.11"}}
        },
        ("POST", "/dispatch/assign"): {"lease_seconds": 60},
        ("POST", "/dispatch/tick"): {"lease_seconds": 60, "limit": 2},
        ("POST", "/repository-refs/reconcile"): {
            "mode": "off",
            "actor": "operator",
        },
        ("POST", "/github-ingest/run"): {},
        ("POST", "/backlog-groom/run"): {},
        ("POST", "/nap-tick/run"): {},
        ("POST", "/curiosity-review/run"): {},
        ("POST", "/self-heal/run"): {},
        ("POST", "/model-selection/refresh"): {},
        ("POST", "/model-selection/promote"): {},
        ("POST", "/observability/metrics"): {
            "name": "route.metric",
            "value": 1.0,
            "unit": "count",
            "layer": "test",
            "source": "route-coverage",
            "subject_type": "task",
            "subject_id": ctx["task_id"],
        },
        ("POST", "/observability/logs"): {
            "name": "route.log",
            "level": "info",
            "layer": "test",
            "source": "route-coverage",
            "detail": {"ok": True},
        },
        ("POST", "/observability/prune"): {"keep_last": 100},
        ("POST", "/memory/remembered"): {
            "key": "route-memory-key",
            "content": "route remembered memory content",
            "project": "mac",
            "actor": "operator",
        },
        ("POST", "/memory/summarize-actions"): {
            "agent_id": ctx["agent_id"],
            "created_by": "operator",
            "write": False,
        },
        ("POST", "/integrations/findings"): {
            "source_kind": "repository",
            "source_id": "route-repo",
            "finding_type": "route.coverage",
            "title": "Route coverage finding",
            "severity": "info",
            "notify": True,
        },
        ("POST", "/notifications/{notification_id}/delivered"): {"status": "delivered"},
        ("POST", "/notifier/channels"): {
            "name": "route-channel-case",
            "channel_type": "slack",
            "enabled": False,
            "target": {"channel": "C-case"},
        },
        ("POST", "/notifier/deliver"): {"limit": 5},
        ("POST", "/communication/identities"): {
            "name": "route-hive-case",
            "display_name": "Route Hive Case",
        },
        ("POST", "/communication/accounts"): {
            "identity_id": ctx["communication_identity_id"],
            "channel": "mattermost",
            "account_id": "route-case",
            "credential_refs": {"token": "secret://route/mattermost/token"},
        },
        ("POST", "/communication/representations"): {
            "subject_kind": "project",
            "subject_id": "route-representation-case",
            "identity_id": ctx["communication_identity_id"],
            "mode": "delegated",
        },
        ("POST", "/communication/gateway-leases/acquire"): {
            "account_id": ctx["communication_account_id"],
            "agent_id": ctx["agent_id"],
            "lease_seconds": 120,
        },
        ("POST", "/communication/gateway-leases/{lease_id}/renew"): {
            "agent_id": ctx["agent_id"],
            "fencing_token": ctx["gateway_identity_fencing_token"],
            "lease_seconds": 120,
        },
        ("POST", "/communication/gateway-leases/{lease_id}/release"): {
            "agent_id": ctx["agent_id"],
            "fencing_token": ctx["release_gateway_identity_fencing_token"],
        },
        ("POST", "/communication/deliveries"): {
            "target": "channel:C-route",
            "body": "Route coverage outbound message",
            "origin_agent_id": ctx["agent_id"],
            "identity_id": ctx["communication_identity_id"],
            "account_id": ctx["communication_account_id"],
            "channel": "slack",
            "idempotency_key": "route-coverage-case",
        },
        ("POST", "/communication/deliveries/claim"): {
            "agent_id": ctx["agent_id"],
            "limit": 20,
            "lease_seconds": 60,
        },
        ("POST", "/communication/deliveries/{delivery_id}/ack"): {
            "agent_id": ctx["agent_id"],
            "provider_message_id": "route-provider-message",
            "detail": {"route_case": True},
        },
        ("POST", "/communication/deliveries/{delivery_id}/fail"): {
            "agent_id": ctx["agent_id"],
            "error": "route coverage retry",
            "retryable": True,
        },
        ("POST", "/messages"): {
            "sender_agent_id": ctx["agent_id"],
            "recipient_agent_id": ctx["reviewer_agent_id"],
            "message_type": "status_update",
            "payload": {"status": "ok", "text": "case message"},
        },
        ("POST", "/agents/{agent_id}/messages/deliver"): {},
        ("POST", "/agentbus/streams"): {
            "sender_agent_id": ctx["agent_id"],
            "recipient_agent_id": ctx["reviewer_agent_id"],
            "topic": "route.case",
        },
        ("POST", "/agentbus/streams/{stream_id}/chunks"): {
            "sender_agent_id": ctx["agent_id"],
            "payload": {"text": "case chunk"},
        },
        ("POST", "/agentbus/streams/{stream_id}/close"): {},
        ("POST", "/agentbus"): {
            "sender_agent_id": ctx["agent_id"],
            "recipient_agent_id": ctx["reviewer_agent_id"],
            "topic": "route.case.publish",
            "payload": {"text": "published"},
        },
        ("POST", "/agentbus/repo-update"): {
            "sender_agent_id": ctx["agent_id"],
            "recipient_agent_ids": [ctx["reviewer_agent_id"]],
            "repo_path": "/repo",
            "remote": "origin",
            "branch": "main",
            "restart": False,
        },
        ("POST", "/source-convergence/tick"): {},
        ("POST", "/agentbus/artifact-publish"): {
            "sender_agent_id": ctx["agent_id"],
            "recipient_agent_ids": [ctx["reviewer_agent_id"]],
            "digest": "sha256:" + "8" * 64,
            "path": "route/artifact.txt",
            "public_url": "https://example.test/artifacts/route/artifact.txt",
            "metadata": {"route_case": True},
        },
        ("POST", "/tasks/{task_id}/reviews"): {
            "reviewer_agent_id": ctx["reviewer_agent_id"],
            "actor": "dispatcher",
        },
        ("POST", "/reviews/{review_id}/claim"): {
            "reviewer_agent_id": ctx["reviewer_agent_id"],
            "executor_evidence_id": ctx["executor_evidence_id"],
        },
        ("POST", "/reviews/{review_id}/decision"): {
            "status": "changes_requested",
            "reviewer_agent_id": ctx["reviewer_agent_id"],
            "reason": "route coverage review path",
        },
        ("POST", "/publications"): {
            "task_id": ctx["publication_task_id"],
            "target": "test://route-publication",
            "created_by": "operator",
            "evidence_id": ctx["publication_evidence_id"],
        },
        ("POST", "/secrets"): {
            "name": "route-secret-case",
            "value": "case-secret",
            "scopes": {"capabilities": ["python"]},
            "created_by": "operator",
        },
        ("POST", "/secrets/{secret_id}/access"): {
            "accessor_agent_id": ctx["agent_id"],
            "purpose": "route coverage",
        },
        ("POST", "/secrets/{secret_id}/reveal"): {
            "accessor_agent_id": ctx["agent_id"],
            "audit_id": ctx["secret_audit_id"],
        },
        ("POST", "/secrets/{name}/resolve"): {},
        ("POST", "/secrets/{name}/rotate"): {"value": "rotated-route-secret", "actor": "operator"},
        ("POST", "/artifacts"): {
            "kind": "runtime",
            "digest": "sha256:" + "3" * 64,
            "uri": "https://example.test/artifacts/case.tar",
            "created_by": "operator",
        },
        ("POST", "/conversation-threads"): {
            "platform_binding_id": ctx["platform_binding_id"],
            "external_thread_id": "thread-route-case",
            "summary": "case thread",
        },
        ("POST", "/vector-refs"): {
            "memory_id": ctx["memory_id"],
            "vector_db": "qdrant",
            "collection": "mac_memory_medium",
            "point_id": "route-point-case",
        },
        ("POST", "/environments"): {
            "name": "route-env-case",
            "tenant_id": ctx["tenant_id"],
            "channel": "fleet",
        },
        ("POST", "/environments/{env_id}/deploy"): {
            "artifact_id": ctx["artifact_id"],
            "actor": "operator",
            "metadata": {"route_case": True},
        },
        ("POST", "/runtimes"): {
            "name": "route-runtime-case",
            "manifest": _runtime_manifest(),
            "created_by": "operator",
        },
        ("POST", "/runtime-deltas"): _delta_payload(ctx),
        ("POST", "/runtime-deltas/{delta_id}/validate"): {"actor": "operator"},
        ("POST", "/runtime-deltas/{delta_id}/reject"): {
            "actor": "operator",
            "reason": "route coverage reject path",
        },
        ("POST", "/runtime-deltas/{delta_id}/promote"): {
            "actor": "operator",
            "runtime_name": "route-runtime-promoted",
        },
        ("POST", "/runtime-runs"): {
            "task_id": ctx["runtime_task_id"],
            "agent_id": ctx["agent_id"],
            "environment_id": ctx["runtime_id"],
        },
        ("POST", "/runtime-runs/{run_id}/complete"): {
            "evidence_id": ctx["runtime_evidence_id"],
            "status": "completed",
        },
        ("POST", "/bridge/items"): {
            "source": "tickets",
            "external_id": "route-item-1",
            "title": "Route bridge item",
            "project": ctx["project_name"],
            "payload": {"source_url": "https://example.test/route-item"},
            "required_capabilities": ["python"],
        },
        ("POST", "/bridge/repositories"): {
            "name": "route-repo",
            "path": ctx["repository_path"],
            "project": ctx["project_name"],
            "required_capabilities": ["python"],
            "poll_interval_seconds": 30,
        },
        ("POST", "/memory"): {
            "task_id": ctx["task_id"],
            "subject_type": "task",
            "subject_id": ctx["task_id"],
            "record_type": "note",
            "content": "case memory",
            "created_by": "operator",
        },
        ("POST", "/eval-sets"): {"name": "route-eval-case", "created_by": "operator"},
        ("POST", "/eval-sets/{eval_set_id}/baseline"): {
            "baseline_score": 0.82,
            "actor": "operator",
        },
        ("POST", "/eval-runs"): {
            "eval_set_id": ctx["eval_set_id"],
            "target_kind": "runtime_environment",
            "target_id": ctx["runtime_id"],
            "score": 0.83,
        },
        ("POST", "/rollouts"): {
            "version": "route-rollout-case",
            "strategy": "full",
            "target_percent": 0,
            "created_by": "operator",
            "tenant_id": ctx["tenant_id"],
            "runtime_environment_id": ctx["runtime_id"],
            "artifact_uri": "https://example.test/artifacts/rollout-case.tar",
            "artifact_hash": "sha256:" + "4" * 64,
            "health_policy": {"required_checks": ["runtime"]},
        },
        ("POST", "/rollouts/{rollout_id}/advance"): {
            "action": "pause",
            "actor": "operator",
            "detail": {"reason": "route coverage"},
        },
        ("POST", "/rollouts/{rollout_id}/artifact"): {
            "artifact_uri": "https://example.test/artifacts/rollout-new.tar",
            "artifact_hash": "sha256:" + "5" * 64,
            "actor": "operator",
        },
        ("POST", "/rollouts/{rollout_id}/health"): {
            "actor": "operator",
            "checks": {"runtime": {"status": "passed"}},
        },
        ("POST", "/rollouts/{rollout_id}/rescue"): {
            "actor": "operator",
            "reason": "route coverage rescue",
        },
        # paused=False keeps the shared coverage project claimable for other cases.
        ("POST", "/projects/{project}/dispatch"): {
            "paused": False,
            "actor": "operator",
        },
        ("POST", "/tasks/{task_id}/release"): {"actor": "operator"},
        ("POST", "/optimizer/policies"): {
            "name": "route-sci-extra", "project": ctx["project_name"],
            "parameters": {"plan_first": True}, "created_by": "route-coverage"},
        ("POST", "/optimizer/policies/{policy_id}/promote"): {
            "actor": "route-coverage", "reason": "route coverage"},
        ("POST", "/optimizer/projects/{project}/rollback/{policy_id}"): {
            "actor": "route-coverage", "reason": "route coverage"},
        ("POST", "/optimizer/experiments"): {
            "name": "route-sci-exp-2", "project": ctx["project_name"],
            "hypothesis": "route coverage hypothesis",
            "control_policy_id": ctx["sci_policy_id"],
            "treatment_policy_id": ctx["sci_policy2_id"],
            "primary_metric": "accepted_success", "created_by": "route-coverage"},
        ("POST", "/optimizer/experiments/{experiment_id}/start"): {"actor": "route-coverage"},
        ("POST", "/optimizer/experiments/{experiment_id}/pause"): {
            "actor": "route-coverage", "reason": "route coverage"},
        ("POST", "/optimizer/experiments/{experiment_id}/promote"): {
            "actor": "route-coverage", "reason": "route coverage"},
        ("POST", "/tasks/{task_id}/activity"): {
            "phase": "worker",
            "actor": "operator",
            "summary": "route coverage activity entry",
        },
    }
    query_cases: Dict[RouteKey, Dict[str, Any]] = {
        ("POST", "/tasks/{task_id}/claim"): {
            "params": {"agent_id": ctx["claim_agent_id"], "lease_seconds": 60}
        },
        ("POST", "/tasks/{task_id}/start"): {"params": {"agent_id": ctx["start_agent_id"]}},
        ("POST", "/tasks/{task_id}/submit-for-review"): {
            "params": {"agent_id": ctx["submit_agent_id"]}
        },
        ("POST", "/workflows/runs/tick"): {},
        ("POST", "/optimizer/tick"): {},
        ("POST", "/optimizer/experiments/{experiment_id}/observe/{task_id}"): {},
        ("POST", "/optimizer/experiments/{experiment_id}/analyze"): {},
        ("POST", "/reviews/default/tick"): {"params": {"limit": 1}},
        ("POST", "/agents/{agent_id}/attestation-key/rotate"): {},
        ("POST", "/agents/{agent_id}/disable"): {},
        ("DELETE", "/agents/{agent_id}/role"): {},
        ("POST", "/agents/{agent_id}/messages/deliver"): {"params": {"limit": 10}},
        ("POST", "/agentbus/streams/{stream_id}/close"): {
            "params": {"sender_agent_id": ctx["agent_id"], "status": "closed"}
        },
    }
    key = (method, path_template)
    if key in bodies:
        kwargs["json"] = bodies[key]
    if key in query_cases:
        kwargs.update(query_cases[key])
    if method in {"POST", "PUT"} and key not in bodies and key not in query_cases:
        raise KeyError("no route coverage request body for %s %s" % key)
    return RequestCase(path, kwargs, expected)


def test_every_mac_api_route_has_a_realistic_e2e_request(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENHUB_URL", "https://tokenhub.example.test")
    monkeypatch.setenv("TOKENHUB_ADMIN_TOKEN", "route-tokenhub-admin")
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.example.test")
    monkeypatch.setenv("MAC_QDRANT_URL", "https://qdrant.example.test")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://firecrawl.example.test")
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "fleets.yaml"))

    class FakeVectorWriter:
        def __init__(self, *args, **kwargs):
            pass

        def recall(self, query, **kwargs):
            payload = {"dream_confidence": "high", "dream_confidence_score": 1.0}
            return [
                {
                    "memory_id": "mem_route_fake",
                    "score": 0.99,
                    "summary": "fake vector recall for %s" % query,
                    "payload": payload,
                }
            ]

    import mac.vector_writer_service as vector_writer_service

    monkeypatch.setattr(vector_writer_service, "VectorWriterService", FakeVectorWriter)

    cp = ControlPlane.in_memory()
    app = create_app(control_plane=cp)
    app.state.workflow_plan_model = lambda request: {
        "plan_id": "plan_route_preview",
        "goal": request.get("goal"),
        "project": request.get("project"),
        "nodes": [
            {
                "node_id": "plan",
                "title": "Plan route coverage workflow",
                "description": "Draft the route coverage task chain.",
                "required_capabilities": ["python"],
            },
            {
                "node_id": "verify",
                "title": "Verify route coverage workflow",
                "description": "Verify the route coverage task chain.",
                "required_capabilities": ["python"],
                "depends_on": ["plan"],
            },
        ],
    }
    client = TestClient(app)
    ctx = _seed_route_state(client, cp, tmp_path)

    executed: set[RouteKey] = set()
    failures = []
    for method, route_path in _ordered_route_keys(app):
        try:
            case = _case_for(method, route_path, ctx)
        except Exception as exc:  # noqa: BLE001 - fail with the uncovered route name.
            failures.append("%s %s: missing request case: %s" % (method, route_path, exc))
            continue
        response = client.request(method, case.path, **case.kwargs)
        if response.status_code not in case.expected_statuses:
            failures.append(
                "%s %s -> %s %s (expected %s): %s"
                % (
                    method,
                    case.path,
                    response.status_code,
                    response.reason_phrase,
                    case.expected_statuses,
                    response.text[:500],
                )
            )
            continue
        executed.add((method, route_path))

    route_set = set(_route_keys(app))
    missing = route_set - executed
    unexpected = executed - route_set
    if missing:
        failures.append("missing executed route keys: %s" % sorted(missing))
    if unexpected:
        failures.append("unexpected executed route keys: %s" % sorted(unexpected))
    assert not failures, "\n".join(failures)
    assert executed == route_set
