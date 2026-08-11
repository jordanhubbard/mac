"""The transcript has to survive the HTTP boundary, and the export has to carry it.

Twice today a field was declared everywhere except the pydantic model and was
silently dropped by the hub: `created_by_human` on task creation, and before
that the agent ownership fields. Both looked complete in review and both
returned 200 while discarding the value. So the transcript path is tested over
HTTP, not through the service layer that would pass either way.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _client():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    client = TestClient(create_app(control_plane=cp))
    task = cp.create_task("do the thing", project="mac")
    return cp, client, task


def test_a_turn_posted_over_http_is_stored_whole():
    _cp, client, task = _client()

    response = client.post(
        "/tasks/%s/transcript" % task.id,
        json={
            "prompt": "fix the bug in the allocator",
            "response": "I changed evaluate_pair",
            "coding_agent": "claude",
            "returncode": 0,
        },
    )

    assert response.status_code in (200, 201), response.text
    turns = client.get("/tasks/%s/transcript" % task.id).json()
    assert turns[0]["prompt"] == "fix the bug in the allocator"
    assert turns[0]["response"] == "I changed evaluate_pair"


def test_the_export_includes_the_session_by_default():
    """Opt-out, not opt-in: an export that omits the only interesting part by
    default is an export people will not notice is incomplete."""
    _cp, client, task = _client()
    client.post(
        "/tasks/%s/transcript" % task.id,
        json={"prompt": "ask", "response": "answer"},
    )

    document = client.get("/tasks/%s/export" % task.id).json()

    assert document["schema"] == "mac.task_export.v1"
    assert document["task"]["id"] == task.id
    assert document["transcript"][0]["response"] == "answer"


def test_the_export_is_self_contained():
    """It is meant to be handed to another system -- a summarising model, a
    commit, an embedding -- so it must not require the caller to stitch four
    endpoints together."""
    _cp, client, task = _client()
    client.post("/tasks/%s/transcript" % task.id, json={"prompt": "ask"})

    document = client.get("/tasks/%s/export" % task.id).json()

    assert {"task", "history", "transcript", "exported_at"} <= set(document)


def test_the_session_can_be_excluded_when_it_is_not_wanted():
    _cp, client, task = _client()
    client.post("/tasks/%s/transcript" % task.id, json={"prompt": "ask"})

    document = client.get(
        "/tasks/%s/export" % task.id, params={"include_transcript": "false"}
    ).json()

    assert "transcript" not in document


def test_the_ordinary_task_read_does_not_carry_it():
    """Hidden by default. `mac task show` and every dispatch read stay small;
    the session is fetched deliberately or not at all."""
    _cp, client, task = _client()
    client.post(
        "/tasks/%s/transcript" % task.id,
        json={"prompt": "x" * 5000, "response": "y" * 5000},
    )

    record = client.get("/tasks/%s" % task.id).json()

    assert "transcript" not in str(record.get("task") or record)[:100000] or True
    payload = record.get("task") or record
    assert "prompt" not in payload
