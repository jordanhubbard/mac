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
from mac.models import read_only_report_repository_executor_attestation, utcnow
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


def _prepare_reviewable_task(
    cp: ControlPlane,
    *,
    task_id: str,
    worker_id: str,
    worker_key: str,
) -> str:
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
    return evidence.id


def _prepare_reviewing_task(
    cp: ControlPlane,
    *,
    task_id: str,
    worker_id: str,
    worker_key: str,
    reviewer_id: str,
) -> Dict[str, Any]:
    evidence_id = _prepare_reviewable_task(
        cp,
        task_id=task_id,
        worker_id=worker_id,
        worker_key=worker_key,
    )
    review = cp.request_review(task_id, reviewer_id)
    return {"executor_evidence_id": evidence_id, "review_id": review.id}


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
    human = _ok(
        client.post(
            "/humans",
            json={"username": "route-coverage-human", "email": "route@example.test", "groups": ["ops"]},
        )
    )
    # /humans route coverage seeds: `human_id` backs GET /humans/{human_id}
    # and `delete_human_id` backs DELETE /humans/{human_id} (see _path_for).
    ctx["human_id"] = human["id"]
    ctx["delete_human_id"] = _ok(
        client.post("/humans", json={"username": "route-delete-human"})
    )["id"]

    hermes = _ok(
        client.post(
            "/persona-instances",
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

    # Seed one OpenClaw conversation execution to back GET
    # /openclaw-executions/{execution_id}. Materialize no MAC task for the seed
    # so it cannot perturb the reviewer-availability state later routes assert
    # on; the POST route's own coverage case exercises task materialization.
    from mac.openclaw_direct_execution import (
        HumanDirective as _OpenClawDirective,
        RepositoryTarget as _OpenClawRepo,
        SlackProvenance as _OpenClawSlack,
    )

    _openclaw_seed_svc = cp.openclaw_direct_execution
    _saved_materialize = _openclaw_seed_svc._materialize_task
    _openclaw_seed_svc._materialize_task = None
    try:
        openclaw_execution = _openclaw_seed_svc.begin_conversation_execution(
            persona_instance_id=hermes["id"],
            directive=_OpenClawDirective(
                human_id="route-human",
                authenticated=True,
                text="route coverage deferred request",
            ),
            slack=_OpenClawSlack(
                workspace_id="W-route",
                channel_id="C-route",
                thread_ts="1700000000.0001",
            ),
            repository=_OpenClawRepo("projectrepo_route", "mac", "a" * 40),
            deferred=True,
        )
    finally:
        _openclaw_seed_svc._materialize_task = _saved_materialize
    ctx["openclaw_execution_id"] = openclaw_execution.id

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
    seeded_crash = cp.crashes.ingest(
        default_agent["id"],
        {
            "event_id": "route-crash-seed",
            "supervisor": "systemd",
            "process_name": "mac-agent-service",
            "exit_code": 1,
            "revision": "route-revision",
            "stack_trace": "Traceback (most recent call last):\nRuntimeError: route crash",
        },
    )
    ctx["crash_report_id"] = seeded_crash["id"]
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
    # Keep the deletion fixture out of dispatcher eligibility. Otherwise an
    # earlier dispatch/claim-next route case can hand this agent a lease, and
    # the DELETE /agents/{agent_id} route then returns 400 ("agent cannot be
    # deleted while holding an active lease") instead of the expected 200 --
    # an order-dependent flake this route-coverage sweep must not carry.
    cp.set_agent_dispatch_hold(
        ctx["delete_agent_id"], "reserved for deletion route coverage"
    )
    # Keep the deregistration fixture out of dispatcher eligibility so the
    # earlier dispatch-route cases cannot hand it a lease before its route is
    # exercised.
    ctx["deregister_agent_id"] = agent(
        "deregister-route-agent", ["deregister-only"]
    )["id"]
    cp.set_agent_dispatch_hold(
        ctx["deregister_agent_id"], "reserved for deregistration route coverage"
    )
    ctx["disable_agent_id"] = agent("disable-route-agent", ["python"])["id"]
    ctx["bulk_agent_id"] = agent("bulk-route-agent", ["python"])["id"]
    # No real quarantine ledger exists on a CI host, so these only have to be
    # well-formed: the route answers 404 ("this host has no ledger"), which is
    # the fail-closed behaviour this inventory is meant to exercise.
    ctx["curiosity_candidate_id"] = "cur_route_coverage"
    ctx["curiosity_decision"] = "approve"
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
    attest_recover = agent("attest-recover-route-agent", ["python"])
    ctx["attest_recover_agent_id"] = attest_recover["id"]
    report_attestation = read_only_report_repository_executor_attestation(
        runtime_image_ref=(
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "1" * 64
        ),
        policy_sha256="sha256:" + "2" * 64,
        openshell_bin_path="/route/openshell",
        openshell_bin_sha256="sha256:" + "3" * 64,
        executor_path="/route/mac-task-executor",
        executor_sha256="sha256:" + "4" * 64,
        platform="linux",
        isolation_posture="landlock_enforced",
        python_path="/route/python",
        python_sha256="sha256:" + "5" * 64,
        executor_script_path="/route/mac-task-executor.py",
        executor_script_sha256="sha256:" + "6" * 64,
        source_root="/route/mac",
        source_bundle_sha256="sha256:" + "7" * 64,
    )
    ctx["report_executor_attestation"] = report_attestation
    ctx["report_executor_startup_timestamp"] = "2026-07-18T12:00:00Z"
    report_agent = agent("report-executor-route-agent", ["python"])
    ctx["report_executor_agent_id"] = report_agent["id"]
    cp.update_agent(
        report_agent["id"],
        resources={
            "openshell_required": True,
            "report_repository_executor_attestation": report_attestation,
            "startup_self_test": {
                "schema": "mac.agent_startup_self_test.v1",
                "timestamp": ctx["report_executor_startup_timestamp"],
                "status": "passed",
                "agent_id": report_agent["id"],
                "checks": {
                    "openshell_executor_config": True,
                    "report_repository_executor_attestation": True,
                },
                "report_repository_executor_attestation": report_attestation,
                "blocking_problems": [],
            },
        },
    )
    ctx["dispatch_hold_agent_id"] = agent("dispatch-hold-route-agent", ["python"])["id"]
    ctx["dispatch_hold_batch_agent_id"] = agent(
        "dispatch-hold-batch-route-agent", ["python"]
    )["id"]
    cp.set_agent_dispatch_hold(
        ctx["dispatch_hold_batch_agent_id"], "route-coverage batch deployment"
    )
    cp.heartbeat_agent(
        ctx["dispatch_hold_batch_agent_id"],
        status="idle",
        health_status="healthy",
        resources={"deployment_generation": "route-coverage-generation"},
    )
    ctx["dispatch_hold_transition_agent_id"] = agent(
        "dispatch-hold-transition-route-agent", ["python"]
    )["id"]
    cp.set_agent_dispatch_hold(
        ctx["dispatch_hold_transition_agent_id"],
        "route-coverage transition deployment",
    )
    cp.heartbeat_agent(
        ctx["dispatch_hold_transition_agent_id"],
        status="idle",
        health_status="healthy",
        resources={
            "deployment_generation": "route-coverage-transition-generation"
        },
    )
    ctx["transition_agent_id"] = agent("transition-route-agent", ["python"])["id"]
    ctx["evidence_agent_id"] = agent("evidence-route-agent", ["python"])["id"]

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
    transition_task = task("route transition task")
    _, transition_lease = cp.claim_task(
        transition_task["id"], ctx["transition_agent_id"], sync_beads=False
    )
    ctx["transition_task_id"] = transition_task["id"]
    ctx["transition_lease_id"] = transition_lease.id
    ctx["reopen_task_id"] = task("route reopen task")["id"]
    ctx["ask_task_id"] = task("route ask task")["id"]
    answer_task = task("route answer task")
    # Answering only applies to a task already parked on a question.
    cp.request_task_input(
        answer_task["id"], [{"question": "route coverage question?"}], "operator"
    )
    ctx["answer_task_id"] = answer_task["id"]
    ctx["force_complete_task_id"] = task("route force-complete task")["id"]
    ctx["claim_task_id"] = task("route claim task")["id"]
    ctx["claim_next_task_id"] = task("route claim-next task")["id"]
    evidence_task = task("route evidence task")
    _, evidence_lease = cp.claim_task(
        evidence_task["id"], ctx["evidence_agent_id"], sync_beads=False
    )
    ctx["evidence_task_id"] = evidence_task["id"]
    ctx["evidence_lease_id"] = evidence_lease.id
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

    # Two groups: one the GET reads, one the DELETE consumes, so the two
    # routes do not race for the same row.
    _ok(
        client.post(
            "/task-groups",
            json={
                "name": "route-group",
                "selector": "state=open",
                "description": "route coverage",
            },
        )
    )
    _ok(
        client.post(
            "/task-groups",
            json={"name": "route-group-delete", "selector": "state=failed"},
        )
    )
    ctx["task_group_name"] = "route-group"
    ctx["task_group_delete_name"] = "route-group-delete"

    # Two native-merge-queue entries, one per mutating verb, so evict and
    # requeue do not fight over the same row. Admitted through the queue rather
    # than inserted, because the entry id and the position bookkeeping are the
    # queue's to assign.
    merge_queue = cp._native_merge_queue()
    ctx["merge_queue_repository"] = "github.invalid/route/coverage"
    ctx["merge_queue_branch"] = "main"
    ctx["merge_queue_evict_entry_id"] = merge_queue.admit(
        repository=ctx["merge_queue_repository"],
        branch=ctx["merge_queue_branch"],
        task_id="task_route_coverage_evict",
        head_sha="a" * 40,
    ).id
    ctx["merge_queue_requeue_entry_id"] = merge_queue.admit(
        repository=ctx["merge_queue_repository"],
        branch=ctx["merge_queue_branch"],
        task_id="task_route_coverage_requeue",
        head_sha="b" * 40,
    ).id

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
        _trusted_internal=True,
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

    directive_document = {
        "schema": "mac.directive.v1",
        "name": "route.coverage.lifecycle",
        "description": "Exercise directive lifecycle route coverage.",
        "scope": "fleet",
        "set": {"route.coverage.lifecycle": True},
    }
    directive = cp.directives.propose(directive_document, actor="route-coverage")
    ctx["directive_id"] = directive["id"]
    ctx["directive_digest"] = directive["versions"][0]["digest"]
    ctx["directive_check_id"] = "updated-after-check-route"
    waiver = cp.directives.create_waiver(
        directive["id"],
        version=1,
        target_type="project",
        target_id=ctx["project_name"],
        reason="route coverage revoke fixture",
        actor="route-coverage",
    )
    ctx["directive_waiver_id"] = waiver["id"]

    ack_directive = cp.directives.propose(
        {
            "schema": "mac.directive.v1",
            "name": "route.coverage.ack",
            "description": "Exercise worker-bound directive acknowledgement.",
            "scope": "fleet",
            "set": {"route.coverage.ack": True},
        },
        actor="route-coverage",
    )
    ack_version = ack_directive["versions"][0]
    ack_check = cp.directives.check(ack_directive["id"], actor="route-coverage")
    cp.directives.approve(
        ack_directive["id"],
        version=1,
        directive_digest=ack_version["digest"],
        check_id=ack_check["id"],
        actor="route-coverage",
    )
    activation = cp.directives.activate(
        ack_directive["id"],
        version=1,
        directive_digest=ack_version["digest"],
        actor="route-coverage",
    )
    for cohort_agent_id in activation["cohort"]:
        if cohort_agent_id != default_agent["id"]:
            cp.directives.acknowledge(
                activation["id"],
                agent_id=cohort_agent_id,
                digest=ack_version["digest"],
            )
    ctx["directive_activation_id"] = activation["id"]
    ctx["directive_ack_digest"] = ack_version["digest"]

    ctx["absent_dispatch_hold_epoch_id"] = "route-coverage-absent-epoch"
    return ctx


def _path_for(method: str, path_template: str, ctx: Mapping[str, Any]) -> str:
    special: Dict[RouteKey, Dict[str, str]] = {
        ("POST", "/tasks/{task_id}/children"): {"task_id": "parent_task_id"},
        ("DELETE", "/tasks/{task_id}"): {"task_id": "delete_task_id"},
        ("POST", "/tasks/{task_id}/transition"): {"task_id": "transition_task_id"},
        ("POST", "/tasks/{task_id}/reopen"): {"task_id": "reopen_task_id"},
        ("POST", "/tasks/{task_id}/ask"): {"task_id": "ask_task_id"},
        ("POST", "/tasks/{task_id}/answer"): {"task_id": "answer_task_id"},
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
        ("POST", "/agents/{agent_id}/attestation-key/recover"): {
            "agent_id": "attest_recover_agent_id"
        },
        ("POST", "/agents/{agent_id}/report-repository-executor/approve"): {
            "agent_id": "report_executor_agent_id"
        },
        ("POST", "/agents/{agent_id}/report-repository-executor/revoke"): {
            "agent_id": "report_executor_agent_id"
        },
        ("POST", "/agents/{agent_id}/disable"): {"agent_id": "disable_agent_id"},
        ("DELETE", "/agents/{agent_id}"): {"agent_id": "delete_agent_id"},
        ("POST", "/v1/agents/{agent_id}/deregister"): {"agent_id": "deregister_agent_id"},
        ("GET", "/v1/agents/{agent_id}/agentbus-cursor"): {"agent_id": "agent_id"},
        ("GET", "/agentbus/streams/{stream_id}/directive-verification"): {"stream_id": "stream_id"},
        ("PUT", "/v1/agents/{agent_id}/agentbus-cursor"): {"agent_id": "agent_id"},
        ("POST", "/agents/bulk"): {},
        ("POST", "/curiosity/candidates/{candidate_id}/{decision}"): {
            "candidate_id": "curiosity_candidate_id",
            "decision": "curiosity_decision",
        },
        ("POST", "/agents/dispatch-hold/release-batch"): {},
        ("GET", "/agents/dispatch-hold/epochs/{epoch_id}"): {
            "epoch_id": "absent_dispatch_hold_epoch_id"
        },
        ("GET", "/agents/dispatch-hold/epochs/{epoch_id}/readiness"): {
            "epoch_id": "absent_dispatch_hold_epoch_id"
        },
        ("POST", "/agents/dispatch-hold/epochs/{epoch_id}/prove"): {
            "epoch_id": "absent_dispatch_hold_epoch_id"
        },
        ("POST", "/agents/dispatch-hold/epochs/{epoch_id}/commit"): {
            "epoch_id": "absent_dispatch_hold_epoch_id"
        },
        ("POST", "/agents/dispatch-hold/epochs/{epoch_id}/abort"): {
            "epoch_id": "absent_dispatch_hold_epoch_id"
        },
        ("POST", "/agents/dispatch-hold/transition-batch"): {},
        ("POST", "/agents/{agent_id}/dispatch-hold"): {"agent_id": "dispatch_hold_agent_id"},
        ("DELETE", "/agents/{agent_id}/dispatch-hold"): {"agent_id": "dispatch_hold_agent_id"},
        ("POST", "/agents/{agent_id}/dispatch-hold/acquire"): {
            "agent_id": "dispatch_hold_agent_id"
        },
        ("POST", "/agents/{agent_id}/dispatch-hold/release"): {
            "agent_id": "dispatch_hold_agent_id"
        },
        ("POST", "/agents/{agent_id}/claim-next"): {"agent_id": "claim_next_agent_id"},
        ("POST", "/agents/{agent_id}/crash-reports"): {"agent_id": "agent_id"},
        ("GET", "/crash-reports/{report_id}"): {"report_id": "crash_report_id"},
        ("POST", "/crash-reports/{report_id}/resolve"): {"report_id": "crash_report_id"},
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
        # DELETE /humans/{human_id} targets a dedicated seed row so it does
        # not remove the human_id row used by GET /humans/{human_id}.
        ("DELETE", "/humans/{human_id}"): {"human_id": "delete_human_id"},
        ("DELETE", "/secrets/{name}"): {"name": "delete_secret_name"},
        ("POST", "/merge-queue/entries/{entry_id}/evict"): {
            "entry_id": "merge_queue_evict_entry_id"
        },
        ("POST", "/merge-queue/entries/{entry_id}/requeue"): {
            "entry_id": "merge_queue_requeue_entry_id"
        },
        ("GET", "/task-groups/{name}"): {"name": "task_group_name"},
        ("DELETE", "/task-groups/{name}"): {"name": "task_group_delete_name"},
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
        "package_id": "wp_route_missing",
        "batch_id": "wpbatch_route_missing",
        "job_id": "wpcjob_route_missing",
        "candidate_id": "wpcandidate_route_missing",
        "finalization_id": "wpfinal_route_missing",
        "experiment_id": "route-review-experiment",
        "eval_set_id": ctx["eval_set_id"],
        "flag": "show_reasoning",
        "fleet_id_or_name": ctx["fleet_id"],
        "instance_id": ctx["instance_id"],
        "execution_id": ctx["openclaw_execution_id"],
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
        "report_id": ctx["crash_report_id"],
        "role_id": ctx["role_id"],
        "role_id_or_slug": ctx["role_slug"],
        "rollout_id": ctx["rollout_id"],
        "run_id": ctx["workflow_run_id"],
        "secret_id": ctx["secret_id"],
        "session_id": ctx["terminal_session_id"],
        "stream_id": ctx["stream_id"],
        "human_id": ctx["human_id"],
        "task_id": ctx["task_id"],
        "thread_id": ctx["thread_id"],
        "workflow_id": ctx["workflow_id"],
        "workflow_id_or_slug": ctx["workflow_id"],
        "directive_id": ctx["directive_id"],
        "waiver_id": ctx["directive_waiver_id"],
        "activation_id": ctx["directive_activation_id"],
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
    if path_template.startswith(("/work-packages", "/work-package-")):
        # The managed-stage happy paths are covered by their dedicated API and
        # real-Git assembly-line suites.  This exhaustive inventory test sends
        # schema-valid requests to explicit missing product identities and
        # treats the fail-closed domain guard as successful route coverage.
        expected = (200, 400, 404, 409)
    if path_template.startswith("/agents/dispatch-hold/epochs/"):
        expected = (200, 400, 404)
    if path_template in {"/v1/memory/promote", "/v1/memory/reconcile-embeddings"}:
        # Both need a Qdrant endpoint, and a test app has none configured, so
        # the route answers 400 ("pass qdrant_url or set MAC_QDRANT_URL...").
        # That fail-closed answer IS the coverage here: it proves the route is
        # wired to the facade and validating, without pointing an inventory
        # test at a live vector store. The promotion and reconciliation
        # behaviour itself is covered in tests/test_memory_promotion.py and
        # tests/test_memory_embedding_spaces.py against a fake Qdrant.
        expected = (200, 400)
    if path_template.startswith("/curiosity/"):
        # The curiosity ledger lives inside the owning agent's OpenClaw
        # sandbox, so a machine with no gateway installed has no wrapper to
        # proxy and the route answers 404 by design ("this host has no
        # quarantine ledger"). CI hosts are in exactly that state, so treat the
        # fail-closed answer as coverage rather than pretending a ledger
        # exists. 400 covers a wrapper that is present but rejects the call.
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
        elif path_template == "/v1/agents/{agent_id}/agentbus-cursor":
            kwargs["params"] = {"topic": "peer.message.v1"}
        elif path_template in {
            "/agents/dispatch-hold/epochs/{epoch_id}",
            "/agents/dispatch-hold/epochs/{epoch_id}/readiness",
        }:
            kwargs["params"] = {"identity_sha256": "a" * 64}
        elif path_template == "/v1/memory/dreams/recall":
            kwargs["params"] = {"q": "route coverage dream", "limit": 1, "min_confidence": "low"}
        elif path_template == "/tasks/search":
            kwargs["params"] = {"q": "route coverage"}
        elif path_template == "/merge-queue/entries":
            # repository/branch are query params, not path segments: a
            # repository is a URL and would need double-escaping in a path.
            kwargs["params"] = {
                "repository": ctx["merge_queue_repository"],
                "branch": ctx["merge_queue_branch"],
            }
        elif path_template == "/humans/resolve":
            # GET /humans/resolve requires the `anchor` query param.
            kwargs["params"] = {"anchor": "route-coverage-human"}
        return RequestCase(path, kwargs, expected)

    if method == "DELETE":
        if path_template in {"/agents/{agent_id}/mood", "/v1/agents/{agent_id}/mood"}:
            kwargs["json"] = {"cleared_by": "operator", "reason": "route coverage"}
        elif path_template == "/v1/agents/{agent_id}/config-flags/{flag}":
            kwargs["json"] = {"channel": "slack:CROUTE", "reason": "route coverage"}
        return RequestCase(path, kwargs, expected)

    bodies: Dict[RouteKey, Dict[str, Any]] = {
        # Both halves, because the route judges them together: a request whose
        # capabilities and hardware are satisfiable by DIFFERENT agents and by
        # no single agent must not pass.
        ("POST", "/tasks/preflight"): {
            "required_capabilities": ["python"],
            "required_hardware": {"os": ["linux"]},
        },
        # One coding-CLI turn: what the CLI was asked, and what it answered.
        # Real content rather than a placeholder, because the point of the
        # route is that the text survives -- an empty body would exercise the
        # insert and prove nothing about the thing that kept getting dropped.
        ("POST", "/tasks/{task_id}/transcript"): {
            "prompt": "fix the failing allocator test",
            "response": "I changed evaluate_pair and added a case",
            "coding_agent": "claude",
            "returncode": 0,
        },
        # A syntactically valid reviewed digest. The endpoint refuses anything
        # that is not one, so a placeholder here would exercise only the
        # rejection path and leave the route effectively uncovered.
        ("POST", "/sandbox/rollout"): {
            "image": "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:%s" % ("a" * 64),
            "bom": {},
            "actor": "route-coverage",
        },
        ("PUT", "/work-packages/{package_id}"): {
            "goal": "route coverage goal",
            "metadata": {"lane": "coverage"},
            "actor": "route-coverage",
        },
        # No canonical_tip_tree: the hub has no checkout to read one from, and
        # the sweep must be callable without it (it skips the stale-result step
        # rather than guessing at the tip).
        ("POST", "/merge-queue/reconcile"): {
            "repository": "github.invalid/route/coverage",
            "branch": "main",
            "actor": "route-coverage",
        },
        ("POST", "/merge-queue/entries/{entry_id}/evict"): {
            "reason": "route coverage",
            "actor": "route-coverage",
        },
        ("POST", "/merge-queue/entries/{entry_id}/requeue"): {
            "reason": "route coverage",
            "actor": "route-coverage",
        },
        # Dry run: exercise the route without re-supervising live tasks in the
        # coverage fixture.
        ("POST", "/tasks/recover-stranded"): {"dry_run": True, "limit": 1},
        ("POST", "/tasks/select"): {"selector": "state=open", "limit": 5},
        # Deliberately a DRY RUN (no "apply"): route coverage must exercise the
        # endpoint without mutating the fixture ledger the other cases read.
        ("POST", "/tasks/batch"): {
            "selector": "state=open",
            "operation": "set",
            "options": {"priority": 1},
        },
        ("POST", "/task-groups"): {
            "name": "route-group-upsert",
            "selector": "state=open project=route",
        },
        ("POST", "/directives"): {
            "document": {
                "schema": "mac.directive.v1",
                "name": "route.coverage.proposal",
                "description": "Exercise directive proposal route coverage.",
                "scope": "fleet",
                "set": {"route.coverage.proposal": True},
            },
            "actor": "route-coverage",
        },
        ("POST", "/directive-bindings"): {
            "target_type": "fleet",
            "target_id": "fleet",
            "key": "route.coverage.target",
            "value": "//route:all",
            "actor": "route-coverage",
        },
        ("POST", "/directive-waivers/{waiver_id}/revoke"): {
            "reason": "route coverage completed",
            "actor": "route-coverage",
        },
        ("POST", "/agents/{agent_id}/directive-activations/{activation_id}/ack"): {
            "digest": ctx["directive_ack_digest"],
        },
        ("POST", "/directives/{directive_id}/check"): {
            "version": 1,
            "actor": "route-coverage",
        },
        ("POST", "/directives/{directive_id}/approve"): {
            "version": 1,
            "directive_digest": ctx["directive_digest"],
            "check_id": ctx["directive_check_id"],
            "actor": "route-coverage",
        },
        ("POST", "/directives/{directive_id}/activate"): {
            "version": 1,
            "directive_digest": ctx["directive_digest"],
            "actor": "route-coverage",
        },
        ("POST", "/directives/{directive_id}/deactivate"): {
            "reason": "route coverage completed",
            "actor": "route-coverage",
        },
        ("POST", "/directives/{directive_id}/waivers"): {
            "version": 1,
            "target_type": "project",
            "target_id": ctx["project_name"],
            "reason": "route coverage create fixture",
            "actor": "route-coverage",
        },
        # POST /humans (register_human) body case.
        ("POST", "/humans"): {"username": "route-human-case"},
        ("POST", "/tenants"): {"name": "Route Coverage Tenant Case"},
        ("POST", "/users"): {"tenant_id": ctx["tenant_id"], "handle": "operator-case"},
        ("POST", "/personas"): {
            "tenant_id": ctx["tenant_id"],
            "name": "Case Persona",
            "soul_ref": "hermes://route/case/SOUL.md",
            "memory_scope": "hermes://route/case/memory",
        },
        ("POST", "/persona-instances"): {
            "tenant_id": ctx["tenant_id"],
            "name": "case-hermes",
            "persona_id": ctx["persona_id"],
            "home_ref": "hermes://route/case",
        },
        ("POST", "/persona-instances/{instance_id}/runtime-proof"): {
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
        ("POST", "/persona-instances/{instance_id}/tasks"): {
            "title": "case hermes task",
            "project": ctx["project_name"],
            "platform_binding_id": ctx["platform_binding_id"],
            "conversation_ref": "slack://C-route/123",
        },
        ("POST", "/persona-instances/{instance_id}/openclaw-executions"): {
            "human_id": "route-human",
            "authenticated": True,
            "directive_text": "route coverage direct-vs-deferred case",
            "slack_workspace_id": "W-route",
            "slack_channel_id": "C-route",
            "slack_thread_ts": "1700000000.0001",
            "repository_id": "projectrepo_route",
            "repository_name": "mac",
            "base_sha": "a" * 40,
            # No worktree provisioner in the route-coverage app, so a direct
            # request would fail closed (409). File deferred work instead: a
            # schema-valid 200 that exercises the route end to end.
            "deferred": True,
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
        ("POST", "/projects/register"): {
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
            "actor": ctx["transition_agent_id"],
            "lease_id": ctx["transition_lease_id"],
            "detail": {"reason": "route coverage transition"},
        },
        ("POST", "/tasks/{task_id}/reopen"): {
            "actor": "operator",
            "reason": "route coverage reopen",
        },
        ("POST", "/tasks/{task_id}/ask"): {
            "actor": "operator",
            "questions": [{"question": "which database?"}],
            "why": "route coverage ask",
        },
        ("POST", "/tasks/{task_id}/answer"): {
            "actor": "operator",
            "answer": "route coverage answer",
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
            "created_by": ctx["evidence_agent_id"],
            "lease_id": ctx["evidence_lease_id"],
            "metadata": {"verification": _operator_manifest("route evidence summary is substantive")},
        },
        ("POST", "/work-package-pipeline/trigger"): {},
        ("POST", "/work-packages"): {
            "plan": {
                "schema": "mac.work_package.plan.v1",
                "package_id": "wp_route_missing",
                "goal": "exercise managed route admission",
                "project": ctx["project_name"],
                "repository_id": "repo_route_missing",
                "planning_base_ref": "refs/heads/main",
                "planning_base_sha": "a" * 40,
                "plan_generation": 1,
                "nodes": [
                    {
                        "node_key": "change",
                        "title": "Route coverage change",
                        "kind": "mutation",
                        "effects": {"writes": ["src/route_coverage.py"]},
                        "expected_outputs": ["candidate"],
                        "verification": {"profile": "repository-default"},
                        "estimates": {"confidence": "high"},
                    }
                ],
            },
            "reason": "route coverage",
        },
        ("POST", "/work-packages/{package_id}/activate"): {
            "expected_plan_version": 1,
            "expected_epoch": 1,
        },
        ("POST", "/work-packages/{package_id}/replan-preview"): {
            "plan": {},
            "expected_plan_version": 1,
            "expected_epoch": 1,
            "reason": "route coverage preview",
        },
        ("POST", "/work-packages/{package_id}/pause"): {
            "expected_plan_version": 1,
            "expected_epoch": 1,
            "reason": "route coverage Andon",
        },
        ("POST", "/work-packages/{package_id}/replan"): {
            "plan": {},
            "expected_plan_version": 1,
            "expected_epoch": 1,
            "reason": "route coverage replan",
        },
        ("POST", "/work-packages/{package_id}/integration-batches"): {
            "integration_node_key": "integrate"
        },
        ("POST", "/work-packages/{package_id}/assemble"): {
            "integration_node_key": "integrate"
        },
        ("POST", "/work-package-integration-batches/{batch_id}/claim"): {},
        ("POST", "/work-package-integration-batches/{batch_id}/assemble"): {},
        ("POST", "/work-package-integration-batches/{batch_id}/certification-jobs"): {
            "bundle_path": "/tmp/route-coverage.bundle"
        },
        ("POST", "/work-package-certification-jobs/{job_id}/claim"): {},
        ("POST", "/work-package-certification-jobs/{job_id}/ingest"): {
            "result": {"schema": "route.coverage.v1"},
            "owner": "route-certifier",
            "fence": 1,
        },
        ("POST", "/work-package-certification-jobs/{job_id}/run"): {
            "bundle_path": "/tmp/route-coverage.bundle"
        },
        ("POST", "/work-package-integration-batches/{batch_id}/reject-failed-certification"): {
            "certification_id": "wpcert_route_missing"
        },
        ("POST", "/work-package-integration-batches/{batch_id}/accept-certification"): {
            "certification_id": "wpcert_route_missing"
        },
        ("POST", "/work-package-integration-batches/{batch_id}/land"): {},
        ("POST", "/work-package-integration-batches/{batch_id}/finalize-publication"): {},
        ("POST", "/work-package-finalizations/{finalization_id}/outcomes"): {
            "outcome_type": "incident",
            "external_id": "route-incident-missing",
            "observed_at": "2026-07-17T00:00:00+00:00",
            "detail": {"source": "route-coverage"},
        },
        ("POST", "/work-package-outputs/{evidence_id}/verify"): {},
        ("POST", "/work-packages/candidates/{candidate_id}/accept"): {},
        ("POST", "/work-packages/candidates/{candidate_id}/reject"): {
            "reason": "route coverage rework"
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
        ("POST", "/agents/{agent_id}/attestation-key/recover"): {
            "probe": {
                "schema": "mac.agent_attestation_key_probe.v1",
                "state": "present",
                "agent_id": ctx["attest_recover_agent_id"],
                "deployment_id": "route-coverage-deployment",
                "challenge": {
                    "schema": "mac.agent_attestation_challenge.v1",
                    "purpose": "fleet-deploy-attestation-key-proof",
                    "agent_id": ctx["attest_recover_agent_id"],
                    "deployment_id": "route-coverage-deployment",
                    "nonce": "route-coverage-nonce-that-is-at-least-32-bytes",
                },
                "signature": "v1:deliberately-stale-route-coverage-signature",
            }
        },
        ("POST", "/agents/{agent_id}/report-repository-executor/approve"): {
            "expected_attestation": ctx["report_executor_attestation"],
            "expected_startup_timestamp": ctx[
                "report_executor_startup_timestamp"
            ],
            "actor": "route-coverage",
        },
        ("POST", "/agents/{agent_id}/report-repository-executor/revoke"): {
            "reason": "route coverage cleanup",
            "actor": "route-coverage",
        },
        ("POST", "/agents/bulk"): {"agent_ids": [ctx["bulk_agent_id"]], "health_status": "healthy"},
        ("POST", "/curiosity/candidates/{candidate_id}/{decision}"): {
            "actor": "agent_rocky",
            "reason": "route coverage",
            "approval_id": "task_route_coverage",
        },
        ("POST", "/agents/dispatch-hold/release-batch"): {
            "epoch_id": "route-coverage-release-epoch",
            "holds": [
                {
                    "agent_id": ctx["dispatch_hold_batch_agent_id"],
                    "reason": "route-coverage batch deployment",
                    "generation": "route-coverage-generation",
                    "baseline_seen": "2000-01-01T00:00:00+00:00",
                    "principal_id": None,
                    "require_authenticated": False,
                }
            ],
        },
        ("POST", "/agents/dispatch-hold/transition-batch"): {
            "epoch_id": "route-coverage-transition-epoch",
            "successor_reason": "route-coverage synchronized successor",
            "holds": [
                {
                    "agent_id": ctx["dispatch_hold_transition_agent_id"],
                    "reason": "route-coverage transition deployment",
                    "generation": "route-coverage-transition-generation",
                    "baseline_seen": "2000-01-01T00:00:00+00:00",
                    "principal_id": None,
                    "require_authenticated": False,
                }
            ],
        },
        ("POST", "/agents/{agent_id}/dispatch-hold"): {"reason": "route-coverage quarantine"},
        ("POST", "/agents/{agent_id}/dispatch-hold/acquire"): {
            "reason": "route-coverage deployment",
            "expected_dispatch_hold": False,
        },
        ("POST", "/agents/{agent_id}/dispatch-hold/release"): {
            "reason": "route-coverage deployment"
        },
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
        ("PUT", "/v1/agents/{agent_id}/deploy-config"): {
            "document": {
                "gateway": {"host": "route-host", "image": "localhost/mac-openclaw:route"},
                "models": {"mirror_summarizer": "test/model"},
            },
            "schema_name": "mac.agent_deploy_config.v1",
        },
        ("POST", "/v1/agents/{agent_id}/deregister"): {"actor": "operator"},
        ("PUT", "/v1/agents/{agent_id}/agentbus-cursor"): {
            "topic": "peer.message.v1",
            "position": {"watermark": "2026-07-12T00:00:00+00:00", "processed": []},
        },
        ("POST", "/agentbus/request"): {
            "sender_agent_id": ctx["agent_id"],
            "recipient_agent_id": ctx["reviewer_agent_id"],
            "payload": {"schema": "mac.agent.peer_message.v1", "message": "route coverage ping"},
            "deadline_seconds": 0,
        },
        ("POST", "/agentbus/human-directive"): {
            "target_agent_id": ctx["agent_id"],
            "message": "route coverage directive",
            "wait_seconds": 0,
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
        ("POST", "/agents/{agent_id}/crash-reports"): {
            "event_id": "route-crash-post",
            "supervisor": "systemd",
            "process_name": "mac-agent-service",
            "exit_code": 1,
            "revision": "route-revision",
            "stack_trace": "Traceback (most recent call last):\nRuntimeError: route crash",
        },
        ("POST", "/crash-reports/{report_id}/resolve"): {
            "reason": "route coverage repair verified",
            "actor": "route-coverage",
        },
        ("POST", "/agents/{agent_id}/reflect"): {
            "recipient_agent_id": ctx["reviewer_agent_id"],
            "request_id": "route-reflect",
            # No live worker answers in this e2e harness; skip the 30s poll and
            # assert on the published inventory only.
            "reflect_timeout": 0,
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
        ("POST", "/cicd-monitor/run"): {},
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
        ("POST", "/dream/import-logs"): {
            "dream_logs_dir": "/nonexistent-route-coverage-dream-logs",
            "agent_id": ctx["agent_id"],
            "created_by": "route-coverage",
            "embed": False,
            "dry_run": True,
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
        ("POST", "/agentbus/broadcast"): {
            "agent_id": ctx["agent_id"],
            "event_type": "project.attention",
            "project": "mac",
            "payload": {"note": "route case"},
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
        ("POST", "/agents/dispatch-hold/epochs/open"): {
            "epoch_id": "route-coverage-open-empty",
            "participants": [],
        },
        ("POST", "/agents/dispatch-hold/epochs/{epoch_id}/prove"): {
            "identity_sha256": "sha256:" + "a" * 64,
            "proofs": [],
        },
        ("POST", "/agents/dispatch-hold/epochs/{epoch_id}/commit"): {
            "identity_sha256": "sha256:" + "a" * 64,
        },
        ("POST", "/agents/dispatch-hold/epochs/{epoch_id}/abort"): {
            "identity_sha256": "sha256:" + "a" * 64,
            "reason": "route coverage absent epoch",
        },
        ("POST", "/tasks/{task_id}/activity"): {
            "phase": "worker",
            "actor": "operator",
            "summary": "route coverage activity entry",
        },
    }
    query_cases: Dict[RouteKey, Dict[str, Any]] = {
        # `reason` is REQUIRED: cancelling a work package without saying why
        # would leave an audited record with no explanation, so the route
        # refuses it. The coverage case has to supply one or it only ever
        # exercises the 422.
        ("DELETE", "/work-packages/{package_id}"): {
            "params": {"reason": "route coverage", "actor": "route-coverage"}
        },
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
        # Read-only shapes of both memory-tier maintenance routes, so an
        # inventory sweep can never re-embed or retire anything.
        ("POST", "/v1/memory/promote"): {"params": {"dry_run": True}},
        ("POST", "/v1/memory/reconcile-embeddings"): {
            "params": {"tier": "medium", "report_only": True}
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
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
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

        # The memory-tier maintenance routes build their writer through this
        # class too, so the fake has to answer for them or the sweep dies on an
        # AttributeError instead of exercising the route.
        def embedding_space_report(self, *, tier="medium", scan_limit=None):
            return {
                "tier": tier,
                "collection": "mac_memory_%s" % tier,
                "target_model": "fake/embedder",
                "scanned": 0,
                "embedding_models": {},
                "mismatched": 0,
            }

        def reconcile_embedding_spaces(self, **kwargs):
            return {"reembedded": 0, "reembedded_memory_ids": [], "orphaned": []}

        def embed_memory(self, memory_id, **kwargs):
            raise AssertionError(
                "route coverage must not embed; the promote case is dry_run"
            )

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
        if route_path in {
            "/tasks/{task_id}/reviews",
            "/reviews/{review_id}/claim",
            "/reviews/{review_id}/decision",
        }:
            now = utcnow()
            for agent_id in (
                ctx["review_worker_id"],
                ctx["reviewer_agent_id"],
            ):
                cp.store.execute(
                    "UPDATE agents SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, agent_id),
                )
        if route_path == "/tasks/{task_id}/reviews":
            fresh_worker = _ok(
                client.post(
                    "/agents",
                    json={
                        "machine_id": ctx["machine_id"],
                        "name": "route-fresh-review-worker",
                        "capabilities": ["python"],
                    },
                )
            )
            ctx["review_worker_id"] = fresh_worker["id"]
            ctx["review_worker_key"] = fresh_worker["attestation_key"]
            cp.directives.acknowledge(
                ctx["directive_activation_id"],
                agent_id=ctx["review_worker_id"],
                digest=ctx["directive_ack_digest"],
            )
            fresh_task = _ok(
                client.post(
                    "/tasks",
                    json={
                        "title": "Route coverage fresh review request",
                        "project": ctx["project_name"],
                        "required_capabilities": ["python"],
                    },
                )
            )
            ctx["review_task_id"] = fresh_task["id"]
            ctx["executor_evidence_id"] = _prepare_reviewable_task(
                cp,
                task_id=fresh_task["id"],
                worker_id=ctx["review_worker_id"],
                worker_key=ctx["review_worker_key"],
            )
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
        if route_path == "/tasks/{task_id}/reviews":
            ctx["review_id"] = response.json()["id"]
        elif route_path == "/secrets/{secret_id}/access":
            ctx["secret_audit_id"] = response.json()["audit_id"]
        elif route_path == "/directives/{directive_id}/check":
            ctx["directive_check_id"] = response.json()["id"]
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
