from __future__ import annotations

import copy

import pytest

from mac.models import ValidationError
from mac.review_experiments import (
    ASSIGNMENT_SCHEMA,
    append_outcome,
    build_assignment,
    build_observation,
    build_outcome,
    build_report,
    choose_weighted_arm,
)
from mac.services import ControlPlane


def _detail(arm: str, outcome_status: str) -> dict:
    assignment = build_assignment(
        task_id="task_%s" % arm,
        experiment_id="exp-review",
        arm=arm,
        blind=arm == "blind",
    )
    finding_id = "finding_%s" % arm
    outcome = build_outcome(
        kind="finding_validation",
        status=outcome_status,
        finding_id=finding_id,
        severity_weight=1,
        observed_by="operator",
    )
    metadata = append_outcome({"review_experiment": assignment}, outcome)
    return {
        "task": {
            "id": "task_%s" % arm,
            "project": "demo",
            "state": "completed",
            "metadata": metadata,
        },
        "evidence": [
            {
                "id": "executor_%s" % arm,
                "metadata": {
                    "verification": {
                        "evidence_type": "repo_change",
                        "llm": {
                            "model": "gpt-5.1-codex",
                            "family": "gpt-5",
                            "provider": "openai",
                        },
                    }
                },
            },
            {
                "id": "review_%s" % arm,
                "created_at": "2026-01-01T00:00:00+00:00",
                "metadata": {
                    "verification": {
                        "evidence_type": "review_verdict",
                        "review_id": "r_%s" % arm,
                        "reviewed_evidence_id": "executor_%s" % arm,
                        "verdict": "approved",
                        "semantic_verdict": "approved",
                        "llm": {
                            "model": "claude-sonnet-4.5",
                            "family": "claude",
                            "provider": "anthropic",
                        },
                        "review_experiment": {
                            **assignment,
                            "protocol": {
                                "schema": "mac.review_protocol.v1",
                                "protocol_compliant": True,
                                "discovery_duration_ms": 12 if arm == "blind" else 0,
                            },
                        },
                        "independent_findings": (
                            [{"summary": "independent defect"}]
                            if arm == "blind"
                            else []
                        ),
                        "findings": [
                            {"id": finding_id, "summary": "validated defect"}
                        ],
                    }
                },
            },
        ],
        "reviews": [
            {
                "id": "r_%s" % arm,
                "evidence_id": "review_%s" % arm,
                "status": "approved",
            }
        ],
        "publications": [],
    }


def test_weighted_assignment_is_stable_and_records_propensity():
    first = choose_weighted_arm(
        "task_1", "exp_1", "v2", {"standard": 1, "blind": 3}
    )
    second = choose_weighted_arm(
        "task_1", "exp_1", "v2", {"blind": 3, "standard": 1}
    )

    assert first == second
    assert first[2] == {"blind": 0.75, "standard": 0.25}
    assert first[1] == first[2][first[0]]


def test_assignment_and_outcome_validation_fail_closed():
    with pytest.raises(ValidationError, match="either arm or arms"):
        build_assignment(
            task_id="task_1",
            experiment_id="exp",
            arm="blind",
            arms={"blind": 1},
        )
    with pytest.raises(ValidationError, match="computed for weighted arms"):
        build_assignment(
            task_id="task_1",
            experiment_id="exp",
            arms={"blind": 1, "standard": 1},
            assignment_probability=0.5,
        )
    with pytest.raises(ValidationError, match="not present"):
        build_assignment(
            task_id="task_1",
            experiment_id="exp",
            arms={"standard": 1},
            blind_arms=["blind"],
        )
    with pytest.raises(ValidationError, match="confirmed, refuted, or pending"):
        build_outcome(kind="finding_validation", status="maybe", observed_by="operator")
    with pytest.raises(ValidationError, match="must be an integer"):
        build_report("exp", [], min_tasks_per_arm="many")


def test_outcome_identity_is_idempotent_across_transport_retries():
    first = build_outcome(
        kind="clean_window",
        status="confirmed",
        observed_by="operator",
        detail={"window_end": "2026-07-04"},
        observed_at="2026-07-04T10:00:00+00:00",
    )
    retry = build_outcome(
        kind="clean_window",
        status="confirmed",
        observed_by="operator",
        detail={"window_end": "2026-07-04"},
        observed_at="2026-07-04T10:00:01+00:00",
    )

    assert first["id"] == retry["id"]
    assert len(append_outcome(append_outcome({}, first), retry)["review_outcomes"]) == 1


def test_observation_links_signed_review_models_findings_and_outcomes():
    observation = build_observation(_detail("blind", "confirmed"))

    review_pass = observation["review_passes"][0]
    assert observation["experiment"]["schema"] == ASSIGNMENT_SCHEMA
    assert review_pass["actual_strategy"] == "cross_family"
    assert review_pass["findings"][0]["validation_status"] == "confirmed"
    assert observation["totals"]["independent_findings"] == 1
    assert observation["totals"]["confirmed_findings"] == 1


