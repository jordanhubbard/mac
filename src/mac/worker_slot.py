"""Fail-closed client for WebMux's authoritative heavy-worker FIFO queue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

QUEUE_CONTRACT = "webmux-worker-queue/v1"
COORDINATION_CONTRACT = "webmux-session-coordination/v1"
QUEUE_STATES = {
    "queued": "waiting",
    "queue-cancelled": "cancelled",
    "queue-expired": "expired",
    "queue-completed": "completed",
    "queue-granted": "granted",
}
QUEUE_ACTIONS = {
    "enqueue-reservation",
    "inspect-reservation",
    "cancel-reservation",
}
LEASE_ACTIONS = {
    "acquire-reservation": "reserved",
    "renew-reservation": "renewed",
    "release-reservation": "released",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")


class WorkerSlotError(RuntimeError):
    """A safe, secret-free worker admission failure."""


@dataclass(frozen=True)
class WorkerSlotConfig:
    executable: Path
    sha256: str
    node_id: str
    owner: str
    session_id: str
    incarnation: str
    timeout_seconds: int = 10

    @property
    def subject(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "owner": self.owner,
            "session_id": self.session_id,
            "incarnation": self.incarnation,
        }


def _timestamp(value: Any, description: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise WorkerSlotError(f"WebMux returned an invalid {description}") from None
    if parsed.tzinfo is None:
        raise WorkerSlotError(f"WebMux returned an invalid {description}")
    return parsed


class WorkerSlotClient:
    def __init__(self, config: WorkerSlotConfig) -> None:
        self.config = config
        self._verify_binary()

    def _verify_binary(self) -> None:
        path = self.config.executable
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise WorkerSlotError("trusted WebMux executable is not an executable absolute file")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest.lower() != self.config.sha256.lower():
            raise WorkerSlotError("trusted WebMux executable fingerprint mismatch")

    def _run(
        self,
        args: list[str],
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        env = dict(os.environ)
        env["WEBMUX_COORDINATION_EXPECTED_SESSION"] = self.config.session_id
        try:
            result = subprocess.run(
                [str(self.config.executable), "coordinate", *args],
                input=json.dumps(payload) if payload is not None else None,
                text=True,
                capture_output=True,
                timeout=timeout or self.config.timeout_seconds,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise WorkerSlotError("WebMux coordination command failed") from None
        try:
            body = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise WorkerSlotError("WebMux returned no valid structured receipt") from None
        if not isinstance(body, dict):
            raise WorkerSlotError("WebMux returned an invalid structured receipt")
        return result.returncode, body

    def _preflight(self, resource: str) -> dict[str, Any]:
        self._verify_binary()
        if resource != f"node:{self.config.node_id}:heavy":
            raise WorkerSlotError("resource does not name the expected executor node")
        rc, guide = self._run(["guide"])
        if (
            rc
            or guide.get("contract") != COORDINATION_CONTRACT
            or guide.get("subject") != self.config.subject
        ):
            raise WorkerSlotError("WebMux coordination identity mismatch")
        rc, view = self._run(["view"])
        own = view.get("self")
        resources = view.get("resources")
        epoch = view.get("history_epoch")
        valid_self = (
            isinstance(own, Mapping)
            and own.get("ref") == self.config.subject
            and own.get("active") is True
            and own.get("present") is True
            and own.get("revoked") is None
        )
        match = next(
            (
                row
                for row in resources or []
                if isinstance(row, Mapping) and row.get("id") == resource
            ),
            None,
        )
        if (
            rc
            or view.get("contract") != COORDINATION_CONTRACT
            or view.get("enabled") is not True
            or not valid_self
            or not isinstance(epoch, str)
            or not epoch
            or not match
            or match.get("authority_node_id") != self.config.node_id
            or match.get("queue_contract") != QUEUE_CONTRACT
            or type(match.get("capacity")) is not int
            or match.get("capacity") != 1
        ):
            raise WorkerSlotError("WebMux worker queue authority is unavailable")
        return view

    @staticmethod
    def _validate_request(action: str, request_id: str, fields: Mapping[str, Any]) -> None:
        if action not in QUEUE_ACTIONS | set(LEASE_ACTIONS):
            raise WorkerSlotError("unsupported worker queue action")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise WorkerSlotError("request ID must contain 8 to 128 characters")
        allowed: dict[str, set[str]] = {
            "enqueue-reservation": {"lease_seconds", "wait_seconds"},
            "inspect-reservation": {"record_id"},
            "acquire-reservation": {"record_id"},
            "cancel-reservation": {"record_id"},
            "renew-reservation": {"record_id", "fence", "lease_seconds"},
            "release-reservation": {"record_id", "fence"},
        }
        if set(fields) != allowed[action]:
            raise WorkerSlotError("worker queue action fields are incomplete or unsupported")
        if "record_id" in fields and not (
            isinstance(fields["record_id"], str) and _HEX64.fullmatch(fields["record_id"])
        ):
            raise WorkerSlotError("record ID must be 64 lowercase hexadecimal characters")
        for key, minimum, maximum in (
            ("lease_seconds", 15, 900),
            ("wait_seconds", 15, 86400),
        ):
            if key in fields:
                value = fields[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not minimum <= value <= maximum
                ):
                    raise WorkerSlotError(
                        f"{key.replace('_', ' ')} must be between {minimum} and {maximum}"
                    )
        if "fence" in fields:
            fence = fields["fence"]
            if isinstance(fence, bool) or not isinstance(fence, int) or fence <= 0:
                raise WorkerSlotError("fence must be a positive integer")

    def action(self, action: str, resource: str, request_id: str, **fields: Any) -> dict[str, Any]:
        self._validate_request(action, request_id, fields)
        view = self._preflight(resource)
        payload = {"action": action, "resource": resource, "request_id": request_id, **fields}
        rc, receipt = self._run(["act"], payload)
        status = receipt.get("status")
        if status in QUEUE_STATES:
            if action not in QUEUE_ACTIONS | {"acquire-reservation"}:
                raise WorkerSlotError("WebMux did not confirm the requested lease transition")
            return self._queue_receipt(receipt, fields.get("record_id"), action, request_id)
        if action not in LEASE_ACTIONS:
            if rc:
                raise WorkerSlotError("WebMux worker queue action failed")
            raise WorkerSlotError("WebMux returned an unexpected queue status")
        normalized = self._lease_receipt(receipt, action, fields, view, request_id)
        if rc:
            raise WorkerSlotError("WebMux worker queue action failed")
        return normalized

    def wait(
        self,
        resource: str,
        record_id: str,
        request_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not _HEX64.fullmatch(record_id):
            raise WorkerSlotError("record ID must be 64 lowercase hexadecimal characters")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise WorkerSlotError("wait request ID must contain 8 to 100 characters")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 30
        ):
            raise WorkerSlotError("wait timeout must be between 1 and 30 seconds")
        self._preflight(resource)
        rc, receipt = self._run(
            [
                "wait",
                "--resource",
                resource,
                "--record-id",
                record_id,
                "--request-id",
                request_id,
                "--timeout-seconds",
                str(timeout_seconds),
            ],
            timeout=timeout_seconds + self.config.timeout_seconds,
        )
        if receipt.get("status") not in QUEUE_STATES:
            raise WorkerSlotError("WebMux queue observation failed")
        # A timeout is an expected observer-only outcome; the structured last
        # observation is still authoritative and never grants the lease.
        return self._queue_receipt(receipt, record_id, "wait", request_id)

    @staticmethod
    def _queue_receipt(
        receipt: Mapping[str, Any], expected_record: Any, action: str, request_id: str
    ) -> dict[str, Any]:
        status = receipt.get("status")
        expected_state = QUEUE_STATES.get(status)
        record = receipt.get("record_id")
        queue = receipt.get("queue")
        if not isinstance(record, str) or not _HEX64.fullmatch(record):
            raise WorkerSlotError("WebMux returned an invalid queue record")
        if expected_record is not None and record != expected_record:
            raise WorkerSlotError("WebMux returned a different queue record")
        if (
            not isinstance(queue, Mapping)
            or receipt.get("state") != expected_state
            or queue.get("state") != expected_state
        ):
            raise WorkerSlotError("WebMux returned an incoherent queue state")
        sequence = queue.get("sequence")
        position = queue.get("position")
        ready = queue.get("ready")
        waiting = expected_state == "waiting"
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or isinstance(position, bool)
            or not isinstance(position, int)
            or (waiting and not 1 <= position <= 1024)
            or (not waiting and position != 0)
            or not isinstance(ready, bool)
            or (ready and (not waiting or position != 1))
            or (ready and action not in {"inspect-reservation", "wait"})
        ):
            raise WorkerSlotError("WebMux returned invalid queue position evidence")
        deadline = _timestamp(queue.get("deadline"), "queue deadline")
        if expected_state == "waiting" and deadline <= datetime.now(timezone.utc):
            raise WorkerSlotError("WebMux returned an expired waiting deadline")
        cancelled = status == "queue-cancelled"
        return {
            "status": status,
            "state": expected_state,
            "ticket_id": record,
            "lease_id": record if status in {"queue-granted", "queue-completed"} else None,
            "granted": False,
            "cancelled": cancelled,
            "queue": {
                "sequence": sequence,
                "position": position,
                "deadline": deadline.isoformat(),
                "state": expected_state,
                "ready": ready,
            },
            "action": action,
            "request_id": request_id,
        }

    def _lease_receipt(
        self,
        receipt: Mapping[str, Any],
        action: str,
        fields: Mapping[str, Any],
        view: Mapping[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        status = receipt.get("status")
        record = receipt.get("record_id")
        fence = receipt.get("fence")
        if status != LEASE_ACTIONS[action]:
            raise WorkerSlotError("WebMux did not confirm the requested lease transition")
        if record != fields.get("record_id"):
            raise WorkerSlotError("WebMux returned a different lease record")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence <= 0:
            raise WorkerSlotError("WebMux returned an invalid lease fence")
        if action in {"renew-reservation", "release-reservation"} and fence != fields.get("fence"):
            raise WorkerSlotError("WebMux returned a different lease fence")
        result: dict[str, Any] = {
            "status": status,
            "ticket_id": record,
            "lease_id": record,
            "fence": fence,
            "granted": action != "release-reservation",
            "request_id": request_id,
        }
        if action == "release-reservation":
            result["completed"] = True
            return result
        lease = receipt.get("lease")
        if not isinstance(lease, Mapping) or lease.get("current") is not True:
            raise WorkerSlotError("WebMux did not return a current lease")
        if lease.get("authority_node_id") != self.config.node_id:
            raise WorkerSlotError("WebMux lease authority mismatch")
        if lease.get("history_epoch") != view.get("history_epoch"):
            raise WorkerSlotError("WebMux lease history epoch mismatch")
        expiry = _timestamp(receipt.get("expires"), "lease expiry")
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        maximum = fields.get("lease_seconds", 900)
        if remaining <= 5 or remaining > maximum + 5:
            raise WorkerSlotError("WebMux lease expiry is outside the safe requested interval")
        result.update(
            {
                "expires": expiry.isoformat(),
                "lease": {
                    "current": True,
                    "authority_node_id": self.config.node_id,
                    "history_epoch": view["history_epoch"],
                },
            }
        )
        return result
