from __future__ import annotations

import copy

import pytest

from mac.models import (
    ValidationError,
    WorkPackage,
    WorkPackageBatchState,
    WorkPackageCertificationState,
    WorkPackageEpochState,
    WorkPackageState,
)
from mac.work_package_models import (
    WORK_PACKAGE_PLAN_SCHEMA,
    WorkPackageEffects,
    compile_work_package_plan,
    validate_supported_work_package_topology,
    work_package_effect_conflicts,
)


def _plan() -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_test",
        "goal": "Ship the coordinated change",
        "project": "mac",
        "repository_id": "projectrepo_mac",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "max_in_flight": 3,
        "integration": {"strategy": "batch", "required": True},
        "metadata": {"request": {"id": "req_1"}},
        "nodes": [
            {
                "node_id": "verify",
                "title": "Verify assembled candidate",
                "node_type": "verify",
                "depends_on": ["assemble"],
                "required_capabilities": ["tests"],
                "expected_outputs": [
                    {"name": "report", "kind": "test", "required": True}
                ],
                "verification": {"profile": "certification-default"},
            },
            {
                "node_id": "frontend",
                "title": "Build UI",
                "depends_on": ["contract"],
                "required_capabilities": ["typescript", "ui", "typescript"],
                "effects": {
                    "writes": ["src/ui", "src/ui"],
                    "reads": ["contracts/api"],
                },
                "expected_outputs": ["ui-patch"],
                "verification": {
                    "profile": "repository-default",
                    "command": "npm test",
                },
                "scope_confidence": 0.8,
            },
            {
                "node_id": "contract",
                "title": "Analyze existing contract",
                "node_type": "analysis",
                "expected_outputs": ["api-contract"],
                "verification": {
                    "profile": "analysis-default",
                    "required_evidence": ["schema-validation"],
                },
            },
            {
                "node_id": "backend",
                "title": "Build API",
                "depends_on": ["contract"],
                "external_dependencies": ["task_existing"],
                "effects": {
                    "external_effects": ["github:repo"],
                    "external_contract": {
                        "idempotency_key": "backend-publish",
                        "exclusive": True,
                    },
                    "writes": ["src/api"],
                    "reads": ["contracts/api"],
                },
                "expected_outputs": ["api-patch"],
                "verification": {
                    "profile": "repository-default",
                    "command": "pytest tests/api",
                },
                "estimates": {
                    "duration_seconds": 1800,
                    "cost_units": 1,
                    "confidence": "high",
                },
            },
            {
                "node_id": "assemble",
                "title": "Assemble candidate",
                "kind": "integration",
                "depends_on": ["backend", "frontend"],
                "inputs": ["api-patch", "ui-patch"],
                "expected_outputs": ["candidate-tree"],
                "verification": {"profile": "integration-default"},
                "estimates": {
                    "duration_seconds": 300,
                    "cost_units": 0.5,
                    "confidence": "medium",
                },
                "rework": {"max_cycles": 2},
            },
        ],
    }


def test_persisted_work_package_model_and_state_tokens() -> None:
    package = WorkPackage(
        id="wp_1",
        tenant_id=None,
        project="mac",
        repository_id="repo_1",
        root_task_id=None,
        goal="coordinate work",
        state=WorkPackageState.ACTIVE.value,
        current_plan_version=2,
        current_epoch=3,
        metadata={"owner": "control-plane"},
        created_by="human",
        created_at="created",
        updated_at="updated",
        completed_at=None,
    )
    assert package.to_dict()["current_epoch"] == 3
    assert {item.value for item in WorkPackageState} == {
        "draft",
        "admitted",
        "active",
        "paused",
        "replanning",
        "completed",
        "failed",
        "cancelled",
    }
    assert {item.value for item in WorkPackageEpochState} == {
        "staged",
        "active",
        "superseded",
        "completed",
        "cancelled",
    }
    assert "certified" in {item.value for item in WorkPackageBatchState}
    assert {item.value for item in WorkPackageCertificationState} == {
        "passed",
        "failed",
        "invalidated",
        "published",
    }


