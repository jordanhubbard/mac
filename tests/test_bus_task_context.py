"""What a task is told about the fleet before it starts, and what it is not.

The broadcast channel was write-only in practice: workers announced, nothing
read back. This is the read side's contract.

What these tests pin:

* relevance is the stated minimum — same task, same repository, same project,
  or an event naming a branch/tip this task builds on — and nothing else gets
  in, because a context an agent learns to ignore is worse than none;
* the agent's OWN echo is excluded (``self_emitted``);
* the bound is enforced and truncation is VISIBLE, both in the structure and
  in the rendered prompt text. This codebase refuses silent truncation
  elsewhere for the same reason: a consumer that cannot tell a clipped context
  from a complete one will eventually trust a clipped one;
* the three questions that have to be answerable before work starts are
  answered: has this task already been published, has the canonical tip moved,
  is a peer holding something related.
"""

from __future__ import annotations

from mac.bus_task_context import (
    BUS_TASK_CONTEXT_EVENT_BOUND,
    already_published,
    build_bus_task_context,
    canonical_moved,
    context_event_bound,
    relevance,
    render_bus_context_section,
    task_focus,
)

FOCUS = {
    "task_id": "task_mine",
    "project": "mac",
    "repository": "git@github.com:acme/widgets.git",
    "branch": "mac/agent/task_mine",
    "canonical_branch": "main",
    "base_sha": "base000",
}


def _event(sequence, event_type, **kw):
    payload = kw.pop("payload", {})
    event = {
        "sequence": sequence,
        "event_type": event_type,
        "agent_id": kw.pop("agent_id", "peer-1"),
        "task_id": kw.pop("task_id", ""),
        "project": kw.pop("project", ""),
        "payload": payload,
        "self_emitted": kw.pop("self_emitted", False),
    }
    event.update(kw)
    return event


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------


def test_the_same_task_is_relevant():
    assert relevance(_event(1, "git.merged", task_id="task_mine"), FOCUS) == "same task"


def test_the_same_repository_is_relevant():
    event = _event(1, "git.pushed", payload={"repository": FOCUS["repository"]})
    assert relevance(event, FOCUS) == "same repository"


def test_a_branch_this_task_builds_on_is_relevant():
    event = _event(1, "git.canonical_advanced", payload={"canonical_branch": "main"})
    assert relevance(event, FOCUS) == "names a branch this task builds on"


def test_the_tip_this_task_was_cut_from_is_relevant():
    event = _event(1, "git.pushed", payload={"sha": "base000"})
    assert relevance(event, FOCUS) == "names the tip this task was cut from"


def test_an_unrelated_event_is_not_relevant():
    event = _event(1, "git.pushed", project="other", payload={"branch": "unrelated"})
    assert relevance(event, FOCUS) == ""


def test_the_agents_own_echo_is_excluded():
    """Reasoning about your own announcement teaches you nothing."""
    event = _event(1, "git.pushed", task_id="task_mine", self_emitted=True)
    assert relevance(event, FOCUS) == ""

    context = build_bus_task_context([event], FOCUS)
    assert context["events"] == []


# ---------------------------------------------------------------------------
# The bound, and saying so
# ---------------------------------------------------------------------------


def test_the_bound_is_enforced_and_truncation_is_reported():
    events = [
        _event(index, "git.pushed", task_id="task_mine", payload={"sha": "s%d" % index})
        for index in range(1, 121)
    ]

    context = build_bus_task_context(events, FOCUS, bound=50)

    assert context["bound"] == 50
    assert context["relevant"] == 120
    assert context["included"] == 50
    assert context["omitted"] == 70
    assert context["truncated"] is True
    # Newest first: the bound keeps the events closest to now, not the oldest.
    assert [item["sequence"] for item in context["events"][:3]] == [120, 119, 118]


def test_truncation_is_visible_in_the_prompt_text():
    events = [_event(index, "git.pushed", task_id="task_mine") for index in range(1, 121)]

    rendered = render_bus_context_section(build_bus_task_context(events, FOCUS, bound=50))

    assert "TRUNCATED" in rendered
    assert "70 further relevant events were omitted" in rendered


def test_an_untruncated_context_does_not_claim_to_be_truncated():
    events = [_event(index, "git.pushed", task_id="task_mine") for index in range(1, 5)]

    context = build_bus_task_context(events, FOCUS, bound=50)

    assert context["truncated"] is False
    assert "TRUNCATED" not in render_bus_context_section(context)


