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
  fleet publish           → artifact publish info + CRUD records (agentbus)

This module ALSO registers a companion ``tasks`` tool (tasks-01) on the same hub
plumbing. That one is the chat agent's read/write window onto the SHARED hub task
ledger — the same durable store the autonomous worker and the ``mac task`` CLI
use. It exists because the only other task tool a chat agent has is ``todo``,
which is in-memory and per-session: when one agent "filed tasks for itself" via
``todo`` the rest of the fleet couldn't see them. ``tasks`` fixes that — work
filed/claimed/closed there is visible fleet-wide.

The fleet actions are best-effort; writes are limited to agentbus messages and
publish-record CRUD. The ``tasks`` tool deliberately writes to the shared ledger.
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


def _publish_info() -> Dict[str, Any]:
    return {
        "schema": "mac.hermes.publish_info.v1",
        "method": os.environ.get("MAC_PUBLISH_METHOD") or "hub_directory_http",
        "publish_dir": os.environ.get("MAC_PUBLISH_DIR") or os.environ.get("MAC_WEBDAV_ROOT") or "",
        "public_url": (
            os.environ.get("MAC_PUBLISH_PUBLIC_URL")
            or os.environ.get("MAC_PUBLISH_WEBDAV_URL")
            or os.environ.get("MAC_WEBDAV_PUBLIC_URL")
            or ""
        ),
        "agentbus_crud": "/agentbus/artifact-publish",
        "http_ingress": False,
    }


def _metadata_arg(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("metadata must be a JSON object: %s" % exc) from exc
        if isinstance(loaded, dict):
            return loaded
        raise ValueError("metadata must be a JSON object")
    return {}


def _action_publish(args: Dict[str, Any]) -> str:
    me = _self_agent_id()
    if not me:
        return tool_error("cannot determine my own agent id (MAC_AGENT_ID unset)")
    operation = str(args.get("operation") or "info").strip().lower()
    if operation == "info":
        return json.dumps(_publish_info(), sort_keys=True)
    payload: Dict[str, Any] = {
        "sender_agent_id": me,
        "operation": operation,
        "artifact_id": args.get("artifact") or None,
        "digest": args.get("digest") or None,
        "kind": args.get("kind") or "public-artifact",
        "uri": args.get("uri") or None,
        "public_url": args.get("public_url") or None,
        "path": args.get("path") or None,
        "publish_dir": args.get("publish_dir") or _publish_info().get("publish_dir") or None,
        "task_id": args.get("task_id") or None,
        "request_id": args.get("request_id") or None,
        "metadata": _metadata_arg(args.get("metadata")),
        "all_agents": bool(args.get("all_agents")),
    }
    recipients = args.get("recipient_agent_ids") or args.get("recipient_agent_id") or args.get("recipient")
    if isinstance(recipients, str):
        payload["recipient_agent_ids"] = [item.strip() for item in recipients.split(",") if item.strip()]
    elif isinstance(recipients, list):
        payload["recipient_agent_ids"] = [str(item).strip() for item in recipients if str(item).strip()]
    result = _hub_request("POST", "/agentbus/artifact-publish", payload)
    return json.dumps(result, sort_keys=True)


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
                "enum": ["status", "message", "inbox", "publish"],
                "description": "status = fleet roster + what each agent is working on; "
                "message = send a message to another agent; inbox = read your recent messages; "
                "publish = show publish directory info or run artifact publish CRUD",
            },
            "recipient": {"type": "string", "description": "for action=message: the agent name or id to send to"},
            "recipient_agent_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "for action=publish: recipient agent ids to notify",
            },
            "message": {"type": "string", "description": "for action=message: the message text"},
            "limit": {"type": "integer", "description": "for action=inbox: how many recent messages (default 10)"},
            "operation": {
                "type": "string",
                "enum": ["info", "create", "upsert", "update", "get", "read", "list", "delete"],
                "description": "for action=publish: info or artifact CRUD operation",
            },
            "artifact": {"type": "string", "description": "for action=publish get/delete: artifact id"},
            "digest": {"type": "string", "description": "for action=publish upsert/get/delete: sha256 digest"},
            "kind": {"type": "string", "description": "for action=publish: artifact kind"},
            "uri": {"type": "string", "description": "for action=publish upsert: canonical artifact URI"},
            "public_url": {"type": "string", "description": "for action=publish upsert: public read URL"},
            "path": {"type": "string", "description": "for action=publish: path under MAC_PUBLISH_DIR"},
            "publish_dir": {"type": "string", "description": "for action=publish: override publish directory metadata"},
            "task_id": {"type": "string", "description": "for action=publish: source task id"},
            "request_id": {"type": "string", "description": "for action=publish: idempotency/correlation id"},
            "metadata": {"type": "object", "description": "for action=publish: artifact metadata"},
            "all_agents": {"type": "boolean", "description": "for action=publish: notify every agent"},
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
        if action == "publish":
            return _action_publish(args)
        return tool_error("unknown fleet action %r (use status/message/inbox/publish)" % action)
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


# ---------------------------------------------------------------------------
# Shared task ledger (tasks-01)
# ---------------------------------------------------------------------------
# The chat agent's window onto the SAME hub task store the autonomous worker
# and the `mac task` CLI use. Unlike `todo` (in-memory, per-session, invisible
# to others), everything here is durable and visible fleet-wide — so when one
# agent files or claims work, the rest of the fleet sees it. Reuses the fleet
# tool's _hub_request / _self_agent_id (same hub URL + token in the agent env).


