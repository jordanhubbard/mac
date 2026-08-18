"""A merge queue that mac owns, for repositories the forge will not serialize.

## Why this exists

:mod:`mac.merge_queue` implements the OCC *validation phase* for one landing:
serialize per repository, project the merge against the CURRENT tip with ``git
merge-tree``, and run the contract suite against that projected tree.  What it
does not do is order *several* approved changes against each other.  Until now
that ordering was borrowed from the forge: ``gitops.merge_queue_enabled`` asks
GitHub whether the canonical branch has a merge queue, and when it does the
landing is enqueued with ``expectedHeadOid`` and GitHub serializes it.

GitHub merge queues are an **organization-only** feature.  On a User-owned
repository the API refuses the rule outright -- adding a ``merge_queue`` rule to
a personal repo's ruleset returns HTTP 422 ``Invalid rule 'merge_queue'`` even
with no parameters -- and GitHub has said it does not plan to open the feature
to personal accounts.  So for every personal repository mac manages, the
serialization guarantee simply is not available from the forge, and #400's
"no forge queue" path degraded to a plain squash merge whose weaker guarantee it
recorded honestly but could not fix.

This module is the fix: the queue itself, inside mac, durable in the ledger.

## The algorithm (well-trodden; nothing here is invented)

* **Ordering.**  Entries are approved changes awaiting land, ordered by
  admission, keyed by (repository, canonical branch).
* **Speculative batching** (Zuul's model).  Assume every queued entry will pass.
  Entry *N* is projected and tested on top of entries *1..N-1* rather than on
  the bare tip, so the entries can be tested in parallel instead of serially.
  If they all pass they land in order.  If entry *K* fails, everything behind it
  was tested against a state that will never exist: those speculative results
  are **discarded**, *K* is **evicted**, and the survivors are re-planned in a
  new speculation epoch without it.
* **Window sizing** (Zuul again, explicitly modelled on TCP congestion
  control).  An active window bounds how many entries may be speculating at
  once.  It grows **additively** on each successful land up to a ceiling and is
  **halved** on failure down to a floor.  That is what stops a flaky or
  conflict-heavy period from burning the whole fleet on speculation that will be
  thrown away.  The window starts at the floor, so a fresh queue behaves exactly
  like a serial queue and only speculates once it has evidence that landing is
  working.

## The invariant that outranks all of the above

**Never land an untested tree.**  Every other property here is negotiable and
this one is not.  It is enforced structurally rather than by bookkeeping: an
entry records the *tree* it was tested against (``tested_base_tree``) and the
*tree* the test produced (``tested_merge_tree``), and :func:`landing_is_safe`
refuses the land unless the canonical tip's tree is byte-identical to the tree
the entry was tested on top of.  Comparing trees rather than commit SHAs is what
makes speculation safe *and* survives squash merges, which change the commit but
not the tree.

Ambiguity resolves to NOT landing: an unreadable tip, a missing entry, a lease
we no longer hold, or a queue state we cannot parse all defer through the
publication retry backoff.  None of them can reach "merge anyway".

References: Hoare, "The Not Rocket Science Rule"; bors-ng; GitHub Merge Queue;
Zuul project gating (speculative merge trains, windowed on TCP congestion
control); Kung & Robinson, "On Optimistic Methods for Concurrency Control"
(ACM TODS 1981).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from mac.models import (
    JsonDict,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)

# States an entry can be in.  The first three are live; the last three are
# terminal and are what the partial unique index excludes, so a task may be
# re-admitted after an eviction without colliding with its own history.
STATE_QUEUED = "queued"
STATE_TESTING = "testing"
STATE_TESTED = "tested"
STATE_LANDED = "landed"
STATE_EVICTED = "evicted"
STATE_SUPERSEDED = "superseded"

LIVE_STATES = (STATE_QUEUED, STATE_TESTING, STATE_TESTED)
TERMINAL_STATES = (STATE_LANDED, STATE_EVICTED, STATE_SUPERSEDED)

QUEUE_SCHEMA = "mac.native_merge_queue.v1"

# Serialization modes, recorded in publication evidence as `merge_serialization`
# exactly the way #400 records the forge's.  These strings are a contract: they
# show up in `mac task show` and in the integration proof.
MODE_FORGE_QUEUE = "merge_queue"
MODE_NATIVE_QUEUE = "mac_native_queue"
MODE_DIRECT_SQUASH = "direct_squash"


# ----------------------------------------------------------------------------
# Window sizing: additive increase, multiplicative decrease.
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowBounds:
    """Bounds and step size for the AIMD speculation window.

    ``floor`` of 1 means the degenerate case is a *serial* queue: one entry
    tested against the real tip, landed, then the next.  That is the safe
    behaviour, and it is where every queue starts and where a failing queue is
    driven back to.  ``ceiling`` is the cap the brief asks to be stated: it is
    the maximum number of entries that may hold a speculative slot at once, and
    therefore the maximum number of workers speculation can occupy.
    """

    floor: int = 1
    ceiling: int = 4
    increment: int = 1

    def __post_init__(self) -> None:
        if self.floor < 1:
            raise ValueError("merge queue window floor must be >= 1")
        if self.ceiling < self.floor:
            raise ValueError("merge queue window ceiling must be >= floor")
        if self.increment < 1:
            raise ValueError("merge queue window increment must be >= 1")

    def clamp(self, value: int) -> int:
        return max(self.floor, min(self.ceiling, int(value)))


def next_window(current: int, *, outcome: str, bounds: WindowBounds) -> int:
    """The AIMD step.  ``landed`` grows additively; anything else halves.

    Halving on *any* non-land outcome is deliberate.  A conflict, a failing
    test, and a forge that could not be read all mean the same thing to the
    controller: speculation built on this queue is currently being thrown away,
    so buy less of it.
    """

    size = bounds.clamp(current)
    if outcome == "landed":
        return bounds.clamp(size + bounds.increment)
    return bounds.clamp(size // 2 if size > bounds.floor else size)


def bounds_from_env(environ: Optional[Dict[str, str]] = None) -> WindowBounds:
    """Read the window knobs.

    ``MAC_MERGE_QUEUE_WINDOW_FLOOR`` (default 1), ``..._WINDOW_CEILING``
    (default 4) and ``..._WINDOW_INCREMENT`` (default 1).  A ceiling of 1
    disables speculation entirely and leaves a strictly serial queue, which is
    the documented way to turn speculation off without turning the queue off.
    """

    env = os.environ if environ is None else environ

    def _int(name: str, default: int) -> int:
        try:
            return int(str(env.get(name, "")).strip() or default)
        except (TypeError, ValueError):
            return default

    floor = max(1, _int("MAC_MERGE_QUEUE_WINDOW_FLOOR", 1))
    ceiling = max(floor, _int("MAC_MERGE_QUEUE_WINDOW_CEILING", 4))
    increment = max(1, _int("MAC_MERGE_QUEUE_WINDOW_INCREMENT", 1))
    return WindowBounds(floor=floor, ceiling=ceiling, increment=increment)


def lease_seconds_from_env(environ: Optional[Dict[str, str]] = None) -> int:
    """How long a speculative slot may be held before it is reclaimable.

    ``MAC_MERGE_QUEUE_LEASE_SECONDS``, default 5400 (90 minutes).  The full
    contract suite here runs ~45 minutes and the scoped one ~15, so the default
    is deliberately longer than a slow test run: reclaiming a slot from a worker
    that is still testing wastes the test, while reclaiming one from a hub that
    died is the entire point of the lease.
    """

    env = os.environ if environ is None else environ
    try:
        value = int(str(env.get("MAC_MERGE_QUEUE_LEASE_SECONDS", "")).strip() or 5400)
    except (TypeError, ValueError):
        value = 5400
    return max(60, value)


# ----------------------------------------------------------------------------
# Entries and pure planning.
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueEntry:
    """One approved change awaiting land."""

    id: str
    repository: str
    branch: str
    task_id: str
    pull_request_number: int
    head_sha: str
    state: str
    position: int
    speculation_epoch: int
    tested_base_sha: str = ""
    tested_base_tree: str = ""
    tested_merge_tree: str = ""
    predecessors: Tuple[str, ...] = ()
    lease_owner: str = ""
    lease_expires_at: str = ""
    attempts: int = 0
    eviction_reason: str = ""
    landed_sha: str = ""
    detail: JsonDict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def to_dict(self) -> JsonDict:
        return {
            "schema": QUEUE_SCHEMA,
            "id": self.id,
            "repository": self.repository,
            "branch": self.branch,
            "task_id": self.task_id,
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "state": self.state,
            "position": self.position,
            "speculation_epoch": self.speculation_epoch,
            "tested_base_sha": self.tested_base_sha,
            "tested_base_tree": self.tested_base_tree,
            "tested_merge_tree": self.tested_merge_tree,
            "predecessors": list(self.predecessors),
            "lease_owner": self.lease_owner,
            "attempts": self.attempts,
            "eviction_reason": self.eviction_reason,
            "landed_sha": self.landed_sha,
        }


@dataclass(frozen=True)
class BatchSlot:
    """One entry's place in the speculative batch."""

    entry_id: str
    task_id: str
    head_sha: str
    depth: int
    predecessors: Tuple[str, ...]