def test_the_bound_is_configurable_but_never_unbounded(monkeypatch):
    assert context_event_bound({}) == BUS_TASK_CONTEXT_EVENT_BOUND
    assert context_event_bound({"MAC_BUS_TASK_CONTEXT_EVENTS": "10"}) == 10
    assert context_event_bound({"MAC_BUS_TASK_CONTEXT_EVENTS": "100000"}) == 200
    assert context_event_bound({"MAC_BUS_TASK_CONTEXT_EVENTS": "0"}) == 1
    assert (
        context_event_bound({"MAC_BUS_TASK_CONTEXT_EVENTS": "nonsense"})
        == BUS_TASK_CONTEXT_EVENT_BOUND
    )


# ---------------------------------------------------------------------------
# The three questions
# ---------------------------------------------------------------------------


def test_it_answers_whether_this_task_has_already_been_published():
    events = [
        _event(
            9,
            "git.merged",
            task_id="task_mine",
            payload={
                "branch": FOCUS["branch"],
                "canonical_branch": "main",
                "sha": "squashed1",
                "tree_sha": "tree-abc",
                "pr_number": 461,
                "url": "https://forge.invalid/pull/461",
            },
        )
    ]

    landed = already_published(build_bus_task_context(events, FOCUS))

    assert landed is not None
    # Tree identity, because the commit sha above was minted by the squash and
    # matches nothing the task ever held.
    assert landed["tree_sha"] == "tree-abc"
    assert landed["pr_number"] == 461


def test_a_merge_of_someone_elses_task_is_not_my_work_landing():
    events = [_event(9, "git.merged", task_id="task_theirs", payload={"canonical_branch": "main"})]

    context = build_bus_task_context(events, FOCUS)

    assert already_published(context) is None
    # ...but it IS a canonical advance, which is a different fact about it.
    assert context["events"], "a peer's merge onto my base branch is still relevant"
    siblings = context["signals"]["sibling_landings"]
    assert siblings
    assert siblings[0]["task_id"] == "task_theirs"
    rendered = render_bus_context_section(context)
    assert "RECENT LANDINGS" in rendered
    assert "ALREADY LANDED" not in rendered


def test_it_answers_whether_the_canonical_tip_moved():
    events = [
        _event(
            9,
            "git.canonical_advanced",
            payload={
                "canonical_branch": "main",
                "from_sha": "base000",
                "to_sha": "newtip1",
                "tree_sha": "tree-new",
            },
        )
    ]

    moved = canonical_moved(build_bus_task_context(events, FOCUS))

    assert moved is not None
    assert moved["to_sha"] == "newtip1"
    assert moved["base_sha_at_prepare"] == "base000"
    assert "BASE MOVED" in render_bus_context_section(build_bus_task_context(events, FOCUS))


def test_an_advance_to_the_tip_i_already_have_is_not_a_move():
    events = [
        _event(
            9,
            "git.canonical_advanced",
            payload={"canonical_branch": "main", "to_sha": "base000"},
        )
    ]

    assert canonical_moved(build_bus_task_context(events, FOCUS)) is None


def test_it_answers_whether_a_peer_holds_something_related():
    events = [
        _event(
            9,
            "git.worktree_added",
            agent_id="peer-7",
            payload={"repository": FOCUS["repository"], "branch": "peer/branch"},
        )
    ]

    context = build_bus_task_context(events, FOCUS)

    peers = context["signals"]["peer_activity"]
    assert peers and peers[0]["agent_id"] == "peer-7"
    assert "PEERS ACTIVE" in render_bus_context_section(context)


# ---------------------------------------------------------------------------
# Focus derivation
# ---------------------------------------------------------------------------


def test_focus_is_derived_from_the_task_and_the_prepared_worktree():
    task = {
        "id": "task_mine",
        "project": "mac",
        "metadata": {
            "origin": {
                "repository_contract": {
                    "canonical_branch": "trunk",
                    "canonical_remote": "git@github.com:acme/widgets.git",
                }
            }
        },
    }
    repository_context = {
        "repository_branch": "mac/agent/task_mine",
        "repository_base_sha": "base000",
        "repository_canonical_branch": "main",
        "repository_canonical_remote": "git@github.com:acme/widgets.git",
    }

    focus = task_focus(task, repository_context)

    assert focus["task_id"] == "task_mine"
    assert focus["project"] == "mac"
    assert focus["canonical_branch"] == "main"
    assert focus["base_sha"] == "base000"
    assert focus["repository"] == "git@github.com:acme/widgets.git"


def test_a_task_with_no_bus_traffic_renders_nothing():
    assert render_bus_context_section(build_bus_task_context([], FOCUS)) == ""
