"""When a change lands, the fleet is told — with the tree it landed as.

The merge happens in the hub (``_publish_via_pull_request``), so the hub is the
only party that can announce it truthfully. These tests drive the real
publication path against a real bare git remote and a faked forge that performs
an actual squash merge, then assert on what reached the bus.

``tree_sha`` is checked against the real tree of the merged commit, because it
is the field the whole terminal-event design rests on: the squash mints a new
commit sha at merge time, so a consumer matching on commit identity would miss
every merge this fleet performs. Tree identity survives the squash — which is
why ``native_merge_queue.landing_is_safe`` gates on it too.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane
from tests.test_publication_pull_request import (
    FakeForge,
    build_repo,
    drive_to_approval,
    git,
    install_forge,
)


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _heard(cp, reader_id, event_type):
    return [
        item
        for item in cp.read_agentbus_broadcasts(reader_id, limit=200)
        if item["event_type"] == event_type
    ]


def test_a_landed_merge_is_announced_with_its_tree(cp, tmp_path, monkeypatch):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )
    assert publication.status == "published"

    git(source, "fetch", "origin", "main")
    final = git(source, "ls-remote", "origin", "refs/heads/main").split()[0]
    real_tree = git(source, "rev-parse", "%s^{tree}" % final)

    merged = _heard(cp, reviewer.id, "git.merged")
    assert len(merged) == 1
    event = merged[0]
    assert event["task_id"] == task.id
    assert event["payload"]["tree_sha"] == real_tree
    assert event["payload"]["sha"] == final
    assert event["payload"]["pr_number"] == 101
    assert event["payload"]["url"].endswith("/pull/101")
    # The commit sha is NOT what the reviewed head was: that is the squash, and
    # exactly why the tree is carried.
    assert event["payload"]["sha"] != task_head
    assert event["payload"]["head_sha"] == task_head


def test_the_merge_is_spoken_as_the_hub_so_the_task_owner_hears_it(
    cp, tmp_path, monkeypatch
):
    """The hub performed the merge; attributing it to the agent would both be
    a lie and make that agent skip its own "echo" — the one consumer that must
    not miss it."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)
    worker = [
        agent for agent in cp.list_agents() if agent.name.startswith("worker")
    ][0]

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    merged = _heard(cp, worker.id, "git.merged")
    assert merged and merged[0]["agent_id"] == "hub"
    assert merged[0]["self_emitted"] is False


def test_the_canonical_branch_advance_is_announced_separately(
    cp, tmp_path, monkeypatch
):
    """A peer cares that the trunk moved even when the task is not its own."""
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    final = git(source, "ls-remote", "origin", "refs/heads/main").split()[0]
    advanced = _heard(cp, reviewer.id, "git.canonical_advanced")
    assert len(advanced) == 1
    payload = advanced[0]["payload"]
    assert payload["canonical_branch"] == "main"
    assert payload["from_sha"] == main_head
    assert payload["to_sha"] == final
    assert payload["tree_sha"]


def test_a_merge_that_never_happens_announces_nothing(cp, tmp_path, monkeypatch):
    """Nothing is announced from a path that did not do the thing."""
    remote, source, _main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge", merge_blocked="required checks failed")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(Exception):
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert _heard(cp, reviewer.id, "git.merged") == []
    assert _heard(cp, reviewer.id, "git.canonical_advanced") == []
