"""Phase-1 integration: the ACP executor backend wired into task_executor.

Exercises the MAC_EXECUTOR_BACKEND=acp seam without spawning a real agent: an
injected fake executor drives the session/update sink and stop-reason -> return
code mapping; the permission handler is tested directly. Hermes remains the
default backend when the flag is unset.
"""

from __future__ import annotations

import types
from pathlib import Path

from mac import task_executor
from mac.acp.executor import ACPExecutor
from mac.acp.protocol import (
    PermissionOption,
    PermissionOutcome,
    RequestPermissionParams,
    SessionUpdateKind,
    StopReason,
    text_block,
)


def test_backend_defaults_to_hermes(monkeypatch):
    monkeypatch.delenv("MAC_EXECUTOR_BACKEND", raising=False)
    assert task_executor._executor_backend() == "hermes"
    monkeypatch.setenv("MAC_EXECUTOR_BACKEND", "ACP")
    assert task_executor._executor_backend() == "acp"


def test_acp_agent_argv_parses_and_is_empty_when_unset(monkeypatch):
    monkeypatch.delenv("MAC_ACP_AGENT_CMD", raising=False)
    assert task_executor._acp_agent_argv() == []
    monkeypatch.setenv("MAC_ACP_AGENT_CMD", "claude-code acp --flag")
    assert task_executor._acp_agent_argv() == ["claude-code", "acp", "--flag"]


def test_invoke_agent_routes_to_acp_when_flagged(monkeypatch):
    sentinel = object()
    monkeypatch.setenv("MAC_EXECUTOR_BACKEND", "acp")
    monkeypatch.setattr(task_executor, "_invoke_acp_agent", lambda *a, **k: sentinel)
    # runner must NOT be called on the ACP path
    def _boom(*a, **k):  # pragma: no cover - asserts it is never invoked
        raise AssertionError("hermes runner should not run under backend=acp")

    result = task_executor._invoke_agent(_boom, "prompt", Path("."), "task_1", {})
    assert result is sentinel


def test_invoke_acp_agent_streams_updates_and_maps_stop_reason(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(task_executor, "_hub_post", lambda path, payload, **k: posted.append((path, payload)) or True)

    class _FakeExecutor:
        _argv = ["fake-acp-agent"]

        def run(self, prompt, *, on_update, on_permission, timeout=None):
            on_update({"sessionId": "s1", "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": text_block("hello "),
            }})
            on_update({"sessionId": "s1", "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": text_block("world"),
            }})
            on_update({"sessionId": "s1", "update": {"sessionUpdate": SessionUpdateKind.TOOL_CALL}})
            return types.SimpleNamespace(stop_reason=StopReason.END_TURN)

    result = task_executor._invoke_acp_agent(
        "do it", tmp_path, "task_42", {"timeout": None}, executor=_FakeExecutor()
    )
    assert result.returncode == 0
    assert result.stdout == "hello world"  # only agent_message_chunk text is collected
    # every update was streamed to the /action-events ledger
    action_events = [p for (path, p) in posted if path == "/action-events"]
    assert len(action_events) == 3
    assert {e["action_name"] for e in action_events} == {"agent_message_chunk", "tool_call"}
    assert all(e["task_id"] == "task_42" for e in action_events)


def test_invoke_acp_agent_nonzero_on_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(task_executor, "_hub_post", lambda *a, **k: True)

    class _Refusing:
        _argv = ["fake"]

        def run(self, prompt, *, on_update, on_permission, timeout=None):
            return types.SimpleNamespace(stop_reason=StopReason.REFUSAL)

    result = task_executor._invoke_acp_agent("x", tmp_path, "t", {}, executor=_Refusing())
    assert result.returncode == 1
    assert "refusal" in result.stderr


