"""Additional worker message, registry, and evidence branch coverage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import worker


class _Client:
    def __init__(self):
        self.get_value = []
        self.posts = []
        self.requests = []
        self.fail = False

    def get(self, path):
        if self.fail:
            raise RuntimeError("offline")
        return self.get_value

    def post(self, path, payload):
        if self.fail:
            raise RuntimeError("offline")
        self.posts.append((path, payload))
        return {}

    def request(self, method, path, payload):
        self.requests.append((method, path, payload))
        return {}


def _instance(tmp_path, client=None):
    return worker.MacWorker(
        client or _Client(),
        "agent",
        tmp_path,
        lambda *_a: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def test_review_nudge_poll_filters_and_skipped_result(monkeypatch, tmp_path) -> None:
    client = _Client()
    instance = _instance(tmp_path, client)
    client.fail = True
    assert instance._process_review_nudges() is None
    client.fail = False
    client.get_value = {}
    client.post = lambda *_a, **_k: {}
    assert instance._process_review_nudges() is None
    statuses = []
    monkeypatch.setattr(instance, "_handle_status_update_message", statuses.append)
    monkeypatch.setattr(
        instance,
        "_handle_review_verdict_nudge",
        lambda *_a: worker.WorkerRunResult(status="review_not_claimable"),
    )
    messages = [
        "bad",
        {"message_type": "status_update"},
        {"message_type": "other"},
        {"message_type": "nudge", "payload": "bad"},
        {"message_type": "nudge", "payload": {"reason": "other"}},
        {"message_type": "nudge", "payload": {"reason": "produce_review_verdict"}},
    ]
    client.post = lambda *_a, **_k: messages
    assert instance._process_review_nudges().status == "review_not_claimable"
    assert statuses == [{"message_type": "status_update"}]


def test_status_update_message_validation_success_and_failure(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    instance._handle_status_update_message({"payload": "bad"})
    instance._handle_status_update_message({"payload": {"schema": "other"}})
    instance.status_update_sink = lambda *_a: {"status": "sent", "sent": 1}
    instance._handle_status_update_message(
        {"id": "m", "payload": {"schema": "mac.notifier.task_progress.v1"}}
    )
    instance.status_update_sink = lambda *_a: (_ for _ in ()).throw(RuntimeError("send failed"))
    instance._handle_status_update_message(
        {"id": "m", "payload": {"schema": "mac.notifier.task_progress.v1"}}
    )


def test_status_update_slack_routing_matrix(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    assert (
        instance._send_status_update_to_home_channels({"channel_type": "email"})["status"]
        == "skipped"
    )
    monkeypatch.setattr(worker, "_load_slack_accounts", lambda *_a: [])
    monkeypatch.setattr(worker, "_load_slack_home_channels", lambda *_a: [])
    assert instance._send_status_update_to_home_channels({})["status"] == "skipped"

    accounts = [
        {"name": "good", "bot_token": "token"},
        {"name": "fail", "token": "fail-token"},
        {"name": "raise", "slack_bot_token": "raise-token"},
        {"name": "empty"},
    ]
    channels = [
        {},
        {"channel_id": "other", "team_id": "T", "name": "good"},
        {"channel_id": "target", "team_id": "OTHER", "name": "good"},
        {"channel_id": "target", "team_id": "T", "name": "missing"},
        {"channel_id": "target", "team_id": "T", "name": "empty"},
        {"channel_id": "target", "team_id": "T", "name": "good"},
        {"channel_id": "target", "team_id": "T", "name": "fail"},
        {"channel_id": "target", "team_id": "T", "name": "raise"},
    ]
    monkeypatch.setattr(worker, "_load_slack_accounts", lambda *_a: accounts)
    monkeypatch.setattr(worker, "_load_slack_home_channels", lambda *_a: channels)

    class WebClient:
        def __init__(self, token):
            self.token = token

        def chat_postMessage(self, **_kwargs):
            if self.token == "raise-token":
                raise RuntimeError("slack down")
            return {"ok": self.token != "fail-token"}

    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", WebClient)
    result = instance._send_status_update_to_home_channels(
        {
            "target": {"channel_type": "slack", "external_id": "T/target"},
            "notification": {"title": "Done", "body": "Complete"},
        }
    )
    assert result == {"status": "sent", "sent": 1, "skipped": 5, "failed": 2}


def test_review_verdict_nudge_invalid_claim_and_repository_error(monkeypatch, tmp_path) -> None:
    client = _Client()
    instance = _instance(tmp_path, client)
    assert instance._handle_review_verdict_nudge({}, {}).status == "review_nudge_invalid"
    client.post = lambda *_a, **_k: {"status": "busy", "reason": "claimed"}
    result = instance._handle_review_verdict_nudge(
        {}, {"task_id": "t", "review_id": "r", "executor_evidence_id": "e"}
    )
    assert result.status == "review_not_claimable"
    client.post = lambda *_a, **_k: {"status": "claimed"}
    client.get = lambda *_a, **_k: {}
    monkeypatch.setattr(
        instance,
        "_prepare_review_workspace",
        lambda *_a, **_k: (_ for _ in ()).throw(
            worker.RepositoryAccessError("denied", failure_class="auth")
        ),
    )
    monkeypatch.setattr(instance, "_advance_review_workflow_after_verdict", lambda *_a: None)
    client.fail = True
    result = instance._handle_review_verdict_nudge(
        {}, {"task_id": "t", "review_id": "r", "executor_evidence_id": "e"}
    )
    assert result.status == "review_verdict_failed"


def test_agentbus_control_filters_and_handlers(monkeypatch, tmp_path) -> None:
    client = _Client()
    instance = _instance(tmp_path, client)
    instance.agentbus_control_enabled = False
    assert instance._process_agentbus_control() is None
    instance.agentbus_control_enabled = True
    client.fail = True
    assert instance._process_agentbus_control() is None
    client.fail = False
    client.get_value = {}
    assert instance._process_agentbus_control() is None
    streams = [
        "bad",
        {},
        {"id": "done"},
        {"id": "wrong", "recipient_agent_id": "other"},
        {"id": "unknown", "recipient_agent_id": "agent", "topic": "other"},
        {
            "id": "repo",
            "recipient_agent_id": "agent",
            "topic": worker.REPO_UPDATE_TOPIC,
            "content_type": worker.REPO_UPDATE_CONTENT_TYPE,
        },
        {
            "id": "debug",
            "recipient_agent_id": "agent",
            "topic": worker.DEBUG_TERMINAL_OPEN_TOPIC,
            "content_type": worker.DEBUG_TERMINAL_OPEN_CONTENT_TYPE,
        },
    ]
    client.get_value = streams
    monkeypatch.setattr(instance, "_load_agentbus_control_state", lambda: ["done"])
    saved = []
    monkeypatch.setattr(
        instance, "_save_agentbus_control_state", lambda state: saved.append(list(state))
    )
    monkeypatch.setattr(
        instance, "_handle_debug_terminal_open_stream", lambda *_a: {"status": "opened"}
    )
    monkeypatch.setattr(
        instance,
        "_handle_repo_update_stream",
        lambda *_a: {"status": "updated", "restart_requested": True},
    )
    monkeypatch.setattr(instance, "_publish_repo_update_result", lambda *_a, **_k: None)
    monkeypatch.setattr(
        instance, "_run_repo_update_service_restarts", lambda *_a: {"status": "service_restarted"}
    )
    result = instance._process_agentbus_control()
    assert result["restart_requested"] is True
    assert saved


def test_control_stream_handler_exception_paths(monkeypatch, tmp_path) -> None:
    client = _Client()
    instance = _instance(tmp_path, client)
    client.get_value = [{"payload": {"x": 1}}]
    monkeypatch.setattr(
        instance,
        "_execute_debug_terminal_open",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("debug")),
    )
    monkeypatch.setattr(instance, "_publish_debug_terminal_output", lambda *_a, **_k: None)
    assert instance._handle_debug_terminal_open_stream({"id": "s"})["status"] == "error"
    monkeypatch.setattr(
        instance, "_execute_repo_update", lambda *_a: (_ for _ in ()).throw(RuntimeError("repo"))
    )
    assert instance._handle_repo_update_stream({"id": "s"})["status"] == "error"


def test_local_registry_dict_list_invalid_and_enabled(tmp_path) -> None:
    path = tmp_path / "fleets.yaml"
    assert (
        worker._agent_configured_in_local_registry(
            fleet_name="fleet", agent_name="agent", registry_path=path
        )
        is False
    )
    path.write_text("bad: [")
    assert (
        worker._agent_configured_in_local_registry(
            fleet_name="fleet", agent_name="agent", registry_path=path
        )
        is False
    )
    path.write_text("fleets: invalid\n")
    assert (
        worker._agent_configured_in_local_registry(
            fleet_name="fleet", agent_name="agent", registry_path=path
        )
        is False
    )
    path.write_text("""
