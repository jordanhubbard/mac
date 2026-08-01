from __future__ import annotations

from dataclasses import dataclass

import pytest

from mac.directive_models import parse_directive_document
from mac.directive_service import (
    SYSTEM_DIRECTIVE_ID,
    SYSTEM_DIRECTIVE_NAME,
    DirectiveService,
)
from mac.models import NotFoundError, ValidationError, json_dumps
from mac.store import Store
from mac.test_support import ephemeral_store


@dataclass
class _Workflow:
    enabled: bool = True


def _register_project(store: Store, *, build_system: str = "make") -> None:
    store.execute(
        "INSERT INTO projects (id, name, description, metadata, status, created_at, updated_at) "
        "VALUES (?, ?, '', '{}', 'active', 'created', 'updated')",
        ("project_demo", "demo"),
    )
    store.execute(
        "INSERT INTO project_repositories (id, name, path, source, project, "
        "required_capabilities, enabled, poll_interval_seconds, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '[]', 1, 60, ?, 'created', 'updated')",
        (
            "repo_demo",
            "demo",
            "/tmp/demo",
            "git@example.invalid:demo/repo.git",
            "demo",
            '{"build_system":"%s"}' % build_system,
        ),
    )


def _register_agent(store: Store, agent_id: str) -> None:
    store.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, '{}', '{}', 1, 'created', 'updated', 'seen')",
        ("machine_" + agent_id, agent_id),
    )
    store.execute(
        "INSERT INTO agents (id, machine_id, name, capabilities, resources, status, "
        "health_status, created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, ?, '[]', '{}', 'idle', 'healthy', 'created', 'updated', 'seen')",
        (agent_id, "machine_" + agent_id, agent_id),
    )


def _document(*, policy_value=True, macro=False):
    raw = {
        "schema": "mac.directive.v1",
        "name": "build.bazel-first",
        "description": "Require Bazel for repositories currently using Make.",
        "scope": "fleet",
        "when": {
            "eq": [
                {"fact": "repository.metadata.build_system"},
                {"literal": "make"},
            ]
        },
        "variables": {
            "primary_target": {
                "type": "string",
                "binding": "build.primary_target",
                "required": True,
            }
        },
        "set": {"build.bazel.required": policy_value},
    }
    if macro:
        raw["macro"] = {
            "workflow": "build-system.make-to-bazel",
            "version": 1,
            "inputs": {"target": {"template": "${primary_target}"}},
            "effects": {
                "exclusive": [{"template": "repository:${repository.id}:build-system"}]
            },
        }
    return raw


@pytest.fixture
def service():
    store = ephemeral_store()
    _register_project(store)
    _register_agent(store, "agent_one")
    _register_agent(store, "agent_two")
    expansions = []

    def expand(activation, repository, macro, context):
        expansions.append((activation, repository, macro, context))
        return {
            "work_package_id": "wp_held",
            "task_ids": ["task_mutate", "task_assemble", "task_certify"],
            "held": True,
        }

    directives = DirectiveService(
        store,
        enabled=True,
        workflow_resolver=lambda _slug, _version: _Workflow(),
        macro_expander=expand,
    )
    yield store, directives, expansions
    store.close()


def _approve(service: DirectiveService, document):
    proposed = service.propose(document, actor="operator")
    service.set_binding(
        target_type="fleet",
        target_id="fleet",
        key="build.primary_target",
        value="//app:all",
        actor="operator",
    )
    check = service.check(proposed["id"], actor="operator")
    assert check["status"] == "pass"
    version = proposed["versions"][0]
    service.approve(
        proposed["id"],
        version=version["version"],
        directive_digest=version["digest"],
        check_id=check["id"],
        actor="operator",
    )
    return proposed, version, check


