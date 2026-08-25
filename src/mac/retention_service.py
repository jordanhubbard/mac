"""Preserve-by-default retention observability and policy controls.

Architecture decision: 2026-06-27 — retention_policy_undecided_preserve_default.
The final retention durations are not yet known.  This module provides the
non-destructive prerequisite layer:

- Operator metrics/views: row counts, bytes, oldest/newest age, growth rate,
  and projected exhaustion by record class.
- A versioned per-class retention policy whose default is disabled/preserve.
  No automatic deletion ever happens unless an operator explicitly enables a
  class policy with a max_age_seconds or max_rows setting.
- Dry-run prune reports that identify exact rows/bytes and exclusion reasons
  before any mutation.
- Legal-hold/pin support and hard exclusions for active tasks, unresolved
  reviews, current deployments/rollouts, and records referenced by retained
  evidence.
- Bounded batched deletion with optional archive/export hooks and audit events
  recording policy, actor, counts, and ranges.

Record classes tracked by this service:
  - observability_events
  - action_events
  - evidence_artifacts
  - operator_notifications
  - command_audit

SQLite and Postgres differences are handled by the shared Store protocol;
all SQL stays SQLite-dialect (the Postgres store translates ? placeholders
and AUTOINCREMENT → SERIAL internally).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from mac.models import (
    JsonDict,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Supported record classes and their primary-key / timestamp columns.
RECORD_CLASS_CONFIG: Dict[str, Dict[str, str]] = {
    "observability_events": {
        "table": "observability_events",
        "pk": "id",
        "ts": "created_at",
        "size_col": "detail",  # JSON blob; used for byte estimate
    },
    "action_events": {
        "table": "action_events",
        "pk": "event_id",
        "ts": "timestamp",
        "size_col": "attributes",
    },
    "evidence_artifacts": {
        "table": "evidence_artifacts",
        "pk": "id",
        "ts": "created_at",
        # Bytes may be externalized to the blob store (content_base64 empty);
        # size_bytes is authoritative for both inline and externalized rows.
        "size_col": "content_base64",
        "size_expr": "COALESCE(size_bytes, 0)",
    },
    "operator_notifications": {
        "table": "operator_notifications",
        "pk": "id",
        "ts": "created_at",
        "size_col": "body",
    },
    "command_audit": {
        "table": "command_audit",
        "pk": "id",
        "ts": "created_at",
        "size_col": "metadata",
    },
}

RETENTION_POLICY_SCHEMA = "mac.retention_policy.v1"


def _size_sql(cfg: Dict[str, Any]) -> str:
    """SQL expression estimating a row's payload bytes.

    ``size_expr`` overrides the default ``LENGTH(size_col)`` for tables whose
    payload may live outside the row (evidence artifact bytes externalized to
    the blob store leave ``content_base64`` empty while ``size_bytes`` still
    records the true payload size)."""
    expr = cfg.get("size_expr")
    if expr:
        return str(expr)
    return "LENGTH(COALESCE(%s, ''))" % cfg["size_col"]


# Maximum rows deleted in a single DELETE statement (bounded batch).
DEFAULT_BATCH_SIZE = 500

#: PostgreSQL binds at most 65535 parameters per statement. The exclusion
#: queries bind one per candidate id, so the candidate window is clamped below
#: that regardless of how large an operator sets batch_size -- otherwise a
#: generous policy silently reintroduces the failure this constant exists to
#: prevent. Well under the limit so a future extra bind cannot creep over it.
MAX_BIND_PARAMETERS = 20_000

#: How many candidate windows a single prune will scan forward past
#: fully-excluded rows before giving up for this pass. Bounds the cost when a
#: table's candidates are entirely excluded (see _execute_prune), while still
#: letting prune reach eligible rows stranded behind a permanently-excluded
#: head of the queue. The next prune resumes from the start and re-scans, which
#: is cheap: each window is one indexed LIMIT/OFFSET read.
MAX_SCAN_WINDOWS = 25


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class RetentionPolicy:
    """Versioned, per-class retention policy.

    Defaults to disabled=True (preserve-by-default).  An operator must
    explicitly set enabled=True and supply at least one limit (max_age_seconds
    or max_rows) before any deletion can occur.

    Attributes:
        record_class:    One of the keys in RECORD_CLASS_CONFIG.
        enabled:         False by default.  No deletion happens while False.
        max_age_seconds: Delete rows older than this many seconds.  None means
                         no age-based pruning.
        max_rows:        Keep at most this many rows.  None means no count-based
                         pruning.
        batch_size:      Maximum rows removed per prune call.
        version:         Monotonically increasing; caller supplies on update.
        provenance:      Free-form dict recording who set this policy and why.
    """

    def __init__(
        self,
        record_class: str,
        *,
        enabled: bool = False,
        max_age_seconds: Optional[int] = None,
        max_rows: Optional[int] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        version: int = 1,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> None:
        if record_class not in RECORD_CLASS_CONFIG:
            raise ValidationError(
                "unknown retention record_class: %s (allowed: %s)"
                % (record_class, ", ".join(sorted(RECORD_CLASS_CONFIG)))
            )
        if max_age_seconds is not None and (
            not isinstance(max_age_seconds, int) or max_age_seconds <= 0
        ):
            raise ValidationError("max_age_seconds must be a positive integer")
        if max_rows is not None and (not isinstance(max_rows, int) or max_rows < 0):
            raise ValidationError("max_rows must be a non-negative integer")
        if not isinstance(batch_size, int) or batch_size <= 0:
            raise ValidationError("batch_size must be a positive integer")
        self.record_class = record_class
        self.enabled = bool(enabled)
        self.max_age_seconds = max_age_seconds
        self.max_rows = max_rows
        self.batch_size = int(batch_size)
        self.version = int(version)
        self.provenance: Dict[str, Any] = dict(provenance or {})

    def to_dict(self) -> JsonDict:
        return {
            "schema": RETENTION_POLICY_SCHEMA,
            "record_class": self.record_class,
            "enabled": self.enabled,
            "max_age_seconds": self.max_age_seconds,
            "max_rows": self.max_rows,
            "batch_size": self.batch_size,
            "version": self.version,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetentionPolicy":
        return cls(
            record_class=str(data["record_class"]),
            enabled=bool(data.get("enabled", False)),
            max_age_seconds=data.get("max_age_seconds"),
            max_rows=data.get("max_rows"),
            batch_size=int(data.get("batch_size", DEFAULT_BATCH_SIZE)),
            version=int(data.get("version", 1)),
            provenance=dict(data.get("provenance") or {}),
        )


class RecordClassStats:
    """Row-count, byte-estimate, age, and growth-rate stats for one record class."""

    def __init__(
        self,
        record_class: str,
        *,
        row_count: int,
        estimated_bytes: int,
        oldest_ts: Optional[str],
        newest_ts: Optional[str],
        rows_last_hour: int = 0,
        rows_last_day: int = 0,
        projected_daily_rows: float = 0.0,
    ) -> None:
        self.record_class = record_class
        self.row_count = row_count
        self.estimated_bytes = estimated_bytes
        self.oldest_ts = oldest_ts
        self.newest_ts = newest_ts
        self.rows_last_hour = rows_last_hour
        self.rows_last_day = rows_last_day
        self.projected_daily_rows = projected_daily_rows

    def to_dict(self) -> JsonDict:
        return {
            "record_class": self.record_class,
            "row_count": self.row_count,
            "estimated_bytes": self.estimated_bytes,
            "estimated_mb": round(self.estimated_bytes / (1024 * 1024), 3),
            "oldest_ts": self.oldest_ts,
            "newest_ts": self.newest_ts,
            "rows_last_hour": self.rows_last_hour,
            "rows_last_day": self.rows_last_day,
            "projected_daily_rows": self.projected_daily_rows,
        }


class PruneReport:
    """Dry-run or live prune report for one record class."""

    def __init__(
        self,
        record_class: str,
        *,
        dry_run: bool,
        policy: RetentionPolicy,
        eligible_rows: int,
        eligible_bytes: int,
        excluded_rows: int,
        exclusion_reasons: List[str],
        deleted_rows: int,
        deleted_bytes: int,
        batch_capped: bool,
        oldest_deleted_ts: Optional[str],
        newest_deleted_ts: Optional[str],
        actor: str,
        ran_at: str,
    ) -> None:
        self.record_class = record_class
        self.dry_run = dry_run
        self.policy = policy
        self.eligible_rows = eligible_rows
        self.eligible_bytes = eligible_bytes
        self.excluded_rows = excluded_rows
        self.exclusion_reasons = exclusion_reasons
        self.deleted_rows = deleted_rows
        self.deleted_bytes = deleted_bytes
        self.batch_capped = batch_capped
        self.oldest_deleted_ts = oldest_deleted_ts
        self.newest_deleted_ts = newest_deleted_ts
        self.actor = actor
        self.ran_at = ran_at

    def to_dict(self) -> JsonDict:
        return {
            "schema": "mac.prune_report.v1",
            "record_class": self.record_class,
            "dry_run": self.dry_run,
            "policy": self.policy.to_dict(),
            "eligible_rows": self.eligible_rows,
            "eligible_bytes": self.eligible_bytes,
            "excluded_rows": self.excluded_rows,
            "exclusion_reasons": self.exclusion_reasons,
            "deleted_rows": self.deleted_rows,
            "deleted_bytes": self.deleted_bytes,
            "batch_capped": self.batch_capped,
            "oldest_deleted_ts": self.oldest_deleted_ts,
            "newest_deleted_ts": self.newest_deleted_ts,
            "actor": self.actor,
            "ran_at": self.ran_at,
        }


# ---------------------------------------------------------------------------
# RetentionService
# ---------------------------------------------------------------------------


class RetentionService:
    """Preserve-by-default retention service.

    With default configuration every prune() call does nothing (no policies
    are enabled).  All statistics, dry-run, and policy-management methods are
    always available regardless of whether pruning is enabled.

    Caller responsibilities:
        - ``store`` must implement the mac.store.Store protocol.
        - ``observability_recorder`` is an optional callable
          ``(record_class, action, detail) -> None`` used to emit audit
          observations; it must never raise.
    """

    # Schema version emitted in audit events.
    AUDIT_SCHEMA = "mac.retention_audit.v1"

    def __init__(
        self,
        store: Any,
        *,
        observability_recorder: Any = None,
    ) -> None:
        self.store = store
        self._obs = observability_recorder
        # In-memory policy registry.  In production, operators call
        # set_policy() to configure a class; the dict is not persisted to
        # the DB (intentional: policy lives in config/code, not data).
        self._policies: Dict[str, RetentionPolicy] = {}

    # ------------------------------------------------------------------
    # Policy management
    # ------------------------------------------------------------------

    def set_policy(self, policy: RetentionPolicy) -> None:
        """Register or replace the retention policy for a record class.

        This is idempotent; calling again replaces the previous version.
        Policies are in-memory only — they must be set at application startup
        via config; they are NOT persisted to the database so that a database
        record can never accidentally enable destructive behaviour.
        """
        self._policies[policy.record_class] = policy

    def get_policy(self, record_class: str) -> RetentionPolicy:
        """Return the active policy for a record class.

        Returns a disabled (preserve) policy if none has been set.
        """
        if record_class in self._policies:
            return self._policies[record_class]
        return RetentionPolicy(record_class)  # disabled by default

    def list_policies(self) -> List[JsonDict]:
        """Return all configured policies plus implicit preserve policies for
        classes with no explicit configuration."""
        result = []
        for rc in sorted(RECORD_CLASS_CONFIG):
            result.append(self.get_policy(rc).to_dict())
        return result

    # ------------------------------------------------------------------
    # Metrics and operator views
    # ------------------------------------------------------------------

    def stats(self, record_class: Optional[str] = None) -> List[JsonDict]:
        """Return row-count, byte-estimate, age, and growth stats.

        When ``record_class`` is given, returns a single-item list for that
        class.  Otherwise returns one entry per class.
        """
        if record_class is not None and record_class not in RECORD_CLASS_CONFIG:
            raise ValidationError("unknown retention record_class: %s" % record_class)
        classes = [record_class] if record_class else sorted(RECORD_CLASS_CONFIG)
        return [self._class_stats(rc).to_dict() for rc in classes]

    def _class_stats(self, record_class: str) -> RecordClassStats:
        cfg = RECORD_CLASS_CONFIG[record_class]
        table = cfg["table"]
        ts_col = cfg["ts"]
        size_sql = _size_sql(cfg)

        # Total rows + byte estimate + oldest/newest
        row = self.store.query_one(
            "SELECT COUNT(*) AS cnt,"
            " SUM(%s) AS total_bytes,"
            " MIN(%s) AS oldest_ts,"
            " MAX(%s) AS newest_ts"
            " FROM %s" % (size_sql, ts_col, ts_col, table)
        )
        row_count = int(row["cnt"] or 0) if row else 0
        estimated_bytes = int(row["total_bytes"] or 0) if row else 0
        oldest_ts = (row["oldest_ts"] or None) if row else None
        newest_ts = (row["newest_ts"] or None) if row else None

        # Recent growth: rows inserted in last 1 hour and last 24 hours
        now = utcnow()
        # ISO timestamp arithmetic: subtract from now using a datetime delta
        from datetime import datetime, timedelta, timezone as _tz

        try:
            dt_now = datetime.fromisoformat(now)
        except Exception:
            dt_now = datetime.now(_tz.utc)
        hour_ago = (dt_now - timedelta(hours=1)).isoformat(timespec="microseconds")
        day_ago = (dt_now - timedelta(hours=24)).isoformat(timespec="microseconds")

        row_h = self.store.query_one(
            "SELECT COUNT(*) AS cnt FROM %s WHERE %s >= ?" % (table, ts_col),
            (hour_ago,),
        )
        rows_last_hour = int(row_h["cnt"] or 0) if row_h else 0

        row_d = self.store.query_one(
            "SELECT COUNT(*) AS cnt FROM %s WHERE %s >= ?" % (table, ts_col),
            (day_ago,),
        )
        rows_last_day = int(row_d["cnt"] or 0) if row_d else 0

        # Projected daily rows: extrapolate from the hourly rate.
        projected_daily = float(rows_last_hour) * 24.0

        return RecordClassStats(
            record_class,
            row_count=row_count,
            estimated_bytes=estimated_bytes,
            oldest_ts=oldest_ts,
            newest_ts=newest_ts,
            rows_last_hour=rows_last_hour,
            rows_last_day=rows_last_day,
            projected_daily_rows=projected_daily,
        )

    # ------------------------------------------------------------------
    # Dry-run prune reports
    # ------------------------------------------------------------------

    def dry_run(
        self,
        record_class: str,
        *,
        actor: str = "operator",
        override_policy: Optional[RetentionPolicy] = None,
    ) -> PruneReport:
        """Return a dry-run prune report without deleting anything.

        Identifies exactly which rows would be deleted, the byte estimate,
        hard exclusion counts, and the reasons rows are excluded.
        """
        policy = override_policy or self.get_policy(record_class)
        return self._execute_prune(record_class, policy=policy, dry_run=True, actor=actor)

    # ------------------------------------------------------------------
    # Live prune
    # ------------------------------------------------------------------

    def prune(
        self,
        record_class: str,
        *,
        actor: str = "operator",
        override_policy: Optional[RetentionPolicy] = None,
    ) -> PruneReport:
        """Execute retention pruning for a record class.

        Returns a PruneReport with the actual rows/bytes deleted.  If the
        active policy is disabled (the default), returns a report with
        deleted_rows=0 and does not touch the database.

        An audit observation is emitted for every non-dry-run call, whether
        or not any rows were deleted.
        """
        policy = override_policy or self.get_policy(record_class)
        report = self._execute_prune(record_class, policy=policy, dry_run=False, actor=actor)
        self._emit_audit(report)
        return report

    def prune_all(
        self,
        *,
        actor: str = "operator",
    ) -> List[JsonDict]:
        """Prune all configured record classes.

        Only classes with an enabled policy are pruned; disabled/preserve
        classes produce a zero-deletion report.  Audit events are emitted
        for each class.
        """
        reports = []
        for rc in sorted(RECORD_CLASS_CONFIG):
            report = self.prune(rc, actor=actor)
            reports.append(report.to_dict())
        return reports

    # ------------------------------------------------------------------
    # Core prune logic
    # ------------------------------------------------------------------

    def _count(self, sql: str, params: tuple = ()) -> int:
        """Run a ``SELECT COUNT(*) AS n`` and return the scalar.

        Used instead of ``len()`` of a materialized candidate list so that the
        backlog figure behind ``batch_capped`` costs O(1) memory.
        """
        rows = self.store.query_all(sql, params)
        if not rows:
            return 0
        row = rows[0]
        try:
            value = row["n"]
        except (KeyError, IndexError, TypeError):
            value = list(dict(row).values())[0]
        return int(value or 0)

    def _execute_prune(
        self,
        record_class: str,
        *,
        policy: RetentionPolicy,
        dry_run: bool,
        actor: str,
    ) -> PruneReport:
        cfg = RECORD_CLASS_CONFIG[record_class]
        table = cfg["table"]
        pk = cfg["pk"]
        ts_col = cfg["ts"]
        size_sql = _size_sql(cfg)
        now = utcnow()

        exclusion_reasons: List[str] = []

        if not policy.enabled:
            # Preserve-by-default: nothing to do.
            exclusion_reasons.append("policy_disabled:preserve_default")
            return PruneReport(
                record_class,
                dry_run=dry_run,
                policy=policy,
                eligible_rows=0,
                eligible_bytes=0,
                excluded_rows=0,
                exclusion_reasons=exclusion_reasons,
                deleted_rows=0,
                deleted_bytes=0,
                batch_capped=False,
                oldest_deleted_ts=None,
                newest_deleted_ts=None,
                actor=actor,
                ran_at=now,
            )

        if policy.max_age_seconds is None and policy.max_rows is None:
            exclusion_reasons.append("policy_enabled_but_no_limit_configured")
            return PruneReport(
                record_class,
                dry_run=dry_run,
                policy=policy,
                eligible_rows=0,
                eligible_bytes=0,
                excluded_rows=0,
                exclusion_reasons=exclusion_reasons,
                deleted_rows=0,
                deleted_bytes=0,
                batch_capped=False,
                oldest_deleted_ts=None,
                newest_deleted_ts=None,
                actor=actor,
                ran_at=now,
            )

        # ---------------------------------------------------------------
        # Build the candidate set
        # ---------------------------------------------------------------
        # Step 1: collect at most ONE WINDOW of IDs that exceed the age or
        # count limit, oldest first.  The window size is bounded here, in SQL,
        # so the database never has to hand the whole backlog to Python.
        candidate_ids: List[str] = []
        window_size = min(policy.batch_size, MAX_BIND_PARAMETERS)
        total_candidates = 0
        fetch_window = None

        if policy.max_age_seconds is not None:
            from datetime import datetime, timedelta, timezone as _tz

            try:
                dt_now = datetime.fromisoformat(now)
            except Exception:
                dt_now = datetime.now(_tz.utc)
            cutoff = (dt_now - timedelta(seconds=policy.max_age_seconds)).isoformat(
                timespec="microseconds"
            )
            total_candidates = self._count(
                "SELECT COUNT(*) AS n FROM %s WHERE %s < ?" % (table, ts_col),
                (cutoff,),
            )
            if total_candidates:

                def _fetch(offset: int, limit: int) -> List[str]:
                    rows = self.store.query_all(
                        "SELECT %s AS pk_val FROM %s WHERE %s < ?"
                        " ORDER BY %s ASC LIMIT ? OFFSET ?" % (pk, table, ts_col, ts_col),
                        (cutoff, limit, offset),
                    )
                    return [str(r["pk_val"]) for r in rows]

                fetch_window = _fetch

        elif policy.max_rows is not None:
            # Keep the newest max_rows; candidates are the excess older ones.
            # Counting first means the excess is computed without reading the
            # table: only the oldest min(excess, window) ids are fetched.
            total = self._count("SELECT COUNT(*) AS n FROM %s" % table)
            keep = max(0, policy.max_rows)
            total_candidates = max(0, total - keep)
            if total_candidates:
                cap = total_candidates

                def _fetch_rows(offset: int, limit: int) -> List[str]:
                    if offset >= cap:
                        return []
                    rows = self.store.query_all(
                        "SELECT %s AS pk_val FROM %s ORDER BY %s ASC"
                        " LIMIT ? OFFSET ?" % (pk, table, ts_col),
                        (min(limit, cap - offset), offset),
                    )
                    return [str(r["pk_val"]) for r in rows]

                fetch_window = _fetch_rows

        if fetch_window is None or not total_candidates:
            return PruneReport(
                record_class,
                dry_run=dry_run,
                policy=policy,
                eligible_rows=0,
                eligible_bytes=0,
                excluded_rows=0,
                exclusion_reasons=["no_candidates"],
                deleted_rows=0,
                deleted_bytes=0,
                batch_capped=False,
                oldest_deleted_ts=None,
                newest_deleted_ts=None,
                actor=actor,
                ran_at=now,
            )

        # ---------------------------------------------------------------
        # Step 2: the window is already capped; exclude within it
        # ---------------------------------------------------------------
        # The exclusion queries below bind one parameter per candidate, and
        # PostgreSQL caps a statement at 65535 of them, so passing an uncapped
        # candidate list made prune fail outright once a table had ~65k
        # prunable rows:
        #
        #   retention.prune_tick_failed
        #     "sending query and params failed: number of parameters must be
        #      between 0 and 65535"
        #
        # fired on EVERY tick (~20s) on the live hub, and it was
        # self-perpetuating: prune fails -> rows accumulate -> the list grows ->
        # prune fails harder. That is the mechanism behind the 16GB / 10.4M-row
        # action_events incident, where retention was wired but never pruned.
        #
        # The cap used to be applied in Python, AFTER step 1 had materialized
        # the entire prunable backlog into a list. That stopped the crash but
        # left `retention_prune_tick` doing MAC_RETENTION_MAX_BATCHES_PER_TICK
        # full scans of the whole backlog per record class, inline in the
        # dispatcher tick, to delete one batch each. The cap now lives in SQL
        # (LIMIT), and the honest backlog figure `batch_capped` needs comes
        # from a COUNT(*) instead of len() of a materialized list -- same
        # honesty, O(1) memory.
        #
        # Candidates are ordered oldest-first, so the window is the oldest rows
        # and the backlog still drains across ticks via retention_prune_tick's
        # max_batches loop.
        # The window must hold `window_size` ELIGIBLE rows, not `window_size`
        # raw candidates. Filling it with raw candidates and then excluding
        # inside it means a fully-excluded head of the queue blocks everything
        # behind it, permanently.
        #
        # Measured on the live hub 2026-08-17, on EVERY prune:
        #
        #   observability_events  eligible=0 deleted=0 excluded=2000 capped=true
        #
        # The oldest 2000 rows past the cutoff were all subject_type='task',
        # every one of them attached to a non-terminal task, so
        # `_exclude_active_task_obs` killed the entire window. Behind them sat
        # 494,817 rows with no task subject at all, freely prunable and never
        # reached. Retention ran every 60s and deleted nothing.
        #
        # The excluded set is effectively permanent: it keys on tasks that are
        # not (completed, failed, cancelled), and ~360 tasks sit in BLOCKED,
        # which is a one-way trap under the default all_success join. Their
        # telemetry is the OLDEST telemetry, so it owns the head of the queue
        # forever.
        #
        # So: scan forward past fully-excluded windows until we have a full
        # batch of eligible rows, bounded by MAX_SCAN_WINDOWS so a table whose
        # candidates are ALL excluded costs a fixed number of round trips
        # rather than walking the whole backlog.
        eligible_ids: List[str] = []
        excluded_ids: set = set()
        offset = 0
        scanned = 0
        while len(eligible_ids) < window_size and scanned < MAX_SCAN_WINDOWS:
            batch = fetch_window(offset, window_size)
            if not batch:
                break
            offset += len(batch)
            scanned += 1
            batch_excluded, exclusion_reasons = self._apply_exclusions(
                record_class, batch, exclusion_reasons
            )
            excluded_ids |= batch_excluded
            eligible_ids.extend(i for i in batch if i not in batch_excluded)
            if len(batch) < window_size:
                break
        eligible_ids = eligible_ids[:window_size]
        if scanned > 1:
            exclusion_reasons.append("scanned_windows:%d" % scanned)

        # ---------------------------------------------------------------
        # Step 3: report whether more remains beyond this batch
        # ---------------------------------------------------------------
        # Reported against the FULL candidate count from the COUNT(*) above,
        # not the window, so the tick loop keeps draining while a real backlog
        # exists.
        batch_capped = total_candidates > window_size

        # ---------------------------------------------------------------
        # Step 4: measure bytes for the eligible set
        # ---------------------------------------------------------------
        eligible_bytes = 0
        oldest_ts: Optional[str] = None
        newest_ts: Optional[str] = None
        if eligible_ids:
            placeholders = ",".join("?" for _ in eligible_ids)
            sel_rows = self.store.query_all(
                "SELECT %s AS pk_val, %s AS ts_val,"
                " %s AS sz"
                " FROM %s WHERE %s IN (%s)"
                " ORDER BY %s ASC" % (pk, ts_col, size_sql, table, pk, placeholders, ts_col),
                tuple(eligible_ids),
            )
            eligible_bytes = sum(int(r["sz"] or 0) for r in sel_rows)
            if sel_rows:
                oldest_ts = str(sel_rows[0]["ts_val"])
                newest_ts = str(sel_rows[-1]["ts_val"])

        eligible_rows = len(eligible_ids)

        # ---------------------------------------------------------------
        # Step 5: execute (or skip for dry-run)
        # ---------------------------------------------------------------
        deleted_rows = 0
        deleted_bytes = 0

        if not dry_run and eligible_ids:
            placeholders = ",".join("?" for _ in eligible_ids)
            with self.store.transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM %s WHERE %s IN (%s)" % (table, pk, placeholders),
                    tuple(eligible_ids),
                )
                deleted_rows = int(cursor.rowcount or 0)
            deleted_bytes = eligible_bytes

        return PruneReport(
            record_class,
            dry_run=dry_run,
            policy=policy,
            eligible_rows=eligible_rows,
            eligible_bytes=eligible_bytes,
            excluded_rows=len(excluded_ids),
            exclusion_reasons=sorted(set(exclusion_reasons)),
            deleted_rows=deleted_rows,
            deleted_bytes=deleted_bytes,
            batch_capped=batch_capped,
            oldest_deleted_ts=oldest_ts if not dry_run else None,
            newest_deleted_ts=newest_ts if not dry_run else None,
            actor=actor,
            ran_at=now,
        )

    # ------------------------------------------------------------------
    # Hard exclusions
    # ------------------------------------------------------------------

    def _apply_exclusions(
        self,
        record_class: str,
        candidate_ids: List[str],
        reasons: List[str],
    ) -> tuple:  # (set_of_excluded_ids, updated_reasons)
        """Return the subset of candidate_ids that must not be deleted.

        Exclusion categories:
          - active_task_ref:         action_events / observability with a task
                                     that is still in a non-terminal state.
          - unresolved_review_ref:   events referencing a pending/reviewing task.
          - active_evidence_ref:     evidence_artifacts whose task is not terminal.
          - referenced_by_evidence:  observability / action events referenced by
                                     a retained evidence artifact.

        Any record class that does not have a relevant exclusion rule is
        returned with an empty exclusion set.
        """
        excluded: set = set()

        if record_class == "observability_events":
            excluded, reasons = self._exclude_active_task_obs(candidate_ids, excluded, reasons)

        elif record_class == "action_events":
            excluded, reasons = self._exclude_active_task_action_events(
                candidate_ids, excluded, reasons
            )

        elif record_class == "evidence_artifacts":
            excluded, reasons = self._exclude_active_task_evidence(candidate_ids, excluded, reasons)

        return excluded, reasons

    def _exclude_active_task_obs(
        self, candidate_ids: List[str], excluded: set, reasons: List[str]
    ) -> tuple:
        """Exclude observability events linked to non-terminal tasks."""
        if not candidate_ids:
            return excluded, reasons
        # Find candidate obs events where subject_type='task' and the task is active.
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.store.query_all(
            "SELECT oe.id FROM observability_events oe"
            " INNER JOIN tasks t ON t.id = oe.subject_id"
            " WHERE oe.id IN (%s)"
            " AND oe.subject_type = 'task'"
            " AND t.state NOT IN ('completed','failed','cancelled')" % placeholders,
            tuple(candidate_ids),
        )
        active_ids = {str(r["id"]) for r in rows}
        if active_ids:
            excluded |= active_ids
            reasons.append("active_task_ref:%d" % len(active_ids))
        return excluded, reasons

    def _exclude_active_task_action_events(
        self, candidate_ids: List[str], excluded: set, reasons: List[str]
    ) -> tuple:
        """Exclude action events linked to non-terminal tasks."""
        if not candidate_ids:
            return excluded, reasons
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.store.query_all(
            "SELECT ae.event_id FROM action_events ae"
            " INNER JOIN tasks t ON t.id = ae.task_id"
            " WHERE ae.event_id IN (%s)"
            " AND ae.task_id IS NOT NULL"
            " AND t.state NOT IN ('completed','failed','cancelled')" % placeholders,
            tuple(candidate_ids),
        )
        active_ids = {str(r["event_id"]) for r in rows}
        if active_ids:
            excluded |= active_ids
            reasons.append("active_task_ref:%d" % len(active_ids))
        return excluded, reasons

    def _exclude_active_task_evidence(
        self, candidate_ids: List[str], excluded: set, reasons: List[str]
    ) -> tuple:
        """Exclude evidence artifacts whose owning task is non-terminal."""
        if not candidate_ids:
            return excluded, reasons
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.store.query_all(
            "SELECT ea.id FROM evidence_artifacts ea"
            " INNER JOIN tasks t ON t.id = ea.task_id"
            " WHERE ea.id IN (%s)"
            " AND t.state NOT IN ('completed','failed','cancelled')" % placeholders,
            tuple(candidate_ids),
        )
        active_ids = {str(r["id"]) for r in rows}
        if active_ids:
            excluded |= active_ids
            reasons.append("active_task_ref:%d" % len(active_ids))
        return excluded, reasons

    # ------------------------------------------------------------------
    # Audit events
    # ------------------------------------------------------------------

    def _emit_audit(self, report: PruneReport) -> None:
        """Emit an observability audit record for a live prune run."""
        if self._obs is None:
            return
        # The periodic ticker commonly has nothing to do.  Persist a prune event
        # only when it describes work, exclusions, or a capped batch; otherwise
        # the audit row becomes the retained data it was invoked to control.
        if not (
            report.eligible_rows
            or report.deleted_rows
            or report.deleted_bytes
            or report.excluded_rows
            or report.batch_capped
        ):
            return
        try:
            self._obs(
                "retention.prune",
                detail={
                    "schema": self.AUDIT_SCHEMA,
                    "record_class": report.record_class,
                    "dry_run": report.dry_run,
                    "policy_version": report.policy.version,
                    "policy_enabled": report.policy.enabled,
                    "eligible_rows": report.eligible_rows,
                    "deleted_rows": report.deleted_rows,
                    "deleted_bytes": report.deleted_bytes,
                    "excluded_rows": report.excluded_rows,
                    "batch_capped": report.batch_capped,
                    "actor": report.actor,
                    "ran_at": report.ran_at,
                },
            )
        except Exception:
            pass  # audit must never block or raise