def format_task_list(tasks: List[Dict[str, Any]], limit: int = 40) -> str:
    """Render the shared backlog compactly (id / state / title / owner)."""
    if not tasks:
        return "No tasks on the shared ledger."
    shown = tasks[:limit]
    header = "Shared fleet tasks (%d shown%s):" % (
        len(shown), "" if len(tasks) <= limit else " of %d" % len(tasks))
    lines = [header]
    for t in shown:
        lines.append(
            "- %s [%s] %s (owner: %s)"
            % (
                str(t.get("id") or "?")[:18],
                t.get("state", "?"),
                (t.get("title") or "")[:70],
                t.get("owner_agent_id") or "-",
            )
        )
    return "\n".join(lines)


def _action_tasks_list(state: str) -> str:
    path = "/tasks"
    if state:
        path += "?%s" % urlencode({"state": state})
    tasks = _hub_request("GET", path) or []
    if not isinstance(tasks, list):
        tasks = []
    return format_task_list(tasks)


def _action_tasks_create(title: str, description: str, priority: int) -> str:
    if not title:
        return tool_error("tasks create requires a title")
    me = _self_agent_id()
    payload = {
        "title": title,
        "description": description or "",
        "priority": int(priority or 0),
        "actor": me or "agent",
    }
    t = _hub_request("POST", "/tasks", payload) or {}
    return json.dumps({
        "success": True,
        "task_id": t.get("id"),
        "state": t.get("state"),
        "title": t.get("title"),
        "note": "Filed on the shared fleet ledger — visible to every agent.",
    })


def _action_tasks_claim(task_id: str) -> str:
    if not task_id:
        return tool_error("tasks claim requires a task_id")
    me = _self_agent_id()
    if not me:
        return tool_error("cannot determine my own agent id (MAC_AGENT_ID unset)")
    res = _hub_request(
        "POST",
        "/tasks/%s/claim?%s" % (quote(task_id, safe=""), urlencode({"agent_id": me})),
    ) or {}
    task = res.get("task") if isinstance(res, dict) else None
    return json.dumps({
        "success": True,
        "claimed": task_id,
        "owner": me,
        "state": (task or {}).get("state"),
    })


def _action_tasks_close(task_id: str, cancelled: bool, reason: str) -> str:
    if not task_id:
        return tool_error("tasks close requires a task_id")
    me = _self_agent_id()
    target = "cancelled" if cancelled else "completed"
    payload = {
        "target_state": target,
        "actor": me or "agent",
        "detail": {"reason": reason} if reason else {},
    }
    res = _hub_request("POST", "/tasks/%s/transition" % quote(task_id, safe=""), payload) or {}
    return json.dumps({
        "success": True,
        "task_id": task_id,
        "state": res.get("state") or target,
    })


def _action_tasks_show(task_id: str) -> str:
    if not task_id:
        return tool_error("tasks show requires a task_id")
    return json.dumps(_hub_request("GET", "/tasks/%s" % quote(task_id, safe="")) or {})


TASKS_SCHEMA = {
    "name": "tasks",
    "description": (
        "The SHARED, fleet-wide task ledger — the same durable store the rest of the "
        "fleet (and the autonomous worker) sees. Use this, NOT the session-local `todo` "
        "tool, for any real work that should be tracked, handed off, or visible to other "
        "agents. Anything you file here, the whole fleet can see and pick up.\n\n"
        "Actions:\n"
        "- list:   the shared backlog (optionally filter by state: open / claimed / "
        "running / needs_review / completed / cancelled)\n"
        "- create: file a new task (title required) — returns its task_id\n"
        "- claim:  take ownership of a task so others know you're on it\n"
        "- close:  finish a task (completed, or cancelled=true to abandon) with an "
        "optional reason\n"
        "- show:   full detail of one task\n\n"
        "Reserve the `todo` tool for private, within-this-conversation scratch steps that "
        "nobody else needs to see."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "claim", "close", "show"],
                "description": "what to do against the shared ledger",
            },
            "title": {"type": "string", "description": "for action=create: the task title"},
            "description": {"type": "string", "description": "for action=create: longer detail (optional)"},
            "priority": {"type": "integer", "description": "for action=create: higher = more urgent (default 0)"},
            "task_id": {"type": "string", "description": "for claim/close/show: the task id"},
            "state": {"type": "string", "description": "for action=list: optional state filter"},
            "cancelled": {"type": "boolean", "description": "for action=close: true to cancel/abandon instead of completing (default false)"},
            "reason": {"type": "string", "description": "for action=close: short reason / outcome note"},
        },
        "required": ["action"],
    },
}


def _handle_tasks(args, **kw):
    action = str(args.get("action") or "").strip().lower()
    try:
        if action == "list":
            return _action_tasks_list(str(args.get("state") or "").strip())
        if action == "create":
            return _action_tasks_create(
                str(args.get("title") or "").strip(),
                str(args.get("description") or "").strip(),
                args.get("priority") or 0,
            )
        if action == "claim":
            return _action_tasks_claim(str(args.get("task_id") or "").strip())
        if action == "close":
            return _action_tasks_close(
                str(args.get("task_id") or "").strip(),
                bool(args.get("cancelled")),
                str(args.get("reason") or "").strip(),
            )
        if action == "show":
            return _action_tasks_show(str(args.get("task_id") or "").strip())
        return tool_error("unknown tasks action %r (use list/create/claim/close/show)" % action)
    except RuntimeError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error("tasks tool error: %s" % exc)


registry.register(
    name="tasks",
    toolset="fleet",
    schema=TASKS_SCHEMA,
    handler=_handle_tasks,
    check_fn=check_fleet_requirements,
    requires_env=[],
    is_async=False,
    emoji="🗂️",
)
