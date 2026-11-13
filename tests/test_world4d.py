"""
Tests for world4d.py — shared 4D world engine.

Requires MAC_TEST_PG_URL env var pointing to a live Postgres instance
(agent_brain on the hub, or any test DB).
"""

from __future__ import annotations

import os
import pytest
from mac.world4d import World4D, Vec4, Shape, ShapeKind, FieldKind  # noqa: F401
from mac.world4d import WorldObject, WorldField

PG_URL = os.environ.get(
    "MAC_TEST_PG_URL",
    "postgresql://fleet_agent@100.72.16.110:5432/agent_brain",
)


@pytest.fixture(scope="module")
def world():
    # apply_schema=True: CI uses a fresh test DB where DDL must be applied.
    # CREATE TABLE IF NOT EXISTS is idempotent — safe to run against agent_brain too.
    w = World4D.connect(pg_url=PG_URL, agent="test_agent", apply_schema=True)
    yield w
    w.close()


# ------------------------------------------------------------------
# Vec4
# ------------------------------------------------------------------


class TestVec4:
    def test_add(self):
        a = Vec4(1, 2, 3, 4)
        b = Vec4(10, 20, 30, 40)
        c = a + b
        assert c.x == 11 and c.y == 22 and c.z == 33 and c.w == 44

    def test_sub(self):
        a = Vec4(5, 5, 5, 5)
        b = Vec4(1, 2, 3, 4)
        c = a - b
        assert c.to_list() == [4, 3, 2, 1]

    def test_dot(self):
        a = Vec4(1, 0, 0, 0)
        b = Vec4(0, 1, 0, 0)
        assert a.dot(b) == 0.0
        assert a.dot(a) == 1.0

    def test_norm_sq(self):
        v = Vec4(1, 1, 1, 1)
        assert v.norm_sq() == 4.0

    def test_roundtrip(self):
        v = Vec4(1.5, -2.0, 3.14, 0.0)
        assert Vec4.from_list(v.to_list()) == v


# ------------------------------------------------------------------
# Shape
# ------------------------------------------------------------------


class TestShape:
    def test_sphere_contains(self):
        s = Shape(kind=ShapeKind.sphere, params={"radius": 2.0})
        origin = Vec4(0, 0, 0, 0)
        assert s.contains(origin, Vec4(1, 0, 0, 0)) is True
        assert s.contains(origin, Vec4(3, 0, 0, 0)) is False

    def test_box_contains(self):
        s = Shape(kind=ShapeKind.box, params={"half_extents": [1, 1, 1, 1]})
        origin = Vec4(0, 0, 0, 0)
        assert s.contains(origin, Vec4(0.5, 0.5, 0.5, 0.5)) is True
        assert s.contains(origin, Vec4(2, 0, 0, 0)) is False

    def test_point_contains(self):
        s = Shape(kind=ShapeKind.point)
        origin = Vec4(1, 2, 3, 4)
        assert s.contains(origin, Vec4(1, 2, 3, 4)) is True
        assert s.contains(origin, Vec4(1.0001, 2, 3, 4)) is False

    def test_json_roundtrip(self):
        s = Shape(kind=ShapeKind.box, params={"half_extents": [1, 2, 3, 4]})
        s2 = Shape.from_json(s.to_json())
        assert s2.kind == s.kind
        assert s2.params == s.params


# ------------------------------------------------------------------
# World4D — objects
# ------------------------------------------------------------------


