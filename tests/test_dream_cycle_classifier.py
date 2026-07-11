"""dream-classifier: tests for mac.dream_cycle_classifier (mem-11).

Covers three canonical signal categories from the task spec:

  * Ambiguous: candidate has a single evidence record and minimal signal
    text → overall confidence is "low", areas list is empty or sparse.
  * Single-signal: one specific tool or provider appears → single "low"
    or "medium" area entry depending on evidence count.
  * High-confidence: three or more evidence records plus multiple distinct
    signal types → areas all land at "high".

All tests are pure-Python (no ControlPlane, no SQLite, no Qdrant) so they
run in any environment including the hermetic contract-test sandbox.
"""
from __future__ import annotations

import json

import pytest

from mac.dream_cycle_classifier import (
    CLASSIFIER_SCHEMA,
    CONFIDENCE_THRESHOLDS,
    classify_candidate,
    classify_candidates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    kind: str = "knowledge_snippet",
    scope: str = "agent",
    confidence: str = "low",
    summary: str = "some observation",
    observations: list | None = None,
    evidence_count: int = 1,
    record_type_counts: dict | None = None,
    task_id: str | None = None,
) -> dict:
    evidence = [
        {"memory_id": "mem_%d" % i, "record_type": "note", "task_id": task_id}
        for i in range(evidence_count)
    ]
    return {
        "schema": "mac.dream.v1",
        "kind": kind,
        "scope": scope,
        "confidence": confidence,
        "confidence_score": CONFIDENCE_THRESHOLDS[confidence][1],
        "summary": summary,
        "observations": observations or [],
        "record_type_counts": record_type_counts or {"note": evidence_count},
        "task_id": task_id,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Schema / structural tests
# ---------------------------------------------------------------------------


def test_report_has_required_keys():
    """Every report must carry the stable JSON shape keys."""
    cand = _make_candidate()
    report = classify_candidate(cand)
    for key in ("schema", "kind", "scope", "areas", "overall_confidence",
                 "overall_confidence_score", "evidence_count", "redacted"):
        assert key in report, "missing key: %s" % key


def test_schema_version_is_classifier():
    report = classify_candidate(_make_candidate())
    assert report["schema"] == CLASSIFIER_SCHEMA


def test_report_is_json_serialisable():
    """The report must survive a json.dumps round-trip."""
    cand = _make_candidate(summary="tool_pattern failure in terminal_tool")
    report = classify_candidate(cand)
    blob = json.dumps(report)
    restored = json.loads(blob)
    assert restored["schema"] == CLASSIFIER_SCHEMA


def test_kind_and_scope_propagated():
    cand = _make_candidate(kind="failure_pattern", scope="project")
    report = classify_candidate(cand)
    assert report["kind"] == "failure_pattern"
    assert report["scope"] == "project"


# ---------------------------------------------------------------------------
# Ambiguous / low-signal tests
# ---------------------------------------------------------------------------


def test_ambiguous_empty_summary_yields_low_confidence():
    """A candidate with no distinguishing signal stays at low confidence."""
    cand = _make_candidate(summary="generic agent work", evidence_count=1)
    report = classify_candidate(cand)
    assert report["overall_confidence"] == "low"
    assert report["evidence_count"] == 1


def test_ambiguous_areas_may_be_empty():
    """With no keyword matches the areas list is empty, not an error."""
    cand = _make_candidate(summary="nothing special happened", evidence_count=1)
    report = classify_candidate(cand)
    # areas is a list (possibly empty)
    assert isinstance(report["areas"], list)


def test_ambiguous_single_evidence_no_keywords():
    cand = _make_candidate(
        summary="agent completed some work",
        evidence_count=1,
        confidence="low",
    )
    report = classify_candidate(cand)
    assert report["overall_confidence"] == "low"
    assert report["overall_confidence_score"] == pytest.approx(0.35, abs=1e-9)


# ---------------------------------------------------------------------------
# Single-signal tests
# ---------------------------------------------------------------------------


def test_single_signal_tool_reference_detected():
    """A summary mentioning terminal_tool should produce a tool area hit."""
    cand = _make_candidate(
        summary="agent called terminal_tool to run a command",
        evidence_count=1,
        confidence="low",
    )
    report = classify_candidate(cand)
    tool_areas = [a for a in report["areas"] if a["area_type"] == "tool"]
    assert len(tool_areas) >= 1
    names = {a["area_name"] for a in tool_areas}
    assert "terminal" in names


def test_single_signal_provider_reference_detected():
    """A summary mentioning openai should produce a provider area hit."""
    cand = _make_candidate(
        summary="the openai rate limit was hit during inference",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    provider_areas = [a for a in report["areas"] if a["area_type"] == "provider"]
    assert len(provider_areas) >= 1
    names = {a["area_name"] for a in provider_areas}
    assert "openai" in names


def test_single_signal_skill_reference_detected():
    """'skill' keyword in the summary triggers a skill area."""
    cand = _make_candidate(
        summary="the skill bundle failed to load on the agent",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1


def test_single_signal_repo_area_detected():
    """A module path like mac.services in the summary triggers a repo_area hit."""
    cand = _make_candidate(
        summary="exception in mac.services during task lifecycle transition",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    repo_areas = [a for a in report["areas"] if a["area_type"] == "repo_area"]
    assert len(repo_areas) >= 1
    names = {a["area_name"] for a in repo_areas}
    assert "mac.services" in names


def test_single_signal_with_two_evidence_upgrades_to_medium():
    """Two evidence records → medium confidence even for a single-type signal."""
    cand = _make_candidate(
        summary="web_search returned no results",
        evidence_count=2,
        confidence="low",
    )
    report = classify_candidate(cand)
    web_areas = [
        a for a in report["areas"]
        if a["area_type"] == "tool" and a["area_name"] in ("web_search", "web")
    ]
    assert len(web_areas) >= 1
    for area in web_areas:
        assert area["confidence"] == "medium"
        assert area["confidence_score"] == pytest.approx(0.65, abs=1e-9)


# ---------------------------------------------------------------------------
# High-confidence tests
# ---------------------------------------------------------------------------


def test_high_confidence_with_three_evidence_records():
    """Three or more evidence records always produces high confidence."""
    cand = _make_candidate(
        summary="anthropic provider timeout in terminal_tool execution",
        evidence_count=3,
        confidence="medium",
    )
    report = classify_candidate(cand)
    assert report["overall_confidence"] == "high"
    assert report["overall_confidence_score"] == pytest.approx(0.90, abs=1e-9)
    for area in report["areas"]:
        assert area["confidence"] == "high"


def test_high_confidence_candidate_plus_multi_signal_types():
    """Candidate marked high + multiple signal types = high even without 3 evidence."""
    cand = _make_candidate(
        summary="skill_bundle and terminal_tool both failed; curator also hit an error",
        evidence_count=2,
        confidence="high",
        record_type_counts={"note": 1, "deployment_learning": 1},
    )
    report = classify_candidate(cand)
    assert report["overall_confidence"] == "high"


def test_high_confidence_multiple_providers_detected():
    """Multiple providers in one artifact should each get their own area entry."""
    cand = _make_candidate(
        summary="openai rate limit fallback to anthropic; qdrant vector write failed",
        evidence_count=3,
    )
    report = classify_candidate(cand)
    provider_areas = [a for a in report["areas"] if a["area_type"] == "provider"]
    names = {a["area_name"] for a in provider_areas}
    assert "openai" in names
    assert "anthropic" in names
    assert "qdrant" in names


def test_high_confidence_failure_pattern_with_repo_area():
    """Failure pattern kind + repo path + 3 evidence → all areas high."""
    cand = _make_candidate(
        kind="failure_pattern",
        summary="mac.gitops raised an auth error during fleet deployment",
        observations=["git push failed", "GH_TOKEN missing"],
        evidence_count=3,
        confidence="medium",
        record_type_counts={"deployment_learning": 2, "note": 1},
    )
    report = classify_candidate(cand)
    assert report["overall_confidence"] == "high"
    repo_areas = [a for a in report["areas"] if a["area_type"] == "repo_area"]
    assert len(repo_areas) >= 1


# ---------------------------------------------------------------------------
# Redaction transparency
# ---------------------------------------------------------------------------


def test_redacted_token_sets_flag():
    """<redacted> in any text field → redacted=True in the report."""
    cand = _make_candidate(
        summary="API call failed with token=<redacted> in the request header"
    )
    report = classify_candidate(cand)
    assert report["redacted"] is True


def test_clean_candidate_has_redacted_false():
    cand = _make_candidate(summary="everything worked fine")
    report = classify_candidate(cand)
    assert report["redacted"] is False


def test_redacted_does_not_crash_classification():
    """Even a fully redacted summary must produce a valid report."""
    cand = _make_candidate(summary="<redacted>")
    report = classify_candidate(cand)
    assert report["schema"] == CLASSIFIER_SCHEMA
    assert isinstance(report["areas"], list)


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------


def test_classify_candidates_returns_one_report_per_input():
    candidates = [
        _make_candidate(summary="openai failure"),
        _make_candidate(summary="terminal_tool ok", evidence_count=3),
        _make_candidate(summary="nothing notable"),
    ]
    reports = classify_candidates(candidates)
    assert len(reports) == 3
    assert all(r["schema"] == CLASSIFIER_SCHEMA for r in reports)


def test_classify_candidates_empty_list():
    assert classify_candidates([]) == []


# ---------------------------------------------------------------------------
# Evidence count extraction
# ---------------------------------------------------------------------------


def test_evidence_count_from_evidence_list():
    """evidence list length is used as the primary evidence count."""
    cand = _make_candidate(evidence_count=5)
    report = classify_candidate(cand)
    assert report["evidence_count"] == 5


def test_evidence_count_fallback_to_record_type_counts():
    """When evidence list is absent, sum of record_type_counts is used."""
    cand = {
        "schema": "mac.dream.v1",
        "kind": "knowledge_snippet",
        "scope": "agent",
        "confidence": "low",
        "summary": "observation",
        "record_type_counts": {"note": 2, "deployment_learning": 1},
    }
    report = classify_candidate(cand)
    assert report["evidence_count"] == 3


# ---------------------------------------------------------------------------
# Observations field is mined for signals
# ---------------------------------------------------------------------------


def test_observations_contribute_to_area_detection():
    """Signal in observations, not just summary, should be detected."""
    cand = {
        "schema": "mac.dream.v1",
        "kind": "tool_pattern",
        "scope": "agent",
        "confidence": "low",
        "summary": "tool usage observed",
        "observations": ["agent used web_search to fetch content"],
        "evidence": [{"memory_id": "m1", "record_type": "note"}],
    }
    report = classify_candidate(cand)
    tool_areas = [a for a in report["areas"] if a["area_type"] == "tool"]
    names = {a["area_name"] for a in tool_areas}
    assert "web_search" in names or "web" in names


# ---------------------------------------------------------------------------
# Area entry structure
# ---------------------------------------------------------------------------


def test_area_entry_has_required_keys():
    """Each area dict must have all required keys."""
    cand = _make_candidate(
        summary="openai timeout with 3 retries",
        evidence_count=3,
    )
    report = classify_candidate(cand)
    required = {"area_type", "area_name", "confidence", "confidence_score",
                 "signals", "evidence_count"}
    for area in report["areas"]:
        missing = required - set(area.keys())
        assert not missing, "area missing keys: %s" % missing


def test_area_confidence_score_is_float():
    cand = _make_candidate(summary="anthropic rate limit", evidence_count=1)
    report = classify_candidate(cand)
    for area in report["areas"]:
        assert isinstance(area["confidence_score"], float)


def test_area_signals_is_non_empty_list():
    cand = _make_candidate(summary="slack message delivery failed", evidence_count=1)
    report = classify_candidate(cand)
    provider_areas = [a for a in report["areas"] if a["area_type"] == "provider"]
    for area in provider_areas:
        assert isinstance(area["signals"], list)
        assert len(area["signals"]) >= 1


# ---------------------------------------------------------------------------
# Skill-area classification tests (mem-11 repair coverage)
# ---------------------------------------------------------------------------


def test_skill_curator_pattern_detected():
    """Candidate with 'curator' keyword triggers a skill area."""
    cand = _make_candidate(
        summary="curator updated the skill bundle",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1, "expected at least one skill area for 'curator'"
    area_names = {a["area_name"] for a in skill_areas}
    assert "skill" in area_names


def test_skill_hermes_skill_pattern_detected():
    """Candidate with 'hermes.skill' triggers a skill area."""
    cand = _make_candidate(
        summary="hermes.skill invocation failed during agent startup",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1, "expected at least one skill area for 'hermes.skill'"
    area_names = {a["area_name"] for a in skill_areas}
    assert "skill" in area_names


def test_skill_command_and_utils_patterns_detected():
    """skill_command and skill_utils keywords each produce skill area signals."""
    cand = _make_candidate(
        summary="skill_command registered but skill_utils import failed",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1, "expected skill areas for skill_command / skill_utils"
    combined_signals = []
    for area in skill_areas:
        combined_signals.extend(area["signals"])
    assert any("skill[_-]command" in s for s in combined_signals), \
        "skill_command pattern not in signals"
    assert any("skill[_-]utils" in s for s in combined_signals), \
        "skill_utils pattern not in signals"


def test_skill_area_medium_confidence_with_two_evidence():
    """skill keyword + 2 evidence records upgrades the skill area to medium confidence."""
    cand = _make_candidate(
        summary="skill usage observed during nap consolidation",
        evidence_count=2,
        confidence="low",
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1, "expected a skill area"
    for area in skill_areas:
        assert area["confidence"] == "medium", \
            "expected medium confidence with 2 evidence records, got: %s" % area["confidence"]
        assert area["confidence_score"] == pytest.approx(0.65, abs=1e-9)


def test_skill_area_high_confidence_with_three_evidence():
    """skill keyword + 3 evidence records yields high confidence."""
    cand = _make_candidate(
        summary="skill invocation traced across three sessions",
        evidence_count=3,
        confidence="low",
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1, "expected a skill area"
    for area in skill_areas:
        assert area["confidence"] == "high", \
            "expected high confidence with 3 evidence records, got: %s" % area["confidence"]
        assert area["confidence_score"] == pytest.approx(0.90, abs=1e-9)
    assert report["overall_confidence"] == "high"


def test_skill_and_tool_co_occurrence():
    """Candidate with both skill and tool keywords produces both area_type=skill and area_type=tool entries."""
    cand = _make_candidate(
        summary="skill bundle loaded then terminal_tool executed a command",
        evidence_count=1,
    )
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    tool_areas = [a for a in report["areas"] if a["area_type"] == "tool"]
    assert len(skill_areas) >= 1, "expected at least one skill area"
    assert len(tool_areas) >= 1, "expected at least one tool area"