def test_compile_is_canonical_and_independent_of_input_order() -> None:
    first_input = _plan()
    first = compile_work_package_plan(first_input)

    second_input = _plan()
    second_input["nodes"] = list(reversed(second_input["nodes"]))
    frontend = next(
        node for node in second_input["nodes"] if node["node_id"] == "frontend"
    )
    frontend["required_capabilities"] = list(
        reversed(frontend["required_capabilities"])
    )
    verify = next(node for node in second_input["nodes"] if node["node_id"] == "verify")
    verify["depends_on"] = list(reversed(verify["depends_on"]))
    second = compile_work_package_plan(second_input)

    assert first.plan_digest.startswith("sha256:")
    assert first.plan_digest == second.plan_digest
    assert first.definition == second.definition
    assert first.topological_order == (
        "contract",
        "backend",
        "frontend",
        "assemble",
        "verify",
    )
    assert [node.node_key for node in first.task_specs] == list(first.topological_order)


def test_compile_normalizes_materialization_contract() -> None:
    compiled = compile_work_package_plan(_plan())
    frontend = next(node for node in compiled.task_specs if node.node_key == "frontend")
    backend = next(node for node in compiled.task_specs if node.node_key == "backend")
    assemble = next(node for node in compiled.task_specs if node.node_key == "assemble")
    certify = next(node for node in compiled.task_specs if node.node_key == "verify")

    assert frontend.required_capabilities == ("typescript", "ui", "work_package_v1")
    assert "work_package_integrator_v1" in assemble.required_capabilities
    assert "work_package_certifier_v1" in certify.required_capabilities
    assert frontend.effects.writes == ("src/ui",)
    assert frontend.expected_outputs == (
        {"kind": "artifact", "metadata": {}, "name": "ui-patch", "required": True},
    )
    assert backend.external_dependencies == (
        {
            "accepted_evidence_digest": None,
            "carry_forward_eligible": False,
            "contract_digest": None,
            "lineage_status": "unresolved",
            "output_digest": None,
            "task_id": "task_existing",
        },
    )
    assert backend.input_lineage_status == "unresolved"
    assert backend.carry_forward_eligible is False
    assert backend.effects.external == ("github:repo",)
    assert backend.effects.external_contract == {
        "exclusive": True,
        "idempotency_key": "backend-publish",
    }
    assert backend.verification == {
        "command_authority": "advisory",
        "commands": ["pytest tests/api"],
        "metadata": {},
        "policy_resolution": "required",
        "profile": "repository-default",
        "required_evidence": [],
        "timeout_seconds": 3600,
    }
    assert backend.node_type == "mutation"
    assert backend.effects_digest.startswith("sha256:")
    assert backend.contract_digest.startswith("sha256:")
    assert backend.input_digest.startswith("sha256:")
    assert compiled.definition["integration"]["worker_completion"] == (
        "candidate_submission"
    )
    assert compiled.definition["mutation_wip"]["transfer_on"] == (
        "candidate_submission"
    )
    assert compiled.levels["contract"] == 0
    assert compiled.levels["assemble"] == 2
    assert (
        compiled.critical_path_rank["contract"]
        > compiled.critical_path_rank["assemble"]
    )
    assert compiled.integration_groups == (
        {
            "capacity_scope": "integration:assemble",
            "integration_node_key": "assemble",
            "member_node_keys": ["backend", "frontend"],
        },
    )
    assert set(compiled.materialization_map) == set(compiled.topological_order)
    assert compiled.definition["resource_namespace"] == {
        "case_sensitive": False,
        "conflict_policy": "conservative",
        "status": "unresolved",
        "symlink_resolution": "unresolved",
        "unicode_normalization": "NFC",
    }
    assert "repo:*" in frontend.effects.exclusive


def test_compile_does_not_mutate_input_or_leak_nested_state() -> None:
    raw = _plan()
    before = copy.deepcopy(raw)
    compiled = compile_work_package_plan(raw)
    exported = compiled.to_dict()
    exported["definition"]["metadata"]["request"]["id"] = "mutated"
    exported["task_specs"][0]["metadata"]["new"] = True

    assert raw == before
    assert compiled.definition["metadata"]["request"]["id"] == "req_1"
    assert "new" not in compiled.task_specs[0].metadata


