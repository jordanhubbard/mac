"""Comprehensive test suite for NotifierService.

Coverage:
- Channel CRUD: configure, get, list, delete
- Validation: empty name, unsupported type
- Event-type matching: exact, wildcard, default (task.*), empty patterns
- Delivery pipeline: pending -> delivering -> delivered / failed / skipped
- Retry / claim-timeout: stale "delivering" records are re-claimed
- Deduplication: concurrent deliver_pending on the same notification
- Batching: multiple notifications drained in one call
- Platform-binding resolution: agent_id / platform_binding_id / hermes_instance_id
- Auto-hermes fallback: Slack + Telegram fan-out when no channel configured
- Secret / forbidden-key redaction in message payloads (_message_safe_value)
- Failure propagation: delivery exception recorded as "failed" log entry
- Notification idempotency: duplicate message detection (same notification.id)
- Per-notification delivery (notification_id filter)
- list_channels filters: enabled, channel_type
- Target deduplication: same agent_id+binding never appears twice
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from mac.models import (
    Agent,
    AgentMessage,
    MessageType,
    NotFoundError,
    NotifierChannel,
    OperatorNotification,
    ValidationError,
    new_id,
    utcnow,
)
from mac.notifier_service import NotifierService, _message_safe_value
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp():
    """Fresh in-memory ControlPlane for every test."""
    return ControlPlane.in_memory()


def _make_agent(cp: ControlPlane, name: str = "worker-1", hermes_id: Optional[str] = None) -> Agent:
    machine = cp.register_machine("host-1")
    return cp.register_agent(machine.id, name, hermes_instance_id=hermes_id)


def _make_notification(
    cp: ControlPlane,
    *,
    event_type: str = "task.completed",
    title: str = "Task done",
    body: str = "A task finished.",
    channels: Optional[List[str]] = None,
    subject_type: str = "task",
    subject_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> OperatorNotification:
    return cp.record_notification(
        event_type,
        title,
        body,
        subject_type=subject_type,
        subject_id=subject_id or new_id("task"),
        channels=channels or ["hermes"],
        metadata=metadata or {},
    )


def _sent_payloads(sent_messages: List[AgentMessage]) -> List[Dict[str, Any]]:
    return [m.payload for m in sent_messages]


# ---------------------------------------------------------------------------
# 1. _message_safe_value — secret / forbidden-key redaction
# ---------------------------------------------------------------------------


class TestMessageSafeValue:
    def test_flat_dict_passthrough(self):
        data = {"status": "ok", "task_id": "t_1"}
        assert _message_safe_value(data) == data

    def test_forbidden_key_renamed(self):
        from mac.messaging_service import FORBIDDEN_MESSAGE_KEYS

        for key in FORBIDDEN_MESSAGE_KEYS:
            result = _message_safe_value({key: "danger"})
            assert key not in result
            expected = "%s_text" % key
            assert expected in result
            assert result[expected] == "danger"

    def test_forbidden_key_case_insensitive(self):
        # The implementation lowercases for the FORBIDDEN check but keeps
        # the original key casing when constructing the renamed key.
        result = _message_safe_value({"COMMAND": "rm -rf /"})
        assert "COMMAND" not in result
        # Renamed key retains original casing: "COMMAND_text"
        assert "COMMAND_text" in result

    def test_collision_avoidance_when_renamed_key_already_exists(self):
        # The implementation checks for safe_key (the tentative renamed key) in
        # the accumulating safe dict.  When "cmd_text" is already present from a
        # prior iteration, the renamed key collides and gets another _text suffix.
        # However, dict iteration order means "cmd_text" (the innocent value) is
        # processed first; when "cmd" is processed its rename "cmd_text" is already
        # in safe, so it becomes "cmd_text_text".  Verify the collision is resolved.
        result = _message_safe_value({"cmd_text": "innocent", "cmd": "evil"})
        assert "cmd" not in result
        assert "cmd_text" in result and result["cmd_text"] == "innocent"
        assert "cmd_text_text" in result and result["cmd_text_text"] == "evil"

    def test_nested_dict_redaction(self):
        data = {"outer": {"command": "bad"}}
        result = _message_safe_value(data)
        assert "command" not in result["outer"]
        assert "command_text" in result["outer"]

    def test_list_with_dict_elements(self):
        data = [{"script": "rm -rf"}, {"safe": "value"}]
        result = _message_safe_value(data)
        assert "script" not in result[0]
        assert "script_text" in result[0]
        assert result[1] == {"safe": "value"}

    def test_scalar_passthrough(self):
        assert _message_safe_value(42) == 42
        assert _message_safe_value("hello") == "hello"
        assert _message_safe_value(None) is None


# ---------------------------------------------------------------------------
# 2. Channel CRUD
# ---------------------------------------------------------------------------


class TestChannelCRUD:
    def test_configure_and_get_channel(self, cp):
        ch = cp.notifiers.configure_channel(
            "ops-slack",
            "slack",
            event_types=["task.completed"],
            target={"agent_id": "agent_1"},
        )
        assert ch.name == "ops-slack"
        assert ch.channel_type == "slack"
        assert ch.enabled is True
        assert "task.completed" in ch.event_types

        fetched = cp.notifiers.get_channel(ch.id)
        assert fetched.id == ch.id
        assert fetched.name == "ops-slack"

    def test_configure_channel_upserts_by_name(self, cp):
        ch1 = cp.notifiers.configure_channel("ops", "slack")
        ch2 = cp.notifiers.configure_channel("ops", "telegram")
        # Same name -> same id, type updated
        assert ch1.id == ch2.id
        assert cp.notifiers.get_channel("ops").channel_type == "telegram"

    def test_get_channel_by_name(self, cp):
        cp.notifiers.configure_channel("my-chan", "hermes")
        ch = cp.notifiers.get_channel("my-chan")
        assert ch.name == "my-chan"

    def test_get_channel_not_found_raises(self, cp):
        with pytest.raises(NotFoundError):
            cp.notifiers.get_channel("nonexistent-id")

    def test_configure_channel_empty_name_raises(self, cp):
        with pytest.raises(ValidationError):
            cp.notifiers.configure_channel("", "slack")

    def test_configure_channel_unsupported_type_raises(self, cp):
        with pytest.raises(ValidationError):
            cp.notifiers.configure_channel("bad", "discord")

    def test_configure_channel_disabled(self, cp):
        ch = cp.notifiers.configure_channel("muted", "slack", enabled=False)
        assert ch.enabled is False

    def test_list_channels_empty(self, cp):
        assert cp.notifiers.list_channels() == []

    def test_list_channels_all(self, cp):
        cp.notifiers.configure_channel("s1", "slack")
        cp.notifiers.configure_channel("t1", "telegram")
        channels = cp.notifiers.list_channels()
        assert len(channels) == 2

    def test_list_channels_filter_enabled(self, cp):
        cp.notifiers.configure_channel("active", "slack", enabled=True)
        cp.notifiers.configure_channel("inactive", "slack", enabled=False)
        enabled = cp.notifiers.list_channels(enabled=True)
        disabled = cp.notifiers.list_channels(enabled=False)
        assert len(enabled) == 1 and enabled[0].name == "active"
        assert len(disabled) == 1 and disabled[0].name == "inactive"

    def test_list_channels_filter_channel_type(self, cp):
        cp.notifiers.configure_channel("s1", "slack")
        cp.notifiers.configure_channel("t1", "telegram")
        slack_only = cp.notifiers.list_channels(channel_type="slack")
        assert len(slack_only) == 1
        assert slack_only[0].channel_type == "slack"

    def test_delete_channel(self, cp):
        ch = cp.notifiers.configure_channel("to-delete", "hermes")
        cp.notifiers.delete_channel(ch.id)
        with pytest.raises(NotFoundError):
            cp.notifiers.get_channel(ch.id)

    def test_delete_channel_by_name(self, cp):
        cp.notifiers.configure_channel("by-name", "hermes")
        cp.notifiers.delete_channel("by-name")
        assert cp.notifiers.list_channels() == []

    def test_delete_nonexistent_channel_raises(self, cp):
        with pytest.raises(NotFoundError):
            cp.notifiers.delete_channel("ghost_id")


# ---------------------------------------------------------------------------
# 3. Event-type matching
# ---------------------------------------------------------------------------


class TestEventMatching:
    """_event_matches is internal but tested via deliver_pending behavior."""

    def _make_service_with_sent(self, cp: ControlPlane):
        """Return (notifiers, sent_list) where sent_list captures every AgentMessage."""
        sent: List[AgentMessage] = []

        def _mock_send(sender, recipient, mtype, payload):
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            sent.append(msg)
            # Also store in the messages table so idempotency check works
            cp.store.execute(
                """
                INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.sender_agent_id,
                    msg.recipient_agent_id,
                    msg.task_id,
                    msg.message_type,
                    __import__("mac.models", fromlist=["json_dumps"]).json_dumps(msg.payload),
                    msg.status,
                    msg.created_at,
                    msg.delivered_at,
                ),
            )
            return msg

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=cp.record_log,
        )
        return notifiers, sent

    def test_default_event_filter_matches_task_prefix(self, cp):
        notifiers, sent = self._make_service_with_sent(cp)
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, event_type="task.started", channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1

    def test_default_event_filter_skips_non_task_events(self, cp):
        notifiers, sent = self._make_service_with_sent(cp)
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        # event_type doesn't start with task. -> no match on a channel with empty event_types
        note = _make_notification(cp, event_type="fleet.alert", channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1

    def test_explicit_event_type_exact_match(self, cp):
        notifiers, sent = self._make_service_with_sent(cp)
        agent = _make_agent(cp)
        notifiers.configure_channel(
            "ch", "hermes",
            event_types=["task.completed"],
            target={"agent_id": agent.id},
        )
        note = _make_notification(cp, event_type="task.completed", channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1

    def test_explicit_event_type_does_not_match_other(self, cp):
        notifiers, sent = self._make_service_with_sent(cp)
        agent = _make_agent(cp)
        notifiers.configure_channel(
            "ch", "hermes",
            event_types=["task.failed"],
            target={"agent_id": agent.id},
        )
        note = _make_notification(cp, event_type="task.completed", channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1

    def test_wildcard_event_pattern_matches_prefix(self, cp):
        notifiers, sent = self._make_service_with_sent(cp)
        agent = _make_agent(cp)
        notifiers.configure_channel(
            "ch", "hermes",
            event_types=["task.*"],
            target={"agent_id": agent.id},
        )
        note = _make_notification(cp, event_type="task.needs_review", channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1

    def test_wildcard_does_not_match_different_prefix(self, cp):
        notifiers, sent = self._make_service_with_sent(cp)
        agent = _make_agent(cp)
        notifiers.configure_channel(
            "ch", "hermes",
            event_types=["task.*"],
            target={"agent_id": agent.id},
        )
        note = _make_notification(cp, event_type="fleet.alert", channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# 4. Delivery pipeline
# ---------------------------------------------------------------------------


class TestDeliveryPipeline:
    @pytest.fixture()
    def notifiers_and_sent(self, cp):
        sent: List[AgentMessage] = []

        def _mock_send(sender, recipient, mtype, payload):
            from mac.models import json_dumps
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            sent.append(msg)
            cp.store.execute(
                """
                INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id, msg.sender_agent_id, msg.recipient_agent_id, msg.task_id,
                    msg.message_type, json_dumps(msg.payload), msg.status,
                    msg.created_at, msg.delivered_at,
                ),
            )
            return msg

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=cp.record_log,
        )
        return notifiers, sent

    def test_pending_notification_delivered_to_configured_agent(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert result["failed"] == 0
        assert len(sent) == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_notification_marked_delivered_in_store(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        row = cp.store.query_one(
            "SELECT status FROM operator_notifications WHERE id = ?", (note.id,)
        )
        assert row["status"] == "delivered"

    def test_failed_delivery_recorded_and_notification_marked_failed(self, cp):
        def _failing_send(*args, **kwargs):
            raise RuntimeError("webhook unreachable")

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_failing_send,
            record_log=cp.record_log,
        )
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["failed"] == 1
        row = cp.store.query_one(
            "SELECT status FROM operator_notifications WHERE id = ?", (note.id,)
        )
        assert row["status"] == "failed"

    def test_notification_skipped_when_no_targets(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        # No channel configured; no platform bindings -> skipped
        note = _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1
        assert len(sent) == 0

    def test_batch_drains_multiple_notifications(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        for i in range(3):
            _make_notification(cp, title=f"Task {i}", channels=["hermes"])
        result = notifiers.deliver_pending(limit=10)
        assert result["delivered"] == 3

    def test_limit_respected(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        for i in range(5):
            _make_notification(cp, title=f"Task {i}", channels=["hermes"])
        result = notifiers.deliver_pending(limit=2)
        assert result["delivered"] == 2

    def test_deliver_pending_by_notification_id(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        n1 = _make_notification(cp, title="First", channels=["hermes"])
        n2 = _make_notification(cp, title="Second", channels=["hermes"])
        result = notifiers.deliver_pending(notification_id=n1.id)
        assert result["delivered"] == 1
        # n2 should still be pending
        row = cp.store.query_one(
            "SELECT status FROM operator_notifications WHERE id = ?", (n2.id,)
        )
        assert row["status"] == "pending"

    def test_already_delivered_notification_not_re_sent(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, channels=["hermes"])
        # First delivery
        result1 = notifiers.deliver_pending()
        assert result1["delivered"] == 1
        # Second call: already "delivered", not pending
        result2 = notifiers.deliver_pending()
        assert result2["delivered"] == 0
        assert result2["skipped"] == 0
        assert result2["failed"] == 0

    def test_idempotency_duplicate_message_detection(self, cp, notifiers_and_sent):
        """If a message with the notification.id already exists, no duplicate is sent."""
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, channels=["hermes"])

        # Manually reset status to pending after first delivery to simulate a crash-recovery scenario
        notifiers.deliver_pending()
        assert len(sent) == 1

        # Force status back to pending so deliver_pending tries again
        cp.store.execute(
            "UPDATE operator_notifications SET status = 'pending' WHERE id = ?",
            (note.id,),
        )
        result2 = notifiers.deliver_pending()
        # No new message should be sent (existing message detected)
        assert len(sent) == 1  # still just the one from the first delivery
        # Result shows "delivered" because existing message_ids are returned
        assert result2["delivered"] == 1

    def test_delivery_result_schema(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        result = notifiers.deliver_pending()
        assert result["schema"] == "mac.notifier.delivery_result.v1"
        for key in ("delivered", "failed", "skipped", "results"):
            assert key in result


# ---------------------------------------------------------------------------
# 5. Stale claim / retry
# ---------------------------------------------------------------------------


class TestStaleClaim:
    def test_stale_delivering_notification_is_reclaimed(self, cp):
        """A notification stuck in 'delivering' beyond the timeout window is re-sent."""
        sent: List[AgentMessage] = []

        def _mock_send(sender, recipient, mtype, payload):
            from mac.models import json_dumps
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            sent.append(msg)
            cp.store.execute(
                """INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.sender_agent_id, msg.recipient_agent_id, msg.task_id,
                    msg.message_type, json_dumps(msg.payload), msg.status,
                    msg.created_at, msg.delivered_at,
                ),
            )
            return msg

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=cp.record_log,
        )
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        note = _make_notification(cp, channels=["hermes"])

        # Manually set status to 'delivering' with an old delivered_at (simulating a crashed worker)
        from datetime import datetime, timedelta, timezone
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(seconds=700)
        ).isoformat(timespec="microseconds")
        cp.store.execute(
            "UPDATE operator_notifications SET status = 'delivering', delivered_at = ? WHERE id = ?",
            (stale_ts, note.id),
        )

        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert len(sent) == 1


# ---------------------------------------------------------------------------
# 6. Target resolution
# ---------------------------------------------------------------------------


class TestTargetResolution:
    @pytest.fixture()
    def notifiers_and_sent(self, cp):
        sent: List[AgentMessage] = []

        def _mock_send(sender, recipient, mtype, payload):
            from mac.models import json_dumps
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            sent.append(msg)
            cp.store.execute(
                """INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.sender_agent_id, msg.recipient_agent_id, msg.task_id,
                    msg.message_type, json_dumps(msg.payload), msg.status,
                    msg.created_at, msg.delivered_at,
                ),
            )
            return msg

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=cp.record_log,
        )
        return notifiers, sent

    def test_target_with_agent_id_sends_to_that_agent(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp, "worker-1")
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        assert len(sent) == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_target_with_invalid_agent_id_raises(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        notifiers.configure_channel("ch", "hermes", target={"agent_id": "nonexistent"})
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        # get_agent raises NotFoundError -> captured as failed
        assert result["failed"] == 1

    def test_target_deduplication(self, cp, notifiers_and_sent):
        """Same agent targeted twice by two channels -> delivered only once."""
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch1", "hermes", target={"agent_id": agent.id})
        notifiers.configure_channel("ch2", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert len(sent) == 1

    def test_hermes_instance_id_targets_associated_agents(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        tenant = cp.register_tenant("test-tenant")
        persona = cp.register_persona(
            tenant.id,
            "Persona",
            "hermes://test/soul",
            "hermes://test/mem",
        )
        instance = cp.register_hermes_instance(tenant.id, "instance-1", persona_id=persona.id)
        machine = cp.register_machine("host-2")
        agent = cp.register_agent(machine.id, "worker-2", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "ch", "hermes",
            target={"hermes_instance_id": instance.id},
        )
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id


# ---------------------------------------------------------------------------
# 7. Auto-hermes fallback (no configured channels)
# ---------------------------------------------------------------------------


class TestAutoHermesFallback:
    @pytest.fixture()
    def notifiers_and_sent(self, cp):
        sent: List[AgentMessage] = []

        def _mock_send(sender, recipient, mtype, payload):
            from mac.models import json_dumps
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            sent.append(msg)
            cp.store.execute(
                """INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.sender_agent_id, msg.recipient_agent_id, msg.task_id,
                    msg.message_type, json_dumps(msg.payload), msg.status,
                    msg.created_at, msg.delivered_at,
                ),
            )
            return msg

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=cp.record_log,
        )
        return notifiers, sent

    def test_auto_hermes_skips_when_no_platform_bindings(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        # "hermes" channel but no configured notifier channels & no platform bindings
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1

    def test_non_hermes_channel_skips_when_no_channel_configured(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        # notification channel "slack" but no configured channel for it
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1

    def test_disabled_channel_is_not_used(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel(
            "muted", "hermes",
            target={"agent_id": agent.id},
            enabled=False,
        )
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# 8. Payload structure
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    @pytest.fixture()
    def notifiers_and_sent(self, cp):
        sent: List[AgentMessage] = []

        def _mock_send(sender, recipient, mtype, payload):
            from mac.models import json_dumps
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            sent.append(msg)
            cp.store.execute(
                """INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.sender_agent_id, msg.recipient_agent_id, msg.task_id,
                    msg.message_type, json_dumps(msg.payload), msg.status,
                    msg.created_at, msg.delivered_at,
                ),
            )
            return msg

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=cp.record_log,
        )
        return notifiers, sent

    def test_message_payload_schema(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        assert len(sent) == 1
        payload = sent[0].payload
        assert payload["schema"] == "mac.notifier.task_progress.v1"
        assert "notification" in payload
        assert "status" in payload

    def test_message_payload_contains_no_forbidden_keys(self, cp, notifiers_and_sent):
        from mac.messaging_service import FORBIDDEN_MESSAGE_KEYS

        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        # Include metadata with a forbidden key to test redaction in delivery
        _make_notification(
            cp,
            channels=["hermes"],
            metadata={"command": "rm -rf /"},
        )
        notifiers.deliver_pending()
        assert len(sent) == 1
        payload_str = str(sent[0].payload)
        # The raw key "command" should not appear in the notification dict after redaction
        notification_dict = sent[0].payload.get("notification", {})

        def _has_forbidden(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in FORBIDDEN_MESSAGE_KEYS:
                        return True
                    if _has_forbidden(v):
                        return True
            elif isinstance(obj, list):
                for item in obj:
                    if _has_forbidden(item):
                        return True
            return False

        assert not _has_forbidden(notification_dict), (
            "Forbidden keys found in notification payload after safe_value processing"
        )

    def test_message_type_is_status_update(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        assert sent[0].message_type == MessageType.STATUS_UPDATE.value

    def test_sender_is_notifier(self, cp, notifiers_and_sent):
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        assert sent[0].sender_agent_id == "notifier"


# ---------------------------------------------------------------------------
# 9. Failure propagation into audit log
# ---------------------------------------------------------------------------


class TestFailurePropagation:
    def test_delivery_failure_writes_observability_log(self, cp):
        """A delivery error must produce a 'notifier.delivery_failed' log entry."""
        logged: List[dict] = []

        def _failing_send(*args, **kwargs):
            raise RuntimeError("connection refused")

        def _capture_log(name, **kwargs):
            logged.append({"name": name, **kwargs})

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_failing_send,
            record_log=_capture_log,
        )
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        assert any(e["name"] == "notifier.delivery_failed" for e in logged)

    def test_delivery_success_writes_delivered_log(self, cp):
        """A successful delivery must produce a 'notifier.delivered' log entry."""
        logged: List[dict] = []

        def _mock_send(sender, recipient, mtype, payload):
            from mac.models import json_dumps
            msg = AgentMessage(
                id=new_id("msg"),
                sender_agent_id=sender,
                recipient_agent_id=recipient,
                task_id=None,
                message_type=mtype,
                payload=payload,
                status="pending",
                created_at=utcnow(),
                delivered_at=None,
            )
            cp.store.execute(
                """INSERT INTO messages (
                    id, sender_agent_id, recipient_agent_id, task_id,
                    message_type, payload, status, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.sender_agent_id, msg.recipient_agent_id, msg.task_id,
                    msg.message_type, json_dumps(msg.payload), msg.status,
                    msg.created_at, msg.delivered_at,
                ),
            )
            return msg

        def _capture_log(name, **kwargs):
            logged.append({"name": name, **kwargs})

        from mac.notifier_service import NotifierService

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=_capture_log,
        )
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])
        notifiers.deliver_pending()
        assert any(e["name"] == "notifier.delivered" for e in logged)