class TestWorldObjects:
    def test_spawn_and_get(self, world):
        h = world.spawn(
            "test_orb",
            pos=Vec4(1, 2, 3, 0.5),
            shape=Shape(kind=ShapeKind.sphere, params={"radius": 1.5}),
            meta={"color": "red"},
        )
        assert h.startswith("obj_")
        obj = world.get(h)
        assert obj is not None
        assert obj.name == "test_orb"
        assert pytest.approx(obj.pos.x) == 1.0
        assert pytest.approx(obj.pos.w) == 0.5
        assert obj.shape.kind == ShapeKind.sphere
        assert obj.meta["color"] == "red"
        world.despawn(h)

    def test_move(self, world):
        h = world.spawn("mover", pos=Vec4(0, 0, 0, 0))
        world.move(h, Vec4(10, 20, 30, 1))
        obj = world.get(h)
        assert obj is not None
        assert pytest.approx(obj.pos.x) == 10.0
        assert pytest.approx(obj.pos.w) == 1.0
        world.despawn(h)

    def test_set_shape(self, world):
        h = world.spawn("reshaper", shape=Shape(kind=ShapeKind.point))
        world.set_shape(h, Shape(kind=ShapeKind.box, params={"half_extents": [5, 5, 5, 1]}))
        obj = world.get(h)
        assert obj is not None
        assert obj.shape.kind == ShapeKind.box
        assert obj.shape.params["half_extents"] == [5, 5, 5, 1]
        world.despawn(h)

    def test_patch_meta(self, world):
        h = world.spawn("meta_obj", meta={"a": 1, "b": 2})
        world.patch_meta(h, {"b": 99, "c": 3})
        obj = world.get(h)
        assert obj is not None
        assert obj.meta["a"] == 1
        assert obj.meta["b"] == 99
        assert obj.meta["c"] == 3
        world.despawn(h)

    def test_despawn_nonexistent(self, world):
        result = world.despawn("obj_doesnotexist999")
        assert result is False

    def test_query_region(self, world):
        h1 = world.spawn("in_region", pos=Vec4(1, 1, 1, 0))
        h2 = world.spawn("out_region", pos=Vec4(100, 100, 100, 0))
        results = world.query_region(Vec4(-5, -5, -5, -1), Vec4(5, 5, 5, 1))
        handles = [o.handle for o in results]
        assert h1 in handles
        assert h2 not in handles
        world.despawn(h1)
        world.despawn(h2)

    def test_list_objects(self, world):
        h = world.spawn("list_test", pos=Vec4(0, 0, 0, 0))
        objs = world.list_objects()
        assert any(o.handle == h for o in objs)
        world.despawn(h)

    def test_list_objects_by_agent(self, world):
        h = world.spawn("agent_obj", pos=Vec4(0, 0, 0, 0))
        objs = world.list_objects(agent="test_agent")
        assert any(o.handle == h for o in objs)
        # Non-existent agent returns empty
        none_objs = world.list_objects(agent="agent_that_doesnt_exist_xyz")
        assert none_objs == []
        world.despawn(h)


# ------------------------------------------------------------------
# World4D — fields
# ------------------------------------------------------------------


class TestWorldFields:
    def test_add_and_get_field(self, world):
        fh = world.add_field(
            name="gravity",
            kind=FieldKind.scalar,
            origin=Vec4(-10, -10, -10, -1),
            extent=Vec4(20, 20, 20, 2),
            value={"potential": -9.8},
            meta={"source": "earth"},
        )
        assert fh.startswith("fld_")
        f = world.get_field(fh)
        assert f is not None
        assert f.name == "gravity"
        assert f.kind == FieldKind.scalar
        assert f.value["potential"] == pytest.approx(-9.8)
        assert f.meta["source"] == "earth"
        world.remove_field(fh)

    def test_set_field_value(self, world):
        fh = world.add_field(
            name="heat",
            kind=FieldKind.scalar,
            origin=Vec4(0, 0, 0, 0),
            extent=Vec4(1, 1, 1, 1),
            value={"temp": 20.0},
        )
        world.set_field_value(fh, {"temp": 100.0, "unit": "C"})
        f = world.get_field(fh)
        assert f is not None
        assert f.value["temp"] == pytest.approx(100.0)
        assert f.value["unit"] == "C"
        world.remove_field(fh)

    def test_fields_at(self, world):
        fh = world.add_field(
            name="zone",
            kind=FieldKind.vector,
            origin=Vec4(0, 0, 0, 0),
            extent=Vec4(10, 10, 10, 2),
            value={"dir": [1, 0, 0, 0]},
        )
        # Point inside the field's bounding box
        inside = world.fields_at(Vec4(5, 5, 5, 1))
        assert any(f.handle == fh for f in inside)
        # Point outside
        outside = world.fields_at(Vec4(50, 50, 50, 50))
        assert not any(f.handle == fh for f in outside)
        world.remove_field(fh)

    def test_list_fields_by_kind(self, world):
        fh = world.add_field(
            name="tensor_field",
            kind=FieldKind.tensor,
            origin=Vec4(0, 0, 0, 0),
            extent=Vec4(1, 1, 1, 1),
            value={"components": [[1, 0], [0, 1]]},
        )
        fields = world.list_fields(kind=FieldKind.tensor)
        assert any(f.handle == fh for f in fields)
        # Other kinds not included
        scalar_fields = world.list_fields(kind=FieldKind.scalar)
        assert not any(f.handle == fh for f in scalar_fields)
        world.remove_field(fh)

    def test_remove_field_nonexistent(self, world):
        result = world.remove_field("fld_doesnotexist999")
        assert result is False