def test_semantic_change_changes_digest() -> None:
    first = compile_work_package_plan(_plan())
    changed = _plan()
    changed["nodes"][1]["effects"]["writes"] = ["src/new-ui"]
    second = compile_work_package_plan(changed)
    assert first.plan_digest != second.plan_digest


def test_node_contract_and_input_digests_support_safe_carry_forward() -> None:
    first = compile_work_package_plan(_plan())
    renamed = _plan()
    contract = next(node for node in renamed["nodes"] if node["node_id"] == "contract")
    contract["node_id"] = "contract_v2"
    for node in renamed["nodes"]:
        if "depends_on" in node:
            node["depends_on"] = [
                "contract_v2" if item == "contract" else item
                for item in node["depends_on"]
            ]
    second = compile_work_package_plan(renamed)

    first_contract = next(
        node for node in first.task_specs if node.node_key == "contract"
    )
    second_contract = next(
        node for node in second.task_specs if node.node_key == "contract_v2"
    )
    assert first_contract.contract_digest == second_contract.contract_digest
    assert (
        next(
            node for node in first.task_specs if node.node_key == "backend"
        ).input_digest
        == next(
            node for node in second.task_specs if node.node_key == "backend"
        ).input_digest
    )
    assert first.plan_digest != second.plan_digest


def test_input_lineage_is_transitive_across_three_hops() -> None:
    first = compile_work_package_plan(_plan())
    changed = _plan()
    contract = next(node for node in changed["nodes"] if node["node_id"] == "contract")
    contract["description"] = "Semantically different contract"
    second = compile_work_package_plan(changed)
    for node_key in ("backend", "assemble", "verify"):
        assert (
            next(
                node for node in first.task_specs if node.node_key == node_key
            ).input_digest
            != next(
                node for node in second.task_specs if node.node_key == node_key
            ).input_digest
        )


def test_external_lineage_is_unresolved_until_all_digests_are_pinned() -> None:
    unresolved = compile_work_package_plan(_plan())
    for node_key in ("backend", "assemble", "verify"):
        node = next(node for node in unresolved.task_specs if node.node_key == node_key)
        assert node.input_lineage_status == "unresolved"
        assert node.carry_forward_eligible is False

    resolved_plan = _plan()
    backend = next(
        node for node in resolved_plan["nodes"] if node["node_id"] == "backend"
    )
    backend["external_dependencies"] = [
        {
            "task_id": "task_existing",
            "accepted_evidence_digest": "sha256:" + "1" * 64,
            "output_digest": "sha256:" + "2" * 64,
            "contract_digest": "sha256:" + "3" * 64,
        }
    ]
    resolved = compile_work_package_plan(resolved_plan)
    for node_key in ("backend", "assemble", "verify"):
        node = next(node for node in resolved.task_specs if node.node_key == node_key)
        assert node.input_lineage_status == "resolved"
        assert node.carry_forward_eligible is True

    partial = _plan()
    backend = next(node for node in partial["nodes"] if node["node_id"] == "backend")
    backend["external_dependencies"] = [
        {
            "task_id": "task_existing",
            "accepted_evidence_digest": "sha256:" + "1" * 64,
        }
    ]
    with pytest.raises(ValidationError, match="digests together"):
        compile_work_package_plan(partial)


def test_resource_namespace_is_conservative_until_repository_semantics_resolve() -> (
    None
):
    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["effects"]["writes"] = ["Src/Caf\N{LATIN SMALL LETTER E WITH ACUTE}"]
    conservative = compile_work_package_plan(raw)
    frontend_spec = next(
        node for node in conservative.task_specs if node.node_key == "frontend"
    )
    assert frontend_spec.effects.writes == (
        "src/caf\N{LATIN SMALL LETTER E WITH ACUTE}",
    )
    assert "repo:*" in frontend_spec.effects.exclusive

    raw["resource_namespace"] = {
        "case_sensitive": True,
        "unicode_normalization": "NFC",
        "symlink_resolution": "resolved",
    }
    resolved = compile_work_package_plan(raw)
    frontend_spec = next(
        node for node in resolved.task_specs if node.node_key == "frontend"
    )
    assert frontend_spec.effects.writes == (
        "Src/Caf\N{LATIN SMALL LETTER E WITH ACUTE}",
    )
    assert "repo:*" not in frontend_spec.effects.exclusive
    assert resolved.definition["resource_namespace"]["status"] == "resolved"


