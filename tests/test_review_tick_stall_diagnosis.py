"""Characterization tests for the REVIEWING-backlog stall diagnosis.

Diagnosis-only child of "Unwedge review advancement without granting workers
admin scope". These tests PIN the current (stalling) behavior against the real
shipped code so the sibling fix tasks have a red/green target and so a future
refactor cannot silently reintroduce the same stall. They make NO behavioral
change; where a test asserts a defect (C1, C2) it asserts the *current* code
shape and is annotated with the sibling concern that will change it.

Companion document: docs/review-tick-stall-diagnosis.md.

Postgres is not required: every assertion is either source introspection of the
real functions (`inspect.getsource`) or exercises a DB-free helper
(`ReconciliationCoordinator` against a fake store, `resolve_hub_agent`,
`_hub_review_verify_enabled`, the cursor codec via a stub).
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from mac import api, services
from mac.env_config import resolve_hub_agent
from mac.models import parse_time, utcnow
from mac.reconciliation import ReconciliationCoordinator
from mac.services import ControlPlane, _hub_review_verify_enabled


# --------------------------------------------------------------------------- #
# C1 -- the non-blocking nudge is dropped unless the event consumer is running #
# --------------------------------------------------------------------------- #


class _NudgeOnlyControlPlane:
    """Minimal stand-in exposing just the nudge machinery.

    ``_nudge_review_workflow`` only touches ``self._advance_queue``,
    ``self._advance_queued`` and ``self._advance_state_lock``; binding the real
    method to this object exercises the shipped logic without a database.
    """

    def __init__(self, queue):
        import threading

        self._advance_queue = queue
        self._advance_queued = set()
        self._advance_state_lock = threading.Lock()

    nudge = ControlPlane._nudge_review_workflow


class _RecordingQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def test_nudge_is_a_noop_without_the_event_consumer():
    """C1: with ``_advance_queue is None`` (its constructor default) the nudge
    is silently discarded -- the task is never re-queued for advancement.

    Sibling fix: decouple the nudge sink from the tick gate / make the drop
    observable (see docs recommended fix #1)."""
    cp = _NudgeOnlyControlPlane(queue=None)

    cp.nudge("task_frozen")

    assert cp._advance_queued == set(), (
        "a dropped nudge must not even be marked queued"
    )


def test_nudge_enqueues_when_the_consumer_is_running():
    """Counterpart: once ``enable_event_driven_review_advance`` has set the
    queue, the same nudge is delivered. This proves the drop in the test above
    is caused by the missing consumer, not by the nudge logic itself."""
    q = _RecordingQueue()
    cp = _NudgeOnlyControlPlane(queue=q)

    cp.nudge("task_frozen")

    assert q.items == ["task_frozen"]
    assert cp._advance_queued == {"task_frozen"}


def test_advance_queue_defaults_to_none_in_the_constructor():
    """C1 precondition: nothing populates ``_advance_queue`` unless the event
    consumer is explicitly enabled, so the nudge no-op is the default state."""
    src = inspect.getsource(ControlPlane.__init__)
    assert "self._advance_queue: Optional[Any] = None" in src


def test_nudge_noop_branch_returns_without_recording():
    """C1: the drop path returns early with no telemetry -- which is exactly why
    the stall was invisible. Pinning this makes the sibling fix (emit a
    diagnostic) a visible diff."""
    src = inspect.getsource(ControlPlane._nudge_review_workflow)
    assert "if q is None or not task_id:" in src
    assert "return" in src
    # No observation/log call on the drop path today.
    assert "record_log" not in src and "_record_default_review_observation" not in src


def test_nonblocking_sweep_branch_only_nudges():
    """C1: when hub verify is not allowed to block, the workflow takes the
    else-branch and only nudges -- it never verifies. Combined with the test
    above, an unrunning consumer means that task cannot advance from a
    non-blocking sweep."""
    src = inspect.getsource(ControlPlane.advance_default_review_workflow)
    assert "if allow_blocking_hub_verify:" in src
    assert "self._run_hub_review_verification(task, review, evidence, actor)" in src
    assert "self._nudge_review_workflow(task.id)" in src


def test_event_consumer_is_gated_by_the_tick_interval():
    """C1: the event-driven consumer is only started from the hub tick loop,
    which is gated by MAC_HUB_TICK_INTERVAL_SECONDS. Any process with the tick
    disabled has ``_advance_queue is None`` and therefore drops nudges."""
    tick_src = inspect.getsource(api._start_hub_tick_loop)
    assert "MAC_HUB_TICK_INTERVAL_SECONDS" in tick_src
    assert "cp.enable_event_driven_review_advance()" in tick_src


def test_enable_event_driven_review_advance_populates_the_queue():
    """The only place ``_advance_queue`` becomes non-None."""
    src = inspect.getsource(ControlPlane.enable_event_driven_review_advance)
    assert "self._advance_queue = q" in src


# --------------------------------------------------------------------------- #
# C2 -- uncapped waiting_for_hub_verify traps a pushed change forever          #
# --------------------------------------------------------------------------- #


def test_waiting_for_hub_verify_has_no_iteration_ceiling():
    """C2: when hub verify produces no verdict, the workflow returns
    ``waiting_for_hub_verify`` for hub-verifiable evidence with no attempt/age
    cap, so every sweep re-parks the same task.

    Sibling fix: bound the loop and retract after N attempts (see docs
    recommended fix #2). This test pins the *absence* of a ceiling today."""
    src = inspect.getsource(ControlPlane.advance_default_review_workflow)
    assert '"status": "waiting_for_hub_verify"' in src
    # There is no attempt-count / age ceiling gating that return today.
    for ceiling_marker in (
        "max_hub_verify_attempts",
        "hub_verify_attempt",
        "waiting_for_hub_verify_attempts",
        "hub_verify_deadline",
    ):
        assert ceiling_marker not in src, (
            "a ceiling appeared -- update this diagnosis test and the doc; the "
            "sibling fix for C2 may have landed"
        )


def test_hub_verifiable_evidence_holds_the_merge_gate():
    """C2: the merge gate is held only for evidence that is actually
    hub-verifiable (a pushed repo change); non-verifiable evidence deliberately
    falls through to the agent-nudge path. This distinguishes C2 from the
    intended no-evidence behavior."""
    src = inspect.getsource(ControlPlane.advance_default_review_workflow)
    assert "hub_verifiable = (" in src
    assert "self._hub_verify_repo_info(task, evidence) is not None" in src
    assert "if hub_verifiable:" in src


def test_inflight_guard_is_unbounded_per_review():
    """C2 mechanism: the in-flight guard parks a review id and short-circuits
    every subsequent verify with ``return None`` while a verify is 'in
    progress'. Nothing ages the id out, so a verify that never completes keeps
    returning None -> no verdict -> waiting_for_hub_verify forever."""
    src = inspect.getsource(ControlPlane._run_hub_review_verification)
    assert "if review.id in inflight:" in src
    assert "return None" in src


# --------------------------------------------------------------------------- #
# R1 -- cursor starvation is RULED OUT (the cursor wraps, it does not park)     #
# --------------------------------------------------------------------------- #


class _CursorStub:
    """Bind only the cursor codec + next_cursor logic, no database."""

    _encode_review_sweep_cursor = ControlPlane._encode_review_sweep_cursor
    _decode_review_sweep_cursor = ControlPlane._decode_review_sweep_cursor


class _FakeTask:
    def __init__(self, priority, created_at, task_id):
        self.priority = priority
        self.created_at = created_at
        self.id = task_id


def test_cursor_roundtrips_priority_created_at_and_id():
    stub = _CursorStub()
    task = _FakeTask(5, "2026-07-29T22:46:00+00:00", "task_abc")

    encoded = stub._encode_review_sweep_cursor(task)
    priority, created_at, task_id = stub._decode_review_sweep_cursor(encoded)

    assert (priority, created_at, task_id) == (5, "2026-07-29T22:46:00+00:00", "task_abc")
    assert encoded.startswith("v1:")


def test_last_page_persists_null_cursor():
    """R1: ``next_cursor`` is set only when ``has_more`` -- so the final page
    persists None and the sweep restarts from the head rather than parking."""
    src = inspect.getsource(ControlPlane.advance_default_review_workflows)
    assert "if has_more and tasks" in src


def test_cursor_resets_to_head_when_no_more():
    """R1: the sweep page persists ``result.get('next_cursor')`` which is None on
    the last page, so the durable reconciliation cursor wraps to the head. A
    subset of tasks is therefore revisited on a bounded cycle -- not starved."""
    src = inspect.getsource(ControlPlane._advance_default_review_sweep_page)
    assert 'cursor=result.get("next_cursor")' in src


def test_cursor_advances_monotonically_within_a_page_run():
    """R1: the WHERE clause strictly advances past the cursor tuple
    (priority, created_at, id), so a full page moves forward and cannot re-serve
    the same head row within a run."""
    src = inspect.getsource(ControlPlane.advance_default_review_workflows)
    assert "priority < ?" in src
    assert "priority = ? AND created_at > ?" in src
    assert "priority = ? AND created_at = ? AND id > ?" in src


# --------------------------------------------------------------------------- #
# R2 -- hub-agent name/id matching is RULED OUT                                #
# --------------------------------------------------------------------------- #


def test_hub_agent_matches_by_name_or_id():
    """R2: the heartbeat guard accepts a match on EITHER name or id, so
    MAC_REVIEW_TICK_HUB_AGENT=rocky matches an agent named or ided 'rocky'."""
    src = inspect.getsource(ControlPlane._maybe_advance_reviews_on_heartbeat)
    assert "if agent.name != hub_agent and agent.id != hub_agent:" in src


def test_resolve_hub_agent_returns_the_configured_value_verbatim():
    """R2: no transformation of the configured hub-agent value that could cause
    a name/id mismatch."""
    assert resolve_hub_agent(
        "MAC_REVIEW_TICK_HUB_AGENT", environ={"MAC_REVIEW_TICK_HUB_AGENT": "rocky"}
    ) == "rocky"
    assert resolve_hub_agent(
        "MAC_REVIEW_TICK_HUB_AGENT", environ={"MAC_REVIEW_TICK_HUB_AGENT": " rocky "}
    ) == "rocky"
    assert resolve_hub_agent(
        "MAC_REVIEW_TICK_HUB_AGENT", environ={}
    ) == ""


def test_heartbeat_review_tick_is_off_by_default():
    """R2: the heartbeat path is default OFF, so it is not the current driver --
    advancement is expected from the publication worker instead."""
    src = inspect.getsource(ControlPlane._maybe_advance_reviews_on_heartbeat)
    assert 'if not _truthy_env("MAC_REVIEW_TICK_ON_HEARTBEAT", "0"):' in src


def test_publication_worker_default_depends_on_tick_interval():
    """Context for R2/C1: the sweep worker (the actual driver) defaults ON only
    when the hub tick interval is set, and always blocks-to-verify."""
    src = inspect.getsource(api._start_publication_worker)
    assert '"30" if tick_interval > 0 else "0"' in src
    assert "allow_blocking_hub_verify=True" in src


# --------------------------------------------------------------------------- #
# R3 -- ordinary reconciliation lease expiry is RULED OUT (a fake store proves #
#        a stale lease short-circuits but is reclaimed after expiry)           #
# --------------------------------------------------------------------------- #


class _FakeRow(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class _FakeConn:
    """A one-row emulation of the reconciliation_state upsert + select.

    Implements exactly the two statements ``claim`` issues: the conditional
    UPSERT and the follow-up SELECT. Enough to exercise the real lease predicate
    (``lease_owner IS NULL OR lease_expires_at <= updated_at``) without Postgres.
    """

    def __init__(self, state):
        self.state = state  # {name: {"cursor", "lease_owner", "lease_expires_at"}}

    def execute(self, sql, params):
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith("INSERT INTO reconciliation_state"):
            name, claim_owner, expires_at, now = params
            row = self.state.get(name)
            if row is None:
                self.state[name] = {
                    "cursor": None,
                    "lease_owner": claim_owner,
                    "lease_expires_at": expires_at,
                }
            else:
                free = row["lease_owner"] is None or row["lease_expires_at"] <= now
                if free:
                    row["lease_owner"] = claim_owner
                    row["lease_expires_at"] = expires_at
            return self
        if sql_norm.startswith("SELECT cursor, lease_owner"):
            (name,) = params
            row = self.state.get(name)
            if row is None:
                self._fetch = None
            else:
                self._fetch = _FakeRow(
                    cursor=row["cursor"], lease_owner=row["lease_owner"]
                )
            return self
        raise AssertionError("unexpected sql: %s" % sql_norm)

    def fetchone(self):
        return self._fetch


class _FakeTxn:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


class _FakeStore:
    def __init__(self):
        self.state = {}
        self.conn = _FakeConn(self.state)

    def transaction(self):
        return _FakeTxn(self.conn)

    def execute(self, sql, params):
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith("UPDATE reconciliation_state"):
            cursor, now, name, owner = params
            row = self.state.get(name)
            changed = 0
            if row is not None and row["lease_owner"] == owner:
                row["cursor"] = cursor
                row["lease_owner"] = None
                row["lease_expires_at"] = None
                changed = 1
            return type("R", (), {"rowcount": changed})()
        raise AssertionError("unexpected sql: %s" % sql_norm)


def test_stale_lease_short_circuits_until_expiry():
    """R3: while a lease is held (not expired), a concurrent claim returns
    None -- the sweep short-circuits with skipped=lease_held."""
    store = _FakeStore()
    a = ReconciliationCoordinator(store, owner_id="owner-a", lease_seconds=3600)
    b = ReconciliationCoordinator(store, owner_id="owner-b", lease_seconds=3600)

    claim_a = a.claim("default-review-sweep")
    assert claim_a is not None
    # A never completes/abandons -> lease stays held.
    assert b.claim("default-review-sweep") is None


def test_expired_lease_is_reclaimable():
    """R3: once ``lease_expires_at`` is in the past, the next claim reacquires --
    so ordinary expiry recovers the sweep rather than wedging it forever."""
    store = _FakeStore()
    a = ReconciliationCoordinator(store, owner_id="owner-a", lease_seconds=3600)
    claim_a = a.claim("default-review-sweep")
    assert claim_a is not None

    # Simulate the lease having expired.
    store.state["default-review-sweep"]["lease_expires_at"] = (
        parse_time(utcnow()) - timedelta(seconds=1)
    ).isoformat(timespec="microseconds")

    b = ReconciliationCoordinator(store, owner_id="owner-b", lease_seconds=3600)
    reclaimed = b.claim("default-review-sweep")
    assert reclaimed is not None
    assert reclaimed.owner_id.startswith("owner-b")


def test_lease_release_requires_the_owning_claim_id():
    """R3 detail: complete/abandon only release the row whose lease_owner
    matches the exact per-call claim id, so a stale owner cannot be 'completed'
    by another process -- expiry is the only cross-process recovery."""
    store = _FakeStore()
    a = ReconciliationCoordinator(store, owner_id="owner-a", lease_seconds=3600)
    claim_a = a.claim("default-review-sweep")

    # A different coordinator cannot release A's lease via a forged claim.
    from mac.reconciliation import ReconciliationClaim

    forged = ReconciliationClaim(
        name="default-review-sweep", owner_id="someone-else", cursor=None
    )
    b = ReconciliationCoordinator(store, owner_id="owner-b", lease_seconds=3600)
    assert b.complete(forged, cursor=None) is False
    # The real owner can.
    assert a.complete(claim_a, cursor="v1:next") is True
    assert store.state["default-review-sweep"]["lease_owner"] is None
    assert store.state["default-review-sweep"]["cursor"] == "v1:next"


def test_lease_seconds_is_clamped():
    """R3 detail: the lease is bounded (1..3600, default 60), so a stale lease
    can only delay a page by one bounded interval, never permanently."""
    store = _FakeStore()
    assert ReconciliationCoordinator(store, lease_seconds=999999).lease_seconds == 3600
    assert ReconciliationCoordinator(store, lease_seconds=0).lease_seconds == 1
    assert ReconciliationCoordinator(store, lease_seconds=None).lease_seconds == 60


def test_abandon_preserves_the_durable_cursor():
    """R3 detail: a failed page releases the lease WITHOUT advancing the cursor,
    so a crash mid-page does not skip rows -- another reason the cursor does not
    starve tasks (supports R1 too)."""
    src = inspect.getsource(ReconciliationCoordinator.abandon)
    assert "cursor=claim.cursor" in src


# --------------------------------------------------------------------------- #
# Option-C enablement flag -- context that both C1 and C2 depend on            #
# --------------------------------------------------------------------------- #


def test_hub_review_verify_flag_parsing():
    assert _hub_review_verify_enabled({"MAC_REVIEW_HUB_VERIFY": "1"}) is True
    assert _hub_review_verify_enabled({"MAC_REVIEW_HUB_VERIFY": "true"}) is True
    assert _hub_review_verify_enabled({"MAC_REVIEW_HUB_VERIFY": "on"}) is True
    assert _hub_review_verify_enabled({"MAC_REVIEW_HUB_VERIFY": "0"}) is False
    assert _hub_review_verify_enabled({}) is False
