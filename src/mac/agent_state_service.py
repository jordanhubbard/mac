"""Agent state-overlay service: moods + naps + agent-events audit.

Moods are self-reported transient agent states (e.g., focused, blocked,
debugging). Naps are scheduled summarization windows when an agent
drains, writes a summary evidence row, and returns to idle. Both are
overlays on the agent — the underlying ``agents`` row is updated only
when status changes (begin_nap → DRAINING, complete_nap → IDLE).

``agent_events`` is the cross-overlay audit log; every mood and nap
transition writes an event here plus a matching observability log.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    Agent,
    AgentStatus,
    Evidence,
    MOOD_MODES,
    MoodOverlay,
    NAP_DEFAULT_DURATION_MINUTES,
    NAP_WINDOW_MINUTES,
    NapRun,
    NapSchedule,
    NapStatus,
    NotFoundError,
    TransitionError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)
from mac.observability_service import ObservabilityService


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


def _deterministic_nap_offset(agent_name: str) -> int:
    """MD5-derived per-hour offset in minutes, in [0, NAP_WINDOW_MINUTES)."""
    digest = hashlib.md5(agent_name.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], byteorder="little", signed=False)
    return int(value % NAP_WINDOW_MINUTES)


def _nap_cycle_start(reference: datetime) -> datetime:
    day_start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = int((reference - day_start).total_seconds() // 60)
    bucket_minutes = (elapsed_minutes // NAP_WINDOW_MINUTES) * NAP_WINDOW_MINUTES
    return day_start + timedelta(minutes=bucket_minutes)


class AgentStateService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_agent: Callable[[str], Agent],
        get_evidence: Callable[[str], Evidence],
        agent_has_active_lease: Callable[[str], bool],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_agent = get_agent
        self._get_evidence = get_evidence
        self._agent_has_active_lease = agent_has_active_lease

    # Moods -------------------------------------------------------------

    def set_mood(
        self,
        agent_id: str,
        mode: str,
        set_by: Optional[str] = None,
        reason: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MoodOverlay:
        agent = self._get_agent(agent_id)
        mode_value = _state_value(mode)
        if mode_value not in MOOD_MODES:
            raise ValidationError(
                "unsupported mood mode: %s (allowed: %s)"
                % (mode_value, ", ".join(sorted(MOOD_MODES)))
            )
        actor = (set_by or agent.id).strip() or agent.id
        now = utcnow()
        expires_at: Optional[str] = None
        if ttl_seconds is not None:
            if int(ttl_seconds) <= 0:
                raise ValidationError("mood ttl_seconds must be > 0 when provided")
            expires_at = (
                parse_time(now) + timedelta(seconds=int(ttl_seconds))
            ).isoformat(timespec="microseconds")
        overlay_id = new_id("mood")
        metadata_json = json_dumps(ensure_json_object(metadata))
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE mood_overlays
                SET cleared_at = ?, cleared_by = ?, cleared_reason = ?
                WHERE agent_id = ? AND cleared_at IS NULL
                """,
                (now, actor, "replaced", agent.id),
            )
            conn.execute(
                """
                INSERT INTO mood_overlays (
                    id, agent_id, mode, reason, metadata,
                    set_by, set_at, expires_at,
                    cleared_at, cleared_by, cleared_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (overlay_id, agent.id, mode_value, reason, metadata_json, actor, now, expires_at),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.mood_set",
                actor,
                {
                    "overlay_id": overlay_id,
                    "mode": mode_value,
                    "reason": reason,
                    "expires_at": expires_at,
                },
                now,
            )
        return self.get_mood_overlay(overlay_id)

    def get_current_mood(self, agent_id: str) -> Optional[MoodOverlay]:
        agent = self._get_agent(agent_id)
        now = utcnow()
        row = self.store.query_one(
            """
            SELECT * FROM mood_overlays
            WHERE agent_id = ?
              AND cleared_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY set_at DESC, id DESC
            LIMIT 1
            """,
            (agent.id, now),
        )
        return self._mood_from_row(row) if row is not None else None

    def clear_mood(
        self,
        agent_id: str,
        cleared_by: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Optional[MoodOverlay]:
        agent = self._get_agent(agent_id)
        actor = (cleared_by or agent.id).strip() or agent.id
        now = utcnow()
        with self.store.transaction() as conn:
            row = conn.execute(
                """
                SELECT id FROM mood_overlays
                WHERE agent_id = ? AND cleared_at IS NULL
                ORDER BY set_at DESC, id DESC
                LIMIT 1
                """,
                (agent.id,),
            ).fetchone()
            if row is None:
                return None
            overlay_id = row["id"]
            conn.execute(
                """
                UPDATE mood_overlays
                SET cleared_at = ?, cleared_by = ?, cleared_reason = ?
                WHERE id = ?
                """,
                (now, actor, reason, overlay_id),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.mood_cleared",
                actor,
                {"overlay_id": overlay_id, "reason": reason},
                now,
            )
        return self.get_mood_overlay(overlay_id)

    def get_mood_overlay(self, overlay_id: str) -> MoodOverlay:
        row = self.store.query_one(
            "SELECT * FROM mood_overlays WHERE id = ?", (overlay_id,)
        )
        if row is None:
            raise NotFoundError("mood overlay not found: %s" % overlay_id)
        return self._mood_from_row(row)

    def list_mood_history(self, agent_id: str, limit: int = 50) -> List[MoodOverlay]:
        agent = self._get_agent(agent_id)
        rows = self.store.query_all(
            """
            SELECT * FROM mood_overlays
            WHERE agent_id = ?
            ORDER BY set_at DESC, id DESC
            LIMIT ?
            """,
            (agent.id, min(max(1, int(limit)), 500)),
        )
        return [self._mood_from_row(row) for row in rows]

    # Config flags --------------------------------------------------------
    #
    # Allowlisted runtime-settable flags (mac/config_flags.py), scoped per
    # (agent, flag, channel). The conversational entry point: a user asks in
    # chat, the agent's gateway tool calls the self-scoped API, the value
    # lands here with an agent_events audit row. Effective resolution is
    # channel row -> agent-global row ('') -> registry default.

    def set_config_flag(
        self,
        agent_id: str,
        flag: str,
        value: Any,
        channel: str = "",
        set_by: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        from mac.config_flags import normalise_channel, validate_flag_value

        agent = self._get_agent(agent_id)
        channel_key = normalise_channel(channel)
        normalised = validate_flag_value(flag, value)
        actor = (set_by or agent.id).strip() or agent.id
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_config_flags (
                    agent_id, flag, channel, value, set_by, reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, flag, channel) DO UPDATE SET
                    value = excluded.value,
                    set_by = excluded.set_by,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    agent.id,
                    flag,
                    channel_key,
                    json_dumps(normalised),
                    actor,
                    reason,
                    now,
                ),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.config_flag_set",
                actor,
                {
                    "flag": flag,
                    "channel": channel_key,
                    "value": normalised,
                    "reason": reason,
                },
                now,
            )
        return {
            "agent_id": agent.id,
            "flag": flag,
            "channel": channel_key,
            "value": normalised,
            "set_by": actor,
            "reason": reason,
            "updated_at": now,
            "source": "channel" if channel_key else "agent",
        }

    def get_config_flag(
        self,
        agent_id: str,
        flag: str,
        channel: str = "",
    ) -> Dict[str, Any]:
        from mac.config_flags import (
            flag_default,
            normalise_channel,
            validate_flag_value,
        )

        agent = self._get_agent(agent_id)
        channel_key = normalise_channel(channel)
        # Validates the flag name (raises on unknown flags) without
        # requiring a stored row.
        flag_default(flag)
        scopes = [channel_key] if channel_key else []
        scopes.append("")
        for scope in scopes:
            row = self.store.query_one(
                """
                SELECT * FROM agent_config_flags
                WHERE agent_id = ? AND flag = ? AND channel = ?
                """,
                (agent.id, flag, scope),
            )
            if row is not None:
                return {
                    "agent_id": agent.id,
                    "flag": flag,
                    "channel": channel_key,
                    "value": validate_flag_value(flag, json_loads(row["value"], None)),
                    "set_by": row["set_by"],
                    "reason": row["reason"],
                    "updated_at": row["updated_at"],
                    "source": "channel" if row["channel"] else "agent",
                }
        return {
            "agent_id": agent.id,
            "flag": flag,
            "channel": channel_key,
            "value": flag_default(flag),
            "set_by": None,
            "reason": None,
            "updated_at": None,
            "source": "default",
        }

    def list_config_flags(
        self,
        agent_id: str,
        channel: str = "",
    ) -> List[Dict[str, Any]]:
        """Effective value of every registry flag for one (agent, channel)."""
        from mac.config_flags import CONFIG_FLAG_REGISTRY

        results = []
        for flag in sorted(CONFIG_FLAG_REGISTRY):
            spec = CONFIG_FLAG_REGISTRY[flag]
            entry = self.get_config_flag(agent_id, flag, channel=channel)
            entry["description"] = spec["description"]
            entry["type"] = spec["type"]
            if spec["type"] == "enum":
                entry["values"] = list(spec["values"])
            results.append(entry)
        return results

    def clear_config_flag(
        self,
        agent_id: str,
        flag: str,
        channel: str = "",
        cleared_by: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        from mac.config_flags import flag_default, normalise_channel

        agent = self._get_agent(agent_id)
        channel_key = normalise_channel(channel)
        flag_default(flag)
        actor = (cleared_by or agent.id).strip() or agent.id
        now = utcnow()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM agent_config_flags
                WHERE agent_id = ? AND flag = ? AND channel = ?
                """,
                (agent.id, flag, channel_key),
            )
            if cursor.rowcount == 0:
                return False
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.config_flag_cleared",
                actor,
                {"flag": flag, "channel": channel_key, "reason": reason},
                now,
            )
        return True

    # Nap schedules + runs ----------------------------------------------

    def configure_nap(
        self,
        agent_id: str,
        offset_minutes: Optional[int] = None,
        window_minutes: int = NAP_DEFAULT_DURATION_MINUTES,
        enabled: bool = True,
        actor: Optional[str] = None,
    ) -> NapSchedule:
        agent = self._get_agent(agent_id)
        if offset_minutes is None:
            offset_minutes = _deterministic_nap_offset(agent.name)
        offset_minutes = int(offset_minutes)
        if not 0 <= offset_minutes < NAP_WINDOW_MINUTES:
            raise ValidationError(
                "nap offset_minutes must be in [0, %d)" % NAP_WINDOW_MINUTES
            )
        window_minutes = int(window_minutes)
        if window_minutes <= 0 or window_minutes > 120:
            raise ValidationError("nap window_minutes must be in (0, 120]")
        now = utcnow()
        actor_value = (actor or agent.id).strip() or agent.id
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT enabled, offset_minutes, window_minutes FROM nap_schedules WHERE agent_id = ?",
                (agent.id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO nap_schedules (
                    agent_id, offset_minutes, window_minutes, enabled,
                    last_completed_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    offset_minutes = excluded.offset_minutes,
                    window_minutes = excluded.window_minutes,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (agent.id, offset_minutes, window_minutes, 1 if enabled else 0, now),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.nap_configured",
                actor_value,
                {
                    "offset_minutes": offset_minutes,
                    "window_minutes": window_minutes,
                    "enabled": bool(enabled),
                    "previous": (
                        {
                            "offset_minutes": existing["offset_minutes"],
                            "window_minutes": existing["window_minutes"],
                            "enabled": bool(existing["enabled"]),
                        }
                        if existing is not None
                        else None
                    ),
                },
                now,
            )
        return self.get_nap_schedule(agent.id)

    def get_nap_schedule(self, agent_id: str) -> Optional[NapSchedule]:
        agent = self._get_agent(agent_id)
        row = self.store.query_one(
            "SELECT * FROM nap_schedules WHERE agent_id = ?", (agent.id,)
        )
        return self._schedule_from_row(row) if row is not None else None

    def list_nap_schedules(self) -> List[NapSchedule]:
        rows = self.store.query_all(
            "SELECT * FROM nap_schedules ORDER BY offset_minutes, agent_id"
        )
        return [self._schedule_from_row(row) for row in rows]

    def next_nap_window(
        self,
        agent_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, str]]:
        schedule = self.get_nap_schedule(agent_id)
        if schedule is None or not schedule.enabled:
            return None
        reference = now if now is not None else datetime.now(timezone.utc)
        candidate = _nap_cycle_start(reference) + timedelta(minutes=schedule.offset_minutes)
        if candidate <= reference:
            candidate = candidate + timedelta(minutes=NAP_WINDOW_MINUTES)
        end = candidate + timedelta(minutes=schedule.window_minutes)
        return {
            "agent_id": schedule.agent_id,
            "start": candidate.isoformat(timespec="microseconds"),
            "end": end.isoformat(timespec="microseconds"),
            "offset_minutes": schedule.offset_minutes,
            "window_minutes": schedule.window_minutes,
        }

    def begin_nap(
        self,
        agent_id: str,
        actor: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> NapRun:
        agent = self._get_agent(agent_id)
        if self._agent_has_active_lease(agent.id):
            raise ValidationError(
                "agent %s holds an active lease; release it before napping" % agent.id
            )
        actor_value = (actor or agent.id).strip() or agent.id
        now = utcnow()
        run_id = new_id("nap")
        with self.store.transaction() as conn:
            # Reap any prior RUNNING run for this agent before starting a
            # new one. Without this, a cycle that died after begin_nap
            # (e.g. the process was killed mid-nap) leaves an orphan
            # RUNNING row that never resolves; the next nap would then
            # stack a second RUNNING run on top of it.
            conn.execute(
                """
                UPDATE nap_runs
                SET status = ?, completed_at = ?, updated_at = ?, detail = ?
                WHERE agent_id = ? AND status = ?
                """,
                (
                    NapStatus.FAILED.value,
                    now,
                    now,
                    json_dumps({"failure_reason": "superseded by a new nap_run"}),
                    agent.id,
                    NapStatus.RUNNING.value,
                ),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, current_task_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (AgentStatus.DRAINING.value, now, agent.id),
            )
            conn.execute(
                """
                INSERT INTO nap_runs (
                    id, agent_id, status, started_at, completed_at,
                    summary_evidence_id, detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    agent.id,
                    NapStatus.RUNNING.value,
                    now,
                    json_dumps(ensure_json_object(detail)),
                    now,
                    now,
                ),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.nap_started",
                actor_value,
                {"nap_run_id": run_id},
                now,
            )
        return self.get_nap_run(run_id)

    def complete_nap(
        self,
        run_id: str,
        summary_evidence_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> NapRun:
        run = self.get_nap_run(run_id)
        if run.status != NapStatus.RUNNING.value:
            raise TransitionError(
                "nap_run %s is %s, not running" % (run_id, run.status)
            )
        if summary_evidence_id is not None:
            evidence = self._get_evidence(summary_evidence_id)
            if evidence.kind != "log":
                raise ValidationError(
                    "nap summary evidence must have kind='log' (got %r)" % evidence.kind
                )
        agent = self._get_agent(run.agent_id)
        actor_value = (actor or agent.id).strip() or agent.id
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE nap_runs
                SET status = ?, completed_at = ?, summary_evidence_id = ?,
                    detail = COALESCE(?, detail), updated_at = ?
                WHERE id = ?
                """,
                (
                    NapStatus.COMPLETED.value,
                    now,
                    summary_evidence_id,
                    json_dumps(ensure_json_object(detail)) if detail is not None else None,
                    now,
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE nap_schedules
                SET last_completed_at = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (now, now, agent.id),
            )
            # Only restore the agent if it is still DRAINING — an offline
            # transition during the nap wins.
            conn.execute(
                """
                UPDATE agents
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (AgentStatus.IDLE.value, now, agent.id, AgentStatus.DRAINING.value),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.nap_completed",
                actor_value,
                {
                    "nap_run_id": run_id,
                    "summary_evidence_id": summary_evidence_id,
                },
                now,
            )
        return self.get_nap_run(run_id)

    def fail_nap(
        self,
        run_id: str,
        reason: str,
        actor: Optional[str] = None,
    ) -> NapRun:
        run = self.get_nap_run(run_id)
        if run.status != NapStatus.RUNNING.value:
            raise TransitionError(
                "nap_run %s is %s, not running" % (run_id, run.status)
            )
        agent = self._get_agent(run.agent_id)
        actor_value = (actor or agent.id).strip() or agent.id
        if not reason:
            raise ValidationError("fail_nap requires a reason")
        now = utcnow()
        merged_detail = dict(run.detail)
        merged_detail["failure_reason"] = reason
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE nap_runs
                SET status = ?, completed_at = ?, detail = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    NapStatus.FAILED.value,
                    now,
                    json_dumps(merged_detail),
                    now,
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE agents
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (AgentStatus.IDLE.value, now, agent.id, AgentStatus.DRAINING.value),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.nap_failed",
                actor_value,
                {"nap_run_id": run_id, "reason": reason},
                now,
            )
        return self.get_nap_run(run_id)

    def get_nap_run(self, run_id: str) -> NapRun:
        row = self.store.query_one(
            "SELECT * FROM nap_runs WHERE id = ?", (run_id,)
        )
        if row is None:
            raise NotFoundError("nap_run not found: %s" % run_id)
        return self._run_from_row(row)

    def list_nap_runs(self, agent_id: Optional[str] = None) -> List[NapRun]:
        if agent_id is not None:
            agent = self._get_agent(agent_id)
            rows = self.store.query_all(
                "SELECT * FROM nap_runs WHERE agent_id = ? ORDER BY started_at DESC, id DESC",
                (agent.id,),
            )
        else:
            rows = self.store.query_all(
                "SELECT * FROM nap_runs ORDER BY started_at DESC, id DESC"
            )
        return [self._run_from_row(row) for row in rows]

    # Agent-event audit trail (shared by moods, naps, future overlays) -

    def insert_agent_event(
        self,
        conn: Any,
        agent_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO agent_events (id, agent_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("aevt"), agent_id, event_type, actor, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            conn,
            "log",
            event_type,
            "control_plane",
            "agent",
            "info",
            None,
            "",
            "agent",
            agent_id,
            {"actor": actor, **detail},
            when,
        )

    # Row hydration -----------------------------------------------------

    def _mood_from_row(self, row: Any) -> MoodOverlay:
        return MoodOverlay(
            row["id"],
            row["agent_id"],
            row["mode"],
            row["reason"],
            json_loads(row["metadata"], {}),
            row["set_by"],
            row["set_at"],
            row["expires_at"],
            row["cleared_at"],
            row["cleared_by"],
            row["cleared_reason"],
        )

    def _schedule_from_row(self, row: Any) -> NapSchedule:
        return NapSchedule(
            row["agent_id"],
            int(row["offset_minutes"]) % NAP_WINDOW_MINUTES,
            int(row["window_minutes"]),
            bool(row["enabled"]),
            row["last_completed_at"],
            row["updated_at"],
        )

    def _run_from_row(self, row: Any) -> NapRun:
        return NapRun(
            row["id"],
            row["agent_id"],
            row["status"],
            row["started_at"],
            row["completed_at"],
            row["summary_evidence_id"],
            json_loads(row["detail"], {}),
            row["created_at"],
            row["updated_at"],
        )
