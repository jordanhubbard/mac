"""Types for the dreaming pipeline.

The old dream cycle encoded three assumptions that this module deliberately
breaks:

1. *Only failures are worth remembering.*  Its artifact taxonomy had a
   ``failure_pattern`` kind and nothing for a win, so 1,443 successful
   outcomes in the live ledger produced zero artifacts while 5,300 failures
   produced 154,273.  :class:`MemoryKind` makes ``PRACTICE`` — a thing that
   worked and should be repeated — a peer of ``PITFALL``.

2. *Confidence means volume.*  It scored a finding ``high`` once three rows
   backed it, so re-reading the same row three times looked like corroboration.
   :func:`confidence_for` counts **distinct independent sources** instead.

3. *A conversation is a bag of lines.*  Nothing ever asked whether a session
   reached its objective.  :class:`SessionOutcome` makes that the unit of
   reflection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from mac.models import JsonDict, new_id, utcnow

DREAM_SCHEMA_VERSION = "mac.dream.v2"
CANDIDATE_SCHEMA = "mac.dream_candidate.v2"
REFLECTION_SCHEMA = "mac.dream_session_reflection.v2"
RUN_SCHEMA = "mac.dream_run.v2"


class MemoryKind(str, Enum):
    """What a durable memory *is*.

    Wins and losses are peers here on purpose; see the module docstring.
    """

    PRACTICE = "practice"      # worked; repeat it
    PITFALL = "pitfall"        # failed; avoid it
    FACT = "fact"              # durable truth about the system or repo
    PREFERENCE = "preference"  # a human's stated preference
    OBLIGATION = "obligation"  # an unresolved commitment

    @classmethod
    def parse(cls, value: Any) -> Optional["MemoryKind"]:
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        return None


#: Kinds that record something that went right. Tracked so the pipeline can
#: assert it is not silently reverting to a failure-only scanner.
WIN_KINDS = frozenset({MemoryKind.PRACTICE})


class SessionOutcome(str, Enum):
    """Did the conversation actually get where it was going?"""

    OBJECTIVE_MET = "objective_met"
    PARTIALLY_MET = "partially_met"
    ABANDONED = "abandoned"      # stopped without resolution
    DERAILED = "derailed"        # went somewhere other than the objective
    UNRESOLVED = "unresolved"    # still open at the end of the transcript
    UNKNOWN = "unknown"          # not enough transcript to judge

    @classmethod
    def parse(cls, value: Any) -> "SessionOutcome":
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN


#: Outcomes that mean the objective was not reached. A reflection carrying one
#: of these is what a ``PITFALL`` should be derived from.
FAILED_OUTCOMES = frozenset(
    {SessionOutcome.ABANDONED, SessionOutcome.DERAILED, SessionOutcome.UNRESOLVED}
)


class StoreState(str, Enum):
    """Lifecycle of a candidate store.

    A dream never writes to the live memory store. It writes a candidate
    store, which a human or a policy promotes. ``QUARANTINED`` means the
    quality gates rejected it — it is kept for inspection, not adopted.
    """

    READY_FOR_REVIEW = "ready_for_review"
    QUARANTINED = "quarantined"
    PROMOTED = "promoted"
    DISCARDED = "discarded"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def confidence_for(source_count: int) -> tuple[str, float]:
    """Map *independent source* count to a confidence label.

    The distinction from the old implementation matters: it counted memory
    rows, and the consolidator wrote a fresh row every pass, so a single
    observation re-read 3 times scored ``high``. Here the caller passes the
    number of **distinct** sessions/tasks that independently support the
    statement, so corroboration has to actually come from somewhere else.
    """

    if source_count >= 3:
        return "high", 0.90
    if source_count == 2:
        return "medium", 0.65
    return "low", 0.35


@dataclass
class SourceRef:
    """Where a candidate came from. Every candidate must carry at least one."""

    kind: str          # "session" | "memory" | "task"
    id: str
    detail: str = ""

    def to_dict(self) -> JsonDict:
        out: JsonDict = {"kind": self.kind, "id": self.id}
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, data: Any) -> Optional["SourceRef"]:
        if not isinstance(data, dict):
            return None
        ref_id = str(data.get("id") or "").strip()
        kind = str(data.get("kind") or "").strip() or "memory"
        if not ref_id:
            return None
        return cls(kind=kind, id=ref_id, detail=str(data.get("detail") or "")[:200])

    @property
    def origin(self) -> str:
        """Key used to judge independence — two refs from the same session
        are one source, however many rows they produced."""
        return "%s:%s" % (self.kind, self.id)


@dataclass
class MemoryCandidate:
    """One durable memory proposed by a dream."""

    kind: MemoryKind
    statement: str
    scope: str = "agent"
    project: Optional[str] = None
    agent_id: Optional[str] = None
    applies_when: str = ""
    sources: List[SourceRef] = field(default_factory=list)
    #: ids of *input* memory rows this candidate replaces. Populated by the
    #: resolve stage; the promoter retires them so the store shrinks.
    supersedes: List[str] = field(default_factory=list)
    #: statements from the input store this candidate directly contradicts.
    contradicts: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("dreammem"))
    created_at: str = field(default_factory=utcnow)

    @property
    def source_count(self) -> int:
        """Distinct origins, not raw refs — see :func:`confidence_for`."""
        return len({ref.origin for ref in self.sources})

    @property
    def confidence(self) -> str:
        return confidence_for(self.source_count)[0]

    @property
    def confidence_score(self) -> float:
        return confidence_for(self.source_count)[1]

    @property
    def is_win(self) -> bool:
        return self.kind in WIN_KINDS

    def to_dict(self) -> JsonDict:
        return {
            "schema": CANDIDATE_SCHEMA,
            "id": self.id,
            "kind": self.kind.value,
            "statement": self.statement,
            "scope": self.scope,
            "project": self.project,
            "agent_id": self.agent_id,
            "applies_when": self.applies_when,
            "confidence": self.confidence,
            "confidence_score": self.confidence_score,
            "source_count": self.source_count,
            "sources": [ref.to_dict() for ref in self.sources],
            "supersedes": list(self.supersedes),
            "contradicts": list(self.contradicts),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["MemoryCandidate"]:
        if not isinstance(data, dict):
            return None
        kind = MemoryKind.parse(data.get("kind"))
        statement = str(data.get("statement") or "").strip()
        if kind is None or not statement:
            return None
        sources = [
            ref
            for ref in (SourceRef.from_dict(item) for item in data.get("sources") or [])
            if ref is not None
        ]
        candidate = cls(
            kind=kind,
            statement=statement,
            scope=str(data.get("scope") or "agent"),
            project=data.get("project"),
            agent_id=data.get("agent_id"),
            applies_when=str(data.get("applies_when") or "")[:400],
            sources=sources,
            supersedes=[str(item) for item in data.get("supersedes") or []],
            contradicts=[str(item) for item in data.get("contradicts") or []],
        )
        if data.get("id"):
            candidate.id = str(data["id"])
        return candidate


@dataclass
class SessionReflection:
    """The dream's read on one conversation: did it get where it was going?"""

    session_id: str
    objective: str
    outcome: SessionOutcome
    reason: str = ""
    derailed_at: str = ""
    sources: List[SourceRef] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "schema": REFLECTION_SCHEMA,
            "session_id": self.session_id,
            "objective": self.objective,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "derailed_at": self.derailed_at,
            "sources": [ref.to_dict() for ref in self.sources],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["SessionReflection"]:
        if not isinstance(data, dict):
            return None
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            return None
        return cls(
            session_id=session_id,
            objective=str(data.get("objective") or "")[:500],
            outcome=SessionOutcome.parse(data.get("outcome")),
            reason=str(data.get("reason") or "")[:800],
            derailed_at=str(data.get("derailed_at") or "")[:200],
            sources=[
                ref
                for ref in (
                    SourceRef.from_dict(item) for item in data.get("sources") or []
                )
                if ref is not None
            ],
        )