def test_materialization_identity_is_package_scoped() -> None:
    first = compile_work_package_plan(_plan())
    other_package = _plan()
    other_package["package_id"] = "wp_other"
    second = compile_work_package_plan(other_package)
    assert (
        first.materialization_map["backend"]["task_id"]
        != (second.materialization_map["backend"]["task_id"])
    )

    next_generation = _plan()
    next_generation["plan_generation"] = 2
    next_generation["planning_base_sha"] = "b" * 40
    third = compile_work_package_plan(next_generation)
    assert third.materialization_map["backend"]["node_generation"] == 2
    assert (
        first.materialization_map["backend"]["task_id"]
        != (third.materialization_map["backend"]["task_id"])
    )


def test_compiler_rejects_undeclared_contract_surface() -> None:
    raw = _plan()
    raw["surprise"] = True
    with pytest.raises(ValidationError, match="unknown fields"):
        compile_work_package_plan(raw)

    raw = _plan()
    raw["nodes"][0]["surprise"] = True
    with pytest.raises(ValidationError, match="unknown fields"):
        compile_work_package_plan(raw)


def test_effect_paths_are_canonical_and_secret_free() -> None:
    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["effects"]["writes"] = ["src//ui/./component.ts"]
    compiled = compile_work_package_plan(raw)
    assert next(
        node for node in compiled.task_specs if node.node_key == "frontend"
    ).effects.writes == ("src/ui/component.ts",)

    for unsafe in (
        "/tmp/output",
        "src/../secrets",
        "https://token@github.com/org/repo",
        "https://example.test/repo?client_secret=redacted",
        "https://example.test/repo?X-Amz-Signature=redacted",
        "https://example.test/repo#token=redacted",
    ):
        raw = _plan()
        frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
        frontend["effects"]["writes"] = [unsafe]
        with pytest.raises(ValidationError):
            compile_work_package_plan(raw)


def test_external_effects_require_idempotent_exclusive_contract() -> None:
    raw = _plan()
    backend = next(node for node in raw["nodes"] if node["node_id"] == "backend")
    backend["effects"].pop("external_contract")
    with pytest.raises(ValidationError, match="idempotency_key"):
        compile_work_package_plan(raw)


def test_mutation_contract_and_verification_are_bounded() -> None:
    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["expected_outputs"] = []
    with pytest.raises(ValidationError, match="expected output"):
        compile_work_package_plan(raw)

    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["verification"] = {"command": "pytest\nrm -rf output"}
    with pytest.raises(ValidationError, match="single-line"):
        compile_work_package_plan(raw)

    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["verification"] = {
        "profile": "repository-default",
        "command": "true",
    }
    with pytest.raises(ValidationError, match="no-op"):
        compile_work_package_plan(raw)

    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["verification"] = {"profile": "analysis-default"}
    with pytest.raises(ValidationError, match="mutation nodes"):
        compile_work_package_plan(raw)


@pytest.mark.parametrize(
    "command",
    [
        "curl https://example.test/check",
        "git push origin main",
        "pytest $(steal-secret)",
        "pytest > /tmp/forged-report",
        "pytest; rm -rf output",
    ],
)
def test_planner_verification_commands_are_advisory_and_non_authoritative(
    command: str,
) -> None:
    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["verification"] = {
        "profile": "repository-default",
        "command": command,
    }
    with pytest.raises(ValidationError, match="advisory only"):
        compile_work_package_plan(raw)


