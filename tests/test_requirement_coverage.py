"""A review must cover every requirement the task statement enumerates.

Live 2026-10-07: task_8361a260 asked for three OpenShell 0.1.2 fixes and
required a live canary as acceptance. Its reviewer approved a diff that touched
only the first fix, reporting ``finding_count 0``, because nothing mapped the
statement's enumerated requirements to the change. The review now records an
explicit coverage mapping and sends the task back with ``changes_requested``,
naming each unaddressed item, instead of approving partial work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.executor_prompt import build_review_prompt, build_task_prompt
from mac.models import ReviewStatus, TaskState
from mac.requirement_coverage import (
    STATUS_FAIL,
    STATUS_NOT_REQUIRED,
    STATUS_PASS,
    evaluate_requirement_coverage,
    parse_task_requirements,
)
from mac.services import ControlPlane
from tests.test_control_plane import _sign, register_agent, verified_repo_metadata


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


# The task statement that exposed the bug: three numbered fixes inline in prose.
THREE_FIXES = (
    "task_8361a260 asked for three fixes to run MAC on OpenShell 0.1.2: "
    "(1) sandbox list --limit, (2) task sandbox missing before harvest, "
    "(3) driver config refused."
)


def test_inline_numbered_requirements_are_parsed():
    requirements = parse_task_requirements(THREE_FIXES)

    assert [item["id"] for item in requirements] == ["1", "2", "3"]
    assert requirements[0]["text"] == "sandbox list --limit"
    assert requirements[2]["text"] == "driver config refused"


def test_acceptance_section_items_are_parsed():
    description = (
        "Fix the widget.\n\n"
        "Acceptance:\n"
        "- a live canary reports success\n"
        "- the unit suite is green\n"
    )

    requirements = parse_task_requirements(description)

    assert [item["text"] for item in requirements] == [
        "a live canary reports success",
        "the unit suite is green",
    ]


def test_a_statement_without_enumeration_requires_nothing():
    assert parse_task_requirements("Implement the thing") == []
    assert evaluate_requirement_coverage("Implement the thing", {})["status"] == STATUS_NOT_REQUIRED


def test_unmapped_and_unaddressed_requirements_fail_and_are_named():
    manifest = {
        "repo": {"files_changed": ["src/mac/openshell_sandbox_gc.py"]},
        "requirements": [
            {
                "id": "1",
                "addressed": True,
                "evidence": ["src/mac/openshell_sandbox_gc.py"],
            },
            {"id": "3", "addressed": False},
        ],
    }

    result = evaluate_requirement_coverage(THREE_FIXES, manifest)

    assert result["status"] == STATUS_FAIL
    assert [item["id"] for item in result["unaddressed"]] == ["2", "3"]
    assert "requirement 2 is not mapped" in result["problems"][0]
    assert "requirement 3 is not addressed" in result["problems"][1]


def test_a_complete_mapping_passes():
    manifest = {
        "repo": {"files_changed": ["src/mac/openshell_sandbox_gc.py"]},
        "requirements": [
            {"id": "1", "addressed": True, "evidence": ["list --limit"]},
            {"id": "2", "addressed": True, "evidence": ["harvest guard"]},
            {"id": "3", "addressed": True, "evidence": ["driver config"]},
        ],
    }

    assert evaluate_requirement_coverage(THREE_FIXES, manifest)["status"] == STATUS_PASS


def test_line_oriented_numbered_list_is_parsed():
    description = "Do all of this:\n1. add alpha\n2. add beta\n3. add gamma\n"

    assert [item["id"] for item in parse_task_requirements(description)] == ["1", "2", "3"]


def test_mapping_keyed_by_id_and_text_both_match():
    keyed = {
        "requirements": {
            "1": {"addressed": True, "evidence": "alpha"},
            "2": {"addressed": True, "evidence": ["beta"]},
            "3": {"addressed": True, "evidence": ["gamma"]},
        }
    }
    assert evaluate_requirement_coverage(THREE_FIXES, keyed)["status"] == STATUS_PASS

    by_text = {
        "requirements": [
            {"requirement": "sandbox list --limit", "evidence": ["list --limit"]},
            {"requirement": "task sandbox missing before harvest", "evidence": ["harvest"]},
            {"requirement": "driver config refused", "evidence": ["driver"]},
        ]
    }
    assert evaluate_requirement_coverage(THREE_FIXES, by_text)["status"] == STATUS_PASS


def test_an_explicit_unaddressed_status_is_not_covered():
    manifest = {
        "requirements": [
            {"id": "1", "status": "unaddressed", "evidence": ["something"]},
            {"id": "2", "addressed": True, "evidence": ["beta"]},
            {"id": "3", "addressed": True, "evidence": ["gamma"]},
        ]
    }

    result = evaluate_requirement_coverage(THREE_FIXES, manifest)

    assert result["status"] == STATUS_FAIL
    assert [item["id"] for item in result["unaddressed"]] == ["1"]


def test_reviewer_mapping_takes_precedence_over_executor_mapping():
    executor = {"requirements": [{"id": "1", "addressed": True, "evidence": ["alpha"]}]}
    reviewer = {"requirements": [{"id": "1", "addressed": False, "evidence": []}]}

    result = evaluate_requirement_coverage(THREE_FIXES, executor, reviewer)

    assert result["status"] == STATUS_FAIL
    assert result["unaddressed"][0]["id"] == "1"


def test_task_and_review_prompts_request_the_mapping(tmp_path):
    task = {"id": "task_x", "title": "t", "description": THREE_FIXES, "metadata": {}}

    task_prompt = build_task_prompt(task)
    assert "Task requirements" in task_prompt
    assert "sandbox list --limit" in task_prompt
    assert "`requirements` list" in task_prompt

    review_prompt = build_review_prompt(
        task, Path(str(tmp_path)), {"executor_evidence_id": "ev", "review_id": "rv"}
    )
    assert "This task enumerates requirements" in review_prompt
    assert "`requirements` list" in review_prompt

    plain = {"id": "task_x", "title": "t", "description": "Implement the thing", "metadata": {}}
    assert "Task requirements" not in build_task_prompt(plain)


def test_deterministic_finalizer_carries_the_agent_mapping():
    from mac.executor_finalizer import _carry_requirement_coverage

    manifest: dict = {}
    _carry_requirement_coverage(
        manifest,
        {"requirements": [{"id": "1", "addressed": True, "evidence": ["src/x.py"]}]},
    )
    assert manifest["requirements"][0]["id"] == "1"

    empty: dict = {}
    _carry_requirement_coverage(empty, {})
    assert "requirements" not in empty


def _submit_repo_evidence(cp, task, worker, *, requirements):
    metadata = verified_repo_metadata(
        cp, worker.id, files_changed=["src/mac/openshell_sandbox_gc.py"]
    )
    manifest = metadata["verification"]
    manifest.pop("signature", None)
    manifest.pop("signed_by", None)
    manifest["requirements"] = requirements
    metadata["verification"] = _sign(cp, worker.id, manifest)
    evidence = cp.add_evidence(
        task.id,
        "test",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=metadata,
    )
    cp.submit_for_review(task.id, worker.id)
    return evidence


def test_review_sends_back_a_change_covering_one_of_three_requirements(cp):
    """Replays task_8361a260: the diff names item 1 only, so the task returns to
    the worker as changes_requested naming items 2 and 3."""
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "Run MAC on OpenShell 0.1.2",
        description=THREE_FIXES,
        project="mac",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    _submit_repo_evidence(
        cp,
        task,
        worker,
        requirements=[
            {
                "id": "1",
                "addressed": True,
                "evidence": ["src/mac/openshell_sandbox_gc.py"],
            }
        ],
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "review_not_approved"
    current = cp.get_task(task.id)
    assert current.state == TaskState.OPEN.value
    assert current.attempt_count < current.max_attempts
    reviews = cp.list_reviews(task.id)
    assert reviews[-1].status == ReviewStatus.CHANGES_REQUESTED.value
    verdict = cp.get_evidence(reviews[-1].evidence_id)
    manifest = verdict.metadata["verification"]
    assert manifest["requirement_coverage"]["status"] == STATUS_FAIL
    assert [item["id"] for item in manifest["requirement_coverage"]["unaddressed"]] == [
        "2",
        "3",
    ]
    assert "requirements not addressed" in manifest["feedback"]
    assert cp.list_publications(task.id) == []
    names = {event.name for event in cp.list_observability(limit=100)}
    assert "workflow.default_review.changes_requested" in names
    assert "workflow.default_review.published" not in names


def test_review_approves_when_every_requirement_is_mapped(cp):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "Run MAC on OpenShell 0.1.2",
        description=THREE_FIXES,
        project="mac",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    _submit_repo_evidence(
        cp,
        task,
        worker,
        requirements=[
            {"id": "1", "addressed": True, "evidence": ["list --limit"]},
            {"id": "2", "addressed": True, "evidence": ["harvest guard"]},
            {"id": "3", "addressed": True, "evidence": ["driver config"]},
        ],
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert cp.list_reviews(task.id)[-1].status == ReviewStatus.APPROVED.value
