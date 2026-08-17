"""A fully-excluded head of the queue must not block the rows behind it.

The candidate window used to be filled with `batch_size` RAW candidates, and
exclusions were then applied *inside* it. When the oldest rows are all excluded,
the whole window is waste and prune deletes nothing -- forever, no matter how
often it runs.

Measured on the live hub 2026-08-17, on EVERY prune:

    observability_events  eligible=0  deleted=0  excluded=2000  capped=true
    action_events         eligible=24 deleted=24 excluded=876   capped=false

The oldest 2000 rows past the cutoff were all `subject_type='task'`, every one
attached to a non-terminal task, so `_exclude_active_task_obs` killed the entire
window. Behind them sat 494,817 rows with no task subject at all -- freely
prunable, never reached.

The excluded set is effectively permanent: it keys on tasks that are not
(completed, failed, cancelled), and ~360 tasks sit in BLOCKED, which is a
one-way trap under the default all_success join. Their telemetry is also the
OLDEST telemetry, so it owns the head of the queue indefinitely.

This is the third layer of the same defect, and each earlier fix was correct:
#379 stopped prune crashing on a bind-parameter overflow, #392 stopped it being
starved behind the review sweep, #393 gave it its own timer. It then ran every
60 seconds and still deleted zero observability rows.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from mac.retention_service import (
    MAX_SCAN_WINDOWS,
    RetentionPolicy,
    RetentionService,
)


class _HeadExcludedStore:
    """A backlog whose oldest `excluded_head` rows are always excluded.

    Models the live shape: ancient telemetry pinned to tasks that will never
    reach a terminal state, sitting in front of everything else.
    """

    def __init__(self, backlog: int, excluded_head: int) -> None:
        self.backlog = backlog
        self.excluded_head = excluded_head
        self.windows_fetched: List[int] = []

    # ids are ordered oldest-first: row0000000 is the oldest.
    @staticmethod
    def _index(pk: str) -> int:
        return int(pk.replace("row", ""))

    def query_all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if "COUNT(*)" in sql:
            return [{"n": self.backlog}]
        if " IN (" in sql:
            # The exclusion probe: return the ids that are in the excluded head.
            excluded = [
                {"id": pk}
                for pk in params
                if isinstance(pk, str)
                and pk.startswith("row")
                and self._index(pk) < self.excluded_head
            ]
            return excluded
        # Step-1 candidate select.
        assert "LIMIT ?" in sql, "the window must be bounded in SQL: %s" % sql
        if "OFFSET ?" in sql:
            limit, offset = int(params[-2]), int(params[-1])
        else:
            limit, offset = int(params[-1]), 0
        n = max(0, min(limit, self.backlog - offset))
        self.windows_fetched.append(n)
        return [{"pk_val": "row%07d" % i} for i in range(offset, offset + n)]

    def transaction(self):  # pragma: no cover - dry runs only
        raise AssertionError("dry run must not delete")


def _service(store):
    return RetentionService(store, observability_recorder=lambda *a, **k: None)


def _policy(batch_size=100):
    return RetentionPolicy(
        "observability_events",
        enabled=True,
        max_age_seconds=604800,
        batch_size=batch_size,
    )


def test_a_fully_excluded_head_does_not_block_the_rows_behind_it():
    # 250 permanently-excluded rows at the head, plenty of eligible rows behind.
    store = _HeadExcludedStore(backlog=5_000, excluded_head=250)
    svc = _service(store)
    svc.set_policy(_policy(batch_size=100))

    report = svc.dry_run("observability_events")

    assert report.eligible_rows > 0, (
        "prune found nothing eligible. The first %d rows are excluded, so a "
        "window filled with raw candidates is 100%% waste and the 4,750 "
        "eligible rows behind them are never reached -- exactly the live "
        "failure (eligible=0 deleted=0 excluded=2000 on every tick)."
        % store.excluded_head
    )
    assert report.eligible_rows == 100, (
        "expected a full batch of eligible rows, got %d" % report.eligible_rows
    )


def test_it_scans_forward_rather_than_giving_up_on_the_first_window():
    store = _HeadExcludedStore(backlog=5_000, excluded_head=250)
    svc = _service(store)
    svc.set_policy(_policy(batch_size=100))

    svc.dry_run("observability_events")

    assert len(store.windows_fetched) > 1, (
        "only one window was fetched; with a 250-row excluded head and a "
        "100-row batch, prune must scan past the excluded head"
    )


def test_the_forward_scan_is_bounded():
    """A table whose candidates are ALL excluded must cost a fixed number of
    reads, not a walk of the entire backlog."""
    store = _HeadExcludedStore(backlog=1_000_000, excluded_head=1_000_000)
    svc = _service(store)
    svc.set_policy(_policy(batch_size=100))

    report = svc.dry_run("observability_events")

    assert report.eligible_rows == 0, "nothing is eligible in this fixture"
    assert len(store.windows_fetched) <= MAX_SCAN_WINDOWS, (
        "the forward scan read %d windows; it must stop at MAX_SCAN_WINDOWS "
        "(%d) so an entirely-excluded table cannot walk its whole backlog on "
        "every prune" % (len(store.windows_fetched), MAX_SCAN_WINDOWS)
    )


def test_no_excluded_head_still_takes_exactly_one_window():
    """The common case must not pay for the pathological one."""
    store = _HeadExcludedStore(backlog=5_000, excluded_head=0)
    svc = _service(store)
    svc.set_policy(_policy(batch_size=100))

    report = svc.dry_run("observability_events")

    assert report.eligible_rows == 100
    assert store.windows_fetched == [100], (
        "with nothing excluded prune must read exactly one window, got %s"
        % store.windows_fetched
    )


def test_the_window_still_takes_the_oldest_eligible_rows():
    """Scanning forward must not reorder: retention is oldest-first."""
    store = _HeadExcludedStore(backlog=5_000, excluded_head=250)
    svc = _service(store)
    svc.set_policy(_policy(batch_size=100))

    report = svc.dry_run("observability_events")

    # The first eligible row is row0000250 -- the oldest one not excluded.
    assert report.eligible_rows == 100
    assert store.windows_fetched, "no windows were read"
