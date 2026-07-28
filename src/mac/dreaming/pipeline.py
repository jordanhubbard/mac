"""The four transforming stages of a dream.

``freeze_inputs`` → ``extract_candidates`` → ``resolve_duplicates_and_conflicts``
→ ``compress_with_provenance``

Each stage is a pure function over the frozen snapshot. Nothing here reads the
live store or writes anything; persistence lives in :mod:`mac.dreaming.store`
and orchestration in :mod:`mac.dreaming.engine`. That separation is what makes
the copy-on-write guarantee checkable rather than aspirational.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from mac.dreaming.models import (
    DreamPolicy,
    InputRecord,
    InputSession,
    MemoryCandidate,
    MemoryKind,
    SessionOutcome,
    SessionReflection,
    Snapshot,
    SourceRef,
)
from mac.dreaming.redact import redact

#: ``caller(model, prompt, context) -> (answer, citations, elapsed_ms)``.
#: Matches ``mac.eval_runner.router_model_caller`` so the engine can pass the
#: existing router seam straight through.
ModelCaller = Callable[[str, str, str], Tuple[str, Any, Any]]

_WORD_RE = re.compile(r"[a-z0-9_./-]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "because", "been", "but", "by",
        "for", "from", "had", "has", "have", "if", "in", "into", "is", "it", "its",
        "of", "on", "or", "that", "the", "then", "there", "this", "to", "was",
        "were", "when", "which", "while", "with", "you", "your",
    }
)


# ---------------------------------------------------------------------------
# Stage 1 — freeze
# ---------------------------------------------------------------------------


def freeze_inputs(
    records: Iterable[InputRecord],
    sessions: Iterable[InputSession] = (),
    existing: Iterable[InputRecord] = (),
) -> Snapshot:
    """Take an immutable copy of the inputs.

    The upstream design is explicit that "the input memory store is not
    modified"; freezing here is what lets the gates compare output against a
    stable input size even though the live hub keeps writing during the run.
    """

    return Snapshot(
        records=list(records), sessions=list(sessions), existing=list(existing)
    )


# ---------------------------------------------------------------------------
# Stage 2 — extract
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """\
You are the reflection stage of an agent's dream cycle. You are given past \
session transcripts and the agent's existing memory records. Produce durable \
memories worth carrying into future sessions, and judge whether each session \
reached its objective.

Rules:
- Record what WORKED as readily as what failed. A repeatable practice is worth \
at least as much as a pitfall. Do not return only failures.
- Every memory must cite at least one source id from the input, verbatim.
- Write each memory as one self-contained sentence that will still make sense \
months from now, with no reference to "this task" or "the above".
- Merge duplicates. If two observations say the same thing, emit one memory \
citing both sources.
- Omit anything transient: one-off paths, temporary state, scratch debugging.
- If nothing durable is present, return empty lists. Returning nothing is a \
correct answer and is strongly preferred over padding.
%(instructions)s

Memory kinds:
  practice   - an approach that worked and should be repeated
  pitfall    - a failure mode worth avoiding
  fact       - a durable truth about the system or repository
  preference - a stated human preference about how work should be done
  obligation - an unresolved commitment someone still owes

Session outcomes:
  objective_met, partially_met, abandoned, derailed, unresolved, unknown

=== SESSIONS ===
%(sessions)s

=== EXISTING MEMORY (do not repeat these; supersede them if contradicted) ===
%(existing)s

Reply with JSON only, no prose, matching exactly:
{"memories": [{"kind": "...", "statement": "...", "applies_when": "...",
               "sources": [{"kind": "session|memory|task", "id": "..."}],
               "contradicts": ["..."]}],
 "reflections": [{"session_id": "...", "objective": "...", "outcome": "...",
                  "reason": "..."}]}