def test_system_safety_baseline_self_versions_when_builtin_rules_change() -> None:
    store = ephemeral_store()
    previous = parse_directive_document(
        {
            "schema": "mac.directive.v1",
            "name": SYSTEM_DIRECTIVE_NAME,
            "description": "Previous built-in executor safety constraints.",
            "scope": "fleet",
            "set": {"verification.tests_required": True},
        }
    )
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO fleet_directives (id, name, description, scope, current_version, state, "
            "reserved, created_by, updated_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'fleet', 1, 'active', 1, 'system', 'system', 'created', 'updated')",
            (SYSTEM_DIRECTIVE_ID, SYSTEM_DIRECTIVE_NAME, previous.description),
        )
        conn.execute(
            "INSERT INTO fleet_directive_versions "
            "(id, directive_id, version, document, digest, created_by, created_at) "
            "VALUES ('version_old', ?, 1, ?, ?, 'system', 'created')",
            (SYSTEM_DIRECTIVE_ID, json_dumps(previous.to_dict()), previous.digest),
        )

    directives = DirectiveService(store, enabled=True)
    directive = directives.get(SYSTEM_DIRECTIVE_ID)
    assert directive["current_version"] == 2
    assert [item["version"] for item in directive["versions"]] == [2, 1]
    assert directive["versions"][0]["document"]["set"] == {
        "executor.host_package_install_allowed": False,
        "publication.owner": "hub",
        "review.required": True,
        "secrets.exposure_allowed": False,
        "verification.codegraph_required_for_code_change": True,
        "verification.tests_required": True,
    }
    store.close()


def test_proposal_is_idempotent_and_changed_document_creates_immutable_version(service) -> None:
    _store, directives, _expansions = service
    first = directives.propose(_document(), actor="operator")
    identical = directives.propose(_document(), actor="second-operator")

    assert identical["id"] == first["id"]
    assert identical["current_version"] == 1
    assert len(identical["versions"]) == 1

    changed = _document()
    changed["description"] = "Require Bazel and retain the prior version for audit."
    second = directives.propose(changed, actor="second-operator")

    assert second["id"] == first["id"]
    assert second["current_version"] == 2
    assert [item["version"] for item in second["versions"]] == [2, 1]
    assert second["versions"][0]["digest"] != second["versions"][1]["digest"]


def test_disabled_service_is_fail_closed_for_mutation_and_has_no_policy_gate() -> None:
    store = ephemeral_store()
    directives = DirectiveService(store, enabled=False)

    snapshot = directives.effective_snapshot()
    assert snapshot["enabled"] is False
    assert directives.pending_activations("agent") == []
    assert directives.agent_policy_ready("agent") is True
    with pytest.raises(ValidationError, match="directives are disabled"):
        directives.propose(_document(), actor="operator")

    store.close()


def test_directive_listing_lookup_and_reserved_name_boundaries(service) -> None:
    _store, directives, _expansions = service
    proposed = directives.propose(_document(), actor="operator")

    assert {item["id"] for item in directives.list()} == {
        SYSTEM_DIRECTIVE_ID,
        proposed["id"],
    }
    assert [item["id"] for item in directives.list(state="proposed")] == [
        proposed["id"]
    ]
    with pytest.raises(NotFoundError, match="directive not found"):
        directives.get("directive_missing")

    reserved = _document()
    reserved["name"] = SYSTEM_DIRECTIVE_NAME
    with pytest.raises(ValidationError, match="reserved"):
        directives.propose(reserved, actor="operator")


def test_binding_history_filters_and_credential_names_fail_closed(service) -> None:
    _store, directives, _expansions = service
    first = directives.set_binding(
        target_type="repository",
        target_id="repo_demo",
        key="build.primary_target",
        value="//app:first",
        actor="operator",
    )
    second = directives.set_binding(
        target_type="repository",
        target_id="repo_demo",
        key="build.primary_target",
        value="//app:second",
        actor="operator",
    )

    active = directives.list_bindings(
        target_type="repository", target_id="repo_demo"
    )
    inactive = directives.list_bindings(
        target_type="repository", target_id="repo_demo", active=False
    )
    assert [item["id"] for item in active] == [second["id"]]
    assert [item["id"] for item in inactive] == [first["id"]]
    with pytest.raises(ValidationError, match="credential material"):
        directives.set_binding(
            target_type="fleet",
            target_id="fleet",
            key="service.api_key",
            value="redacted",
            actor="operator",
        )


