"""MCP lifecycle, identity, peer exchange and backend confidentiality controls."""

from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mac import world4d_agent_mcp as mcp


@dataclass
class Object:
    handle: str
    kind: str
    pos: tuple = (0, 0, 0, 0)
    fields: dict = field(default_factory=dict)


class Store:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def factory(self, agent, world):
        store = self

        class Presence:
            def __init__(self):
                self.handle = agent
                self.w = self

            def list(self, limit):
                return list(store.objects.values())[:limit]

            def ensure(self):
                if agent not in store.objects:
                    self.create(agent, kind="agent", pos=(0, 0, 0, 0))
                self.set_fields(agent, agent_id=agent, status="online")

            def obj(self):
                return store.objects.get(agent)

            def create(self, handle, kind, pos):
                store.writes.append((agent, "create", handle))
                store.objects[handle] = Object(handle, kind, pos)

            def set_fields(self, handle, **updates):
                store.writes.append((agent, "fields", handle))
                store.objects[handle].fields.update(updates)

            def set_shape(self, handle, shape):
                store.writes.append((agent, "shape", handle))

            def move(self, pos):
                store.writes.append((agent, "move", agent))
                self.obj().pos = pos

            def recv_signals(self, consume):
                result = []
                for o in store.objects.values():
                    if (
                        o.kind == "signal"
                        and o.fields["to_agent"] == agent
                        and not o.fields["consumed"]
                    ):
                        result.append({"handle": o.handle, **o.fields})
                        if consume:
                            self.set_fields(o.handle, consumed=True)
                return result

        return Presence()


def test_peer_exchange_has_fixed_sender_unique_handles_and_explicit_acknowledgement():
    store = Store()
    a = mcp.WorldTools(store.factory, "agent_alpha", "fleet")
    b = mcp.WorldTools(store.factory, "agent_beta", "fleet")
    assert a.call("world4d_observe", {})["objects"] == []
    assert not store.writes
    a.call("world4d_checkin", {"status": "working", "note": "Investigating task one"})
    a.call("world4d_move", {"position": [1, 2, 3, 4]})
    assert store.objects["agent_alpha"].fields["status"] == "working"
    b.call("world4d_checkin", {"status": "working"})
    observed = b.call("world4d_observe", {})["objects"]
    assert next(o for o in observed if o["handle"] == "agent_alpha")["pos"] == (1, 2, 3, 4)
    first = a.call("world4d_send_signal", {"to_agent": "agent_beta", "payload": {"task": "one"}})
    second = a.call("world4d_send_signal", {"to_agent": "agent_beta", "payload": {"task": "two"}})
    assert first["handle"] != second["handle"]
    assert first["from_agent"] == "agent_alpha"
    assert a.call("world4d_receive_signals", {}) == []
    pending = b.call("world4d_receive_signals", {})
    assert len(pending) == 2
    assert all(not store.objects[o["handle"]].fields["consumed"] for o in pending)
    assert len(b.call("world4d_receive_signals", {"consume": True})) == 2
    assert b.call("world4d_receive_signals", {}) == []
    assert all(
        actor == "agent_alpha" for actor, _, handle in store.writes if handle == "agent_alpha"
    )


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("world4d_checkin", {"agent": "agent_impersonated"}),
        ("world4d_set_status", {"agent_id": "agent_impersonated"}),
        ("world4d_move", {"position": [0, 0, 0, float("inf")]}),
        ("world4d_move", {"position": [False, 0, 0, 0]}),
        ("world4d_move", {"position": [0, 0, 0]}),
        ("world4d_send_signal", {"to_agent": "../peer", "payload": {}}),
        ("world4d_send_signal", {"to_agent": "agent_peer", "payload": {"n": float("nan")}}),
        ("world4d_send_signal", {"to_agent": "agent_peer", "payload": {"text": "x" * 8001}}),
        ("world4d_receive_signals", {"consume": "yes"}),
        ("world4d_set_status", {"status": "invented"}),
    ],
)
def test_invalid_actions_are_rejected_before_constructing_the_backend(name, args):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid tool request reached backend")

    with pytest.raises(mcp.InputError):
        mcp.WorldTools(forbidden, "agent_alpha", "fleet").call(name, args)


def test_real_stdio_lifecycle_is_read_only_recovers_bad_requests_and_hides_backend_secrets(
    tmp_path,
):
    # The backend must never even be constructed for initialize/list/notifications.
    (tmp_path / "world4d_agents.py").write_text(
        "class AgentPresence:\n"
        " def __init__(self, *a, **kw):\n"
        "  raise ValueError('postgresql://private-secret@backend/database')\n"
    )
    env = tmp_path / "environment.json"
    env.write_text("{}")
    env.chmod(0o600)
    config = tmp_path / "service.json"
    config.write_text(
        json.dumps(
            {"worlds": [{"name": "fleet"}], "source": str(tmp_path), "environment_file": str(env)}
        )
    )
    requests = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"method": "tools/call", "params": {"name": "world4d_checkin"}},
        {"id": 3, "method": "tools/call", "params": []},
        {"id": 4, "method": "tools/call", "params": {"name": "world4d_observe", "arguments": {}}},
        {"id": 5, "method": "ping"},
    ]
    result = subprocess.run(
        [
            sys.executable,
            str(Path(mcp.__file__)),
            "--config",
            str(config),
            "--agent",
            "agent_alpha",
        ],
        input="not-json\n" + "\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 6
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"]["protocolVersion"] == "2025-03-26"
    assert len(replies[2]["result"]["tools"]) == 6
    assert replies[3]["error"]["code"] == -32602
    assert replies[4]["result"]["isError"]
    assert "private-secret" not in result.stdout + result.stderr
    assert replies[5] == {"jsonrpc": "2.0", "id": 5, "result": {}}


def test_private_environment_and_world_selection_fail_closed(tmp_path):
    env = tmp_path / "environment.json"
    env.write_text("{}")
    env.chmod(0o644)
    config = tmp_path / "service.json"
    config.write_text(
        json.dumps(
            {"worlds": [{"name": "fleet"}], "source": str(tmp_path), "environment_file": str(env)}
        )
    )
    with pytest.raises(ValueError, match="not configured"):
        mcp.load_tools(config, "agent_alpha", "unknown")
    with pytest.raises(ValueError, match="0600"):
        mcp.load_tools(config, "agent_alpha", "fleet")


def test_unknown_rpc_and_tool_fail_without_writes():
    store = Store()
    tools = mcp.WorldTools(store.factory, "agent_alpha", "fleet")
    output = io.StringIO()
    mcp.serve(tools, io.StringIO('{"id":1,"method":"unknown"}\n'), output)
    assert json.loads(output.getvalue())["error"]["code"] == -32601
    with pytest.raises(mcp.InputError, match="unknown"):
        tools.call("not-a-tool", {})
    assert not store.writes
