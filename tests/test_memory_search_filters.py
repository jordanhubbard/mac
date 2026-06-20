"""Tests for operator-grade memory search filters (record_type, record_type_prefix,
created_by, since, until, limit, order) added in the nap/dream usability fix.

Covers:
  - Local mode: ControlPlane.in_memory() -> MemoryService.search_memory()
  - Hub mode: dispatch.HubPlane.search_memory() passes all kwargs to GET /memory
  - CLI argument wiring: argparse flags map to the right kwargs
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import pytest

from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _add(
    cp: ControlPlane,
    *,
    subject_type: str = "project",
    subject_id: str = "mac",
    record_type: str = "nap_summary",
    content: str = "test",
    created_by: str = "nap-consolidator",
    task_id: Optional[str] = None,
) -> Any:
    return cp.add_memory(
        task_id, subject_type, subject_id, record_type, content, None, created_by
    )


def _age(cp: ControlPlane, mem_id: str, days_ago: float) -> None:
    """Backdate a record's created_at so we can test since/until filters."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="microseconds"
    )
    cp.store.execute(
        "UPDATE memory_records SET created_at = ? WHERE id = ?", (ts, mem_id)
    )


# ---------------------------------------------------------------------------
# record_type exact filter
# ---------------------------------------------------------------------------

class TestRecordTypeExact:
    def test_matches_exact(self):
        cp = _cp()
        _add(cp, record_type="nap_summary")
        _add(cp, record_type="dream:reflection")

        result = cp.search_memory(record_type="nap_summary")
        assert len(result) == 1
        assert result[0].record_type == "nap_summary"

    def test_no_match_returns_empty(self):
        cp = _cp()
        _add(cp, record_type="nap_summary")
        result = cp.search_memory(record_type="dream:reflection")
        assert result == []

    def test_exact_wins_over_prefix(self):
        """When both record_type and record_type_prefix are given, exact wins."""
        cp = _cp()
        _add(cp, record_type="nap_summary")
        _add(cp, record_type="nap_detail")
        result = cp.search_memory(record_type="nap_summary", record_type_prefix="nap")
        assert len(result) == 1
        assert result[0].record_type == "nap_summary"


# ---------------------------------------------------------------------------
# record_type prefix filter
# ---------------------------------------------------------------------------

class TestRecordTypePrefix:
    def test_prefix_matches_all_variants(self):
        cp = _cp()
        _add(cp, record_type="dream:reflection")
        _add(cp, record_type="dream:lesson")
        _add(cp, record_type="dream:hypothesis")
        _add(cp, record_type="nap_summary")  # should NOT match

        result = cp.search_memory(record_type_prefix="dream:")
        types = {r.record_type for r in result}
        assert types == {"dream:reflection", "dream:lesson", "dream:hypothesis"}

    def test_prefix_without_colon(self):
        cp = _cp()
        _add(cp, record_type="nap_summary")
        _add(cp, record_type="nap_detail")
        _add(cp, record_type="dream:reflection")

        result = cp.search_memory(record_type_prefix="nap")
        types = {r.record_type for r in result}
        assert types == {"nap_summary", "nap_detail"}

    def test_trailing_percent_in_prefix_is_stripped(self):
        """A caller passing 'dream:%' should get the same result as 'dream:'."""
        cp = _cp()
        _add(cp, record_type="dream:reflection")
        _add(cp, record_type="nap_summary")

        result = cp.search_memory(record_type_prefix="dream:%")
        assert len(result) == 1
        assert result[0].record_type == "dream:reflection"

    def test_prefix_no_match(self):
        cp = _cp()
        _add(cp, record_type="nap_summary")
        result = cp.search_memory(record_type_prefix="dream:")
        assert result == []


# ---------------------------------------------------------------------------
# created_by filter
# ---------------------------------------------------------------------------

class TestCreatedByFilter:
    def test_filter_by_creator(self):
        cp = _cp()
        _add(cp, created_by="nap-consolidator", record_type="nap_summary")
        _add(cp, created_by="agent_rocky", record_type="dream:reflection")
        _add(cp, created_by="nap-consolidator", record_type="nap_summary")

        result = cp.search_memory(created_by="nap-consolidator")
        assert len(result) == 2
        assert all(r.created_by == "nap-consolidator" for r in result)

    def test_created_by_per_agent_count(self):
        """Simulate the acceptance-criteria query: count nap_summary per agent."""
        cp = _cp()
        for agent in ("agent_alpha", "agent_beta", "agent_alpha"):
            _add(cp, created_by=agent, record_type="nap_summary")

        alpha = cp.search_memory(created_by="agent_alpha", record_type="nap_summary")
        beta = cp.search_memory(created_by="agent_beta", record_type="nap_summary")
        assert len(alpha) == 2
        assert len(beta) == 1

    def test_filter_no_match(self):
        cp = _cp()
        _add(cp, created_by="nap-consolidator")
        result = cp.search_memory(created_by="agent_unknown")
        assert result == []


