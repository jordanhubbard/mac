"""Conservative garbage collection of OpenClaw checkpoint-candidate staging dirs.

An OpenClaw gateway stages each in-flight checkpoint under a per-owner directory
named ``.checkpoint-<pid>`` inside the checkpoint root. A candidate becomes a
*validated pair* only once both its downloaded halves — a workspace archive and
a state archive — are present and pass integrity validation; the gateway then
promotes the pair into the retained rollout generation. If the owning process
dies mid-flight (workspace download, state download, validation, or promotion),
the ``.checkpoint-<pid>`` directory is left behind as *staging garbage* that no
live process owns. Over time these abandoned candidates accumulate without
bound and consume disk, even though they are separate from OpenShell sandbox
garbage collection and from the active retained rollout generation.

This module is the conservative lifecycle + observability layer for those
candidates. It is a pure, side-effect-free decision engine: callers inject a
snapshot of the candidates (path identity, owner PID, presence/validation of
each half, byte count, age) plus injected predicates for *owner liveness* and
the *grace window*; the module classifies each candidate and decides — under a
single caller-held checkpoint lock — which candidates may be retired. It never
performs I/O itself, never reads database content or credentials, and emits an
auditable :class:`RetireReceipt` for every candidate it decides to retire.

Safety invariants (proven by the crash-injection tests):

  * The current active candidate (a live owner PID) is NEVER retired.
  * The most recent validated last-good pair is NEVER retired, even when its
    owner PID is dead — it is the rollback anchor.
  * Only candidates that are provably unowned (dead owner) AND outside the
    configured grace period are eligible; anything younger, live, or of
    unknown liveness is preserved.
  * A crash at any staging phase can only leave residue to be retired later; it
    can never delete the last validated pair nor cause unbounded accumulation.
  * Cleanup is an observation/maintenance concern: :func:`plan_checkpoint_gc`
    reports what it *would* retire and why; a failure to actually delete a
    retired candidate is a maintenance item, never a reason to block unrelated
    fleet work.

Classification values: active, complete_pair, incomplete, stale
Action values: preserve, retire

All liveness and grace decisions are expressed through injected callables — no
live process table or wall clock is consulted here — mirroring the pure,
injectable convention of adjacent OpenClaw modules
(e.g. ``openclaw_fleet_rollout.py`` / ``openclaw_delivery_continuity.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Exported schema identifier. Mirrors the ``mac.<module>.vN`` convention used by
# adjacent fleet modules (e.g. ``ROLLOUT_PLAN_SCHEMA`` in
# openclaw_fleet_rollout.py). Consumers pin receipt/plan compatibility here.
CHECKPOINT_GC_SCHEMA = "mac.openclaw_checkpoint_gc.v1"

# Classification of a staged checkpoint candidate.
CLASS_ACTIVE = "active"  # owner PID is live -> in-flight, never touched
CLASS_COMPLETE_PAIR = "complete_pair"  # both halves present + validated
CLASS_INCOMPLETE = "incomplete"  # crash residue: a half is missing/invalid
CLASS_STALE = "stale"  # dead-owner residue past the grace window
VALID_CLASSES = frozenset(
    {CLASS_ACTIVE, CLASS_COMPLETE_PAIR, CLASS_INCOMPLETE, CLASS_STALE}
)

# Action decided for a candidate.
ACTION_PRESERVE = "preserve"
ACTION_RETIRE = "retire"
VALID_ACTIONS = frozenset({ACTION_PRESERVE, ACTION_RETIRE})

# A liveness predicate answers "is this owner PID currently alive?".
LivenessFn = Callable[[int], bool]

# A grace predicate answers "is this candidate still inside its grace window?"
# given its age in seconds. Returning True protects the candidate from retiral.
GraceFn = Callable[[float], bool]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CheckpointCandidate:
    """A single ``.checkpoint-<pid>`` staging directory to be classified.

    Attributes:
        path: Directory identity (``.../.checkpoint-<pid>``). Used only as an
            opaque identity string in receipts; no I/O is performed on it.
        owner_pid: The PID encoded by the staging directory's owner.
        has_workspace: Whether the workspace-archive half is present.
        has_state: Whether the state-archive half is present.
        validated: Whether the present halves passed integrity validation.
        byte_count: Total bytes occupied by the candidate.
        age_seconds: Age of the candidate (seconds since last write).
    """

    path: str
    owner_pid: int
    has_workspace: bool = False
    has_state: bool = False
    validated: bool = False
    byte_count: int = 0
    age_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.path).strip():
            raise ValueError("CheckpointCandidate.path must be non-empty")
        if int(self.owner_pid) <= 0:
            raise ValueError("CheckpointCandidate.owner_pid must be positive")
        if int(self.byte_count) < 0:
            raise ValueError("CheckpointCandidate.byte_count must be >= 0")
        if float(self.age_seconds) < 0:
            raise ValueError("CheckpointCandidate.age_seconds must be >= 0")

    @property
    def is_complete_pair(self) -> bool:
        """True only when BOTH halves are present AND validated."""
        return bool(self.has_workspace and self.has_state and self.validated)


@dataclass
class RetireReceipt:
    """Auditable record of a single retire/preserve decision.

    Deliberately carries only path identity, age, liveness/validation results,
    byte count, classification, action, and a plain-language reason — NEVER any
    database content or credential material.
    """

    path: str
    classification: str
    action: str
    reason: str
    owner_pid: int
    owner_alive: bool
    validated: bool
    byte_count: int
    age_seconds: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": CHECKPOINT_GC_SCHEMA,
            "path": self.path,
            "classification": self.classification,
            "action": self.action,
            "reason": self.reason,
            "owner_pid": self.owner_pid,
            "owner_alive": self.owner_alive,
            "validated": self.validated,
            "byte_count": self.byte_count,
            "age_seconds": self.age_seconds,
        }


@dataclass
class CheckpointGCPlan:
    """Result of a conservative checkpoint-candidate GC pass.

    ``receipts`` covers every candidate (preserve and retire). ``retire`` and
    ``preserve`` are convenience views. ``reclaimable_bytes`` sums only the
    bytes of candidates marked for retiral.
    """

    schema: str = CHECKPOINT_GC_SCHEMA
    receipts: List[RetireReceipt] = field(default_factory=list)

    @property
    def retire(self) -> List[RetireReceipt]:
        return [r for r in self.receipts if r.action == ACTION_RETIRE]

    @property
    def preserve(self) -> List[RetireReceipt]:
        return [r for r in self.receipts if r.action == ACTION_PRESERVE]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(r.byte_count for r in self.retire)

    def counts(self) -> Dict[str, int]:
        """Per-classification counts, useful for maintenance observability."""
        out: Dict[str, int] = {c: 0 for c in VALID_CLASSES}
        for r in self.receipts:
            out[r.classification] = out.get(r.classification, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Injectable predicate builders
# ---------------------------------------------------------------------------


def liveness_from_set(alive_pids: Optional[set] = None) -> LivenessFn:
    """Build a liveness predicate from an explicit set of alive PIDs.

    Convenience for tests / callers that already know the live-PID set instead
    of probing the process table. Absent set -> nothing is alive.
    """
    alive = set(alive_pids or ())

    def _fn(pid: int) -> bool:
        return pid in alive

    return _fn


def grace_from_seconds(grace_seconds: float) -> GraceFn:
    """Build a grace predicate that protects candidates younger than a window.

    A candidate is inside its grace window when ``age_seconds < grace_seconds``.
    A non-positive window disables the grace protection entirely.
    """
    window = float(grace_seconds)

    def _fn(age_seconds: float) -> bool:
        if window <= 0:
            return False
        return float(age_seconds) < window

    return _fn


# ---------------------------------------------------------------------------
# Classification + planning
# ---------------------------------------------------------------------------


def classify_candidate(
    candidate: CheckpointCandidate,
    *,
    owner_alive: bool,
    in_grace: bool,
) -> str:
    """Classify a single candidate given liveness and grace results.

    Precedence (most-protective first):
      1. live owner            -> active
      2. complete validated    -> complete_pair
      3. within grace / other  -> incomplete (crash residue, not yet stale)
      4. dead owner, out grace  -> stale (retirable residue)
    """
    if owner_alive:
        return CLASS_ACTIVE
    if candidate.is_complete_pair:
        return CLASS_COMPLETE_PAIR
    if in_grace:
        return CLASS_INCOMPLETE
    return CLASS_STALE


def plan_checkpoint_gc(
    candidates: List[CheckpointCandidate],
    *,
    is_owner_alive: LivenessFn,
    is_in_grace: GraceFn,
    lock: Optional[object] = None,
) -> CheckpointGCPlan:
    """Decide, conservatively, which checkpoint candidates may be retired.

    The whole decision runs under ``lock`` when provided — the caller's single
    checkpoint lock — so classification and the "keep the most-recent validated
    pair" choice observe a consistent snapshot. ``lock`` must be a context
    manager (e.g. ``threading.Lock``); ``None`` runs unlocked (tests / callers
    that already hold the lock).

    Retire ONLY candidates that are all of:
      * provably unowned (``is_owner_alive`` returns False), and
      * outside the grace window (``is_in_grace`` returns False), and
      * NOT the single most-recent validated last-good pair.

    Everything else is preserved: any live owner (active), any candidate still
    in grace (recent crash residue), any candidate whose liveness cannot be
    proven dead, and the rollback-anchor validated pair. This guarantees a
    crash can neither delete the last validated pair nor let candidates
    accumulate without bound (stale dead-owner residue is always reclaimed on a
    later pass).
    """
    if lock is not None:
        with lock:  # type: ignore[union-attr]
            return _plan_locked(candidates, is_owner_alive, is_in_grace)
    return _plan_locked(candidates, is_owner_alive, is_in_grace)


def _plan_locked(
    candidates: List[CheckpointCandidate],
    is_owner_alive: LivenessFn,
    is_in_grace: GraceFn,
) -> CheckpointGCPlan:
    # First pass: classify everything against a consistent snapshot.
    classified: List[tuple] = []  # (candidate, classification, owner_alive)
    for candidate in candidates:
        owner_alive = bool(is_owner_alive(candidate.owner_pid))
        in_grace = bool(is_in_grace(candidate.age_seconds))
        classification = classify_candidate(
            candidate, owner_alive=owner_alive, in_grace=in_grace
        )
        classified.append((candidate, classification, owner_alive))

    # Identify the single most-recent validated last-good pair (smallest age)
    # among complete pairs. This is the rollback anchor and is ALWAYS preserved
    # regardless of owner liveness.
    anchor_path: Optional[str] = None
    anchor_age: Optional[float] = None
    for candidate, classification, _alive in classified:
        if classification != CLASS_COMPLETE_PAIR:
            continue
        if anchor_age is None or candidate.age_seconds < anchor_age:
            anchor_age = candidate.age_seconds
            anchor_path = candidate.path

    plan = CheckpointGCPlan()
    for candidate, classification, owner_alive in classified:
        action, reason = _decide(
            candidate, classification, owner_alive, is_anchor=candidate.path == anchor_path
        )
        plan.receipts.append(
            RetireReceipt(
                path=candidate.path,
                classification=classification,
                action=action,
                reason=reason,
                owner_pid=candidate.owner_pid,
                owner_alive=owner_alive,
                validated=candidate.validated,
                byte_count=candidate.byte_count,
                age_seconds=candidate.age_seconds,
            )
        )
    return plan


def _decide(
    candidate: CheckpointCandidate,
    classification: str,
    owner_alive: bool,
    *,
    is_anchor: bool,
) -> tuple:
    """Map a classification to (action, reason), applying the safety rails."""
    if classification == CLASS_ACTIVE:
        return ACTION_PRESERVE, "active candidate: owner pid alive"
    if is_anchor:
        return ACTION_PRESERVE, "most-recent validated pair: rollback anchor"
    if classification == CLASS_COMPLETE_PAIR:
        # A validated pair that is NOT the most-recent anchor is a superseded
        # last-good; still conservative — preserve unless dead + out of grace.
        return ACTION_PRESERVE, "validated pair retained pending promotion"
    if classification == CLASS_INCOMPLETE:
        return ACTION_PRESERVE, "crash residue still within grace window"
    if classification == CLASS_STALE:
        return ACTION_RETIRE, "stale dead-owner residue outside grace window"
    # Unknown classification: fail safe by preserving.
    return ACTION_PRESERVE, "unknown classification: preserved (fail-safe)"
