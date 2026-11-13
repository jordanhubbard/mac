"""
world4d.py — Shared 4D world environment for the MAC fleet.

A live, Postgres-backed 4D coordinate space (x, y, z, w) with:
  - Object handles: stable string IDs (UUID-based)
  - Shape management: sphere, box, cylinder, hyperplane, hypersphere, mesh
  - Field system: scalar, vector, tensor fields over R^4 regions
  - LISTEN/NOTIFY for real-time events across all connected agents

Schema: world4d_objects + world4d_fields (on agent_brain, shared hub PG)

Usage:
    from mac.world4d import World4D, Vec4, Shape, ShapeKind, Field, FieldKind

    world = World4D.connect()

    # Create an object with a sphere shape at the origin
    handle = world.spawn(
        name="photon",
        pos=Vec4(0, 0, 0, 0),
        shape=Shape(kind=ShapeKind.sphere, params={"radius": 1.0}),
    )

    # Move it
    world.move(handle, Vec4(1, 2, 3, 0.5))

    # Add a scalar potential field
    fhandle = world.add_field(
        name="gravity",
        kind=FieldKind.scalar,
        origin=Vec4(0, 0, 0, 0),
        extent=Vec4(100, 100, 100, 1),
        value={"potential": -9.8},
        meta={"source": handle},
    )

    # Query what's in a region
    objs = world.query_region(Vec4(-5, -5, -5, -1), Vec4(5, 5, 5, 1))
    fields = world.fields_at(Vec4(1, 2, 3, 0.5))

    world.close()
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Optional

try:
    import psycopg  # type: ignore[import]
    from psycopg.rows import dict_row as _dict_row  # type: ignore[import]

    _PSYCOPG_VERSION = 3
    _dict_row_factory = _dict_row
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore[no-redef,import]

        _PSYCOPG_VERSION = 2
        _dict_row_factory = None  # psycopg2 uses cursor_factory instead
    except ImportError:
        psycopg = None  # type: ignore[assignment]
        _PSYCOPG_VERSION = 0
        _dict_row_factory = None

# ---------------------------------------------------------------------------
# Default connection — same hub Postgres every fleet member uses
# ---------------------------------------------------------------------------
DEFAULT_PG_URL = "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"
# Full URL with credentials: set WORLD4D_PG_URL in the environment (or ~/.pgpass)
# Fleet default resolves via ~/.pgpass for fleet_agent on the hub network.

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass
class Vec4:
    """A point or vector in 4-space (x, y, z, w)."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 0.0

    def __iter__(self):
        return iter((self.x, self.y, self.z, self.w))

    def to_list(self) -> list[float]:
        return [self.x, self.y, self.z, self.w]

    @classmethod
    def from_list(cls, v: list[float]) -> "Vec4":
        return cls(*v[:4])

    def __add__(self, other: "Vec4") -> "Vec4":
        return Vec4(self.x + other.x, self.y + other.y, self.z + other.z, self.w + other.w)

    def __sub__(self, other: "Vec4") -> "Vec4":
        return Vec4(self.x - other.x, self.y - other.y, self.z - other.z, self.w - other.w)

    def __mul__(self, scalar: float) -> "Vec4":
        return Vec4(self.x * scalar, self.y * scalar, self.z * scalar, self.w * scalar)

    def dot(self, other: "Vec4") -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z + self.w * other.w

    def norm_sq(self) -> float:
        return self.dot(self)


class ShapeKind(str, Enum):
    sphere = "sphere"  # 3-sphere (ball in 4D)
    box = "box"  # axis-aligned 4D box
    cylinder = "cylinder"  # cylinder along a 4D axis
    hyperplane = "hyperplane"  # infinite hyperplane (3D cross-section)
    hypersphere = "hypersphere"  # 3-sphere boundary (4D sphere surface)
    mesh = "mesh"  # arbitrary mesh (vertex list in R^4)
    point = "point"  # degenerate shape (single 4D point)