def test_low_confidence_mutation_and_fan_in_fail_closed() -> None:
    raw = _plan()
    frontend = next(node for node in raw["nodes"] if node["node_id"] == "frontend")
    frontend["scope_confidence"] = 0.1
    with pytest.raises(ValidationError, match="exclusive effect"):
        compile_work_package_plan(raw)

    frontend["effects"]["exclusive"] = ["*"]
    compiled = compile_work_package_plan(raw)
    assert (
        "*"
        in next(
            node for node in compiled.task_specs if node.node_key == "frontend"
        ).effects.exclusive
    )

    raw = _plan()
    raw["mutation_wip"] = {"max_tokens": 1, "fan_in_reservation": False}
    with pytest.raises(ValidationError, match="fan_in_reservation"):
        compile_work_package_plan(raw)


def test_multiple_mutation_leaves_require_explicit_integration_node() -> None:
    raw = _plan()
    raw["nodes"] = [node for node in raw["nodes"] if node["node_id"] != "assemble"]
    verify = next(node for node in raw["nodes"] if node["node_id"] == "verify")
    verify["depends_on"] = ["backend", "frontend"]
    with pytest.raises(ValidationError, match="integration fan-in"):
        compile_work_package_plan(raw)


def test_mutation_cannot_depend_on_another_mutation_candidate() -> None:
    raw = _plan()
    backend = next(node for node in raw["nodes"] if node["node_id"] == "backend")
    backend["depends_on"] = ["frontend"]

    with pytest.raises(
        ValidationError,
        match=r"flat mutation wave.*backend.*frontend \(mutation\)",
    ):
        compile_work_package_plan(raw)


def test_mutation_cannot_run_after_an_integration_candidate() -> None:
    raw = _plan()
    raw["nodes"].append(
        {
            "node_id": "followup",
            "title": "Mutate the assembled candidate",
            "node_type": "mutation",
            "depends_on": ["assemble"],
            "effects": {"writes": ["src/followup"]},
            "expected_outputs": ["followup-patch"],
            "verification": {"profile": "repository-default"},
            "scope_confidence": 0.9,
        }
    )
    verify = next(node for node in raw["nodes"] if node["node_id"] == "verify")
    verify["depends_on"] = ["followup"]

    with pytest.raises(
        ValidationError,
        match=r"flat mutation wave.*followup.*assemble \(integration\)",
    ):
        compile_work_package_plan(raw)


def test_persisted_plan_revalidation_fences_transitive_mutation_lineage() -> None:
    definition = compile_work_package_plan(_plan()).definition
    nodes = {node["node_key"]: node for node in definition["nodes"]}
    nodes["backend"]["depends_on"] = ["frontend"]

    with pytest.raises(
        ValidationError,
        match=r"flat mutation wave.*backend.*frontend \(mutation\)",
    ):
        validate_supported_work_package_topology(definition)