"""


def extract_candidates(
    snapshot: Snapshot,
    policy: DreamPolicy,
    *,
    model: str = "",
    model_caller: Optional[ModelCaller] = None,
) -> Tuple[List[MemoryCandidate], List[SessionReflection], str]:
    """Mine the snapshot for durable memories and session verdicts.

    Returns ``(candidates, reflections, extractor_name)``. When no model is
    available the heuristic path runs, which only reads *structured* outcome
    fields — it never keyword-scans free text, which is precisely how the
    previous implementation manufactured 154,273 false failure patterns.
    """

    if model_caller is not None and model:
        try:
            return _extract_with_model(snapshot, policy, model, model_caller)
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail the run
            if not policy.allow_heuristic_fallback:
                raise
            candidates, reflections, _ = _extract_heuristic(snapshot, policy)
            return candidates, reflections, "heuristic(after-model-error:%s)" % (
                str(exc)[:80],
            )
    if not policy.allow_heuristic_fallback:
        raise RuntimeError("no model available and heuristic fallback is disabled")
    return _extract_heuristic(snapshot, policy)


def _extract_with_model(
    snapshot: Snapshot,
    policy: DreamPolicy,
    model: str,
    model_caller: ModelCaller,
) -> Tuple[List[MemoryCandidate], List[SessionReflection], str]:
    sessions_text = "\n\n".join(
        "--- session %s (project=%s) ---\n%s"
        % (session.id, session.project or "?", redact(session.transcript(), limit=12000, collapse_space=False))
        for session in snapshot.sessions
    ) or "(no transcripts supplied)"
    # Show the previously curated store, not the raw evidence: the model's job
    # here is to notice which existing memories this batch supersedes or
    # contradicts. Falling back to raw records only matters on a cold start.
    prior = snapshot.existing or snapshot.records
    existing_text = "\n".join(
        "- [%s] %s" % (record.id, redact(record.content, limit=300))
        for record in prior[:200]
    ) or "- (empty store)"
    instructions = (
        "\n- Operator instructions: %s" % policy.instructions.strip()
        if policy.instructions.strip()
        else ""
    )
    prompt = _EXTRACT_PROMPT % {
        "sessions": sessions_text,
        "existing": existing_text,
        "instructions": instructions,
    }
    answer, _citations, _elapsed = model_caller(model, prompt, "")
    payload = _parse_json_object(str(answer or ""))
    candidates = [
        candidate
        for candidate in (
            MemoryCandidate.from_dict(item) for item in payload.get("memories") or []
        )
        if candidate is not None
    ]
    reflections = [
        reflection
        for reflection in (
            SessionReflection.from_dict(item)
            for item in payload.get("reflections") or []
        )
        if reflection is not None
    ]
    for candidate in candidates:
        candidate.statement = redact(candidate.statement, limit=600)
        candidate.applies_when = redact(candidate.applies_when, limit=300)
    for reflection in reflections:
        reflection.objective = redact(reflection.objective, limit=400)
        reflection.reason = redact(reflection.reason, limit=600)
    return candidates, reflections, "model:%s" % model


def _parse_json_object(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Models fence JSON, prepend prose, or both. Rather than fail the run on a
    formatting quirk, locate the outermost braces and parse that.
    """

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        loaded = json.loads(stripped)
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError):
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        loaded = json.loads(stripped[start : end + 1])
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


#: Structured outcome values that mean the work succeeded. Read from the
#: ``outcome`` field of ``mac.deployment_learning.v1`` records — an explicit
#: field, never inferred from prose.
_SUCCESS_OUTCOMES = frozenset({"success", "approved_published"})
_FAILURE_OUTCOMES = frozenset({"failure", "review_rejected"})


def _extract_heuristic(
    snapshot: Snapshot,
    policy: DreamPolicy,
) -> Tuple[List[MemoryCandidate], List[SessionReflection], str]:
    """Structured-field-only extraction, used when no model is configured.

    Deliberately narrow. It reads the explicit ``outcome`` field of learning
    records and nothing else — no regex over message text. The old cycle's
    ``_dream_kind`` marked a whole group ``failure_pattern`` if the substring
    "error" appeared anywhere in it, which is how successful tasks became
    failure findings.
    """

    by_statement: Dict[Tuple[str, str], MemoryCandidate] = {}
    for record in snapshot.records:
        payload = _record_payload(record.content)
        outcome = str(payload.get("outcome") or "").strip().lower()
        if outcome in _SUCCESS_OUTCOMES:
            kind = MemoryKind.PRACTICE
        elif outcome in _FAILURE_OUTCOMES:
            kind = MemoryKind.PITFALL
        else:
            continue
        statement = _heuristic_statement(kind, payload)
        if not statement:
            continue
        key = (kind.value, _normalize(statement))
        existing = by_statement.get(key)
        ref = SourceRef(kind="memory", id=record.id, detail=record.record_type)
        if existing is None:
            by_statement[key] = MemoryCandidate(
                kind=kind,
                statement=statement,
                scope="project" if record.project else "agent",
                project=record.project,
                sources=[ref],
            )
        else:
            existing.sources.append(ref)
    candidates = list(by_statement.values())
    candidates.sort(key=lambda c: (-c.source_count, c.kind.value, c.statement))
    return candidates[: policy.max_candidates], [], "heuristic"


