"""An agent's question reaches people, and the answer reaches the agent.

A non-blocking question is a board post the agent keeps working past. A
blocking one parks the task in NEEDS_INPUT once the agent stops; answering
it on the board returns the task to the queue, and a question asked with a
default and a deadline answers itself when the deadline passes.
"""

from __future__ import annotations

import json
from pathlib import Path

from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on
from mac.worker import _blocking_question_marker


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="task-question-test-key-with-32-bytes",
    )
    machine = cp.register_machine("host", machine_id="machine_q", labels={})
    cp.register_agent(machine.id, "alpha", ["python"], resources={}, agent_id="agent_alpha")
    return cp


def _notifications(cp: ControlPlane, event_type: str):
    return cp.store.query_all(
        "SELECT * FROM operator_notifications WHERE event_type = ?", (event_type,)
    )


def test_a_question_goes_to_the_notification_outbox_with_how_to_answer():
    cp = _plane()
    task = cp.create_task("Pick a region")
    posted = cp.post_task_message(
        task.id,
        author_kind="agent",
        author="agent_alpha",
        kind="question",
        body="Which region?",
        metadata={"options": ["us", "eu"], "default": "us"},
    )
    rows = _notifications(cp, "task.question")
    assert len(rows) == 1
    body = rows[0]["body"]
    assert "Which region?" in body and "us, eu" in body
    assert "mac task say %s --answer %s" % (task.id, posted["id"]) in body
    assert rows[0]["title"].startswith("Question on")


def test_a_blocking_question_is_announced_once_when_the_task_parks():
    cp = _plane()
    task = cp.create_task("Pick a region")
    _, lease = cp.claim_task(task.id, "agent_alpha")
    cp.start_task(task.id, "agent_alpha", lease_id=lease.id)
    question = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="Which region?",
        metadata={"blocking": True},
    )
    assert _notifications(cp, "task.question") == []
    cp.transition_task(
        task.id,
        "needs_input",
        "agent_alpha",
        {"questions": [{"question": "Which region?"}], "board_message_id": question["id"]},
        lease_id=lease.id,
    )
    rows = _notifications(cp, "task.question")
    assert len(rows) == 1 and "Which region?" in rows[0]["body"]


def test_answering_the_parked_question_returns_the_task_to_the_queue():
    cp = _plane()
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
    assert cp.get_task(task.id).state == "needs_input"
    cp.post_task_message(
        task.id, author_kind="human", author="jkh", kind="answer", body="eu", reply_to=question["id"]
    )
    resumed = cp.get_task(task.id)
    assert resumed.state == "open"
    assert resumed.metadata["needs_input_history"][-1]["answer"] == "eu"


def test_an_answer_to_a_task_that_is_not_parked_changes_nothing():
    cp = _plane()
    task = cp.create_task("still running")
    question = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="rename it?"
    )
    cp.post_task_message(
        task.id, author_kind="human", author="jkh", kind="answer", body="no", reply_to=question["id"]
    )
    assert cp.get_task(task.id).state == "open"


def test_an_expired_question_with_a_default_answers_itself_and_one_without_is_flagged_once():
    cp = _plane()
    task = cp.create_task("defaults")
    with_default = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="tabs or spaces?",
        metadata={"default": "spaces", "expires_at": "2026-01-01T00:00:00Z"},
    )
    without = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="ship it?",
        metadata={"expires_at": "2026-01-01T00:00:00Z"},
    )
    later = cp.post_task_message(
        task.id, author_kind="agent", author="agent_alpha", kind="question", body="later?",
        metadata={"default": "x", "expires_at": "2099-01-01T00:00:00Z"},
    )
    first = cp.expire_task_questions(now="2026-06-01T00:00:00+00:00")
    assert first["defaults_applied"] == [with_default["id"]]
    assert first["overdue"] == [without["id"]]
    second = cp.expire_task_questions(now="2026-06-01T00:00:00+00:00")
    assert second == {**second, "defaults_applied": [], "overdue": []}
    board = cp.list_task_messages(task.id)["messages"]
    answers = [m for m in board if m["kind"] == "answer"]
    assert [(a["reply_to"], a["author_kind"]) for a in answers] == [(with_default["id"], "hub")]
    assert "spaces" in answers[0]["body"]
    assert later["id"] not in {m.get("reply_to") for m in board}


def test_the_worker_parks_only_on_a_well_formed_host_marker(tmp_path: Path):
    assert _blocking_question_marker(tmp_path) is None
    (tmp_path / "needs-input.json").write_text("not json")
    assert _blocking_question_marker(tmp_path) is None
    (tmp_path / "needs-input.json").write_text(json.dumps({"questions": [{"question": "q?"}]}))
    assert _blocking_question_marker(tmp_path) == {"questions": [{"question": "q?"}]}


def test_the_sandbox_cannot_plant_the_parking_marker():
    from mac.executor_sandbox import _sandbox_download_path_is_host_control

    assert _sandbox_download_path_is_host_control(Path("needs-input.json"))
