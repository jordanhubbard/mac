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


@pytest.fixture(scope="module")
def default_report():
    """Classification of a default (no-signal) candidate, computed once.

    Shared by the structural tests below, which assert on different fields of
    the same report and therefore only need the computation performed once.
    """
    return classify_candidate(_make_candidate())


def test_report_has_required_keys(default_report):
    """Every report must carry the stable JSON shape keys."""
    for key in ("schema", "kind", "scope", "areas", "overall_confidence",
                 "overall_confidence_score", "evidence_count", "redacted"):
        assert key in default_report, "missing key: %s" % key


def test_schema_version_is_classifier(default_report):
    assert default_report["schema"] == CLASSIFIER_SCHEMA


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


@pytest.mark.parametrize(
    "summary",
    [
        pytest.param("curator updated the skill bundle", id="curator"),
        pytest.param(
            "hermes.skill invocation failed during agent startup",
            id="hermes_skill",
        ),
    ],
)
def test_skill_keyword_variant_detected(summary):
    """'curator' and 'hermes.skill' keywords each trigger a named skill area."""
    cand = _make_candidate(summary=summary, evidence_count=1)
    report = classify_candidate(cand)
    skill_areas = [a for a in report["areas"] if a["area_type"] == "skill"]
    assert len(skill_areas) >= 1, "expected at least one skill area"
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


# ---------------------------------------------------------------------------
# Tests for bug fixes (candidate_id, _generic_tool public name, suppression)
# ---------------------------------------------------------------------------


def test_candidate_id_uses_candidate_id_key_first():
    """candidate_id field should prefer the 'candidate_id' key over task_id."""
    cand = _make_candidate(summary="some text", task_id="task_aaa")
    cand["candidate_id"] = "cand_explicit_999"
    report = classify_candidate(cand)
    assert report["candidate_id"] == "cand_explicit_999", (
        "expected candidate_id from 'candidate_id' key, got: %s" % report["candidate_id"]
    )


def test_candidate_id_falls_back_to_task_id_when_no_candidate_id_key():
    """When 'candidate_id' key is absent, candidate_id should fall back to task_id."""
    cand = _make_candidate(summary="some text", task_id="task_bbb")
    assert "candidate_id" not in cand
    report = classify_candidate(cand)
    assert report["candidate_id"] == "task_bbb", (
        "expected task_id fallback, got: %s" % report["candidate_id"]
    )


def test_generic_tool_area_name_is_public_tool_not_internal():
    """_generic_tool internal marker must not leak into public area_name; should be 'tool'."""
    cand = _make_candidate(summary="the tool was invoked during processing")
    report = classify_candidate(cand)
    tool_areas = [a for a in report["areas"] if a["area_type"] == "tool"]
    assert len(tool_areas) >= 1, "expected at least one tool area"
    for area in tool_areas:
        assert area["area_name"] != "_generic_tool", (
            "internal _generic_tool marker leaked into public output"
        )
        assert area["area_name"] == "tool", (
            "expected area_name 'tool' for generic match, got: %s" % area["area_name"]
        )


def test_generic_tool_suppressed_when_specific_tool_present():
    """When a specific tool (e.g. terminal_tool) matches, generic 'tool' must not appear."""
    cand = _make_candidate(summary="terminal_tool executed the command via tool interface")
    report = classify_candidate(cand)
    tool_areas = [a for a in report["areas"] if a["area_type"] == "tool"]
    area_names = [a["area_name"] for a in tool_areas]
    assert "terminal" in area_names, "expected 'terminal' area from terminal_tool"
    assert "tool" not in area_names and "_generic_tool" not in area_names, (
        "generic tool should be suppressed when specific tool present; got: %s" % area_names
    )


# ---------------------------------------------------------------------------
# CARGO_HOME / Rust toolchain patterns (skill pitfall gap)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        pytest.param(
            "cargo provisioning failed: CARGO_HOME not on PATH in sandbox",
            id="cargo_home",
        ),
        pytest.param(
            "rustup install failed during toolchain provisioning step",
            id="rustup",
        ),
        pytest.param(
            "command not found: checked cargo/bin but symlink missing from MAC_TOOLCHAIN_BIN",
            id="cargo_bin_path",
        ),
        pytest.param(
            "rust-toolchain override file not found; falling back to stable",
            id="rust_toolchain",
        ),
    ],
)
def test_rust_toolchain_signal_routes_to_task_executor(summary):
    """Rust/Cargo toolchain signals all route to the mac.task_executor repo area."""
    cand = _make_candidate(summary=summary, evidence_count=1)
    report = classify_candidate(cand)
    repo_areas = [a for a in report["areas"] if a["area_type"] == "repo_area"]
    names = {a["area_name"] for a in repo_areas}
    assert "mac.task_executor" in names, (
        "expected toolchain signal to route to mac.task_executor, got: %s" % names
    )