# ------------------------------------------------------------------
# World4D — stats & snapshot
# ------------------------------------------------------------------


class TestWorldMeta:
    def test_stats(self, world):
        h = world.spawn("stat_obj")
        s = world.stats()
        assert s["objects"] >= 1
        world.despawn(h)

    def test_snapshot(self, world):
        h = world.spawn("snap_obj", pos=Vec4(7, 8, 9, 0))
        snap = world.snapshot()
        assert "objects" in snap and "fields" in snap
        assert any(o["handle"] == h for o in snap["objects"])
        world.despawn(h)

    def test_upsert_on_conflict(self, world):
        h = "obj_fixed_handle_test_world4d"
        h1 = world.spawn("v1", pos=Vec4(1, 0, 0, 0), handle=h)
        h2 = world.spawn("v2", pos=Vec4(2, 0, 0, 0), handle=h)
        assert h1 == h2 == h
        obj = world.get(h)
        assert obj is not None
        assert obj.name == "v2"
        assert pytest.approx(obj.pos.x) == 2.0
        world.despawn(h)


# ------------------------------------------------------------------
# World4D — presence
# ------------------------------------------------------------------


class TestPresence:
    def _make_world(self) -> "World4D":
        """Each presence test gets a fresh agent-tagged connection."""
        from mac.world4d import World4D

        return World4D.connect(
            pg_url=PG_URL,
            agent="test_presence_agent",
            apply_schema=True,  # CI may run TestPresence before module fixture; idempotent
        )

    def test_announce_creates_handle(self):
        w = self._make_world()
        h = w.announce(meta={"role": "tester"})
        assert h == "agent:test_presence_agent"
        obj = w.get(h)
        assert obj is not None
        assert obj.meta.get("presence") is True
        assert obj.meta.get("role") == "tester"
        w.depart()
        w.close()

    def test_announce_is_idempotent(self):
        w = self._make_world()
        h1 = w.announce()
        h2 = w.announce()
        assert h1 == h2
        # Only one object with that handle
        objs = w.list_objects(agent="test_presence_agent")
        presence_objs = [o for o in objs if o.handle == h1]
        assert len(presence_objs) == 1
        w.depart()
        w.close()

    def test_heartbeat_updates_timestamp(self):
        import time

        w = self._make_world()
        w.announce()
        time.sleep(0.05)
        t_before = time.time()
        w.heartbeat(meta_patch={"state": "working"})
        obj = w.get("agent:test_presence_agent")
        assert obj is not None
        # w-coord should be >= t_before
        assert obj.pos.w >= t_before - 1.0
        assert obj.meta.get("state") == "working"
        w.depart()
        w.close()

    def test_presence_returns_online_agents(self):
        w = self._make_world()
        w.announce(meta={"role": "tester"})
        online = w.presence(ttl=60.0)
        handles = [o.handle for o in online]
        assert "agent:test_presence_agent" in handles
        w.depart()
        w.close()

    def test_presence_excludes_expired(self):
        import time

        w = self._make_world()
        # Announce with a very old timestamp (ttl=0.001 will expire it)
        import time as _t

        old_ts = _t.time() - 300  # 5 minutes ago
        w.announce(pos=Vec4(0, 0, 0, old_ts))
        online = w.presence(ttl=1.0)  # only within 1 second
        handles = [o.handle for o in online]
        assert "agent:test_presence_agent" not in handles
        w.depart()
        w.close()

    def test_depart_removes_object(self):
        w = self._make_world()
        w.announce()
        w.depart()
        obj = w.get("agent:test_presence_agent")
        assert obj is None
        w.close()
