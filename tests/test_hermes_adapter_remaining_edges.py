from __future__ import annotations

import argparse
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from mac import hermes_adapter


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.get_values: dict[str, Any] = {}

    def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return self.get_values.get(path, {})

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        self.calls.append(("POST", path, payload))
        return {"ok": True}


def test_description_put_url_error_and_web_client_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = hermes_adapter.ConversationTaskInput(
        title="Task", summary="Summary", links=[" https://example.com "]
    )
    assert "References:" in task.description()

    transport_calls: list[Any] = []
    client = hermes_adapter.MacApiClient(
        "http://mac", transport=lambda *args: transport_calls.append(args) or {}
    )
    client.put("/resource", {"value": 1})
    assert transport_calls[-1][0] == "PUT"

    monkeypatch.setattr(
        hermes_adapter.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )
    with pytest.raises(hermes_adapter.MacApiError, match="offline"):
        hermes_adapter.MacApiClient("http://mac").get("/resource")

    adapter = hermes_adapter.HermesMacAdapter(client)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("MAC_WEB_SEARCH_URL", raising=False)
    with pytest.raises(hermes_adapter.MacApiError, match="web search requires"):
        adapter._web_client()
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl/")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "key")
    assert adapter._web_client().base_url == "http://firecrawl"


def test_conversation_child_and_web_input_validation_edges() -> None:
    client = _Client()
    adapter = hermes_adapter.HermesMacAdapter(client)  # type: ignore[arg-type]
    with pytest.raises(hermes_adapter.MacApiError, match="title"):
        adapter.create_task_from_conversation(
            "instance", hermes_adapter.ConversationTaskInput("", "summary")
        )
    with pytest.raises(hermes_adapter.MacApiError, match="summary"):
        adapter.create_task_from_conversation(
            "instance", hermes_adapter.ConversationTaskInput("title", "")
        )

    adapter.add_child_task(
        "parent/id",
        title="child",
        required_capabilities=["python"],
        dependencies=["dep"],
    )
    path, payload = client.calls[-1][1:]
    assert path == "/tasks/parent%2Fid/children"
    assert payload["children"][0]["required_capabilities"] == ["python"]
    assert payload["children"][0]["dependencies"] == ["dep"]

    for invoke, match in [
        (lambda: adapter.web_search(""), "query"),
        (lambda: adapter.web_scrape(""), "url"),
        (lambda: adapter.web_crawl(""), "url"),
        (lambda: adapter.web_crawl_status(""), "job id"),
    ]:
        with pytest.raises(hermes_adapter.MacApiError, match=match):
            invoke()


def test_reply_and_memory_writeback_error_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = hermes_adapter.HermesMacAdapter(_Client())  # type: ignore[arg-type]
    summaries = iter(
        [
            {"title": "Done", "state": "completed", "publications": []},
            {"title": "Bad", "state": "failed"},
            {"title": "Open", "state": "running"},
        ]
    )
    monkeypatch.setattr(adapter, "task_summary", lambda _task: next(summaries))
    assert adapter.user_reply_for_task("task") == "Done is complete."
    assert adapter.user_reply_for_task("task") == "Bad is failed."
    assert adapter.user_reply_for_task("task") == "Open is currently running."

    client = _Client()
    adapter = hermes_adapter.HermesMacAdapter(client)  # type: ignore[arg-type]
    client.get_values["/persona-instances/instance/context"] = {
        "memory_contract": {"memory_scope": ""},
        "hermes_instance": {},
    }
    monkeypatch.setattr(adapter, "task_summary", lambda _task: {"state": "running"})
    with pytest.raises(hermes_adapter.MacApiError, match="completed"):
        adapter.memory_writeback_payload("instance", "task")
    monkeypatch.setattr(adapter, "task_summary", lambda _task: {"state": "completed"})
    with pytest.raises(hermes_adapter.MacApiError, match="memory_scope"):
        adapter.memory_writeback_payload("instance", "task")


def test_sanitizers_and_json_argument_edges() -> None:
    assert hermes_adapter._sanitize_command_argv(["x" * 513]) == [
        "<truncated:chars=513>"
    ]
    assert hermes_adapter._csv(None) == []
    assert hermes_adapter._json_arg(None, {"default": True}) == {"default": True}
    with pytest.raises(hermes_adapter.MacApiError, match="array"):
        hermes_adapter._json_list_arg("{}")


