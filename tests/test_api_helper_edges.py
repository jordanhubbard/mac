"""Focused coverage for API normalization and validation helpers."""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest

from mac import api
from mac.models import ValidationError


def test_qdrant_resolution_and_vector_writer_factory(monkeypatch) -> None:
    for name in ("MAC_QDRANT_URL", "QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        monkeypatch.delenv(name, raising=False)
    assert api._configured_qdrant_url("http://explicit") == "http://explicit"
    assert api._configured_qdrant_url() is None
    monkeypatch.setenv("QDRANT_ADDRESS", "http://env")
    assert api._configured_qdrant_url() == "http://env"
    cp = SimpleNamespace(memory=object())
    assert api._vector_writer_for_memory(cp, enabled=False, qdrant_url="http://q") is None
    assert api._vector_writer_for_memory(cp, enabled=True, qdrant_url=None) is not None


def test_auth_env_websocket_and_observation_helpers(monkeypatch) -> None:
    from mac import fleet_env

    values = {"MAC_API_TOKENS": '{"token":["read"]}', "MAC_API_TOKEN": None}
    monkeypatch.setattr(fleet_env, "resolve", lambda name: values.get(name))
    assert api._load_auth_tokens_from_env()["token"].has_scope("read")
    values["MAC_API_TOKENS"] = None
    values["MAC_API_TOKEN"] = " "
    with pytest.raises(ValueError, match="set but empty"):
        api._load_auth_tokens_from_env()

    principal = api.TokenPrincipal(scopes=frozenset({"agent"}))
    websocket = SimpleNamespace(query_params={"token": "tok"}, headers={})
    assert api._authorize_acp_websocket(websocket, {"tok": principal}) == (principal, None)
    websocket = SimpleNamespace(
        query_params={}, headers={"sec-websocket-protocol": "Authorization, Bearer tok"}
    )
    assert api._authorize_acp_websocket(websocket, {"tok": principal}) == (
        principal,
        "Authorization",
    )
    assert api._authorize_acp_websocket(
        SimpleNamespace(query_params={"token": "bad"}, headers={}), {"tok": principal}
    ) == (None, None)
    read_only = api.TokenPrincipal(scopes=frozenset({"read"}))
    assert api._authorize_acp_websocket(
        SimpleNamespace(query_params={"token": "tok"}, headers={}), {"tok": read_only}
    ) == (None, None)

    assert api._safe_observation_source("agent one") == "agent_one"
    assert api._safe_observation_source("/bad") == "router"
    assert api._safe_observation_source("x" * 129) == "router"


def test_payload_bounds_clamps_and_terminal_id_validation() -> None:
    api._ensure_payload_bounded(None, "field")
    with pytest.raises(ValidationError, match="serializable"):
        api._ensure_payload_bounded({"value": object()}, "field")
    with pytest.raises(ValidationError, match="exceeds"):
        api._ensure_payload_bounded({"value": "x" * api.MAX_REGISTRATION_PAYLOAD_BYTES}, "field")
    assert api._clamp_int("bad", 1, 10, 5) == 5
    assert api._clamp_int(99, 1, 10, 5) == 10
    assert api._validate_terminal_session_id("term.good-1") == "term.good-1"
    with pytest.raises(ValidationError, match="invalid terminal"):
        api._validate_terminal_session_id("bad/session")


def test_terminal_stream_validation_and_session_summary_shapes() -> None:
    valid = {
        "id": "input",
        "topic": api.DEBUG_TERMINAL_INPUT_TOPIC,
        "content_type": api.DEBUG_TERMINAL_INPUT_CONTENT_TYPE + "; charset=utf-8",
        "headers": {
            "schema": api.DEBUG_TERMINAL_INPUT_SCHEMA,
            "terminal_session_id": "term1",
        },
        "sender_agent_id": "dashboard",
        "recipient_agent_id": "agent",
        "status": "open",
        "created_at": "2026-01-02",
        "updated_at": "2026-01-02",
    }
    cp = SimpleNamespace(get_agentbus_stream=lambda _id: SimpleNamespace(to_dict=lambda: valid))
    assert api._terminal_stream_for_session(
        cp,
        session_id="term1",
        stream_id="input",
        expected_topic=api.DEBUG_TERMINAL_INPUT_TOPIC,
        expected_content_type=api.DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
        expected_schema=api.DEBUG_TERMINAL_INPUT_SCHEMA,
    )["id"] == "input"
    with pytest.raises(ValidationError, match="not part"):
        api._terminal_stream_for_session(
            cp,
            session_id="other",
            stream_id="input",
            expected_topic=api.DEBUG_TERMINAL_INPUT_TOPIC,
            expected_content_type=api.DEBUG_TERMINAL_INPUT_CONTENT_TYPE,
            expected_schema=api.DEBUG_TERMINAL_INPUT_SCHEMA,
        )

    output = {
        "id": "output",
        "topic": api.DEBUG_TERMINAL_OUTPUT_TOPIC,
        "headers": {"terminal_session_id": "term1"},
        "sender_agent_id": "agent",
        "recipient_agent_id": "dashboard",
        "status": "closed",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-03",
        "closed_at": "2026-01-03",
    }
    sessions = api._dashboard_terminal_sessions_from_streams(
        [
            {"id": "skip", "headers": {}, "topic": "other"},
            {"id": "skip2", "headers": {"terminal_session_id": "x"}, "topic": "other"},
            valid,
            output,
        ]
    )
    assert sessions[0]["input_stream_id"] == "input"
    assert sessions[0]["output_stream_id"] == "output"
    assert sessions[0]["status"] == "closed"


def test_dashboard_agent_and_dispatch_reason_matrix(monkeypatch) -> None:
    task = SimpleNamespace(
        id="task",
        owner_agent_id="agent",
        state="open",
        metadata={},
        required_capabilities=["python", "gpu"],
        to_dict=lambda: {"id": "task"},
    )
    agent = SimpleNamespace(
        id="agent",
        name="agent",
        machine_id="machine",
        status="offline",
        health_status="degraded",
        running_digest="old",
        capabilities=["python"],
        to_dict=lambda: {"id": "agent"},
    )
    machine = SimpleNamespace(
        trusted=False,
        to_dict=lambda: {"id": "machine"},
    )
    cp = SimpleNamespace(
        _agent_capacity=lambda _agent: 1,
        _agent_active_lease_count=lambda _id: 1,
        _machine_allows_tenant=lambda *_a: False,
        _task_tenant_id=lambda _task: "tenant",
        _agent_resources_satisfy=lambda *_a: False,
        _task_required_runtime_digest=lambda _task: "new",
    )
    base = api._dashboard_agent_base(cp, agent, [task], {"machine": machine})
    assert base["availability"]["eligible"] is False
    assert set(base["availability"]["reasons"]) >= {
        "untrusted machine",
        "offline",
        "degraded",
        "at capacity",
    }
    reasons = api._dashboard_dispatch_reasons(cp, agent, task, machine)
    assert set(reasons) >= {
        "agent status is offline",
        "agent health is degraded",
        "machine is not trusted",
        "agent is at capacity",
        "machine tenant policy blocks task",
        "resources do not satisfy task",
        "runtime digest mismatch",
        "missing capabilities: gpu",
    }
    assert "agent machine is missing" in api._dashboard_dispatch_reasons(cp, agent, task, None)


def test_service_url_status_and_ticket_helpers(monkeypatch) -> None:
    assert api._strip_shell_quotes(" 'value' ") == "value"
    assert api._strip_shell_quotes("value") == "value"
    assert api._redact_service_url(None) == ""
    assert api._redact_service_url("relative/path") == "relative/path"
    assert "redacted@host" in api._redact_service_url("https://user:pass@host/path")
    assert api._redact_service_url("http://[") == "<invalid-url>"
    assert api._service_status(None, "x", "") == "not_configured"
    assert api._service_status(None, "x", "http://x") == "configured"
    assert api._service_status({"x": {"ready": True}}, "x", "") == "ready"

    values = {
        "TOKENHUB_URL": "https://tokenhub",
        "TOKENHUB_ADMIN_TOKEN": "secret",
    }
    monkeypatch.setattr(
        api,
        "_lookup_config_value",
        lambda names, _files=None: {"value": next((values[name] for name in names if name in values), "")},
    )
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    ticket = api._tokenhub_session_ticket_url()
    assert ticket.startswith("https://tokenhub/admin/v1/session/claim?")


def test_workflow_prompt_and_router_model_paths(monkeypatch) -> None:
    cp = SimpleNamespace(list_projects=lambda: [{"project": "p", "count": 1}])
    prompt = api._dashboard_workflow_plan_prompt(
        cp, {"goal": "ship", "project": "p", "context": {"x": 1}}
    )
    assert "ship" in prompt and '"project_summary"' in prompt

    from mac import router_app

    monkeypatch.setattr(router_app, "build_proxy_from_env", lambda **_kwargs: None)
    with pytest.raises(ValidationError, match="requires configured"):
        api._dashboard_workflow_plan_from_router(
            cp, {"goal": "ship"}, secret_resolver=lambda _x: None, route_observer=lambda _x: None
        )
    proxy = SimpleNamespace(
        complete=lambda *_a, **_k: (500, {}),
    )
    monkeypatch.setattr(router_app, "build_proxy_from_env", lambda **_kwargs: proxy)
    with pytest.raises(ValidationError, match="HTTP 500"):
        api._dashboard_workflow_plan_from_router(
            cp, {"goal": "ship"}, secret_resolver=lambda _x: None, route_observer=lambda _x: None
        )
    proxy.complete = lambda *_a, **_k: (
        200,
        {"choices": [{"message": {"content": '{"nodes":[]}'}}]},
    )
    assert api._dashboard_workflow_plan_from_router(
        cp, {"goal": "ship"}, secret_resolver=lambda _x: None, route_observer=lambda _x: None
    ) == {"nodes": []}


def test_terminal_input_accepts_text_and_canonical_base64() -> None:
    text = api.DashboardTerminalInput(input_stream_id="s", data="hello")
    assert base64.b64decode(api._terminal_input_data_b64(text)) == b"hello"
    encoded = api.DashboardTerminalInput(input_stream_id="s", data_b64="aGk=")
    assert api._terminal_input_data_b64(encoded) == "aGk="
    empty = api.DashboardTerminalInput(input_stream_id="s")
    assert api._terminal_input_data_b64(empty) is None


def test_terminal_input_rejects_invalid_and_oversized_values() -> None:
    with pytest.raises(ValidationError, match="data_b64 is invalid"):
        api._terminal_input_data_b64(
            api.DashboardTerminalInput(input_stream_id="s", data_b64="not base64")
        )
    too_large = b"x" * (api.MAX_TERMINAL_INPUT_BYTES + 1)
    with pytest.raises(ValidationError, match="exceeds"):
        api._terminal_input_data_b64(
            api.DashboardTerminalInput(
                input_stream_id="s", data_b64=base64.b64encode(too_large).decode()
            )
        )
    with pytest.raises(ValidationError, match="exceeds"):
        api._terminal_input_data_b64(
            api.DashboardTerminalInput(input_stream_id="s", data="x" * len(too_large))
        )


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (SimpleNamespace(project="direct", metadata={}), "direct"),
        (SimpleNamespace(project="", metadata={"project": "meta"}), "meta"),
        (SimpleNamespace(project="", metadata={"repository": "repo"}), "repo"),
        (SimpleNamespace(project="", metadata={"repo": "short"}), "short"),
        (
            SimpleNamespace(project="", metadata={"origin": {"source": "origin"}}),
            "origin",
        ),
        (SimpleNamespace(project="", metadata=[]), "unassigned"),
        (SimpleNamespace(project="", metadata={"origin": []}), "unassigned"),
    ],
)
def test_task_project_key_sources(task, expected: str) -> None:
    assert api._task_project_key(task) == expected