def speculation_plan(
    entries: Sequence[QueueEntry], window: int
) -> List[BatchSlot]:
    """Assign each live entry, in order, the heads it must speculate on top of.

    The frontmost entry speculates on nothing (it is tested against the real
    tip); entry *N* speculates on the heads of entries *1..N-1*.  Only the first
    ``window`` live entries get a slot -- that is the bound, and it is what
    keeps speculation from consuming every worker.
    """

    live = [entry for entry in entries if entry.live]
    live.sort(key=lambda item: (item.position, item.id))
    slots: List[BatchSlot] = []
    heads: List[str] = []
    for depth, entry in enumerate(live[: max(1, int(window))]):
        slots.append(
            BatchSlot(
                entry_id=entry.id,
                task_id=entry.task_id,
                head_sha=entry.head_sha,
                depth=depth,
                predecessors=tuple(heads),
            )
        )
        heads.append(entry.head_sha)
    return slots


@dataclass(frozen=True)
class EvictionPlan:
    """What an eviction does to the rest of the batch.

    ``discarded`` is the part that matters and the part that is easy to forget:
    every entry BEHIND the failure was tested against a projected state that
    now will never exist, so its result is worthless even though its tests were
    green.  Keeping those results is precisely how a speculative queue lands an
    untested tree.
    """

    evicted_id: str
    reason: str
    discarded: Tuple[str, ...]
    survivors: Tuple[str, ...]


