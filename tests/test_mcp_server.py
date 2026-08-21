"""The MCP server: a client of the hub's routes, with a consumer.

TWO PROPERTIES, and the second is the one this repository keeps failing.

1. It is a CLIENT, not a second implementation. Every tool goes through
   RemoteDispatch -- the seam cli.py uses -- so the routes it calls are the
   routes tests/test_dispatch_route_contract.py already proves against the live
   FastAPI route table. #418 was the CLI and the API disagreeing about one
   query parameter with nothing comparing them; a third surface speaking to the
   hub its own way would be that again.

2. It is CONSUMED. `mcp_path` sat at None from the retirement of the vendored
   Hermes messaging MCP until now, so mcp_config_document,
   supports_per_invocation_mcp and the --mcp-config insertion were all built
   and never fed. ADR-0006 records the ACP->AgentBus half being removed after a
   census found zero streams on its topic. A server nothing launches is that
   story told slower.
"""

from __future__ import annotations

import io
import json

import pytest

from mac.mcp_server import MacTools, MCPServer, server_command


class _Plane:
    """A stand-in for RemoteDispatch that records what it was asked."""

    def __init__(self):
        self.calls = []

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return {"id": task_id, "state": "open", "title": "a task"}

    def list_tasks(self, state=None, project=None, limit=None, **kw):
        self.calls.append(("list_tasks", state, project, limit))
        return [{"id": "task_1", "state": "open"}]

    def ready_tasks(self, project=None, limit=None):
        self.calls.append(("ready_tasks", project, limit))
        return [{"id": "task_2", "state": "open"}]

    def create_task(self, title, description="", project=None):
        self.calls.append(("create_task", title, description, project))
        return {"id": "task_new", "title": title}

    def agentbus_roll_call(self, agent_id, include_departed=False):
        self.calls.append(("agentbus_roll_call", agent_id, include_departed))
        return {"schema": "mac.agentbus.roll_call.v1", "agents": []}

    def read_agentbus_traffic(
        self, agent_id, after_cursor=None, limit=100, include_addressed=True
    ):
        self.calls.append(
            ("read_agentbus_traffic", agent_id, after_cursor, limit, include_addressed)
        )
        return [{"cursor": "cursor-1", "topic": "peer.message.v1"}]

    def publish_agentbus_content(self, **kwargs):
        self.calls.append(("publish_agentbus_content", kwargs))
        return {"id": "bus_1", **kwargs}


