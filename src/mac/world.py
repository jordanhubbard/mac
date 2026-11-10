"""Shared 4D world environment for the MAC fleet.

Objects live in a persistent Postgres-backed world (fleet_brain).
Every fleet agent reads and writes the same world state.

Coordinate system
-----------------
x, y, z  : spatial axes (metres by convention, unitless by policy)
t        : world-time — floating-point epoch seconds OR a logical tick counter
           depending on how the world is being used.  ``world_tick()`` advances
           the global clock; callers may also set ``t`` directly when spawning
           or moving objects.

Object handles
--------------
Every object gets a stable handle of the form ``wobj_<32-hex-chars>`` returned
from ``world_spawn()``.  Handles are stable across restarts and usable by any
agent in the fleet.

Shape descriptor (JSONB)
------------------------
Shapes are open-ended JSONB so callers can extend them without a migration:

    {"type": "point"}
    {"type": "sphere",  "radius": 1.5}
    {"type": "box",     "dx": 2, "dy": 1, "dz": 0.5}          # half-extents
    {"type": "capsule", "radius": 0.3, "height": 1.8}
    {"type": "mesh",    "vertices": [[x,y,z], ...], "faces": [[0,1,2], ...]}
    {"type": "custom",  ...anything...}

Fields (JSONB)
--------------
Per-object named fields hold any JSON-serialisable value:

    temperature: 42.0
    velocity:    [0.0, 1.0, 0.0]
    color:       "red"
    metadata:    {"author": "bullwinkle"}

Field values are merged on set — existing keys not mentioned are preserved.
Use ``world_field_set(handle, {"key": None})`` to delete a key.

Thread safety
-------------
All writes use ``FOR UPDATE`` row-level locks on the object row so concurrent
agents do not race.  World-clock advances use ``SELECT ... FOR UPDATE`` on the
singleton ``world_state`` row.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Default DSN — Tailscale fleet_brain accessible from every agent node
# ---------------------------------------------------------------------------
DEFAULT_DSN = "host=100.72.16.110 port=5432 dbname=fleet_brain user=bullwinkle"
_LOCALHOST_DSN = "dbname=fleet_brain user=jkh"


def _connect(dsn: str | None = None) -> psycopg2.extensions.connection:
    """Open a connection; fall back to localhost peer-auth if remote fails."""
    for d in [dsn] if dsn is not None else [DEFAULT_DSN, _LOCALHOST_DSN]:
        try:
            conn = psycopg2.connect(d, cursor_factory=psycopg2.extras.RealDictCursor)
            conn.autocommit = False
            return conn
        except psycopg2.OperationalError:
            continue
    raise RuntimeError("Cannot connect to fleet_brain via any known DSN")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WorldObject:
    id: str
    name: str
    agent: str
    x: float
    y: float
    z: float
    t: float
    shape: dict[str, Any]
    fields: dict[str, Any]
    tags: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict) -> "WorldObject":
        return cls(
            id=row["id"],
            name=row["name"],
            agent=row["agent"],
            x=float(row["x"]),
            y=float(row["y"]),
            z=float(row["z"]),
            t=float(row["t"]),
            shape=row["shape"] if isinstance(row["shape"], dict) else json.loads(row["shape"]),
            fields=row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"]),
            tags=list(row["tags"]) if row["tags"] else [],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "agent": self.agent,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "t": self.t,
            "shape": self.shape,
            "fields": self.fields,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def world_spawn(
    agent: str,
    name: str = "",
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    t: float | None = None,
    shape: dict | None = None,
    fields: dict | None = None,
    tags: list[str] | None = None,
    id: str | None = None,
    dsn: str | None = None,
) -> WorldObject:
    """Spawn a new object in the world.  Returns the object with its handle.

    Pass ``id`` to use a deterministic handle (e.g. for agent presence objects).
    If omitted a random ``wobj_<uuid>`` is generated.
    """
    handle = id if id is not None else ("wobj_" + uuid.uuid4().hex)
    world_t = t if t is not None else _current_world_t(dsn=dsn)
    _shape = shape or {"type": "point"}
    _fields = fields or {}
    _tags = tags or []

    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO world_objects
                        (id, name, agent, x, y, z, t, shape, fields, tags)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                    RETURNING *
                    """,
                    (
                        handle,
                        name,
                        agent,
                        x,
                        y,
                        z,
                        world_t,
                        json.dumps(_shape),
                        json.dumps(_fields),
                        _tags,
                    ),
                )
                row = cur.fetchone()
                _log_event(
                    cur,
                    handle,
                    agent,
                    "spawn",
                    world_t,
                    {"x": x, "y": y, "z": z, "shape": _shape, "fields": _fields},
                )
        return WorldObject.from_row(row)
    finally:
        conn.close()


