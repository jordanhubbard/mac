"""Agent state-overlay service: moods, config flags, deploy config and the
agent-events audit.

Moods are self-reported transient agent states (e.g., focused, blocked,
debugging), layered over the ``agents`` row rather than stored on it.

``agent_events`` is the cross-overlay audit log; every mood transition writes
an event here plus a matching observability log.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    Agent,
    MOOD_MODES,
    MoodOverlay,
    NotFoundError,
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


class AgentStateService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_agent: Callable[[str], Agent],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_agent = get_agent

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
            expires_at = (parse_time(now) + timedelta(seconds=int(ttl_seconds))).isoformat(
                timespec="microseconds"
            )
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
        row = self.store.query_one("SELECT * FROM mood_overlays WHERE id = ?", (overlay_id,))
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

    # Deploy config: one consolidated per-agent document of the non-secret
    # "geek knobs" the agent's gateway actually launched with (image tag,
    # sandbox, home channel, model defaults, plugin tuning). Self-reported
    # at gateway startup; `effective_agent_config` merges it with the
    # runtime flag registry so operators have a single place to look.

    DEPLOY_CONFIG_SCHEMA = "mac.agent_deploy_config.v1"
    DEPLOY_CONFIG_MAX_BYTES = 32768
    _SECRETISH_KEY_MARKERS = (
        "token",
        "secret",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "credential",
        "bearer",
        "private_key",
    )

    @classmethod
    def _reject_secretish_keys(cls, document: Any, path: str = "") -> None:
        """Refuse documents carrying keys that look like credentials.

        The deploy-config store is readable fleet-wide by design; rejecting
        (not silently redacting) keeps a gateway from believing a secret was
        durably stored when it never will be.
        """
        if isinstance(document, dict):
            for key, value in document.items():
                key_text = str(key).lower()
                where = "%s.%s" % (path, key) if path else str(key)
                if any(marker in key_text for marker in cls._SECRETISH_KEY_MARKERS):
                    raise ValidationError(
                        "deploy config must not contain secret-like keys: %s" % where
                    )
                cls._reject_secretish_keys(value, where)
        elif isinstance(document, list):
            for index, item in enumerate(document):
                cls._reject_secretish_keys(item, "%s[%d]" % (path, index))

    def report_deploy_config(
        self,
        agent_id: str,
        document: Dict[str, Any],
        reported_by: Optional[str] = None,
        schema: Optional[str] = None,
    ) -> Dict[str, Any]:
        agent = self._get_agent(agent_id)
        doc = ensure_json_object(document)
        if not doc:
            raise ValidationError("deploy config document must be a non-empty object")
        self._reject_secretish_keys(doc)
        schema_name = (schema or self.DEPLOY_CONFIG_SCHEMA).strip()
        encoded = json_dumps(doc)
        if len(encoded.encode("utf-8")) > self.DEPLOY_CONFIG_MAX_BYTES:
            raise ValidationError(
                "deploy config document exceeds %d bytes" % self.DEPLOY_CONFIG_MAX_BYTES
            )
        actor = (reported_by or agent.id).strip() or agent.id
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_deploy_configs (
                    agent_id, document, schema_name, reported_by, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    document = excluded.document,
                    schema_name = excluded.schema_name,
                    reported_by = excluded.reported_by,
                    updated_at = excluded.updated_at
                """,
                (agent.id, encoded, schema_name, actor, now),
            )
            self.insert_agent_event(
                conn,
                agent.id,
                "agent.deploy_config_reported",
                actor,
                {
                    "schema": schema_name,
                    "keys": sorted(doc),
                    "bytes": len(encoded),
                },
                now,
            )
        return {
            "agent_id": agent.id,
            "schema": schema_name,
            "document": doc,
            "reported_by": actor,
            "updated_at": now,
        }

    def get_deploy_config(self, agent_id: str) -> Optional[Dict[str, Any]]:
        agent = self._get_agent(agent_id)
        row = self.store.query_one(
            "SELECT * FROM agent_deploy_configs WHERE agent_id = ?",
            (agent.id,),
        )
        if row is None:
            return None
        return {
            "agent_id": agent.id,
            "schema": row["schema_name"],
            "document": json_loads(row["document"], {}),
            "reported_by": row["reported_by"],
            "updated_at": row["updated_at"],
        }

    def effective_agent_config(self, agent_id: str) -> Dict[str, Any]:
        """Everything configurable about one agent, from one place.

        Merges the agent registry row, the effective runtime flag registry
        (agent-global scope), the gateway-reported deploy config, and the
        current mood overlay — the consolidated view that used to require
        chasing launcher scripts, runtime.env, and plugin constants.
        """
        agent = self._get_agent(agent_id)
        agent_row = agent.to_dict()
        identity = {
            key: agent_row.get(key)
            for key in (
                "id",
                "name",
                "machine_id",
                "status",
                "capabilities",
                "dispatch_hold",
                "hermes_instance_id",
            )
            if key in agent_row
        }
        mood = self.get_current_mood(agent.id)
        return {
            "schema": "mac.agent_effective_config.v1",
            "agent": identity,
            "config_flags": self.list_config_flags(agent.id),
            "deploy_config": self.get_deploy_config(agent.id),
            "mood": mood.to_dict() if mood is not None else None,
        }

    # Agent-event audit trail (shared by moods and future overlays) ----

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
