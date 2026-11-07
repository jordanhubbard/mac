"""MCP server: shared 4D world environment for the MAC fleet.

Exposes world.py as a set of MCP tools.  Run as:

    python -m mac.world_mcp [--db-url DSN] [--agent AGENT_ID]

or via stdio (Hermes config):

    {
      "command": "python",
      "args": ["-m", "mac.world_mcp", "--agent", "bullwinkle"]
    }

Tools
-----
world_spawn         Spawn a new object; returns {handle, ...object}
world_move          Move object to new (x,y,z,t); partial updates ok
world_shape         Replace shape descriptor
world_field_set     Merge field values (None deletes a key)
world_field_get     Get one or all fields for an object
world_query_region  Spatial 4D range query; returns list of objects
world_tick          Advance the global world clock
world_snapshot      Full world dump (clock + all objects)
world_despawn       Remove an object
world_get           Fetch a single object by handle
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from mac import world as W

# ---------------------------------------------------------------------------
# MCP wire protocol helpers
# ---------------------------------------------------------------------------

def _ok(result) -> dict:
    return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}}


def _err(id_, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


TOOLS = [
    {
        "name": "world_spawn",
        "description": "Spawn a new 4D object in the shared fleet world. Returns the object with its stable handle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":   {"type": "string", "description": "Human label for the object"},
                "x":      {"type": "number", "default": 0.0},
                "y":      {"type": "number", "default": 0.0},
                "z":      {"type": "number", "default": 0.0},
                "t":      {"type": "number", "description": "World-time (default: current world_t)"},
                "shape":  {"type": "object", "description": "Shape descriptor e.g. {type:'box',dx:1,dy:1,dz:1}"},
                "fields": {"type": "object", "description": "Initial named field values"},
                "tags":   {"type": "array",  "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "world_move",
        "description": "Move an object to a new position. Provide only the axes you want to change.",
        "inputSchema": {
            "type": "object",
            "required": ["handle"],
            "properties": {
                "handle": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "z": {"type": "number"}, "t": {"type": "number"},
            },
        },
    },
    {
        "name": "world_shape",
        "description": "Replace the shape descriptor of an object.",
        "inputSchema": {
            "type": "object",
            "required": ["handle", "shape"],
            "properties": {
                "handle": {"type": "string"},
                "shape":  {"type": "object"},
            },
        },
    },
    {
        "name": "world_field_set",
        "description": "Merge field values into an object. Set a key to null to delete it.",
        "inputSchema": {
            "type": "object",
            "required": ["handle", "updates"],
            "properties": {
                "handle":  {"type": "string"},
                "updates": {"type": "object"},
            },
        },
    },
    {
        "name": "world_field_get",
        "description": "Get one field value (key given) or all fields (key omitted).",
        "inputSchema": {
            "type": "object",
            "required": ["handle"],
            "properties": {
                "handle": {"type": "string"},
                "key":    {"type": "string"},
            },
        },
    },
    {
        "name": "world_query_region",
        "description": "Return objects within a 4D bounding volume.",
        "inputSchema": {
            "type": "object",
            "required": ["x_min", "x_max", "y_min", "y_max", "z_min", "z_max"],
            "properties": {
                "x_min": {"type": "number"}, "x_max": {"type": "number"},
                "y_min": {"type": "number"}, "y_max": {"type": "number"},
                "z_min": {"type": "number"}, "z_max": {"type": "number"},
                "t_min": {"type": "number"}, "t_max": {"type": "number"},
                "tags":  {"type": "array", "items": {"type": "string"}},
                "agent": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "world_tick",
        "description": "Advance the global world clock. Returns {tick, world_t, updated_at}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "delta": {"type": "number", "default": 1.0, "description": "Amount to advance world_t"},
            },
        },
    },
    {
        "name": "world_snapshot",
        "description": "Return world clock and all (or filtered) objects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit":  {"type": "integer", "default": 500},
                "agent":  {"type": "string"},
                "tags":   {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "world_despawn",
        "description": "Remove an object from the world.",
        "inputSchema": {
            "type": "object",
            "required": ["handle"],
            "properties": {"handle": {"type": "string"}},
        },
    },
    {
        "name": "world_get",
        "description": "Fetch a single object by handle.",
        "inputSchema": {
            "type": "object",
            "required": ["handle"],
            "properties": {"handle": {"type": "string"}},
        },
    },
]


# ---------------------------------------------------------------------------
# Request dispatch
# ---------------------------------------------------------------------------

def _dispatch(method: str, params: dict, agent: str, dsn: str | None) -> object:
    p = params or {}
    kw = {"dsn": dsn}

    if method == "world_spawn":
        obj = W.world_spawn(agent=agent, **{k: p[k] for k in ("name","x","y","z","t","shape","fields","tags") if k in p}, **kw)
        return obj.to_dict()

    if method == "world_move":
        obj = W.world_move(handle=p["handle"], agent=agent,
                           x=p.get("x"), y=p.get("y"), z=p.get("z"), t=p.get("t"), **kw)
        return obj.to_dict()

    if method == "world_shape":
        obj = W.world_shape(handle=p["handle"], agent=agent, shape=p["shape"], **kw)
        return obj.to_dict()

    if method == "world_field_set":
        obj = W.world_field_set(handle=p["handle"], agent=agent, updates=p["updates"], **kw)
        return obj.to_dict()

    if method == "world_field_get":
        return W.world_field_get(handle=p["handle"], key=p.get("key"), **kw)

    if method == "world_query_region":
        objects = W.world_query_region(
            x_min=p["x_min"], x_max=p["x_max"],
            y_min=p["y_min"], y_max=p["y_max"],
            z_min=p["z_min"], z_max=p["z_max"],
            t_min=p.get("t_min"), t_max=p.get("t_max"),
            tags=p.get("tags"), agent=p.get("agent"),
            limit=p.get("limit", 100), **kw,
        )
        return [o.to_dict() for o in objects]

    if method == "world_tick":
        return W.world_tick(delta=p.get("delta", 1.0), agent=agent, **kw)

    if method == "world_snapshot":
        return W.world_snapshot(limit=p.get("limit", 500), agent=p.get("agent"),
                                tags=p.get("tags"), **kw)

    if method == "world_despawn":
        return W.world_despawn(handle=p["handle"], agent=agent, **kw)

    if method == "world_get":
        return W.world_get(handle=p["handle"], **kw)

    raise ValueError(f"Unknown tool: {method}")


# ---------------------------------------------------------------------------
# MCP stdio server
# ---------------------------------------------------------------------------

def serve(agent: str = "bullwinkle", dsn: str | None = None) -> None:
    sys.stdout.reconfigure(line_buffering=True)
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": str(exc)}}), flush=True)
            continue

        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        # MCP lifecycle
        if method == "initialize":
            print(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "world_mcp", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                },
            }), flush=True)
            continue

        if method == "initialized":
            continue

        if method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}), flush=True)
            continue

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            try:
                result = _dispatch(tool_name, tool_args, agent=agent, dsn=dsn)
                print(json.dumps(_ok(result) | {"id": req_id}), flush=True)
            except KeyError as exc:
                print(json.dumps(_err(req_id, -32602, f"Not found: {exc}"), flush=True))
            except Exception as exc:
                print(json.dumps(_err(req_id, -32603, traceback.format_exc(), flush=True)), flush=True)
            continue

        # Unknown method
        print(json.dumps(_err(req_id, -32601, f"Method not found: {method}"), flush=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared 4D world MCP server")
    parser.add_argument("--agent", default="bullwinkle", help="Agent identity for writes")
    parser.add_argument("--db-url", default=None, help="PostgreSQL DSN override")
    args = parser.parse_args()
    serve(agent=args.agent, dsn=args.db_url)


if __name__ == "__main__":
    main()
