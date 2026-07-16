"""Restart-safe delivery continuity for the OpenClaw scope-approval window.

When an OpenClaw gateway restarts, a target device may still be inside its
*scope-approval window* — the device has connected but its scope grant has not
yet been approved. Historically, deliveries aimed at such a device failed (or
were silently dropped) because the delivery path treated "not yet approved" as
a hard failure. This module models the fix as a small, self-contained,
side-effect-free state machine:

  1. Deliveries for a device that is still inside its approval window are
     *held* (not failed) via :class:`DeliveryContinuityQueue`.
  2. The held state is snapshot/restore-able (:meth:`DeliveryContinuityQueue.to_snapshot`
     / :meth:`DeliveryContinuityQueue.from_snapshot`) using plain
     JSON-serializable dicts, so it survives a gateway restart.
  3. Once a device's scope approval is granted, held items are re-delivered in
     FIFO order through an injected ``deliver_fn`` (:meth:`DeliveryContinuityQueue.drain_for_device`),
     each marked delivered or failed, and summarized in
     :class:`DeliveryContinuityResult`.

Approval is expressed through an injected predicate (a callable) or an explicit
approved-device set — never a live device/gateway integration. All functions
are pure/injectable and hold no global fleet state.

Status values: held, delivered, failed, expired
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

# Exported snapshot schema identifier. Mirrors the ``mac.<module>.vN``
# convention used by adjacent fleet modules (e.g. ``ROLLOUT_PLAN_SCHEMA``
# in openclaw_fleet_rollout.py). Consumers pin snapshot compatibility
# against this value.
DELIVERY_CONTINUITY_SCHEMA = "mac.openclaw_delivery_continuity.v1"

# Terminal + non-terminal delivery states.
VALID_STATUSES = frozenset({"held", "delivered", "failed", "expired"})

# An approval predicate answers "is this device's scope approval granted?".
ApprovalPredicate = Callable[[str], bool]

# A delivery callable performs the actual (injected) delivery of one item and
# returns True on success.
DeliverFn = Callable[["PendingDelivery"], bool]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PendingDelivery:
    """A single delivery targeted at a device, tracked across restarts.

    Attributes:
        delivery_id: Stable unique id for this delivery.
        device_id: Target device whose scope-approval window gates delivery.
        channel: Logical delivery channel (e.g. "push", "webhook").
        payload: JSON-serializable delivery payload.
        status: One of :data:`VALID_STATUSES`.
        created_seq: Monotonic marker used to preserve FIFO order across a
            snapshot/restore cycle.
        attempts: Number of delivery attempts made so far.
    """

    delivery_id: str
    device_id: str
    channel: str
    payload: dict
    status: str = "held"
    created_seq: int = 0
    attempts: int = 0

    def __post_init__(self) -> None:
        self.delivery_id = str(self.delivery_id or "").strip()
        self.device_id = str(self.device_id or "").strip()
        self.channel = str(self.channel or "").strip()
        if not self.delivery_id:
            raise ValueError("delivery_id is required")
        if not self.device_id:
            raise ValueError("device_id is required")
        if not self.channel:
            raise ValueError("channel is required")
        if self.status not in VALID_STATUSES:
            raise ValueError(
                "invalid status %r (expected one of %s)"
                % (self.status, sorted(VALID_STATUSES))
            )
        if self.payload is None:
            self.payload = {}
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a JSON-serializable dict")
        self.created_seq = int(self.created_seq)
        self.attempts = int(self.attempts)
        if self.attempts < 0:
            raise ValueError("attempts must be >= 0")

    def to_dict(self) -> dict:
        """Return a plain JSON-serializable dict for this delivery."""
        return {
            "delivery_id": self.delivery_id,
            "device_id": self.device_id,
            "channel": self.channel,
            "payload": self.payload,
            "status": self.status,
            "created_seq": self.created_seq,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingDelivery":
        """Rebuild a :class:`PendingDelivery` from a plain dict.

        Raises:
            ValueError: If *data* is not a dict or holds malformed fields
                (empty ids, unknown status, non-dict payload, etc.).
        """
        if not isinstance(data, dict):
            raise ValueError("pending delivery entry must be a dict")
        return cls(
            delivery_id=data.get("delivery_id", ""),
            device_id=data.get("device_id", ""),
            channel=data.get("channel", ""),
            payload=data.get("payload", {}),
            status=data.get("status", "held"),
            created_seq=data.get("created_seq", 0),
            attempts=data.get("attempts", 0),
        )


@dataclass
class DeliveryContinuityResult:
    """Summary of a drain/re-delivery pass for a device."""

    device_id: str
    delivered: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    expired: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed during the drain pass."""
        return not self.failed