def _record_payload(content: str) -> Dict[str, Any]:
    try:
        loaded = json.loads(content)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _heuristic_statement(kind: MemoryKind, payload: Dict[str, Any]) -> str:
    evidence_type = str(payload.get("evidence_type") or "work").strip()
    signature = str(payload.get("error_signature") or "").strip()
    if kind is MemoryKind.PITFALL:
        if not signature:
            return ""
        return redact("%s work fails with: %s" % (evidence_type, signature), limit=400)
    detail = signature or "no follow-up issues recorded"
    return redact(
        "%s work completed successfully; %s" % (evidence_type, detail), limit=400
    )


# ---------------------------------------------------------------------------
# Stage 3 — resolve duplicates and contradictions
# ---------------------------------------------------------------------------


def resolve_duplicates_and_conflicts(
    candidates: Sequence[MemoryCandidate],
    snapshot: Snapshot,
    policy: DreamPolicy,
) -> List[MemoryCandidate]:
    """Collapse near-duplicate candidates and attach supersession.

    This is the stage the previous implementation simply did not have. Without
    it the consolidator wrote a fresh row per pass, producing a measured 35:1
    duplication ratio in the live ledger.
    """

    canonical: List[MemoryCandidate] = []
    for candidate in sorted(
        candidates, key=lambda c: (-c.source_count, len(c.statement))
    ):
        merged_into = None
        for existing in canonical:
            if existing.kind is not candidate.kind:
                continue
            if _similarity(existing.statement, candidate.statement) < policy.max_pairwise_similarity:
                continue
            merged_into = existing
            break
        if merged_into is None:
            canonical.append(candidate)
            continue
        # Merge: keep the better-supported statement, union the provenance.
        seen = {ref.origin for ref in merged_into.sources}
        for ref in candidate.sources:
            if ref.origin not in seen:
                merged_into.sources.append(ref)
                seen.add(ref.origin)
        for statement in candidate.contradicts:
            if statement not in merged_into.contradicts:
                merged_into.contradicts.append(statement)

    # A candidate supersedes the input rows it was distilled from — raw
    # evidence and any stale prior memory it replaces — so promoting it can
    # retire them. This is the mechanism by which the store shrinks.
    known = snapshot.supersedable_ids
    for candidate in canonical:
        candidate.supersedes = sorted(
            {
                ref.id
                for ref in candidate.sources
                if ref.kind == "memory" and ref.id in known
            }
        )
    return canonical


def _normalize(text: str) -> str:
    return " ".join(sorted(_tokens(text)))


def _tokens(text: str) -> set:
    return {
        word
        for word in _WORD_RE.findall(str(text or "").lower())
        if word not in _STOPWORDS and len(word) > 2
    }


def _similarity(left: str, right: str) -> float:
    """Jaccard overlap on content words. Cheap, deterministic, good enough to
    catch the "same insight, reworded" case that dominates real duplication."""

    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


# ---------------------------------------------------------------------------
# Stage 4 — compress with provenance
# ---------------------------------------------------------------------------


def compress_with_provenance(
    candidates: Sequence[MemoryCandidate],
    policy: DreamPolicy,
) -> List[MemoryCandidate]:
    """Drop under-supported candidates and cap the output.

    Ordering is by support then brevity, so if the cap bites it drops the
    weakest evidence rather than an arbitrary tail.
    """

    kept = [
        candidate
        for candidate in candidates
        if candidate.source_count >= policy.min_sources and candidate.statement.strip()
    ]
    kept.sort(key=lambda c: (-c.source_count, c.kind.value, c.statement))
    return kept[: policy.max_candidates]


__all__ = [
    "ModelCaller",
    "compress_with_provenance",
    "extract_candidates",
    "freeze_inputs",
    "resolve_duplicates_and_conflicts",
]