# ---------------------------------------------------------------------------
# since / until filters
# ---------------------------------------------------------------------------

class TestSinceUntilFilters:
    def test_since_excludes_old_records(self):
        cp = _cp()
        old = _add(cp, record_type="nap_summary", content="old")
        new = _add(cp, record_type="nap_summary", content="new")

        # Backdate 'old' to 10 days ago
        _age(cp, old.id, days_ago=10)

        cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(
            timespec="microseconds"
        )
        result = cp.search_memory(since=cutoff)
        ids = {r.id for r in result}
        assert new.id in ids
        assert old.id not in ids

    def test_until_excludes_recent_records(self):
        cp = _cp()
        old = _add(cp, record_type="nap_summary", content="old")
        new = _add(cp, record_type="nap_summary", content="new")

        _age(cp, old.id, days_ago=10)

        cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(
            timespec="microseconds"
        )
        result = cp.search_memory(until=cutoff)
        ids = {r.id for r in result}
        assert old.id in ids
        assert new.id not in ids

    def test_since_until_window(self):
        cp = _cp()
        r7 = _add(cp, content="7-days-ago")
        r3 = _add(cp, content="3-days-ago")
        r1 = _add(cp, content="1-day-ago")

        _age(cp, r7.id, days_ago=7)
        _age(cp, r3.id, days_ago=3)
        _age(cp, r1.id, days_ago=1)

        since = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="microseconds")
        until = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="microseconds")
        result = cp.search_memory(since=since, until=until)
        ids = {r.id for r in result}
        assert r3.id in ids
        assert r7.id not in ids
        assert r1.id not in ids


# ---------------------------------------------------------------------------
# limit filter
# ---------------------------------------------------------------------------

class TestLimitFilter:
    def test_limit_caps_results(self):
        cp = _cp()
        for i in range(10):
            _add(cp, content=f"record-{i}")
        result = cp.search_memory(limit=3)
        assert len(result) == 3

    def test_limit_larger_than_total(self):
        cp = _cp()
        for i in range(5):
            _add(cp, content=f"record-{i}")
        result = cp.search_memory(limit=100)
        assert len(result) == 5

    def test_limit_combined_with_record_type(self):
        cp = _cp()
        for i in range(6):
            _add(cp, record_type="nap_summary", content=f"nap-{i}")
        for i in range(4):
            _add(cp, record_type="dream:reflection", content=f"dream-{i}")
        result = cp.search_memory(record_type="nap_summary", limit=2)
        assert len(result) == 2
        assert all(r.record_type == "nap_summary" for r in result)


# ---------------------------------------------------------------------------
# order filter
# ---------------------------------------------------------------------------

class TestOrderFilter:
    def test_asc_default(self):
        cp = _cp()
        r_old = _add(cp, content="first")
        r_new = _add(cp, content="second")
        _age(cp, r_old.id, days_ago=5)

        result = cp.search_memory(order="asc")
        assert result[0].id == r_old.id
        assert result[-1].id == r_new.id

    def test_desc_newest_first(self):
        cp = _cp()
        r_old = _add(cp, content="first")
        r_new = _add(cp, content="second")
        _age(cp, r_old.id, days_ago=5)

        result = cp.search_memory(order="desc")
        assert result[0].id == r_new.id
        assert result[-1].id == r_old.id

    def test_desc_with_limit_returns_newest(self):
        """desc + limit=1 should return the most recent record."""
        cp = _cp()
        r_old = _add(cp, content="old")
        r_new = _add(cp, content="new")
        _age(cp, r_old.id, days_ago=5)

        result = cp.search_memory(order="desc", limit=1)
        assert len(result) == 1
        assert result[0].id == r_new.id


# ---------------------------------------------------------------------------
# Combined filter scenarios (acceptance criteria)
# ---------------------------------------------------------------------------