def test_observation_joins_task_routes_and_id_keyed_findings():
    detail = _detail("blind", "confirmed")
    executor = detail["evidence"][0]
    review = detail["evidence"][1]
    executor["created_by"] = "agent_executor"
    executor["created_at"] = "2026-01-01T00:01:00+00:00"
    review["created_by"] = "agent_reviewer"
    executor["metadata"]["verification"].pop("llm")
    review["metadata"]["verification"].pop("llm")
    finding = review["metadata"]["verification"]["findings"][0]
    review["metadata"]["verification"]["findings"] = {
        finding["id"]: {"summary": finding["summary"]}
    }
    detail["reviews"][0].update(
        {
            "reviewer_agent_id": "agent_reviewer",
            "created_at": "2026-01-01T00:01:01+00:00",
            "completed_at": "2026-01-01T00:02:00+00:00",
        }
    )
    routes = [
        {
            "created_at": "2026-01-01T00:00:30+00:00",
            "source": "agent_executor",
            "detail": {
                "schema": "mac.llm_route.v1",
                "agent_id": "agent_executor",
                "resolved_model": "gpt-5.1-codex",
                "provider": "openai",
                "duration_ms": 10,
                "usage": {"prompt_tokens": 20, "completion_tokens": 3},
            },
        },
        {
            "created_at": "2026-01-01T00:01:30+00:00",
            "source": "agent_reviewer",
            "detail": {
                "schema": "mac.llm_route.v1",
                "agent_id": "agent_reviewer",
                "resolved_model": "claude-sonnet-4.6",
                "provider": "anthropic",
                "duration_ms": 15,
                "usage": {"prompt_tokens": 30, "completion_tokens": 4},
            },
        },
    ]

    observation = build_observation(detail, llm_routes=routes)
    review_pass = observation["review_passes"][0]

    assert review_pass["actual_strategy"] == "cross_family"
    assert review_pass["executor_model"]["model"] == "gpt-5.1-codex"
    assert review_pass["reviewer_model"]["model"] == "claude-sonnet-4.6"
    assert review_pass["usage"]["input_tokens"] == 30
    assert review_pass["usage"]["output_tokens"] == 4
    assert review_pass["findings"][0]["id"] == "finding_blind"
    assert observation["totals"]["findings"] == 1


def test_protocol_invalid_outcome_overrides_signed_compliance():
    detail = _detail("blind", "confirmed")
    invalidation = build_outcome(
        kind="protocol_invalid",
        status="confirmed",
        finding_id="operator:blind-leak",
        source="payload-audit",
        observed_by="operator",
        detail={"summary": "executor evidence leaked into discovery metadata"},
    )
    detail["task"]["metadata"] = append_outcome(
        detail["task"]["metadata"], invalidation
    )

    observation = build_observation(detail)
    review_pass = observation["review_passes"][0]

    assert observation["sample_valid"] is False
    assert observation["totals"]["protocol_invalidations"] == 1
    protocol = review_pass["experiment_protocol"]["protocol"]
    assert protocol["protocol_compliant"] is False
    assert protocol["operator_invalidated"] is True
    assert protocol["invalidations"][0]["finding_id"] == "operator:blind-leak"

    report = build_report(
        "exp-review",
        [observation],
        min_tasks_per_arm=1,
        min_validated_outcomes_per_arm=0,
    )
    arm = report["arms"][0]
    assert arm["protocol_invalid_tasks"] == 1
    assert arm["protocol_noncompliant_passes"] == 1


def test_report_requires_completed_compliant_lifecycles_and_positive_separation():
    blind = build_observation(_detail("blind", "confirmed"))
    standard = build_observation(_detail("standard", "refuted"))

    default_report = build_report("exp-review", [blind, standard])
    assert default_report["policy"]["status"] == "insufficient_evidence"

    report = build_report(
        "exp-review",
        [blind, standard],
        min_tasks_per_arm=1,
        min_validated_outcomes_per_arm=1,
    )
    assert report["policy"]["status"] == "candidate"
    assert report["policy"]["candidate_arm"] == "blind"
    assert report["policy"]["score_margin"] == 2.0

    invalid = copy.deepcopy(blind)
    invalid["review_passes"][0]["experiment_protocol"]["protocol"][
        "protocol_compliant"
    ] = False
    invalid_report = build_report(
        "exp-review",
        [invalid, standard],
        min_tasks_per_arm=1,
        min_validated_outcomes_per_arm=1,
    )
    assert invalid_report["policy"]["status"] == "insufficient_evidence"


def test_control_plane_persists_immutable_assignment_and_delayed_outcome(monkeypatch):
    cp = ControlPlane.in_memory()
    task = cp.create_task("Experiment task", project="demo")

    assignment = cp.assign_review_experiment(
        task.id,
        experiment_id="exp-control",
        arm="standard",
        actor="operator",
    )
    assert assignment["arm"] == "standard"
    assert cp.assign_review_experiment(
        task.id,
        experiment_id="exp-control",
        arm="standard",
        actor="operator",
    ) == assignment
    with pytest.raises(ValidationError, match="immutable"):
        cp.assign_review_experiment(
            task.id,
            experiment_id="exp-control",
            arm="blind",
            actor="operator",
        )

    outcome = cp.record_review_outcome(
        task.id,
        kind="clean_window",
        status="confirmed",
        detail={"window_days": 7},
        actor="operator",
    )
    detail = cp.task_detail(task.id)
    detail["reviews"] = [{"id": "review_1"}]
    observed_subject_ids = []
    monkeypatch.setattr(cp, "task_detail", lambda _task_id: detail)
    monkeypatch.setattr(
        cp,
        "list_observability",
        lambda **kwargs: observed_subject_ids.append(kwargs["subject_id"]) or [],
    )
    observation = cp.review_observation(task.id)
    assert observation["experiment"]["experiment_id"] == "exp-control"
    assert observation["outcomes"][0]["id"] == outcome["id"]
    assert observed_subject_ids == [task.id, "review_review_1"]
    assert any(
        item["event_type"] == "task.review_experiment_assigned"
        for item in cp.task_detail(task.id)["history"]
    )
