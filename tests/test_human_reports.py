"""Act, then tell: agents report to people what might concern them.

Agents keep their authority ("do the thing but tell people afterwards", the
owner, 2026-10-10). What they may not do is let a consequential action pass
silently -- their own, or another agent's. On 2026-10-02 an agent created an
active ruleset on jordanhubbard/Aviation and nobody was told; these tests pin
every path a report takes to the humans' Slack channel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from mac import claude_hooks as hooks
from mac import human_reports, repo_admin_watch
from mac.api import _required_scope
from mac.executor_prompt import build_review_prompt, build_task_prompt
from mac.inference_tokens import InferenceTokenLifecycle
from mac.mcp_server import MacTools
from mac.models import ValidationError
from mac.worker import MacWorker, WorkerExecution, _status_update_slack_text
from tests.test_claude_hooks import FakeHub
from tests.test_task_board import _app, _bearer, _plane, _worker_token


def _reports(cp) -> List[Any]:
    return [n for n in cp.list_notifications() if n.event_type == human_reports.REPORT_EVENT]


def _filed_logs(cp) -> List[Any]:
    return [e for e in cp.list_observability(limit=200) if e.name == human_reports.REPORT_LOG]


# -- the hub ------------------------------------------------------------------


def test_a_self_report_becomes_one_slack_notification_and_an_audit_log():
    cp = _plane()
    task = cp.create_task("configure CI")

    filed = cp.file_human_report(
        "created ruleset required-ci on acme/widgets",
        reporter="agent_alpha",
        task_id=task.id,
        why="the task asked for required CI",
        undo="Settings > Rules > required-ci > Delete",
    )

    assert filed["status"] == "filed"
    (note,) = _reports(cp)
    assert note.status == "pending"
    # "hermes" is what lets the notifier deliver it to Slack.
    assert "hermes" in note.channels
    assert note.subject_type == "task" and note.subject_id == task.id
    assert "agent_alpha acted on configure CI" in note.title
    assert "To undo: Settings > Rules > required-ci > Delete" in note.body
    assert "Why: the task asked for required CI" in note.body
    assert note.metadata["dedupe_key"] == filed["dedupe_key"]
    (log,) = _filed_logs(cp)
    assert log.detail["notification_id"] == note.id
    assert log.detail["duplicate"] is False


def test_the_same_report_filed_twice_notifies_people_once():
    cp = _plane()
    task = cp.create_task("configure CI")
    first = cp.file_human_report("force-pushed main", reporter="agent_alpha", task_id=task.id)
    again = cp.file_human_report("force-pushed  MAIN", reporter="agent_alpha", task_id=task.id)

    assert again["status"] == "duplicate"
    assert again["notification_id"] == first["notification_id"]
    assert len(_reports(cp)) == 1
    # Both filings are on the audit trail.
    assert [log.detail["duplicate"] for log in _filed_logs(cp)] == [True, False]


def test_two_reporters_sharing_a_key_notify_once():
    cp = _plane()
    one = cp.file_human_report("ruleset added", reporter="agent_alpha", key="repo-admin:a/b:1")
    two = cp.file_human_report("ruleset added (seen too)", reporter="agent_beta", key="repo-admin:a/b:1")
    assert two["status"] == "duplicate" and two["notification_id"] == one["notification_id"]
    assert len(_reports(cp)) == 1


def test_a_peer_report_names_who_it_is_about():
    cp = _plane()
    mine = cp.create_task("review")
    with pytest.raises(ValidationError, match="must name the agent or task"):
        cp.file_human_report("someone broke main", reporter="agent_alpha", report="peer")

    cp.file_human_report(
        "force-pushed main, dropping 3 commits",
        reporter="agent_alpha",
        task_id=mine.id,
        report="peer",
        about_agent="agent_beta",
        about_task="task_other",
        evidence="main moved from abc123 to def456",
    )

    (note,) = _reports(cp)
    assert "agent_alpha flagged agent_beta" in note.title
    assert note.subject_type == "task" and note.subject_id == "task_other"
    assert "Evidence: main moved from abc123 to def456" in note.body
    assert _filed_logs(cp)[0].level == "warning"


def test_a_report_still_reaches_people_after_the_task_fails():
    cp = _plane()
    task = cp.create_task("doomed")
    cp.post_task_message(
        task.id,
        author_kind="agent",
        author="agent_alpha",
        kind="report",
        body="deleted the stale release branch",
        metadata={"undo": "git push origin <sha>:refs/heads/release"},
    )
    cp.close_task(task.id, "cancelled", "operator", {"reason": "gave up", "disposition": "not_applicable"})

    (note,) = _reports(cp)
    assert note.status == "pending"
    assert note.metadata["task_message_id"]
    assert note.metadata["source"] == "task_board"


def test_a_malformed_board_report_stays_on_the_board_and_is_logged():
    cp = _plane()
    task = cp.create_task("t")
    posted = cp.post_task_message(
        task.id,
        author_kind="agent",
        author="agent_alpha",
        kind="report",
        body="someone did something",
        metadata={"report": "peer"},
    )
    assert posted["kind"] == "report"
    assert _reports(cp) == []
    names = {event.name for event in cp.list_observability(limit=100)}
    assert "task_board.report_notification_failed" in names


def test_a_report_reaches_the_slack_channel_that_subscribes_to_it():
    cp = _plane()
    cp.configure_notifier_channel(
        "reports-slack",
        "slack",
        event_types=["task.question", human_reports.REPORT_EVENT],
        target={"agent_id": "agent_alpha"},
    )
    task = cp.create_task("t")
    cp.file_human_report("rotated the deploy key", reporter="agent_beta", task_id=task.id)

    result = cp.deliver_pending_notifications(limit=10)

    assert result["delivered"] == 1
    (note,) = _reports(cp)
    assert cp.get_notification(note.id).status == "delivered"
    # The worker behind agent_alpha posts it to its Slack home channels.
    text = _status_update_slack_text(note.to_dict())
    assert text.startswith("*Agent report: agent_beta acted on t")
    assert "rotated the deploy key" in text


# -- the API ------------------------------------------------------------------


def test_reports_route_scope_and_who_the_reporter_is():
    assert _required_scope("POST", "/reports") == "task_board"
    cp = _plane()
    mine, other = cp.create_task("mine"), cp.create_task("other")
    client = TestClient(_app(cp))
    token = InferenceTokenLifecycle(cp.store).mint("agent_alpha", task_id=mine.id).token

    filed = client.post("/reports", headers=_bearer(token), json={"body": "changed webhook"})
    assert filed.status_code == 200, filed.text
    assert filed.json()["task_id"] == mine.id
    # A per-task token reports only from its own task.
    refused = client.post(
        "/reports", headers=_bearer(token), json={"body": "x", "task_id": other.id}
    )
    assert refused.status_code == 403

    worker = client.post(
        "/reports",
        headers=_bearer(_worker_token(cp, "agent_beta")),
        json={"body": "saw agent_alpha force-push", "report": "peer", "about_agent": "agent_alpha"},
    )
    assert worker.status_code == 200, worker.text
    reporters = {n.metadata["reporter"] for n in _reports(cp)}
    assert reporters == {"agent_alpha", "agent_beta"}

    assert (
        client.post("/reports", headers=_bearer("static-reader"), json={"body": "x"}).status_code
        == 403
    )
    person = client.post(
        "/reports", headers=_bearer("static-writer"), json={"body": "x", "reporter": "jkh"}
    )
    assert person.status_code == 200, person.text


# -- what agents are told, and their tools --------------------------------------


def test_agents_and_reviewers_are_told_the_rule():
    task = {"id": "task_1", "title": "t", "metadata": {}}
    prompt = build_task_prompt(task)
    assert "Act, then tell." in prompt and "Tell on others too." in prompt
    assert human_reports.WORKSPACE_REPORTS_FILE in prompt
    review = build_review_prompt(task, Path("/w"), {})
    assert "file a peer report" in review
    assert "Act, then tell." in hooks.BOARD_GUIDE


def test_the_board_command_files_a_report(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_AGENT_STATE_DIR", str(tmp_path / "state"))
    hub = FakeHub()
    assert hooks.board_main(["report", "changed branch protection", "--undo", "re-enable it"], hub) == 0
    assert hooks.board_main(["report", "it force-pushed", "--about-agent", "agent_beta"], hub) == 0
    assert hub.posts[0] == {
        "kind": "report",
        "body": "changed branch protection",
        "metadata": {"report": "self", "undo": "re-enable it"},
    }
    assert hub.posts[1]["metadata"] == {"report": "peer", "about_agent": "agent_beta"}


def test_the_mcp_tool_files_a_peer_report_when_it_names_someone():
    calls = []

    class _Plane:
        def file_human_report(self, body, **kwargs):
            calls.append((body, kwargs))
            return {"status": "filed"}

    MacTools(_Plane()).call("mac_report", {"body": "x", "about_task": "task_9"})
    assert calls[0][1]["report"] == "peer"
    assert calls[0][1]["about_task"] == "task_9"
    assert "error" in json.dumps(MacTools(_Plane()).call("mac_report", {})).lower()


def test_workspace_reports_parse_and_skip_bad_lines():
    text = "\n".join(
        [
            json.dumps({"body": "rotated a secret", "undo": "restore from vault"}),
            "not json",
            json.dumps({"body": "peer without subject", "report": "peer"}),
            json.dumps({"what": "closed PR #12", "about_task": "task_x"}),
        ]
    )
    reports = human_reports.parse_workspace_reports(text)
    assert [r["body"] for r in reports] == ["rotated a secret", "closed PR #12"]


# -- the worker -----------------------------------------------------------------


class _Client:
    def __init__(self):
        self.posts: List[Dict[str, Any]] = []

    def post(self, path, payload):
        self.posts.append({"path": path, "payload": payload})
        return {"status": "filed"}


def _worker(tmp_path, client):
    return MacWorker(
        client,  # type: ignore[arg-type]
        "agent_alpha",
        tmp_path,
        lambda _task, _directory: WorkerExecution(0, "unused"),
    )


def _snapshot(rulesets):
    return {
        "repository": "acme/widgets",
        "sections": {
            "settings": {"default_branch": "main"},
            "rulesets": rulesets,
            "webhooks": {},
            "branch_protection": {"protected": False},
        },
    }


def test_the_worker_files_workspace_reports_and_detected_admin_changes(tmp_path, monkeypatch):
    client = _Client()
    worker = _worker(tmp_path, client)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / human_reports.WORKSPACE_REPORTS_FILE).write_text(
        json.dumps({"body": "deleted branch old-release", "undo": "push it back"}) + "\n",
        encoding="utf-8",
    )
    task = {
        "id": "task_1",
        "metadata": {
            "execution_contract": {
                "repository_contract": {
                    "canonical_remote_url": "https://github.com/acme/widgets.git"
                }
            }
        },
    }
    before = _snapshot({})
    after = _snapshot({"24407548": {"name": "required-ci", "enforcement": "active"}})
    monkeypatch.setattr(repo_admin_watch, "snapshot", lambda url, **_: after)

    worker._tell_humans_after_attempt(task, task_dir, before)

    filed = [post["payload"] for post in client.posts if post["path"] == "/reports"]
    assert len(filed) == 2
    workspace, detected = filed
    assert workspace["task_id"] == "task_1"
    assert workspace["body"] == "deleted branch old-release"
    assert "ruleset 24407548 added (required-ci), enforcement active" in detected["body"]
    assert detected["key"].startswith("repo-admin:acme/widgets:")


def test_the_worker_files_nothing_when_nothing_happened(tmp_path, monkeypatch):
    client = _Client()
    worker = _worker(tmp_path, client)
    monkeypatch.setattr(repo_admin_watch, "snapshot", lambda url, **_: _snapshot({}))
    worker._tell_humans_after_attempt({"id": "t", "metadata": {}}, tmp_path, _snapshot({}))
    assert [post for post in client.posts if post["path"] == "/reports"] == []


# -- repository administration snapshots ----------------------------------------


def _fake_github(state):
    def get_json(url, headers):
        path = url.split("/repos/acme/widgets", 1)[1]
        if path in state:
            value = state[path]
            if isinstance(value, Exception):
                raise value
            return value
        raise RuntimeError("HTTP Error 404: Not Found")

    return get_json


def test_snapshot_and_diff_report_what_a_person_cares_about():
    base = {
        "": {"default_branch": "main", "allow_squash_merge": True, "pushed_at": "t1"},
        "/rulesets?includes_parents=false&per_page=100": [],
        "/hooks?per_page=100": [
            {"id": 7, "active": True, "events": ["push"], "config": {"url": "https://ci", "secret": "********"}}
        ],
    }
    before = repo_admin_watch.snapshot(
        "https://github.com/acme/widgets.git", token="t", get_json=_fake_github(base)
    )
    changed = dict(base)
    changed[""] = {"default_branch": "main", "allow_squash_merge": False, "pushed_at": "t2"}
    changed["/rulesets?includes_parents=false&per_page=100"] = [{"id": 5}]
    changed["/rulesets/5"] = {"name": "required-ci", "enforcement": "active", "rules": [], "updated_at": "x"}
    after = repo_admin_watch.snapshot(
        "https://github.com/acme/widgets.git", token="t", get_json=_fake_github(changed)
    )

    changes = repo_admin_watch.diff(before, after)

    # pushed_at moves on every push and is not compared; the secret is never kept.
    assert {(c["section"], c["item"], c["change"]) for c in changes} == {
        ("settings", "allow_squash_merge", "changed"),
        ("rulesets", "ruleset 5", "added"),
    }
    assert "secret" not in json.dumps(before)
    assert before["sections"]["branch_protection"] == {"protected": False}


def test_an_unreadable_section_is_never_reported_as_a_change():
    before = _snapshot({"1": {"name": "a"}})
    after = _snapshot({})
    after["sections"]["rulesets"] = {"unreadable": "HTTP Error 403"}
    assert repo_admin_watch.diff(before, after) == []


def test_only_github_repositories_are_watched():
    assert repo_admin_watch.snapshot("file:///tmp/repo.git", token="t") is None
    assert repo_admin_watch.snapshot("https://github.com/acme/widgets.git", token="") is None