def test_nested_integration_groups_consume_immediate_candidate_frontier() -> None:
    def mutation(key: str) -> dict:
        return {
            "node_id": key,
            "title": key,
            "effects": {"writes": ["src/%s" % key.lower()]},
            "expected_outputs": ["%s-patch" % key.lower()],
            "verification": {"profile": "repository-default"},
            "scope_confidence": 0.9,
        }

    raw = {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_nested",
        "goal": "assemble a hierarchy",
        "repository_id": "repo_nested",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "nodes": [
            mutation("A"),
            mutation("B"),
            mutation("C"),
            {
                "node_id": "I1",
                "title": "first fan-in",
                "kind": "integration",
                "depends_on": ["A", "B"],
                "expected_outputs": ["i1-candidate"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_id": "I2",
                "title": "second fan-in",
                "kind": "integration",
                "depends_on": ["I1", "C"],
                "expected_outputs": ["i2-candidate"],
                "verification": {"profile": "integration-default"},
            },
        ],
    }
    compiled = compile_work_package_plan(raw)
    groups = {
        group["integration_node_key"]: group["member_node_keys"]
        for group in compiled.integration_groups
    }
    assert groups == {"I1": ["A", "B"], "I2": ["C", "I1"]}
    assert (
        sum(key in group for group in groups.values() for key in ("A", "B", "C")) == 3
    )


@pytest.mark.parametrize(
    "bad_ref",
    [
        "refs/heads/bad:name",
        "refs/heads/bad~name",
        "refs/heads/bad^name",
        "refs/heads/bad?name",
        "refs/heads/bad*name",
        "refs/heads/bad[name",
        "refs/heads/bad@{name",
    ],
)
def test_git_refs_are_strictly_validated(bad_ref: str) -> None:
    raw = _plan()
    raw["planning_base_ref"] = bad_ref
    with pytest.raises(ValidationError, match="safe repository ref"):
        compile_work_package_plan(raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: plan.update(goal=""), "plan.goal"),
        (lambda plan: plan.update(schema="mac.work_package.plan.v2"), "plan.schema"),
        (lambda plan: plan.update(nodes=[]), "non-empty list"),
        (
            lambda plan: plan["nodes"].append(copy.deepcopy(plan["nodes"][0])),
            "duplicate",
        ),
        (
            lambda plan: plan["nodes"][0].update(depends_on=["missing"]),
            "does not reference",
        ),
        (
            lambda plan: plan["nodes"][2].update(depends_on=["contract"]),
            "cannot depend on itself",
        ),
        (
            lambda plan: plan["nodes"][2].update(depends_on=["verify"]),
            "dependency cycle",
        ),
        (lambda plan: plan["nodes"][0].update(node_type="unknown"), "node_type"),
        (lambda plan: plan["nodes"][0].update(max_attempts=0), "max_attempts"),
        (
            lambda plan: plan["nodes"][0].update(scope_confidence=1.1),
            "estimates.confidence",
        ),
    ],
)
def test_compile_rejects_invalid_plans(mutate, message: str) -> None:
    raw = _plan()
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        compile_work_package_plan(raw)


def test_expected_outputs_are_unique_and_strict() -> None:
    raw = _plan()
    raw["nodes"][0]["expected_outputs"] = ["report", {"name": "report"}]
    with pytest.raises(ValidationError, match="duplicate expected output"):
        compile_work_package_plan(raw)

    raw = _plan()
    raw["nodes"][0]["expected_outputs"] = [{"name": "report", "surprise": True}]
    with pytest.raises(ValidationError, match="unknown fields"):
        compile_work_package_plan(raw)


def test_effect_conflicts_encode_parallel_safety_rules() -> None:
    reader = WorkPackageEffects(reads=("db:tasks",))
    other_reader = WorkPackageEffects(reads=("db:tasks",))
    writer = WorkPackageEffects(writes=("db:tasks",))
    exclusive = WorkPackageEffects(exclusive=("repo:mac",))
    repo_reader = WorkPackageEffects(reads=("repo:mac",))
    publisher = WorkPackageEffects(external=("github:mac",))

    assert work_package_effect_conflicts(reader, other_reader) == []
    assert work_package_effect_conflicts(reader, writer) == ["write:db:tasks"]
    assert work_package_effect_conflicts(exclusive, repo_reader) == [
        "exclusive:repo:mac"
    ]
    assert work_package_effect_conflicts(publisher, publisher) == [
        "external:github:mac"
    ]
    repository_lock = WorkPackageEffects(exclusive=("repo:mac",))
    path_writer = WorkPackageEffects(writes=("src/api",))
    assert work_package_effect_conflicts(repository_lock, path_writer) == [
        "exclusive:repo:mac~src/api"
    ]


# ---------------------------------------------------------------------------
# Controller-owned composed-base station receipts
# ---------------------------------------------------------------------------

from mac.work_package_models import (  # noqa: E402
    WorkPackageComposedBasePredecessor,
    composed_base_lineage_digest,
)


def _composed_base_plan() -> dict:
    """A staged plan where mutation ``backend`` composes mutation ``base``."""

    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_composed",
        "goal": "Stage a mutation on a composed predecessor",
        "repository_id": "repo_composed",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "nodes": [
            {
                "node_id": "base",
                "title": "First mutation",
                "node_type": "mutation",
                "effects": {"writes": ["src/base"]},
                "expected_outputs": ["base-patch"],
                "verification": {"profile": "repository-default"},
                "scope_confidence": 0.9,
            },
            {
                "node_id": "backend",
                "title": "Mutation on composed base",
                "node_type": "mutation",
                "depends_on": ["base"],
                "effects": {"writes": ["src/backend"]},
                "expected_outputs": ["backend-patch"],
                "verification": {"profile": "repository-default"},
                "scope_confidence": 0.9,
            },
        ],
    }