def plan_eviction(
    entries: Sequence[QueueEntry], failed_entry_id: str, reason: str
) -> EvictionPlan:
    live = [entry for entry in entries if entry.live]
    live.sort(key=lambda item: (item.position, item.id))
    ids = [entry.id for entry in live]
    if failed_entry_id not in ids:
        return EvictionPlan(failed_entry_id, reason, (), tuple(ids))
    index = ids.index(failed_entry_id)
    behind = live[index + 1 :]
    # Only entries that actually carry a speculative result are "discarded";
    # an entry that never got as far as testing has nothing to throw away.
    discarded = tuple(
        entry.id
        for entry in behind
        if entry.state in (STATE_TESTING, STATE_TESTED) or entry.predecessors
    )
    survivors = tuple(entry.id for entry in live if entry.id != failed_entry_id)
    return EvictionPlan(failed_entry_id, reason, discarded, survivors)


def landing_is_safe(
    entry: QueueEntry,
    *,
    canonical_tip_tree: str,
    front_entry_id: str,
) -> Tuple[bool, str]:
    """The one gate that may never be wrong: is it safe to land ``entry`` now?

    Three conditions, all of which fail toward NOT landing:

    1. the entry is the FRONT of its queue -- landing out of order would put a
       tree on the trunk that nothing was tested against;
    2. it actually carries a test result (``tested_base_tree`` /
       ``tested_merge_tree``);
    3. the canonical tip's tree is byte-identical to the tree the entry was
       tested on top of.  A tip that moved -- whether by a concurrent land, a
       human push, or a predecessor landing something other than what we
       speculated -- means the tested projection is stale.

    Anything unreadable (an empty tip tree) is a refusal, not a pass.
    """

    if entry.id != front_entry_id:
        return False, "entry is not at the front of the queue"
    if entry.state not in (STATE_TESTED, STATE_TESTING):
        return False, "entry has no recorded test result (state=%s)" % entry.state
    if not entry.tested_base_tree or not entry.tested_merge_tree:
        return False, "entry carries no tested trees"
    tip = str(canonical_tip_tree or "").strip()
    if not tip:
        return False, "canonical tip tree could not be read"
    if tip != entry.tested_base_tree:
        return False, (
            "canonical tip tree %s is not the tree this entry was tested on top "
            "of (%s); the tested projection is stale"
            % (tip[:12], entry.tested_base_tree[:12])
        )
    return True, ""


@dataclass(frozen=True)
class SlotDecision:
    """Whether this publication attempt may proceed, and on what base."""

    admitted: bool
    entry: Optional[QueueEntry] = None
    predecessors: Tuple[str, ...] = ()
    depth: int = 0
    window: int = 1
    depth_in_queue: int = 0
    reason: str = ""
    defer_seconds: int = 0

    def to_dict(self) -> JsonDict:
        return {
            "schema": "mac.native_merge_queue.slot.v1",
            "admitted": self.admitted,
            "entry_id": self.entry.id if self.entry else "",
            "position": self.entry.position if self.entry else 0,
            "speculation_epoch": self.entry.speculation_epoch if self.entry else 0,
            "predecessors": list(self.predecessors),
            "speculation_depth": self.depth,
            "window": self.window,
            "queue_depth": self.depth_in_queue,
            "reason": self.reason,
            "defer_seconds": self.defer_seconds,
        }


