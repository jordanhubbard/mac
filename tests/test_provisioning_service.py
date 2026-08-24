"""Tests for the agent-provisioning hook (the on-demand-agent signal).

For now the provisioner is unimplemented — these tests pin the *signal*
side: dispatch and the default-review workflow must emit
``agent_provisioning_requests`` rows when no eligible agent exists, and
the rows must carry enough detail (role, capabilities, hardware, task)
that a future provisioner can act on them.
"""

from __future__ import annotations

import pytest

from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA
from mac.models import ProvisioningStatus, ValidationError
from mac.services import ControlPlane
from tests.conftest import bind_soul


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def test_request_agent_records_signal_and_observability(cp):
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        role_slug="qa-engineer",
        capabilities=["python", "qa"],
        hardware={"cpu_arch": ["arm64"]},
        detail={"hint": "scale-out"},
    )
    assert request.status == ProvisioningStatus.PENDING.value
    assert request.role_slug == "qa-engineer"
    assert set(request.capabilities) == {"python", "qa"}
    assert request.hardware == {"cpu_arch": ["arm64"]}
    # Observability picks it up so external watchers can subscribe.
    names = {event.name for event in cp.list_observability(limit=20)}
    assert "provisioning.agent_requested" in names


def test_request_is_idempotent_on_persistent_shortage(cp):
    first = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        role_slug="code-reviewer",
        task_id=None,
    )
    second = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        role_slug="code-reviewer",
        task_id=None,
    )
    # Same (reason, role, task, tenant) + still pending => same row,
    # updated_at refreshed.
    assert first.id == second.id
    pending = cp.provisioning.list_pending_requests()
    assert len(pending) == 1


def test_fulfill_marks_request_fulfilled(cp):
    machine = cp.register_machine("h")
    # mac-1oi4: agent must have the required role + capabilities to
    # satisfy the request; assigning before fulfill is the legitimate
    # operator workflow.
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    soul = bind_soul(cp, persona_name="qa-soul", allowed_role_slugs=["qa"])
    agent = cp.register_agent(machine.id, "newcomer", capabilities=["python"], hermes_instance_id=soul)
    cp.roles.assign_role(agent.id, "qa")
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        role_slug="qa",
        requested_by="dispatcher",
    )
    fulfilled = cp.provisioning.fulfill_request(
        request.id, agent.id, fulfilled_by="operator"
    )
    assert fulfilled.status == ProvisioningStatus.FULFILLED.value
    assert fulfilled.fulfilled_agent_id == agent.id
    assert fulfilled.closed_at is not None


def test_fulfill_refuses_same_actor_request_and_approval(cp):
    """mac-1oi4: a compromised dispatcher cannot request-then-fulfill its
    own request — the two-party check must reject same-actor approval."""
    machine = cp.register_machine("h")
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    soul = bind_soul(cp, persona_name="qa-soul", allowed_role_slugs=["qa"])
    agent = cp.register_agent(machine.id, "a", capabilities=["python"], hermes_instance_id=soul)
    cp.roles.assign_role(agent.id, "qa")
    request = cp.provisioning.request_agent(
        reason="r", role_slug="qa", requested_by="dispatcher"
    )
    with pytest.raises(ValidationError, match="two-party check"):
        cp.provisioning.fulfill_request(request.id, agent.id, fulfilled_by="dispatcher")
    # A different approver succeeds.
    ok = cp.provisioning.fulfill_request(request.id, agent.id, fulfilled_by="operator")
    assert ok.status == ProvisioningStatus.FULFILLED.value


