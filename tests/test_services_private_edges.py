"""Focused branch coverage for control-plane service edges."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from mac import services
from mac.models import AgentStatus, HealthStatus, NotFoundError, TaskState
from mac.services import ControlPlane


def _fixture(cp: ControlPlane):
    machine = cp.register_machine("host", resources={"cpu": 8})
    agent = cp.register_agent(
        machine.id,
        "agent",
        capabilities=["python", "review"],
        resources={"capacity": 2},
    )
    task = cp.create_task("work", required_capabilities=["python"])
    return machine, agent, task


def test_blocked_manual_repair_history_paths(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    task = SimpleNamespace(id="task", state=TaskState.OPEN.value)
    assert cp._blocked_task_requires_manual_repair(task) is False
    task.state = TaskState.BLOCKED.value
    monkeypatch.setattr(cp, "task_history", lambda *_a, **_k: [])
    assert cp._blocked_task_requires_manual_repair(task) is False
    monkeypatch.setattr(
        cp,
        "task_history",
        lambda *_a, **_k: [
            SimpleNamespace(to_state="open", detail={}),
            SimpleNamespace(to_state="blocked", detail={"manual_repair_required": True}),
        ],
    )
    assert cp._blocked_task_requires_manual_repair(task) is True
    monkeypatch.setattr(
        cp,
        "task_history",
        lambda *_a, **_k: [
            SimpleNamespace(to_state="blocked", detail={"reason": "executor_failed"})
        ],
    )
    assert cp._blocked_task_requires_manual_repair(task) is True


def test_allocator_v2_uses_canonical_task_and_agent_gates() -> None:
    cp = ControlPlane.in_memory()
    _machine, agent, task = _fixture(cp)

    ready = cp.explain_task_dispatch(task.id)
    assert ready["task_ready"] is True
    assert ready["dispatchable"] is True
    assert ready["candidates"][0]["agent_id"] == agent.id
    assert ready["candidates"][0]["reasons"] == []

    missing_capability = cp.create_task(
        "git work",
        required_capabilities=["git"],
    )
    blocked = cp.explain_task_dispatch(missing_capability.id)
    assert blocked["task_ready"] is True
    assert blocked["dispatchable"] is False
    assert blocked["candidates"][0]["reasons"][0]["code"] == ("agent_capabilities_missing")


def test_agent_available_remaining_resource_and_role_gates(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    machine, agent, task = _fixture(cp)
    monkeypatch.setattr(cp, "get_machine", lambda *_a: replace(machine, trusted=False))
    assert cp._agent_available_for(agent, task) is False
    monkeypatch.setattr(cp, "get_machine", lambda *_a: machine)
    monkeypatch.setattr(cp, "_machine_allows_tenant", lambda *_a: False)
    assert cp._agent_available_for(agent, task) is False
    monkeypatch.setattr(cp, "_machine_allows_tenant", lambda *_a: True)
    monkeypatch.setattr(cp, "_agent_resources_satisfy", lambda *_a: False)
    assert cp._agent_available_for(agent, task) is False
    monkeypatch.setattr(cp, "_agent_resources_satisfy", lambda *_a: True)
    monkeypatch.setattr(cp, "_agent_has_repository_commands", lambda *_a: False)
    assert cp._agent_available_for(agent, task) is False
    monkeypatch.setattr(cp, "_agent_has_repository_commands", lambda *_a: True)

    role_task = replace(task, metadata={"required_role": "missing"})
    assert cp._agent_available_for(agent, role_task) is False
    target_role = SimpleNamespace(slug="builder", required_capabilities=["gpu"])
    monkeypatch.setattr(cp.roles, "get_role", lambda *_a: target_role)
    assert (
        cp._agent_available_for(agent, replace(task, metadata={"required_role": "builder"}))
        is False
    )
    capable = replace(agent, capabilities=["python", "review", "gpu"])
    assert (
        cp._agent_available_for(capable, replace(task, metadata={"required_role": "builder"}))
        is True
    )


def test_reviewer_time_identity_and_executor_helpers(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    _machine, agent, task = _fixture(cp)
    monkeypatch.delenv("MAC_DEFAULT_REVIEWER_STALE_AFTER_SECONDS", raising=False)
    monkeypatch.delenv("MAC_AGENT_STALE_AFTER_SECONDS", raising=False)
    assert cp._default_reviewer_stale_after_seconds() == 300
    monkeypatch.setenv("MAC_AGENT_STALE_AFTER_SECONDS", "0")
    assert cp._default_reviewer_stale_after_seconds() == 1
    monkeypatch.setenv("MAC_DEFAULT_REVIEWER_STALE_AFTER_SECONDS", "bad")
    assert cp._default_reviewer_stale_after_seconds() == 300
    assert cp._agent_seen_recently(replace(agent, last_seen_at="bad"), 300) is False

    assert cp._agent_tenant_and_persona(agent) == (None, None)
    attached = replace(agent, hermes_instance_id="persona")
    monkeypatch.setattr(
        cp.identity, "get_persona_instance", lambda *_a: (_ for _ in ()).throw(NotFoundError())
    )
    assert cp._agent_tenant_and_persona(attached) == (None, None)
    monkeypatch.setattr(
        cp.identity,
        "get_persona_instance",
        lambda *_a: SimpleNamespace(tenant_id="tenant", persona_id=None),
    )
    assert cp._agent_tenant_and_persona(attached) == ("tenant", None)
    monkeypatch.setattr(
        cp.identity,
        "get_persona_instance",
        lambda *_a: SimpleNamespace(tenant_id="tenant", persona_id="persona"),
    )
    monkeypatch.setattr(
        cp.identity, "get_persona", lambda *_a: (_ for _ in ()).throw(NotFoundError())
    )
    assert cp._agent_tenant_and_persona(attached) == ("tenant", None)
    monkeypatch.setattr(
        cp.identity, "get_persona", lambda *_a: SimpleNamespace(name="Code Reviewer")
    )
    assert cp._agent_tenant_and_persona(attached) == ("tenant", "code-reviewer")

    monkeypatch.setattr(cp.store, "query_one", lambda *_a, **_k: None)
    assert cp._task_executor_persona_slug(task) is None
    monkeypatch.setattr(cp.store, "query_one", lambda *_a, **_k: {"agent_id": "missing"})
    monkeypatch.setattr(cp, "get_agent", lambda *_a: (_ for _ in ()).throw(NotFoundError()))
    assert cp._task_executor_persona_slug(task) is None


def test_publication_target_and_project_metadata_edges(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("work", project="implicit")
    assert cp._project_publication_target(replace(task, project=None)) is None
    assert cp._project_publication_target(task) is None
    project = cp.create_project("configured", metadata={"publish_target": " target "})
    configured = replace(task, project=project.name)
    assert cp._project_publication_target(configured) == "target"
    cp.update_project(project.id, metadata={"publication": {"target": "nested"}})
    assert cp._project_publication_target(configured) == "nested"

    assert (
        cp._default_publication_target(replace(task, metadata={"publish_target": " direct "}))
        == "direct"
    )
    assert (
        cp._default_publication_target(
            replace(task, metadata={"publication": {"target": " nested "}})
        )
        == "nested"
    )
    assert (
        cp._default_publication_target(
            replace(task, metadata={"acc_metadata": {"beads_id": " abc "}})
        )
        == "beads://abc"
    )
    monkeypatch.setenv("MAC_DEFAULT_PUBLICATION_TARGET", "artifact://archive")
    assert cp._default_publication_target(task) == "artifact://archive"


@pytest.mark.parametrize(
    ("labels", "tenant", "expected"),
    [
        ({"tenant_policy": "bad"}, "a", True),
        ({"tenant_policy": {"mode": "denied"}}, "a", False),
        ({"tenant_policy": {"mode": "private", "tenant_ids": ["a"]}}, None, False),
        ({"tenant_policy": {"deny_tenants": ["a"]}}, "a", False),
        ({"tenant_policy": {"mode": "private", "tenant_ids": ["a"]}}, "a", True),
        ({"tenant_policy": {"allow_tenants": ["b"]}}, "a", False),
        ({"tenant_policy": {}}, "a", True),
    ],
)
def test_machine_tenant_policy_matrix(labels, tenant, expected) -> None:
    cp = ControlPlane.in_memory()
    assert cp._machine_allows_tenant(SimpleNamespace(labels=labels), tenant) is expected


def test_service_role_eligibility_and_holder_edges(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    _machine, agent, _task = _fixture(cp)
    role = SimpleNamespace(required_capabilities=["gpu"], hardware_requirements={}, model_id=None)
    assert cp._agent_eligible_for_service(agent, role) is False
    role.required_capabilities = ["python"]
    role.hardware_requirements = {"gpu": True}
    monkeypatch.setattr(
        "mac.roles_service.machine_hardware_satisfies", lambda *_a: (False, ["gpu"])
    )
    assert cp._agent_eligible_for_service(agent, role) is False
    role.hardware_requirements = {}
    role.model_id = "unknown"
    monkeypatch.setattr(
        "mac.local_gen_catalog.get_model",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("catalog")),
    )
    assert cp._agent_eligible_for_service(agent, role) is True
    monkeypatch.setattr(cp, "get_agent", lambda *_a: (_ for _ in ()).throw(NotFoundError()))
    assert cp._service_holder_live("missing") is False
    monkeypatch.setattr(
        cp, "get_agent", lambda *_a: replace(agent, status=AgentStatus.OFFLINE.value)
    )
    assert cp._service_holder_live("offline") is False


def _verdict(manifest: dict, **extra):
    values = {
        "id": "verdict",
        "created_by": "reviewer",
        "created_at": "2026-01-02T00:00:00+00:00",
        "metadata": {"returncode": 0, "verification": manifest},
    }
    values.update(extra)
    return SimpleNamespace(**values)


def _base_verdict(**extra):
    manifest = {
        "schema": services.VERIFICATION_SCHEMA,
        "status": "complete",
        "evidence_type": "review_verdict",
        "reviewed_evidence_id": "executor",
        "signed_by": "reviewer",
        "signature": "sig",
        "verdict": "approved",
        "worktree_digest": "sha256:" + "a" * 64,
        "tests": [{"status": "passed"}],
    }
    manifest.update(extra)
    return manifest


@pytest.mark.parametrize(
    ("manifest", "metadata", "problem"),
    [
        (_base_verdict(), {"returncode": 2}, "nonzero returncode"),
        (None, {"returncode": 0}, "missing verification"),
        (_base_verdict(schema="bad"), None, "schema mismatch"),
        (_base_verdict(status="running"), None, "status not complete"),
        (_base_verdict(reviewed_evidence_id="other"), None, "references wrong"),
        (_base_verdict(signed_by="other"), None, "signed_by"),
        (_base_verdict(verdict="maybe"), None, "requires verdict"),
        (_base_verdict(worktree_digest="bad"), None, "worktree_digest"),
        (_base_verdict(tests=[]), None, "independent passing check"),
        (_base_verdict(semantic_verdict="invalid"), None, "semantic verdict is invalid"),
        (
            _base_verdict(
                semantic_verdict="approved",
                review_experiment={
                    "blind": True,
                    "protocol": {"protocol_compliant": False},
                },
            ),
            None,
            "blind review protocol is noncompliant",
        ),
    ],
)
def test_review_verdict_rejection_matrix(monkeypatch, manifest, metadata, problem) -> None:
    cp = ControlPlane.in_memory()
    monkeypatch.setattr(cp, "get_task", lambda *_a: SimpleNamespace(metadata={}))
    evidence_metadata = (
        metadata if metadata is not None else {"returncode": 0, "verification": manifest}
    )
    item = _verdict(_base_verdict(), metadata=evidence_metadata)
    monkeypatch.setattr(cp, "list_evidence", lambda *_a: [item])
    monkeypatch.setattr(cp, "_agent_attestation_key", lambda *_a: "key")
    monkeypatch.setattr(services, "verify_verification_manifest_signature", lambda *_a: True)
    monkeypatch.setattr(
        cp, "get_evidence", lambda *_a: SimpleNamespace(metadata={"verification": {}})
    )
    monkeypatch.setattr(services, "cross_llm_review_problems", lambda *_a, **_k: [])
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None
    assert any(problem in value for value in problems)


def test_review_verdict_filters_signature_cross_llm_rejection_and_success(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    monkeypatch.setattr(cp, "get_task", lambda *_a: SimpleNamespace(metadata={}))
    monkeypatch.setattr(cp, "_agent_attestation_key", lambda *_a: None)
    monkeypatch.setattr(cp, "list_evidence", lambda *_a: [_verdict(_base_verdict())])
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None and "no attestation key" in problems[0]

    monkeypatch.setattr(cp, "_agent_attestation_key", lambda *_a: "key")
    monkeypatch.setattr(services, "verify_verification_manifest_signature", lambda *_a: False)
    monkeypatch.setattr(cp.store, "query_one", lambda *_a, **_k: None)
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None and "does not verify" in problems[0]

    monkeypatch.setattr(services, "verify_verification_manifest_signature", lambda *_a: True)
    monkeypatch.setattr(
        cp, "get_evidence", lambda *_a: SimpleNamespace(metadata={"verification": {}})
    )
    monkeypatch.setattr(services, "cross_llm_review_problems", lambda *_a, **_k: ["same model"])
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None and "same model" in problems[0]

    rejected = _verdict(_base_verdict(verdict="rejected"))
    monkeypatch.setattr(cp, "list_evidence", lambda *_a: [rejected])
    monkeypatch.setattr(services, "cross_llm_review_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(services, "rejected_verdict_feedback_problems", lambda *_a: [])
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is rejected and problems == []

    approved = _verdict(_base_verdict())
    monkeypatch.setattr(cp, "list_evidence", lambda *_a: [approved])
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is approved and problems == []


def test_pushed_executor_commit_skips_ephemeral_reachability_probe(monkeypatch, tmp_path) -> None:
    """mac-9kij local-path reachability probe must run only for UNPUSHED work.
    A pushed commit's SHA is durably on origin (re-verified at publish) but is
    frequently unreachable in the recycled per-lease workspace, so probing it
    there produced a false 'not reachable' that failed publishable reviews."""
    import subprocess

    cp = ControlPlane.in_memory()
    # A real git repo that does NOT contain head_sha (the recycled ephemeral ws).
    ws = tmp_path / "repo-lease"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    repo = {
        "head_sha": "a" * 40,
        "files_changed": ["src/x.py"],
        "dirty": False,
        "pushed": True,
        "remote_ref": "refs/heads/mac/agent/task-lease",
        "path": str(ws),
    }
    monkeypatch.setattr(cp, "get_task", lambda *_a: SimpleNamespace(metadata={}))
    monkeypatch.setattr(cp, "_agent_attestation_key", lambda *_a: "key")
    monkeypatch.setattr(services, "verify_verification_manifest_signature", lambda *_a: True)
    monkeypatch.setattr(services, "cross_llm_review_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(cp, "_cooperative_review_integration_problems", lambda *_a, **_k: [])

    def _run_case(executor_pushed):
        exec_repo = {**repo, "pushed": executor_pushed}
        monkeypatch.setattr(
            cp,
            "get_evidence",
            lambda *_a: SimpleNamespace(metadata={"verification": {"repo": exec_repo}}),
        )
        monkeypatch.setattr(
            cp, "list_evidence", lambda *_a: [_verdict(_base_verdict(repo=dict(repo)))]
        )
        return cp._find_review_verdict_evidence("task", "reviewer", executor_evidence_id="executor")

    # Pushed executor commit: the ephemeral probe is skipped -> verdict accepted.
    found, problems = _run_case(True)
    assert not any("not reachable" in p for p in problems), problems
    assert found is not None

    # Unpushed executor commit: the probe runs and correctly flags the missing SHA.
    found_unpushed, problems_unpushed = _run_case(False)
    assert any("not reachable" in p for p in problems_unpushed)
    assert found_unpushed is None


def test_review_verdict_selection_and_time_filters(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    monkeypatch.setattr(cp, "get_task", lambda *_a: SimpleNamespace(metadata={}))
    ignored = _verdict(_base_verdict(), id="ignored", created_by="other")
    old = _verdict(_base_verdict(), id="old", created_at="2025-01-01T00:00:00+00:00")
    invalid = _verdict(_base_verdict(), id="invalid", created_at="bad")
    monkeypatch.setattr(cp, "list_evidence", lambda *_a: [ignored, old, invalid])
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor", not_before="2026-01-01T00:00:00+00:00"
    )
    assert found is None
    assert len(problems) == 2
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor", verdict_evidence_id="missing"
    )
    assert found is None and problems == []


def test_review_verdict_reports_deep_validation_failures(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    task = SimpleNamespace(metadata={})
    evidence = [_verdict(_base_verdict(verdict="rejected"))]
    executor = SimpleNamespace(metadata={"verification": {}})
    monkeypatch.setattr(cp, "get_task", lambda *_a: task)
    monkeypatch.setattr(cp, "list_evidence", lambda *_a: evidence)
    monkeypatch.setattr(cp, "_agent_attestation_key", lambda *_a: "key")
    monkeypatch.setattr(services, "verify_verification_manifest_signature", lambda *_a: True)
    monkeypatch.setattr(cp, "get_evidence", lambda *_a: executor)
    monkeypatch.setattr(services, "cross_llm_review_problems", lambda *_a, **_k: [])

    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None
    assert any("requires feedback" in problem for problem in problems)

    evidence[0] = _verdict(_base_verdict())
    executor.metadata = {"verification": ["invalid"]}
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None
    assert any("cannot resolve executor" in problem for problem in problems)

    executor.metadata = {"verification": {"repo": {"head_sha": "a" * 40}}}
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None
    assert any("verification.repo object" in problem for problem in problems)

    executor.metadata = {"verification": {}}
    task.metadata = {"coordination": {"phase": "integration", "child_outputs": []}}
    found, problems = cp._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is None
    assert any("cooperative integration" in problem for problem in problems)


def test_reviewer_independence_problem_edges(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    task = SimpleNamespace(metadata={})
    reviewer = SimpleNamespace(id="reviewer", machine_id="machine")
    monkeypatch.setattr(cp, "_coordination_excluded_agent_ids", lambda *_a: {"reviewer"})
    assert "cooperative work family" in cp._reviewer_independence_problem(task, reviewer)

    monkeypatch.setattr(cp, "_coordination_excluded_agent_ids", lambda *_a: set())
    monkeypatch.setattr(cp, "_task_tenant_id", lambda *_a: "tenant-a")
    monkeypatch.setattr(cp, "_agent_tenant_and_persona", lambda *_a: (None, None))
    monkeypatch.setattr(
        cp,
        "get_machine",
        lambda *_a: (_ for _ in ()).throw(NotFoundError("missing")),
    )
    assert "machine is missing" in cp._reviewer_independence_problem(task, reviewer)

    monkeypatch.setattr(cp, "get_machine", lambda *_a: SimpleNamespace())
    monkeypatch.setattr(cp, "_machine_allows_tenant", lambda *_a: False)
    assert "tenant boundary" in cp._reviewer_independence_problem(task, reviewer)

    monkeypatch.setattr(cp, "_agent_tenant_and_persona", lambda *_a: ("tenant-b", None))
    assert "tenant boundary" in cp._reviewer_independence_problem(task, reviewer)

    monkeypatch.setattr(cp, "_task_tenant_id", lambda *_a: None)
    monkeypatch.setattr(cp, "_agent_tenant_and_persona", lambda *_a: (None, "shared-persona"))
    monkeypatch.setattr(cp, "_task_executor_persona_slug", lambda *_a: "shared-persona")
    assert "same persona" in cp._reviewer_independence_problem(task, reviewer)