def test_workflow_scalar_normalizers_cover_input_shapes() -> None:
    assert api._workflow_plan_string_list(None) == []
    assert api._workflow_plan_string_list(" one, two\none ") == ["one", "two"]
    assert api._workflow_plan_string_list(("a", "", "b")) == ["a", "b"]
    assert api._workflow_plan_string_list(3) == ["3"]
    assert api._workflow_plan_int("4", 0) == 4
    assert api._workflow_plan_int("bad", 7) == 7
    used = set()
    assert api._workflow_plan_node_id(" Hello world! ", 1, used) == "hello_world"
    assert api._workflow_plan_node_id("Hello world!", 2, used) == "hello_world_2"
    assert api._workflow_plan_node_id("", 3, used) == "task_3"


def test_extract_json_object_accepts_wrapped_json_and_rejects_bad_values() -> None:
    assert api._extract_json_object('{"a":1}') == {"a": 1}
    assert api._extract_json_object('prefix {"a":2} suffix') == {"a": 2}
    for text, message in [
        ("no object", "did not contain JSON"),
        ("prefix {bad} suffix", "JSON is invalid"),
        ("prefix [1] suffix", "did not contain JSON"),
        ("prefix {[1]} suffix", "JSON is invalid"),
    ]:
        with pytest.raises(ValidationError, match=message):
            api._extract_json_object(text)


