"""Tests for the shared 4D world environment (world.py + world_mcp.py).

These tests use the live fleet_brain database (requires Tailscale or localhost).
Every object spawned is tagged 'pytest-world' and despawned in teardown.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
import pytest

from mac.world import (
    world_spawn, world_move, world_shape, world_field_set, world_field_get,
    world_query_region, world_tick, world_snapshot, world_despawn, world_get,
    _connect,
)

AGENT = "bullwinkle-test"
TEST_TAG = f"pytest-world-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def cleanup_test_objects():
    """Remove all objects tagged with our unique test tag after each test."""
    yield
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM world_events WHERE object_id IN "
                    "(SELECT id FROM world_objects WHERE %s = ANY(tags))",
                    (TEST_TAG,),
                )
                cur.execute(
                    "DELETE FROM world_objects WHERE %s = ANY(tags)",
                    (TEST_TAG,),
                )
    finally:
        conn.close()


def _spawn(**kw):
    tags = kw.pop("tags", [])
    return world_spawn(agent=AGENT, tags=[TEST_TAG] + tags, **kw)


# ---------------------------------------------------------------------------
# world_spawn
# ---------------------------------------------------------------------------

def test_spawn_returns_handle():
    obj = _spawn(name="test-point")
    assert obj.id.startswith("wobj_")
    assert obj.name == "test-point"
    assert obj.x == 0.0 and obj.y == 0.0 and obj.z == 0.0


def test_spawn_with_position_and_shape():
    obj = _spawn(
        name="sphere-test",
        x=1.0, y=2.0, z=3.0,
        shape={"type": "sphere", "radius": 2.5},
    )
    assert obj.x == 1.0
    assert obj.shape["type"] == "sphere"
    assert obj.shape["radius"] == 2.5


def test_spawn_with_fields():
    obj = _spawn(name="field-test", fields={"temp": 300.0, "color": "blue"})
    assert obj.fields["temp"] == 300.0
    assert obj.fields["color"] == "blue"


def test_spawn_handle_is_unique():
    a = _spawn(name="a")
    b = _spawn(name="b")
    assert a.id != b.id


# ---------------------------------------------------------------------------
# world_get
# ---------------------------------------------------------------------------

def test_get_roundtrip():
    orig = _spawn(name="get-test", x=5.0, y=6.0, z=7.0)
    fetched = world_get(orig.id)
    assert fetched.id == orig.id
    assert fetched.x == 5.0


def test_get_missing_raises():
    with pytest.raises(KeyError):
        world_get("wobj_doesnotexist000000000000000000")


# ---------------------------------------------------------------------------
# world_move
# ---------------------------------------------------------------------------

def test_move_partial():
    obj = _spawn(name="mover", x=0.0, y=0.0, z=0.0)
    moved = world_move(obj.id, agent=AGENT, x=99.0)
    assert moved.x == 99.0
    assert moved.y == 0.0  # unchanged
    assert moved.z == 0.0


def test_move_all_axes():
    obj = _spawn(name="full-mover")
    moved = world_move(obj.id, agent=AGENT, x=1.0, y=2.0, z=3.0)
    assert (moved.x, moved.y, moved.z) == (1.0, 2.0, 3.0)


def test_move_missing_raises():
    with pytest.raises(KeyError):
        world_move("wobj_bad000000000000000000000000000", agent=AGENT, x=1.0)


# ---------------------------------------------------------------------------
# world_shape
# ---------------------------------------------------------------------------

def test_shape_replace():
    obj = _spawn(name="shaper", shape={"type": "point"})
    obj = world_shape(obj.id, agent=AGENT, shape={"type": "box", "dx": 1, "dy": 2, "dz": 3})
    assert obj.shape["type"] == "box"
    assert obj.shape["dx"] == 1


# ---------------------------------------------------------------------------
# world_field_set / world_field_get
# ---------------------------------------------------------------------------

def test_field_set_merge():
    obj = _spawn(name="fielder", fields={"a": 1, "b": 2})
    obj = world_field_set(obj.id, agent=AGENT, updates={"b": 99, "c": "new"})
    assert obj.fields["a"] == 1   # preserved
    assert obj.fields["b"] == 99  # updated
    assert obj.fields["c"] == "new"  # added


def test_field_set_delete_with_none():
    obj = _spawn(name="deleter", fields={"keep": "yes", "remove": "no"})
    obj = world_field_set(obj.id, agent=AGENT, updates={"remove": None})
    assert "remove" not in obj.fields
    assert obj.fields["keep"] == "yes"


def test_field_get_single_key():
    obj = _spawn(name="getter", fields={"temp": 42.0})
    val = world_field_get(obj.id, key="temp")
    assert val == 42.0


def test_field_get_all():
    obj = _spawn(name="getter-all", fields={"x": 1, "y": 2})
    all_fields = world_field_get(obj.id)
    assert all_fields["x"] == 1
    assert all_fields["y"] == 2


def test_field_get_missing_key_returns_none():
    obj = _spawn(name="miss")
    assert world_field_get(obj.id, key="nonexistent") is None


# ---------------------------------------------------------------------------
# world_query_region
# ---------------------------------------------------------------------------

def test_query_region_finds_object():
    obj = _spawn(name="in-region", x=50.0, y=50.0, z=50.0)
    results = world_query_region(
        x_min=0, x_max=100, y_min=0, y_max=100, z_min=0, z_max=100,
        tags=[TEST_TAG],
    )
    ids = [o.id for o in results]
    assert obj.id in ids


def test_query_region_excludes_outside():
    _spawn(name="outside", x=200.0, y=200.0, z=200.0)
    results = world_query_region(
        x_min=0, x_max=100, y_min=0, y_max=100, z_min=0, z_max=100,
        tags=[TEST_TAG],
    )
    for o in results:
        assert o.x <= 100.0


def test_query_region_t_filter():
    obj = _spawn(name="old-obj", x=10.0, y=10.0, z=10.0, t=1.0)
    results = world_query_region(
        x_min=0, x_max=50, y_min=0, y_max=50, z_min=0, z_max=50,
        t_min=100.0, tags=[TEST_TAG],
    )
    assert obj.id not in [o.id for o in results]


# ---------------------------------------------------------------------------
# world_tick
# ---------------------------------------------------------------------------

def test_tick_advances_clock():
    before = world_snapshot()
    t0 = before["clock"]["world_t"]
    tick0 = before["clock"]["tick"]
    world_tick(delta=5.0, agent=AGENT)
    after = world_snapshot()
    assert after["clock"]["world_t"] == pytest.approx(t0 + 5.0)
    assert after["clock"]["tick"] == tick0 + 1


# ---------------------------------------------------------------------------
# world_snapshot
# ---------------------------------------------------------------------------

def test_snapshot_returns_clock_and_objects():
    _spawn(name="snap-obj")
    snap = world_snapshot(tags=[TEST_TAG])
    assert "clock" in snap
    assert "objects" in snap
    assert snap["count"] >= 1
    assert snap["clock"]["tick"] >= 0


# ---------------------------------------------------------------------------
# world_despawn
# ---------------------------------------------------------------------------

def test_despawn_removes_object():
    obj = _spawn(name="mortal")
    world_despawn(obj.id, agent=AGENT)
    with pytest.raises(KeyError):
        world_get(obj.id)


def test_despawn_missing_raises():
    with pytest.raises(KeyError):
        world_despawn("wobj_bad000000000000000000000000000", agent=AGENT)


# ---------------------------------------------------------------------------
# MCP server: stdio protocol test
# ---------------------------------------------------------------------------

def test_mcp_initialize_and_snapshot():
    """MCP server must respond to initialize + tools/call over real pipes."""
    messages = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "world_snapshot", "arguments": {"limit": 1}}}),
    ]) + "\n"

    result = subprocess.run(
        [sys.executable, "-u", "-m", "mac.world_mcp", "--agent", AGENT],
        input=messages, capture_output=True, text=True, timeout=15,
    )
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    assert len(lines) == 2, f"Expected 2 JSON lines, got: {result.stdout!r}\nstderr: {result.stderr}"

    r1 = json.loads(lines[0])
    assert r1["id"] == 1
    assert "protocolVersion" in r1["result"]

    r2 = json.loads(lines[1])
    assert r2["id"] == 2
    content = json.loads(r2["result"]["content"][0]["text"])
    assert "clock" in content
    assert "objects" in content


def test_mcp_spawn_and_get():
    """MCP spawn → get round-trip."""
    messages = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "world_spawn",
                               "arguments": {"name": "mcp-test", "x": 7.0, "y": 8.0, "z": 9.0,
                                             "tags": [TEST_TAG]}}}),
    ]) + "\n"

    result = subprocess.run(
        [sys.executable, "-u", "-m", "mac.world_mcp", "--agent", AGENT],
        input=messages, capture_output=True, text=True, timeout=15,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    assert len(lines) == 2, f"stderr: {result.stderr}"
    obj_data = json.loads(json.loads(lines[1])["result"]["content"][0]["text"])
    assert obj_data["name"] == "mcp-test"
    assert obj_data["x"] == 7.0
    assert obj_data["id"].startswith("wobj_")