def world_move(
    handle: str,
    agent: str,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    t: float | None = None,
    dsn: str | None = None,
) -> WorldObject:
    """Move an object to a new (x,y,z,t) position.  Pass only axes to change."""
    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM world_objects WHERE id=%s FOR UPDATE", (handle,))
                old = cur.fetchone()
                if not old:
                    raise KeyError(f"Object not found: {handle}")
                nx = x if x is not None else float(old["x"])
                ny = y if y is not None else float(old["y"])
                nz = z if z is not None else float(old["z"])
                nt = t if t is not None else _current_world_t(dsn=dsn)
                cur.execute(
                    """
                    UPDATE world_objects
                    SET x=%s, y=%s, z=%s, t=%s, updated_at=now()
                    WHERE id=%s RETURNING *
                    """,
                    (nx, ny, nz, nt, handle),
                )
                row = cur.fetchone()
                _log_event(
                    cur,
                    handle,
                    agent,
                    "move",
                    nt,
                    {
                        "x": nx,
                        "y": ny,
                        "z": nz,
                        "prev_x": float(old["x"]),
                        "prev_y": float(old["y"]),
                        "prev_z": float(old["z"]),
                    },
                )
        return WorldObject.from_row(row)
    finally:
        conn.close()


def world_shape(
    handle: str,
    agent: str,
    shape: dict,
    dsn: str | None = None,
) -> WorldObject:
    """Replace the shape descriptor of an object."""
    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT t FROM world_objects WHERE id=%s FOR UPDATE", (handle,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Object not found: {handle}")
                world_t = float(row["t"])
                cur.execute(
                    """
                    UPDATE world_objects
                    SET shape=%s::jsonb, updated_at=now()
                    WHERE id=%s RETURNING *
                    """,
                    (json.dumps(shape), handle),
                )
                row = cur.fetchone()
                _log_event(cur, handle, agent, "shape", world_t, {"shape": shape})
        return WorldObject.from_row(row)
    finally:
        conn.close()


def world_field_set(
    handle: str,
    agent: str,
    updates: dict,
    dsn: str | None = None,
) -> WorldObject:
    """Merge ``updates`` into the object's fields.  ``None`` values delete keys."""
    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT fields, t FROM world_objects WHERE id=%s FOR UPDATE", (handle,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Object not found: {handle}")
                existing: dict = (
                    row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
                )
                world_t = float(row["t"])
                merged = {**existing, **updates}
                # Remove keys explicitly set to None
                merged = {k: v for k, v in merged.items() if v is not None}
                cur.execute(
                    """
                    UPDATE world_objects
                    SET fields=%s::jsonb, updated_at=now()
                    WHERE id=%s RETURNING *
                    """,
                    (json.dumps(merged), handle),
                )
                row = cur.fetchone()
                _log_event(cur, handle, agent, "field_set", world_t, {"updates": updates})
        return WorldObject.from_row(row)
    finally:
        conn.close()


