from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import workflow_service
from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane


def _cp() -> ControlPlane:
    cp = ControlPlane.in_memory()
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="quality",
        system_prompt="test",
        level="ic",
    )
    return cp


def _definition(*, attempts: int = 1) -> dict:
    return {
        "nodes": [
            {
                "node_key": "check",
                "node_type": "task",
                "role_required": "qa",
                "max_attempts": attempts,
            }
        ],
        "edges": [
            {
                "from_node_key": "",
                "to_node_key": "check",
                "condition": "success",
                "priority": 100,
            },
            {
                "from_node_key": "check",
                "to_node_key": "",
                "condition": "success",
                "priority": 1,
            },
        ],
    }


def test_question_normalization_migration_and_instruction_binding_edges() -> None:
    questions = workflow_service._normalize_questions(
        [
            "skip",
            {"id": "same", "text": "First", "annotation": "keep"},
            {"id": "same", "text": "Second"},
        ]
    )
    assert [item["id"] for item in questions] == ["same", "same_2"]
    assert questions[0]["annotation"] == "keep"
    assert workflow_service._migrate_answers_to_ids(
        questions, {"unmatched": "preserved"}
    ) == {"unmatched": "preserved"}

    node = {"node_key": "check", "instructions": "original"}
    bound = workflow_service._apply_question_bindings(
        node,
        [
            {"id": "optional", "text": "Optional", "binds_to_node": "check"},
            {
                "id": "replace",
                "text": "Replacement",
                "binds_to_node": "check",
                "binds_to_param": "instructions",
            },
        ],
        {"replace": "new instructions"},
    )
    assert bound["instructions"] == "new instructions"


def test_create_validation_tenant_and_version_lookup_edges() -> None:
    cp = _cp()
    for kwargs, match in [
        ({"slug": "", "name": "n", "workflow_type": "t"}, "slug"),
        ({"slug": "s", "name": "", "workflow_type": "t"}, "name"),
        ({"slug": "s", "name": "n", "workflow_type": ""}, "workflow_type"),
    ]:
        with pytest.raises(ValidationError, match=match):
            cp.workflows.create_workflow(
                description="d",
                definition=_definition(),
                created_by="actor",
                **kwargs,
            )

    tenant = cp.register_tenant("team")
    workflow = cp.workflows.create_workflow(
        slug="tenant-flow",
        name="Tenant Flow",
        description="d",
        workflow_type="custom",
        definition=_definition(),
        created_by="actor",
        tenant_id=tenant.id,
    )
    assert cp.workflows.get_workflow(
        "tenant-flow", tenant_id=tenant.id, version=1
    ).id == workflow.id
    assert cp.workflows.list_workflows(
        tenant_id=tenant.id, workflow_type="custom", enabled=True
    )[0].id == workflow.id


def test_update_workflow_definition_metadata_and_noop_edges() -> None:
    cp = _cp()
    workflow = cp.workflows.create_workflow(
        slug="flow",
        name="Flow",
        description="d",
        workflow_type="custom",
        definition=_definition(),
        created_by="actor",
    )
    assert cp.workflows.update_workflow(workflow.id).id == workflow.id
    updated = cp.workflows.update_workflow(
        workflow.id,
        name="Renamed",
        workflow_type="review",
        is_default=True,
        metadata={"owner": "ops"},
    )
    assert updated.name == "Renamed"
    assert updated.workflow_type == "review"
    assert updated.is_default is True
    assert updated.metadata == {"owner": "ops"}
    assert cp.workflows.enable_workflow(workflow.id).enabled is True

    versioned = cp.workflows.update_workflow(
        workflow.id,
        definition=_definition(attempts=2),
        created_by="editor",
    )
    assert versioned.version == 2


def test_draft_validation_update_and_filter_edges() -> None:
    cp = _cp()
    with pytest.raises(ValidationError, match="goal"):
        cp.workflows.create_draft(" ")

    tenant = cp.register_tenant("draft-team")
    draft = cp.workflows.create_draft(
        "Goal",
        tenant_id=tenant.id,
        proposed_steps=[{"node_key": "check", "role_required": "qa"}],
    )
    with pytest.raises(ValidationError, match="unsupported"):
        cp.workflows.update_draft(draft.id, status="invalid")

    updated = cp.workflows.update_draft(
        draft.id,
        goal="New goal",
        proposed_steps=[{"node_key": "check", "role_required": "qa"}],
        questions=[{"text": "Proceed?"}],
        status="questions",
    )
    assert updated.goal == "New goal"
    assert updated.status == "questions"
    assert cp.workflows.list_drafts(
        tenant_id=tenant.id, status="questions", limit=5000
    )[0].id == draft.id


def test_definition_decision_import_seed_and_role_fallback_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cp = _cp()
    empty = cp.workflows.create_draft("Empty")
    with pytest.raises(ValidationError, match="no proposed_steps"):
        cp.workflows.definition_from_draft(empty)

    missing_role = cp.workflows.create_draft(
        "Missing role", proposed_steps=[{"node_key": "step"}]
    )
    with pytest.raises(ValidationError, match="missing role_required"):
        cp.workflows.definition_from_draft(missing_role)

    with pytest.raises(ValidationError, match="mapping"):
        cp.workflows.import_yaml("- item\n", created_by="actor")
    with pytest.raises(NotFoundError, match="seed directory"):
        cp.workflows.seed_defaults(source=tmp_path / "missing")
    with pytest.raises(ValidationError, match="role hint"):
        cp.workflows._slug_from_node({})

    monkeypatch.setattr(cp.workflows, "_get_role", lambda slug: SimpleNamespace(slug=slug))
    assert cp.workflows._validate_definition(_definition()).nodes[0].role_required == "qa"

    run = SimpleNamespace(
        id="run",
        workflow_id="workflow",
        state="running",
        current_node_key=None,
        current_task_id=None,
        context={"pre_decisions": {"gate": "approved"}},
        definition_snapshot={
            "nodes": [{"node_key": "gate", "node_type": "approval"}],
            "edges": [],
        },
    )
    assert cp.workflows.decisions_for_run(run)["decisions"][0]["state"] == (
        "pre_decided"
    )