def test_cargo_home_skill_co_occurrence_detects_both_areas():
    """A finding with both a skill reference and CARGO_HOME should surface both areas."""
    cand = _make_candidate(
        summary="skill_bundle invocation failed: CARGO_HOME not symlinked into sandbox PATH",
        evidence_count=2,
    )
    report = classify_candidate(cand)
    area_types = {a["area_type"] for a in report["areas"]}
    assert "skill" in area_types, "expected skill area from skill_bundle mention"
    repo_names = {a["area_name"] for a in report["areas"] if a["area_type"] == "repo_area"}
    assert "mac.task_executor" in repo_names, (
        "expected CARGO_HOME to also route to mac.task_executor; got repo areas: %s" % repo_names
    )


# ---------------------------------------------------------------------------
# Additional edge-case tests for uncovered branches
# ---------------------------------------------------------------------------


def test_non_mapping_record_type_counts_falls_back_to_evidence_list():
    """When record_type_counts is not a dict, _unique_record_types falls back to evidence list."""
    cand = _make_candidate(evidence_count=2)
    # Override record_type_counts with a non-dict value to hit lines 229-230
    cand["record_type_counts"] = "not-a-dict"
    report = classify_candidate(cand)
    # Should still produce a valid report
    assert report["evidence_count"] == 2


def test_unknown_confidence_string_normalised_to_low():
    """A candidate with an unrecognised confidence value is treated as 'low' (line 318)."""
    cand = _make_candidate(confidence="low")
    cand["confidence"] = "bogus_confidence_level"
    classify_candidate(cand)
    # When no areas match, overall_confidence falls back to 'low'
    cand_no_signals = {
        "schema": "mac.dream.v1",
        "kind": "knowledge_snippet",
        "scope": "agent",
        "confidence": "totally_unknown",
        "summary": "some mundane event",
        "evidence": [],
        "record_type_counts": {},
    }
    report2 = classify_candidate(cand_no_signals)
    assert report2["overall_confidence"] == "low"


def test_confidence_medium_with_single_area_signal():
    """Candidate confidence 'medium' with exactly one signal and no evidence → medium via rule 4 (line 257).

    ``_confidence_for`` is only reached when an area pattern fires.  With
    ev_count=0, signal_count=1, unique_rt=0 the function falls through rules 1-3
    and must hit line 257 (``candidate_confidence == 'medium'``).
    """
    from mac.dream_cycle_classifier import _confidence_for  # noqa: PLC0415

    label, score = _confidence_for(0, 1, "medium", 0)
    assert label == "medium"

    # Also verify through the full classify pipeline: a 'medium' candidate with
    # one area signal and empty evidence list lands at 'medium'.
    cand = {
        "schema": "mac.dream.v1",
        "kind": "knowledge_snippet",
        "scope": "agent",
        "confidence": "medium",
        "summary": "terminal_tool encountered a transient issue",
        "evidence": [],
        "record_type_counts": {},
    }
    report = classify_candidate(cand)
    assert report["overall_confidence"] == "medium"


def test_generic_tool_suppressed_when_also_matched_by_other_area_via_direct_match():
    """_generic_tool entry in hits is deleted when another area fires (line 277).

    This exercises the branch ``if '_generic_tool' in hits and len(hits) > 1``.
    The guard at line 271 checks _TOOL_PATTERNS globally; to exercise line 277
    we call _match_patterns directly with custom patterns where a non-generic
    pattern fires alongside the generic one.
    """
    from mac.dream_cycle_classifier import _match_patterns  # noqa: PLC0415

    # Text matches both the generic 'tool' pattern and a custom specific area.
    # The guard at 271 only checks _TOOL_PATTERNS, so no specific TOOL_PATTERN fires;
    # _generic_tool is added. But 'custom_area' also fires -> hits has two entries.
    # Line 277 then removes _generic_tool, leaving only 'custom_area'.
    patterns = [
        (r"\btool\b", "_generic_tool"),
        (r"\bspecialop\b", "custom_area"),
    ]
    hits = _match_patterns("call some tool for specialop", patterns, "tool")
    area_names = [h["area_name"] for h in hits]
    assert "tool" not in area_names, (
        "_generic_tool should have been suppressed by custom_area; got: %s" % area_names
    )
    assert "custom_area" in area_names


def test_non_string_observations_are_skipped():
    """Non-string observations don't crash and are silently skipped (branches 199->198)."""
    cand = _make_candidate(
        summary="test observation",
        observations=["valid observation", None, 42, {"nested": "dict"}, "another string"],
        evidence_count=1,
    )
    report = classify_candidate(cand)
    assert report["schema"] == CLASSIFIER_SCHEMA


def test_non_string_query_terms_are_skipped():
    """Non-string query_terms are silently skipped (branches 207->206)."""
    cand = _make_candidate(summary="skill_bundle validation")
    cand["retrieval"] = {"query_terms": ["skill_bundle", None, 123]}
    report = classify_candidate(cand)
    assert report["schema"] == CLASSIFIER_SCHEMA


def test_non_string_candidate_values_are_skipped():
    """Non-string summary/kind/scope values are silently skipped (branches 196->194)."""
    cand = {
        "schema": "mac.dream.v1",
        "kind": None,
        "scope": 42,
        "confidence": "low",
        "summary": None,
        "evidence": [],
        "record_type_counts": {},
    }
    report = classify_candidate(cand)
    assert report["schema"] == CLASSIFIER_SCHEMA
