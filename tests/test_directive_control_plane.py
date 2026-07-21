from __future__ import annotations

from mac.services import ControlPlane
from mac.models import json_loads
from mac.work_package_service import RepositoryBaseAttestation
from mac.work_plan_admission import CanonicalRepositoryBase


class _BaseResolver:
    def resolve(self, repository, *, requested_ref=None):
        return CanonicalRepositoryBase(
            repository_id=repository["id"],
            planning_base_ref=requested_ref or "refs/heads/main",
            planning_base_sha="a" * 40,
            resource_namespace={"status": "unresolved"},
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


def test_control_plane_pins_effective_snapshot_on_new_tasks(monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    proposed = cp.propose_directive(
        {
            "schema": "mac.directive.v1",
            "name": "review.require-independent",
            "description": "Require an independent review for new work.",
            "scope": "fleet",
            "set": {"review.independent_required": True},
        },
        actor="operator",
    )
    version = proposed["versions"][0]
    checked = cp.check_directive(proposed["id"], actor="operator")
    cp.approve_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        check_id=checked["id"],
        actor="operator",
    )
    activation = cp.activate_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )
    assert activation["state"] == "active"  # no live workers in the cohort

    task = cp.create_task("Document the policy snapshot")
    snapshot = task.metadata["directive_snapshot"]
    assert snapshot["schema"] == "mac.directive.snapshot.v1"
    assert snapshot["set"]["review.independent_required"] is True
    assert snapshot["set"]["verification.tests_required"] is True
    assert snapshot["epoch"] == activation["epoch"]


def test_dispatch_gate_rejects_unacknowledged_policy_epoch(monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker")
    task = cp.create_task("Wait for policy convergence")
    proposed = cp.propose_directive(
        {
            "schema": "mac.directive.v1",
            "name": "review.require-independent",
            "description": "Require an independent review for new work.",
            "scope": "fleet",
            "set": {"review.independent_required": True},
        },
        actor="operator",
    )
    version = proposed["versions"][0]
    checked = cp.check_directive(proposed["id"], actor="operator")
    cp.approve_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        check_id=checked["id"],
        actor="operator",
    )
    activation = cp.activate_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )

    eligible, reason = cp._agent_availability_for_task(agent, task)
    assert eligible is False
    assert reason == "directive_policy_unacknowledged"

    cp.acknowledge_directive_activation(
        agent.id, activation["id"], digest=version["digest"]
    )
    _eligible_after, reason_after = cp._agent_availability_for_task(agent, task)
    assert reason_after != "directive_policy_unacknowledged"


def test_registered_workflow_macro_materializes_real_held_package(monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    cp.store.execute(
        "INSERT INTO projects (id, name, description, metadata, status, created_at, updated_at) "
        "VALUES ('project_demo', 'demo', '', '{}', 'active', 'created', 'updated')"
    )
    cp.store.execute(
        "INSERT INTO project_repositories (id, name, path, source, project, "
        "required_capabilities, enabled, poll_interval_seconds, metadata, created_at, updated_at) "
        "VALUES ('repo_demo', 'demo', '/tmp/demo', 'git@example.invalid:demo/repo.git', "
        "'demo', '[]', 1, 60, '{\"build_system\":\"make\"}', 'created', 'updated')"
    )
    cp.create_role(
        "builder",
        "Builder",
        "Build-system migration role.",
        "Convert the build while preserving behavior.",
        "ic",
    )
    cp.create_workflow(
        "build-system.make-to-bazel",
        "Make to Bazel",
        "Convert a Make build to Bazel.",
        "repository-change",
        {
            "nodes": [
                {
                    "node_key": "convert",
                    "node_type": "task",
                    "role_required": "builder",
                    "instructions": "Author Bazel targets and preserve the existing test contract.",
                    "required_capabilities": ["coding"],
                }
            ],
            "edges": [
                {
                    "from_node_key": "",
                    "to_node_key": "convert",
                    "condition": "success",
                }
            ],
        },
        "operator",
    )
    cp.managed_work_plans.base_resolver = _BaseResolver()
    cp.work_packages.repository_verifier = _Attestor()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", capabilities=["coding"])

    proposed = cp.propose_directive(
        {
            "schema": "mac.directive.v1",
            "name": "build.bazel-first",
            "description": "Convert Make repositories through the registered workflow.",
            "scope": "fleet",
            "when": {
                "eq": [
                    {"fact": "repository.metadata.build_system"},
                    {"literal": "make"},
                ]
            },
            "set": {"build.bazel.required": True},
            "macro": {
                "workflow": "build-system.make-to-bazel",
                "version": 1,
                "inputs": {"repository_id": {"fact": "repository.id"}},
                "effects": {
                    "exclusive": [
                        {"template": "repository:${repository.id}:build-system"}
                    ]
                },
            },
        },
        actor="operator",
    )
    version = proposed["versions"][0]
    checked = cp.check_directive(proposed["id"], actor="operator")
    assert checked["status"] == "pass"
    cp.approve_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        check_id=checked["id"],
        actor="operator",
    )
    activation = cp.activate_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )
    cp.acknowledge_directive_activation(
        agent.id, activation["id"], digest=version["digest"]
    )

    impact = cp.directive_impact(proposed["id"])
    assert impact["macro_instances"][0]["state"] == "held", impact["macro_instances"][0]["detail"]
    package_id = impact["macro_instances"][0]["work_package_id"]
    package = cp.work_packages.get(package_id).to_dict()
    assert package["state"] == "admitted"
    links = cp.store.query_all(
        "SELECT t.metadata FROM work_package_task_links l JOIN tasks t ON t.id = l.task_id "
        "WHERE l.package_id = ?",
        (package_id,),
    )
    assert len(links) == 3
    for row in links:
        snapshot = json_loads(row["metadata"], {})["directive_snapshot"]
        assert snapshot["schema"] == "mac.directive.snapshot.v1"
        assert snapshot["set"]["build.bazel.required"] is True
