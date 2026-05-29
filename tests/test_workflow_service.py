import pytest

from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    cp = ControlPlane.in_memory()
    cp.roles.create_role(
        slug="qa",
        name="QA",
        description="d",
        system_prompt="p",
        level="ic",
    )
    cp.roles.create_role(
        slug="dev",
        name="Dev",
        description="d",
        system_prompt="p",
        level="ic",
    )
    return cp


def _two_node_definition() -> dict:
    return {
        "nodes": [
            {
                "node_key": "investigate",
                "node_type": "task",
                "role_required": "qa",
                "max_attempts": 2,
            },
            {
                "node_key": "fix",
                "node_type": "task",
                "role_required": "dev",
                "max_attempts": 1,
            },
        ],
        "edges": [
            {"from_node_key": "", "to_node_key": "investigate", "condition": "success", "priority": 100},
            {"from_node_key": "investigate", "to_node_key": "fix", "condition": "success", "priority": 100},
            {"from_node_key": "fix", "to_node_key": "", "condition": "success", "priority": 100},
        ],
    }


def test_create_workflow_resolves_roles_and_persists_definition(cp):
    wf = cp.workflows.create_workflow(
        slug="bug-default",
        name="Bug Fix",
        description="auto-filed bugs",
        workflow_type="bug",
        definition=_two_node_definition(),
        created_by="human",
    )
    assert wf.version == 1
    assert len(wf.definition["nodes"]) == 2
    again = cp.workflows.get_workflow("bug-default")
    assert again.id == wf.id


def test_create_workflow_rejects_unknown_role(cp):
    bad = _two_node_definition()
    bad["nodes"][0]["role_required"] = "ghost-role"
    with pytest.raises(ValidationError) as exc:
        cp.workflows.create_workflow(
            slug="bug",
            name="b",
            description="d",
            workflow_type="bug",
            definition=bad,
            created_by="h",
        )
    assert "ghost-role" in str(exc.value)


def test_create_workflow_rejects_duplicate_node_keys(cp):
    bad = _two_node_definition()
    bad["nodes"][1]["node_key"] = "investigate"
    with pytest.raises(ValidationError):
        cp.workflows.create_workflow(
            slug="bug",
            name="b",
            description="d",
            workflow_type="bug",
            definition=bad,
            created_by="h",
        )


def test_create_workflow_requires_exactly_one_start_edge(cp):
    bad = _two_node_definition()
    bad["edges"][0]["from_node_key"] = "investigate"  # remove start edge
    with pytest.raises(ValidationError):
        cp.workflows.create_workflow(
            slug="bug",
            name="b",
            description="d",
            workflow_type="bug",
            definition=bad,
            created_by="h",
        )


def test_create_workflow_rejects_unreachable_node(cp):
    bad = _two_node_definition()
    bad["edges"][1]["from_node_key"] = ""  # second start edge — the first node now has none
    with pytest.raises(ValidationError):
        cp.workflows.create_workflow(
            slug="bug",
            name="b",
            description="d",
            workflow_type="bug",
            definition=bad,
            created_by="h",
        )


def test_workflow_definition_change_bumps_version(cp):
    cp.workflows.create_workflow(
        slug="bug",
        name="b",
        description="d",
        workflow_type="bug",
        definition=_two_node_definition(),
        created_by="h",
    )
    new_definition = _two_node_definition()
    new_definition["nodes"][0]["max_attempts"] = 5
    wf2 = cp.workflows.create_workflow(
        slug="bug",
        name="b",
        description="d",
        workflow_type="bug",
        definition=new_definition,
        created_by="h",
    )
    assert wf2.version == 2
    # Same definition again: no new row.
    wf3 = cp.workflows.create_workflow(
        slug="bug",
        name="b",
        description="d",
        workflow_type="bug",
        definition=new_definition,
        created_by="h",
    )
    assert wf3.id == wf2.id
    assert wf3.version == 2


def test_workflow_definition_serializes_with_versioned_typed_shape(cp):
    wf = cp.workflows.create_workflow(
        slug="typed",
        name="Typed",
        description="typed",
        workflow_type="bug",
        definition=_two_node_definition(),
        created_by="h",
    )

    assert wf.definition["schema_version"] == 1
    assert wf.definition["nodes"][0]["node_key"] == "investigate"
    preview = cp.preview_workflow(wf.id, input={"ticket": "mac-123"})
    assert preview["schema"] == "mac.workflow.preview.v1"
    assert preview["task_count"] == 2
    assert preview["tasks"][1]["dependencies"] == ["investigate"]
    assert preview["tasks"][0]["metadata"]["inherited_context"]["ticket"] == "mac-123"