def test_invoke_acp_agent_errors_without_agent_cmd(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_EXECUTOR_BACKEND", "acp")
    monkeypatch.delenv("MAC_ACP_AGENT_CMD", raising=False)
    result = task_executor._invoke_acp_agent("x", tmp_path, "t", {})
    assert result.returncode == 1
    assert "MAC_ACP_AGENT_CMD" in result.stderr


def test_invoke_acp_agent_survives_backend_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(task_executor, "_hub_post", lambda *a, **k: True)

    class _Boom:
        _argv = ["fake"]

        def run(self, *a, **k):
            raise RuntimeError("agent died")

    result = task_executor._invoke_acp_agent("x", tmp_path, "t", {}, executor=_Boom())
    assert result.returncode == 1
    assert "agent died" in result.stderr


def test_permission_handler_auto_approves_allow_option(monkeypatch):
    posted = []
    monkeypatch.setattr(task_executor, "_hub_post", lambda path, payload, **k: posted.append(payload) or True)
    monkeypatch.setattr(task_executor, "_openshell_enabled", lambda: False)
    monkeypatch.setattr("mac.acp.permission.load_openshell_policy", lambda *a, **k: None)
    monkeypatch.delenv("MAC_ACP_PERMISSION_MODE", raising=False)
    handler = task_executor._acp_permission_handler("task_9")
    params = RequestPermissionParams(
        session_id="s1",
        tool_call={"title": "write file", "toolCallId": "tc1"},
        options=[
            PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
            PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        ],
    )
    decision = handler(params)
    assert decision.outcome == PermissionOutcome.SELECTED
    assert decision.option_id == "allow"
    assert posted and posted[0]["outcome"] == "allowed"
    assert posted[0]["action_name"] == "write file"


def test_permission_handler_cancels_when_no_options(monkeypatch):
    monkeypatch.setattr(task_executor, "_hub_post", lambda *a, **k: True)
    handler = task_executor._acp_permission_handler("task_9")
    decision = handler(RequestPermissionParams(session_id="s1", tool_call={}, options=[]))
    assert decision.outcome == PermissionOutcome.CANCELLED


# -- Phase 3: permission handler consults the policy evaluator ---------------


_LOCKDOWN_POLICY = {"network_policies": {}, "filesystem_policy": {"read_write": []}}


def test_permission_handler_denies_network_under_lockdown_policy(monkeypatch):
    """Unsandboxed + lockdown policy + a network tool_call -> deny (reject option),
    and the recorded action-event carries the deny reason."""
    posted = []
    monkeypatch.setattr(task_executor, "_hub_post", lambda path, payload, **k: posted.append(payload) or True)
    monkeypatch.setattr(task_executor, "_openshell_enabled", lambda: False)
    monkeypatch.setattr(task_executor, "_acp_permission_handler", task_executor._acp_permission_handler)
    # inject the lockdown policy via the loader
    monkeypatch.setattr("mac.acp.permission.load_openshell_policy", lambda *a, **k: _LOCKDOWN_POLICY)
    monkeypatch.setenv("MAC_ACP_PERMISSION_MODE", "policy")

    handler = task_executor._acp_permission_handler("task_lock")
    params = RequestPermissionParams(
        session_id="s1",
        tool_call={"kind": "fetch", "title": "fetch url", "toolCallId": "tc1"},
        options=[
            PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
            PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        ],
    )
    decision = handler(params)
    # denied -> picks the reject option
    assert decision.outcome == PermissionOutcome.SELECTED
    assert decision.option_id == "reject"
    assert posted and posted[0]["outcome"] == "denied"
    assert posted[0]["attributes"]["permission_reason"] == "policy-network-lockdown"
    assert posted[0]["attributes"]["allowed"] is False


def test_permission_handler_allows_when_sandboxed(monkeypatch):
    """Sandboxed run short-circuits to allow ('sandbox-enforced'); the policy is
    never even loaded."""
    posted = []
    monkeypatch.setattr(task_executor, "_hub_post", lambda path, payload, **k: posted.append(payload) or True)
    monkeypatch.setattr(task_executor, "_openshell_enabled", lambda: True)

    def _boom(*a, **k):  # pragma: no cover - asserts it is never invoked
        raise AssertionError("policy must not be loaded when sandboxed")

    monkeypatch.setattr("mac.acp.permission.load_openshell_policy", _boom)

    handler = task_executor._acp_permission_handler("task_box")
    params = RequestPermissionParams(
        session_id="s1",
        tool_call={"kind": "execute", "title": "run shell", "toolCallId": "tc2"},
        options=[
            PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
            PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        ],
    )
    decision = handler(params)
    assert decision.outcome == PermissionOutcome.SELECTED
    assert decision.option_id == "allow"
    assert posted[0]["outcome"] == "allowed"
    assert posted[0]["attributes"]["permission_reason"] == "sandbox-enforced"


# -- Phase 3: AgentBus mirror lifecycle --------------------------------------


def test_invoke_acp_agent_mirrors_session_updates_to_agentbus(monkeypatch, tmp_path):
    """The mirror opens a stream, appends one chunk per session/update, and closes
    it — alongside (not instead of) the /action-events posts."""
    posts = []  # (path, payload) for _hub_post
    json_posts = []  # (path, payload) for _hub_post_json

    def _post(path, payload, **k):
        posts.append((path, payload))
        return True

    def _post_json(path, payload, **k):
        json_posts.append((path, payload))
        # the open returns the created stream with its server-assigned id
        return {"id": "bus_acp_1", "status": "open"}

    monkeypatch.setattr(task_executor, "_hub_post", _post)
    monkeypatch.setattr(task_executor, "_hub_post_json", _post_json)
    monkeypatch.setattr(task_executor, "local_agent_id", lambda: "agent_test")

    class _FakeExecutor:
        _argv = ["fake-acp-agent"]

        def run(self, prompt, *, on_update, on_permission, timeout=None):
            on_update({"sessionId": "s1", "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": text_block("hi"),
            }})
            on_update({"sessionId": "s1", "update": {"sessionUpdate": SessionUpdateKind.TOOL_CALL}})
            return types.SimpleNamespace(stop_reason=StopReason.END_TURN)

    result = task_executor._invoke_acp_agent(
        "go", tmp_path, "task_mirror", {"timeout": None}, executor=_FakeExecutor()
    )
    assert result.returncode == 0

    # opened exactly one stream
    opens = [p for (path, p) in json_posts if path == "/agentbus/streams"]
    assert len(opens) == 1
    assert opens[0]["sender_agent_id"] == "agent_test"
    assert opens[0]["recipient_agent_id"] == "agent_test"  # self-stream
    assert opens[0]["content_type"] == "application/vnd.mac.acp.update+json"

    # one chunk appended per session/update
    chunks = [(path, p) for (path, p) in posts if path == "/agentbus/streams/bus_acp_1/chunks"]
    assert len(chunks) == 2
    assert chunks[0][1]["payload"]["sessionId"] == "s1"

    # stream closed exactly once
    closes = [path for (path, p) in posts if path.startswith("/agentbus/streams/bus_acp_1/close")]
    assert len(closes) == 1
    assert "sender_agent_id=agent_test" in closes[0]
    assert "status=closed" in closes[0]


