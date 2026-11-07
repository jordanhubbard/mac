"""Agent presence in the shared 4D world.

Each fleet agent maintains a live world object representing itself.
On session start, call ``presence_checkin(agent_id)``.
The object fields carry live state: task, status, heartbeat.
Any agent can read the entire fleet's presence with ``presence_all()``.

Object naming: ``wobj_presence_<agent_id>`` — deterministic so we can
upsert without scanning.  Presence objects sit at (0,0,0,world_t) by
default; agents can relocate themselves as the world develops meaning.

Fields schema (all optional, all mergeable):
    status        : "idle" | "working" | "reviewing" | "offline"
    current_task  : MAC task ID or "" 
    heartbeat_utc : ISO-8601 UTC timestamp
    hermes        : hermes instance ID
    note          : freeform short note visible to peers
    capabilities  : list of capability strings
"""

from __future__ import annotations

import datetime
import socket
from typing import Any

from mac.world import (
    _connect, world_spawn, world_field_set, world_field_get,
    world_tick, world_snapshot, WorldObject,
)

PRESENCE_TAG = "agent-presence"


def _presence_id(agent_id: str) -> str:
    return f"wobj_presence_{agent_id}"


def presence_checkin(
    agent_id: str,
    status: str = "idle",
    current_task: str = "",
    hermes_instance: str = "",
    note: str = "",
    capabilities: list[str] | None = None,
    dsn: str | None = None,
) -> WorldObject:
    """Upsert this agent's presence object in the world.

    Safe to call multiple times — creates on first call, updates on
    subsequent calls.  Returns the live WorldObject.
    """
    handle = _presence_id(agent_id)
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    fields: dict[str, Any] = {
        "status": status,
        "current_task": current_task,
        "heartbeat_utc": now_utc,
        "hermes": hermes_instance,
        "note": note,
        "hostname": socket.gethostname(),
    }
    if capabilities is not None:
        fields["capabilities"] = capabilities

    # Check if the object already exists
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM world_objects WHERE id=%s", (handle,))
            exists = cur.fetchone() is not None
    finally:
        conn.close()

    if exists:
        return world_field_set(handle, agent=agent_id, updates=fields, dsn=dsn)
    else:
        return world_spawn(
            agent=agent_id,
            id=handle,
            name=f"agent:{agent_id}",
            x=0.0, y=0.0, z=0.0,
            shape={"type": "point", "kind": "agent"},
            fields=fields,
            tags=[PRESENCE_TAG, agent_id],
            dsn=dsn,
        )


def presence_update(
    agent_id: str,
    dsn: str | None = None,
    **kwargs: Any,
) -> WorldObject:
    """Update specific fields on this agent's presence object.

    Convenience wrapper — only pass the fields you want to change.
    ``status``, ``current_task``, ``note`` are typical targets.
    Always refreshes ``heartbeat_utc``.
    """
    handle = _presence_id(agent_id)
    updates = dict(kwargs)
    updates["heartbeat_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return world_field_set(handle, agent=agent_id, updates=updates, dsn=dsn)


def presence_all(dsn: str | None = None) -> list[WorldObject]:
    """Return presence objects for all online agents."""
    from mac.world import world_query_region
    return world_query_region(
        x_min=-1e9, x_max=1e9,
        y_min=-1e9, y_max=1e9,
        z_min=-1e9, z_max=1e9,
        tags=[PRESENCE_TAG],
        limit=50,
        dsn=dsn,
    )


def presence_get(agent_id: str, dsn: str | None = None) -> WorldObject | None:
    """Return one agent's presence object, or None if not checked in."""
    from mac.world import world_get
    try:
        return world_get(_presence_id(agent_id), dsn=dsn)
    except KeyError:
        return None


def presence_checkout(agent_id: str, dsn: str | None = None) -> dict:
    """Mark agent as offline (call on clean shutdown)."""
    return presence_update(agent_id, status="offline", current_task="", dsn=dsn).to_dict()