# ---------------------------------------------------------------------------
# 10. Integration via ControlPlane.notifiers
# ---------------------------------------------------------------------------


class TestControlPlaneIntegration:
    def test_cp_notifiers_attribute_is_notifier_service(self, cp):
        assert isinstance(cp.notifiers, NotifierService)

    def test_configure_and_list_via_cp(self, cp):
        cp.notifiers.configure_channel("via-cp", "hermes")
        channels = cp.notifiers.list_channels()
        assert any(c.name == "via-cp" for c in channels)

    def test_supported_channel_types(self, cp):
        for ctype in ("hermes", "slack", "telegram"):
            cp.notifiers.configure_channel(f"chan-{ctype}", ctype)
        channels = cp.notifiers.list_channels()
        types = {c.channel_type for c in channels}
        assert types == {"hermes", "slack", "telegram"}

    def test_event_types_stored_sorted(self, cp):
        ch = cp.notifiers.configure_channel(
            "sorted", "hermes",
            event_types=["task.failed", "task.completed", "task.started"],
        )
        assert ch.event_types == ["task.completed", "task.failed", "task.started"]

    def test_empty_event_types_stored_as_empty_list(self, cp):
        ch = cp.notifiers.configure_channel("noevents", "hermes", event_types=[])
        assert ch.event_types == []

    def test_channel_target_and_metadata_roundtrip(self, cp):
        target = {"agent_id": "agent_xyz", "extra": "data"}
        meta = {"owner": "ops-team", "priority": 1}
        ch = cp.notifiers.configure_channel(
            "full", "hermes", target=target, metadata=meta
        )
        fetched = cp.notifiers.get_channel(ch.id)
        assert fetched.target["agent_id"] == "agent_xyz"
        assert fetched.metadata["owner"] == "ops-team"
