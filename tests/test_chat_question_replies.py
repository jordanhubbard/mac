"""A reply to a question in Slack becomes that question's answer on the board.

The worker that posted a ``task.question`` to a Slack channel watches the
message's thread. A person's reply is relayed to the hub, which records it as
the answer: a board answer to the agent's question (resuming a task parked on
it), or a direct answer to a task parked with ``mac task ask``. The worker
stops watching once the hub says the question no longer wants an answer.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on
from mac.worker import MacWorker, WorkerExecution


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="chat-question-test-key-with-32-bytes",
    )
    machine = cp.register_machine("host", machine_id="machine_q", labels={})
    cp.register_agent(machine.id, "alpha", ["python"], resources={}, agent_id="agent_alpha")
    cp.register_agent(machine.id, "relay", ["python"], resources={}, agent_id="agent_relay")
    cp.register_agent(machine.id, "other", ["python"], resources={}, agent_id="agent_other")
    return cp


def _board(cp: ControlPlane, task_id: str, *kinds: str) -> List[Dict[str, Any]]:
    return cp.list_task_messages(task_id, kinds=list(kinds))["messages"]


def _question_note(cp: ControlPlane, task_id: str) -> str:
    rows = cp.store.query_all(
        "SELECT id FROM operator_notifications WHERE event_type = 'task.question' AND subject_id = ?"
        " ORDER BY created_at",
        (task_id,),
    )
    assert rows, "no task.question notification for %s" % task_id
    return rows[-1]["id"]


def _parked_on_board_question(cp: ControlPlane):
    task = cp.create_task("Pick a region")
    _, lease = cp.claim_task(task.id, "agent_alpha")
    cp.start_task(task.id, "agent_alpha", lease_id=lease.id)
    question = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="Which region?",
        metadata={"blocking": True},
    )
    cp.transition_task(
        task.id,
        "needs_input",
        "agent_alpha",
        {"questions": [{"question": "Which region?"}], "board_message_id": question["id"]},
        lease_id=lease.id,
    )
    return task, question


def test_a_chat_reply_answers_the_parked_question_once_and_resumes_the_task():
    cp = _plane()
    task, question = _parked_on_board_question(cp)
    note = _question_note(cp, task.id)

    result = cp.relay_question_reply(
        note, body="eu", author="Pat (slack:U1)", ref="slack:T/C/1.1", source="slack",
        relayed_by="agent_relay",
    )
    again = cp.relay_question_reply(
        note, body="eu", author="Pat (slack:U1)", ref="slack:T/C/1.1", source="slack",
        relayed_by="agent_relay",
    )

    assert result["status"] == "answered" and result["question_open"] is False
    assert again["status"] == "duplicate"
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    answers = _board(cp, task.id, "answer")
    assert [(m["author_kind"], m["author"], m["body"], m["reply_to"]) for m in answers] == [
        ("human", "Pat (slack:U1)", "eu", question["id"])
    ]
    assert answers[0]["metadata"]["source"] == "slack"
    assert answers[0]["metadata"]["relayed_by"] == "agent_relay"


def test_a_later_reply_to_an_answered_question_is_kept_as_a_message():
    cp = _plane()
    task = cp.create_task("Pick a region")
    question = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="Which region?"
    )
    note = _question_note(cp, task.id)

    first = cp.relay_question_reply(
        note, body="us", author="Pat", ref="slack:T/C/1", source="slack", relayed_by="agent_relay"
    )
    second = cp.relay_question_reply(
        note, body="actually eu", author="Sam", ref="slack:T/C/2", source="slack",
        relayed_by="agent_relay",
    )

    assert first["status"] == "answered"
    assert second["status"] == "not_waiting" and second["question_open"] is False
    kinds = [(m["kind"], m["body"], m["reply_to"]) for m in _board(cp, task.id)]
    assert ("answer", "us", question["id"]) in kinds
    assert ("message", "actually eu", question["id"]) in kinds


def test_a_task_parked_by_mac_task_ask_is_answered_directly():
    cp = _plane()
    task = cp.create_task("Pick a region")
    cp.request_task_input(task.id, [{"question": "Which region?"}], "operator")
    note = _question_note(cp, task.id)
    assert cp.question_status(note)["question_open"] is True

    result = cp.relay_question_reply(
        note, body="eu", author="Pat", ref="slack:T/C/9", source="slack", relayed_by="agent_relay"
    )

    assert result["status"] == "answered"
    assert cp.get_task(task.id).state != TaskState.NEEDS_INPUT.value
    assert cp.question_status(note)["question_open"] is False


def test_a_non_blocking_question_reaches_a_slack_channel_subscribed_to_questions():
    cp = _plane()
    cp.configure_notifier_channel(
        "questions", "slack", event_types=["task.question"], target={"agent_id": "agent_relay"}
    )
    task = cp.create_task("Pick a region")
    cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="Which region?"
    )

    cp.deliver_pending_notifications()

    payloads = [m.payload for m in cp.list_messages("agent_relay")]
    assert [p["notification"]["event_type"] for p in payloads] == ["task.question"]


def _api(cp: ControlPlane) -> TestClient:
    def worker(agent_id: str) -> Dict[str, Any]:
        return {
            "scopes": ["agent", "dispatch", "read", "write"],
            "tenant_id": None,
            "agent_id": agent_id,
            "principal_kind": "worker",
        }

    return TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={"relay": worker("agent_relay"), "other": worker("agent_other")},
        )
    )


def test_only_the_agent_that_delivered_a_question_may_relay_replies_to_it():
    cp = _plane()
    cp.configure_notifier_channel(
        "questions", "slack", event_types=["task.question"], target={"agent_id": "agent_relay"}
    )
    task, _ = _parked_on_board_question(cp)
    cp.deliver_pending_notifications()
    note = _question_note(cp, task.id)
    client = _api(cp)
    reply = {"body": "eu", "ref": "slack:T/C/1", "author": "Pat", "source": "slack"}

    refused = client.post(
        "/notifications/%s/replies" % note, json=reply, headers={"Authorization": "Bearer other"}
    )
    peek = client.get("/notifications/%s/question" % note, headers={"Authorization": "Bearer other"})
    accepted = client.post(
        "/notifications/%s/replies" % note, json=reply, headers={"Authorization": "Bearer relay"}
    )

    assert refused.status_code == 403 and peek.status_code == 403
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "answered"
    assert cp.get_task(task.id).state == TaskState.OPEN.value


class _FakeSlack:
    """One Slack workspace: posted messages, thread replies, user names."""

    def __init__(self) -> None:
        self.posted: List[Dict[str, Any]] = []
        self.threads: Dict[str, List[Dict[str, Any]]] = {}
        self.channel: List[Dict[str, Any]] = []  # top-level messages people post

    def client(self, token: str) -> Any:
        slack = self

        class WebClient:
            def __init__(self, token: str) -> None:
                self.token = token

            def chat_postMessage(self, channel: str, text: str, thread_ts: Optional[str] = None):
                ts = "100.%d" % (len(slack.posted) + 1)
                slack.posted.append({"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts})
                return {"ok": True, "ts": ts}

            def conversations_replies(self, channel: str, ts: str, limit: int = 100):
                return {"messages": [{"ts": ts, "text": "question"}] + slack.threads.get(ts, [])}

            def conversations_history(self, channel: str, oldest: str, limit: int = 200):
                own = [{"ts": p["ts"], "bot_id": "B0", "text": p["text"]} for p in slack.posted]
                newest_first = sorted(own + slack.channel, key=lambda m: float(m["ts"]), reverse=True)
                return {"messages": [m for m in newest_first if float(m["ts"]) >= float(oldest)]}

            def users_info(self, user: str):
                return {"user": {"name": "pat", "profile": {"display_name": "Pat"}}}

        return WebClient


def _relay_worker(tmp_path: Path, monkeypatch, cp: ControlPlane) -> tuple:
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
    http = _api(cp)

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        kwargs: Dict[str, Any] = {"headers": {"Authorization": "Bearer relay"}}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(http, method.lower())(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text, status_code=response.status_code)
        return response.json() if response.content else None

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=transport),
        "agent_relay",
        tmp_path / "ws",
        lambda _task, _dir: WorkerExecution(0, "unused"),
    )
    return worker, slack


def test_the_worker_relays_a_slack_thread_reply_and_stops_watching(tmp_path: Path, monkeypatch):
    cp = _plane()
    cp.configure_notifier_channel(
        "questions", "slack", event_types=["task.question"], target={"agent_id": "agent_relay"}
    )
    task, question = _parked_on_board_question(cp)
    cp.deliver_pending_notifications()
    worker, slack = _relay_worker(tmp_path, monkeypatch, cp)

    worker._process_control_messages()
    assert len(slack.posted) == 1
    parent = slack.posted[0]
    assert parent["channel"] == "C1" and parent["text"].startswith("*Q1* Answer needed: Pick a region")
    assert "post `Q1 <your answer>` in this channel" in parent["text"]
    # The CLI command that answers it is left out: in Slack it invites Hermes
    # to answer for the person.
    assert "mac task" not in parent["text"]
    assert len(worker._load_chat_questions()) == 1

    worker._relay_chat_question_replies()  # no replies yet: nothing relayed, still watched
    assert len(worker._load_chat_questions()) == 1
    slack.threads[parent["ts"]] = [
        {"ts": "200.1", "bot_id": "B1", "text": "Hermes chiming in"},
        {"ts": "200.2", "user": "U1", "text": "eu"},
    ]
    worker._relay_chat_question_replies()

    answers = _board(cp, task.id, "answer")
    assert [(m["author"], m["body"], m["reply_to"]) for m in answers] == [
        ("Pat (slack:U1)", "eu", question["id"])
    ]
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    receipt = slack.posted[-1]
    assert receipt["thread_ts"] == parent["ts"] and "Recorded as the answer" in receipt["text"]
    assert worker._load_chat_questions() == []

    slack.threads[parent["ts"]].append({"ts": "200.3", "user": "U1", "text": "eu, final"})
    worker._relay_chat_question_replies()
    assert len(_board(cp, task.id, "answer", "message")) == 1


def test_the_worker_drops_a_question_answered_on_the_board(tmp_path: Path, monkeypatch):
    cp = _plane()
    cp.configure_notifier_channel(
        "questions", "slack", event_types=["task.question"], target={"agent_id": "agent_relay"}
    )
    task, question = _parked_on_board_question(cp)
    cp.deliver_pending_notifications()
    worker, slack = _relay_worker(tmp_path, monkeypatch, cp)
    worker._process_control_messages()
    assert len(worker._load_chat_questions()) == 1

    cp.post_task_message(
        task.id, author_kind="human", author="op", kind="answer", body="us", reply_to=question["id"]
    )
    worker._relay_chat_question_replies()

    assert worker._load_chat_questions() == []


def test_a_channel_message_starting_with_the_code_answers_the_question(tmp_path: Path, monkeypatch):
    cp = _plane()
    cp.configure_notifier_channel(
        "questions", "slack", event_types=["task.question"], target={"agent_id": "agent_relay"}
    )
    first, _ = _parked_on_board_question(cp)
    second = cp.create_task("Pick a size")
    cp.request_task_input(second.id, [{"question": "Which size?"}], "operator")
    cp.deliver_pending_notifications()
    worker, slack = _relay_worker(tmp_path, monkeypatch, cp)
    worker._process_control_messages()
    codes = sorted(e["code"] for e in worker._load_chat_questions())
    assert codes == ["Q1", "Q2"]
    second_code = next(
        e["code"] for e in worker._load_chat_questions() if e["notification_id"] == _question_note(cp, second.id)
    )

    slack.channel = [
        {"ts": "300.1", "user": "U1", "text": "blue"},  # no code: just chat
        {"ts": "300.2", "user": "U1", "text": "Q99 green"},  # no such question
        {"ts": "300.3", "user": "U1", "text": "%s: eu-west" % second_code.lower()},
    ]
    worker._relay_chat_question_replies()

    answers = _board(cp, second.id, "answer")
    assert [(m["body"], m["reply_to"]) for m in answers] == [("eu-west", None)]
    assert answers[0]["metadata"]["chat_ref"] == "slack:T1/C1/300.3"
    assert cp.get_task(second.id).state == TaskState.OPEN.value
    assert cp.get_task(first.id).state == TaskState.NEEDS_INPUT.value
    receipt = slack.posted[-1]
    assert receipt["thread_ts"] == "300.3" and "Recorded as the answer" in receipt["text"]
    assert [e["notification_id"] for e in worker._load_chat_questions()] == [_question_note(cp, first.id)]
