"""
world4d_mcp.py — MCP server for the shared 4D world engine.

Exposes World4D as a set of JSON-RPC 2.0 tools over stdio,
following the same pattern as mcp_server.py and soul_mcp.py.

Usage:
    python -m mac.world4d_mcp --agent natasha

Hermes config (mcp_servers.world4d):
    command: /path/to/venv/bin/python
    args: [-m, mac.world4d_mcp, --agent, natasha]
    env:
      WORLD4D_PG_URL: "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"

Tools exposed:
  world_spawn        — create an object, returns handle
  world_despawn      — remove an object by handle
  world_move         — update an object's 4D position
  world_set_shape    — replace an object's shape
  world_patch_meta   — merge-patch object metadata
  world_get          — fetch one object by handle
  world_list         — list objects (optionally filter by agent)
  world_query_region — broad-phase 4D AABB query
  world_add_field    — register a scalar/vector/tensor field
  world_remove_field — remove a field
  world_set_field_value — replace field value data
  world_get_field    — fetch one field by handle
  world_list_fields  — list fields (optionally filter by kind/agent)
  world_fields_at    — fields whose bounding box contains a point
  world_stats        — object + field counts
  world_snapshot     — full serialisable world state
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from mac.world4d import World4D, Vec4, Shape, ShapeKind, FieldKind

# ---------------------------------------------------------------------------
# MCP wire protocol helpers (matches mcp_server.py / soul_mcp.py)
# ---------------------------------------------------------------------------


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


# ---------------------------------------------------------------------------
# Argument coercion helpers
# ---------------------------------------------------------------------------


def _vec4(v: Any) -> Vec4:
    """Accept [x,y,z,w] list or {"x":…,"y":…,"z":…,"w":…} dict."""
    if isinstance(v, (list, tuple)):
        return Vec4(*[float(c) for c in v[:4]])
    if isinstance(v, dict):
        return Vec4(
            float(v.get("x", 0)),
            float(v.get("y", 0)),
            float(v.get("z", 0)),
            float(v.get("w", 0)),
        )
    raise ValueError(f"Cannot coerce to Vec4: {v!r}")


def _shape(s: Any) -> Shape:
    """Accept {"kind": "sphere", "params": {"radius": 1.0}} dict."""
    if not isinstance(s, dict):
        raise ValueError(f"shape must be a dict, got {type(s).__name__}")
    kind = ShapeKind(s.get("kind", "point"))
    params = s.get("params", {})
    return Shape(kind=kind, params=params)


def _field_kind(k: Any) -> FieldKind:
    return FieldKind(k)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "world_spawn",
        "description": "Create a 4D world object. Returns its stable handle string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":   {"type": "string", "description": "Human-readable name"},
                "pos":    {"description": "[x,y,z,w] or {x,y,z,w} 4D position"},
                "shape":  {"description": '{"kind":"sphere","params":{"radius":1}} — kinds: sphere,box,cylinder,hyperplane,hypersphere,mesh,point'},
                "meta":   {"type": "object", "description": "Arbitrary metadata dict"},
                "handle": {"type": "string", "description": "Optional explicit handle (upserts on conflict)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "world_despawn",
        "description": "Remove a world object by handle. Returns true if it existed.",
        "inputSchema": {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    },
    {
        "name": "world_move",
        "description": "Update an object's 4D position.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "pos":    {"description": "[x,y,z,w] or {x,y,z,w}"},
            },
            "required": ["handle", "pos"],
        },
    },
    {
        "name": "world_set_shape",
        "description": "Replace an object's shape.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "shape":  {"description": '{"kind":…,"params":{…}}'},
            },
            "required": ["handle", "shape"],
        },
    },
    {
        "name": "world_patch_meta",
        "description": "Merge-patch (top-level keys) an object's metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "patch":  {"type": "object"},
            },
            "required": ["handle", "patch"],
        },
    },
    {
        "name": "world_get",
        "description": "Fetch one world object by handle.",
        "inputSchema": {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    },
    {
        "name": "world_list",
        "description": "List world objects, optionally filtered by owning agent.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent": {"type": "string", "description": "Filter by agent ID"}},
        },
    },
    {
        "name": "world_query_region",
        "description": "Return objects whose anchor is inside the 4D AABB [lo, hi].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lo": {"description": "[x,y,z,w] min corner"},
                "hi": {"description": "[x,y,z,w] max corner"},
            },
            "required": ["lo", "hi"],
        },
    },
    {
        "name": "world_add_field",
        "description": "Register a scalar/vector/tensor field over a 4D region. Returns handle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":   {"type": "string"},
                "kind":   {"type": "string", "enum": ["scalar", "vector", "tensor"]},
                "origin": {"description": "[x,y,z,w] min corner of bounding box"},
                "extent": {"description": "[x,y,z,w] size of bounding box"},
                "value":  {"type": "object", "description": "Field data or function spec"},
                "meta":   {"type": "object"},
                "handle": {"type": "string", "description": "Optional explicit handle"},
            },
            "required": ["name", "kind"],
        },
    },
    {
        "name": "world_remove_field",
        "description": "Remove a field by handle. Returns true if it existed.",
        "inputSchema": {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    },
    {
        "name": "world_set_field_value",
        "description": "Replace a field's value data entirely.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "value":  {"type": "object"},
            },
            "required": ["handle", "value"],
        },
    },
    {
        "name": "world_get_field",
        "description": "Fetch one field by handle.",
        "inputSchema": {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    },
    {
        "name": "world_list_fields",
        "description": "List fields, optionally filtered by kind and/or agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind":  {"type": "string", "enum": ["scalar", "vector", "tensor"]},
                "agent": {"type": "string"},
            },
        },
    },
    {
        "name": "world_fields_at",
        "description": "Return fields whose bounding box contains the given 4D point.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pos":  {"description": "[x,y,z,w] or {x,y,z,w}"},
                "kind": {"type": "string", "enum": ["scalar", "vector", "tensor"]},
            },
            "required": ["pos"],
        },
    },
    {
        "name": "world_stats",
        "description": "Return object count and field counts by kind.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "world_snapshot",
        "description": "Full serialisable snapshot of all world objects and fields.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _obj_dict(obj: Any) -> dict:
    return {
        "handle":     obj.handle,
        "name":       obj.name,
        "agent":      obj.agent,
        "pos":        obj.pos.to_list(),
        "shape":      {"kind": obj.shape.kind.value, "params": obj.shape.params},
        "meta":       obj.meta,
        "created_at": obj.created_at,
        "updated_at": obj.updated_at,
    }


def _field_dict(f: Any) -> dict:
    return {
        "handle":     f.handle,
        "name":       f.name,
        "agent":      f.agent,
        "kind":       f.kind.value,
        "origin":     f.origin.to_list(),
        "extent":     f.extent.to_list(),
        "value":      f.value,
        "meta":       f.meta,
        "created_at": f.created_at,
        "updated_at": f.updated_at,
    }


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def handle_request(world: World4D, req: dict) -> dict:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    # MCP protocol handshake
    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "world4d", "version": "1.0.0"},
        })

    if method == "notifications/initialized":
        return {}  # no response for notification

    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            result = dispatch_tool(world, tool_name, args)
            return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}]})
        except Exception as exc:
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })

    return _error(req_id, -32601, f"Method not found: {method}")


def dispatch_tool(world: World4D, name: str, args: dict) -> Any:
    if name == "world_spawn":
        pos = _vec4(args["pos"]) if "pos" in args else Vec4()
        shape = _shape(args["shape"]) if "shape" in args else None
        meta = args.get("meta", {})
        handle = args.get("handle")
        h = world.spawn(args["name"], pos=pos, shape=shape, meta=meta, handle=handle)
        return {"handle": h}

    elif name == "world_despawn":
        ok = world.despawn(args["handle"])
        return {"deleted": ok}

    elif name == "world_move":
        world.move(args["handle"], _vec4(args["pos"]))
        return {"ok": True}

    elif name == "world_set_shape":
        world.set_shape(args["handle"], _shape(args["shape"]))
        return {"ok": True}

    elif name == "world_patch_meta":
        world.patch_meta(args["handle"], args["patch"])
        return {"ok": True}

    elif name == "world_get":
        obj = world.get(args["handle"])
        if obj is None:
            return {"object": None}
        return {"object": _obj_dict(obj)}

    elif name == "world_list":
        agent = args.get("agent")
        objs = world.list_objects(agent=agent)
        return {"objects": [_obj_dict(o) for o in objs]}

    elif name == "world_query_region":
        lo = _vec4(args["lo"])
        hi = _vec4(args["hi"])
        objs = world.query_region(lo, hi)
        return {"objects": [_obj_dict(o) for o in objs]}

    elif name == "world_add_field":
        kind = _field_kind(args["kind"])
        origin = _vec4(args["origin"]) if "origin" in args else Vec4()
        extent = _vec4(args["extent"]) if "extent" in args else Vec4(1, 1, 1, 1)
        value = args.get("value", {})
        meta = args.get("meta", {})
        handle = args.get("handle")
        h = world.add_field(args["name"], kind=kind, origin=origin, extent=extent,
                            value=value, meta=meta, handle=handle)
        return {"handle": h}

    elif name == "world_remove_field":
        ok = world.remove_field(args["handle"])
        return {"deleted": ok}

    elif name == "world_set_field_value":
        world.set_field_value(args["handle"], args["value"])
        return {"ok": True}

    elif name == "world_get_field":
        f = world.get_field(args["handle"])
        if f is None:
            return {"field": None}
        return {"field": _field_dict(f)}

    elif name == "world_list_fields":
        kind = _field_kind(args["kind"]) if "kind" in args else None
        agent = args.get("agent")
        fields = world.list_fields(kind=kind, agent=agent)
        return {"fields": [_field_dict(f) for f in fields]}

    elif name == "world_fields_at":
        pt = _vec4(args["pos"])
        kind = _field_kind(args["kind"]) if "kind" in args else None
        fields = world.fields_at(pt, kind=kind)
        return {"fields": [_field_dict(f) for f in fields]}

    elif name == "world_stats":
        return world.stats()

    elif name == "world_snapshot":
        return world.snapshot()

    else:
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="World4D MCP server")
    parser.add_argument("--agent", default="", help="Agent identity tag for new objects")
    parser.add_argument("--pg-url", default="", dest="pg_url",
                        help="Postgres URL (default: WORLD4D_PG_URL env or ~/.pgpass default)")
    args = parser.parse_args()

    pg_url = args.pg_url or os.environ.get("WORLD4D_PG_URL", "")
    world = World4D.connect(pg_url=pg_url, agent=args.agent, apply_schema=False)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _send(_error(None, -32700, f"Parse error: {e}"))
            continue
        resp = handle_request(world, req)
        if resp:  # notifications return {}
            _send(resp)

    world.close()


if __name__ == "__main__":
    main()
