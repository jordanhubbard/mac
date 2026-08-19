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
from mac.notifier_service import NotifierService
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
# 1. Notification payloads are passed through, not rewritten
# ---------------------------------------------------------------------------


class TestNotificationPayloadsAreNotRewritten:
    """The notifier used to rename keys to survive a guard that is now gone.

    `_message_safe_value` renamed any key spelled like an execution verb --
    command -> command_text, and command_text -> command_text_text on
    collision -- purely so the payload would pass messaging_service's
    execution-key filter. That filter predates OpenShell and was removed with
    it: containment is the sandbox's job, not a key-spelling check.

    With the filter gone the renaming had no purpose left and was actively
    corrupting operator notifications, so it went too. This is the canary: if
    a helper like it comes back, something has re-added a guard, and the
    question to ask is what boundary it enforces that the sandbox does not.
    """

    def test_the_renaming_helper_is_gone(self):
        import mac.notifier_service as notifier

        assert not hasattr(notifier, "_message_safe_value")
        assert "FORBIDDEN_MESSAGE_KEYS" not in notifier.__dict__

    def test_messaging_service_no_longer_filters_keys_by_name(self):
        import mac.messaging_service as messaging

        assert not hasattr(messaging, "FORBIDDEN_MESSAGE_KEYS")


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

    def test_metadata_keys_reach_the_message_intact(self, cp, notifiers_and_sent):
        """A metadata key named like an execution verb is delivered unchanged.

        This test used to assert the opposite: that `command` was renamed to
        `command_text` before delivery. That redaction existed to satisfy
        messaging_service's execution-key filter, which predates OpenShell.
        Containment is enforced by the sandbox -- which commands an agent may
        run, which endpoints it may reach -- so filtering a key by its spelling
        bought nothing and silently corrupted operator notifications, which are
        the record a human reads to understand what happened.
        """
        notifiers, sent = notifiers_and_sent
        agent = _make_agent(cp)
        notifiers.configure_channel("ch", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"], metadata={"command": "rm -rf /"})
        notifiers.deliver_pending()
        assert len(sent) == 1

        def _find(obj, wanted):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key == wanted:
                        return value
                    found = _find(value, wanted)
                    if found is not None:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = _find(item, wanted)
                    if found is not None:
                        return found
            return None

        notification = sent[0].payload.get("notification", {})
        assert _find(notification, "command") == "rm -rf /"
        assert _find(notification, "command_text") is None

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


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11. OpenClaw outbox delivery path (_enqueue_openclaw_delivery)
# ---------------------------------------------------------------------------


def _register_infra(cp, hermes_id: str, platform: str = "slack", external_id: str = "channel:C001"):
    """Register the full stack (tenant, persona, hermes instance, binding) and return
    (tenant, instance, binding).  The hermes instance_id will equal *hermes_id*."""
    tenant = cp.register_tenant("tenant-%s" % hermes_id)
    persona = cp.register_persona(
        tenant.id,
        "Bot-%s" % hermes_id,
        "hermes://%s/soul" % hermes_id,
        "hermes://%s/mem" % hermes_id,
    )
    instance = cp.register_hermes_instance(tenant.id, hermes_id, persona_id=persona.id)
    binding = cp.register_platform_binding(
        tenant.id, instance.id, platform, external_id, display_name="bot-%s" % hermes_id
    )
    return tenant, instance, binding


def _make_openclaw_notifier(
    cp,
    *,
    sent_ids=None,
    logged=None,
    resolve_result=None,
    enqueue_raises=None,
):
    """Build a NotifierService with all three openclaw callbacks present."""
    sent_ids = [] if sent_ids is None else sent_ids
    logged = [] if logged is None else logged

    if resolve_result is None:
        resolve_result = {"identity": {"id": "identity_001", "name": "ops-bot"}}

    def _list_comm_ids(enabled=True):
        from mac.models import CommunicationIdentity
        return [
            CommunicationIdentity(
                id="identity_001",
                name="ops-bot",
                display_name="Ops Bot",
                description="",
                is_default=True,
                enabled=True,
                metadata={},
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        ]

    def _resolve(agent_id):
        return resolve_result

    def _enqueue(target, body, **kwargs):
        if enqueue_raises:
            raise enqueue_raises
        from mac.models import HumanMessageDelivery
        delivery = HumanMessageDelivery(
            id=new_id("delivery"),
            identity_id=kwargs.get("identity_id", "identity_001"),
            account_id=None,
            channel=kwargs.get("channel"),
            target=target,
            body=body,
            origin_agent_id=kwargs.get("origin_agent_id"),
            task_id=kwargs.get("task_id"),
            idempotency_key=kwargs.get("idempotency_key", ""),
            status="pending",
            attempt_count=0,
            max_attempts=3,
            delivery_agent_id=None,
            delivery_lease_id=None,
            leased_until=None,
            provider_message_id=None,
            last_error=None,
            metadata=kwargs.get("metadata", {}),
            created_at=utcnow(),
            updated_at=utcnow(),
            delivered_at=None,
        )
        sent_ids.append(delivery.id)
        return delivery

    def _capture_log(name, **kwargs):
        logged.append({"name": name, **kwargs})

    sent_msgs: List[AgentMessage] = []

    def _send_message(sender, recipient, mtype, payload):
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
        sent_msgs.append(msg)
        return msg

    from mac.notifier_service import NotifierService as NS
    return NS(
        cp.store,
        list_agents=cp.list_agents,
        get_agent=cp.get_agent,
        list_platform_bindings=cp.identity.list_platform_bindings,
        get_platform_binding=cp.identity.get_platform_binding,
        send_message=_send_message,
        record_log=_capture_log,
        enqueue_human_message=_enqueue,
        resolve_agent_representation=_resolve,
        list_communication_identities=_list_comm_ids,
    ), logged, sent_ids


class TestOpenClawOutboxDelivery:
    """Tests for _enqueue_openclaw_delivery and the openclaw branch in _deliver_notification."""

    def test_slack_channel_routed_via_openclaw(self, cp):
        """Slack-type channel target invokes _enqueue_human_message, not send_message."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        _, instance, binding = _register_infra(cp, "ocsl-1", "slack", "channel:C001")
        machine = cp.register_machine("host-ocsl-1")
        agent = cp.register_agent(machine.id, "ocsl-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-ch",
            "slack",
            target={"agent_id": agent.id, "external_id": binding.external_id},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert len(sent_ids) == 1

    def test_telegram_channel_routed_via_openclaw(self, cp):
        """Telegram-type channel target invokes the openclaw enqueue path."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        _, instance, binding = _register_infra(cp, "octg-1", "telegram", "tg_chat_123")
        machine = cp.register_machine("host-octg-1")
        agent = cp.register_agent(machine.id, "octg-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "tg-ch",
            "telegram",
            target={"agent_id": agent.id, "external_id": "tg_chat_123"},
        )
        _make_notification(cp, channels=["telegram"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert len(sent_ids) == 1

    def test_representation_unavailable_logs_and_skips(self, cp):
        """When resolve returns no identity, log representation_unavailable and skip."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(
            cp,
            resolve_result={"identity": {}},
        )
        _, instance, binding = _register_infra(cp, "ocru-1", "slack", "channel:C002")
        machine = cp.register_machine("host-ocru-1")
        agent = cp.register_agent(machine.id, "ocru-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-repr",
            "slack",
            target={"agent_id": agent.id, "external_id": binding.external_id},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1
        assert any(e["name"] == "notifier.representation_unavailable" for e in logged)

    def test_channel_target_missing_logs_when_no_external_id(self, cp):
        """When external_id is absent in target, log channel_target_missing and skip."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        _, instance, binding = _register_infra(cp, "octm-1", "slack", "channel:C003")
        machine = cp.register_machine("host-octm-1")
        agent = cp.register_agent(machine.id, "octm-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-noext",
            "slack",
            target={"agent_id": agent.id},  # no external_id
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1
        assert any(e["name"] == "notifier.channel_target_missing" for e in logged)

    def test_openclaw_enqueue_failed_logs_on_exception(self, cp):
        """When _enqueue_human_message raises, log openclaw_enqueue_failed."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(
            cp,
            enqueue_raises=RuntimeError("delivery backend down"),
        )
        _, instance, binding = _register_infra(cp, "ocef-1", "slack", "channel:C004")
        machine = cp.register_machine("host-ocef-1")
        agent = cp.register_agent(machine.id, "ocef-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-fail",
            "slack",
            target={"agent_id": agent.id, "external_id": binding.external_id},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["skipped"] == 1
        assert any(e["name"] == "notifier.openclaw_enqueue_failed" for e in logged)

    def test_enqueue_openclaw_non_slack_telegram_returns_none(self, cp):
        """_enqueue_openclaw_delivery returns None for unsupported channel types."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        notification = _make_notification(cp, channels=["hermes"])
        result = notifiers._enqueue_openclaw_delivery(
            notification,
            {"channel_type": "hermes", "external_id": "some-id"},
            "agent_001",
        )
        assert result is None

    def test_slack_external_id_plain_gets_channel_prefix(self, cp):
        """A raw Slack channel ID without a prefix gets 'channel:' prepended."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        _, instance, binding = _register_infra(cp, "ocpfx-1", "slack", "C005RAWID")
        machine = cp.register_machine("host-ocpfx-1")
        agent = cp.register_agent(machine.id, "ocpfx-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-raw",
            "slack",
            target={"agent_id": agent.id, "external_id": "C005RAWID"},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1

    def test_slack_external_id_with_user_prefix_preserved(self, cp):
        """Slack external_id with 'user:' prefix is passed through unchanged."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        _, instance, binding = _register_infra(cp, "ocupfx-1", "slack", "user:U006")
        machine = cp.register_machine("host-ocupfx-1")
        agent = cp.register_agent(machine.id, "ocupfx-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-user",
            "slack",
            target={"agent_id": agent.id, "external_id": "user:U006"},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1

    def test_openclaw_delivery_id_recorded_in_message_ids(self, cp):
        """The delivery id from enqueue_human_message is recorded in message_ids."""
        notifiers, logged, sent_ids = _make_openclaw_notifier(cp)
        _, instance, binding = _register_infra(cp, "ocid-1", "slack", "channel:C007")
        machine = cp.register_machine("host-ocid-1")
        agent = cp.register_agent(machine.id, "ocid-agent-1", hermes_instance_id=instance.id)
        notifiers.configure_channel(
            "slack-id",
            "slack",
            target={"agent_id": agent.id, "external_id": binding.external_id},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert len(sent_ids) == 1
        assert sent_ids[0] in result["results"][0]["message_ids"]


# ---------------------------------------------------------------------------
# 12. Concurrent-claim race in _claim_notification
# ---------------------------------------------------------------------------


class TestClaimNotificationRace:
    """Test the False-return branch when two callers race on the same notification."""

    def _make_plain_notifiers(self, cp):
        return NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=MagicMock(return_value=MagicMock()),
            record_log=MagicMock(),
        )

    def test_first_claim_returns_true(self, cp):
        """Claiming a fresh pending notification returns True."""
        notifiers = self._make_plain_notifiers(cp)
        notification = _make_notification(cp)
        assert notifiers._claim_notification(notification.id) is True

    def test_second_claim_returns_false_when_first_wins(self, cp):
        """A second immediate claim on an already-delivering notification returns False."""
        notifiers = self._make_plain_notifiers(cp)
        notification = _make_notification(cp)
        assert notifiers._claim_notification(notification.id) is True
        # Status is now 'delivering' and timestamp is not stale — claim fails.
        assert notifiers._claim_notification(notification.id) is False

    def test_claim_returns_false_for_nonexistent_id(self, cp):
        """Claiming a notification that does not exist returns False."""
        notifiers = self._make_plain_notifiers(cp)
        assert notifiers._claim_notification("notification_does_not_exist") is False

    def test_deliver_pending_skips_already_claimed_notification(self, cp):
        """When a notification has been pre-claimed, deliver_pending skips it."""
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
            return msg

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=MagicMock(),
        )
        agent = _make_agent(cp)
        notifiers.configure_channel("ch-race", "hermes", target={"agent_id": agent.id})
        notification = _make_notification(cp, channels=["hermes"])

        # Simulate: another worker already claimed this notification
        assert notifiers._claim_notification(notification.id) is True

        # deliver_pending should find rowcount=0 and skip without sending
        result = notifiers.deliver_pending()
        assert result["delivered"] == 0
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert len(sent) == 0


# ---------------------------------------------------------------------------
# 13. _targets_for_channel sub-path coverage
# ---------------------------------------------------------------------------


def _make_notifiers_plain(cp):
    """Return a NotifierService without openclaw extras (uses send_message path)."""
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
        return msg

    notifiers = NotifierService(
        cp.store,
        list_agents=cp.list_agents,
        get_agent=cp.get_agent,
        list_platform_bindings=cp.identity.list_platform_bindings,
        get_platform_binding=cp.identity.get_platform_binding,
        send_message=_mock_send,
        record_log=MagicMock(),
    )
    return notifiers, sent


def _setup_platform_infra(cp, label: str, platform: str = "slack"):
    """Register a full stack and return (tenant, instance, agent, binding)."""
    tenant = cp.register_tenant("t-%s" % label)
    persona = cp.register_persona(
        tenant.id, "Bot-%s" % label,
        "hermes://%s/soul" % label,
        "hermes://%s/mem" % label,
    )
    instance = cp.register_hermes_instance(tenant.id, "hinst-%s" % label, persona_id=persona.id)
    machine = cp.register_machine("host-%s" % label)
    agent = cp.register_agent(machine.id, "agent-%s" % label, hermes_instance_id=instance.id)
    binding = cp.register_platform_binding(
        tenant.id, instance.id, platform, "ext-%s" % label, display_name="bot-%s" % label
    )
    return tenant, instance, agent, binding


class TestTargetsForChannelSubPaths:
    """Cover platform_binding_id, hermes_instance_id, and implicit-platform branches."""

    def test_platform_binding_id_target_resolves_agents(self, cp):
        """Channel configured with platform_binding_id returns bound agents as targets."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "bid1", "slack")
        notifiers.configure_channel(
            "binding-ch",
            "slack",
            target={"platform_binding_id": binding.id},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_hermes_instance_id_target_resolves_agents(self, cp):
        """Channel configured with hermes_instance_id resolves all associated agents."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "hid1")
        notifiers.configure_channel(
            "hid-ch",
            "hermes",
            target={"hermes_instance_id": instance.id},
        )
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_slack_channel_type_implicit_platform_routing(self, cp):
        """Slack channel with no agent_id/binding uses channel_type as implicit platform."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "sl-implicit", "slack")
        notifiers.configure_channel(
            "slack-implicit",
            "slack",
            target={},
        )
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_telegram_channel_type_implicit_platform_routing(self, cp):
        """Telegram channel with no agent_id/binding uses channel_type as implicit platform."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "tg-implicit", "telegram")
        notifiers.configure_channel(
            "tg-implicit",
            "telegram",
            target={},
        )
        _make_notification(cp, channels=["telegram"])
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_hermes_channel_with_empty_target_returns_no_targets(self, cp):
        """A hermes channel with no routing info returns no configured targets."""
        notifiers, sent = _make_notifiers_plain(cp)
        notifiers.configure_channel("hermes-empty", "hermes", target={})
        _make_notification(cp, channels=["hermes"])
        result = notifiers.deliver_pending()
        # No configured targets and no platform bindings → auto-hermes falls back to empty
        assert result["skipped"] == 1
        assert len(sent) == 0

    def test_platform_binding_id_sets_platform_and_external_id(self, cp):
        """When routing via platform_binding_id, platform and external_id are propagated."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "bid2", "slack")
        notifiers.configure_channel(
            "binding-ch2",
            "slack",
            target={"platform_binding_id": binding.id},
        )
        _make_notification(cp, channels=["slack"])
        notifiers.deliver_pending()
        assert len(sent) == 1
        target_in_payload = sent[0].payload.get("target", {})
        assert target_in_payload.get("platform") == "slack"


# ---------------------------------------------------------------------------
# 14. Additional gap-closing tests
# ---------------------------------------------------------------------------


class TestDeliverPendingClaimRaceSkip:
    """Line 209: the continue branch when _claim_notification returns False inside deliver_pending."""

    def test_deliver_pending_skips_when_claim_returns_false(self, cp):
        """Patch _claim_notification to return False so the continue branch is exercised."""
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
            return msg

        notifiers = NotifierService(
            cp.store,
            list_agents=cp.list_agents,
            get_agent=cp.get_agent,
            list_platform_bindings=cp.identity.list_platform_bindings,
            get_platform_binding=cp.identity.get_platform_binding,
            send_message=_mock_send,
            record_log=MagicMock(),
        )
        agent = _make_agent(cp)
        notifiers.configure_channel("ch-claimrace", "hermes", target={"agent_id": agent.id})
        _make_notification(cp, channels=["hermes"])

        # Patch _claim_notification to always return False
        with patch.object(notifiers, "_claim_notification", return_value=False):
            result = notifiers.deliver_pending()

        assert result["delivered"] == 0
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert len(sent) == 0


class TestDeliverNotificationNoAgentIdSkip:
    """Line 300: the continue branch when a target has no agent_id."""

    def test_target_without_agent_id_is_skipped(self, cp):
        """A target dict with no agent_id is silently skipped in _deliver_notification."""
        logged: List[dict] = []
        notifiers, _, sent_ids = _make_openclaw_notifier(cp, logged=logged)

        notification = _make_notification(cp, channels=["hermes"])
        # Inject a target with no agent_id directly via _deliver_notification
        # We do this by configuring a channel, then monkeypatching _configured_targets.
        original_configured_targets = notifiers._configured_targets

        def _patched_targets(notif):
            # Return a target missing the agent_id field
            return [{"channel_type": "hermes", "no_agent_here": True}]

        with patch.object(notifiers, "_configured_targets", side_effect=_patched_targets):
            with patch.object(notifiers, "_auto_persona_instance_targets", return_value=[]):
                message_ids = notifiers._deliver_notification(notification)

        assert message_ids == []


class TestConfiguredTargetsChannelMismatch:
    """Line 435: the continue branch in _configured_targets when channel_type not in notification.channels."""

    def test_channel_type_mismatch_skips_configured_channel(self, cp):
        """A 'telegram' channel is skipped when the notification targets only 'slack'.

        The condition is:
            channel.channel_type not in notification.channels
            AND 'hermes' not in notification.channels
        So we need notification.channels=['slack'] and channel.channel_type='telegram'.
        """
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "chanmatch", "telegram")
        notifiers.configure_channel(
            "tg-mismatch",
            "telegram",
            target={"agent_id": agent.id, "external_id": "tg_ext_001"},
        )
        # Notification targets 'slack' only; telegram channel should be skipped (line 435 continue)
        _make_notification(cp, channels=["slack"])
        result = notifiers.deliver_pending()
        # Telegram channel skipped, auto-hermes not triggered (no 'hermes' in channels)
        # → no targets → skipped
        assert result["skipped"] == 1
        assert len(sent) == 0

class TestAutoHermesTargetsActorPath:
    """Lines 477-482, 489: _auto_persona_instance_targets actor and NotFoundError branches."""

    def test_auto_hermes_actor_with_hermes_id_routes_to_platform_targets(self, cp):
        """When notification.metadata.actor is set to an agent with a hermes_instance_id,
        _auto_persona_instance_targets returns the platform-specific targets for that instance."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "actorhid", "slack")

        # Use metadata actor pointing to the agent with a hermes instance
        _make_notification(
            cp,
            channels=["hermes"],
            metadata={"actor": agent.id},
        )
        result = notifiers.deliver_pending()
        # The auto-hermes path with actor → platform_targets_for_hermes → returns targets
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_auto_hermes_actor_notfound_falls_back_to_all_platforms(self, cp):
        """When the actor in metadata is not found, fall back to all platform bindings."""
        notifiers, sent = _make_notifiers_plain(cp)
        _, instance, agent, binding = _setup_platform_infra(cp, "actornf", "slack")

        _make_notification(
            cp,
            channels=["hermes"],
            metadata={"actor": "agent_does_not_exist"},
        )
        result = notifiers.deliver_pending()
        # Falls back to _platform_targets("slack") + _platform_targets("telegram")
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id

    def test_auto_hermes_actor_with_no_hermes_id_falls_back(self, cp):
        """When actor exists but has no hermes_instance_id, fall back to all platforms."""
        notifiers, sent = _make_notifiers_plain(cp)
        # Set up a slack binding so the fallback can find something
        _, instance, agent_with_binding, binding = _setup_platform_infra(cp, "actornhid", "slack")

        # Create a separate agent without a hermes_instance_id
        machine2 = cp.register_machine("host-nohid")
        agent_no_hid = cp.register_agent(machine2.id, "agent-nohid")

        _make_notification(
            cp,
            channels=["hermes"],
            metadata={"actor": agent_no_hid.id},
        )
        result = notifiers.deliver_pending()
        # agent_no_hid has no hermes_instance_id → falls through to all-platform fallback
        assert result["delivered"] == 1


class TestPlatformTargetsForHermes:
    """Lines 505-522: _platform_targets_for_persona_instance function."""

    def test_platform_targets_for_persona_instance_returns_slack_and_telegram(self, cp):
        """_platform_targets_for_persona_instance returns entries for both slack and telegram bindings."""
        notifiers, sent = _make_notifiers_plain(cp)
        tenant = cp.register_tenant("t-ptfh")
        persona = cp.register_persona(
            tenant.id, "BotPTFH", "hermes://ptfh/soul", "hermes://ptfh/mem"
        )
        instance = cp.register_hermes_instance(tenant.id, "hinst-ptfh", persona_id=persona.id)
        machine = cp.register_machine("host-ptfh")
        agent = cp.register_agent(machine.id, "agent-ptfh", hermes_instance_id=instance.id)

        # Register both slack and telegram bindings for the same hermes instance
        cp.register_platform_binding(
            tenant.id, instance.id, "slack", "channel:PTFHSLACK", display_name="slack-ptfh"
        )
        cp.register_platform_binding(
            tenant.id, instance.id, "telegram", "tg_ptfh_chat", display_name="tg-ptfh"
        )

        targets = notifiers._platform_targets_for_persona_instance(instance.id)
        assert len(targets) == 2
        platforms = {t["platform"] for t in targets}
        assert platforms == {"slack", "telegram"}

    def test_platform_targets_for_persona_instance_excludes_hermes_platform(self, cp):
        """_platform_targets_for_persona_instance only returns slack/telegram, not hermes bindings."""
        notifiers, sent = _make_notifiers_plain(cp)
        # Nothing registered → empty
        targets = notifiers._platform_targets_for_persona_instance("nonexistent-hermes-instance")
        assert targets == []

    def test_auto_hermes_actor_uses_platform_targets_for_persona_instance_path(self, cp):
        """When actor has hermes_instance_id, _platform_targets_for_persona_instance is invoked."""
        notifiers, sent = _make_notifiers_plain(cp)
        tenant = cp.register_tenant("t-ptfh2")
        persona = cp.register_persona(
            tenant.id, "BotPTFH2", "hermes://ptfh2/soul", "hermes://ptfh2/mem"
        )
        instance = cp.register_hermes_instance(tenant.id, "hinst-ptfh2", persona_id=persona.id)
        machine = cp.register_machine("host-ptfh2")
        agent = cp.register_agent(machine.id, "agent-ptfh2", hermes_instance_id=instance.id)
        cp.register_platform_binding(
            tenant.id, instance.id, "slack", "channel:PTFH2SL", display_name="sl-ptfh2"
        )

        # Notification with actor set → _auto_persona_instance_targets → _platform_targets_for_persona_instance
        _make_notification(cp, channels=["hermes"], metadata={"actor": agent.id})
        result = notifiers.deliver_pending()
        assert result["delivered"] == 1
        assert sent[0].recipient_agent_id == agent.id


class TestPlatformTargetsForHermesFilters:
    """Cover the filtering branches inside _platform_targets_for_persona_instance (lines 508, 510)."""

    def test_bindings_from_other_hermes_instance_are_excluded(self, cp):
        """Bindings whose hermes_instance_id does not match are skipped (line 508 branch)."""
        notifiers, sent = _make_notifiers_plain(cp)
        # Register two separate hermes instances
        tenant = cp.register_tenant("t-ptfhfilt")
        persona = cp.register_persona(
            tenant.id, "BotFilt", "hermes://filtA/soul", "hermes://filtA/mem"
        )
        instanceA = cp.register_hermes_instance(tenant.id, "hinst-filtA", persona_id=persona.id)
        personaB = cp.register_persona(
            tenant.id, "BotFiltB", "hermes://filtB/soul", "hermes://filtB/mem"
        )
        instanceB = cp.register_hermes_instance(tenant.id, "hinst-filtB", persona_id=personaB.id)
        machine = cp.register_machine("host-filtA")
        agentA = cp.register_agent(machine.id, "agent-filtA", hermes_instance_id=instanceA.id)

        # Register a slack binding on instanceA and instanceB
        cp.register_platform_binding(
            tenant.id, instanceA.id, "slack", "channel:FILTA", display_name="slack-filtA"
        )
        cp.register_platform_binding(
            tenant.id, instanceB.id, "slack", "channel:FILTB", display_name="slack-filtB"
        )

        # Query _platform_targets_for_persona_instance for instanceA — should only return instanceA binding
        targets = notifiers._platform_targets_for_persona_instance(instanceA.id)
        assert len(targets) == 1
        assert targets[0]["platform_binding_id"] is not None
        # Ensure binding for instanceB is excluded
        external_ids = {t["external_id"] for t in targets}
        assert "channel:FILTA" in external_ids
        assert "channel:FILTB" not in external_ids

    def test_non_slack_telegram_bindings_are_excluded(self, cp):
        """Bindings with platform other than slack/telegram are skipped (line 510 branch)."""
        notifiers, sent = _make_notifiers_plain(cp)
        tenant = cp.register_tenant("t-ptfhother")
        persona = cp.register_persona(
            tenant.id, "BotOther", "hermes://other/soul", "hermes://other/mem"
        )
        instance = cp.register_hermes_instance(tenant.id, "hinst-other", persona_id=persona.id)
        machine = cp.register_machine("host-other")
        agent = cp.register_agent(machine.id, "agent-other", hermes_instance_id=instance.id)

        # Register both a slack binding and a "discord" binding (not in {"slack","telegram"})
        cp.register_platform_binding(
            tenant.id, instance.id, "slack", "channel:OTHER", display_name="slack-other"
        )
        cp.register_platform_binding(
            tenant.id, instance.id, "discord", "discord_guild_123", display_name="discord-other"
        )

        targets = notifiers._platform_targets_for_persona_instance(instance.id)
        # Only the slack binding should be returned; the discord binding triggers line 510 continue
        assert len(targets) == 1
        assert targets[0]["platform"] == "slack"
