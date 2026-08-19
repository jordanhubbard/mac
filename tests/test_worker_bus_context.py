"""A worker reads its messages before it starts work — and acts on them.

Publishing without a consumer is decoration. These tests pin the consumer:

* the worker drains the feed BEFORE the coding agent is handed the task, and
  attaches the relevant part to the task record the agent reads;
* it hears ``git.merged`` for its OWN task and does not open a second pull
  request — the failure that produced #405, #437, #442, #443 and #445-448;
* it learns from ``git.canonical_advanced`` that the tip it was cut from has
  moved, before it starts rather than at push time;
* its own echo is excluded, and the bound is enforced with the truncation
  stated out loud.

The feed is read UNFILTERED and filtered locally, and that is deliberate: the
hub's filtered read scans a bounded number of pages and returns ``[]`` on a
miss without advancing the caller's cursor, so a rare event can sit permanently
beyond the window (tracked as task_8cc72ba4). One of these tests pins exactly
that: a rare terminal event buried under a page of chatter is still found.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from mac import worker
from mac.bus_task_context import canonical_moved


class _Client:
    """Serves a scripted broadcast feed and records posts."""

    def __init__(self, events=None):
        self.posts = []
        self.events = list(events or [])
        self.reads = 0

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {}

    def get(self, path):
        parsed = urlparse(path)
        if parsed.path.endswith("/agentbus/broadcast"):
            self.reads += 1
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            limit = int(query.get("limit", ["200"])[0])
            return [e for e in self.events if int(e["sequence"]) > after][:limit]
        return {}


def _worker(tmp_path, client):
    return worker.MacWorker(
        client,
        "agent-1",
        tmp_path,
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def _task():
    return {
        "id": "task_mine",
        "title": "do the thing",
        "project": "mac",
        "metadata": {},
    }


REPO_CONTEXT = {
    "repository_branch": "mac/agent-1/task_mine",
    "repository_base_sha": "base000",
    "repository_canonical_branch": "main",
    "repository_canonical_remote": "git@github.com:acme/widgets.git",
}


def _event(sequence, event_type, **kw):
    return {
        "sequence": sequence,
        "event_type": event_type,
        "agent_id": kw.pop("agent_id", "peer-1"),
        "task_id": kw.pop("task_id", ""),
        "project": kw.pop("project", ""),
        "payload": kw.pop("payload", {}),
        "self_emitted": kw.pop("self_emitted", False),
    }


def _merged(sequence, task_id="task_mine"):
    return _event(
        sequence,
        "git.merged",
        agent_id="hub",
        task_id=task_id,
        project="mac",
        payload={
            "branch": "mac/agent-1/task_mine",
            "canonical_branch": "main",
            "sha": "squashed1",
            "tree_sha": "tree-abc",
            "pr_number": 461,
            "url": "https://forge.invalid/pull/461",
        },
    )


# ---------------------------------------------------------------------------
# The read happens before the work
# ---------------------------------------------------------------------------


def test_the_task_the_agent_receives_carries_the_bus_context(tmp_path):
    client = _Client([_merged(11)])
    instance = _worker(tmp_path, client)
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    assert context["events"], "the worker read nothing"
    # It reached the record the coding agent is handed...
    assert task["metadata"]["runtime"]["bus_context"]["events"]
    # ...and the durable copy an operator can audit afterwards.
    on_disk = json.loads((task_dir / "bus-context.json").read_text(encoding="utf-8"))
    assert on_disk["signals"]["already_published"]["tree_sha"] == "tree-abc"


def test_the_prompt_the_agent_is_given_names_what_it_must_not_redo(tmp_path):
    from mac.executor_prompt import build_task_prompt

    client = _Client([_merged(11)])
    instance = _worker(tmp_path, client)
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    prompt = build_task_prompt(task, task_dir / "task.json")

    assert "AgentBus context" in prompt
    assert "ALREADY LANDED" in prompt
    assert "do NOT open a second pull request" in prompt


def test_the_workers_own_echo_is_not_fed_back_to_it(tmp_path):
    client = _Client(
        [
            _event(
                11,
                "git.pushed",
                agent_id="agent-1",
                task_id="task_mine",
                self_emitted=True,
            )
        ]
    )
    instance = _worker(tmp_path, client)
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    assert context["events"] == []
    assert "bus_context" not in task["metadata"].get("runtime", {})


def test_a_rare_terminal_event_under_a_page_of_chatter_is_still_found(tmp_path):
    """The unfiltered read is the point: a filtered one could never reach it.

    See task_8cc72ba4 — the hub's filtered read scans a bounded number of pages
    and returns nothing on a miss, leaving the caller's cursor where it was.
    """
    events = [
        _event(index, "task.progress", agent_id="peer-%d" % index, project="other")
        for index in range(1, 400)
    ]
    events.append(_merged(400))
    client = _Client(events)
    instance = _worker(tmp_path, client)
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    assert context["signals"]["already_published"] is not None


def test_the_context_is_bounded_and_says_when_it_clipped(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_BUS_TASK_CONTEXT_EVENTS", "5")
    client = _Client(
        [
            _event(index, "git.pushed", task_id="task_mine", payload={"sha": "s%d" % index})
            for index in range(1, 40)
        ]
    )
    instance = _worker(tmp_path, client)
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    assert context["included"] == 5
    assert context["omitted"] == 34
    assert context["truncated"] is True


# ---------------------------------------------------------------------------
# ...and it acts on what it read
# ---------------------------------------------------------------------------


def test_a_worker_that_hears_its_own_merge_does_not_open_a_second_pr(
    tmp_path, monkeypatch
):
    client = _Client([_merged(11)])
    instance = _worker(tmp_path, client)
    monkeypatch.setattr(
        worker,
        "agent_pull_request",
        lambda *_a, **_k: pytest.fail(
            "opened a duplicate pull request for already-merged work"
        ),
    )

    outcome = instance._open_task_pull_request(
        _task(),
        object(),
        branch="mac/agent-1/task_mine",
        head_sha="head111",
        base_sha="base000",
    )

    assert outcome["opened"] is False
    assert "already merged" in outcome["reason"]
    assert outcome["already_merged"]["tree_sha"] == "tree-abc"


def test_a_merge_of_someone_elses_task_does_not_suppress_this_ones_pr(
    tmp_path, monkeypatch
):
    """The guard must be about THIS task, or it becomes a way to lose work."""
    client = _Client([_merged(11, task_id="task_theirs")])
    instance = _worker(tmp_path, client)
    monkeypatch.setattr(
        worker,
        "agent_pull_request",
        lambda *_a, **_k: {
            "opened": True,
            "number": 999,
            "url": "https://forge.invalid/pull/999",
            "base": "main",
            "head": "mac/agent-1/task_mine",
        },
    )

    outcome = instance._open_task_pull_request(
        _task(),
        object(),
        branch="mac/agent-1/task_mine",
        head_sha="head111",
        base_sha="base000",
    )

    assert outcome["opened"] is True
    announced = [
        payload
        for path, payload in client.posts
        if path == "/agentbus/broadcast" and payload["event_type"] == "git.pr_opened"
    ]
    assert announced and announced[0]["payload"]["pr_number"] == 999
    assert announced[0]["task_id"] == "task_mine"


def test_the_worker_learns_the_canonical_tip_moved_under_it(tmp_path):
    client = _Client(
        [
            _event(
                12,
                "git.canonical_advanced",
                agent_id="hub",
                payload={
                    "canonical_branch": "main",
                    "from_sha": "base000",
                    "to_sha": "newtip9",
                    "tree_sha": "tree-new",
                },
            )
        ]
    )
    instance = _worker(tmp_path, client)
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    moved = canonical_moved(context)
    assert moved is not None
    assert moved["to_sha"] == "newtip9"
    assert moved["base_sha_at_prepare"] == "base000"


def test_an_unreachable_hub_does_not_break_task_preparation(tmp_path):
    class _Broken(_Client):
        def get(self, path):
            raise RuntimeError("hub unreachable")

    instance = _worker(tmp_path, _Broken())
    task = _task()
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = instance._attach_bus_task_context(task, task_dir, REPO_CONTEXT)

    assert context["events"] == []


# ---------------------------------------------------------------------------
# The finalizer makes the same decision, from the context it was handed
# ---------------------------------------------------------------------------


def test_the_finalizer_refuses_a_duplicate_from_the_attached_context(monkeypatch):
    from mac import executor_finalizer

    task = _task()
    task["metadata"]["runtime"] = {
        "bus_context": {
            "signals": {
                "already_published": {
                    "tree_sha": "tree-abc",
                    "pr_number": 461,
                    "url": "https://forge.invalid/pull/461",
                }
            }
        }
    }
    monkeypatch.setattr(
        executor_finalizer,
        "agent_pull_request",
        lambda *_a, **_k: pytest.fail("finalizer opened a duplicate pull request"),
    )

    outcome = executor_finalizer.open_task_pull_request(
        task, object(), task_id="task_mine", head_sha="head111", base_sha="base000"
    )

    assert outcome["opened"] is False
    assert outcome["already_merged"]["pr_number"] == 461


def test_the_finalizer_announces_the_pull_request_it_did_open(monkeypatch):
    from mac import executor_finalizer

    announced = []
    monkeypatch.setattr(
        executor_finalizer,
        "agent_pull_request",
        lambda *_a, **_k: {
            "opened": True,
            "number": 12,
            "url": "https://forge.invalid/pull/12",
            "base": "main",
            "head": "mac/agent-1/task_mine",
        },
    )
    monkeypatch.setattr(
        executor_finalizer,
        "broadcast_event",
        lambda event_type, **kw: announced.append((event_type, kw)) or True,
    )

    executor_finalizer.open_task_pull_request(
        _task(), object(), task_id="task_mine", head_sha="head111", base_sha="base000"
    )

    assert announced and announced[0][0] == "git.pr_opened"
    assert announced[0][1]["payload"]["pr_number"] == 12
    assert announced[0][1]["task_id"] == "task_mine"
