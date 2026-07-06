"""Boundary coverage for review feedback, authorization, and publication."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mac.models import (
    AuthorizationError,
    ReviewStatus,
    TaskState,
    TransitionError,
    ValidationError,
)
from mac.services import ControlPlane


def _review(**extra):
    values = {
        "id": "review",
        "task_id": "task",
        "reviewer_agent_id": "reviewer",
        "status": ReviewStatus.PENDING.value,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def _task(**extra):
    values = {
        "id": "task",
        "state": TaskState.REVIEWING.value,
        "metadata": {},
        "owner_agent_id": None,
        "attempt_count": 0,
        "max_attempts": 3,
    }
    values.update(extra)
    return SimpleNamespace(**values)


def _evidence(**extra):
    values = {
        "id": "evidence",
        "task_id": "task",
        "kind": "publication",
        "checksum": "sha256:" + "a" * 64,
        "metadata": {},
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_request_review_transition_and_existing_review_paths(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    service = cp.reviews
    monkeypatch.setattr(
        service,
        "_get_agent",
        lambda *_a: SimpleNamespace(capabilities={"review"}),
    )
    monkeypatch.setattr(service, "agent_has_owned_task", lambda *_a: False)
    monkeypatch.setattr(service, "latest_executor_evidence_author", lambda *_a: None)
    monkeypatch.setattr(service, "_reviewer_independence_check", None)
    monkeypatch.setattr(service, "_reviewer_fallback_check", None)
    monkeypatch.setattr(service, "_get_task", lambda *_a: _task(state=TaskState.OPEN.value))
    with pytest.raises(TransitionError, match="must need review"):
        service.request_review("task", "reviewer")
    monkeypatch.setattr(service, "agent_has_owned_task", lambda *_a: True)
    with pytest.raises(AuthorizationError, match="own"):
        service.request_review("task", "reviewer")


def test_submit_review_validation_and_verdict_finder_edges(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    service = cp.reviews
    monkeypatch.setattr(
        service, "current_review_target_evidence_id", lambda *_a: "executor"
    )
    monkeypatch.setattr(service, "get_review", lambda *_a: _review())
    with pytest.raises(AuthorizationError):
        service.submit_review("review", "approved", "other", evidence_id="e")
    monkeypatch.setattr(service, "get_review", lambda *_a: _review(status="approved"))
    with pytest.raises(ValidationError, match="already completed"):
        service.submit_review("review", "approved", "reviewer", evidence_id="e")
    monkeypatch.setattr(service, "get_review", lambda *_a: _review())
    with pytest.raises(ValidationError, match="unsupported"):
        service.submit_review("review", "unknown", "reviewer")
    with pytest.raises(ValidationError, match="requires an evidence"):
        service.submit_review("review", "approved", "reviewer")
    monkeypatch.setattr(service, "_get_evidence", lambda *_a: _evidence(task_id="other"))
    with pytest.raises(ValidationError, match="belong"):
        service.submit_review("review", "approved", "reviewer", evidence_id="e")

    service._find_verdict_evidence = lambda *_a, **_k: (None, ["bad signature"])
    monkeypatch.setattr(service, "_get_evidence", lambda *_a: _evidence(metadata={"verification": {}}))
    with pytest.raises(ValidationError, match="missing verification"):
        service.submit_review("review", "approved", "reviewer", evidence_id="e")
    verdict_manifest = {"reviewed_evidence_id": "executor", "llm": {"model": "same"}}
    verdict = _evidence(metadata={"verification": verdict_manifest})
    monkeypatch.setattr(service, "_get_evidence", lambda evidence_id: verdict if evidence_id == "e" else _evidence(metadata={"verification": {"llm": {"model": "same"}, "agent_generated": True}}))
    with pytest.raises(ValidationError, match="signed review_verdict"):
        service.submit_review("review", "approved", "reviewer", evidence_id="e")
    service._find_verdict_evidence = lambda *_a, **_k: (verdict, [])
    with pytest.raises(ValidationError, match="different reviewer LLM"):
        service.submit_review("review", "approved", "reviewer", evidence_id="e")


def test_review_and_publication_list_limit_paths(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    service = cp.reviews
    assert service.list_reviews("task", limit=0) == []
    rows = [{"id": "one"}, {"id": "two"}]
    monkeypatch.setattr(service.store, "query_all", lambda *_a, **_k: rows)
    monkeypatch.setattr(service, "_review_from_row", lambda row: row["id"])
    assert service.list_reviews("task", limit=2) == ["two", "one"]
    assert service.list_publications(limit=0) == []
    monkeypatch.setattr(service, "_get_task", lambda *_a: _task())
    monkeypatch.setattr(service, "_publication_from_row", lambda row: row["id"])
    assert service.list_publications(task_id="task") == ["one", "two"]
    assert service.list_publications(task_id="task", limit=2) == ["two", "one"]
    assert service.list_publications() == ["one", "two"]
    assert service.list_publications(limit=2) == ["two", "one"]


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (_evidence(task_id="other"), "belong"),
        (_evidence(kind="test"), "requires publication evidence"),
        (_evidence(checksum=""), "requires a checksum"),
        (_evidence(checksum="garbage"), "checksum must be"),
        (_evidence(checksum="md5:" + "a" * 32), "checksum must be"),
        (_evidence(checksum="sha256:xyz"), "checksum must be"),
    ],
)
def test_publish_task_evidence_validation(monkeypatch, evidence, message) -> None:
    cp = ControlPlane.in_memory()
    service = cp.reviews
    task = _task(metadata={"policy": {"require_publication_evidence": True}})
    monkeypatch.setattr(service, "_get_task", lambda *_a: task)
    monkeypatch.setattr(service, "completion_authorized", lambda *_a: True)
    monkeypatch.setattr(service, "_get_evidence", lambda *_a: evidence)
    with pytest.raises(ValidationError, match=message):
        service.publish_task("task", "target", "actor", evidence_id="e")


def test_publish_task_state_authorization_and_required_evidence(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    service = cp.reviews
    monkeypatch.setattr(service, "_get_task", lambda *_a: _task(state=TaskState.OPEN.value))
    with pytest.raises(TransitionError, match="in review"):
        service.publish_task("task", "target", "actor")
    monkeypatch.setattr(service, "_get_task", lambda *_a: _task())
    monkeypatch.setattr(service, "completion_authorized", lambda *_a: False)
    with pytest.raises(ValidationError, match="approved review"):
        service.publish_task("task", "target", "actor")
    monkeypatch.setattr(service, "completion_authorized", lambda *_a: True)
    monkeypatch.setattr(service, "task_requires_publication_evidence", lambda *_a: True)
    with pytest.raises(ValidationError, match="requires publication evidence"):
        service.publish_task("task", "target", "actor")


def test_owner_and_latest_executor_evidence_helpers(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    service = cp.reviews
    monkeypatch.setattr(service, "_get_task", lambda *_a: _task(owner_agent_id="agent"))
    assert service.agent_has_owned_task("task", "agent") is True
    assert service.agent_is_current_owner_or_latest_evidence_author("task", "agent") is True
    monkeypatch.setattr(service, "_get_task", lambda *_a: _task())
    monkeypatch.setattr(service.store, "query_one", lambda *_a, **_k: {"found": 1})
    assert service.agent_has_owned_task("task", "agent") is True
    monkeypatch.setattr(service.store, "query_all", lambda *_a, **_k: [
        {"created_by": "reviewer", "metadata": json.dumps({"verification": {"evidence_type": "review_verdict"}})},
        {"created_by": "executor", "metadata": "not-json"},
    ])
    assert service.latest_executor_evidence_author("task") == "executor"
    monkeypatch.setattr(service.store, "query_all", lambda *_a, **_k: [])
    assert service.latest_executor_evidence_author("task") is None


def test_feedback_bounding_and_manifest_shapes() -> None:
    service = ControlPlane.in_memory().reviews
    assert service._bounded_review_findings("bad") == []
    findings = service._bounded_review_findings([
        "bad",
        {"severity": "x" * 100, "path": "p" * 600, "line": "bad", "message": "m" * 3000},
    ])
    assert len(findings) == 1
    assert len(findings[0]["severity"]) == 64
    assert findings[0]["line"] is None
    huge = {
        "feedback": "f" * 50000,
        "summary": "s" * 10000,
        "findings": [{"message": "m" * 10000}] * 20,
    }
    block = service._bounded_review_feedback_block(huge, [huge] * 10)
    assert block["history"] == []
    assert "truncated" in block["latest"]["feedback"]
    assert service._review_feedback_from_evidence(_review(), None) is None
    service._get_evidence = lambda *_a: _evidence(metadata={})
    assert service._review_feedback_from_evidence(_review(), "e") is None


def test_publication_policy_requires_dictionary() -> None:
    service = ControlPlane.in_memory().reviews
    assert service.task_requires_publication_evidence(_task(metadata={"policy": "bad"})) is False
    assert service.task_requires_publication_evidence(
        _task(metadata={"policy": {"publication_evidence_required": True}})
    ) is True