def test_waiver_lifecycle_is_auditable_and_revoke_is_idempotent(service) -> None:
    _store, directives, _expansions = service
    proposed = directives.propose(_document(), actor="operator")
    with pytest.raises(ValidationError, match="fleet-wide waivers"):
        directives.create_waiver(
            proposed["id"],
            version=1,
            target_type="fleet",
            target_id="fleet",
            reason="too broad",
            actor="operator",
        )

    waiver = directives.create_waiver(
        proposed["id"],
        version=1,
        target_type="project",
        target_id="demo",
        reason="temporary migration window",
        actor="operator",
    )
    assert [item["id"] for item in directives.list_waivers()] == [waiver["id"]]
    assert [item["id"] for item in directives.list_waivers(proposed["id"])] == [
        waiver["id"]
    ]

    revoked = directives.revoke_waiver(
        waiver["id"], actor="operator", reason="migration complete"
    )
    assert revoked["revoked_by"] == "operator"
    assert directives.revoke_waiver(
        waiver["id"], actor="ignored", reason="already revoked"
    ) == revoked
    with pytest.raises(NotFoundError, match="waiver not found"):
        directives.revoke_waiver(
            "waiver_missing", actor="operator", reason="not present"
        )


def test_exact_version_activation_waits_for_all_acks_and_expands_held_macro(service) -> None:
    _store, directives, expansions = service
    proposed, version, _check = _approve(directives, _document(macro=True))

    activation = directives.activate(
        proposed["id"],
        version=version["version"],
        directive_digest=version["digest"],
        actor="operator",
    )
    assert activation["state"] == "distributing"
    assert set(activation["cohort"]) == {"agent_one", "agent_two"}
    assert directives.agent_policy_ready("agent_one") is False
    assert expansions == []

    first = directives.acknowledge(
        activation["id"], agent_id="agent_one", digest=version["digest"]
    )
    assert first["state"] == "distributing"
    assert expansions == []
    final = directives.acknowledge(
        activation["id"], agent_id="agent_two", digest=version["digest"]
    )
    assert final["state"] == "active"
    assert final["finalized"] is True
    assert len(expansions) == 1
    assert expansions[0][2]["inputs"] == {"target": "//app:all"}
    impact = directives.impact(proposed["id"])
    assert impact["macro_instances"][0]["state"] == "held"
    assert impact["macro_instances"][0]["work_package_id"] == "wp_held"


def test_context_change_after_approval_requires_recheck_and_reapproval(service) -> None:
    _store, directives, _expansions = service
    proposed, version, _check = _approve(directives, _document())
    directives.set_binding(
        target_type="repository",
        target_id="repo_demo",
        key="build.primary_target",
        value="//changed:target",
        actor="operator",
    )
    with pytest.raises(ValidationError, match="context changed after approval"):
        directives.activate(
            proposed["id"],
            version=version["version"],
            directive_digest=version["digest"],
            actor="operator",
        )


def test_new_agent_must_ack_active_epoch_before_dispatch(service) -> None:
    store, directives, _expansions = service
    proposed, version, _check = _approve(directives, _document())
    activation = directives.activate(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )
    for agent_id in ("agent_one", "agent_two"):
        directives.acknowledge(
            activation["id"], agent_id=agent_id, digest=version["digest"]
        )
    _register_agent(store, "agent_new")

    assert directives.agent_policy_ready("agent_new") is False
    pending = directives.pending_activations("agent_new")
    assert [item["activation_id"] for item in pending] == [activation["id"]]
    directives.acknowledge(
        activation["id"], agent_id="agent_new", digest=version["digest"]
    )
    assert directives.agent_policy_ready("agent_new") is True
    deactivated = directives.deactivate(
        proposed["id"], actor="operator", reason="replace with version 2"
    )
    assert deactivated["deactivated_by"] == "operator"
    assert deactivated["deactivation_reason"] == "replace with version 2"