@dataclass
class DreamPolicy:
    """Knobs for one dream. Defaults are deliberately conservative."""

    #: Hard ceiling on output size relative to input. A dream that cannot
    #: express the input more compactly than this has not curated anything.
    max_output_ratio: float = 0.75
    #: Minimum distinct sources for a candidate to be emitted at all.
    min_sources: int = 1
    #: Reject the run if fewer than this fraction of candidates cite a source
    #: that actually exists in the snapshot.
    min_provenance_coverage: float = 1.0
    #: Reject the run if two output candidates are near-duplicates above this
    #: token-overlap threshold — the gate the old cycle lacked.
    max_pairwise_similarity: float = 0.85
    #: Cap on candidates emitted per run.
    max_candidates: int = 200
    #: Steering text handed to the extractor, mirroring the ``instructions``
    #: field on the upstream dreams API.
    instructions: str = ""
    #: When false the pipeline refuses to run without a model, rather than
    #: silently degrading to the keyword matching that caused the old mess.
    allow_heuristic_fallback: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "max_output_ratio": self.max_output_ratio,
            "min_sources": self.min_sources,
            "min_provenance_coverage": self.min_provenance_coverage,
            "max_pairwise_similarity": self.max_pairwise_similarity,
            "max_candidates": self.max_candidates,
            "instructions": self.instructions,
            "allow_heuristic_fallback": self.allow_heuristic_fallback,
        }