@dataclass
class Shape:
    """Geometric shape descriptor for a world object."""

    kind: ShapeKind
    params: dict[str, Any] = field(default_factory=dict)
    # Examples:
    #   sphere:      {"radius": 1.0}
    #   box:         {"half_extents": [1, 1, 1, 0.5]}
    #   cylinder:    {"radius": 1.0, "height": 2.0, "axis": [0, 1, 0, 0]}
    #   hyperplane:  {"normal": [0, 1, 0, 0], "d": 0.0}
    #   hypersphere: {"radius": 1.0}
    #   mesh:        {"vertices": [[x,y,z,w], ...], "faces": [[i,j,k], ...]}
    #   point:       {}

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind.value, "params": self.params})

    @classmethod
    def from_json(cls, s: str) -> "Shape":
        d = json.loads(s)
        # Accept both "kind" (canonical) and "type" (legacy/external)
        kind_val = d.get("kind") or d.get("type") or "point"
        return cls(kind=ShapeKind(kind_val), params=d.get("params", {}))

    def contains(self, pos: Vec4, test_pt: Vec4) -> bool:
        """Rough containment test (pos = object anchor, test_pt = point to test)."""
        delta = test_pt - pos
        if self.kind == ShapeKind.sphere:
            r = self.params.get("radius", 1.0)
            return delta.norm_sq() <= r * r
        elif self.kind == ShapeKind.box:
            he = self.params.get("half_extents", [1, 1, 1, 1])
            return all(abs(d) <= h for d, h in zip(list(delta), he))
        elif self.kind == ShapeKind.hypersphere:
            r = self.params.get("radius", 1.0)
            ns = delta.norm_sq()
            return abs(ns - r * r) < r * 0.01  # on surface within 1% tolerance
        elif self.kind == ShapeKind.hyperplane:
            normal = Vec4(*self.params.get("normal", [0, 1, 0, 0]))
            d = self.params.get("d", 0.0)
            dist = abs(normal.dot(test_pt) - d)
            return dist < 1e-6
        elif self.kind == ShapeKind.point:
            return delta.norm_sq() < 1e-12
        # cylinder, mesh: return True (not implemented in-process, DB query preferred)
        return True


class FieldKind(str, Enum):
    scalar = "scalar"  # R^4 → R
    vector = "vector"  # R^4 → R^4
    tensor = "tensor"  # R^4 → R^(n×m)


@dataclass
class WorldObject:
    """A live object in the shared world."""

    handle: str
    name: str
    agent: str
    pos: Vec4
    shape: Shape
    meta: dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class WorldField:
    """A field (scalar, vector, or tensor) covering a 4D region."""

    handle: str
    name: str
    agent: str
    kind: FieldKind
    origin: Vec4  # min-corner of bounding box
    extent: Vec4  # size of bounding box
    value: dict[str, Any]  # field data / function spec
    meta: dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# DDL (run once against agent_brain)
# ---------------------------------------------------------------------------

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS world4d_objects (
    handle        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    agent         TEXT NOT NULL DEFAULT '',
    pos           JSONB NOT NULL DEFAULT '[0,0,0,0]',
    shape         JSONB NOT NULL DEFAULT '{"kind":"point","params":{}}',
    meta          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS world4d_fields (
    handle        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    agent         TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'scalar',
    origin        JSONB NOT NULL DEFAULT '[0,0,0,0]',
    extent        JSONB NOT NULL DEFAULT '[1,1,1,1]',
    value         JSONB NOT NULL DEFAULT '{}',
    meta          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS world4d_objects_agent ON world4d_objects (agent);
CREATE INDEX IF NOT EXISTS world4d_fields_agent  ON world4d_fields  (agent);
CREATE INDEX IF NOT EXISTS world4d_fields_kind   ON world4d_fields  (kind);

-- Trigger to auto-update updated_at
CREATE OR REPLACE FUNCTION world4d_touch()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'world4d_objects_touch'
      AND tgrelid = 'world4d_objects'::regclass
  ) THEN
    CREATE TRIGGER world4d_objects_touch
      BEFORE UPDATE ON world4d_objects
      FOR EACH ROW EXECUTE FUNCTION world4d_touch();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'world4d_fields_touch'
      AND tgrelid = 'world4d_fields'::regclass
  ) THEN
    CREATE TRIGGER world4d_fields_touch
      BEFORE UPDATE ON world4d_fields
      FOR EACH ROW EXECUTE FUNCTION world4d_touch();
  END IF;
