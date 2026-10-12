"""The hub's Slack relay carries agent reports and questions without a task agent.

rocky became hub-and-chat only (#1006): its task agent was retired and its
agent record deleted. The notifier still queues reports and questions for that
agent, and only the task agent used to post them, so they sat ``queued`` and
nobody was told. ``mac.slack_relay`` drains the same mailbox with the hub's
admin token, posts through Hermes's Slack accounts, relays answers back, and
never touches a task.
"""

from __future__ import annotations

import json
import plistlib
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from mac import slack_relay
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on
from tests.test_chat_question_replies import _FakeSlack, _board, _parked_on_board_question


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="slack-relay-test-key-with-32-bytes!",
    )
    machine = cp.register_machine("hub", machine_id="machine_hub", labels={})
    cp.register_agent(machine.id, "rocky", ["python"], resources={}, agent_id="agent_rocky")
    cp.register_agent(machine.id, "alpha", ["python"], resources={}, agent_id="agent_alpha")
    cp.configure_notifier_channel(
        "questions-slack",
        "slack",
        event_types=["agent.report", "task.question"],
        target={"agent_id": "agent_rocky"},
    )
    cp.configure_notifier_channel(
        "task-progress", "slack", event_types=["task.*"], target={"agent_id": "agent_other"},
        enabled=False,
    )
    # What fleet-update does to a hub-and-chat-only hub.
    cp.delete_agent("agent_rocky", actor="fleet-update")
    return cp


def _client(cp: ControlPlane, calls: List[str]) -> MacApiClient:
    http = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={"admin": {"scopes": ["admin", "dispatch", "read", "write"]}},
        )
    )

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        calls.append("%s %s" % (method, path))
        kwargs: Dict[str, Any] = {"headers": {"Authorization": "Bearer admin"}}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(http, method.lower())(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text, status_code=response.status_code)
        return response.json() if response.content else None

    return MacApiClient("http://hub.test", transport=transport)


def _slack(tmp_path: Path, monkeypatch) -> _FakeSlack:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "slack_accounts.json").write_text(
        json.dumps([{"name": "team", "bot_token": "xoxb-one"}]), encoding="utf-8"
    )
    (hermes_home / "slack_home_channels.json").write_text(
        json.dumps([{"name": "team", "team_id": "T1", "channel_id": "C1"}]), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    slack = _FakeSlack()
    monkeypatch.setitem(sys.modules, "slack_sdk", types.SimpleNamespace(WebClient=slack.client("")))
    return slack


def test_only_enabled_slack_report_or_question_channels_are_relayed():
    cp = _plane()
    assert slack_relay.notifier_targets(_client(cp, [])) == ["agent_rocky"]


def test_a_report_for_the_retired_hub_agent_reaches_slack_once(tmp_path: Path, monkeypatch):
    cp = _plane()
    slack = _slack(tmp_path, monkeypatch)
    cp.file_human_report(
        "I removed natasha's supervisor image", reporter="claude", reporter_kind="operator"
    )
    cp.deliver_pending_notifications()
    calls: List[str] = []
    client = _client(cp, calls)

    slack_relay.run(client=client, mac_home=tmp_path / "mac", poll_seconds=5, once=True)
    slack_relay.run(client=client, mac_home=tmp_path / "mac", poll_seconds=5, once=True)

    assert len(slack.posted) == 1
    assert "natasha's supervisor image" in slack.posted[0]["text"]
    assert [m.status for m in cp.list_messages("agent_rocky")] == ["delivered"]
    # A relay, not a worker: it never asks for, claims or touches a task.
    assert not [c for c in calls if "/tasks" in c or "/claim" in c or "/leases" in c]


def test_a_question_is_posted_and_its_thread_reply_answers_it(tmp_path: Path, monkeypatch):
    cp = _plane()
    slack = _slack(tmp_path, monkeypatch)
    task, question = _parked_on_board_question(cp)
    cp.deliver_pending_notifications()
    client = _client(cp, [])
    mac_home = tmp_path / "mac"

    slack_relay.run(client=client, mac_home=mac_home, poll_seconds=5, once=True)
    parent = slack.posted[0]
    assert parent["text"].startswith("*Q1* Answer needed: Pick a region")

    slack.threads[parent["ts"]] = [{"ts": "200.1", "user": "U1", "text": "eu"}]
    slack_relay.run(client=client, mac_home=mac_home, poll_seconds=5, once=True)

    answers = _board(cp, task.id, "answer")
    assert [(m["body"], m["reply_to"]) for m in answers] == [("eu", question["id"])]
    assert cp.get_task(task.id).state == TaskState.OPEN.value


def test_question_codes_continue_from_the_retired_agents_state(tmp_path: Path):
    mac_home = tmp_path / "mac"
    legacy = mac_home / slack_relay.LEGACY_STATE
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"next_code": 6, "questions": []}), encoding="utf-8")

    path = slack_relay.state_path_for(mac_home, "agent_rocky")

    assert json.loads(path.read_text(encoding="utf-8"))["next_code"] == 6
    path.write_text(json.dumps({"next_code": 9}), encoding="utf-8")
    assert json.loads(slack_relay.state_path_for(mac_home, "agent_rocky").read_text())["next_code"] == 9


def test_install_loads_the_launch_agent_then_restarts_it_on_new_code(tmp_path: Path):
    mac_home, agents = tmp_path / "mac", tmp_path / "LaunchAgents"
    calls: List[List[str]] = []
    loaded = {"yes": False}

    def runner(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            return (0 if loaded["yes"] else 113, "")
        if cmd[:2] == ["launchctl", "bootstrap"]:
            loaded["yes"] = True
        return (0, "")

    first = slack_relay.install(mac_home=mac_home, agents_dir=agents, system="Darwin", runner=runner)
    second = slack_relay.install(mac_home=mac_home, agents_dir=agents, system="Darwin", runner=runner)

    assert first == {"status": "ok", "units": "updated: launcher, plist"}
    assert second == {"status": "ok", "units": "unchanged"}
    verbs = [c[1] for c in calls if c[0] == "launchctl"]
    assert verbs == ["print", "bootstrap", "print", "kickstart"]
    plist = plistlib.loads((agents / "com.mac.slack-relay.plist").read_bytes())
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    launcher = (mac_home / "bin" / "slack-relay").read_text()
    assert "-m mac.slack_relay run" in launcher and '. "%s/mac.env"' % mac_home in launcher


def test_install_is_a_no_op_off_macos(tmp_path: Path):
    result = slack_relay.install(mac_home=tmp_path, system="Linux", runner=lambda c: (1, "unused"))
    assert result["status"] == "skipped"
