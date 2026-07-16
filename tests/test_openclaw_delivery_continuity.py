"""Tests for src/mac/openclaw_delivery_continuity.py.

Covers:
- PendingDelivery / DeliveryContinuityResult / DeliveryContinuityQueue helpers
- enqueue(): deliveries for a device inside its scope-approval window are HELD
  (not delivered, not failed), even when an approval predicate is injected
- to_snapshot()/from_snapshot(): round-trips held state using the
  mac.openclaw_delivery_continuity.v1 schema constant, proving continuity
  across a simulated gateway restart (queue -> snapshot -> from_snapshot)
- drain_for_device(): still-in-window devices stay held; once approval is
  granted, held items re-deliver in FIFO order via an injected deliver_fn,
  are marked delivered, and other devices' items stay held
- drain_for_device(): a deliver_fn returning failure marks that item failed
  and is reflected in the result / ok property
- DeliveryContinuityResult.ok property semantics
- ValueError paths: empty delivery_id / device_id, unknown status on restore,
  wrong snapshot schema

All deliveries are simulated through injected fakes; no network is used.
"""

from __future__ import annotations

import pytest

from mac.openclaw_delivery_continuity import (
    DELIVERY_CONTINUITY_SCHEMA,
    DeliveryContinuityQueue,
    DeliveryContinuityResult,
    PendingDelivery,
    approval_from_set,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _RecordingDeliverFn:
    """A fake deliver_fn that records delivery order and returns a fixed result."""

    def __init__(self, *, succeed: bool = True) -> None:
        self._succeed = succeed
        self.calls: list[str] = []

    def __call__(self, item: PendingDelivery) -> bool:
        self.calls.append(item.delivery_id)
        return self._succeed


def _fail_for(*failing_ids: str):
    """Build a deliver_fn that fails for the given delivery ids, succeeds otherwise."""
    failing = set(failing_ids)
    calls: list[str] = []

    def _deliver(item: PendingDelivery) -> bool:
        calls.append(item.delivery_id)
        return item.delivery_id not in failing

    _deliver.calls = calls  # type: ignore[attr-defined]
    return _deliver


def _never_approved(_device_id: str) -> bool:
    return False


# ---------------------------------------------------------------------------
# PendingDelivery data-class + validation
# ---------------------------------------------------------------------------


def test_pending_delivery_defaults_to_held() -> None:
    item = PendingDelivery(delivery_id="d1", device_id="dev1", channel="push", payload={})
    assert item.status == "held"
    assert item.attempts == 0


def test_pending_delivery_empty_delivery_id_raises() -> None:
    with pytest.raises(ValueError):
        PendingDelivery(delivery_id="  ", device_id="dev1", channel="push", payload={})


def test_pending_delivery_empty_device_id_raises() -> None:
    with pytest.raises(ValueError):
        PendingDelivery(delivery_id="d1", device_id="", channel="push", payload={})


# ---------------------------------------------------------------------------
# DeliveryContinuityResult.ok semantics
# ---------------------------------------------------------------------------


def test_result_ok_true_when_no_failures() -> None:
    result = DeliveryContinuityResult(device_id="dev1", delivered=["d1"], held=["d2"])
    assert result.ok is True


def test_result_ok_false_when_any_failed() -> None:
    result = DeliveryContinuityResult(device_id="dev1", delivered=["d1"], failed=["d2"])
    assert result.ok is False


# ---------------------------------------------------------------------------
# enqueue(): deliveries inside the scope-approval window are HELD, not failed
# ---------------------------------------------------------------------------


def test_enqueue_inside_approval_window_holds_not_fails() -> None:
    queue = DeliveryContinuityQueue()
    is_approved = approval_from_set([])  # nothing approved -> all in-window

    item = queue.enqueue(
        "d1", "dev1", "push", {"body": "hi"}, is_approved=is_approved
    )

    assert item.status == "held"
    held = queue.held_for_device("dev1")
    assert [i.delivery_id for i in held] == ["d1"]
    # Held is neither delivered nor failed.
    assert item.status not in {"delivered", "failed"}


def test_enqueue_preserves_fifo_order_across_devices() -> None:
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "devA", "push")
    queue.enqueue("d2", "devB", "push")
    queue.enqueue("d3", "devA", "push")

    assert [i.delivery_id for i in queue.held_for_device("devA")] == ["d1", "d3"]
    assert [i.delivery_id for i in queue.held] == ["d1", "d2", "d3"]


def test_enqueue_duplicate_delivery_id_raises() -> None:
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "dev1", "push")
    with pytest.raises(ValueError):
        queue.enqueue("d1", "dev1", "push")


# ---------------------------------------------------------------------------
# to_snapshot()/from_snapshot(): continuity across a simulated restart
# ---------------------------------------------------------------------------


def test_snapshot_uses_schema_constant() -> None:
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "dev1", "push")
    snapshot = queue.to_snapshot()
    assert snapshot["schema"] == DELIVERY_CONTINUITY_SCHEMA
    assert snapshot["schema"] == "mac.openclaw_delivery_continuity.v1"


