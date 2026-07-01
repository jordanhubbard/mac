from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from mac import nap_consolidator as nap
from mac.models import ValidationError
from mac.services import ControlPlane


def _record(cp: ControlPlane, content: str, *, agent: str = "agent-1", **kwargs: Any):
    return cp.add_memory(
        task_id=kwargs.get("task_id"),
        subject_type=kwargs.get("subject_type", "topic"),
        subject_id=kwargs.get("subject_id"),
        record_type=kwargs.get("record_type", "note"),
        content=content,
        evidence_id=None,
        created_by=agent,
    )


def test_pure_summary_observation_and_classification_edges() -> None:
    cp = ControlPlane.in_memory()
    plain = _record(cp, "tool command worked")
    deployment = _record(
        cp,
        json.dumps(
            {
                "schema": "mac.deployment_learning.v1",
                "outcome": "failed",
                "task_title": "Deploy",
                "evidence_type": "test",
                "error_signature": "timeout",
            }
        ),
        record_type="deployment_learning:mac",
    )
    non_object = _record(cp, "[]")

    assert nap._default_summarizer([], {}) == ""
    assert nap._record_payload(non_object) == {}
    assert nap._record_observation(deployment) == "[failed] Deploy (test) failed with timeout"
    assert nap._dream_kind([deployment]) == "failure_pattern"
    assert nap._dream_kind([plain]) == "tool_pattern"
    assert nap._confidence_for_records([plain, deployment, non_object])[0] == "high"
    assert nap._default_dreamer([], {}) == []


def test_consolidate_validation_blank_summary_and_disabled_dreams() -> None:
    cp = ControlPlane.in_memory()
    _record(cp, "fact")
    service = nap.NapConsolidatorService(
        store=cp.store,
        memory=cp.memory,
        summarizer_fn=lambda _records, _context: "   ",
    )
    with pytest.raises(ValidationError, match="agent_id"):
        service.consolidate_agent(" ")

    result = service.consolidate_agent(
        "agent-1", embed_into_medium=False, emit_dream_artifacts=False
    )
    assert result["summaries_written"] == 0
    assert result["dream_artifacts_written"] == 0


def test_consolidate_records_summary_and_dream_embedding_errors() -> None:
    cp = ControlPlane.in_memory()
    _record(cp, "important fact")

    class BrokenWriter:
        def embed_memory(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("vector unavailable")

    result = nap.NapConsolidatorService(
        store=cp.store, memory=cp.memory, vector_writer=BrokenWriter()
    ).consolidate_agent("agent-1")
    assert {item["phase"] for item in result["errors"]} == {
        "embed_summary",
        "embed_dream",
    }


def test_consolidate_records_summarizer_and_dreamer_errors() -> None:
    cp = ControlPlane.in_memory()
    _record(cp, "fact")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("generation failed")

    result = nap.NapConsolidatorService(
        store=cp.store,
        memory=cp.memory,
        summarizer_fn=fail,
        dreamer_fn=fail,
    ).consolidate_agent("agent-1", embed_into_medium=False)
    assert {item["phase"] for item in result["errors"]} == {
        "summarize_or_write_summary",
        "dream_or_write_artifacts",
    }


def test_dream_candidate_validation_and_normalization_edges() -> None:
    cp = ControlPlane.in_memory()
    record = _record(cp, "knowledge")
    context = {
        "agent_id": "agent-1",
        "project": "mac",
        "task_id": None,
        "nap_run_id": "nap-1",
        "window_start": "start",
        "window_end": "end",
    }
    service = nap.NapConsolidatorService(store=cp.store, memory=cp.memory)
    assert service._normalize_dream_candidate({}, [record], context) is None

    for candidate, match in [
        ({"summary": "x", "kind": "unknown"}, "kind"),
        ({"summary": "x", "scope": "unknown"}, "scope"),
        ({"summary": "x", "confidence": "certain"}, "confidence"),
    ]:
        with pytest.raises(ValidationError, match=match):
            service._normalize_dream_candidate(candidate, [record], context)

    artifact = service._normalize_dream_candidate(
        {
            "summary": "normalized",
            "scope": "fleet",
            "confidence": "medium",
            "retrieval": "invalid",
            "confidence_score": 2,
            "observations": ["one"],
        },
        [record],
        context,
    )
    assert artifact is not None
    assert artifact["confidence_score"] == 1.0
    assert artifact["retrieval"]["query_terms"]
    assert artifact["observations"] == ["one"]
    assert service._dream_subject_id(artifact) == "fleet"
    default_score = service._normalize_dream_candidate(
        {"summary": "default confidence"}, [record], context
    )
    assert default_score is not None
    assert default_score["confidence_score"] == pytest.approx(0.35)


def test_dreamer_contract_grouping_and_missing_project_edges() -> None:
    cp = ControlPlane.in_memory()
    project_record = _record(
        cp, "ambient", subject_type="project", subject_id="mac"
    )
    missing_task_record = replace(project_record, task_id="missing-task")
    service = nap.NapConsolidatorService(store=cp.store, memory=cp.memory)
    assert (None, "mac") in service._group_records([project_record])
    assert service._project_for_task("missing-task") is None
    assert ("missing-task", None) in service._group_records([missing_task_record])

    no_candidates = nap.NapConsolidatorService(
        store=cp.store, memory=cp.memory, dreamer_fn=lambda *_a: []
    )
    assert no_candidates._normalized_dream_artifacts([project_record], {}) == []

    invalid_list = nap.NapConsolidatorService(
        store=cp.store, memory=cp.memory, dreamer_fn=lambda *_a: {"summary": "x"}
    )
    with pytest.raises(ValidationError, match="must return a list"):
        invalid_list._normalized_dream_artifacts([project_record], {})

    invalid_item = nap.NapConsolidatorService(
        store=cp.store, memory=cp.memory, dreamer_fn=lambda *_a: ["bad"]
    )
    with pytest.raises(ValidationError, match="must be an object"):
        invalid_item._normalized_dream_artifacts([project_record], {})
