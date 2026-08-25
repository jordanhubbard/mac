"""Hub-only stall nudge for registered agents that have gone silent (ADR 0023).

Peers may observe silence on the broadcast bus. They must not address a
"get on with it" message. Recovery is one sender — the hub tick — with a
cap and a cooldown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from mac.agentbus_broadcast import BROADCAST_LAYER
from mac.models import TaskState, utcnow

NUDGE_SCHEMA = "mac.session_nudge.v1"
NUDGE_TOPIC = "mac.session.nudge.v1"
NUDGE_RESOURCE_KEY = "stall_nudge"
DEFAULT_SILENCE_SECONDS = 15 * 60
DEFAULT_COOLDOWN_SECONDS = 10 * 60
DEFAULT_MAX_ATTEMPTS = 3
LIVE_STATES = frozenset({TaskState.CLAIMED.value, TaskState.RUNNING.value})
HUB_SENDER_CANDIDATES = ("agent_operator",)


def peer_must_not_nudge() -> str:
    """Named obligation for tests: stall recovery is hub-only."""
    return "hub-only"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now(value: Optional[str]) -> datetime:
    parsed = _parse_time(value) if value else None
    return parsed or _parse_time(utcnow()) or datetime.now(timezone.utc)


def last_progress_at(plane: Any, agent_id: str, task_id: Optional[str]) -> Optional[str]:
    """Latest broadcast the worker (not the hub) made about this work."""
    try:
        sql = (
            "SELECT created_at FROM observability_events "
            "WHERE layer = ? AND source = ? "
            "AND name NOT LIKE 'bcast.agent.heartbeat%' "
        )
        params: List[Any] = [BROADCAST_LAYER, agent_id]
        if task_id:
            sql += "AND subject_id = ? "
            params.append(task_id)
        sql += "ORDER BY sequence DESC LIMIT 1"
        row = plane.store.query_one(sql, tuple(params))
    except Exception:  # noqa: BLE001 - missing table must not fail the tick
        return None
    if row is None:
        return None
    if hasattr(row, "keys"):
        return str(row["created_at"]) if row["created_at"] else None
    return str(row[0]) if row else None


def _hub_sender_id(plane: Any) -> Optional[str]:
    for agent_id in HUB_SENDER_CANDIDATES:
        try:
            agent = plane.get_agent(agent_id)
        except Exception:  # noqa: BLE001 - missing sender is a skip, not a crash
            continue
        resources = agent.resources if isinstance(agent.resources, dict) else {}
        if resources.get("virtual") is True:
            return agent.id
    for agent in plane.list_agents():
        resources = agent.resources if isinstance(agent.resources, dict) else {}
        if resources.get("virtual") is True:
            return agent.id
    return None


def _nudge_state(agent: Any) -> Dict[str, Any]:
    resources = agent.resources if isinstance(agent.resources, dict) else {}
    state = resources.get(NUDGE_RESOURCE_KEY)
    return dict(state) if isinstance(state, dict) else {}


def should_nudge(
    *,
    last_progress: Optional[str],
    fallback_at: Optional[str],
    nudge_state: Dict[str, Any],
    now: datetime,
    silence_seconds: int,
    cooldown_seconds: int,
    max_attempts: int,
) -> bool:
    attempts = int(nudge_state.get("count") or 0)
    if attempts >= max_attempts:
        return False
    last_nudge = _parse_time(nudge_state.get("last_at"))
    if last_nudge is not None and now - last_nudge < timedelta(seconds=cooldown_seconds):
        return False
    mark = _parse_time(last_progress) or _parse_time(fallback_at)
    if mark is None:
        return False
    return now - mark >= timedelta(seconds=silence_seconds)


def nudge_stalled_sessions(
    plane: Any,
    *,
    now: Optional[str] = None,
    silence_seconds: int = DEFAULT_SILENCE_SECONDS,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Dict[str, Any]:
    """Address one stall nudge per silent live task. Hub tick is the only caller."""
    clock = _now(now)
    ensure = getattr(plane, "_ensure_operator_persona", None)
    if callable(ensure):
        try:
            ensure()
        except Exception:  # noqa: BLE001 - missing steward is a skip, not a crash
            pass
    sender_id = _hub_sender_id(plane)
    nudged: List[Dict[str, Any]] = []
    skipped: List[str] = []
    if sender_id is None:
        return {
            "schema": NUDGE_SCHEMA,
            "nudged": [],
            "skipped": ["no_virtual_hub_sender"],
            "sender_id": None,
        }
    for task in plane.list_tasks(state=list(LIVE_STATES)):
        if str(getattr(task, "state", "")) not in LIVE_STATES:
            continue
        agent_id = str(getattr(task, "owner_agent_id", "") or "").strip()
        if not agent_id:
            skipped.append("task_without_owner:%s" % task.id)
            continue
        try:
            agent = plane.get_agent(agent_id)
        except Exception:  # noqa: BLE001
            skipped.append("missing_agent:%s" % agent_id)
            continue
        resources = dict(agent.resources) if isinstance(agent.resources, dict) else {}
        if resources.get("virtual") is True:
            skipped.append("virtual:%s" % agent_id)
            continue
        state = _nudge_state(agent)
        progress = last_progress_at(plane, agent_id, task.id)
        fallback = getattr(task, "started_at", None) or getattr(task, "updated_at", None)
        if not should_nudge(
            last_progress=progress,
            fallback_at=fallback,
            nudge_state=state,
            now=clock,
            silence_seconds=silence_seconds,
            cooldown_seconds=cooldown_seconds,
            max_attempts=max_attempts,
        ):
            continue
        attempt = int(state.get("count") or 0) + 1
        payload = {
            "schema": NUDGE_SCHEMA,
            "reason": "stall",
            "task_id": task.id,
            "attempt": attempt,
            "next_step": (
                "Read the AgentBus broadcast, then either claim ready work, "
                "announce progress, or address the hub with what is blocking you. "
                "Do not wait for a peer to nudge you."
            ),
        }
        plane.agentbus.publish(
            sender_id,
            recipient_agent_id=agent_id,
            topic=NUDGE_TOPIC,
            content_type="application/json",
            payload=payload,
            task_id=task.id,
        )
        resources[NUDGE_RESOURCE_KEY] = {
            "count": attempt,
            "last_at": clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "task_id": task.id,
        }
        plane.update_agent(agent_id, resources=resources, actor="hub.tick")
        nudged.append({"agent_id": agent_id, "task_id": task.id, "attempt": attempt})
    return {
        "schema": NUDGE_SCHEMA,
        "sender_id": sender_id,
        "nudged": nudged,
        "skipped": skipped,
    }
