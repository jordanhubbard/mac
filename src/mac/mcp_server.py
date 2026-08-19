"""An MCP server exposing the mac ledger to coding agents.

WHY THIS IS A CLIENT AND NOT A SECOND IMPLEMENTATION. Every tool here goes
through :class:`mac.dispatch.RemoteDispatch` -- the same seam ``cli.py`` uses --
so the routes it calls are the routes the CLI calls, and
``tests/test_dispatch_route_contract.py`` proves both against the live FastAPI
route table. A tool surface that spoke to the hub its own way would be a third
thing to drift: #418 was the CLI and the API disagreeing about one query
parameter, with nothing comparing them.

WHY TOOLS RATHER THAN A SHELL. A coding agent driving `mac` today parses a
table. Nearly every CLI defect this fleet has produced lived in that seam -- a
LANE column that could only ever print one value, short ids that 500'd
`mac task release`, `--all-states` failing when `--json` preceded it, a
DEPENDENCIES column that starved every title. A typed tool call has none of
those failure modes.

WHY IT IS A ``mac`` SUBCOMMAND. The sandbox notes in ``executor_sandbox`` say
MCP wiring is unconfined-only because "the host config path + host MCP-server
interpreter do not reliably resolve inside the sandbox". A server launched as
``mac admin mcp serve`` has no interpreter path to resolve: it runs wherever
the CLI already runs.

The protocol is JSON-RPC 2.0 over stdio -- ``initialize``, ``tools/list``,
``tools/call`` -- implemented directly rather than pulled in, because that is
about a hundred lines and a dependency is a thing to keep current.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO

JsonDict = Dict[str, Any]

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mac"


def _text(payload: Any) -> JsonDict:
    """An MCP tool result carrying JSON as text.

    Tools return JSON rather than the CLI's table rendering on purpose: the
    rendering is for eyes, and a model re-parsing it is exactly the seam this
    server exists to remove.
    """
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True, default=str)}
        ]
    }


def _error(message: str) -> JsonDict:
    """A tool-level failure.

    ``isError`` rather than a JSON-RPC error: the call was well-formed and the
    model should see what went wrong and adapt, not have the transport report a
    protocol fault.
    """
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _as_dicts(rows: Any) -> List[JsonDict]:
    out = []
    for row in rows or []:
        to_dict = getattr(row, "to_dict", None)
        out.append(to_dict() if callable(to_dict) else dict(row))
    return out


def _one(record: Any) -> Any:
    to_dict = getattr(record, "to_dict", None)
    return to_dict() if callable(to_dict) else record


class MacTools:
    """The tool surface, bound to a dispatch plane.

    Deliberately small. The retired Hermes plugin exposed eight tools including
    notification acknowledgement, which is a persona concern; a coding agent
    working a task needs to see its task, find work, and file what it learned.
    """

    def __init__(self, plane: Any) -> None:
        self._plane = plane

    def descriptors(self) -> List[JsonDict]:
        return [
            {
                "name": "mac_task_show",
                "description": (
                    "Show one task: state, project, dependencies, and metadata. "
                    "Accepts an abbreviated id (task_1a2b3c4d)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
            {
                "name": "mac_task_list",
                "description": (
                    "List tasks. Defaults to active work only; pass all_states "
                    "to include completed, failed and cancelled."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "state": {"type": "string"},
                        "all_states": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "mac_task_ready",
                "description": (
                    "Tasks that are open, unclaimed, and have no unfinished "
                    "dependencies -- what the fleet could actually pick up now."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "mac_task_create",
                "description": (
                    "File a task. Use this to record follow-up work rather than "
                    "leaving a TODO in the code."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "project": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
        ]

    # -- implementations ----------------------------------------------------

    def mac_task_show(self, task_id: str = "", **_: Any) -> JsonDict:
        if not str(task_id or "").strip():
            return _error("task_id is required")
        return _text(_one(self._plane.get_task(str(task_id).strip())))

    def mac_task_list(
        self,
        project: Optional[str] = None,
        state: Optional[str] = None,
        all_states: bool = False,
        limit: Optional[int] = None,
        **_: Any,
    ) -> JsonDict:
        from mac.models import ACTIVE_TASK_STATES

        # Mirrors the CLI's default (#407): active work unless asked otherwise.
        # A tool that returned 3,500 cancelled tasks by default would be the
        # same unusable view the CLI had before that change.
        selector: Any = state
        if not selector and not all_states:
            selector = ACTIVE_TASK_STATES
        return _text(
            _as_dicts(
                self._plane.list_tasks(selector, project=project, limit=limit)
            )
        )

    def mac_task_ready(
        self, project: Optional[str] = None, limit: Optional[int] = None, **_: Any
    ) -> JsonDict:
        return _text(
            _as_dicts(self._plane.ready_tasks(project=project, limit=limit))
        )

    def mac_task_create(
        self,
        title: str = "",
        description: str = "",
        project: Optional[str] = None,
        **_: Any,
    ) -> JsonDict:
        if not str(title or "").strip():
            return _error("title is required")
        return _text(
            _one(
                self._plane.create_task(
                    str(title).strip(),
                    description=str(description or ""),
                    project=project,
                )
            )
        )

    def call(self, name: str, arguments: JsonDict) -> JsonDict:
        handler: Optional[Callable[..., JsonDict]] = getattr(self, name, None)
        if handler is None or name not in {d["name"] for d in self.descriptors()}:
            return _error("unknown tool: %s" % name)
        try:
            return handler(**(arguments or {}))
        except Exception as exc:  # noqa: BLE001 - surface it to the model
            # A hub error is information the agent can act on ("that task does
            # not exist"), not a transport fault. Never let it kill the server:
            # the agent is mid-task and a dead tool server strands it.
            return _error("%s: %s" % (type(exc).__name__, exc))


class MCPServer:
    """JSON-RPC 2.0 over stdio."""

    def __init__(self, tools: MacTools) -> None:
        self.tools = tools

    def handle(self, message: JsonDict) -> Optional[JsonDict]:
        """Return a response, or None for a notification.

        A notification has no ``id`` and MUST NOT be answered; replying to one
        is a protocol violation that some clients treat as a fatal desync.
        """
        method = str(message.get("method") or "")
        message_id = message.get("id")
        if message_id is None:
            return None
        if method == "initialize":
            return self._ok(
                message_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": _version()},
                },
            )
        if method == "tools/list":
            return self._ok(message_id, {"tools": self.tools.descriptors()})
        if method == "tools/call":
            params = message.get("params") or {}
            return self._ok(
                message_id,
                self.tools.call(
                    str(params.get("name") or ""), params.get("arguments") or {}
                ),
            )
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32601, "message": "method not found: %s" % method},
        }

    @staticmethod
    def _ok(message_id: Any, result: JsonDict) -> JsonDict:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def serve(self, stdin: TextIO, stdout: TextIO) -> int:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Malformed input is the client's problem, and a server that
                # exits on it takes the agent's session with it.
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
        return 0


def _version() -> str:
    from mac import __version__

    return __version__


def server_command() -> List[str]:
    """The argv that launches this server.

    A ``mac`` subcommand, so it resolves wherever the CLI does rather than
    needing a host interpreter path the sandbox cannot see.
    """
    return ["mac", "admin", "mcp", "serve"]


def serve(plane: Any, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    return MCPServer(MacTools(plane)).serve(stdin or sys.stdin, stdout or sys.stdout)