def world_field_get(
    handle: str,
    key: str | None = None,
    dsn: str | None = None,
) -> Any:
    """Return one field value (key given) or all fields (key=None)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fields FROM world_objects WHERE id=%s", (handle,))
            row = cur.fetchone()
        if not row:
            raise KeyError(f"Object not found: {handle}")
        fields: dict = (
            row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
        )
        if key is None:
            return fields
        return fields.get(key)
    finally:
        conn.close()


def world_query_region(
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    z_min: float,
    z_max: float,
    t_min: float | None = None,
    t_max: float | None = None,
    tags: list[str] | None = None,
    agent: str | None = None,
    limit: int = 100,
    dsn: str | None = None,
) -> list[WorldObject]:
    """Return objects whose positions fall within the 4D bounding volume."""
    clauses = [
        "x BETWEEN %s AND %s",
        "y BETWEEN %s AND %s",
        "z BETWEEN %s AND %s",
    ]
    params: list[Any] = [x_min, x_max, y_min, y_max, z_min, z_max]

    if t_min is not None:
        clauses.append("t >= %s")
        params.append(t_min)
    if t_max is not None:
        clauses.append("t <= %s")
        params.append(t_max)
    if agent:
        clauses.append("agent = %s")
        params.append(agent)
    if tags:
        clauses.append("tags @> %s")
        params.append(tags)

    params.append(limit)
    sql = f"SELECT * FROM world_objects WHERE {' AND '.join(clauses)} ORDER BY t DESC LIMIT %s"

    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [WorldObject.from_row(r) for r in rows]
    finally:
        conn.close()


def world_tick(
    delta: float = 1.0,
    agent: str = "system",
    dsn: str | None = None,
) -> dict:
    """Advance the global world clock by ``delta`` and return the new state."""
    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM world_state WHERE id='world' FOR UPDATE")
                state = cur.fetchone()
                new_tick = int(state["tick"]) + 1
                new_t = float(state["world_t"]) + delta
                cur.execute(
                    """
                    UPDATE world_state
                    SET tick=%s, world_t=%s, updated_at=now()
                    WHERE id='world' RETURNING *
                    """,
                    (new_tick, new_t),
                )
                state = cur.fetchone()
        return {
            "tick": int(state["tick"]),
            "world_t": float(state["world_t"]),
            "updated_at": str(state["updated_at"]),
        }
    finally:
        conn.close()


def world_snapshot(
    limit: int = 500,
    agent: str | None = None,
    tags: list[str] | None = None,
    dsn: str | None = None,
) -> dict:
    """Return world clock state plus all (or filtered) objects."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM world_state WHERE id='world'")
            clock = cur.fetchone()

            clauses, params = [], []
            if agent:
                clauses.append("agent = %s")
                params.append(agent)
            if tags:
                clauses.append("tags @> %s")
                params.append(tags)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            cur.execute(
                f"SELECT * FROM world_objects {where} ORDER BY updated_at DESC LIMIT %s", params
            )
            objects = [WorldObject.from_row(r).to_dict() for r in cur.fetchall()]

        return {
            "clock": {
                "tick": int(clock["tick"]),
                "world_t": float(clock["world_t"]),
                "updated_at": str(clock["updated_at"]),
            },
            "objects": objects,
            "count": len(objects),
        }
    finally:
        conn.close()


def world_despawn(
    handle: str,
    agent: str,
    dsn: str | None = None,
) -> dict:
    """Remove an object from the world (logs a despawn event first)."""
    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT t FROM world_objects WHERE id=%s FOR UPDATE", (handle,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Object not found: {handle}")
                _log_event(cur, handle, agent, "despawn", float(row["t"]), {})
                cur.execute("DELETE FROM world_objects WHERE id=%s", (handle,))
        return {"despawned": handle}
    finally:
        conn.close()


def world_get(handle: str, dsn: str | None = None) -> WorldObject:
    """Fetch a single object by handle."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM world_objects WHERE id=%s", (handle,))
            row = cur.fetchone()
        if not row:
            raise KeyError(f"Object not found: {handle}")
        return WorldObject.from_row(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _current_world_t(dsn: str | None = None) -> float:
    """Return the current world_t from the global clock (non-transactional read)."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT world_t FROM world_state WHERE id='world'")
            row = cur.fetchone()
        return float(row["world_t"]) if row else 0.0
    finally:
        conn.close()


def _log_event(
    cur: Any,
    object_id: str,
    agent: str,
    event_type: str,
    world_t: float,
    payload: dict,
) -> None:
    """Append an event inside an open transaction (cursor must be active)."""
    cur.execute(
        """
        INSERT INTO world_events (object_id, agent, event_type, world_t, payload)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (object_id, agent, event_type, world_t, json.dumps(payload)),
    )