fleets:
  fleet:
    fleet_name: fleet
    agents:
      - bad
      - name: other
      - name: agent
        enabled: false
""")
    assert (
        worker._agent_configured_in_local_registry(
            fleet_name="fleet", agent_name="agent", registry_path=path
        )
        is False
    )
    path.write_text("""
fleets:
  - hub_agent: hub
    fleet_name: fleet
    agents:
      - name: agent
""")
    assert (
        worker._agent_configured_in_local_registry(
            fleet_name="fleet", agent_name="agent", registry_path=path
        )
        is True
    )


def test_worker_evidence_and_text_helper_edges(tmp_path) -> None:
    assert worker._required_changed_files_from_task({"metadata": "bad"}) == []
    task = {"metadata": {"acceptance": {"required_changed_files": ["src/*.py", "src/*.py"]}}}
    assert worker._required_changed_files_from_task(task) == ["src/*.py"]
    assert worker._repo_path_satisfies_requirement("src/a.py", "src/*.py") is True
    assert worker._repo_path_satisfies_requirement("", "src/a.py") is False
    assert worker._worker_required_changed_file_problems(task, {"repo": {"files_changed": []}})
    assert worker._remote_branch_from_ref("heads/main") == "main"
    assert worker._remote_branch_from_ref("origin/topic") == "topic"
    assert worker._remote_branch_from_ref("refs/tags/v1") == ""
    assert worker._worker_int_value("bad") is None
    assert worker._prose_tail("diff --git x\n+++ file\nplain useful summary here\ntoken", 2) == [
        "plain useful summary here"
    ]
    assert worker._prose_tail("token\nother", 1) == ["other"]


def test_agentbus_state_file_invalid_and_save_failure(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    path = instance.agentbus_control_state_path
    path.write_text("bad")
    assert instance._load_agentbus_control_state() == []
    path.write_text(json.dumps({"processed_stream_ids": "bad"}))
    assert instance._load_agentbus_control_state() == []
    monkeypatch.setattr(Path, "replace", lambda *_a: (_ for _ in ()).throw(OSError("readonly")))
    instance._save_agentbus_control_state(["one"])