def test_chat_completion_content_accepts_message_parts_and_text_fallback() -> None:
    assert api._chat_completion_content(
        {"choices": [{"message": {"content": [{"text": "one"}, "skip", {"content": "two"}]}}]}
    ) == "one\ntwo"
    assert api._chat_completion_content({"choices": [{"text": "fallback"}]}) == "fallback"
    for body, message in [
        (None, "invalid chat response"),
        ({}, "no choices"),
        ({"choices": [None]}, "was empty"),
        ({"choices": [{"message": {"content": " "}}]}, "was empty"),
    ]:
        with pytest.raises(ValidationError, match=message):
            api._chat_completion_content(body)


def test_normalize_workflow_plan_aliases_defaults_and_limits() -> None:
    raw = {
        "tasks": [
            "skip",
            {
                "name": "First",
                "capabilities": "python, git",
                "dependencies": [],
                "priority": "bad",
                "metadata": "bad",
            },
            {"id": "second", "summary": "Do second", "parents": ["task_2"]},
        ],
        "project": "",
    }
    plan = api._normalize_dashboard_workflow_plan(raw, {"goal": "Goal", "max_tasks": "bad", "priority": 8})
    assert plan["goal"] == "Goal"
    assert plan["project"] is None
    assert plan["nodes"][0]["node_id"] == "task_2"
    assert plan["nodes"][0]["priority"] == 8
    assert plan["nodes"][0]["metadata"] == {}
    assert plan["nodes"][1]["depends_on"] == ["task_2"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "non-object"),
        ({}, "include nodes"),
        ({"nodes": ["invalid"]}, "no task nodes"),
        ({"nodes": [{"id": "a", "title": "A", "depends_on": ["missing"]}]}, "does not reference"),
    ],
)
def test_normalize_workflow_plan_rejects_invalid_shapes(raw, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        api._normalize_dashboard_workflow_plan(raw, {})


def test_workflow_topological_order_validates_unique_titles_external_and_cycles() -> None:
    with pytest.raises(ValidationError, match="unique"):
        api._workflow_plan_topological_order(
            [{"node_id": "a", "title": "A"}, {"node_id": "a", "title": "B"}],
            allow_external=False,
        )
    with pytest.raises(ValidationError, match="title"):
        api._workflow_plan_topological_order([{"node_id": "a", "title": ""}], allow_external=False)
    with pytest.raises(ValidationError, match="cycle"):
        api._workflow_plan_topological_order(
            [
                {"node_id": "a", "title": "A", "depends_on": ["b"]},
                {"node_id": "b", "title": "B", "depends_on": ["a"]},
            ],
            allow_external=False,
        )
    cp = SimpleNamespace(get_task=lambda task_id: {"id": task_id})
    nodes = [{"node_id": "a", "title": "A", "depends_on": ["external"]}]
    assert api._workflow_plan_topological_order(nodes, allow_external=True, cp=cp) == nodes
