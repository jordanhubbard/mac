"""Claude Code's MAC hooks: board delivery, nudges and the Stop gate.

The hooks are what make a Claude Code task agent steerable and keep it from
stopping early, so each behaviour is pinned here against a fake board.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from mac import claude_hooks as hooks
from mac import coding_agent as ca


class FakeHub(hooks.Hub):
    def __init__(self) -> None:
        super().__init__("http://hub", "tok", "task_1")
        self.messages: List[Dict[str, Any]] = []
        self.posts: List[Dict[str, Any]] = []

    def say(self, author_kind: str, kind: str, body: str) -> None:
        self.messages.append(
            {"id": len(self.messages) + 1, "author_kind": author_kind, "author": "jkh", "kind": kind, "body": body}
        )

    def read(self, after: int) -> Dict[str, Any]:
        return {"messages": [m for m in self.messages if m["id"] > after]}

    def post(self, kind: str, body: str, *, reply_to=None, **metadata: Any) -> Any:
        self.posts.append({"kind": kind, "body": body, "metadata": metadata})
        self.say("agent", kind, body)
        return {"id": len(self.messages)}


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_AGENT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def _run(hook, payload, hub, capsys) -> Dict[str, Any]:
    hook(payload, hub)
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


def test_post_tool_delivers_a_directive_once_and_never_echoes_the_agent(state_dir, capsys):
    hub = FakeHub()
    hub.say("human", "directive", "only touch src/parser.c")
    hub.say("agent", "status", "reading")
    out = _run(hooks.hook_post_tool, {"tool_name": "Read", "tool_input": {"file_path": "a.c"}}, hub, capsys)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "only touch src/parser.c" in context and "directive" in context
    assert "reading" not in context
    # Delivered once: the cursor moved past it.
    again = _run(hooks.hook_post_tool, {"tool_name": "Read"}, hub, capsys)
    assert "only touch" not in json.dumps(again)


def test_activity_is_posted_at_most_once_a_minute(state_dir, capsys, monkeypatch):
    hub = FakeHub()
    clock = [1000.0]
    monkeypatch.setattr(hooks, "_now", lambda: clock[0])
    _run(hooks.hook_post_tool, {"tool_name": "Bash", "tool_input": {"command": "make test"}}, hub, capsys)
    _run(hooks.hook_post_tool, {"tool_name": "Bash", "tool_input": {"command": "make lint"}}, hub, capsys)
    clock[0] += hooks.ACTIVITY_INTERVAL_SECONDS
    _run(hooks.hook_post_tool, {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}, hub, capsys)
    assert [p["body"] for p in hub.posts if p["kind"] == "activity"] == ["Bash: make test", "Edit: x.py"]


def test_a_quiet_agent_is_asked_for_status_once_per_quiet_window(state_dir, capsys, monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(hooks, "ACTIVITY_INTERVAL_SECONDS", 10**9)
    nudges = []
    for _ in range(hooks.NUDGE_AFTER_CALLS + 5):
        out = _run(hooks.hook_post_tool, {"tool_name": "Read"}, hub, capsys)
        if "status?" in json.dumps(out):
            nudges.append(out)
    assert len(nudges) == 1
    # Posting a status opens a new window.
    assert hooks.board_main(["status", "found the bug"], hub) == 0
    capsys.readouterr()
    for _ in range(hooks.NUDGE_AFTER_CALLS):
        out = _run(hooks.hook_post_tool, {"tool_name": "Read"}, hub, capsys)
    assert "status?" in json.dumps(out)


def test_stop_is_refused_while_messages_are_unread(state_dir, capsys):
    hub = FakeHub()
    hub.say("human", "answer", "use postgres")
    out = _run(hooks.hook_stop, {"stop_hook_active": False}, hub, capsys)
    assert out["decision"] == "block"
    assert "use postgres" in out["reason"]


def test_stop_without_a_handoff_is_refused_a_bounded_number_of_times(state_dir, capsys):
    hub = FakeHub()
    refusals = [_run(hooks.hook_stop, {}, hub, capsys) for _ in range(hooks.MAX_HANDOFF_REFUSALS + 1)]
    assert [r.get("decision") for r in refusals] == ["block"] * hooks.MAX_HANDOFF_REFUSALS + [None]
    assert "Do not stop to ask whether to continue" in refusals[0]["reason"]


@pytest.mark.parametrize(
    "argv,kind,metadata",
    [
        (["done", "fixed the parser; make test passes"], "done", {}),
        (["no-change", "already fixed in abc123"], "done", {"no_change": True}),
        (["ask", "which region?", "--blocking", "--default", "us"], "question", {"blocking": True, "default": "us"}),
    ],
)
def test_a_handoff_or_blocking_question_lets_the_agent_stop(state_dir, capsys, argv, kind, metadata):
    hub = FakeHub()
    assert hooks.board_main(argv, hub) == 0
    capsys.readouterr()
    assert hub.posts[-1]["kind"] == kind
    assert hub.posts[-1]["metadata"] == {**({"blocking": False} if kind == "question" else {}), **metadata}
    assert _run(hooks.hook_stop, {}, hub, capsys) == {}


def test_a_non_blocking_question_does_not_end_the_work(state_dir, capsys):
    hub = FakeHub()
    assert hooks.board_main(["ask", "rename the flag?", "--default", "keep it"], hub) == 0
    capsys.readouterr()
    assert _run(hooks.hook_stop, {}, hub, capsys)["decision"] == "block"


def test_without_a_board_nothing_blocks(state_dir, capsys):
    assert _run(hooks.hook_stop, {}, None, capsys) == {}
    assert hooks.board_main(["status", "x"], None) == 2


def test_session_start_explains_the_board_and_replays_earlier_direction(state_dir, capsys):
    hub = FakeHub()
    hub.say("human", "directive", "keep the public API")
    out = _run(hooks.hook_session_start, {}, hub, capsys)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert ".mac-agent/board done" in context
    assert "keep the public API" in context


def test_a_hook_never_fails_the_agent(state_dir, capsys, monkeypatch):
    class Broken(FakeHub):
        def read(self, after):
            raise OSError("hub down")

    monkeypatch.setattr(hooks.Hub, "from_env", classmethod(lambda cls: Broken()))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert hooks.main(["post-tool"]) == 0
    assert "hub down" in (state_dir / "errors.log").read_text()


# -- the executor's side ------------------------------------------------------


def test_executor_writes_claude_files_outside_the_repository_with_no_secret_on_disk(tmp_path):
    from mac import executor_sandbox as ex

    env = {"MAC_HUB_URL": "http://hub:8789", "MAC_INFERENCE_TOKEN": "mac_inference_x", "MAC_TASK_ID": "task_1"}
    overlay = ex._write_claude_agent_files(tmp_path, "/sandbox/ws", env, python="/opt/mac-venv/bin/python")
    assert overlay["ANTHROPIC_BASE_URL"] == "http://hub:8789"
    assert overlay["ANTHROPIC_AUTH_TOKEN"] == "mac_inference_x"
    assert overlay["CLAUDE_CONFIG_DIR"] == "/sandbox/ws/.mac-agent/claude"
    assert overlay["MAC_AGENT_PYTHON"] == "/opt/mac-venv/bin/python"
    assert overlay["ANTHROPIC_CUSTOM_HEADERS"] == "X-MAC-Task-ID: task_1"
    agent_dir = tmp_path / ".mac-agent"
    settings = json.loads((agent_dir / "settings.json").read_text())
    assert set(settings["hooks"]) == {"SessionStart", "PostToolUse", "Stop"}
    post_tool = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert post_tool == '"$MAC_AGENT_PYTHON" "$MAC_AGENT_DIR/claude_hooks.py" post-tool'
    assert (agent_dir / "claude_hooks.py").read_text() == Path(hooks.__file__).read_text()
    assert (agent_dir / "board").stat().st_mode & 0o111
    for path in agent_dir.rglob("*"):
        if path.is_file():
            assert "mac_inference_x" not in path.read_text()
    assert ex._write_claude_agent_files(tmp_path, "/s", {"MAC_HUB_URL": "http://hub"}, python="p") == {}


def test_the_selected_agent_decides_which_config_is_written(tmp_path, monkeypatch):
    from mac import executor_sandbox as ex

    env = {"MAC_HUB_URL": "http://hub", "MAC_INFERENCE_TOKEN": "t"}
    monkeypatch.setenv("MAC_CODING_AGENT", "claude")
    assert "ANTHROPIC_BASE_URL" in ex._write_coding_agent_config(tmp_path, "/s", env, python="p")
    monkeypatch.setenv("MAC_CODING_AGENT", "opencode")
    assert "OPENCODE_CONFIG" in ex._write_coding_agent_config(tmp_path, "/s", env, python="p")
    assert ca.selected_agent({"MAC_CODING_AGENT": "claude"}) == "claude"
    assert ca.selected_agent({}) == "opencode"


def test_claude_reaches_only_the_hubs_messages_routes():
    from mac import openshell_policy as op

    template = (Path(__file__).resolve().parents[1] / "deploy" / "openshell" / "mac-hermes-policy.yaml").read_text()
    doc = yaml.safe_load(op.render_policy(template, agent_user="jkh", hub_host="10.0.0.1", hub_port=8789))
    block = doc["network_policies"]["claude_router"]
    assert [(e["host"], e["port"]) for e in block["endpoints"]] == [("10.0.0.1", 8789)]
    assert {r["allow"]["path"] for r in block["endpoints"][0]["rules"]} == {
        "/v1/messages",
        "/v1/messages/count_tokens",
    }
    assert {b["path"] for b in block["binaries"]} == {
        "/usr/local/bin/claude",
        "/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    }