def test_fulfill_refuses_agent_lacking_required_role_or_capabilities(cp):
    """mac-1oi4: don't let the operator satisfy a request with an
    unrelated agent. Capabilities and role must match the request."""
    machine = cp.register_machine("h")
    cp.roles.create_role(slug="qa", name="qa", description="d", system_prompt="p", level="ic")
    cp.roles.create_role(slug="ops", name="ops", description="d", system_prompt="p", level="ic")
    ops_soul = bind_soul(cp, persona_name="ops-soul", allowed_role_slugs=["ops"])
    qa_soul = bind_soul(cp, persona_name="qa-soul-2", allowed_role_slugs=["qa"])
    wrong_role_agent = cp.register_agent(machine.id, "wrong", capabilities=["python"], hermes_instance_id=ops_soul)
    cp.roles.assign_role(wrong_role_agent.id, "ops")
    no_caps_agent = cp.register_agent(machine.id, "nocap", hermes_instance_id=qa_soul)
    cp.roles.assign_role(no_caps_agent.id, "qa")

    request = cp.provisioning.request_agent(
        reason="r",
        role_slug="qa",
        capabilities=["python"],
        requested_by="dispatcher",
    )
    with pytest.raises(ValidationError, match="role"):
        cp.provisioning.fulfill_request(request.id, wrong_role_agent.id, fulfilled_by="ops")
    with pytest.raises(ValidationError, match="capabilities"):
        cp.provisioning.fulfill_request(request.id, no_caps_agent.id, fulfilled_by="ops")


def test_fulfill_refuses_agent_lacking_required_commands(cp):
    machine = cp.register_machine("h")
    missing_gh = cp.register_agent(
        machine.id,
        "missing-gh",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git"],
            }
        },
    )
    ready = cp.register_agent(
        machine.id,
        "ready",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
            }
        },
    )
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        capabilities=["python"],
        detail={"required_commands": ["python3", "git", "gh"]},
        requested_by="dispatcher",
    )

    with pytest.raises(ValidationError, match="required commands"):
        cp.provisioning.fulfill_request(request.id, missing_gh.id, fulfilled_by="operator")

    fulfilled = cp.provisioning.fulfill_request(
        request.id,
        ready.id,
        fulfilled_by="operator",
    )
    assert fulfilled.status == ProvisioningStatus.FULFILLED.value
    assert fulfilled.fulfilled_agent_id == ready.id


def test_provisioner_hook_runs_synchronously(cp):
    machine = cp.register_machine("h")
    spawned = cp.register_agent(machine.id, "auto")
    seen: list = []

    def stub_provisioner(request):
        seen.append(request.id)
        return spawned.id

    cp.provisioning.register_provisioner(stub_provisioner)
    request = cp.provisioning.request_agent(reason="dispatch.no_eligible_agent")
    # Hook fulfilled the request synchronously.
    assert request.status == ProvisioningStatus.FULFILLED.value
    assert request.fulfilled_agent_id == spawned.id
    assert seen == [request.id]


def test_request_listener_wakes_on_new_and_refreshed_durable_signal(cp):
    seen: list[str] = []

    def listener(request):
        seen.append(request.id)

    cp.provisioning.register_request_listener(listener)
    first = cp.provisioning.request_agent(reason="dispatch.no_eligible_agent")
    second = cp.provisioning.request_agent(reason="dispatch.no_eligible_agent")
    cp.provisioning.unregister_request_listener(listener)
    cp.provisioning.request_agent(reason="review.no_eligible_reviewer")

    assert first.id == second.id
    assert seen == [first.id, first.id]


def test_request_listener_failure_does_not_abort_dispatch_signal(cp):
    def broken_listener(_request):
        raise RuntimeError("wake path unavailable")

    cp.provisioning.register_request_listener(broken_listener)
    request = cp.provisioning.request_agent(reason="dispatch.no_eligible_agent")

    assert request.status == ProvisioningStatus.PENDING.value
    names = {event.name for event in cp.list_observability(limit=30)}
    assert "provisioning.listener_failed" in names


def test_provisioner_hook_result_must_satisfy_required_commands(cp):
    machine = cp.register_machine("h")
    missing_gh = cp.register_agent(
        machine.id,
        "auto-missing-gh",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git"],
            }
        },
    )

    cp.provisioning.register_provisioner(lambda request: missing_gh.id)
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        capabilities=["python"],
        detail={"required_commands": ["python3", "git", "gh"]},
    )

    assert request.status == ProvisioningStatus.PENDING.value
    assert request.fulfilled_agent_id is None
    names = {event.name for event in cp.list_observability(limit=30)}
    assert "provisioning.hook_failed" in names


