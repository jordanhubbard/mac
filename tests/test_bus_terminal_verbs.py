"""The bus can say a change FINISHED, and what tree it finished as.

Every git verb the broadcast vocabulary shipped with describes work starting
or colliding: ``git.branch_created``, ``git.worktree_added``, ``git.pushed``,
``git.merge_conflict``, ``git.force_push``. Nothing said work landed, and
nothing said the canonical tip moved. The bill for that arrived as eight
duplicate pull requests (#405, #437, #442, #443, #445-448) opened against work
that was already merged: no task could learn its own change was in.

What these tests pin:

* the three terminal verbs exist and are publishable;
* ``git.merged`` carries ``tree_sha``, and the hub takes a ledger fact off it
  without being told a second time;
* two DISTINCT merges inside the coalesce window are not collapsed into one --
  coalescing may suppress noise, never a distinct terminal fact. This is the
  same trap #454 hit with policy events, whose payloads carry no git-shaped
  fields;
* the payload cap keeps the identifying fields, ``tree_sha`` among them,
  because a truncated terminal event that lost its tree identifies nothing.
"""

from __future__ import annotations

import pytest

from mac.agentbus_broadcast import (
    BROADCAST_COALESCE_FIELDS,
    BROADCAST_EVENT_TYPE_SET,
    BROADCAST_MAX_VALUE_CHARS,
    LEDGER_DERIVING_EVENT_TYPES,
)
from mac.services import ControlPlane


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    return cp, cp.register_agent(machine.id, "worker-1"), cp.register_agent(machine.id, "worker-2")


def test_the_terminal_verbs_are_in_the_vocabulary():
    for verb in ("git.pr_opened", "git.merged", "git.canonical_advanced"):
        assert verb in BROADCAST_EVENT_TYPE_SET


def test_tree_sha_is_a_coalesce_field():
    """Squash merges mint new commit shas; the tree is what identifies them."""
    assert "tree_sha" in BROADCAST_COALESCE_FIELDS
    assert "pr_number" in BROADCAST_COALESCE_FIELDS


def test_a_merge_is_announced_with_its_tree(fleet):
    cp, a, b = fleet
    task = cp.create_task("land something", project="mac")

    envelope = cp.publish_agentbus_broadcast(
        a.id,
        "git.merged",
        task_id=task.id,
        project="mac",
        payload={
            "branch": "certifier/x",
            "canonical_branch": "main",
            "sha": "cafe1234",
            "tree_sha": "7ee5ha1",
            "pr_number": 461,
        },
    )

    assert envelope["accepted"] is True
    heard = cp.read_agentbus_broadcasts(b.id, limit=10)
    merged = [item for item in heard if item["event_type"] == "git.merged"]
    assert merged and merged[0]["payload"]["tree_sha"] == "7ee5ha1"
    assert merged[0]["self_emitted"] is False


def test_the_hub_records_a_merge_in_the_ledger_without_being_told_twice(fleet):
    cp, a, _b = fleet
    task = cp.create_task("land something", project="mac")

    envelope = cp.publish_agentbus_broadcast(
        a.id,
        "git.merged",
        task_id=task.id,
        payload={"branch": "certifier/x", "sha": "cafe", "tree_sha": "t1", "pr_number": 7},
    )

    assert envelope["derived"] == ["bus.observed.git.merged"]
    assert "git.merged" in LEDGER_DERIVING_EVENT_TYPES
    derived = [
        event for event in cp.task_history(task.id) if event.event_type == "bus.observed.git.merged"
    ]
    assert len(derived) == 1
    assert derived[0].detail["tree_sha"] == "t1"
    assert derived[0].detail["pr_number"] == 7


def test_two_distinct_merges_in_the_window_are_both_announced(fleet):
    """Coalescing must not swallow a second, different landing.

    Two squash merges onto the same branch inside the coalesce window are
    different facts. They agree on project, task-lessness, branch and
    canonical_branch; what distinguishes them is the resulting tree.
    """
    cp, a, b = fleet

    first = cp.publish_agentbus_broadcast(
        a.id,
        "git.canonical_advanced",
        project="mac",
        payload={
            "repository": "org/mac",
            "canonical_branch": "main",
            "sha": "aaa111",
            "to_sha": "aaa111",
            "tree_sha": "tree-a",
        },
    )
    second = cp.publish_agentbus_broadcast(
        a.id,
        "git.canonical_advanced",
        project="mac",
        payload={
            "repository": "org/mac",
            "canonical_branch": "main",
            "sha": "bbb222",
            "to_sha": "bbb222",
            "tree_sha": "tree-b",
        },
    )

    assert first["accepted"] is True
    assert second["accepted"] is True, second.get("reason")
    heard = [
        item
        for item in cp.read_agentbus_broadcasts(b.id, limit=50)
        if item["event_type"] == "git.canonical_advanced"
    ]
    assert {item["payload"]["tree_sha"] for item in heard} == {"tree-a", "tree-b"}


def test_a_repeat_of_the_same_merge_is_still_coalesced(fleet):
    """The bound still works: the same landing announced twice is one row."""
    cp, a, _b = fleet
    payload = {
        "repository": "org/mac",
        "canonical_branch": "main",
        "sha": "aaa111",
        "to_sha": "aaa111",
        "tree_sha": "tree-a",
    }

    cp.publish_agentbus_broadcast(a.id, "git.canonical_advanced", payload=dict(payload))
    repeat = cp.publish_agentbus_broadcast(a.id, "git.canonical_advanced", payload=dict(payload))

    assert repeat["accepted"] is False
    assert repeat["reason"] == "coalesced"


def test_an_oversized_merge_payload_keeps_its_tree_and_says_it_was_clipped(fleet):
    """A terminal event that lost its tree identifies nothing."""
    cp, a, _b = fleet

    envelope = cp.publish_agentbus_broadcast(
        a.id,
        "git.merged",
        payload={
            "branch": "certifier/x",
            "tree_sha": "tree-a",
            "pr_number": 12,
            **{"filler_%d" % index: "x" * BROADCAST_MAX_VALUE_CHARS for index in range(20)},
        },
    )

    body = envelope["payload"]
    assert body["truncated"] is True
    assert body["tree_sha"] == "tree-a"
    assert body["pr_number"] == 12