def test_snapshot_roundtrip_preserves_held_state_and_order() -> None:
    # Build a queue, hold several items across two devices.
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "devA", "push", {"n": 1})
    queue.enqueue("d2", "devB", "webhook", {"n": 2})
    queue.enqueue("d3", "devA", "push", {"n": 3})

    # Simulate a gateway restart: snapshot -> brand-new queue.from_snapshot.
    snapshot = queue.to_snapshot()
    restored = DeliveryContinuityQueue.from_snapshot(snapshot)

    # Held items survive intact and in FIFO order per device.
    assert [i.delivery_id for i in restored.held] == ["d1", "d2", "d3"]
    assert [i.delivery_id for i in restored.held_for_device("devA")] == ["d1", "d3"]
    devA_first = restored.held_for_device("devA")[0]
    assert devA_first.status == "held"
    assert devA_first.channel == "push"
    assert devA_first.payload == {"n": 1}


def test_from_snapshot_rejects_wrong_schema() -> None:
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "dev1", "push")
    snapshot = queue.to_snapshot()
    snapshot["schema"] = "mac.openclaw_delivery_continuity.v999"
    with pytest.raises(ValueError):
        DeliveryContinuityQueue.from_snapshot(snapshot)


def test_from_snapshot_rejects_unknown_status() -> None:
    snapshot = {
        "schema": DELIVERY_CONTINUITY_SCHEMA,
        "next_seq": 1,
        "deliveries": [
            {
                "delivery_id": "d1",
                "device_id": "dev1",
                "channel": "push",
                "payload": {},
                "status": "bogus",
                "created_seq": 0,
                "attempts": 0,
            }
        ],
    }
    with pytest.raises(ValueError):
        DeliveryContinuityQueue.from_snapshot(snapshot)


# ---------------------------------------------------------------------------
# drain_for_device(): re-delivery after approval is granted
# ---------------------------------------------------------------------------


def test_drain_while_in_window_holds_everything() -> None:
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "dev1", "push")
    queue.enqueue("d2", "dev1", "push")
    deliver_fn = _RecordingDeliverFn(succeed=True)

    result = queue.drain_for_device(
        "dev1", deliver_fn=deliver_fn, is_approved=_never_approved
    )

    # Still inside the window: nothing delivered/failed, all reported as held.
    assert deliver_fn.calls == []
    assert result.delivered == []
    assert result.failed == []
    assert result.held == ["d1", "d2"]
    assert result.ok is True
    # Items remain held in the queue.
    assert [i.delivery_id for i in queue.held_for_device("dev1")] == ["d1", "d2"]


def test_drain_after_approval_redelivers_fifo_and_marks_delivered() -> None:
    queue = DeliveryContinuityQueue()
    # Interleave two devices to prove per-device FIFO + isolation.
    queue.enqueue("a1", "devA", "push")
    queue.enqueue("b1", "devB", "push")
    queue.enqueue("a2", "devA", "push")
    queue.enqueue("a3", "devA", "push")

    deliver_fn = _RecordingDeliverFn(succeed=True)
    is_approved = approval_from_set(["devA"])  # devA approved, devB still in-window

    result = queue.drain_for_device(
        "devA", deliver_fn=deliver_fn, is_approved=is_approved
    )

    # Exact FIFO ordering of re-delivery for devA only.
    assert deliver_fn.calls == ["a1", "a2", "a3"]
    assert result.delivered == ["a1", "a2", "a3"]
    assert result.failed == []
    assert result.ok is True

    # devA items are now delivered (no longer held); devB untouched/held.
    assert queue.held_for_device("devA") == []
    assert [i.delivery_id for i in queue.held_for_device("devB")] == ["b1"]
    for item in queue.held:
        assert item.device_id == "devB"


def test_drain_after_restart_then_approval_redelivers() -> None:
    # Enqueue + hold, restart via snapshot, then approve and drain.
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "dev1", "push", {"n": 1})
    queue.enqueue("d2", "dev1", "push", {"n": 2})

    restored = DeliveryContinuityQueue.from_snapshot(queue.to_snapshot())

    deliver_fn = _RecordingDeliverFn(succeed=True)
    result = restored.drain_for_device(
        "dev1", deliver_fn=deliver_fn, is_approved=approval_from_set(["dev1"])
    )

    assert deliver_fn.calls == ["d1", "d2"]
    assert result.delivered == ["d1", "d2"]
    assert result.ok is True
    assert restored.held_for_device("dev1") == []


def test_drain_deliver_fn_failure_marks_failed_and_reflected_in_result() -> None:
    queue = DeliveryContinuityQueue()
    queue.enqueue("d1", "dev1", "push")
    queue.enqueue("d2", "dev1", "push")
    queue.enqueue("d3", "dev1", "push")

    deliver_fn = _fail_for("d2")
    result = queue.drain_for_device(
        "dev1", deliver_fn=deliver_fn, is_approved=approval_from_set(["dev1"])
    )

    # All attempted in FIFO order; d2 fails, the rest succeed.
    assert deliver_fn.calls == ["d1", "d2", "d3"]  # type: ignore[attr-defined]
    assert result.delivered == ["d1", "d3"]
    assert result.failed == ["d2"]
    assert result.ok is False

    # Status transitions are reflected on the underlying items (via snapshot).
    statuses = {
        d["delivery_id"]: d["status"] for d in queue.to_snapshot()["deliveries"]
    }
    assert statuses == {"d1": "delivered", "d2": "failed", "d3": "delivered"}
    # After the drain no item is left in the held state for this device.
    assert queue.held_for_device("dev1") == []


def test_drain_empty_device_id_raises() -> None:
    queue = DeliveryContinuityQueue()
    with pytest.raises(ValueError):
        queue.drain_for_device(
            "  ", deliver_fn=_RecordingDeliverFn(), is_approved=approval_from_set([])
        )