def test_provisioner_hook_failure_does_not_abort_request(cp):
    def broken_provisioner(request):
        raise RuntimeError("provisioner stack is down")

    cp.provisioning.register_provisioner(broken_provisioner)
    request = cp.provisioning.request_agent(reason="dispatch.no_eligible_agent")
    # Request still landed; hook failure was logged via observability.
    assert request.status == ProvisioningStatus.PENDING.value
    names = {event.name for event in cp.list_observability(limit=30)}
    assert "provisioning.hook_failed" in names


def test_dispatch_emits_provisioning_signal_when_no_agent_matches(cp):
    # A task that no agent can claim — no agents registered at all.
    task = cp.create_task("orphan", required_capabilities=["python"])
    assert cp.dispatch_once(lease_seconds=300) is None
    pending = cp.provisioning.list_pending_requests()
    assert len(pending) == 1
    assert pending[0].task_id == task.id
    assert pending[0].reason == "dispatch.no_eligible_agent"
    assert set(pending[0].capabilities) == {"python"}


def test_dispatch_signal_carries_role_and_hardware_from_task_metadata(cp):
    cp.create_task(
        "specialized",
        required_capabilities=["python"],
        metadata={
            "required_role": "gpu-runner",
            "hardware": {"cpu_arch": ["arm64"], "memory_gb_min": 64},
        },
    )
    cp.dispatch_once(lease_seconds=300)
    pending = cp.provisioning.list_pending_requests()
    assert len(pending) == 1
    assert pending[0].role_slug == "gpu-runner"
    assert pending[0].hardware == {"cpu_arch": ["arm64"], "memory_gb_min": 64}


def test_dispatch_signal_is_idempotent_across_ticks(cp):
    cp.create_task("orphan", required_capabilities=["python"])
    # Three dispatch passes; the signal should remain a single pending row.
    for _ in range(3):
        cp.dispatch_once(lease_seconds=300)
    pending = cp.provisioning.list_pending_requests()
    assert len(pending) == 1


def test_review_workflow_emits_provisioning_signal_when_no_reviewer(cp, semantic_reviewer_on):
    # Worker is the only agent; it cannot review its own work, so the
    # default review workflow has no eligible reviewer. The signal must
    # explain what's missing.
    machine = cp.register_machine("h")
    worker = cp.register_agent(machine.id, "worker", capabilities=["python"])
    task = cp.create_task("solo", required_capabilities=["python"])
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    from mac.services import sign_verification_manifest

    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "pushed": True,
            "remote_ref": "refs/heads/x",
            "dirty": False,
            "files_changed": ["src/x.py"],
        },
        "codegraph": {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": ["src/x.py"],
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        },
        "tests": [{"command": "pytest", "returncode": 0}],
    }
    key = cp._agent_attestation_key(worker.id)
    manifest["signed_by"] = worker.id
    manifest["signature"] = sign_verification_manifest(key, manifest)
    cp.add_evidence(
        task.id,
        "log",
        "artifact://x",
        "done",
        worker.id,
        metadata={"returncode": 0, "verification": manifest},
    )
    cp.submit_for_review(task.id, worker.id)
    result = cp.advance_default_review_workflow(task.id)
    assert result["status"] == "waiting_for_reviewer"
    pending = cp.provisioning.list_pending_requests()
    assert any(
        req.reason == "review.no_eligible_reviewer"
        and req.task_id == task.id
        and "review" in req.capabilities
        for req in pending
    )


def test_cancel_request_terminates_signal(cp):
    request = cp.provisioning.request_agent(reason="dispatch.no_eligible_agent")
    cancelled = cp.provisioning.cancel_request(request.id, reason="not-needed")
    assert cancelled.status == ProvisioningStatus.CANCELLED.value
    assert "cancel_reason" in cancelled.detail
    # A second cancel is idempotent.
    again = cp.provisioning.cancel_request(request.id)
    assert again.status == ProvisioningStatus.CANCELLED.value


def test_request_agent_requires_reason(cp):
    with pytest.raises(ValidationError):
        cp.provisioning.request_agent(reason="")