def _receipt(*, covers: list[str], **overrides) -> dict:
    predecessors = [
        WorkPackageComposedBasePredecessor(
            node_key=key,
            candidate_id="cand_%s" % key,
            ref="refs/wp/candidate/%s" % key,
            commit=str(index + 1) * 40,
            tree=str(index + 5) * 40,
        )
        for index, key in enumerate(covers)
    ]
    merge_order = tuple(covers)
    composed_base_ref = overrides.get(
        "composed_base_ref", "refs/wp/composed-base/station-1"
    )
    composed_base_sha = overrides.get("composed_base_sha", "b" * 40)
    merge_strategy = overrides.get("merge_strategy", "ort")
    plan_version = overrides.get("plan_version", 1)
    epoch = overrides.get("epoch", 7)
    owner_fence = overrides.get("owner_fence", "controller:fence:1")
    lineage_digest = composed_base_lineage_digest(
        composed_base_ref=composed_base_ref,
        composed_base_sha=composed_base_sha,
        merge_strategy=merge_strategy,
        merge_order=merge_order,
        predecessors=tuple(predecessors),
        plan_version=plan_version,
        epoch=epoch,
        owner_fence=owner_fence,
    )
    receipt = {
        "station_id": overrides.get("station_id", "station-1"),
        "composed_base_ref": composed_base_ref,
        "composed_base_sha": composed_base_sha,
        "merge_strategy": merge_strategy,
        "merge_order": list(merge_order),
        "predecessors": [item.to_dict() for item in predecessors],
        "plan_version": plan_version,
        "epoch": epoch,
        "owner_fence": owner_fence,
        "recovery": overrides.get("recovery", "none"),
        "lineage_digest": overrides.get("lineage_digest", lineage_digest),
    }
    return receipt


def test_composed_base_receipt_relaxes_flat_mutation_wave() -> None:
    raw = _composed_base_plan()
    with pytest.raises(ValidationError, match="flat mutation wave"):
        compile_work_package_plan(raw)

    raw["composed_bases"] = [_receipt(covers=["base"])]
    compiled = compile_work_package_plan(raw)
    assert "composed_bases" in compiled.definition
    assert compiled.definition["composed_bases"][0]["station_id"] == "station-1"
    # The compiled definition still fences on the persisted revalidation path.
    validate_supported_work_package_topology(compiled.definition)


def test_composed_base_lineage_digest_is_deterministic() -> None:
    raw = _composed_base_plan()
    raw["composed_bases"] = [_receipt(covers=["base"])]
    first = compile_work_package_plan(raw)
    second = compile_work_package_plan(copy.deepcopy(raw))
    assert first.plan_digest == second.plan_digest
    assert first.definition == second.definition


def test_composed_base_receipt_rejects_tampered_lineage_digest() -> None:
    raw = _composed_base_plan()
    raw["composed_bases"] = [
        _receipt(covers=["base"], lineage_digest="sha256:" + "0" * 64)
    ]
    with pytest.raises(ValidationError, match="lineage_digest does not match"):
        compile_work_package_plan(raw)


def test_composed_base_receipt_rejects_altered_readback_sha() -> None:
    raw = _composed_base_plan()
    receipt = _receipt(covers=["base"])
    # Rewrite the protected read-back SHA without recomputing the digest.
    receipt["composed_base_sha"] = "c" * 40
    raw["composed_bases"] = [receipt]
    with pytest.raises(ValidationError, match="lineage_digest does not match"):
        compile_work_package_plan(raw)


def test_composed_base_receipt_requires_exact_frontier_coverage() -> None:
    raw = _composed_base_plan()
    # ``backend`` has repository ancestor frontier {base}.  A receipt that also
    # lists ``backend`` (a descendant of ``base``) is not an antichain and must
    # be rejected so the station never double-merges an absorbed lineage.
    raw["composed_bases"] = [_receipt(covers=["base", "backend"])]
    with pytest.raises(ValidationError, match="antichain"):
        compile_work_package_plan(raw)