END $$;
"""

# ---------------------------------------------------------------------------
# World4D client
# ---------------------------------------------------------------------------


class World4D:
    """
    Shared 4D world client.

    All writes are immediately committed and broadcast via NOTIFY world4d_events
    so every connected agent sees the update on their next receive() call.
    """

    def __init__(self, conn: Any, agent: str = ""):
        self._conn = conn
        self._agent = agent
        self._listeners: list[Callable[[str, dict], None]] = []
        self._listen_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        pg_url: str = "",
        agent: str = "",
        apply_schema: bool = True,
    ) -> "World4D":
        """Open a connection to the shared world on the fleet hub.

        pg_url priority:
          1. Explicit argument
          2. WORLD4D_PG_URL env var
          3. DEFAULT_PG_URL (uses ~/.pgpass for auth)
        """
        import os as _os

        if not pg_url:
            pg_url = _os.environ.get("WORLD4D_PG_URL", DEFAULT_PG_URL)
        if psycopg is None:
            raise ImportError("psycopg (v3) or psycopg2 must be installed.")
        if _PSYCOPG_VERSION == 3:
            conn = psycopg.connect(pg_url, autocommit=False, row_factory=_dict_row_factory)  # type: ignore[call-arg]
        else:
            conn = psycopg.connect(pg_url)
            conn.autocommit = False
        w = cls(conn, agent=agent)
        if apply_schema:
            w._apply_schema()
        return w

    def _apply_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._stop_event.set()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=3)
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _new_handle(self, prefix: str = "obj") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def _notify(self, payload: dict) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_notify('world4d_events', %s)",
                    (json.dumps(payload),),
                )
        except Exception:
            pass  # Non-fatal; best effort

    def _row_to_obj(self, row: Any) -> WorldObject:
        if isinstance(row, dict):
            d = row
        else:
            # psycopg2 tuple fallback (shouldn't happen with dict_row but safety)
            raise ValueError("Expected dict row")
        pos_raw = d["pos"]
        if isinstance(pos_raw, str):
            pos_raw = json.loads(pos_raw)
        shape_raw = d["shape"]
        if isinstance(shape_raw, str):
            shape_raw = json.loads(shape_raw)
        meta_raw = d["meta"]
        if isinstance(meta_raw, str):
            meta_raw = json.loads(meta_raw)
        pos = Vec4(*pos_raw)
        shape_j = json.dumps(shape_raw)
        shape = Shape.from_json(shape_j)
        return WorldObject(
            handle=d["handle"],
            name=d["name"],
            agent=d.get("agent", ""),
            pos=pos,
            shape=shape,
            meta=meta_raw,
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
        )

    def _row_to_field(self, row: Any) -> WorldField:
        if isinstance(row, dict):
            d = row
        else:
            raise ValueError("Expected dict row")
        for key in ("origin", "extent", "value", "meta"):
            if isinstance(d[key], str):
                d[key] = json.loads(d[key])
        origin = Vec4(*d["origin"])
        extent = Vec4(*d["extent"])
        return WorldField(
            handle=d["handle"],
            name=d["name"],
            agent=d.get("agent", ""),
            kind=FieldKind(d["kind"]),
            origin=origin,
            extent=extent,
            value=d["value"],
            meta=d["meta"],
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
        )

    # ------------------------------------------------------------------
    # Object CRUD
    # ------------------------------------------------------------------

    def spawn(
        self,
        name: str,
        pos: Vec4 = Vec4(),
        shape: Optional[Shape] = None,
        meta: Optional[dict] = None,
        handle: Optional[str] = None,
    ) -> str:
        """Create an object in the world. Returns its handle (stable string ID)."""
        if shape is None:
            shape = Shape(kind=ShapeKind.point)
        if meta is None:
            meta = {}
        h = handle or self._new_handle("obj")
        shape_dict = {"kind": shape.kind.value, "params": shape.params}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO world4d_objects (handle, name, agent, pos, shape, meta)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                ON CONFLICT (handle) DO UPDATE
                  SET name = EXCLUDED.name,
                      pos = EXCLUDED.pos,
                      shape = EXCLUDED.shape,
                      meta = EXCLUDED.meta
                """,
                (
                    h,
                    name,
                    self._agent,
                    json.dumps(pos.to_list()),
                    json.dumps(shape_dict),
                    json.dumps(meta),
                ),
            )
        self._conn.commit()
        self._notify({"event": "spawned", "handle": h, "name": name, "agent": self._agent})
        return h

    def despawn(self, handle: str) -> bool:
        """Remove an object from the world. Returns True if it existed."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM world4d_objects WHERE handle = %s", (handle,))
            deleted = cur.rowcount > 0
        self._conn.commit()
        if deleted:
            self._notify({"event": "despawned", "handle": handle, "agent": self._agent})
        return deleted

    def move(self, handle: str, pos: Vec4) -> None:
        """Update an object's 4D position."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE world4d_objects SET pos = %s::jsonb WHERE handle = %s",
                (json.dumps(pos.to_list()), handle),
            )
        self._conn.commit()
        self._notify(
            {"event": "moved", "handle": handle, "pos": pos.to_list(), "agent": self._agent}
        )

    def set_shape(self, handle: str, shape: Shape) -> None:
        """Replace an object's shape."""
        shape_dict = {"kind": shape.kind.value, "params": shape.params}
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE world4d_objects SET shape = %s::jsonb WHERE handle = %s",
                (json.dumps(shape_dict), handle),
            )
        self._conn.commit()
        self._notify(
            {"event": "reshaped", "handle": handle, "shape": shape.kind.value, "agent": self._agent}
        )

    def set_meta(self, handle: str, meta: dict) -> None:
        """Replace object metadata entirely."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE world4d_objects SET meta = %s::jsonb WHERE handle = %s",
                (json.dumps(meta), handle),
            )
        self._conn.commit()

    def patch_meta(self, handle: str, patch: dict) -> None:
        """Merge-patch object metadata (top-level keys only)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE world4d_objects SET meta = meta || %s::jsonb WHERE handle = %s",
                (json.dumps(patch), handle),
            )
        self._conn.commit()

    def get(self, handle: str) -> Optional[WorldObject]:
        """Fetch a single object by handle."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM world4d_objects WHERE handle = %s", (handle,))
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_obj(row)

    def list_objects(self, agent: Optional[str] = None) -> list[WorldObject]:
        """List all objects, optionally filtered by owning agent."""
        with self._conn.cursor() as cur:
            if agent:
                cur.execute(
                    "SELECT * FROM world4d_objects WHERE agent = %s ORDER BY created_at", (agent,)
                )
            else:
                cur.execute("SELECT * FROM world4d_objects ORDER BY created_at")
            rows = cur.fetchall()
        return [self._row_to_obj(r) for r in rows]

    def query_region(self, lo: Vec4, hi: Vec4) -> list[WorldObject]:
        """
        Return objects whose anchor position is within the 4D axis-aligned box [lo, hi].
        (Shape containment not checked; use this for broad-phase queries.)
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM world4d_objects
                WHERE (pos->0)::float8 BETWEEN %s AND %s
                  AND (pos->1)::float8 BETWEEN %s AND %s
                  AND (pos->2)::float8 BETWEEN %s AND %s
                  AND (pos->3)::float8 BETWEEN %s AND %s
                ORDER BY created_at
                """,
                (lo.x, hi.x, lo.y, hi.y, lo.z, hi.z, lo.w, hi.w),
            )
            rows = cur.fetchall()
        return [self._row_to_obj(r) for r in rows]

    # ------------------------------------------------------------------
    # Field CRUD
    # ------------------------------------------------------------------

    def add_field(
        self,
        name: str,
        kind: FieldKind,
        origin: Vec4 = Vec4(),
        extent: Vec4 = Vec4(1, 1, 1, 1),
        value: Optional[dict] = None,
        meta: Optional[dict] = None,
        handle: Optional[str] = None,
    ) -> str:
        """Register a field in the world. Returns its handle."""
        if value is None:
            value = {}
        if meta is None:
            meta = {}
        h = handle or self._new_handle("fld")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO world4d_fields (handle, name, agent, kind, origin, extent, value, meta)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)
                ON CONFLICT (handle) DO UPDATE
                  SET name   = EXCLUDED.name,
                      kind   = EXCLUDED.kind,
                      origin = EXCLUDED.origin,
                      extent = EXCLUDED.extent,
                      value  = EXCLUDED.value,
                      meta   = EXCLUDED.meta
                """,
                (
                    h,
                    name,
                    self._agent,
                    kind.value,
                    json.dumps(origin.to_list()),
                    json.dumps(extent.to_list()),
                    json.dumps(value),
                    json.dumps(meta),
                ),
            )
        self._conn.commit()
        self._notify(
            {
                "event": "field_added",
                "handle": h,
                "name": name,
                "kind": kind.value,
                "agent": self._agent,
            }
        )
        return h

    def remove_field(self, handle: str) -> bool:
        """Remove a field. Returns True if it existed."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM world4d_fields WHERE handle = %s", (handle,))
            deleted = cur.rowcount > 0
        self._conn.commit()
        if deleted:
            self._notify({"event": "field_removed", "handle": handle, "agent": self._agent})
        return deleted

    def set_field_value(self, handle: str, value: dict) -> None:
        """Replace a field's value data."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE world4d_fields SET value = %s::jsonb WHERE handle = %s",
                (json.dumps(value), handle),
            )
        self._conn.commit()
        self._notify({"event": "field_updated", "handle": handle, "agent": self._agent})

    def get_field(self, handle: str) -> Optional[WorldField]:
        """Fetch a single field by handle."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM world4d_fields WHERE handle = %s", (handle,))
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_field(row)

    def list_fields(
        self, kind: Optional[FieldKind] = None, agent: Optional[str] = None
    ) -> list[WorldField]:
        """List fields, optionally filtered."""
        where, params = [], []
        if kind:
            where.append("kind = %s")
            params.append(kind.value)
        if agent:
            where.append("agent = %s")
            params.append(agent)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM world4d_fields {clause} ORDER BY created_at", params)
            rows = cur.fetchall()
        return [self._row_to_field(r) for r in rows]

    def fields_at(self, pt: Vec4, kind: Optional[FieldKind] = None) -> list[WorldField]:
        """
        Return fields whose bounding box contains the given 4D point.
        """
        where = [
            "(origin->0)::float8 <= %s AND (origin->0)::float8 + (extent->0)::float8 >= %s",
            "(origin->1)::float8 <= %s AND (origin->1)::float8 + (extent->1)::float8 >= %s",
            "(origin->2)::float8 <= %s AND (origin->2)::float8 + (extent->2)::float8 >= %s",
            "(origin->3)::float8 <= %s AND (origin->3)::float8 + (extent->3)::float8 >= %s",
        ]
        params: list[Any] = [
            pt.x,
            pt.x,
            pt.y,
            pt.y,
            pt.z,
            pt.z,
            pt.w,
            pt.w,
        ]
        if kind:
            where.append("kind = %s")
            params.append(kind.value)
        clause = "WHERE " + " AND ".join(where)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM world4d_fields {clause} ORDER BY created_at", params)
            rows = cur.fetchall()
        return [self._row_to_field(r) for r in rows]

    # ------------------------------------------------------------------
    # Real-time events (LISTEN/NOTIFY)
    # ------------------------------------------------------------------

    def on_event(self, callback: Callable[[str, dict], None]) -> None:
        """
        Register a callback for world events.
        callback(event_type: str, payload: dict) is called in a background thread.
        Call start_listening() to activate.
        """
        self._listeners.append(callback)

    def start_listening(self) -> None:
        """Start a background thread that listens for world4d_events notifications."""
        if _PSYCOPG_VERSION != 3:
            raise RuntimeError("LISTEN/NOTIFY background thread requires psycopg v3.")
        self._stop_event.clear()
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="world4d-listener"
        )
        self._listen_thread.start()

    def _listen_loop(self) -> None:
        try:
            import psycopg as pg3  # noqa: F401

            # Open a separate connection for LISTEN (must be in autocommit)
            listen_conn = psycopg.connect(
                self._conn.info.dsn,
                autocommit=True,
                row_factory=_dict_row_factory,  # type: ignore[call-arg]
            )
            with listen_conn:
                listen_conn.execute("LISTEN world4d_events")
                for notify in listen_conn.notifies(stop_after=None):
                    if self._stop_event.is_set():
                        break
                    try:
                        payload = json.loads(notify.payload)
                        event_type = payload.get("event", "unknown")
                        for cb in self._listeners:
                            try:
                                cb(event_type, payload)
                            except Exception:
                                pass
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass  # Silently die if connection lost; caller can restart

    def stop_listening(self) -> None:
        """Signal the background listener to stop."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # World state summary
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return counts of objects and fields in the shared world."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM world4d_objects")
            row = cur.fetchone()
            obj_count = row["n"] if isinstance(row, dict) else row[0]
            cur.execute("SELECT kind, COUNT(*) AS n FROM world4d_fields GROUP BY kind")
            field_rows = cur.fetchall()
        field_counts = {}
        for r in field_rows:
            if isinstance(r, dict):
                field_counts[r["kind"]] = r["n"]
            else:
                field_counts[r[0]] = r[1]
        return {"objects": obj_count, "fields": field_counts}

    def snapshot(self) -> dict:
        """Full serializable snapshot of the entire world (objects + fields)."""
        objs = self.list_objects()
        fields = self.list_fields()

        def obj_to_dict(o: WorldObject) -> dict:
            return {
                "handle": o.handle,
                "name": o.name,
                "agent": o.agent,
                "pos": o.pos.to_list(),
                "shape": {"kind": o.shape.kind.value, "params": o.shape.params},
                "meta": o.meta,
                "created_at": o.created_at,
                "updated_at": o.updated_at,
            }

        def field_to_dict(f: WorldField) -> dict:
            return {
                "handle": f.handle,
                "name": f.name,
                "agent": f.agent,
                "kind": f.kind.value,
                "origin": f.origin.to_list(),
                "extent": f.extent.to_list(),
                "value": f.value,
                "meta": f.meta,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
            }

        return {
            "objects": [obj_to_dict(o) for o in objs],
            "fields": [field_to_dict(f) for f in fields],
        }

    # ------------------------------------------------------------------
    # Presence — agent identity in the shared world
    # ------------------------------------------------------------------

    def announce(
        self,
        meta: Optional[dict] = None,
        pos: Optional[Vec4] = None,
    ) -> str:
        """
        Announce this agent's presence in the world.

        Creates or upserts a point object with handle ``agent:<agent_id>``.
        The w-coordinate is set to the current Unix timestamp (the temporal axis).
        Returns the handle.

        Call once at session start; call heartbeat() periodically to stay "online".
        """
        import time as _time

        if not self._agent:
            raise ValueError("announce() requires agent to be set at connect time")
        now = _time.time()
        p = pos if pos is not None else Vec4(0.0, 0.0, 0.0, now)
        m = dict(meta or {})
        m.setdefault("presence", True)
        m["last_seen"] = now
        h = f"agent:{self._agent}"
        self.spawn(
            name=self._agent,
            pos=p,
            shape=Shape(kind=ShapeKind.point),
            meta=m,
            handle=h,
        )
        return h

    def heartbeat(self, meta_patch: Optional[dict] = None) -> None:
        """
        Update this agent's presence timestamp (w = now).

        Agents that have not heartbeated within ``ttl`` seconds are
        considered offline by ``presence()``.
        """
        import time as _time

        if not self._agent:
            raise ValueError("heartbeat() requires agent to be set at connect time")
        h = f"agent:{self._agent}"
        now = _time.time()
        # Update w to current time
        obj = self.get(h)
        if obj is None:
            self.announce()
            return
        new_pos = Vec4(obj.pos.x, obj.pos.y, obj.pos.z, now)
        self.move(h, new_pos)
        patch: dict = {"last_seen": now}
        if meta_patch:
            patch.update(meta_patch)
        self.patch_meta(h, patch)

    def presence(self, ttl: float = 120.0) -> list["WorldObject"]:
        """
        Return agents that have heartbeated within ``ttl`` seconds.

        Filters world objects tagged with ``presence=True`` in meta
        whose w-coordinate (last-seen timestamp) is within ttl of now.
        """
        import time as _time

        now = _time.time()
        cutoff = now - ttl
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM world4d_objects
                WHERE (meta->>'presence')::boolean = true
                  AND (pos->3)::float8 >= %s
                ORDER BY (pos->3)::float8 DESC
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
        return [self._row_to_obj(r) for r in rows]

    def depart(self) -> None:
        """Remove this agent's presence object from the world."""
        if not self._agent:
            return
        self.despawn(f"agent:{self._agent}")

    # ------------------------------------------------------------------
    # Schema provisioning (standalone)
    # ------------------------------------------------------------------

    @classmethod
    def apply_schema(cls, pg_url: str = "") -> None:
        """Create world4d tables in the target database (idempotent)."""
        import os as _os

        if not pg_url:
            pg_url = _os.environ.get("WORLD4D_PG_URL", DEFAULT_PG_URL)
        w = cls.connect(pg_url=pg_url, apply_schema=True)
        w.close()
        print("world4d schema applied.")