def test_required_binding_failure_and_exact_version_waiver(service) -> None:
    _store, directives, _expansions = service
    proposed = directives.propose(_document(), actor="operator")
    blocked = directives.check(proposed["id"], actor="operator")
    assert blocked["status"] == "blocked"
    assert blocked["blockers"][0]["code"] == "binding_resolution_failed"

    waiver = directives.create_waiver(
        proposed["id"],
        version=1,
        target_type="repository",
        target_id="repo_demo",
        reason="Repository is frozen pending archival.",
        actor="operator",
    )
    assert waiver["directive_version"] == 1
    passing = directives.check(proposed["id"], actor="operator")
    assert passing["status"] == "pass"
    assert passing["evaluations"][0]["waived"] is True


def test_conflicting_active_policy_is_blocked(service) -> None:
    _store, directives, _expansions = service
    proposed, version, _check = _approve(directives, _document(policy_value=True))
    activation = directives.activate(
        proposed["id"], version=1, directive_digest=version["digest"], actor="operator"
    )
    for agent_id in ("agent_one", "agent_two"):
        directives.acknowledge(
            activation["id"], agent_id=agent_id, digest=version["digest"]
        )

    conflict = _document(policy_value=False)
    conflict["name"] = "build.bazel-disabled"
    second = directives.propose(conflict, actor="operator")
    checked = directives.check(second["id"], actor="operator")
    assert checked["status"] == "blocked"
    assert any(item["code"] == "policy_conflict" for item in checked["blockers"])


def test_reserved_system_constraints_are_effective_and_cannot_be_waived(service) -> None:
    _store, directives, _expansions = service
    snapshot = directives.effective_snapshot(repository_id="repo_demo")
    assert snapshot["set"]["verification.tests_required"] is True
    assert snapshot["set"]["publication.owner"] == "hub"
    with pytest.raises(ValidationError, match="cannot be waived"):
        directives.create_waiver(
            "system.executor-safety",
            version=1,
            target_type="repository",
            target_id="repo_demo",
            reason="unsafe",
            actor="operator",
        )


def test_macro_admission_failure_blocks_activation_instead_of_partial_policy(service) -> None:
    _store, directives, _expansions = service

    def fail_expansion(_activation, _repository, _macro, _context):
        raise ValidationError("admission unavailable")

    directives.macro_expander = fail_expansion
    proposed, version, _check = _approve(directives, _document(macro=True))
    activation = directives.activate(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )
    directives.acknowledge(
        activation["id"], agent_id="agent_one", digest=version["digest"]
    )
    final = directives.acknowledge(
        activation["id"], agent_id="agent_two", digest=version["digest"]
    )

    assert final["state"] == "blocked"
    assert directives.get(proposed["id"])["state"] == "approved"
    snapshot = directives.effective_snapshot(repository_id="repo_demo")
    assert "build.bazel.required" not in snapshot["set"]


def test_unproven_conditional_overlap_blocks_conflicting_macro_effects(service) -> None:
    _store, directives, _expansions = service
    proposed, version, _check = _approve(directives, _document(macro=True))
    activation = directives.activate(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )
    for agent_id in ("agent_one", "agent_two"):
        directives.acknowledge(
            activation["id"], agent_id=agent_id, digest=version["digest"]
        )

    candidate = _document(macro=True)
    candidate["name"] = "build.bazel-second"
    candidate["when"] = {
        "starts_with": [
            {"fact": "repository.name"},
            {"literal": "d"},
        ]
    }
    second = directives.propose(candidate, actor="operator")
    checked = directives.check(second["id"], actor="operator")

    assert checked["status"] == "blocked"
    assert any(
        item["code"] == "macro_effect_overlap_unproven"
        for item in checked["blockers"]
    )
