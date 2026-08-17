"""Tests for RetentionService and action-event attribute cap.

Covers the preserve-by-default retention controls described in the
2026-06-27 architecture decision:

- With default configuration, automated ticks delete nothing.
- Metrics and dry-run reports accurately predict a fixture database's
  reclaimable rows/bytes.
- Held, active, or referenced records are never selected.
- An explicitly configured test policy prunes only eligible records in
  bounded batches and emits a complete audit record.
- The action-event attribute cap truncates oversized payloads without
  breaking normal-sized events.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from mac.action_event_service import (
    ACTION_EVENT_MAX_ATTRIBUTES_BYTES,
    ActionEventService,
    _cap_attributes,
)
from mac.models import new_id, utcnow
from mac.retention_service import (
    DEFAULT_BATCH_SIZE,
    RECORD_CLASS_CONFIG,
    RETENTION_POLICY_SCHEMA,
    RetentionPolicy,
    RetentionService,
)
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    """Ephemeral SQLite store with fully initialised schema."""
    db_path = str(tmp_path / "test.db")
    s = ephemeral_store()
    return s


@pytest.fixture()
def cp(store):
    """ControlPlane backed by the ephemeral store."""
    return ControlPlane(store, secret_key="test-secret-key-for-retention-tests-32+")


@pytest.fixture()
def retention(store):
    """RetentionService backed by the ephemeral store, no obs recorder."""
    return RetentionService(store)


@pytest.fixture()
def retention_with_obs(store):
    """RetentionService with a MagicMock obs recorder to check audit events."""
    recorder = MagicMock()
    svc = RetentionService(store, observability_recorder=recorder)
    svc._mock_recorder = recorder
    return svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_obs(store, name: str = "test.event", level: str = "info") -> str:
    """Insert a minimal observability_events row; returns the row id."""
    obs_id = new_id("obs")
    store.execute(
        "INSERT INTO observability_events"
        " (id, kind, layer, source, level, name, value, unit, detail, created_at)"
        " VALUES (?, 'log', 'control_plane', 'test', ?, ?, NULL, '', '{}', ?)",
        (obs_id, level, name, utcnow()),
    )
    return obs_id


def _insert_action_event(store, task_id: Optional[str] = None) -> str:
    """Insert a minimal action_events row; returns event_id."""
    svc = ActionEventService(store)
    evt = svc.record_action_event(
        actor="test",
        action_type="test",
        action_name="test.action",
        task_id=task_id,
        outcome="success",
    )
    return evt.event_id


def _insert_task(cp, state: str = "completed") -> str:
    """Create a task and set it to a given state; returns task id."""
    task = cp.create_task("test task")
    if state == "open":
        return task.id
    machine = cp.register_machine("m-host", resources={"cpu": 2, "memory_gb": 4})
    agent = cp.register_agent(machine.id, "agent-1", capabilities=[], resources={})
    if state == "claimed":
        cp.claim_task(task.id, agent.id)
        return task.id
    # transition to completed
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)
    if state == "running":
        return task.id
    cp.close_task(task.id, actor=agent.id, reason="done")
    return task.id


# ---------------------------------------------------------------------------
# RetentionPolicy unit tests
# ---------------------------------------------------------------------------


class TestRetentionPolicy:
    def test_defaults_to_disabled(self):
        p = RetentionPolicy("observability_events")
        assert p.enabled is False
        assert p.max_age_seconds is None
        assert p.max_rows is None
        assert p.batch_size == DEFAULT_BATCH_SIZE
        assert p.version == 1

    def test_to_dict_schema(self):
        p = RetentionPolicy("action_events", enabled=False)
        d = p.to_dict()
        assert d["schema"] == RETENTION_POLICY_SCHEMA
        assert d["record_class"] == "action_events"
        assert d["enabled"] is False

    def test_from_dict_round_trip(self):
        orig = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,
            max_rows=10000,
            batch_size=100,
            version=3,
            provenance={"set_by": "operator"},
        )
        d = orig.to_dict()
        restored = RetentionPolicy.from_dict(d)
        assert restored.record_class == orig.record_class
        assert restored.enabled == orig.enabled
        assert restored.max_age_seconds == orig.max_age_seconds
        assert restored.max_rows == orig.max_rows
        assert restored.batch_size == orig.batch_size
        assert restored.version == orig.version
        assert restored.provenance == orig.provenance

    def test_unknown_record_class_raises(self):
        from mac.models import ValidationError
        with pytest.raises(ValidationError, match="unknown retention record_class"):
            RetentionPolicy("no_such_table")

    def test_negative_max_age_raises(self):
        from mac.models import ValidationError
        with pytest.raises(ValidationError, match="max_age_seconds"):
            RetentionPolicy("observability_events", enabled=True, max_age_seconds=-1)

    def test_negative_max_rows_raises(self):
        from mac.models import ValidationError
        with pytest.raises(ValidationError, match="max_rows"):
            RetentionPolicy("observability_events", enabled=True, max_rows=-5)

    def test_zero_batch_size_raises(self):
        from mac.models import ValidationError
        with pytest.raises(ValidationError, match="batch_size"):
            RetentionPolicy("observability_events", enabled=True, batch_size=0)


# ---------------------------------------------------------------------------
# RetentionService.stats
# ---------------------------------------------------------------------------


class TestRetentionStats:
    def test_stats_returns_all_classes_when_no_filter(self, retention):
        stats = retention.stats()
        classes = {s["record_class"] for s in stats}
        assert classes == set(RECORD_CLASS_CONFIG)

    def test_stats_filters_by_class(self, retention):
        stats = retention.stats("observability_events")
        assert len(stats) == 1
        assert stats[0]["record_class"] == "observability_events"

    def test_stats_zero_rows_initially(self, retention):
        stats = retention.stats("observability_events")
        assert stats[0]["row_count"] == 0
        assert stats[0]["estimated_bytes"] == 0

    def test_stats_counts_rows_after_insert(self, store, retention):
        _insert_obs(store, name="stat.test.event")
        _insert_obs(store, name="stat.test.event.2")
        stats = retention.stats("observability_events")
        assert stats[0]["row_count"] == 2

    def test_stats_unknown_class_raises(self, retention):
        from mac.models import ValidationError
        with pytest.raises(ValidationError, match="unknown retention record_class"):
            retention.stats("no_such_table")

    def test_stats_includes_oldest_newest(self, store, retention):
        _insert_obs(store)
        _insert_obs(store)
        stats = retention.stats("observability_events")
        assert stats[0]["oldest_ts"] is not None
        assert stats[0]["newest_ts"] is not None

    def test_stats_via_control_plane(self, cp):
        stats = cp.retention_stats()
        assert isinstance(stats, list)
        assert any(s["record_class"] == "observability_events" for s in stats)


# ---------------------------------------------------------------------------
# RetentionService.list_policies / set_policy / get_policy
# ---------------------------------------------------------------------------


class TestRetentionPolicies:
    def test_all_classes_have_default_preserve_policy(self, retention):
        policies = retention.list_policies()
        for p in policies:
            assert p["enabled"] is False

    def test_set_policy_replaces_default(self, retention):
        pol = RetentionPolicy("observability_events", enabled=True, max_rows=100)
        retention.set_policy(pol)
        got = retention.get_policy("observability_events")
        assert got.enabled is True
        assert got.max_rows == 100

    def test_get_policy_unknown_class_returns_disabled(self, retention):
        # Any known class not explicitly set returns the disabled default
        pol = retention.get_policy("action_events")
        assert pol.enabled is False

    def test_set_policy_via_control_plane_list(self, cp):
        # ControlPlane enables the two high-volume telemetry classes by default
        # (bound the mac.db growth that wedged the hub); everything else stays
        # preserve-by-default.
        enabled = {p["record_class"] for p in cp.retention_list_policies() if p["enabled"]}
        assert enabled == {"action_events", "observability_events"}


# ---------------------------------------------------------------------------
# Dry-run prune reports
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_disabled_policy_returns_zero_eligible(self, store, retention):
        _insert_obs(store)
        report = retention.dry_run("observability_events")
        assert report.dry_run is True
        assert report.eligible_rows == 0
        assert report.deleted_rows == 0
        # row is still in the database
        rows = store.query_all("SELECT id FROM observability_events")
        assert len(rows) == 1

    def test_dry_run_enabled_policy_counts_eligible_rows(self, store, retention):
        # Insert rows with old timestamps by directly overwriting created_at
        for i in range(5):
            obs_id = new_id("obs")
            store.execute(
                "INSERT INTO observability_events"
                " (id, kind, layer, source, level, name, value, unit, detail, created_at)"
                " VALUES (?, 'log', 'control_plane', 'test', 'info', 'old.event',"
                " NULL, '', '{}', '2020-01-01T00:00:00+00:00')",
                (obs_id,),
            )
        pol = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,  # cutoff well after 2020
        )
        report = retention.dry_run("observability_events", override_policy=pol)
        assert report.dry_run is True
        assert report.eligible_rows == 5
        assert report.deleted_rows == 0  # dry-run must not delete
        # rows are still there
        rows = store.query_all("SELECT id FROM observability_events")
        assert len(rows) == 5

    def test_dry_run_via_control_plane(self, cp):
        report = cp.retention_dry_run("observability_events")
        assert report["dry_run"] is True
        assert report["deleted_rows"] == 0

    def test_dry_run_report_schema(self, store, retention):
        report = retention.dry_run("observability_events")
        d = report.to_dict()
        assert d["schema"] == "mac.prune_report.v1"
        assert "policy" in d
        assert d["policy"]["schema"] == RETENTION_POLICY_SCHEMA

    def test_dry_run_reports_exclusion_reason_policy_disabled(self, store, retention):
        _insert_obs(store)
        report = retention.dry_run("observability_events")
        assert any("policy_disabled" in r for r in report.exclusion_reasons)

    def test_dry_run_no_mutation_on_any_class(self, store, retention):
        """With default policies, dry_run on all classes touches nothing."""
        _insert_obs(store)
        _insert_action_event(store)
        for rc in RECORD_CLASS_CONFIG:
            report = retention.dry_run(rc)
            assert report.deleted_rows == 0


# ---------------------------------------------------------------------------
# Live prune: preserve-by-default (no policy enabled)
# ---------------------------------------------------------------------------


class TestLivePrunePreserveDefault:
    def test_prune_disabled_policy_deletes_nothing(self, store, retention):
        _insert_obs(store)
        report = retention.prune("observability_events")
        assert report.dry_run is False
        assert report.deleted_rows == 0
        rows = store.query_all("SELECT id FROM observability_events")
        assert len(rows) == 1

    def test_prune_all_disabled_deletes_nothing(self, store, retention):
        _insert_obs(store)
        reports = retention.prune_all()
        assert all(r["deleted_rows"] == 0 for r in reports)
        assert len(store.query_all("SELECT id FROM observability_events")) == 1

    def test_prune_enabled_no_limit_still_deletes_nothing(self, store, retention):
        _insert_obs(store)
        # Enabled but no limit configured
        pol = RetentionPolicy("observability_events", enabled=True)
        report = retention.prune("observability_events", override_policy=pol)
        assert report.deleted_rows == 0
        assert any("no_limit" in r for r in report.exclusion_reasons)


# ---------------------------------------------------------------------------
# Live prune: age-based deletion
# ---------------------------------------------------------------------------


class TestLivePruneAgeBasedDeletion:
    def _insert_old_obs(self, store, count: int = 3):
        ids = []
        for _ in range(count):
            obs_id = new_id("obs")
            store.execute(
                "INSERT INTO observability_events"
                " (id, kind, layer, source, level, name, value, unit, detail, created_at)"
                " VALUES (?, 'log', 'control_plane', 'test', 'info', 'old.evt',"
                " NULL, '', '{}', '2020-01-01T00:00:00+00:00')",
                (obs_id,),
            )
            ids.append(obs_id)
        return ids

    def test_prune_deletes_old_rows(self, store, retention):
        old_ids = self._insert_old_obs(store, 3)
        new_id_ = _insert_obs(store, name="new.event")
        pol = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,  # 1 hour — old rows are years old
        )
        report = retention.prune("observability_events", override_policy=pol)
        assert report.deleted_rows == 3
        remaining = {r["id"] for r in store.query_all("SELECT id FROM observability_events")}
        assert new_id_ in remaining
        for oid in old_ids:
            assert oid not in remaining

    def test_prune_respects_batch_size(self, store, retention):
        self._insert_old_obs(store, 10)
        pol = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,
            batch_size=4,
        )
        report = retention.prune("observability_events", override_policy=pol)
        assert report.deleted_rows == 4
        assert report.batch_capped is True
        remaining = store.query_all("SELECT id FROM observability_events")
        assert len(remaining) == 6  # 10 - 4

    def test_prune_dry_run_bytes_match_live_bytes(self, store, retention):
        self._insert_old_obs(store, 5)
        pol = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,
        )
        dry = retention.dry_run("observability_events", override_policy=pol)
        live = retention.prune("observability_events", override_policy=pol)
        assert dry.eligible_rows == 5
        assert dry.eligible_bytes == live.deleted_bytes
        assert live.deleted_rows == 5


# ---------------------------------------------------------------------------
# Live prune: max_rows count-based deletion
# ---------------------------------------------------------------------------


class TestLivePruneMaxRows:
    def test_prune_max_rows_keeps_newest(self, store, retention):
        # Insert 10 rows in order
        ids = []
        for i in range(10):
            obs_id = new_id("obs")
            # Use deterministic timestamps spaced 1 second apart
            ts = "2025-01-%02dT12:00:00+00:00" % (i + 1)
            store.execute(
                "INSERT INTO observability_events"
                " (id, kind, layer, source, level, name, value, unit, detail, created_at)"
                " VALUES (?, 'log', 'control_plane', 'test', 'info', 'cnt.evt',"
                " NULL, '', '{}', ?)",
                (obs_id, ts),
            )
            ids.append(obs_id)
        pol = RetentionPolicy("observability_events", enabled=True, max_rows=3)
        report = retention.prune("observability_events", override_policy=pol)
        assert report.deleted_rows == 7
        remaining = [r["id"] for r in store.query_all(
            "SELECT id FROM observability_events ORDER BY created_at DESC"
        )]
        # The 3 newest should survive
        assert ids[7] in remaining
        assert ids[8] in remaining
        assert ids[9] in remaining
        assert ids[0] not in remaining


# ---------------------------------------------------------------------------
# Hard exclusions
# ---------------------------------------------------------------------------


class TestHardExclusions:
    def test_active_task_obs_excluded(self, store, cp):
        """Observability events tied to an active (non-terminal) task must not
        be pruned even when the policy would cover them."""
        # Create a task in open state
        task = cp.create_task("active task for exclusion test")

        # Insert an old obs event that references this task
        obs_id = new_id("obs")
        store.execute(
            "INSERT INTO observability_events"
            " (id, kind, layer, source, level, name, subject_type, subject_id,"
            " value, unit, detail, created_at)"
            " VALUES (?, 'log', 'control_plane', 'test', 'info', 'task.evt',"
            " 'task', ?, NULL, '', '{}', '2020-01-01T00:00:00+00:00')",
            (obs_id, task.id),
        )

        pol = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,
        )
        report = cp.retention.prune("observability_events", override_policy=pol)
        # The obs row must not be deleted because the task is active
        assert report.excluded_rows == 1
        rows = store.query_all("SELECT id FROM observability_events WHERE id = ?", (obs_id,))
        assert len(rows) == 1

    def test_terminal_task_obs_not_excluded(self, store, cp):
        """Observability events tied to a completed task ARE eligible."""
        from mac.models import TaskState
        task = cp.create_task("terminal task for exclusion test")
        # Transition to failed (a terminal state) without requiring agent lifecycle
        cp._transition_task_internal(
            task.id,
            TaskState.FAILED.value,
            "test",
            {"reason": "terminal-test"},
        )

        obs_id = new_id("obs")
        store.execute(
            "INSERT INTO observability_events"
            " (id, kind, layer, source, level, name, subject_type, subject_id,"
            " value, unit, detail, created_at)"
            " VALUES (?, 'log', 'control_plane', 'test', 'info', 'task.evt',"
            " 'task', ?, NULL, '', '{}', '2020-01-01T00:00:00+00:00')",
            (obs_id, task.id),
        )

        pol = RetentionPolicy(
            "observability_events",
            enabled=True,
            max_age_seconds=3600,
        )
        report = cp.retention.prune("observability_events", override_policy=pol)
        assert report.deleted_rows == 1
        assert report.excluded_rows == 0

    def test_active_task_action_event_excluded(self, store, cp):
        """Action events linked to an active task must not be pruned."""
        task = cp.create_task("active task ae exclusion")
        svc = ActionEventService(store)
        evt = svc.record_action_event(
            actor="test",
            action_type="test",
            action_name="some.action",
            task_id=task.id,
            outcome="success",
            timestamp="2020-01-01T00:00:00+00:00",
        )

        pol = RetentionPolicy(
            "action_events",
            enabled=True,
            max_age_seconds=3600,
        )
        report = cp.retention.prune("action_events", override_policy=pol)
        assert report.excluded_rows == 1
        rows = store.query_all("SELECT event_id FROM action_events WHERE event_id = ?", (evt.event_id,))
        assert len(rows) == 1

    def test_no_task_action_event_not_excluded(self, store, retention):
        """Action events with no task_id are not excluded by the task check."""
        svc = ActionEventService(store)
        svc.record_action_event(
            actor="test",
            action_type="test",
            action_name="some.action",
            task_id=None,
            outcome="success",
            timestamp="2020-01-01T00:00:00+00:00",
        )
        pol = RetentionPolicy("action_events", enabled=True, max_age_seconds=3600)
        report = retention.prune("action_events", override_policy=pol)
        assert report.deleted_rows == 1
        assert report.excluded_rows == 0


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


class TestAuditEvents:
    def test_live_prune_emits_audit_observation(self, store, retention_with_obs):
        svc = retention_with_obs
        _insert_obs(store)
        pol = RetentionPolicy("observability_events", enabled=True, max_rows=0)
        svc.set_policy(pol)
        svc.prune("observability_events")
        svc._mock_recorder.assert_called_once()
        call_args = svc._mock_recorder.call_args
        # First positional arg is the event name
        event_name = call_args[0][0]
        assert event_name == "retention.prune"
        detail = call_args[1]["detail"]
        assert detail["schema"] == "mac.retention_audit.v1"
        assert detail["record_class"] == "observability_events"
        assert "deleted_rows" in detail

    def test_dry_run_does_not_emit_audit(self, store, retention_with_obs):
        svc = retention_with_obs
        svc.dry_run("observability_events")
        svc._mock_recorder.assert_not_called()

    def test_audit_via_control_plane_observability(self, store, cp):
        """A live prune emits into the observability stream."""
        cp.record_log("retention.fixture")
        pol = RetentionPolicy("observability_events", enabled=True, max_rows=0)
        cp.retention.set_policy(pol)
        cp.retention_prune("observability_events")
        # Audit record name is "retention.prune"
        events = cp.list_observability(name="retention.prune", limit=10)
        assert len(events) >= 1

    def test_noop_live_prune_does_not_emit_audit(self, retention_with_obs):
        svc = retention_with_obs
        pol = RetentionPolicy("observability_events", enabled=True, max_rows=10)
        svc.set_policy(pol)
        svc.prune("observability_events")
        svc._mock_recorder.assert_not_called()


# ---------------------------------------------------------------------------
# Action-event attribute cap
# ---------------------------------------------------------------------------


class TestActionEventAttributeCap:
    def test_small_attributes_pass_through(self):
        attrs = {"key": "value", "number": 42}
        result = _cap_attributes(attrs)
        assert result == attrs

    def test_oversized_attributes_are_truncated(self):
        big = {"blob": "x" * (ACTION_EVENT_MAX_ATTRIBUTES_BYTES + 100)}
        result = _cap_attributes(big)
        assert result.get("_truncated") is True
        assert result["_max_bytes"] == ACTION_EVENT_MAX_ATTRIBUTES_BYTES
        assert "_original_bytes" in result
        assert "blob" in result["_keys"]

    def test_cap_preserves_top_level_keys(self):
        # 200 keys, each value is 400 bytes → ~80KB total > 64KB cap
        big = {str(i): "x" * 400 for i in range(200)}
        result = _cap_attributes(big)
        assert result["_truncated"] is True
        assert len(result["_keys"]) <= 50

    def test_action_event_service_caps_on_write(self, store):
        """ActionEventService must cap oversized attributes at record time."""
        svc = ActionEventService(store)
        big_attrs = {"payload": "x" * (ACTION_EVENT_MAX_ATTRIBUTES_BYTES + 500)}
        evt = svc.record_action_event(
            actor="test",
            action_type="test",
            action_name="big.attrs",
            attributes=big_attrs,
            outcome="success",
        )
        assert evt.attributes.get("_truncated") is True

    def test_normal_action_event_attributes_not_truncated(self, store):
        svc = ActionEventService(store)
        evt = svc.record_action_event(
            actor="test",
            action_type="test",
            action_name="normal.attrs",
            attributes={"key": "value"},
            outcome="success",
        )
        assert evt.attributes.get("_truncated") is None
        assert evt.attributes["key"] == "value"

    def test_action_event_cap_constant_matches_obs_cap(self):
        """The action-event attribute cap must equal the observability detail
        cap so both tables stay bounded at the same threshold."""
        from mac.observability_service import ObservabilityService
        assert ACTION_EVENT_MAX_ATTRIBUTES_BYTES == ObservabilityService.MAX_DETAIL_BYTES


# ---------------------------------------------------------------------------
# ControlPlane façade
# ---------------------------------------------------------------------------


class TestControlPlaneFacade:
    def test_retention_stats_returns_all_classes(self, cp):
        stats = cp.retention_stats()
        assert len(stats) == len(RECORD_CLASS_CONFIG)

    def test_retention_dry_run_returns_dict(self, cp):
        report = cp.retention_dry_run("observability_events")
        assert isinstance(report, dict)
        assert report["schema"] == "mac.prune_report.v1"

    def test_retention_prune_returns_dict(self, cp):
        report = cp.retention_prune("observability_events")
        assert isinstance(report, dict)
        assert report["deleted_rows"] == 0  # default policy is disabled

    def test_retention_prune_all_returns_list(self, cp):
        reports = cp.retention_prune_all()
        assert isinstance(reports, list)
        assert len(reports) == len(RECORD_CLASS_CONFIG)

    def test_retention_list_policies_enables_only_telemetry_classes(self, cp):
        policies = cp.retention_list_policies()
        enabled = {p["record_class"] for p in policies if p["enabled"]}
        assert enabled == {"action_events", "observability_events"}
        # the task-ledger-adjacent classes must remain preserve-by-default
        assert all(
            p["enabled"] is False
            for p in policies
            if p["record_class"] not in {"action_events", "observability_events"}
        )


class _BacklogStore:
    """A fake store that behaves like a database rather than a Python list.

    ``COUNT(*)`` returns a scalar, ``LIMIT ?`` actually limits, and the
    candidate select is asserted to carry a LIMIT at all -- which is the whole
    point: the bound must live in SQL, so the backlog is never materialized in
    Python.  Rows come back oldest-first, ``row0000000`` being the oldest.
    """

    def __init__(self, backlog: int, total_rows: Optional[int] = None):
        self.backlog = backlog
        self.total_rows = backlog if total_rows is None else total_rows
        self.step1_fetched: List[int] = []  # rows RETURNED by the candidate select
        self.in_clause_binds: List[tuple] = []
        self.counts: List[str] = []

    def query_all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if "COUNT(*)" in sql:
            self.counts.append(sql)
            return [{"n": self.backlog if " WHERE " in sql else self.total_rows}]
        if " IN (" in sql:
            self.in_clause_binds.append(tuple(params))
            return []
        # The step-1 candidate select.
        assert "LIMIT ?" in sql, (
            "the candidate window must be bounded in SQL, not sliced in Python "
            "after the whole backlog has been read: %s" % sql
        )
        assert "ORDER BY" in sql and " ASC" in sql, (
            "the window must still take the OLDEST rows: %s" % sql
        )
        # `LIMIT ? OFFSET ?` -- the offset lets prune scan forward past a
        # fully-excluded head of the queue instead of stalling on it forever.
        if "OFFSET ?" in sql:
            limit = int(params[-2])
            offset = int(params[-1])
        else:
            limit = int(params[-1])
            offset = 0
        available = self.backlog if " WHERE " in sql else self.total_rows
        n = max(0, min(limit, available - offset))
        self.step1_fetched.append(n)
        return [
            {"pk_val": "row%07d" % i} for i in range(offset, offset + n)
        ]

    def transaction(self):  # pragma: no cover - these tests are all dry runs
        raise AssertionError("dry run must not delete")


class TestBindParameterCeiling:
    """Prune must survive a backlog larger than PostgreSQL's parameter limit.

    The exclusion queries bind ONE PARAMETER PER CANDIDATE, and PostgreSQL caps
    a statement at 65535, so passing an uncapped candidate list made prune fail
    outright once a table held ~65k prunable rows:

        retention.prune_tick_failed
          "sending query and params failed: number of parameters must be
           between 0 and 65535"

    Observed on the live hub 2026-08-17 firing on EVERY tick (~20s),
    continuously. It is self-perpetuating -- prune fails, rows accumulate, the
    candidate list grows, prune fails harder -- and it is the mechanism behind
    the 16GB / 10.4M-row action_events incident where retention was believed
    wired but the store kept growing.
    """

    def test_the_exclusion_query_never_binds_more_than_the_ceiling(self):
        """The candidate window, not the full backlog, reaches the SQL."""
        from mac.retention_service import (
            MAX_BIND_PARAMETERS,
            RetentionPolicy,
            RetentionService,
        )

        # A backlog far larger than the bind ceiling.
        store = _BacklogStore(MAX_BIND_PARAMETERS * 3)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy(
                "observability_events",
                enabled=True,
                max_age_seconds=1,
                # an operator asking for a window larger than the bind ceiling
                batch_size=MAX_BIND_PARAMETERS * 2,
            )
        )
        report = service.dry_run("observability_events", actor="test")

        seen = [len(p) for p in store.in_clause_binds]
        assert seen, "no exclusion query ran"
        assert max(seen) <= MAX_BIND_PARAMETERS, (
            "bound %d parameters; PostgreSQL rejects anything over 65535 and the "
            "ceiling exists to stay well clear" % max(seen)
        )
        assert report.batch_capped is True, (
            "a backlog beyond the window must still report as capped so the tick "
            "loop keeps draining"
        )

    def test_a_backlog_still_drains_oldest_first(self):
        """Clamping must not change WHICH rows go: candidates are ordered
        oldest-first and the window takes the head."""
        from mac.retention_service import (
            MAX_BIND_PARAMETERS,
            RetentionPolicy,
            RetentionService,
        )

        store = _BacklogStore(MAX_BIND_PARAMETERS + 500)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy(
                "observability_events",
                enabled=True,
                max_age_seconds=1,
                batch_size=MAX_BIND_PARAMETERS + 500,
            )
        )
        service.dry_run("observability_events", actor="test")

        captured = store.in_clause_binds
        assert captured, "no exclusion query ran"
        first_batch = captured[0]
        assert first_batch[0] == "row0000000", "the oldest row must be in the window"
        assert len(first_batch) <= MAX_BIND_PARAMETERS


class TestCandidateWindowIsBoundedInSql:
    """The window must be bounded by the DATABASE, not sliced in Python.

    #379 capped the window before the exclusion query bound it, which stopped
    the crash. But step 1 still ran

        SELECT <pk>, <ts>, LENGTH(...) FROM <table> WHERE <ts> < ?  ORDER BY <ts> ASC

    with no LIMIT -- and on the max_rows branch, with no WHERE either, i.e. the
    whole table. `StorePostgres.query_all` is `execute(...).fetchall()`, so the
    entire prunable backlog was materialized into a Python list on every call.
    `retention_prune_tick` calls prune() up to MAC_RETENTION_MAX_BATCHES_PER_TICK
    (25) times per record class, inline in the dispatcher tick and before
    `_dispatch_batch_impl` -- so a large-backlog recovery meant 25 full scans of
    the backlog per class per tick to delete 2000 rows.

    Deletion did still make monotonic progress (each batch commits in its own
    transaction), so this is not "the table never drains"; the harm is the
    repeated full-backlog materialization stalling dispatch, plus the memory it
    takes to hold it.

    These assert on rows FETCHED, not rows deleted -- the old code deleted the
    right number while reading everything.
    """

    def test_max_age_branch_fetches_at_most_one_window(self):
        store = _BacklogStore(backlog=50_000)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy(
                "observability_events",
                enabled=True,
                max_age_seconds=1,
                batch_size=2_000,
            )
        )
        report = service.dry_run("observability_events", actor="test")

        assert store.step1_fetched == [2_000], (
            "step 1 fetched %r rows; a 50k backlog must yield exactly one "
            "2000-row window" % store.step1_fetched
        )
        assert report.batch_capped is True, (
            "the backlog beyond the window must still be reported, or the tick "
            "loop stops draining"
        )
        assert store.counts, "batch_capped must come from a COUNT(*), not from len()"

    def test_max_age_window_is_the_oldest_rows(self):
        store = _BacklogStore(backlog=10_000)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy(
                "observability_events",
                enabled=True,
                max_age_seconds=1,
                batch_size=100,
            )
        )
        service.dry_run("observability_events", actor="test")

        window = store.in_clause_binds[0]
        assert window[0] == "row0000000", "the window must start at the oldest row"
        assert window[-1] == "row0000099", "the window must be a contiguous oldest run"

    def test_max_rows_branch_never_reads_the_whole_table(self):
        """The worse branch: it had no WHERE clause at all."""
        store = _BacklogStore(backlog=0, total_rows=1_000_000)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy(
                "observability_events",
                enabled=True,
                max_rows=1_000,
                batch_size=2_000,
            )
        )
        report = service.dry_run("observability_events", actor="test")

        assert store.step1_fetched == [2_000], (
            "step 1 fetched %r rows; the max_rows branch must fetch only the "
            "oldest window of the excess, never the table" % store.step1_fetched
        )
        # 1,000,000 rows - keep 1,000 = 999,000 excess, far beyond one window.
        assert report.batch_capped is True
        assert report.eligible_rows == 2_000

    def test_max_rows_excess_smaller_than_window_is_not_capped(self):
        store = _BacklogStore(backlog=0, total_rows=1_050)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy(
                "observability_events",
                enabled=True,
                max_rows=1_000,
                batch_size=2_000,
            )
        )
        report = service.dry_run("observability_events", actor="test")

        assert store.step1_fetched == [50], (
            "only the 50 excess rows may be fetched, not a full window and not "
            "the table: %r" % store.step1_fetched
        )
        assert report.batch_capped is False
        assert report.eligible_rows == 50

    def test_max_rows_below_limit_fetches_nothing(self):
        store = _BacklogStore(backlog=0, total_rows=10)
        service = RetentionService(store)
        service.set_policy(
            RetentionPolicy("observability_events", enabled=True, max_rows=1_000)
        )
        report = service.dry_run("observability_events", actor="test")

        assert store.step1_fetched == [], "nothing is over the limit; read nothing"
        assert report.eligible_rows == 0
        assert "no_candidates" in report.exclusion_reasons

    def test_max_rows_prunes_oldest_excess_in_batches_against_a_real_store(
        self, store, retention
    ):
        """End-to-end on PostgreSQL: keep the newest, drain the oldest."""
        ids = []
        for i in range(10):
            obs_id = new_id("obs")
            ts = "2025-01-%02dT12:00:00+00:00" % (i + 1)
            store.execute(
                "INSERT INTO observability_events"
                " (id, kind, layer, source, level, name, value, unit, detail, created_at)"
                " VALUES (?, 'log', 'control_plane', 'test', 'info', 'cnt.evt',"
                " NULL, '', '{}', ?)",
                (obs_id, ts),
            )
            ids.append(obs_id)

        pol = RetentionPolicy(
            "observability_events", enabled=True, max_rows=3, batch_size=4
        )
        first = retention.prune("observability_events", override_policy=pol)
        assert first.deleted_rows == 4
        assert first.batch_capped is True, "7 excess > 4 window: more remains"

        second = retention.prune("observability_events", override_policy=pol)
        assert second.deleted_rows == 3
        assert second.batch_capped is False, "3 excess <= 4 window: drained"

        remaining = [
            r["id"]
            for r in store.query_all(
                "SELECT id FROM observability_events ORDER BY created_at ASC"
            )
        ]
        assert remaining == ids[7:], "the 3 newest survive, the 7 oldest went"
