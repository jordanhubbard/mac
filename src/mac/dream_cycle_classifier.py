"""Dream-cycle candidate classifier (mem-11).

Maps scanner candidates — raw dream-cycle artifacts emitted by the nap
consolidator — to one or more affected *areas*:

  skill        A Hermes skill referenced by name in the artifact content.
  tool         A Hermes tool (terminal, file, web, …) mentioned in the artifact.
  provider     An LLM provider or integration (openai, anthropic, qdrant, …).
  repo_area    A source-tree path segment or module area (e.g. ``mac.services``).

For each matched area the classifier also assigns a confidence level using
deterministic thresholds:

  low      Signal is present in the artifact text, but only a single evidence
           record backs it.  Score ≈ 0.35.
  medium   Two independent evidence records or two distinct signal types
           agree.  Score ≈ 0.65.
  high     Three or more independent evidence records, or the artifact's own
           ``confidence`` field is already "high" *and* at least two distinct
           signal types agree.  Score ≈ 0.90.

The thresholds deliberately mirror ``_CONFIDENCE_SCORES`` in
``mac.nap_consolidator`` so operators see a consistent vocabulary across the
whole dream pipeline.

Stable JSON report shape
------------------------
``classify_candidate(candidate)`` always returns::

    {
        "schema": "mac.dream_classifier.v1",
        "candidate_id": str | None,
        "kind": str,               # from the incoming artifact
        "scope": str,              # from the incoming artifact
        "areas": [
            {
                "area_type": "skill" | "tool" | "provider" | "repo_area",
                "area_name": str,
                "confidence": "low" | "medium" | "high",
                "confidence_score": float,
                "signals": [str],  # which keyword patterns matched
                "evidence_count": int,
            },
            ...
        ],
        "overall_confidence": "low" | "medium" | "high",
        "overall_confidence_score": float,
        "evidence_count": int,
        "redacted": bool,          # True if any token was redacted
    }

Redaction transparency
----------------------
The classifier never tries to strip <redacted> tokens — it surfaces
``redacted=True`` in the report so downstream repair stages can flag the
artifact for manual review without losing the full signal.

Usage
-----
::

    from mac.dream_cycle_classifier import classify_candidate

    report = classify_candidate(artifact_dict)
    for area in report["areas"]:
        if area["confidence"] == "high":
            ...

The module is pure-Python, has no runtime dependencies beyond the stdlib, and
imports nothing from ``mac.services`` — making it safe to call from any stage
of the pipeline including the task-filing step that runs before the hub is
available.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Public constants — callers may import these to stay in sync.
# ---------------------------------------------------------------------------

CLASSIFIER_SCHEMA = "mac.dream_classifier.v1"

# Threshold table: name → (label, score).  Mirrors nap_consolidator._CONFIDENCE_SCORES.
CONFIDENCE_THRESHOLDS: Dict[str, Tuple[str, float]] = {
    "low": ("low", 0.35),
    "medium": ("medium", 0.65),
    "high": ("high", 0.90),
}

# Canonical area types.
AREA_TYPES = frozenset({"skill", "tool", "provider", "repo_area"})

# ---------------------------------------------------------------------------
# Signal tables
# ---------------------------------------------------------------------------

# Each entry: (pattern_re_str, area_type, canonical_area_name)
# Patterns are case-insensitive and matched against the combined text formed
# by summary + observations + record_type_counts keys.
_SKILL_PATTERNS: List[Tuple[str, str]] = [
    (r"\bskill[s]?\b", "skill"),
    (r"\bhermes.skill\b", "skill"),
    (r"\bskill[_-]bundle\b", "skill"),
    (r"\bskill[_-]command\b", "skill"),
    (r"\bskill[_-]utils?\b", "skill"),
    (r"\bskill[_-]preprocessing\b", "skill"),
    (r"\bcurator\b", "skill"),
]

_TOOL_PATTERNS: List[Tuple[str, str]] = [
    (r"\bterminal[_\s]?tool\b", "terminal"),
    (r"\bfile[_\s]?tool\b", "file"),
    (r"\bweb[_\s]?tool\b", "web"),
    (r"\bweb[_\s]?search\b", "web_search"),
    (r"\bvision[_\s]?tool\b", "vision"),
    (r"\btts[_\s]?tool\b", "tts"),
    (r"\bmemory[_\s]?tool\b", "memory"),
    (r"\bskill[_\s]?manager[_\s]?tool\b", "skill_manager"),
    (r"\bdelegate[_\s]?tool\b", "delegate"),
    (r"\bimage[_\s]?gen\b", "image_gen"),
    (r"\bkanban[_\s]?tool\b", "kanban"),
    (r"\bfleet[_\s]?tool\b", "fleet"),
    (r"\bcode[_\s]?exec\b", "code_exec"),
    (r"\bbrowser[_\s]?tool\b", "browser"),
    # Generic tool reference — only counts as a signal if a specific tool
    # name isn't present; handled as fallback below.
    (r"\btool\b", "_generic_tool"),
]

_PROVIDER_PATTERNS: List[Tuple[str, str]] = [
    (r"\bopenai\b", "openai"),
    (r"\banthropic\b", "anthropic"),
    (r"\bgemini\b", "gemini"),
    (r"\bbedrock\b", "bedrock"),
    (r"\bopenrouter\b", "openrouter"),
    (r"\bqdrant\b", "qdrant"),
    (r"\blm[_\s]?studio\b", "lm_studio"),
    (r"\bxai\b", "xai"),
    (r"\bnvidia\b", "nvidia"),
    (r"\btokenhub\b", "tokenhub"),
    (r"\bslack\b", "slack"),
    (r"\btelegram\b", "telegram"),
    (r"\bdiscord\b", "discord"),
    (r"\bfeishu\b", "feishu"),
    (r"\bcohere\b", "cohere"),
    (r"\bmistral\b", "mistral"),
    (r"\bfal\b", "fal"),
]

# Source tree path segments — match module paths that appear in stack traces,
# error messages, or record_type content.
_REPO_AREA_PATTERNS: List[Tuple[str, str]] = [
    (r"\bmac\.services\b", "mac.services"),
    (r"\bmac\.nap_consolidator\b", "mac.nap_consolidator"),
    (r"\bmac\.models\b", "mac.models"),
    (r"\bmac\.worker\b", "mac.worker"),
    (r"\bmac\.task_executor\b", "mac.task_executor"),
    (r"\bmac\.gitops\b", "mac.gitops"),
    (r"\bmac\.fleet_\w+", "mac.fleet"),
    (r"\bmac\.k8s\b", "mac.k8s"),
    (r"\bmac\.acp\b", "mac.acp"),
    (r"\bmac\.a2a\b", "mac.a2a"),
    (r"\bmac\.router\b", "mac.router"),
    (r"\bmac\.memory_service\b", "mac.memory_service"),
    (r"\bmac\.vector_writer\b", "mac.vector_writer_service"),
    (r"\bmac\.review_\w+", "mac.review"),
    (r"\bmac\.codegraph\b", "mac.codegraph_audit"),
    (r"\bsrc/mac\b", "mac"),
    (r"\btests/\w+", "tests"),
    (r"\bscripts/\w+", "scripts"),
    # Generic import path
    (r"\bmac\._hermes\b", "mac._hermes"),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_REDACTED_RE = re.compile(r"<redacted>", re.IGNORECASE)


def _combined_text(candidate: Dict[str, Any]) -> str:
    """Produce a single lowercase string for pattern matching."""
    parts: List[str] = []
    for key in ("summary", "kind", "scope"):
        val = candidate.get(key)
        if isinstance(val, str):
            parts.append(val)
    for obs in (candidate.get("observations") or []):
        if isinstance(obs, str):
            parts.append(obs)
    # record_type keys carry semantic signal
    rtc = candidate.get("record_type_counts") or {}
    parts.extend(str(k) for k in rtc)
    # Also mine the retrieval query_terms for area signals
    retrieval = candidate.get("retrieval") or {}
    for term in (retrieval.get("query_terms") or []):
        if isinstance(term, str):
            parts.append(term)
    return " ".join(parts)


def _evidence_count(candidate: Dict[str, Any]) -> int:
    """Return number of evidence records attached to the candidate."""
    evidence = candidate.get("evidence")
    if isinstance(evidence, list):
        return len(evidence)
    # Fallback: check record_type_counts
    rtc = candidate.get("record_type_counts") or {}
    if isinstance(rtc, dict) and rtc:
        return sum(rtc.values())
    return 0


def _unique_record_types(candidate: Dict[str, Any]) -> int:
    """Return number of distinct evidence record types (signal diversity)."""
    rtc = candidate.get("record_type_counts") or {}
    if isinstance(rtc, dict):
        return len(rtc)
    evidence = candidate.get("evidence") or []
    return len({e.get("record_type") for e in evidence if isinstance(e, dict) and e.get("record_type")})


def _confidence_for(
    evidence_count: int,
    signal_count: int,
    candidate_confidence: str,
    unique_record_types: int,
) -> Tuple[str, float]:
    """Assign confidence label and score deterministically.

    Rules (applied in descending priority):
    1. Three-or-more evidence records  → high
    2. Candidate already carries "high" *and* ≥ 2 distinct signal types → high
    3. Two evidence records, or two distinct signal types → medium
    4. Candidate carries "medium" → medium
    5. Otherwise → low
    """
    if evidence_count >= 3:
        return CONFIDENCE_THRESHOLDS["high"]
    if candidate_confidence == "high" and signal_count >= 2 and unique_record_types >= 1:
        return CONFIDENCE_THRESHOLDS["high"]
    if evidence_count == 2:
        return CONFIDENCE_THRESHOLDS["medium"]
    if signal_count >= 2 or unique_record_types >= 2:
        return CONFIDENCE_THRESHOLDS["medium"]
    if candidate_confidence == "medium":
        return CONFIDENCE_THRESHOLDS["medium"]
    return CONFIDENCE_THRESHOLDS["low"]


def _match_patterns(
    text: str,
    patterns: List[Tuple[str, str]],
    area_type: str,
) -> List[Dict[str, Any]]:
    """Return a list of area hit dicts for all patterns that fire."""
    hits: Dict[str, List[str]] = {}  # area_name → [matched_patterns]
    for pattern_str, area_name in patterns:
        if area_name == "_generic_tool":
            # Only emit generic tool if no specific tool already matched
            if any(a_name != "_generic_tool" for p_str, a_name in _TOOL_PATTERNS if re.search(p_str, text, re.IGNORECASE)):
                continue
        if re.search(pattern_str, text, re.IGNORECASE):
            hits.setdefault(area_name, []).append(pattern_str)
    # Suppress _generic_tool if a specific tool fired
    if "_generic_tool" in hits and len(hits) > 1:
        del hits["_generic_tool"]
    results = []
    for area_name, signals in hits.items():
        public_name = "tool" if area_name == "_generic_tool" else area_name
        results.append(
            {
                "area_type": area_type,
                "area_name": public_name,
                "signals": signals,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a single dream-cycle artifact candidate.

    Parameters
    ----------
    candidate:
        A dict conforming to the ``mac.dream.v1`` schema (as produced by
        ``mac.nap_consolidator._default_dreamer``) or any compatible
        superset.  Unknown keys are silently ignored.

    Returns
    -------
    dict
        Stable JSON-serialisable report dict (see module docstring).
    """
    text = _combined_text(candidate)
    redacted = bool(_REDACTED_RE.search(text))

    ev_count = _evidence_count(candidate)
    unique_rt = _unique_record_types(candidate)
    cand_confidence = str(candidate.get("confidence") or "low").strip().lower()
    if cand_confidence not in CONFIDENCE_THRESHOLDS:
        cand_confidence = "low"

    # Gather raw area hits.
    raw_areas: List[Dict[str, Any]] = []
    raw_areas.extend(_match_patterns(text, _SKILL_PATTERNS, "skill"))
    raw_areas.extend(_match_patterns(text, _TOOL_PATTERNS, "tool"))
    raw_areas.extend(_match_patterns(text, _PROVIDER_PATTERNS, "provider"))
    raw_areas.extend(_match_patterns(text, _REPO_AREA_PATTERNS, "repo_area"))

    # Per-area confidence assignment.
    areas: List[Dict[str, Any]] = []
    for raw in raw_areas:
        signal_count = len(raw["signals"])
        label, score = _confidence_for(ev_count, signal_count, cand_confidence, unique_rt)
        areas.append(
            {
                "area_type": raw["area_type"],
                "area_name": raw["area_name"],
                "confidence": label,
                "confidence_score": score,
                "signals": raw["signals"],
                "evidence_count": ev_count,
            }
        )

    # Overall confidence = highest area confidence, or the candidate's own if
    # no areas were matched.
    if areas:
        scores = [a["confidence_score"] for a in areas]
        best_score = max(scores)
    else:
        _, best_score = CONFIDENCE_THRESHOLDS.get(cand_confidence, CONFIDENCE_THRESHOLDS["low"])
    overall_label = _score_to_label(best_score)

    return {
        "schema": CLASSIFIER_SCHEMA,
        "candidate_id": candidate.get("candidate_id") or candidate.get("task_id") or candidate.get("nap_run_id"),
        "kind": str(candidate.get("kind") or "knowledge_snippet"),
        "scope": str(candidate.get("scope") or "agent"),
        "areas": areas,
        "overall_confidence": overall_label,
        "overall_confidence_score": best_score,
        "evidence_count": ev_count,
        "redacted": redacted,
    }


def classify_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify a list of candidates, returning one report per input.

    Suitable for batch processing the output of
    ``NapConsolidatorService.consolidate_agent``.
    """
    return [classify_candidate(c) for c in candidates]


def _score_to_label(score: float) -> str:
    """Map a numeric score back to a confidence label."""
    if score >= CONFIDENCE_THRESHOLDS["high"][1]:
        return "high"
    if score >= CONFIDENCE_THRESHOLDS["medium"][1]:
        return "medium"
    return "low"
