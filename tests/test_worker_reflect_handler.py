"""Tests for the reflect-request handler in MacWorker._process_agentbus_control."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from mac import worker
from mac.agentbus_control import (
    REFLECT_REQUEST_CONTENT_TYPE,
    REFLECT_REQUEST_TOPIC,
    REFLECT_RESULT_CONTENT_TYPE,
    REFLECT_RESULT_TOPIC,
)


# ---------------------------------------------------------------------------
# Minimal test doubles
# ---------------------------------------------------------------------------


class _Client:
    """Minimal fake agentbus client."""

    def __init__(self, chunks: Optional[List[Any]] = None) -> None:
        self._chunks = chunks or []
        self.posts: List[tuple] = []
        # Streams returned by the control-poll GET
        self.streams: List[Dict] = []

    def get(self, path: str) -> Any:
        if "/chunks" in path:
            return self._chunks
        # top-level stream poll
        return self.streams

    def post(self, path: str, payload: Dict) -> Dict:
        self.posts.append((path, payload))
        return {}


def _instance(tmp_path: Path, client: Optional[_Client] = None) -> worker.MacWorker:
    return worker.MacWorker(
        client or _Client(),
        "agent_test",
        tmp_path / "workspace",
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
        agentbus_control_state_path=tmp_path / ".mac-agentbus-control.json",
    )


def _reflect_stream(**extra) -> Dict:
    """Build a minimal agentbus stream dict for a reflect request."""
    base = {
        "id": "stream-reflect-1",
        "topic": REFLECT_REQUEST_TOPIC,
        "content_type": REFLECT_REQUEST_CONTENT_TYPE,
        "recipient_agent_id": "agent_test",
        "sender_agent_id": "agent_sender",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Unit tests for _handle_reflect_request_stream
# ---------------------------------------------------------------------------


class TestHandleReflectRequestStream:
    def test_happy_path_publishes_result(self, tmp_path: Path, monkeypatch) -> None:
        """A successful reflect round-trip should post a REFLECT_RESULT chunk."""
        client = _Client(
            chunks=[{"payload": {"query": "What are you?", "request_id": "req-1"}}]
        )
        inst = _instance(tmp_path, client)
        monkeypatch.setattr(inst, "_run_reflect_query", lambda q, **kw: "I am a mac worker.")
        observations = []
        monkeypatch.setattr(inst, "_observe_log", lambda name, **kw: observations.append(name))

        stream = _reflect_stream()
        result = inst._handle_reflect_request_stream(stream)

        assert result["status"] == "completed"
        # One post to /agentbus with the right content-type
        reflect_posts = [
            p for p in client.posts if p[1].get("content_type") == REFLECT_RESULT_CONTENT_TYPE
        ]
        assert len(reflect_posts) == 1
        posted = reflect_posts[0][1]
        assert posted["topic"] == REFLECT_RESULT_TOPIC
        assert posted["recipient_agent_id"] == "agent_sender"
        assert posted["payload"]["response"] == "I am a mac worker."
        assert posted["payload"]["request_id"] == "req-1"
        assert "worker.agentbus.reflect.completed" in observations

    def test_disabled_returns_error_payload(self, tmp_path: Path, monkeypatch) -> None:
        """When MAC_REFLECT_ENABLED=false an error result is published, not a query."""
        client = _Client(chunks=[])
        inst = _instance(tmp_path, client)
        monkeypatch.setenv("MAC_REFLECT_ENABLED", "false")
        query_called = []
        monkeypatch.setattr(inst, "_run_reflect_query", lambda *a, **kw: query_called.append(1) or "")
        observations = []
        monkeypatch.setattr(inst, "_observe_log", lambda name, **kw: observations.append(name))

        result = inst._handle_reflect_request_stream(_reflect_stream())

        assert result["status"] == "error"
        assert not query_called
        reflect_posts = [
            p for p in client.posts if p[1].get("content_type") == REFLECT_RESULT_CONTENT_TYPE
        ]
        assert len(reflect_posts) == 1
        assert "disabled" in reflect_posts[0][1]["payload"]["response"]
        assert "worker.agentbus.reflect.error" in observations

    def test_default_query_used_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        """If the payload has no 'query' field the default query is passed."""
        client = _Client(chunks=[{"payload": {}}])
        inst = _instance(tmp_path, client)
        captured = []
        monkeypatch.setattr(
            inst, "_run_reflect_query", lambda q, **kw: captured.append(q) or "ok"
        )
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)

        inst._handle_reflect_request_stream(_reflect_stream())

        assert captured
        assert "identity" in captured[0].lower() or "runtime" in captured[0].lower()

    def test_response_truncated_to_300_words(self, tmp_path: Path, monkeypatch) -> None:
        """Response longer than 300 words is truncated at the word boundary."""
        long_response = " ".join(["word"] * 400)
        client = _Client(chunks=[{"payload": {"query": "describe yourself"}}])
        inst = _instance(tmp_path, client)
        monkeypatch.setattr(inst, "_run_reflect_query", lambda q, **kw: long_response)
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)

        inst._handle_reflect_request_stream(_reflect_stream())

        reflect_posts = [
            p for p in client.posts if p[1].get("content_type") == REFLECT_RESULT_CONTENT_TYPE
        ]
        payload = reflect_posts[0][1]["payload"]
        assert payload["word_count"] == 300
        assert len(payload["response"].split()) == 300

    def test_no_sender_skips_publish(self, tmp_path: Path, monkeypatch) -> None:
        """If the stream has no sender_agent_id, the result is not published."""
        client = _Client(chunks=[])
        inst = _instance(tmp_path, client)
        monkeypatch.setattr(inst, "_run_reflect_query", lambda q, **kw: "hello")
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)

        stream = _reflect_stream(sender_agent_id="")
        inst._handle_reflect_request_stream(stream)

        # No /agentbus post should happen for reflect result
        reflect_posts = [
            p for p in client.posts if p[1].get("content_type") == REFLECT_RESULT_CONTENT_TYPE
        ]
        assert len(reflect_posts) == 0

    def test_publish_failure_is_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        """A publish error must not propagate out of the handler."""
        client = _Client(chunks=[{"payload": {"query": "test"}}])

        def _bad_post(path, payload):
            if "content_type" in payload and payload["content_type"] == REFLECT_RESULT_CONTENT_TYPE:
                raise RuntimeError("network down")
            return {}

        client.post = _bad_post  # type: ignore[method-assign]
        inst = _instance(tmp_path, client)
        monkeypatch.setattr(inst, "_run_reflect_query", lambda q, **kw: "hello")
        observations = []
        monkeypatch.setattr(inst, "_observe_log", lambda name, **kw: observations.append(name))

        # Must not raise
        result = inst._handle_reflect_request_stream(_reflect_stream())
        assert result["status"] == "completed"
        assert "worker.agentbus.reflect.publish_failed" in observations

    def test_empty_payload_uses_defaults(self, tmp_path: Path, monkeypatch) -> None:
        """An empty chunk list still produces a valid result with defaults."""
        client = _Client(chunks=[])
        inst = _instance(tmp_path, client)
        monkeypatch.setattr(inst, "_run_reflect_query", lambda q, **kw: "default answer")
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)

        result = inst._handle_reflect_request_stream(_reflect_stream())
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Unit tests for _run_reflect_query
# ---------------------------------------------------------------------------


class TestRunReflectQuery:
    def test_returns_stdout_on_success(self, tmp_path: Path, monkeypatch) -> None:
        inst = _instance(tmp_path)
        monkeypatch.setattr(
            worker.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(
                args=a[0], returncode=0, stdout="I am ready.\n", stderr=""
            ),
        )
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)
        response = inst._run_reflect_query("Who are you?")
        assert response == "I am ready."

    def test_runtime_query_passed_to_openclaw_in_openshell(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        inst = _instance(tmp_path)
        captured: Dict[str, Any] = {}
        agent_bin = tmp_path / "openclaw-agent"

        def _capture_run(argv, *, env, timeout, **kw):
            captured["argv"] = argv
            captured["env"] = env
            captured["timeout"] = timeout
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="runtime answer", stderr=""
            )

        monkeypatch.setenv("MAC_OPENCLAW_AGENT_BIN", str(agent_bin))
        monkeypatch.setenv("MAC_REFLECT_TIMEOUT", "12.5")
        monkeypatch.setattr(worker.subprocess, "run", _capture_run)
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)

        response = inst._run_reflect_query("What task are you running?")

        assert response == "runtime answer"
        argv = captured["argv"]
        assert argv[:4] == [str(agent_bin), "--agent", "main", "--message"]
        runtime_query = argv[4]
        assert "Requester query:\nWhat task are you running?" in runtime_query
        assert "OpenClaw workspace context" in runtime_query
        assert "host or command inventory" in runtime_query
        assert "300 words" in runtime_query
        assert argv[5:] == ["--session-id", "mac-reflect-agent_test", "--json"]
        env = captured["env"]
        assert env["MAC_AGENT_ID"] == "agent_test"
        assert env["MAC_WORKER_AGENT_ID"] == "agent_test"
        assert captured["timeout"] == 12.5

    def test_extracts_text_from_openclaw_json(self, tmp_path: Path, monkeypatch) -> None:
        inst = _instance(tmp_path)
        monkeypatch.setattr(
            worker.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(
                args=a[0],
                returncode=0,
                stdout='{"payloads":[{"text":"OpenClaw answer"}]}',
                stderr="",
            ),
        )
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)
        assert inst._run_reflect_query("Who?") == "OpenClaw answer"

    def test_nonzero_returncode_returns_error_text(self, tmp_path: Path, monkeypatch) -> None:
        inst = _instance(tmp_path)
        monkeypatch.setattr(
            worker.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(
                args=a[0], returncode=1, stdout="", stderr="model error"
            ),
        )
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)
        response = inst._run_reflect_query("Who?")
        assert "returncode 1" in response
        assert "model error" in response

    def test_timeout_returns_timeout_message(self, tmp_path: Path, monkeypatch) -> None:
        inst = _instance(tmp_path)
        observations = []
        captured = {}

        def _timeout_run(*a, **kw):
            captured.update(kw)
            raise subprocess.TimeoutExpired(cmd="openclaw-agent", timeout=120)

        monkeypatch.setenv("MAC_REFLECT_TIMEOUT", "7")
        monkeypatch.setattr(worker.subprocess, "run", _timeout_run)
        monkeypatch.setattr(inst, "_observe_log", lambda name, **kw: observations.append(name))

        response = inst._run_reflect_query("Q?", stream_id="s1")
        assert "timed out after 7 seconds" in response
        assert captured["timeout"] == 7.0
        assert "worker.agentbus.reflect.error" in observations

    def test_subprocess_error_returns_error_message(self, tmp_path: Path, monkeypatch) -> None:
        inst = _instance(tmp_path)
        observations = []

        def _bad_run(*a, **kw):
            raise FileNotFoundError("openclaw-agent not found")

        monkeypatch.setattr(worker.subprocess, "run", _bad_run)
        monkeypatch.setattr(inst, "_observe_log", lambda name, **kw: observations.append(name))

        response = inst._run_reflect_query("Q?", stream_id="s2")
        assert "reflect query failed" in response
        assert "worker.agentbus.reflect.error" in observations

    def test_stream_id_is_sanitized_for_openclaw_session(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        inst = _instance(tmp_path)
        captured_argv = []

        def _capture_run(argv, *, env, **kw):
            captured_argv.extend(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="ok", stderr=""
            )

        monkeypatch.setattr(worker.subprocess, "run", _capture_run)
        monkeypatch.setattr(inst, "_observe_log", lambda *a, **kw: None)

        inst._run_reflect_query("Q?", stream_id="stream/with spaces")
        assert captured_argv[captured_argv.index("--session-id") + 1] == (
            "mac-reflect-stream-with-spaces"
        )


# ---------------------------------------------------------------------------
# Integration: _process_agentbus_control dispatches reflect topic
# ---------------------------------------------------------------------------


class TestProcessAgentbusControlReflectDispatch:
    def test_reflect_topic_dispatched(self, tmp_path: Path, monkeypatch) -> None:
        """_process_agentbus_control must invoke _handle_reflect_request_stream for the reflect topic."""
        client = _Client(chunks=[])
        client.streams = [
            {
                "id": "s1",
                "topic": REFLECT_REQUEST_TOPIC,
                "content_type": REFLECT_REQUEST_CONTENT_TYPE,
                "recipient_agent_id": "agent_test",
                "sender_agent_id": "agent_sender",
            }
        ]
        inst = _instance(tmp_path, client)
        handled = []

        monkeypatch.setattr(
            inst,
            "_handle_reflect_request_stream",
            lambda s: handled.append(s) or {"status": "completed", "summary": "ok", "stream_id": "s1"},
        )

        inst._process_agentbus_control()
        assert len(handled) == 1
        assert handled[0]["id"] == "s1"

    def test_unknown_topic_not_dispatched_to_reflect(self, tmp_path: Path, monkeypatch) -> None:
        """Unknown topics must fall through without invoking the reflect handler."""
        client = _Client(chunks=[])
        client.streams = [
            {
                "id": "s2",
                "topic": "mac.unknown.topic.v1",
                "content_type": "application/vnd.mac.unknown+json",
                "recipient_agent_id": "agent_test",
                "sender_agent_id": "agent_sender",
            }
        ]
        inst = _instance(tmp_path, client)
        handled = []
        monkeypatch.setattr(
            inst,
            "_handle_reflect_request_stream",
            lambda s: handled.append(s),
        )

        inst._process_agentbus_control()
        assert not handled
