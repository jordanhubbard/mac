"""Contract tests for direct human-driven OpenClaw Slack code execution.

Covers the acceptance criteria for
``mac.openclaw_direct_execution``:

* direct human execution begins without a manual task-filing exchange,
* automatic task-keyed bookkeeping when the gates require it,
* conversation idempotency (thread follow-ups attach, not duplicate),
* isolated writable worktrees at the attested base SHA,
* candidate review keyed to the exact candidate SHA,
* deferred-work task filing, and
* legacy-Hermes-name containment behind an adapter.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane
from mac.openclaw_direct_execution import (
    Capability,
    ExecutionMode,
    ExecutionStatus,
    HumanDirective,
    MissingCapabilityError,
    OpenClawDirectExecutionService,
    RepositoryTarget,
    SlackProvenance,
    WritableWorktree,
    classify_request,
    legacy_hermes_instance_adapter,
)


BASE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
OTHER_SHA = "c" * 40


def _isolated_provisioner(calls: list | None = None):
    def provision(repo: RepositoryTarget, branch: str) -> WritableWorktree:
        if calls is not None:
            calls.append((repo.repository_id, branch))
        return WritableWorktree(
            path="/tmp/openclaw/%s" % branch.replace("/", "_"),
            branch=branch,
            base_sha=repo.base_sha,
            isolated=True,
        )

    return provision


def _cp(provisioner=None) -> ControlPlane:
    cp = ControlPlane.in_memory()
    cp.openclaw_direct_execution._provision_worktree = (
        provisioner or _isolated_provisioner()
    )
    return cp


def _persona(cp: ControlPlane):
    tenant = cp.register_tenant("acme")
    return cp.register_persona_instance(tenant.id, "main")


def _direct_directive() -> HumanDirective:
    return HumanDirective(human_id="human_1", authenticated=True, text="fix the bug")


def _slack(thread_ts: str = "1700000000.0001", message_ts: str = "1700000000.0002") -> SlackProvenance:
    return SlackProvenance(
        workspace_id="W1",
        channel_id="C1",
        thread_ts=thread_ts,
        message_ts=message_ts,
    )


def _repo() -> RepositoryTarget:
    return RepositoryTarget("projectrepo_1", "mac", BASE_SHA)


def _begin(cp: ControlPlane, instance, **overrides):
    kwargs = dict(
        persona_instance_id=instance.id,
        directive=_direct_directive(),
        slack=_slack(),
        repository=_repo(),
    )
    kwargs.update(overrides)
    return cp.openclaw_direct_execution.begin_conversation_execution(**kwargs)


# --- Direct human execution without manual task filing -------------------

def test_direct_request_begins_immediately_without_manual_filing():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance)
    # Acted immediately: writable worktree provisioned, no manual task-filing
    # exchange required first.
    assert execution.mode is ExecutionMode.DIRECT
    assert execution.status is ExecutionStatus.WRITABLE
    assert execution.worktree is not None
    assert execution.worktree.isolated is True


def test_direct_request_grants_write_worktree_not_publish_or_merge():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance)
    assert execution.has_capability(Capability.WRITE_WORKTREE)
    assert execution.has_capability(Capability.SOURCE_INSPECTION)
    # write_worktree never implies publish or merge.
    assert not execution.has_capability(Capability.PUBLISH_BRANCH)
    assert not execution.has_capability(Capability.MERGE)


def test_direct_request_cannot_escalate_to_publish_capability():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(
        cp,
        instance,
        requested_capabilities=[Capability.WRITE_WORKTREE, Capability.PUBLISH_BRANCH, Capability.MERGE],
    )
    # A direct human directive can only grant inspection + write_worktree.
    assert Capability.PUBLISH_BRANCH not in execution.granted_capabilities
    assert Capability.MERGE not in execution.granted_capabilities


def test_unauthenticated_request_does_not_execute_directly():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(
        cp,
        instance,
        directive=HumanDirective(human_id="human_1", authenticated=False),
    )
    # Not a direct execution; it is captured as deferred work instead.
    assert execution.mode is not ExecutionMode.DIRECT
    assert execution.status is ExecutionStatus.PENDING
    assert execution.worktree is None


# --- Automatic task-keyed bookkeeping ------------------------------------

def test_direct_execution_auto_materializes_bookkeeping_task():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance)
    # The task-keyed gates need a task_id; one is materialized transparently.
    assert execution.task_id is not None
    task = cp.get_task(execution.task_id)
    origin = task.metadata["origin"]
    assert origin["type"] == "openclaw_direct_execution"
    assert origin["auto_materialized"] is True
    assert origin["bookkeeping_only"] is True
    assert origin["conversation_execution_id"] == execution.id


def test_bookkeeping_task_binds_full_provenance():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance, agent_id="agent_rocky")
    origin = cp.get_task(execution.task_id).metadata["origin"]
    assert origin["persona_instance_id"] == instance.id
    assert origin["agent_id"] == "agent_rocky"
    assert origin["human_id"] == "human_1"
    assert origin["slack"]["workspace_id"] == "W1"
    assert origin["slack"]["channel_id"] == "C1"
    assert origin["slack"]["thread_ts"] == "1700000000.0001"
    assert origin["repository"]["repository_id"] == "projectrepo_1"
    assert origin["repository"]["base_sha"] == BASE_SHA


# --- Conversation idempotency --------------------------------------------

def test_thread_followup_attaches_to_same_execution():
    calls: list = []
    cp = _cp(_isolated_provisioner(calls))
    instance = _persona(cp)
    first = _begin(cp, instance)
    # A later message in the same thread (different message_ts) must attach.
    second = _begin(
        cp,
        instance,
        slack=_slack(message_ts="1700000000.9999"),
        directive=HumanDirective(human_id="human_1", authenticated=True, text="also rename"),
    )
    assert second.id == first.id
    # Only one worktree was ever provisioned for the thread.
    assert len(calls) == 1


def test_different_thread_creates_distinct_execution():
    cp = _cp()
    instance = _persona(cp)
    first = _begin(cp, instance)
    second = _begin(cp, instance, slack=_slack(thread_ts="1700000000.5555"))
    assert second.id != first.id
    assert second.idempotency_key != first.idempotency_key


# --- Isolated writable worktree at attested base -------------------------

def test_worktree_bound_to_attested_base_sha():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance)
    assert execution.worktree.base_sha == BASE_SHA
    assert execution.worktree.branch.startswith("openclaw/%s/" % instance.id)


def test_worktree_base_mismatch_fails_closed():
    def bad_provisioner(repo: RepositoryTarget, branch: str) -> WritableWorktree:
        return WritableWorktree("/tmp/wt", branch, OTHER_SHA, isolated=True)

    cp = _cp(bad_provisioner)
    instance = _persona(cp)
    with pytest.raises(MissingCapabilityError) as exc:
        _begin(cp, instance)
    assert exc.value.capability == "base_attestation"


def test_non_isolated_worktree_fails_closed():
    def shared_provisioner(repo: RepositoryTarget, branch: str) -> WritableWorktree:
        return WritableWorktree("/shared/host", branch, repo.base_sha, isolated=False)

    cp = _cp(shared_provisioner)
    instance = _persona(cp)
    with pytest.raises(MissingCapabilityError) as exc:
        _begin(cp, instance)
    assert exc.value.capability == "write_worktree"


def test_missing_provisioner_fails_closed():
    cp = ControlPlane.in_memory()  # no provisioner injected
    instance = _persona(cp)
    with pytest.raises(MissingCapabilityError) as exc:
        _begin(cp, instance)
    assert exc.value.capability == "write_worktree"
    assert exc.value.to_dict()["code_change_occurred"] is False


def test_invalid_base_sha_fails_closed_before_any_worktree():
    calls: list = []
    cp = _cp(_isolated_provisioner(calls))
    instance = _persona(cp)
    with pytest.raises(MissingCapabilityError) as exc:
        _begin(cp, instance, repository=RepositoryTarget("projectrepo_1", "mac", "nope"))
    assert exc.value.capability == "base_attestation"
    assert calls == []  # never provisioned a worktree on a bad attestation


def test_missing_repository_identity_fails_closed():
    cp = _cp()
    instance = _persona(cp)
    with pytest.raises(MissingCapabilityError) as exc:
        _begin(cp, instance, repository=RepositoryTarget("", "mac", BASE_SHA))
    assert exc.value.capability == "repository"


# --- Candidate review keyed to exact candidate SHA -----------------------

def _pass_all_gates(svc: OpenClawDirectExecutionService, execution_id: str) -> None:
    svc.record_gate_result(execution_id, "tests", True)
    svc.record_gate_result(execution_id, "codegraph", True)
    svc.record_gate_result(execution_id, "evidence", True)


def test_publish_blocked_until_every_gate_and_review_pass():
    cp = _cp()
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    execution = _begin(cp, instance)
    svc.record_candidate(
        execution.id, candidate_ref="refs/openclaw/cand", candidate_sha=CANDIDATE_SHA
    )
    # No gates yet, and no publish capability: blocked.
    reloaded = svc.get_execution(execution.id)
    allowed, reason = reloaded.can_publish()
    assert not allowed

    # Grant publish, still blocked until gates + matching review pass.
    reloaded.granted_capabilities.append(Capability.PUBLISH_BRANCH)
    svc._persist(reloaded)
    _pass_all_gates(svc, execution.id)
    with pytest.raises(MissingCapabilityError):
        svc.publish_candidate(execution.id)

    # Review must target the exact candidate SHA.
    svc.record_review(execution.id, reviewed_sha=OTHER_SHA, passed=True)
    with pytest.raises(MissingCapabilityError):
        svc.publish_candidate(execution.id)

    svc.record_review(execution.id, reviewed_sha=CANDIDATE_SHA, passed=True)
    published = svc.publish_candidate(execution.id)
    assert published.status is ExecutionStatus.PUBLISHED


def test_review_of_wrong_sha_does_not_count_as_passed():
    cp = _cp()
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    execution = _begin(cp, instance)
    svc.record_candidate(
        execution.id, candidate_ref="refs/x", candidate_sha=CANDIDATE_SHA
    )
    reviewed = svc.record_review(execution.id, reviewed_sha=OTHER_SHA, passed=True)
    # Passed review against a different tree is not a review of this candidate.
    assert reviewed.gate_results["review"] is False
    assert not reviewed.review_matches_candidate()


def test_merge_requires_merge_capability_and_all_publish_gates():
    cp = _cp()
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    execution = _begin(cp, instance)
    svc.record_candidate(execution.id, candidate_ref="refs/x", candidate_sha=CANDIDATE_SHA)
    _pass_all_gates(svc, execution.id)
    svc.record_review(execution.id, reviewed_sha=CANDIDATE_SHA, passed=True)
    reloaded = svc.get_execution(execution.id)
    # No publish or merge capability yet: merge blocked.
    with pytest.raises(MissingCapabilityError):
        svc.merge_candidate(execution.id)
    reloaded.granted_capabilities.extend([Capability.PUBLISH_BRANCH, Capability.MERGE])
    svc._persist(reloaded)
    merged = svc.merge_candidate(execution.id)
    assert merged.status is ExecutionStatus.MERGED


def test_candidate_requires_valid_sha():
    cp = _cp()
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    execution = _begin(cp, instance)
    with pytest.raises(ValidationError):
        svc.record_candidate(execution.id, candidate_ref="refs/x", candidate_sha="short")


# --- Deferred / handoff work files a visible task ------------------------

@pytest.mark.parametrize(
    "flag,mode",
    [
        ("deferred", ExecutionMode.DEFERRED),
        ("delegated", ExecutionMode.DELEGATED),
        ("autonomous_followup", ExecutionMode.AUTONOMOUS_FOLLOWUP),
        ("requested_followup", ExecutionMode.REQUESTED_FOLLOWUP),
    ],
)
def test_deferred_work_files_task_instead_of_executing(flag, mode):
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance, **{flag: True})
    assert execution.mode is mode
    assert execution.status is ExecutionStatus.PENDING
    assert execution.worktree is None
    assert execution.task_id is not None
    origin = cp.get_task(execution.task_id).metadata["origin"]
    assert origin["type"] == "openclaw_deferred_work"
    assert origin["deferred"] is True


def test_classify_request_prioritizes_explicit_handoff_over_direct():
    directive = _direct_directive()
    assert classify_request(directive=directive) is ExecutionMode.DIRECT
    assert classify_request(directive=directive, delegated=True) is ExecutionMode.DELEGATED
    assert (
        classify_request(directive=directive, requested_followup=True)
        is ExecutionMode.REQUESTED_FOLLOWUP
    )


# --- Legacy Hermes-name containment --------------------------------------

def test_legacy_hermes_instance_id_accepted_only_behind_adapter():
    cp = _cp()
    instance = _persona(cp)
    # The legacy name is accepted behind the adapter, mapping to persona id.
    execution = cp.openclaw_direct_execution.begin_conversation_execution(
        hermes_instance_id=instance.id,
        directive=_direct_directive(),
        slack=_slack(),
        repository=_repo(),
    )
    assert execution.persona_instance_id == instance.id
    assert execution.mode is ExecutionMode.DIRECT


def test_legacy_adapter_is_identity_mapping():
    assert legacy_hermes_instance_adapter("persona_123") == "persona_123"
    with pytest.raises(ValidationError):
        legacy_hermes_instance_adapter("")


def test_execution_dict_uses_first_class_openclaw_terminology():
    cp = _cp()
    instance = _persona(cp)
    payload = _begin(cp, instance).to_dict()
    # First-class OpenClaw/agent terminology in the public contract.
    assert payload["schema"] == "mac.openclaw_conversation_execution.v1"
    assert "persona_instance_id" in payload
    # No hermes_instance key is exposed in the OpenClaw execution contract.
    assert "hermes_instance_id" not in payload
    assert "hermes_instance" not in payload


# --- HTTP surface --------------------------------------------------------

def _client(cp: ControlPlane) -> TestClient:
    return TestClient(create_app(control_plane=cp))


def test_http_begin_direct_execution_roundtrip():
    cp = _cp()
    client = _client(cp)
    tenant = client.post("/tenants", json={"name": "acme"}).json()
    instance = client.post(
        "/persona-instances", json={"tenant_id": tenant["id"], "name": "main"}
    ).json()
    resp = client.post(
        "/persona-instances/%s/openclaw-executions" % instance["id"],
        json={
            "human_id": "human_1",
            "slack_workspace_id": "W1",
            "slack_channel_id": "C1",
            "slack_thread_ts": "1700000000.0001",
            "repository_id": "projectrepo_1",
            "repository_name": "mac",
            "base_sha": BASE_SHA,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "writable"
    assert body["mode"] == "direct"
    # Follow-up in the same thread attaches to the same execution over HTTP.
    resp2 = client.post(
        "/persona-instances/%s/openclaw-executions" % instance["id"],
        json={
            "human_id": "human_1",
            "slack_workspace_id": "W1",
            "slack_channel_id": "C1",
            "slack_thread_ts": "1700000000.0001",
            "slack_message_ts": "1700000000.7777",
            "repository_id": "projectrepo_1",
            "repository_name": "mac",
            "base_sha": BASE_SHA,
        },
    )
    assert resp2.json()["id"] == body["id"]
    fetched = client.get("/openclaw-executions/%s" % body["id"])
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


def test_http_fails_closed_on_bad_base_attestation():
    cp = _cp()
    client = _client(cp)
    tenant = client.post("/tenants", json={"name": "acme"}).json()
    instance = client.post(
        "/persona-instances", json={"tenant_id": tenant["id"], "name": "main"}
    ).json()
    resp = client.post(
        "/persona-instances/%s/openclaw-executions" % instance["id"],
        json={
            "human_id": "human_1",
            "slack_workspace_id": "W1",
            "slack_channel_id": "C1",
            "slack_thread_ts": "1700000000.0001",
            "repository_id": "projectrepo_1",
            "repository_name": "mac",
            "base_sha": "not-a-sha",
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code_change_occurred"] is False
    assert detail["missing_capability"] == "base_attestation"


def test_persistence_survives_reload_from_store():
    cp = _cp()
    instance = _persona(cp)
    execution = _begin(cp, instance)
    reloaded = cp.openclaw_direct_execution.get_execution(execution.id)
    assert reloaded.id == execution.id
    assert reloaded.persona_instance_id == instance.id
    assert reloaded.granted_capabilities == execution.granted_capabilities
    with pytest.raises(NotFoundError):
        cp.openclaw_direct_execution.get_execution("openclaw-exec_missing")


# --- Validation edges -----------------------------------------------------

def test_missing_persona_instance_id_is_rejected():
    cp = _cp()
    with pytest.raises(ValidationError):
        cp.openclaw_direct_execution.begin_conversation_execution(
            directive=_direct_directive(), slack=_slack(), repository=_repo()
        )


def test_incomplete_slack_provenance_rejected():
    cp = _cp()
    instance = _persona(cp)
    with pytest.raises(ValidationError):
        _begin(
            cp,
            instance,
            slack=SlackProvenance(workspace_id="", channel_id="C1", thread_ts="1.1"),
        )


def test_direct_directive_requires_authenticated_human():
    with pytest.raises(ValidationError):
        HumanDirective(human_id="", authenticated=True).require_authenticated()
    with pytest.raises(ValidationError):
        HumanDirective(human_id="human_1", authenticated=False).require_authenticated()


def test_deferred_mode_property_files_task():
    assert ExecutionMode.DIRECT.files_task is False
    assert ExecutionMode.DEFERRED.files_task is True
    assert ExecutionMode.DELEGATED.files_task is True


def test_record_candidate_without_worktree_fails_closed():
    cp = ControlPlane.in_memory()  # no provisioner -> no worktree possible
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    # Deferred path yields a PENDING execution with no worktree.
    execution = svc.begin_conversation_execution(
        persona_instance_id=instance.id,
        directive=_direct_directive(),
        slack=_slack(),
        repository=_repo(),
        deferred=True,
    )
    with pytest.raises(MissingCapabilityError) as exc:
        svc.record_candidate(execution.id, candidate_ref="r", candidate_sha=CANDIDATE_SHA)
    assert exc.value.capability == "write_worktree"


def test_unknown_gate_rejected():
    cp = _cp()
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    execution = _begin(cp, instance)
    with pytest.raises(ValidationError):
        svc.record_gate_result(execution.id, "not_a_gate", True)


def test_can_publish_reasons_progress_through_gates():
    cp = _cp()
    svc = cp.openclaw_direct_execution
    instance = _persona(cp)
    execution = _begin(cp, instance)
    # No candidate yet.
    reloaded = svc.get_execution(execution.id)
    reloaded.granted_capabilities.append(Capability.PUBLISH_BRANCH)
    svc._persist(reloaded)
    allowed, reason = svc.get_execution(execution.id).can_publish()
    assert not allowed and "no candidate" in reason
    svc.record_candidate(execution.id, candidate_ref="r", candidate_sha=CANDIDATE_SHA)
    allowed, reason = svc.get_execution(execution.id).can_publish()
    assert not allowed and "mandatory gates" in reason
    _pass_all_gates(svc, execution.id)
    # tests+codegraph+evidence pass, but review has not run yet.
    allowed, reason = svc.get_execution(execution.id).can_publish()
    assert not allowed and "review" in reason
    # A review against the wrong SHA does not satisfy the review gate.
    svc.record_review(execution.id, reviewed_sha=OTHER_SHA, passed=True)
    allowed, reason = svc.get_execution(execution.id).can_publish()
    assert not allowed