def test_composed_base_receipt_partial_frontier_still_fenced() -> None:
    # Two independent mutation leaves fanned into an integration, then a further
    # mutation staged on the integration candidate.  Its repository ancestor
    # frontier is {left, right, assemble}; a receipt that covers only part of
    # it does not relax the fence.
    raw = {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_partial",
        "goal": "Partial composed frontier",
        "repository_id": "repo_partial",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "nodes": [
            {
                "node_id": "left",
                "title": "left mutation",
                "node_type": "mutation",
                "effects": {"writes": ["src/left"]},
                "expected_outputs": ["left-patch"],
                "verification": {"profile": "repository-default"},
                "scope_confidence": 0.9,
            },
            {
                "node_id": "right",
                "title": "right mutation",
                "node_type": "mutation",
                "effects": {"writes": ["src/right"]},
                "expected_outputs": ["right-patch"],
                "verification": {"profile": "repository-default"},
                "scope_confidence": 0.9,
            },
            {
                "node_id": "assemble",
                "title": "fan-in",
                "kind": "integration",
                "depends_on": ["left", "right"],
                "expected_outputs": ["assemble-candidate"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_id": "staged",
                "title": "mutation on composed base",
                "node_type": "mutation",
                "depends_on": ["assemble"],
                "effects": {"writes": ["src/staged"]},
                "expected_outputs": ["staged-patch"],
                "verification": {"profile": "repository-default"},
                "scope_confidence": 0.9,
            },
        ],
    }
    # Merging only one leaf leaves ``right`` and ``assemble`` uncovered, so the
    # exact frontier {assemble, left, right} is not matched and the staged
    # mutation stays fenced.
    raw["composed_bases"] = [_receipt(covers=["left"])]
    with pytest.raises(ValidationError, match="flat mutation wave"):
        compile_work_package_plan(raw)

    # Merging the integration candidate absorbs the whole antichain frontier
    # (assemble transitively absorbs left and right), which relaxes the fence.
    raw["composed_bases"] = [_receipt(covers=["assemble"])]
    compiled = compile_work_package_plan(raw)
    assert sorted(compiled.definition["composed_bases"][0]["covers"]) == [
        "assemble",
        "left",
        "right",
    ]


def test_composed_base_receipt_rejects_non_repository_predecessor() -> None:
    raw = _plan()
    # ``contract`` is an analysis node, not a repository-producing candidate.
    receipt = _receipt(covers=["contract"])
    raw["composed_bases"] = [receipt]
    with pytest.raises(ValidationError, match="not a repository-producing"):
        compile_work_package_plan(raw)


def test_composed_base_receipt_requires_crash_recovery_marker() -> None:
    raw = _composed_base_plan()
    receipt = _receipt(covers=["base"], recovery="bogus")
    raw["composed_bases"] = [receipt]
    with pytest.raises(ValidationError, match="recovery must be one of"):
        compile_work_package_plan(raw)


def test_composed_base_receipt_rejects_duplicate_protected_ref() -> None:
    raw = _composed_base_plan()
    receipt_a = _receipt(covers=["base"], station_id="station-a")
    receipt_b = _receipt(covers=["base"], station_id="station-b")
    raw["composed_bases"] = [receipt_a, receipt_b]
    with pytest.raises(ValidationError, match="duplicate protected composed_base_ref"):
        compile_work_package_plan(raw)


def test_composed_base_receipt_survives_persisted_revalidation() -> None:
    raw = _composed_base_plan()
    raw["composed_bases"] = [_receipt(covers=["base"])]
    definition = compile_work_package_plan(raw).definition
    # Revalidation with the receipt present is accepted...
    validate_supported_work_package_topology(definition)
    # ...but stripping the receipt re-arms the flat mutation-wave fence.
    stripped = copy.deepcopy(definition)
    stripped.pop("composed_bases")
    with pytest.raises(ValidationError, match="flat mutation wave"):
        validate_supported_work_package_topology(stripped)
