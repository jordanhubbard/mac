"""Isolation tests for the extracted worker reflect boundary."""

from __future__ import annotations

import json
import subprocess

import pytest

from mac.worker_reflect import ReflectMixin, _bounded_float, _env_bool


class _Client:
    def __init__(self) -> None:
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        return {}


class _Worker(ReflectMixin):
    def __init__(self) -> None:
        self.agent_id = "agent_reflect"
        self.client = _Client()
        self.logs = []

    def _observe_log(self, name, **kwargs):
        self.logs.append((name, kwargs))


def test_bounded_float_defaults_and_clamps() -> None:
    assert _bounded_float("bad", 1, 10, 4) == 4
    assert _bounded_float(0, 1, 10, 4) == 1
    assert _bounded_float(50, 1, 10, 4) == 10


def test_env_bool_honors_false_values(monkeypatch) -> None:
    monkeypatch.delenv("MAC_REFLECT_TEST", raising=False)
    assert _env_bool("MAC_REFLECT_TEST", True) is True
    monkeypatch.setenv("MAC_REFLECT_TEST", "off")
    assert _env_bool("MAC_REFLECT_TEST", True) is False


def test_runtime_query_contains_request_and_bound() -> None:
    query = _Worker()._reflect_runtime_query("describe memory")
    assert "describe memory" in query
    assert "at or below 300 words" in query
    assert "OpenClaw runtime" in query


def test_run_reflect_extracts_nested_json_text(monkeypatch) -> None:
    payload = {"result": {"messages": [{"content": "runtime answer"}]}}
    monkeypatch.setattr(
        "mac.worker_reflect.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    assert _Worker()._run_reflect_query("status", stream_id="stream/1") == "runtime answer"


def test_run_reflect_timeout_is_observable(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 7)

    monkeypatch.setenv("MAC_REFLECT_TIMEOUT", "7")
    monkeypatch.setattr("mac.worker_reflect.subprocess.run", timeout)
    worker = _Worker()
    assert worker._run_reflect_query("status", stream_id="s") == "reflect query timed out after 7 seconds."
    assert worker.logs[-1][1]["detail"]["reason"] == "timeout"


def test_publish_result_targets_the_request_sender() -> None:
    worker = _Worker()
    worker._publish_reflect_result(
        {"id": "stream", "sender_agent_id": "agent_sender"}, {"response": "ok"}
    )
    path, body = worker.client.posts[0]
    assert path == "/agentbus"
    assert body["sender_agent_id"] == "agent_reflect"
    assert body["recipient_agent_id"] == "agent_sender"