def test_register_and_task_command_handlers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(hermes_adapter.MacApiError, match="platform"):
        hermes_adapter._cmd_register(
            SimpleNamespace(binding=["invalid"])
        )

    class Adapter:
        def register_identity(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["platform_bindings"][0].display_name == "Name"
            return {"registered": True}

        def create_task_from_conversation(self, instance: str, task: Any) -> dict[str, Any]:
            assert instance == "instance"
            return {"task": task.title}

    monkeypatch.setattr(hermes_adapter, "_adapter", lambda _args: Adapter())
    hermes_adapter._cmd_register(
        SimpleNamespace(
            binding=["slack:C123:Name"],
            tenant="tenant",
            persona="persona",
            instance="instance",
            soul_ref="soul",
            memory_scope="scope",
            home_ref=None,
        )
    )
    hermes_adapter._cmd_task(
        SimpleNamespace(
            title="Task",
            summary="Summary",
            platform_binding_id=None,
            conversation_ref=None,
            project=None,
            priority=0,
            required_capabilities="python,git",
            snippet=[],
            link=[],
            hermes_instance_id="instance",
        )
    )
    assert '"registered": true' in capsys.readouterr().out


def test_remaining_command_wrappers(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class Adapter:
        def task_summary(self, task_id: str) -> dict[str, str]:
            return {"task": task_id}

        def work_context_brief(self, instance: str) -> str:
            return f"brief:{instance}"

        def add_child_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
            return {"parent": task_id, **kwargs}

        def user_reply_for_task(self, task_id: str) -> str:
            return f"reply:{task_id}"

        def write_completed_task_to_memory(self, instance: str, task: str) -> dict[str, str]:
            return {"instance": instance, "task": task}

    monkeypatch.setattr(hermes_adapter, "_adapter", lambda _args: Adapter())
    hermes_adapter._cmd_summary(SimpleNamespace(task_id="task"))
    hermes_adapter._cmd_work_brief(SimpleNamespace(hermes_instance_id="instance"))
    hermes_adapter._cmd_add_child_task(
        SimpleNamespace(
            task_id="parent",
            title="child",
            description=None,
            project=None,
            priority=None,
            required_capabilities=None,
            dependencies=None,
            metadata=None,
            max_attempts=None,
            actor="actor",
        )
    )
    hermes_adapter._cmd_reply(SimpleNamespace(task_id="task"))
    hermes_adapter._cmd_writeback(
        SimpleNamespace(hermes_instance_id="instance", task_id="task")
    )
    assert "brief:instance" in capsys.readouterr().out


def test_main_resolves_fleet_token_and_reports_api_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mac import fleet_env

    seen: dict[str, Any] = {}
    monkeypatch.setattr(fleet_env, "resolve_first", lambda *_a, **_k: "fleet-token")

    def success(args: argparse.Namespace) -> None:
        seen["token"] = args.token

    monkeypatch.setattr(hermes_adapter, "_cmd_summary", success)
    assert hermes_adapter.main(["--fleet", "fleet", "summary", "task"]) == 0
    assert seen["token"] == "fleet-token"

    def fail(_args: argparse.Namespace) -> None:
        raise hermes_adapter.MacApiError("api failed")

    monkeypatch.setattr(hermes_adapter, "_cmd_summary", fail)
    assert hermes_adapter.main(["summary", "task"]) == 1
    assert "api failed" in capsys.readouterr().err


def test_main_lazily_resolves_fleet_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from mac import fleet_env

    seen: dict[str, Any] = {}

    class Parser:
        def parse_args(self, _argv: Any) -> argparse.Namespace:
            return argparse.Namespace(
                token=None,
                fleet="fleet",
                func=lambda args: seen.update(token=args.token),
            )

    monkeypatch.setattr(hermes_adapter, "build_parser", lambda: Parser())
    monkeypatch.setattr(fleet_env, "resolve_first", lambda *_a, **_k: "lazy-token")
    assert hermes_adapter.main([]) == 0
    assert seen["token"] == "lazy-token"
