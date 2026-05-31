"""Fleet awareness + inter-agent messaging tool (fleet-01).

Gives a hermes chat session a "group view" of the fleet and a way to talk to
other agents over the agentbus — so the three agents behave as a team (each
keeps its own identity/personality, but knows what the others are doing and can
message them quickly), instead of each session being isolated to itself.

It reaches the mac hub over HTTP using the agent's own env (the same hub URL +
token the worker uses), so it works from the in-process gateway without any new
plumbing. Three actions behind one tool:

  fleet status            → who is on the fleet, their status, and current task
  fleet message <agent>   → send a quick message to another agent (agentbus)
  fleet inbox             → read recent messages addressed to me (agentbus)

All hub I/O is best-effort + read-mostly; the only write is a point-to-point
agentbus message the user/agent explicitly sends.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from tools.registry import registry, tool_error


# ---------------------------------------------------------------------------
# Hub access (env-driven; mirrors the worker/executor resolution)
# ---------------------------------------------------------------------------


def _hub_env() -> Tuple[str, str]:
    base_url = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or "").rstrip("/")
    token = (
        os.environ.get("MAC_WORKER_TOKEN")
        or os.environ.get("MAC_TOKEN")
        or os.environ.get("MAC_API_TOKEN")
        or ""
    )
    return base_url, token


def _self_agent_id() -> str:
    return (
        os.environ.get("MAC_AGENT_ID")
        or os.environ.get("MAC_WORKER_AGENT_ID")
        or ""
    )


def _hub_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, *, timeout: float = 10.0) -> Any:
    base_url, token = _hub_env()
    if not base_url or not token:
        raise RuntimeError("fleet tool needs MAC_HUB_URL/MAC_URL + a hub token in the agent env")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base_url + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer %s" % token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured hub)
        raw = resp.read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text) if text.strip() else None


# ---------------------------------------------------------------------------
# Formatting helpers (pure — unit-testable without a hub)
# ---------------------------------------------------------------------------


def format_fleet_status(agents: List[Dict[str, Any]], tasks: List[Dict[str, Any]]) -> str:
    """Render the fleet roster: who's here, their status, and what they're on."""
    by_owner: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        owner = t.get("owner_agent_id")
        if owner and t.get("state") in ("claimed", "running", "needs_review", "reviewing"):
            by_owner.setdefault(owner, t)
    lines = ["Fleet status (%d agents):" % len(agents)]
    for a in sorted(agents, key=lambda x: x.get("name", "")):
        cur = by_owner.get(a.get("id"))
        doing = ""
        if cur:
            doing = " — working on %s: %s" % (cur.get("id", "?")[:18], (cur.get("title") or "")[:60])
        elif a.get("current_task_id"):
            doing = " — task %s" % str(a.get("current_task_id"))[:18]
        lines.append(
            "- %s [%s/%s]%s"
            % (a.get("name", "?"), a.get("status", "?"), a.get("health_status", "?"), doing)
        )
    return "\n".join(lines)


def format_inbox(chunks: List[Dict[str, Any]], limit: int = 10) -> str:
    if not chunks:
        return "Inbox empty — no recent agentbus messages addressed to you."
    lines = ["Recent agentbus messages (%d):" % min(len(chunks), limit)]
    for c in chunks[-limit:]:
        sender = c.get("sender_agent_id") or c.get("sender") or "?"
        topic = c.get("topic") or "content"
        payload = c.get("payload")
        body = payload if isinstance(payload, str) else json.dumps(payload)[:200]
        lines.append("- from %s [%s]: %s" % (sender, topic, body))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _action_status() -> str:
    agents = _hub_request("GET", "/agents") or []
    try:
        tasks = _hub_request("GET", "/tasks?%s" % urlencode({"state": "running"})) or []
    except Exception:
        tasks = []
    if not isinstance(agents, list):
        agents = []
    if not isinstance(tasks, list):
        tasks = []
    return format_fleet_status(agents, tasks)


def _action_message(recipient: str, message: str) -> str:
    me = _self_agent_id()
    if not me:
        return tool_error("cannot determine my own agent id (MAC_AGENT_ID unset)")
    if not recipient:
        return tool_error("fleet message requires a recipient agent (name or id)")
    if not message:
        return tool_error("fleet message requires a non-empty message")
    # Resolve a recipient name → agent id if needed.
    recipient_id = recipient
    try:
        agents = _hub_request("GET", "/agents") or []
        match = next((a for a in agents if a.get("name") == recipient or a.get("id") == recipient), None)
        if match:
            recipient_id = match.get("id")
    except Exception:
        pass
    result = _hub_request(
        "POST",
        "/agentbus",
        {
            "sender_agent_id": me,
            "recipient_agent_id": recipient_id,
            "topic": "chat",
            "content_type": "text/plain",
            "payload": message,
            "payload_encoding": "json",
        },
    )
    return json.dumps({"success": True, "delivered_to": recipient_id, "result": result})


def _action_inbox(limit: int) -> str:
    me = _self_agent_id()
    if not me:
        return tool_error("cannot determine my own agent id (MAC_AGENT_ID unset)")
    streams = _hub_request("GET", "/agentbus/streams?%s" % urlencode({"agent_id": me, "limit": 50})) or []
    chunks: List[Dict[str, Any]] = []
    if isinstance(streams, list):
        for s in streams:
            sid = s.get("id")
            if not sid:
                continue
            try:
                got = _hub_request(
                    "GET",
                    "/agentbus/streams/%s/chunks?%s" % (quote(str(sid), safe=""), urlencode({"agent_id": me, "limit": limit})),
                ) or []
                if isinstance(got, list):
                    chunks.extend(got)
            except Exception:
                continue
    chunks.sort(key=lambda c: str(c.get("created_at") or c.get("sequence") or ""))
    return format_inbox(chunks, limit=limit)


FLEET_SCHEMA = {
    "name": "fleet",
    "description": (
        "See what the rest of the agent fleet is doing and message other agents over "
        "the agentbus. Use this to coordinate as a team: check who is online and what "
        "they are working on (status), send another agent a quick message (message), or "
        "read messages addressed to you (inbox). You keep your own identity — this is "
        "for group awareness and quick agent-to-agent coordination."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "message", "inbox"],
                "description": "status = fleet roster + what each agent is working on; "
                "message = send a message to another agent; inbox = read your recent messages",
            },
            "recipient": {"type": "string", "description": "for action=message: the agent name or id to send to"},
            "message": {"type": "string", "description": "for action=message: the message text"},
            "limit": {"type": "integer", "description": "for action=inbox: how many recent messages (default 10)"},
        },
        "required": ["action"],
    },
}


def _handle_fleet(args, **kw):
    action = str(args.get("action") or "").strip().lower()
    try:
        if action == "status":
            return _action_status()
        if action == "message":
            return _action_message(str(args.get("recipient") or "").strip(), str(args.get("message") or "").strip())
        if action == "inbox":
            try:
                limit = int(args.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            return _action_inbox(max(1, min(50, limit)))
        return tool_error("unknown fleet action %r (use status/message/inbox)" % action)
    except RuntimeError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error("fleet tool error: %s" % exc)


def check_fleet_requirements() -> bool:
    """Available when the agent has hub access (a hub URL + token in env)."""
    base_url, token = _hub_env()
    return bool(base_url and token)


registry.register(
    name="fleet",
    toolset="fleet",
    schema=FLEET_SCHEMA,
    handler=_handle_fleet,
    check_fn=check_fleet_requirements,
    requires_env=[],
    is_async=False,
    emoji="🛰️",
)
