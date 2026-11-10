"""Standalone MCP adapter for the pinned runtime used by world4d_service.

Run on the simulation host, locally or over SSH stdio. Database credentials
remain in the service's private environment file. Initialization is read-only;
only explicit tool calls check an agent in or change the shared world.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid

try:
    from .world4d_presence import project_fields
except ImportError:  # Installed standalone alongside the shared helper.
    from world4d_presence import project_fields


IDENTITY = re.compile(r"agent_[A-Za-z0-9_.-]{1,100}\Z")
STATUSES = ["online", "idle", "working", "reviewing", "offline"]


class InputError(ValueError):
    """Safe, caller-facing validation detail, never a backend exception."""


def tool(name, description, properties=None, required=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool("world4d_observe", "Read the shared simulation and peer avatars; makes no writes."),
    tool(
        "world4d_checkin",
        "Refresh your own avatar's real presence, status and optional note.",
        {"status": {"type": "string", "enum": STATUSES}, "note": {"type": "string"}},
    ),
    tool(
        "world4d_move",
        "Move your own avatar to a finite four-dimensional position.",
        {"position": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
        ["position"],
    ),
    tool(
        "world4d_set_status",
        "Publish your current status, task or note on your own avatar.",
        {
            "status": {"type": "string", "enum": STATUSES},
            "current_task": {"type": "string"},
            "note": {"type": "string"},
        },
    ),
    tool(
        "world4d_send_signal",
        "Leave a persistent signal addressed to a peer; your sender identity is fixed.",
        {"to_agent": {"type": "string"}, "payload": {"type": "object"}},
        ["to_agent", "payload"],
    ),
    tool(
        "world4d_receive_signals",
        "Read signals addressed to you; explicitly set consume=true to acknowledge them.",
        {"consume": {"type": "boolean", "default": False}},
    ),
]


def serialize(obj):
    result = asdict(obj)
    result["fields"] = project_fields(result["fields"])
    return result


class WorldTools:
    def __init__(self, factory, agent: str, world: str, realm_id=None):
        if not IDENTITY.fullmatch(agent):
            raise InputError("agent must be a stable agent_ identifier")
        self.factory, self.agent, self.world = factory, agent, world
        self.presence = None
        self.realm_id = realm_id

    def identity(self):
        return {"realm_id": self.realm_id, "world": self.world, "backend": "prototype"}

    def runtime_presence(self, args):
        # Private management RPC, deliberately absent from the model tool list.
        if set(args) != {"realm_id", "status", "observed_at"} or not self.realm_id:
            raise InputError("invalid runtime presence proof")
        if args["realm_id"] != self.realm_id or args["status"] not in {
            "online",
            "offline",
            "unknown",
        }:
            raise InputError("runtime presence realm or status mismatch")
        try:
            observed = datetime.fromisoformat(args["observed_at"])
            now = datetime.now(timezone.utc)
            if abs((now - observed).total_seconds()) > 60:
                raise ValueError("expired proof")
        except (ValueError, TypeError):
            raise InputError("runtime presence proof must be fresh") from None
        p = self.factory(self.agent, world=self.world)
        p.w.actor = "mac_world4d_presence"
        if p.obj() is None:
            if args["status"] != "online":
                return {**self.identity(), "status": args["status"], "updated": False}
            p.ensure()
        updates = dict(
            runtime_status=args["status"],
            runtime_observed_at=now.isoformat(),
            runtime_valid_until=(now + timedelta(seconds=420)).isoformat(),
            presence_source="gateway_health",
            status="online" if args["status"] == "online" else "offline",
        )
        if args["status"] == "online":
            updates.update(last_seen=now.isoformat(), runtime_last_seen=now.isoformat())
        p.w.set_fields(p.handle, **updates)
        return {**self.identity(), "status": args["status"], "updated": True}

    def call(self, name: str, args: dict):
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None:
            raise InputError("unknown World4D tool")
        schema = spec["inputSchema"]
        if not isinstance(args, dict) or set(args) - schema["properties"].keys():
            raise InputError("unexpected tool arguments")
        if any(k not in args for k in schema["required"]):
            raise InputError("missing required tool argument")
        for key, value in args.items():
            kind = schema["properties"][key]["type"]
            expected = {"string": str, "object": dict, "array": list, "boolean": bool}[kind]
            if not isinstance(value, expected):
                raise InputError(f"invalid {key} type")
            if isinstance(value, str) and len(value) > 2000:
                raise InputError(f"{key} is too long")
        if "status" in args and args["status"] not in STATUSES:
            raise InputError("invalid status")
        if "position" in args:
            pos = args["position"]
            if len(pos) != 4 or not all(
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                and abs(v) <= 1e6
                for v in pos
            ):
                raise InputError("position must contain four finite bounded numbers")
        if "to_agent" in args and not IDENTITY.fullmatch(args["to_agent"]):
            raise InputError("invalid recipient identity")
        if "payload" in args:
            try:
                encoded = json.dumps(args["payload"], allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise InputError("signal payload must be valid JSON") from exc
            if len(encoded) > 8000:
                raise InputError("signal payload is too large")

        if self.presence is None:
            self.presence = self.factory(self.agent, world=self.world)
        p = self.presence
        if name == "world4d_observe":
            return {
                "world": self.world,
                "self": self.agent,
                "objects": [serialize(o) for o in p.w.list(limit=200)],
            }
        if name == "world4d_receive_signals":
            return p.recv_signals(consume=args.get("consume", False))

        if name == "world4d_checkin" or p.obj() is None:
            p.ensure()
        else:
            p.w.set_fields(p.handle, last_seen=datetime.now(timezone.utc).isoformat())
        p.w.set_fields(p.handle, model_last_seen=datetime.now(timezone.utc).isoformat())
        if name == "world4d_checkin":
            updates = {"status": args.get("status", "online")}
            updates["model_status"] = updates["status"]
            if "note" in args:
                updates["note"] = args["note"]
            p.w.set_fields(p.handle, **updates)
        elif name == "world4d_set_status":
            p.w.set_fields(
                p.handle, **args, **({"model_status": args["status"]} if "status" in args else {})
            )
        elif name == "world4d_move":
            p.move(tuple(args["position"]))
        elif name == "world4d_send_signal":
            # The prototype helper uses second-resolution handles. UUIDs let
            # independent sessions signal the same peer without collisions.
            handle = "sig_" + uuid.uuid4().hex
            me = p.obj()
            p.w.create(handle, kind="signal", pos=tuple(me.pos))
            p.w.set_shape(handle, "point")
            p.w.set_fields(
                handle,
                from_agent=self.agent,
                to_agent=args["to_agent"],
                payload=args["payload"],
                ttl=100,
                consumed=False,
                sent_at=datetime.now(timezone.utc).isoformat(),
                source="world4d_mcp",
            )
            return {"handle": handle, "from_agent": self.agent, "to_agent": args["to_agent"]}
        return serialize(p.obj())


def serve(tools: WorldTools, reader=None, writer=None) -> None:
    reader, writer = reader or sys.stdin, writer or sys.stdout
    for line in reader:
        try:
            request = json.loads(line)
        except ValueError:
            writer.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Invalid JSON"},
                    }
                )
                + "\n"
            )
            writer.flush()
            continue
        if not isinstance(request, dict):
            request = {"id": None, "method": None}
        if "id" not in request:
            continue  # Notifications never perform actions or receive replies.
        response = {"jsonrpc": "2.0", "id": request["id"]}
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            response["error"] = {"code": -32602, "message": "params must be an object"}
        elif method == "initialize":
            version = params.get("protocolVersion", "2024-11-05")
            response["result"] = {
                "protocolVersion": version,
                "serverInfo": {"name": "world4d_agent", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            }
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method in {"mac/world4d_identity", "mac/world4d_runtime_presence"}:
            try:
                response["result"] = (
                    tools.identity()
                    if method.endswith("identity")
                    else tools.runtime_presence(params)
                )
            except Exception as exc:
                response["error"] = {
                    "code": -32602,
                    "message": str(exc)
                    if isinstance(exc, InputError)
                    else "World4D backend request failed",
                }
        elif method == "tools/call":
            try:
                result = tools.call(params.get("name"), params.get("arguments", {}))
                response["result"] = {"content": [{"type": "text", "text": json.dumps(result)}]}
            except Exception as exc:
                # DB/SDK exceptions can include connection strings. Preserve
                # validation detail, but keep backend credentials off the wire.
                message = (
                    str(exc) if isinstance(exc, InputError) else "World4D backend request failed"
                )
                response["result"] = {
                    "isError": True,
                    "content": [{"type": "text", "text": message}],
                }
        else:
            response["error"] = {"code": -32601, "message": "Unknown method"}
        writer.write(json.dumps(response) + "\n")
        writer.flush()


def load_tools(path: Path, agent: str, world: str) -> WorldTools:
    config = json.loads(path.read_text())
    if world not in {w["name"] for w in config["worlds"]}:
        raise ValueError("world is not configured in the supervised runtime")
    environment = Path(config["environment_file"])
    if environment.stat().st_mode & 0o077:
        raise ValueError("environment JSON must have mode 0600")
    env = json.loads(environment.read_text())
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ValueError("environment must map names to strings")
    os.environ.update(env)
    os.environ.update(PGCONNECT_TIMEOUT="5", PGOPTIONS="-c statement_timeout=10000")
    sys.path.insert(0, config["source"])
    factory = importlib.import_module("world4d_agents").AgentPresence
    return WorldTools(factory, agent, world, config.get("realm_id"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--world", default="fleet")
    args = parser.parse_args()
    serve(load_tools(args.config, args.agent, args.world))


if __name__ == "__main__":
    main()