# ----------------------------------------------------------------------------
# The durable queue.
# ----------------------------------------------------------------------------


class NativeMergeQueue:
    """Ledger-backed ordered queue, one per (repository, canonical branch).

    Every mutation is a single conditional UPDATE whose row count is the
    compare-and-swap result, so two hubs racing on the same entry cannot both
    win, and a hub that dies mid-flight leaves a leased row that the next hub
    reclaims once the lease expires.  Nothing is held in process memory: a
    restart re-reads the queue and continues.
    """

    def __init__(
        self,
        store: Any,
        *,
        bounds: Optional[WindowBounds] = None,
        lease_seconds: Optional[int] = None,
        now: Callable[[], str] = utcnow,
        observe: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._store = store
        self._bounds = bounds or bounds_from_env()
        self._lease_seconds = (
            int(lease_seconds) if lease_seconds is not None else lease_seconds_from_env()
        )
        self._now = now
        self._observe_hook = observe

    # -- reading ---------------------------------------------------------

    def entries(self, repository: str, branch: str) -> List[QueueEntry]:
        rows = self._store.query_all(
            """
            SELECT * FROM merge_queue_entries
            WHERE repository = ? AND branch = ?
            ORDER BY position, id
            """,
            (repository, branch),
        )
        return [self._from_row(row) for row in rows]

    def live_entries(self, repository: str, branch: str) -> List[QueueEntry]:
        return [entry for entry in self.entries(repository, branch) if entry.live]

    def entry(self, entry_id: str) -> Optional[QueueEntry]:
        row = self._store.query_one(
            "SELECT * FROM merge_queue_entries WHERE id = ?", (entry_id,)
        )
        return self._from_row(row) if row is not None else None

    def window(self, repository: str, branch: str) -> int:
        row = self._store.query_one(
            "SELECT window_size FROM merge_queue_windows WHERE repository = ? AND branch = ?",
            (repository, branch),
        )
        if row is None:
            return self._bounds.floor
        return self._bounds.clamp(int(row["window_size"]))

    def snapshot(self, repository: str, branch: str) -> JsonDict:
        """Everything an operator needs to watch this queue.

        The brief is explicit about why this exists: this repository has
        produced four separate gates that reported healthy while enforcing
        nothing.  A queue nobody can watch is the next one, so depth, window,
        what is testing, what was evicted and why, and how much speculation has
        been discarded are all first-class here rather than inferred from logs.
        """

        entries = self.entries(repository, branch)
        live = [entry for entry in entries if entry.live]
        row = self._store.query_one(
            """
            SELECT window_size, landed_count, failure_count, speculation_discarded,
                   last_event, updated_at
            FROM merge_queue_windows WHERE repository = ? AND branch = ?
            """,
            (repository, branch),
        )
        evicted = [
            entry for entry in entries if entry.state == STATE_EVICTED
        ][-10:]
        return {
            "schema": "mac.native_merge_queue.snapshot.v1",
            "repository": repository,
            "branch": branch,
            "queue_depth": len(live),
            "window_size": self._bounds.clamp(
                int(row["window_size"]) if row is not None else self._bounds.floor
            ),
            "window_floor": self._bounds.floor,
            "window_ceiling": self._bounds.ceiling,
            "entries_testing": sum(
                1 for entry in live if entry.state == STATE_TESTING
            ),
            "entries_tested": sum(1 for entry in live if entry.state == STATE_TESTED),
            "entries_queued": sum(1 for entry in live if entry.state == STATE_QUEUED),
            "landed_count": int(row["landed_count"]) if row is not None else 0,
            "failure_count": int(row["failure_count"]) if row is not None else 0,
            "speculation_discarded": (
                int(row["speculation_discarded"]) if row is not None else 0
            ),
            "last_event": str(row["last_event"]) if row is not None else "",
            "front": live[0].to_dict() if live else None,
            "live": [entry.to_dict() for entry in live],
            "recent_evictions": [
                {
                    "entry_id": entry.id,
                    "task_id": entry.task_id,
                    "reason": entry.eviction_reason,
                    "at": entry.updated_at,
                }
                for entry in evicted
            ],
        }

    # -- admission -------------------------------------------------------

    def admit(
        self,
        *,
        repository: str,
        branch: str,
        task_id: str,
        head_sha: str,
        pull_request_number: int = 0,
        detail: Optional[JsonDict] = None,
    ) -> QueueEntry:
        """Place ``task_id`` in the queue, or return the entry it already has.

        Idempotent by construction: publication is retried, and a retry must
        rejoin the queue where it already stands rather than queue a second
        time.  A retry whose reviewed head CHANGED supersedes the old entry --
        the previous entry's test result was for a different commit and must
        not be reused.
        """

        now = self._now()
        existing = self._live_entry_for_task(repository, branch, task_id)
        if existing is not None:
            if existing.head_sha == head_sha:
                return existing
            self._terminate(
                existing.id,
                STATE_SUPERSEDED,
                reason="reviewed head moved from %s to %s"
                % (existing.head_sha[:12], str(head_sha)[:12]),
            )
        position = self._next_position(repository, branch)
        entry_id = new_id("mergeq")
        self._store.execute(
            """
            INSERT INTO merge_queue_entries (
                id, repository, branch, task_id, pull_request_number, head_sha,
                state, position, speculation_epoch, tested_base_sha,
                tested_base_tree, tested_merge_tree, predecessors, lease_owner,
                lease_expires_at, attempts, eviction_reason, landed_sha, detail,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '[]', NULL, NULL,
                      0, '', '', ?, ?, ?)
            """,
            (
                entry_id,
                repository,
                branch,
                task_id,
                int(pull_request_number or 0),
                str(head_sha),
                STATE_QUEUED,
                position,
                self._current_epoch(repository, branch),
                json_dumps(ensure_json_object(detail)),
                now,
                now,
            ),
        )
        self._ensure_window_row(repository, branch)
        self._emit(
            "merge_queue.admitted",
            repository,
            branch,
            {
                "entry_id": entry_id,
                "task_id": task_id,
                "position": position,
                "value": position,
            },
        )
        admitted = self.entry(entry_id)
        assert admitted is not None
        return admitted

    def claim_slot(
        self,
        *,
        repository: str,
        branch: str,
        task_id: str,
        head_sha: str,
        owner: str,
        pull_request_number: int = 0,
        detail: Optional[JsonDict] = None,
    ) -> SlotDecision:
        """Admit, then hand out a speculative slot if the window has room.

        Returns ``admitted=False`` with a ``defer_seconds`` when the window is
        full.  Deferring is the correct answer, not a failure: the publication
        retry backoff already exists, the PR is already open, and the entry
        keeps its place in line.
        """

        entry = self.admit(
            repository=repository,
            branch=branch,
            task_id=task_id,
            head_sha=head_sha,
            pull_request_number=pull_request_number,
            detail=detail,
        )
        self._reclaim_expired(repository, branch)
        live = self.live_entries(repository, branch)
        window = self.window(repository, branch)
        plan = speculation_plan(live, window)
        slot = next((item for item in plan if item.entry_id == entry.id), None)
        if slot is None:
            position_in_line = next(
                (index for index, item in enumerate(live) if item.id == entry.id),
                len(live),
            )
            return SlotDecision(
                admitted=False,
                entry=entry,
                window=window,
                depth_in_queue=len(live),
                reason=(
                    "merge queue window is %d and this entry is #%d in line"
                    % (window, position_in_line + 1)
                ),
                defer_seconds=300,
            )
        # CAS the slot. A row already leased by a live owner is not ours.
        now = self._now()
        expires = (
            parse_time(now) + timedelta(seconds=self._lease_seconds)
        ).isoformat(timespec="microseconds")
        result = self._store.execute(
            """
            UPDATE merge_queue_entries
               SET state = ?, lease_owner = ?, lease_expires_at = ?,
                   predecessors = ?, attempts = attempts + 1, updated_at = ?
             WHERE id = ?
               AND state IN (?, ?, ?)
               AND (lease_owner IS NULL OR lease_owner = '' OR lease_owner = ?
                    OR lease_expires_at IS NULL OR lease_expires_at < ?)
            """,
            (
                STATE_TESTING,
                owner,
                expires,
                json_dumps(list(slot.predecessors)),
                now,
                entry.id,
                STATE_QUEUED,
                STATE_TESTING,
                STATE_TESTED,
                owner,
                now,
            ),
        )
        if int(getattr(result, "rowcount", 0) or 0) < 1:
            return SlotDecision(
                admitted=False,
                entry=entry,
                window=window,
                depth_in_queue=len(live),
                reason="merge queue slot is leased by another worker",
                defer_seconds=300,
            )
        claimed = self.entry(entry.id)
        assert claimed is not None
        self._emit(
            "merge_queue.slot_claimed",
            repository,
            branch,
            {
                "entry_id": claimed.id,
                "task_id": task_id,
                "speculation_depth": slot.depth,
                "predecessors": list(slot.predecessors),
                "window": window,
                "value": slot.depth,
            },
        )
        return SlotDecision(
            admitted=True,
            entry=claimed,
            predecessors=slot.predecessors,
            depth=slot.depth,
            window=window,
            depth_in_queue=len(live),
        )

    # -- test results ----------------------------------------------------

    def record_tested(
        self,
        entry_id: str,
        *,
        owner: str,
        base_sha: str,
        base_tree: str,
        merge_tree: str,
    ) -> bool:
        """Attach the projected-merge result to the entry, under our lease."""

        now = self._now()
        result = self._store.execute(
            """
            UPDATE merge_queue_entries
               SET state = ?, tested_base_sha = ?, tested_base_tree = ?,
                   tested_merge_tree = ?, updated_at = ?
             WHERE id = ? AND lease_owner = ? AND state IN (?, ?)
            """,
            (
                STATE_TESTED,
                str(base_sha),
                str(base_tree),
                str(merge_tree),
                now,
                entry_id,
                owner,
                STATE_TESTING,
                STATE_TESTED,
            ),
        )
        return int(getattr(result, "rowcount", 0) or 0) >= 1

    def front(self, repository: str, branch: str) -> Optional[QueueEntry]:
        live = self.live_entries(repository, branch)
        return live[0] if live else None

    def may_land(
        self, entry_id: str, *, canonical_tip_tree: str
    ) -> Tuple[bool, str, Optional[QueueEntry]]:
        """Re-read the entry from the ledger and apply :func:`landing_is_safe`.

        Re-reading is the point: the caller may have spent 45 minutes testing
        since it claimed the slot, and the decision must be made on the state
        that exists now, not on a snapshot from before the test run.
        """

        entry = self.entry(entry_id)
        if entry is None:
            return False, "merge queue entry disappeared", None
        if entry.state in TERMINAL_STATES:
            return False, "merge queue entry is %s" % entry.state, entry
        front = self.front(entry.repository, entry.branch)
        ok, reason = landing_is_safe(
            entry,
            canonical_tip_tree=canonical_tip_tree,
            front_entry_id=front.id if front else "",
        )
        return ok, reason, entry

    # -- terminal transitions -------------------------------------------

    def record_landed(self, entry_id: str, *, landed_sha: str) -> JsonDict:
        """Mark the entry landed and grow the window.

        Idempotent: an entry already recorded as landed returns the same
        outcome rather than bumping the window twice.  That matters because the
        forge may have landed the PR between attempts, and observing that is a
        success, not a second land.
        """

        entry = self.entry(entry_id)
        if entry is None:
            return {"changed": False, "reason": "entry not found"}
        if entry.state == STATE_LANDED:
            return {
                "changed": False,
                "reason": "already landed",
                "window_size": self.window(entry.repository, entry.branch),
            }
        now = self._now()
        result = self._store.execute(
            """
            UPDATE merge_queue_entries
               SET state = ?, landed_sha = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ?
             WHERE id = ? AND state <> ?
            """,
            (STATE_LANDED, str(landed_sha or ""), now, entry_id, STATE_LANDED),
        )
        if int(getattr(result, "rowcount", 0) or 0) < 1:
            return {"changed": False, "reason": "entry already terminal"}
        window = self._step_window(
            entry.repository,
            entry.branch,
            outcome="landed",
            event="landed %s" % entry.task_id,
        )
        self._emit(
            "merge_queue.landed",
            entry.repository,
            entry.branch,
            {
                "entry_id": entry_id,
                "task_id": entry.task_id,
                "landed_sha": str(landed_sha or "")[:40],
                "window_size": window,
                "value": window,
            },
        )
        return {"changed": True, "window_size": window}

    def evict(self, entry_id: str, *, reason: str) -> JsonDict:
        """Evict a failed entry and discard every speculative result behind it.

        This is the half of speculation that keeps it honest.  Entries behind
        the failure were green -- against a tree that will never exist.  They go
        back to ``queued`` in a NEW speculation epoch with their test results
        cleared, so nothing downstream can present a stale result as a pass.
        """

        entry = self.entry(entry_id)
        if entry is None:
            return {"changed": False, "reason": "entry not found"}
        repository, branch = entry.repository, entry.branch
        plan = plan_eviction(self.entries(repository, branch), entry_id, reason)
        now = self._now()
        self._store.execute(
            """
            UPDATE merge_queue_entries
               SET state = ?, eviction_reason = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ?
             WHERE id = ? AND state IN (?, ?, ?)
            """,
            (
                STATE_EVICTED,
                str(reason)[:500],
                now,
                entry_id,
                STATE_QUEUED,
                STATE_TESTING,
                STATE_TESTED,
            ),
        )
        epoch = self._bump_epoch(repository, branch)
        discarded = 0
        for other_id in plan.survivors:
            other = self.entry(other_id)
            if other is None or not other.live:
                continue
            had_result = bool(other.tested_base_tree) or bool(other.predecessors)
            self._store.execute(
                """
                UPDATE merge_queue_entries
                   SET state = ?, tested_base_sha = '', tested_base_tree = '',
                       tested_merge_tree = '', predecessors = '[]',
                       speculation_epoch = ?, lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = ?
                 WHERE id = ? AND state IN (?, ?, ?)
                """,
                (
                    STATE_QUEUED,
                    epoch,
                    now,
                    other_id,
                    STATE_QUEUED,
                    STATE_TESTING,
                    STATE_TESTED,
                ),
            )
            if had_result:
                discarded += 1
        window = self._step_window(
            repository,
            branch,
            outcome="failed",
            event="evicted %s: %s" % (entry.task_id, str(reason)[:120]),
            discarded=discarded,
        )
        self._emit(
            "merge_queue.evicted",
            repository,
            branch,
            {
                "entry_id": entry_id,
                "task_id": entry.task_id,
                "reason": str(reason)[:300],
                "speculation_discarded": discarded,
                "discarded_entries": list(plan.discarded),
                "survivors": list(plan.survivors),
                "window_size": window,
                "speculation_epoch": epoch,
                "value": discarded,
            },
            level="warning",
        )
        return {
            "changed": True,
            "window_size": window,
            "speculation_discarded": discarded,
            "discarded_entries": list(plan.discarded),
            "survivors": list(plan.survivors),
            "speculation_epoch": epoch,
        }

    def release(self, entry_id: str, *, owner: str) -> bool:
        """Give the slot back without a verdict (a deferral, not a failure).

        The entry returns to ``queued``, not merely un-leased.  Leaving it in
        ``testing`` with no owner made ``snapshot()['entries_testing']`` count
        an entry nobody was testing -- a queue that reports work in flight that
        is not in flight is the same lie as a gate that reports healthy while
        enforcing nothing.  Any partial result is dropped with it, for the same
        reason :meth:`_reclaim_expired` drops one: it was produced for a
        position this entry may not re-take.
        """

        result = self._store.execute(
            """
            UPDATE merge_queue_entries
               SET lease_owner = NULL, lease_expires_at = NULL, state = ?,
                   tested_base_sha = '', tested_base_tree = '',
                   tested_merge_tree = '', updated_at = ?
             WHERE id = ? AND lease_owner = ? AND state IN (?, ?, ?)
            """,
            (
                STATE_QUEUED,
                self._now(),
                entry_id,
                owner,
                STATE_QUEUED,
                STATE_TESTING,
                STATE_TESTED,
            ),
        )
        return int(getattr(result, "rowcount", 0) or 0) >= 1

    # -- internals -------------------------------------------------------

    def _live_entry_for_task(
        self, repository: str, branch: str, task_id: str
    ) -> Optional[QueueEntry]:
        row = self._store.query_one(
            """
            SELECT * FROM merge_queue_entries
             WHERE repository = ? AND branch = ? AND task_id = ?
               AND state IN (?, ?, ?)
             ORDER BY position DESC LIMIT 1
            """,
            (repository, branch, task_id, *LIVE_STATES),
        )
        return self._from_row(row) if row is not None else None

    def _next_position(self, repository: str, branch: str) -> int:
        row = self._store.query_one(
            "SELECT MAX(position) AS top FROM merge_queue_entries WHERE repository = ? AND branch = ?",
            (repository, branch),
        )
        top = row["top"] if row is not None else None
        return int(top or 0) + 1

    def _current_epoch(self, repository: str, branch: str) -> int:
        row = self._store.query_one(
            "SELECT MAX(speculation_epoch) AS epoch FROM merge_queue_entries WHERE repository = ? AND branch = ?",
            (repository, branch),
        )
        epoch = row["epoch"] if row is not None else None
        return int(epoch or 0)

    def _bump_epoch(self, repository: str, branch: str) -> int:
        return self._current_epoch(repository, branch) + 1

    def _reclaim_expired(self, repository: str, branch: str) -> int:
        """Return slots whose holder died.  This is the crash-safety hinge.

        A hub that dies between claiming a slot and landing leaves the row in
        ``testing`` with a lease that stops being renewed.  Once it expires the
        slot is reclaimable; the entry keeps its POSITION, so recovery resumes
        the queue rather than reordering it.
        """

        result = self._store.execute(
            """
            UPDATE merge_queue_entries
               SET lease_owner = NULL, lease_expires_at = NULL,
                   state = ?, tested_base_sha = '', tested_base_tree = '',
                   tested_merge_tree = '', updated_at = ?
             WHERE repository = ? AND branch = ? AND state IN (?, ?)
               AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
            """,
            (
                STATE_QUEUED,
                self._now(),
                repository,
                branch,
                STATE_TESTING,
                STATE_TESTED,
                self._now(),
            ),
        )
        reclaimed = int(getattr(result, "rowcount", 0) or 0)
        if reclaimed:
            self._emit(
                "merge_queue.lease_reclaimed",
                repository,
                branch,
                {"reclaimed": reclaimed, "value": reclaimed},
                level="warning",
            )
        return reclaimed

    def _ensure_window_row(self, repository: str, branch: str) -> None:
        now = self._now()
        self._store.execute(
            """
            INSERT INTO merge_queue_windows (
                repository, branch, window_size, landed_count, failure_count,
                speculation_discarded, last_event, updated_at
            ) VALUES (?, ?, ?, 0, 0, 0, '', ?)
            ON CONFLICT(repository, branch) DO NOTHING
            """,
            (repository, branch, self._bounds.floor, now),
        )

    def _step_window(
        self,
        repository: str,
        branch: str,
        *,
        outcome: str,
        event: str,
        discarded: int = 0,
    ) -> int:
        self._ensure_window_row(repository, branch)
        current = self.window(repository, branch)
        size = next_window(current, outcome=outcome, bounds=self._bounds)
        landed = 1 if outcome == "landed" else 0
        failed = 0 if outcome == "landed" else 1
        self._store.execute(
            """
            UPDATE merge_queue_windows
               SET window_size = ?, landed_count = landed_count + ?,
                   failure_count = failure_count + ?,
                   speculation_discarded = speculation_discarded + ?,
                   last_event = ?, updated_at = ?
             WHERE repository = ? AND branch = ?
            """,
            (
                size,
                landed,
                failed,
                int(discarded),
                str(event)[:300],
                self._now(),
                repository,
                branch,
            ),
        )
        return size

    def _terminate(self, entry_id: str, state: str, *, reason: str) -> None:
        self._store.execute(
            """
            UPDATE merge_queue_entries
               SET state = ?, eviction_reason = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ?
             WHERE id = ?
            """,
            (state, str(reason)[:500], self._now(), entry_id),
        )

    def _emit(
        self,
        name: str,
        repository: str,
        branch: str,
        detail: JsonDict,
        *,
        level: str = "info",
    ) -> None:
        if self._observe_hook is None:
            return
        payload = dict(detail)
        payload.update({"repository": repository, "branch": branch})
        try:
            # ``observe`` is ControlPlane.record_metric: (name, value, ...).
            # The value carries the number that matters for that event so a
            # `GET /observability/metrics?name=merge_queue.depth` is a real
            # time series and not a bag of JSON blobs.
            self._observe_hook(
                name,
                float(payload.get("value", 1) or 0),
                unit="count",
                layer="control_plane",
                source="native-merge-queue",
                level=level,
                subject_type="repository",
                subject_id="%s#%s" % (repository, branch),
                detail=payload,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break a land.
            pass

    @staticmethod
    def _from_row(row: Any) -> QueueEntry:
        return QueueEntry(
            id=row["id"],
            repository=row["repository"],
            branch=row["branch"],
            task_id=row["task_id"],
            pull_request_number=int(row["pull_request_number"] or 0),
            head_sha=row["head_sha"] or "",
            state=row["state"],
            position=int(row["position"] or 0),
            speculation_epoch=int(row["speculation_epoch"] or 0),
            tested_base_sha=row["tested_base_sha"] or "",
            tested_base_tree=row["tested_base_tree"] or "",
            tested_merge_tree=row["tested_merge_tree"] or "",
            predecessors=tuple(json_loads(row["predecessors"], []) or []),
            lease_owner=row["lease_owner"] or "",
            lease_expires_at=row["lease_expires_at"] or "",
            attempts=int(row["attempts"] or 0),
            eviction_reason=row["eviction_reason"] or "",
            landed_sha=row["landed_sha"] or "",
            detail=ensure_json_object(json_loads(row["detail"], {})),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


__all__ = [
    "MODE_DIRECT_SQUASH",
    "MODE_FORGE_QUEUE",
    "MODE_NATIVE_QUEUE",
    "BatchSlot",
    "EvictionPlan",
    "NativeMergeQueue",
    "QueueEntry",
    "SlotDecision",
    "WindowBounds",
    "bounds_from_env",
    "landing_is_safe",
    "lease_seconds_from_env",
    "next_window",
    "plan_eviction",
    "speculation_plan",
]