@dataclass
class InputRecord:
    """One row of the input memory store, as the pipeline sees it."""

    id: str
    record_type: str
    content: str
    task_id: Optional[str] = None
    project: Optional[str] = None
    subject_id: Optional[str] = None
    created_at: str = ""


@dataclass
class InputSession:
    """One transcript the dream mines. ``turns`` are ordered oldest first."""

    id: str
    turns: List[Dict[str, str]] = field(default_factory=list)
    project: Optional[str] = None
    started_at: str = ""

    def transcript(self, *, max_chars: int = 12000) -> str:
        lines = []
        for turn in self.turns:
            role = str(turn.get("role") or "?")
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            lines.append("%s: %s" % (role, text))
        joined = "\n".join(lines)
        if len(joined) <= max_chars:
            return joined
        # Keep both ends: the objective is stated at the start, the outcome
        # shows at the end. Dropping the middle preserves both.
        head = joined[: max_chars // 2]
        tail = joined[-(max_chars // 2) :]
        return head + "\n…[transcript truncated]…\n" + tail


@dataclass
class Snapshot:
    """Frozen inputs. Nothing downstream may re-read the live store.

    ``records`` is raw evidence to mine. ``existing`` is the previously
    curated store — prior promoted dream memories — which the extractor is
    shown so it can supersede entries that have gone stale or been
    contradicted. Keeping the two lists apart is what lets a dream revise its
    own past conclusions without mining them as if they were fresh evidence;
    feeding curated output back in as evidence is the self-referential loop
    that made the previous cycle's findings unactionable.
    """

    records: List[InputRecord] = field(default_factory=list)
    sessions: List[InputSession] = field(default_factory=list)
    existing: List[InputRecord] = field(default_factory=list)
    taken_at: str = field(default_factory=utcnow)

    @property
    def record_ids(self) -> set:
        return {record.id for record in self.records}

    @property
    def existing_ids(self) -> set:
        return {record.id for record in self.existing}

    @property
    def session_ids(self) -> set:
        return {session.id for session in self.sessions}

    @property
    def known_source_ids(self) -> set:
        ids = self.record_ids | self.session_ids | self.existing_ids
        ids |= {record.task_id for record in self.records if record.task_id}
        return ids

    @property
    def supersedable_ids(self) -> set:
        """Rows a candidate may retire: raw evidence and stale prior memories."""
        return self.record_ids | self.existing_ids

    @property
    def input_size(self) -> int:
        """Size of the store being curated, prior memories included."""
        return len(self.records) + len(self.existing)


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    measured: Optional[float] = None
    threshold: Optional[float] = None

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "measured": self.measured,
            "threshold": self.threshold,
        }


@dataclass
class DreamResult:
    """Everything one dream produced, plus why it was or wasn't adopted."""

    run_id: str
    state: StoreState
    candidates: List[MemoryCandidate] = field(default_factory=list)
    reflections: List[SessionReflection] = field(default_factory=list)
    gates: List[GateResult] = field(default_factory=list)
    stats: JsonDict = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    extractor: str = ""

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def win_count(self) -> int:
        return sum(1 for candidate in self.candidates if candidate.is_win)

    def to_dict(self) -> JsonDict:
        return {
            "schema": RUN_SCHEMA,
            "run_id": self.run_id,
            "state": self.state.value,
            "extractor": self.extractor,
            "candidate_count": len(self.candidates),
            "win_count": self.win_count,
            "reflection_count": len(self.reflections),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "reflections": [reflection.to_dict() for reflection in self.reflections],
            "gates": [gate.to_dict() for gate in self.gates],
            "stats": dict(self.stats),
            "errors": list(self.errors),
        }


def outcome_counts(reflections: Sequence[SessionReflection]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for reflection in reflections:
        counts[reflection.outcome.value] = counts.get(reflection.outcome.value, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "CANDIDATE_SCHEMA",
    "DREAM_SCHEMA_VERSION",
    "REFLECTION_SCHEMA",
    "RUN_SCHEMA",
    "DreamPolicy",
    "DreamResult",
    "FAILED_OUTCOMES",
    "GateResult",
    "InputRecord",
    "InputSession",
    "MemoryCandidate",
    "MemoryKind",
    "RunStatus",
    "SessionOutcome",
    "SessionReflection",
    "Snapshot",
    "SourceRef",
    "StoreState",
    "WIN_KINDS",
    "confidence_for",
    "outcome_counts",
]