class TestCombinedFilters:
    def test_nap_summary_count_by_agent(self):
        """
        Acceptance: mac memory search can directly answer 
        'recent nap_summary and dream:* counts per agent'.
        """
        cp = _cp()
        agents = ["agent_alpha", "agent_beta", "agent_gamma"]
        for agent in agents:
            for rt in ["nap_summary", "dream:reflection", "dream:lesson", "other"]:
                _add(cp, created_by=agent, record_type=rt)

        # Count nap_summary per agent
        nap_counts = {
            agent: len(cp.search_memory(created_by=agent, record_type="nap_summary"))
            for agent in agents
        }
        assert all(v == 1 for v in nap_counts.values())

        # Count dream:* per agent
        dream_counts = {
            agent: len(cp.search_memory(created_by=agent, record_type_prefix="dream:"))
            for agent in agents
        }
        assert all(v == 2 for v in dream_counts.values())

    def test_recent_nap_records_across_fleet(self):
        """since + record_type gives a time-bounded view of nap records."""
        cp = _cp()
        recent = _add(cp, record_type="nap_summary", created_by="nap-consolidator", content="recent")
        old = _add(cp, record_type="nap_summary", created_by="nap-consolidator", content="old")
        _age(cp, old.id, days_ago=30)

        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="microseconds")
        result = cp.search_memory(
            record_type="nap_summary",
            created_by="nap-consolidator",
            since=since,
        )
        ids = {r.id for r in result}
        assert recent.id in ids
        assert old.id not in ids

    def test_hub_mode_dispatch_passes_kwargs(self):
        """
        Verify that the hub-mode dispatch.search_memory passes all new kwargs
        as query-string parameters. We inject a fake _get to capture the call.
        """
        from mac.dispatch import RemoteDispatch

        captured: Dict[str, Any] = {}

        class FakeRemoteDispatch(RemoteDispatch):
            def _get(self, path: str, **kwargs: Any):  # type: ignore[override]
                captured.update(kwargs)
                return []

        # Build a minimal instance without a real server
        plane = object.__new__(FakeRemoteDispatch)

        plane.search_memory(
            record_type="nap_summary",
            created_by="nap-consolidator",
            since="2026-01-01T00:00:00",
            until="2026-12-31T23:59:59",
            limit=5,
            order="desc",
        )
        assert captured["record_type"] == "nap_summary"
        assert captured["created_by"] == "nap-consolidator"
        assert captured["since"] == "2026-01-01T00:00:00"
        assert captured["until"] == "2026-12-31T23:59:59"
        assert captured["limit"] == 5
        assert captured["order"] == "desc"


# ---------------------------------------------------------------------------
# CLI argument wiring
# ---------------------------------------------------------------------------

class TestCLIArgumentWiring:
    """Verify the argparse flags are wired to the right attribute names."""

    def _parse(self, *argv: str) -> Any:
        from mac.cli import build_parser
        parser = build_parser()
        return parser.parse_args(["memory", "search"] + list(argv))

    def test_record_type_flag(self):
        args = self._parse("--record-type", "nap_summary")
        assert args.record_type == "nap_summary"

    def test_record_type_prefix_flag(self):
        args = self._parse("--record-type-prefix", "dream:")
        assert args.record_type_prefix == "dream:"

    def test_created_by_flag(self):
        args = self._parse("--created-by", "nap-consolidator")
        assert args.created_by == "nap-consolidator"

    def test_since_flag(self):
        args = self._parse("--since", "2026-01-01T00:00:00")
        assert args.since == "2026-01-01T00:00:00"

    def test_until_flag(self):
        args = self._parse("--until", "2026-12-31T23:59:59")
        assert args.until == "2026-12-31T23:59:59"

    def test_limit_flag(self):
        args = self._parse("--limit", "10")
        assert args.limit == 10

    def test_order_asc(self):
        args = self._parse("--order", "asc")
        assert args.order == "asc"

    def test_order_desc(self):
        args = self._parse("--order", "desc")
        assert args.order == "desc"

    def test_order_default_is_asc(self):
        args = self._parse()
        assert args.order == "asc"

    def test_combined_flags(self):
        args = self._parse(
            "--record-type-prefix", "dream:",
            "--created-by", "agent_rocky",
            "--since", "2026-06-01T00:00:00",
            "--limit", "20",
            "--order", "desc",
        )
        assert args.record_type_prefix == "dream:"
        assert args.created_by == "agent_rocky"
        assert args.since == "2026-06-01T00:00:00"
        assert args.limit == 20
        assert args.order == "desc"
