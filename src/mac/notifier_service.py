"""Operator notification delivery service.

Builds and dispatches operator notifications and agent messages, sanitizing
message values and routing them to the configured notifier channels.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from mac.models import (
    Agent,
    AgentMessage,
    JsonDict,
    MessageType,
    NotFoundError,
    NotifierChannel,
    OperatorNotification,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.messaging_service import FORBIDDEN_MESSAGE_KEYS


SendMessage = Callable[[str, Optional[str], str, Dict[str, Any]], AgentMessage]
EnqueueHumanMessage = Callable[..., Any]
ResolveRepresentation = Callable[..., Dict[str, Any]]


def _message_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: JsonDict = {}
        for key, item in value.items():
            safe_key = str(key)
            if safe_key.lower() in FORBIDDEN_MESSAGE_KEYS:
                renamed = "%s_text" % safe_key
                while renamed in safe:
                    renamed = "%s_text" % renamed
                safe_key = renamed
            safe[safe_key] = _message_safe_value(item)
        return safe
    if isinstance(value, list):
        return [_message_safe_value(item) for item in value]
    return value


class NotifierService:
    """Configuration and delivery bridge for operator notifications.

    mac records task progress once as durable operator_notifications rows. This
    service decides which Hermes-backed human channels receive those rows.
    """

    SUPPORTED_CHANNEL_TYPES = {"hermes", "slack", "telegram"}
    DELIVERY_CLAIM_TIMEOUT_SECONDS = 600

    def __init__(
        self,
        store: Any,
        *,
        list_agents: Callable[[], List[Agent]],
        get_agent: Callable[[str], Agent],
        list_platform_bindings: Callable[..., List[Any]],
        get_platform_binding: Callable[[str], Any],
        send_message: SendMessage,
        record_log: Callable[..., Any],
        enqueue_human_message: Optional[EnqueueHumanMessage] = None,
        resolve_agent_representation: Optional[ResolveRepresentation] = None,
        list_communication_identities: Optional[Callable[..., List[Any]]] = None,
    ) -> None:
        self.store = store
        self._list_agents = list_agents
        self._get_agent = get_agent
        self._list_platform_bindings = list_platform_bindings
        self._get_platform_binding = get_platform_binding
        self._send_message = send_message
        self._record_log = record_log
        self._enqueue_human_message = enqueue_human_message
        self._resolve_agent_representation = resolve_agent_representation
        self._list_communication_identities = list_communication_identities

    def configure_channel(
        self,
        name: str,
        channel_type: str,
        *,
        event_types: Optional[Iterable[str]] = None,
        target: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
        channel_id: Optional[str] = None,
    ) -> NotifierChannel:
        name_value = str(name or "").strip()
        if not name_value:
            raise ValidationError("notifier channel name is required")
        type_value = str(channel_type or "").strip().lower()
        if type_value not in self.SUPPORTED_CHANNEL_TYPES:
            raise ValidationError(
                "unsupported notifier channel_type: %s (allowed: %s)"
                % (channel_type, ", ".join(sorted(self.SUPPORTED_CHANNEL_TYPES)))
            )
        events = sorted({str(item).strip() for item in (event_types or []) if str(item).strip()})
        now = utcnow()
        row = self.store.query_one("SELECT id FROM notifier_channels WHERE name = ?", (name_value,))
        cid = row["id"] if row is not None else channel_id or new_id("ntfc")
        self.store.execute(
            """
            INSERT INTO notifier_channels (
                id, name, channel_type, enabled, event_types, target, metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                channel_type = excluded.channel_type,
                enabled = excluded.enabled,
                event_types = excluded.event_types,
                target = excluded.target,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                cid,
                name_value,
                type_value,
                1 if enabled else 0,
                json_dumps(events),
                json_dumps(ensure_json_object(target)),
                json_dumps(ensure_json_object(metadata)),
                now,
                now,
            ),
        )
        return self.get_channel(cid)

    def get_channel(self, channel_id_or_name: str) -> NotifierChannel:
        row = self.store.query_one(
            "SELECT * FROM notifier_channels WHERE id = ? OR name = ?",
            (channel_id_or_name, channel_id_or_name),
        )
        if row is None:
            raise NotFoundError("notifier channel not found: %s" % channel_id_or_name)
        return self._from_row(row)

    def list_channels(
        self,
        *,
        enabled: Optional[bool] = None,
        channel_type: Optional[str] = None,
    ) -> List[NotifierChannel]:
        clauses: List[str] = []
        params: List[Any] = []
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        if channel_type is not None:
            clauses.append("channel_type = ?")
            params.append(str(channel_type).strip().lower())
        sql = "SELECT * FROM notifier_channels"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name, id"
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def delete_channel(self, channel_id_or_name: str) -> None:
        channel = self.get_channel(channel_id_or_name)
        self.store.execute("DELETE FROM notifier_channels WHERE id = ?", (channel.id,))

    def deliver_pending(
        self,
        *,
        limit: int = 50,
        notification_id: Optional[str] = None,
    ) -> JsonDict:
        stale_before = self._claim_stale_before()
        if notification_id is not None:
            rows = self.store.query_all(
                """
                SELECT * FROM operator_notifications
                WHERE id = ?
                  AND (
                    status = 'pending'
                    OR (
                      status = 'delivering'
                      AND (delivered_at IS NULL OR delivered_at < ?)
                    )
                  )
                """,
                (notification_id, stale_before),
            )
        else:
            rows = self.store.query_all(
                """
                SELECT * FROM operator_notifications
                WHERE status = 'pending'
                   OR (
                     status = 'delivering'
                     AND (delivered_at IS NULL OR delivered_at < ?)
                   )
                ORDER BY created_at, id
                LIMIT ?
                """,
                (stale_before, min(max(1, int(limit)), 500)),
            )
        delivered = 0
        failed = 0
        skipped = 0
        results = []
        for row in rows:
            notification = self._notification_from_row(row)
            if not self._claim_notification(notification.id):
                continue
            try:
                message_ids = self._deliver_notification(notification)
            except Exception as exc:  # noqa: BLE001 - delivery runner must keep draining.
                failed += 1
                self._mark_notification(notification.id, "failed")
                self._record_log(
                    "notifier.delivery_failed",
                    layer="control_plane",
                    source="notifier",
                    level="warning",
                    subject_type=notification.subject_type,
                    subject_id=notification.subject_id,
                    detail={"notification_id": notification.id, "error": str(exc)},
                )
                results.append({"notification_id": notification.id, "status": "failed", "error": str(exc)})
                continue
            if message_ids:
                delivered += 1
                self._mark_notification(notification.id, "delivered")
                results.append(
                    {
                        "notification_id": notification.id,
                        "status": "delivered",
                        "message_ids": message_ids,
                    }
                )
            else:
                skipped += 1
                self._mark_notification(notification.id, "skipped")
                results.append({"notification_id": notification.id, "status": "skipped"})
        return {
            "schema": "mac.notifier.delivery_result.v1",
            "delivered": delivered,
            "failed": failed,
            "skipped": skipped,
            "results": results,
        }

    def _claim_stale_before(self) -> str:
        return (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.DELIVERY_CLAIM_TIMEOUT_SECONDS)
        ).isoformat(timespec="microseconds")

    def _claim_notification(self, notification_id: str) -> bool:
        result = self.store.execute(
            """
            UPDATE operator_notifications
            SET status = 'delivering', delivered_at = ?
            WHERE id = ?
              AND (
                status = 'pending'
                OR (
                  status = 'delivering'
                  AND (delivered_at IS NULL OR delivered_at < ?)
                )
              )
            """,
            (utcnow(), notification_id, self._claim_stale_before()),
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    def _deliver_notification(self, notification: OperatorNotification) -> List[str]:
        # mac-zipf: notifier delivery is two non-atomic statements
        # (send_message + mark_notification). If we crash between them,
        # the notification stays "pending" and a later retry would send
        # a duplicate message. Dedupe by checking for any existing
        # message whose payload carries this notification.id.
        existing = self.store.query_all(
            """
            SELECT id FROM messages
            WHERE json_extract(payload, '$.notification.id') = ?
            """,
            (notification.id,),
        )
        if existing:
            return [row["id"] for row in existing]
        targets = self._configured_targets(notification)
        if not targets and "hermes" in notification.channels:
            targets = self._auto_persona_instance_targets(notification)
        message_ids: List[str] = []
        use_openclaw_outbox = bool(
            self._enqueue_human_message
            and self._resolve_agent_representation
            and self._list_communication_identities
            and self._list_communication_identities(enabled=True)
        )
        for target in targets:
            agent_id = str(target.get("agent_id") or "").strip()
            if not agent_id:
                continue
            target_channel = str(
                target.get("channel_type") or target.get("platform") or ""
            ).strip().lower()
            if use_openclaw_outbox and target_channel in {"slack", "telegram"}:
                delivery_id = self._enqueue_openclaw_delivery(
                    notification, target, agent_id
                )
                if delivery_id:
                    message_ids.append(delivery_id)
                continue
            payload = {
                "schema": "mac.notifier.task_progress.v1",
                "status": notification.event_type,
                "notification": _message_safe_value(notification.to_dict()),
                "channel_type": target.get("channel_type"),
                "target": _message_safe_value(
                    {k: v for k, v in target.items() if k != "agent_id"}
                ),
            }
            message = self._send_message(
                "notifier",
                agent_id,
                MessageType.STATUS_UPDATE.value,
                payload,
            )
            message_ids.append(message.id)
        if message_ids:
            self._record_log(
                "notifier.delivered",
                layer="control_plane",
                source="notifier",
                subject_type=notification.subject_type,
                subject_id=notification.subject_id,
                detail={"notification_id": notification.id, "message_ids": message_ids},
            )
        return message_ids

    def _enqueue_openclaw_delivery(
        self,
        notification: OperatorNotification,
        target: JsonDict,
        agent_id: str,
    ) -> Optional[str]:
        """Queue one human-facing delivery through a represented identity.

        Once the runtime-neutral identity registry is configured, falling back
        to a worker-local provider SDK would violate the OpenClaw-only channel
        contract.  Misconfigured routes are logged and left undelivered for
        operator repair instead of silently changing the speaking identity.
        """

        channel = str(
            target.get("channel_type") or target.get("platform") or ""
        ).strip().lower()
        if channel not in {"slack", "telegram"}:
            return None
        try:
            resolution = self._resolve_agent_representation(agent_id)
            identity = ensure_json_object(resolution.get("identity"))
            if not identity:
                self._record_log(
                    "notifier.representation_unavailable",
                    layer="control_plane",
                    source="notifier",
                    subject_type=notification.subject_type,
                    subject_id=notification.subject_id,
                    detail={"notification_id": notification.id, "agent_id": agent_id},
                )
                return None
            external_id = str(target.get("external_id") or "").strip()
            if not external_id:
                self._record_log(
                    "notifier.channel_target_missing",
                    layer="control_plane",
                    source="notifier",
                    subject_type=notification.subject_type,
                    subject_id=notification.subject_id,
                    detail={"notification_id": notification.id, "channel": channel},
                )
                return None
            if channel == "slack":
                channel_id = external_id.rsplit("/", 1)[-1]
                external_id = (
                    channel_id
                    if channel_id.startswith(("channel:", "user:"))
                    else "channel:%s" % channel_id
                )
            text = "[%s] %s" % (
                notification.event_type,
                notification.body or notification.title,
            )
            delivery = self._enqueue_human_message(
                external_id,
                text,
                origin_agent_id=agent_id,
                identity_id=identity["id"],
                channel=channel,
                task_id=(
                    notification.subject_id
                    if notification.subject_type == "task"
                    else None
                ),
                idempotency_key="notifier:%s:%s:%s:%s"
                % (notification.id, identity["id"], channel, external_id),
                metadata={
                    "notification_id": notification.id,
                    "represented_agent_id": agent_id,
                    "public_identity_id": identity["id"],
                },
            )
            return str(delivery.id)
        except Exception as exc:  # noqa: BLE001 - preserve pending notification for repair.
            self._record_log(
                "notifier.openclaw_enqueue_failed",
                layer="control_plane",
                source="notifier",
                level="error",
                subject_type=notification.subject_type,
                subject_id=notification.subject_id,
                detail={
                    "notification_id": notification.id,
                    "agent_id": agent_id,
                    "channel": channel,
                    "error": str(exc),
                },
            )
            return None

    def _configured_targets(self, notification: OperatorNotification) -> List[JsonDict]:
        targets: List[JsonDict] = []
        for channel in self.list_channels(enabled=True):
            if not self._event_matches(channel.event_types, notification.event_type):
                continue
            if channel.channel_type not in notification.channels and "hermes" not in notification.channels:
                continue
            targets.extend(self._targets_for_channel(channel, notification))
        return self._dedupe_targets(targets)

    def _targets_for_channel(
        self,
        channel: NotifierChannel,
        notification: OperatorNotification,
    ) -> List[JsonDict]:
        target = dict(channel.target)
        target["channel_type"] = channel.channel_type
        target["notifier_channel_id"] = channel.id
        agent_id = str(target.get("agent_id") or "").strip()
        if agent_id:
            self._get_agent(agent_id)
            return [target]
        binding_id = str(target.get("platform_binding_id") or "").strip()
        if binding_id:
            binding = self._get_platform_binding(binding_id)
            target.setdefault("platform", binding.platform)
            target.setdefault("external_id", binding.external_id)
            return [
                {**target, "agent_id": agent.id}
                for agent in self._agents_for_persona_instance(
                    binding.persona_instance_id
                )
            ]
        persona_instance_id = str(
            target.get("persona_instance_id")
            # Accept the deprecated key for one release.
            or target.get("hermes_instance_id")
            or ""
        ).strip()
        if persona_instance_id:
            return [
                {**target, "agent_id": agent.id}
                for agent in self._agents_for_persona_instance(persona_instance_id)
            ]
        platform = target.get("platform") or (
            channel.channel_type if channel.channel_type in {"slack", "telegram"} else None
        )
        if platform:
            return self._platform_targets(str(platform), target)
        return []

    def _auto_persona_instance_targets(
        self, notification: OperatorNotification
    ) -> List[JsonDict]:
        metadata = ensure_json_object(notification.metadata)
        actor = str(metadata.get("actor") or "").strip()
        if actor:
            try:
                agent = self._get_agent(actor)
                if agent.persona_instance_id:
                    return self._platform_targets_for_persona_instance(
                        agent.persona_instance_id
                    )
            except NotFoundError:
                pass
        return self._platform_targets("slack", {}) + self._platform_targets("telegram", {})

    def _platform_targets(self, platform: str, base: JsonDict) -> List[JsonDict]:
        targets: List[JsonDict] = []
        for binding in self._list_platform_bindings():
            if binding.platform != platform:
                continue
            for agent in self._agents_for_persona_instance(
                binding.persona_instance_id
            ):
                targets.append(
                    {
                        **base,
                        "agent_id": agent.id,
                        "channel_type": platform,
                        "platform": binding.platform,
                        "platform_binding_id": binding.id,
                        "external_id": binding.external_id,
                        "display_name": binding.display_name,
                    }
                )
        return targets

    def _platform_targets_for_persona_instance(
        self, persona_instance_id: str
    ) -> List[JsonDict]:
        targets: List[JsonDict] = []
        for binding in self._list_platform_bindings():
            if binding.persona_instance_id != persona_instance_id:
                continue
            if binding.platform not in {"slack", "telegram"}:
                continue
            for agent in self._agents_for_persona_instance(persona_instance_id):
                targets.append(
                    {
                        "agent_id": agent.id,
                        "channel_type": binding.platform,
                        "platform": binding.platform,
                        "platform_binding_id": binding.id,
                        "external_id": binding.external_id,
                        "display_name": binding.display_name,
                    }
                )
        return targets

    def _agents_for_persona_instance(
        self, persona_instance_id: str
    ) -> List[Agent]:
        return [
            agent
            for agent in self._list_agents()
            if agent.persona_instance_id == persona_instance_id
        ]

    def _dedupe_targets(self, targets: List[JsonDict]) -> List[JsonDict]:
        seen = set()
        deduped: List[JsonDict] = []
        for target in targets:
            key = (
                str(target.get("agent_id") or ""),
                str(target.get("channel_type") or ""),
                str(target.get("platform_binding_id") or target.get("external_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(target)
        return deduped

    def _event_matches(self, event_patterns: List[str], event_type: str) -> bool:
        if not event_patterns:
            return event_type.startswith("task.")
        for pattern in event_patterns:
            if pattern == event_type:
                return True
            if pattern.endswith("*") and event_type.startswith(pattern[:-1]):
                return True
        return False

    def _mark_notification(self, notification_id: str, status: str) -> None:
        self.store.execute(
            """
            UPDATE operator_notifications
            SET status = ?, delivered_at = ?
            WHERE id = ?
            """,
            (status, utcnow(), notification_id),
        )

    def _from_row(self, row: Any) -> NotifierChannel:
        return NotifierChannel(
            id=row["id"],
            name=row["name"],
            channel_type=row["channel_type"],
            enabled=bool(row["enabled"]),
            event_types=json_loads(row["event_types"], []),
            target=json_loads(row["target"], {}),
            metadata=json_loads(row["metadata"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _notification_from_row(self, row: Any) -> OperatorNotification:
        return OperatorNotification(
            id=row["id"],
            event_type=row["event_type"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            title=row["title"],
            body=row["body"],
            channels=json_loads(row["channels"], []),
            metadata=json_loads(row["metadata"], {}),
            status=row["status"],
            created_at=row["created_at"],
            delivered_at=row["delivered_at"],
        )