def _rpc(server, method, params=None, message_id=1):
    return server.handle(
        {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
    )


@pytest.fixture()
def plane():
    return _Plane()


@pytest.fixture()
def server(plane):
    return MCPServer(MacTools(plane, agent_id="agent_session_test"))


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


def test_initialize_reports_the_protocol_and_the_tool_capability(server):
    result = _rpc(server, "initialize")["result"]

    assert result["protocolVersion"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "mac"


def test_a_notification_is_never_answered(server):
    """A message with no id MUST NOT get a response. Replying desyncs clients
    that treat an unsolicited response as a protocol fault."""
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unknown_method_is_a_jsonrpc_error(server):
    response = _rpc(server, "tools/nope")

    assert response["error"]["code"] == -32601


def test_malformed_input_does_not_kill_the_server(server):
    """The agent is mid-task; a dead tool server strands it."""
    out = io.StringIO()
    server.serve(io.StringIO('not json\n\n{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'), out)

    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == 7


# --------------------------------------------------------------------------
# the tools
# --------------------------------------------------------------------------


def test_tools_are_listed_with_schemas(server):
    tools = _rpc(server, "tools/list")["result"]["tools"]

    assert {t["name"] for t in tools} == {
        "mac_task_show",
        "mac_task_list",
        "mac_task_ready",
        "mac_task_create",
        "mac_agentbus_roll_call",
        "mac_agentbus_traffic",
        "mac_agent_send",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_every_listed_tool_is_callable(server):
    """A descriptor without an implementation is a tool that fails only when
    the model finally tries it."""
    for tool in _rpc(server, "tools/list")["result"]["tools"]:
        result = _rpc(server, "tools/call", {"name": tool["name"], "arguments": {}})
        assert "result" in result


def test_show_passes_the_id_through_to_the_plane(server, plane):
    _rpc(server, "tools/call", {"name": "mac_task_show", "arguments": {"task_id": "task_abc"}})

    assert ("get_task", "task_abc") in plane.calls


def test_list_defaults_to_active_work(server, plane):
    """Same default as the CLI (#407). A tool returning 3,500 cancelled tasks
    is the unusable view that change removed."""
    _rpc(server, "tools/call", {"name": "mac_task_list", "arguments": {}})

    _, state, _, _ = plane.calls[0]
    assert state is not None and "cancelled" not in list(state)
    assert "open" in list(state)


def test_all_states_opts_out(server, plane):
    _rpc(server, "tools/call", {"name": "mac_task_list", "arguments": {"all_states": True}})

    assert plane.calls[0][1] is None


def test_an_explicit_state_wins(server, plane):
    _rpc(server, "tools/call", {"name": "mac_task_list", "arguments": {"state": "blocked"}})

    assert plane.calls[0][1] == "blocked"


def test_create_requires_a_title(server, plane):
    result = _rpc(server, "tools/call", {"name": "mac_task_create", "arguments": {}})["result"]

    assert result["isError"] is True
    assert not plane.calls, "an invalid call must not reach the hub"


def test_a_hub_failure_is_reported_to_the_model_not_raised(server, plane):
    """A hub error is information the agent can act on, not a transport fault."""
    def boom(task_id):
        raise RuntimeError("task not found: task_zzz")

    plane.get_task = boom
    result = _rpc(server, "tools/call", {"name": "mac_task_show", "arguments": {"task_id": "task_zzz"}})["result"]

    assert result["isError"] is True
    assert "task not found" in result["content"][0]["text"]


def test_an_unknown_tool_is_an_error_result_not_a_crash(server):
    result = _rpc(server, "tools/call", {"name": "mac_delete_everything", "arguments": {}})["result"]

    assert result["isError"] is True


def test_agentbus_tools_bind_observer_and_sender_to_session_identity(server, plane):
    _rpc(
        server,
        "tools/call",
        {"name": "mac_agentbus_roll_call", "arguments": {"include_departed": True}},
    )
    _rpc(
        server,
        "tools/call",
        {
            "name": "mac_agentbus_traffic",
            "arguments": {"after_cursor": "cursor-0", "limit": 12},
        },
    )
    _rpc(
        server,
        "tools/call",
        {
            "name": "mac_agent_send",
            "arguments": {
                "recipient_agent_id": "agent_peer",
                "message": "ready",
                "from_agent_id": "agent_forged",
            },
        },
    )

    assert ("agentbus_roll_call", "agent_session_test", True) in plane.calls
    assert (
        "read_agentbus_traffic",
        "agent_session_test",
        "cursor-0",
        12,
        True,
    ) in plane.calls
    publish = next(call[1] for call in plane.calls if call[0] == "publish_agentbus_content")
    assert publish["sender_agent_id"] == "agent_session_test"
    assert publish["payload"]["from_agent_id"] == "agent_session_test"
    assert publish["recipient_agent_id"] == "agent_peer"


def test_agentbus_tools_fail_closed_without_bound_identity(plane):
    server = MCPServer(MacTools(plane, agent_id=""))
    result = _rpc(
        server,
        "tools/call",
        {"name": "mac_agentbus_roll_call", "arguments": {}},
    )["result"]

    assert result["isError"] is True
    assert "MAC_AGENT_ID" in result["content"][0]["text"]


# --------------------------------------------------------------------------
# it is a client of the same routes, and it is consumed
# --------------------------------------------------------------------------


def test_the_tools_only_reach_the_hub_through_dispatch():
    """No direct HTTP. If a tool built its own request the route-contract gate
    would not see it, and this becomes a third surface that can drift."""
    import inspect

    from mac import mcp_server

    source = inspect.getsource(mcp_server)

    for forbidden in ("urllib", "requests", "http.client", "HubClient("):
        assert forbidden not in source, (
            "%s speaks to the hub directly; every call must go through the "
            "dispatch plane so tests/test_dispatch_route_contract.py covers it"
            % forbidden
        )


def test_the_server_command_is_a_mac_subcommand():
    """It must resolve wherever the CLI does. executor_sandbox notes that a
    host interpreter path does not reliably resolve inside the sandbox."""
    assert server_command()[0] == "mac"

    import sys

    sys.argv = ["mac"]
    from mac.cli import build_parser

    parsed = build_parser().parse_args(server_command()[1:])
    assert parsed.func.__name__ == "cmd_mcp_serve"


def test_the_sandbox_actually_hands_this_config_to_agents():
    """The property ADR-0006 is a warning about: built, wired, and consumed.

    `mcp_path` was None unconditionally, so the whole injection path existed
    and was never fed.
    """
    from mac.executor_sandbox import _write_mac_mcp_config

    path = _write_mac_mcp_config(task_id="task_t")
    assert path

    document = json.loads(open(path).read())
    server = document["mcpServers"]["mac"]
    assert [server["command"], *server["args"]] == server_command()


def test_a_config_write_failure_does_not_stop_the_agent(monkeypatch):
    """Tools are an enhancement. Losing them must not lose the task."""
    from mac import executor_sandbox

    monkeypatch.setattr(
        executor_sandbox.tempfile, "mkdtemp", lambda **kw: (_ for _ in ()).throw(OSError("no space"))
    )

    assert executor_sandbox._write_mac_mcp_config(task_id="task_t") is None
