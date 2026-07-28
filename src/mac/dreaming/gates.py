"""Promotion gates.

"Memory promotion should be gated with checks such as provenance coverage,
contradiction reduction, privacy filters, and retrieval-quality tests."

The previous implementation built exactly one of those four — the privacy
filter — and shipped. The three it skipped are the three that would have caught
its actual failure: 154,273 rows, 4,414 distinct, all unreachable.

:func:`compression_gate` is the addition that is not on that list. It encodes
the one property both the upstream API docs and the write-up treat as the
*definition* of dreaming — that the output store is smaller and cleaner than
the input. Making it a hard gate means a dream that inflates the store cannot
be promoted, which is the structural fix for the append-forever bug.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from mac.dreaming.models import (
    DreamPolicy,
    GateResult,
    MemoryCandidate,
    MemoryKind,
    SessionReflection,
    Snapshot,
)
from mac.dreaming.pipeline import _similarity
from mac.dreaming.redact import leaks


def provenance_gate(
    candidates: Sequence[MemoryCandidate],
    snapshot: Snapshot,
    policy: DreamPolicy,
) -> GateResult:
    """Every candidate must cite at least one source that really exists.

    A model asked to summarise will happily invent a plausible id. Checking
    citations against the frozen snapshot is what separates a memory from a
    confabulation.
    """

    if not candidates:
        return GateResult("provenance_coverage", True, "no candidates", 1.0, policy.min_provenance_coverage)
    known = snapshot.known_source_ids
    grounded = 0
    ungrounded: List[str] = []
    for candidate in candidates:
        if any(ref.id in known for ref in candidate.sources):
            grounded += 1
        else:
            ungrounded.append(candidate.statement[:60])
    coverage = grounded / len(candidates)
    passed = coverage >= policy.min_provenance_coverage
    detail = "all candidates grounded" if passed else "ungrounded: %s" % "; ".join(ungrounded[:3])
    return GateResult("provenance_coverage", passed, detail, coverage, policy.min_provenance_coverage)


def contradiction_gate(
    candidates: Sequence[MemoryCandidate],
    policy: DreamPolicy,
) -> GateResult:
    """The output must not disagree with itself.

    Two candidates that say substantially the same thing but classify it
    oppositely — a practice and a pitfall over the same subject — are a
    contradiction the resolve stage should have settled. Shipping both would
    make recall return whichever the index happened to rank first.
    """

    conflicts: List[str] = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if left.kind is right.kind:
                continue
            if {left.kind, right.kind} != {MemoryKind.PRACTICE, MemoryKind.PITFALL}:
                continue
            if _similarity(left.statement, right.statement) >= policy.max_pairwise_similarity:
                conflicts.append("%s <> %s" % (left.statement[:40], right.statement[:40]))
    resolved = sum(1 for candidate in candidates if candidate.contradicts)
    passed = not conflicts
    detail = (
        "%d input contradiction(s) resolved" % resolved
        if passed
        else "unresolved: %s" % "; ".join(conflicts[:3])
    )
    return GateResult("contradiction_reduction", passed, detail, float(len(conflicts)), 0.0)


def privacy_gate(
    candidates: Sequence[MemoryCandidate],
    reflections: Sequence[SessionReflection],
) -> GateResult:
    """No credential or local-identity material may reach a durable memory."""

    offenders: List[str] = []
    for candidate in candidates:
        found = leaks(candidate.statement) + leaks(candidate.applies_when)
        if found:
            offenders.append("%s:%s" % (candidate.id, ",".join(sorted(set(found)))))
    for reflection in reflections:
        found = leaks(reflection.objective) + leaks(reflection.reason)
        if found:
            offenders.append("%s:%s" % (reflection.session_id, ",".join(sorted(set(found)))))
    passed = not offenders
    detail = "clean" if passed else "leaks in %s" % "; ".join(offenders[:3])
    return GateResult("privacy", passed, detail, float(len(offenders)), 0.0)


def retrieval_quality_gate(
    candidates: Sequence[MemoryCandidate],
    policy: DreamPolicy,
) -> GateResult:
    """No two output memories may be near-duplicates.

    This is the gate whose absence is directly measurable in the live ledger:
    154,273 dream rows carrying 4,414 distinct statements. Duplicates do not
    merely waste space — they crowd a vector index so recall returns the same
    thing N times instead of N different things.
    """

    duplicates: List[str] = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if left.kind is not right.kind:
                continue
            score = _similarity(left.statement, right.statement)
            if score >= policy.max_pairwise_similarity:
                duplicates.append(
                    "%.2f: %s ~ %s" % (score, left.statement[:35], right.statement[:35])
                )
    passed = not duplicates
    detail = "%d distinct memories" % len(candidates) if passed else "near-duplicates: %s" % "; ".join(duplicates[:3])
    return GateResult(
        "retrieval_quality", passed, detail, float(len(duplicates)), 0.0
    )


def compression_gate(
    candidates: Sequence[MemoryCandidate],
    snapshot: Snapshot,
    policy: DreamPolicy,
) -> GateResult:
    """A dream must express its inputs more compactly than it found them.

    With an empty input store there is nothing to compress, so the gate passes
    trivially; that is the cold-start case, not a loophole, because the output
    is still bounded by ``max_candidates``.
    """

    input_size = snapshot.input_size
    output_size = len(candidates)
    if input_size == 0:
        return GateResult(
            "compression", True, "empty input store", 0.0, policy.max_output_ratio
        )
    ratio = output_size / input_size
    passed = ratio <= policy.max_output_ratio
    detail = "%d in -> %d out (%.0f%%)" % (input_size, output_size, ratio * 100)
    if not passed:
        detail += " — a dream that grows the store is not curation"
    return GateResult("compression", passed, detail, ratio, policy.max_output_ratio)


def balance_gate(candidates: Sequence[MemoryCandidate]) -> GateResult:
    """Advisory: report the win/loss mix rather than enforcing one.

    Always passes. It exists so the run record carries the number that made
    the old cycle's blind spot visible — 154,273 failure artifacts and zero
    wins — where a human or a dashboard will see it, instead of nobody
    noticing for forty days.
    """

    wins = sum(1 for candidate in candidates if candidate.is_win)
    losses = sum(1 for candidate in candidates if candidate.kind is MemoryKind.PITFALL)
    total = len(candidates)
    detail = "%d practice / %d pitfall / %d other" % (wins, losses, total - wins - losses)
    if total and wins == 0 and losses > 0:
        detail += " — failure-only output; check the extractor is not regressing"
    ratio = (wins / total) if total else 0.0
    return GateResult("win_balance", True, detail, ratio, None)


def run_all_gates(
    candidates: Sequence[MemoryCandidate],
    reflections: Sequence[SessionReflection],
    snapshot: Snapshot,
    policy: DreamPolicy,
) -> List[GateResult]:
    """Evaluate every gate. Order is stable so run records diff cleanly."""

    return [
        provenance_gate(candidates, snapshot, policy),
        contradiction_gate(candidates, policy),
        privacy_gate(candidates, reflections),
        retrieval_quality_gate(candidates, policy),
        compression_gate(candidates, snapshot, policy),
        balance_gate(candidates),
    ]


def gate_summary(gates: Sequence[GateResult]) -> Dict[str, bool]:
    return {gate.name: gate.passed for gate in gates}


__all__ = [
    "balance_gate",
    "compression_gate",
    "contradiction_gate",
    "gate_summary",
    "privacy_gate",
    "provenance_gate",
    "retrieval_quality_gate",
    "run_all_gates",
]