def test_workflow_draft_can_be_previewed_and_approved(cp):
    draft = cp.create_workflow_draft(
        "Investigate then fix",
        proposed_steps=[
            {
                "node_key": "investigate",
                "role_required": "qa",
                "instructions": "Find the issue",
            },
            {
                "node_key": "fix",
                "role_required": "dev",
                "instructions": "Patch the issue",
            },
        ],
        questions=[{"id": "scope", "prompt": "What scope?"}],
        answers={"scope": "tests"},
    )

    preview = cp.preview_workflow_draft(draft.id)
    assert preview["draft_id"] == draft.id
    assert [task["node_key"] for task in preview["tasks"]] == ["investigate", "fix"]

    updated = cp.update_workflow_draft(draft.id, answers={"scope": "tests only"}, actor="human")
    assert updated.edit_history[-1]["patch"]["answers"] == {"scope": "tests only"}

    workflow = cp.approve_workflow_draft(
        draft.id,
        slug="draft-workflow",
        name="Draft Workflow",
        approved_by="human",
    )
    assert workflow.metadata["draft_id"] == draft.id
    assert cp.get_workflow_draft(draft.id).status == "compiled"


def test_draft_questions_are_normalized_with_assigned_ids(cp):
    """wf-01: questions without explicit ids get slugged ones; idempotent."""
    draft = cp.create_workflow_draft(
        "Add feature",
        proposed_steps=[
            {"node_key": "design", "role_required": "qa", "instructions": "Design"},
        ],
        # Legacy shape: no `id`, uses `prompt` (or `text`) for the question text.
        questions=[
            {"prompt": "Which database driver?"},
            {"text": "Which auth provider?", "required": True},
        ],
    )
    ids = [q["id"] for q in draft.questions]
    assert all(ids), "every question must have an id"
    assert len(set(ids)) == len(ids), "ids must be unique"
    # Slugs should be derived from the question text where possible.
    assert any("driver" in qid or "database" in qid for qid in ids)
    # Re-reading the draft is idempotent — ids don't shift.
    again = cp.get_workflow_draft(draft.id)
    assert [q["id"] for q in again.questions] == ids


def test_approve_draft_refuses_required_unanswered_questions(cp):
    """wf-01: required questions block approval until answered."""
    draft = cp.create_workflow_draft(
        "Add feature",
        proposed_steps=[
            {"node_key": "design", "role_required": "qa", "instructions": "Design"},
        ],
        questions=[
            {"id": "scope", "text": "What scope?", "required": True},
        ],
        # answers omitted on purpose
    )
    with pytest.raises(ValidationError, match="required questions unanswered"):
        cp.approve_workflow_draft(
            draft.id, slug="gated", name="Gated", approved_by="human"
        )
    # Answer the question and approval succeeds.
    cp.update_workflow_draft(draft.id, answers={"scope": "API only"}, actor="human")
    wf = cp.approve_workflow_draft(
        draft.id, slug="gated", name="Gated", approved_by="human"
    )
    assert wf.metadata["answers"] == {"scope": "API only"}


def test_approve_draft_validates_question_node_bindings(cp):
    """wf-01: a question that binds_to_node must reference a real node_key."""
    draft = cp.create_workflow_draft(
        "Add feature",
        proposed_steps=[
            {"node_key": "design", "role_required": "qa", "instructions": "Design"},
        ],
        questions=[
            {
                "id": "scope",
                "text": "What scope?",
                "required": True,
                "binds_to_node": "nonexistent_node",
            },
        ],
        answers={"scope": "API"},
    )
    with pytest.raises(ValidationError, match="unknown node_keys"):
        cp.approve_workflow_draft(
            draft.id, slug="badbind", name="Badbind", approved_by="human"
        )