def test_invoke_acp_agent_mirror_is_inert_when_open_fails(monkeypatch, tmp_path):
    """If the stream open fails (no hub / error), append + close are skipped and
    the run still succeeds — the mirror is best-effort."""
    posts = []
    monkeypatch.setattr(task_executor, "_hub_post", lambda path, payload, **k: posts.append((path, payload)) or True)
    monkeypatch.setattr(task_executor, "_hub_post_json", lambda *a, **k: None)  # open fails
    monkeypatch.setattr(task_executor, "local_agent_id", lambda: "agent_test")

    class _FakeExecutor:
        _argv = ["fake"]

        def run(self, prompt, *, on_update, on_permission, timeout=None):
            on_update({"sessionId": "s1", "update": {"sessionUpdate": SessionUpdateKind.TOOL_CALL}})
            return types.SimpleNamespace(stop_reason=StopReason.END_TURN)

    result = task_executor._invoke_acp_agent("go", tmp_path, "t", {}, executor=_FakeExecutor())
    assert result.returncode == 0
    # no chunk/close posts when the stream never opened
    assert not [path for (path, p) in posts if "/chunks" in path or "/close" in path]
    # /action-events still flowed
    assert [path for (path, p) in posts if path == "/action-events"]


def test_acpexecutor_is_the_real_seam_type():
    # guard: the integration imports the real adapter, not a shim
    assert ACPExecutor.__module__ == "mac.acp.executor"
