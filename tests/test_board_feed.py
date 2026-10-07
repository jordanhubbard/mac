"""The console's cross-task board feed: every agent in one place."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on


def _plane() -> ControlPlane:
    return ControlPlane(
        store_on(ephemeral_dsn(), initialize=True), secret_key="board-feed-test-key-with-32-bytes-x"
    )


def test_feed_spans_tasks_hides_activity_and_pins_unanswered_questions():
    cp = _plane()
    one, two = cp.create_task("Fix parser", project="p"), cp.create_task("Add docs")
    post = cp.post_task_message
    post(one.id, author_kind="agent", author="a1", kind="activity", body="Bash: make")
    post(one.id, author_kind="agent", author="a1", kind="status", body="found it")
    asked = post(two.id, author_kind="agent", author="a2", kind="question", body="which tone?")
    answered = post(one.id, author_kind="agent", author="a1", kind="question", body="tabs?")
    post(one.id, author_kind="human", author="jkh", kind="answer", body="spaces", reply_to=answered["id"])

    feed = cp.list_recent_board()
    assert [m["body"] for m in feed["messages"]] == ["found it", "which tone?", "tabs?", "spaces"]
    assert feed["messages"][0]["task_title"] == "Fix parser"
    assert feed["messages"][0]["task_project"] == "p"
    assert [q["id"] for q in feed["open_questions"]] == [asked["id"]]
    assert cp.list_recent_board(after=feed["cursor"])["messages"] == []
    with_activity = cp.list_recent_board(include_activity=True)
    assert with_activity["messages"][0]["kind"] == "activity"


def test_feed_is_a_read_scoped_dashboard_route():
    cp = _plane()
    task = cp.create_task("t")
    cp.post_task_message(task.id, author_kind="agent", author="a", kind="status", body="hi")
    client = TestClient(create_app(control_plane=cp, auth_tokens={"reader": {"scopes": ["read"]}}))
    response = client.get("/dashboard/board", headers={"Authorization": "Bearer reader"})
    assert response.status_code == 200
    assert response.json()["messages"][0]["body"] == "hi"
    assert client.get("/dashboard/board").status_code in (401, 403)