def test_approve_draft_injects_answer_into_bound_node_instructions(cp):
    """wf-01: an answered binds_to_node question appends its answer block
    to the target node's instructions so an LLM-driven executor sees it."""
    draft = cp.create_workflow_draft(
        "Add feature",
        proposed_steps=[
            {
                "node_key": "design",
                "role_required": "qa",
                "instructions": "Design the feature",
            },
            {
                "node_key": "implement",
                "role_required": "dev",
                "instructions": "Build it",
            },
        ],
        questions=[
            {
                "id": "scope",
                "text": "What scope?",
                "required": True,
                "binds_to_node": "design",
            },
            {
                "id": "stack",
                "text": "Which stack?",
                "binds_to_node": "implement",
                "binds_to_param": "metadata.preferred_stack",
            },
        ],
        answers={"scope": "API only", "stack": "FastAPI"},
    )
    wf = cp.approve_workflow_draft(
        draft.id, slug="bound", name="Bound", approved_by="human"
    )
    nodes_by_key = {n["node_key"]: n for n in wf.definition["nodes"]}
    # Default binding appended a readable answer block to instructions.
    assert "Pre-supplied answer: What scope?" in nodes_by_key["design"]["instructions"]
    assert "API only" in nodes_by_key["design"]["instructions"]
    # The answer is also in metadata.answers (always).
    assert nodes_by_key["design"]["metadata"]["answers"]["scope"] == "API only"
    # binds_to_param="metadata.preferred_stack" sets the metadata field
    # directly rather than appending to instructions.
    assert nodes_by_key["implement"]["metadata"]["preferred_stack"] == "FastAPI"
    assert "FastAPI" not in nodes_by_key["implement"]["instructions"]


def test_legacy_text_keyed_answers_migrate_to_ids(cp):
    """wf-01: callers used to key answers by free-text question content;
    those answers should map onto the newly-assigned question ids."""
    draft = cp.create_workflow_draft(
        "Legacy shape",
        proposed_steps=[
            {"node_key": "design", "role_required": "qa", "instructions": "Design"},
        ],
        # No `id`, answer keyed by the text instead of an id.
        questions=[{"text": "What scope?", "required": True}],
        answers={"What scope?": "API only"},
    )
    # The answer should have been migrated under the normalized id.
    assert "What scope?" not in draft.answers or draft.answers != {"What scope?": "API only"}
    qid = draft.questions[0]["id"]
    assert draft.answers.get(qid) == "API only"


def test_delete_workflow_refuses_when_runs_in_flight(cp):
    wf = cp.workflows.create_workflow(
        slug="bug",
        name="b",
        description="d",
        workflow_type="bug",
        definition=_two_node_definition(),
        created_by="h",
    )
    # Mock an in-flight run by inserting directly (workflow_runtime ships
    # in phase 4).
    cp.store.execute(
        """
        INSERT INTO workflow_runs (
            id, workflow_id, workflow_version, definition_snapshot, state,
            current_node_key, started_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', 'investigate', 'human', ?, ?)
        """,
        ("run-1", wf.id, wf.version, "{}", "now", "now"),
    )
    with pytest.raises(ValidationError):
        cp.workflows.delete_workflow(wf.id)


def test_seed_defaults_loads_four_loom_workflows(cp):
    # Workflow seeding requires the loom role catalog to be present too,
    # because each node references a role by slug.
    cp.roles.seed_defaults()
    seeded = cp.workflows.seed_defaults()
    types = {wf.workflow_type for wf in seeded}
    assert types == {"bug", "feature", "ui", "self-improvement"}
    listed = cp.workflows.list_workflows()
    assert len(listed) >= 4


def test_import_yaml_round_trips_a_workflow(cp):
    yaml_text = """
id: bug-default
name: Bug Fix
description: tiny
workflow_type: bug
is_default: true
nodes:
  - node_key: investigate
    node_type: task
    role_required: QA
    persona_hint: default/qa
    max_attempts: 1
  - node_key: fix
    node_type: task
    role_required: Dev
    persona_hint: default/dev
    max_attempts: 1
edges:
  - from_node_key: ""
    to_node_key: investigate
    condition: success
    priority: 100
  - from_node_key: investigate
    to_node_key: fix
    condition: success
    priority: 100
  - from_node_key: fix
    to_node_key: ""
    condition: success
    priority: 100
"""
    wf = cp.workflows.import_yaml(yaml_text, created_by="human")
    assert wf.slug == "bug-default"
    assert wf.definition["nodes"][0]["role_required"] == "qa"  # normalised to slug


def test_import_yaml_rejects_oversized_payload(cp):
    """mac-i044: yaml.safe_load with no size cap is a DoS vector via
    billion-laughs-style nested alias expansion. Cap the input size."""
    from mac.workflow_service import WorkflowService

    huge = "x" * (WorkflowService.YAML_IMPORT_MAX_BYTES + 1)
    with pytest.raises(ValidationError, match="byte limit"):
        cp.workflows.import_yaml(huge, created_by="human")