# ---------------------------------------------------------------------------
# Approval-window helpers
# ---------------------------------------------------------------------------


def approval_from_set(approved_devices: Iterable[str]) -> ApprovalPredicate:
    """Build an :data:`ApprovalPredicate` from an explicit approved-device set.

    A device is considered approved (i.e. *outside* its scope-approval window)
    iff its id is present in *approved_devices*.

    Raises:
        ValueError: If *approved_devices* is not iterable of strings.
    """
    try:
        approved: Set[str] = {str(d).strip() for d in approved_devices}
    except TypeError as exc:  # not iterable
        raise ValueError("approved_devices must be an iterable of device ids") from exc
    if "" in approved:
        raise ValueError("approved_devices must not contain empty device ids")

    def _predicate(device_id: str) -> bool:
        return str(device_id).strip() in approved

    return _predicate


# ---------------------------------------------------------------------------
# Continuity queue
# ---------------------------------------------------------------------------


class DeliveryContinuityQueue:
    """A restart-safe queue that holds deliveries during the approval window.

    The queue never performs real I/O. Deliveries are enqueued (held while the
    target device is inside its scope-approval window) and later drained via an
    injected ``deliver_fn`` once approval is granted. State can be snapshotted
    to / restored from plain JSON-serializable dicts so held deliveries survive
    a gateway restart.
    """

    def __init__(self) -> None:
        self._items: List[PendingDelivery] = []
        self._seq: int = 0
        self._known_ids: Set[str] = set()

    # -- enqueue --------------------------------------------------------

    def enqueue(
        self,
        delivery_id: str,
        device_id: str,
        channel: str,
        payload: Optional[dict] = None,
        *,
        is_approved: Optional[ApprovalPredicate] = None,
    ) -> PendingDelivery:
        """Enqueue a delivery, holding it during the device approval window.

        When *is_approved* is provided and reports the device as still inside
        its scope-approval window (not approved), the delivery is recorded with
        status ``"held"`` rather than being failed/dropped. When the device is
        already approved the item is still enqueued as ``"held"`` and delivered
        on the next :meth:`drain_for_device` pass; this keeps enqueue pure and
        free of side effects.

        Args:
            delivery_id: Stable unique id (must be unique within the queue).
            device_id: Target device id.
            channel: Logical delivery channel.
            payload: Optional JSON-serializable payload dict.
            is_approved: Optional approval predicate. Present only to make the
                held-vs-approved decision explicit and injectable; it is never
                used to fail an enqueue.

        Returns:
            The stored :class:`PendingDelivery`.

        Raises:
            ValueError: On malformed input or a duplicate *delivery_id*.
        """
        item = PendingDelivery(
            delivery_id=delivery_id,
            device_id=device_id,
            channel=channel,
            payload=payload if payload is not None else {},
            status="held",
            created_seq=self._seq,
        )
        if item.delivery_id in self._known_ids:
            raise ValueError("duplicate delivery_id %r" % item.delivery_id)
        # Touch the predicate defensively so callers get a clear signal that a
        # not-yet-approved device is intentionally held, never failed.
        if is_approved is not None:
            is_approved(item.device_id)
        self._seq += 1
        self._known_ids.add(item.delivery_id)
        self._items.append(item)
        return item

    # -- inspection -----------------------------------------------------

    def held_for_device(self, device_id: str) -> List[PendingDelivery]:
        """Return held deliveries for *device_id* in FIFO order."""
        key = str(device_id).strip()
        if not key:
            raise ValueError("device_id is required")
        items = [i for i in self._items if i.device_id == key and i.status == "held"]
        return sorted(items, key=lambda i: i.created_seq)

    @property
    def held(self) -> List[PendingDelivery]:
        """All currently-held deliveries in FIFO order."""
        return sorted(
            (i for i in self._items if i.status == "held"),
            key=lambda i: i.created_seq,
        )

    def __len__(self) -> int:
        return len(self._items)

    # -- drain / redeliver ---------------------------------------------

    def drain_for_device(
        self,
        device_id: str,
        *,
        deliver_fn: DeliverFn,
        is_approved: ApprovalPredicate,
    ) -> DeliveryContinuityResult:
        """Re-deliver all held items for a device once its approval is granted.

        If the device is still inside its scope-approval window (``is_approved``
        returns False), every held item stays held and is reported under
        ``held`` — nothing is failed. Once approval is granted, held items are
        delivered in FIFO order through *deliver_fn*; each is marked
        ``"delivered"`` when the callable returns True and ``"failed"``
        otherwise. Non-held items (already delivered/failed/expired) are left
        untouched.

        Args:
            device_id: Target device id.
            deliver_fn: Injected callable performing the actual delivery of one
                :class:`PendingDelivery`; returns True on success.
            is_approved: Approval predicate gating whether to drain.

        Returns:
            A :class:`DeliveryContinuityResult` summarizing the pass.

        Raises:
            ValueError: If *device_id* is empty.
        """
        key = str(device_id).strip()
        if not key:
            raise ValueError("device_id is required")
        if deliver_fn is None:
            raise ValueError("deliver_fn is required")
        if is_approved is None:
            raise ValueError("is_approved predicate is required")

        result = DeliveryContinuityResult(device_id=key)
        pending = self.held_for_device(key)

        if not is_approved(key):
            # Still inside the approval window: hold, do not fail.
            result.held.extend(i.delivery_id for i in pending)
            return result

        for item in pending:
            item.attempts += 1
            try:
                ok = bool(deliver_fn(item))
            except Exception:  # noqa: BLE001 - a raising deliver_fn is a failure
                ok = False
            if ok:
                item.status = "delivered"
                result.delivered.append(item.delivery_id)
            else:
                item.status = "failed"
                result.failed.append(item.delivery_id)
        return result

    def expire(self, delivery_id: str) -> PendingDelivery:
        """Mark a still-held delivery as ``"expired"``.

        Raises:
            ValueError: If the id is unknown or the item is not currently held.
        """
        key = str(delivery_id).strip()
        if not key:
            raise ValueError("delivery_id is required")
        for item in self._items:
            if item.delivery_id == key:
                if item.status != "held":
                    raise ValueError(
                        "delivery %r is %r, only held deliveries can expire"
                        % (key, item.status)
                    )
                item.status = "expired"
                return item
        raise ValueError("unknown delivery_id %r" % key)

    # -- snapshot / restore --------------------------------------------

    def to_snapshot(self) -> dict:
        """Return a plain JSON-serializable snapshot of the queue state.

        The snapshot carries the schema constant so restore can pin
        compatibility, and preserves FIFO ordering across a restart.
        """
        return {
            "schema": DELIVERY_CONTINUITY_SCHEMA,
            "next_seq": self._seq,
            "deliveries": [i.to_dict() for i in self._items],
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "DeliveryContinuityQueue":
        """Rebuild a queue from a snapshot produced by :meth:`to_snapshot`.

        Raises:
            ValueError: If the snapshot is malformed — wrong/missing schema,
                non-list deliveries, duplicate ids, or an entry with an empty
                id or unknown status.
        """
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a dict")
        schema = snapshot.get("schema")
        if schema != DELIVERY_CONTINUITY_SCHEMA:
            raise ValueError(
                "unsupported snapshot schema %r (expected %r)"
                % (schema, DELIVERY_CONTINUITY_SCHEMA)
            )
        raw = snapshot.get("deliveries", [])
        if not isinstance(raw, list):
            raise ValueError("snapshot 'deliveries' must be a list")

        queue = cls()
        max_seq = -1
        for entry in raw:
            item = PendingDelivery.from_dict(entry)
            if item.delivery_id in queue._known_ids:
                raise ValueError("duplicate delivery_id %r in snapshot" % item.delivery_id)
            queue._known_ids.add(item.delivery_id)
            queue._items.append(item)
            if item.created_seq > max_seq:
                max_seq = item.created_seq

        next_seq = snapshot.get("next_seq")
        if next_seq is None:
            queue._seq = max_seq + 1
        else:
            queue._seq = int(next_seq)
            if queue._seq <= max_seq:
                queue._seq = max_seq + 1
        return queue


__all__ = [
    "DELIVERY_CONTINUITY_SCHEMA",
    "VALID_STATUSES",
    "ApprovalPredicate",
    "DeliverFn",
    "PendingDelivery",
    "DeliveryContinuityResult",
    "DeliveryContinuityQueue",
    "approval_from_set",
]
